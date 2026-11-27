"""FFmpeg job construction tests (no rendering: argv + filter graph only)."""

from fractions import Fraction
from pathlib import Path

import pytest

from core_helpers import T23976, add_clip, make_base
from video_editor.core.commands import (
    AddClipCommand,
    RegisterSourceCommand,
    SetFadesCommand,
    SetGainCommand,
)
from video_editor.core.model import Project, StreamSource, Track, TrackKind
from video_editor.render.graph import (
    MP4_H264_AAC,
    RenderConfigError,
    atempo_chain,
    build_job,
    normalise_layout,
    picture_filter_chain,
)
from video_editor.render.plan import (
    PictureSampling,
    build_plan,
    ceil_to_time_base,
    picture_sampling,
)

F = Fraction
FFMPEG = "/usr/bin/ffmpeg"
PATHS = {
    "va": Path("/media/video.mkv"),
    "aa": Path("/media/audio.wav"),
    "aa96": Path("/media/audio96.wav"),
}


DEFAULT_DEST = Path("/tmp/out.mp4")
ZERO = F(0)


def register_96k(project, asset_id: str = "aa96", end: F | None = None):
    """A second audio stream whose *own* sample rate differs from the output.

    Every sample-count assertion that matters at a rate change needs two
    different rates, or a chain that mixed them up would look correct.
    """
    return RegisterSourceCommand(
        StreamSource(asset_id, 3, TrackKind.AUDIO, F(0), end or F(100),
                     sample_rate=96000, stream_position=1)
    ).apply(project)


def job_for(project, out_start=ZERO, out_end=None, preset=MP4_H264_AAC, dest=DEFAULT_DEST):
    plan = build_plan(project, out_start, out_end)
    sources = {(s.asset_id, s.stream_index): s for s in project.sources}
    return build_job(plan, dict(PATHS), sources, dest, FFMPEG, preset)


def job_for_tick_base(project, time_base: F, out_start=ZERO, out_end=None):
    """Same job, with the container's time base made available to the graph."""
    plan = build_plan(project, out_start, out_end)
    sources = {(s.asset_id, s.stream_index): s for s in project.sources}
    return build_job(
        plan, dict(PATHS), sources, DEFAULT_DEST, FFMPEG, MP4_H264_AAC,
        time_base_of=lambda _asset, _stream: time_base,
    )


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
    # the fit is written in the square pixels the output is made of, from the
    # display aspect ratio (measured picture evidence: test_picture_identity.py)
    assert (
        "scale=w='max(2,trunc(min(1920,1080*dar)/2)*2)'"
        ":h='max(2,trunc(min(1080,1920/dar)/2)*2)'" in graph
    )
    assert "pad=1920:1080:(ow-iw)/2:(oh-ih)/2" in graph
    assert "setsar=1" in graph


def test_the_picture_is_fitted_by_display_ratio_before_being_flattened():
    """The stage order is the geometry: fit by DAR, flatten, then pad.

    ``scale=<box>:force_original_aspect_ratio=decrease`` sizes the **coded** frame
    by the display ratio and leaves the sample aspect ratio alone — measured on
    FFmpeg 8.1.3, 720x576 SAR 64:45 becomes 900x720 still carrying SAR 64:45 — so a
    ``setsar=1`` straight after it flattens those anamorphic pixels and the picture
    arrives 900 px wide inside a 1280 px frame, which ``pad`` then fills with 190 px
    bars. The fit therefore has to produce the square-pixel size itself, and only
    then may the sample aspect be pinned, with the pad last.
    """
    p = add_clip(make_base(), "v1", "vt", F(0), F(4))
    graph = graph_of(job_for(p, out_end=F(4)))
    chain = next(part for part in graph.split(";") if part.startswith("[0:v:0]"))
    assert "force_original_aspect_ratio" not in chain
    fit = chain.index("scale=w='max(2,trunc(min(1920,1080*dar)/2)*2)'")
    flat = chain.index("setsar=1")
    padded = chain.index("pad=1920:1080:")
    assert fit < flat < padded, "setsar must follow the fit and precede the pad"
    assert "dar" in chain[fit:flat], "the fit is expressed with the display aspect ratio"
    assert "/2)*2" in chain[fit:flat], "both fitted dimensions stay even (4:2:0)"


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
    job = job_for(p, out_end=F(4))
    graph = graph_of(job)
    assert "setpts=PTS/23.976023976" in graph  # 24000/1001 source s per output s
    # the sampling grid is placed by the plan, not by wherever the seek landed:
    # without ``start_time`` the first grid point is the first decoded picture, so
    # a range beginning mid-clip would sample a different set of pictures from a
    # full render of the same clip. ``round=up`` is part of the contract, not a
    # detail: measured over a 2410-frame matrix, FFmpeg's default ``round=near``
    # resolved each grid point to the *nearest* frame and so showed a picture later
    # than the plan names in 777 frames (a 4x speed-up never showed the slice's
    # first frame); ``round=up`` gives the planned "last frame at or before the
    # sampling time" in all but 26 of them.
    assert "fps=round=up:fps=25/1:start_time=0.000000000" in graph


def test_picture_grid_is_placed_on_an_exactly_representable_seek():
    """The seek goes on a whole container tick, so FFmpeg's rebase is exact.

    Measured on an MP4 with ``time_base`` 1/12800: asking FFmpeg for
    0.333333333 s rebases the decoded timestamps by 4267/12800 s, and half a tick
    of phase error is enough to choose the neighbouring picture whenever a
    sampling time falls exactly on a frame time - the ordinary case at rate 1.0.
    """
    base = F(1, 12800)
    p = add_clip(make_base(), "v1", "vt", F(0), F(4), source_in=F(1, 3))
    plan = build_plan(p, F(0), F(4))
    job = job_for_tick_base(p, base)
    seek = F(job.args[job.args.index("-ss") + 1])
    assert (seek / base).denominator == 1, seek
    # the clip starts too near its own edge for a preroll, so the seek sits on the
    # first representable timestamp at or after the slice start - never before it
    assert seek == ceil_to_time_base(F(1, 3), base)
    sampling = picture_sampling(plan.video_items[0].clip, plan.video_items[0], F(25),
                                time_base=base)
    # and the whole preroll is accounted for in the grid, not left to the decoder:
    # the plan's grid phase is a whole number of the fine ticks the chain moves to,
    # it is taken off every timestamp, and the ``fps`` grid then starts there - so
    # the first grid point is exactly the plan's first sampling time whatever the
    # seek happened to deliver first
    assert (sampling.grid_start / sampling.tick).denominator == 1
    graph = graph_of(job)
    assert f"settb=expr=1/{1 // sampling.tick}" in graph
    # the phase is taken *off* every stamp, so a grid that starts before the seek
    # (a negative phase, as here: the slice edge snapped forward past it) is added
    # back
    term = f"{'-' if sampling.phase > 0 else '+'}{abs(sampling.phase)}"
    assert f"setpts=PTS*1/1{term}" in graph
    assert "fps=round=up:fps=25/1:start_time=0" in graph
    assert job.unanchored == frozenset()
    assert job.inexact_picture == frozenset()


def test_a_stream_without_a_readable_time_base_is_reported_not_hidden():
    """No time base -> the grid rides on whatever picture the seek found."""
    p = add_clip(make_base(), "v1", "vt", F(0), F(4))
    job = job_for(p, out_end=F(4))
    assert job.unanchored == frozenset({("va", 0)})


def test_frames_the_slice_cannot_supply_hold_the_nearest_picture():
    """The plan's hold rule is written into the graph, not left to chance.

    ``fps`` starts late when a grid point precedes every readable picture and
    stops early when the source runs out, so an item would come out short and its
    remaining frames shifted by one: the plan's frame count would then be met with
    the wrong pictures. Cloning at the head (only when the lattice asked before the
    clip's start) and at the tail is what makes "show the nearest legal picture"
    real, and the trim below keeps exactly the promised count either way.
    """
    base = F(1, 12800)
    p = add_clip(make_base(), "v1", "vt", F(0), F(4), source_in=F(1, 3))
    plan = build_plan(p, F(0), F(4))
    item = plan.video_items[0]
    sampling = picture_sampling(item.clip, item, F(25), time_base=base)
    graph = graph_of(job_for_tick_base(p, base))
    assert f"tpad=stop_mode=clone:stop={item.frames}" in graph
    assert f"trim=start_frame=0:end_frame={item.frames}" in graph
    assert sampling.head_gap == 0  # the clip starts cleanly at this boundary


def test_a_head_the_slice_cannot_reach_is_counted_exactly():
    """A clip head the lattice reaches before the source can is held, not shifted.

    The first grid point can precede the clip's first readable picture by less than
    one output frame's worth of *composition* time, but on slow motion that is
    several output frames of *source* time: getting the count wrong moves every
    frame of the item, which is exactly the class of defect this task is about.
    """
    base = F(1, 12800)
    # rate 1/4: one output frame covers 1/100 s of source time, so a stream whose
    # first readable picture sits 30 ms into the slice needs three held frames
    p = add_clip(make_base(), "v1", "vt", F(0), F(8), rate=F(1, 4))
    plan = build_plan(p, F(0), F(8))
    item = plan.video_items[0]
    assert item.clip.rate == F(1, 4)
    measured = picture_sampling(item.clip, item, F(25), time_base=base,
                                first_frame=F(3, 100))
    assert measured.head_gap == 3, measured
    # with no frame index to measure from, the lattice alone only knows that a
    # clip edge can snap forward by at most one frame - and it says so honestly
    # rather than inventing a larger hold
    inferred = picture_sampling(item.clip, item, F(25), time_base=base)
    assert inferred.head_gap == 0 and not inferred.head_held
    graph = graph_of(job_for_tick_base(p, base))
    assert "tpad=start_mode=clone" not in graph
    forced = picture_sampling(item.clip, item, F(25), time_base=base, first_frame=F(0))
    assert forced.head_gap == 0


def test_tempo_chain_appears_for_non_unity_audio_rate():
    p = make_base()
    p = add_clip(p, "a1", "at", F(0), F(4), asset="aa", stream=1, rate=F(4))
    graph = graph_of(job_for(p, out_end=F(4)))
    assert "atempo=2.000000000,atempo=2.000000000" in graph


def test_fades_are_sample_accurate_afade_stages():
    """Fade shape is a sample-addressed ramp, not a per-frame volume expression.

    ``volume=…:eval=frame`` re-evaluates once per filter frame — a buffer of many
    samples — so a short fade would be a staircase. ``afade`` addressed by
    ``start_sample``/``nb_samples`` scales the k-th sample of the ramp by k/N,
    which is exactly what ``core.model.envelope_gain`` computes.
    """
    p = make_base()
    p = add_clip(p, "a1", "at", F(0), F(10), asset="aa", stream=1)
    p = SetFadesCommand("a1", F(2), F(4)).apply(p)
    p = SetGainCommand("a1", F(3, 4)).apply(p)
    graph = graph_of(job_for(p, out_end=F(10)))
    assert "afade=t=in:curve=tri:start_sample=0:nb_samples=96000" in graph  # 2 s at 48 kHz
    # 4 s ramp finishing exactly at the clip's last sample (10 s = 480000)
    assert "afade=t=out:curve=tri:start_sample=288000:nb_samples=192000" in graph
    assert "volume=volume=0.750000000" in graph  # gain is a plain constant multiply
    assert "eval=frame" not in graph


def test_fades_are_anchored_to_the_clip_then_cut_to_the_range():
    """Order in the filter chain is correctness, not style.

    The clip is built in full from its own start, its fades are applied over the
    whole clip, and only then is the render range's piece cut out. Applied to the
    range-clipped piece instead, a chunk that begins mid-fade would restart the
    ramp (or lose it), so a preview and an export of the same composition would
    not sound the same.
    """
    p = make_base()
    p = add_clip(p, "a1", "at", F(2), F(4), asset="aa", stream=1)  # output [2,6)
    p = SetFadesCommand("a1", F(0), F(1)).apply(p)  # fade out over [5,6)
    graph = graph_of(job_for(p, out_end=F(5)))  # range stops 1 s into the clip
    # 4 s clip: the ramp starts 3 s into the *clip*, not at the range's start
    assert "afade=t=out:curve=tri:start_sample=144000:nb_samples=48000" in graph
    fade = graph.index("afade=t=out")
    cut = graph.index("atrim=start_sample=0:end_sample=144000")  # [2,5) of the clip
    assert fade < cut, "the range is cut before the fade is applied to the whole clip"


def test_the_source_slice_is_bounded_on_the_streams_own_axis():
    """Nothing past ``source_out`` is read, and no sample count is ambiguous.

    A 96 kHz source into a 48 kHz output makes the two counts different numbers
    by construction: 4 s is 384000 source samples and 192000 output samples. The
    old chain wrote one cap, in output samples, at a point in the graph where the
    samples being counted were whichever rate FFmpeg had settled on there — it
    happened to insert a resampler first, so the extent came out right for PCM
    sources in FFmpeg 8.1.3 (measured at ``6a09c8b``, nothing leaked past the
    clip), but nothing in the graph said so. Here the conversion is written
    explicitly, and each side states its own count.
    """
    p = register_96k(make_base())
    p = add_clip(p, "a1", "at", F(0), F(4), asset="aa96", stream=3)  # source [0,4)
    job = job_for(p, out_end=F(4))
    args = job.args
    # the input is opened for the clip's material, plus the packet-rounding
    # overread: an input ``-t`` stops at a packet boundary, so a bound exactly on
    # the clip's end loses the last packet (measured: 1 AAC sample instead of
    # 1920). It is still bounded - FFmpeg does not decode the whole stream - and
    # the exact amount kept is the source-sample cap below.
    bounds = [args[i + 1] for i, flag in enumerate(args) if flag == "-t"]
    assert bounds == ["4.250000000"], (
        "the input is not bounded to the clip's material plus one audio packet"
    )
    graph = graph_of(job)
    # 4 s of a 96 kHz stream is 384000 source samples, and that cap comes before
    # the sample-rate conversion is written out at all
    cap = graph.index("atrim=start_sample=0:end_sample=384000")
    resample = graph.index("aresample=48000")
    assert cap < resample
    assert "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo" in graph
    # the clip's own length is then counted in output samples
    assert "atrim=start_sample=0:end_sample=192000" in graph


def test_a_range_inside_a_clip_still_builds_the_whole_clip():
    """A chunk or preview that starts mid-clip continues the clip, exactly."""
    p = register_96k(make_base())
    # output [2,6) built from source [2,6): the render range below starts inside it
    p = add_clip(p, "a1", "at", F(2), F(4), asset="aa96", stream=3, source_in=F(2))
    job = job_for(p, out_start=F(4), out_end=F(6))
    args = job.args
    assert args[args.index("-ss") + 1] == "2.000000000"  # the clip's start, not the range's
    # the clip's material, plus the audio packet bound's rounding allowance (see
    # test_the_source_slice_is_bounded_on_the_streams_own_axis)
    assert args[args.index("-t") + 1] == "4.250000000"
    graph = graph_of(job)
    # whole clip = 4 s = 192000 output samples; the first 2 s belong to an earlier
    # chunk and are discarded only after the fades and gain have been applied
    assert "atrim=start_sample=0:end_sample=192000" in graph
    assert "atrim=start_sample=96000:end_sample=192000" in graph
    # ``atrim`` keeps the timestamps of what it keeps, so after throwing away the
    # first 2 s the contribution still claims to start at 2 s. Muxed like that the
    # container runs to 8 s for a 2 s range: measured before the rebase was added,
    # an export of output [2,6) reported 3.0 s for a 2.0 s plan.
    cut = graph.index("atrim=start_sample=96000:end_sample=192000")
    assert "asetpts=PTS-STARTPTS" in graph[cut:], (
        "the range is cut without rebasing its timestamps"
    )


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
    with pytest.raises(RenderConfigError):
        normalise_layout("bicentric")


def test_side_speakers_are_not_treated_as_rear_speakers():
    """``5.1(side)`` used to be quietly renamed to ``5.1``.

    Those name different speaker positions. Renaming one to the other promises a
    mix that puts sound where the project never asked for it, so the layout is
    refused with the reason rather than guessed at.
    """
    for spelling in ("5.1(side)", "quad(side)", "7.1(wide-side)"):
        with pytest.raises(RenderConfigError, match="side speakers"):
            normalise_layout(spelling)
    assert normalise_layout("5.1") == "5.1"


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


# ---------------------------------------------- picture chain: exact tick and slice


def job_with_lookups(
    project,
    time_base: F,
    out_start=ZERO,
    out_end=None,
    first_frame: F | None = None,
    previous_frame: F | None = None,
):
    """The same job with every stream measurement the exact chain can use."""
    plan = build_plan(project, out_start, out_end)
    sources = {(s.asset_id, s.stream_index): s for s in project.sources}
    return build_job(
        plan, dict(PATHS), sources, DEFAULT_DEST, FFMPEG, MP4_H264_AAC,
        time_base_of=lambda _asset, _stream: time_base,
        first_frame_of=lambda _a, _s, _t: first_frame,
        previous_frame_of=lambda _a, _s, _t: previous_frame,
    )


def picture_part(graph: str) -> str:
    """The picture chain of a one-video-item graph, up to the geometry stages."""
    part = next(p for p in graph.split(";") if "fps=" in p)
    return part.split("scale=")[0]


def test_the_source_slice_is_written_in_whole_fine_ticks():
    """``select`` bounds the composition on the source axis, exactly.

    Hand-worked for ``[1/3, 6)`` at 25/24 on a millisecond container: the seek
    lands on 334/1000 s, the tick the plan chooses is 1/25000 s, and the clip's
    end is ``(6 - 334/1000) * 25000 = 141650`` ticks. Writing that as an integer -
    not a rounded decimal - is what keeps a picture whose own timestamp *is* the
    slice end outside the composition, on every range, because the comparison is
    exact rather than near.
    """
    base = F(1, 1000)
    p = add_clip(make_base(), "v1", "vt", F(0), F(136, 25), rate=F(25, 24),
                 source_in=F(1, 3))
    job = job_with_lookups(p, base, out_end=F(136, 25))
    chain = picture_part(graph_of(job))
    assert "settb=expr=1/25000" in chain
    assert "select='lt(pts,141650)'" in chain
    assert "setpts=PTS*24/25+16" in chain  # the grid's phase, -16 ticks, taken off
    assert "fps=round=up:fps=25/1:start_time=0" in chain
    assert job.unanchored == frozenset() and job.inexact_picture == frozenset()


def test_a_range_uses_the_same_tick_and_a_phase_for_its_own_seek():
    """The join cannot drift: only the phase term differs between the two paths.

    For the second half of the same clip the seek is 2916/1000 s, so the slice end
    is ``(6 - 2.916) * 25000 = 77100`` ticks and the phase is 6016 ticks - the
    arithmetic of the same grid, placed where this range's own seek starts.
    """
    base = F(1, 1000)
    p = add_clip(make_base(), "v1", "vt", F(0), F(136, 25), rate=F(25, 24),
                 source_in=F(1, 3))
    whole = graph_of(job_with_lookups(p, base, out_end=F(136, 25)))
    part = graph_of(job_with_lookups(p, base, out_start=F(68, 25), out_end=F(136, 25)))
    assert "settb=expr=1/25000" in whole and "settb=expr=1/25000" in part
    assert "select='lt(pts,141650)'" in whole
    assert "select='lt(pts,77100)'" in part
    assert "setpts=PTS*24/25+16" in whole
    assert "setpts=PTS*24/25-6016" in part
    assert "fps=round=up:fps=25/1:start_time=0" in part


def test_a_head_the_source_cannot_reach_holds_without_a_padding_stage():
    """The plan still counts the hold; the chain no longer has to pad for it.

    With the grid pinned at the plan's first sampling time, ``fps`` has a grid
    point before any readable picture exists and holds the first one there
    (measured: the phase diagnostic's ``head-hold`` frames arrive at the planned
    index with the planned count). A ``tpad=start_mode=clone`` on top of that
    would add frames the plan never promised and move every picture after them.
    """
    base = F(1, 12800)
    p = add_clip(make_base(), "v1", "vt", F(0), F(8), rate=F(1, 4), source_in=F(1, 3))
    late = F(1, 3) + F(3, 100)  # the first readable picture is 30 ms into the slice
    job = job_with_lookups(p, base, out_end=F(8), first_frame=late)
    plan = build_plan(p, ZERO, F(8))
    item = plan.video_items[0]
    sampling = picture_sampling(item.clip, item, F(25), time_base=base, first_frame=late)
    assert sampling.head_gap == 3 and sampling.tick is not None
    chain = picture_part(graph_of(job))
    assert "tpad=start_mode=clone" not in chain
    assert f"tpad=stop_mode=clone:stop={item.frames}" in chain
    assert f"trim=start_frame=0:end_frame={item.frames}" in graph_of(job)


def test_a_stream_with_no_usable_tick_is_reported_and_drawn_the_old_way():
    """The refusal is bounded and said out loud, never a silent approximation.

    An in-point of 1/50021 s on a millisecond container would need a phase that is
    a 50021st of a container tick, i.e. over a billion ticks per second - past
    ``MAX_PICTURE_TICK``. The item is then drawn with the older container-quantised
    chain, and the job names the stream.
    """
    p = add_clip(make_base(), "v1", "vt", F(0), F(4), rate=F(25, 24),
                 source_in=F(1, 50021))
    job = job_with_lookups(p, F(1, 1000), out_end=F(4))
    assert job.inexact_picture == frozenset({("va", 0)})
    assert job.unanchored == frozenset()
    chain = picture_part(graph_of(job))
    assert "settb=" not in chain and "select=" not in chain
    assert "setpts=PTS/" in chain and "start_time=" in chain


def test_measured_picture_evidence_moves_the_seek_not_the_sampling():
    """The reading window starts at the real picture the first grid point shows.

    ``-ss`` is what makes a range cheap: with the frame index's evidence it lands
    one container tick before the picture that is needed rather than a nominal
    quarter second back. The sampling times, the tick and the slice are the same
    either way, so the pictures cannot move - only the amount decoded.
    """
    base = F(1, 1000)
    p = add_clip(make_base(), "v1", "vt", F(0), F(136, 25), rate=F(25, 24),
                 source_in=F(1, 3))
    nominal = job_with_lookups(p, base, out_start=F(68, 25), out_end=F(136, 25))
    measured = job_with_lookups(p, base, out_start=F(68, 25), out_end=F(136, 25),
                                previous_frame=F(3150, 1000))
    assert F(nominal.args[nominal.args.index("-ss") + 1]) == F(2916, 1000)
    seek = F(measured.args[measured.args.index("-ss") + 1])
    assert seek == F(3150, 1000) - base
    assert seek > F(2916, 1000)  # less decoded material, not more
    whole_nominal = picture_part(graph_of(nominal))
    whole_measured = picture_part(graph_of(measured))
    assert "select='lt(pts,77100)'" in whole_nominal
    # (6 - 3.149) s * 25000 = 71275 ticks, and the phase is 424: both exact
    assert "select='lt(pts,71275)'" in whole_measured
    assert "setpts=PTS*24/25-424" in whole_measured
    assert "settb=expr=1/25000" in whole_measured


def test_a_slice_edge_off_the_container_grid_stays_an_exact_comparison():
    """Splitting a clip leaves source edges that are not whole container ticks.

    ``[1/3, 19/3)`` at 25/24 on a millisecond container - a clip of 144/25 output
    seconds, so 6 source seconds - and the hand-worked numbers are the same as the
    test above: seek 334/1000 s, tick 1/25000 s. The end of the legal slice is then
    ``(19/3 - 334/1000) * 25000 = 449950/3 = 149983 1/3`` ticks. ``pts`` at that
    point in the chain is a whole number of ticks, so "``pts < 149983 1/3``" is
    exactly the same statement as "``pts < 149984``" - the ceiling - and writing the
    integer keeps the comparison exact however long the timeline is, instead of
    asking FFmpeg to hold a fraction in a double where a long film has no room for
    one.
    """
    base = F(1, 1000)
    p = add_clip(make_base(), "v1", "vt", F(0), F(144, 25), rate=F(25, 24),
                 source_in=F(1, 3))
    assert p.clip("v1").source_out == F(19, 3)  # the edge really is off the grid
    job = job_with_lookups(p, base, out_end=F(144, 25))
    chain = picture_part(graph_of(job))
    assert "settb=expr=1/25000" in chain
    assert "select='lt(pts,149984)'" in chain
    assert job.unanchored == frozenset() and job.inexact_picture == frozenset()


def test_a_fine_timescale_stream_is_retimed_by_one_exact_division():
    """What the chain writes once the tempo has been proved against the stream.

    The shape from TASK_12n: container tick 1/50000000, clip ``[1, 6)`` at tempo
    1000/1001 on 25 fps output, rendering only global frame 125 - source ``6001/1001``
    s, a hold of the last picture. The rule that assumes nothing about the stream's
    timestamps wants 50,000,000,000 filter ticks per second; proved against the
    pictures the window really holds (``n/25``), the container's own tick is exact and
    the tempo, the phase and the division go into one statement.

    Hand-worked in ``tests/test_render_plan.py``: the seek is 5.745004 s (the
    tick-aligned point ``-ss`` reproduces) and the grid's phase is
    ``(6001/1001 - 5745004/1000000) * 1001 * 50_000_000 = 12_512_549_800`` ticks, so
    the retimed stamp of a picture ``p`` ticks after the seek is
    ``(p*1001 - 12_512_549_800)/1000`` - whole for every picture of the window, and
    ``fps`` then starts its grid at exactly that sampling time.
    """
    fine = F(1, 50_000_000)
    pictures = tuple(F(n, 25) for n in range(150))
    project = add_clip(make_base(), "v1", "vt", F(0), F(6), source_in=F(1),
                       rate=F(1000, 1001))
    plan = build_plan(project, F(125, 25), F(126, 25))
    sources = {(s.asset_id, s.stream_index): s for s in project.sources}
    job = build_job(
        plan, dict(PATHS), sources, DEFAULT_DEST, FFMPEG, MP4_H264_AAC,
        time_base_of=lambda _a, _s: fine,
        window_pictures_of=lambda _a, _s, _start, _end: pictures,
    )
    graph = graph_of(job)
    assert "settb=expr=1/50000000" in graph, graph
    assert "setpts=(PTS*1001-12512549800)/1000" in graph, graph
    assert "fps=round=up:fps=25/1:start_time=0" in graph, graph
    assert "setpts=PTS/0.999000999" not in graph, (
        "the container-quantised tempo must not survive where an exact one was proved"
    )
    assert job.inexact_picture == frozenset(), (
        "the stream is no longer an approximation: its tempo was measured"
    )

    # the same item with no window read: nothing is proved, so the older chain is
    # written and the job says the picture is approximate
    unproved = build_job(
        plan, dict(PATHS), sources, DEFAULT_DEST, FFMPEG, MP4_H264_AAC,
        time_base_of=lambda _a, _s: fine,
    )
    assert "setpts=PTS/0.999000999" in graph_of(unproved)
    assert unproved.inexact_picture == frozenset({("va", 0)})


def test_the_ordinary_chain_text_does_not_move_when_the_window_is_measured():
    """Reading the window is allowed to prove more, never to respell what was exact.

    An ordinary container tick (1/12800, the measured B-frame MP4) and tempo 1000/1001:
    the stamp-free rule already finds an exact tick (``1000 * 12800`` per second), so
    the factored ``setpts=PTS*<b>/<a>-<phase>`` spelling stays, term for term, whether
    or not the stream's picture stamps were read.
    """
    base = F(1, 12800)
    pictures = tuple(F(n, 25) for n in range(150))
    project = add_clip(make_base(), "v1", "vt", F(0), F(6), source_in=F(1),
                       rate=F(1000, 1001))
    plan = build_plan(project, ZERO, F(1001, 200))
    sources = {(s.asset_id, s.stream_index): s for s in project.sources}
    plain = build_job(plan, dict(PATHS), sources, DEFAULT_DEST, FFMPEG, MP4_H264_AAC,
                      time_base_of=lambda _a, _s: base)
    measured = build_job(
        plan, dict(PATHS), sources, DEFAULT_DEST, FFMPEG, MP4_H264_AAC,
        time_base_of=lambda _a, _s: base,
        window_pictures_of=lambda _a, _s, _start, _end: pictures,
    )
    assert graph_of(measured) == graph_of(plain)
    sampling = picture_sampling(plan.video_items[0].clip, plan.video_items[0], F(25),
                                time_base=base)
    assert sampling.tempo is not None and sampling.tempo.factored
    assert f"settb=expr=1/{1 // sampling.tempo.tick}" in graph_of(plain)
    assert sampling.phase == sampling.tempo.offset // sampling.tempo.divisor


def test_an_item_whose_grid_cannot_reach_a_picture_is_refused_by_name():
    """No picture in reach of the first grid point: refuse, do not render nothing.

    Measured boundary (``tests/test_render_plan.py``, swept with plain FFmpeg on the
    fine fixture: start_time 0.215210996 through 0.275151056): with the grid's first
    point up to 0.62 of an output frame period past the last decoded arrival the
    approximate chain writes pictures, and from 0.63 periods past it writes **none**.
    Only the container-quantised chain has that failure - with an exact tempo the grid
    is placed by the phase, and a sampling time past the last picture is the ordinary
    tail hold - so the refusal names both the tempo's failure and the empty chain.
    """
    plan = build_plan(add_clip(make_base(), "v1", "vt", ZERO, F(4)), ZERO, F(4))
    item = plan.video_items[0]
    clip = item.clip
    stamps = tuple(F(n, 25) for n in range(0, 100))
    step = clip.rate / F(25)
    exact = picture_sampling(clip, item, F(25), time_base=F(1, 1000),
                             window_pictures=stamps)
    assert exact.tempo is not None  # an ordinary tick: exact chain, tail hold fine
    assert exact.window_pictures == stamps
    picture_filter_chain(plan, clip, item, exact, "yuv420p")

    fallback = PictureSampling(
        seek_source=F(5745004, 1_000_000),
        read_source_end=F(6),
        sample_start=F(149, 25) + F(28, 1001),
        sample_step=step,
        grid_start=(F(149, 25) + F(28, 1001) - F(5745004, 1_000_000)) / clip.rate,
        anchored=True,
        head_held=False,
        head_gap=0,
        tail_held=True,
        tick_reason="no exact filter tick could be established for this stream",
        window_pictures=tuple(F(n, 25) for n in range(144, 150)),
    )
    with pytest.raises(RenderConfigError) as caught:
        picture_filter_chain(plan, clip, item, fallback, "yuv420p")
    message = str(caught.value)
    assert "would write no picture at all" in message, message
    assert "no exact filter tick could be established" in message, message
    assert "0.7" not in message  # the refusal quotes the plan's numbers, not a tuned factor


def test_an_item_whose_reading_window_the_stream_leaves_empty_is_refused_by_name():
    """A window with nothing in it is not a chain, exact tick or not.

    The measured fixture's declared 6 s end overshoots its last picture (``149/25``),
    so a slice inside the declared duration and past the last picture decodes nothing.
    No filter graph can show a picture it never receives - ``fps`` has no arrival for
    its grid and ``tpad=stop_mode=clone`` has nothing to clone - so the item is refused
    with the window it asked for, rather than reaching a render that produces no video.
    """
    plan = build_plan(add_clip(make_base(), "v1", "vt", ZERO, F(4)), ZERO, F(4))
    item = plan.video_items[0]
    clip = item.clip
    # measured window, measured empty: the stamp-free rule still finds an exact tick
    # for this ordinary container, so the chain is the exact one, and it still has
    # nothing to show
    empty_window = picture_sampling(clip, item, F(25), time_base=F(1, 1000),
                                    window_pictures=())
    assert empty_window.tempo is not None, (
        "the refusal is not about the tempo: this chain is exact, and it still has "
        "nothing to show"
    )
    assert empty_window.window_pictures == (), (
        "an empty read is an answer, not a missing measurement"
    )
    with pytest.raises(RenderConfigError) as caught:
        picture_filter_chain(plan, clip, item, empty_window, "yuv420p")
    message = str(caught.value)
    assert "holds no picture of that stream at all" in message, message
    assert f"[{float(empty_window.seek_source):.6f}, " in message, message
    assert f"{float(empty_window.read_source_end):.6f})s" in message, message
