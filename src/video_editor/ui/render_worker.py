"""Background render execution: Qt signals + a cancellable worker.

The GUI thread never runs FFmpeg. ``RenderThread`` is a ``QThread`` subclass
whose ``run()`` executes the render, because a plain ``QThread`` whose
``started`` signal is connected to a moved-in worker would sit in an event loop
that this render does not need (verified behaviour of PySide6). Progress,
completion, failure and cancellation arrive as **queued** signals, delivered by
the GUI thread's event loop. The plan is built from an immutable project
snapshot, so the user can keep editing while a render runs.

Cancellation is an ordinary ``threading.Event``: no Qt event loop is needed on
the render thread, and FFmpeg is terminated rather than merely abandoned.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QMetaObject,
    QObject,
    Qt,
    QThread,
    Signal,
    Slot,
)

from ..media.framegrab import (
    StillError,
    extract_composition_still,
    extract_source_still,
)
from ..media.frameindex import AnswerStatus, FrameIndex
from ..render.exporter import (
    RenderCancelled,
    RenderError,
    RenderRequest,
    RenderResult,
    render,
)
from ..render.graph import RenderConfigError
from ..render.plan import RenderPlanError
from ..render.preview import PreviewJob

LOG = logging.getLogger(__name__)


class RenderWorker(QObject):
    """Runs one export; ``RenderThread.run`` calls it on the render thread."""

    progress = Signal(float)  # 0.0 .. 1.0
    finished = Signal(object)  # RenderResult
    failed = Signal(str)
    cancelled = Signal()
    done = Signal()  # always emitted last

    def __init__(self, request: RenderRequest) -> None:
        super().__init__()
        self.request = request
        self._cancel = threading.Event()
        self._last_reported = -1.0

    def cancel(self) -> None:
        """Safe to call from the GUI thread at any time."""
        self._cancel.set()

    def run(self) -> None:
        try:
            result = render(self.request, self._report, self._cancel)
            self.finished.emit(result)
        except RenderCancelled:
            LOG.info("export cancelled")
            self.cancelled.emit()
        except (RenderError, RenderConfigError, RenderPlanError) as exc:
            self.failed.emit(str(exc))
        except OSError as exc:  # pragma: no cover - environment specific
            self.failed.emit(f"export failed: {exc}")
        except Exception as exc:  # pragma: no cover - last-resort guard
            LOG.exception("unexpected export failure")
            self.failed.emit(f"export failed: {exc}")
        self.done.emit()

    def _report(self, fraction: float) -> None:
        # throttle: the GUI only needs a few dozen updates per second
        if fraction - self._last_reported >= 0.01 or fraction >= 1.0:
            self._last_reported = fraction
            self.progress.emit(fraction)


class RenderThread(QThread):
    """QThread subclass running the render directly in ``run``."""

    def __init__(self, worker: RenderWorker, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker = worker  # keep referenced: the C++ thread uses it

    def run(self) -> None:  # runs on the render thread
        self._worker.run()


class RenderController(QObject):
    """Start/cancel one export and re-export its signals for the window."""

    progress = Signal(float)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: RenderThread | None = None
        self._worker: RenderWorker | None = None

    def __del__(self) -> None:
        """Never destroy a running QThread (Qt aborts the process otherwise)."""
        thread = getattr(self, "_thread", None)
        if thread is not None:
            worker = getattr(self, "_worker", None)
            if worker is not None:
                worker.cancel()
            with contextlib.suppress(RuntimeError):  # pragma: no cover - shutdown
                thread.wait(10_000)
            self._thread = None
            self._worker = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self, request: RenderRequest) -> bool:
        if self.running:
            return False
        worker = RenderWorker(request)
        thread = RenderThread(worker, self)
        worker.progress.connect(self.progress)
        worker.finished.connect(self.finished)
        worker.failed.connect(self.failed)
        worker.cancelled.connect(self.cancelled)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._cleared)
        self._thread = thread
        self._worker = worker
        thread.start()
        return True

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def wait_finished(self, timeout_ms: int = 10_000) -> bool:
        """Wait for the render thread and deliver its queued signals.

        Only for tests and shutdown: the normal GUI flow never blocks, the event
        loop delivers the signals on its own.
        """
        thread = self._thread
        if thread is None:
            return True
        deadline = time.monotonic() + timeout_ms / 1000
        while thread.isRunning() and time.monotonic() < deadline:
            _pump_events()
            time.sleep(0.005)
        finished = not thread.isRunning()
        if finished:
            for _ in range(5):  # let queued result signals reach their slots
                _pump_events()
        return finished

    def _cleared(self) -> None:
        self._thread = None
        self._worker = None


def _pump_events() -> None:
    """Let queued cross-thread signals reach their slots."""
    if QCoreApplication.instance() is not None:
        QCoreApplication.processEvents()


# ------------------------------------------------------------------ previews


class PreviewController(QObject):
    """Runs preview renders, one at a time, and tags every result.

    A preview request supersedes the one before it: the running render is
    cancelled and its result dropped, so rapid scrubbing cannot pile up renders or
    land an obsolete picture in the viewer. Results carry the *generation* they
    belong to; the viewer ignores any result whose generation is no longer current.
    """

    progress = Signal(int, float)  # generation, 0.0..1.0
    finished = Signal(int, object)  # generation, RenderResult
    failed = Signal(int, str)
    cancelled = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: RenderThread | None = None
        self._worker: RenderWorker | None = None
        self._generation = 0

    def __del__(self) -> None:  # pragma: no cover - shutdown guard
        thread = getattr(self, "_thread", None)
        if thread is not None:
            worker = getattr(self, "_worker", None)
            if worker is not None:
                worker.cancel()
            with contextlib.suppress(RuntimeError):
                thread.wait(10_000)

    @property
    def running(self) -> bool:
        return self._thread is not None

    @property
    def generation(self) -> int:
        """Identifies the request the viewer should accept results for."""
        return self._generation

    def start(self, job: PreviewJob) -> int:
        """Cancel anything running and render *job*; returns its generation."""
        self.cancel()
        self._settle(10_000)
        generation = self._generation + 1
        self._generation = generation
        worker = RenderWorker(job.request)
        thread = RenderThread(worker, self)
        # explicit lambdas: PySide loses the argument tuple when a signal object is
        # passed through a generic helper, so each signal is emitted directly
        worker.progress.connect(
            lambda value, gen=generation: None if gen != self._generation
            else self.progress.emit(gen, value)
        )
        worker.finished.connect(
            lambda result, gen=generation: None if gen != self._generation
            else self.finished.emit(gen, result)
        )
        worker.failed.connect(
            lambda message, gen=generation: None if gen != self._generation
            else self.failed.emit(gen, message)
        )
        worker.cancelled.connect(
            lambda gen=generation: None if gen != self._generation else self.cancelled.emit(gen)
        )
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda gen=generation: self._cleared(gen))
        # explicit references, and the thread is parented to this controller: a
        # superseded render's objects must not be collected while its thread is
        # still winding down, or its signals would be lost
        self._thread = thread
        self._worker = worker
        thread.start()
        return generation

    def cancel(self) -> None:
        """Ask the running preview to stop (obsolete request, or window closing)."""
        if self._worker is not None:
            self._worker.cancel()

    def stop(self) -> None:
        """Cancel and wait, so no FFmpeg process outlives the window."""
        self.cancel()
        self._settle(10_000)

    def wait_finished(self, timeout_ms: int = 30_000) -> bool:
        """Wait and deliver queued signals (tests and shutdown only)."""
        deadline = time.monotonic() + timeout_ms / 1000
        while self.running and time.monotonic() < deadline:
            _pump_events()
            time.sleep(0.005)
        for _ in range(5):
            _pump_events()
        return not self.running

    def _settle(self, timeout_ms: int) -> None:
        """Wait for a superseded render to actually stop (blocks briefly by design).

        Only on supersede or shutdown: two FFmpeg renders at once would compete for
        CPU and could interleave writes, and the point of generation tags is that an
        obsolete render must not reach the viewer afterwards.
        """
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.wait(timeout_ms)
        self._thread = None
        self._worker = None

    def _cleared(self, generation: int) -> None:
        """Forget a finished render only if it is still the current one."""
        if generation == self._generation:
            self._thread = None
            self._worker = None


# ------------------------------------------------------- still-frame inspection


class StillWorker(QObject):
    """Decodes one composition still per event-loop turn of its own thread.

    ``request`` is cheap: it stores the newest request and wakes the loop, so
    scrubbing never spawns a decoder per click — only the most recent request
    survives. A request superseded *before* its decode started is therefore never
    decoded at all. A decode already in flight cannot be interrupted, so its result
    is dropped by the controller's generation check instead: that costs at most one
    still decode of latency and never a wrong picture.
    """

    finished = Signal(int, object)  # generation, StillFrame
    failed = Signal(int, str)  # generation, message

    def __init__(self) -> None:
        super().__init__()
        self._pending: tuple | None = None
        self._stopping = threading.Event()

    def request(
        self,
        generation: int,
        project,
        asset_paths: dict,
        output_time,
        asset_identities: dict | None = None,
    ) -> None:
        """Queue the newest request (the controller wakes the thread)."""
        self._pending = (generation, project, asset_paths, output_time, asset_identities)

    def stop(self) -> None:
        """Refuse queued work and let the thread exit (shutdown)."""
        self._stopping.set()
        self._pending = None

    @property
    def pending(self) -> bool:
        return self._pending is not None

    @Slot()
    def process_request(self) -> None:
        """Handle the newest queued request (invoked across threads)."""
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        generation, project, asset_paths, output_time, identities = pending
        if self._stopping.is_set():
            return
        try:
            frame = extract_composition_still(
                project, asset_paths, output_time, asset_identities=identities
            )
        except StillError as exc:
            self.failed.emit(generation, str(exc))
            return
        except Exception as exc:  # pragma: no cover - last-resort guard
            LOG.exception("unexpected still-frame failure")
            self.failed.emit(generation, f"still frame failed: {exc}")
            return
        self.finished.emit(generation, frame)


class WorkerThread(QThread):
    """A ``QThread`` that actually runs an event loop (unlike ``RenderThread``).

    ``QThread.start()`` alone does not process queued signals: without ``exec()``
    a moved-in worker would never receive anything, so this is what lets a mailbox
    worker (still-frame decoding) pick up requests.
    """

    def run(self) -> None:  # runs on the worker thread
        self.exec()


class StillController(QObject):
    """Frame-accurate paused inspection: asynchronous, de-duplicated, ordered.

    Each request gets a generation number and only the newest one is delivered, so
    a rapidly scrubbing user gets the frame they stopped on rather than whichever
    decode happened to finish first.
    """

    finished = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = WorkerThread(self)
        self._worker = StillWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.process_request)
        self._worker.finished.connect(self._deliver)
        self._worker.failed.connect(self._deliver)
        self._thread.start()
        self._generation = 0
        self._settled = True  # nothing asked for yet

    def __del__(self) -> None:  # pragma: no cover - shutdown guard
        worker = getattr(self, "_worker", None)
        if worker is not None:
            worker.stop()
        thread = getattr(self, "_thread", None)
        if thread is not None:
            with contextlib.suppress(RuntimeError):
                thread.quit()
                thread.wait(5_000)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def busy(self) -> bool:
        """True while the newest request has not been delivered yet.

        Delivery lands in the GUI thread through a queued signal, so "the worker
        is no longer holding a request" is not the same as "the picture arrived":
        waiting for the latter is what a caller inspecting a still frame needs.
        """
        return not self._settled

    def request(
        self, project, asset_paths: dict, output_time, asset_identities: dict | None = None
    ) -> int:
        """Ask for the composition still at ``output_time``.

        ``QMetaObject.invokeMethod`` posts the call to the worker's own event loop:
        no thread is spawned per request (and PySide6 exposes no ``QThread.wake``).
        *asset_identities* travels with the request so the decode can refuse a file
        that is no longer the one the project recorded.
        """
        self._generation += 1
        self._settled = False
        self._worker.request(
            self._generation, project, asset_paths, output_time, asset_identities
        )
        QMetaObject.invokeMethod(
            self._worker, "process_request", Qt.ConnectionType.QueuedConnection
        )
        return self._generation

    def _deliver(self, generation: int, payload: object) -> None:
        if generation != self._generation:
            return  # superseded while decoding: not what the user is looking at
        self._settled = True
        if isinstance(payload, str):
            self.failed.emit(generation, payload)
        else:
            self.finished.emit(generation, payload)

    def wait_idle(self, timeout_ms: int = 20_000) -> bool:
        """Wait until the worker has caught up (tests and shutdown only)."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            _pump_events()
            if not self.busy:
                for _ in range(3):  # let the queued result signal arrive
                    _pump_events()
                return True
            time.sleep(0.005)
        return not self.busy

    def shutdown(self) -> None:
        """Stop the worker thread; call from ``closeEvent``."""
        self._worker.stop()
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(5_000)


@dataclass(frozen=True)
class SourceStepProblem:
    """A source step that produced no picture, and what the index could prove.

    This is a **structured** failure, not a sentence, because the difference
    between the two ways a step runs out is a fact the interface must not merge:

    * :attr:`AnswerStatus.NO_PICTURE` - the stream really has no picture that way.
      The index enumerated up to a limit beyond which nothing can exist (the start
      of its timeline, or past the end this container claims for its content);
    * :attr:`AnswerStatus.UNKNOWN` - the index did not read far enough to know.
      Saying "the stream ends here" from this would be an invention.

    The window words both; the worker only reports which one happened, how far it
    got and what had been read.
    """

    #: NO_PICTURE (proven absence) or UNKNOWN (not read far enough)
    status: AnswerStatus
    #: True when the step was looking later in the stream, False when earlier
    forward: bool
    #: the real picture the step moved from when it met the wall
    from_pts: Fraction
    #: the signed frame count that was asked for
    steps: int
    #: frames actually moved before the wall (0 when the very first step failed)
    moved: int
    #: the span of the stream that had been enumerated when the answer was given
    coverage: tuple[Fraction, Fraction] | None = None
    #: what this container claims about where its content stops (a claim, not a
    #: measurement: it is reported because it is the licence for ``NO_PICTURE``)
    declared_end: Fraction | None = None


class SourceStepWorker(QObject):
    """Finds and decodes one neighbouring *source* picture, off the GUI thread.

    Two things belong here and not in the interface: reading the stream's real
    picture timestamps (a bounded, decoded :class:`FrameIndex` read of one window)
    and decoding the picture. The index is kept per file identity and stream, so a
    run of steps across hundreds of frames costs one probe, and a file replaced on
    disk gets a fresh one instead of timestamps from the old encode.

    A step moves to the neighbouring **actual decoded presentation timestamp** of
    this stream: nothing in this path consults the output frame rate, the clip's
    tempo or the composition's extent, and the picture for the target is decoded
    directly from the source stream (:func:`extract_source_still`), so the bytes
    and the timestamp shown are the same picture's.

    Like the still worker, only the newest request survives: rapid stepping does
    not queue a decode per click, and a decode already in flight is dropped by the
    controller's generation check rather than displayed late. What the caller
    accumulates is a signed frame count from the last picture it actually showed.
    """

    finished = Signal(int, object)  # generation, StillFrame
    #: generation, either a :class:`SourceStepProblem` (no picture that way) or
    #: already-in-words text (a probe or decode that failed for some other reason)
    failed = Signal(int, object)

    #: how many streams keep a probed window cache (each is a few thousand
    #: timestamps at most; stepping between two sources must not evict either)
    MAX_INDEXES = 4

    def __init__(self) -> None:
        super().__init__()
        self._pending: tuple | None = None
        self._stopping = threading.Event()
        self._indexes: dict[tuple, FrameIndex] = {}

    def request(
        self,
        generation: int,
        project,
        asset_paths: dict,
        clip_id: str,
        from_pts,
        steps: int,
        output_time,
        asset_identities: dict | None = None,
    ) -> None:
        """Queue the newest step (the controller wakes the thread).

        ``steps`` is a signed count of *real source frames* to move from
        ``from_pts``: clicks made while a decode is running add up instead of being
        lost or decoded one by one.
        """
        self._pending = (
            generation,
            project,
            asset_paths,
            clip_id,
            from_pts,
            steps,
            output_time,
            asset_identities,
        )

    def stop(self) -> None:
        self._stopping.set()
        self._pending = None

    @property
    def pending(self) -> bool:
        return self._pending is not None

    def _index_for(self, path: Path, stream_index: int) -> FrameIndex:
        """Cached bounded frame index for this exact file and stream."""
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns, stream_index)
        index = self._indexes.get(key)
        if index is None:
            index = FrameIndex(path, stream_index)
            if len(self._indexes) >= self.MAX_INDEXES:
                self._indexes.pop(next(iter(self._indexes)))
            self._indexes[key] = index
        return index

    @Slot()
    def process_request(self) -> None:
        pending = self._pending
        if pending is None:
            return
        self._pending = None
        (generation, project, asset_paths, clip_id, from_pts, steps,
         output_time, identities) = pending
        if self._stopping.is_set():
            return
        clip = next((c for c in project.clips if c.id == clip_id), None)
        if clip is None:
            self.failed.emit(generation, f"clip {clip_id} is no longer in the project")
            return
        path = asset_paths.get(clip.asset_id)
        if path is None:
            self.failed.emit(
                generation, f"asset {clip.asset_id} has no file recorded; relink it"
            )
            return
        try:
            index = self._index_for(Path(path), clip.stream_index)
            target, _moved, problem = self._advance(index, from_pts, steps)
            if target is None:
                # no invented picture and no invented edge: the interface is told
                # which kind of non-answer this is and decides what to claim
                self.failed.emit(generation, problem)
                return
            frame = extract_source_still(
                project,
                asset_paths,
                clip_id,
                target,
                output_time,
                asset_identities=identities,
            )
        except (StillError, OSError) as exc:
            self.failed.emit(generation, str(exc))
            return
        except Exception as exc:  # pragma: no cover - last-resort guard
            LOG.exception("unexpected source-step failure")
            self.failed.emit(generation, f"source step failed: {exc}")
            return
        self.finished.emit(generation, frame)

    @staticmethod
    def _advance(index: FrameIndex, from_pts, steps: int):
        """Move ``steps`` real frames either side of ``from_pts``.

        Returns ``(target, moved, problem)``. Every move asks
        :meth:`FrameIndex.answer_step`, which answers from **decoded pictures** and
        says what it can support: ``FOUND`` is a real neighbour, ``NO_PICTURE`` is
        a proven edge of the stream, and ``UNKNOWN`` is ground the index was not
        allowed to read. The last two are reported apart, because one is "this
        stream has nothing there" and the other is "I have not looked that far";
        the plain :meth:`FrameIndex.step` merges them, and a caller that tells a
        user where their film ends must not.

        ``target`` is None when the walk could not reach the requested count;
        ``problem`` then carries the status, how far the walk did get and what had
        been enumerated.
        """
        if not steps:
            return None, 0, None
        step = 1 if steps > 0 else -1
        current = from_pts
        for moved in range(1, abs(steps) + 1):
            answer = index.answer_step(current, step)
            if answer.status is AnswerStatus.FOUND:
                current = answer.pts
                continue
            return None, moved - 1, SourceStepProblem(
                status=answer.status,
                forward=step > 0,
                from_pts=current,
                steps=steps,
                moved=moved - 1,
                coverage=answer.coverage,
                declared_end=index.declared_end.end,
            )
        return current, abs(steps), None


class SourceStepController(QObject):
    """Source-frame navigation: asynchronous, newest-request-wins, cancellable.

    Every request carries a generation and only the newest one is delivered, so a
    rapidly stepping user gets the frame they stopped on. That is only half of the
    decision, though: the window also has to know **which request it is still
    waiting for**, because a result for a clip or file that is no longer being
    inspected is worthless even when its generation is current. The window keeps
    that identity alongside the generation (``MainWindow._SourceStepRequest``).
    """

    finished = Signal(int, object)
    #: generation, either a :class:`SourceStepProblem` (no picture that way) or
    #: already-in-words text (a probe or decode that failed for another reason)
    failed = Signal(int, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = WorkerThread(self)
        self._worker = SourceStepWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.process_request)
        # the two outcomes stay separate: a picture is a picture, and a
        # no-picture answer carries what it proved (SourceStepProblem)
        self._worker.finished.connect(self._deliver_picture)
        self._worker.failed.connect(self._deliver_problem)
        self._thread.start()
        self._generation = 0
        self._settled = True

    def __del__(self) -> None:  # pragma: no cover - shutdown guard
        worker = getattr(self, "_worker", None)
        if worker is not None:
            worker.stop()
        thread = getattr(self, "_thread", None)
        if thread is not None:
            with contextlib.suppress(RuntimeError):
                thread.quit()
                thread.wait(5_000)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def busy(self) -> bool:
        """True until the newest request's picture has actually arrived."""
        return not self._settled

    def request(
        self,
        project,
        asset_paths: dict,
        clip_id: str,
        from_pts,
        steps: int,
        output_time,
        asset_identities: dict | None = None,
    ) -> int:
        self._generation += 1
        self._settled = False
        self._worker.request(
            self._generation, project, asset_paths, clip_id, from_pts, steps,
            output_time, asset_identities,
        )
        QMetaObject.invokeMethod(
            self._worker, "process_request", Qt.ConnectionType.QueuedConnection
        )
        return self._generation

    def _deliver_picture(self, generation: int, frame: object) -> None:
        if generation != self._generation:
            return  # superseded while decoding: not what the user is looking at now
        self._settled = True
        self.finished.emit(generation, frame)

    def _deliver_problem(self, generation: int, problem: object) -> None:
        if generation != self._generation:
            return  # superseded: the user has since asked for somewhere else
        self._settled = True
        self.failed.emit(generation, problem)

    def shutdown(self) -> None:
        """Stop the worker thread; call from ``closeEvent``."""
        self._worker.stop()
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(5_000)

    def wait_idle(self, timeout_ms: int = 20_000) -> bool:
        """Wait until the newest step's result has been delivered.

        Tests and shutdown only: the GUI never blocks on a decode. Waiting for
        *delivery* rather than for the worker to be idle is the point — the result
        crosses threads as a queued signal and has to reach the window's own slot.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            _pump_events()
            if not self.busy:
                for _ in range(3):  # let the queued result signal arrive
                    _pump_events()
                return True
            time.sleep(0.005)
        return not self.busy


def describe_result(result: RenderResult) -> str:
    chunks = f" in {result.chunks} chunks" if result.chunks > 1 else ""
    duration = float(result.duration)
    return f"Exported {result.path.name} ({duration:.2f}s output{chunks})"


def format_fraction(value: Fraction) -> str:
    return f"{float(value):.3f}s"
