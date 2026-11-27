"""The cancellable subprocess runner: cancellation, deadlines and bounded capture.

The fixtures are small Python child programs started from an argv list - no
FFmpeg, no shell, nothing that touches media.  Every timing bound below states the
number it asserts and why that is the number; none of them is copied from a
constant of the module under test, and wherever the runner's own timing is what is
under test the wall clock is measured and asserted as well.  The environment facts
come from the probe runs kept in ``/tmp/opencode/m13/`` (``pipe_probe.log``,
``term_probe.log``), measured on this machine before the runner existed:

* **a Linux pipe holds 65 536 bytes** before its writer blocks (``select.PIPE_BUF``
  is 4 096), so a flood fixture has to be several times that to prove both pipes
  are drained at the same time - a serial reader deadlocks;
* a plain child dies **1.3 ms** after ``SIGTERM`` with status **-15**; a child with
  a ``SIGTERM`` handler answers in **3.3 ms** with *its own* status, which is how
  "TERM reached it, KILL was not needed" is told apart from a killed child (-9);
* a child that **ignores** ``SIGTERM`` is still alive when a 0.5 s grace expires and
  dies **501.8 ms** after the stop request, with status **-9**;
* after a successful ``wait()``, ``os.kill(pid, 0)`` raises ``ProcessLookupError``.
  That is the check used here for "reaped, and no orphan": a child that was never
  waited for stays a zombie that ``os.kill`` still answers to.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from video_editor.media.process import (
    CapturePolicy,
    Keep,
    ProcessCallbackFailed,
    ProcessCancelled,
    ProcessLaunchError,
    ProcessOverflow,
    ProcessResult,
    ProcessTimeout,
    run_process,
)

# The documented drain-thread names: used to prove both streams are drained at
# once, and that no drain thread outlives the job that started it.
RUNNER_THREAD_PREFIX = "process-runner-"
STDOUT_THREAD_NAME = f"{RUNNER_THREAD_PREFIX}stdout"

FLOOD_LINES = 40_000  # ~262 KiB per stream: four pipe-fulls of the measured 64 KiB
FLOOD_TAIL = 2_048  # capture limit this test asks for
BULK_BYTES = 256 * 800  # 204 800 bytes of binary stdout, written by the fixture
ENDLESS_BYTES = 4 * 1024 * 1024  # 4 MiB with no newline anywhere
MAX_LINE = 1 << 20  # the documented ceiling on one line handed to a callback


@pytest.fixture(autouse=True)
def _no_runner_thread_outlives_the_test():
    """Every job joins its own drain threads before it returns."""
    yield
    assert live_runner_threads() == [], "a drain thread outlived the job that started it"


def live_runner_threads() -> list[str]:
    return sorted(
        t.name for t in threading.enumerate() if t.name.startswith(RUNNER_THREAD_PREFIX)
    )


def child(body: str, marker: Path | None = None) -> list[str]:
    """argv for a Python child running *body*; ``sys.argv[1]`` is *marker*."""
    argv = [sys.executable, "-c", body]
    if marker is not None:
        argv.append(str(marker))
    return argv


#: the child records its own pid; that file is how a test knows the process exists.
REPORT_PID_LINES = (
    "with open(sys.argv[1], 'w') as handle:\n"
    "    handle.write(str(os.getpid()))\n"
)
REPORT_PID = "import os, sys\n" + REPORT_PID_LINES
SLEEP_FOREVER = "import time\ntime.sleep(30)\n"

#: Fixtures that promise a specific SIGTERM disposition install it *before* they
#: report their pid, because that marker is the test's readiness signal: reporting
#: first lets a cancellation land while the child still has the default
#: disposition.  That was measured as a flake - the child died with -15 where the
#: test expected -9 - and the mechanism is proved by
#: ``/tmp/opencode/m13/fixture_order_probe.log``: with this ordering a deliberate
#: 60 ms window still answers -9 in 10 of 10 runs, and without it 10 of 10 give
#: -15.
HANDLES_TERM = (
    "import os, signal, sys, time\n"
    "def stop(signum, frame):\n"
    "    sys.stderr.write('caught-term\\n'); sys.stderr.flush(); os._exit(7)\n"
    "signal.signal(signal.SIGTERM, stop)\n"
    + REPORT_PID_LINES
    + "time.sleep(30)\n"
)
IGNORES_TERM = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    + REPORT_PID_LINES
    + "time.sleep(30)\n"
)


def read_reported_pid(marker: Path, within: float = 15.0) -> int:
    """The pid a child wrote about itself, once the write has landed.

    Reading it from the child's own record - not from the runner's bookkeeping -
    is what makes the orphan check independent of the code under test.
    """
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            text = marker.read_text().strip()
        except OSError:
            text = ""
        if text.isdigit():
            return int(text)
        time.sleep(0.01)
    raise AssertionError(f"the child never reported its pid within {within} s")


def assert_reaped_and_no_orphan(pid: int) -> None:
    """The child must be gone entirely, not a zombie of this test process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    pytest.fail(f"pid {pid} is still there: a live process or an unreaped zombie was left")


class Canceller:
    """Sets the shared Event from another thread, once the child exists.

    That is the shape a real cancellation has: the runner blocks in its worker
    while somebody else sets the Event.  ``set_at`` is the instant it was set, so
    a test can measure how long the runner took to answer; the bounds are about
    that interval, not about the length of the fixture.
    """

    def __init__(self, marker: Path) -> None:
        self.event = threading.Event()
        self.set_at: float | None = None
        self._marker = marker
        self._thread = threading.Thread(target=self._run, name="test-canceller", daemon=True)

    def _run(self) -> None:
        read_reported_pid(self._marker)  # past this the process certainly exists
        self.set_at = time.monotonic()
        self.event.set()

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        self._thread.join(timeout=10)
        assert not self._thread.is_alive(), "the helper thread that cancels did not finish"


def test_a_child_that_only_sleeps_still_gets_its_status() -> None:
    """No output at all must not be mistaken for "finished", nor hang the runner.

    Bound: elapsed >= 0.30 s, because the fixture sleeps 0.35 s by itself - a
    runner that treats two empty pipes as completion answers at once and fails
    here.  Upper bound 5 s: far above the sleep, far below the 20 s timeout, so a
    runner that waited on the wrong thing reports itself instead of blocking.
    """
    started = time.monotonic()
    result = run_process(child("import time\ntime.sleep(0.35)\n"), timeout=20)
    elapsed = time.monotonic() - started

    assert isinstance(result, ProcessResult)
    assert result.returncode == 0
    assert result.succeeded
    assert result.stdout == b""
    assert result.stderr == b""
    assert result.stdout_dropped == 0
    assert result.stderr_dropped == 0
    assert elapsed >= 0.30, f"answered in {elapsed:.3f} s, before the child's own sleep"
    assert elapsed < 5.0, f"{elapsed:.3f} s for a 0.35 s child"


def test_a_flood_on_both_pipes_is_drained_and_kept_bounded() -> None:
    """A child that floods both pipes is drained, and capture stays inside its limit.

    The fixture writes 262 KiB to each stream - over four times the measured
    65 536-byte pipe capacity - so a runner that reads one pipe and only then the
    other deadlocks with the child blocked in ``write``; here that would surface
    as a timeout instead of the exit status the child reaches.

    The arithmetic is the test's own: the expected streams are built here, the
    captured bytes must equal their last 2 048 bytes exactly (the tail, ending
    with the fixture's last line ``e39999``), and the dropped count is the
    subtraction that follows.  elapsed < 10 s against a 30 s timeout, so the run
    finished by draining rather than by waiting the deadline out.
    """
    expected_out = "".join(f"o{i}\n" for i in range(FLOOD_LINES))
    expected_err = "".join(f"e{i}\n" for i in range(FLOOD_LINES))
    assert len(expected_err) > 4 * 65_536, "the flood fixture is not bigger than a pipe"
    assert expected_err.endswith(f"e{FLOOD_LINES - 1}\n")
    body = (
        "import sys\n"
        f"for i in range({FLOOD_LINES}):\n"
        "    sys.stdout.write(f'o{i}\\n')\n"
        "    sys.stderr.write(f'e{i}\\n')\n"
    )
    started = time.monotonic()
    result = run_process(
        child(body),
        timeout=30,
        capture=CapturePolicy(stdout_limit=FLOOD_TAIL, stderr_limit=FLOOD_TAIL),
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, "the flood child must finish on its own, not be stopped"
    assert result.stderr == expected_err.encode()[-FLOOD_TAIL:]
    assert result.stderr.endswith(f"e{FLOOD_LINES - 1}\n".encode())
    assert result.stdout == expected_out.encode()[-FLOOD_TAIL:]
    assert len(result.stdout) == FLOOD_TAIL and len(result.stderr) == FLOOD_TAIL
    assert result.stdout_dropped == len(expected_out) - FLOOD_TAIL
    assert result.stderr_dropped == len(expected_err) - FLOOD_TAIL
    assert elapsed < 10.0, f"{elapsed:.3f} s: drained, or waited for the deadline?"


def test_stdout_lines_reach_the_callback_in_order_with_a_partial_last_line() -> None:
    """Complete lines in order, and the child's final partial line too.

    Order is asserted only *within* one stream: two independent pipes promise no
    cross-stream order.  A last line with no newline still has to be delivered -
    dropping it would lose the final message of a real FFmpeg run.  The callback
    also records how many runner threads are live while it is being called: two,
    one per stream, which is the concurrency that stops a full pipe from stalling
    the other one.
    """
    seen: list[str] = []
    threads: set[str] = set()
    live: list[int] = []

    def on_line(line: str) -> None:
        seen.append(line)
        threads.add(threading.current_thread().name)
        live.append(len(live_runner_threads()))

    body = (
        "import sys\n"
        "sys.stdout.write('out-1\\n'); sys.stdout.flush()\n"
        "sys.stderr.write('err-1\\n'); sys.stderr.flush()\n"
        "sys.stdout.write('out-2\\n'); sys.stdout.write('out-3'); sys.stdout.flush()\n"
        "sys.stderr.write('err-2\\n'); sys.stderr.flush()\n"
    )
    result = run_process(child(body), timeout=20, on_stdout_line=on_line)

    assert seen == ["out-1", "out-2", "out-3"]
    assert threads == {STDOUT_THREAD_NAME}, "the callback must run on the stdout drain thread"
    assert live and max(live) == 2, f"expected two drain threads, saw {live}"
    assert result.stdout == b"out-1\nout-2\nout-3"
    assert result.stderr == b"err-1\nerr-2\n"
    assert result.returncode == 0


def test_a_nonzero_exit_is_an_answer_with_its_diagnostics() -> None:
    """A failing program is reported, not raised: only the caller knows what 3 means."""
    body = "import sys\nsys.stderr.write('bad input name\\n'); sys.stderr.flush()\nsys.exit(3)\n"
    result = run_process(child(body), timeout=20)

    assert result.returncode == 3
    assert not result.succeeded
    assert "bad input name" in result.stderr_text
    assert result.stderr_dropped == 0
    assert result.stdout == b""


def test_cancelling_before_launch_starts_nothing(tmp_path: Path) -> None:
    """A job nobody wants any more is refused before a process exists.

    ``launched`` is false and the child's own marker file - its very first action -
    is never written, which proves that no process was started instead of trusting
    the runner's bookkeeping.  Nothing was waited for either: elapsed stays well
    under a second although the fixture would have slept 30 s.
    """
    marker = tmp_path / "child.pid"
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(ProcessCancelled) as caught:
        run_process(
            child(REPORT_PID + SLEEP_FOREVER, marker), timeout=20, cancel=cancel
        )

    assert caught.value.launched is False
    assert caught.value.returncode is None
    assert caught.value.stdout == b""
    assert caught.value.elapsed < 0.5
    assert not marker.exists(), "a cancelled-before-launch job started a child anyway"


def test_cancelling_a_running_child_stops_it_with_sigterm(tmp_path: Path) -> None:
    """Cancellation during execution terminates, reaps and reports exactly once.

    The fixture handles ``SIGTERM`` by writing a line and exiting **7**, its own
    status, so the reported status proves SIGTERM did the work: a child killed by
    ``SIGKILL`` would report -9 and would never have written that line.

    Bound: the runner answers within 1 s of the Event being set.  It polls every
    20 ms at worst and the measured handler answer is 3.3 ms, so 1 s is about 200x
    the path actually taken - while the fixture sleeps 30 s, which is what a runner
    that ignored the Event would need.
    """
    marker = tmp_path / "child.pid"
    canceller = Canceller(marker)
    canceller.start()
    try:
        with pytest.raises(ProcessCancelled) as caught:
            run_process(
                child(HANDLES_TERM, marker), timeout=30, cancel=canceller.event,
                terminate_grace=1.0,
            )
    finally:
        canceller.join()
    assert canceller.set_at is not None, "the child never started, so nothing was cancelled"
    answered = time.monotonic() - canceller.set_at

    assert caught.value.launched is True
    assert caught.value.returncode == 7
    assert b"caught-term" in caught.value.stderr
    assert answered < 1.0, f"cancellation took {answered:.3f} s to answer"
    assert_reaped_and_no_orphan(read_reported_pid(marker))


def test_a_child_that_ignores_sigterm_is_killed_within_the_grace(tmp_path: Path) -> None:
    """TERM, then a bounded grace, then KILL - and the grace is really waited.

    The fixture ignores ``SIGTERM``, so only ``SIGKILL`` can end it and the
    reported status must be **-9**, the one status that cannot be caught or faked.

    Bounds around the 0.5 s grace, measured from the instant the Event was set: at
    least 0.4 s - the grace less the scheduling skew between this thread's stamp
    and the runner starting its own grace - which is what proves the grace was
    actually waited instead of jumping straight to KILL (measured KILL-to-dead:
    ~1 ms); and at most 2 s, about 4x the measured 501.8 ms of the whole
    TERM+grace+KILL path, so a child sleeping 30 s cannot stretch a cancellation.
    """
    marker = tmp_path / "child.pid"
    canceller = Canceller(marker)
    canceller.start()
    try:
        with pytest.raises(ProcessCancelled) as caught:
            run_process(
                child(IGNORES_TERM, marker), timeout=30, cancel=canceller.event,
                terminate_grace=0.5,
            )
    finally:
        canceller.join()
    assert canceller.set_at is not None, "the child never started, so nothing was cancelled"
    answered = time.monotonic() - canceller.set_at

    assert caught.value.launched is True
    assert caught.value.returncode == -signal.SIGKILL
    assert 0.4 <= answered < 2.0, f"TERM-ignoring child answered in {answered:.3f} s"
    assert_reaped_and_no_orphan(read_reported_pid(marker))


def test_a_deadline_stops_a_child_that_says_nothing(tmp_path: Path) -> None:
    """The deadline is a wall-clock promise, independent of any output.

    The fixture sleeps 30 s and writes nothing to either pipe, so no read could
    ever return: only a clock can end this.  The measured status is **-15**, the
    default disposition of ``SIGTERM`` for a plain child (~1 ms, inside the 1 s
    grace), so the child was not merely declared over - it was stopped and reaped.

    Bound: elapsed >= 0.35 s, the 0.4 s deadline less scheduling slack: a runner
    that answered earlier did not honour it.  And < 2 s - deadline plus grace,
    nowhere near the 30 s sleep.
    """
    marker = tmp_path / "child.pid"
    started = time.monotonic()
    with pytest.raises(ProcessTimeout) as caught:
        run_process(
            child(REPORT_PID + SLEEP_FOREVER, marker), timeout=0.4, terminate_grace=1.0
        )
    elapsed = time.monotonic() - started

    assert caught.value.returncode == -signal.SIGTERM
    assert caught.value.stdout == b""
    assert 0.35 <= elapsed < 2.0, f"deadline honoured in {elapsed:.3f} s?"
    assert 0.35 <= caught.value.elapsed < 2.0
    assert_reaped_and_no_orphan(read_reported_pid(marker))


def test_a_raising_callback_still_stops_and_reaps_the_child(tmp_path: Path) -> None:
    """A callback that raises loses neither the process nor its own exception.

    The fixture prints six lines and then sleeps 30 s.  Raising on the third must
    stop the job: the later lines never arrive, the status is the ``SIGTERM``
    disposition (-15, measured at ~1 ms for a sleeping child), and the answer comes
    back in well under 3 s.  The caller's exception is kept as ``callback_error``
    rather than replaced by a generic message, and it stays the job's only outcome.
    """
    marker = tmp_path / "child.pid"
    seen: list[str] = []

    def on_line(line: str) -> None:
        seen.append(line)
        if line == "line-3":
            raise RuntimeError("callback boom")

    body = (
        REPORT_PID
        + "import time\n"
        "for i in range(1, 7):\n"
        "    sys.stdout.write(f'line-{i}\\n'); sys.stdout.flush()\n"
        "    time.sleep(0.05)\n"
        "time.sleep(30)\n"
        "sys.stdout.write('late\\n'); sys.stdout.flush()\n"
    )
    started = time.monotonic()
    with pytest.raises(ProcessCallbackFailed) as caught:
        run_process(child(body, marker), timeout=30, on_stdout_line=on_line)
    elapsed = time.monotonic() - started

    assert isinstance(caught.value.callback_error, RuntimeError)
    assert str(caught.value.callback_error) == "callback boom"
    assert seen == ["line-1", "line-2", "line-3"]
    assert b"line-6" not in caught.value.stdout
    assert b"late" not in caught.value.stdout, "the child ran on after its callback failed"
    assert caught.value.returncode == -signal.SIGTERM
    assert elapsed < 3.0, f"{elapsed:.3f} s to stop a child whose callback failed"
    assert_reaped_and_no_orphan(read_reported_pid(marker))


def test_bulk_binary_stdout_is_available_with_an_explicit_size_limit() -> None:
    """A caller that states its byte limit gets the whole binary payload back.

    The fixture writes exactly ``bytes(range(256)) * 800`` - 204 800 bytes, NULs
    and high bytes included - and the result is compared byte for byte against the
    same expression built here, so nothing was decoded, re-encoded or shortened.
    """
    expected = bytes(range(256)) * 800
    assert len(expected) == BULK_BYTES
    body = (
        "import sys\n"
        "sys.stdout.buffer.write(bytes(range(256)) * 800)\n"
        "sys.stdout.buffer.flush()\n"
    )
    result = run_process(
        child(body),
        timeout=20,
        capture=CapturePolicy(stdout_keep=Keep.STRICT, stdout_limit=BULK_BYTES + 100_000),
    )

    assert result.returncode == 0
    assert result.stdout == expected
    assert len(result.stdout) == BULK_BYTES
    assert result.stdout_dropped == 0


def test_bulk_stdout_over_its_stated_limit_is_refused_not_truncated(tmp_path: Path) -> None:
    """Too much for a strict stream is a named failure, never half a payload.

    The limit (100 000) is below the fixture's 204 800 bytes, so the job is refused
    naming the stream, the limit and the amount seen, while the head up to the
    limit is still reported byte for byte.  ``total`` is bounded by the fixture's
    own size; and because the child sleeps 20 s after writing, status -15 plus an
    answer in under 3 s shows the overflowing child was stopped, not drained.
    """
    marker = tmp_path / "child.pid"
    expected = bytes(range(256)) * 800
    body = (
        REPORT_PID
        + "import time\n"
        "sys.stdout.buffer.write(bytes(range(256)) * 800)\n"
        "sys.stdout.buffer.flush()\n"
        "time.sleep(20)\n"
    )
    started = time.monotonic()
    with pytest.raises(ProcessOverflow) as caught:
        run_process(
            child(body, marker),
            timeout=20,
            capture=CapturePolicy(stdout_keep=Keep.STRICT, stdout_limit=100_000),
        )
    elapsed = time.monotonic() - started

    assert caught.value.stream == "stdout"
    assert caught.value.limit == 100_000
    assert 100_000 <= caught.value.total <= BULK_BYTES
    assert caught.value.stdout == expected[:100_000]
    assert caught.value.returncode == -signal.SIGTERM
    assert elapsed < 3.0, f"{elapsed:.3f} s: the overflowing child was not stopped"
    assert_reaped_and_no_orphan(read_reported_pid(marker))


def test_a_stream_with_no_newline_is_streamed_in_bounded_pieces() -> None:
    """A 4 MiB newline-free stream cannot make one callback line grow.

    Every delivered piece is at most the documented 1 MiB line ceiling, and the
    pieces add up to all 4 194 304 bytes the fixture wrote, so the line buffer
    stays bounded *and* loses nothing.  Capture was set to 0 bytes, which separates
    streaming from capture: the callback saw everything, the result kept nothing,
    and all 4 MiB is accounted as dropped.
    """
    pieces: list[str] = []

    def on_line(line: str) -> None:
        pieces.append(line)

    body = (
        "import sys\n"
        f"payload = b'x' * {ENDLESS_BYTES}\n"
        "sys.stdout.buffer.write(payload)\nsys.stdout.buffer.flush()\n"
    )
    started = time.monotonic()
    result = run_process(
        child(body),
        timeout=30,
        on_stdout_line=on_line,
        capture=CapturePolicy(stdout_limit=0),
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0
    assert pieces, "nothing was streamed"
    assert max(len(piece) for piece in pieces) <= MAX_LINE
    assert sum(len(piece) for piece in pieces) == ENDLESS_BYTES
    assert result.stdout == b""
    assert result.stdout_dropped == ENDLESS_BYTES
    assert elapsed < 15.0, f"{elapsed:.3f} s for 4 MiB through a pipe"


def test_a_command_string_and_an_unusable_deadline_are_refused() -> None:
    """One string is not an argv, and a job without a usable deadline is not started."""
    with pytest.raises(ValueError, match="command string"):
        run_process("echo hi", timeout=10)
    with pytest.raises(ValueError, match="program"):
        run_process([], timeout=10)
    with pytest.raises(ValueError):
        run_process([sys.executable, "-c", "pass"], timeout=0)
    with pytest.raises(ValueError):
        run_process([sys.executable, "-c", "pass"], timeout=-1)
    with pytest.raises(ValueError):
        run_process([sys.executable, "-c", "pass"], timeout=10, terminate_grace=-0.5)
    with pytest.raises(ValueError):
        run_process(
            [sys.executable, "-c", "pass"], timeout=10, capture=CapturePolicy(stderr_limit=-1)
        )
    assert isinstance(run_process([sys.executable, "-c", "pass"], timeout=10), ProcessResult)


def test_a_missing_program_is_reported_without_a_process() -> None:
    """Failure to start is its own outcome: no status, no output, nothing to reap."""
    with pytest.raises(ProcessLaunchError) as caught:
        run_process(["video-editor-not-a-real-program-9c2f7"], timeout=10)

    assert caught.value.returncode is None
    assert caught.value.stdout == b""
    assert "video-editor-not-a-real-program-9c2f7" in str(caught.value)
    assert live_runner_threads() == []
