"""Which picture actually came out, measured frame by frame.

These are the acceptance tests for frame identity. Every fixture carries its own
frame number as a colour (see ``media_fixtures.identity_rgb``), so "the picture is
one frame off" is a *different number* rather than a slightly blurry match, and the
comparison is exact arithmetic rather than a judgement about similarity.

Two kinds of test live here, deliberately kept apart:

(a) **The exact mathematical pick.** What an output frame must show is worked out in
    this file, in integer output indices and exact ``Fraction`` seconds: index ``n``
    presents ``n / frame_rate``; the clip that owns that frame is the one whose
    half-open output interval contains that time; it samples
    ``source_in + (time - output_start) * rate``; the picture is the last *decoded*
    picture of the stream at or before that time, found with ``bisect`` in the
    fixture's measured PTS list (:func:`reference_pick`). Nothing in that chain asks
    the planner how it intends to sample, so the planner cannot mark its own
    homework - and when the two disagree the diff names both answers.
(b) **Agreement between paths.** The still viewer, an independently rendered range
    and a whole render must show the same decoded pictures, including at a range
    join. That comparison needs no arithmetic at all: a join that shifts pictures is
    a defect however self-consistent each path looks on its own.

The fixtures are measured, not trusted: decoded picture count, real PTS and gaps,
``pict_type`` census (``-bf`` on the command line is not evidence that a file holds
B-frames - the census is), the recorded sample aspect ratio, and the identity colour
read back out of each decoded frame. One measurement pass per fixture file
(:func:`media_fixtures.picture_facts`), never one per expected frame.

The single documented exception is ``allow_one_frame`` in :func:`assert_matches`:
FFmpeg quantises retimed source timestamps to the container's own ticks before it
chooses a picture, so a tempo that sends them between ticks can show the picture one
place later in the stream. Its budget is a measured count, it is never widened to let
a test pass, and every tolerated difference must still be exactly one picture.
"""

import itertools
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from fractions import Fraction as F
from pathlib import Path
from subprocess import run

import pytest

from media_fixtures import (
    HAS_FFMPEG,
    PictureFacts,
    identity_at,
    identity_rgb,
    make_identity_anamorphic,
    make_identity_bframes,
    make_identity_fine_timescale,
    make_identity_offset,
    make_identity_vfr,
    make_identity_video,
    picture_facts,
    source_from_probe,
)
from video_editor.core.commands import AddClipCommand, RegisterSourceCommand
from video_editor.core.model import OutputSettings, Project, Track, TrackKind
from video_editor.media.framegrab import StillError, extract_composition_still, stream_timing
from video_editor.render.graph import LOSSLESS_INTERMEDIATE, build_job
from video_editor.render.plan import build_plan

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")

FFMPEG = "ffmpeg"
T1000_1001 = F(1000, 1001)
T1001_1000 = F(1001, 1000)
ZERO = F(0)
#: the output rate nearly every case here is measured on
RATE = F(25)


def video_project(path: Path, asset_id: str, frame_rate: F, size=(64, 48)) -> Project:
    """A one-track project around one real picture stream, registered by probing it.

    The stream record comes from probing, never hand-written: a test that invented
    the stream end could place a clip into material the file does not contain and then
    blame the renderer for what it found there. The decoded end is asserted separately
    in each case that depends on it.
    """
    project = Project(output=OutputSettings(size[0], size[1], frame_rate, 48000, "stereo"))
    project = RegisterSourceCommand(source_from_probe(path, 0, asset_id)).apply(project)
    return project.with_tracks((Track("v1", TrackKind.VIDEO, "V1"),))


def place(
    project: Project,
    clip_id: str,
    source_in: F,
    source_out: F,
    output_start: F = ZERO,
    rate: F = F(1),
    asset_id: str = "src",
) -> Project:
    """Put one clip on the track: output length follows from its rate."""
    return AddClipCommand(
        asset_id=asset_id,
        stream_index=0,
        track_id="v1",
        output_start=output_start,
        source_in=source_in,
        source_out=source_out,
        rate=rate,
        clip_id=clip_id,
    ).apply(project)


def time_bases(paths: dict[str, Path]):
    """A real container-time-base lookup, as the application gives the graph."""

    def lookup(asset_id: str, stream_index: int) -> F | None:
        path = paths.get(asset_id)
        if path is None:
            return None
        try:
            return stream_timing(path, stream_index).time_base
        except Exception:
            return None

    return lookup


def measured_lookups(paths: dict[str, Path]) -> dict:
    """The stream measurements the exporter gives the graph, from a real frame index.

    A graph that is only told the container's tick samples a fine-timescale stream on
    the container-quantised chain, because nothing there proves its tempo; the
    application (``render.exporter.RenderRequest``) also hands it the first and last
    readable pictures and the timestamps of the reading window. This is the same three
    lookups, built the same way, so a test can measure both configurations.
    """
    from video_editor.media.frameindex import FrameIndex

    found: dict[tuple[str, int], FrameIndex] = {}

    def index_for(asset_id: str, stream_index: int) -> FrameIndex:
        key = (asset_id, stream_index)
        if key not in found:
            found[key] = FrameIndex(paths[asset_id], stream_index, "ffprobe")
        return found[key]

    def window(asset_id: str, stream_index: int, start: F, end: F):
        span = index_for(asset_id, stream_index).answer_frames_in(start, end)
        return span.pts if span.complete else None

    return {
        "first_frame_of": lambda a, s, t: index_for(a, s).at_or_after(t),
        "previous_frame_of": lambda a, s, t: index_for(a, s).at_or_before(t),
        "window_pictures_of": window,
    }


def lossless_frames(
    project: Project, paths: dict[str, Path], out_start: F, out_end: F, dest: Path,
    lookups: dict | None = None,
) -> tuple[object, list[bytes]]:
    """Render a range losslessly and return ``(plan, every frame as RGB24)``.

    The frames come back whole rather than reduced to a number, so a test can ask
    where in the frame the picture actually is, not only which picture it is.

    ``lookups`` adds the stream measurements the application hands the graph
    (``first_frame_of``/``previous_frame_of``/``window_pictures_of``): a composition
    whose exact tempo can only be proved against the stream's own picture stamps
    samples approximately without them, which is worth being able to measure both
    ways. ``time_base_of`` is always given.
    """
    plan = build_plan(project, out_start, out_end)
    sources = {(s.asset_id, s.stream_index): s for s in project.sources}
    job = build_job(
        plan, paths, sources, dest, FFMPEG, LOSSLESS_INTERMEDIATE,
        time_base_of=time_bases(paths),
        **(lookups or {}),
    )
    proc = run(job.args, capture_output=True, timeout=600)
    assert proc.returncode == 0, proc.stderr.decode()[:800]
    raw = run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(dest), "-map", "0:v:0",
         "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
        timeout=600,
    )
    assert raw.returncode == 0, raw.stderr.decode()[:400]
    stride = plan.width * plan.height * 3
    frames = [raw.stdout[i * stride: (i + 1) * stride] for i in range(len(raw.stdout) // stride)]
    assert len(frames) == plan.total_frames, (
        f"the file holds {len(frames)} frames, the plan called for {plan.total_frames}"
    )
    return plan, frames


def render(
    project: Project, paths: dict[str, Path], out_start: F, out_end: F, dest: Path,
    lookups: dict | None = None,
):
    """Render a range losslessly and return (plan, measured identities per frame).

    ``None`` in the measured list means every pixel of that frame is zero: black is
    what the composition writes where there is no picture, and a fixture's own frame
    0 is lifted off black (``media_fixtures.IDENTITY_LIFT``), so the two are
    different pixels and this distinguishes them by the pixels, not by a guess.
    """
    plan, frames = lossless_frames(project, paths, out_start, out_end, dest, lookups)
    measured = [
        None if all(byte == 0 for byte in frame) else identity_at(frame, plan.width, plan.height)
        for frame in frames
    ]
    return plan, measured


# ---------------------------------------------------------------- the reference


@dataclass(frozen=True)
class Placed:
    """One clip as the test placed it: source slice, output start, tempo."""

    clip_id: str
    source_in: F
    source_out: F
    output_start: F = ZERO
    rate: F = F(1)

    @property
    def output_end(self) -> F:
        """Output length = source seconds / rate (rate is source seconds per output s)."""
        return self.output_start + (self.source_out - self.source_in) / self.rate


def ceil_frames(seconds: F, frame_rate: F) -> int:
    """``ceil(seconds * frame_rate)``, worked out here instead of asked of the plan.

    Both questions - how many frames a length needs, and which frame covers a point -
    use this one rounding, so a frame count below is the same lattice the plan claims
    and can be compared with it.
    """
    value = F(seconds) * F(frame_rate)
    return -((-value.numerator) // value.denominator)


def reference_pick(
    placed: tuple[Placed, ...], frame_rate: F, index: int, facts: PictureFacts
) -> int | None:
    """The picture global output frame ``index`` must show - arithmetic, no backend.

    ``facts`` is the measured fixture (its decoded PTS list): the stream decides where
    its real pictures are, so this says nothing about frame rates and works unchanged
    for variable frame rate and a nonzero start. ``None`` is black: no clip covers
    that output time.
    """
    moment = F(index) / frame_rate
    owners = [clip for clip in placed if clip.output_start <= moment < clip.output_end]
    if not owners:
        return None
    assert len(owners) == 1, f"output {moment} lies inside {len(owners)} clips of one track"
    clip = owners[0]
    legal = [
        i for i, pts in enumerate(facts.pictures) if clip.source_in <= pts < clip.source_out
    ]
    assert legal, (
        f"clip {clip.clip_id} slices [{clip.source_in}, {clip.source_out}) of "
        f"{facts.path.name}, which holds nothing there ({facts.pictures[0]} .. {facts.last} "
        "is what the file actually presents)"
    )
    wanted = clip.source_in + (moment - clip.output_start) * clip.rate
    if wanted < facts.pictures[legal[0]]:
        return legal[0]  # the head grid point precedes any readable picture: hold it
    if wanted >= facts.pictures[legal[-1]]:
        return legal[-1]  # the tail grid point is past the last one: hold it
    return bisect_right(facts.pictures, wanted, legal[0], legal[-1] + 1) - 1


def reference_range(
    placed: tuple[Placed, ...], frame_rate: F, first_index: int, count: int,
    facts: PictureFacts,
) -> list[int | None]:
    """Expected identities for output frame indices ``first_index .. +count``."""
    return [reference_pick(placed, frame_rate, first_index + k, facts) for k in range(count)]


def assert_identity_fixture(facts: PictureFacts, count: int, first: int = 0) -> None:
    """Make a fixture prove what its name claims before any expectation uses it.

    A file that quietly lost, duplicated or renumbered pictures would make every
    identity below meaningless, so the count and the numbers read back out of the
    decoded frames are both checked.
    """
    assert facts.count == count, (
        f"{facts.path.name}: {facts.count} decoded pictures, the fixture claims {count}"
    )
    assert facts.identities == tuple(range(first, first + count)), (
        f"{facts.path.name}: decoded frames do not carry their own numbers "
        f"({facts.identities[:5]} .. {facts.identities[-5:]})"
    )
    if facts.declared_frames is not None:
        assert facts.declared_frames == count, (
            f"{facts.path.name}: the container declares {facts.declared_frames} frames "
            f"and {facts.count} decode"
        )


def assert_matches(measured, expected, label: str, *, allow_one_frame: int = 0) -> int:
    """Compare rendered pictures with the pictures the composition's arithmetic names.

    Equal frame-for-frame by default. ``allow_one_frame`` is the documented
    exception, used only where the tempo sends retimed source timestamps between
    the container's own ticks: FFmpeg quantises them before it chooses, so such a
    frame may show the picture one place later in the stream. The count is capped
    and every difference must be exactly one place, so a real off-by-N bug cannot
    hide behind the tolerance. The measured table is in ``docs/architecture.md``.
    """
    assert len(measured) == len(expected), f"{label}: {len(measured)} frames rendered, " \
        f"{len(expected)} planned"
    wrong = [
        (i, got, want)
        for i, (got, want) in enumerate(zip(measured, expected, strict=True))
        if got != want
    ]
    if not allow_one_frame:
        assert not wrong, f"{label}: {len(wrong)} of {len(measured)} frames differ; first " \
            f"five (frame, rendered, required): {wrong[:5]}"
        return 0
    assert len(wrong) <= allow_one_frame, (
        f"{label}: {len(wrong)} frames differ, more than the {allow_one_frame} the "
        f"container quantisation measures; first five: {wrong[:5]}"
    )
    for index, got, want in wrong:
        assert got is not None and want is not None and abs(got - want) == 1, (
            f"{label}: frame {index} shows {got} where the contract says {want}: more "
            "than the one-frame quantisation difference that was measured"
        )
    return len(wrong)


def assert_real_pictures(measured, label: str, low: int, high: int) -> None:
    """No invented picture, none from outside the slice, and never out of order.

    This is the part of the contract that holds at *every* tempo, including the
    ones where FFmpeg's tick quantisation moves a pick by one frame.
    """
    ids = [m for m in measured if m is not None]
    assert ids, f"{label}: nothing was rendered"
    outside = [m for m in ids if not low <= m <= high]
    assert not outside, f"{label}: {len(outside)} frames came from outside the slice: " \
        f"{sorted(set(outside))[:5]} (allowed {low}..{high})"
    backwards = [(a, b) for a, b in itertools.pairwise(ids) if b < a]
    assert not backwards, f"{label}: pictures went backwards: {backwards[:5]}"


# ---------------------------------------------------------------- retimed clips


@pytest.mark.parametrize(
    "rate,label,expected_frames,allow_one_frame",
    [
        # a [1,6) source slice is 5 source seconds, so the output is 5/rate seconds
        # and ceil(5/rate * 25) frames: 125, 120, 131, 125, 126, ceil(125/4)=32, 500
        (F(1), "identity", 125, 0),
        (F(25, 24), "24 fps material at 25 fps output", 120, 0),
        (F(24, 25), "25 fps material at 24 fps output", 131, 0),
        (T1001_1000, "NTSC pull-down style 0.1% speed-up", 125, 0),
        (T1000_1001, "0.1% slow-down: ticks land between frames", 126, 24),
        (F(4), "4x fast forward", 32, 0),
        (F(1, 4), "4x slow motion", 500, 0),
    ],
)
def test_every_output_frame_shows_the_picture_the_arithmetic_names(
    tmp_path, rate, label, expected_frames, allow_one_frame
):
    source = make_identity_video(tmp_path, count=150)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 150)
    assert facts.uniform_spacing == F(1, 25), f"constant-rate fixture: {facts.spacings}"
    clip = Placed("clip", F(1), F(6), rate=rate)
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out, rate=rate)
    end = project.output_end()
    assert end == F(5) / rate, "output length follows from the tempo"
    plan, measured = render(project, {"src": source}, ZERO, end, tmp_path / "whole.mkv")
    # a speed-up legitimately has fewer frames; what is not legitimate is a plan
    # that disagrees with the arithmetic, because drift is then impossible to see
    assert plan.total_frames == expected_frames, (
        f"{label}: ceil(5/{rate} * 25) = {expected_frames} output frames, "
        f"the plan made {plan.total_frames}"
    )
    expected = reference_range((clip,), RATE, 0, plan.total_frames, facts)
    assert_matches(measured, expected, f"{label} at rate {rate}",
                   allow_one_frame=allow_one_frame)
    first = bisect_left(facts.pictures, F(1))  # the slice's first real picture
    last = bisect_left(facts.pictures, F(6)) - 1  # and its last
    assert (first, last) == (25, 149), "the slice is not where the fixture says it is"
    assert_real_pictures(measured, f"{label} at rate {rate}", first, last)


def test_a_fractional_source_position_stays_on_the_right_picture(tmp_path):
    """An in-point that is not a frame boundary must not move the pictures.

    ``1/3`` s lies between source frames 8 (0.32 s) and 9 (0.36 s), so the clip's
    first legal picture is 9 and its first output frame - which samples 1/3 s, where
    no picture is - holds that picture. After that every frame shows the last picture
    at or before its own sampling time: output 25 samples 1/3 + 1 = 4/3 s, and
    33/25 = 1.32 <= 4/3 < 34/25 = 1.36; output 75 samples 1/3 + 3 = 10/3 s, and
    83/25 = 3.32 <= 10/3 < 84/25 = 3.36.
    """
    source = make_identity_video(tmp_path, count=150)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 150)
    clip = Placed("clip", F(1, 3), F(5))
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out)
    plan, measured = render(project, {"src": source}, ZERO, project.output_end(),
                            tmp_path / "fractional.mkv")
    assert plan.total_frames == ceil_frames(F(5) - F(1, 3), RATE) == 117
    expected = reference_range((clip,), RATE, 0, plan.total_frames, facts)
    assert (expected[0], expected[1], expected[2]) == (9, 9, 10), (
        "the head of a clip starting between frames must hold its first legal picture"
    )
    assert (expected[25], expected[75]) == (33, 83), (
        "the hand-worked picks at output 25 and 75 are not what the arithmetic gives"
    )
    assert_matches(measured, expected, "fractional source in at rate 1")


def test_two_clips_meeting_inside_a_frame_keep_their_pictures(tmp_path):
    """Half-frame boundaries: both edges snap forward to the same frame.

    The cut is at 1/3 s: ceil(25/3) = 9, so the first clip owns output frames 0-8 and
    the second owns 9-24. No frame is counted twice and none is lost, and each frame
    shows its own clip's picture: output 8 (0.32 s) is the last frame of the first
    clip and shows source 8; output 9 (0.36 s) belongs to the second clip, samples
    2 + (0.36 - 1/3) = 2.0266.. s, and shows source 50 (2.0 s, the clip's in-point).
    """
    source = make_identity_video(tmp_path, count=150)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 150)
    first = Placed("a", ZERO, F(1, 3))
    second = Placed("b", F(2), F(4), output_start=F(1, 3))
    project = video_project(source, "src", RATE)
    project = place(project, "a", first.source_in, first.source_out)
    project = place(project, "b", second.source_in, second.source_out, output_start=F(1, 3))
    plan, measured = render(project, {"src": source}, ZERO, F(1), tmp_path / "half.mkv")
    assert plan.total_frames == 25, "ceil(1 s * 25) output frames"
    assert plan.total_frames == ceil_frames(F(1), RATE)
    expected = reference_range((first, second), RATE, 0, plan.total_frames, facts)
    assert expected[8] == 8, "the first clip lost or gained a frame at the cut"
    assert expected[9] == 50, "the second clip's first picture is not its in-point"
    assert_matches(measured, expected, "half-frame cut")
    owners = [
        plan.picture_at(F(i, 25)).clip.id for i in range(plan.total_frames)
    ]
    assert owners[:9] == ["a"] * 9 and owners[9:] == ["b"] * 16, "the cut is not at frame 9"


# -------------------------------------------------------------------- the paths


def test_a_range_render_shows_exactly_the_frames_of_the_whole_render(tmp_path):
    """The preview path cannot promise a picture the export would not write.

    Rendered as two ranges and joined, the pictures must be identical to one render
    of the whole range - including at the join, where an unanchored sampling grid
    restarts and the frames near it shift.

    Hand-worked: the clip spans 6 - 1/3 = 17/3 source seconds at 25/24, so the output
    is (17/3) * 24/25 = 136/25 = 5.44 s, i.e. exactly 136 output frames; its middle is
    68/25 = 2.72 s, which is frame 68's own time, so both ranges are 68 frames.

    Measured here, for whoever chases it: the two ranges and the whole render disagree
    with each other at output 87, 111 and 135 - the second range gives pictures 98,
    123 and 148, the whole render gives 99, 124 and 149 - and it is the whole render
    that contradicts the arithmetic at those frames. Separately, both paths give one
    picture later than the arithmetic at output 15, 39 and 63, which is the container
    tick quantisation documented above; that shared effect is why this test compares
    the paths with each other and not with the arithmetic.
    """
    source = make_identity_video(tmp_path, count=150)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 150)
    clip = Placed("clip", F(1, 3), F(6), rate=F(25, 24))
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out, rate=F(25, 24))
    end = project.output_end()
    assert end == F(136, 25), "output length follows from the tempo"
    plan, whole = render(project, {"src": source}, ZERO, end, tmp_path / "whole.mkv")
    assert plan.total_frames == 136
    split = F(68, 25)
    _, first = render(project, {"src": source}, ZERO, split, tmp_path / "first.mkv")
    _, second = render(project, {"src": source}, split, end, tmp_path / "second.mkv")
    assert len(first) == len(second) == 68, f"ranges gave {len(first)} + {len(second)} frames"
    assert len(first) + len(second) == len(whole), (
        f"ranges gave {len(first)}+{len(second)} frames for {len(whole)}"
    )
    assert_matches(first + second, whole, "joined ranges against the whole render")


def test_a_still_is_the_frame_the_export_writes_at_that_index(tmp_path):
    """One sampling contract: the still viewer and the export agree everywhere.

    Checked at the places a difference hides: an exact frame boundary, a fraction of a
    frame past one, a fraction before one, and the last frame of the output - each
    inside a clip whose rate is not 1, where the sampling time and the source frames
    are on different grids.

    Two questions, hand-worked separately and never read from a helper in
    ``render/plan.py``. What the export writes is the render-range question: 5 source
    seconds at 25/24 is 24/5 = 4.8 s, and ``ceil(4.8 * 25) = 120`` frames, indices
    0..119. What a playhead at ``t`` shows is the inspection question, ``floor(t * 25)``:
    ``i/25`` is frame ``i``'s own first instant (``i/25 * 25 = i``); ``i/25`` + 1/1000
    is still inside frame ``i``, because ``(i/25 + 1/1000) * 25 = i + 1/40`` and that
    floors to ``i``; ``i/25`` - 1/1000 is still inside frame ``i - 1``, because
    ``(i/25 - 1/1000) * 25 = i - 1/40``; and ``1/25`` is where frame 1 begins.

    Measured here, for whoever chases it: every probe up to the last frame succeeds,
    and the *last* frame of the composition - output 119, which the export does write,
    at source sampling 143/24 = 5.958333 s - fails as an empty decode: the still path's
    one-frame window reads back to 5.9995 s, FFmpeg reports "Output file is empty", and
    the failure surfaces as StillError. So the still and the export do not agree at the
    end of a clip whose slice ends at the stream's end.
    """
    source = make_identity_video(tmp_path, count=150)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 150)
    clip = Placed("clip", F(1), F(6), rate=F(25, 24))
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out, rate=F(25, 24))
    end = project.output_end()
    assert end == F(24, 5)
    plan, exported = render(project, {"src": source}, ZERO, end, tmp_path / "out.mkv")
    assert plan.total_frames == 120, "ceil(24/5 * 25) frames"
    paths = {"src": source}
    # each probe says the frame the playhead shows, worked out above; the last frame
    # of the output comes last, because it is where an end-of-slice failure would
    # otherwise hide the cases that do work
    probes = [(F(i, 25), i) for i in (0, 1, 40, 41, 118)]  # exactly on a boundary
    # 1/1000 s into a frame: the frame already covering the playhead, never the next
    # one (the next one is footage this playhead has not reached)
    probes += [(F(i, 25) + F(1, 1000), i) for i in (0, 1, 40)]
    # 1/1000 s before a boundary: the earlier frame is still the one on screen
    probes += [(F(i, 25) - F(1, 1000), i - 1) for i in (1, 40)]
    probes.append((F(1, 25), 1))  # the next boundary: frame 1 has started
    probes.append((F(119, 25), 119))  # the last frame the composition writes
    for moment, frame_index in probes:
        still = extract_composition_still(project, paths, moment)
        assert still.frame_index == frame_index, (
            f"output {float(moment):.6f}s: expected frame {frame_index}, "
            f"the viewer chose {still.frame_index}"
        )
        shown = identity_at(still.rgb, still.width, still.height)
        assert exported[still.frame_index] == shown, (
            f"output {float(moment):.6f}s: still and export show different pictures "
            f"({shown} against {exported[still.frame_index]})"
        )
        assert shown == reference_pick((clip,), RATE, still.frame_index, facts), (
            f"output frame {still.frame_index}: both paths show {shown}, "
            "the arithmetic names something else"
        )
        assert still.grid_exact, "the container time base is readable for this fixture"
        assert still.decoded_source_pts is not None, "the picture's source time is measurable"


def test_a_still_after_a_cut_shows_the_next_clip(tmp_path):
    """Frame identity across an edit: before, on, and one frame after the boundary.

    Source frame 100 is at 100/25 = 4 s, so clip B's slice is [4, 6) - the rest of the
    fixture - placed at output 2 s. Output 49 (1.96 s) is the last frame of A and shows
    source 49; output 50 (2 s) is the first frame of B, samples 4 + 0 = 4 s and shows
    source 100; output 51 (2.04 s) samples 4.04 s and shows source 101.
    """
    source = make_identity_video(tmp_path, count=150)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 150)
    assert facts.pictures[100] == F(4), "the fixture's 101st picture is at 4 s"
    a = Placed("a", ZERO, F(2))
    b = Placed("b", F(100, 25), F(150, 25), output_start=F(2))
    project = video_project(source, "src", RATE)
    project = place(project, "a", a.source_in, a.source_out)
    project = place(project, "b", b.source_in, b.source_out, output_start=F(2))
    assert project.output_end() == b.output_end == F(4), "B is 2 s long at rate 1"
    paths = {"src": source}
    before = extract_composition_still(project, paths, F(49, 25))
    at = extract_composition_still(project, paths, F(2))
    after = extract_composition_still(project, paths, F(51, 25))
    assert (before.frame_index, at.frame_index, after.frame_index) == (49, 50, 51)
    assert before.clip_id == "a" and at.clip_id == "b" and after.clip_id == "b"
    assert identity_at(before.rgb, before.width, before.height) == 49
    assert identity_at(at.rgb, at.width, at.height) == 100
    assert identity_at(after.rgb, after.width, after.height) == 101
    assert at.decoded_source_pts == F(100, 25)
    assert after.decoded_source_pts == F(101, 25)
    expected = reference_range((a, b), RATE, 49, 3, facts)
    assert expected == [49, 100, 101], "the arithmetic disagrees with these frame identities"


# ------------------------------------------------ awkward streams and bounds


def test_bframes_do_not_change_which_picture_is_which(tmp_path):
    """MP4/H.264 with B-frames: presentation order is what the plan addresses."""
    source = make_identity_bframes(tmp_path, count=100)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 100)
    assert facts.of_type("B") > 0, (
        f"the fixture has no B-frames to reorder, so this tests nothing: "
        f"{facts.pict_types} (decode order cannot differ from presentation order)"
    )
    assert facts.uniform_spacing == F(1, 25), f"{facts.spacings}"
    clip = Placed("clip", F(1, 2), F(4), rate=F(25, 24))
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out, rate=F(25, 24))
    plan, measured = render(project, {"src": source}, ZERO, project.output_end(),
                            tmp_path / "b.mkv")
    # 3.5 source seconds at 25/24 is 3.36 s of output: exactly 84 frames
    assert plan.total_frames == ceil_frames(F(7, 2) / F(25, 24), RATE) == 84
    assert_matches(measured, reference_range((clip,), RATE, 0, plan.total_frames, facts),
                   "B-frame source")
    still = extract_composition_still(project, {"src": source}, F(1))
    assert still.decoded_source_pts in facts.pictures, (
        f"the reported PTS {still.decoded_source_pts} is not a picture of this stream"
    )


def test_a_stream_that_does_not_start_at_zero_keeps_its_own_origin(tmp_path):
    """Nonzero start time: asset-local addressing, seek arithmetic included.

    Measured on the fixture: 100 pictures, the first at 5/2 s and the last at 323/50
    s, 1/25 s apart, so the presentation runs 5/2 .. 13/2 s and no further. The slice
    here is [7/2, 13/2): at 25/24 that is 3 * 24/25 = 72/25 s of output, exactly 72
    frames, sampling 7/2 + k/24 s - the last of them 155/24 = 6.4583 s, which is
    before the last picture at 6.46 s, so the last output frame shows source 98.

    The *container* of this fixture claims a longer stream than that (ffprobe gives
    ``start_time`` 2.5 and ``duration`` 6.5, i.e. an end at 9 s, while 100 frames at
    25 fps decode to 4 s), so a slice chosen from the probed end would reach into
    material the file does not hold. The bounds the application records are TASK_12e's
    question; what the tests may not do is plan against numbers the media disproves.
    """
    source = make_identity_offset(tmp_path, count=100)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 100)
    assert facts.pictures[0] == F(5, 2) and facts.last == F(323, 50), (
        f"{facts.pictures[0]} .. {facts.last}"
    )
    assert facts.uniform_spacing == F(1, 25)
    project = video_project(source, "src", RATE)
    origin = project.source("src", 0).origin
    assert origin == facts.pictures[0] == F(5, 2), "the fixture's presentation starts at 2.5 s"
    clip = Placed("clip", F(7, 2), F(13, 2), rate=F(25, 24))
    project = place(project, "clip", clip.source_in, clip.source_out, rate=F(25, 24))
    plan, measured = render(project, {"src": source}, ZERO, project.output_end(),
                            tmp_path / "o.mkv")
    # 3 source seconds at 25/24 is 3 * 24/25 = 72/25 s of output: 72 frames at 25 fps
    assert plan.total_frames == ceil_frames(F(3) / F(25, 24), RATE) == 72
    expected = reference_range((clip,), RATE, 0, plan.total_frames, facts)
    assert expected[0] == 25 and expected[-1] == 98, (
        f"the slice [7/2, 13/2) runs from picture 25 to picture 99, and the last "
        f"sampling time 155/24 s precedes picture 99 at 323/50 s: {expected[-3:]}"
    )
    assert_matches(measured, expected, "nonzero stream origin")


def test_variable_frame_rate_material_uses_the_real_timestamps(tmp_path):
    """Irregular spacing: a nominal-rate assumption lands on the wrong pictures.

    Measured on the fixture: 150 pictures, the first 100 exactly 1/25 s apart and the
    last 50 about 1/12 s apart - a gap 1/1000 s ticks cannot represent, so the decoded
    gaps come out as 83/1000 and 21/250. The last picture is at 8083/1000 s and the
    container claims 8.166 s, not the 9 s that "100 at 25 fps plus 50 at 12 fps" would
    suggest, so the slice here is [3, 8), inside what the file really holds.
    """
    source = make_identity_vfr(tmp_path)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 150)
    fast = facts.pictures[:100]
    assert {b - a for a, b in itertools.pairwise(fast)} == {F(1, 25)}, "fast section"
    slow = facts.pictures[100:]
    assert all(F(1, 16) < b - a < F(1, 8) for a, b in itertools.pairwise(slow)), (
        f"slow section is near 12 fps: {facts.spacings}"
    )
    assert F(1, 12) not in facts.spacings, "1/12 is not representable on this time base"
    assert facts.pictures[99] == F(99, 25) and facts.pictures[100] == F(4), (
        f"picture 100 (the 101st) lands exactly at 4 s: {facts.pictures[99:102]}"
    )
    assert facts.last < F(9), f"the stream holds {facts.last}, not 9 s of pictures"
    clip = Placed("clip", F(3), F(8), rate=F(5, 4))
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out, rate=F(5, 4))
    plan, measured = render(project, {"src": source}, ZERO, project.output_end(),
                            tmp_path / "vfr.mkv")
    assert plan.total_frames == ceil_frames(F(5) / F(5, 4), RATE) == 100
    expected = reference_range((clip,), RATE, 0, plan.total_frames, facts)
    assert expected[0] == 75, "picture 75 of this stream sits at exactly 3 s"
    assert_matches(measured, expected, "VFR source")
    first = bisect_left(facts.pictures, F(3))
    last = bisect_left(facts.pictures, F(8)) - 1
    assert_real_pictures(measured, "VFR source", first, last)


def test_a_retimed_tail_shows_the_pictures_the_grid_actually_asks_for(tmp_path):
    """Sampling must stop where the sampling times stop - not pad to the clip's end.

    ``[40/25, 50/25)`` at rate 5/4 on a 25 fps output is (10/25)/(5/4) = 8/25 s, i.e.
    eight output frames, sampling 1.60, 1.65, 1.70, 1.75, 1.80, 1.85, 1.90 and 1.95 s.
    Those grid points ask for pictures 40, 41, 42, 43, 45, 46, 47 and 48: every one of
    them is inside the slice, so nothing is held and the clip's last picture (49, at
    1.96 s) is never asked for. It must not appear.
    """
    source = make_identity_video(tmp_path, count=50)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 50)
    clip = Placed("clip", F(40, 25), F(50, 25), rate=F(5, 4))
    step = clip.rate / RATE  # source seconds per output frame: 1/20 s
    sampling_times = [clip.source_in + k * step for k in range(8)]
    assert sampling_times == [F(160 + 5 * k, 100) for k in range(8)], (
        f"the grid asks for {sampling_times}"
    )
    hand = [40, 41, 42, 43, 45, 46, 47, 48]
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out, rate=clip.rate)
    assert project.output_end() == F(8, 25)
    plan, measured = render(project, {"src": source}, ZERO, project.output_end(),
                            tmp_path / "tail.mkv")
    assert plan.total_frames == ceil_frames(F(8, 25), RATE) == 8
    assert reference_range((clip,), RATE, 0, plan.total_frames, facts) == hand, (
        "the reference pick disagrees with the hand-worked table"
    )
    assert_matches(measured, hand, "retimed slice ending on the last legal frame")
    assert 49 not in measured, "the tail padded with a picture the grid never asked for"


def test_a_slice_that_runs_out_of_pictures_holds_its_last_one(tmp_path):
    """The picture holds only when the grid runs past the last one it can read.

    ``[48/25, 50/25)`` at rate 1/2 is (2/25)/(1/2) = 4/25 s: four output frames at
    0.02 s of source each, sampling 1.92, 1.94, 1.96 and 1.98 s. The slice holds
    pictures 48 (1.92 s) and 49 (1.96 s), so the answer is 48, 48, 49, 49: the last
    picture is shown twice because the last sampling time is after it, not because the
    renderer decided to finish the clip off.
    """
    source = make_identity_video(tmp_path, count=50)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 50)
    clip = Placed("clip", F(48, 25), F(50, 25), rate=F(1, 2))
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out, rate=clip.rate)
    assert project.output_end() == F(4, 25)
    plan, measured = render(project, {"src": source}, ZERO, project.output_end(),
                            tmp_path / "held.mkv")
    assert plan.total_frames == ceil_frames(F(4, 25), RATE) == 4
    hand = [48, 48, 49, 49]
    assert reference_range((clip,), RATE, 0, plan.total_frames, facts) == hand, (
        "the reference pick disagrees with the hand-worked table"
    )
    assert_matches(measured, hand, "slice that runs out of pictures")
    assert measured.count(49) == 2, f"the last picture is held for the tail: {measured}"


def test_a_gap_is_black_and_does_not_steal_a_neighbouring_picture(tmp_path):
    """25 frames of picture, 25 of black, 25 of picture - and black is not frame 0.

    Clip A takes source [0, 1) and clip B source [2, 3), placed at output 2 s: at 25
    fps both are 25 frames, the gap covers output frames 25..49, and B's first frame
    samples exactly 2 s, which is source picture 50 of this 100-frame fixture.

    The identity colour cannot be confused with the composition's black: every channel
    of every fixture frame is lifted off zero, and the renderer's black is all zero
    bytes, so a picture in the gap and a gap where a picture belongs are both visible.
    """
    source = make_identity_video(tmp_path, count=100)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 100)
    assert min(identity_rgb(0)) > 0, "a fixture's frame 0 must not be the colour of no picture"
    assert min(facts.centres[0]) > 0, f"decoded frame 0 reads as black: {facts.centres[0]}"
    a = Placed("a", ZERO, F(1))
    b = Placed("b", F(2), F(3), output_start=F(2))
    project = video_project(source, "src", RATE)
    project = place(project, "a", a.source_in, a.source_out)
    project = place(project, "b", b.source_in, b.source_out, output_start=F(2))
    plan, measured = render(project, {"src": source}, ZERO, F(3), tmp_path / "gap.mkv")
    assert plan.total_frames == 75, "three seconds of 25 fps output"
    hand = list(range(25)) + [None] * 25 + list(range(50, 75))
    assert reference_range((a, b), RATE, 0, plan.total_frames, facts) == hand, (
        "the reference pick disagrees with the hand-worked table"
    )
    assert_matches(measured, hand, "gap")
    assert measured[24] == 24 and measured[25] is None and measured[50] == 50
    assert sum(1 for item in plan.video_items if item.is_black) == 1, "one explicit black item"
    assert [item.frames for item in plan.video_items if item.is_black] == [25]


def test_a_frame_past_the_end_of_the_composition_is_refused_not_invented(tmp_path):
    """Inspection cannot show a frame the composition does not have.

    Two similar requests, two different honest answers. Asking the viewer for a frame
    at or past the end is an error that names the end: there is no picture there, and
    showing black would present a frame the export never writes. Asking the *planner*
    for a range that runs past the end is a request for a longer timeline, and it
    answers with explicit black items - the behaviour documented for the planner since
    TASK_05, checked here so both stay as stated instead of drifting towards each
    other.

    Hand-worked for the range ``[3/2, 3)`` at 25 fps: ceil(1.5 * 25) = 38 and
    ceil(3 * 25) = 75, so frames 38..74 - 37 frames, of which the clip covers 38..49
    (12 frames, showing source 38..49) and 50..74 are black (25 frames).
    """
    source = make_identity_video(tmp_path, count=50)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 50)
    clip = Placed("clip", ZERO, F(2))
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out)
    paths = {"src": source}
    end = project.output_end()
    assert end == F(2)
    for moment in (end, end + F(1, 25), end + F(1)):
        with pytest.raises(StillError, match="ends at"):
            extract_composition_still(project, paths, moment)
    last = extract_composition_still(project, paths, end - F(1, 25))
    assert last.frame_index == 49
    assert identity_at(last.rgb, last.width, last.height) == 49
    plan, measured = render(project, paths, F(3, 2), end + F(1), tmp_path / "edge.mkv")
    assert plan.total_frames == 37, "frames 38..74: 37 frames, explicit black included"
    black = [item for item in plan.video_items if item.is_black]
    assert sum(item.frames for item in black) == 25, "the tail past the end is explicit black"
    hand = list(range(38, 50)) + [None] * 25
    assert reference_range((clip,), RATE, 38, 37, facts) == hand, (
        "the reference pick disagrees with the hand-worked table"
    )
    assert_matches(measured, hand, "range running past the end of the composition")
    assert measured[0] == 38 and measured[-1] is None


# --------------------------------------------------------------------- geometry


# --------------------------------------------------------------------- geometry
#
# Everything above asks *which* picture arrived; this section asks *where it is* and
# *what shape it kept*, because a frame can hold the right content at the wrong shape
# and pass every identity check. The fixtures are solid light grey with a block
# painted white, sized so the block is a **square on screen** (its coded size is the
# recorded sample aspect ratio the other way round), and every expectation below is
# worked out from ``display = coded x SAR`` and the output box - never read back out
# of the renderer being tested.

#: A pixel is picture rather than the composition's black when its brightest channel
#: is above this. Measured on FFmpeg 8.1.3: ``pad``'s bars come back as exactly
#: (0,0,0) through both paths, and the grey field below is painted 161 and reads 161
#: through the still's rgb24 chain and 160 through the export's yuv420p chain.
BLACK_MAX = 24

#: The marker block is painted 255 against that field of 161; measured through the
#: FFV1 round trip the block's interior reads (255,255,255) and the field
#: (160,160,160), so 200 separates them. The block's **edge** column is antialiased
#: by the scale (measured: it reads (237,237,237) for the 5/4 anamorphic case), so a
#: block's extent is stated by this threshold, never by an exact colour match.
MARKER_MIN = 200


def grey_fixture(
    tmp: Path,
    name: str,
    size: tuple[int, int],
    sar: str | None = None,
    marker: tuple[int, int, int, int] | None = None,
    count: int = 5,
    band_rows: int = 0,
    band_value: int = 16,
) -> Path:
    """Lossless solid-grey pictures with an optional white block, at ``sar`` if given.

    Built here rather than in ``media_fixtures`` because these files exist only to be
    measured for shape. ``sar`` goes in as ``setsar=<num>/<den>``: the colon spelling
    is parsed as two options and silently records a different sample aspect ratio,
    which is what made an earlier anamorphic fixture test nothing. ``band_rows`` paints
    that many rows at the top and bottom of the *source* with ``band_value`` - a
    border the source itself carries, dark but not the composition's black, so the
    pixels can say which black a given row is.
    """
    width, height = size
    directory = tmp / f"geometry_{name}"
    directory.mkdir(parents=True, exist_ok=True)
    header = f"P6\n{width} {height}\n255\n".encode()
    rows = []
    for y in range(height):
        field = band_value if y < band_rows or y >= height - band_rows else 161
        row = bytearray()
        for x in range(width):
            inside = bool(marker) and marker[0] <= x < marker[2] and marker[1] <= y < marker[3]
            value = 255 if inside else field
            row += bytes((value, value, value))
        rows.append(bytes(row))
    for n in range(count):
        (directory / f"{n:04d}.ppm").write_bytes(header + b"".join(rows))
    out = tmp / f"geometry_{name}.mkv"
    video_filter = [] if sar is None else ["-vf", f"setsar={sar}"]
    proc = run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-framerate", "25",
         "-i", str(directory / "%04d.ppm"), "-map_metadata", "-1", "-an", *video_filter,
         "-c:v", "ffv1", "-level", "3", str(out)],
        capture_output=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr.decode()[:400]
    return out


def pixel(frame: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
    offset = (y * width + x) * 3
    return (frame[offset], frame[offset + 1], frame[offset + 2])


def is_bar(rgb: tuple[int, int, int]) -> bool:
    """The grey geometry fixtures' field is painted 161 and reads 160, so anything at
    or below :data:`BLACK_MAX` is a bar the composition added."""
    return max(rgb) <= BLACK_MAX


def is_composition_black(rgb: tuple[int, int, int]) -> bool:
    """The pixels the composition writes where there is no picture.

    Measured on FFmpeg 8.1.3: ``pad``'s bars and the ``color`` source both come back as
    exactly (0,0,0) through both paths, while an identity fixture's pixels are lifted
    off that (``media_fixtures.IDENTITY_LIFT``) even in a channel whose digit is level
    0 - a decoded frame 2 corner measured (109,6,7) - so for those fixtures "not a bar"
    has to mean "not exactly black", not "bright".
    """
    return rgb == (0, 0, 0)


def lit_extent(
    frame: bytes,
    width: int,
    height: int,
    column: int | None = None,
    bar=is_bar,
) -> dict:
    """Where the picture reaches: ``(first, last, count)`` lit pixels along the middle
    row and along ``column`` (the middle one by default), plus the four corners.

    The column is settable because a picture only two columns wide does not reach the
    frame's middle column at all, and its vertical extent then has to be read along
    itself. ``bar`` says what counts as a bar: the fixtures painted for shape have a
    bright field, the identity-coloured ones do not.
    """
    row, col = height // 2, width // 2 if column is None else column
    columns = [x for x in range(width) if not bar(pixel(frame, width, x, row))]
    rows = [y for y in range(height) if not bar(pixel(frame, width, col, y))]
    return {
        "columns": (columns[0], columns[-1], len(columns)) if columns else (0, -1, 0),
        "rows": (rows[0], rows[-1], len(rows)) if rows else (0, -1, 0),
        "corners": [pixel(frame, width, x, y) for x, y in
                    ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))],
    }


def marker_box(frame: bytes, width: int, height: int) -> tuple[int, int, int, int]:
    """Inclusive bounding box of the marker block: ``(x0, y0, x1, y1)``.

    A marker pixel is one whose *dimmest* channel is still above :data:`MARKER_MIN`,
    which is the criterion the hand-worked boxes below were measured with; the block's
    antialiased edge column reads 237-245 and is deliberately not counted, exactly as
    it is not counted in the expectations.
    """
    row_bytes = width * 3

    def marker_row(y: int) -> bytes:
        return frame[y * row_bytes:(y + 1) * row_bytes]

    rows = [y for y in range(height) if max(marker_row(y)) > MARKER_MIN]
    assert rows, "the fixture's marker block is not in the rendered frame at all"
    middle = marker_row(rows[len(rows) // 2])
    columns = [x for x in range(width) if min(middle[x * 3:x * 3 + 3]) > MARKER_MIN]
    assert columns, "the marker block has no width on its own middle row"
    box = (columns[0], rows[0], columns[-1], rows[-1])
    for x, y in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])):
        assert min(pixel(frame, width, x, y)) > MARKER_MIN, (
            f"the reported marker box {box} has a corner that is not marker: "
            f"{pixel(frame, width, x, y)} at ({x},{y})"
        )
    # and the field beside it is a step down, so the box is the block and not a glow
    # over the whole frame (the fixtures paint the block inside the field, so 20 px
    # left of its edge is field)
    field_x, field_y = box[0] - 20, (box[1] + box[3]) // 2
    assert field_x >= 0, "the marker block touches the left edge of the frame"
    assert max(pixel(frame, width, field_x, field_y)) <= MARKER_MIN, (
        f"the field 20 px outside the reported marker box is as bright as the block: "
        f"{pixel(frame, width, field_x, field_y)}"
    )
    return box


def geometry_case(tmp: Path, name: str, size: tuple[int, int], sar: str | None,
                  marker: tuple[int, int, int, int] | None, seconds: F = F(1, 5)):
    """The same grey fixture through both paths, as raw pixels plus what was probed.

    Returns ``(rendered_facts, export_frame, still_frame)``: the export is the
    lossless render the exporter would write, the still is the viewer's own decode,
    and neither is asked what it intended.
    """
    source = grey_fixture(tmp, name, size, sar, marker)
    source_facts = picture_facts(source)
    assert (source_facts.width, source_facts.height) == size, (
        f"{name}: the fixture is {source_facts.width}x{source_facts.height}, "
        f"the test asked for {size[0]}x{size[1]}"
    )
    assert source_facts.sample_aspect_ratio == (F(sar) if sar else None), (
        f"{name}: SAR measured {source_facts.sample_aspect_ratio}, "
        f"the fixture was written with {sar!r}"
    )
    assert min(source_facts.centres[0]) > BLACK_MAX, (
        f"{name}: the fixture's own picture is black: {source_facts.centres[0]}"
    )
    project = video_project(source, "src", RATE, size=(1280, 720))
    project = place(project, "clip", ZERO, seconds)
    dest = tmp / f"geometry_{name}_out.mkv"
    plan, frames = lossless_frames(project, {"src": source}, ZERO, seconds, dest)
    assert (plan.width, plan.height) == (1280, 720)
    still = extract_composition_still(project, {"src": source}, F(1, 25))
    assert (still.width, still.height) == (1280, 720)
    return picture_facts(dest), frames[0], still.rgb


def assert_both_paths(export: bytes, still: bytes, columns, rows, expected_box, label: str):
    """Both paths put the picture in the same place, exactly.

    ``columns``/``rows`` are the expected ``(first, last, count)`` of the middle row
    and column; a bar the composition adds is black, so the counts are the fitted
    picture's own width and height. ``expected_box`` is the marker block's measured
    place, which is what says the shape came through rather than only the extent.
    """
    for name, frame in (("export", export), ("still", still)):
        extent = lit_extent(frame, 1280, 720)
        assert extent["columns"][:2] == columns[:2], (
            f"{label} ({name}): the middle row's picture spans columns "
            f"{extent['columns'][:2]}, expected {columns[:2]}"
        )
        assert extent["columns"][2] == columns[2], (
            f"{label} ({name}): {extent['columns'][2]} lit columns of the middle row, "
            f"expected {columns[2]} of picture"
        )
        assert extent["rows"] == rows, (
            f"{label} ({name}): the middle column spans {extent['rows']}, expected {rows}"
        )
        assert marker_box(frame, 1280, 720) == expected_box, (
            f"{label} ({name}): the marker block landed at "
            f"{marker_box(frame, 1280, 720)}, expected {expected_box}"
        )


def test_a_display_square_stays_square_under_anamorphic_scaling(tmp_path):
    """720x576 at SAR 64:45 is 16:9 on screen, so it fills 1280x720 edge to edge.

    Hand-worked from the fixture, not from the renderer: the coded frame is 720x576
    and the recorded SAR is 64:45, so on screen it is 720*64/45 x 576 = 1024x576.
    The largest even square-pixel size of that shape inside 1280x720 is the box
    itself, so nothing is left over for a bar and the scale factor is 1280/1024 = 5/4.
    The fixture's white block is coded 180x256 at (270,160) - a *display* square,
    because 180*64/45 = 256 - at display x [270*64/45, 450*64/45) = [384, 640) and
    y [160, 416), so the output must show it at x [480, 800) and y [200, 520): a
    320x320 square. The chain this test guards replaced measured the block at
    (527,200,752,519): 226 px wide against 320 tall, and the picture inset with
    190 px bars either side.
    """
    rendered, export, still = geometry_case(
        tmp_path, "an169", (720, 576), "64/45", (270, 160, 450, 416))
    assert rendered.width == 1280 and rendered.height == 720
    assert rendered.sample_aspect_ratio == F(1), (
        f"the output must be square-pixel, ffprobe says SAR {rendered.sample_aspect_ratio}"
    )
    assert rendered.display_aspect_ratio == F(16, 9), (
        f"output display aspect {rendered.display_aspect_ratio}"
    )
    assert_both_paths(export, still, (0, 1279, 1280), (0, 719, 720),
                      (480, 200, 799, 519), "anamorphic 16:9")
    for frame in (export, still):
        assert all(min(pixel(frame, 1280, x, y)) > BLACK_MAX for x, y in
                   ((0, 0), (1279, 0), (0, 719), (1279, 719))), (
            "a 16:9 display inside a 16:9 output must reach all four corners"
        )


def test_a_four_by_three_source_gets_even_pillarboxes_in_a_wide_output(tmp_path):
    """720x576 at SAR 16:15 is 4:3 on screen: 960x720, with 160 px of bar each side.

    Hand-worked: on screen the fixture is 720*16/15 x 576 = 768x576, so inside
    1280x720 the fit is 960x720 (720*4/3) and ``pad`` puts (1280-960)/2 = 160 px of
    black on each side: the middle row's picture must be the 960 columns 160..1119.
    The scale factor is 960/768 = 5/4 and the block is coded 240x256 at (240,160),
    i.e. display x [256, 512) and y [160, 416), so it must arrive at x
    [256*5/4 + 160, 512*5/4 + 160) = [480, 800) and y [200, 520): the same 320x320
    square as the 16:9 case, which is what "no stretch" means here.
    """
    rendered, export, still = geometry_case(
        tmp_path, "an43", (720, 576), "16/15", (240, 160, 480, 416))
    assert rendered.sample_aspect_ratio == F(1)
    assert rendered.display_aspect_ratio == F(16, 9)
    assert_both_paths(export, still, (160, 1119, 960), (0, 719, 720),
                      (480, 200, 799, 519), "anamorphic 4:3")
    for frame in (export, still):
        extent = lit_extent(frame, 1280, 720)
        assert extent["columns"][0] == 1280 - 1 - extent["columns"][1], (
            f"the bars are not equal: {extent['columns'][0]} px left, "
            f"{1280 - 1 - extent['columns'][1]} px right"
        )
        assert all(max(pixel(frame, 1280, x, y)) <= BLACK_MAX for x, y in
                   ((0, 0), (1279, 0), (0, 719), (1279, 719))), (
            "a 4:3 picture in a 16:9 output must leave the corners black"
        )


def test_a_wider_source_is_letterboxed_and_keeps_its_own_borders(tmp_path):
    """1280x600 square pixels (32:15) in 1280x720: 60 px of bar above and below.

    Hand-worked: the shape is already square-pixel, so the fit is 1280x600 and the
    pad adds (720-600)/2 = 60 rows. Nothing is cropped: the fixture's grey field,
    which reaches its own left and right edges, still reaches the output's, and the
    coded 256x256 block at (512,172) moves down by the pad offset and no further:
    x [512, 768), y [232, 488), still a 256x256 square because the factor is 1.
    """
    rendered, export, still = geometry_case(
        tmp_path, "wide", (1280, 600), None, (512, 172, 768, 428))
    assert rendered.sample_aspect_ratio == F(1)
    assert_both_paths(export, still, (0, 1279, 1280), (60, 659, 600),
                      (512, 232, 767, 487), "wider than 16:9")


def test_a_source_that_carries_its_own_borders_keeps_them(tmp_path):
    """A frame that already has 60 rows of border keeps them and only gains the rest.

    The fixture is 1280x600 with its top and bottom 60 coded rows painted 16 rather
    than 161 - a border belonging to the source, dark but deliberately not the
    composition's black. Fitted into 1280x720 the picture is 1280x600 at offset 60,
    so the output rows must read: 0..59 and 660..719 black added by ``pad``, 60..119
    and 600..659 the fixture's own border, 120..599 its field. Measured on FFmpeg
    8.1.3, the painted 16 survives as exactly (16,16,16) in both paths while the added
    bars are exactly (0,0,0), so the pixels themselves say whose black a row is; and
    the 600 rows of picture between the bars, with the marker still 256x256 at
    (512,232,767,487), say nothing was cropped away and the factor stayed 1.
    """
    source = grey_fixture(tmp_path, "bordered", (1280, 600), None,
                          (512, 172, 768, 428), band_rows=60)
    project = video_project(source, "src", RATE, size=(1280, 720))
    project = place(project, "clip", ZERO, F(1, 5))
    plan, frames = lossless_frames(project, {"src": source}, ZERO, F(1, 5),
                                   tmp_path / "bordered_out.mkv")
    assert (plan.width, plan.height) == (1280, 720)
    assert len(frames) == plan.total_frames == 5
    still = extract_composition_still(project, {"src": source}, F(1, 25))
    for name, frame in (("export", frames[0]), ("still", still.rgb)):
        extent = lit_extent(frame, 1280, 720, bar=is_composition_black)
        assert extent["rows"] == (60, 659, 600), (
            f"{name}: the source's own frame covers {extent['rows']} of the output; "
            "expected its full 600 rows at offset 60, i.e. nothing cropped"
        )
        assert extent["columns"] == (0, 1279, 1280), (
            f"{name}: the source reaches its own left and right edges, so the output "
            f"must too; got {extent['columns'][:2]}"
        )
        for y in (0, 59, 660, 719):
            assert pixel(frame, 1280, 200, y) == (0, 0, 0), (
                f"{name}: row {y} should be the bar the composition added, reads "
                f"{pixel(frame, 1280, 200, y)}"
            )
        for y in (60, 119, 600, 659):
            assert pixel(frame, 1280, 200, y) == (16, 16, 16), (
                f"{name}: row {y} should still be the border the source arrived with, "
                f"reads {pixel(frame, 1280, 200, y)}"
            )
        for y in (120, 599):
            assert pixel(frame, 1280, 200, y)[0] > 100, (
                f"{name}: row {y} should be the source's field, reads "
                f"{pixel(frame, 1280, 200, y)}"
            )
        assert marker_box(frame, 1280, 720) == (512, 232, 767, 487)


def test_square_pixel_sources_keep_the_geometry_they_arrived_with(tmp_path):
    """A 16:9 square-pixel source still fills the frame; a 4:3 one keeps its bars.

    Both fixtures have no sample aspect ratio in the container at all, measured as
    ``None`` by ffprobe: FFmpeg's scale then treats the sample aspect as 1:1 and its
    ``dar`` is the coded ratio (measured: ``dar`` 4/3 for 640x480 written with no SAR,
    and still 4/3 for a file written with ``setsar=0/1``), which is what ordinary
    square-pixel material means. Hand-worked: 1280x720 in 1280x720 is the box itself
    with factor 1, so the coded 256x256 block at (512,232) stays put; 640x480 fits
    960x720 (720*4/3) with 160 px of bar each side at factor 960/640 = 1.5, so its
    block at (192,112) arrives at x [192*1.5 + 160, 448*1.5 + 160) = [448, 832) and y
    [168, 552): 384x384, i.e. the same 3:2 stretch of a square as the source itself
    had, and the same numbers this chain produced before the display-ratio fix.
    """
    rendered, export, still = geometry_case(
        tmp_path, "sq169", (1280, 720), None, (512, 232, 768, 488))
    assert rendered.sample_aspect_ratio == F(1)
    assert_both_paths(export, still, (0, 1279, 1280), (0, 719, 720),
                      (512, 232, 767, 487), "square-pixel 16:9")
    rendered, export, still = geometry_case(
        tmp_path, "sq43", (640, 480), None, (192, 112, 448, 368))
    assert rendered.sample_aspect_ratio == F(1)
    assert_both_paths(export, still, (160, 1119, 960), (0, 719, 720),
                      (448, 168, 831, 551), "square-pixel 4:3")


def test_an_extreme_display_ratio_is_clamped_to_the_box_not_to_the_input(tmp_path):
    """A ratio the output box cannot hold is clamped, and the clamp is measurable.

    A 3x2000 source is 3:2000 on screen, and no even square-pixel size inside
    1280x720 has that shape: at the full 720 rows the fit wants 720*3/2000 = 1.08
    columns, which rounds to 0. A computed 0 is not an error to ``scale`` - it means
    *"use the input size"*, so the unguarded expression gives this file 3 columns
    because the file happens to be 3 px wide, and the chain this task replaced gave
    it 1. The ``max(2, ...)`` in the fit makes the choice explicit: the smallest
    legal even width, so the picture is exactly 2 columns wide in both paths.
    Measured positions of that strip differ by one column between the two pixel
    formats (still 639..640, export 638..639, because a 2 px wide picture has no
    room for 4:2:0 chroma to sit symmetrically), so the assertion pins the width
    exactly and only says the strip is the centred one.
    """
    source = grey_fixture(tmp_path, "extreme", (3, 2000), None, None)
    project = video_project(source, "src", RATE, size=(1280, 720))
    project = place(project, "clip", ZERO, F(1, 5))
    plan, frames = lossless_frames(project, {"src": source}, ZERO, F(1, 5),
                                   tmp_path / "extreme_out.mkv")
    assert (plan.width, plan.height) == (1280, 720)
    assert len(frames) == plan.total_frames == 5
    still = extract_composition_still(project, {"src": source}, F(1, 25))
    for name, frame in (("export", frames[0]), ("still", still.rgb)):
        across = lit_extent(frame, 1280, 720)
        assert across["columns"][2] == 2, (
            f"{name}: the clamped picture is {across['columns'][2]} columns wide; "
            f"expected 2 (the clamp), spanning {across['columns'][:2]}"
        )
        assert 638 <= across["columns"][0] <= 639, (
            f"{name}: the 2 px strip is at {across['columns'][0]}, not centred"
        )
        # read the strip's own height along itself: at 2 px wide it does not reach the
        # frame's middle column in the export's 4:2:0 picture
        down = lit_extent(frame, 1280, 720, column=across["columns"][0])
        assert down["rows"] == (0, 719, 720), (
            f"{name}: the clamped picture covers {down['rows']} of 720 rows"
        )


def test_anamorphic_picture_is_placed_by_its_display_aspect(tmp_path):
    """The identity-coloured anamorphic fixture: right pictures *and* right geometry.

    720x576 at 64:45 sample aspect is 16:9, so it must fill a 1280x720 output with no
    bar anywhere; ignoring the recorded sample aspect ratio scales the 5:4 *pixels*
    instead and leaves 190 px pillarbars. The still and the export must make the same
    choice, and either one alone would look plausible. The fixture is measured first:
    the file must really carry SAR 64:45 (an earlier version of this fixture wrote
    ``setsar`` with a colon, which FFmpeg reads as two options) and its pictures must
    still read back as their own numbers after the 4:2:0 round trip.
    """
    source = make_identity_anamorphic(tmp_path, count=50)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 50)
    assert (facts.width, facts.height) == (720, 576)
    assert facts.sample_aspect_ratio == F(64, 45), (
        f"the fixture must carry the sample aspect ratio it claims: {facts.sample_aspect_ratio}"
    )
    assert F(facts.width, facts.height) * facts.sample_aspect_ratio == F(16, 9)
    assert facts.display_aspect_ratio == F(16, 9), (
        f"measured display aspect: {facts.display_aspect_ratio}"
    )
    project = video_project(source, "src", RATE, size=(1280, 720))
    project = place(project, "clip", ZERO, F(2))
    plan, measured = render(project, {"src": source}, ZERO, F(1), tmp_path / "an.mkv")
    assert plan.width == 1280 and plan.height == 720
    assert all(m is not None for m in measured), "no frame of an anamorphic source is black"
    assert measured[2] == 2, "the picture identity survives the scaling"
    assert_matches(measured, reference_range((Placed("clip", ZERO, F(2)),), RATE, 0,
                                             plan.total_frames, facts),
                   "anamorphic export")
    still = extract_composition_still(project, {"src": source}, F(2, 25))
    assert (still.width, still.height) == (1280, 720)
    assert identity_at(still.rgb, still.width, still.height) == measured[2]
    # the whole frame is used: the display aspect leaves no bar on either side, in
    # both paths, and it reaches the corners and both centres
    _, export_frame = lossless_frames(project, {"src": source}, ZERO, F(1),
                                      tmp_path / "an_geom.mkv")
    for name, frame in (("export", export_frame[2]), ("still", still.rgb)):
        extent = lit_extent(frame, 1280, 720, bar=is_composition_black)
        assert extent["columns"] == (0, 1279, 1280), (
            f"{name}: the scaled picture is inset by black bars: its middle row spans "
            f"columns {extent['columns'][0]}..{extent['columns'][1]} of 1280, so the "
            f"measured display aspect {facts.display_aspect_ratio} was not honoured"
        )
        assert extent["rows"] == (0, 719, 720), (
            f"{name}: the middle column spans {extent['rows']}, expected the whole 720"
        )
        assert all(color != (0, 0, 0) for color in extent["corners"]), (
            f"{name}: a corner of a 16:9 picture in a 16:9 output is black: "
            f"{extent['corners']}"
        )
    rendered = picture_facts(tmp_path / "an.mkv")
    assert (rendered.width, rendered.height) == (1280, 720)
    assert rendered.sample_aspect_ratio == F(1), (
        f"the exported frame is not square-pixel: SAR {rendered.sample_aspect_ratio}"
    )
    assert rendered.display_aspect_ratio == F(16, 9)



def test_where_reading_starts_cannot_choose_which_picture_is_shown(tmp_path):
    """Measured frame evidence changes how much is decoded, never what is seen.

    The second half of ``[1/3, 6)`` at 25/24 starts at output 68/25 s, so its first
    sampling time is ``1/3 + 68/24 = 19/6 = 3.16667`` s. Without the frame index the
    reading window opens a nominal quarter second earlier, at 2.916 s; with it, the
    index says the picture that frame shows is the one at 79/25 = 3.16 s, so reading
    starts one container millisecond earlier, at 3.159 s - about six source
    pictures instead of a quarter second of them.

    Hand-worked for the pictures: output 68 of the composition samples 19/6 s, and
    the last real timestamp at or before that is 79/25 = 3.16 s, so the range's
    first frame must be picture 79; output 72 samples ``1/3 + 3`` = 10/3 = 3.3333 s,
    where the next picture (84/25 = 3.36 s) has not started, so it is 83. Both paths
    must give the same numbers, and the whole render's own frames at those global
    indices must agree with them.
    """
    source = make_identity_video(tmp_path, count=150)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 150)
    clip = Placed("clip", F(1, 3), F(6), rate=F(25, 24))
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out, rate=F(25, 24))
    paths = {"src": source}
    start, end = F(68, 25), F(136, 25)

    plan = build_plan(project, start, end)
    sources = {(s.asset_id, s.stream_index): s for s in project.sources}
    base = stream_timing(source, 0).time_base
    assert base == F(1, 1000), f"the fixture's own container tick: {base}"
    wanted = F(19, 6)
    evidence = max(pts for pts in facts.pictures if pts <= wanted)
    assert evidence == F(79, 25), f"the frame index should find 79/25: {evidence}"

    def render_with(evidence_on: bool, name: str) -> tuple[object, list[int | None], str]:
        job = build_job(
            plan, paths, sources, tmp_path / name, FFMPEG, LOSSLESS_INTERMEDIATE,
            time_base_of=time_bases(paths),
            previous_frame_of=(lambda _a, _s, _t: evidence) if evidence_on else None,
        )
        seek = job.args[job.args.index("-ss") + 1]
        read = job.args[job.args.index("-t") + 1]
        proc = run(job.args, capture_output=True, timeout=600)
        assert proc.returncode == 0, proc.stderr.decode()[:600]
        raw = run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(tmp_path / name),
             "-map", "0:v:0", "-fps_mode", "passthrough", "-f", "rawvideo",
             "-pix_fmt", "rgb24", "-"],
            capture_output=True, timeout=600,
        )
        assert raw.returncode == 0, raw.stderr.decode()[:400]
        stride = plan.width * plan.height * 3
        frames = raw.stdout[:(len(raw.stdout) // stride) * stride]
        ids = [
            None if all(b == 0 for b in frames[i * stride:(i + 1) * stride])
            else identity_at(frames[i * stride:(i + 1) * stride], plan.width, plan.height)
            for i in range(len(frames) // stride)
        ]
        return plan, ids, f"-ss {seek} -t {read}"

    nominal_plan, nominal, nominal_args = render_with(False, "nominal.mkv")
    measured_plan, measured, measured_args = render_with(True, "measured.mkv")
    assert nominal == measured, (
        f"the reading window changed the pictures: nominal {nominal_args}, "
        f"measured {measured_args}"
    )
    assert nominal_plan.total_frames == measured_plan.total_frames == 68
    assert nominal[0] == 79 and nominal[4] == 83, f"first frames of the range: {nominal[:6]}"
    # the evidence reads less, never more: the same range, a later window start
    assert F(measured_args.split()[1]) > F(nominal_args.split()[1])
    assert F(measured_args.split()[3]) < F(nominal_args.split()[3])
    # and both agree with the whole render at the same global indices
    whole_plan, whole = render(project, paths, ZERO, F(136, 25), tmp_path / "whole.mkv")
    assert whole_plan.total_frames == 136
    assert whole[68:136] == nominal == measured
    expected = reference_range((clip,), RATE, 68, 68, facts)
    assert_matches(nominal, expected, "range opened at the measured picture")


# ---------------------------- what a still claims about the frame it shows (TASK_12k)
#
# A still answers two different questions, and the tests below keep them apart.
# "Which picture is this?" is settled by the pixels against a real lossless export of
# the same global output index; "where in the stream is it?" may only be answered when
# that particular picture's identity survives the chain's own selection. So every
# probe here reads the identity out of the still, finds that same picture in the
# export, and checks the reported source stamp against *that picture's own* measured
# timestamp - never against the time the plan asked for. A reported stamp that belongs
# to some other picture is a failure; so is one that was never measured but is
# presented as if it had been.


def probe_table(
    project: Project,
    paths: dict[str, Path],
    exported: list[int | None],
    first_index: int,
    facts: PictureFacts,
    indices: list[int],
    label: str,
) -> list[tuple[F, int, int | None, int | None, F | None, str]]:
    """Every still probe against the exported frame and its own source stamp.

    Returns ``(requested output time, global index, still identity, export identity,
    reported source PTS, measured or not)`` for the record. Each row is asserted as it
    is produced, so the first disagreement is the one reported.
    """
    rows = []
    for index in indices:
        moment = F(index) / RATE
        still = extract_composition_still(project, paths, moment)
        assert still.frame_index == index, (
            f"{label}: a playhead at {float(moment):.6f}s named frame "
            f"{still.frame_index}, expected {index}"
        )
        shown = (
            None if all(byte == 0 for byte in still.rgb)
            else identity_at(still.rgb, still.width, still.height)
        )
        at = exported[index - first_index]
        assert shown == at, (
            f"{label}: output frame {index} - the still shows {shown}, the lossless "
            f"export of the same composition writes {at}"
        )
        reported = still.decoded_source_pts
        if reported is None:
            rows.append((moment, index, shown, at, None, "not measured"))
            continue
        # the stamp is only honest if it is the timestamp of the picture that was
        # shown: measured from the fixture's own decode, not from the plan
        assert shown is not None, f"{label}: frame {index} reports {reported} for black"
        ordinal = facts.identities.index(shown)
        assert reported == facts.pictures[ordinal], (
            f"{label}: output frame {index} shows picture {shown}, whose real "
            f"timestamp is {facts.pictures[ordinal]}, but the still reports "
            f"{reported} ({float(reported):.9f}s) - a stamp that is not this picture's"
        )
        assert still.grid_exact, (
            f"{label}: frame {index} reports a stamp without claiming its grid is exact"
        )
        rows.append((moment, index, shown, at, reported, "measured"))
    return rows


def test_a_head_hold_reports_the_held_picture_not_the_time_the_grid_asked_for(tmp_path):
    """The two numbers a still can give, kept apart at the head of a clip.

    Hand-worked on the 150-picture fixture: clip ``[1/3, 5)`` at rate 1 on 25 fps
    output is 14/3 s of output, so ``ceil(14/3 * 25) = 117`` frames. Output frame 0
    samples source ``1/3`` s = 0.3333 s. The last picture at or before that instant,
    picture 8 at 8/25 = 0.32 s, is **outside the clip's slice**, so the first picture
    the slice offers is 9 at 9/25 = 0.36 s, and the chain holds it for that frame. Frame 1
    samples ``1/3 + 1/25`` = 0.3733 s and is still inside picture 9's display. So the
    honest answer for frame 0 is: this is picture 9, whose measured timestamp is
    ``9/25`` - while the time the composition asked for stays ``1/3``, a prediction.
    """
    source = make_identity_video(tmp_path, count=150)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 150)
    clip = Placed("clip", F(1, 3), F(5))
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out)
    paths = {"src": source}
    plan, exported = render(project, paths, ZERO, project.output_end(),
                            tmp_path / "head.mkv")
    assert plan.total_frames == 117
    assert exported[0] == exported[1] == 9, "the head hold is the export's own answer"

    for index in (0, 1):
        still = extract_composition_still(project, paths, F(index) / RATE)
        assert identity_at(still.rgb, still.width, still.height) == 9
        assert still.decoded_source_pts == F(9, 25), (
            f"frame {index} holds picture 9, whose own timestamp is 9/25, but the "
            f"still reports {still.decoded_source_pts}"
        )
        assert still.source_pts == F(1, 3) + F(index) / RATE, (
            "the planned sampling time must survive as the prediction it is"
        )
        assert still.grid_exact


def test_a_stills_source_stamp_belongs_to_the_picture_it_shows(tmp_path):
    """Cuts, a gap and the composition's own first and last frames.

    Hand-worked table on the 150-picture fixture at 25 fps output, rate 1: clip A
    covers source 0-1 s at output 0-1 s (frames 0..24, showing pictures 0..24),
    output 1-2 s is a gap the composition leaves black (frames 25..49), and clip B
    covers source 3-4 s at output 2-3 s. Frame 50 is the cut's first frame (source
    3 s = picture 75) and frames 50..74 sample pictures 75..99; frame 74 is the last
    frame the export writes (the composition ends at 3 s).
    """
    source = make_identity_video(tmp_path, count=150)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 150)
    a = Placed("a", ZERO, F(1))
    b = Placed("b", F(3), F(4), output_start=F(2))
    project = video_project(source, "src", RATE)
    project = place(project, "a", a.source_in, a.source_out)
    project = place(project, "b", b.source_in, b.source_out, output_start=F(2))
    assert project.output_end() == F(3)
    paths = {"src": source}
    plan, exported = render(project, paths, ZERO, F(3), tmp_path / "table.mkv")
    assert plan.total_frames == 75
    hand = list(range(25)) + [None] * 25 + list(range(75, 100))
    assert reference_range((a, b), RATE, 0, 75, facts) == hand, (
        "the reference disagrees with the hand-worked table"
    )
    assert exported == hand, "the export disagrees with the hand-worked table"
    rows = probe_table(project, paths, exported, 0, facts,
                       [0, 1, 24, 25, 37, 49, 50, 51, 73, 74], "cut and gap")
    assert [row[2] for row in rows] == [0, 1, 24, None, None, None, 75, 76, 98, 99]
    assert [row[4] for row in rows] == [
        F(0), F(1, 25), F(24, 25), None, None, None, F(3), F(76, 25), F(98, 25),
        F(99, 25),
    ]


def test_every_fractional_retime_reports_the_frame_it_actually_shows(tmp_path):
    """The rate matrix, one table row per global index, still against export.

    Same fixture and slice, six tempos, output rate 25. For each composition the whole
    output is rendered losslessly once, and its first frame, second frame, middle
    frame and last frame are probed as stills: every probe must show the same picture
    the export writes at that global index, and every reported source stamp must be
    that picture's own measured timestamp (or nothing at all).
    """
    source = make_identity_video(tmp_path, count=150)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 150)
    paths = {"src": source}
    rows: list[tuple] = []
    for rate, source_out in (
        (F(25, 24), F(6)), (F(24, 25), F(6)), (F(1000, 1001), F(6)),
        (F(1001, 1000), F(6)), (F(4), F(6)), (F(1, 4), F(3)),
    ):
        clip = Placed("clip", F(1), source_out, rate=rate)
        project = video_project(source, "src", RATE)
        project = place(project, "clip", clip.source_in, clip.source_out, rate=rate)
        total = ceil_frames(project.output_end(), RATE)
        plan, exported = render(project, paths, ZERO, project.output_end(),
                                tmp_path / f"rate_{rate.numerator}_{rate.denominator}.mkv")
        assert plan.total_frames == total, f"rate {rate}: plan {plan.total_frames}, " \
            f"lattice {total}"
        # the reference, worked out from the fixture's decoded PTS list, names the
        # pictures the export must - and does - show
        assert_matches(exported, reference_range((clip,), RATE, 0, total, facts),
                       f"export at rate {rate}")
        rows += probe_table(
            project, paths, exported, 0, facts,
            [0, 1, total // 2, total - 1], f"rate {rate}",
        )
    assert len(rows) == 24
    assert all(row[2] == row[3] for row in rows), "a still disagreed with the export"


def test_the_1000_1001_still_never_borrows_another_pictures_timestamp(tmp_path):
    """The reviewer's case, pinned.

    Clip [1, 6) at rate 1000/1001 on 25 fps output is 1001/200 s of output, and
    ``ceil(1001/200 * 25) = 126`` frames. Hand-worked: output 0 asks for source
    1 s, which is the stream's own picture 25 at exactly 1 s; output 1 asks for
    1041/1001 s, still inside picture 25; output 2 asks for 1081/1001 s, where
    picture 26 (at 26/25 = 1.04 s) has arrived. So a probe of output 2 that answers
    "1 s" is labelling picture 26 with picture 25's timestamp - the mistake this
    test exists to make impossible.
    """
    source = make_identity_video(tmp_path, count=150)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 150)
    clip = Placed("clip", F(1), F(6), rate=T1000_1001)
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out, rate=T1000_1001)
    assert project.output_end() == F(1001, 200)
    paths = {"src": source}
    plan, exported = render(project, paths, ZERO, project.output_end(),
                            tmp_path / "reviewer.mkv")
    assert plan.total_frames == 126
    rows = probe_table(project, paths, exported, 0, facts,
                       [0, 1, 2, 3, 26, 52, 78, 104, 125], "rate 1000/1001")
    # the first three probes, hand-worked above: 25, 25, 26
    assert [row[2] for row in rows[:3]] == [25, 25, 26]
    assert rows[0][4] == F(1) and rows[2][4] == F(26, 25)
    # and no probe ever answers with a number that is not some picture's timestamp
    for row in rows:
        if row[4] is not None:
            assert row[4] in facts.pictures, f"index {row[1]}: {row[4]} is not a picture"
        assert row[4] != F(1) or row[2] == 25, (
            f"index {row[1]}: picture {row[2]} was labelled 1 s"
        )


def test_a_fine_timescale_stream_holds_its_last_picture(tmp_path):
    """Twenty-nanosecond container ticks must not cost the composition its tail.

    Measured fixture: 150 identity pictures at ``n/25`` s in an MP4 whose track
    timescale is 50,000,000, so the container's tick is ``1/50000000`` while the
    pictures are 2,000,000 ticks apart, and the declared 6 s duration overshoots the
    last picture (``149/25`` = 5.96 s) by ``4/25`` s.

    Hand-worked composition - clip ``[1, 6)`` at tempo 1000/1001 on 25 fps output:
    output length ``5 / (1000/1001) = 1001/200 = 5.005`` s, so
    ``ceil(5.005 * 25) = ceil(125.125) = 126`` frames, and output frame ``i`` samples
    source ``1 + (i/25)*(1000/1001) = 1 + 40i/1001``:

    * frame 0 samples exactly 1 s - picture 25's own timestamp, a tie;
    * frame 123 samples ``5921/1001`` = 5.915084915 s: picture 147, at 5.88 s;
    * frame 124 samples ``5961/1001`` = 5.955044955 s: picture 148, at 5.92 s;
    * frame 125 samples ``6001/1001`` = 5.995004995 s: picture **149**, at 5.96 s.
      The next picture would be 6.0 s, which the file does not hold and the slice
      ``[1, 6)`` excludes anyway, so this frame is a hold of the last picture.

    The last frame is where this shape used to fail: on a container this fine, a tick
    that is exact for *any* timestamp would have to be finer than an FFmpeg time base
    can state, and the container-quantised fallback puts its grid 0.03504 s - 0.876 of
    an output frame - past the last picture it decoded, which is out of reach of its
    own first slot and writes no picture at all. The exact tempo is now proved from the
    stamps this reading window really holds, so the still and the export both answer
    with picture 149.
    """
    source = make_identity_fine_timescale(tmp_path, count=150)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 150)
    assert facts.time_base == F(1, 50_000_000), (
        f"the fixture's whole point is its container tick, measured {facts.time_base}"
    )
    assert facts.pictures[25] == F(1) and facts.last == F(149, 25), (
        f"pictures must sit on 1/25 s with the last at 149/25, measured {facts.pictures[25]}"
        f" and {facts.last}"
    )
    assert facts.container_duration == F(6), (
        f"the declared end must overshoot the last picture, measured "
        f"{facts.container_duration}"
    )

    clip = Placed("clip", F(1), F(6), rate=T1000_1001)
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out, rate=T1000_1001)
    assert project.output_end() == F(1001, 200)
    paths = {"src": source}

    plan, exported = render(project, paths, ZERO, project.output_end(),
                            tmp_path / "fine_whole.mkv", measured_lookups(paths))
    assert plan.total_frames == 126 == len(exported)
    # the hand-worked picks, and the referee's arithmetic agreeing with them
    expected = {0: 25, 1: 25, 2: 26, 123: 147, 124: 148, 125: 149}
    for index, picture in expected.items():
        assert exported[index] == picture, (
            f"output frame {index} samples {F(1) + F(40 * index, 1001)} s and must show "
            f"picture {picture}; the export wrote {exported[index]}"
        )
        assert reference_pick((clip,), RATE, index, facts) == picture

    # the still path, with no frame index handed to it: same pictures, and this time
    # each one carries its own measured timestamp rather than nothing
    rows = probe_table(project, paths, exported, 0, facts,
                       [0, 1, 2, 122, 123, 124, 125], "fine timescale")
    assert [row[5] for row in rows] == ["measured"] * len(rows), (
        f"a fine-timescale stream has an exact tempo proved from its own stamps, so "
        f"every still can say which picture it decoded: {[(r[1], r[5]) for r in rows]}"
    )
    assert rows[-1][4] == F(149, 25), (
        f"the last still shows picture 149, whose measured timestamp is 149/25, but "
        f"reported {rows[-1][4]}"
    )


def test_a_one_frame_range_of_a_fine_timescale_stream_writes_that_one_frame(tmp_path):
    """One frame held past the last picture is one frame of media, not zero.

    The same fixture and composition as above, rendered as only global frame 125:
    source ``6001/1001`` s, a hold of picture 149 at 5.96 s. The plan promises one
    frame, so the file must hold exactly one - a chain whose grid cannot reach its first
    picture writes none, and the frame-count check below is what stands between such a
    chain and a published short file.
    """
    source = make_identity_fine_timescale(tmp_path, count=150)
    facts = picture_facts(source)
    assert_identity_fixture(facts, 150)
    clip = Placed("clip", F(1), F(6), rate=T1000_1001)
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out, rate=T1000_1001)
    paths = {"src": source}
    plan, exported = render(project, paths, F(125) / RATE, F(126) / RATE,
                            tmp_path / "fine_one.mkv", measured_lookups(paths))
    assert plan.total_frames == 1, "the range [5, 5.04) is global index 125 alone"
    assert exported == [149], (
        f"output 125 samples 6001/1001 s and holds picture 149; the file wrote {exported}"
    )


def test_a_fine_timescale_stream_without_stream_measurements_is_reported_not_silent(
    tmp_path,
):
    """What a graph is left with when no frame index was read for the stream.

    Same composition, one-frame range, but the graph is given only the container's
    tick: nothing proves the tempo, so the chain takes the container-quantised form and
    its grid lands 0.876 of an output frame past the last picture it decodes - the
    measured shape in which that chain writes **no picture at all**. The job says so
    (``inexact_picture`` names the stream) and the exporter's own check refuses a file
    that comes up short, so this is a reported limitation rather than media with frames
    missing. A still of the same frame answers with picture 149 because the still path
    reads the window itself.
    """
    source = make_identity_fine_timescale(tmp_path, count=150)
    clip = Placed("clip", F(1), F(6), rate=T1000_1001)
    project = video_project(source, "src", RATE)
    project = place(project, "clip", clip.source_in, clip.source_out, rate=T1000_1001)
    paths = {"src": source}
    plan = build_plan(project, F(125) / RATE, F(126) / RATE)
    sources = {(s.asset_id, s.stream_index): s for s in project.sources}
    job = build_job(plan, paths, sources, tmp_path / "fine_unmeasured.mkv", FFMPEG,
                    LOSSLESS_INTERMEDIATE, time_base_of=time_bases(paths))
    assert job.inexact_picture == {("src", 0)}, (
        "an unproved tempo is a reported approximation, never an exact claim"
    )
    graph = job.args[job.args.index("-filter_complex") + 1]
    assert "fps=round=up:fps=25/1:start_time=0.250250996" in graph, (
        f"the approximate chain places its grid from the decimal seek, which is the "
        f"shape that reaches nothing: {graph}"
    )
    assert (job.video_items, plan.total_frames) == (1, 1)

    # and the still path, which does read the window, still answers the right picture
    still = extract_composition_still(project, paths, F(125) / RATE)
    assert identity_at(still.rgb, still.width, still.height) == 149
    assert still.decoded_source_pts == F(149, 25) and still.grid_exact
