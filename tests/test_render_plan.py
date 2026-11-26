"""Pure render-plan tests: quantisation, stacking, gaps, fades, ranges, geometry."""

from dataclasses import replace
from fractions import Fraction

import pytest

from core_helpers import T25, T23976, add_clip, make_base
from video_editor.core.commands import (
    AddClipCommand,
    RegisterSourceCommand,
    RemoveClipsCommand,
    SetFadesCommand,
    SetGainCommand,
    SetTrackEnabledCommand,
    SetTrackMutedCommand,
)
from video_editor.core.model import (
    Clip,
    OutputSettings,
    Project,
    StreamSource,
    Track,
    TrackKind,
)
from video_editor.render.plan import (
    Quantisation,
    RenderPlanError,
    build_plan,
    plan_geometry,
    round_half_up,
)

F = Fraction


def two_video_tracks() -> Project:
    """Two video tracks (top, bottom) + one audio track, 25 fps / 48 kHz."""
    p = make_base()
    p = p.with_tracks(
        (
            Track("vtop", TrackKind.VIDEO, "Top"),
            Track("vbot", TrackKind.VIDEO, "Bottom"),
            Track("at", TrackKind.AUDIO, "A1"),
        )
    )
    return p


# ------------------------------------------------------------------ rounding


def test_round_half_up_is_exact_and_sign_safe():
    assert round_half_up(F(1, 2)) == 1
    assert round_half_up(F(3, 10)) == 0
    assert round_half_up(F(7, 10)) == 1
    assert round_half_up(F(-1, 2)) == 0  # -0.5 -> 0 (toward +inf), never -1
    assert round_half_up(F(-3, 4)) == -1
    assert round_half_up(F(5)) == 5


def test_quantisation_snaps_forward_to_the_covering_frame():
    """The frame that covers a time starts at or after it, never more than a frame away."""
    q = Quantisation(F(25), 48000)
    for num in range(0, 200):
        t = F(num, 3)  # deliberately not frame aligned
        i = q.frame_index(t)
        offset = q.frame_time(i) - t
        assert 0 <= offset < F(1, 25), (t, i, offset)
        # the frame before it is the one that ends at or before t
        assert q.frame_time(i - 1) <= t
        s = q.sample_index(t)
        assert 0 <= q.sample_time(s) - t < F(1, 48000)


# -------------------------------------------------------------- empty ranges


def test_one_boundary_rule_names_and_counts_the_same_frame():
    """Naming a frame and counting a length are the same rounding: up.

    They used to differ (indices rounded half up, lengths rounded up), and the
    difference was observable: a composition 0.01 s long had *zero* frames for
    the exporter but a one-frame range for the preview, and a length of 1.005 s
    ended one frame past the last frame the export would write. One rule removes
    the contradiction; the cost is that a boundary never snaps backwards, so a
    clip edge moves forward by at most one frame.
    """
    quant = Quantisation(T25, 48000)
    assert quant.frame_index(F(1, 50)) == quant.frame_count(F(1, 50)) == 1
    assert quant.frame_index(F(1, 100)) == quant.frame_count(F(1, 100)) == 1
    assert quant.frame_index(F(1, 500)) == quant.frame_count(F(1, 500)) == 1
    assert quant.frame_index(F(2, 25)) == quant.frame_count(F(2, 25)) == 2
    assert quant.frame_count(F(0)) == 0 and quant.frame_index(F(0)) == 0
    assert quant.frame_count(F(-3)) == 0
    # counting a whole length equals the indices it spans
    assert quant.frame_count(F(7)) == quant.frame_index(F(7)) - quant.frame_index(F(0))
    assert quant.frame_span(F(0), F(7)) == (0, 175)
    assert quant.frame_span(F(1, 100), F(1, 100)) == (0, 0)
    # samples follow the same rule
    assert quant.sample_index(F(1, 100)) == 480 and quant.sample_count(F(1, 100)) == 480
    assert quant.sample_span(F(1), F(2)) == (48000, 96000)


def test_empty_project_yields_zero_length_plan():
    plan = build_plan(Project())
    assert plan.duration == 0
    assert plan.total_frames == 0 and plan.total_samples == 0
    assert plan.video_items == () and plan.audio_items == ()
    assert build_plan(Project(), F(5), F(5)).total_frames == 0


def test_range_beyond_content_is_black_silence_not_short():
    p = add_clip(make_base(), "v1", "vt", F(0), F(4))
    plan = build_plan(p, F(0), F(10))
    assert plan.total_frames == 250
    assert sum(i.frames for i in plan.video_items) == 250
    black = [i for i in plan.video_items if i.is_black]
    assert len(black) == 1 and black[0].start_frame == 100 and black[0].frames == 150


# ------------------------------------------------------------------- picture


def test_topmost_enabled_video_track_wins_per_frame():
    p = two_video_tracks()
    p = add_clip(p, "bottom", "vbot", F(0), F(10))
    p = add_clip(p, "top", "vtop", F(2), F(3))  # covers [2,5)
    plan = build_plan(p)
    assert plan.output_end == F(10)
    assert plan.video_items[0].clip.id == "bottom"
    assert plan.video_items[0].frames == 50  # [0,2) at 25 fps
    assert plan.video_items[1].clip.id == "top"
    assert plan.video_items[1].frames == 75  # [2,5)
    assert plan.video_items[2].clip.id == "bottom"
    assert plan.video_items[-1].start_frame == 125
    assert sum(i.frames for i in plan.video_items) == 250


def test_disabled_video_track_is_not_picture():
    p = two_video_tracks()
    p = add_clip(p, "top", "vtop", F(0), F(4))
    p = add_clip(p, "bottom", "vbot", F(0), F(4))
    p = SetTrackEnabledCommand("vtop", False).apply(p)
    plan = build_plan(p)
    # the top track is skipped, the next enabled track wins (core agrees)
    assert [i.clip.id for i in plan.video_items] == ["bottom"]
    assert p.video_clip_at(F(1)).id == "bottom"
    both_off = SetTrackEnabledCommand("vbot", False).apply(p)
    plan_off = build_plan(both_off)
    assert all(i.is_black for i in plan_off.video_items)
    assert both_off.video_clip_at(F(1)) is None
    assert sum(i.frames for i in plan_off.video_items) == plan_off.total_frames


def test_disabled_track_still_defines_output_length():
    p = two_video_tracks()
    p = add_clip(p, "keep", "vtop", F(0), F(4))
    p = add_clip(p, "gone", "vbot", F(0), F(9))
    p = SetTrackEnabledCommand("vbot", False).apply(p)
    plan = build_plan(p)
    assert plan.output_end == F(9)  # duration is not shifted by toggles
    assert plan.total_frames == 225


def test_gaps_between_clips_are_explicit_black():
    p = two_video_tracks()
    p = add_clip(p, "a", "vtop", F(0), F(2))
    p = add_clip(p, "b", "vtop", F(5), F(2))
    plan = build_plan(p)
    kinds = [(i.clip.id if i.clip else None, i.frames) for i in plan.video_items]
    assert kinds == [("a", 50), (None, 75), ("b", 50)]


def test_consecutive_frames_of_one_clip_are_merged():
    p = two_video_tracks()
    p = add_clip(p, "top", "vtop", F(0), F(4))
    p = add_clip(p, "other", "vbot", F(1, 3), F(1, 3))  # cuts a boundary inside
    plan = build_plan(p)
    assert len(plan.video_items) == 1
    assert plan.video_items[0].frames == 100


# ------------------------------------------------------------ quantisation


def test_non_frame_aligned_boundaries_use_global_indices():
    """1/3 s and 10/3 s at 25 fps: no per-segment rounding drift."""
    p = two_video_tracks()
    p = add_clip(p, "a", "vtop", F(0), F(1, 3))
    p = add_clip(p, "b", "vtop", F(1, 3), F(10, 3) - F(1, 3))
    plan = build_plan(p, F(0), F(10, 3))
    frames = [i.frames for i in plan.video_items]
    quant = Quantisation(F(25), 48000)
    # one rule for every boundary: each snaps forward to the frame that covers it
    assert sum(frames) == plan.total_frames == quant.frame_count(F(10, 3)) == 84
    # each boundary is within one frame of exact (always the frame after it), and
    # the indices are contiguous
    ends = [i.end_frame for i in plan.video_items]
    assert ends == sorted(set(ends))
    assert plan.video_items[0].end_frame == plan.video_items[1].start_frame
    for item in plan.video_items:
        assert F(0) <= quant.frame_time(item.start_frame) - item.output_start < F(1, 25)


def test_source_slices_follow_quantised_output_times():
    p = two_video_tracks()
    p = add_clip(p, "a", "vtop", F(0), F(4), rate=T23976)
    plan = build_plan(p)
    item = plan.video_items[0]
    assert item.source_start == F(0)
    # the item covers output [0,4) exactly -> source span is 4 * rate, and each
    # interior frame boundary is resolved from its quantised output time
    assert item.source_end == 4 * T23976
    at_frame_99 = item.clip.source_time_at(F(99, 25))
    assert at_frame_99 == F(99, 25) * T23976


def test_source_offset_of_stream_is_preserved():
    """A stream starting at 1.0 s asset-local keeps that origin in the slice."""
    p = Project()
    p = RegisterSourceCommand(
        StreamSource("off", 0, TrackKind.VIDEO, F(1), F(61), frame_rate=F(25))
    ).apply(p)
    p = p.with_tracks((Track("vt", TrackKind.VIDEO, "V1"),))
    p = AddClipCommand("off", 0, "vt", F(0), source_in=F(1), source_out=F(11)).apply(p)
    plan = build_plan(p)
    item = plan.video_items[0]
    assert item.source_start == F(1)
    assert item.source_end == F(11)


# ---------------------------------------------------------------- render range


def test_range_starting_mid_clip_trims_source_slice_exactly():
    p = two_video_tracks()
    p = add_clip(p, "a", "vtop", F(0), F(10), source_in=F(30))
    plan = build_plan(p, F(4), F(6))
    assert plan.total_frames == 50
    item = plan.video_items[0]
    assert item.start_frame == 0 and item.frames == 50
    assert item.source_start == F(34) and item.source_end == F(36)


def test_range_excludes_material_outside_it():
    p = two_video_tracks()
    p = add_clip(p, "early", "vtop", F(0), F(2))
    p = add_clip(p, "late", "vtop", F(8), F(2))
    plan = build_plan(p, F(3), F(8))
    assert plan.video_items[0].is_black
    assert all(i.clip is None for i in plan.video_items)
    assert plan.total_frames == 125


def test_invalid_ranges_rejected():
    p = two_video_tracks()
    with pytest.raises(RenderPlanError, match="non-negative"):
        build_plan(p, F(-1), F(5))
    with pytest.raises(RenderPlanError, match="before its start"):
        build_plan(p, F(5), F(4))


# -------------------------------------------------------------------- audio


def test_all_unmuted_audio_tracks_contribute_simultaneously():
    p = make_base()  # tracks vt + at
    p = p.with_tracks((*p.tracks, Track("at2", TrackKind.AUDIO, "A2")))
    p = add_clip(p, "a1", "at", F(0), F(6), asset="aa", stream=1)
    p = add_clip(p, "a2", "at2", F(2), F(6), asset="aa", stream=1)
    plan = build_plan(p)
    both = plan.audio_at(F(3))
    assert {i.clip.id for i in both} == {"a1", "a2"}
    assert plan.audio_at(F(1))[0].clip.id == "a1"
    assert plan.audio_at(F(7))[0].clip.id == "a2"
    assert plan.audio_at(F(8)) == ()  # past the last clip: silence
    assert plan.total_samples == 8 * 48000
    assert plan.output_end == F(8)
    # a hole between two contributions is still counted in total_samples
    p2 = RemoveClipsCommand(("a2",)).apply(p)
    p2 = add_clip(p2, "a3", "at2", F(7), F(2), asset="aa", stream=1)  # hole at [6,7)
    plan2 = build_plan(p2)
    assert plan2.audio_at(F(5))[0].clip.id == "a1"
    assert plan2.audio_at(F(13, 2)) == ()  # 6.5 s: the hole
    assert plan2.audio_at(F(15, 2))[0].clip.id == "a3"  # 7.5 s
    assert plan2.total_samples == 9 * 48000


def test_muted_track_excluded_but_length_kept():
    p = make_base()
    p = add_clip(p, "v1", "vt", F(0), F(8))
    p = add_clip(p, "a1", "at", F(0), F(4), asset="aa", stream=1)
    p = SetTrackMutedCommand("at", True).apply(p)
    plan = build_plan(p)
    assert plan.audio_items == ()
    assert plan.output_end == F(8) and plan.total_samples == 8 * 48000


def test_audio_sample_extent_is_contiguous_and_exact():
    p = make_base()
    p = add_clip(p, "a1", "at", F(0), F(1, 3), asset="aa", stream=1)
    p = add_clip(p, "a2", "at", F(1, 3), F(2, 3), asset="aa", stream=1)
    plan = build_plan(p, F(0), F(1))
    first, second = plan.audio_items
    assert first.end_sample == second.start_sample
    assert second.end_sample <= plan.total_samples


def test_fade_anchors_are_absolute_across_chunk_splits():
    """Chunked rendering must not restart or truncate a fade ramp."""
    p = make_base()
    p = add_clip(p, "a1", "at", F(0), F(10), asset="aa", stream=1)
    p = SetFadesCommand("a1", F(0), F(4)).apply(p)  # fade out over [6,10)
    whole = build_plan(p)
    chunk = build_plan(p, F(7), F(9))
    item = chunk.audio_items[0]
    assert item.clip.fade_out == F(4)
    assert item.clip.output_end == F(10)  # absolute anchor survives
    assert item.level_at(F(8)) == F(1, 2)  # same level as in the whole plan
    assert whole.audio_items[0].level_at(F(8)) == item.level_at(F(8))
    assert item.source_start == F(7) and item.source_end == F(9)


def test_gain_and_envelope_levels_match_domain_definition():
    p = make_base()
    p = add_clip(p, "a1", "at", F(0), F(4), asset="aa", stream=1)
    p = SetGainCommand("a1", F(1, 2)).apply(p)
    p = SetFadesCommand("a1", F(2), F(0)).apply(p)
    plan = build_plan(p)
    item = plan.audio_items[0]
    assert item.level_at(F(0)) == 0
    assert item.level_at(F(1)) == F(1, 4)  # half through a 2 s fade-in at 0.5 gain
    assert item.level_at(F(3)) == F(1, 2)


def test_overload_is_reported_not_hidden():
    p = make_base()
    p = p.with_tracks((*p.tracks, Track("at2", TrackKind.AUDIO, "A2")))
    p = add_clip(p, "a1", "at", F(0), F(4), asset="aa", stream=1)
    p = add_clip(p, "a2", "at2", F(0), F(4), asset="aa", stream=1)
    p = SetGainCommand("a1", F(3, 2)).apply(p)
    p = SetGainCommand("a2", F(3, 2)).apply(p)
    plan = build_plan(p)
    assert any("full scale" in w for w in plan.warnings)
    assert plan.peak_audio_level(F(2)) == F(3)


# ------------------------------------------------------------- failure paths


def test_unregistered_stream_is_refused():
    p = make_base()
    p = p.with_tracks((Track("vt", TrackKind.VIDEO, "V1"),))
    p = p.with_clips(
        (*p.clips, Clip("ghost", "nope", 7, "vt", F(0), F(5), F(0)))
    )
    with pytest.raises(RenderPlanError, match="not registered"):
        build_plan(p)


def test_unknown_bounds_stream_is_refused():
    p = Project()
    p = RegisterSourceCommand(StreamSource("bad", 0, TrackKind.VIDEO, F(0), None)).apply(p)
    p = p.with_tracks((Track("vt", TrackKind.VIDEO, "V1"),))
    # illegal to create through a command, so inject it to test the planner too
    p = p.with_clips((Clip("c", "bad", 0, "vt", F(0), F(10), F(0)),))
    with pytest.raises(RenderPlanError, match="bounds are"):
        build_plan(p)


def test_kind_mismatch_is_refused():
    p = make_base()
    p = p.with_clips((Clip("c", "aa", 1, "vt", F(0), F(5), F(0)),))
    with pytest.raises(RenderPlanError, match="audio stream on a video track"):
        build_plan(p)


def test_conservative_stream_end_is_warned():
    p = Project()
    p = RegisterSourceCommand(
        StreamSource("ts", 0, TrackKind.VIDEO, F(0), F(60), conservative_end=True,
                     frame_rate=F(25))
    ).apply(p)
    p = p.with_tracks((Track("vt", TrackKind.VIDEO, "V1"),))
    p = AddClipCommand("ts", 0, "vt", F(0), source_in=F(0), source_out=F(60)).apply(p)
    plan = build_plan(p)
    assert any("upper bound" in w for w in plan.warnings)
    # a clip that stops short of the uncertain end needs no warning
    p2 = RemoveClipsCommand(tuple(c.id for c in p.clips)).apply(p)
    p2 = AddClipCommand("ts", 0, "vt", F(0), source_in=F(0), source_out=F(30)).apply(p2)
    assert build_plan(p2).warnings == ()


# ------------------------------------------------------------ long timeline


def test_two_hour_project_plans_exactly():
    fps = F(30000, 1001)
    p = Project(output=OutputSettings(1280, 720, fps, 48000, "stereo"))
    p = RegisterSourceCommand(
        StreamSource("va", 0, TrackKind.VIDEO, F(0), F(80000), frame_rate=fps)
    ).apply(p)
    p = p.with_tracks(
        (
            Track("vtop", TrackKind.VIDEO, "Top"),
            Track("vbot", TrackKind.VIDEO, "Bottom"),
            Track("at", TrackKind.AUDIO, "A1"),
        )
    )
    for k in range(200):  # back-to-back 36 s clips: 2 hours
        p = add_clip(p, f"c{k}", "vtop", F(k * 36), F(36))
    plan = build_plan(p)
    assert plan.output_end == F(7200)
    # the same single rule at either end of a two-hour timeline: the frame that
    # covers each boundary, never a per-segment rounding
    assert plan.total_frames == Quantisation(fps, 48000).frame_count(F(7200)) == 215785
    assert sum(i.frames for i in plan.video_items) == plan.total_frames
    assert len(plan.video_items) == 200
    last = plan.video_items[-1]
    assert last.end_frame == plan.total_frames


# ------------------------------------------------------- geometry scaling (preview path)


def test_geometry_scale_changes_picture_size_only():
    project = add_clip(add_clip(make_base(), "v1", "vt", F(0), F(2)), "v2", "vt", F(2), F(2))
    full = build_plan(project, F(0), F(4))
    half = build_plan(project, F(0), F(4), geometry_scale=F(1, 2))
    # geometry comes from the project's output settings (1920x1080 here), not from
    # the 128x96 asset, and the scale halves that and nothing else
    assert (full.width, full.height) == (1920, 1080)
    assert (half.width, half.height) == (960, 540)
    assert half.total_frames == full.total_frames
    assert half.total_samples == full.total_samples
    assert half.quantisation == full.quantisation
    assert [(i.start_frame, i.frames, i.source_start, i.source_end) for i in half.video_items] == [
        (i.start_frame, i.frames, i.source_start, i.source_end) for i in full.video_items
    ]
    assert half.audio_items == full.audio_items


def test_preview_and_export_resolve_the_same_picture_per_frame():
    project = add_clip(add_clip(make_base(), "a", "vt", F(0), F(2)), "b", "vt", F(2), F(2))
    full = build_plan(project, F(0), F(4))
    small = build_plan(project, F(0), F(4), geometry_scale=F(1, 4))
    for t in (F(0), F(199999, 100000), F(2), F(3, 2), F(4) - F(1, 25)):
        assert small.picture_at(t).clip.id == full.picture_at(t).clip.id, t
    assert small.warnings == full.warnings


def test_odd_output_sizes_become_even_and_say_so():
    project = make_base()
    project = replace(project, output=OutputSettings(333, 222, T23976, 48000, "stereo"))
    plan = build_plan(project, F(0), F(1))
    assert (plan.width, plan.height) == (332, 222)
    assert any("even dimensions" in w for w in plan.warnings), plan.warnings


def test_geometry_scale_has_a_floor_and_must_be_positive():
    project = make_base()
    plan = build_plan(project, F(0), F(1), geometry_scale=F(1, 1000))
    assert (plan.width, plan.height) == (16, 16)
    for bad in (F(0), F(-1, 2)):
        with pytest.raises(RenderPlanError, match=r"scale .* must be positive"):
            build_plan(project, F(0), F(1), geometry_scale=bad)


def test_invalid_geometry_is_refused_not_guessed():
    with pytest.raises(ValueError, match=r"dimensions must be positive"):
        OutputSettings(0, 720, F(25), 48000, "stereo")  # the model guards first
    with pytest.raises(RenderPlanError, match=r"Output size .* must be positive"):
        plan_geometry(0, 720)
    with pytest.raises(RenderPlanError, match=r"scale .* must be positive"):
        plan_geometry(640, 360, F(-1))


def test_scaled_odd_dimensions_round_half_up_then_to_even():
    width, height, warning = plan_geometry(1001, 1001, F(1, 2))
    assert (width, height) == (500, 500)  # 500.5 -> 501 -> 500, documented
    assert warning is not None and "500x500" in warning and "even dimensions" in warning

    width, height, warning = plan_geometry(1280, 720, F(1, 3))
    assert (width, height) == (426, 240)  # 426.67 -> 427 -> 426
    assert warning is not None and "426x240" in warning

    assert plan_geometry(1280, 720, F(1, 4)) == (320, 180, None)  # exact: no warning
