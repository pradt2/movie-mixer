"""Frame-accurate inspection tests: composition stills vs export, and real PTS.

Everything is measured against real FFmpeg output: a still is compared with the
frame the *exporter* writes at the same output frame index, and source stepping
is compared with the packet timestamps ffprobe reports for a VFR fixture.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from media_fixtures import (
    HAS_FFMPEG,
    make_colour_steps,
    make_tone,
    make_vfr,
    make_whole_asset_offset,
)
from test_render_export import composition, export, frame_at, nearest, reference_colours
from video_editor.core.commands import AddClipCommand, RegisterSourceCommand
from video_editor.core.model import OutputSettings, Project, StreamSource, Track, TrackKind
from video_editor.fs.identity import Identity
from video_editor.media.framegrab import (
    SourceFrame,
    StillError,
    extract_composition_still,
    frames_around,
    nearest_frame,
    presented_frame,
    source_frame_times,
    still_plan_for,
    stream_position_of,
)
from video_editor.render.plan import RenderPlanError, build_plan

F = Fraction
pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")

FPS = F(25)
SR = 48000


def picture_only(steps: Path) -> tuple[Project, dict[str, Path]]:
    """One video track playing the whole 6 s colour-steps fixture."""
    project = Project(
        output=OutputSettings(128, 96, FPS, SR, "stereo")
    )
    project = RegisterSourceCommand(
        StreamSource("steps", 0, TrackKind.VIDEO, F(0), F(6), frame_rate=FPS,
                     width=128, height=96, stream_position=0)
    ).apply(project)
    project = project.with_tracks((Track("vbot", TrackKind.VIDEO, "Bottom"),))
    project = AddClipCommand("steps", 0, "vbot", F(0), source_in=F(0), source_out=F(6),
                             clip_id="bottom").apply(project)
    return project, {"steps": steps}


def centre(frame) -> tuple[int, int, int]:
    mid = (frame.height // 2) * frame.width * 3 + (frame.width // 2) * 3
    return frame.rgb[mid], frame.rgb[mid + 1], frame.rgb[mid + 2]


# --------------------------------------------------------- composition stills


def test_still_shows_the_picture_the_composition_presents(tmp_path):
    steps = make_colour_steps(tmp_path)
    project, paths = picture_only(steps)
    still = extract_composition_still(project, paths, F(5, 2))
    assert (still.width, still.height) == (128, 96)
    assert not still.black
    assert still.frame_index == 63  # round_half_up(2.5*25) = round_half_up(62.5)
    assert still.output_time == F(63, 25)  # the quantised time that frame presents
    assert still.clip_id == "bottom"
    assert still.asset_id == "steps" and still.stream_index == 0
    # rate 1: the frame's own quantised time is also the asset-local slice start
    assert still.source_pts == F(63, 25)
    # 63/25 s = 2.52 s of the source: the third 1 s block
    reference = reference_colours(steps)
    assert nearest(reference, centre(still)) == "blue"


def test_a_source_time_that_is_a_real_frame_reports_that_frame(tmp_path):
    """The reported source PTS must not slip to the next frame.

    The plan asks for source 8/25 s of a 25 fps stream, which is a real frame, so
    the picture and the reported PTS are that frame. A probe that looked at the
    *next* frame instead would make the "real frame at …" note, and therefore any
    source-edge marking guided by it, point one picture too far.
    """
    steps = make_colour_steps(tmp_path)
    project, paths = picture_only(steps)
    still = extract_composition_still(project, paths, F(3, 10))
    assert still.frame_index == 8 and still.output_time == F(8, 25)
    assert still.source_pts == F(8, 25)
    assert still.decoded_source_pts == F(8, 25)
    reference = reference_colours(steps)
    assert nearest(reference, centre(still)) == "red"


def test_presented_frame_prefers_the_exact_frame_and_otherwise_the_next():
    frames = [
        SourceFrame(pts=F(0), index=0),
        SourceFrame(pts=F(1, 25), index=1),
        SourceFrame(pts=F(3, 50), index=2),  # a shorter gap: VFR-like
    ]
    assert presented_frame(frames, F(1, 25)).pts == F(1, 25)  # exactly this frame
    assert presented_frame(frames, F(1, 25) + F(1, 100)).pts == F(3, 50)  # between two
    assert presented_frame(frames, F(1)) is None  # past the last picture


def test_still_agrees_with_the_exported_frame_at_the_same_index(tmp_path):
    """The agreed acceptance rule: sampled picture == exported picture."""
    steps = make_colour_steps(tmp_path)
    t660 = make_tone(tmp_path, "a.wav", 660)
    t990 = make_tone(tmp_path, "b.wav", 990)
    project, paths = composition(steps, t660, t990)
    reference = reference_colours(steps)
    out = tmp_path / "composition.mp4"
    export(project, paths, out, out_end=F(6))

    for t in (F(0), F(3, 10), F(1), F(9999, 10000), F(2), F(7, 2), F(5, 100), F(6) - F(1, 25)):
        still = extract_composition_still(project, paths, t, ffprobe=None)
        exported = frame_at(out, still.frame_index)
        assert nearest(reference, centre(still)) == nearest(reference, exported), (
            f"output t={t}: still {centre(still)} vs export {exported}"
        )
        # and the frame index is exactly the one the full-range plan picks
        full = build_plan(project, F(0), F(6))
        assert still.frame_index == full.quantisation.frame_index(t)


def test_still_respects_preview_geometry_scale_without_changing_time(tmp_path):
    steps = make_colour_steps(tmp_path)
    project, paths = picture_only(steps)
    still = extract_composition_still(project, paths, F(1, 2), geometry_scale=F(1, 2))
    assert (still.width, still.height) == (64, 48)
    assert still.frame_index == 13 and still.output_time == F(13, 25)
    assert still.nbytes == 64 * 48 * 3


def test_black_gap_yields_a_truthful_black_frame(tmp_path):
    steps = make_colour_steps(tmp_path)
    project, paths = picture_only(steps)
    still = extract_composition_still(project, paths, F(7))  # past the only clip
    assert still.black and still.clip_id is None and still.source_pts is None
    assert centre(still) == (0, 0, 0)


def test_a_rate_changed_clip_reads_the_source_it_actually_uses(tmp_path):
    steps = make_colour_steps(tmp_path)
    project, paths = picture_only(steps)
    # 2 source seconds per output second, starting at source 4 s: output 0.5 s
    # sits at source 5 s -> the last block (cyan)
    project = AddClipCommand("steps", 0, "vbot", F(10), source_in=F(4), source_out=F(6),
                             rate=F(2), clip_id="fast").apply(project)
    still = extract_composition_still(project, paths, F(21, 2))
    assert still.clip_id == "fast"
    # the plan's global index for output 10.5 s is 126/25 = 5.04 s of source
    assert still.source_pts == F(126, 25)
    # 5.04 s is a real frame of this 25 fps stream (126 x 1/25), so the reported
    # source PTS is that frame itself, not the next one
    assert still.decoded_source_pts == F(126, 25)


def test_a_planned_time_between_real_frames_names_the_frame_seek_produces(tmp_path):
    """On VFR material the plan can ask for a time with no frame at it.

    FFmpeg's input ``-ss`` discards pictures before the requested time, so the
    picture shown - and the PTS the inspector reports - is the later frame. Naming
    the planned time instead would label the source edge the user is looking at
    wrongly, exactly where the two editions' cuts are being compared.
    """
    vfr = make_vfr(tmp_path)
    project = Project(output=OutputSettings(128, 96, FPS, SR, "stereo"))
    project = RegisterSourceCommand(
        StreamSource("vfr", 0, TrackKind.VIDEO, F(0), F(12), frame_rate=F(25),
                     width=128, height=96, stream_position=0)
    ).apply(project)
    project = project.with_tracks((Track("vbot", TrackKind.VIDEO, "Bottom"),))
    project = AddClipCommand("vfr", 0, "vbot", F(0), source_in=F(8), source_out=F(12),
                             clip_id="slow").apply(project)
    still = extract_composition_still(project, {"vfr": vfr}, F(2, 5))
    assert still.source_pts == F(210, 25)  # 8.4 s: not a frame of this stream
    assert still.decoded_source_pts == F(8417, 1000)  # the first real frame at/after it


def test_missing_asset_is_reported_not_substituted(tmp_path):
    steps = make_colour_steps(tmp_path)
    project, _paths = picture_only(steps)
    with pytest.raises(StillError, match=r"is not there"):
        extract_composition_still(project, {"steps": tmp_path / "gone.mkv"}, F(1))


def test_asset_missing_from_disk_is_named(tmp_path):
    steps = make_colour_steps(tmp_path)
    project, paths = picture_only(steps)
    project = RegisterSourceCommand(
        StreamSource("other", 0, TrackKind.VIDEO, F(0), F(6), frame_rate=FPS,
                     width=128, height=96, stream_position=0)
    ).apply(project)
    project = AddClipCommand("other", 0, "vbot", F(7), source_in=F(0), source_out=F(1),
                             clip_id="ghost").apply(project)
    with pytest.raises(StillError, match=r"has no file recorded"):
        extract_composition_still(project, paths, F(15, 2))  # inside the ghost clip


def test_a_replaced_source_is_refused_rather_than_guessed(tmp_path):
    """A different encode at the right path must not be decoded.

    The recorded stream bounds and frame rate belong to the file that was
    imported; decoding a different one and presenting its picture would look
    exactly like a correct still — so the identity check, not a decode error, is
    what stops it.
    """
    steps = make_colour_steps(tmp_path)
    project, paths = picture_only(steps)
    recorded = Identity.of(steps)
    substitute = make_colour_steps(tmp_path, name="other-encode.mkv")
    steps.write_bytes(substitute.read_bytes())  # a valid file, but not this one
    with pytest.raises(StillError, match=r"not the file recorded here"):
        extract_composition_still(project, paths, F(1), asset_identities={"steps": recorded})
    # with no recorded identity there is nothing to contradict: the same bytes
    # decode, which shows the refusal above came from the identity check
    assert extract_composition_still(project, paths, F(1)).width > 0


def test_unregistered_stream_is_reported(tmp_path):
    """A clip whose stream is not in ``project.sources`` cannot be resolved."""
    steps = make_colour_steps(tmp_path)
    project, paths = picture_only(steps)
    project = replace(
        project, sources=tuple(s for s in project.sources if s.asset_id != "steps")
    )
    # the planner refuses the same way the exporter does, before any decoding
    with pytest.raises(RenderPlanError, match=r"not registered in the project"):
        extract_composition_still(project, paths, F(1))


def test_garbage_source_fails_rather_than_showing_something_else(tmp_path):
    from media_fixtures import make_garbage

    broken = make_garbage(tmp_path)
    project = Project(output=OutputSettings(128, 96, FPS, SR, "stereo"))
    project = RegisterSourceCommand(
        StreamSource("broken", 0, TrackKind.VIDEO, F(0), F(1), frame_rate=FPS,
                     width=128, height=96, stream_position=0)
    ).apply(project)
    project = project.with_tracks((Track("v", TrackKind.VIDEO, "V1"),))
    project = AddClipCommand("broken", 0, "v", F(0), source_in=F(0), source_out=F(1),
                             clip_id="c").apply(project)
    with pytest.raises(StillError, match=r"cannot decode the frame"):
        extract_composition_still(project, {"broken": broken}, F(0))


def test_silly_requests_are_refused(tmp_path):
    steps = make_colour_steps(tmp_path)
    project, paths = picture_only(steps)
    with pytest.raises(StillError, match="non-negative"):
        extract_composition_still(project, paths, F(-1))
    with pytest.raises(RenderPlanError, match=r"scale .* must be positive"):
        extract_composition_still(project, paths, F(1), geometry_scale=F(0))


def test_still_plan_frame_time_matches_the_plan_it_builds(tmp_path):
    steps = make_colour_steps(tmp_path)
    project, _paths = picture_only(steps)
    plan, frame_time, index = still_plan_for(project, F(199999, 100000))
    assert index == 50 and frame_time == F(2)
    assert plan.total_frames == 1
    assert plan.picture_at(frame_time) is not None


# ------------------------------------------------------------------ real PTS


def test_stream_position_translation_matches_the_container(tmp_path):
    steps = make_colour_steps(tmp_path)
    assert stream_position_of(steps, 0) == 0
    with pytest.raises(StillError, match="not present"):
        stream_position_of(steps, 7)


def test_vfr_timestamps_are_irregular_and_exact(tmp_path):
    """Real packet timestamps, not nominal-rate arithmetic.

    Measured on this fixture: the container stores this stream with a 1 ms time
    base, so the slow section is irregular at the container's own granularity
    (~83/84 ms steps) rather than exactly 1/12. Timestamps come back as exact
    rationals built from integer pts x time_base, which is what keeps a source
    step on a real frame instead of a printed-decimal approximation of it.
    """
    vfr = make_vfr(tmp_path)
    frames = source_frame_times(vfr, 0)
    assert len(frames) == 248  # 8 s @25 fps + 4 s @12 fps
    assert [f.pts for f in frames] == sorted({f.pts for f in frames})
    assert frames[0].pts == F(0) and frames[-1].pts == F(11917, 1000)
    gaps = [b.pts - a.pts for a, b in itertools.pairwise(frames)]
    assert all(g > 0 for g in gaps)
    assert gaps.count(F(1, 25)) == 200  # the constant-rate section is exact
    slow = [g for g in gaps if abs(g - F(1, 12)) < F(1, 500)]
    assert len(slow) == 47
    # the slow section is irregular at the container's own granularity: the steps
    # alternate rather than being one constant frame duration
    assert {g for g in slow} == {F(83, 1000), F(21, 250)}


def test_source_stepping_lands_on_real_neighbouring_frames(tmp_path):
    vfr = make_vfr(tmp_path)
    frames = source_frame_times(vfr, 0)
    stamps = {f.pts for f in frames}

    # across the rate change: the real neighbour is 8.0 s, a 1/12 step from the
    # previous frame would have asked for 8.083 s
    last_25 = nearest_frame(frames, F(8), -1)
    assert last_25.pts == F(199, 25)
    assert nearest_frame(frames, last_25.pts, +1).pts == F(8)

    # inside the slow section the nominal 25 fps grid has drifted off the real
    # frames: 8.4 s (one nominal step from 8.36) is not a frame at all, while
    # 8.333 and 8.417 are
    assert F(210, 25) not in stamps
    assert nearest_frame(frames, F(210, 25), -1).pts == F(8333, 1000)
    assert nearest_frame(frames, F(210, 25), +1).pts == F(8417, 1000)
    assert nearest_frame(frames, F(8333, 1000), +1).pts == F(8417, 1000)


def test_probe_window_keeps_source_stepping_bounded(tmp_path):
    vfr = make_vfr(tmp_path)
    source = StreamSource("vfr", 0, TrackKind.VIDEO, F(0), F(12), frame_rate=F(25),
                          stream_position=0)
    neighbours = frames_around(vfr, source, F(9), window=F(1))
    assert neighbours.at_or_before is not None and neighbours.at_or_after is not None
    assert F(8) <= neighbours.at_or_before.pts <= F(9) <= neighbours.at_or_after.pts <= F(10)
    # stepping wants the frames on *either side*, so the frame sitting exactly on
    # 9.000 s is not offered as its own neighbour (direction 0 returns it)
    assert neighbours.at_or_before.pts == F(8917, 1000)
    assert neighbours.at_or_after.pts == F(9083, 1000)
    full = source_frame_times(vfr, 0)
    assert nearest_frame(full, F(9), 0).pts == F(9)
    step = neighbours.at_or_after.pts - neighbours.at_or_before.pts
    assert abs(step - 2 * F(1, 12)) <= F(1, 500)
    full = source_frame_times(vfr, 0)
    assert any(f.pts == neighbours.at_or_before.pts for f in full)


def test_an_offset_asset_keeps_its_frames_on_the_asset_axis(tmp_path):
    """Regression: packet PTS are already on the asset-local axis.

    This file's whole presentation starts at 2.5 s, and ffprobe reports the first
    video packet as ``pts 2500`` at ``time_base 1/1000`` together with
    ``start_time 2.5``. Adding ``start_time`` to ``pts x time_base`` - which an
    earlier revision did - put every frame 2.5 s late, so stepping offered times
    with no frame at them and a source edge taken from them was seconds away from
    the picture being shown. Measured on Matroska and MP4, for whole-asset and
    per-stream offsets alike.
    """
    offset = make_whole_asset_offset(tmp_path)
    frames = source_frame_times(offset, 0)
    assert len(frames) == 50  # 2 s at 25 fps
    assert frames[0].pts == F(5, 2) and frames[-1].pts == F(5, 2) + F(49, 25)
    assert {b.pts - a.pts for a, b in itertools.pairwise(frames)} == {F(1, 25)}
    assert stream_position_of(offset, 0) == 0
    source = StreamSource("offset", 0, TrackKind.VIDEO, F(5, 2), F(9, 2),
                          frame_rate=FPS, stream_position=0)
    neighbours = frames_around(offset, source, F(3) + F(1, 50), window=F(1))
    # the grid of this file is shifted by its own start: 3.02 s is a frame, 3.0 s
    # is not, and the neighbours of 3.02 are 2.98 and 3.06
    assert neighbours.at_or_before.pts == F(149, 50) and neighbours.at_or_after.pts == F(153, 50)
    assert nearest_frame(frames, F(5, 2), +1).pts == F(127, 50)  # 2.54 s


def test_a_still_from_an_offset_asset_reports_asset_local_time(tmp_path):
    """Nonzero stream origins survive the whole still path, not only the probe."""
    offset = make_whole_asset_offset(tmp_path)
    project = Project(output=OutputSettings(128, 96, FPS, SR, "stereo"))
    project = RegisterSourceCommand(
        StreamSource("offset", 0, TrackKind.VIDEO, F(5, 2), F(9, 2), frame_rate=FPS,
                     width=128, height=96, input_start=F(5, 2), stream_position=0)
    ).apply(project)
    project = project.with_tracks((Track("vbot", TrackKind.VIDEO, "Bottom"),))
    project = AddClipCommand("offset", 0, "vbot", F(0), source_in=F(5, 2),
                             source_out=F(9, 2), clip_id="late").apply(project)
    still = extract_composition_still(project, {"offset": offset}, F(1, 50))
    assert not still.black and still.clip_id == "late"
    assert still.source_pts == F(127, 50)  # 2.54 s asset-local, a real frame here
    assert still.decoded_source_pts == F(127, 50)
    red, green, blue = centre(still)
    assert blue > red + 40 and blue > green + 40  # the fixture is solid blue


def test_a_non_video_stream_is_refused(tmp_path):
    tone = make_tone(tmp_path, "tone.wav", 660)
    with pytest.raises(StillError, match=r"not present|not video"):
        source_frame_times(tone, 0)
