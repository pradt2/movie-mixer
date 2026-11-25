"""FFmpeg job construction tests (no rendering: argv + filter graph only)."""

from fractions import Fraction
from pathlib import Path

import pytest

from core_helpers import T23976, add_clip, make_base
from video_editor.core.commands import AddClipCommand, SetFadesCommand, SetGainCommand
from video_editor.core.model import Project, StreamSource, Track, TrackKind
from video_editor.render.graph import (
    MP4_H264_AAC,
    RenderConfigError,
    atempo_chain,
    build_job,
    normalise_layout,
)
from video_editor.render.plan import build_plan

F = Fraction
FFMPEG = "/usr/bin/ffmpeg"
PATHS = {"va": Path("/media/video.mkv"), "aa": Path("/media/audio.wav")}


DEFAULT_DEST = Path("/tmp/out.mp4")
ZERO = F(0)


def job_for(project, out_start=ZERO, out_end=None, preset=MP4_H264_AAC, dest=DEFAULT_DEST):
    plan = build_plan(project, out_start, out_end)
    sources = {(s.asset_id, s.stream_index): s for s in project.sources}
    return build_job(plan, dict(PATHS), sources, dest, FFMPEG, preset)


def graph_of(job) -> str:
    args = job.args
    return args[args.index("-filter_complex") + 1]


def test_atempo_chain_stays_in_range_and_multiplies_exactly():
    for rate in (F(1), F(2), F(4), F(1, 3), T23976, F(10), F(1, 10)):
        chain = atempo_chain(rate)
        product = F(1)
        for stage in chain:
            factor = F(stage.split("=")[1])
            assert F(1, 2) <= factor <= 2
            product *= factor
        assert abs(product - rate) < F(1, 10**6)
    assert atempo_chain(F(4)) == ["atempo=2.000000000", "atempo=2.000000000"]
    with pytest.raises(RenderConfigError, match="positive"):
        atempo_chain(F(0))


def test_basic_argv_shape():
    p = add_clip(make_base(), "v1", "vt", F(0), F(4))
    job = job_for(p, out_end=F(4))
    args = job.args
    assert args[0] == FFMPEG
    assert "-filter_complex" in args
    assert args[args.index("-c:v") + 1] == "libx264"
    assert args[args.index("-c:a") + 1] == "aac"
    assert args[args.index("-r") + 1] == "25/1"
    assert args[args.index("-ar") + 1] == "48000"
    assert args[-1] == "/tmp/out.mp4"
    assert "-progress" in args and "pipe:1" in args
    assert job.video_items == 1 and job.duration == F(4)


def test_picture_item_is_forced_to_exact_frame_count():
    p = add_clip(make_base(), "v1", "vt", F(0), F(4))
    graph = graph_of(job_for(p, out_end=F(4)))
    assert "fps=25/1" in graph
    assert "trim=start_frame=0:end_frame=100" in graph
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in graph
    assert "pad=1920:1080:(ow-iw)/2:(oh-ih)/2" in graph
    assert "setsar=1" in graph


def test_gaps_become_black_source_items():
    p = make_base()
    p = p.with_tracks((Track("vt", p.track("vt").kind, "V1"),))
    p = add_clip(p, "v1", "vt", F(0), F(2))
    graph = graph_of(job_for(p, out_end=F(6)))
    assert "color=c=black:" in graph
    assert "end_frame=100" in graph  # the 2 s black tail at 25 fps


def test_streams_are_addressed_by_type_and_position_not_container_index():
    """aa#1 is the container's second stream but its first audio stream."""
    p = make_base()
    p = add_clip(p, "a1", "at", F(0), F(4), asset="aa", stream=1)
    graph = graph_of(job_for(p, out_end=F(4)))
    assert "[0:a:0]" in graph
    assert "[0:1]" not in graph  # would address the wrong stream in FFmpeg
    assert "[0:a]" not in graph and "[0:v]" not in graph


def test_unrecorded_stream_position_is_refused_not_guessed():
    """Guessing a position could render a different soundtrack, so it is an error."""
    from video_editor.core.commands import RegisterSourceCommand

    p = make_base()
    p = RegisterSourceCommand(
        StreamSource("legacy", 2, TrackKind.AUDIO, F(0), F(10), sample_rate=48000)
    ).apply(p)
    p = add_clip(p, "a1", "at", F(0), F(4), asset="legacy", stream=2)
    paths = {**PATHS, "legacy": Path("/media/legacy.mkv")}
    plan = build_plan(p, ZERO, F(4))
    sources = {(src.asset_id, src.stream_index): src for src in p.sources}
    with pytest.raises(RenderConfigError, match="no recorded stream position"):
        build_job(plan, paths, sources, DEFAULT_DEST, FFMPEG, MP4_H264_AAC)


def test_unknown_stream_is_refused():
    plan = build_plan(add_clip(make_base(), "v1", "vt", F(0), F(4)), ZERO, F(4))
    sources = {(s.asset_id, s.stream_index): s for s in make_base().sources}
    sources.pop(("va", 0))
    with pytest.raises(RenderConfigError, match="is unknown"):
        build_job(plan, dict(PATHS), sources, DEFAULT_DEST, FFMPEG, MP4_H264_AAC)


def test_input_seeking_uses_asset_local_source_time():
    p = add_clip(make_base(), "v1", "vt", F(0), F(4), source_in=F(90))
    args = job_for(p, out_end=F(4)).args
    assert args[args.index("-ss") + 1] == "90.000000000"
    # exact rational text, no float drift
    args2 = job_for(add_clip(make_base(), "v1", "vt", F(0), F(2), source_in=F(1, 3))).args
    assert args2[args2.index("-ss") + 1] == "0.333333333"


def test_seek_is_measured_from_the_file_start_not_from_zero():
    """A presentation whose timeline begins at 2.5 s is seeked relative to 2.5 s."""
    from video_editor.core.commands import RegisterSourceCommand
    from video_editor.core.model import StreamSource

    paths = {**PATHS, "off": Path("/media/offset.mkv")}

    def job(project, out_end):
        plan = build_plan(project, ZERO, out_end)
        sources = {(s.asset_id, s.stream_index): s for s in project.sources}
        return build_job(plan, paths, sources, DEFAULT_DEST, FFMPEG, MP4_H264_AAC)

    def offset_project(source_in: Fraction, clip_id: str) -> Project:
        p = Project()
        p = RegisterSourceCommand(
            StreamSource("off", 0, TrackKind.VIDEO, F(5, 2), F(9, 2), frame_rate=F(25),
                         width=128, height=96, input_start=F(5, 2), stream_position=0)
        ).apply(p)
        p = p.with_tracks((Track("vt", TrackKind.VIDEO, "V1"),))
        return AddClipCommand("off", 0, "vt", F(0), source_in=source_in,
                              source_out=F(9, 2), clip_id=clip_id).apply(p)

    p = offset_project(F(5, 2), "c")
    args = job(p, F(2)).args
    assert args[args.index("-ss") + 1] == "0.000000000"
    p2 = offset_project(F(3), "c2")
    args2 = job(p2, F(2)).args
    assert args2[args2.index("-ss") + 1] == "0.500000000"


def test_rate_retime_uses_source_seconds_per_output_second():
    p = add_clip(make_base(), "v1", "vt", F(0), F(4), rate=T23976)
    graph = graph_of(job_for(p, out_end=F(4)))
    assert "setpts=(PTS-STARTPTS)/23.976023976" in graph  # 24000/1001
    assert "fps=25/1" in graph


def test_tempo_chain_appears_for_non_unity_audio_rate():
    p = make_base()
    p = add_clip(p, "a1", "at", F(0), F(4), asset="aa", stream=1, rate=F(4))
    graph = graph_of(job_for(p, out_end=F(4)))
    assert "atempo=2.000000000,atempo=2.000000000" in graph


def test_fades_become_frame_evaluated_volume_envelopes():
    p = make_base()
    p = add_clip(p, "a1", "at", F(0), F(10), asset="aa", stream=1)
    p = SetFadesCommand("a1", F(2), F(4)).apply(p)
    p = SetGainCommand("a1", F(3, 4)).apply(p)
    graph = graph_of(job_for(p, out_end=F(10)))
    assert "volume=volume='" in graph and "eval=frame" in graph
    assert "if(lt(t,0.000000000),0,if(lt(t,2.000000000),(t-0.000000000)/2.000000000,1))" in graph
    assert "(10.000000000-t)/4.000000000" in graph  # ramps to 0 at the clip end (10 s)
    assert graph.count("0.750000000") >= 1  # clip gain folded in


def test_the_envelope_is_applied_to_the_positioned_contribution():
    """Order in the filter chain is correctness, not style.

    The fade expression is written in time relative to the start of the rendered
    range. A contribution that does not begin there only has that clock once the
    leading silence has been concatenated, so the ``volume`` filter must come
    after it. Applied before, ``t`` restarts at 0, the fade's anchors are never
    reached, and the export silently carries no fade at all.
    """
    p = make_base()
    p = add_clip(p, "a1", "at", F(2), F(4), asset="aa", stream=1)  # output [2,6)
    p = SetFadesCommand("a1", F(0), F(1)).apply(p)  # fade out over [5,6)
    graph = graph_of(job_for(p, out_end=F(6)))
    placed = graph.index("concat=n=2:v=0:a=1")
    assert graph.index("volume=volume=") > placed, (
        "the envelope is applied before the leading silence that positions the clip"
    )
    assert "(6.000000000-t)/1.000000000" in graph


def test_contributions_are_summed_without_normalisation():
    p = make_base()
    p = p.with_tracks((*p.tracks, Track("at2", p.track("at").kind, "A2")))
    p = add_clip(p, "a1", "at", F(0), F(6), asset="aa", stream=1)
    p = add_clip(p, "a2", "at2", F(0), F(6), asset="aa", stream=1)
    graph = graph_of(job_for(p, out_end=F(6)))
    assert "amix=inputs=2:normalize=0" in graph


def test_offset_contribution_gets_exact_leading_silence():
    p = make_base()
    p = add_clip(p, "a1", "at", F(1), F(3), asset="aa", stream=1)
    graph = graph_of(job_for(p, out_end=F(4)))
    assert "anullsrc=" in graph
    assert "end_sample=48000" in graph  # 1 s of silence before the contribution
    assert "concat=n=2:v=0:a=1" in graph


def test_audio_only_range_still_carries_black_picture():
    p = make_base()
    p = p.with_tracks((Track("at", p.track("at").kind, "A1"),))
    p = add_clip(p, "a1", "at", F(0), F(3), asset="aa", stream=1)
    graph = graph_of(job_for(p, out_end=F(3)))
    assert "color=c=black:" in graph
    assert "atrim=start_sample=0:end_sample=144000" in graph


def test_silent_range_gets_explicit_silence_track():
    p = add_clip(make_base(), "v1", "vt", F(0), F(2))
    graph = graph_of(job_for(p, out_end=F(2)))
    assert "anullsrc=" in graph


def test_unsupported_channel_layout_is_refused():
    from video_editor.core.model import OutputSettings, Project

    p = Project(output=OutputSettings(320, 240, F(25), 48000, "hexagonal"))
    with pytest.raises(RenderConfigError, match="not supported for export"):
        job_for(p, out_end=F(1))
    assert normalise_layout("5.1(side)") == "5.1"
    with pytest.raises(RenderConfigError):
        normalise_layout("bicentric")


def test_missing_asset_path_is_reported_not_silently_skipped():
    p = add_clip(make_base(), "v1", "vt", F(0), F(2))
    plan = build_plan(p, F(0), F(2))
    sources = {(s.asset_id, s.stream_index): s for s in project_sources(p)}
    with pytest.raises(RenderConfigError, match="not available for rendering"):
        build_job(plan, {}, sources, Path("/tmp/x.mp4"), FFMPEG, MP4_H264_AAC)


def project_sources(project):
    return project.sources


def test_chunk_plans_join_to_the_same_frame_totals():
    p = add_clip(make_base(), "v1", "vt", F(0), F(4))
    whole = build_plan(p)
    first = build_plan(p, F(0), F(2))
    second = build_plan(p, F(2), F(4))
    assert first.total_frames + second.total_frames == whole.total_frames
    assert first.total_samples + second.total_samples == whole.total_samples
    g1 = graph_of(job_for(p, F(0), F(2)))
    g2 = graph_of(job_for(p, F(2), F(4)))
    assert "end_frame=50" in g1 and "end_frame=50" in g2
    assert "90.000000000" not in g1
