"""Which picture actually came out, measured frame by frame.

These are the acceptance tests for frame identity. Every fixture carries its own
frame number as a colour (see ``media_fixtures.identity_rgb``), so "the picture is
one frame off" is a *different number* rather than a slightly blurry match, and the
comparison is exact arithmetic. Each test computes what the composition contract
says the picture must be - the last real source frame at or before the plan's
sampling time, held to the nearest legal picture when the slice cannot supply one -
from the plan and the stream's own timestamps, and then reads what the renderer or
the still path actually produced.

Covered on purpose: fractional frame rates (24/25, 25/24, 1001/1000), fractional
source positions, a nonzero stream origin, variable frame rate, B-frames, gaps,
half-frame boundaries, a slice ending on the last legal frame, range renders versus
whole renders, and the still path against the export path.
"""

import itertools
from fractions import Fraction as F
from pathlib import Path
from subprocess import run

import pytest

from media_fixtures import (
    HAS_FFMPEG,
    identity_at,
    make_identity_anamorphic,
    make_identity_bframes,
    make_identity_offset,
    make_identity_vfr,
    make_identity_video,
    source_from_probe,
)
from video_editor.core.commands import AddClipCommand, RegisterSourceCommand
from video_editor.core.model import OutputSettings, Project, Track, TrackKind
from video_editor.media.framegrab import (
    StillError,
    extract_composition_still,
    source_frame_times,
    stream_timing,
)
from video_editor.render.graph import LOSSLESS_INTERMEDIATE, build_job
from video_editor.render.plan import (
    build_plan,
    picture_preroll,
    picture_sampling,
)

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")

FFMPEG = "ffmpeg"
T24 = F(24)
T1000_1001 = F(1000, 1001)
T1001_1000 = F(1001, 1000)
ZERO = F(0)


def video_project(path: Path, asset_id: str, frame_rate: F, size=(64, 48)) -> Project:
    """A one-track project around one real picture stream, registered by probing it."""
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


def render(
    project: Project, paths: dict[str, Path], out_start: F, out_end: F, dest: Path
):
    """Render a range losslessly and return (plan, measured identities per frame).

    ``None`` in the measured list means a frame whose pixels are all black: black
    and the fixture's frame 0 are the same colour, so the plan, not the pixels,
    decides which one it was - and the test checks the pixels are black too.
    """
    plan = build_plan(project, out_start, out_end)
    sources = {(s.asset_id, s.stream_index): s for s in project.sources}
    job = build_job(
        plan, paths, sources, dest, FFMPEG, LOSSLESS_INTERMEDIATE,
        time_base_of=time_bases(paths),
    )
    proc = run(job.args, capture_output=True, timeout=600)
    assert proc.returncode == 0, proc.stderr.decode()[:800]
    raw = run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(dest), "-map", "0:v:0",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
        timeout=600,
    )
    assert raw.returncode == 0, raw.stderr.decode()[:400]
    stride = plan.width * plan.height * 3
    frames = [raw.stdout[i * stride : (i + 1) * stride] for i in range(len(raw.stdout) // stride)]
    assert len(frames) == plan.total_frames, (
        f"the file holds {len(frames)} frames, the plan called for {plan.total_frames}"
    )
    measured = [
        None if all(byte == 0 for byte in frame) else identity_at(frame, plan.width, plan.height)
        for frame in frames
    ]
    return plan, measured


def stamp_seconds(path: Path) -> list[F]:
    """Real picture timestamps of a fixture, on the asset-local axis."""
    return [frame.pts for frame in source_frame_times(path, 0)]


def planned(
    plan, project: Project, paths: dict[str, Path], stamps: list[F]
) -> list[int | None]:
    """What the contract says each output frame must show, from the plan itself.

    For output frame ``i`` the plan gives a sampling time in source seconds; the
    picture is the last real frame at or before it, and when the slice cannot
    supply one the nearest legal picture is held. The stream's measured timestamps
    decide where the real frames are, so this says nothing about frame rates.
    """
    quant = plan.quantisation
    base = quant.frame_index(plan.output_start)
    sources = {(s.asset_id, s.stream_index): s for s in project.sources}
    out: list[int | None] = []
    for k in range(plan.total_frames):
        moment = quant.frame_time(base + k)
        item = plan.picture_at(moment)
        if item is None or item.is_black:
            out.append(None)
            continue
        clip = item.clip
        source = sources[(clip.asset_id, clip.stream_index)]
        sampling = picture_sampling(
            clip,
            item,
            quant.frame_rate,
            time_base=stream_timing(paths[clip.asset_id], clip.stream_index).time_base,
            preroll=picture_preroll(clip, source, quant.frame_rate),
        )
        wanted = sampling.sample_time(k - item.start_frame)
        legal = [pts for pts in stamps if clip.source_in <= pts < clip.source_out]
        if not legal:
            out.append(-1)  # nothing readable: a plan this fixture never produces
            continue
        shown = [pts for pts in legal if pts <= wanted]
        chosen = shown[-1] if shown else legal[0]  # the hold rule
        out.append(stamps.index(chosen))
    return out


def assert_matches(measured, expected, label: str, *, allow_one_frame: int = 0) -> int:
    """Compare rendered pictures with the pictures the plan's contract names.

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
            f"five (frame, rendered, planned): {wrong[:5]}"
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
    "rate,label,allow_one_frame",
    [
        (F(1), "identity", 0),
        (F(25, 24), "24 fps material at 25 fps output", 0),
        (F(24, 25), "25 fps material at 24 fps output", 0),
        (T1001_1000, "NTSC pull-down style 0.1% speed-up", 0),
        (T1000_1001, "0.1% slow-down: ticks land between frames", 24),
        (F(4), "4x fast forward", 0),
        (F(1, 4), "4x slow motion", 0),
    ],
)
def test_every_output_frame_shows_the_planned_picture(tmp_path, rate, label, allow_one_frame):
    source = make_identity_video(tmp_path, count=150)
    project = video_project(source, "src", F(25))
    project = place(project, "clip", F(1), F(6), rate=rate)
    end = project.output_end()
    plan, measured = render(project, {"src": source}, ZERO, end, tmp_path / "whole.mkv")
    # a speed-up legitimately has fewer frames; what must never happen is drift,
    # so the fixture is checked to be long enough for the rate under test
    assert plan.total_frames >= min(90, 40), "the fixture must be long enough to catch drift"
    stamps = stamp_seconds(source)
    assert_matches(measured, planned(plan, project, {"src": source}, stamps),
                   f"{label} at rate {rate}", allow_one_frame=allow_one_frame)
    first = min(i for i, pts in enumerate(stamps) if pts >= F(1))
    last = max(i for i, pts in enumerate(stamps) if pts < F(6))
    assert_real_pictures(measured, f"{label} at rate {rate}", first, last)


def test_a_fractional_source_position_stays_on_the_right_picture(tmp_path):
    """An in-point that is not a frame boundary must not move the pictures."""
    source = make_identity_video(tmp_path, count=150)
    project = video_project(source, "src", F(25))
    project = place(project, "clip", F(1, 3), F(5))
    stamps = stamp_seconds(source)
    plan, measured = render(project, {"src": source}, ZERO, project.output_end(),
                            tmp_path / "fractional.mkv")
    assert_matches(measured, planned(plan, project, {"src": source}, stamps),
                   "fractional source in at rate 1")


def test_two_clips_meeting_inside_a_frame_keep_their_pictures(tmp_path):
    """Half-frame boundaries: both edges snap forward to the same frame.

    The cut is at 1/3 s, which is 8.33 frames: the lattice gives the first clip
    frames 0-8 and the second clip frames 9-24. No frame is counted twice and none
    is lost, and each frame shows its own clip's picture.
    """
    source = make_identity_video(tmp_path, count=150)
    project = video_project(source, "src", F(25))
    project = place(project, "a", ZERO, F(1, 3))  # 0 .. 0.0333 s
    project = place(project, "b", F(2), F(4), output_start=F(1, 3))
    stamps = stamp_seconds(source)
    plan, measured = render(project, {"src": source}, ZERO, F(1), tmp_path / "half.mkv")
    quant = plan.quantisation
    assert plan.total_frames == quant.frame_count(F(1)) == 25
    base = quant.frame_index(ZERO)
    owner = [plan.picture_at(quant.frame_time(base + i)).clip.id for i in range(25)]
    assert owner[:9] == ["a"] * 9 and owner[9:] == ["b"] * 16, "the cut is not at frame 9"
    expected = planned(plan, project, {"src": source}, stamps)
    assert expected[8] == 8, "the first clip lost or gained a frame at the cut"
    assert expected[9] == 50, "the second clip's first picture is not its in-point"
    assert_matches(measured, expected, "half-frame cut")


# -------------------------------------------------------------------- the paths


def test_a_range_render_shows_exactly_the_frames_of_the_whole_render(tmp_path):
    """The preview path cannot promise a picture the export would not write.

    Rendered as two ranges and joined, the pictures must be identical to one
    render of the whole range - including at the join, where an unanchored sampling
    grid restarts and the frames near it shift.
    """
    source = make_identity_video(tmp_path, count=150)
    project = video_project(source, "src", F(25))
    project = place(project, "clip", F(1, 3), F(6), rate=F(25, 24))
    end = project.output_end()
    plan, whole = render(project, {"src": source}, ZERO, end, tmp_path / "whole.mkv")
    split = plan.quantisation.frame_time(plan.quantisation.frame_index(end / 2))
    _, first = render(project, {"src": source}, ZERO, split, tmp_path / "first.mkv")
    _, second = render(project, {"src": source}, split, end, tmp_path / "second.mkv")
    assert len(first) + len(second) == len(whole), (
        f"ranges gave {len(first)}+{len(second)} frames for {len(whole)}"
    )
    assert_matches(first + second, whole, "joined ranges against the whole render")


def test_a_still_is_the_frame_the_export_writes_at_that_index(tmp_path):
    """One sampling contract: the still viewer and the export agree everywhere.

    Checked at the places a difference hides: an exact frame boundary, a fraction
    of a frame past one, a fraction before one, and the last frame of the output -
    each inside a clip whose rate is not 1, where the sampling time and the source
    frames are on different grids.
    """
    source = make_identity_video(tmp_path, count=150)
    project = video_project(source, "src", F(25))
    project = place(project, "clip", F(1), F(6), rate=F(25, 24))
    end = project.output_end()
    plan, exported = render(project, {"src": source}, ZERO, end, tmp_path / "out.mkv")
    paths = {"src": source}
    quant = plan.quantisation
    base = quant.frame_index(ZERO)
    probes = [
        quant.frame_time(base + i)
        for i in (0, 1, quant.total_frames // 3, quant.total_frames // 3 + 1,
                  quant.total_frames - 1)
    ]
    probes += [t + F(1, 1000) for t in probes[:3]]  # not on a frame boundary
    for moment in probes:
        still = extract_composition_still(project, paths, moment)
        assert still.frame_index == quant.frame_index(moment)
        assert exported[still.frame_index - base] == identity_at(
            still.rgb, still.width, still.height
        ), f"output {float(moment):.6f}s: still and export show different pictures"
        assert still.grid_exact, "the container time base is readable for this fixture"
        assert still.decoded_source_pts is not None, "the picture's source time is measurable"


def test_a_still_after_a_cut_shows_the_next_clip(tmp_path):
    """Frame identity across an edit: before, on, and one frame after the boundary."""
    source = make_identity_video(tmp_path, count=150)
    project = video_project(source, "src", F(25))
    project = place(project, "a", ZERO, F(2))
    project = place(project, "b", F(100), F(102), output_start=F(2))
    paths = {"src": source}
    before = extract_composition_still(project, paths, F(2) - F(1, 25))
    at = extract_composition_still(project, paths, F(2))
    assert before.clip_id == "a" and at.clip_id == "b"
    assert identity_at(before.rgb, before.width, before.height) == 49
    assert identity_at(at.rgb, at.width, at.height) == 100
    assert at.decoded_source_pts == F(100, 25)


# --------------------------------------------------- awkward streams and bounds


def test_bframes_do_not_change_which_picture_is_which(tmp_path):
    """MP4/H.264 with B-frames: presentation order is what the plan addresses."""
    source = make_identity_bframes(tmp_path, count=100)
    project = video_project(source, "src", F(25))
    project = place(project, "clip", F(1, 2), F(4), rate=F(25, 24))
    stamps = stamp_seconds(source)
    plan, measured = render(project, {"src": source}, ZERO, project.output_end(),
                            tmp_path / "b.mkv")
    assert_matches(measured, planned(plan, project, {"src": source}, stamps), "B-frame source")
    still = extract_composition_still(project, {"src": source}, F(1))
    assert still.decoded_source_pts in stamps, "the reported PTS is a real frame of the stream"


def test_a_stream_that_does_not_start_at_zero_keeps_its_own_origin(tmp_path):
    """Nonzero start time: asset-local addressing, seek arithmetic included."""
    source = make_identity_offset(tmp_path, count=100)
    project = video_project(source, "src", F(25))
    origin = project.source("src", 0).origin
    assert origin == F(5, 2), "the fixture's presentation starts at 2.5 s"
    project = place(project, "clip", origin + F(1), origin + F(5), rate=F(25, 24))
    stamps = stamp_seconds(source)
    plan, measured = render(project, {"src": source}, ZERO, project.output_end(),
                            tmp_path / "o.mkv")
    assert_matches(measured, planned(plan, project, {"src": source}, stamps),
                   "nonzero stream origin")


def test_variable_frame_rate_material_uses_the_real_timestamps(tmp_path):
    """Irregular spacing: a nominal-rate assumption lands on the wrong pictures."""
    source = make_identity_vfr(tmp_path)
    project = video_project(source, "src", F(25))
    project = place(project, "clip", F(3), F(9), rate=F(5, 4))
    stamps = stamp_seconds(source)
    gaps = {b - a for a, b in itertools.pairwise(stamps)}
    assert len(gaps) > 2, "the fixture is irregular"
    plan, measured = render(project, {"src": source}, ZERO, project.output_end(),
                            tmp_path / "vfr.mkv")
    assert_matches(measured, planned(plan, project, {"src": source}, stamps), "VFR source")


def test_a_slice_ending_on_the_last_legal_frame_holds_that_picture(tmp_path):
    """The last frames of a clip: the lattice asks past the end, so the picture holds.

    The plan's frame count is still met exactly - the held picture is repeated -
    rather than the item coming up short and every later frame shifting.
    """
    source = make_identity_video(tmp_path, count=50)
    stamps = stamp_seconds(source)
    project = video_project(source, "src", F(25))
    project = place(project, "clip", F(40, 25), F(50, 25), rate=F(5, 4))
    plan, measured = render(project, {"src": source}, ZERO, project.output_end(),
                            tmp_path / "tail.mkv")
    expected = planned(plan, project, {"src": source}, stamps)
    assert expected[-1] == 49 and measured[-1] == 49, "the last legal picture is held"
    assert_matches(measured, expected, "slice ending at the last legal frame")
    assert measured.count(49) >= 1


def test_a_gap_is_black_and_does_not_steal_a_neighbouring_picture(tmp_path):
    source = make_identity_video(tmp_path, count=100)
    project = video_project(source, "src", F(25))
    project = place(project, "a", ZERO, F(1))
    project = place(project, "b", F(50), F(51), output_start=F(2))
    stamps = stamp_seconds(source)
    plan, measured = render(project, {"src": source}, ZERO, F(3), tmp_path / "gap.mkv")
    assert_matches(measured, planned(plan, project, {"src": source}, stamps), "gap")
    assert measured[0] is not None and measured[-1] is not None
    assert len([m for m in measured if m is None]) == plan.total_frames - 50


def test_a_frame_past_the_end_of_the_composition_is_refused_not_invented(tmp_path):
    """Inspection cannot show a frame the composition does not have.

    Two similar requests, two different honest answers. Asking the viewer for a
    frame at or past the end is an error that names the end: there is no picture
    there, and showing black would present a frame the export never writes.
    Asking the *planner* for a range that runs past the end is a request for a
    longer timeline, and it answers with explicit black items - the behaviour
    documented for the planner since TASK_05, checked here so both stay as stated
    instead of drifting towards each other.
    """
    source = make_identity_video(tmp_path, count=50)
    project = video_project(source, "src", F(25))
    project = place(project, "clip", ZERO, F(2))
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
    assert plan.total_frames == 60, "the range is 2.4 s: 60 frames, black included"
    black = [item for item in plan.video_items if item.is_black]
    assert sum(item.frames for item in black) == 25, "the tail past the end is explicit black"
    assert measured[0] == 37 and measured[-1] is None


# --------------------------------------------------------------------- geometry


def test_anamorphic_picture_is_placed_by_its_display_aspect(tmp_path):
    """720x576 at 64:45 sample aspect is 16:9: it must fill a 1280x720 output.

    Ignoring the recorded sample aspect ratio would scale the 5:4 *pixels* instead
    and leave pillarbars; the still and the export must make the same choice, and
    either one alone would look plausible.
    """
    source = make_identity_anamorphic(tmp_path, count=50)
    project = video_project(source, "src", F(25), size=(1280, 720))
    project = place(project, "clip", ZERO, F(2))
    plan, measured = render(project, {"src": source}, ZERO, F(1), tmp_path / "an.mkv")
    assert plan.width == 1280 and plan.height == 720
    assert all(m is not None for m in measured), "no frame of an anamorphic source is black"
    assert measured[2] == 2, "the picture identity survives the scaling"
    still = extract_composition_still(project, {"src": source}, F(2, 25))
    assert (still.width, still.height) == (1280, 720)
    assert identity_at(still.rgb, still.width, still.height) == measured[2]
    # the whole frame is used: no black border on either side at the mid-line
    row = still.rgb
    corners = [
        row[y * still.width * 3 + x * 3]
        for y in (0, still.height // 2, still.height - 1)
        for x in (0, still.width - 1)
    ]
    assert any(value != 0 for value in corners), (
        "the scaled picture left black corners: the display aspect was not honoured"
    )
