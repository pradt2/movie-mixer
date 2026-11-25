"""End-to-end export tests: real FFmpeg render, then decode and measure.

Fixtures are synthetic solid-colour/sine media (never user films). Correctness
is checked by decoding the exported file: frame colours at exact output frame
times, spectral magnitude and level of each tone, silence in gaps, plus
duration, cancellation, non-overwrite and chunk-join behaviour.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import threading
from array import array
from fractions import Fraction
from pathlib import Path

import pytest

from media_fixtures import HAS_FFMPEG, make_colour_steps, make_long_video, make_tone
from video_editor.core.commands import (
    AddClipCommand,
    RegisterSourceCommand,
    SetFadesCommand,
    SetGainCommand,
)
from video_editor.core.model import OutputSettings, Project, StreamSource, Track, TrackKind
from video_editor.fs.identity import Identity
from video_editor.render.exporter import (
    RenderCancelled,
    RenderError,
    RenderRequest,
    probe_duration,
    render,
)
from video_editor.render.plan import build_plan

F = Fraction
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")

FPS = F(25)
SR = 48000
PALETTE = ["red", "green", "blue", "magenta", "yellow", "cyan"]


# --------------------------------------------------------------------- project


def composition(steps: Path, tone660: Path, tone990: Path) -> tuple[Project, dict[str, Path]]:
    """Bottom picture + rate-2 overlay + two independent audio contributions."""
    p = Project(
        output=OutputSettings(width=128, height=96, frame_rate=FPS, sample_rate=SR,
                              channel_layout="stereo")
    )
    p = RegisterSourceCommand(
        StreamSource("steps", 0, TrackKind.VIDEO, F(0), F(6), frame_rate=FPS,
                         width=128, height=96, stream_position=0)
    ).apply(p)
    p = RegisterSourceCommand(
        StreamSource("t660", 0, TrackKind.AUDIO, F(0), F(6), sample_rate=SR, stream_position=0)
    ).apply(p)
    p = RegisterSourceCommand(
        StreamSource("t990", 0, TrackKind.AUDIO, F(0), F(6), sample_rate=SR, stream_position=0)
    ).apply(p)
    p = p.with_tracks(
        (
            Track("vtop", TrackKind.VIDEO, "Top"),
            Track("vbot", TrackKind.VIDEO, "Bottom"),
            Track("a1", TrackKind.AUDIO, "A1"),
            Track("a2", TrackKind.AUDIO, "A2"),
        )
    )
    p = AddClipCommand("steps", 0, "vbot", F(0), source_in=F(0), source_out=F(6),
                       clip_id="bottom").apply(p)
    # 2 source seconds in 1 output second: rate = source s / output s = 2
    p = AddClipCommand("steps", 0, "vtop", F(1), source_in=F(2), source_out=F(4), rate=F(2),
                       clip_id="toprate").apply(p)
    p = AddClipCommand("t660", 0, "a1", F(0), source_in=F(0), source_out=F(6),
                       clip_id="tone660").apply(p)
    p = SetFadesCommand("tone660", F(1), F(0)).apply(p)
    p = AddClipCommand("t990", 0, "a2", F(4), source_in=F(0), source_out=F(2),
                       clip_id="tone990").apply(p)
    p = SetGainCommand("tone990", F(1, 2)).apply(p)
    paths = {"steps": steps, "t660": tone660, "t990": tone990}
    return p, paths


# ------------------------------------------------------------------- measuring


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", *args],
                          capture_output=True, timeout=180)


def frame_at(path: Path, frame: int, fps: Fraction = FPS) -> tuple[int, int, int]:
    """RGB of the frame with output index ``frame``.

    Selected by decoded frame index instead of an input ``-ss``: input seeking
    keeps the first frame at or after the timestamp, which silently measures the
    *next* frame. Fixtures are short, so decoding from the start is affordable.
    """
    del fps  # index selection is rate independent; the parameter documents intent
    proc = run([
        "-i", str(path), "-vf", f"select=eq(n\\,{frame})", "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ])
    assert proc.returncode == 0, proc.stderr.decode()
    data = proc.stdout
    assert len(data) >= 3, f"no frame {frame} decoded from {path}"
    return (data[0], data[1], data[2])


def pcm(path: Path, start: Fraction, seconds: Fraction) -> list[float]:
    """Mono float samples of ``[start, start+seconds)`` from the export.

    Trimmed after decoding (sample accurate) rather than with an input seek.
    """
    proc = run([
        "-i", str(path), "-vn",
        "-af", f"atrim=start={float(start):.9f}:end={float(start + seconds):.9f}",
        "-ac", "1", "-ar", str(SR), "-f", "f32le", "-",
    ])
    assert proc.returncode == 0, proc.stderr.decode()
    return list(array("f", proc.stdout))


def goertzel(samples: list[float], freq: float) -> float:
    """Magnitude at one frequency (pure Python, tolerance-driven use only)."""
    if not samples:
        return 0.0
    omega = 2 * math.pi * freq / SR
    coeff = 2 * math.cos(omega)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s1, s2 = s0, s1
    return math.sqrt(max(0.0, s1 * s1 + s2 * s2 - coeff * s1 * s2)) / len(samples)


def rms(samples: list[float]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(x * x for x in samples) / len(samples))


def reference_colours(steps: Path) -> dict[str, tuple[int, int, int]]:
    """Sample the source fixture itself so expectations are not hard-coded."""
    return {colour: frame_at(steps, index * 25 + 12) for index, colour in enumerate(PALETTE)}


def nearest(reference: dict[str, tuple[int, int, int]], rgb) -> str:
    def distance(name: str) -> int:
        return sum((a - b) ** 2 for a, b in zip(reference[name], rgb, strict=False))

    return min(reference, key=distance)


# --------------------------------------------------------------------- exports


@pytest.fixture()
def built(tmp_path):
    steps = make_colour_steps(tmp_path)
    t660 = make_tone(tmp_path, "tone660.wav", 660)
    t990 = make_tone(tmp_path, "tone990.wav", 990)
    return steps, t660, t990


def export(project, paths, destination, **kwargs) -> Path:
    request = RenderRequest(project=project, asset_paths=paths, destination=destination, **kwargs)
    result = render(request)
    assert result.path == destination and destination.is_file()
    return destination


def test_export_reproduces_picture_and_sound(tmp_path, built):
    steps, t660, t990 = built
    project, paths = composition(steps, t660, t990)
    reference = reference_colours(steps)
    out = tmp_path / "composition.mp4"
    export(project, paths, out, out_end=F(7))

    assert probe_duration(FFPROBE, out) <= F(7) + F(1, 25) + F(2048, SR) + F(1, 1000)
    assert probe_duration(FFPROBE, out) >= F(7) - F(1, 10)

    # one output frame per check, at mid-frame times
    checks = {
        12: "red",       # bottom only, source 0.5
        30: "blue",      # rate-2 overlay: source 2 + 0.22*2 = 2.44
        45: "magenta",   # rate-2 overlay: source 2 + 1.64 = 3.64
        55: "blue",      # bottom only, source 2.22
        75: "magenta",   # bottom only, source 3.02
        100: "yellow",    # bottom only, source 4.02
        162: "black",    # past every clip: explicit black
    }
    for frame, expected in checks.items():
        rgb = frame_at(out, frame)
        if expected == "black":
            assert sum(rgb) < 40, f"frame {frame} not black: {rgb}"
        else:
            assert nearest(reference, rgb) == expected, (frame, rgb, expected)

    # audio: fade-in level, single tone, then the second contribution at half gain
    early = pcm(out, F(1, 100), F(1, 10))
    steady = pcm(out, F(2), F(1, 10))
    assert goertzel(steady, 660) > 0.02
    assert goertzel(early, 660) < 0.4 * goertzel(steady, 660)  # 1 s fade-in honoured
    assert goertzel(steady, 990) < 0.2 * goertzel(steady, 660)  # not started yet

    both = pcm(out, F(9, 2), F(1, 5))
    low_660, low_990 = goertzel(both, 660), goertzel(both, 990)
    assert low_660 > 0.02 and low_990 > 0.02  # both tracks contribute
    ratio = low_990 / low_660
    assert 0.2 < ratio < 0.9, f"990 Hz should sit near half level, got {ratio:.2f}"

    # gap after the last clip is silent, not a hold of the last sound
    silent = pcm(out, F(62, 10), F(7, 10))
    assert rms(silent) < 0.01


def faded_project(steps: Path, tone660: Path, tone990: Path) -> tuple[Project, dict[str, Path]]:
    """Sound that starts two seconds into the output, faded at its own edges."""
    p = Project(
        output=OutputSettings(width=128, height=96, frame_rate=FPS, sample_rate=SR,
                              channel_layout="stereo")
    )
    p = RegisterSourceCommand(
        StreamSource("steps", 0, TrackKind.VIDEO, F(0), F(6), frame_rate=FPS,
                     width=128, height=96, stream_position=0)
    ).apply(p)
    p = RegisterSourceCommand(
        StreamSource("t660", 0, TrackKind.AUDIO, F(0), F(4), sample_rate=SR, stream_position=0)
    ).apply(p)
    p = RegisterSourceCommand(
        StreamSource("t990", 0, TrackKind.AUDIO, F(0), F(4), sample_rate=SR, stream_position=0)
    ).apply(p)
    p = p.with_tracks((
        Track("v", TrackKind.VIDEO, "V"),
        Track("a1", TrackKind.AUDIO, "A1"),
        Track("a2", TrackKind.AUDIO, "A2"),
    ))
    p = AddClipCommand("steps", 0, "v", F(0), source_in=F(0), source_out=F(6),
                       clip_id="pic").apply(p)
    # both contributions occupy output [2,6): a fade-out over [4,6) ...
    p = AddClipCommand("t660", 0, "a1", F(2), source_in=F(0), source_out=F(4),
                       clip_id="fading-out").apply(p)
    p = SetFadesCommand("fading-out", F(0), F(2)).apply(p)
    # ... and a fade-in over [2,4)
    p = AddClipCommand("t990", 0, "a2", F(2), source_in=F(0), source_out=F(4),
                       clip_id="fading-in").apply(p)
    p = SetFadesCommand("fading-in", F(2), F(0)).apply(p)
    return p, {"steps": steps, "t660": tone660, "t990": tone990}


def test_fades_are_anchored_to_output_time_not_to_the_clip_local_stream(tmp_path, built):
    """A fade must ramp at its output-time anchor, wherever the clip was placed.

    The envelope reaches FFmpeg as an expression in range-relative ``t``, but it
    used to be applied to the clip's own sliced stream *before* the leading
    silence that places the clip inside the range. That stream's ``t`` restarts at
    zero, so an anchor at output 4 s was never reached and the fade did nothing at
    all (or ramped in the wrong place). A clip starting exactly at the range start
    hides the bug, because there the two clocks agree: that is why the existing
    fade check, whose clip starts at 0, passed.
    """
    steps, t660, t990 = built
    project, paths = faded_project(steps, t660, t990)
    out = tmp_path / "fades.mp4"
    export(project, paths, out, out_end=F(6))

    loud660 = pcm(out, F(22, 10), F(5, 10))  # [2.2, 2.7): 660 Hz before its ramp
    assert goertzel(loud660, 660) > 0.02, "the 660 Hz contribution is missing"
    full990 = pcm(out, F(54, 10), F(4, 10))  # [5.4, 5.8): 990 Hz after its ramp
    assert goertzel(full990, 990) > 0.02, "the 990 Hz contribution is missing"

    before = pcm(out, F(35, 10), F(4, 10))  # [3.5, 3.9): just before the fade-out
    faded = pcm(out, F(55, 10), F(4, 10))  # [5.5, 5.9): near the end of that ramp
    assert goertzel(before, 660) > 0.8 * goertzel(loud660, 660), "level dropped before its ramp"
    assert goertzel(faded, 660) < 0.2 * goertzel(loud660, 660), (
        f"fade-out did nothing: {goertzel(faded, 660):.4f} vs {goertzel(loud660, 660):.4f}")

    early = pcm(out, F(22, 10), F(4, 10))  # [2.2, 2.6): just inside the fade-in
    assert goertzel(early, 990) < 0.35 * goertzel(full990, 990), (
        f"fade-in did not start quiet: {goertzel(early, 990):.4f} vs "
        f"{goertzel(full990, 990):.4f}")

    # rendering only part of the range must not move a fade into that part
    window = tmp_path / "fades-window.mp4"
    export(project, paths, window, out_start=F(2), out_end=F(4))
    inside = pcm(window, F(1, 10), F(18, 10))  # whole window: 660 Hz at full level
    assert goertzel(inside, 660) > 0.02
    assert goertzel(inside, 990) > 0.01  # mid-ramp, but present
    tail = pcm(window, F(15, 10), F(4, 10))  # [3.5, 3.9) of the output
    assert goertzel(tail, 660) > 0.8 * goertzel(inside, 660), (
        "a fade outside the rendered range leaked into it")


def test_export_of_a_range_matches_the_range(tmp_path, built):
    steps, t660, t990 = built
    project, paths = composition(steps, t660, t990)
    out = tmp_path / "range.mp4"
    export(project, paths, out, out_start=F(2), out_end=F(4))
    produced = probe_duration(FFPROBE, out)
    assert abs(produced - 2) <= F(1, 25) + F(2048, SR) + F(1, 1000)
    reference = reference_colours(steps)
    # frame 25 of the range = output 3.02 absolute -> bottom source 3.02
    assert nearest(reference, frame_at(out, 25)) == "magenta"


def test_chunked_export_keeps_timing_and_stacking(tmp_path, built):
    steps, t660, t990 = built
    project, paths = composition(steps, t660, t990)
    out = tmp_path / "chunked.mp4"
    destination = export(project, paths, out, out_end=F(7), chunk_item_limit=2)
    produced = probe_duration(FFPROBE, destination)
    assert abs(produced - 7) <= F(1, 25) + F(2048, SR) + F(1, 10)
    reference = reference_colours(steps)
    # same frame indices as the single-pass render: joins did not shift anything
    for frame, expected in {12: "red", 30: "blue", 45: "magenta", 100: "yellow"}.items():
        assert nearest(reference, frame_at(out, frame)) == expected, frame
    assert rms(pcm(out, F(62, 10), F(7, 10))) < 0.01
    assert not list(tmp_path.glob("*.partial.*")) and not list(tmp_path.glob("*.chunk*"))


def test_cancel_leaves_no_output_and_no_temp_files(tmp_path):
    long_file = make_long_video(tmp_path, seconds=12)
    project = Project(output=OutputSettings(320, 240, FPS, SR, "stereo"))
    project = RegisterSourceCommand(
        StreamSource("big", 0, TrackKind.VIDEO, F(0), F(12), frame_rate=FPS,
                         width=320, height=240, stream_position=0)
    ).apply(project)
    project = project.with_tracks((Track("v", TrackKind.VIDEO, "V1"),))
    project = AddClipCommand("big", 0, "v", F(0), source_in=F(0), source_out=F(12),
                             clip_id="full").apply(project)
    out = tmp_path / "never.mp4"
    cancel = threading.Event()
    cancel.set()
    request = RenderRequest(project=project, asset_paths={"big": long_file}, destination=out)
    with pytest.raises(RenderCancelled):
        render(request, cancel_event=cancel)
    assert not out.exists()
    assert list(tmp_path.glob("*.partial.*")) == []


def test_existing_output_is_protected(tmp_path, built):
    steps, t660, t990 = built
    project, paths = composition(steps, t660, t990)
    out = tmp_path / "protect.mp4"
    export(project, paths, out, out_end=F(2))
    first = out.read_bytes()
    with pytest.raises(RenderError, match="already exists"):
        export(project, paths, out, out_end=F(2))
    assert out.read_bytes() == first  # untouched
    export(project, paths, out, out_end=F(2), overwrite=True)
    assert out.stat().st_size > 0


def test_missing_asset_is_reported(tmp_path, built):
    steps, t660, t990 = built
    project, paths = composition(steps, t660, t990)
    paths["t990"] = tmp_path / "gone.wav"
    # the range has to reach the material that asset provides: a range that never
    # reads an offline source is renderable, which is the point of scoping
    with pytest.raises(RenderError, match="cannot be used before rendering"):
        export(project, paths, tmp_path / "x.mp4", out_end=F(5))


def test_a_range_that_ignores_an_offline_asset_is_still_rendered(tmp_path, built):
    steps, t660, t990 = built
    project, paths = composition(steps, t660, t990)
    paths["t990"] = tmp_path / "gone.wav"
    export(project, paths, tmp_path / "early.mp4", out_end=F(2))  # only 0-2s: no 990 Hz
    assert (tmp_path / "early.mp4").stat().st_size > 0


def test_a_source_replaced_at_the_same_path_is_refused(tmp_path, built):
    """A different file at a recorded path is CHANGED, not 'a file is there'.

    The old bounds, stream indexes and frame rate describe the file that was
    imported, not whatever was written over it, so rendering must stop.
    """
    steps, t660, t990 = built
    project, paths = composition(steps, t660, t990)
    identities = {
        "steps": Identity.of(steps),
        "t660": Identity.of(t660),
        "t990": Identity.of(t990),
    }
    t990.write_bytes(b"not the tone any more, and a different length")
    with pytest.raises(RenderError, match=r"changed|not the file recorded"):
        export(project, paths, tmp_path / "x.mp4", out_end=F(5), asset_identities=identities)


def test_empty_range_is_refused(tmp_path, built):
    steps, t660, t990 = built
    project, paths = composition(steps, t660, t990)
    with pytest.raises(RenderError, match="empty"):
        export(project, paths, tmp_path / "empty.mp4", out_start=F(1), out_end=F(1))


def test_progress_reaches_one_and_never_exceeds_it(tmp_path, built):
    steps, t660, t990 = built
    project, paths = composition(steps, t660, t990)
    seen: list[float] = []
    request = RenderRequest(project=project, asset_paths=paths,
                            destination=tmp_path / "progress.mp4", out_end=F(3))
    render(request, on_progress=seen.append)
    assert seen and seen[-1] == pytest.approx(1.0)
    assert all(0.0 <= value <= 1.0 for value in seen)
    assert sorted(seen) == seen  # monotonic


def test_exported_streams_match_output_settings(tmp_path, built):
    steps, t660, t990 = built
    project, paths = composition(steps, t660, t990)
    out = tmp_path / "streams.mp4"
    export(project, paths, out, out_end=F(3))
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries",
         "stream=codec_type,width,height,r_frame_rate,sample_rate,channels",
         "-of", "default=noprint_wrappers=1", str(out)],
        capture_output=True, text=True, check=True,
    )
    text = proc.stdout
    assert "codec_type=video" in text and "codec_type=audio" in text
    assert "width=128" in text and "height=96" in text
    assert "sample_rate=48000" in text and "channels=2" in text
    assert "r_frame_rate=25/1" in text


def test_plan_snapshot_is_independent_of_later_edits(built):
    """A running export keeps the snapshot it was built from."""
    steps, t660, t990 = built
    project, _ = composition(steps, t660, t990)
    plan = build_plan(project, F(0), F(7))
    before = (plan.total_frames, plan.total_samples, len(plan.video_items))
    project = AddClipCommand("steps", 0, "vbot", F(8), source_in=F(0), source_out=F(2),
                             clip_id="later").apply(project)
    after = build_plan(project, F(0), F(7))
    assert (plan.total_frames, plan.total_samples, len(plan.video_items)) == before
    assert after.total_frames == before[0]  # the extra clip starts past this range
