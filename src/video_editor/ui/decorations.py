"""Lazy, bounded clip decorations: timeline thumbnails and waveforms.

The timeline says which output range is on screen at the current zoom; this layer
works out what is worth decoding, decodes it in one background thread, caches it
by stream *and file identity*, and hands it back when it lands. It is
deliberately not authoritative: the clip's geometry is the edit, and a clip whose
thumbnail or waveform has not arrived, was refused, or belongs to a file that has
been replaced is still drawn as a shape with its real extent. Nothing here may
change what a clip means.

What gets decoded is bounded three ways. Granularity comes from a fixed ladder,
so two nearby zoom levels share one cache entry instead of decoding everything
again. Only the range the timeline is actually looking at is fetched (the window
the timeline passes already includes its own margin). And the caches hold a
limited number of bytes, evicting least recently used first. Requests are
deduplicated by key and the queue is capped, so scrubbing through a two-hour
film does not leave a backlog of obsolete decodes.

The same :meth:`ClipDecorations.plan` computes both the request and the lookup,
which is what makes a hit possible: a decoration is asked for and drawn on
identical terms — same asset, same stream, same file identity, same anchored
grid, same step.
"""

from __future__ import annotations

import queue
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..core.model import Clip, Project, StreamSource, TrackKind
from ..fs.identity import Identity, check
from ..media.thumbnails import MAX_FRAMES, ThumbnailError, ThumbStrip, video_strip
from ..media.thumbnails import Cancelled as ExtractionCancelled
from ..media.waveform import MAX_BUCKETS, Envelope, WaveformError, audio_envelope
from ..media.waveform import Cancelled as MeasurementCancelled

#: Granularities a decoration may be decoded at. A ladder rather than an exact
#: per-pixel choice: nearby zoom levels land on the same rung and share a cache
#: entry, which is what keeps panning cheap on a long timeline.
BUCKET_LADDER: tuple[Fraction, ...] = (
    Fraction(1, 1000), Fraction(1, 250), Fraction(1, 100), Fraction(1, 50),
    Fraction(1, 25), Fraction(1, 10), Fraction(1, 5), Fraction(1),
    Fraction(2), Fraction(5), Fraction(15), Fraction(60), Fraction(240), Fraction(900),
)
#: how many horizontal pixels one decoded sample should cover
PIXELS_PER_SAMPLE = 4
#: clips narrower than this are not worth decoding
MIN_CLIP_PIXELS = 12
MAX_QUEUED_JOBS = 32
ENVELOPE_BUDGET = 2 << 20  # 2 MB of peak readings
STRIP_BUDGET = 6 << 20  # 6 MB of small pictures
MAX_TRACKED_FAILURES = 64

#: every layer whose thread is still running, so shutdown can be reached from
#: outside (application exit, test session) even if nobody kept a reference
_LIVE: weakref.WeakSet[ClipDecorations] = weakref.WeakSet()


def shutdown_all() -> int:
    """Stop every running extraction thread. Returns how many were stopped.

    Destroying a live QThread aborts the process, and a decoration layer is
    easily forgotten: it is owned by a window nobody is holding any more.
    """
    stopped = 0
    for layer in list(_LIVE):
        layer.shutdown()
        stopped += 1
    return stopped


def ladder_step(target: Fraction) -> Fraction:
    """Smallest ladder rung that is at least ``target`` (never finer)."""
    for step in BUCKET_LADDER:
        if step >= target:
            return step
    return BUCKET_LADDER[-1]


def thumb_box(width: int, height: int) -> tuple[int, int]:
    """Thumbnail box for a row of ``height`` pixels, clamped to sane sizes."""
    return (max(16, min(240, width)), max(12, min(135, height)))


@dataclass(frozen=True)
class Decorations:
    """What a clip is made of: its source record plus the file it came from."""

    source: StreamSource
    path: Path
    #: file identity (size, mtime_ns) the material was read as, so a replaced
    #: file cannot be drawn with the old file's pictures
    identity: tuple[int, int]
    #: identity recorded in the asset record, if it has one. The worker re-checks
    #: the file against *this* before decoding: a decode that starts after the
    #: file was swapped must not produce thumbnails of a different encode.
    recorded: tuple[int, int] | None = None


@dataclass(frozen=True)
class Plan:
    """One decoration: what to decode, where it starts and how coarse it is."""

    kind: str  # "envelope" | "strip"
    key: tuple
    start: Fraction
    duration: Fraction
    step: Fraction


@dataclass(frozen=True)
class Job(Plan):
    clip_id: str
    source: StreamSource
    path: Path
    width: int
    height: int
    #: identity the asset record claims for this file (see Decorations.recorded)
    recorded: tuple[int, int] | None = None


class _Cache:
    """LRU of decoded decorations under a byte budget."""

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.entries: OrderedDict[tuple, object] = OrderedDict()
        self.used = 0

    def get(self, key: tuple):
        value = self.entries.get(key)
        if value is not None:
            self.entries.move_to_end(key)
        return value

    def put(self, key: tuple, value) -> None:
        self.entries[key] = value
        self.entries.move_to_end(key)
        self.used += value.nbytes
        while self.used > self.budget and len(self.entries) > 1:
            evicted_key, evicted = next(iter(self.entries.items()))
            del self.entries[evicted_key]
            self.used -= evicted.nbytes

    def clear(self) -> None:
        self.entries.clear()
        self.used = 0


class DecorationWorker(QObject):
    """Decodes one decoration at a time, stopping work nobody waits for."""

    ready = Signal(str, object, object)  # clip id, key, Envelope | ThumbStrip
    failed = Signal(str, object, str)  # clip id, key, reason

    def __init__(self, jobs: queue.SimpleQueue) -> None:
        super().__init__()
        self._jobs = jobs
        self._stopping = threading.Event()

    def stop(self) -> None:
        self._stopping.set()

    @Slot()
    def run(self) -> None:
        """Pop jobs until the shutdown sentinel or a stop request."""
        while not self._stopping.is_set():
            try:
                job = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                return
            self._handle(job)

    def _handle(self, job: Job) -> None:
        if self._stopping.is_set():
            return
        if job.recorded is not None:
            # the file may have been replaced between scheduling and this decode:
            # measuring a different encode and drawing it over these clips would
            # look exactly like a correct waveform
            entry = check(job.path, Identity(*job.recorded), job.path.name)
            if not entry.usable:
                self.failed.emit(job.clip_id, job.key, entry.describe())
                return
        try:
            if job.kind == "envelope":
                value = audio_envelope(
                    job.path, job.source, start=job.start, duration=job.duration,
                    bucket=job.step, cancel=self._stopping.is_set,
                )
            else:
                value = video_strip(
                    job.path, job.source, start=job.start, duration=job.duration,
                    interval=job.step, width=job.width, height=job.height,
                    cancel=self._stopping.is_set,
                )
        except (MeasurementCancelled, ExtractionCancelled):
            return  # superseded or shutting down: nothing happened, say so by silence
        except (WaveformError, ThumbnailError) as exc:
            self.failed.emit(job.clip_id, job.key, str(exc))
            return
        except Exception as exc:  # pragma: no cover - last-resort guard
            self.failed.emit(job.clip_id, job.key, f"decoration failed: {exc}")
            return
        self.ready.emit(job.clip_id, job.key, value)


class _DecorationThread(QThread):
    """Event-loop thread, so the worker can be moved into it and stopped cleanly."""

    def run(self) -> None:  # runs on the worker thread
        self.exec()


class ClipDecorations(QObject):
    """Cache plus background extraction for timeline pictures and waveforms.

    ``changed`` fires when a decoration lands for a clip, so the timeline can
    repaint it. ``complained`` carries the reason a decoration could not be
    produced, which the window shows rather than hides: a missing waveform must
    be visible as missing, not silently drawn as something else.
    """

    changed = Signal(str)  # clip id
    complained = Signal(str)  # why a decoration could not be produced

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = _DecorationThread()
        self._worker = DecorationWorker(self._jobs)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.ready.connect(self._take_ready)
        self._worker.failed.connect(self._take_failure)
        self._envelopes = _Cache(ENVELOPE_BUDGET)
        self._strips = _Cache(STRIP_BUDGET)
        self._queued: set[tuple] = set()
        # plans known to have failed: a broken stream must not be decoded again
        # on every repaint of the clips that point at it
        self._abandoned: set[tuple] = set()
        self._failures: OrderedDict[str, str] = OrderedDict()
        self._thread.setObjectName("clip-decorations")
        self._thread.start()
        _LIVE.add(self)

    def __del__(self) -> None:  # pragma: no cover - interpreter teardown guard
        """Never let a running extraction thread outlive its owner.

        Destroying a QThread that is still running aborts the process — and a
        decoration layer is exactly the kind of object that can be dropped without
        anyone calling ``shutdown()`` first, because forgetting it is harmless
        everywhere except here. At interpreter teardown the worker may already be
        unreachable, so the thread is stopped on its own: `quit()` ends its event
        loop and `wait()` makes sure it has really finished before the C++
        `QThread` goes away.
        """
        thread = getattr(self, "_thread", None)
        if thread is None:
            return
        try:
            if not thread.isRunning():
                return
            try:
                self._worker.stop()
                self._jobs.put(None)
            except (RuntimeError, AttributeError):  # worker already gone
                pass
            thread.requestInterruption()
            thread.quit()
            thread.wait(5_000)
        except (RuntimeError, AttributeError):  # Qt objects already gone
            pass

    # ------------------------------------------------------------------ shutdown
    def shutdown(self) -> None:
        """Stop the worker and wake it so its thread can actually exit."""
        _LIVE.discard(self)
        self._worker.stop()
        self._jobs.put(None)
        self._thread.quit()
        self._thread.wait(5_000)

    # ---------------------------------------------------------------- planning
    def plan(
        self,
        clip: Clip,
        matter: Decorations,
        out_from: Fraction,
        out_to: Fraction,
        pixels_per_second: Fraction,
        box: tuple[int, int],
    ) -> Plan | None:
        """The decoration this clip needs for an output range, or None.

        ``None`` means there is nothing to ask for: the clip is not material this
        layer can read (no registered source or no container position), or the
        requested range does not touch it.
        """
        if pixels_per_second <= 0 or out_to <= out_from:
            return None
        low = max(out_from, clip.output_start)
        high = min(out_to, clip.output_end)
        if high <= low:
            return None
        start, end = self._source_times(clip, matter, low, high)
        if end <= start:
            return None
        step = self._step(clip, matter, pixels_per_second)
        grid = (start // step) * step  # anchored on asset-local time, not on the clip
        blocks = -(-(end - grid) // step)  # ceiling
        audio = matter.source.kind is TrackKind.AUDIO
        kind = "envelope" if audio else "strip"
        if audio:
            blocks = min(MAX_BUCKETS, blocks)
            key = (kind, clip.asset_id, clip.stream_index, matter.identity, grid, step)
        else:
            blocks = min(MAX_FRAMES, blocks)
            key = (kind, clip.asset_id, clip.stream_index, matter.identity, grid, step,
                   box[0], box[1])
        return Plan(kind=kind, key=key, start=grid, duration=blocks * step, step=step)

    def _source_times(
        self, clip: Clip, matter: Decorations, low: Fraction, high: Fraction
    ) -> tuple[Fraction, Fraction]:
        """Source times the clip shows under an output range, inside its material.

        The clip's own rate and in-point do this mapping: a retimed clip's
        waveform is stretched or compressed exactly as its sound is, and a clip
        cannot show material outside its registered stream.
        """
        start = clip.source_in + (low - clip.output_start) * clip.rate
        end = clip.source_in + (high - clip.output_start) * clip.rate
        if matter.source.end is not None:
            end = min(end, matter.source.end)
        start = max(start, matter.source.origin)
        return start, max(start, end)

    @staticmethod
    def _step(clip: Clip, matter: Decorations, pixels_per_second: Fraction) -> Fraction:
        """Ladder rung for the current zoom, in source seconds."""
        target = clip.rate * Fraction(PIXELS_PER_SAMPLE, 1) / pixels_per_second
        if target <= 0:
            target = BUCKET_LADDER[0]
        if matter.source.kind is TrackKind.VIDEO and matter.source.frame_rate:
            # sampling faster than the source's own pictures only repeats them
            target = max(target, Fraction(1) / matter.source.frame_rate)
        return ladder_step(target)

    # ------------------------------------------------------------- scheduling
    def request(
        self,
        project: Project,
        material: dict[tuple[str, int], Decorations],
        out_from: Fraction,
        out_to: Fraction,
        pixels_per_second: Fraction,
        box: tuple[int, int],
    ) -> int:
        """Queue whatever the visible clips need; returns how many were queued."""
        queued = 0
        for clip in project.clips:
            # Cheap interval test first. A feature-length timeline has hundreds of
            # clips and only a handful are on screen; deciding that by comparison
            # costs nothing, whereas reaching the file-system check and the plan
            # arithmetic below for every clip in the project cost ~9 ms per view
            # change at 843 clips.
            if clip.output_end <= out_from or clip.output_start >= out_to:
                continue
            if (clip.output_end - clip.output_start) * pixels_per_second < MIN_CLIP_PIXELS:
                continue
            matter = material.get((clip.asset_id, clip.stream_index))
            if matter is None or matter.source.stream_position is None:
                continue  # not readable material: the clip is drawn as a shape
            if not matter.path.is_file():
                continue  # gone since the last refresh: there is nothing to decode
            planned = self.plan(clip, matter, out_from, out_to, pixels_per_second, box)
            if (planned is None or planned.key in self._queued
                    or planned.key in self._abandoned):
                continue
            if self._cache_for(planned.kind).get(planned.key) is not None:
                continue
            if len(self._queued) >= MAX_QUEUED_JOBS:
                break  # the view keeps moving; a capped queue keeps it responsive
            self._queued.add(planned.key)
            self._jobs.put(Job(
                clip_id=clip.id, source=matter.source, path=matter.path,
                width=box[0], height=box[1], recorded=matter.recorded, **{
                    "kind": planned.kind, "key": planned.key, "start": planned.start,
                    "duration": planned.duration, "step": planned.step,
                },
            ))
            queued += 1
        return queued

    def _cache_for(self, kind: str) -> _Cache:
        return self._envelopes if kind == "envelope" else self._strips

    # ----------------------------------------------------------------- delivery
    def envelope_for(self, clip: Clip, matter: Decorations, out_from: Fraction,
                     out_to: Fraction, pixels_per_second: Fraction) -> Envelope | None:
        planned = self.plan(clip, matter, out_from, out_to, pixels_per_second, (0, 0))
        if planned is None or planned.kind != "envelope":
            return None
        return self._envelopes.get(planned.key)

    def strip_for(self, clip: Clip, matter: Decorations, out_from: Fraction,
                  out_to: Fraction, pixels_per_second: Fraction,
                  box: tuple[int, int]) -> ThumbStrip | None:
        planned = self.plan(clip, matter, out_from, out_to, pixels_per_second, box)
        if planned is None or planned.kind != "strip":
            return None
        return self._strips.get(planned.key)

    def reason_for(self, clip_id: str) -> str | None:
        return self._failures.get(clip_id)

    @property
    def cached_bytes(self) -> int:
        return self._envelopes.used + self._strips.used

    @property
    def pending(self) -> int:
        return len(self._queued)

    def invalidate(self) -> None:
        """Forget everything: a different project, or a file that changed."""
        self._envelopes.clear()
        self._strips.clear()
        self._queued.clear()
        self._abandoned.clear()
        self._failures.clear()

    @Slot(str, object, object)
    def _take_ready(self, clip_id: str, key: tuple, value) -> None:
        self._queued.discard(key)
        self._cache_for(key[0]).put(key, value)
        self._failures.pop(clip_id, None)
        self.changed.emit(clip_id)

    @Slot(str, object, str)
    def _take_failure(self, clip_id: str, key: tuple, reason: str) -> None:
        self._queued.discard(key)
        self._abandoned.add(key)
        if len(self._abandoned) > MAX_TRACKED_FAILURES * 8:
            self._abandoned.clear()  # bounded; an old plan may be tried again
        self._failures[clip_id] = reason
        while len(self._failures) > MAX_TRACKED_FAILURES:
            self._failures.popitem(last=False)
        self.complained.emit(reason)
        self.changed.emit(clip_id)
