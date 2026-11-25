"""Audio slice extent, resampling and fades, measured on decoded PCM (TASK_11).

Every expectation here is measured on the *decoded output samples*, at both ends
of a contribution, because container duration cannot tell a full two seconds of
sound from one second of sound plus one second of silence. It also cannot tell a
promise the graph states from a promise FFmpeg's format negotiation happens to
keep, which is what the previous chain depended on (see
``tests/test_render_graph.py``).

Sources are lossless PCM (no encoder delay, no codec padding); the delivery path
(AAC) is measured separately with an explicit, documented tolerance.
"""

from __future__ import annotations

import math
import subprocess
from array import array
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from media_fixtures import HAS_FFMPEG
from video_editor.core.commands import (
    AddClipCommand,
    RegisterSourceCommand,
    SetFadesCommand,
    SetGainCommand,
)
from video_editor.core.model import OutputSettings, Project, StreamSource, Track, TrackKind
from video_editor.media.ffprobe import probe_file
from video_editor.render.graph import (
    LOSSLESS_INTERMEDIATE,
    CodecPreset,
    build_job,
)
from video_editor.render.plan import build_plan

F = Fraction
FPS = F(25)
#: The lossless intermediate again, but with 32-bit float sound: measured through
#: 16-bit samples every level sits on a 1/65536 grid, which is the same size as
#: the fade error being asked about, so the question would be unanswerable.
FLOAT_PCM = replace(LOSSLESS_INTERMEDIATE, audio_codec="pcm_f32le")
# three 2-second blocks, each a distinct tone: "before", "inside", "after" the
# interval a clip selects, so both the extent and any leak are measurable
SECTIONS = (330, 660, 990)
SECTION_SECONDS = F(2)
TOTAL_SECONDS = SECTION_SECONDS * len(SECTIONS)

#: How far a chunked render may deviate from a single one. Each chunk rebuilds a
#: clip from that clip's own start and cuts the range afterwards, so for lossless
#: PCM sources the joined samples are identical: the measured worst difference at
#: rates 1, 25/24 and 24/25 is exactly 0. A lossy source can differ at a seek
#: point, which is why the *documented* promise in docs/architecture.md is a
#: bounded tolerance rather than bit-exactness.
CHUNK_JOIN_EXACT = 0.0

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")


def run(args: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
                          capture_output=True, timeout=180)
    assert proc.returncode == 0, proc.stderr.decode()[:800]
    return proc


# ---------------------------------------------------------------- fixtures


def sectioned_source(tmp_path: Path, rate: int, name: str | None = None) -> Path:
    """Lossless PCM: 330 Hz, then 660 Hz, then 990 Hz, two seconds each."""
    out = tmp_path / (name or f"sections_{rate}.wav")
    args: list[str] = []
    for frequency in SECTIONS:
        args += ["-f", "lavfi",
                 "-i", f"sine=frequency={frequency}:duration=2:sample_rate={rate}"]
    args += [
        "-filter_complex",
        f"[0:a][1:a][2:a]concat=n=3:v=0:a=1,aresample={rate}[a]",
        "-map", "[a]", "-c:a", "pcm_s16le", str(out),
    ]
    run(args)
    return out


def audio_source(path: Path, asset_id: str = "src") -> StreamSource:
    """The stream record the UI would register after probing this file."""
    result = probe_file(path)
    assert result.ok, result.error
    metadata = result.metadata
    assert metadata is not None
    audio = next(stream for stream in metadata.streams if stream.kind.value == "audio")
    assert audio.bounds_known, "fixture must have reliable bounds"
    # the same translation the window performs when a stream is placed
    return StreamSource(
        asset_id=asset_id, stream_index=audio.stream_index, kind=TrackKind.AUDIO,
        origin=audio.origin, end=audio.end_time,
        conservative_end=audio.bounds_are_conservative, sample_rate=audio.sample_rate,
        channel_layout=audio.channel_layout,
        stream_position=metadata.position_of(audio.stream_index),
        input_start=metadata.container_start or F(0),
    )


def one_clip_project(
    source: StreamSource, out_rate: int, source_in: F, source_out: F, at: F,
    rate: F | None = None, layout: str = "mono",
) -> Project:
    """One audio clip, placed at ``at``, selecting [source_in, source_out)."""
    rate = F(1) if rate is None else rate
    project = Project(output=OutputSettings(64, 64, FPS, out_rate, layout))
    project = RegisterSourceCommand(source).apply(project)
    project = project.with_tracks((Track("a", TrackKind.AUDIO, "A1"),))
    return AddClipCommand(
        source.asset_id, source.stream_index, "a", at, source_in=source_in,
        source_out=source_out, rate=rate, clip_id="a",
    ).apply(project)


def render_exact(
    project: Project, paths: dict[str, Path], destination: Path,
    start: Fraction | None = None, end: Fraction | None = None,
    preset: CodecPreset = LOSSLESS_INTERMEDIATE,
) -> Path:
    """Render losslessly, so samples can be counted exactly.

    FFV1 + PCM has no encoder delay and no tail padding: what the graph produced
    is what the file holds, which is what makes a sample-exact assertion
    meaningful. The delivery path is covered by the AAC tests in
    ``tests/test_render_ffmpeg_features.py``. Pass ``FLOAT_PCM`` when the
    measurement must not be limited by the intermediate's 16-bit sample grid.
    """
    plan = build_plan(project, start if start is not None else F(0), end)
    sources = {(s.asset_id, s.stream_index): s for s in project.sources}
    job = build_job(plan, paths, sources, destination, "ffmpeg", preset=preset)
    proc = subprocess.run(job.args, capture_output=True, timeout=300)
    assert proc.returncode == 0, proc.stderr.decode()[:800]
    return destination


# ------------------------------------------------------------------ measuring


def audio_samples(path: Path, start_sample: int, count: int, rate: int) -> list[float]:
    """Mono float samples ``[start_sample, start_sample+count)`` of the output.

    Selected by sample index after decoding: an input ``-ss`` would land on the
    nearest seek point and measure the wrong window.
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn",
         "-af", f"atrim=start_sample={start_sample}:end_sample={start_sample + count},"
                f"asetpts=PTS-STARTPTS",
         "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"],
        capture_output=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr.decode()[:800]
    return list(array("f", proc.stdout))


def goertzel(samples: list[float], frequency: float, rate: int) -> float:
    """Normalised magnitude at one frequency (pure Python, tolerance-driven)."""
    if not samples:
        return 0.0
    omega = 2 * math.pi * frequency / rate
    coeff = 2 * math.cos(omega)
    s1 = s2 = 0.0
    for sample in samples:
        s0 = sample + coeff * s1 - s2
        s1, s2 = s0, s1
    return math.sqrt(max(0.0, s1 * s1 + s2 * s2 - coeff * s1 * s2)) / len(samples)


def rms(samples: list[float]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def tone_here(samples: list[float], rate: int) -> tuple[int, float]:
    """Which section tone this window holds, and how strongly."""
    scored = {frequency: goertzel(samples, float(frequency), rate) for frequency in SECTIONS}
    best = max(scored, key=lambda key: scored[key])
    return best, scored[best]


def window(path: Path, rate: int, start_sample: int, count: int) -> tuple[int, float, float]:
    """(dominant tone, its magnitude, level) for one output window."""
    samples = audio_samples(path, start_sample, count, rate)
    tone, magnitude = tone_here(samples, rate)
    return tone, magnitude, rms(samples)


# --------------------------------------------------------------------- extent


@pytest.mark.parametrize("source_rate", [44100, 48000, 96000])
@pytest.mark.parametrize("out_rate", [44100, 48000])
@pytest.mark.parametrize("rate", [F(1), F(25, 24)])
def test_a_selection_is_heard_throughout_its_whole_extent(
    tmp_path, source_rate, out_rate, rate
):
    """Every output sample of the clip must come from the selected source range.

    The previous chain wrote one sample-count cap and left it ambiguous *which*
    samples it counted — source-rate or output-rate — because it never stated
    where the sample-rate conversion happened. FFmpeg 8.1.3 happened to resample
    first for these PCM sources, so the extent was right (verified by rendering
    the same cases at ``6a09c8b``); this test locks the promise that the chunked
    exporter, the preview cache and every later task rely on, at the start,
    middle and end of the contribution rather than only in the middle.
    """
    media = sectioned_source(tmp_path, source_rate)
    source = audio_source(media)
    source_in, source_out = source.origin + F(2), source.origin + F(4)  # the 660 Hz block
    project = one_clip_project(source, out_rate, source_in, source_out, F(0), rate=rate)
    clip = project.clips[0]

    out = tmp_path / "exact.mkv"
    render_exact(project, {"src": media}, out)

    plan = build_plan(project)
    item = plan.audio_items[0]
    assert item.samples == int(clip.output_duration * out_rate)  # the plan promise

    quarter = max(1, item.samples // 4)
    for label, start in (
        ("first samples", 0),
        ("middle samples", item.samples // 2 - quarter // 2),
        ("last samples", item.samples - quarter),
    ):
        tone, magnitude, level = window(out, out_rate, start, quarter)
        assert tone == 660, f"{label}: expected the selected 660 Hz block, heard {tone} Hz"
        assert magnitude > 0.05, f"{label}: selected audio too quiet (magnitude {magnitude})"
        assert level > 0.05, f"{label}: no signal at all in {label} (rms {level})"


@pytest.mark.parametrize("source_rate", [44100, 96000])
def test_nothing_outside_the_selection_leaks_into_the_following_gap(
    tmp_path, source_rate
):
    """The material after ``source_out`` belongs to nobody; a gap stays silent.

    A contribution that reads past its own end does not just lengthen itself: it
    plays *over* the timeline after it, which is how the old graph behaved.
    """
    media = sectioned_source(tmp_path, source_rate)
    source = audio_source(media)
    source_in = source.origin + F(2)
    source_out = source.origin + F(3)  # half of the 660 Hz block
    project = one_clip_project(source, 48000, source_in, source_out, F(0))
    out = tmp_path / "gap.mkv"
    render_exact(project, {"src": media}, out)

    plan = build_plan(project)
    item = plan.audio_items[0]
    after = window(out, 48000, item.end_sample, 48000 // 2)  # the 0.5 s right after
    assert after[2] < 1e-3, f"material leaked past source_out: rms={after[2]:.6f} " \
                            f"(heard {after[0]} Hz)"
    before = window(out, 48000, item.end_sample + 48000 // 2, 48000 // 2)
    assert before[2] < 1e-3, f"leak continued: rms={before[2]:.6f}"


# ------------------------------------------------------- sample-accurate fades


def dc_source(tmp_path: Path, rate: int = 48000, seconds: int = 6) -> Path:
    """A constant signal of known value, so a fade's shape can be read per sample."""
    out = tmp_path / f"dc_{rate}.wav"
    run(["-f", "lavfi", "-i", f"aevalsrc=0.5:duration={seconds}:sample_rate={rate}",
         "-c:a", "pcm_f32le", str(out)])
    return out


def offset_source(tmp_path: Path, rate: int, offset: F) -> Path:
    """The sectioned tones on a timeline that starts at ``offset``, lossless."""
    base = sectioned_source(tmp_path, rate)
    out = tmp_path / f"offset_{rate}_{offset.numerator}_{offset.denominator}.mkv"
    run(["-itsoffset", str(float(offset)), "-i", str(base), "-c:a", "pcm_s24le", str(out)])
    return out


def decoded_sample_count(path: Path) -> int:
    """How many samples per channel the produced file really decodes to."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1",
         "-f", "f32le", "-"], capture_output=True, timeout=300)
    assert proc.returncode == 0, proc.stderr.decode()[:400]
    return len(array("f", proc.stdout))


def test_a_short_fade_is_a_straight_line_at_sample_resolution(tmp_path):
    """40 ms of fade-in is 1920 samples of ramp, not one step per filter frame.

    ``volume=…:eval=frame`` recomputed the level once per *filter frame* — a whole
    buffer of samples. Measured on the previous chain, a 40 ms fade of a constant
    signal came out as exactly two levels (0.0, then 0.5 from sample 4096), a
    worst per-sample error of 0.427 of full scale: the fade existed only as one
    step at a buffer boundary. The domain envelope (``core.model.envelope_gain``)
    says the k-th sample of a ramp of N is scaled by k/N, so that is what is
    asserted here, sample by sample.
    """
    media = dc_source(tmp_path)
    source = audio_source(media)
    project = one_clip_project(source, 48000, source.origin, source.origin + F(2), F(0))
    project = SetFadesCommand("a", F(2, 50), F(0)).apply(project)  # 40 ms in
    out = tmp_path / "fade_in.mkv"
    render_exact(project, {"src": media}, out, preset=FLOAT_PCM)

    item = build_plan(project).audio_items[0]
    assert item.fade_in_samples == 1920, "40 ms at 48 kHz is 1920 samples"
    head = audio_samples(out, 0, item.fade_in_samples + 240, 48000)
    worst = max(
        abs(sample - 0.5 * index / item.fade_in_samples)
        for index, sample in enumerate(head[: item.fade_in_samples])
    )
    assert worst < 1e-6, f"fade-in is not sample exact: worst error {worst:.8f}"
    assert head[0] == pytest.approx(0.0, abs=1e-9), "the clip's first sample is silent"
    assert head[item.fade_in_samples] == pytest.approx(0.5, abs=1e-6)


def test_a_fade_out_finishes_on_the_clips_last_sample(tmp_path):
    media = dc_source(tmp_path)
    source = audio_source(media)
    project = one_clip_project(source, 48000, source.origin, source.origin + F(2), F(0))
    project = SetFadesCommand("a", F(0), F(1, 50)).apply(project)  # 20 ms out
    out = tmp_path / "fade_out.mkv"
    render_exact(project, {"src": media}, out, preset=FLOAT_PCM)

    item = build_plan(project).audio_items[0]
    ramp, last = item.fade_out_samples, item.clip_samples
    assert ramp == 960
    samples = audio_samples(out, last - ramp - 1, ramp + 1, 48000)
    # samples[0] is the sample just before the ramp: still full level, because the
    # domain envelope only ramps once *remaining* is less than the fade length
    assert samples[0] == pytest.approx(0.5, abs=1e-6)
    worst = max(
        abs(sample - 0.5 * (ramp + 1 - index) / ramp)
        for index, sample in enumerate(samples[1:], start=1)
    )
    assert worst < 1e-6, f"fade-out is not sample exact: worst error {worst:.8f}"
    assert samples[-1] < 1e-3, "the clip's last sample must be at the bottom of the ramp"


@pytest.mark.parametrize("rate", [F(1), F(25, 24), F(24, 25)])
def test_a_range_cut_inside_a_fade_continues_the_ramp(tmp_path, rate):
    """Rendering two ranges and joining them must equal rendering one range.

    The chunked exporter and the preview cache both render sub-ranges, so a fade
    that restarted (or fell silent) at a range boundary would make a preview
    disagree with the export, and a long film's chunks disagree with each other.
    """
    media = dc_source(tmp_path)
    source = audio_source(media)
    project = one_clip_project(source, 48000, source.origin, source.origin + F(4), F(0),
                               rate=rate)
    project = SetFadesCommand("a", F(1), F(1)).apply(project)  # fades the range cuts
    plan = build_plan(project)
    total = plan.total_samples

    whole = tmp_path / "whole.mkv"
    render_exact(project, {"src": media}, whole)
    continuous = audio_samples(whole, 0, total, 48000)
    assert len(continuous) == total

    # a boundary at 0.4 s, in the middle of the 1 s fade-in
    split = F(2, 5)
    first, second = tmp_path / "first.mkv", tmp_path / "second.mkv"
    render_exact(project, {"src": media}, first, F(0), split)
    render_exact(project, {"src": media}, second, split, plan.output_end)
    joined = audio_samples(first, 0, build_plan(project, F(0), split).total_samples, 48000)
    joined += audio_samples(second, 0, build_plan(project, split, plan.output_end).total_samples,
                           48000)
    assert len(joined) == total, (
        f"the two ranges produced {len(joined)} samples for a {total} sample timeline"
    )
    worst = max(abs(a - b) for a, b in zip(continuous, joined, strict=True))
    assert worst <= CHUNK_JOIN_EXACT, (
        f"chunked audio differs from a single render by {worst:.8f} at rate {rate}"
    )


def test_a_retimed_clip_across_a_chunk_boundary_stays_within_tolerance(tmp_path):
    """Long retimed material is rendered in chunks; the joins must not add silence.

    ``atempo`` is stateful, so a chunk boundary in the middle of a retimed clip is
    the one place where sample-for-sample equality cannot be promised. The
    measured deviation is what the exporter documents, not an assumption.
    """
    media = sectioned_source(tmp_path, 96000)
    source = audio_source(media)
    project = one_clip_project(source, 48000, source.origin, source.origin + F(6), F(0),
                               rate=F(25, 24))
    plan = build_plan(project)
    whole = tmp_path / "whole.mkv"
    render_exact(project, {"src": media}, whole)
    continuous = audio_samples(whole, 0, plan.total_samples, 48000)

    boundary = F(1)  # well inside the retimed clip
    first, second = tmp_path / "first.mkv", tmp_path / "second.mkv"
    render_exact(project, {"src": media}, first, F(0), boundary)
    render_exact(project, {"src": media}, second, boundary, plan.output_end)
    first_plan = build_plan(project, F(0), boundary)
    second_plan = build_plan(project, boundary, plan.output_end)
    joined = audio_samples(first, 0, first_plan.total_samples, 48000)
    joined += audio_samples(second, 0, second_plan.total_samples, 48000)

    assert len(joined) == len(continuous), (
        f"chunking changed the sample count: {len(joined)} vs {len(continuous)}"
    )
    worst = max(abs(a - b) for a, b in zip(continuous, joined, strict=True))
    assert worst <= CHUNK_JOIN_EXACT, (
        f"chunked retime deviates by {worst:.8f}; document or fix"
    )
    # no silence anywhere: the whole thing is the 660 Hz block region
    for start in range(0, plan.total_samples - 4800, 4800):
        _tone, _magnitude, level = window(whole, 48000, start, 4800)
        assert level > 0.05, f"hole at {start / 48000:.3f}s in the retimed clip"


def test_staggered_clips_from_different_rates_mix_as_a_plain_sum(tmp_path):
    """The whole TASK_11 surface in one composition.

    Two tracks, source streams at 44.1 kHz and 96 kHz, a stream whose own
    timeline starts at 1 s, fractional placement, a retimed clip, a short fade
    and a half gain — and the checks are made at *both* edges of every
    contribution, on decoded samples.
    """
    a_media = sectioned_source(tmp_path, 44100)
    a = audio_source(a_media, "a")
    b_media = offset_source(tmp_path, 96000, F(1))
    b = audio_source(b_media, "b")
    assert b.origin == F(1), f"fixture needs a nonzero stream origin, got {b.origin}"

    project = Project(output=OutputSettings(64, 64, FPS, 48000, "stereo"))
    project = RegisterSourceCommand(a).apply(project)
    project = RegisterSourceCommand(b).apply(project)
    project = project.with_tracks(
        (Track("a", TrackKind.AUDIO, "A1"), Track("b", TrackKind.AUDIO, "B1"))
    )
    # A: starts at 1/3 s, retimed 25/24, 20 ms fade in, one fifth gain
    project = AddClipCommand(
        "a", a.stream_index, "a", F(1, 3), source_in=a.origin + F(1),
        source_out=a.origin + F(3), rate=F(25, 24), clip_id="a",
    ).apply(project)
    project = SetFadesCommand("a", F(1, 50), F(0)).apply(project)
    project = SetGainCommand("a", F(1, 5)).apply(project)
    # B: the 660 Hz block of a 96 kHz stream that itself starts at 1 s. The gains
    # are deliberately low: summed they stay under full scale, because a mix that
    # overloads is saturated by the sample format and no longer measures cleanly.
    project = AddClipCommand(
        "b", b.stream_index, "b", F(1), source_in=b.origin + F(2),
        source_out=b.origin + F(4), clip_id="b",
    ).apply(project)
    project = SetGainCommand("b", F(2, 5)).apply(project)

    plan = build_plan(project)
    out = tmp_path / "mixed.mkv"
    render_exact(project, {"a": a_media, "b": b_media}, out)
    assert decoded_sample_count(out) == plan.total_samples

    item_a = next(i for i in plan.audio_items if i.clip.id == "a")
    item_b = next(i for i in plan.audio_items if i.clip.id == "b")

    # before A: silence
    _tone, _mag, level = window(out, 48000, 0, 4800)
    assert level < 1e-3, f"something plays before the first clip: rms={level:.6f}"
    # A's first 20 ms: the fade, then the 330 Hz block at half the level
    _tone, _mag, fading = window(out, 48000, item_a.start_sample, 480)  # 10 ms
    _tone, _mag, steady = window(out, 48000, item_a.start_sample + 2400, 4800)
    assert fading < steady / 2, (
        f"A's 20 ms fade-in is not there: 10 ms rms {fading:.5f} vs steady {steady:.5f}"
    )
    tone_a, _mag_a, _level = window(out, 48000, item_a.start_sample + 4800, 4800)
    assert tone_a == 330, f"A should be hearing its 330 Hz block, heard {tone_a} Hz"
    # after A ends, only B's 660 Hz is left, and it is the full level
    _tone, mag_b, _level = window(out, 48000, item_b.end_sample - 4800, 4800)
    tone_after, mag_after, _level = window(out, 48000, item_a.end_sample + 240, 4800)
    assert tone_after == 660, f"B should carry on alone, heard {tone_after} Hz"
    assert mag_b > 0.01 and mag_after > 0.01, "B's contribution is too quiet to measure"
    # and after the last clip: silence, not the rest of either source
    _tone, _mag, tail = window(out, 48000, plan.total_samples, 4800)
    assert tail < 1e-3, f"material past the timeline's end: rms={tail:.6f}"

    # the mix is a plain sum and the clip gain is real: while both run, both
    # tones are present, and the one-fifth-gain clip sits at half the level of the
    # two-fifths one. The window is just after B starts, while A is still in its
    # 330 Hz block.
    overlap = audio_samples(out, item_b.start_sample + 4800, 4800, 48000)
    gained = goertzel(overlap, 330.0, 48000)
    full = goertzel(overlap, 660.0, 48000)
    assert gained > 0.005 and full > 0.005, "one of the two contributions is missing"
    ratio = gained / full
    assert 0.35 < ratio < 0.65, f"the half-gain clip contributes {ratio:.3f} of the other"
