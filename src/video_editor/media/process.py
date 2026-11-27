"""One cancellable subprocess runner: the only place a background job owns a process.

TASK_13 builds the primitive; no caller uses it yet. Every existing path in
``media/`` and ``render/`` still runs its own ``subprocess`` call, and moving them
onto this runner is the rest of group B. Nothing in this module knows about media,
FFmpeg, destinations or temporary files: it owns a process and that process's two
pipes, and nothing else, so a caller's staging files stay the caller's business.

Each rule below is tied to an environment fact measured for it (the probe scripts
and their output are in ``/tmp/opencode/m13/``; the timing bounds asserted in
``tests/test_process_runner.py`` come from the same numbers):

**Synchronous, callable from a worker thread.**
    ``run_process`` blocks until the job has exactly one terminal outcome, so the
    caller's worker thread *is* the job's owner and cancellation is a shared
    :class:`threading.Event` rather than an application-specific protocol. No Qt,
    no thread pool, no future: exactly two drain threads per job, both joined
    before it returns, named ``process-runner-stdout`` and
    ``process-runner-stderr``.

**argv list, never a shell string.**
    Passing one string is refused by name. ``/bin/sh -c`` would turn argument text
    into syntax, and FFmpeg filter graphs are full of quotes, commas, ``$`` and
    pipes that cannot be quoted safely by hand.

**The wait is never tied to output.**
    A Linux pipe holds 65 536 bytes before its writer blocks (measured:
    ``pipe_probe.log``; ``select.PIPE_BUF`` is 4 096), so a runner that reads one
    pipe at a time deadlocks as soon as the other one fills, and a runner that
    blocks in ``read()`` cannot notice a deadline or a cancel request at all.
    Both pipes are drained by their own thread to EOF, and the wait loop only
    ever polls: cancellation and the deadline therefore work identically for a
    child that says nothing (a sleeping child, killed 1.3 ms after ``SIGTERM``)
    and for one that floods both pipes (a 400 KiB flood on each stream drains in
    well under the deadline, never by waiting for the child to finish).

**Bounded capture, with an explicit limit for bulk output.**
    At most one pipe-full (65 536 bytes, the measured capacity) of each stream is
    kept by default. ``Keep.TAIL`` keeps the end - where an FFmpeg failure
    message is - and counts what fell out; ``Keep.STRICT`` refuses the job as
    soon as the stream passes its limit, because a truncated JSON probe answer or
    half a JPEG is worse than a named failure. Bulk stdout is therefore only
    possible with a caller-supplied number of bytes; larger media measurements
    should stream (the line callback, or a ``Keep.STRICT`` limit) or write to a
    file the caller owns. Neither mode can accumulate without bound, and a line
    handed to the progress callback is cut at :data:`MAX_LINE_BYTES` rather than
    buffered forever when a stream contains no newline.

**Termination is TERM, a bounded grace, then KILL, and always a reap.**
    Measured: a plain child dies 1.3 ms after ``SIGTERM`` (exit status -15); a
    child that handles it answers in 3.3 ms; a child that ignores it is still
    alive at the end of a 0.5 s grace and dies 501.8 ms after the stop request,
    with status -9 (``term_probe.log``). The grace exists for a program that
    needs to close what it opened, and it is bounded so that one that does not
    cannot stretch a cancellation. ``wait()`` is called on every path: a child is
    never left as a zombie or an orphan, and after the runner returns
    ``os.kill(pid, 0)`` raises :class:`ProcessLookupError`.

**Exactly one terminal outcome.**
    Either a :class:`ProcessResult` (exit status plus the bounded diagnostics,
    whatever that status is - a non-zero exit is an answer, not an error) or one
    exception from this module: cancelled, timed out, output over an explicit
    limit, the program could not be started, or the caller's own line callback
    failed. A callback exception never loses the process: it is stopped and
    reaped first, and then re-raised inside :class:`ProcessCallbackFailed`, which
    carries the original exception and the diagnostics reached.

Cancellation distinguishes the two moments it can arrive: before the process
exists (nothing is started, ``launched`` is false, no output exists) and while it
runs (terminate, reap, and the partial capture is reported).
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import IO

__all__ = [
    "CapturePolicy",
    "Keep",
    "ProcessCallbackFailed",
    "ProcessCancelled",
    "ProcessError",
    "ProcessLaunchError",
    "ProcessOverflow",
    "ProcessResult",
    "ProcessTimeout",
    "run_process",
]

# Measured Linux pipe capacity (``pipe_probe.log``): one full pipe of each stream
# is the default ceiling on what a single job may hold in memory.
DEFAULT_CAPTURE_LIMIT = 65_536

# Room for a well-behaved program to close what it opened between SIGTERM and
# SIGKILL.  Measured answers arrive in milliseconds (see the module docstring),
# so this is never a wait for a healthy child, only a bound on a stubborn one.
DEFAULT_TERMINATE_GRACE = 2.0

# SIGKILL cannot be caught or ignored; a child still alive after this is stuck in
# the kernel.  Say so instead of waiting forever.
KILL_WAIT = 5.0

# Once the child is reaped its pipes close, so the drain threads end in
# milliseconds.  Longer than this means a process this runner did not start is
# still holding the pipe open: report it, never wait for it indefinitely.
JOIN_GRACE = 2.0

# How often the wait loop reads the clock and the cancel Event.  This is the
# worst-case latency of a cancellation or a deadline, and the cost is ~50 wakeups
# a second next to a decode.
POLL_INTERVAL = 0.02

# A partial line is buffered for the callback only up to this much: a stream with
# no newline in it is split rather than accumulated.
MAX_LINE_BYTES = 1 << 20

_READ_SIZE = 65_536


class ProcessError(Exception):
    """Base for every terminal failure of one job.

    Every subclass carries what the pipes held, so a cancelled or timed-out job
    still reports the diagnostic it reached instead of losing it.
    """

    def __init__(
        self,
        message: str,
        *,
        argv: Sequence[str] = (),
        returncode: int | None = None,
        stdout: bytes = b"",
        stderr: bytes = b"",
        stdout_dropped: int = 0,
        stderr_dropped: int = 0,
        elapsed: float = 0.0,
    ) -> None:
        super().__init__(message)
        self.argv: tuple[str, ...] = tuple(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.stdout_dropped = stdout_dropped
        self.stderr_dropped = stderr_dropped
        self.elapsed = elapsed


class ProcessLaunchError(ProcessError):
    """The program could not be started; no process ever existed to terminate."""


class ProcessCancelled(ProcessError):
    """The shared cancel Event was set.

    ``launched`` says which half of the job was cancelled: ``False`` means the
    Event was already set when the runner looked, so nothing was started and
    there was nothing to terminate; ``True`` means a running process was
    terminated, reaped and reported here with whatever output it had produced.
    """

    def __init__(self, message: str, *, launched: bool, **details: object) -> None:
        super().__init__(message, **details)
        self.launched = launched


class ProcessTimeout(ProcessError):
    """The deadline passed.  The process was terminated and reaped first.

    ``returncode`` is the child's exit status, which is already known when a
    child exits but leaves its output pipe open elsewhere.
    """


class ProcessOverflow(ProcessError):
    """A ``Keep.STRICT`` stream passed its explicit limit, so the job is refused.

    Truncating a JSON answer or an image payload would hand the caller something
    false; this names the stream, the limit and how much arrived.
    """

    def __init__(
        self,
        message: str,
        *,
        stream: str,
        limit: int,
        total: int,
        **details: object,
    ) -> None:
        super().__init__(message, **details)
        self.stream = stream
        self.limit = limit
        self.total = total


class ProcessCallbackFailed(ProcessError):
    """The caller's stdout line callback raised.

    The process was stopped and reaped before this was raised, so a failure in
    the callback cannot strand a child; the caller's own exception is kept in
    :attr:`callback_error` rather than replaced by a generic message.
    """

    def __init__(
        self, message: str, *, callback_error: BaseException, **details: object
    ) -> None:
        super().__init__(message, **details)
        self.callback_error = callback_error


class Keep(Enum):
    """What a capture limit means once it is reached."""

    TAIL = "tail"
    """Keep the last N bytes and count the rest as dropped (diagnostics live here)."""

    STRICT = "strict"
    """Keep the first N bytes, then refuse the job: a truncated payload is a lie."""


@dataclass(frozen=True)
class CapturePolicy:
    """How much of each stream the result may hold.

    A limit of 0 discards the stream: it is still drained (so the child cannot
    block) and the line callback still sees it, but nothing is kept.  A job that
    wants bulk stdout must say how many bytes it accepts, with
    ``stdout_keep=Keep.STRICT``.
    """

    stdout_limit: int = DEFAULT_CAPTURE_LIMIT
    stderr_limit: int = DEFAULT_CAPTURE_LIMIT
    stdout_keep: Keep = Keep.TAIL
    stderr_keep: Keep = Keep.TAIL

    def __post_init__(self) -> None:
        for name in ("stdout_limit", "stderr_limit"):
            limit = getattr(self, name)
            if not isinstance(limit, int) or limit < 0:
                raise ValueError(f"{name} must be a non-negative number of bytes, got {limit!r}")
        for name in ("stdout_keep", "stderr_keep"):
            keep = getattr(self, name)
            if not isinstance(keep, Keep):
                raise ValueError(f"{name} must be a Keep, got {keep!r}")


@dataclass(frozen=True)
class ProcessResult:
    """The terminal outcome of a job that ran: exit status plus diagnostics.

    A non-zero status is an answer, not an error - only the caller knows which
    statuses mean what for its command.
    """

    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_dropped: int
    stderr_dropped: int
    elapsed: float

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0

    @property
    def stdout_text(self) -> str:
        """Captured stdout decoded leniently (a diagnostic, never media payload)."""
        return self.stdout.decode("utf-8", "replace")

    @property
    def stderr_text(self) -> str:
        """Captured stderr decoded leniently."""
        return self.stderr.decode("utf-8", "replace")


class _BoundedBuffer:
    """One stream's capture: at most ``limit`` bytes, either tail or head."""

    def __init__(self, limit: int, keep: Keep) -> None:
        self.limit = limit
        self.keep = keep
        self.total = 0
        self._chunks: list[bytes] = []
        self._kept = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self.limit <= 0:
            return
        if self.keep is Keep.TAIL:
            self._chunks.append(chunk)
            self._kept += len(chunk)
            excess = self._kept - self.limit
            while excess > 0 and self._chunks:
                first = self._chunks[0]
                if len(first) <= excess:
                    excess -= len(first)
                    self._kept -= len(first)
                    self._chunks.pop(0)
                else:
                    self._chunks[0] = first[excess:]
                    self._kept -= excess
                    excess = 0
        else:
            room = self.limit - self._kept
            if room > 0:
                self._chunks.append(chunk[:room])
                self._kept += min(len(chunk), room)

    def value(self) -> bytes:
        return b"".join(self._chunks)

    @property
    def dropped(self) -> int:
        return self.total - self._kept


class _PipeDrain(threading.Thread):
    """Reads one pipe to EOF on its own thread.

    Keeping this thread independent of the other pipe is what makes a full pipe
    harmless: neither stream can be the reason the other stops.  It stores no
    more than the capture policy allows, splits stdout into lines for the
    progress callback across read boundaries, and delivers a final line without a
    trailing newline at EOF - a child that ends mid-line still has its last
    message reported.

    A failure of the callback of *any* kind is recorded, not propagated here: the
    waiting runner reaps the process and then reports it, which is the only way a
    callback that raises cannot leave a child running.
    """

    def __init__(
        self,
        stream: IO[bytes],
        *,
        stream_name: str,
        limit: int,
        keep: Keep,
        on_line: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(name=f"process-runner-{stream_name}", daemon=True)
        self.stream_name = stream_name
        self.buffer = _BoundedBuffer(limit, keep)
        self.on_line = on_line
        self.finished = False
        self.callback_error: BaseException | None = None
        self.overflow: tuple[str, int, int] | None = None
        self.stream_error: OSError | None = None
        self._stream = stream
        self._pending = bytearray()

    def run(self) -> None:
        try:
            while True:
                try:
                    chunk = self._stream.read(_READ_SIZE)
                except (OSError, ValueError) as exc:  # pipe vanished under us
                    self.stream_error = exc
                    break
                if not chunk:
                    break
                self._consume(chunk)
                if self.callback_error is not None or self.overflow is not None:
                    break
        finally:
            self._deliver_pending()
            self.finished = True

    def _consume(self, chunk: bytes) -> None:
        self.buffer.add(chunk)
        if self.buffer.keep is Keep.STRICT and self.buffer.total > self.buffer.limit:
            self.overflow = (self.stream_name, self.buffer.limit, self.buffer.total)
            return
        if self.on_line is None:
            return
        self._pending += chunk
        while True:
            newline = self._pending.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._pending[:newline])
            del self._pending[: newline + 1]
            self._deliver(line)
            if self.callback_error is not None:
                return
        while len(self._pending) > MAX_LINE_BYTES:
            # No newline in sight: hand over a whole block rather than grow here.
            block = bytes(self._pending[:MAX_LINE_BYTES])
            del self._pending[:MAX_LINE_BYTES]
            self._deliver(block)
            if self.callback_error is not None:
                return

    def _deliver_pending(self) -> None:
        """Deliver a final line that had no newline, so nothing is silently lost."""
        if not self._pending:
            return
        line = bytes(self._pending)
        self._pending.clear()
        self._deliver(line)

    def _deliver(self, raw: bytes) -> None:
        if self.on_line is None or self.callback_error is not None:
            return
        try:
            self.on_line(raw.decode("utf-8", "replace"))
        except BaseException as exc:  # never strand a process over a callback
            self.callback_error = exc


def run_process(
    argv: Sequence[str],
    *,
    timeout: float,
    cancel: threading.Event | None = None,
    on_stdout_line: Callable[[str], None] | None = None,
    capture: CapturePolicy | None = None,
    terminate_grace: float = DEFAULT_TERMINATE_GRACE,
) -> ProcessResult:
    """Run *argv* to one terminal outcome, cancelling and reaping honestly.

    Blocks in the calling thread until the job is over, so it belongs on a worker
    thread, never on a UI thread.

    Args:
        argv: the program and its arguments, separately.  One string is refused.
        timeout: seconds of wall clock this job may take.  Required: every job
            must state a deadline, because a hung FFmpeg must not be able to
            occupy a worker forever.
        cancel: shared Event.  Set it from any thread (the UI does); it is
            checked before launching and while waiting, independent of output.
            If it is set in the same instant the child finishes, the job is
            reported as cancelled rather than completed: a request that reached
            the runner is honoured.
        on_stdout_line: called once per complete stdout line, and once more for a
            final line without a newline.  Always on the ``process-runner-stdout``
            thread, in stream order; raising from it stops the job.
        capture: how much of each stream to keep (default: one pipe-full of each,
            keeping the tail).  Bulk stdout needs an explicit byte limit.
        terminate_grace: how long SIGTERM is allowed to work before SIGKILL.

    Returns:
        a :class:`ProcessResult` with the exit status and the captured bytes -
        including a non-zero status, which the caller interprets.

    Raises:
        ProcessCancelled: the Event was set (``launched`` says when).
        ProcessTimeout: the deadline passed.
        ProcessOverflow: a strict stream passed its explicit limit.
        ProcessCallbackFailed: the line callback raised (``callback_error``).
        ProcessLaunchError: the program could not be started.
        ProcessError: a process that survived SIGKILL could not be reaped, or one
            of its streams was cut short by a read error rather than by EOF.
        ValueError: argv, timeout, grace or capture was not usable; nothing ran.
    """
    args = _check_argv(argv)
    policy = capture if capture is not None else CapturePolicy()
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError(f"timeout must be a positive number of seconds, got {timeout!r}")
    if (
        not isinstance(terminate_grace, (int, float))
        or isinstance(terminate_grace, bool)
        or terminate_grace < 0
    ):
        raise ValueError(
            f"terminate_grace must be a non-negative number of seconds, got {terminate_grace!r}"
        )

    # Asking for cancellation before anything exists is the cheap case: do not
    # start a process that nobody wants, and say which case this was.
    if cancel is not None and cancel.is_set():
        raise ProcessCancelled(
            f"{args[0]!r} was cancelled before it was started", argv=args, launched=False
        )

    try:
        # stdin is /dev/null so an interactive prompt can never be the reason a
        # background job hangs, and no output goes anywhere but these two pipes.
        proc = subprocess.Popen(
            list(args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as exc:
        raise ProcessLaunchError(
            f"cannot start {args[0]!r}: {exc}", argv=args, returncode=None
        ) from exc

    started = time.monotonic()
    deadline = started + timeout
    assert proc.stdout is not None and proc.stderr is not None  # both were piped
    out = _PipeDrain(
        proc.stdout,
        stream_name="stdout",
        limit=policy.stdout_limit,
        keep=policy.stdout_keep,
        on_line=on_stdout_line,
    )
    err = _PipeDrain(
        proc.stderr, stream_name="stderr", limit=policy.stderr_limit, keep=policy.stderr_keep
    )
    out.start()
    err.start()
    readers = (out, err)

    stopped = False
    try:
        reason = _wait_for_outcome(proc, readers, deadline, cancel)
        # One stop path for every early outcome; a child that already exited
        # needs no signal, and ``poll`` has already reaped it in that case.
        stopped = True
        if reason:
            returncode, _killed = _stop(proc, terminate_grace)
        else:
            returncode = proc.poll()
    except BaseException:
        # Anything unexpected still ends with the process gone and its pipes
        # read, so an internal fault cannot leak a child or a thread.
        if not stopped:
            _stop(proc, terminate_grace)
        _join_readers(readers)
        raise

    _join_readers(readers)
    _close_pipes(proc, readers)

    stdout_bytes = out.buffer.value()
    stderr_bytes = err.buffer.value()
    elapsed = time.monotonic() - started
    details = {
        "argv": args,
        "returncode": returncode,
        "stdout": stdout_bytes,
        "stderr": stderr_bytes,
        "stdout_dropped": out.buffer.dropped,
        "stderr_dropped": err.buffer.dropped,
        "elapsed": elapsed,
    }
    notes = ""
    if any(reader.is_alive() for reader in readers):
        # A process this runner did not start still holds a pipe; the outcome is
        # still terminal, and the caller is told why the capture may be short.
        notes += " (its output pipe is still open in another process)"
    failed = out if out.stream_error is not None else err
    if failed.stream_error is not None:
        notes += f" (reading its {failed.stream_name} failed: {failed.stream_error})"

    if reason is None and failed.stream_error is not None:
        # The child finished, but one of its streams was cut short by something
        # other than EOF: the capture cannot be trusted, so say so rather than
        # hand back bytes that may be missing a middle.
        raise ProcessError(
            f"{args[0]!r} finished, but the capture is incomplete{notes}",
            **details,
        )
    if reason is None:
        return ProcessResult(
            argv=args,
            returncode=returncode,
            stdout=stdout_bytes,
            stderr=stderr_bytes,
            stdout_dropped=out.buffer.dropped,
            stderr_dropped=err.buffer.dropped,
            elapsed=elapsed,
        )
    if reason == "cancelled":
        raise ProcessCancelled(
            f"{args[0]!r} was cancelled{notes}", launched=True, **details
        )
    if reason == "timeout":
        raise ProcessTimeout(
            f"{args[0]!r} did not finish within {timeout:g} seconds{notes}", **details
        )
    if reason == "overflow":
        stream, limit, total = out.overflow or err.overflow
        raise ProcessOverflow(
            f"{args[0]!r} wrote {total} bytes to {stream}, past the explicit "
            f"limit of {limit}{notes}",
            stream=stream,
            limit=limit,
            total=total,
            **details,
        )
    callback_error = out.callback_error
    raise ProcessCallbackFailed(
        f"the stdout line callback raised {type(callback_error).__name__}{notes}",
        callback_error=callback_error,
        **details,
    ) from callback_error


def _check_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Accept only a real argument vector: never one command string."""
    if isinstance(argv, (str, bytes, bytearray)):
        raise ValueError(
            "argv must be separate arguments, never one command string: "
            "running it through a shell would turn argument text into syntax"
        )
    args = tuple(argv)
    if not args:
        raise ValueError("argv must name the program to run")
    for index, argument in enumerate(args):
        if not isinstance(argument, str):
            raise ValueError(f"argv[{index}] must be a str, got {type(argument).__name__}")
    return args


def _wait_for_outcome(
    proc: subprocess.Popen[bytes],
    readers: tuple[_PipeDrain, _PipeDrain],
    deadline: float,
    cancel: threading.Event | None,
) -> str | None:
    """Poll until the job has an outcome; never blocks on the child or a pipe.

    Returns ``None`` for "the child exited and both pipes reached EOF", or the
    name of the early outcome.  A child that exited but whose pipes are held open
    by another process keeps the loop going until the deadline: this runner does
    not own processes it did not start, but it does refuse to wait without bound.
    """
    out, err = readers
    while True:
        if cancel is not None and cancel.is_set():
            return "cancelled"
        if out.callback_error is not None:
            return "callback"
        if out.overflow is not None or err.overflow is not None:
            return "overflow"
        if proc.poll() is not None and out.finished and err.finished:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        time.sleep(min(POLL_INTERVAL, remaining))


def _stop(proc: subprocess.Popen[bytes], grace: float) -> tuple[int, bool]:
    """SIGTERM, wait up to *grace* for it, SIGKILL, and always reap.

    Returns ``(exit status, killed)``.  ``wait()`` runs on every path: without it
    a finished child stays a zombie that ``os.kill`` still answers to.
    """
    already = proc.poll()
    if already is not None:
        return already, False
    # It may have died between that check and the signal; either way SIGTERM is
    # the first request, and the wait below is what proves it was obeyed.
    with contextlib.suppress(OSError):
        proc.terminate()
    killed = False
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        killed = True
        with contextlib.suppress(OSError):
            proc.kill()
        try:
            proc.wait(timeout=KILL_WAIT)
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - stuck in the kernel
            raise ProcessError(
                f"cannot reap pid {proc.pid}: it survived SIGKILL for {KILL_WAIT:g} seconds"
            ) from exc
    return proc.returncode, killed


def _join_readers(readers: tuple[_PipeDrain, _PipeDrain]) -> None:
    """Join both drain threads against one shared deadline.

    Their pipes close when the child is reaped, so this returns at once in every
    normal case; a thread still running after :data:`JOIN_GRACE` means a foreign
    process holds the pipe, which the caller is told about rather than waited on.
    """
    until = time.monotonic() + JOIN_GRACE
    for reader in readers:
        reader.join(timeout=max(0.0, until - time.monotonic()))


def _close_pipes(proc: subprocess.Popen[bytes], readers: tuple[_PipeDrain, _PipeDrain]) -> None:
    """Hand back the two read ends this runner opened (never while a drain uses one)."""
    if any(reader.is_alive() for reader in readers):
        return
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):  # already closed with the child's exit
                stream.close()
