"""Background import jobs: probe files off the UI thread, bounded and cancellable.

Pure threads (no Qt). The UI bridges callbacks to the main thread with Qt queued
signals, so results from worker threads never race on shared state. Media files
are read-only; nothing is copied.

The bounds, stated because a caller has to plan around them (TASK_14):

``MAX_WORKERS`` = **2** probe threads per controller.
    Probing is what an ``ffprobe`` child costs, and two keeps the bin responsive
    while putting a hard ceiling on how many processes a dropped folder of files
    can start at once.

``MAX_PENDING`` = **64** jobs may wait their turn.
    A controller therefore holds at most ``capacity`` = 2 + 64 = **66** live jobs
    at any instant. A submission beyond that is **refused by name** in
    :attr:`ImportSubmission.refused` - never queued silently, never dropped - and
    the owner releases or reports those paths itself, because no job and therefore
    no completion ever exists for them.

Workers are daemon threads parked on the queue. Their explicit stop/join contract
belongs to TASK_22; until then :meth:`ImportController.wait` drains **jobs**, which
is what an owner needs in order to close safely.

Cancellation is per job and reaches the process
-----------------------------------------------
Each job owns a :class:`threading.Event`, passed to ``probe_file`` and from there
into :func:`video_editor.media.process.run_process` (the TASK_13 runner), which
checks it before launching and while waiting, then terminates and reaps the child.
A request arriving before a queued job reaches a worker takes it out of the queue,
so no probe is started for it at all; a request arriving during a probe ends that
probe and publishes nothing.

One job, one outcome
--------------------
The registry holds exactly the jobs that have not finished. :meth:`_finish` removes
the registry entry and claims the job's completion in one critical section, so
exactly one caller per job emits it - success, failure or cancelled - and the
worker loop catches every fault of a job so a broken job can never strand the ones
behind it. A probe result is published only when it is complete *and* error-free:
a cancelled or truncated probe leaves the job failed with ``asset=None``, never a
partially probed asset.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .assets import Asset
from .ffprobe import CANCELLED_MESSAGE, ProbeResult, probe_file

log = logging.getLogger(__name__)

#: Probe threads per controller.  See the module docstring.
MAX_WORKERS = 2
#: Jobs allowed to wait for a worker.  See the module docstring.
MAX_PENDING = 64


class ImportState(Enum):
    """Where one job is between submission and its single completion."""

    QUEUED = "queued"
    RUNNING = "running"
    FINISHED = "finished"


@dataclass
class ImportJob:
    """One requested probe.  Every field is read or written under the controller's guard."""

    id: str
    path: Path
    state: ImportState = ImportState.QUEUED
    #: the cancellation token this job hands to the subprocess runner
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    #: the error of the terminal outcome, if any (a diagnostic for the owner)
    error: str | None = None
    #: set by the one caller entitled to emit this job's completion
    settled: bool = False


@dataclass(frozen=True)
class ImportSubmission:
    """What one :meth:`ImportController.import_paths` call took on, and what it refused.

    A refused path is not a job: nothing was queued for it, no completion will ever
    arrive for it, and the owner is told about it here, by path, at the moment of
    submission.
    """

    accepted: tuple[ImportJob, ...] = ()
    refused: tuple[Path, ...] = ()


class ImportController:
    """Probes files on a bounded pool of worker threads, each job cancellable.

    Callbacks (``on_started`` then exactly one ``on_finished``) always run on a
    worker thread, never on the thread that submitted the job.
    """

    def __init__(
        self,
        on_started: Callable[[ImportJob], None],
        on_finished: Callable[[ImportJob, Asset | None, str | None], None],
        *,
        max_workers: int = MAX_WORKERS,
        max_pending: int = MAX_PENDING,
    ) -> None:
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1:
            raise ValueError(f"max_workers must be a positive thread count, got {max_workers!r}")
        if not isinstance(max_pending, int) or isinstance(max_pending, bool) or max_pending < 0:
            raise ValueError(f"max_pending must be a non-negative job count, got {max_pending!r}")
        self._on_started = on_started
        self._on_finished = on_finished
        self._max_workers = max_workers
        self._max_pending = max_pending
        # One guard for the registry, the pending queue and the worker hand-off:
        # "is this job still queued?" and "is this job still in the queue?" can
        # then never disagree, which is what makes queued cancellation exact.
        self._guard = threading.Condition(threading.Lock())
        self._jobs: dict[str, ImportJob] = {}  # every live (queued or running) job
        self._pending: deque[str] = deque()  # job ids waiting for a worker
        self._workers: list[threading.Thread] = []

    # ------------------------------------------------------------- properties
    @property
    def max_workers(self) -> int:
        return self._max_workers

    @property
    def max_pending(self) -> int:
        """Jobs that may wait for a worker before the next submission is refused."""
        return self._max_pending

    @property
    def capacity(self) -> int:
        """Live jobs this controller accepts at once: running plus waiting."""
        return self._max_workers + self._max_pending

    # -------------------------------------------------------------- submitting
    def import_paths(self, paths: Sequence[Path]) -> ImportSubmission:
        """Queue *paths* for probing, refusing any that do not fit the budget.

        Returns the accepted jobs in order plus the refused paths by name; a
        refusal starts nothing and emits nothing.
        """
        accepted: list[ImportJob] = []
        refused: list[Path] = []
        with self._guard:
            self._start_workers_locked()
            for path in paths:
                if len(self._jobs) >= self.capacity:
                    refused.append(path)
                    continue
                job = ImportJob(id=uuid.uuid4().hex, path=path)
                self._jobs[job.id] = job
                self._pending.append(job.id)
                accepted.append(job)
            if accepted:
                self._guard.notify_all()
        return ImportSubmission(tuple(accepted), tuple(refused))

    # ------------------------------------------------------------ cancelling
    def cancel(self, job_id: str) -> bool:
        """Ask one job to stop.  True when it had not already finished.

        A job still waiting is taken out of the queue and completed here, so no
        probe is ever started for it and its place in the budget is released at
        once.  A job already probing keeps its registry entry until its worker
        answers: the shared Event reaches the ``ffprobe`` child itself.
        """
        with self._guard:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.cancel_event.set()
            queued = job.state is ImportState.QUEUED
            if queued:
                with contextlib.suppress(ValueError):  # defensive: not in the queue
                    self._pending.remove(job_id)
        if queued:
            self._finish(job, None, CANCELLED_MESSAGE)
        return True

    def cancel_all(self) -> None:
        """Ask every live job to stop: queued ones are dropped, running ones killed."""
        with self._guard:
            ids = list(self._jobs)
        for job_id in ids:
            self.cancel(job_id)

    # ------------------------------------------------------------- observing
    def pending_count(self) -> int:
        """Jobs that have not reached their completion yet (queued plus running)."""
        with self._guard:
            return len(self._jobs)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until no job is live.  True when drained, False if *timeout* passed.

        The owner's join: :meth:`cancel_all` then :meth:`wait` means no worker is
        still inside a probe.  Worker *threads* stay parked after that - their
        stop/join contract is TASK_22's.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._guard:
            while self._jobs:
                if deadline is None:
                    self._guard.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._guard.wait(remaining)
            return True

    # ---------------------------------------------------------------- workers
    def _start_workers_locked(self) -> None:
        """Start the pool on the first accepted job, never on construction."""
        while len(self._workers) < self._max_workers:
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"import-worker-{len(self._workers) + 1}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def _worker_loop(self) -> None:
        while True:
            job = self._next_job()
            if job is None:
                continue  # a job that finished while it waited: nothing to run
            try:
                self._run_job(job)
            except BaseException:  # a worker fault must never end the pool
                log.exception("import job %s could not be completed", job.id)

    def _next_job(self) -> ImportJob | None:
        """Take the next job, or park until there is one.

        Marking it running here, under the guard, is what makes cancellation
        unambiguous: before this instant ``cancel`` can remove the job from the
        queue, after it only the worker can finish the job.
        """
        with self._guard:
            while not self._pending:
                self._guard.wait()
            job_id = self._pending.popleft()
            job = self._jobs.get(job_id)
            if job is None or job.settled:
                return None
            job.state = ImportState.RUNNING
            return job

    def _run_job(self, job: ImportJob) -> None:
        """Runs one job start to finish and always completes it exactly once."""
        try:
            self._on_started(job)
            asset, error = self._probe_for_asset(job)
        except BaseException as exc:  # a broken job is a failed job, not a dead worker
            log.exception("import job %s failed", job.id)
            asset, error = None, f"Import failed: {type(exc).__name__}: {exc}"
        self._finish(job, asset, error)

    def _probe_for_asset(self, job: ImportJob) -> tuple[Asset | None, str | None]:
        """Probe, and build the asset only from a complete, error-free answer."""
        if job.cancel_event.is_set():
            return None, CANCELLED_MESSAGE
        if not job.path.is_file():
            return None, "File not found"
        result: ProbeResult = probe_file(job.path, cancel=job.cancel_event)
        if job.cancel_event.is_set():
            # honour a request that arrived while the probe ran: nothing that probe
            # produced, complete or not, is published
            return None, CANCELLED_MESSAGE
        if result.metadata is None or result.error is not None:
            return None, result.error or "Probe returned no metadata"
        try:
            stat = job.path.stat()
        except OSError as exc:
            return None, f"File vanished during import: {exc}"
        return (
            Asset(
                id=Asset.new_id(),
                path=job.path,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                metadata=result.metadata,
            ),
            None,
        )

    def _finish(self, job: ImportJob, asset: Asset | None, error: str | None) -> bool:
        """Close one job: out of the registry, and at most one completion.

        Returns True for exactly one caller per job - whoever flips ``settled`` in
        the same critical section that removes the registry entry.  Everyone else
        (a second cancel, a worker racing it) gets False and emits nothing.
        """
        with self._guard:
            if job.settled:
                return False
            job.settled = True
            job.state = ImportState.FINISHED
            job.error = error
            self._jobs.pop(job.id, None)
            self._guard.notify_all()  # wake wait(), and workers checking the queue
        # outside the guard: a completion callback is free to call back in
        self._on_finished(job, asset, error)
        return True
