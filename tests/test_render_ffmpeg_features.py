"""FFmpeg feature coverage: offsets, VFR, odd boundaries, stream selection.

These are the real-world format features AGENTS.md requires to be either
rendered correctly or refused clearly. Sources are taken through the *real*
probe path (``probe_file`` -> ``StreamSource``) and expectations are derived
from the fixtures themselves, so the tests measure behaviour instead of baking
in assumptions about how FFmpeg wrote the file.
"""

from __future__ import annotations

import itertools
import json
import subprocess
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from media_fixtures import (
    HAS_FFMPEG,
    make_colour_steps,
    make_mixed,
    make_stream_offset,
    make_video_only,
    make_whole_asset_offset,
)
from test_render_export import (
    FFPROBE,
    SR,
    frame_at,
    goertzel,
    nearest,
    pcm,
    reference_colours,
    rms,
    run,
)
from video_editor.core.commands import AddClipCommand, RegisterSourceCommand
from video_editor.core.model import OutputSettings, Project, StreamSource, Track, TrackKind
from video_editor.media.ffprobe import probe_file
from video_editor.render.exporter import RenderRequest, probe_duration, render
from video_editor.render.graph import RenderConfigError, build_job
from video_editor.render.plan import build_plan

F = Fraction
FPS = F(25)
pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")


# ------------------------------------------------------- probe-driven sources


def sources_from(asset_id: str, path: Path) -> list[StreamSource]:
    """The same translation the UI performs when an asset is placed."""
    result = probe_file(path)
    assert result.ok, result.error
    metadata = result.metadata
    assert metadata is not None
    out: list[StreamSource] = []
    for stream in metadata.streams:
        if not stream.bounds_known:
            continue
        kind = TrackKind.VIDEO if stream.kind.value == "video" else TrackKind.AUDIO
        out.append(
            StreamSource(
                asset_id=asset_id,
                stream_index=stream.stream_index,
                kind=kind,
                origin=stream.origin,
                end=stream.end_time,
                conservative_end=stream.bounds_are_conservative,
                frame_rate=stream.frame_rate,
                sample_rate=stream.sample_rate,
                channel_layout=stream.channel_layout,
                width=stream.width,
                height=stream.height,
                stream_position=metadata.position_of(stream.stream_index),
                input_start=metadata.container_start or F(0),
            )
        )
    return out


def project_for(sources: list[StreamSource], width: int, height: int) -> Project:
    p = Project(output=OutputSettings(width, height, FPS, SR, "stereo"))
    for source in sources:
        p = RegisterSourceCommand(source).apply(p)
    return p.with_tracks(
        (Track("v", TrackKind.VIDEO, "V1"), Track("a", TrackKind.AUDIO, "A1"))
    )


def pick(sources: list[StreamSource], kind: TrackKind, position: int = 0) -> StreamSource:
    return next(s for s in sources if s.kind is kind and s.stream_position == position)


# ----------------------------------------------------------- stream origins


def test_container_offset_is_removed_not_shifted(tmp_path):
    """A presentation whose timeline starts at 2.5 s renders from output 0."""
    media = make_whole_asset_offset(tmp_path)
    sources = sources_from("off", media)
    video = pick(sources, TrackKind.VIDEO)
    audio = pick(sources, TrackKind.AUDIO)
    assert video.origin > F(2), "fixture should carry a nonzero presentation start"

    p = project_for(sources, 128, 96)
    span = F(2)
    p = AddClipCommand("off", video.stream_index, "v", F(0), source_in=video.origin,
                       source_out=video.origin + span, clip_id="v").apply(p)
    p = AddClipCommand("off", audio.stream_index, "a", F(0), source_in=audio.origin,
                       source_out=audio.origin + span, clip_id="a").apply(p)
    out = tmp_path / "origin.mp4"
    result = render(RenderRequest(project=p, asset_paths={"off": media}, destination=out))
    assert result.duration == span
    assert abs(probe_duration(FFPROBE, out) - span) <= F(1, 25) + F(2048, SR) + F(1, 10)

    # the first output frame is the source's first frame, not a black gap
    reference = frame_at(media, 0)  # first frame the source actually contains
    exported = frame_at(out, 0)
    assert sum((a - b) ** 2 for a, b in zip(reference, exported, strict=True)) < 4000

    # and its sound starts at output 0 as well, and continues to the end
    assert goertzel(pcm(out, F(1, 5), F(1, 5)), 220) > 0.02
    assert goertzel(pcm(out, span - F(4, 10), F(1, 5)), 220) > 0.02


def test_stream_offset_audio_is_sought_to_its_own_origin(tmp_path):
    """Audio beginning 1 s into the asset is read from there, not from 0."""
    media = make_stream_offset(tmp_path)
    sources = sources_from("so", media)
    video = pick(sources, TrackKind.VIDEO)
    audio = pick(sources, TrackKind.AUDIO)
    assert audio.origin > F(1, 2), "fixture should carry an offset audio stream"

    p = project_for(sources, 128, 96)
    p = AddClipCommand("so", video.stream_index, "v", F(0), source_in=video.origin,
                       source_out=video.origin + F(3), clip_id="v").apply(p)
    # the clip's material starts at the stream's own origin, placed at output 1 s
    p = AddClipCommand("so", audio.stream_index, "a", F(1), source_in=audio.origin,
                       source_out=audio.origin + F(2), clip_id="a").apply(p)
    out = tmp_path / "stream_offset.mp4"
    render(RenderRequest(project=p, asset_paths={"so": media}, destination=out, out_end=F(3)))

    assert goertzel(pcm(out, F(1, 10), F(2, 10)), 660) < 0.02, "gap before the clip is silent"
    assert goertzel(pcm(out, F(11, 10), F(2, 10)), 660) > 0.02, "audio follows its origin"
    assert rms(pcm(out, F(4, 10), F(2, 10))) < 0.02, "no audio leaked into the gap"
    # picture is unaffected by the audio stream's offset
    assert goertzel(pcm(out, F(21, 10), F(2, 10)), 660) > 0.02


# --------------------------------------------------------------- VFR / jitter


def make_jittered_video(tmp: Path, name: str = "jitter.mkv") -> Path:
    """3 s of red video with frames dropped at irregular intervals (VFR-like)."""
    out = tmp / name
    drop = {20, 21, 45, 70, 71, 72}
    # commas inside a filter expression must be escaped for the filter parser
    condition = "+".join(f"eq(n\\, {n})" for n in sorted(drop))
    run([
        "-f", "lavfi", "-i", "color=c=red:s=128x96:r=25:d=3",
        "-vf", f"select='not({condition})'", "-fps_mode", "vfr",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
    ])
    return out


def frame_pts(path: Path) -> list[Fraction]:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    frames = json.loads(out)["frames"]
    values = [Fraction(f["pts_time"]) for f in frames if f.get("pts_time") not in (None, "N/A")]
    return sorted(values)


def test_irregular_source_pts_use_presentation_times(tmp_path):
    """Frames are addressed by PTS; the output is rebased to constant rate."""
    media = make_jittered_video(tmp_path)
    pts = frame_pts(media)
    assert len(pts) >= 20
    gaps = [b - a for a, b in itertools.pairwise(pts)]
    assert max(gaps) > F(1, 10), "fixture should contain an irregular PTS gap"
    span = pts[-1] - pts[0] + F(1, 25)

    p = project_for(sources_from("jitter", media), 128, 96)
    source = pick(sources_from("jitter", media), TrackKind.VIDEO)
    p = AddClipCommand("jitter", source.stream_index, "v", F(0), source_in=source.origin,
                       source_out=source.origin + span, clip_id="v").apply(p)
    out = tmp_path / "jitter.mp4"
    result = render(RenderRequest(project=p, asset_paths={"jitter": media}, destination=out))
    assert result.duration == span
    produced = probe_duration(FFPROBE, out)
    assert abs(produced - span) <= F(1, 25) + F(1, 10)
    assert output_frame_rate(out) == FPS, "preview output must be constant rate"
    assert sum(frame_at(out, 0)) > 30
    assert sum(frame_at(out, int(span * FPS) - 1)) > 30


def output_frame_rate(path: Path) -> Fraction:
    streams = json.loads(
        subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=r_frame_rate", "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout
    )["streams"]
    return Fraction(streams[0]["r_frame_rate"])


# ---------------------------------------------------- odd boundaries & rates


def test_non_frame_aligned_boundaries_land_on_frames(tmp_path):
    """A cut at 30/1000 s is quantised once, by global frame index, no gap."""
    steps = make_colour_steps(tmp_path)
    reference = reference_colours(steps)
    sources = sources_from("steps", steps)
    p = project_for(sources, 128, 96)
    video = pick(sources, TrackKind.VIDEO)
    cut = F(30, 1000)  # 0.75 frames at 25 fps -> rounds to frame 1
    p = AddClipCommand("steps", video.stream_index, "v", F(0), source_in=video.origin,
                       source_out=cut, clip_id="a").apply(p)
    p = AddClipCommand("steps", video.stream_index, "v", cut, source_in=cut,
                       source_out=F(2), clip_id="b").apply(p)
    plan = build_plan(p, F(0), F(2))
    assert plan.video_items[0].end_frame == plan.video_items[1].start_frame
    assert plan.total_frames == 50

    out = tmp_path / "odd_boundary.mp4"
    render(RenderRequest(project=p, asset_paths={"steps": steps}, destination=out, out_end=F(2)))
    # clip a owns only frame 0; clip b runs frames 1..49 over source 0.04..2.0
    assert nearest(reference, frame_at(out, 0)) == "red", "clip a owns frame 0"
    assert nearest(reference, frame_at(out, 24)) == "red", "source 0.96"
    assert nearest(reference, frame_at(out, 25)) == "green", "source 1.00"
    assert nearest(reference, frame_at(out, 49)) == "green", "last frame belongs to clip b"


def test_source_fractional_rate_into_integer_output(tmp_path):
    """24000/1001 source material rendered at 25 fps output settings."""
    media = make_video_only(tmp_path)
    sources = sources_from("ntsc", media)
    video = pick(sources, TrackKind.VIDEO)
    assert video.frame_rate == F(24000, 1001)

    p = Project(output=OutputSettings(160, 96, FPS, SR, "stereo"))
    p = RegisterSourceCommand(video).apply(p)
    p = p.with_tracks((Track("v", TrackKind.VIDEO, "V1"),))
    p = AddClipCommand("ntsc", video.stream_index, "v", F(0), source_in=video.origin,
                       source_out=video.origin + F(4), clip_id="v").apply(p)
    out = tmp_path / "ntsc.mp4"
    result = render(RenderRequest(project=p, asset_paths={"ntsc": media}, destination=out))
    assert result.duration == F(4)
    assert abs(probe_duration(FFPROBE, out) - F(4)) <= F(1, 25) + F(1, 10)
    assert output_frame_rate(out) == FPS
    assert count_frames(out) in range(98, 102)  # 4 s at 25 fps, +/- rounding
    reference = frame_at(media, 25)  # the fixture's own green
    exported = frame_at(out, 50)
    assert sum((a - b) ** 2 for a, b in zip(reference, exported, strict=True)) < 4000


def count_frames(path: Path) -> int:
    streams = json.loads(
        subprocess.run(
            [FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
             "stream=nb_read_frames", "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout
    )["streams"]
    return int(streams[0]["nb_read_frames"])


# ------------------------------------------------------------ stream selection


def test_independent_audio_stream_selection_and_resampling(tmp_path):
    """The second (French, 44.1 kHz) stream only, resampled to 48 kHz output."""
    media = make_mixed(tmp_path)
    sources = sources_from("mixed", media)
    audio_streams = [s for s in sources if s.kind is TrackKind.AUDIO]
    assert len(audio_streams) == 2
    second = pick(sources, TrackKind.AUDIO, position=1)
    # container index is not the FFmpeg position: the domain model keeps both
    assert second.stream_index != second.stream_position

    p = Project(output=OutputSettings(128, 96, FPS, SR, "stereo"))
    p = RegisterSourceCommand(second).apply(p)
    p = p.with_tracks((Track("a", TrackKind.AUDIO, "A1"),))
    p = AddClipCommand("mixed", second.stream_index, "a", F(0), source_in=second.origin,
                       source_out=second.origin + F(4), clip_id="a").apply(p)
    out = tmp_path / "french.mp4"
    render(RenderRequest(project=p, asset_paths={"mixed": media}, destination=out))
    samples = pcm(out, F(1), F(1))
    assert goertzel(samples, 880) > 0.02, "selected stream must be audible"
    assert goertzel(samples, 440) < 0.2 * goertzel(samples, 880), "other stream must not leak"


def test_unaddressable_stream_is_reported_before_any_render(tmp_path):
    """A plan whose source lacks addressing data fails before FFmpeg starts."""
    steps = make_colour_steps(tmp_path)
    source = pick(sources_from("steps", steps), TrackKind.VIDEO)
    p = project_for([source], 128, 96)
    p = AddClipCommand("steps", source.stream_index, "v", F(0), source_in=source.origin,
                       source_out=source.origin + F(2), clip_id="v").apply(p)
    plan = build_plan(p, F(0), F(2))
    stripped = replace(source, stream_position=None)
    sources = {("steps", source.stream_index): stripped}
    with pytest.raises(RenderConfigError, match="no recorded stream position"):
        build_job(plan, {"steps": steps}, sources, tmp_path / "x.mp4", "ffmpeg")
