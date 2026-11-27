"""TASK_14: imports are bounded, genuinely cancellable, and settle exactly once.

``probe_file`` is replaced by fakes here except in the last test, which drives the
real ``ffprobe``. Every fake records the cancellation token it was handed, and the
child fixtures record **their own pid** so the process claims are checked against
the operating system, never against the bookkeeping of the code under test.

The numbers asserted, and where each comes from. Every one of them was measured
first by a separate probe of the finished code (``/tmp/opencode/m14/measure_bounds.py``,
log ``measure_bounds.log``), never read off a constant of the module under test:

* **measured maximum in-flight count == the configured worker count.** The fake
  probe counts calls entering and leaving under its own lock; ``peak`` is that
  high-water mark. Reaching it also proves the workers really do overlap - a pool
  that serialised everything would peak at 1.
* **cancelling a running job answers in under 1.0 s** for a child that dies on
  ``SIGTERM``: measured **0.0084-0.0087 s** over three trials (the runner polls
  every ``POLL_INTERVAL`` = 0.02 s and a plain child dies 1.3 ms after ``SIGTERM``,
  TASK_13 ``term_probe.log``), so the bound is ~115x the measured path, against a
  fixture that sleeps 60 s - an ignored request would never answer at all.
* **a child that ignores ``SIGTERM`` is killed between 1.9 s and 4.0 s** after the
  request: measured **2.008-2.020 s**, the runner's ``DEFAULT_TERMINATE_GRACE`` of
  2.0 s plus the poll and the reap. The lower bound is what proves the grace really
  was waited instead of jumping straight to ``SIGKILL`` (which would answer in
  ~0.01 s); the upper is ~2x the measurement and ~30x below the fixture's sleep.
* **a real ``ffprobe`` blocked on a FIFO dies under 1.5 s** after the request:
  measured **0.005-0.018 s** over three trials (that child is visible in ``/proc``
  within ~6 ms of launch and exits ~1 ms after ``SIGTERM``, status -15, so the
  grace is never waited); the FIFO means nothing else can end the probe.

``os.kill(pid, 0) -> ProcessLookupError`` is the reap check throughout: a child
that was killed but never ``wait()``-ed stays a zombie that ``os.kill`` answers.
Every trial above checked that, and that no ``ffprobe`` for the probe's own path
survives in ``/proc``.
"""

from __future__ import annotations

import errno
import os
import sys
import threading
import time
from fractions import Fraction as F
from pathlib import Path

import pytest

from media_fixtures import HAS_FFMPEG
from video_editor.media import importer as importer_module
from video_editor.media.assets import AssetMetadata, StreamKind, StreamMetadata
from video_editor.media.ffprobe import CANCELLED_MESSAGE, ProbeResult, probe_file
from video_editor.media.importer import ImportController, ImportState
from video_editor.media.process import ProcessCancelled, run_process

#: How long the child fixtures sleep: long enough that only a cancellation, never
#: the child's own course of action, can end the job.
CHILD_SLEEP_SECONDS = 60
CHILD_TIMEOUT_SECONDS = 120

#: Hand-built probe metadata for the fake adapter: nothing about it comes from the
#: parser under test, so "published" and "refused" are decided by the controller.
FAKE_METADATA = AssetMetadata(
    format_name="fake",
    container_duration=F(6),
    container_start=F(0),
    streams=(
        StreamMetadata(
            stream_index=0,
            kind=StreamKind.VIDEO,
            codec="fake",
            language=None,
            frame_rate=F(25),
            nominal_frame_rate=F(25),
            width=128,
            height=96,
            sample_rate=None,
            channels=None,
            channel_layout=None,
            start_time=F(0),
            duration=F(6),
            time_base=F(1, 25),
            nb_frames=150,
            duration_source="hand-built",
        ),
    ),
)


@pytest.fixture(autouse=True)
def _no_runner_thread_outlives_the_test():
    """Every job this file starts joins its own drain threads before it finishes."""
    yield
    left = sorted(
        t.name for t in threading.enumerate() if t.name.startswith("process-runner-")
    )
    assert left == [], "a subprocess drain thread outlived the import job that started it"


# --------------------------------------------------------------------- helpers
def files(tmp_path: Path, count: int, stem: str = "clip") -> list[Path]:
    """Real, regular files: the controller's existence check must be satisfiable."""
    paths = []
    for index in range(count):
        path = tmp_path / f"{stem}-{index}.bin"
        path.write_bytes(b"\x00\x01\x02")
        paths.append(path)
    return paths


class Harness:
    """A controller plus recorded callbacks, so "exactly one completion" is countable.

    Callbacks run on worker threads, so every read and write of the records takes
    a lock. Nothing here calls back into the controller: the harness only writes
    things down, which keeps test-thread and worker-thread interleavings simple.
    """

    def __init__(self, **kwargs) -> None:
        self.started: list[str] = []
        self.completions: dict[str, list[tuple[object, str | None]]] = {}
        self._lock = threading.Lock()
        #: paths whose completion callback explodes (set before submitting: the
        #: job ids do not exist yet, and a worker may finish a job immediately)
        self.failing_callback_paths: set[Path] = set()
        self.controller = ImportController(
            on_started=self._started, on_finished=self._finished, **kwargs
        )

    def _started(self, job) -> None:
        with self._lock:
            self.started.append(job.id)

    def _finished(self, job, asset, error) -> None:
        if job.path in self.failing_callback_paths:
            raise RuntimeError("the completion callback exploded")
        with self._lock:
            self.completions.setdefault(job.id, []).append((asset, error))

    def completions_for(self, job_id: str) -> list[tuple[object, str | None]]:
        with self._lock:
            return list(self.completions.get(job_id, ()))

    def completion_count(self) -> int:
        with self._lock:
            return sum(len(entries) for entries in self.completions.values())

    def started_count(self) -> int:
        with self._lock:
            return len(self.started)

    def await_completion(self, job_id: str, within: float = 30.0) -> tuple[object, str | None]:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            entries = self.completions_for(job_id)
            if entries:
                return entries[0]
            time.sleep(0.002)
        raise AssertionError(f"job {job_id} emitted no completion within {within} s")

    def assert_exactly_one_each(self, job_ids) -> None:
        ids = list(job_ids)
        for job_id in ids:
            entries = self.completions_for(job_id)
            assert len(entries) == 1, f"job {job_id} emitted {len(entries)} completions: {entries}"
        total = self.completion_count()
        assert total == len(ids), f"{total} completions for {len(ids)} jobs: a duplicate somewhere"


class FakeProbe:
    """Stands in for ``probe_file``: measures real overlap and controls the answer.

    ``gate`` holds every call inside the fake until the test releases it, which is
    how a test both observes the in-flight count and decides when jobs finish.
    """

    def __init__(
        self,
        gate: threading.Event | None = None,
        *,
        default: ProbeResult | None = None,
        per_path: dict[Path, ProbeResult] | None = None,
        raising: tuple[Path, ...] = (),
    ) -> None:
        if gate is None:
            self.gate = threading.Event()
            self.gate.set()  # by default the fake does not block at all
        else:
            self.gate = gate
        self.default = default or ProbeResult(
            path=Path(), metadata=FAKE_METADATA, error=None
        )
        self.per_path = dict(per_path or {})
        self.raising = set(raising)
        self.calls: list[tuple[Path, threading.Event | None]] = []
        self._live = 0
        self._lock = threading.Lock()
        self.peak = 0

    @property
    def live(self) -> int:
        with self._lock:
            return self._live

    def paths(self) -> list[Path]:
        with self._lock:
            return [path for path, _ in self.calls]

    def events(self) -> dict[Path, threading.Event | None]:
        with self._lock:
            return {path: event for path, event in self.calls}

    def __call__(self, path, ffprobe_path: str = "ffprobe", *, cancel=None) -> ProbeResult:
        target = Path(path)
        with self._lock:
            self.calls.append((target, cancel))
            self._live += 1
            self.peak = max(self._live, self.peak)
        try:
            self._await_gate(cancel)
            if cancel is not None and cancel.is_set():
                # the same answer the real adapter gives when the runner reports a
                # cancelled child: no metadata, the one shared message
                return ProbeResult(path=target, metadata=None, error=CANCELLED_MESSAGE)
            if target in self.raising:
                raise RuntimeError(f"fake probe exploded for {target.name}")
            return self.per_path.get(target, self.default)
        finally:
            with self._lock:
                self._live -= 1

    def _await_gate(self, cancel: threading.Event | None) -> None:
        """Waits for the test's gate, watching the job's token as the runner does.

        A fake probe that ignored the token would not be testing cancellation at
        all: it is the token, not the gate, that has to end a running job.
        """
        deadline = time.monotonic() + 60
        while not self.gate.is_set():
            if cancel is not None and cancel.is_set():
                return
            assert time.monotonic() < deadline, "the test never released its probe gate"
            time.sleep(0.005)


def child_body(marker: Path, *, ignores_term: bool) -> str:
    """A silent child that reports its own pid, then sleeps for a century.

    A fixture that promises a signal disposition installs it **before** writing
    its marker, because the marker is the test's readiness signal: reporting
    first leaves a window in which a cancellation reaches a child that still has
    ``SIGTERM``'s default action (the TASK_13 flake, proved in
    ``/tmp/opencode/m13/fixture_order_probe.log``).
    """
    disposition = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignores_term else ""
    return (
        "import os, signal, sys, time\n"
        + disposition
        + f"with open({str(marker)!r}, 'w') as handle:\n"
        "    handle.write(str(os.getpid()))\n"
        + f"time.sleep({CHILD_SLEEP_SECONDS})\n"
    )


def child_probe(body: str):
    """A fake probe that starts a **real** child through the TASK_13 runner.

    It mirrors what ``probe_file`` actually does - hand the job's Event to
    ``run_process``, translate ``ProcessCancelled`` into the cancelled answer -
    so the controller's plumbing is tested against a real process while the
    command line stays under the test's control.
    """
    calls: list[tuple[Path, threading.Event | None]] = []

    def run(path, ffprobe_path: str = "ffprobe", *, cancel=None) -> ProbeResult:
        target = Path(path)
        calls.append((target, cancel))
        try:
            run_process(
                [sys.executable, "-c", body, str(target)],
                timeout=CHILD_TIMEOUT_SECONDS,
                cancel=cancel,
            )
        except ProcessCancelled:
            return ProbeResult(path=target, metadata=None, error=CANCELLED_MESSAGE)
        return ProbeResult(path=target, metadata=None, error="the child ended by itself")

    run.calls = calls
    return run


def read_reported_pid(marker: Path, within: float = 20.0) -> int:
    """The pid a child wrote about itself - independent proof that it exists."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            text = marker.read_text().strip()
        except OSError:
            text = ""
        if text.isdigit():
            return int(text)
        time.sleep(0.005)
    raise AssertionError(f"the child never reported its pid within {within} s")


def assert_reaped(pid: int) -> None:
    """The child is gone entirely: not running, and not an unreaped zombie either."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    pytest.fail(f"pid {pid} is still there: a live process or an unreaped zombie was left")


def live_ffprobe_pids(needle: str) -> list[int]:
    """Live processes whose argv names *needle*, read from ``/proc``.

    Deliberately not the adapter's own bookkeeping: this is how the real-tool test
    knows a child existed, and how it demands that the same child is gone.
    """
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        if needle in argv and "ffprobe" in argv:
            pids.append(int(entry.name))
    return pids


# ------------------------------------------------------------------- the bound
@pytest.mark.parametrize("workers", (1, 2, 3))
def test_thirty_jobs_run_never_more_than_the_worker_count_at_once(tmp_path, monkeypatch, workers):
    """The pool is a bound, not a suggestion: measured overlap equals the worker count.

    30 jobs, a gate that holds every probe inside the fake, and a peak counter that
    only ever moves up. The gate is released only once the test has seen
    ``workers`` probes in flight together, so the measurement covers the whole
    batch: ``peak`` must be exactly ``workers`` - more would be an unbounded pool,
    less would mean the workers never actually ran at once.
    """
    gate = threading.Event()
    gate.clear()
    probe = FakeProbe(gate=gate)
    monkeypatch.setattr(importer_module, "probe_file", probe)
    harness = Harness(max_workers=workers, max_pending=64)
    paths = files(tmp_path, 30)

    submission = harness.controller.import_paths(paths)
    assert len(submission.accepted) == 30
    assert submission.refused == ()
    assert harness.controller.capacity == workers + 64

    deadline = time.monotonic() + 20
    while probe.live < workers and time.monotonic() < deadline:
        time.sleep(0.002)
    assert probe.live == workers, f"{probe.live} probes in flight, expected {workers}"
    assert harness.controller.pending_count() == 30
    assert harness.started_count() == workers, "a worker began a second job while blocked"

    gate.set()
    assert harness.controller.wait(timeout=30), "the batch never drained"
    assert probe.peak == workers, f"measured maximum in-flight was {probe.peak}"
    harness.assert_exactly_one_each(job.id for job in submission.accepted)
    for job in submission.accepted:
        asset, error = harness.completions_for(job.id)[0]
        assert error is None and asset is not None
        assert job.state is ImportState.FINISHED
    # the token each probe received is the job's own: that is the whole of the
    # controller's cancellation plumbing, asserted for all 30 jobs
    events = probe.events()
    for job in submission.accepted:
        assert events[job.path] is job.cancel_event
    assert harness.controller.pending_count() == 0


# ------------------------------------------------------------- queued cancel
def test_cancelling_a_queued_job_starts_no_probe_and_frees_its_place(tmp_path, monkeypatch):
    """A job cancelled before a worker takes it is removed from the queue.

    One worker, three accepted jobs, and a gate that keeps the first inside the
    fake: the other two can only be queued. Cancelling them must (a) complete them
    immediately as cancelled, (b) never call the probe for them, and (c) give
    their budget back, which is what "removed" rather than "skipped later" means -
    proven here by the two new submissions that then fit, and the third that does
    not.
    """
    gate = threading.Event()
    gate.clear()
    probe = FakeProbe(gate=gate)
    monkeypatch.setattr(importer_module, "probe_file", probe)
    harness = Harness(max_workers=1, max_pending=2)  # capacity 3
    first, second, third = files(tmp_path, 3, "queued")

    submission = harness.controller.import_paths([first, second, third])
    assert len(submission.accepted) == 3
    deadline = time.monotonic() + 20
    while probe.live < 1 and time.monotonic() < deadline:
        time.sleep(0.002)
    assert probe.live == 1, "the only worker never reached the fake probe"

    queued = [job for job in submission.accepted[1:]]
    for job in queued:
        assert job.state is ImportState.QUEUED
        assert harness.controller.cancel(job.id) is True
        # emitted by cancel() itself: no worker will ever run this job
        assert len(harness.completions_for(job.id)) == 1
        asset, error = harness.completions_for(job.id)[0]
        assert asset is None and error == CANCELLED_MESSAGE
        assert job.state is ImportState.FINISHED
    assert harness.controller.pending_count() == 1, "a cancelled queued job stayed registered"
    assert probe.paths() == [first], "a cancelled queued job still probed"

    # capacity released: two more fit into the freed places, a third is refused
    follow_up = harness.controller.import_paths(files(tmp_path, 3, "extra"))
    assert len(follow_up.accepted) == 2
    assert len(follow_up.refused) == 1

    gate.set()
    assert harness.controller.wait(timeout=30)
    harness.assert_exactly_one_each(
        [job.id for job in list(submission.accepted) + list(follow_up.accepted)]
    )
    assert harness.controller.pending_count() == 0
    probed = sorted(str(path) for path in probe.paths())
    expected = sorted(str(job.path) for job in [submission.accepted[0], *follow_up.accepted])
    assert probed == expected, f"the probe ran for {probed}"
    assert len(probe.calls) == 3


# ----------------------------------------------------------------- refusal
def test_a_full_queue_is_refused_by_name_not_queued(tmp_path, monkeypatch):
    """Over capacity is an answer at submission time, never silent queuing.

    Capacity is ``max_workers + max_pending`` = 3. Five submissions give three
    jobs and name the two that do not fit, in order. A refusal starts no probe,
    emits no completion, enters no registry, and is re-offerable once the queue
    drains - so the owner is never left holding a file nothing will report on.
    """
    gate = threading.Event()
    gate.clear()
    probe = FakeProbe(gate=gate)
    monkeypatch.setattr(importer_module, "probe_file", probe)
    harness = Harness(max_workers=1, max_pending=2)
    paths = files(tmp_path, 5, "full")

    submission = harness.controller.import_paths(paths)
    assert [job.path for job in submission.accepted] == paths[:3]
    assert list(submission.refused) == paths[3:]
    assert harness.controller.pending_count() == 3
    assert harness.completion_count() == 0, "a refusal emitted a completion anyway"

    gate.set()
    assert harness.controller.wait(timeout=30)
    harness.assert_exactly_one_each(job.id for job in submission.accepted)
    assert probe.paths() == paths[:3], "a refused path was probed anyway"
    # drained: the budget is free again, so the same paths are accepted now
    again = harness.controller.import_paths(paths[3:])
    assert len(again.accepted) == 2 and again.refused == ()
    assert harness.controller.wait(timeout=30)


# ------------------------------------------------------------- running cancel
def test_cancelling_a_running_import_terminates_and_reaps_the_child(tmp_path, monkeypatch):
    """A running job's Event reaches a real process, which is then stopped and waited.

    Bound: the completion arrives under 1.0 s after ``cancel()``.  Measured for this
    exact path: **0.0084-0.0087 s** over three trials - the runner polls every
    0.02 s and a plain child dies 1.3 ms after ``SIGTERM`` (TASK_13
    ``term_probe.log``) - so the bound is ~115x what actually happens, while this
    child sleeps 60 s, which is what an ignored request would need.  The pid comes
    from the child's own marker file and must then answer ``ProcessLookupError``:
    killed *and* reaped, no zombie left.
    """
    marker = tmp_path / "child.pid"
    (job_path,) = files(tmp_path, 1, "running")
    probe = child_probe(child_body(marker, ignores_term=False))
    monkeypatch.setattr(importer_module, "probe_file", probe)
    harness = Harness(max_workers=1, max_pending=4)
    (job,) = harness.controller.import_paths([job_path]).accepted

    pid = read_reported_pid(marker)
    assert job.state is ImportState.RUNNING, "the child reported a pid but the job is not running"
    requested = time.monotonic()
    assert harness.controller.cancel(job.id) is True
    _, error = harness.await_completion(job.id, within=15)
    answered = time.monotonic() - requested

    assert error == CANCELLED_MESSAGE
    assert harness.completions_for(job.id)[0][0] is None, "a cancelled probe published metadata"
    assert answered < 1.0, f"the cancellation was answered {answered:.3f} s later"
    assert len(probe.calls) == 1, "the job probed more than once"
    assert probe.calls[0][1] is job.cancel_event, "the probe got some other cancellation token"
    assert harness.controller.pending_count() == 0
    assert harness.controller.wait(timeout=5) is True
    assert job.settled and job.state is ImportState.FINISHED
    assert_reaped(pid)


def test_cancelling_a_running_import_kills_a_child_that_ignores_sigterm(tmp_path, monkeypatch):
    """A stubborn child cannot stretch a cancellation: the grace is bounded.

    Bounds around ``DEFAULT_TERMINATE_GRACE`` = 2.0 s, measured from the instant
    ``cancel()`` is called.  Measured for this path: **2.008-2.020 s** over three
    trials (grace + poll + reap).  At least 1.9 s is what proves the grace really
    was waited instead of going straight to ``SIGKILL``, which would have answered
    in about 0.01 s; under 4.0 s is ~2x the measurement and ~15x below the 60 s the
    fixture sleeps, so a child that ignores ``SIGTERM`` cannot stretch a cancel.
    """
    marker = tmp_path / "stubborn.pid"
    (job_path,) = files(tmp_path, 1, "stubborn")
    probe = child_probe(child_body(marker, ignores_term=True))
    monkeypatch.setattr(importer_module, "probe_file", probe)
    harness = Harness(max_workers=1, max_pending=4)
    (job,) = harness.controller.import_paths([job_path]).accepted

    pid = read_reported_pid(marker)
    requested = time.monotonic()
    assert harness.controller.cancel(job.id) is True
    _, error = harness.await_completion(job.id, within=30)
    answered = time.monotonic() - requested

    assert error == CANCELLED_MESSAGE
    assert 1.9 <= answered < 4.0, f"TERM-ignoring child was answered in {answered:.3f} s"
    assert harness.controller.pending_count() == 0
    assert_reaped(pid)


def test_cancel_all_drops_queued_jobs_and_stops_running_ones(tmp_path, monkeypatch):
    """The owner's exit path: ``cancel_all`` then ``wait`` leaves nothing live."""
    gate = threading.Event()
    gate.clear()
    probe = FakeProbe(gate=gate)
    monkeypatch.setattr(importer_module, "probe_file", probe)
    harness = Harness(max_workers=2, max_pending=16)
    submission = harness.controller.import_paths(files(tmp_path, 10, "all"))

    deadline = time.monotonic() + 20
    while probe.live < 2 and time.monotonic() < deadline:
        time.sleep(0.002)
    harness.controller.cancel_all()
    assert harness.controller.wait(timeout=15), "cancel_all left a job live"

    harness.assert_exactly_one_each(job.id for job in submission.accepted)
    for job in submission.accepted:
        asset, error = harness.completions_for(job.id)[0]
        assert asset is None and error == CANCELLED_MESSAGE
    assert harness.controller.pending_count() == 0
    assert probe.peak == 2, f"measured in-flight peak was {probe.peak}"
    assert len(probe.calls) == 2, f"{len(probe.calls)} probes started for a cancel_all of 10 jobs"


# ------------------------------------------------------ no partial publication
def test_a_cancelled_probe_never_publishes_what_it_had_reached(tmp_path, monkeypatch):
    """A request that arrives as the probe answers is still honoured.

    The fake sets the job's own Event and then hands back a *successful* answer,
    which is exactly the race a truncated or half-read probe would produce. Nothing
    is published: the job completes as cancelled, once.
    """
    (job_path,) = files(tmp_path, 1, "late")
    events_seen: list[threading.Event] = []

    def racing_probe(path, ffprobe_path: str = "ffprobe", *, cancel=None) -> ProbeResult:
        assert cancel is not None
        events_seen.append(cancel)
        cancel.set()  # the request arrives before the answer is handed back
        return ProbeResult(path=Path(path), metadata=FAKE_METADATA, error=None)

    monkeypatch.setattr(importer_module, "probe_file", racing_probe)
    harness = Harness(max_workers=1, max_pending=4)
    (job,) = harness.controller.import_paths([job_path]).accepted

    assert harness.controller.wait(timeout=15)
    asset, error = harness.await_completion(job.id, within=5)
    assert asset is None, "a cancelled probe published an asset"
    assert error == CANCELLED_MESSAGE
    assert events_seen and events_seen[0] is job.cancel_event
    harness.assert_exactly_one_each([job.id])


@pytest.mark.parametrize(
    ("metadata", "error", "expect_asset"),
    (
        (FAKE_METADATA, None, True),  # a complete answer is published
        (FAKE_METADATA, "ffprobe returned malformed JSON", False),  # contradictory
        (None, None, False),  # nothing to publish and no reason given
    ),
    ids=("complete", "metadata-with-an-error", "nothing-at-all"),
)
def test_only_a_complete_error_free_probe_becomes_an_asset(
    tmp_path, monkeypatch, metadata, error, expect_asset
):
    """Partial or contradictory probe data is refused with a named reason.

    The asset is the one thing an import may publish, so an answer that is both a
    result and an error is treated as no result, and an answer with neither is
    refused rather than published as empty metadata.
    """
    (job_path,) = files(tmp_path, 1, "partial")
    probe = FakeProbe(default=ProbeResult(path=job_path, metadata=metadata, error=error))
    monkeypatch.setattr(importer_module, "probe_file", probe)
    harness = Harness(max_workers=1, max_pending=4)
    (job,) = harness.controller.import_paths([job_path]).accepted

    assert harness.controller.wait(timeout=15)
    asset, completion_error = harness.await_completion(job.id, within=5)
    if expect_asset:
        assert asset is not None and completion_error is None
        assert asset.metadata is FAKE_METADATA
        assert asset.path == job_path and asset.size_bytes == 3
    else:
        assert asset is None
        assert completion_error, f"no reason given for refusing: {completion_error!r}"
        if error:
            assert completion_error == error
    harness.assert_exactly_one_each([job.id])
    assert job.state is ImportState.FINISHED


def test_success_and_failure_each_leave_no_registry_entry(tmp_path, monkeypatch):
    """Both ordinary outcomes clean the registry and emit exactly one completion."""
    good, broken = files(tmp_path, 2, "outcome")
    probe = FakeProbe(
        per_path={
            good: ProbeResult(path=good, metadata=FAKE_METADATA, error=None),
            broken: ProbeResult(path=broken, metadata=None, error="ffprobe failed: bad file"),
        }
    )
    monkeypatch.setattr(importer_module, "probe_file", probe)
    harness = Harness(max_workers=2, max_pending=8)
    submission = harness.controller.import_paths([good, broken])

    assert harness.controller.wait(timeout=15)
    good_job, broken_job = submission.accepted
    assert harness.completions_for(good_job.id)[0][1] is None
    asset, error = harness.completions_for(broken_job.id)[0]
    assert asset is None
    assert error == "ffprobe failed: bad file"
    harness.assert_exactly_one_each([good_job.id, broken_job.id])
    assert harness.controller.pending_count() == 0
    # cancelling a finished job is a plain "no", and adds no second completion
    assert harness.controller.cancel(good_job.id) is False
    assert harness.controller.cancel("never-a-job-id") is False
    harness.assert_exactly_one_each([good_job.id, broken_job.id])


# --------------------------------------------------------- worker resilience
def test_a_worker_that_raises_completes_its_job_and_keeps_the_queue_moving(
    tmp_path, monkeypatch
):
    """One broken job cannot strand the ones behind it.

    One worker only, so every later job depends on this one's thread surviving. The
    fault is reported as that job's error and the other three still complete,
    each exactly once.
    """
    exploded, rest_a, rest_b, rest_c = files(tmp_path, 4, "raising")
    probe = FakeProbe(raising=(exploded,))
    monkeypatch.setattr(importer_module, "probe_file", probe)
    harness = Harness(max_workers=1, max_pending=8)
    submission = harness.controller.import_paths([exploded, rest_a, rest_b, rest_c])

    assert harness.controller.wait(timeout=20), "a raising worker stranded the queue"
    harness.assert_exactly_one_each(job.id for job in submission.accepted)
    asset, error = harness.completions_for(submission.accepted[0].id)[0]
    assert asset is None
    assert "fake probe exploded" in error
    for job in submission.accepted[1:]:
        asset, error = harness.completions_for(job.id)[0]
        assert asset is not None and error is None
    assert harness.controller.pending_count() == 0


def test_a_completion_callback_that_raises_does_not_kill_the_worker(tmp_path, monkeypatch):
    """The callback belongs to the UI, so its failure cannot stop the pool either."""
    first, second = files(tmp_path, 2, "callback")
    probe = FakeProbe()
    monkeypatch.setattr(importer_module, "probe_file", probe)
    harness = Harness(max_workers=1, max_pending=8)
    harness.failing_callback_paths.add(first)  # decided before submitting: no race with the worker
    submission = harness.controller.import_paths([first, second])

    assert harness.controller.wait(timeout=20), "a raising callback stranded the queue"
    assert len(harness.completions_for(submission.accepted[1].id)) == 1
    assert harness.controller.pending_count() == 0
    # the failed job is settled all the same: no retry, no second completion
    assert submission.accepted[0].settled
    assert len(harness.completions_for(submission.accepted[0].id)) == 0


# ------------------------------------------------------------- real ffprobe
def _hold_fifo_open(path: Path, stop: threading.Event) -> None:
    """Holds a FIFO open for writing without ever sending a byte.

    ``ffprobe`` therefore blocks in its read and cannot finish on its own, which
    is the only way this test can be sure a completion came from the cancellation.
    ``O_NONBLOCK`` keeps this thread honest: opening for write fails at once
    (``ENXIO``) while no reader has the FIFO yet, so the loop retries instead of
    parking in ``open()`` for ever if the probe dies before it reads.
    """
    fd = -1
    while fd < 0 and not stop.is_set():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno != errno.ENXIO:
                return
            stop.wait(0.02)
    if fd < 0:
        return
    try:
        while not stop.is_set():
            stop.wait(0.2)  # never write: the reader must have nothing to read
    finally:
        os.close(fd)


class _ProbeChildWatcher(threading.Thread):
    """Sets the cancel token once a real ``ffprobe`` for *needle* exists."""

    def __init__(self, needle: str) -> None:
        super().__init__(name="test-probe-watcher", daemon=True)
        self.needle = needle
        self.event = threading.Event()
        self.pids: list[int] = []
        self.set_at: float | None = None

    def run(self) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            pids = live_ffprobe_pids(self.needle)
            if pids:
                self.pids = pids
                self.set_at = time.monotonic()
                self.event.set()
                return
            time.sleep(0.005)


@pytest.mark.skipif(not HAS_FFMPEG, reason="a real ffprobe is needed to cancel one")
def test_cancelling_a_real_ffprobe_probe_kills_the_child(tmp_path):
    """The adapter's Event stops and reaps the actual tool, not a stand-in.

    The input is a FIFO whose writer never sends a byte, so real ``ffprobe`` is
    blocked in its read and would sit there until the 120 s deadline.  The child is
    found from ``/proc``, and the same pids must answer ``ProcessLookupError``
    afterwards.  Bound: the answer arrives under 1.5 s of the request - measured
    **0.005-0.018 s** over three trials (``/tmp/opencode/m14/measure_bounds.log``:
    0.02 s poll plus a ~1 ms death after ``SIGTERM``, status -15, so the 2.0 s
    grace is never waited), i.e. ~80x headroom on a job that otherwise never ends.
    """
    fifo = tmp_path / "never-arrives.probe"
    os.mkfifo(fifo)
    stop = threading.Event()
    holder = threading.Thread(
        target=_hold_fifo_open, args=(fifo, stop), name="fifo-holder", daemon=True
    )
    holder.start()
    watcher = _ProbeChildWatcher(str(fifo))
    watcher.start()
    try:
        result = probe_file(fifo, cancel=watcher.event)
        answered_from_request = time.monotonic() - (watcher.set_at or time.monotonic())
    finally:
        stop.set()
        holder.join(timeout=5)

    assert not holder.is_alive(), "the thread holding the FIFO open never finished"
    watcher.join(timeout=25)
    assert watcher.set_at is not None, "no real ffprobe child ever started for the FIFO"
    assert result.metadata is None
    assert result.error == CANCELLED_MESSAGE
    assert not result.ok
    assert answered_from_request < 1.5, f"real probe cancelled in {answered_from_request:.3f} s"
    assert watcher.pids, "the watcher saw no pid"
    for pid in watcher.pids:
        assert_reaped(pid)
    assert live_ffprobe_pids(str(fifo)) == [], "an ffprobe child for this probe is still alive"
