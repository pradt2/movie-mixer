"""Preview planning and cache reuse: same composition as export, cheaper picture."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from core_helpers import add_clip, make_base
from media_fixtures import HAS_FFMPEG, make_colour_steps, make_tone
from video_editor.core.commands import SetTrackMutedCommand
from video_editor.core.model import OutputSettings, Project
from video_editor.render.cache import PreviewCache
from video_editor.render.exporter import render
from video_editor.render.graph import MP4_H264_AAC, PREVIEW_H264_FAST
from video_editor.render.plan import Quantisation
from video_editor.render.preview import (
    DEFAULT_PREVIEW_SECONDS,
    MAX_PREVIEW_SECONDS,
    PreviewError,
    playback_offset,
    prepare_preview,
    preview_range,
)

F = Fraction
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")


def stream_properties(path: Path) -> dict:
    proc = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    video = next(s for s in payload["streams"] if s["codec_type"] == "video")
    audio = next(s for s in payload["streams"] if s["codec_type"] == "audio")
    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "vcodec": video["codec_name"],
        "acodec": audio["codec_name"],
        "r_frame_rate": video["r_frame_rate"],
        "duration": Fraction(payload["format"]["duration"]),
    }


# ---------------------------------------------------------------- range choice


def test_default_window_is_centred_on_the_playhead_and_frame_snapped():
    project = add_clip(make_base(), "c1", "vt", F(0), F(20))
    window = preview_range(F(10), project)
    assert window == type(window)(F(5), F(15))
    assert window.duration == DEFAULT_PREVIEW_SECONDS


def test_window_is_clamped_to_the_composition_at_both_ends():
    project = add_clip(make_base(), "c1", "vt", F(0), F(4))
    # a 10 s window around a 4 s composition *is* the whole composition: the range
    # never runs past the output end, and it is not silently shortened elsewhere
    whole = preview_range(F(0), project)
    assert (whole.start, whole.end) == (F(0), F(4))
    # near the end the window is walked back rather than truncated to one frame
    end = preview_range(F(4), project, duration=F(1))
    assert end.end == F(4) and end.duration == F(1)
    assert preview_range(F(3, 10), project, duration=F(1)).duration == F(1)


def test_snapshotted_boundaries_are_whole_output_frames():
    project = replace(
        add_clip(make_base(), "c1", "vt", F(0), F(20)),
        output=OutputSettings(640, 360, F(1000, 1001), 48000, "stereo"),
    )
    window = preview_range(F(10), project, duration=F(3))
    quant = Quantisation(F(1000, 1001), 48000)
    for t in (window.start, window.end):
        assert quant.frame_index(t) * F(1001, 1000) == t, t
    frames = quant.frame_index(window.end) - quant.frame_index(window.start)
    assert frames == quant.frame_count(F(3))


def test_a_sub_frame_composition_previews_exactly_one_frame():
    """Even a quarter-frame composition gets a real one-frame range, not a lie."""
    project = add_clip(make_base(), "c1", "vt", F(0), F(1, 100))
    clip = project.clip("c1")
    # place a sub-frame clip directly: commands may round, the planner must not lie
    project = replace(
        project,
        clips=(replace(clip, source_out=clip.source_in + F(1, 100), output_start=F(0)),),
    )
    assert project.output_end() == F(1, 100)  # a quarter frame at 25 fps
    window = preview_range(F(0), project)
    quant = Quantisation(F(25), 48000)
    assert quant.frame_count(window.duration) == 1
    assert window.start == F(0) and window.end == F(1, 25)


def test_unplayable_requests_are_refused_with_a_reason():
    project = add_clip(make_base(), "c1", "vt", F(0), F(20))
    with pytest.raises(PreviewError, match="empty"):
        preview_range(F(0), Project())
    with pytest.raises(PreviewError, match="must be positive"):
        preview_range(F(1), project, duration=F(0))
    with pytest.raises(PreviewError, match="exceeds"):
        preview_range(F(1), project, duration=MAX_PREVIEW_SECONDS * 2)
    with pytest.raises(PreviewError, match="non-negative"):
        preview_range(F(-1), project)


def test_gaps_still_count_towards_the_preview_length():
    """A composition whose picture stops early still has output duration."""
    project = add_clip(make_base(), "c1", "vt", F(0), F(1))
    assert project.output_end() == F(1)
    window = preview_range(F(0), project)
    assert window.end == F(1)  # the range cannot be longer than the output


# ---------------------------------------------------------------- cache reuse


def project_for(steps: Path, tone: Path) -> Project:
    from test_render_export import composition

    project, _paths = composition(steps, tone, tone)
    return project


def test_playback_offset_maps_output_time_into_the_rendered_range():
    # exact rationals, no float drift: a 24000/1001-style grid stays exact
    assert playback_offset(F(1), F(5, 2), F(4)) == F(3, 2)
    assert playback_offset(F(24, 25), F(100, 24), F(6)) == F(100, 24) - F(24, 25)
    assert playback_offset(F(1), F(1), F(4)) == F(0)
    assert playback_offset(F(1), F(4), F(4)) is None  # the end is half-open
    assert playback_offset(F(1), F(1, 2), F(4)) is None
    assert playback_offset(F(1), F(9), F(4)) is None


def test_first_preview_renders_then_the_same_request_is_reused(tmp_path):
    steps = make_colour_steps(tmp_path)
    tone = make_tone(tmp_path, "t.wav", 660)
    project = project_for(steps, tone)
    paths = {"steps": steps, "t660": tone, "t990": tone}
    cache = PreviewCache(tmp_path / "cache", max_bytes=50_000_000, max_entries=6)

    job = prepare_preview(project, paths, F(0), F(2), cache)
    assert not job.reused and job.key
    result = render(job.request)
    assert result.path == job.request.destination
    assert job.request.destination.is_file()

    again = prepare_preview(project, paths, F(0), F(2), cache)
    assert again.reused
    assert again.request.destination == job.request.destination
    assert len(cache.entries()) == 1


def test_an_edit_produces_a_different_preview_rather_than_reusing_the_old_one(tmp_path):
    steps = make_colour_steps(tmp_path)
    tone = make_tone(tmp_path, "t.wav", 660)
    project = project_for(steps, tone)
    paths = {"steps": steps, "t660": tone, "t990": tone}
    cache = PreviewCache(tmp_path / "cache", max_bytes=50_000_000, max_entries=6)

    before = prepare_preview(project, paths, F(0), F(2), cache)
    render(before.request)
    muted = SetTrackMutedCommand("a1", True).apply(project)
    after = prepare_preview(muted, paths, F(0), F(2), cache)
    assert not after.reused and after.key != before.key
    render(after.request)
    assert len(cache.entries()) == 2  # stale one is still bounded by the budget


def test_preview_uses_the_preview_preset_at_the_reduced_size(tmp_path):
    steps = make_colour_steps(tmp_path)
    tone = make_tone(tmp_path, "t.wav", 660)
    project = project_for(steps, tone)
    paths = {"steps": steps, "t660": tone, "t990": tone}
    cache = PreviewCache(tmp_path / "cache")
    job = prepare_preview(project, paths, F(0), F(2), cache, geometry_scale=F(1, 4))
    render(job.request)
    props = stream_properties(job.request.destination)
    assert (props["width"], props["height"]) == (32, 24)  # 128x96 at quarter size
    assert props["vcodec"] == "h264" and props["acodec"] == "aac"
    assert props["r_frame_rate"] == "25/1"
    assert abs(props["duration"] - F(2)) < F(1, 25) + F(2048, 48000) + F(1, 100)
    assert job.plan.width == 32 and job.plan.height == 24
    assert job.plan.total_frames == 50  # timing untouched by the picture scale


def test_preview_and_export_of_the_same_range_agree_on_duration(tmp_path):
    steps = make_colour_steps(tmp_path)
    tone = make_tone(tmp_path, "t.wav", 660)
    project = project_for(steps, tone)
    paths = {"steps": steps, "t660": tone, "t990": tone}
    cache = PreviewCache(tmp_path / "cache")
    job = prepare_preview(project, paths, F(1), F(4), cache, geometry_scale=F(1, 2))
    preview_result = render(job.request)

    delivery = tmp_path / "export.mp4"
    full = render(
        replace(job.request, destination=delivery, preset=MP4_H264_AAC, geometry_scale=F(1),
                cache=None, overwrite=True)
    )
    assert preview_result.duration == full.duration
    assert stream_properties(job.request.destination)["duration"] == pytest.approx(
        float(full.produced_duration), abs=0.02
    )


def test_a_preview_spanning_cuts_black_and_two_tones_matches_the_export(tmp_path):
    """The acceptance rule: sampled preview picture/sound == exported equivalent.

    Range 1-7 s spans the rate-2 overlay's cut (magenta next to blue on
    neighbouring frames), the end of the picture (explicit black), the 660 Hz
    tone, the half-gain 990 Hz contribution, and the silent tail - so both media
    are measured where the composition actually changes.
    """
    from test_render_export import (
        composition,
        frame_at,
        goertzel,
        nearest,
        pcm,
        reference_colours,
        rms,
    )

    steps = make_colour_steps(tmp_path)
    t660 = make_tone(tmp_path, "a.wav", 660)
    t990 = make_tone(tmp_path, "b.wav", 990)
    project, paths = composition(steps, t660, t990)
    cache = PreviewCache(tmp_path / "cache")
    job = prepare_preview(project, paths, F(1), F(7), cache, geometry_scale=F(1, 4))
    preview = render(job.request).path
    delivery = tmp_path / "export.mp4"
    render(
        replace(job.request, destination=delivery, preset=MP4_H264_AAC,
                geometry_scale=F(1), cache=None, overwrite=True)
    )

    assert stream_properties(preview)["width"] == 32  # only the picture is cheaper

    reference = reference_colours(steps)
    # frame i of both files is the same output frame: 1 s + i/25
    expected = {12: "blue", 24: "magenta", 25: "blue", 62: "magenta", 112: "cyan"}
    for index, colour in expected.items():
        assert nearest(reference, frame_at(preview, index)) == colour, index
        assert nearest(reference, frame_at(delivery, index)) == colour, index
    for index in (137, 149):  # past every clip: explicit black in both
        assert sum(frame_at(preview, index)) < 40, index
        assert sum(frame_at(delivery, index)) < 40, index

    def mix(path):
        early = pcm(path, F(1, 2), F(1, 2))  # output 1.5-2.0 s: the 660 Hz tone only
        late = pcm(path, F(7, 2), F(1, 2))  # output 4.5-5.0 s: both contributions
        tail = pcm(path, F(11, 2), F(1, 2))  # output 6.5-7.0 s: nothing is playing
        return (
            goertzel(early, 660), goertzel(early, 990),
            goertzel(late, 660), goertzel(late, 990), rms(tail),
        )

    heard_preview, heard_export = mix(preview), mix(delivery)
    for name, a, b in zip(("660 early", "990 early", "660 late", "990 late", "tail"),
                          heard_preview, heard_export, strict=True):
        assert abs(a - b) <= 0.15 * max(a, b) + 0.005, f"{name}: {a} vs {b}"

    p660_early, p990_early, p660_late, p990_late, tail = heard_preview
    assert p660_early > 0.02 and p990_early < 0.2 * p660_early  # not started yet
    assert p660_late > 0.02 and p990_late > 0.02  # both tracks contribute
    assert 0.2 < p990_late / p660_late < 0.9  # the half-gain contribution stays half
    assert tail < 0.01  # a gap is silent, not a hold of the last sound


def test_a_chunked_preview_covers_the_whole_range(tmp_path):
    steps = make_colour_steps(tmp_path)
    tone = make_tone(tmp_path, "t.wav", 660)
    project = project_for(steps, tone)
    paths = {"steps": steps, "t660": tone, "t990": tone}
    cache = PreviewCache(tmp_path / "cache")
    job = prepare_preview(project, paths, F(0), F(2), cache)
    # force the chunked path: same planner, same range, many small graphs
    result = render(replace(job.request, chunk_item_limit=1))
    assert result.chunks > 1
    assert abs(result.produced_duration - F(2)) < F(1, 25) + F(2048, 48000) + F(1, 100)


def test_cache_files_live_in_the_cache_and_never_touch_a_user_export(tmp_path):
    steps = make_colour_steps(tmp_path)
    tone = make_tone(tmp_path, "t.wav", 660)
    project = project_for(steps, tone)
    paths = {"steps": steps, "t660": tone, "t990": tone}
    user_file = tmp_path / "my-export.mp4"
    user_file.write_bytes(b"precious")
    cache = PreviewCache(tmp_path / "cache", max_bytes=5_000, max_entries=6)
    job = prepare_preview(project, paths, F(0), F(2), cache)
    render(job.request)
    assert job.request.destination.parent == cache.root
    # the preview is bigger than the budget, so the cache drops it again straight
    # away instead of growing: budgets are enforced, not advisory
    assert cache.total_bytes <= 5_000
    assert list(cache.entries()) == []
    assert user_file.read_bytes() == b"precious"


def test_a_preview_request_can_be_planned_before_it_is_rendered(tmp_path):
    """The UI needs plan numbers (frames, warnings) without rendering anything."""
    steps = make_colour_steps(tmp_path)
    tone = make_tone(tmp_path, "t.wav", 660)
    project = project_for(steps, tone)
    paths = {"steps": steps, "t660": tone, "t990": tone}
    cache = PreviewCache(tmp_path / "cache")
    job = prepare_preview(project, paths, F(1), F(3), cache, preset=PREVIEW_H264_FAST)
    assert job.plan.output_start == F(1) and job.plan.output_end == F(3)
    assert job.plan.duration == F(2)
    assert any(isinstance(w, str) for w in job.plan.warnings) or job.plan.warnings == ()
    assert not job.request.destination.exists()
