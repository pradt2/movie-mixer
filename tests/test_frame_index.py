"""The bounded picture-timestamp index: answers, edges and the probe budget.

The index exists so navigation can ask "which real frame is next?" without reading
a whole film. What is checked here is therefore both the answer *and* the cost:
one question must not turn into a scan, and a question with no answer must come
back as "no frame that way" instead of spinning.

The regression that matters most is ``test_a_question_answered_inside_one_window``:
an earlier version of the search moved its cursor to the edge of the window it had
just read before asking that window the question, and when the answer was inside
the window it looped forever without ever spending another probe. It presented
itself as an export that never finished, because the render path asks the same
question to place a picture item's sampling grid.
"""

import itertools
from fractions import Fraction as F

import pytest

from media_fixtures import HAS_FFMPEG, make_colour_steps, make_long_video, make_vfr
from video_editor.media.framegrab import source_frame_times
from video_editor.media.frameindex import MAX_PROBES_PER_QUERY, FrameIndex

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")


def test_a_question_answered_inside_one_window(tmp_path):
    """A 6 s clip: the answer is in the first window, and only that window is read.

    Regression for an infinite loop (not a wrong answer): the search used to move
    to the window's edge before consulting it, so it re-asked a question the same
    window could never answer, forever.
    """
    source = make_colour_steps(tmp_path)
    index = FrameIndex(source, 0)
    assert index.at_or_after(F(1)) == F(1), "a real 25 fps frame sits exactly at 1 s"
    assert index.probes == 1, f"one 20 s window covers the whole clip, got {index.probes}"
    assert index.at_or_before(F(1)) == F(1)
    assert index.at_or_after(F(21)) is None, "past the end of the stream is no frame"
    assert index.probes <= MAX_PROBES_PER_QUERY, (
        f"a question with no answer spent {index.probes} probes "
        f"(budget {MAX_PROBES_PER_QUERY})"
    )


def test_edges_answer_none_rather_than_guessing(tmp_path):
    source = make_colour_steps(tmp_path)
    index = FrameIndex(source, 0)
    assert index.step(F(0), -1) is None, "nothing before the first frame"
    stamps = [frame.pts for frame in source_frame_times(source, 0)]
    last = stamps[-1]
    assert index.step(last, +1) is None, "nothing after the last frame"
    assert index.step(last, -1) == stamps[-2]
    assert index.at_or_before(F(0)) == F(0), "the first frame is at the axis origin"


def test_stepping_uses_real_timestamps_on_irregular_material(tmp_path):
    """Variable frame rate: neighbours are the stream's own, not a nominal rate."""
    source = make_vfr(tmp_path)
    stamps = [frame.pts for frame in source_frame_times(source, 0)]
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
    index = FrameIndex(source, 0)
    stamps = [frame.pts for frame in source_frame_times(source, 0)]
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
    found = index.frames_in(F(1), F(2))
    stamps = [frame.pts for frame in source_frame_times(source, 0)]
    inside = [pts for pts in stamps if F(1) <= pts < F(2)]
    assert found == tuple(inside), "the span must be exactly the stream's own frames"
    assert index.probes <= MAX_PROBES_PER_QUERY
    assert index.frames_in(F(2), F(2)) == ()
