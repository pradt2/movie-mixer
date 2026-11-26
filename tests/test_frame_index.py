"""The bounded picture-timestamp index: answers, edges, coverage and probe cost.

The index exists so navigation can ask "which real frame is next?" without reading
a whole film. What is checked here is therefore the answer, what the answer is
allowed to claim, and the cost of getting it: one question must not turn into a
scan, a question with no answer must not spin, and a question that could not be
answered must come back as "not known" rather than as the end of the stream.

Every expectation about *which* pictures exist is an independent measurement:
``picture_facts`` decodes the fixture and refuses it unless ``ffprobe -show_frames``
and a ``rawvideo`` decode with ``-fps_mode passthrough`` agree on the picture count,
so the index is compared against real decoded presentation times and never against
the same probe that produced its answer. Where a coverage or termination rule is
being checked, the probe itself is fabricated, so the arithmetic is hand-worked.

The regression that started this file is ``test_a_question_answered_inside_one_window``:
an earlier version of the search moved its cursor to the edge of the window it had
just read *before* asking that window the question, and when the answer was inside
the window it looped forever without ever spending another probe. It presented
itself as an export that never finished, because the render path asks the same
question to place a picture item's sampling grid. The two fabricated tests named
``..._walks_forward_...`` and ``..._spends_its_budget_and_stops`` are the same
failure class, checked with a bounded probe count instead of by not hanging.
"""

import itertools
from fractions import Fraction as F

import pytest

from media_fixtures import (
    HAS_FFMPEG,
    make_colour_steps,
    make_identity_bframes,
    make_identity_video,
    make_long_video,
    make_presentation_hole,
    make_truncated_identity_offset,
    make_vfr,
    make_whole_asset_offset,
    picture_facts,
)
from video_editor.media.framegrab import source_frame_times
from video_editor.media.frameindex import MAX_PROBES_PER_QUERY, AnswerStatus, FrameIndex
from video_editor.media.frameprobe import PictureWindow

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")

def subdirectory(tmp_path, name: str):
    """One directory per fixture: these helpers name their output by shape."""
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def decoded_pts(path) -> tuple[F, ...]:
    """The stream's real decoded presentation times, measured once per fixture."""
    return picture_facts(path).pictures


# ------------------------------------------------- fake probe answers (no media)


def fake_window(start: F, end: F, stamps, time_base=F(1, 1000)) -> PictureWindow:
    """One fabricated probe answer: what a read of ``[start, end)`` reported.

    The numbers in these tests are hand-worked, so the coverage and termination
    rules can be checked against arithmetic rather than against a decoder.
    """
    stamps = tuple(sorted(stamps))
    return PictureWindow(
        read_from=start,
        read_to=end,
        stamps=stamps,
        reported=len(stamps),
        pts_fallback=0,
        time_base=time_base,
    )


class FakeProbe:
    """Feeds the index fabricated reads and records every window it was asked for."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.asks: list[tuple[F, F]] = []

    def __call__(self, start: F, end: F) -> PictureWindow:
        self.asks.append((start, end))
        return self.answer(start, end)


#: pictures at whole seconds 0 s to 5 s, used by the fabricated cases
PICTURES = [F(seconds) for seconds in range(6)]


# ---------------------------------------------------------------- real fixtures


def test_a_question_answered_inside_one_window(tmp_path):
    """A 6 s clip: the answer is in the first window, and only that window is read.

    Regression for an infinite loop (not a wrong answer): the search used to move
    to the window's edge before consulting it, so it re-asked a question the same
    window could never answer, forever.
    """
    source = make_colour_steps(tmp_path)
    index = FrameIndex(source, 0)
    assert index.at_or_after(F(1)) == F(1), "a real 25 fps frame sits exactly at 1 s"
    assert index.probes == 1, f"one window covers the whole clip, got {index.probes}"
    assert index.at_or_before(F(1)) == F(1)
    past_end = index.answer_at_or_after(F(21))
    assert past_end.pts is None
    assert past_end.status is AnswerStatus.NO_PICTURE, (
        "the reading reached the end of this stream, so it may say there is none"
    )
    assert index.probes <= MAX_PROBES_PER_QUERY, (
        f"a question with no answer spent {index.probes} probes "
        f"(budget {MAX_PROBES_PER_QUERY})"
    )


def test_edges_answer_none_rather_than_guessing(tmp_path):
    source = make_colour_steps(tmp_path)
    stamps = decoded_pts(source)
    index = FrameIndex(source, 0)
    start = index.answer_step(stamps[0], -1)
    assert start.pts is None and start.status is AnswerStatus.NO_PICTURE, (
        "nothing exists before this stream's first decoded picture"
    )
    end = index.answer_step(stamps[-1], +1)
    assert end.pts is None and end.status is AnswerStatus.NO_PICTURE, (
        "nothing exists after this stream's last decoded picture"
    )
    assert index.step(stamps[-1], -1) == stamps[-2]
    assert index.at_or_before(F(0)) == F(0), "the first frame is at the axis origin"


def test_stepping_uses_real_timestamps_on_irregular_material(tmp_path):
    """Variable frame rate: neighbours are the stream's own, not a nominal rate."""
    source = make_vfr(tmp_path)
    stamps = decoded_pts(source)
    assert len({b - a for a, b in itertools.pairwise(stamps)}) > 1, (
        "the fixture must have uneven frame spacing to prove anything"
    )
    index = FrameIndex(source, 0)
    for t in (stamps[0], stamps[len(stamps) // 2], stamps[-1]):
        forward = index.step(t, +1)
        if forward is not None:
            assert forward > t and forward in stamps
            assert index.step(t, +1) == stamps[stamps.index(t) + 1]
        backward = index.step(t, -1)
        if backward is not None:
            assert backward < t and backward in stamps
            assert index.step(t, -1) == stamps[stamps.index(t) - 1]


def test_the_probe_budget_holds_on_a_long_source(tmp_path):
    """Navigation stays local: asking near the start never reads the whole film."""
    source = make_long_video(tmp_path, seconds=60)
    stamps = decoded_pts(source)
    index = FrameIndex(source, 0)
    before = index.probes
    found = index.at_or_after(F(31, 10))
    assert found is not None and found in stamps
    assert index.probes - before <= 1, "one window around the question is enough"
    spans = index.cached_spans()
    assert all(end - start <= index.window for start, end in spans)
    covered = sum(end - start for start, end in spans)
    assert covered <= MAX_PROBES_PER_QUERY * index.window, (
        f"{covered}s of a {len(stamps)}-frame film was read for one step"
    )


def test_a_wide_request_is_answered_from_bounded_reads(tmp_path):
    """A silly span is a bounded read plus what actually exists, not a scan."""
    source = make_colour_steps(tmp_path)
    index = FrameIndex(source, 0)
    answer = index.answer_frames_in(F(1), F(2))
    inside = [pts for pts in decoded_pts(source) if F(1) <= pts < F(2)]
    assert answer.pts == tuple(inside), "the span must be exactly the stream's own frames"
    assert answer.complete, "the span was read all the way across"
    assert index.probes <= MAX_PROBES_PER_QUERY
    assert index.frames_in(F(2), F(2)) == ()


def test_a_span_past_the_end_of_a_stream_is_answered_in_one_read(tmp_path):
    """A request beyond the material is not a walk: the end was already established.

    ``make_vfr`` decodes 248 pictures ending at 11.917 s while its container claims
    9.94 s. A read asked at 100-110 s therefore runs past the claim, demuxes the rest
    of the stream and answers with the tail: that establishes there is nothing later,
    so the span is complete and empty in one read instead of walking rightwards
    through the probe budget looking for ground the file does not have.
    """
    source = make_vfr(tmp_path)
    facts = picture_facts(source)
    assert facts.pictures[-1] == F(11917, 1000), "measured end of this fixture"
    index = FrameIndex(source, 0)
    answer = index.answer_frames_in(F(100), F(160))
    assert answer.pts == (), "no picture of this stream lies in that span"
    assert answer.complete, "a read asked past the end this file claims closed the question"
    assert index.probes == 1, f"the span walked {index.probes} reads for an ended stream"
    assert answer.coverage is not None and answer.coverage[1] == F(110)
    assert answer.coverage[0] <= facts.pictures[-1] <= answer.coverage[1], (
        f"the read came back with the stream's tail: {answer.coverage}"
    )


def test_a_break_wider_than_any_window_stays_unknown_until_it_is_read_past(tmp_path):
    """A 36 s break in a 44 s file: no window in the budget reaches across it.

    Measured on this fixture (44 s claimed, pictures 0.00-3.96 s then 40.04-44.00 s):
    reads asked at 10-12, 20-22 and 38-40 s all answer with 3.00-3.96 s - the same
    records, and the same shape of answer, as a read asked at 90-92 s of a stream that
    really has ended. So the index may not turn its own exhausted reading into "there
    is no picture that way", and it may not invent a next frame either.

    What it may say, and does: ``UNKNOWN`` at 20 s; the real picture at 40.04 s when
    the question is placed where a read reaches it; and ``NO_PICTURE`` at 45 s, where
    the first read of the search was asked past the end this container declares.
    """
    source = make_presentation_hole(tmp_path, name="long_break.mkv", seconds=44,
                                    hole_start=4.0, hole_end=40.0)
    facts = picture_facts(source)
    assert max(p for p in facts.pictures if p <= F(20)) == F(99, 25)
    assert min(p for p in facts.pictures if p >= F(20)) == F(1001, 25), (
        "measured: the break runs 3.96-40.04 s on this file"
    )

    middle = FrameIndex(source, 0)
    answer = middle.answer_at_or_after(F(20))
    assert answer.pts is None and answer.status is AnswerStatus.UNKNOWN, (
        f"the reading ran out inside the break: {answer.describe()}"
    )
    assert middle.probes <= MAX_PROBES_PER_QUERY, "bounded, whatever the answer is"
    assert middle.at_or_after(F(20)) is None, "the plain lookup keeps answering None"

    later = FrameIndex(source, 0)
    after = later.answer_at_or_after(F(41))
    assert after.status is AnswerStatus.FOUND and after.pts == F(41), (
        f"the break is not the end of this stream: {after.describe()}"
    )
    assert after.coverage is not None and after.coverage[1] >= F(44), (
        f"the read that answered was asked past the end this file claims: {after.coverage}"
    )
    earlier = later.answer_at_or_after(F(1001, 25))
    assert earlier.status is AnswerStatus.FOUND and earlier.pts == F(1001, 25), (
        f"the first picture after the break is real and reachable: {earlier.describe()}"
    )
    past = later.answer_at_or_after(F(45))
    assert past.pts is None and past.status is AnswerStatus.NO_PICTURE, (
        f"a read asked past the declared end may say there is nothing later: "
        f"{past.describe()}"
    )


def test_the_index_reports_the_decoded_pictures_of_every_shape_of_stream(tmp_path):
    """CFR, real B-frames, VFR and an offset: the stamps are the decoded ones.

    ``picture_facts.pictures`` is the independent measurement — ffprobe's frame
    records cross-checked against a rawvideo decode — and the shape checks below
    prove each fixture is the material it claims, so a passing comparison here is
    arithmetic about a real file rather than two probes agreeing with each other.
    Measured here: for every well-formed fixture the packet hints happen to carry
    the same times, and that agreement is asserted so this finding cannot rot
    unnoticed. The index asks for pictures anyway, because a packet says only
    where to seek; a decoded frame says a picture is there to show.
    """
    cases = (
        (make_colour_steps, "constant rate", lambda facts: facts.uniform_spacing == F(1, 25)),
        (make_identity_bframes, "real B-frames", lambda facts: facts.of_type("B") > 0),
        (make_vfr, "variable rate", lambda facts: len(facts.spacings) > 1),
        (make_whole_asset_offset, "offset presentation",
         lambda facts: facts.pictures[0] == F(5, 2)),
    )
    for maker, shape, check in cases:
        source = maker(subdirectory(tmp_path, maker.__name__))
        facts = picture_facts(source)
        assert check(facts), f"{shape} fixture is not the material this test needs: {facts}"
        index = FrameIndex(source, 0)
        answer = index.answer_frames_in(facts.pictures[0], facts.pictures[-1] + F(1, 1000))
        assert answer.complete, f"{shape}: the span was not read across in {index.probes} probes"
        assert answer.pts == facts.pictures, (
            f"{shape}: {len(answer.pts)} indexed pictures against "
            f"{len(facts.pictures)} decoded ones"
        )
        proven: set[F] = set()
        for low, high in index.proven_spans():
            proven.update(index.frames_in(low, high + F(1, 1000)))
        assert proven == set(facts.pictures), (
            f"{shape}: the spans the index claims to have read missed decoded pictures"
        )
        hints = {frame.pts for frame in source_frame_times(source, 0)}
        assert hints == set(facts.pictures), (
            f"{shape}: the packet hints no longer match the decoded pictures, so the "
            "measured comparison in this test's docstring has to be restated"
        )
        for pts in facts.pictures:
            ticks = pts / facts.time_base
            assert ticks == int(ticks), f"{pts} is not a whole number of {facts.time_base} ticks"


def test_every_stamp_is_on_the_container_s_own_tick_grid(tmp_path):
    """An MP4 with a 1/12800 tick: no printed decimals, no accumulated floats.

    ``identity_bframes.mp4`` stores 100 pictures of a 25 fps stream with
    ``time_base 1/12800``. Read through ffprobe's printed times those stamps come
    back rounded, and a frame step then lands between two real pictures.
    """
    source = make_identity_bframes(tmp_path)
    facts = picture_facts(source)
    assert facts.time_base == F(1, 12800), facts.time_base
    index = FrameIndex(source, 0)
    assert index.time_base == facts.time_base
    stamps = decoded_pts(source)
    for first, second in itertools.pairwise(stamps):
        answer = index.answer_at_or_after(first + F(1, 1000))
        assert answer.status is AnswerStatus.FOUND
        assert answer.pts == second
        ticks = answer.pts / facts.time_base
        assert ticks == int(ticks), f"{answer.pts} left the container's tick grid"


def test_a_presentation_hole_is_a_hole_and_not_the_end_of_the_stream(tmp_path):
    """Pictures stop, then resume: the next one is real, and reachable if read for.

    Measured on this fixture: 202 pictures, 0.00-3.96 s then 7.92-11.96 s, so the
    only spacing is 1/25 or 99/25 - nothing about this file is evenly spaced across
    the break. The packet-timestamp index this replaces read the empty window as "no
    frame that way" and ``at_or_after(5)`` answered None; a read of a window that
    reaches across the break enumerates it, so the wide-window question below is
    answered 7.92 s in one read.

    A window too narrow to reach across stays UNKNOWN. Measured behaviour is that a
    read placed inside the break reports the pictures *before* it and nothing from
    after it - the same shape of answer a stream's end produces, which is exactly
    why the index must not turn it into "the stream ended", and why it stops rather
    than re-reading the same ground until the budget runs out.
    """
    source = make_presentation_hole(tmp_path)
    facts = picture_facts(source)
    assert len(facts.spacings) > 1, f"the fixture lost its break: {facts.spacings}"
    expected = min(pts for pts in facts.pictures if pts >= F(5))
    assert expected == F(198, 25), "measured: the break runs from 3.96 s to 7.92 s"

    wide = FrameIndex(source, 0)
    answer = wide.answer_at_or_after(F(5))
    assert answer.status is AnswerStatus.FOUND and answer.pts == expected
    assert wide.probes == 1, "one read of 10 s reaches across a 3.96 s break"
    before = wide.answer_at_or_before(F(5))
    assert before.status is AnswerStatus.FOUND
    assert before.pts == max(pts for pts in facts.pictures if pts <= F(5)) == F(99, 25)

    end = wide.answer_at_or_after(F(12))
    assert end.pts is None and end.status is AnswerStatus.NO_PICTURE, (
        "the reading reached past the end this file claims, so it may say there is none"
    )

    narrow = FrameIndex(source, 0, window=F(1, 2))
    unknown = narrow.answer_at_or_after(F(5))
    assert unknown.pts is None and unknown.status is AnswerStatus.UNKNOWN
    assert unknown.coverage == (F(3), F(99, 25)), (
        f"only the pictures before the break were enumerated: {unknown.coverage}"
    )
    # hand-worked: the read asked for 4.75-5.25 s answers with the group before the
    # break, 3.00-3.96 s; the walk then reads 3.96-4.46 s, which brings nothing new,
    # so the search stops having proved only 3.00-3.96 s - two reads, not four
    assert narrow.probes == 2, f"the search spent {narrow.probes} reads"
    assert narrow.answer_at_or_after(F(5)).status is AnswerStatus.UNKNOWN
    assert narrow.probes == 2, "an unread gap is not re-read by asking again"


def test_a_short_read_does_not_claim_a_stream_ended_where_it_has_not_read(tmp_path):
    """Truncated tail: the container still claims 9 s, so absence has to be reached.

    ``make_truncated_identity_offset`` keeps its declared 2.5-7.023 s timeline while
    only 54 pictures decode, the last at 4.62 s. One-second windows starting just
    past that keep answering with the same tail - measured behaviour is that a read
    finds the stream's last pictures rather than nothing - so the reading never gets
    as far as the end this file claims, and the honest answer is UNKNOWN. One window
    wide enough to read past the claimed end answers NO_PICTURE, in a single read.
    """
    source = make_truncated_identity_offset(tmp_path)
    facts = picture_facts(source)
    last = facts.pictures[-1]
    assert last == F(231, 50), "measured: the material stops at 4.62 s"
    asked = last + F(1, 50)

    narrow = FrameIndex(source, 0, window=F(1))
    unknown = narrow.answer_at_or_after(asked)
    assert unknown.pts is None and unknown.status is AnswerStatus.UNKNOWN
    assert unknown.coverage is not None and unknown.coverage[1] == last, (
        f"the reading stopped at its last decoded picture: {unknown.coverage}"
    )
    spans = narrow.cached_spans()
    assert len(set(spans)) == len(spans), f"a window was read twice: {spans}"
    assert narrow.probes < MAX_PROBES_PER_QUERY, (
        f"{narrow.probes} reads for a question that gained nothing after the second: "
        "the search has to stop on lack of progress, not only on the budget"
    )
    assert narrow.declared_end.end > last, "the file's own claim reaches beyond its material"

    wide = FrameIndex(source, 0, window=F(10))
    edge = wide.answer_at_or_after(asked)
    assert edge.pts is None and edge.status is AnswerStatus.NO_PICTURE
    assert wide.probes == 1, "the single read ran past the end this file claims"
    assert wide.declared_end.end < F(10), "measured claim, which is what that read passed"


def test_a_file_replaced_under_the_index_invalidates_every_window(tmp_path):
    """Identity-scoped cache: the path alone is not enough to trust a cached read."""
    source = make_identity_video(tmp_path, count=100)
    index = FrameIndex(source, 0, window=F(2))
    assert index.at_or_after(F(1)) == F(1)
    assert index.probes == 1
    recorded = index.file_identity
    assert recorded is not None

    replacement = make_identity_bframes(tmp_path, count=100)
    source.write_bytes(replacement.read_bytes())

    answer = index.answer_at_or_after(F(1))
    assert index.file_identity != recorded, "size or mtime moved, and the index noticed"
    assert index.invalidations == 1, "the change has to be visible, not just tolerated"
    assert index.probes == 2, "the replaced file's windows were dropped, not reused"
    assert index.time_base == F(1, 12800), "the stream metadata was re-read with the new file"
    assert answer.status is AnswerStatus.FOUND and answer.pts == F(1)


# ------------------------------------------------------------ fabricated probes


def test_a_read_that_landed_before_the_window_proves_that_ground_only(tmp_path):
    """Earlier seek point: the keyframe preroll is evidence, and only that much.

    Hand-worked: pictures at whole seconds; the read asked for 2.5-4.5 s seeks to the
    keyframe a second earlier, so it decodes the pictures at 2, 3 and 4 s. It may
    answer for 2.5 s - the preroll picture is a real picture - and its span runs from
    2 s to **4 s**, the last picture it decoded, not to the 4.5 s it was asked for:
    measured behaviour is that a read can stop short of its requested end, so the
    strip above its last picture is unclaimed ground and a question there is UNKNOWN
    until it is read.
    """
    probe = FakeProbe(
        lambda start, end: fake_window(start, end, [p for p in PICTURES if start - 1 <= p < end])
    )
    index = FrameIndex(make_colour_steps(tmp_path), 0, window=F(2), probe=probe)

    answer = index.answer_at_or_after(F(7, 2))
    assert answer.status is AnswerStatus.FOUND and answer.pts == F(4)
    assert probe.asks == [(F(5, 2), F(9, 2))], probe.asks
    assert index.proven_spans() == ((F(2), F(4)),), (
        f"the span the read enumerated, not the window it was asked for: "
        f"{index.proven_spans()}"
    )
    assert index.at_or_before(F(5, 2)) == F(2), "the preroll picture is a real picture"

    earlier = index.answer_at_or_before(F(19, 10))
    assert probe.asks[-1] == (F(1), F(3)), "the unread ground to the left was read"
    assert earlier.status is AnswerStatus.FOUND and earlier.pts == F(1), (
        f"that read enumerated 0-2 s and joined the spans: {earlier.describe()}"
    )

    late = index.answer_at_or_after(F(4) + F(1, 20))
    assert late.pts is None and late.status is AnswerStatus.UNKNOWN, (
        f"the strip above the first read's last decoded picture was never enumerated: "
        f"{late.describe()}"
    )
    assert late.coverage == (F(0), F(4))
    assert index.answer_at_or_before(F(1)).status is AnswerStatus.FOUND
    assert index.answer_at_or_before(F(1)).pts == F(1)


def test_a_span_wider_than_the_budget_is_reported_as_partial(tmp_path):
    """Partial coverage is a status, not a silently shortened list of frames.

    Hand-worked with 1 s of preroll: reads at 0-2, 1-3, 2-4 and 3-5 s enumerate the
    ground 0-4 s, so a request for 0-10 s lists the pictures at 0, 1, 2, 3 and 4 s
    and says the span was not read across.
    """
    probe = FakeProbe(
        lambda start, end: fake_window(start, end, [p for p in PICTURES if start - 1 <= p < end])
    )
    index = FrameIndex(make_colour_steps(tmp_path), 0, window=F(2), probe=probe)
    answer = index.answer_frames_in(F(0), F(10))
    assert answer.pts == (F(0), F(1), F(2), F(3), F(4)), "only the pictures inside what was read"
    assert not answer.complete, "the span ran to 10 s and the reading stopped short of it"
    assert answer.coverage == (F(0), F(4))
    assert index.probes == MAX_PROBES_PER_QUERY


def test_a_span_query_walks_forward_instead_of_re_asking_the_same_place(tmp_path):
    """Progress, or a stop: never the same midpoint over and over.

    The span helper this replaces probed ``(start + end) / 2`` once per budget
    iteration, which for any span wider than one window asked the identical
    question every time: measured on the 15-minute title, a 60 s request produced
    four reads of the *same* 20 s window and returned 500 of the 1500 frames that
    were there, with nothing said about the rest.
    """
    probe = FakeProbe(
        lambda start, end: fake_window(start, end, [p for p in PICTURES if start - 1 <= p < end])
    )
    index = FrameIndex(make_colour_steps(tmp_path), 0, window=F(1), probe=probe)
    answer = index.answer_frames_in(F(0), F(5))
    assert len(set(probe.asks)) == len(probe.asks), f"a window was asked twice: {probe.asks}"
    starts = [start for start, _ in probe.asks]
    assert starts == sorted(starts) and starts[0] < starts[-1], f"no forward walk: {probe.asks}"
    assert index.proven_spans() == ((F(0), F(1)),), f"coverage: {index.proven_spans()}"
    assert answer.pts == (F(0), F(1)) and not answer.complete


def test_a_long_span_on_a_long_file_walks_forward(tmp_path):
    """The same progress rule on real 15-minute material, without a fake probe.

    A 60 s request against 10 s windows cannot be read across in the budget, and the
    old helper answered it by re-reading one 20 s window four times. Measured here:
    the reads are distinct, they move rightwards, and the answer says it is partial.
    """
    source = make_long_video(tmp_path, seconds=120)
    facts = picture_facts(source)
    index = FrameIndex(source, 0, window=F(10))
    answer = index.answer_frames_in(F(40), F(100))
    assert index.probes <= MAX_PROBES_PER_QUERY
    spans = index.cached_spans()
    assert len(set(spans)) == len(spans), f"the same window was read twice: {spans}"
    assert [start for start, _ in spans] == sorted(start for start, _ in spans)
    assert not answer.complete, "60 s of ground cannot be read in four 10 s windows"
    assert answer.coverage is not None and answer.coverage[1] < F(100)
    inside = [
        pts for pts in facts.pictures if answer.coverage[0] <= pts <= answer.coverage[1]
    ]
    assert set(answer.pts) <= set(inside), "no picture outside the enumerated ground is claimed"
    assert len(answer.pts) == len(set(answer.pts)), "a stamp was listed twice"


def test_a_question_that_cannot_be_answered_spends_its_budget_and_stops(tmp_path):
    """Termination with a bound: the probe count, not a repeated cursor, ends it.

    Every window comes back empty here, so nothing can ever be found - exactly the
    shape of the old hang. The index must stop at its budget and report UNKNOWN
    with what it read, rather than the end of the stream. The metadata comes from a
    one-minute fixture so that no window in the budget can reach its claimed end.
    Hand-worked with a 2 s window and 1 s of overlap: reads at 2-4, 3-5, 4-6 and
    5-7 s, so the ground 2-7 s is enumerated and nothing beyond it is claimed.
    """
    probe = FakeProbe(lambda start, end: fake_window(start, end, []))
    index = FrameIndex(make_long_video(tmp_path, seconds=60), 0, window=F(2), probe=probe)
    answer = index.answer_at_or_after(F(3))
    assert answer.pts is None and answer.status is AnswerStatus.UNKNOWN
    assert index.probes == MAX_PROBES_PER_QUERY, (
        f"{index.probes} probes for a question with no answer "
        f"(budget {MAX_PROBES_PER_QUERY})"
    )
    assert len(set(probe.asks)) == len(probe.asks), (
        f"the search re-asked a place it had already read: {probe.asks}"
    )
    assert answer.coverage == (F(2), F(7)), "what it did read is still reported"
    assert index.at_or_after(F(3)) is None, "the plain lookup keeps answering None"


def test_a_tail_answer_proves_its_window_empty_but_never_an_end(tmp_path):
    """Measured behaviour: asked past material, a read answers with what precedes it.

    This fake probe always returns the two pictures at 5.00 and 5.96 s, so every
    window from 7 s on comes back "decoded, but nothing inside". Each such read shows
    the decoder ran over ground with no picture in it; none of them shows the stream
    stopped, because measured behaviour at a long presentation break is the same
    answer. Against a fixture whose container claims 60 s the answer therefore stays
    UNKNOWN, and the search stops after the second read has gained nothing; against a
    6 s clip, where the first read is already asked past the end that clip claims, the
    same read shape answers NO_PICTURE.
    """
    def answer_of(start: F, end: F) -> PictureWindow:
        return fake_window(start, end, [F(5), F(199, 25)])
    probe = FakeProbe(answer_of)
    index = FrameIndex(make_long_video(tmp_path, seconds=60), 0, window=F(2), probe=probe)
    answer = index.answer_at_or_after(F(8))
    assert probe.asks[0] == (F(7), F(9)), probe.asks[0]
    assert index.proven_spans() == ((F(5), F(199, 25)),), (
        f"a read past the material extends the span to its last picture only: "
        f"{index.proven_spans()}"
    )
    assert answer.pts is None and answer.status is AnswerStatus.UNKNOWN
    assert answer.coverage == (F(5), F(199, 25))
    assert index.probes == 2, f"{index.probes} reads; the second gained nothing, stop there"
    assert index.proven_spans()[0][1] < index.declared_end.end

    short = FrameIndex(make_colour_steps(tmp_path), 0, window=F(2), probe=FakeProbe(answer_of))
    edge = short.answer_at_or_after(F(8))
    assert edge.pts is None and edge.status is AnswerStatus.NO_PICTURE
    assert short.declared_end.end == F(6), "the container's claim, used only to close this one"
    assert short.probes == 1, "the read was already asked past the end this file claims"


def test_the_window_cache_stays_bounded_and_forgets_the_far_side(tmp_path):
    """Bounded eviction: a scrubbing user keeps the neighbourhood they are in.

    This also pins the eviction rule, which before discarded the window *nearest*
    the read that had just been paid for - sorting by distance descending and
    popping the end threw away the newest read, so a scrub spent a fresh probe on
    every step instead of reusing the window it had just decoded.
    """
    probe = FakeProbe(
        lambda start, end: fake_window(start, end, [p for p in PICTURES if start - 1 <= p < end])
    )
    index = FrameIndex(make_colour_steps(tmp_path), 0, window=F(2), max_windows=2, probe=probe)
    assert index.at_or_after(F(1)) == F(1)
    assert index.at_or_after(F(3)) == F(3)
    assert index.at_or_after(F(5)) == F(5)
    spans = index.cached_spans()
    assert len(spans) == index.max_windows == 2, f"the cache grew past its bound: {spans}"
    assert spans == ((F(3), F(5)), (F(4), F(6))), (
        f"the ground the last question needed should be what survives: {spans}"
    )
    before = index.probes
    assert index.answer_at_or_after(F(5)).pts == F(5)
    assert index.probes == before, "a question these reads already answered costs nothing"
    index.forget()
    assert index.cached_spans() == ()


def test_a_replaced_file_makes_the_index_read_the_new_file(tmp_path):
    """Identity scoping without media: the previous file's answers must not be reused."""
    source = make_colour_steps(tmp_path)
    pictures = {"before": [F(1)], "after": [F(1, 2)]}
    state = {"key": "before"}
    probe = FakeProbe(
        lambda start, end: fake_window(
            start, end, [p for p in pictures[state["key"]] if start <= p < end]
        )
    )
    index = FrameIndex(source, 0, window=F(2), probe=probe)
    assert index.answer_at_or_after(F(0)).pts == F(1)
    assert index.probes == 1

    state["key"] = "after"
    source.write_bytes(source.read_bytes() + b"extra bytes make a different file")

    answer = index.answer_at_or_after(F(0))
    assert index.invalidations == 1
    assert index.probes == 2, "the cached window belonged to the previous file"
    assert answer.pts == F(1, 2), "the answer now comes from a read of the new file"
