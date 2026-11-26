"""Command tests: atomicity, overlap rules, split/trim/rate math, links."""

import pytest

from core_helpers import T23976, F, add_clip, make_base, video_source
from video_editor.core.commands import (
    AddClipCommand,
    AddTrackCommand,
    CommandError,
    DuplicateClipCommand,
    DuplicateClipsCommand,
    ImportGroupCommand,
    InsertClipCommand,
    LinkClipsCommand,
    MoveClipCommand,
    RegisterSourceCommand,
    RemoveClipsCommand,
    RemoveTrackCommand,
    RenameTrackCommand,
    ReorderTrackCommand,
    SetFadesCommand,
    SetGainCommand,
    SetRateCommand,
    SetTrackEnabledCommand,
    SetTrackMutedCommand,
    SplitClipCommand,
    TrimClipCommand,
    TrimEdge,
    UnlinkClipCommand,
    validate_project,
)
from video_editor.core.history import ProjectSession
from video_editor.core.model import Project, StreamSource, TrackKind

# -------------------------------------------------------------------- sources


def test_register_source_and_duplicate_rejected():
    base = make_base()
    again = RegisterSourceCommand(video_source("va"))
    assert again.apply(base) == base  # identical registration is a no-op
    different = RegisterSourceCommand(video_source("va", end=F(50)))
    with pytest.raises(CommandError):
        different.apply(base)


def test_unplaceable_stream_rejected():
    p = Project()
    p = RegisterSourceCommand(StreamSource("x", 0, TrackKind.VIDEO, F(0), None)).apply(p)
    p = AddTrackCommand(TrackKind.VIDEO, "V1").apply(p)
    with pytest.raises(CommandError, match="unknown or empty bounds"):
        AddClipCommand("x", 0, p.tracks[0].id, F(0)).apply(p)


# --------------------------------------------------------------------- tracks


def test_track_add_reorder_rename():
    base = make_base()
    p = AddTrackCommand(TrackKind.AUDIO, "A2", index=0).apply(base)
    assert p.tracks[0].name == "A2"
    p = ReorderTrackCommand(p.tracks[0].id, 2).apply(p)
    assert p.tracks[2].name == "A2"
    p = RenameTrackCommand(p.tracks[0].id, "Picture").apply(p)
    assert p.track(p.tracks[0].id).name == "Picture"
    with pytest.raises(CommandError):
        ReorderTrackCommand(p.tracks[0].id, 99).apply(p)
    with pytest.raises(CommandError):
        RenameTrackCommand(p.tracks[0].id, "  ").apply(p)


def test_remove_populated_track_cascades():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    p2 = RemoveTrackCommand("vt").apply(p)
    assert p2.tracks == tuple(t for t in p.tracks if t.id != "vt")
    assert all(c.track_id != "vt" for c in p2.clips)
    assert validate_project(p2) == []


def test_mute_only_for_audio():
    base = make_base()
    with pytest.raises(CommandError):
        SetTrackMutedCommand("vt", True).apply(base)
    p = SetTrackMutedCommand("at", True).apply(base)
    assert p.track("at").muted
    p = SetTrackEnabledCommand("vt", False).apply(p)
    assert not p.track("vt").enabled


# ---------------------------------------------------------------------- clips


def test_add_clip_whole_stream_default_interval():
    base = make_base()
    p = AddClipCommand("va", 0, "vt", F(0)).apply(base)
    c = p.clips[0]
    assert (c.source_in, c.source_out) == (F(0), F(100))


def test_add_clip_rejects_kind_mismatch_and_bounds():
    base = make_base()
    with pytest.raises(CommandError, match="video but track A1 is audio"):
        AddClipCommand("va", 0, "at", F(0)).apply(base)
    with pytest.raises(CommandError, match="exceeds the stream end"):
        AddClipCommand("va", 0, "vt", F(0), source_out=F(101)).apply(base)


def test_source_in_before_origin_rejected():
    p = Project()
    src = StreamSource("va", 0, TrackKind.VIDEO, F(2), F(50))  # nonzero origin
    p = RegisterSourceCommand(src).apply(p)
    p = AddTrackCommand(TrackKind.VIDEO, "V1").apply(p)
    tid = p.tracks[0].id
    with pytest.raises(CommandError, match="before the stream origin"):
        AddClipCommand("va", 0, tid, F(0), source_in=F(1), source_out=F(10)).apply(p)
    ok = AddClipCommand("va", 0, tid, F(0), source_in=F(2), source_out=F(10)).apply(p)
    assert validate_project(ok) == []


def test_an_offset_stream_gets_its_measured_end_as_its_bound():
    """The legal selection stops where the material does, the rest is refused.

    Numbers from ``make_identity_offset`` (TASK_12e, measured): 100 pictures at
    25 fps whose presentation starts at 5/2 s and whose last frame ends at 13/2 s.
    A clip inside that range is legal; one that reaches 15/2 s asks for a second
    of picture the file does not hold, so it is refused here - before any render,
    before any publication - rather than being filled with a held frame.
    """
    p = Project()
    src = StreamSource(
        "va", 0, TrackKind.VIDEO, F(5, 2), F(13, 2), conservative_end=True,
        frame_rate=F(25),
    )
    p = RegisterSourceCommand(src).apply(p)
    p = AddTrackCommand(TrackKind.VIDEO, "V1").apply(p)
    tid = p.tracks[0].id
    ok = AddClipCommand("va", 0, tid, F(0), source_in=F(7, 2), source_out=F(13, 2)).apply(p)
    assert validate_project(ok) == []
    with pytest.raises(CommandError, match=r"exceeds the stream end \(13/2\)"):
        AddClipCommand("va", 0, tid, F(0), source_in=F(7, 2), source_out=F(15, 2)).apply(p)


def test_same_track_overlap_rejected_different_track_allowed():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    with pytest.raises(CommandError, match="Overlaps clip c1"):
        add_clip(p, "c2", "vt", F(9), F(5))
    p2 = add_clip(p, "c3", "at", F(9), F(5), asset="aa", stream=1)
    assert validate_project(p2) == []  # intentional cross-track overlap


def test_touching_intervals_allowed():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    p = add_clip(p, "c2", "vt", F(10), F(5))
    assert validate_project(p) == []


def test_move_no_ripple_and_gaps_preserved():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    p = add_clip(p, "c2", "vt", F(20), F(10))  # gap [10,20)
    p = MoveClipCommand("c2", output_start=F(12)).apply(p)
    clips = {c.id: c for c in p.clips}
    assert clips["c1"].output_start == F(0)  # untouched
    assert clips["c2"].output_start == F(12)
    assert clips["c2"].output_end == F(22)


def test_move_invalid_overlap_atomic():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    p = add_clip(p, "c2", "vt", F(20), F(10))
    before = p
    with pytest.raises(CommandError):
        MoveClipCommand("c2", output_start=F(5)).apply(p)
    assert p == before  # unchanged


def test_move_to_incompatible_track_rejected():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    with pytest.raises(CommandError, match="Cannot move video clip onto audio track"):
        MoveClipCommand("c1", track_id="at").apply(p)


def test_move_negative_time_rejected():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(5), F(10))
    with pytest.raises(CommandError):
        MoveClipCommand("c1", output_start=F(-1)).apply(p)


def test_move_to_compatible_second_track():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    p = AddTrackCommand(TrackKind.VIDEO, "V2").apply(p)
    second = next(t for t in p.tracks if t.name == "V2").id
    p = MoveClipCommand("c1", track_id=second).apply(p)
    assert p.clip("c1").track_id == second
    assert p.clip("c1").output_start == F(0)


def test_duplicate_to_explicit_destination():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    p = DuplicateClipCommand("c1", output_start=F(15)).apply(p)
    assert len(p.clips) == 2
    clone = next(c for c in p.clips if c.id != "c1")
    assert clone.output_start == F(15) and clone.source_in == F(0)
    with pytest.raises(CommandError):
        DuplicateClipCommand("c1", output_start=F(1)).apply(p)  # overlaps original


def test_remove_clips_leaves_gaps():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    p = add_clip(p, "c2", "vt", F(10), F(10))
    p = add_clip(p, "c3", "vt", F(20), F(10))
    p = RemoveClipsCommand(("c2",)).apply(p)
    assert {c.id for c in p.clips} == {"c1", "c3"}
    assert p.clips_on_track("vt")[1].output_start == F(20)  # gap preserved


# ----------------------------------------------------------------- split math


def test_split_exact_fractional_position():
    base = make_base()
    # 2 output seconds of 24000/1001 material, split at 1/3 s
    p = add_clip(base, "c1", "vt", F(0), F(2), rate=T23976)
    p = SplitClipCommand("c1", F(1, 3)).apply(p)
    left = p.clip("c1")
    right = next(c for c in p.clips if c.id != "c1")
    expected_split = T23976 * F(1, 3)
    assert left.source_out == expected_split
    assert right.source_in == expected_split
    assert left.output_end == right.output_start == F(1, 3)
    assert left.rate == right.rate == T23976
    assert left.id == "c1"  # left keeps the original id
    assert validate_project(p) == []


def test_split_boundaries_rejected():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    for boundary in (F(0), F(10), F(-1), F(11)):
        with pytest.raises(CommandError):
            SplitClipCommand("c1", boundary).apply(p)


def test_split_nonzero_origin_source_axis():
    p = Project()
    p = RegisterSourceCommand(StreamSource("va", 0, TrackKind.VIDEO, F(5), F(105))).apply(p)
    p = AddTrackCommand(TrackKind.VIDEO, "V1").apply(p)
    tid = p.tracks[0].id
    p = AddClipCommand("va", 0, tid, F(0), source_in=F(5), source_out=F(25)).apply(p)
    clip_id = p.clips[0].id
    p = SplitClipCommand(clip_id, F(10)).apply(p)
    left = p.clips[0]
    right = p.clips[1]
    assert left.source_out == F(15)
    assert right.source_in == F(15)
    assert right.source_out == F(25)


# ----------------------------------------------------------------- trim math


def test_trim_left_preserves_output_end_and_source_out():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(10), F(10))  # out [10,20), src [0,10)
    p = TrimClipCommand("c1", TrimEdge.LEFT, F(4)).apply(p)
    c = p.clip("c1")
    assert c.source_in == F(4)
    assert c.source_out == F(10)  # kept
    assert c.output_end == F(20)  # kept
    assert c.output_start == F(14)  # shifted right


def test_trim_right_preserves_start_and_source_in():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(10), F(10))
    p = TrimClipCommand("c1", TrimEdge.RIGHT, F(3)).apply(p)
    c = p.clip("c1")
    assert c.source_out == F(3)
    assert c.source_in == F(0)
    assert c.output_start == F(10)
    assert c.output_end == F(13)


def test_trim_within_rate_scaling_exact():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(4), rate=T23976)
    p = TrimClipCommand("c1", TrimEdge.RIGHT, T23976 * F(2)).apply(p)
    c = p.clip("c1")
    assert c.output_duration == F(2)
    assert c.output_end == F(2)


def test_trim_extension_beyond_source_bounds_rejected():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10), source_in=F(20))
    # source interval [20,30): trimming the LEFT edge past source_out is invalid
    with pytest.raises(CommandError, match="cross its far edge"):
        TrimClipCommand("c1", TrimEdge.LEFT, F(100)).apply(p)


def test_trim_inward_and_outward_within_stream_bounds():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10), source_in=F(40))  # src [40,50)
    # extend right edge to reclaim material up to the stream end
    p = TrimClipCommand("c1", TrimEdge.RIGHT, F(99)).apply(p)
    assert p.clip("c1").source_out == F(99)
    assert p.clip("c1").output_end == F(59)
    # beyond stream end rejected
    with pytest.raises(CommandError, match="outside source bounds"):
        TrimClipCommand("c1", TrimEdge.RIGHT, F(101)).apply(p)


def test_trim_extension_blocked_by_overlap_atomic():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10), source_in=F(40))  # src [40,50)
    p = add_clip(p, "c2", "vt", F(15), F(5))
    before = p
    with pytest.raises(CommandError, match="Overlaps"):
        TrimClipCommand("c1", TrimEdge.RIGHT, F(56)).apply(p)  # output would reach 16
    assert p == before
    # trimming to exactly the neighbour boundary is allowed (half-open)
    p2 = TrimClipCommand("c1", TrimEdge.RIGHT, F(55)).apply(p)
    assert p2.clip("c1").output_end == F(15)


def test_trim_to_zero_length_rejected():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))  # src [0,10)
    with pytest.raises(CommandError, match="cross its far edge"):
        TrimClipCommand("c1", TrimEdge.RIGHT, F(0)).apply(p)
    with pytest.raises(CommandError, match="cross its far edge"):
        TrimClipCommand("c1", TrimEdge.LEFT, F(10)).apply(p)


# ----------------------------------------------------------------- fade rules


def test_fade_validation():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    with pytest.raises(CommandError, match="exceed the clip"):
        SetFadesCommand("c1", F(6), F(6)).apply(p)
    p = SetFadesCommand("c1", F(2), F(3)).apply(p)
    assert p.clip("c1").fade_in == F(2)


def test_split_cannot_cut_through_fade():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    p = SetFadesCommand("c1", F(4), F(4)).apply(p)
    with pytest.raises(CommandError, match="fade-in"):
        SplitClipCommand("c1", F(2)).apply(p)
    with pytest.raises(CommandError, match="fade-out"):
        SplitClipCommand("c1", F(8)).apply(p)
    # outside the fades works; envelopes follow their edge
    p2 = SplitClipCommand("c1", F(5)).apply(p)
    left = p2.clip("c1")
    right = next(c for c in p2.clips if c.id != "c1")
    assert (left.fade_in, left.fade_out) == (F(4), F(0))
    assert (right.fade_in, right.fade_out) == (F(0), F(4))


def test_trim_cannot_cut_through_fade():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    p = SetFadesCommand("c1", F(0), F(4)).apply(p)
    with pytest.raises(CommandError, match="fade-out"):
        TrimClipCommand("c1", TrimEdge.RIGHT, F(8)).apply(p)
    p2 = TrimClipCommand("c1", TrimEdge.RIGHT, F(6)).apply(p)
    assert p2.clip("c1").fade_out == F(4)
    # fades must still fit after trim
    p3 = add_clip(base, "c2", "vt", F(20), F(10))
    p3 = SetFadesCommand("c2", F(3), F(3)).apply(p3)
    with pytest.raises(CommandError):
        TrimClipCommand("c2", TrimEdge.LEFT, F(7)).apply(p3)  # new dur 3 < fades 6


# ---------------------------------------------------------------------- rate


def test_set_rate_keeps_source_and_output_start():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(3), F(10))
    p = SetRateCommand("c1", F(2)).apply(p)
    c = p.clip("c1")
    assert (c.source_in, c.source_out) == (F(0), F(10))
    assert c.output_start == F(3)
    assert c.output_duration == F(5)  # 10 source seconds / rate 2


def test_set_rate_rational_pulldown():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(1), rate=T23976)  # 23.976 src sec in 1 out sec
    p = SetRateCommand("c1", F(24)).apply(p)
    c = p.clip("c1")
    assert c.output_duration == T23976 / 24
    assert c.output_duration < 1  # slightly shorter than 1 s


def test_set_rate_overlap_rejected_atomically():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    p = add_clip(p, "c2", "vt", F(12), F(2))
    before = p
    with pytest.raises(CommandError, match="Overlaps"):
        SetRateCommand("c1", F(1, 2)).apply(p)  # would extend c1 to 20
    assert p == before


def test_set_rate_invalid_values():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    for bad in (F(0), F(-1)):
        with pytest.raises(CommandError):
            SetRateCommand("c1", bad).apply(p)


def test_gain_bounds():
    base = make_base()
    p = add_clip(base, "c1", "vt", F(0), F(10))
    p = SetGainCommand("c1", F(3, 4)).apply(p)
    assert p.clip("c1").gain == F(3, 4)
    with pytest.raises(CommandError):
        SetGainCommand("c1", F(-1)).apply(p)


# --------------------------------------------------------------------- links


def linked_pair_project():
    p = make_base()
    p = add_clip(p, "v1", "vt", F(0), F(10))
    p = add_clip(p, "a1", "at", F(0), F(10), asset="aa", stream=1)
    p = LinkClipsCommand(("v1", "a1")).apply(p)
    return p


def test_link_and_unlink():
    base = make_base()
    p = add_clip(base, "v1", "vt", F(0), F(10))
    p = add_clip(p, "a1", "at", F(0), F(10), asset="aa", stream=1)
    p = LinkClipsCommand(("v1", "a1")).apply(p)
    assert p.clip("v1").link_group == p.clip("a1").link_group
    with pytest.raises(CommandError, match="already linked"):
        LinkClipsCommand(("v1", "a1")).apply(p)
    with pytest.raises(CommandError):
        LinkClipsCommand(("v1",)).apply(p)
    p = UnlinkClipCommand("v1").apply(p)
    assert p.clip("v1").link_group is None
    assert p.clip("a1").link_group is None  # group of one dissolves


def test_link_video_only_rejected():
    base = make_base()
    p2 = AddTrackCommand(TrackKind.VIDEO, "V2").apply(base)
    vtrack2 = next(t for t in p2.tracks if t.name == "V2").id
    p = add_clip(p2, "v1", "vt", F(0), F(10))
    p = add_clip(p, "v2", vtrack2, F(0), F(10))
    with pytest.raises(CommandError, match="video and an audio"):
        LinkClipsCommand(("v1", "v2")).apply(p)


def test_linked_move_applies_to_all_members():
    p = linked_pair_project()
    p = MoveClipCommand("v1", output_start=F(5)).apply(p)
    assert p.clip("v1").output_start == F(5)
    assert p.clip("a1").output_start == F(5)


def test_linked_move_conflict_rejects_whole_transaction():
    p = linked_pair_project()
    p = add_clip(p, "blocker", "at", F(12), F(2), asset="aa", stream=1)
    before = p
    with pytest.raises(CommandError):
        MoveClipCommand("v1", output_start=F(7)).apply(p)  # a1 [7,17) hits blocker
    assert p == before


def test_linked_split_splits_all_members_atomic():
    """Left pieces keep the old link, right pieces share one new one.

    This replaced an earlier rule that left all four halves in a single group.
    That was observable and wrong: moving the right-hand picture dragged its
    left-hand partner, and splitting the right pair again was refused because
    the left pieces did not contain the second split point.
    """
    p = linked_pair_project()
    before = p
    original_group = p.clip("v1").link_group
    assert original_group is not None
    p = SplitClipCommand("v1", F(4)).apply(p)
    assert len(p.clips) == 4
    left = [clip for clip in p.clips if clip.output_start == F(0)]
    right = [clip for clip in p.clips if clip.output_start == F(4)]
    assert {clip.id for clip in left} == {"v1", "a1"}  # original ids preserved
    assert {clip.link_group for clip in left} == {original_group}
    right_groups = {clip.link_group for clip in right}
    assert len(right_groups) == 1 and None not in right_groups
    assert right_groups != {original_group}
    # the halves are exact: no drift in times or rates
    for clip in left + right:
        assert clip.rate == F(1)
    assert all(clip.output_duration == F(6) for clip in right)
    # and the right pair really moves as its own unit
    moved = MoveClipCommand(right[0].id, output_start=F(12)).apply(p)
    assert all(clip.output_start == F(12) for clip in moved.clips
               if clip.link_group in right_groups)
    assert all(clip.output_start == F(0) for clip in moved.clips if clip.id in ("v1", "a1"))
    # a second split of the right pair is not refused
    again = SplitClipCommand(right[0].id, F(7)).apply(p)
    assert len(again.clips) == 6
    # undo restores both group identities exactly
    assert SplitClipCommand("v1", F(4)).describe() == "split clip"
    session = ProjectSession(before)
    session.apply(SplitClipCommand("v1", F(4)))
    session.undo()
    assert session.project == before
    session.redo()
    after = session.project
    assert {clip.link_group for clip in after.clips if clip.output_start == F(0)} == {
        original_group
    }


def test_linked_split_requires_all_members_to_cover_time():
    p = make_base()
    p = add_clip(p, "v1", "vt", F(0), F(10))
    p = add_clip(p, "a1", "at", F(6), F(4), asset="aa", stream=1)
    p = LinkClipsCommand(("v1", "a1")).apply(p)
    before = p
    with pytest.raises(CommandError, match="outside clip a1"):
        SplitClipCommand("v1", F(3)).apply(p)  # a1 starts at 6
    assert p == before


def test_linked_track_change_requires_unlink():
    p = linked_pair_project()
    p2 = AddTrackCommand(TrackKind.VIDEO, "V2").apply(p)
    second = next(t for t in p2.tracks if t.name == "V2").id
    with pytest.raises(CommandError, match="unlink first"):
        MoveClipCommand("v1", track_id=second).apply(p2)


def test_linked_rate_applies_to_group():
    p = linked_pair_project()
    p = SetRateCommand("v1", F(2)).apply(p)
    assert p.clip("v1").rate == F(2)
    assert p.clip("a1").rate == F(2)


def test_unlinked_clips_edit_independently():
    base = make_base()
    p = add_clip(base, "v1", "vt", F(0), F(10))
    p = add_clip(p, "a1", "at", F(0), F(10), asset="aa", stream=1)
    p = MoveClipCommand("a1", output_start=F(30)).apply(p)
    assert p.clip("v1").output_start == F(0)


def test_remove_one_linked_clip_does_not_propagate():
    p = linked_pair_project()
    p = RemoveClipsCommand(("a1",)).apply(p)
    assert {c.id for c in p.clips} == {"v1"}


# ---------------------------------------------------------- grouped import


def test_grouped_import_preserves_stream_offsets():
    p = Project()
    p = RegisterSourceCommand(StreamSource("ed", 0, TrackKind.VIDEO, F(0), F(60))).apply(p)
    p = RegisterSourceCommand(StreamSource("ed", 1, TrackKind.AUDIO, F(0), F(60))).apply(p)
    p = RegisterSourceCommand(StreamSource("ed", 2, TrackKind.AUDIO, F("1.5"), F(60))).apply(p)
    p = ImportGroupCommand("ed", (0, 1, 2), F(0)).apply(p)
    assert len(p.tracks) == 3
    by_stream = {c.stream_index: c for c in p.clips}
    assert by_stream[0].output_start == F(0)
    assert by_stream[1].output_start == F(0)
    assert by_stream[2].output_start == F("1.5")  # offset preserved exactly
    assert by_stream[2].source_in == F("1.5")
    assert validate_project(p) == []


def test_grouped_import_into_existing_tracks():
    base = make_base()
    p = ImportGroupCommand("va", (0,), F(30), track_ids=("vt",)).apply(base)
    assert p.clips[0].track_id == "vt"
    assert p.clips[0].output_start == F(30)
    # overlapping group import onto the same track fails atomically
    before = p
    with pytest.raises(CommandError):
        ImportGroupCommand("va", (0,), F(10), track_ids=("vt",)).apply(p)
    assert p == before


def test_grouped_import_anchored_at_min_origin():
    p = Project()
    p = RegisterSourceCommand(StreamSource("d", 0, TrackKind.VIDEO, F(1), F(60))).apply(p)
    p = RegisterSourceCommand(StreamSource("d", 1, TrackKind.AUDIO, F(0), F(60))).apply(p)
    p = ImportGroupCommand("d", (0, 1), F(7)).apply(p)
    by = {c.stream_index: c for c in p.clips}
    assert by[1].output_start == F(7)  # earliest origin anchors the group
    assert by[0].output_start == F(8)  # video origin 1 => +1 offset


def test_grouped_import_kind_mismatch_target_track():
    base = make_base()
    with pytest.raises(CommandError, match="Target track"):
        ImportGroupCommand("va", (0,), F(0), track_ids=("at",)).apply(base)


# ------------------------------------------------ mixed-source composition


def test_picture_from_a_sound_from_b_independent_edits():
    """Core acceptance: A picture + B sound, edited independently."""
    p = make_base()  # va video, aa audio
    p = ImportGroupCommand("va", (0,), F(0), track_ids=("vt",)).apply(p)
    p = add_clip(p, "bsound", "at", F(2), F(15), asset="aa", stream=1)
    vid = p.clips[0]
    # move sound independently, no ripple on picture
    p = MoveClipCommand("bsound", output_start=F(30)).apply(p)
    assert p.clip(vid.id).output_start == F(0)
    assert p.clip("bsound").output_start == F(30)
    # split only the sound
    p = SplitClipCommand("bsound", F(35)).apply(p)
    assert len(p.clips) == 3
    # change rate of only the sound
    p = SetRateCommand("bsound", F(2)).apply(p)
    assert p.clip(vid.id).rate == F(1)
    assert p.clip("bsound").rate == F(2)
    assert validate_project(p) == []


# ------------------------------------------------------------- duplicate block


def duplicated_block(p: Project) -> Project:
    p = add_clip(p, "v1", "vt", F(0), F(10))
    p = add_clip(p, "v2", "vt", F(20), F(10))
    p = add_clip(p, "a1", "at", F(0), F(10), asset="aa", stream=1)
    return p


def test_duplicate_block_shifts_uniformly():
    p = duplicated_block(make_base())
    before = set(p.clips)
    q = DuplicateClipsCommand(("v1", "v2"), F(45)).apply(p)
    clones = [c for c in q.clips if c.id not in {c.id for c in before}]
    assert len(clones) == 2
    assert sorted(c.output_interval for c in clones) == [(F(45), F(55)), (F(65), F(75))]
    # originals untouched, clone ids are fresh and distinct
    for original in before:
        assert q.clip(original.id) == original
    clone_ids = {c.id for c in clones}
    assert len(clone_ids) == 2
    assert clone_ids.isdisjoint({c.id for c in before})
    assert validate_project(q) == []


def test_duplicate_block_regroups_links():
    p = duplicated_block(make_base())
    p = LinkClipsCommand(("v1", "a1")).apply(p)
    group = p.clip("v1").link_group
    q = DuplicateClipsCommand(("v1", "a1"), F(30)).apply(p)
    clone_v = next(c for c in q.clips if c.id not in {c.id for c in p.clips} and c.track_id == "vt")
    clone_a = next(c for c in q.clips if c.id not in {c.id for c in p.clips} and c.track_id == "at")
    assert clone_v.link_group == clone_a.link_group != group
    assert p.clip("v1").link_group == group  # originals keep their own group
    assert len(q.linked_clips(clone_v.id)) == 2
    assert validate_project(q) == []


def test_duplicate_block_rejects_bad_destination():
    p = duplicated_block(make_base())
    with pytest.raises(CommandError, match="at least one clip"):
        DuplicateClipsCommand((), F(10)).apply(p)
    with pytest.raises(CommandError, match="equals the source position"):
        DuplicateClipsCommand(("v1",), F(0)).apply(p)
    with pytest.raises(CommandError, match="non-negative"):
        DuplicateClipsCommand(("v1",), F(-5)).apply(p)
    # landing on top of an existing clip on the same track: refused, no change
    with pytest.raises(CommandError, match=r"[Oo]verlap"):
        DuplicateClipsCommand(("v1",), F(25)).apply(p)
    assert len(p.clips) == 3


def test_duplicate_block_across_tracks_keeps_track_ids():
    p = duplicated_block(make_base())
    q = DuplicateClipsCommand(("v1", "a1"), F(50)).apply(p)
    clones = [c for c in q.clips if c.id not in {c.id for c in p.clips}]
    assert {c.track_id for c in clones} == {"vt", "at"}


# ----------------------------------------------------------------- ripple insert


def test_insert_shifts_only_later_clips_of_target_track():
    p = duplicated_block(make_base())
    q = InsertClipCommand("va", 0, "vt", F(12), source_in=F(0), source_out=F(6)).apply(p)
    assert q.clip("v1").output_interval == (F(0), F(10))  # earlier: untouched
    assert q.clip("v2").output_interval == (F(26), F(36))  # later: shifted by 6
    assert q.clip("a1").output_interval == (F(0), F(10))  # other track: untouched
    inserted = next(c for c in q.clips if c.id not in {c.id for c in p.clips})
    assert inserted.output_interval == (F(12), F(18))
    assert validate_project(q) == []


def test_insert_at_gap_boundary_uses_fractional_ripple():
    """Rate-aware ripple: 3 source seconds at 24000/1001 shift later clips exactly."""
    p = duplicated_block(make_base())
    q = InsertClipCommand(
        "va", 0, "vt", F(10), source_in=F(0), source_out=F(3), rate=T23976
    ).apply(p)
    shift = F(3) / T23976
    assert q.clip("v2").output_start == F(20) + shift
    assert q.output_end() == F(30) + shift
    assert validate_project(q) == []


def test_insert_rejects_position_inside_existing_clip():
    p = duplicated_block(make_base())
    with pytest.raises(CommandError, match="inside clip v1"):
        InsertClipCommand("va", 0, "vt", F(5), source_in=F(0), source_out=F(2)).apply(p)
    assert len(p.clips) == 3


def test_insert_shifts_linked_mates_as_a_block():
    p = duplicated_block(make_base())
    p = LinkClipsCommand(("v2", "a1")).apply(p)
    # a1 [0,10) must not collide with anything on at after the shift
    q = InsertClipCommand("va", 0, "vt", F(20), source_in=F(0), source_out=F(4)).apply(p)
    assert q.clip("v2").output_interval == (F(24), F(34))
    assert q.clip("a1").output_interval == (F(4), F(14))
    assert validate_project(q) == []


def test_insert_conflict_after_shift_rejects_whole_transaction():
    """A linked mate shifted onto occupied material must abort the insert."""
    p = duplicated_block(make_base())
    p = LinkClipsCommand(("v2", "a1")).apply(p)
    p = add_clip(p, "a2", "at", F(12), F(10), asset="aa", stream=1)  # at [12,22)
    with pytest.raises(CommandError, match=r"[Oo]verlap"):
        InsertClipCommand("va", 0, "vt", F(20), source_in=F(0), source_out=F(4)).apply(p)
    assert len(p.clips) == 4
    assert p.clip("v2").output_interval == (F(20), F(30))
    assert p.clip("a1").output_interval == (F(0), F(10))


def test_negative_positions_are_command_errors_not_valueerrors():
    """The UI only catches CommandError: model invariants must not leak out."""
    p = duplicated_block(make_base())
    with pytest.raises(CommandError, match="non-negative"):
        AddClipCommand("va", 0, "vt", F(-1)).apply(p)
    with pytest.raises(CommandError, match="non-negative"):
        InsertClipCommand("va", 0, "vt", F(-1)).apply(p)
    with pytest.raises(CommandError, match="non-negative"):
        DuplicateClipCommand("v1", F(-1)).apply(p)
    with pytest.raises(CommandError, match="non-negative"):
        DuplicateClipsCommand(("v1",), F(-1)).apply(p)
    assert len(p.clips) == 3


def test_insert_kind_mismatch_and_bad_bounds_rejected():
    p = duplicated_block(make_base())
    with pytest.raises(CommandError, match="audio"):
        InsertClipCommand("aa", 1, "vt", F(0)).apply(p)
    with pytest.raises(CommandError, match="non-empty"):
        InsertClipCommand("va", 0, "vt", F(0), source_in=F(5), source_out=F(5)).apply(p)
    with pytest.raises(CommandError, match=r"origin|bounds"):
        InsertClipCommand("va", 0, "vt", F(0), source_in=F(-1), source_out=F(5)).apply(p)
    assert len(p.clips) == 3


def test_insert_into_empty_track_creates_no_shift():
    p = make_base()
    q = InsertClipCommand("va", 0, "vt", F(7), source_in=F(0), source_out=F(4)).apply(p)
    assert [c.output_interval for c in q.clips] == [(F(7), F(11))]
