"""Every still, source-step and frame-index child belongs to the runner (TASK_17).

The still decode, the decoded-PTS probes and the frame index used to own their own
``subprocess.run`` children: no token, no reap guarantee, and no limit on what a child
could answer.  They now run on ``media/process.py::run_process``.  What is under test
here is the **handling of a child**, so most children here are fake programs - a small
executable that hangs, floods stderr, answers too much, or answers a hand-written
amount - while two tests drive real FFmpeg on a real fixture, because a fake that
behaves is not evidence that a real still still decodes.

Why the fixtures and the bounds are shaped like this.  The environment facts are the
ones measured for ``media/process.py`` and recorded in ``/tmp/opencode/m13/``:
``pipe_probe.log`` says a Linux pipe holds **65 536 bytes** before its writer blocks,
``term_probe.log`` says a plain child dies **1.3 ms** after ``SIGTERM`` (and 501.8 ms
after the stop request if it ignores SIGTERM), and ``POLL_INTERVAL`` is **0.02 s**.

* **Fake-child bound: 1 s.**  Worst honest path for a child that obeys ``SIGTERM``:
  0.02 s polling + ~0.002 s to death + milliseconds of joining the two drain threads
  after the reap, so ~**0.024 s**.  1 s is ~40x that, and the fixtures themselves
  sleep 60 s - exactly what a caller that cannot see the Event would sit through.
* **Real-tool bound would be 5 s** (0.02 + ``DEFAULT_TERMINATE_GRACE`` 2.0 +
  ``JOIN_GRACE`` 2.0, as ``tests/test_render_cancellation.py`` derives), but no real
  FFmpeg *cancellation* is timed here: a one-picture decode of these fixtures finishes
  in tens of milliseconds, so "cancel it while it runs" against a real tool would
  measure scheduling luck rather than the bound.  The real tool is instead checked for
  **unchanged answers**, which is what it has to keep.
* **The flooding child counts its own stderr bytes** into a state file.  A reader that
  does not drain stderr leaves it blocked at one pipe-full, so waiting for a count
  above 65 536 is a readiness signal that a deadlocked runner can never satisfy.
* **Nothing survives the call** is asserted from the child's own recorded pid with
  ``os.kill(pid, 0)`` raising ``ProcessLookupError``, from the kernel's own child list
  ``/proc/self/task/*/children``, and from ``threading.enumerate()`` for any
  ``process-runner-*`` thread.  ``tests/test_process_runner.py`` pins the runner's own
  reaping promise independently.
* **Hand-worked expectations.**  The fake decode stamps ``pts: 2500`` on a tap that
  reports ``time_base 1/1000``, so the stamp this call may report is
  ``2500 x 1/1000 = 5/2`` s.  The fake window probe answers a quarter and three
  quarters into whatever window it was asked for, so for the window ``[0, 2)`` the
  index asks when asked about 1 s, the first picture at or after it is ``3/2`` s.  The
  raw capture limit is ``64 x 48 x 3 = 9 216`` bytes, so a child answering two
  pictures writes ``18 432``.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from fractions import Fraction as F
from pathlib import Path

import pytest

from media_fixtures import (
    HAS_FFMPEG,
    identity_at,
    make_identity_video,
    picture_facts,
    source_from_probe,
)
from video_editor.core.commands import AddClipCommand, RegisterSourceCommand
from video_editor.core.model import OutputSettings, Project, StreamSource, Track, TrackKind
from video_editor.media import framegrab
from video_editor.media.framegrab import (
    StillCancelled,
    StillError,
    extract_composition_still,
    extract_source_still,
)
from video_editor.media.frameindex import FrameIndex
from video_editor.media.frameprobe import decoded_picture_times
from video_editor.ui.render_worker import SourceStepWorker, StillWorker

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")

WIDTH, HEIGHT = 64, 48
#: one RGB24 picture of that geometry: 64 * 48 * 3
FRAME_BYTES = WIDTH * HEIGHT * 3
FPS = F(25)

#: measured Linux pipe capacity (``/tmp/opencode/m13/pipe_probe.log``)
PIPE_FULL_BYTES = 65_536
#: ~1.3 MB of stderr: twenty pipe-fulls, still under DECODE_LOG_LIMIT
FLOOD_LINES = 40_000

#: cancel of a fake child must answer within this (module docstring)
CANCEL_BOUND_SECONDS = 1.0
#: the fake's own deadline: what a caller with no token would sit through
HANG_SECONDS = 60

#: what the fake decode stamps, and the grid it stamps it on: the answer this call
#: may report is therefore 2500/1000 = 5/2 s, by arithmetic
FAKE_TAP_TIME_BASE = "1/1000"
FAKE_TAP_PTS = 2500
FAKE_DECODED_PTS = F(FAKE_TAP_PTS, 1000)


def live_runner_threads() -> list[str]:
    return sorted(
        thread.name for thread in threading.enumerate()
        if thread.name.startswith("process-runner-")
    )


def live_children() -> set[int]:
    """Direct children of this test process, from the kernel's own list."""
    found: set[int] = set()
    for task in Path("/proc/self/task").iterdir():
        try:
            text = (task / "children").read_text()
        except OSError:  # a thread that ended between listing and reading
            continue
        found.update(int(field) for field in text.split())
    return found


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_until_gone(pid: int, within: float) -> float:
    """Seconds until *pid* is gone; raises if it survives *within*."""
    started = time.monotonic()
    deadline = started + within
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return time.monotonic() - started
        time.sleep(0.005)
    raise AssertionError(f"pid {pid} is still there after {within} s")


def assert_reaped(pid: int) -> None:
    """Gone entirely: an unreaped child stays a zombie that ``os.kill`` answers to."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    pytest.fail(f"pid {pid} is still there: a live process or an unreaped zombie was left")


# ------------------------------------------------------------------ fake children

CHILD_SOURCE = r'''
"""Stand-in for ffmpeg/ffprobe: this role does whatever FAKE_ROLES says."""
import json
import os
import sys
import time

KIND = os.environ["FAKE_KIND"]  # baked into each copy of this script
args = sys.argv[1:]
work = os.environ["FAKE_WORK"]
behaviours = json.loads(os.environ["FAKE_ROLES"])
FLOOD = int(os.environ.get("FAKE_FLOOD_LINES", "40000"))
WIDTH = int(os.environ.get("FAKE_WIDTH", "64"))
HEIGHT = int(os.environ.get("FAKE_HEIGHT", "48"))
TIME_BASE = os.environ.get("FAKE_TIME_BASE", "1/1000")
TAP_PTS = int(os.environ.get("FAKE_TAP_PTS", "2500"))
OVERFLOW = int(os.environ.get("FAKE_OVERFLOW_BYTES", "9000000"))


def flag_value(name):
    if name in args:
        index = args.index(name) + 1
        if index < len(args):
            return args[index]
    return None


entries = flag_value("-show_entries") or ""
if KIND == "ffprobe":
    if entries.startswith("stream=index,time_base"):
        role = "timing"
    elif entries.startswith("format=duration"):
        role = "declared"
    elif entries.startswith("frame=media_type,best_effort_timestamp,pts"):
        role = "frames"
    elif entries.startswith("frame=best_effort_timestamp,pts,duration"):
        role = "tail"
    elif entries.startswith("packet=pts"):
        role = "packets"
    elif entries.startswith("stream=index,codec_type"):
        role = "cap_streams"
    elif entries.startswith("frame=best_effort_timestamp,interlaced_frame"):
        role = "cap_frames"
    else:
        role = "other"
else:
    role = "decode" if flag_value("-f") == "rawvideo" else "ffmpeg_other"

here = os.path.join(work, role)


def record(value):
    with open(here + ".calls", "a") as handle:
        handle.write(str(value) + "\n")


def state(text):
    with open(here + ".state", "w") as handle:
        handle.write(text)


record(os.getpid())
behaviour = behaviours.get(role, "ok")


def answer(document):
    sys.stdout.write(json.dumps(document))
    sys.stdout.flush()


def read_window():
    """The [from, to) seconds of the ``-read_intervals A%B`` window, as Fractions."""
    text = flag_value("-read_intervals") or "0%1"
    if "%" not in text:
        return 0.0, 1.0
    start, stop = text.split("%", 1)
    return float(start or 0), float(stop)


def flood_forever():
    """Megabytes of stderr, stdout silent, and never finishing by itself."""
    written = 0
    index = 0
    while True:
        line = "flood %06d %s\n" % (index, "x" * 24)
        sys.stderr.write(line)
        sys.stderr.flush()
        written += len(line)
        index += 1
        if index % 200 == 0:
            state("wrote %d" % written)


def flood_once(times):
    written = 0
    for index in range(times):
        line = "e%06d %s\n" % (index, "x" * 24)
        sys.stderr.write(line)
        written += len(line)
    sys.stderr.flush()
    state("wrote %d" % written)


def decode_log(pictures=1):
    """The showinfo tap lines a real decode writes for these pictures."""
    sys.stderr.write("[Parsed_showinfo_0 @ 0x1] config in time_base: %s, "
                     "frame_rate: 25/1\n" % TIME_BASE)
    for index in range(pictures):
        sys.stderr.write(
            "[Parsed_showinfo_0 @ 0x1] n:%4d pts:%8d pts_time:2.500000 "
            "duration: 40 log: 0 pict_type: I interp_type: P key_frame: 1 "
            "baseline: 0 checksum: 0x00000000\n" % (index, TAP_PTS + index))
    sys.stderr.flush()


def raw(pictures=1):
    sys.stdout.buffer.write(b"\x40" * (WIDTH * HEIGHT * 3 * pictures))
    sys.stdout.flush()


if behaviour == "hang":
    state("pid %d" % os.getpid())
    time.sleep(60)
    raise SystemExit(0)

if behaviour == "flood-hang":
    flood_forever()

if behaviour == "flood-ok":
    flood_once(FLOOD)
    decode_log(1)
    raw(1)
    raise SystemExit(0)

if behaviour == "two-frames":
    decode_log(1)
    raw(2)
    raise SystemExit(0)

if behaviour == "short":
    decode_log(1)
    sys.stdout.buffer.write(b"\x40" * (WIDTH * HEIGHT * 3 - 100))
    sys.stdout.flush()
    raise SystemExit(0)

if behaviour == "huge":
    sys.stdout.write('{"frames": [{"note": "' + ("y" * OVERFLOW) + '"}]}\n')
    sys.stdout.flush()
    raise SystemExit(0)

if behaviour == "fail":
    sys.stderr.write("FAKE-CHILD-FAILURE\n")
    raise SystemExit(1)

if KIND == "ffmpeg":
    decode_log(1)
    raw(1)
    raise SystemExit(0)

if role == "timing":
    answer({"streams": [{"index": 0, "time_base": TIME_BASE, "start_time": "0.000000"}]})
elif role == "declared":
    answer({"format": {"duration": "6.000000", "start_time": "0.000000"}})
elif role == "packets":
    answer({"packets": [{"pts": 2000}, {"pts": 2500}, {"pts": 3000}]})
elif role == "frames":
    # a quarter and three quarters into the window that was asked for, on TIME_BASE
    start, stop = read_window()
    span = stop - start
    base_num, base_den = 1, 1000
    stamps = []
    for part in (1, 3):
        seconds = start + span * part / 4
        ticks = int(round(seconds * base_den / base_num))
        stamps.append({"media_type": "video", "best_effort_timestamp": ticks, "pts": ticks})
    answer({"frames": stamps})
elif role == "tail":
    answer({"frames": [{"best_effort_timestamp": 5500, "pts": 5500, "duration": 40}]})
elif role == "cap_streams":
    answer({"streams": [{
        "index": 0, "codec_type": "video", "codec_name": "h264",
        "field_order": "progressive", "color_range": "tv", "color_space": "bt709",
        "color_transfer": "bt709", "color_primaries": "bt709", "pix_fmt": "yuv420p",
        "bits_per_raw_sample": 8}]})
elif role == "cap_frames":
    answer({"frames": [{
        "best_effort_timestamp": 2500, "interlaced_frame": 0, "top_field_first": 0,
        "color_space": "bt709", "color_transfer": "bt709",
        "color_primaries": "bt709", "pix_fmt": "yuv420p", "bits_per_raw_sample": 8}]})
else:
    sys.stderr.write("this fake tool does not read media headers\n")
    raise SystemExit(1)
'''


class FakeTools:
    """Fake ``ffmpeg``/``ffprobe`` placed on ``PATH``, plus the directory they report to.

    A call's role is read from its own arguments, so one script answers every probe
    shape; what that role does comes from ``FAKE_ROLES``.  Every call appends its pid
    to ``<role>.calls``, which is how a test proves which child ran and reaps the one
    it cancelled.  They go on ``PATH`` rather than being passed in, so the workers'
    own ``shutil.which`` resolution is exercised too.
    """

    def __init__(self, tmp_path) -> None:
        self.work = tmp_path / "tool-report"
        self.work.mkdir()
        self.bindir = tmp_path / "fake-bin"
        self.bindir.mkdir()
        self.ffmpeg = str(self._write("ffmpeg", "ffmpeg"))
        self.ffprobe = str(self._write("ffprobe", "ffprobe"))

    def _write(self, name: str, kind: str) -> Path:
        # the kind is baked into the copy: a child inherits its parent's environment,
        # so one environment variable could not tell the two tools apart
        script = self.bindir / name
        script.write_text(
            f"#!{sys.executable}\nimport os\nos.environ['FAKE_KIND'] = {kind!r}\n"
            f"{CHILD_SOURCE}",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def install(self, monkeypatch, roles: dict) -> None:
        """Roles not named here do their ordinary job (answer JSON, write a picture)."""
        monkeypatch.setenv("FAKE_WORK", str(self.work))
        monkeypatch.setenv("FAKE_ROLES", json.dumps(roles))
        monkeypatch.setenv("FAKE_FLOOD_LINES", str(FLOOD_LINES))
        monkeypatch.setenv("FAKE_WIDTH", str(WIDTH))
        monkeypatch.setenv("FAKE_HEIGHT", str(HEIGHT))
        monkeypatch.setenv("FAKE_TIME_BASE", FAKE_TAP_TIME_BASE)
        monkeypatch.setenv("FAKE_TAP_PTS", str(FAKE_TAP_PTS))
        monkeypatch.setenv(
            "PATH", os.pathsep.join([str(self.bindir), os.environ["PATH"]])
        )

    def calls(self, role: str) -> list:
        try:
            lines = (self.work / f"{role}.calls").read_text().splitlines()
        except OSError:
            return []
        return [int(line) for line in lines if line.strip()]

    def call_count(self, role: str) -> int:
        return len(self.calls(role))

    def wait_for_call(self, role: str, within: float = 30.0) -> int:
        """The pid of that role's newest child, once it has started."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            pids = self.calls(role)
            if pids:
                return pids[-1]
            time.sleep(0.01)
        raise AssertionError(f"the {role!r} child never started within {within} s")

    def wait_for_stderr_bytes(self, role: str, at_least: int, within: float = 30.0) -> int:
        """Wait until the flooding child has written *at_least* bytes of stderr.

        That counter is the readiness signal for cancelling a flood: a caller that
        does not drain stderr leaves the child blocked at one pipe-full, so the count
        never passes it and this raises instead of quietly passing.
        """
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            try:
                text = (self.work / f"{role}.state").read_text()
            except OSError:
                text = ""
            if text.startswith("wrote "):
                written = int(text.split()[1])
                if written >= at_least:
                    return written
            time.sleep(0.01)
        raise AssertionError(
            f"the {role!r} child never wrote {at_least} bytes of stderr within {within} s"
        )


def hand_project() -> Project:
    """A hand-written 6 s 25 fps stream: the fake tools never read a real file.

    The geometry is the one the fake decode is told to answer with, so a picture's
    byte count is arithmetic: 64 x 48 x 3 = 9 216.
    """
    project = Project(output=OutputSettings(WIDTH, HEIGHT, FPS, 48_000, "stereo"))
    project = RegisterSourceCommand(
        StreamSource("va", 0, TrackKind.VIDEO, F(0), F(6), frame_rate=FPS,
                     width=WIDTH, height=HEIGHT, stream_position=0)
    ).apply(project)
    project = project.with_tracks((Track("v1", TrackKind.VIDEO, "V1"),))
    return AddClipCommand("va", 0, "v1", F(0), source_in=F(0), source_out=F(6),
                          clip_id="c1").apply(project)


def placeholder(tmp_path):
    """A file that exists but is never parsed: these tests are about children."""
    path = tmp_path / "video.mkv"
    path.write_bytes(b"placeholder input, never read by these tests")
    return hand_project(), {"va": path}


def video_project(path, asset_id: str):
    """One video track playing all of one real picture stream, registered by probing it."""
    project = Project(output=OutputSettings(WIDTH, HEIGHT, FPS, 48_000, "stereo"))
    project = RegisterSourceCommand(source_from_probe(path, 0, asset_id)).apply(project)
    project = project.with_tracks((Track("v1", TrackKind.VIDEO, "V1"),))
    end = project.source(asset_id, 0).end
    project = AddClipCommand(asset_id, 0, "v1", F(0), source_in=F(0), source_out=end,
                             clip_id="c1").apply(project)
    return project, {asset_id: path}


# ------------------------------------------------- a silent child that will not end


def test_a_silent_hanging_probe_is_cancelled_and_reaped(tmp_path, monkeypatch):
    """No output at all: the token may not depend on a stream producing anything.

    Bound: 1 s from the Event being set (0.02 s polling + a measured 1.3 ms SIGTERM
    death + millisecond joins).  The child sleeps 60 s, which is what the old
    ``subprocess.run(timeout=60)`` sat through with no token at all.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"frames": "hang"})
    token = threading.Event()
    path = tmp_path / "video.mkv"
    path.write_bytes(b"placeholder input, never read by these tests")

    def ask() -> None:
        with contextlib.suppress(StillCancelled):
            decoded_picture_times(path, 0, F(0), F(2), ffprobe=tools.ffprobe,
                                  timeout=HANG_SECONDS, cancel=token)

    worker = threading.Thread(target=ask, name="test-probe-request", daemon=True)
    worker.start()
    child = tools.wait_for_call("frames")
    when = time.monotonic()
    token.set()
    worker.join(timeout=30)
    assert not worker.is_alive(), "the request did not come back from its cancelled child"
    answered = time.monotonic() - when
    assert answered < CANCEL_BOUND_SECONDS, f"cancel answered in {answered:.3f} s"
    assert_reaped(child)
    assert live_runner_threads() == [], "a drain thread outlived the cancelled read"
    assert tools.call_count("frames") == 1


def test_a_cancelled_still_decode_is_never_reported_as_a_decode_failure(
    tmp_path, monkeypatch
):
    """Cancelled and failed are different answers, and only one of them is a problem.

    The old code turned a hung or killed child into
    ``StillError("cannot decode the source frame: ...")``, indistinguishable from a
    real decode failure.  The same call with the same silent child now answers
    :class:`StillCancelled` - the type the workers branch on - so a stopped request
    cannot put a decode error on screen.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"decode": "hang"})
    project, paths = placeholder(tmp_path)
    token = threading.Event()
    caught: list = []

    def ask() -> None:
        try:
            extract_source_still(project, paths, "c1", F(2), F(2), cancel=token)
        except StillError as exc:
            caught.append(exc)

    worker = threading.Thread(target=ask, name="test-decode-request", daemon=True)
    worker.start()
    child = tools.wait_for_call("decode")
    when = time.monotonic()
    token.set()
    worker.join(timeout=30)
    assert not worker.is_alive(), "the decode did not come back from its cancelled child"
    answered = time.monotonic() - when
    assert answered < CANCEL_BOUND_SECONDS, f"cancel answered in {answered:.3f} s"
    assert_reaped(child)
    assert len(caught) == 1 and isinstance(caught[0], StillCancelled), caught
    assert tools.call_count("decode") == 1


def test_a_deadline_on_an_uncancellable_child_is_a_failure_not_a_cancellation(
    tmp_path, monkeypatch
):
    """The other half of the contract: a child that outlived its deadline is a failure.

    Same silent child, no token, a stated 1 s deadline.  The answer must be a
    ``StillError`` that is **not** ``StillCancelled`` and must say the child did not
    finish: "the user stopped this" and "the tool hung" are different facts for
    whoever reads the report.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"frames": "hang"})
    path = tmp_path / "video.mkv"
    path.write_bytes(b"placeholder input, never read by these tests")
    started = time.monotonic()
    with pytest.raises(StillError) as refused:
        decoded_picture_times(path, 0, F(0), F(2), ffprobe=tools.ffprobe, timeout=1.0)
    elapsed = time.monotonic() - started
    assert not isinstance(refused.value, StillCancelled), type(refused.value)
    assert "did not finish within 1 seconds" in str(refused.value), str(refused.value)
    assert 0.9 < elapsed < 3.0, f"the deadline fired at {elapsed:.3f} s"
    assert_reaped(tools.wait_for_call("frames"))


def test_a_token_set_before_the_call_starts_no_child_at_all(tmp_path, monkeypatch):
    """A request nobody wants any more must not spawn a decoder to notice that."""
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"frames": "hang", "decode": "hang", "tail": "hang"})
    path = tmp_path / "video.mkv"
    path.write_bytes(b"placeholder input, never read by these tests")
    token = threading.Event()
    token.set()
    started = time.monotonic()
    with pytest.raises(StillCancelled) as refused:
        decoded_picture_times(path, 0, F(0), F(2), ffprobe=tools.ffprobe,
                              timeout=HANG_SECONDS, cancel=token)
    assert time.monotonic() - started < 1.0, "a cancelled read still did work"
    assert "before it was started" in str(refused.value), str(refused.value)
    for role in ("frames", "decode", "tail"):
        assert tools.call_count(role) == 0, f"a cancelled request still started {role}"


# ------------------------------------------------------ a child that floods stderr


def test_a_flooding_child_neither_blocks_nor_hides_the_request(tmp_path, monkeypatch):
    """Megabytes of stderr with stdout idle, then a cancel request.

    The old still paths read nothing until the child had exited, so this child blocked
    in ``write`` at one pipe-full and the request was never looked at again.  The
    token is set only once the child has proved it wrote **more than one pipe-full** of
    stderr, which a non-draining caller can never let happen.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"decode": "flood-hang"})
    project, paths = placeholder(tmp_path)
    token = threading.Event()

    def ask() -> None:
        with contextlib.suppress(StillCancelled):
            extract_source_still(project, paths, "c1", F(2), F(2), cancel=token)

    worker = threading.Thread(target=ask, name="test-flood-request", daemon=True)
    worker.start()
    child = tools.wait_for_call("decode")
    written = tools.wait_for_stderr_bytes("decode", PIPE_FULL_BYTES + 4_096)
    assert written > PIPE_FULL_BYTES, written
    when = time.monotonic()
    token.set()
    worker.join(timeout=30)
    assert not worker.is_alive(), "a flooding child hid the cancel request"
    answered = time.monotonic() - when
    assert answered < CANCEL_BOUND_SECONDS, f"cancel answered in {answered:.3f} s"
    assert_reaped(child)


def test_a_decode_that_floods_its_log_still_returns_the_same_picture(
    tmp_path, monkeypatch
):
    """A big log must not cost the answer, nor its measured stamp.

    The child writes ~1.3 MB of stderr (twenty pipe-fulls) and then one picture with
    its tap lines.  That stays under the stated decode-log limit, so the stamp is read
    from it exactly as before: 2500 ticks of ``1/1000`` is ``5/2`` s.  The bound is
    this call's own 30 s deadline - answering in a fraction of it says the pipes were
    drained while the child ran.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"decode": "flood-ok"})
    project, paths = placeholder(tmp_path)
    written = sum(len(f"e{index:06d} {'x' * 24}") + 1 for index in range(FLOOD_LINES))
    assert written > 20 * PIPE_FULL_BYTES, f"the flood is only {written} bytes"
    started = time.monotonic()
    frame = extract_source_still(project, paths, "c1", F(2), F(2))
    elapsed = time.monotonic() - started
    assert elapsed < 15.0, f"{elapsed:.3f} s: drained, or waiting for the deadline?"
    assert (frame.width, frame.height) == (WIDTH, HEIGHT)
    assert frame.nbytes == FRAME_BYTES
    assert frame.decoded_source_pts == FAKE_DECODED_PTS, frame.decoded_source_pts
    assert frame.grid_exact


# ------------------------------------------- raw output bounded by the geometry


def test_a_raw_answer_larger_than_one_picture_is_refused_by_name(tmp_path, monkeypatch):
    """``-f rawvideo`` is captured up to the validated geometry, and no further.

    The child answers **two** pictures: 2 x 64 x 48 x 3 = 18 432 bytes.  The capture
    limit is the one picture the geometry occupies, 9 216, so the job is refused with
    both numbers named and no picture handed back - a truncated picture, or whichever
    one happened to survive, is exactly what a viewer must not show while a cut is
    being chosen.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"decode": "two-frames"})
    project, paths = placeholder(tmp_path)
    with pytest.raises(StillError) as refused:
        extract_source_still(project, paths, "c1", F(2), F(2))
    message = str(refused.value)
    assert not isinstance(refused.value, StillCancelled), type(refused.value)
    assert f"limit of {FRAME_BYTES}" in message, message
    assert str(2 * FRAME_BYTES) in message, message
    assert tools.call_count("decode") == 1


def test_a_short_raw_answer_is_still_reported_as_a_short_read(tmp_path, monkeypatch):
    """The byte-count check is unchanged: 9 116 of 9 216 is not one picture."""
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"decode": "short"})
    project, paths = placeholder(tmp_path)
    with pytest.raises(StillError) as refused:
        extract_source_still(project, paths, "c1", F(2), F(2))
    assert f"returned {FRAME_BYTES - 100} bytes" in str(refused.value), str(refused.value)
    assert f"needs {FRAME_BYTES}" in str(refused.value), str(refused.value)


def test_a_probe_answer_past_its_stated_limit_is_refused_not_parsed(tmp_path, monkeypatch):
    """A window probe's JSON is kept strictly: half a payload is not an enumeration.

    The stated window limit is 8 MiB; the child answers 9 000 000 bytes of filler, so
    the read is refused naming that limit instead of parsing a truncated ``frames``
    list - which would look like a span with fewer pictures in it, the one thing an
    index must never invent.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"frames": "huge"})
    path = tmp_path / "video.mkv"
    path.write_bytes(b"placeholder input, never read by these tests")
    with pytest.raises(StillError) as refused:
        decoded_picture_times(path, 0, F(0), F(2), ffprobe=tools.ffprobe, timeout=30)
    message = str(refused.value)
    assert not isinstance(refused.value, StillCancelled), type(refused.value)
    assert str(framegrab.WINDOW_PROBE_JSON_LIMIT) in message, message
    assert_reaped(tools.wait_for_call("frames"))


def test_a_failing_probe_child_still_names_ffmpegs_own_last_line(tmp_path, monkeypatch):
    """Bounding the log must not throw away the reason a probe failed.

    The capture keeps the **tail** of stderr, where FFmpeg's own error line is, and a
    short failure answer arrives whole.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"frames": "fail"})
    path = tmp_path / "video.mkv"
    path.write_bytes(b"placeholder input, never read by these tests")
    with pytest.raises(StillError) as refused:
        decoded_picture_times(path, 0, F(0), F(2), ffprobe=tools.ffprobe, timeout=30)
    assert "FAKE-CHILD-FAILURE" in str(refused.value), str(refused.value)
    assert_reaped(tools.wait_for_call("frames"))


# ------------------------------------------------------- the frame index's children


def test_a_frame_index_child_is_cancelled_and_nothing_is_written_down(
    tmp_path, monkeypatch
):
    """A cancelled index read is not a window, not a span and not an ``UNKNOWN``.

    This is the class of bug TASK_16 removed in the render path: treating a stopped
    measurement as an answer.  An index whose read was cancelled keeps its probe count
    at zero, caches no window, proves no span and reports no status - and the next
    request, pointed at a new token, reads normally and answers what the fixture
    answered.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"frames": "hang"})
    _project, paths = placeholder(tmp_path)
    index = FrameIndex(paths["va"], 0, ffprobe=tools.ffprobe, window=F(2))
    token = threading.Event()
    index.set_cancel(token)
    caught: list = []

    def ask() -> None:
        try:
            index.answer_at_or_after(F(1))
        except StillCancelled:
            caught.append(True)
        except Exception as exc:  # a stop reported as anything else is the bug
            caught.append(exc)

    worker = threading.Thread(target=ask, name="test-index-request", daemon=True)
    worker.start()
    child = tools.wait_for_call("frames")
    when = time.monotonic()
    token.set()
    worker.join(timeout=30)
    assert not worker.is_alive(), "the index did not come back from its cancelled read"
    assert caught == [True], f"a cancelled read was reported as {caught[0]!r}, not a stop"
    answered = time.monotonic() - when
    assert answered < CANCEL_BOUND_SECONDS, f"cancel answered in {answered:.3f} s"
    assert_reaped(child)
    # nothing was written down about the read that never ran
    assert index.probes == 0, index.probes
    assert index.cached_spans() == (), index.cached_spans()
    assert index.proven_spans() == (), index.proven_spans()

    # the same index, pointed at the next request's token, reads and answers: the
    # window centred on 1 s is [0, 2) and the fake answers 1/2 s and 3/2 s in it, so
    # the first picture at or after 1 s is 3/2 s
    tools.install(monkeypatch, {})
    index.set_cancel(threading.Event())
    answer = index.answer_at_or_after(F(1))
    assert answer.pts == F(3, 2), answer.describe()
    assert index.probes == 1, index.probes


def test_a_superseded_frame_index_token_does_not_cancel_a_later_request(
    tmp_path, monkeypatch
):
    """A cached index must follow the request that is running, not the one that built it.

    The stepping worker keeps its indexes; a token left over from an earlier,
    cancelled request would make every later read fail.  ``set_cancel`` moves it, so
    the stale token is set and the current request still gets its answer.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {})
    _project, paths = placeholder(tmp_path)
    index = FrameIndex(paths["va"], 0, ffprobe=tools.ffprobe, window=F(2))
    stale = threading.Event()
    stale.set()
    index.set_cancel(stale)
    index.set_cancel(threading.Event())  # this request's token
    answer = index.answer_at_or_after(F(1))
    assert answer.pts == F(3, 2), answer.describe()


# ---------------------------------------------------------------- the UI workers


def _drive(worker):
    """Run a mailbox worker's turn on its own thread, as its QThread would."""
    thread = threading.Thread(target=worker.process_request, name="test-worker-turn",
                              daemon=True)
    thread.start()
    return thread


def _record(worker) -> list:
    """Everything the worker emits, in order (direct connection, same process)."""
    delivered: list = []
    worker.finished.connect(delivered.append)
    worker.failed.connect(delivered.append)
    return delivered


def test_a_superseded_still_request_signals_the_running_decode(tmp_path, monkeypatch):
    """The viewer asked for a newer frame: the decode in flight is stopped, not finished.

    Asserted from the child: its pid is gone within the bound that starts when the
    newer request arrives, and the worker emits **neither** a picture nor a failure
    for the request it stopped.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"decode": "hang"})
    project, paths = placeholder(tmp_path)
    worker = StillWorker()
    delivered = _record(worker)
    worker.request(1, project, paths, F(3, 10))
    thread = _drive(worker)
    child = tools.wait_for_call("decode")
    when = time.monotonic()
    worker.request(2, project, paths, F(1, 2))  # superseded, from another thread
    wait_until_gone(child, within=CANCEL_BOUND_SECONDS)
    answered = time.monotonic() - when
    thread.join(timeout=30)
    assert not thread.is_alive(), "the worker never came back from the cancelled decode"
    assert answered < CANCEL_BOUND_SECONDS, f"supersede answered in {answered:.3f} s"
    assert_reaped(child)
    assert delivered == [], f"a stopped decode reported something: {delivered}"


def test_a_still_worker_stop_signals_its_active_token(tmp_path, monkeypatch):
    """Closing the window must not leave a decoder running for the window it lost.

    ``stop()`` refuses queued work **and** sets the token of the decode already
    running, so the child is terminated and reaped, the thread returns, and nothing is
    delivered for a request whose owner is gone.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"decode": "hang"})
    project, paths = placeholder(tmp_path)
    worker = StillWorker()
    delivered = _record(worker)
    worker.request(1, project, paths, F(3, 10))
    thread = _drive(worker)
    child = tools.wait_for_call("decode")
    when = time.monotonic()
    worker.stop()
    wait_until_gone(child, within=CANCEL_BOUND_SECONDS)
    answered = time.monotonic() - when
    thread.join(timeout=30)
    assert not thread.is_alive(), "the worker never came back from the cancelled decode"
    assert answered < CANCEL_BOUND_SECONDS, f"stop answered in {answered:.3f} s"
    assert_reaped(child)
    assert delivered == [], f"a stopped decode reported something: {delivered}"
    assert not worker.pending, "a stopped worker kept queued work"


def test_a_stopped_source_step_cancels_its_index_read_and_decodes_nothing(
    tmp_path, monkeypatch
):
    """The step's own children are its frame-index read and its decode.

    This step stops inside the index read, so the assertions are that the index's
    child is reaped, the decode never starts, and neither a picture nor a
    ``SourceStepProblem`` is emitted for the stopped step.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"frames": "hang"})
    project, paths = placeholder(tmp_path)
    worker = SourceStepWorker()
    delivered = _record(worker)
    worker.request(1, project, paths, "c1", F(1), 1, F(1))
    thread = _drive(worker)
    child = tools.wait_for_call("frames")
    when = time.monotonic()
    worker.stop()
    wait_until_gone(child, within=CANCEL_BOUND_SECONDS)
    answered = time.monotonic() - when
    thread.join(timeout=30)
    assert not thread.is_alive(), "the step never came back from its cancelled read"
    assert answered < CANCEL_BOUND_SECONDS, f"stop answered in {answered:.3f} s"
    assert_reaped(child)
    assert delivered == [], f"a stopped step reported something: {delivered}"
    assert tools.call_count("decode") == 0, "a stopped step went on to decode"


# ------------------------------------------------- real FFmpeg: unchanged behaviour


def test_a_real_still_still_returns_the_same_picture_and_stamp(tmp_path):
    """The real tool, a real fixture, and expectations checked against an oracle.

    ``picture_facts`` measures the fixture independently of these helpers (``ffprobe
    -show_frames`` for the timestamps, a separate ``rawvideo`` decode for the identity
    colours), and the expectations here are hand-worked from that: a playhead at 3/10 s
    of a 25 fps composition is output frame ``floor(3/10 x 25) = 7``, and picture 7 of
    an identity fixture carries the number 7.  A source request for 1/3 s asks for the
    first real picture at or after it, and 8/25 s < 1/3 s < 9/25 s, so it is 9/25 s.
    Nothing is left behind either: a still writes no output.
    """
    identity = make_identity_video(tmp_path, "identity.mkv", count=150)
    facts = picture_facts(identity)
    before = sorted(path.name for path in tmp_path.rglob("*") if path.is_file())
    assert facts.pictures[7] == F(7, 25), facts.pictures[7]  # measured: n/25 s
    assert facts.identities[7] == 7, facts.identities[7]

    project, paths = video_project(identity, "identity")
    still = extract_composition_still(project, paths, F(3, 10))
    assert still.frame_index == 7 and still.output_time == F(7, 25)
    assert still.decoded_source_pts == facts.pictures[7], still.decoded_source_pts
    assert still.grid_exact
    assert identity_at(still.rgb, still.width, still.height) == facts.identities[7]

    stepped = extract_source_still(project, paths, "c1", F(1, 3), F(1, 3))
    assert F(8, 25) < F(1, 3) < F(9, 25), "the hand-worked neighbour of the request"
    assert stepped.decoded_source_pts == F(9, 25), stepped.decoded_source_pts
    assert stepped.grid_exact and stepped.source_only
    assert identity_at(stepped.rgb, stepped.width, stepped.height) == 9

    after = sorted(path.name for path in tmp_path.rglob("*") if path.is_file())
    assert after == before, f"a still left something behind: {set(after) - set(before)}"


def test_a_real_frame_index_answer_survives_a_cancelled_neighbour(tmp_path):
    """A cancelled read next to a real one must not change what the real one proves.

    The index reads the real fixture, then a read for another question is stopped
    before it starts; asking the first question again comes back with the same measured
    timestamp, the same proven spans and the same probe count, so a stopped request
    cannot quietly narrow what the index is entitled to claim.
    """
    identity = make_identity_video(tmp_path, "identity.mkv", count=150)
    facts = picture_facts(identity)
    index = FrameIndex(identity, 0, window=F(2))
    first = index.answer_at_or_before(facts.pictures[50])
    assert first.pts == facts.pictures[50], first.describe()
    spans = index.proven_spans()
    probes = index.probes
    assert spans and probes, "the first read proved nothing: the fixture is wrong"

    stale = threading.Event()
    stale.set()  # this next question is over before it asks anything
    index.set_cancel(stale)
    with pytest.raises(StillCancelled):
        index.answer_at_or_after(facts.pictures[-1] + 1)
    assert index.proven_spans() == spans, "a cancelled read changed what is proven"
    assert index.probes == probes, "a cancelled read was counted as a read"

    index.set_cancel(None)
    again = index.answer_at_or_before(facts.pictures[50])
    assert again.pts == facts.pictures[50], again.describe()
    assert index.proven_spans() == spans, "the later read changed what is proven"
