"""Every child the render job starts is cancellable, bounded and reaped (TASK_16).

The encoder, the chunk join, the encoder-availability probe, the source-tail
measurement and the verification probes are driven here by **fake programs**: small
executables that stay silent, flood stderr, hang, or answer badly, so what is under
test is the job's handling of a child and not FFmpeg's behaviour.  Two real-FFmpeg
tests are included, because a fake that behaves is not evidence that a real render
still works.

Why the fixtures are shaped like this.  The environment facts are the ones measured
for ``media/process.py`` and kept in ``/tmp/opencode/m13/``: ``pipe_probe.log`` says a
Linux pipe holds **65 536 bytes** before its writer blocks, and ``term_probe.log``
says a plain child dies **1.3 ms** after ``SIGTERM``.

* **stderr-heavy with stdout idle.**  The exporter used to read ``proc.stdout`` line
  by line and only touched ``proc.stderr`` after ``proc.wait()``, so such a child
  blocked in ``write`` once its pipe was full and the job never came back.  The flood
  fixtures write several pipe-fulls of stderr with **nothing on stdout**, which is
  exactly that shape.
* **Timing bounds are derived, never copied from a passing run.**  The runner polls
  the cancel Event every ``POLL_INTERVAL`` = 0.02 s, then sends ``SIGTERM`` (measured
  1.3 ms to death for a plain child), then joins its two drain threads after the reap
  (milliseconds).  So a child that dies on ``SIGTERM`` must be answered in well under
  **1 s**, about 50 times the path actually taken, while the fixtures themselves
  60 s — which is what a job that never looked at the Event would sit through.  For a
  real FFmpeg the bound is the documented worst path instead: 0.02 poll + 2.0 grace +
  2.0 join ≈ 4.02 s, so **5 s**.
* **No leaked child** is proved from the child's own recorded pid with
  ``os.kill(pid, 0)`` raising ``ProcessLookupError``, and independently by listing
  ``/proc/self/task/*/children``.  No ``process-runner-*`` thread outlives the call.
* **No leftover artifacts** is proved from the directory after the call: the staging
  file, every chunk intermediate and the concat list are gone, and the destination is
  either absent or byte-identical to the file that was there before.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path

import pytest

from media_fixtures import HAS_FFMPEG, make_long_video
from video_editor.core.commands import AddClipCommand, RegisterSourceCommand
from video_editor.core.model import OutputSettings, Project, StreamSource, Track, TrackKind
from video_editor.render import exporter
from video_editor.render.exporter import (
    RenderCancelled,
    RenderError,
    RenderRequest,
    measure_decoded_span,
    render,
)
from video_editor.render.graph import encoder_available

F = Fraction
FPS = F(25)
SAMPLE_RATE = 48_000
WIDTH, HEIGHT = 64, 48
#: 4 s at 25 fps and 48 kHz stereo, so **100 frames** and **192 000 samples**
#: (4 x 25 and 4 x 48 000: arithmetic, not a measurement).  The fake tools answer
#: with exactly these numbers and the result is compared with them.
SECONDS = F(4)
FRAMES = 100
SAMPLES = 192_000

#: Measured Linux pipe capacity (``/tmp/opencode/m13/pipe_probe.log``): how much
#: stderr a child can write before it blocks if nobody reads it.
PIPE_FULL_BYTES = 65_536
FLOOD_LINES = 40_000  # ~262 KiB, four pipe-fulls: a serial reader deadlocks on this

#: "the job answered my cancel request".  Derived in the module docstring.
CANCEL_BOUND_SECONDS = 1.0
#: Same, for a real FFmpeg that gets to close the file it was writing.
REAL_CANCEL_BOUND_SECONDS = 5.0

needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg and ffprobe")

CHILD_SOURCE = '''
"""Stand-in for ffmpeg/ffprobe: this role does whatever FAKE_ROLES says."""
import json
import os
import sys
import time

KIND = os.environ["FAKE_KIND"]  # written into each copy of this script
args = sys.argv[1:]
work = os.environ["FAKE_WORK"]
behaviours = json.loads(os.environ["FAKE_ROLES"])
FLOOD = int(os.environ.get("FAKE_FLOOD_LINES", "40000"))
OUT_US = int(os.environ.get("FAKE_OUTPUT_US", "4000000"))
FRAMES = int(os.environ.get("FAKE_FRAMES", "100"))
WIDTH = int(os.environ.get("FAKE_WIDTH", "64"))
HEIGHT = int(os.environ.get("FAKE_HEIGHT", "48"))
RATE = int(os.environ.get("FAKE_SAMPLE_RATE", "48000"))
LINES = int(os.environ.get("FAKE_PROGRESS_LINES", "40000"))
TOP_STAMP = int(os.environ.get("FAKE_TOP_STAMP", "3999999"))


def flag_value(name):
    if name in args:
        index = args.index(name) + 1
        if index < len(args):
            return args[index]
    return None


if KIND == "ffprobe":
    if "-count_packets" in args:
        role = "streams"
    elif "-show_format" in args and "-show_streams" not in args:
        role = "duration"
    else:
        role = "probe-other"
else:
    target = flag_value("-f")
    if "-encoders" in args:
        role = "capability"
    elif target == "null":
        role = "measure"
    elif target == "concat":
        role = "join"
    else:
        role = "encode"

here = os.path.join(work, role)


def record(value):
    with open(here + ".calls", "a") as handle:
        handle.write(str(value) + "\\n")


def state(text):
    with open(here + ".state", "w") as handle:
        handle.write(text)


record(os.getpid())
behaviour = behaviours.get(role, "ok")


def flood(times):
    for index in range(times):
        sys.stderr.write("e%d\\n" % index)
    sys.stderr.flush()


def flood_forever():
    """Megabytes of stderr, stdout silent, and never finishing by itself."""
    written = 0
    index = 0
    while True:
        line = "flood %06d %s\\n" % (index, "x" * 24)
        sys.stderr.write(line)
        sys.stderr.flush()
        written += len(line)
        index += 1
        if index % 200 == 0:
            state("wrote %d" % written)


def progress(microseconds):
    sys.stdout.write("frame=1\\n")
    sys.stdout.write("out_time_us=%d\\n" % microseconds)
    sys.stdout.write("out_time_ms=%d\\n" % microseconds)
    sys.stdout.write("progress=continue\\n")
    sys.stdout.flush()


def stream_json(document):
    sys.stdout.write(json.dumps(document))
    sys.stdout.flush()


def encode_output():
    with open(args[-1], "wb") as handle:
        handle.write(b"fake rendered media: never decoded by these tests\\n")


if behaviour == "hang":
    state("pid %d" % os.getpid())
    time.sleep(60)
    raise SystemExit(0)

if behaviour == "flood-hang":
    flood_forever()

if behaviour in ("flood-ok", "flood-fail"):
    flood(FLOOD)
    sys.stderr.write("FLOOD-END-%d\\n" % FLOOD)
    sys.stderr.flush()
    if behaviour == "flood-fail":
        raise SystemExit(1)

if behaviour == "many-lines":
    for index in range(LINES):
        sys.stdout.write("frame=1\\nout_time_us=%d\\n" % index)
    sys.stdout.write("out_time_us=%d\\n" % TOP_STAMP)
    sys.stdout.flush()
    raise SystemExit(0)

if behaviour == "garbage":
    sys.stdout.write("this is not json at all\\n")
    raise SystemExit(0)

if behaviour == "huge":
    sys.stdout.write('{"format": {"note": "' + ("y" * 8 * 1024 * 1024) + '"}}\\n')
    sys.stdout.flush()
    raise SystemExit(0)

if behaviour == "no-packets":
    stream_json({"streams": [{"codec_type": "video", "codec_name": "h264",
                              "width": WIDTH, "height": HEIGHT}]})
    raise SystemExit(0)

if behaviour == "empty-streams":
    stream_json({"streams": []})
    raise SystemExit(0)

if role == "capability" and KIND == "ffmpeg":
    sys.stdout.write("Encoders:\\n V....D libx264                libx264 (codec h264)\\n"
                     " A....D aac                    AAC (codec aac)\\n")
    sys.stdout.flush()
    raise SystemExit(0)

if role == "measure":
    asked = flag_value("-t")
    progress(int(float(asked) * 1000000) if asked else OUT_US)
    raise SystemExit(0)

if role == "duration":
    stream_json({"format": {"duration": "%d.000000" % (OUT_US // 1000000)}})
    raise SystemExit(0)

if role == "streams":
    stream_json({"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "width": WIDTH,
         "height": HEIGHT, "nb_read_packets": "%d" % FRAMES, "duration": "4.000000"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "sample_rate": "%d" % RATE,
         "channels": 2, "channel_layout": "stereo", "nb_read_packets": "200",
         "duration": "4.000000"},
    ]})
    raise SystemExit(0)

if role == "probe-other":
    # what a real ffprobe answers for a file that is not media, which is what the
    # placeholder inputs here are: the capability preflight turns that into a warning
    sys.stderr.write("this fake tool does not read media headers\\n")
    raise SystemExit(1)

encode_output()
raise SystemExit(0)
'''


def live_runner_threads() -> list[str]:
    return sorted(
        thread.name for thread in threading.enumerate()
        if thread.name.startswith("process-runner-")
    )


def live_children() -> set[int]:
    """Direct children of this test process, from the kernel's own list.

    Independent of the runner's bookkeeping: an unreaped zombie stays on this list,
    and so does a child that was merely abandoned.
    """
    found: set[int] = set()
    for task in Path("/proc/self/task").iterdir():
        try:
            text = (task / "children").read_text()
        except OSError:  # a thread that ended between listing and reading
            continue
        found.update(int(field) for field in text.split())
    return found


@pytest.fixture(autouse=True)
def _nothing_outlives_the_test():
    """No drain thread, no child process may survive a render call in this file."""
    yield
    assert live_runner_threads() == [], "a subprocess runner thread outlived its job"
    assert live_children() == set(), f"child processes outlived the render call: {live_children()}"


class FakeTools:
    """Fake ffmpeg/ffprobe executables plus the directory they report into.

    A call's role is read from its own arguments: ``-encoders`` is the capability
    probe, ``-f null`` the decode measurement, ``-f concat`` the chunk join,
    ``-count_packets`` the stream verification, ``-show_format`` alone the duration
    probe.  What that role does comes from ``FAKE_ROLES``; every call appends its pid
    to ``<role>.calls``, so a test can prove which children ran and reap-check the one
    it cancelled.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.work = tmp_path / "tool-report"
        self.work.mkdir()
        self.ffmpeg = self._write(tmp_path / "fake-ffmpeg", "ffmpeg")
        self.ffprobe = self._write(tmp_path / "fake-ffprobe", "ffprobe")

    def _write(self, script: Path, kind: str) -> str:
        # the kind is baked into the copy: a child inherits its parent's environment,
        # so an environment variable could not tell the two tools apart
        script.write_text(
            f"#!{sys.executable}\nimport os\nos.environ['FAKE_KIND'] = {kind!r}\n{CHILD_SOURCE}",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return str(script)

    def install(self, monkeypatch, roles: dict[str, str]) -> None:
        """Roles not named here do their normal job (write the file, answer the JSON)."""
        monkeypatch.setenv("FAKE_WORK", str(self.work))
        monkeypatch.setenv("FAKE_ROLES", json.dumps(roles))
        monkeypatch.setenv("FAKE_FLOOD_LINES", str(FLOOD_LINES))
        monkeypatch.setenv("FAKE_FRAMES", str(FRAMES))
        monkeypatch.setenv("FAKE_WIDTH", str(WIDTH))
        monkeypatch.setenv("FAKE_HEIGHT", str(HEIGHT))
        monkeypatch.setenv("FAKE_SAMPLE_RATE", str(SAMPLE_RATE))
        monkeypatch.setenv("FAKE_OUTPUT_US", str(int(SECONDS * 1_000_000)))

    def calls(self, role: str) -> list[int]:
        try:
            lines = (self.work / f"{role}.calls").read_text().splitlines()
        except OSError:
            return []
        return [int(line) for line in lines if line.strip()]

    def call_count(self, role: str) -> int:
        return len(self.calls(role))

    def wait_for_call(self, role: str, within: float = 30.0) -> int:
        """The pid of that role's child, once it has started (not once it finished)."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            pids = self.calls(role)
            if pids:
                return pids[-1]
            time.sleep(0.01)
        raise AssertionError(f"the {role!r} child never started within {within} s")

    def wait_for_stderr_bytes(self, role: str, at_least: int, within: float = 30.0) -> int:
        """Wait until the flooding child has written *at_least* bytes of stderr.

        That counter is the readiness signal for cancelling a flood: a runner that
        does not drain stderr leaves the child blocked at one pipe-full, so the
        counter never passes it and this raises instead of quietly passing.
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


class Canceller:
    """Sets the job's token from another thread, once *ready* says the child is there."""

    def __init__(self, ready) -> None:
        self.event = threading.Event()
        self.set_at: float | None = None
        self._ready = ready
        self._fault: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="test-canceller", daemon=True)

    def _run(self) -> None:
        try:
            self._ready()
        except BaseException as exc:  # reported where pytest can see it, not swallowed
            self._fault = exc
            self.event.set()  # never leave the job waiting for a cancel that will not come
            return
        self.set_at = time.monotonic()
        self.event.set()

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        self._thread.join(timeout=60)
        assert not self._thread.is_alive(), "the thread that cancels did not finish"
        if self._fault is not None:
            raise self._fault
        assert self.set_at is not None, "the child never reached the point to cancel at"


def assert_reaped(pid: int) -> None:
    """Gone entirely: a child that was never waited for stays a zombie to ``os.kill``."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    pytest.fail(f"pid {pid} is still there: a live process or an unreaped zombie was left")


def composition() -> Project:
    """4 s of 25 fps / 48 kHz stereo, as three items so a chunked render can be forced.

    Two adjacent picture items (0-2 s and 2-4 s) and one sound item (0-4 s).  The
    picture clip stops at 4 s of a stream recorded to 100 s, so the picture-tail
    decode — ``media/framegrab``, whose child does not take a token until TASK_17 —
    is never asked for, and this file measures the exporter's own children only.
    """
    project = Project(
        output=OutputSettings(
            width=WIDTH, height=HEIGHT, frame_rate=FPS, sample_rate=SAMPLE_RATE,
            channel_layout="stereo",
        )
    )
    project = RegisterSourceCommand(
        StreamSource("va", 0, TrackKind.VIDEO, F(0), F(100), frame_rate=FPS,
                     width=WIDTH, height=HEIGHT, stream_position=0)
    ).apply(project)
    project = RegisterSourceCommand(
        StreamSource("aa", 1, TrackKind.AUDIO, F(0), SECONDS, sample_rate=SAMPLE_RATE,
                     stream_position=0)
    ).apply(project)
    project = project.with_tracks(
        (Track("vt", TrackKind.VIDEO, "V1"), Track("at", TrackKind.AUDIO, "A1"))
    )
    project = AddClipCommand("va", 0, "vt", F(0), source_in=F(0), source_out=F(2),
                             clip_id="v1").apply(project)
    project = AddClipCommand("va", 0, "vt", F(2), source_in=F(2), source_out=SECONDS,
                             clip_id="v2").apply(project)
    # this clip reads to its stream's recorded end, so the sound tail is measured
    project = AddClipCommand("aa", 1, "at", F(0), source_in=F(0), source_out=SECONDS,
                             clip_id="a1").apply(project)
    return project


def asset_paths(tmp_path: Path) -> dict[str, Path]:
    """Placeholder inputs: these tests are about children, never about decoding."""
    paths = {}
    for asset_id, name in (("va", "video.mkv"), ("aa", "audio.wav")):
        path = tmp_path / name
        path.write_bytes(b"placeholder input, never read by these tests")
        paths[asset_id] = path
    return paths


def request_for(tmp_path: Path, tools: FakeTools, destination: Path, **overrides):
    return RenderRequest(
        project=composition(),
        asset_paths=asset_paths(tmp_path),
        destination=destination,
        ffmpeg=tools.ffmpeg,
        ffprobe=tools.ffprobe,
        **overrides,
    )


def staging_files(tmp_path: Path) -> list[str]:
    """Every owned temporary: staging file, chunk intermediates and the concat list."""
    return sorted(path.name for path in tmp_path.glob("*.partial.*"))


def flood_written_bytes() -> int:
    """What the flood fixture writes, counted here from the same line list."""
    lines = sum(len(f"e{index}") + 1 for index in range(FLOOD_LINES))
    return lines + len(f"FLOOD-END-{FLOOD_LINES}\n")


# ------------------------------------------------------------------ a flood on stderr


def test_a_stderr_flood_with_stdout_idle_does_not_stop_a_render(tmp_path, monkeypatch):
    """The exact old deadlock: megabytes of stderr, nothing on stdout, exit 0.

    The old loop read stdout lines and only then waited, so this child blocked in
    ``write`` once its 65 536-byte pipe was full and never came back.  Here the
    render finishes: the file is published and no temporary is left.  The bound is
    arithmetic — this job states an encode deadline of at least 600 s — so answering
    in under 15 s means the pipes were drained while the child ran, and the child's
    own exit status (0) is what ended the job.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"encode": "flood-ok"})
    out = tmp_path / "movie.mp4"
    started = time.monotonic()
    result = render(request_for(tmp_path, tools, out))
    elapsed = time.monotonic() - started

    written = flood_written_bytes()
    assert written > 4 * PIPE_FULL_BYTES, f"the flood is only {written} bytes"
    assert elapsed < 15.0, f"{elapsed:.3f} s: drained, or waited for the deadline?"
    assert out.exists() and out.stat().st_size > 0
    assert staging_files(tmp_path) == []
    # the counts come back from the verification probes, which answered the plan's
    # own numbers: 4 s at 25 fps and 48 kHz
    assert result.produced_frames == FRAMES
    assert result.produced_audio_samples == SAMPLES
    assert result.chunks == 1


def test_a_stderr_flood_that_fails_names_the_end_of_the_diagnostic(tmp_path, monkeypatch):
    """A failing child that floods: the error line survives, and the cut says so.

    The capture keeps one pipe-full of the **tail**, because that is where FFmpeg's
    own error is.  The dropped count is the subtraction the fixture implies:
    everything written (:func:`flood_written_bytes`, counted in this test) minus the
    one pipe-full that was kept.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"encode": "flood-fail"})
    out = tmp_path / "movie.mp4"
    with pytest.raises(RenderError) as refused:
        render(request_for(tmp_path, tools, out))

    message = str(refused.value)
    assert f"FLOOD-END-{FLOOD_LINES}" in message, message[:400]
    assert f"{flood_written_bytes() - PIPE_FULL_BYTES} earlier stderr bytes" in message, message
    assert not out.exists()
    assert staging_files(tmp_path) == []


# -------------------------------------------------------------- cancelled children


def test_a_silent_hanging_encoder_is_cancelled_in_bounded_time(tmp_path, monkeypatch):
    """No output at all: the cancel check may not depend on any stream producing.

    Bound: 1 s from the Event being set, derived from 0.02 s polling + a measured
    1.3 ms ``SIGTERM`` death + millisecond joins.  The child sleeps 60 s, which is
    what a job that cannot see the Event would sit through.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"encode": "hang"})
    canceller = Canceller(lambda: tools.wait_for_call("encode"))
    canceller.start()
    out = tmp_path / "movie.mp4"
    seen: list[float] = []
    try:
        with pytest.raises(RenderCancelled):
            render(
                request_for(tmp_path, tools, out),
                on_progress=seen.append,
                cancel_event=canceller.event,
            )
    finally:
        canceller.join()
    answered = time.monotonic() - canceller.set_at
    assert answered < CANCEL_BOUND_SECONDS, f"cancel answered in {answered:.3f} s"
    assert_reaped(tools.wait_for_call("encode"))
    # a child that never wrote stdout produced no progress event, and no completion
    assert seen == [], seen
    assert not out.exists()
    assert staging_files(tmp_path) == []


def test_a_child_flooding_stderr_while_stdout_is_idle_is_cancelled(tmp_path, monkeypatch):
    """The other half of the old defect: a cancel request in front of a flood.

    The old code checked cancellation only inside its ``for line in proc.stdout``
    loop, and this child never writes to stdout, so the request was never looked at;
    its stderr also filled the pipe and blocked it in ``write`` forever.  Here the
    token is set only once the child has proved it wrote **more than one pipe-full**
    of stderr (its own counter), and the job answers inside the same derived second.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"encode": "flood-hang"})

    def ready() -> None:
        tools.wait_for_call("encode")
        tools.wait_for_stderr_bytes("encode", PIPE_FULL_BYTES + 4_096)

    canceller = Canceller(ready)
    canceller.start()
    out = tmp_path / "movie.mp4"
    try:
        with pytest.raises(RenderCancelled):
            render(request_for(tmp_path, tools, out), cancel_event=canceller.event)
    finally:
        canceller.join()
    answered = time.monotonic() - canceller.set_at
    assert answered < CANCEL_BOUND_SECONDS, f"cancel answered in {answered:.3f} s"
    assert_reaped(tools.wait_for_call("encode"))
    assert not out.exists()
    assert staging_files(tmp_path) == []


def test_a_cancelled_token_before_launch_starts_no_child_at_all(tmp_path, monkeypatch):
    """Being cancelled while queued is not the same event as cancelling a running child."""
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {
        "capability": "hang", "encode": "hang", "measure": "hang", "join": "hang",
        "duration": "hang", "streams": "hang",
    })
    cancel = threading.Event()
    cancel.set()
    started = time.monotonic()
    with pytest.raises(RenderCancelled):
        render(request_for(tmp_path, tools, tmp_path / "movie.mp4"), cancel_event=cancel)
    assert time.monotonic() - started < 1.0, "a cancelled job still did work"
    for role in ("capability", "encode", "measure", "join", "duration", "streams"):
        assert tools.call_count(role) == 0, f"a cancelled job still started {role}"
    assert staging_files(tmp_path) == []


def test_a_hanging_capability_probe_is_a_cancellation_not_a_missing_encoder(
    tmp_path, monkeypatch
):
    """Cancelled and failed must not collapse into one message.

    ``encoder_available`` answers False for every way it can fail, so a naive caller
    would report "this FFmpeg cannot encode libx264" about a build that was never
    asked.  The job owns the token, so it says cancelled instead.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"capability": "hang"})
    canceller = Canceller(lambda: tools.wait_for_call("capability"))
    canceller.start()
    try:
        with pytest.raises(RenderCancelled):
            render(
                request_for(tmp_path, tools, tmp_path / "movie.mp4"),
                cancel_event=canceller.event,
            )
    finally:
        canceller.join()
    answered = time.monotonic() - canceller.set_at
    assert answered < CANCEL_BOUND_SECONDS, f"cancel answered in {answered:.3f} s"
    assert_reaped(tools.wait_for_call("capability"))
    assert tools.call_count("encode") == 0
    assert staging_files(tmp_path) == []


def test_a_cancelled_source_tail_probe_never_becomes_a_warning_and_a_render(
    tmp_path, monkeypatch
):
    """"Could not be measured" is a warning; "cancelled" has to stop the job.

    The sound clip here reads to its stream's recorded end, so the last moments are
    measured before anything is encoded.  When that measuring child is cancelled the
    render must end there — treating it as an unmeasurable tail would run the encoder
    and publish a file nobody asked for.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"measure": "hang"})
    canceller = Canceller(lambda: tools.wait_for_call("measure"))
    canceller.start()
    out = tmp_path / "movie.mp4"
    try:
        with pytest.raises(RenderCancelled):
            render(request_for(tmp_path, tools, out), cancel_event=canceller.event)
    finally:
        canceller.join()
    answered = time.monotonic() - canceller.set_at
    assert answered < CANCEL_BOUND_SECONDS, f"cancel answered in {answered:.3f} s"
    assert_reaped(tools.wait_for_call("measure"))
    assert tools.call_count("encode") == 0, "the encoder ran after a cancelled probe"
    assert not out.exists()
    assert staging_files(tmp_path) == []


def test_cancelling_the_final_verification_publishes_nothing(tmp_path, monkeypatch):
    """A cancel that arrives during verification arrives before the file exists.

    The destination already holds the user's file and was approved for replacement,
    so nothing here refuses on protection grounds: only the cancellation can stop
    publication, and the user's bytes have to be identical afterwards.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"streams": "hang"})
    out = tmp_path / "movie.mp4"
    original = b"the export the user already had, byte for byte\n"
    out.write_bytes(original)
    canceller = Canceller(lambda: tools.wait_for_call("streams"))
    canceller.start()
    try:
        with pytest.raises(RenderCancelled):
            render(request_for(tmp_path, tools, out, overwrite=True), cancel_event=canceller.event)
    finally:
        canceller.join()
    answered = time.monotonic() - canceller.set_at
    assert answered < CANCEL_BOUND_SECONDS, f"cancel answered in {answered:.3f} s"
    assert_reaped(tools.wait_for_call("streams"))
    assert out.read_bytes() == original, "a cancelled render changed the user's file"
    assert staging_files(tmp_path) == []
    assert tools.call_count("encode") == 1, "the encode itself still ran once"


def test_a_hanging_join_after_lossless_chunks_is_cancelled_and_its_files_removed(
    tmp_path, monkeypatch
):
    """Chunking must not escape the token, and must not leave its intermediates.

    The chunk files really exist while the join hangs — they are listed at the
    instant the token is set — and after the cancellation none of them, nor the
    concat list, nor the staging file, is left behind.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"join": "hang"})
    seen_while_hung: list[str] = []

    def ready() -> None:
        tools.wait_for_call("join")
        seen_while_hung.extend(staging_files(tmp_path))

    canceller = Canceller(ready)
    canceller.start()
    out = tmp_path / "movie.mp4"
    try:
        with pytest.raises(RenderCancelled):
            render(
                request_for(tmp_path, tools, out, chunk_item_limit=1),
                cancel_event=canceller.event,
            )
    finally:
        canceller.join()
    answered = time.monotonic() - canceller.set_at
    assert answered < CANCEL_BOUND_SECONDS, f"cancel answered in {answered:.3f} s"
    assert tools.call_count("encode") == 2, "three items at one per chunk is two chunks"
    assert any(".chunk" in name for name in seen_while_hung), seen_while_hung
    assert any(name.endswith(".concat.txt") for name in seen_while_hung), seen_while_hung
    assert_reaped(tools.wait_for_call("join"))
    assert staging_files(tmp_path) == []
    assert not out.exists()


# --------------------------------------------- refused output and malformed answers


def test_a_deadline_that_passes_is_a_failure_and_not_a_cancellation(tmp_path, monkeypatch):
    """The deadline exists, and it is reported as failure rather than as "cancelled".

    The deadline is shortened here so the test costs a second instead of an hour;
    what is asserted is the *kind* of answer, and that the over-deadline child was
    stopped and reaped instead of left running.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"encode": "hang"})
    monkeypatch.setattr(exporter, "FFMPEG_JOB_MIN_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(exporter, "FFMPEG_JOB_TIMEOUT_PER_OUTPUT_SECOND", 0.0)
    out = tmp_path / "movie.mp4"
    started = time.monotonic()
    with pytest.raises(RenderError) as refused:
        render(request_for(tmp_path, tools, out))
    elapsed = time.monotonic() - started
    assert not isinstance(refused.value, RenderCancelled)
    assert "did not finish within" in str(refused.value)
    assert elapsed < REAL_CANCEL_BOUND_SECONDS, f"deadline answered in {elapsed:.3f} s"
    assert_reaped(tools.wait_for_call("encode"))
    assert not out.exists()
    assert staging_files(tmp_path) == []


@pytest.mark.parametrize(
    "streams_behaviour, message",
    [
        ("garbage", "no stream information"),
        ("empty-streams", "no readable streams"),
        ("no-packets", "could not count the packets"),
        ("huge", "explicit limit"),
    ],
    ids=["not json", "no streams", "un-countable", "over the cap"],
)
def test_a_malformed_or_oversized_verification_answer_is_refused_and_publishes_nothing(
    tmp_path, monkeypatch, streams_behaviour, message
):
    """Half an answer is never a pass: nothing is published, and the reason is named.

    The oversized case is the capture limit doing its job: 8 MiB of stdout against
    the stated 4 MiB ceiling (4 * 1024 * 1024 = 4 194 304) is refused by name
    of parsed from truncated bytes.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"streams": streams_behaviour})
    out = tmp_path / "movie.mp4"
    started = time.monotonic()
    with pytest.raises(RenderError, match=message):
        render(request_for(tmp_path, tools, out))
    assert not out.exists()
    assert staging_files(tmp_path) == []
    assert time.monotonic() - started < 30.0, "a bad answer should not cost a timeout"


def test_a_long_progress_stream_is_counted_without_being_buffered(tmp_path, monkeypatch):
    """The decode measurement streams its progress and keeps only the furthest stamp.

    40 000 progress lines is well over a megabyte of stdout; the answer is the
    largest ``out_time_us`` the fixture wrote, written out here as a fraction by hand
    (3 999 999 µs = 3999999/1000000 s) rather than read back from the implementation.
    Nothing else from that stream is kept by the caller, and the decoded samples
    never leave FFmpeg at all: the measuring graph ends in ``-f null``.
    """
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"measure": "many-lines"})
    started = time.monotonic()
    produced = measure_decoded_span(tools.ffmpeg, tmp_path / "anything.wav", "a", 0)
    elapsed = time.monotonic() - started
    assert produced == F(3_999_999, 1_000_000)
    assert elapsed < 30.0, f"{elapsed:.3f} s to read 40 000 progress lines"


# ------------------------------------------------------------- the capability probe


def test_the_encoder_probe_answers_for_a_real_ffmpeg_and_refuses_a_fiction():
    """The migrated probe still answers the real question, and fails closed."""
    if not HAS_FFMPEG:
        pytest.skip("requires ffmpeg on PATH")
    assert encoder_available(shutil.which("ffmpeg"), "libx264") is True
    assert encoder_available(shutil.which("ffmpeg"), "not-a-real-codec-8f21") is False
    assert encoder_available("/nonexistent/ffmpeg-not-here", "libx264") is False


def test_the_encoder_probe_stops_when_the_job_is_cancelled(tmp_path, monkeypatch):
    """A capability check is a child process too, so the job's token reaches it."""
    tools = FakeTools(tmp_path)
    tools.install(monkeypatch, {"capability": "hang"})
    outcome: list[tuple[bool, float]] = []
    canceller = Canceller(lambda: tools.wait_for_call("capability"))

    def probe() -> None:
        outcome.append(
            (encoder_available(tools.ffmpeg, "libx264", cancel=canceller.event), time.monotonic())
        )

    thread = threading.Thread(target=probe, name="probe-thread")
    started = time.monotonic()
    thread.start()
    canceller.start()
    try:
        canceller.join()
    finally:
        thread.join(timeout=60)
    assert not thread.is_alive(), "the capability probe outlived the cancellation"
    assert outcome, "the capability probe never answered"
    available, finished = outcome[0]
    assert available is False, "a cancelled probe must not claim an encoder exists"
    assert finished - canceller.set_at < CANCEL_BOUND_SECONDS
    assert finished - started < 10.0, "the probe waited out its own 30 s deadline"
    assert_reaped(tools.wait_for_call("capability"))


# --------------------------------------------------------------------- real FFmpeg


@needs_ffmpeg
def test_a_real_ffmpeg_render_is_cancelled_and_leaves_nothing_behind(tmp_path):
    """Real encoder, real probes, cancellation raised by the progress callback itself.

    The token is set on the first real ``out_time_us`` event, which is what the GUI's
    cancel does at the earliest moment it can.  Bound: 5 s — the runner's documented
    worst path (0.02 poll + 2.0 grace + 2.0 join) with margin, generous because
    FFmpeg gets to close the file it was writing.  A 12-second 320x240 export takes
    about 2 s here, so a job that ignored the Event would simply finish and publish.
    """
    source = make_long_video(tmp_path, seconds=12)
    project = Project(output=OutputSettings(320, 240, FPS, SAMPLE_RATE, "stereo"))
    project = RegisterSourceCommand(
        StreamSource("big", 0, TrackKind.VIDEO, F(0), F(12), frame_rate=FPS,
                     width=320, height=240, stream_position=0)
    ).apply(project)
    project = project.with_tracks((Track("v", TrackKind.VIDEO, "V1"),))
    project = AddClipCommand("big", 0, "v", F(0), source_in=F(0), source_out=F(12),
                             clip_id="full").apply(project)
    out = tmp_path / "never.mp4"
    cancel = threading.Event()
    seen: list[float] = []
    cancelled_at: list[float] = []

    def report(fraction: float) -> None:
        seen.append(fraction)
        if not cancel.is_set():
            cancel.set()
            cancelled_at.append(time.monotonic())

    before = live_children()
    with pytest.raises(RenderCancelled):
        render(
            RenderRequest(project=project, asset_paths={"big": source}, destination=out),
            on_progress=report,
            cancel_event=cancel,
        )
    assert cancelled_at, "the real encode never reported progress"
    answered = time.monotonic() - cancelled_at[0]
    assert answered < REAL_CANCEL_BOUND_SECONDS, f"cancel answered in {answered:.3f} s"
    assert seen, "a real encode should have reported progress before it was cancelled"
    assert all(value < 1.0 for value in seen), seen  # no completion event after cancel
    assert not out.exists()
    assert staging_files(tmp_path) == []
    assert live_children() == before, "the cancelled FFmpeg was left running or unreaped"


@needs_ffmpeg
def test_a_real_ffmpeg_render_still_succeeds_and_streams_progress(tmp_path):
    """Nothing about a working render moved: progress streams, and the counts hold.

    2 s at 25 fps is **50 frames** — arithmetic, and the frame count the exporter
    reports is the one it read back out of the produced file with ``-count_packets``.
    """
    source = make_long_video(tmp_path, seconds=12)
    project = Project(output=OutputSettings(320, 240, FPS, SAMPLE_RATE, "stereo"))
    project = RegisterSourceCommand(
        StreamSource("big", 0, TrackKind.VIDEO, F(0), F(12), frame_rate=FPS,
                     width=320, height=240, stream_position=0)
    ).apply(project)
    project = project.with_tracks((Track("v", TrackKind.VIDEO, "V1"),))
    project = AddClipCommand("big", 0, "v", F(0), source_in=F(0), source_out=F(2),
                             clip_id="front").apply(project)
    out = tmp_path / "front.mp4"
    seen: list[float] = []
    result = render(
        RenderRequest(project=project, asset_paths={"big": source}, destination=out),
        on_progress=seen.append,
    )
    assert result.produced_frames == 50, "2 s at 25 fps is 50 frames"
    assert result.chunks == 1
    assert seen and seen[-1] == pytest.approx(1.0)
    assert all(0.0 <= value <= 1.0 for value in seen), seen
    # at least one event from the child's own stream, plus the completion event
    assert len(seen) >= 2, seen
    assert staging_files(tmp_path) == []
