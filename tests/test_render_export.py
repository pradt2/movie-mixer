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

from media_fixtures import (
    HAS_FFMPEG,
    identity_at,
    make_colour_steps,
    make_identity_offset,
    make_identity_video,
    make_long_video,
    make_tone,
    make_truncated_audio,
    make_truncated_identity_offset,
    source_from_probe,
)
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
    verify_output,
)
from video_editor.render.graph import LOSSLESS_INTERMEDIATE
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


def test_the_produced_file_is_counted_not_just_timed(tmp_path, built):
    """What the file holds is measured per stream, not inferred from duration.

    A container can report exactly the right duration while one of its streams is
    short, and a truncated file still reports a plausible one. So the frames are
    counted in the file and its sound is decoded and measured.
    """
    steps, t660, t990 = built
    project, paths = composition(steps, t660, t990)
    plan = build_plan(project, F(2), F(4))
    out = tmp_path / "counts.mp4"
    result = render(
        RenderRequest(project=project, asset_paths=paths, destination=out, out_start=F(2),
                      out_end=F(4))
    )
    assert result.produced_frames == plan.total_frames
    # measured by decoding: never less than asked for, and never more than the
    # encoder's own padding (2048 samples at 48 kHz; this render measured +256)
    assert plan.total_samples - 1 <= result.produced_audio_samples
    assert result.produced_audio_samples <= plan.total_samples + 2048

    facts = verify_output(FFPROBE, FFMPEG, out, plan)
    assert facts.audio_samples == result.produced_audio_samples
    assert (facts.video.width, facts.video.height) == (plan.width, plan.height)
    assert facts.audio.sample_rate == SR
    assert facts.audio.channels == 2
    assert facts.audio.channel_layout == "stereo"
    assert "50 frame(s)" in facts.describe()  # 2 s at 25 fps, counted in the file


def test_a_lossless_render_measures_exactly_the_planned_sound(tmp_path, built):
    """FFV1 + PCM adds nothing: the measured count is the plan's own.

    This is the case that packet arithmetic got wrong — PCM in Matroska writes
    4096-sample blocks, so 25 packets are not 25 samples — and it is the case the
    chunked exporter relies on: a chunk join can only be exact if the pieces and
    the whole are measured the same way.
    """
    steps, t660, t990 = built
    project, paths = composition(steps, t660, t990)
    plan = build_plan(project, F(2), F(4))
    out = tmp_path / "counts.mkv"
    result = render(
        RenderRequest(project=project, asset_paths=paths, destination=out, out_start=F(2),
                      out_end=F(4), preset=LOSSLESS_INTERMEDIATE)
    )
    assert result.produced_frames == plan.total_frames
    assert result.produced_audio_samples == plan.total_samples


def test_a_chunked_render_reports_exact_counts(tmp_path, built):
    """Chunking may not add or lose frames and samples at the joins."""
    steps, t660, t990 = built
    project, paths = composition(steps, t660, t990)
    plan = build_plan(project, F(0), F(7))
    out = tmp_path / "chunk_counts.mp4"
    result = render(
        RenderRequest(project=project, asset_paths=paths, destination=out, out_end=F(7),
                      chunk_item_limit=2)
    )
    assert result.chunks > 1
    assert result.produced_frames == plan.total_frames
    assert plan.total_samples - 1 <= result.produced_audio_samples
    assert result.produced_audio_samples <= plan.total_samples + 2048


def test_a_truncated_output_is_refused_even_though_it_reports_a_duration(tmp_path, built):
    """The claim in the header is not the content in the file."""
    steps, t660, t990 = built
    project, paths = composition(steps, t660, t990)
    plan = build_plan(project, F(0), F(6))
    out = tmp_path / "whole.mp4"
    render(RenderRequest(project=project, asset_paths=paths, destination=out))
    damage = tmp_path / "truncated.mp4"
    data = out.read_bytes()
    damage.write_bytes(data[: int(len(data) * 0.6)])

    # it still *claims* a duration, which is why duration alone is not a check
    claimed = probe_duration(FFPROBE, damage)
    assert claimed > 0
    with pytest.raises(RenderError):
        verify_output(FFPROBE, FFMPEG, damage, plan)


def test_a_source_shorter_than_its_own_header_is_refused_not_padded(tmp_path):
    """The container claims 6 s; the file holds about 4.2 s of sound.

    Padding would hide that completely: the export would be exactly as long as
    the plan asked, and the last one and a half seconds would be silence the film
    never had. So the end of any stream a clip reads up to is measured, and a real
    shortfall is an error rather than an ending.
    """
    media = make_truncated_audio(tmp_path)
    source = source_from_probe(media, 0, "broken")
    project = Project(output=OutputSettings(64, 64, FPS, SR, "stereo"))
    project = RegisterSourceCommand(source).apply(project)
    project = project.with_tracks((Track("a", TrackKind.AUDIO, "A1"),))
    project = AddClipCommand(
        "broken", source.stream_index, "a", F(0), source_in=source.origin,
        source_out=source.end, clip_id="a",
    ).apply(project)
    out = tmp_path / "padded.mp4"
    with pytest.raises(RenderError, match="before its recorded end"):
        render(RenderRequest(project=project, asset_paths={"broken": media}, destination=out))
    assert not out.exists()
    assert not list(tmp_path.glob("*.partial.*"))


def test_a_clip_that_stops_short_of_the_stream_end_needs_no_tail_probe(tmp_path):
    """A healthy clip does not pay for the check, and renders normally."""
    media = make_truncated_audio(tmp_path)
    source = source_from_probe(media, 0, "broken")
    project = Project(output=OutputSettings(64, 64, FPS, SR, "stereo"))
    project = RegisterSourceCommand(source).apply(project)
    project = project.with_tracks((Track("a", TrackKind.AUDIO, "A1"),))
    project = AddClipCommand(
        "broken", source.stream_index, "a", F(0), source_in=source.origin,
        source_out=source.origin + F(2), clip_id="a",
    ).apply(project)
    out = tmp_path / "front.mp4"
    result = render(RenderRequest(project=project, asset_paths={"broken": media},
                                  destination=out))
    assert result.produced_frames == 50
    assert not any("recorded end" in w for w in result.warnings)


def test_a_picture_source_shorter_than_its_own_header_is_refused_not_frozen(tmp_path):
    """The same defence as sound, for the failure that looks like an ending.

    Measured for ``make_truncated_identity_offset``: 45% of the file's bytes are
    gone, yet it still claims to run to 13/2 s while only 54 pictures decode, the
    last ending at 233/50 s. Rendering the span the header claims would show one
    frozen picture for the last 1.8 s of the export; that is refused, and nothing
    is written.
    """
    media = make_truncated_identity_offset(tmp_path, count=100)
    source = source_from_probe(media, 0, "cut")
    assert source.end == F(13, 2) and source.conservative_end
    project = Project(output=OutputSettings(64, 48, FPS, SR, "stereo"))
    project = RegisterSourceCommand(source).apply(project)
    project = project.with_tracks((Track("v", TrackKind.VIDEO, "V1"),))
    project = AddClipCommand(
        "cut", source.stream_index, "v", F(0), source_in=source.origin,
        source_out=source.end, clip_id="v",
    ).apply(project)
    out = tmp_path / "frozen.mp4"
    with pytest.raises(RenderError, match="hold that one picture") as refused:
        render(RenderRequest(project=project, asset_paths={"cut": media}, destination=out))
    # the last sample this range asks for is 323/50 s and the last picture that
    # really decodes runs to 233/50 s: 90/50 s of held frame, far over one frame
    assert "stops 1.800s before the 6.46s" in str(refused.value), str(refused.value)
    assert not out.exists()
    assert not list(tmp_path.glob("*.partial.*"))


def test_a_stale_stream_record_from_an_older_project_is_still_refused(tmp_path):
    """Old projects keep their old numbers; the render does not trust them.

    The record below is what a project saved before this task holds for the offset
    fixture: the container's start plus its duration, 9 s, marked conservative.
    The media really ends at 13/2 s, so a clip that reads to 9 s would spend 2.46 s
    of the export on one frozen frame. It is refused before anything is encoded -
    re-probing the asset is the remedy, not a precondition for being caught.
    """
    media = make_identity_offset(tmp_path, count=100)
    project = Project(output=OutputSettings(64, 48, FPS, SR, "stereo"))
    project = RegisterSourceCommand(
        StreamSource("off", 0, TrackKind.VIDEO, F(5, 2), F(9), conservative_end=True,
                     frame_rate=FPS, width=64, height=48, input_start=F(5, 2),
                     stream_position=0)
    ).apply(project)
    project = project.with_tracks((Track("v", TrackKind.VIDEO, "V1"),))
    project = AddClipCommand("off", 0, "v", F(0), source_in=F(5, 2), source_out=F(9),
                             clip_id="v").apply(project)
    out = tmp_path / "stale.mp4"
    with pytest.raises(RenderError, match="hold that one picture") as refused:
        render(RenderRequest(project=project, asset_paths={"off": media}, destination=out))
    assert "recorded as running to 9.0s" in str(refused.value), str(refused.value)
    assert not out.exists()


def test_an_honest_picture_end_costs_a_probe_and_no_warning(tmp_path):
    """A clip that ends where the material does is measured, passes, and stays quiet.

    The check is not a tax that turns every ending into a warning: with the
    corrected bound (13/2 s, measured from the same file) the pictures are all
    there, so the render goes through with its 75 frames and says nothing about
    the tail. The plan's own conservative-end warning is expected and untouched.
    """
    media = make_identity_offset(tmp_path, count=100)
    source = source_from_probe(media, 0, "off")
    assert source.end == F(13, 2)
    project = Project(output=OutputSettings(64, 48, FPS, SR, "stereo"))
    project = RegisterSourceCommand(source).apply(project)
    project = project.with_tracks((Track("v", TrackKind.VIDEO, "V1"),))
    project = AddClipCommand("off", 0, "v", F(0), source_in=F(7, 2), source_out=F(13, 2),
                             clip_id="v").apply(project)
    out = tmp_path / "honest.mp4"
    result = render(RenderRequest(project=project, asset_paths={"off": media},
                                  destination=out))
    assert result.produced_frames == 75, result.produced_frames
    assert not any(
        "runs out of pictures" in w or "could not be measured" in w
        for w in result.warnings
    ), result.warnings


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


# ------------------------------------- picture identity through the exporter path


def identity_source(tmp_path: Path, count: int = 150) -> Path:
    """A fixture whose every picture carries its own number, so identity is measurable."""
    return make_identity_video(tmp_path, name="identity.mkv", count=count)


def exported_identities(path: Path, width: int, height: int) -> list[int | None]:
    """Every picture of a lossless export, as the number it carries.

    ``None`` where the composition wrote black: every byte zero, which a fixture
    picture never is.
    """
    raw = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:v:0",
         "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, timeout=600,
    )
    assert raw.returncode == 0, raw.stderr.decode()[:400]
    stride = width * height * 3
    out: list[int | None] = []
    for i in range(len(raw.stdout) // stride):
        frame = raw.stdout[i * stride:(i + 1) * stride]
        out.append(None if all(b == 0 for b in frame) else identity_at(frame, width, height))
    return out


def identity_composition(source: Path) -> tuple[Project, dict[str, Path]]:
    """``[1/3, 6)`` of the identity stream at 25/24: 136 output frames.

    Hand-worked: 6 - 1/3 = 17/3 source seconds, each output second showing 24/25
    source seconds, so the output is (17/3) * 24/25 = 136/25 s = 136 frames at 25 fps.
    """
    project = Project(output=OutputSettings(64, 48, FPS, SR, "stereo"))
    project = RegisterSourceCommand(source_from_probe(source, 0, "src")).apply(project)
    project = project.with_tracks((Track("v1", TrackKind.VIDEO, "V1"),))
    project = AddClipCommand(
        asset_id="src", stream_index=0, track_id="v1", output_start=F(0),
        source_in=F(1, 3), source_out=F(6), rate=F(25, 24), clip_id="clip",
    ).apply(project)
    return project, {"src": source}


def test_export_of_a_range_writes_the_pictures_the_whole_export_writes(tmp_path):
    """The preview/chunk path cannot promise pictures the export would not write.

    The same 136-frame composition exported whole, and exported as two ranges at
    output 68/25 s (frame 68's own instant, so both ranges are 68 frames), must
    agree frame by frame. Six of those frames are hand-worked separately: output
    frame ``j`` samples ``1/3 + j/24`` s of the source, and the picture is the last
    one whose own timestamp is at or before it - so for j = 15 the sampling time is
    23/24 = 0.958333 s, the real timestamps either side are 23/25 = 0.92 s and
    24/25 = 0.96 s, and the picture is 23. The same arithmetic gives 48, 73, 98,
    123 and 148 for j = 39, 63, 87, 111 and 135, and the distance from the sampling
    time to the nearer timestamp is 1/600 s: five times finer than this fixture's
    own millisecond tick, which is exactly why a tempo quantised to that tick used
    to choose 24, 49, 74, 99, 124 and 149 instead - and why the two paths used to
    disagree at 87, 111 and 135.
    """
    source = identity_source(tmp_path)
    project, paths = identity_composition(source)

    def export_ids(start: F, end: F, name: str) -> list[int | None]:
        out = tmp_path / name
        render(
            RenderRequest(project=project, asset_paths=paths, destination=out,
                          out_start=start, out_end=end, preset=LOSSLESS_INTERMEDIATE)
        )
        return exported_identities(out, 64, 48)

    whole = export_ids(F(0), F(136, 25), "whole.mkv")
    first = export_ids(F(0), F(68, 25), "first.mkv")
    second = export_ids(F(68, 25), F(136, 25), "second.mkv")
    assert len(whole) == 136 and len(first) == len(second) == 68
    assert first + second == whole, "a range writes different pictures from the whole"
    for index, want in ((15, 23), (39, 48), (63, 73), (87, 98), (111, 123), (135, 148)):
        assert whole[index] == want, f"whole render: output {index}"
    for index, want in ((15, 23), (39, 48), (63, 73)):
        assert first[index] == want, f"first range: output {index}"
    for index, want in ((87, 98), (111, 123), (135, 148)):
        assert second[index - 68] == want, f"second range: output {index}"
