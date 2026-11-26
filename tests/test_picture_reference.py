"""The picture reference checked against arithmetic done by hand.

Nothing here renders anything: these tests exist to prove the *reference* of
``picture_reference.py`` answers the fixed contract correctly, so that when a
render disagrees with it the disagreement is evidence about the render. Every
expectation below is worked out in the docstring from the fixture's measured
presentation times - never read back out of the reference, and never out of the
renderer.

Hand-worked tables use the identity fixtures, whose picture *N* carries the
number *N* (``media_fixtures.identity_rgb``), measured once per file by
``media_fixtures.picture_facts``:

``identity.mkv`` (150 pictures)   PTS of picture n = n/25, 0 .. 149/25 = 5.96 s
``identity_offset.mkv`` (100)     PTS of picture n = 5/2 + n/25, 2.5 .. 323/50 = 6.46 s
``identity_vfr.mkv`` (150)        0 .. 99/25 at 1/25, then 4, 4083/1000, 4167/1000,
                                  17/4, ... measured spacings 83/1000 and 21/250
"""

import itertools
from fractions import Fraction as F
from pathlib import Path

import pytest

from media_fixtures import (
    HAS_FFMPEG,
    make_identity_offset,
    make_identity_vfr,
    make_identity_video,
    picture_facts,
)
from picture_reference import (
    BLACK_MISMATCH,
    HEAD_HOLD,
    OUTSIDE_SLICE_MISMATCH,
    PICK,
    RETIMING_MISMATCH,
    TAIL_HOLD,
    TAIL_HOLD_MISMATCH,
    DecodedStream,
    EmptySliceError,
    OverlapError,
    PictureReference,
    PlacedClip,
    compare,
    output_frames,
    path_disagreements,
    range_frame_indices,
)

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")

RATE = F(25)
ZERO = F(0)


def stream_of(path: Path) -> DecodedStream:
    """The reference's only knowledge of a file: its decoded PTS and identities."""
    facts = picture_facts(path)
    return DecodedStream(pts=facts.pictures, identities=facts.identities,
                         time_base=facts.time_base)


def reference(tmp_path: Path, clips: tuple[PlacedClip, ...], count: int = 150,
              maker=make_identity_video) -> PictureReference:
    return PictureReference(stream_of(maker(tmp_path, count=count)), RATE, clips)


# ------------------------------------------------------------- the output lattice


def test_the_frame_lattice_is_the_ceiling_of_exact_output_time():
    """Range edges snap forward; the arithmetic is here, not in a plan.

    ``[1/3, 1)`` at 25 fps: ``ceil(25/3) = 9`` and ``ceil(25) = 25``, so frames
    9..24 - 16 frames, and frame 9 is the first one at or after the cut.
    ``[68/25, 136/25)``: 68 and 136 exactly, so 68 frames - the split the
    range-vs-whole comparison uses. A length under one frame still needs one
    frame (``ceil(1/1000 * 25) = 1``) and a length of zero needs none.
    """
    assert range_frame_indices(F(1, 3), F(1), RATE) == (9, 25)
    assert range_frame_indices(F(68, 25), F(136, 25), RATE) == (68, 136)
    assert range_frame_indices(ZERO, F(1, 1000), RATE) == (0, 1)
    assert range_frame_indices(F(1), F(1), RATE) == (0, 0)
    assert output_frames(F(5), RATE) == 125
    assert output_frames(F(5) / F(25, 24), RATE) == 120  # 4.8 s at 25 fps
    assert output_frames(F(5) / F(24, 25), RATE) == 131  # 5.2083.. s, ceil
    # two ranges that meet share no index and lose none
    first, last = range_frame_indices(ZERO, F(136, 25), RATE)
    left = range_frame_indices(ZERO, F(68, 25), RATE)
    right = range_frame_indices(F(68, 25), F(136, 25), RATE)
    assert left[1] == right[0] and (first, last) == (left[0], right[1])


# ----------------------------------------------------------- rate 4 on constant rate


def test_rate_four_on_constant_rate_material_shows_every_fourth_picture(tmp_path):
    """``[1, 6)`` at rate 4 in a 25 fps output: the grid lands on real frames.

    The step is ``rate / R = 4/25`` source seconds, so frame *k* samples
    ``1 + 4k/25 = (25 + 4k)/25`` - always exactly a frame time of this stream,
    whose picture *n* sits at ``n/25``. The preceding picture is therefore
    picture ``25 + 4k`` itself, with no hold anywhere:
    ``[25, 29, 33, ..., 149]``. The output length is ``5 / 4 = 1.25 s`` =
    ``ceil(1.25 * 25) = 32`` frames, so ``k`` runs 0..31 and the last sample is
    ``1 + 124/25 = 5.96 s`` - the slice's last real picture, asked for exactly.
    Every tie distance is 0 here, which is why a tie distance alone never
    condemns a render.
    """
    clip = PlacedClip("clip", F(1), F(6), rate=F(4))
    ref = reference(tmp_path, (clip,))
    assert output_frames(clip.output_end, RATE) == 32
    assert ref.expected_identities(0, 32) == list(range(25, 150, 4))
    picks = ref.picks(0, 32)
    assert [p.kind for p in picks] == [PICK] * 32
    assert ref.holds(0, 32) == []
    # the sampling time is a real frame time at every index: distance exactly 0
    assert [t.distance for t in ref.ties(0, 32, F(1, 1000))] == [F(0)] * 32
    # the legal slice is exactly the pictures 25..149 and it ends the stream
    legal = ref.legal(clip)
    assert (legal.first, legal.last, legal.count) == (25, 149, 125)
    assert legal.ends_at_stream_end


# ------------------------------------------------- rate 25/24, and its tie pattern


def test_25_24_in_a_25_fps_output_skips_one_picture_every_24_frames(tmp_path):
    """24 fps material in a 25 fps timeline, on a stream of 25 fps pictures.

    The step is ``(25/24) / 25 = 1/24`` source seconds, so frame *k* samples
    ``1 + k/24``. Picture *n* sits at ``n/25``, so the answer is

        n = floor((1 + k/24) * 25) = floor(25 + 25k/24) = 25 + k + floor(k/24)

    - one picture every frame, and one extra skip each time *k* passes a multiple
    of 24: frames 0..23 show 25..48, frame 24 shows **50** (picture 49 is never
    shown), frame 48 shows 75, frame 72 shows 100. The length is
    ``5 / (25/24) = 24/5 = 4.8 s`` -> ``ceil(4.8 * 25) = 120`` frames, so the
    last sample is ``1 + 119/24 = 143/24 = 5.9583 s``: picture 148. Picture 149
    sits at 5.96 s, past the last sampling time, so it must never appear and
    nothing is held.

    Ties, quantified rather than budgeted: ``wanted * 600 = 600 + 25k`` and a
    picture time is ``24n/600``, so the distance to the *preceding* picture is
    ``(k mod 24)/600`` s and to the *following* one ``(24 - k mod 24)/600`` s. The
    nearer of the two is within ``1/600`` s exactly when ``k mod 24`` is 0, 1 or 23
    - fifteen of the 120 frames, five of them sitting on a picture time exactly.
    A recorded tie distance is always the **nearer** side: at k=87 (``wanted =
    37/8 s``, ``87 mod 24 = 15``) the preceding picture is 15/600 s away and the
    next one 9/600 s, so the distance there is 9/600 s.
    """
    clip = PlacedClip("clip", F(1), F(6), rate=F(25, 24))
    ref = reference(tmp_path, (clip,))
    assert clip.output_end == F(24, 5)
    frames = output_frames(clip.output_end, RATE)
    assert frames == 120
    expected = [25 + k + k // 24 for k in range(frames)]
    assert ref.expected_identities(0, frames) == expected
    assert (expected[0], expected[23], expected[24], expected[48], expected[119]) == (
        25, 48, 50, 75, 148,
    )
    assert 49 not in expected and 149 not in expected
    assert ref.holds(0, frames) == []  # the grid stops before the last picture
    # a tie is a distance, stated as one: (k mod 24)/600 s from the previous frame
    assert [(t.index, t.distance) for t in ref.ties(0, frames, F(1, 600))] == [
        (0, F(0)), (1, F(1, 600)), (23, F(1, 600)), (24, F(0)), (25, F(1, 600)),
        (47, F(1, 600)), (48, F(0)), (49, F(1, 600)), (71, F(1, 600)), (72, F(0)),
        (73, F(1, 600)), (95, F(1, 600)), (96, F(0)), (97, F(1, 600)),
        (119, F(1, 600)),
    ]
    assert ref.tie(87).wanted == F(37, 8) and ref.tie(87).distance == F(9, 600)


def test_the_fractional_in_point_of_the_reviewer_case_names_frame_98_at_87(tmp_path):
    """The composition of the known-red range test, answered by hand.

    Clip ``[1/3, 6)`` at rate 25/24: output length ``(6 - 1/3) / (25/24) =
    136/25 s`` = 136 frames. Frame *k* samples ``1/3 + k/24``. At ``k = 87`` that
    is ``1/3 + 87/24 = 95/24 = 3.9583 s``. The pictures either side are 98 at
    ``98/25 = 3.92 s`` and 99 at ``99/25 = 3.96 s``, and 95/24 < 99/25 because
    ``2375 < 2376``: the preceding-frame rule requires **98**, and it is a real
    pick, not an edge hold. The distance to the next picture is
    ``99/25 - 95/24 = 1/600 s`` - 1.67 ms, the whole size of the question a
    backend timestamp-quantisation tie could be about at this index.
    """
    clip = PlacedClip("clip", F(1, 3), F(6), rate=F(25, 24))
    ref = reference(tmp_path, (clip,))
    assert clip.output_end == F(136, 25)
    pick = ref.pick(87)
    assert (pick.identity, pick.kind) == (98, PICK)
    assert pick.wanted == F(95, 24)
    assert (pick.prev_pts, pick.next_pts) == (F(98, 25), F(99, 25))
    tie = ref.tie(87)
    assert tie.distance == F(1, 600)
    for index, required in ((111, 123), (135, 148)):
        assert ref.pick(index).identity == required, f"frame {index}"


# ---------------------------------------------------------------- edge behaviour


def test_a_retimed_tail_shows_what_the_grid_asked_for_and_stops(tmp_path):
    """``[40/25, 50/25)`` at rate 5/4 -> ``40, 41, 42, 43, 45, 46, 47, 48``.

    The step is ``(5/4)/25 = 1/20`` source seconds, so the eight frames of this
    ``ceil((10/25)/(5/4) * 25) = 8`` frame output sample
    ``40/25 + k/20 = (160 + 5k)/100``: 1.60, 1.65, 1.70, 1.75, 1.80, 1.85, 1.90,
    1.95 s. Picture *n* sits at ``n/25 = 4n/100``, so the pick is picture
    ``floor(160/4 + 5k/4) = 40 + floor(5k/4)``: 40, 41, 42, 43, 45, 46, 47, 48 -
    picture 44 is skipped at k=4, and 49 (1.96 s) is never asked for. Nothing is
    held: the last sampling time is *before* the slice's last picture.
    """
    clip = PlacedClip("clip", F(40, 25), F(50, 25), rate=F(5, 4))
    ref = reference(tmp_path, (clip,), count=50)
    assert output_frames(clip.output_end, RATE) == 8
    assert ref.expected_identities(0, 8) == [40, 41, 42, 43, 45, 46, 47, 48]
    assert [p.kind for p in ref.picks(0, 8)] == [PICK] * 8
    assert ref.holds(0, 8) == []
    assert ref.pick(7).wanted == F(195, 100) and ref.pick(7).pts == F(192, 100)


def test_a_slice_that_runs_past_its_last_picture_holds_it_and_says_so(tmp_path):
    """``[48/25, 50/25)`` at rate 1/2 -> ``48, 48, 49, 49``, one of them a hold.

    Step ``(1/2)/25 = 1/50`` source seconds, length ``(2/25)/(1/2) = 4/25 s`` =
    ``ceil(4/25 * 25) = 4`` frames, sampling ``48/25 + k/50 = (96 + k)/50``:
    1.92, 1.94, 1.96, 1.98 s. The slice holds exactly pictures 48 (1.92) and 49
    (1.96), so the picks are 48, 48, 49 - and the fourth sampling time, 1.98 s,
    is past the last picture the slice can read, so 49 is *held*. That hold is
    one frame long and is the required behaviour, not an accident to apologise
    for; a hold thousands of frames long would be footage the file does not have.
    """
    clip = PlacedClip("clip", F(48, 25), F(50, 25), rate=F(1, 2))
    ref = reference(tmp_path, (clip,), count=50)
    assert output_frames(clip.output_end, RATE) == 4
    assert ref.expected_identities(0, 4) == [48, 48, 49, 49]
    assert [p.kind for p in ref.picks(0, 4)] == [PICK, PICK, PICK, TAIL_HOLD]
    assert ref.holds(0, 4) == [(TAIL_HOLD, 3, 1)]
    legal = ref.legal(clip)
    assert (legal.first, legal.last) == (48, 49)
    # this 50-picture fixture ends at picture 49, so the hold is "the film ended
    # here", which is what makes it legitimate rather than an invented tail
    assert legal.ends_at_stream_end


def test_a_head_between_frames_holds_the_first_legal_picture(tmp_path):
    """In-point ``1/3`` s: the first picture the slice can read is 9 at 9/25 s.

    Frame 0 samples ``1/3 = 0.3333 s``, which is between picture 8 (0.32 s) and
    picture 9 (0.36 s). Picture 8 is outside the slice, so it can never be shown:
    the answer is the first legal picture, 9, held. Frame 1 samples
    ``1/3 + 1/25 = 28/75 = 0.3733 s``, still before picture 10 at 0.40 s, so it
    shows 9 again - now as an ordinary pick, not a head hold. Frame 2 samples
    ``1/3 + 2/25 = 31/75 = 0.4133 s`` and shows 10.

    The tie distance at frame 0 is ``1/75 s`` (to picture 8, which the slice
    forbids) and ``2/75 s`` to the picture it actually shows.
    """
    clip = PlacedClip("clip", F(1, 3), F(5))
    ref = reference(tmp_path, (clip,))
    picks = ref.picks(0, 3)
    assert [p.identity for p in picks] == [9, 9, 10]
    assert [p.kind for p in picks] == [HEAD_HOLD, PICK, PICK]
    assert ref.holds(0, 3) == [(HEAD_HOLD, 0, 1)]
    assert picks[0].pts == F(9, 25)
    assert (picks[0].prev_pts, picks[0].next_pts) == (F(8, 25), F(9, 25))
    assert ref.tie(0).distance == F(1, 75)


def test_a_gap_is_black_for_the_reference_too(tmp_path):
    """25 frames of picture, 25 of nothing, 25 of picture.

    Clip A takes source ``[0, 1)`` and clip B source ``[2, 3)`` placed at output
    2 s; at 25 fps both are 25 frames, so global frames 0..24 show pictures
    0..24, frames 25..49 belong to no clip, and frame 50 samples exactly 2 s,
    which is picture 50 of this 100-picture fixture.
    """
    a = PlacedClip("a", ZERO, F(1))
    b = PlacedClip("b", F(2), F(3), output_start=F(2))
    ref = reference(tmp_path, (a, b), count=100)
    expected = list(range(25)) + [None] * 25 + list(range(50, 75))
    assert ref.expected_identities(0, 75) == expected
    assert ref.pick(25) is None and ref.tie(25) is None
    assert ref.pick(49) is None and ref.pick(50).identity == 50


def test_a_stream_that_does_not_start_at_zero_address_by_its_own_origin(tmp_path):
    """The offset fixture: pictures at ``5/2 + n/25``, 2.5 s .. 6.46 s.

    Slice ``[7/2, 13/2)`` at rate 25/24 (step 1/24): frame *k* samples
    ``7/2 + k/24``, and the picture at or before it is the largest *n* with
    ``5/2 + n/25 <= 7/2 + k/24``, i.e. ``n = floor(25 + 25k/24) = 25 + k +
    floor(k/24)``. The length is ``3 / (25/24) = 72/25 s`` = 72 frames, so the
    last sample is ``7/2 + 71/24 = 155/24 = 6.4583 s`` and the answer is
    ``25 + 71 + 2 = 98``: picture 99 (323/50 = 6.46 s) is inside the slice but
    after the last sampling time, so it must not appear and nothing is held.
    """
    clip = PlacedClip("clip", F(7, 2), F(13, 2), rate=F(25, 24))
    ref = PictureReference(
        stream_of(make_identity_offset(tmp_path, count=100)), RATE, (clip,)
    )
    assert ref.stream.first == F(5, 2) and ref.stream.last == F(323, 50)
    frames = output_frames(clip.output_end, RATE)
    assert frames == 72
    expected = ref.expected_identities(0, frames)
    assert expected == [25 + k + k // 24 for k in range(72)]
    assert (expected[0], expected[-1]) == (25, 98)
    assert ref.holds(0, frames) == []
    assert 99 not in expected


def test_variable_frame_rate_material_answers_from_its_own_timestamps(tmp_path):
    """The VFR fixture: measured 1/25 steps, then 83/1000 and 21/250.

    Slice ``[3, 8)`` at rate 1, so frame *k* samples ``3 + k/25``. The measured
    pictures around the rate change are picture 75 at exactly 3, picture 99 at
    99/25 = 3.96, picture 100 at exactly 4, then 4083/1000, 4167/1000, 17/4,
    4333/1000, 4417/1000, 9/2. So:

    * k=0 samples 3 -> picture 75 (exactly on it, tie distance 0);
    * k=25 samples 4 -> picture 100 (exactly on it);
    * k=26 samples 4.04 and k=27 samples 4.08, both still before 4083/1000 =
      4.083, so both show picture 100 - a repeat that a nominal 25 fps assumption
      (``75 + k`` = 102 at k=27) would get wrong;
    * k=28 samples 4.12 and k=29 samples 4.16, both at or after 4.083 and below
      4.167, so both show 101;
    * k=30, 31 -> 102 (4.167); k=32, 33 -> 103 (4.25); k=34 -> 104 (4.333).
    """
    clip = PlacedClip("clip", F(3), F(8))
    ref = PictureReference(stream_of(make_identity_vfr(tmp_path)), RATE, (clip,))
    assert ref.stream.pts[75] == F(3) and ref.stream.pts[100] == F(4)
    assert ref.stream.pts[101] == F(4083, 1000) and ref.stream.pts[102] == F(4167, 1000)
    assert ref.stream.pts[103] == F(17, 4)
    picks = {k: ref.pick(k) for k in (0, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34)}
    assert [p.identity for p in picks.values()] == [
        75, 100, 100, 100, 101, 101, 102, 102, 103, 103, 104,
    ]
    assert all(p.kind == PICK for p in picks.values())
    # the last legal picture of this slice is 147 at 7917/1000; 148 sits at exactly
    # 8 and the half-open slice excludes it
    legal = ref.legal(clip)
    assert (legal.first, legal.last, legal.last_pts) == (75, 147, F(7917, 1000))
    assert not legal.ends_at_stream_end
    frames = output_frames(clip.output_end, RATE)
    assert frames == 125
    # the slice keeps running to 8 s but its newest picture is 147 at 7.917 s, so
    # the last two sampling times (7.92 and 7.96 s) are answered by holding it:
    # honest edge behaviour, two frames long, not a retiming fault
    assert [(ref.pick(k).identity, ref.pick(k).kind) for k in (122, 123, 124)] == [
        (146, PICK), (147, TAIL_HOLD), (147, TAIL_HOLD),
    ]
    assert ref.holds(0, frames) == [(TAIL_HOLD, 123, 2)]


# --------------------------------------------------------------------- refusals


def test_a_slice_with_no_picture_is_an_error_not_a_black_frame(tmp_path):
    """``[13/2, 14/2)`` of the offset file asks for material the file does not hold.

    The stream's last real picture is 323/50 = 6.46 s, so a slice beginning at
    6.5 s contains nothing: the reference refuses, naming the range the stream
    really presents. Black and a held picture are both answers this contract
    forbids there - they would present footage that does not exist as a choice.
    """
    clip = PlacedClip("clip", F(13, 2), F(14, 2))
    ref = PictureReference(
        stream_of(make_identity_offset(tmp_path, count=100)), RATE, (clip,)
    )
    with pytest.raises(EmptySliceError, match=r"real pictures run 5/2 \.\. 323/50"):
        ref.pick(0)


def test_two_clips_on_one_frame_are_refused_rather_than_resolved(tmp_path):
    """The reference models one track; a frame owned twice is a bug in the caller."""
    a = PlacedClip("a", ZERO, F(2))
    b = PlacedClip("b", F(1), F(3), output_start=F(1))
    ref = reference(tmp_path, (a, b), count=100)
    with pytest.raises(OverlapError, match=r"\['a', 'b'\]"):
        ref.pick(25)


def test_a_clip_that_is_not_a_clip_is_refused_before_anything_is_measured():
    with pytest.raises(ValueError, match="source slice"):
        PlacedClip("bad", F(2), F(2))
    with pytest.raises(ValueError, match="rate"):
        PlacedClip("bad", ZERO, F(1), rate=F(0))
    with pytest.raises(ValueError, match="strictly ascend"):
        DecodedStream(pts=(F(0), F(0)))


# ------------------------------------------------- how disagreements are named


def test_a_shifted_picture_is_named_retiming_and_an_edge_hold_is_not(tmp_path):
    """The three mismatch classes, applied to known observations.

    On ``[1, 6)`` at rate 25/24 the reference requires 25, 49-skip, ..., 148 (see
    the hand table above). Feeding it the pictures a render actually made:

    * frame 87 showing 116 where 115 is required -> ``retiming``: the grid asked
      for real material (115 sits at 4.6 s, well inside the slice) and got the
      next picture, ``offset +1``;
    * frame 0 showing 24 -> ``outside-slice``: picture 24 is at 24/25 s, before
      the clip's in-point, so it is footage this clip does not contain;
    * on the held-tail case, frame 3 -> ``tail-hold``, because the grid itself
      asked past the last legal picture and the reference answered by holding.
    """
    clip = PlacedClip("clip", F(1), F(6), rate=F(25, 24))
    ref = reference(tmp_path, (clip,))
    required = ref.expected_identities(0, 120)
    observed = list(required)
    observed[87] = 116
    observed[0] = 24
    wrong = compare(ref, observed, "reference vs render")
    assert [m.index for m in wrong] == [0, 87]
    assert [(m.kind, m.offset) for m in wrong] == [
        (OUTSIDE_SLICE_MISMATCH, -1),
        (RETIMING_MISMATCH, 1),
    ]
    assert wrong[1].wanted == F(111, 24) and wrong[1].tie_distance == F(9, 600)

    tail = PlacedClip("clip", F(48, 25), F(50, 25), rate=F(1, 2))
    ref2 = reference(tmp_path, (tail,), count=50)
    # a backend that simply stops when the real pictures run out would show 48
    # again at the held frame; the reference names that a tail hold, not a fault
    held = compare(ref2, [48, 48, 49, 48], "reference vs render")
    assert [(m.index, m.kind) for m in held] == [(3, TAIL_HOLD_MISMATCH)]
    # and a picture missing where a picture is required is its own class
    black = compare(ref2, [48, 48, None, 49], "reference vs render")
    assert [(m.index, m.kind) for m in black] == [(2, BLACK_MISMATCH)]


def test_two_paths_that_disagree_are_reported_by_global_index(tmp_path):
    """The reviewer's join case, as a comparison of two renders.

    A fixed-origin render trimmed at global index 87 shows 99; an independently
    sought range at the same global index shows 98; the arithmetic requires 98.
    The comparison is by *global index*, so a range that starts at a different
    position in its own file is still the same frame.
    """
    whole = [None] * 87 + [99] + [None] * 48
    sought = [None] * 87 + [98] + [None] * 48
    out, problems = path_disagreements(whole, 0, sought, 0)
    assert problems == []
    assert [(d.index, d.reference, d.sought) for d in out] == [(87, 99, 98)]
    # a range that covers a different set of global indices is a structural fault,
    # said separately from a picture that moved
    out, problems = path_disagreements([5, 6, 7], 0, [6, 7], 1)
    assert out == [] and any("coverage differs" in p for p in problems)
    # and a frame one side leaves black where the other has a picture is its own note
    out, problems = path_disagreements([5, 6, 7], 0, [5, None, 7], 0)
    assert [(d.index, d.sought) for d in out] == [(1, None)]
    assert any("black" in p for p in problems)


def test_the_reference_and_the_measured_fixture_agree_on_what_a_frame_is(tmp_path):
    """The reference's picture numbers are the fixture's own printed numbers.

    If ``picture_facts`` read a file whose pictures were out of order, duplicated
    or renumbered, every table above would be measuring nothing, so the identity
    each decoded picture carries is checked against its own position here.
    """
    for path, count in (
        (make_identity_video(tmp_path, count=150), 150),
        (make_identity_offset(tmp_path, count=100), 100),
        (make_identity_vfr(tmp_path), 150),
    ):
        stream = stream_of(path)
        assert stream.count == count
        assert stream.identities == tuple(range(count))
        assert all(b > a for a, b in itertools.pairwise(stream.pts))
        assert stream.identity(0) == 0 and stream.ordinal_of(count - 1) == count - 1
