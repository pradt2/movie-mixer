"""Frame-accurate inspection tests: composition stills vs export, and real PTS.

Everything is measured against real FFmpeg output: a still is compared with the
frame the *exporter* writes at the same output frame index, source stepping is
compared with the timestamps a VFR fixture really carries, and the bounded
decoded-picture probe underneath the frame index is checked against the pictures
that file decodes — including what one read can and cannot claim about the ground
it covered.
"""

from __future__ import annotations

import itertools
import json
import subprocess
from bisect import bisect_left
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from media_fixtures import (
    FFPROBE,
    HAS_FFMPEG,
    identity_at,
    make_colour_steps,
    make_garbage,
    make_identity_bframes,
    make_identity_offset,
    make_identity_vfr,
    make_tone,
    make_truncated_identity_offset,
    make_vfr,
    make_whole_asset_offset,
    picture_facts,
    source_from_probe,
)
from test_render_export import composition, export, frame_at, nearest, reference_colours
from video_editor.core.commands import AddClipCommand, RegisterSourceCommand
from video_editor.core.model import OutputSettings, Project, StreamSource, Track, TrackKind
from video_editor.fs.identity import Identity
from video_editor.media.framegrab import (
    StillError,
    extract_composition_still,
    extract_source_still,
    source_frame_times,
    still_plan_for,
    stream_position_of,
    stream_timing,
)
from video_editor.media.frameindex import FrameIndex
from video_editor.media.frameprobe import (
    FrameProbeError,
    declared_end,
    decoded_picture_times,
    picture_stamps,
)
from video_editor.render.plan import RenderPlanError, build_plan, picture_read_window

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
    assert still.frame_index == 62  # floor(2.5*25): the frame already covering 2.5 s
    assert still.output_time == F(62, 25)  # the quantised time that frame presents
    assert still.clip_id == "bottom"
    assert still.asset_id == "steps" and still.stream_index == 0
    # rate 1: the frame's own quantised time is also the asset-local slice start
    assert still.source_pts == F(62, 25)
    # 62/25 s = 2.48 s of the source: still inside the third 1 s block
    reference = reference_colours(steps)
    assert nearest(reference, centre(still)) == "blue"


def test_a_source_time_that_is_a_real_frame_reports_that_frame(tmp_path):
    """The reported source PTS must not slip to the next frame.

    A playhead at 0.3 s shows frame ``floor(0.3*25)`` = 7, whose sampling time is
    7/25 s - a real frame of this 25 fps stream - so the picture and the reported
    PTS are that frame. A probe that looked at the *next* frame instead would make
    the "real frame at …" note, and therefore any source-edge marking guided by it,
    point one picture too far.
    """
    steps = make_colour_steps(tmp_path)
    project, paths = picture_only(steps)
    still = extract_composition_still(project, paths, F(3, 10))
    assert still.frame_index == 7 and still.output_time == F(7, 25)
    assert still.source_pts == F(7, 25)
    assert still.decoded_source_pts == F(7, 25)
    reference = reference_colours(steps)
    assert nearest(reference, centre(still)) == "red"


def test_still_agrees_with_the_exported_frame_at_the_same_index(tmp_path):
    """The agreed acceptance rule: sampled picture == exported picture.

    Each probe names, by hand, the frame a playhead at that output time shows:
    ``floor(t*25)`` — 0.3 s is frame 7 (7.5 rounded down), 0.9999 s is frame 24
    (24.9975), 5.96 s is frame 149, the last frame this composition writes.
    """
    steps = make_colour_steps(tmp_path)
    t660 = make_tone(tmp_path, "a.wav", 660)
    t990 = make_tone(tmp_path, "b.wav", 990)
    project, paths = composition(steps, t660, t990)
    reference = reference_colours(steps)
    out = tmp_path / "composition.mp4"
    export(project, paths, out, out_end=F(6))

    probes = [
        (F(0), 0),
        (F(3, 10), 7),
        (F(1), 25),
        (F(9999, 10000), 24),
        (F(2), 50),
        (F(7, 2), 87),
        (F(5, 100), 1),
        (F(6) - F(1, 25), 149),
    ]
    for t, index in probes:
        still = extract_composition_still(project, paths, t, ffprobe=None)
        assert still.frame_index == index, f"playhead {float(t)}s shows {still.frame_index}"
        exported = frame_at(out, index)
        assert nearest(reference, centre(still)) == nearest(reference, exported), (
            f"output t={t}: still {centre(still)} vs export {exported}"
        )


def test_still_respects_preview_geometry_scale_without_changing_time(tmp_path):
    steps = make_colour_steps(tmp_path)
    project, paths = picture_only(steps)
    still = extract_composition_still(project, paths, F(1, 2), geometry_scale=F(1, 2))
    assert (still.width, still.height) == (64, 48)
    assert still.frame_index == 12 and still.output_time == F(12, 25)  # floor(12.5)
    assert still.nbytes == 64 * 48 * 3


def test_black_gap_yields_a_truthful_black_frame(tmp_path):
    """Where no clip reaches *inside* the composition the frame is black.

    Asking past the end of the composition is a different question and gets a
    different answer: there is no frame there, and showing black would present a
    frame the export never writes at that index.
    """
    steps = make_colour_steps(tmp_path)
    project = Project(
        output=OutputSettings(128, 96, FPS, SR, "stereo")
    )
    project = RegisterSourceCommand(
        StreamSource("steps", 0, TrackKind.VIDEO, F(0), F(6), frame_rate=FPS,
                     width=128, height=96, stream_position=0)
    ).apply(project)
    project = project.with_tracks((Track("vbot", TrackKind.VIDEO, "Bottom"),))
    project = AddClipCommand("steps", 0, "vbot", F(0), source_in=F(0), source_out=F(2),
                             clip_id="first").apply(project)
    project = AddClipCommand("steps", 0, "vbot", F(4), source_in=F(4), source_out=F(6),
                             clip_id="second").apply(project)
    paths = {"steps": steps}
    assert project.output_end() == F(6)
    still = extract_composition_still(project, paths, F(3))  # the gap between clips
    assert still.black and still.clip_id is None and still.source_pts is None
    assert still.frame_index == 75 and still.output_time == F(3)
    assert centre(still) == (0, 0, 0)
    with pytest.raises(StillError, match="ends at") as refused:
        extract_composition_still(project, paths, F(6))  # the end, not a gap
    # the refusal points back at the last frame that exists; nothing is padded in
    assert "the last frame it has is at 5.960000s" in str(refused.value)


def test_a_black_gap_is_answered_without_anything_being_decoded(tmp_path, monkeypatch):
    """A black output frame costs no decode — and here that is proven, not assumed.

    The still path reaches an external tool in two places (the time-base probe and
    the picture decode), so both are replaced by a call that fails the test. The plan
    says this output frame is black, so the answer is the composition's own black
    frame with no clip under it, and nothing is asked of FFmpeg to produce it.
    """
    from video_editor.media import framegrab as framegrab_module

    class _NoTools:
        """Stands in for ``subprocess``: any call is a decode, and there must be none."""

        TimeoutExpired = subprocess.TimeoutExpired

        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def run(self, args, **_kwargs):
            self.calls.append(list(args))
            raise AssertionError(
                f"a black output frame asked for a decode: {args[:3]}"
            )

    sentinel = _NoTools()
    monkeypatch.setattr(framegrab_module, "subprocess", sentinel)
    steps = make_colour_steps(tmp_path)
    project = Project(output=OutputSettings(128, 96, FPS, SR, "stereo"))
    project = RegisterSourceCommand(
        StreamSource("steps", 0, TrackKind.VIDEO, F(0), F(6), frame_rate=FPS,
                     width=128, height=96, stream_position=0)
    ).apply(project)
    project = project.with_tracks((Track("vbot", TrackKind.VIDEO, "Bottom"),))
    project = AddClipCommand("steps", 0, "vbot", F(0), source_in=F(0), source_out=F(2),
                             clip_id="first").apply(project)
    project = AddClipCommand("steps", 0, "vbot", F(4), source_in=F(4), source_out=F(6),
                             clip_id="second").apply(project)
    still = extract_composition_still(project, {"steps": steps}, F(3))
    assert sentinel.calls == [], "a black frame was decoded rather than planned"
    assert still.black and still.clip_id is None
    assert still.source_pts is None and still.decoded_source_pts is None
    assert still.frame_index == 75 and still.output_time == F(3)
    assert centre(still) == (0, 0, 0)


def make_fine_timescale_identity(
    tmp_path: Path, name: str = "identity_fine.mp4", count: int = 150,
    timescale: int = 50_000_000
) -> Path:
    """Identity pictures in an MP4 whose track timescale leaves no exact filter tick.

    ``render.plan.picture_tick`` refuses a tick finer than
    :data:`~video_editor.render.plan.MAX_PICTURE_TICK`, which puts this stream on the
    picture chain's older, container-quantised form - the one where ``setpts``
    truncates every stamp. The picture is still decodable; what is no longer available
    is a stamp that can be tied to it.
    """
    from media_fixtures import identity_sequence

    directory = identity_sequence(tmp_path, count)
    out = tmp_path / name
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", "25",
         "-i", str(directory / "%04d.ppm"), "-map_metadata", "-1", "-an",
         "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", "-g", "5",
         "-video_track_timescale", str(timescale), str(out)],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[:400]
    return out


def test_a_stream_without_an_exact_tick_keeps_its_picture_and_loses_its_claim(tmp_path):
    """Honesty over a number: no exact tick, so no reported source stamp.

    Hand-worked for this composition (clip ``[1, 6)`` at tempo 1001/1000, 25 fps
    output, output frame 40 at 8/5 s): the sampling time is
    ``1 + 8/5 * 1001/1000 = 1626/625 = 2.6016 s``. The fixture's pictures sit at
    ``n/25``, and ``65/25 = 1625/625 <= 1626/625 < 66/25``, so the last picture at or
    before the sampling time is number **65**, at 2.6 s - which is also what the
    independent picture reference answers for that frame.
    The chain this stream is rendered through has no exact tick - ``setpts`` truncates
    its stamps - so the reported source position must be **None**. Measured on this
    window, ``setpts=PTS/1.001`` over a 1/50000000 tick puts the first six arrivals at
    419580, 2417582, 4415584, 6413586, 8411588 and 10409590 ticks where their exact
    positions are 419580.419580, 2417582.417582, 4415584.415584, 6413586.413586,
    8411588.411588 and 10409590.409590. Those stamps belong to the resampler's
    arithmetic, not to a picture, and presenting one as this picture's position would
    be a wrong timestamp.

    This test used to ask for tempo 25/24 on the same fixture, and then pass by
    reading the container tick alone. That shape is no longer the inexact one: for
    25/24 the tempo's division only needs the container tick expanded by 25, which is
    past the limit, but its pictures *are* on the container's own grid, so measuring
    the window proves an exact tick and the same still answers 66 at 66/25 (asserted
    in :func:`test_the_last_frame_of_a_fine_timescale_stream_reports_the_picture_it_holds`).
    The tempo here needs the container tick expanded 1001 * 1001 times even after the
    pictures are measured, so it is the shape where a picture survives without a claim.
    """
    from video_editor.render.plan import picture_preroll, picture_sampling

    source = make_fine_timescale_identity(tmp_path)
    facts = picture_facts(source)
    assert facts.count == 150 and facts.identities == tuple(range(150))
    assert facts.time_base == F(1, 50_000_000), (
        f"the fixture's container tick is what makes its exact tick impossible: "
        f"{facts.time_base}"
    )
    project = Project(output=OutputSettings(64, 48, FPS, SR, "stereo"))
    project = RegisterSourceCommand(source_from_probe(source, 0, "src")).apply(project)
    project = project.with_tracks((Track("vbot", TrackKind.VIDEO, "Bottom"),))
    project = AddClipCommand("src", 0, "vbot", F(0), source_in=F(1), source_out=F(6),
                             rate=F(1001, 1000), clip_id="slow").apply(project)
    # the precondition, measured rather than assumed: this item has no exact tick
    plan, frame_time, index = still_plan_for(project, F(40, 25))
    assert index == 40
    item = plan.picture_at(frame_time)
    sampling = picture_sampling(
        item.clip, item, FPS, time_base=facts.time_base,
        preroll=picture_preroll(item.clip, project.source("src", 0), FPS),
    )
    assert sampling.tick is None and sampling.phase is None, (
        f"this test needs the chain with no exact tick; the plan gives tick "
        f"{sampling.tick} at {facts.time_base}"
    )
    assert sampling.anchored, "the seek is still on the container's grid"
    assert sampling.sample_start == F(1626, 625), "hand-worked sampling time"
    # measuring the stream's own pictures does not rescue this tempo either
    window = picture_read_window(
        item.clip, item, FPS, facts.time_base,
        picture_preroll(item.clip, project.source("src", 0), FPS),
    )
    read = decoded_picture_times(source, 0, window.seek_source,
                                 window.generous_source_end)
    assert read.stamps, "the reading window must hold pictures, or this proves nothing"
    measured = picture_sampling(
        item.clip, item, FPS, time_base=facts.time_base,
        preroll=picture_preroll(item.clip, project.source("src", 0), FPS),
        window_pictures=read.stamps,
    )
    assert measured.tempo is None, (
        f"1001/1000 on a 1/50000000 container stays inexact even measured; the plan "
        f"found {measured.tempo}"
    )
    assert "1/50050000000" in measured.tick_reason, measured.tick_reason
    assert "expanded 1001 times" in measured.tick_reason, measured.tick_reason

    # the answer the fixture itself supports: the last picture at or before 1626/625
    expected = max(ordinal for ordinal, stamp in enumerate(facts.pictures)
                   if stamp <= F(1626, 625))
    assert (expected, facts.pictures[expected]) == (65, F(13, 5)), (
        f"hand-worked above; the fixture answers {expected} at "
        f"{facts.pictures[expected]}"
    )

    still = extract_composition_still(project, {"src": source}, F(40, 25))
    assert identity_at(still.rgb, still.width, still.height) == expected, (
        "the picture itself must still be the right one"
    )
    assert still.decoded_source_pts is None, (
        "a stamp taken upstream of fps is not this picture's position; the honest "
        "answer here is that the source frame was not measured"
    )
    assert not still.grid_exact, "an unmeasured position is never an exact one"
    # the prediction is kept, and is labelled as such by the interface
    assert still.source_pts == F(1626, 625)


def test_a_rate_changed_clip_reads_the_source_it_actually_uses(tmp_path):
    steps = make_colour_steps(tmp_path)
    project, paths = picture_only(steps)
    # 2 source seconds per output second, starting at source 4 s: output 0.5 s
    # sits at source 5 s -> the last block (cyan)
    project = AddClipCommand("steps", 0, "vbot", F(10), source_in=F(4), source_out=F(6),
                             rate=F(2), clip_id="fast").apply(project)
    still = extract_composition_still(project, paths, F(21, 2))
    assert still.clip_id == "fast"
    # the frame a playhead at output 10.5 s shows is index floor(10.5*25) = 262,
    # whose own time 262/25 s is 4 + (262/25 - 10)*2 = 124/25 s of the source
    assert still.frame_index == 262
    assert still.source_pts == F(124, 25)
    # 124/25 = 4.96 s is a real frame of this 25 fps stream (124 x 1/25), so the
    # reported source PTS is that frame itself, not the next one
    assert still.decoded_source_pts == F(124, 25)


def test_a_planned_time_between_real_frames_shows_the_frame_at_or_before_it(tmp_path):
    """On VFR material the plan can ask for a time with no frame at it.

    The composition's rule is the last real picture at or before the sampling time,
    so the picture shown - and the PTS the inspector reports - is the *earlier*
    frame, and it is read from this decode rather than inferred. Naming the planned
    time instead would label the source edge the user is looking at wrongly, exactly
    where two editions' cuts are being compared.
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
    assert still.decoded_source_pts == F(8333, 1000)  # the last real frame at/before it
    assert still.grid_exact  # the container's time base was read, so the grid is exact


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
    # floor(1.99999 * 25) = 49: the frame already covering the playhead, not the
    # one that starts just after it
    assert index == 49 and frame_time == F(49, 25)
    assert plan.total_frames == 1
    # the plan is that frame's own lattice interval: exactly what the exporter
    # would emit for [49/25, 50/25)
    assert (plan.output_start, plan.output_end) == (F(49, 25), F(50, 25))
    assert plan.picture_at(frame_time) is not None


# ------------------------------------------ what the playhead shows (pure, no decode)


def one_clip_project(out_end: Fraction) -> Project:
    """A composition ``[0, out_end)`` over a 25 fps stream; no media is read."""
    project = Project(output=OutputSettings(128, 96, FPS, SR, "stereo"))
    project = RegisterSourceCommand(
        StreamSource("steps", 0, TrackKind.VIDEO, F(0), F(60), frame_rate=FPS,
                     width=128, height=96, stream_position=0)
    ).apply(project)
    project = project.with_tracks((Track("vbot", TrackKind.VIDEO, "Bottom"),))
    return AddClipCommand("steps", 0, "vbot", F(0), source_in=F(0), source_out=out_end,
                          clip_id="only").apply(project)


def test_a_playhead_shows_the_frame_already_covering_it(tmp_path):
    """Inspection question, hand-worked at 25 fps: the still shows ``floor(t*25)``.

    0.039 s is still inside frame 0's picture (0.0 s until 0.04 s); 0.04 s is where
    frame 1 starts. The ceil *range boundary* would name frame 1 at 0.039 s, a
    picture from up to one frame in the future, so it is not used here.

    The plan for the inspected frame is that frame's own lattice interval:
    ``[index/25, (index+1)/25)`` is exactly the one-frame range the exporter emits
    frame ``index`` for, so the still is the frame the export writes at that index.
    """
    project, _paths = picture_only(tmp_path / "unused.mkv")
    for playhead, index in (
        (F(0), 0),
        (F(39, 1000), 0),  # floor(0.975)
        (F(4, 100), 1),  # floor(1.0): the next frame has started
        (F(1, 3), 8),  # floor(8.333): ceil would have shown frame 9
        (F(9999, 10000), 24),  # floor(24.9975)
    ):
        plan, frame_time, shown = still_plan_for(project, playhead)
        assert shown == index, f"playhead {float(playhead)}s shows frame {shown}"
        assert frame_time == F(index, 25)
        assert plan.total_frames == 1
        assert (plan.output_start, plan.output_end) == (F(index, 25), F(index + 1, 25))


def test_a_playhead_one_tick_before_the_end_shows_a_frame_that_exists(tmp_path):
    """The horizon question: a 0.08 s composition has frames 0 and 1, and that is all.

    Hand-worked: floor(0.079*25) = 1, and the export really writes two frames
    (ceil(0.08*25) = 2). The ceil boundary at 0.079 s is 2 - a frame the
    composition never writes - which is exactly what the inspection rule avoids.
    """
    project = one_clip_project(F(8, 100))
    assert project.output_end() == F(8, 100)
    assert build_plan(project, F(0), F(8, 100)).total_frames == 2

    plan, frame_time, shown = still_plan_for(project, F(79, 1000))
    assert shown == 1 and frame_time == F(1, 25)
    assert plan.total_frames == 1 and (plan.output_start, plan.output_end) == (F(1, 25), F(2, 25))
    assert not plan.picture_at(frame_time).is_black


def test_a_still_at_or_past_the_exact_end_is_refused_not_padded(tmp_path):
    """At the composition end there is no frame to show, so the answer is a refusal.

    Black there would present a frame the export never writes at that index; the
    message names the last frame the composition really has.
    """
    project = one_clip_project(F(8, 100))
    for playhead in (F(8, 100), F(8, 100) + F(1, 1000), F(6)):
        with pytest.raises(StillError) as refused:
            still_plan_for(project, playhead)
        message = str(refused.value)
        assert "no output frame at" in message
        assert "the composition ends at 0.080000s" in message
        assert "the last frame it has is at 0.040000s" in message


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
    assert [f.pts for f in frames] == list(picture_facts(vfr).pictures), (
        "measured on this fixture: the packet hints carry exactly the times the file "
        "decodes, which is why the frame index moved to decoded pictures for what a "
        "probe can *prove*, not for different numbers on a well-formed file"
    )
    gaps = [b.pts - a.pts for a, b in itertools.pairwise(frames)]
    assert all(g > 0 for g in gaps)
    assert gaps.count(F(1, 25)) == 200  # the constant-rate section is exact
    slow = [g for g in gaps if abs(g - F(1, 12)) < F(1, 500)]
    assert len(slow) == 47
    # the slow section is irregular at the container's own granularity: the steps
    # alternate rather than being one constant frame duration
    assert {g for g in slow} == {F(83, 1000), F(21, 250)}


def test_source_stepping_lands_on_real_neighbouring_frames(tmp_path):
    """Stepping uses the stream's own timestamps, never a nominal grid.

    Every value below is a picture this file actually decodes (checked against
    ``picture_facts``, whose frame list is cross-checked with a rawvideo decode),
    so a step landing between two of them would fail rather than look plausible.
    """
    vfr = make_vfr(tmp_path)
    decoded = set(picture_facts(vfr).pictures)
    index = FrameIndex(vfr, 0, window=F(2))

    # across the rate change: the real neighbour is 8.0 s, a 1/12 step from the
    # previous frame would have asked for 8.083 s
    assert index.at_or_before(F(8)) == F(8)
    assert index.step(F(8), -1) == F(199, 25)
    assert index.step(F(199, 25), +1) == F(8)

    # inside the slow section the nominal 25 fps grid has drifted off the real
    # frames: 8.4 s (one nominal step from 8.36) is not a frame at all, while
    # 8.333 and 8.417 are
    assert index.at_or_before(F(210, 25)) == F(8333, 1000)
    assert index.at_or_after(F(210, 25)) == F(8417, 1000)
    assert index.step(F(8333, 1000), +1) == F(8417, 1000)
    # and the edges of the stream are reported as edges, not wrapped around
    assert index.step(index.at_or_after(F(0)) or F(0), -1) is None
    assert index.at_or_after(F(11917, 1000)) == F(11917, 1000)
    assert index.at_or_after(F(11917, 1000) + F(1, 25)) is None
    for answer in (F(8), F(199, 25), F(8333, 1000), F(8417, 1000), F(11917, 1000)):
        assert answer in decoded, f"{answer} is not a picture this stream decodes"


def test_stepping_stays_bounded_on_a_long_source(tmp_path):
    """A window is probed once and reused, so navigation does not re-scan.

    The window is what keeps a one-hour source usable: without it, stepping one
    frame would read the whole packet list (measured: 90 000 timestamps in 0.56 s,
    against 0.12 s for a 20 s window).
    """
    vfr = make_vfr(tmp_path)
    index = FrameIndex(vfr, 0, window=F(2))
    before = index.at_or_before(F(9))
    after = index.at_or_after(F(9) + F(1, 12))
    # measured stamps of this fixture around 9 s: 8.75, 8.833, 8.917, 9.0, 9.083,
    # 9.167, 9.25. Two things follow, and both are the point of the assertion.
    # The slow section is 12 fps, so 9.0 is itself a real frame. And the frame a
    # nominal 1/12 step from 8.75 "should" be at 9.0833 is carried by this
    # millisecond container at 9.083 - so a query at exactly 9 + 1/12 is *past*
    # it, and the answer is 9.167. Nominal arithmetic lands off real frames.
    assert before == F(9) and after == F(9167, 1000)
    assert index.probes == 1, "the second query re-used the first window"
    assert len(index.cached_spans()) == 1
    # a jump far away costs one more window, not a scan of the file
    index.at_or_before(F(1))
    assert index.probes == 2
    assert all(end - start == F(2) for start, end in index.cached_spans())
    index.forget()
    assert index.cached_spans() == ()


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
    index = FrameIndex(offset, 0, window=F(2))
    # the grid of this file is shifted by its own start: 3.02 s is a frame, 3.0 s
    # is not, and the neighbours of 3.02 are 2.98 and 3.06
    assert index.at_or_before(F(3) + F(1, 50)) == F(151, 50)  # 3.02 is a frame itself
    assert index.at_or_after(F(3) + F(1, 50)) == F(151, 50)
    assert index.step(F(3) + F(1, 50), -1) == F(149, 50)  # strict neighbours
    assert index.step(F(3) + F(1, 50), +1) == F(153, 50)
    assert index.at_or_before(F(3)) == F(149, 50)  # 2.98 is the frame before 3.0
    assert index.step(F(5, 2), +1) == F(127, 50)  # 2.54 s
    assert index.step(F(5, 2), -1) is None  # 2.5 s is this stream's first picture


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
    # a playhead 0.05 s in shows frame floor(1.25) = 1, which reads 2.54 s
    # asset-local: a real frame of this file, whose grid starts at 2.5 s
    still = extract_composition_still(project, {"offset": offset}, F(5, 100))
    assert not still.black and still.clip_id == "late"
    assert still.source_pts == F(127, 50)  # 2.54 s asset-local, a real frame here
    assert still.decoded_source_pts == F(127, 50)
    red, green, blue = centre(still)
    assert blue > red + 40 and blue > green + 40  # the fixture is solid blue


def test_a_non_video_stream_is_refused(tmp_path):
    tone = make_tone(tmp_path, "tone.wav", 660)
    with pytest.raises(StillError, match=r"not present|not video"):
        source_frame_times(tone, 0)


# ------------------------------------------------- the decoded picture probe
#
# What ``media.frameprobe`` feeds the frame index. The measured facts these tests
# pin, on FFmpeg 8.1.3: a bounded read seeks to the keyframe at or before its
# start, so pictures before the window come back; it stops short of the end it was
# asked for when the stream has reorder distance, leaving a picture or two of the
# window unreported; asked past the material it answers with the tail rather than
# with nothing, and asked inside a break it may answer with the first picture after
# it; a window lying wholly before the first picture reports no record at all; and
# ``-show_frames`` no longer prints a per-frame ``time_base``, so the integer stamps
# have to be multiplied by the **stream** time base.


def container_claim(path: Path) -> tuple[F | None, F | None]:
    """The container's own start and duration, read straight from ffprobe."""
    proc = subprocess.run(
        [FFPROBE, "-hide_banner", "-v", "error", "-show_entries",
         "format=start_time,duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[:300]
    fields = json.loads(proc.stdout)["format"]

    def exact(value: object) -> F | None:
        return None if value in (None, "N/A") else F(str(value))

    return exact(fields.get("start_time")), exact(fields.get("duration"))


def test_a_bounded_read_reports_the_pictures_and_the_ground_it_covered(tmp_path):
    """Preroll from the seek is kept, and is what the read can be trusted about."""
    steps = make_colour_steps(tmp_path)
    facts = picture_facts(steps)
    start, end = F(11, 5), F(21, 5)  # 2.2 s .. 4.2 s
    window = decoded_picture_times(steps, 0, start, end)
    covered = [pts for pts in facts.pictures if F(2) <= pts < end]
    assert window.stamps == tuple(covered), (
        f"the read returned {window.decoded} pictures, the fixture decodes "
        f"{len(covered)} from its 2.0 s keyframe to {float(end)}s"
    )
    assert window.started_earlier, "the seek landed on the keyframe at 2.0 s"
    assert window.inside == tuple(p for p in facts.pictures if start <= p < end)
    assert not window.read_past, (
        "measured on this fixture this read stopped inside its window; the loss is at "
        "the end, where it stops short - see the B-frame test below"
    )
    assert window.proven_from == F(2), "the preroll from the 2.0 s keyframe counts as read"
    assert window.enumerated(None) == (F(2), F(104, 25)), (
        "trusted up to the last picture it decoded (4.16 s), not up to the 4.2 s it "
        "was asked for: measured behaviour is that a read stops short of its end"
    )
    assert window.time_base == stream_timing(steps, 0).time_base
    assert window.reported >= window.decoded


def test_a_read_past_the_end_answers_with_the_tail_not_with_nothing(tmp_path):
    """Measured, for frames as for packets: 20%40 of a 6 s clip returns its tail.

    Keeping those stamps unfiltered is what let an earlier index claim there were
    pictures at 20 s; dropping them would hide that the window was empty because
    the material stopped. They are kept, and what the read is allowed to claim is
    that its *window* holds no picture.
    """
    steps = make_colour_steps(tmp_path)
    facts = picture_facts(steps)
    window = decoded_picture_times(steps, 0, F(20), F(40))
    assert window.stamps == facts.pictures[-25:], "the last 25 pictures come back"
    assert window.inside == () and window.tail_only
    assert window.proven_from == facts.pictures[-25] == F(5)
    assert facts.pictures[-1] == F(149, 25), "measured last picture of this clip"
    assert window.enumerated(None) == (F(5), F(149, 25)), (
        "a read that only found pictures before its window is trusted as far as its "
        "last picture and no farther: measured behaviour at a long presentation "
        "break is the same answer, so it cannot claim the stream ended"
    )
    assert window.enumerated(F(6)) == (F(5), F(40)), (
        "asked past the end this container claims for itself, the same read may "
        "enumerate its whole window"
    )


def test_a_window_before_the_first_picture_proves_that_span_empty(tmp_path):
    """A file whose presentation starts at 2.5 s really has nothing before it."""
    offset = make_whole_asset_offset(tmp_path)
    window = decoded_picture_times(offset, 0, F(0), F(2))
    assert window.decoded == 0, "ffprobe produced no picture records for that span"
    assert window.inside == () and not window.tail_only
    assert window.proven_from == F(0)
    assert window.enumerated(None) == (F(0), F(2)), (
        "no record at all is proof the window held no picture: measured, a read that "
        "reaches real pictures reports them even when they lie outside its window"
    )


def test_the_probe_counts_the_pictures_the_stream_holds(tmp_path):
    """``-show_frames`` gives frames, and the count is checked, not assumed.

    ``picture_facts`` only accepts a fixture whose ffprobe frame count matches a
    ``rawvideo`` decode with ``-fps_mode passthrough`` — without that flag FFmpeg
    duplicates frames up to the muxer's nominal rate, measured as 204 pictures out
    of a 150-picture VFR fixture — so this compares the probe with a decode rather
    than with another probe.
    """
    vfr = make_vfr(tmp_path)
    facts = picture_facts(vfr)
    window = decoded_picture_times(vfr, 0, F(0), F(13))
    assert window.decoded == facts.count == 248, (
        f"probe saw {window.decoded}, the file decodes {facts.count}"
    )
    assert window.stamps == facts.pictures
    assert len(facts.spacings) > 1, "the fixture must stay variable-rate"


def test_stamps_survive_a_container_whose_tick_is_not_a_frame(tmp_path):
    """MP4 at ``time_base 1/12800``: integer ticks times the stream base, exactly.

    Measured here: the read for 1.0-2.0 s reports its first picture at 0.92 s, not
    at the 0.8 s keyframe it sought — with B-frames the earliest *presentation*
    time in a read is later than the seek point's keyframe — and it reports nothing
    at or after 2.0 s. Every stamp is a whole number of 1/12800 ticks.
    """
    source = make_identity_bframes(tmp_path)
    facts = picture_facts(source)
    assert facts.time_base == F(1, 12800), facts.time_base
    assert facts.of_type("B") > 0, "the fixture needs real B-frames for this to mean anything"
    window = decoded_picture_times(source, 0, F(1), F(2))
    inside_truth = tuple(p for p in facts.pictures if F(1) <= p < F(2))
    assert window.inside == inside_truth[:-1], (
        f"measured: with B-frames the read stops short of the end it was asked for "
        f"and never reports the picture at {inside_truth[-1]} - which is why a span "
        f"is only trusted as far as the last picture decoded"
    )
    assert set(window.stamps) <= set(facts.pictures), "every stamp is a picture of this file"
    assert window.started_earlier and window.stamps[0] == F(23, 25), (
        f"measured first picture of this read: {window.stamps[0]}"
    )
    assert not window.read_past and window.stamps[-1] < F(2)
    assert window.enumerated(None) == (F(23, 25), F(48, 25)), (
        f"the span this read proved: {window.stamps[0]} to {window.stamps[-1]}"
    )
    assert list(window.stamps) == sorted(set(window.stamps))
    for pts in window.stamps:
        ticks = pts / facts.time_base
        assert ticks == int(ticks), f"{pts} left the container's tick grid"


def test_record_parsing_sorts_deduplicates_and_skips_the_unaddressable():
    """The stamp rule, driven by fabricated ``-show_frames`` records.

    Nothing guarantees that records arrive in presentation order — this is the
    B-frame case, a ``1/12800`` MP4's pictures listed as the decoder produced them.
    A duplicated timestamp would make the index step past a picture, a record with
    no usable timestamp cannot be addressed at all, and a non-picture record is
    not a frame.
    """
    base = F(1, 12800)
    decode_order = [
        {"media_type": "video", "best_effort_timestamp": 25600},  # 2.0 s
        {"media_type": "video", "best_effort_timestamp": 12800},  # 1.0 s (a B-frame)
        {"media_type": "video", "best_effort_timestamp": 19200},  # 1.5 s
        {"media_type": "video", "best_effort_timestamp": 12800},  # the same picture again
        {"media_type": "video", "best_effort_timestamp": "N/A", "pts": 6400},  # pts fallback
        {"media_type": "subtitle", "best_effort_timestamp": 3200},  # not a picture
        {"media_type": "video", "best_effort_timestamp": "N/A", "pts": "N/A"},
        {"media_type": "video"},
        "not a record at all",
    ]
    assert picture_stamps(decode_order, base) == (F(1, 2), F(1), F(3, 2), F(2))
    assert picture_stamps(decode_order, F(0)) == ()
    assert picture_stamps([], base) == ()


def test_the_declared_end_is_the_container_s_claim_taken_loosely(tmp_path):
    """What the file says about its own length, recorded as a claim, never a bound.

    The container's duration means different things by family (a Matroska duration
    is an end on the asset axis, for others a span from the stream's own start), so
    the later of the two readings is taken: that can only ever make a "no picture
    that way" answer harder to claim, never easier.
    """
    steps = make_colour_steps(tmp_path)
    container_start, duration = container_claim(steps)
    assert (container_start, duration) == (F(0), F(6))
    claimed = declared_end(steps, stream_start=F(0))
    assert claimed.end == F(6), claimed.detail

    offset = make_whole_asset_offset(tmp_path)
    container_start, duration = container_claim(offset)
    timing = stream_timing(offset, 0)
    claimed = declared_end(offset, timing.start_time)
    assert claimed.end == max(container_start, timing.start_time) + duration

    truncated = make_truncated_identity_offset(tmp_path)
    facts = picture_facts(truncated)
    claimed = declared_end(truncated, facts.stream_start or F(0))
    assert claimed.end is not None and claimed.end > facts.pictures[-1], (
        "this file's own claim still overstates its material, which is exactly why "
        "a claim may not be used as a measurement"
    )


def test_the_probe_refuses_a_window_it_could_not_mean(tmp_path):
    """No unbounded form, no backwards window, no pretending about a junk file."""
    vfr = make_vfr(tmp_path)
    with pytest.raises(FrameProbeError, match="empty"):
        decoded_picture_times(vfr, 0, F(2), F(2))
    with pytest.raises(FrameProbeError, match="before the asset axis"):
        decoded_picture_times(vfr, 0, F(-1), F(1))
    garbage = make_garbage(tmp_path)
    # the metadata probe refuses before the picture read is even attempted
    with pytest.raises(StillError, match="cannot probe"):
        decoded_picture_times(garbage, 0, F(0), F(1))


# ------------------------------------------- source picture with its own PTS
#
# ``extract_source_still`` inspects the **source stream**: one accurate input
# seek, exactly one decoded picture behind a ``showinfo`` tap run with
# ``-copyts`` (so the stamp is the picture's own container timestamp), then only
# the composition's display geometry. Nothing here is compared against what the
# planner would have chosen - the checks are against ``picture_facts``, the
# independently decoded PTS/identity mapping of the fixture file (its PTS list
# cross-checked with a rawvideo decode by the fixture helper itself).


def identity_project(
    path: Path, rate: Fraction | None = None, clip_id: str = "c",
    source_in: Fraction | None = None, source_out: Fraction | None = None,
    size: tuple[int, int] = (64, 48),
) -> tuple[Project, dict[str, Path]]:
    """One clip over a real identity stream, the stream registered by probing it.

    The slice defaults to the whole stream because source stepping is about moving
    the cut, not about the cut that happens to exist; individual tests narrow it.
    """
    project = Project(output=OutputSettings(size[0], size[1], FPS, SR, "stereo"))
    project = RegisterSourceCommand(source_from_probe(path, 0, "src")).apply(project)
    project = project.with_tracks((Track("v1", TrackKind.VIDEO, "V1"),))
    source = project.source("src", 0)
    project = AddClipCommand(
        "src", 0, "v1", F(0),
        source_in=source.origin if source_in is None else source_in,
        source_out=source.end if source_out is None else source_out,
        rate=F(1) if rate is None else rate, clip_id=clip_id,
    ).apply(project)
    return project, {"src": path}


def source_case(still, facts, requested, want_index):
    """One source still, cross-checked against the fixture's own decoded mapping.

    Every fact here comes from the fixture measurement, never from the still path:
    the PTS must be the picture the mapping says the request selects, the identity
    number the still carries must be that picture's number, and its centre pixel
    must be the very bytes the fixture's decode produced for it.
    """
    assert still.decoded_source_pts == facts.pictures[want_index], (
        f"request {float(requested)}s: still reports {still.decoded_source_pts}, the "
        f"decoded mapping puts the chosen picture at {facts.pictures[want_index]}"
    )
    assert identity_at(still.rgb, still.width, still.height) == facts.identities[want_index]
    assert centre(still) == facts.centres[want_index], (
        f"request {float(requested)}s: still centre {centre(still)} is not the fixture's "
        f"decoded centre {facts.centres[want_index]} for picture {want_index}"
    )
    assert still.source_pts == requested, "the requested time is reported as requested"
    assert not still.black and still.source_only and still.clip_id == "c"
    assert still.asset_id == "src" and still.stream_index == 0


def test_a_bframes_source_picture_carries_its_own_measured_pts(tmp_path):
    """Exact PTS requests on real B-frames: picture, identity and PTS all match.

    The fixture measures 22 I + 40 B + 38 P, so decode order and presentation
    order disagree; every probe below is an exact picture timestamp of the file.
    ``grid_exact`` True means the reported position was validated against the
    container's own tick grid (1/12800 here), not just printed.
    """
    source = make_identity_bframes(tmp_path, count=100)
    facts = picture_facts(source)
    assert facts.of_type("B") > 0, f"census must stay real: {facts.pict_types}"
    assert facts.time_base == F(1, 12800), facts.time_base
    project, paths = identity_project(source)
    for index in (0, 1, 37, 47, 77, 98, 99):  # first, neighbours, a middle, the last
        requested = facts.pictures[index]
        still = extract_source_still(project, paths, "c", requested, F(0))
        source_case(still, facts, requested, index)
        assert still.grid_exact, "the timestamp was reconstructed on the tick grid"
        assert still.decoded_source_pts == requested, "an exact request returns that frame"


def test_a_source_request_between_two_bframes_reports_the_actual_picture(tmp_path):
    """In-between requests: the reported PTS is the frame that actually decoded.

    Three shapes of in-between, each expected from the fixture's decoded list by
    ``bisect`` (the first real picture at or after the request), never from the
    code under test: a plain midpoint, a hair above an exact frame time (one half
    tick up, where the container's tick grid - not the decimal text - decides what
    survives the seek), and a midpoint before the last frame.
    """
    source = make_identity_bframes(tmp_path, count=100)
    facts = picture_facts(source)
    tick = facts.time_base
    project, paths = identity_project(source)
    requests = [
        (facts.pictures[24] + facts.pictures[25]) / 2,  # between 0.96 and 1.0
        facts.pictures[25] + tick / 2,  # half a tick past a real frame's timestamp
        facts.pictures[98] + (facts.pictures[99] - facts.pictures[98]) / 2,
    ]
    for requested in requests:
        want = bisect_left(facts.pictures, requested)
        assert want < facts.count, f"{requested} is past the last picture"
        assert facts.pictures[want] >= requested
        still = extract_source_still(project, paths, "c", requested, F(0))
        source_case(still, facts, requested, want)
        assert still.decoded_source_pts != requested, (
            "an in-between request must report the frame it actually decoded"
        )


def test_an_offset_streams_source_picture_is_asset_local(tmp_path):
    """Nonzero origin: the seek is on the file's content axis, the PTS is not.

    ``make_identity_offset`` presents 5/2 .. 13/2 (first picture 5/2, last 323/50,
    container start 2.5). A request of 151/50 s must report 151/50 s asset-local -
    not a file-axis number - and the frame it decoded must be the 13th picture of
    the file. The decimal-seek case (3.0 s is not a frame here) exercises the same
    axis with a rounded seek text.
    """
    source = make_identity_offset(tmp_path, count=100)
    facts = picture_facts(source)
    assert facts.pictures[0] == F(5, 2) and facts.last == F(323, 50), facts.pictures[:2]
    project, paths = identity_project(source)
    exact = [(facts.pictures[0], 0), (F(151, 50), 13), (F(127, 50), 1), (F(323, 50), 99)]
    for requested, index in exact:
        still = extract_source_still(project, paths, "c", requested, F(0))
        source_case(still, facts, requested, index)
        assert still.decoded_source_pts == requested
    for requested in (F(3), F(645, 100)):  # in-between: 3.02 and 6.46 are the answers
        want = bisect_left(facts.pictures, requested)
        still = extract_source_still(project, paths, "c", requested, F(0))
        source_case(still, facts, requested, want)


def test_in_between_requests_on_a_vfr_stream_lands_on_the_next_real_frame(tmp_path):
    """Irregular timestamps: no nominal grid is consulted anywhere.

    The slow section steps by 83/1000 and 21/250 on a 1/1000 tick: a request of
    4.05 s has no frame near it on any uniform grid, and the honest answer is the
    first picture this file really decodes at or after it (4.083 s).
    """
    source = make_identity_vfr(tmp_path)
    facts = picture_facts(source)
    assert len(facts.spacings) > 1, "the fixture must stay variable-rate"
    project, paths = identity_project(source)
    for requested in (F(4), F(405, 100), F(2001, 500), F(8083, 1000)):
        want = bisect_left(facts.pictures, requested)
        still = extract_source_still(project, paths, "c", requested, F(0))
        source_case(still, facts, requested, want)
    assert extract_source_still(project, paths, "c", F(405, 100), F(0)).decoded_source_pts \
        == F(4083, 1000)


def test_the_same_source_pts_decodes_the_same_picture_at_every_clip_rate(tmp_path):
    """Clip rate is irrelevant to a source picture: same PTS, same bytes.

    Four clips over the same slice at 1, 25/24, 4 and 1/4 source-seconds per
    output second; asking each for the same asset-local time must return one and
    the same decoded picture - the earlier path sampled through the clip's tempo
    and the output grid, which made the answer depend on both.
    """
    source = make_identity_bframes(tmp_path, count=100)
    facts = picture_facts(source)
    requested = facts.pictures[47]
    reference: bytes | None = None
    for rate in (F(1), F(25, 24), F(4), F(1, 4)):
        project, paths = identity_project(source, rate=rate)
        still = extract_source_still(project, paths, "c", requested, F(1, 2))
        source_case(still, facts, requested, 47)
        if reference is None:
            reference = still.rgb
        assert still.rgb == reference, f"rate {rate} decoded a different picture"
        assert still.decoded_source_pts == requested
        assert still.width == 64 and still.height == 48, "the display box is the project's"


def test_source_inspection_reaches_past_the_clip_edges_but_not_past_the_stream(tmp_path):
    """The clip's in/out do not bound a source step; the stream's bounds do.

    The clip slices [5/2, 3) of the offset fixture; the point of stepping is to
    move those edges, so 6.4 s - inside the stream's material, 3.4 s past the
    clip's out-point - must decode (it is picture 98, at 321/50 s). 2.4 s and the
    recorded end itself are refused by name, and a point inside the recorded
    bounds but past the decodable material refuses too, naming what ran out -
    never a black substitute.
    """
    source = make_identity_offset(tmp_path, count=100)
    facts = picture_facts(source)
    project, paths = identity_project(source, source_in=F(5, 2), source_out=F(3))
    still = extract_source_still(project, paths, "c", F(32, 5), F(0))
    assert still.decoded_source_pts == F(321, 50) == facts.pictures[98]
    assert identity_at(still.rgb, still.width, still.height) == 98
    for outside in (F(12, 5), F(13, 2)):  # before the origin, at the recorded end
        with pytest.raises(StillError, match="is outside asset"):
            extract_source_still(project, paths, "c", outside, F(0))
    # inside the recorded bounds, past every picture the file decodes: refused
    with pytest.raises(StillError, match="holds no picture at or after") as past:
        extract_source_still(project, paths, "c", F(649, 100), F(0))
    assert "6.490000s" in str(past.value)


def test_a_replaced_source_file_is_refused_for_source_pictures_too(tmp_path):
    """The fs/identity check guards this path exactly as it guards the others."""
    source = make_identity_bframes(tmp_path, count=100)
    project, paths = identity_project(source)
    recorded = Identity.of(source)
    substitute = make_identity_bframes(tmp_path, name="other.mp4", count=100)
    source.write_bytes(substitute.read_bytes())  # a valid file, but not the recorded one
    with pytest.raises(StillError, match="not the file recorded here"):
        extract_source_still(project, paths, "c", F(1), F(0), asset_identities={"src": recorded})


def test_an_unreadable_tap_makes_the_position_unknown_never_a_guess(tmp_path, monkeypatch):
    """If the timestamp cannot be parsed, the picture is still returned - honestly.

    The decode is faked at the subprocess boundary: valid one-frame pixels, but a
    stderr whose tap either says nothing at all, prints a stamp that cannot be
    read, or never states a time base. In every case the picture and its geometry
    are real, while ``decoded_source_pts`` refuses to invent a position and
    ``grid_exact`` says the position is not guaranteed - which is what the viewer
    shows and what blocks a cut being set from it.
    """
    source = make_identity_bframes(tmp_path, count=100)
    project, paths = identity_project(source)
    facts = picture_facts(source)
    pixels = b"\x11" * (64 * 48 * 3)
    junk_taps = {
        "no showinfo at all": "size=N/A time=00:00:01.00 bitrate=N/A speed=1x\n",
        "stamp unreadable": (
            "[Parsed_showinfo_0 @ 0x7f0] config in time_base: 1/12800, frame_rate: 25/1\n"
            "[Parsed_showinfo_0 @ 0x7f0] n:   0 pts:  N/A pts_time:N/A  duration:   512\n"
        ),
        "no time base": (
            "[Parsed_showinfo_0 @ 0x7f0] n:   0 pts:  12800 pts_time:1  duration:   512\n"
        ),
    }
    for label, stderr_text in junk_taps.items():
        real_run = subprocess.run

        def fake_run(args, *args_, _stderr=stderr_text, _real=real_run, **kwargs):
            if "ffprobe" in str(args[0]):  # the metadata probe stays real
                return _real(args, *args_, **kwargs)
            return subprocess.CompletedProcess(
                args, 0, stdout=pixels, stderr=_stderr.encode()
            )

        monkeypatch.setattr("video_editor.media.framegrab.subprocess.run", fake_run)
        still = extract_source_still(project, paths, "c", facts.pictures[25], F(0))
        assert still.rgb == pixels, f"{label}: the decoded picture should still come back"
        assert still.width == 64 and still.height == 48
        assert still.decoded_source_pts is None, f"{label}: position must be reported unknown"
        assert not still.grid_exact, f"{label}: an unmeasured position is never exact"
        assert still.source_only and still.source_pts == facts.pictures[25]


def test_the_normal_fixture_returns_a_verified_pts_and_matching_pixels(tmp_path):
    """The positive half of the same contract, on the un-faked decode.

    Same request as the crafted-input test above: here the tap parses, the PTS is
    verified on the container grid, and the pixels are the fixture's own decoded
    centre for that picture.
    """
    source = make_identity_bframes(tmp_path, count=100)
    facts = picture_facts(source)
    project, paths = identity_project(source)
    still = extract_source_still(project, paths, "c", facts.pictures[25], F(0))
    assert still.decoded_source_pts == facts.pictures[25]
    assert still.grid_exact
    assert centre(still) == facts.centres[25]


def test_source_still_output_time_labels_the_playhead_without_a_composition_plan(tmp_path):
    """The labels stay honest while the picture ignores the composition entirely.

    ``frame_index``/``output_time`` name the output frame under the playhead
    (floor at the project's frame rate) even when the playhead is outside the
    composition's horizon - a source picture does not depend on the composition
    reaching that far, so the labels must not either. The picture itself is the
    stream's, identical at either label.
    """
    source = make_identity_bframes(tmp_path, count=100)
    facts = picture_facts(source)
    project, paths = identity_project(source)
    requested = facts.pictures[47]
    here = extract_source_still(project, paths, "c", requested, F(3, 10))
    assert here.frame_index == 7 and here.output_time == F(7, 25)  # floor(0.3 * 25)
    away = extract_source_still(project, paths, "c", requested, F(60))  # past any horizon
    assert away.frame_index == 1500 and away.output_time == F(60)
    assert away.rgb == here.rgb and away.decoded_source_pts == here.decoded_source_pts
    with pytest.raises(StillError, match="non-negative"):
        extract_source_still(project, paths, "c", requested, F(-1))


def test_the_last_frame_of_a_fine_timescale_stream_reports_the_picture_it_holds(tmp_path):
    """The held last picture survives a container that counts in twenty nanoseconds.

    Same fixture as the test above, tempo 1000/1001 instead of 25/24, and the
    composition's **last** frame: clip ``[1, 6)`` at 1000/1001 on 25 fps output is
    ``1001/200`` s of output, ``ceil(5.005 * 25) = 126`` frames, and frame 125 samples
    ``1 + 125 * 40/1001 = 6001/1001`` s = 5.995005 s. The stream's last picture is
    149 at 5.96 s and its declared end (6 s) holds nothing, so that frame is a hold of
    picture 149.

    This used to raise ``StillError`` ("Output file is empty"): with no exact tick the
    chain placed its grid 0.876 of an output frame past the last picture it decoded,
    which is past the reach measured for that chain, and it wrote no picture at all.
    The exact tempo is now proved from the stamps in the reading window, so the grid
    starts at the plan's sampling time and answers with picture 149.
    """
    source = make_fine_timescale_identity(tmp_path)
    facts = picture_facts(source)
    assert facts.time_base == F(1, 50_000_000) and facts.last == F(149, 25)
    project = Project(output=OutputSettings(64, 48, FPS, SR, "stereo"))
    project = RegisterSourceCommand(source_from_probe(source, 0, "src")).apply(project)
    project = project.with_tracks((Track("vbot", TrackKind.VIDEO, "Bottom"),))
    project = AddClipCommand("src", 0, "vbot", F(0), source_in=F(1), source_out=F(6),
                             rate=F(1000, 1001), clip_id="speed").apply(project)
    assert project.output_end() == F(1001, 200)

    for index, picture in ((125, 149), (124, 148), (123, 147)):
        still = extract_composition_still(project, {"src": source}, F(index) / FPS)
        assert identity_at(still.rgb, still.width, still.height) == picture, (
            f"output frame {index} samples {F(1) + F(40 * index, 1001)} s and must hold "
            f"picture {picture}"
        )
        ordinal = facts.identities.index(picture)
        assert still.decoded_source_pts == facts.pictures[ordinal] == F(picture, 25), (
            f"the reported stamp must be picture {picture}'s own measured timestamp "
            f"({facts.pictures[ordinal]}), not {still.decoded_source_pts}"
        )
        assert still.grid_exact
        # the plan's prediction stays beside it, unchanged by what was measured
        assert still.source_pts == F(1) + F(40 * index, 1001)


def test_a_slice_the_stream_leaves_empty_is_refused_with_its_reason(tmp_path):
    """A slice inside the declared duration but past the last picture: named, not empty.

    Measured fixture: 150 pictures, the last at ``149/25`` = 5.96 s, declared duration
    6 s. A clip of ``[5.98, 6)`` is inside what the container claims and holds nothing
    that decodes. Every chain - exact tick or not - gets no input frame, so it can only
    write nothing; the answer is the reason, naming the window and the stream.
    """
    source = make_fine_timescale_identity(tmp_path)
    facts = picture_facts(source)
    assert facts.last == F(149, 25) and facts.container_duration == F(6), (
        f"the refusal depends on the declared end overshooting the last picture: "
        f"{facts.last} against {facts.container_duration}"
    )
    project = Project(output=OutputSettings(64, 48, FPS, SR, "stereo"))
    project = RegisterSourceCommand(source_from_probe(source, 0, "src")).apply(project)
    project = project.with_tracks((Track("vbot", TrackKind.VIDEO, "Bottom"),))
    project = AddClipCommand("src", 0, "vbot", F(0), source_in=F("5.98"), source_out=F(6),
                             rate=F(1000, 1001), clip_id="tail").apply(project)

    with pytest.raises(StillError) as caught:
        extract_composition_still(project, {"src": source}, F(0))
    message = str(caught.value)
    assert "holds no picture of that stream at all" in message, message
    assert "[5.980000, 6.000000)" in message, message
