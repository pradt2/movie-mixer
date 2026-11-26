"""Every picture path refuses what this build cannot draw — and nothing else does.

The renderer ends its picture chain at the delivery pixel format (``format=yuv420p``
for the MP4 preset) and performs no colour, tone or field-order conversion. So picture
whose format the file *states* as interlaced or HDR has to be refused before media is
produced, in the same words wherever it is met: export, chunked export, preview and the
composition still frame. Picture whose format the file does not state is warned about
and rendered — a file not saying something is not proof of what it is.

Nothing here mocks the probe. Each test builds a real file whose picture format was
first measured with raw ``ffprobe`` (the measurements are recorded in
``tests/test_probe_capability.py``), then runs the real path against it. Every refusal
case is paired with a case proving that a file this render never looks at cannot cause
one: a source in the bin, on a disabled track, under another track, outside the range,
or used only for its sound.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from fractions import Fraction as F
from pathlib import Path

import pytest

from media_fixtures import (
    HAS_FFMPEG,
    HAS_HEVC,
    HAS_MPEG2,
    make_container_flagged_progressive,
    make_field_coded_h264,
    make_garbage,
    make_hdr10,
    make_hdr_metadata_h265,
    make_hlg,
    make_identity_video,
    make_interlaced_mpeg2,
    make_progressive_sdr,
    make_sdr_10bit,
    make_untagged_avi,
    picture_facts,
    source_from_probe,
)
from video_editor.core.commands import AddClipCommand, RegisterSourceCommand
from video_editor.core.model import OutputSettings, Project, StreamSource, Track, TrackKind
from video_editor.media.capability import check_picture_capabilities, clear_probe_cache
from video_editor.media.framegrab import (
    StillError,
    extract_composition_still,
    extract_source_still,
)
from video_editor.render.exporter import RenderError, RenderRequest, render
from video_editor.render.plan import build_plan
from video_editor.render.preview import PreviewCache, PreviewError, prepare_preview

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")

FPS = F(25)
SIZE = (64, 48)


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    clear_probe_cache()
    yield
    clear_probe_cache()


# --------------------------------------------------------------------- projects


def place(project: Project, path: Path, asset_id: str, track_id: str, clip_id: str,
          at: F = F(0), stream_index: int = 0, source_in: F | None = None,
          source_out: F | None = None) -> Project:
    """Register one real stream and put a clip of it on the timeline."""
    source = source_from_probe(path, stream_index, asset_id=asset_id)
    project = RegisterSourceCommand(source).apply(project)
    return AddClipCommand(asset_id, stream_index, track_id, at, source_in=source_in,
                          source_out=source_out, clip_id=clip_id).apply(project)


def one_clip_project(path: Path, asset_id: str = "src", track_id: str = "v1",
                     tracks: tuple[Track, ...] | None = None) -> tuple[Project, dict[str, Path]]:
    project = Project(output=OutputSettings(width=SIZE[0], height=SIZE[1], frame_rate=FPS))
    project = project.with_tracks(tracks or (Track(track_id, TrackKind.VIDEO, "V1"),))
    project = place(project, path, asset_id, track_id, "c1")
    return project, {asset_id: path}


# ------------------------------------------------------------------- refusals

#: One row per shape a file can *state* that this renderer cannot draw. The expected
#: words are the refusal's own: the reason, and the value that made it so.
REFUSING = [
    pytest.param(make_field_coded_h264, "field_coded.mkv",
                 ["is interlaced", "report interlaced frames", "no deinterlacer"],
                 id="field-coded-h264"),
    pytest.param(make_interlaced_mpeg2, "interlaced.ts",
                 ["is interlaced", "field_order 'tt'", "no deinterlacer"],
                 id="interlaced-mpeg2",
                 marks=pytest.mark.skipif(not HAS_MPEG2, reason="this FFmpeg has no mpeg2video")),
    pytest.param(make_hdr10, "hdr10.mkv",
                 ["has an HDR colour transfer", "smpte2084", "no colour-space or tone conversion"],
                 id="hdr10-pq"),
    pytest.param(make_hlg, "hlg.mkv", ["has an HDR colour transfer", "arib-std-b67"],
                 id="hlg"),
    pytest.param(make_hdr_metadata_h265, "hdr_metadata.mkv",
                 ["carries HDR metadata", "Content light level metadata"],
                 id="hdr-static-metadata",
                 marks=pytest.mark.skipif(not HAS_HEVC, reason="this FFmpeg has no libx265")),
]


@pytest.mark.parametrize("build,name,phrases", REFUSING)
def test_export_refuses_and_says_why(tmp_path, build, name, phrases):
    """A render of refused picture produces no file and an explanation, not media."""
    if not HAS_MPEG2:
        pytest.skip("this FFmpeg has no mpeg2video")
    path = build(tmp_path, name)
    project, paths = one_clip_project(path)
    destination = tmp_path / "out.mp4"

    with pytest.raises(RenderError) as caught:
        render(RenderRequest(project=project, asset_paths=paths, destination=destination))
    message = str(caught.value)
    for phrase in phrases:
        assert phrase in message, message
    assert "then render again" in message
    assert not destination.exists()
    assert list(tmp_path.glob("*.partial.*")) == []


@pytest.mark.parametrize("build,name,phrases", REFUSING)
def test_a_chunked_render_refuses_before_any_chunk(tmp_path, build, name, phrases):
    """Chunking must not get past the same gate: one item per graph here."""
    if not HAS_MPEG2:
        pytest.skip("this FFmpeg has no mpeg2video")
    path = build(tmp_path, name)
    project, paths = one_clip_project(path)
    destination = tmp_path / "chunked.mp4"

    with pytest.raises(RenderError) as caught:
        render(RenderRequest(project=project, asset_paths=paths, destination=destination,
                             chunk_item_limit=1))
    for phrase in phrases:
        assert phrase in str(caught.value)
    assert not destination.exists()
    assert list(tmp_path.glob("*.partial.*")) == []


@pytest.mark.parametrize("build,name,phrases", REFUSING)
def test_preview_refuses_the_same_way(tmp_path, build, name, phrases):
    """A half-size wrong picture is still a wrong picture.

    The refusal also has to come *before* the cache is consulted: a refused range must
    not be served from a file rendered before the rule existed, and must not leave a
    file behind.
    """
    if not HAS_MPEG2:
        pytest.skip("this FFmpeg has no mpeg2video")
    path = build(tmp_path, name)
    project, paths = one_clip_project(path)
    cache = PreviewCache(tmp_path / "cache")

    with pytest.raises(PreviewError) as caught:
        prepare_preview(project, paths, F(0), F(2), cache)
    for phrase in phrases:
        assert phrase in str(caught.value), str(caught.value)
    assert list(cache.entries()) == []
    assert list((tmp_path / "cache").glob("preview-*")) == []


@pytest.mark.parametrize("build,name,phrases", REFUSING)
def test_a_still_frame_refuses_the_same_way(tmp_path, build, name, phrases):
    """Stepping through refused picture says so, instead of showing a wrong frame."""
    if not HAS_MPEG2:
        pytest.skip("this FFmpeg has no mpeg2video")
    path = build(tmp_path, name)
    project, paths = one_clip_project(path)

    with pytest.raises(StillError) as caught:
        extract_composition_still(project, paths, F(1))
    for phrase in phrases:
        assert phrase in str(caught.value), str(caught.value)


def test_the_three_paths_say_exactly_the_same_thing(tmp_path):
    """One decision, three entry points: the wording may not drift apart."""
    if not HAS_HEVC:
        pytest.skip("this FFmpeg has no libx265")
    path = make_hdr_metadata_h265(tmp_path)
    project, paths = one_clip_project(path)

    with pytest.raises(RenderError) as export:
        render(RenderRequest(project=project, asset_paths=paths,
                             destination=tmp_path / "out.mp4"))
    with pytest.raises(PreviewError) as preview:
        prepare_preview(project, paths, F(0), F(1), PreviewCache(tmp_path / "cache"))
    with pytest.raises(StillError) as still:
        extract_composition_still(project, paths, F(1))
    assert str(preview.value) == str(export.value)
    assert str(still.value) == str(export.value)


# ------------------------------------------------------ what must not be refused

RENDERABLE = [
    pytest.param(make_progressive_sdr, "progressive_sdr.mkv", id="progressive-sdr-tagged"),
    pytest.param(make_container_flagged_progressive, "flagged.mkv",
                 id="muxer-asked-for-interlaced-but-file-is-progressive"),
    pytest.param(make_sdr_10bit, "ten_bit.mkv", id="ten-bit-sdr-is-not-hdr"),
    pytest.param(make_untagged_avi, "untagged.avi", id="field-order-not-stated"),
]


@pytest.mark.parametrize("build,name", RENDERABLE)
def test_picture_this_build_can_draw_still_renders(tmp_path, build, name):
    """The probe must not disturb what is rendered: same pictures, same order.

    Each output picture is compared with the picture the source file *decodes to* at
    the same place in the sequence — measured, not assumed. The tolerance is measured
    too: 3 codes worst through the MP4 preset on these fixtures (the encoder's own
    4:2:0 rounding on a 64x48 flat field), against a colour step of 51, so one wrong or
    missing picture cannot hide inside it.
    """
    path = build(tmp_path, name)
    project, paths = one_clip_project(path)
    destination = tmp_path / "out.mp4"
    result = render(RenderRequest(project=project, asset_paths=paths, destination=destination))

    plan = build_plan(project, F(0), None)
    assert result.produced_frames == plan.total_frames
    source_centres = picture_facts(path).centres
    output_centres = picture_facts(destination).centres
    assert len(output_centres) == plan.total_frames == len(source_centres)
    worst = max(
        max(abs(a - b) for a, b in zip(src, out, strict=True))
        for src, out in zip(source_centres, output_centres, strict=True)
    )
    assert worst <= 8, f"centre colour moved by {worst} codes"


@pytest.mark.parametrize("build,name,phrase", [
    pytest.param(make_sdr_10bit, "ten_bit.mkv", "does not state a colour transfer",
                 id="ten-bit"),
    pytest.param(make_untagged_avi, "untagged.avi", "does not state a field order",
                 id="no-field-order"),
])
def test_a_format_that_is_not_stated_is_warned_about_and_rendered(tmp_path, build, name, phrase):
    """Unknown is unknown: said out loud, never refused, never guessed at."""
    path = build(tmp_path, name)
    project, paths = one_clip_project(path)
    result = render(RenderRequest(project=project, asset_paths=paths,
                                  destination=tmp_path / "out.mp4"))
    assert any(phrase in warning for warning in result.warnings), result.warnings
    assert not any("no deinterlacer" in w and "is interlaced" in w for w in result.warnings)


@pytest.mark.parametrize("build,name", [
    pytest.param(make_progressive_sdr, "progressive_sdr.mkv", id="fully-tagged-sdr"),
    pytest.param(make_container_flagged_progressive, "flagged.mkv", id="container-flagged"),
    pytest.param(make_identity_video, "identity.mkv", id="lossless-identity"),
])
def test_ordinary_picture_produces_no_capability_message(tmp_path, build, name):
    """No warning, no refusal: the common case stays silent."""
    path = build(tmp_path, name)
    project, paths = one_clip_project(path)
    result = render(RenderRequest(project=project, asset_paths=paths,
                                  destination=tmp_path / "out.mp4"))
    for warning in result.warnings:
        assert "field order" not in warning, warning
        assert "colour transfer" not in warning, warning
        assert "deinterlacer" not in warning, warning
        assert "tone" not in warning, warning


# ---------------------------------------------------- an unused source cannot block

def test_a_refusable_source_sitting_in_the_bin_does_not_stop_a_render(tmp_path):
    """Imported and registered, but no clip of it: the render never looks at it."""
    if not HAS_MPEG2:
        pytest.skip("this FFmpeg has no mpeg2video")
    good = make_identity_video(tmp_path, "good.mkv", count=50)
    bad = make_hdr10(tmp_path, "hdr.mkv")
    project = Project(output=OutputSettings(width=SIZE[0], height=SIZE[1], frame_rate=FPS))
    project = project.with_tracks((Track("v1", TrackKind.VIDEO, "V1"),))
    project = place(project, good, "good", "v1", "c1")
    # the HDR stream is in the project's sources, exactly as importing it puts it
    project = RegisterSourceCommand(source_from_probe(bad, 0, asset_id="hdr")).apply(project)
    paths = {"good": good, "hdr": bad}

    result = render(RenderRequest(project=project, asset_paths=paths,
                                  destination=tmp_path / "out.mp4"))
    assert result.produced_frames > 0


def test_a_disabled_video_track_does_not_contribute_picture_and_cannot_block(tmp_path):
    """The plan resolves stacking by *enabled* track, so that is what the check sees."""
    if not HAS_MPEG2:
        pytest.skip("this FFmpeg has no mpeg2video")
    good = make_identity_video(tmp_path, "good.mkv", count=50)
    bad = make_interlaced_mpeg2(tmp_path, "interlaced.ts")
    project = Project(output=OutputSettings(width=SIZE[0], height=SIZE[1], frame_rate=FPS))
    project = project.with_tracks((
        Track("vtop", TrackKind.VIDEO, "Top"),
        Track("voff", TrackKind.VIDEO, "Off", enabled=False),
    ))
    project = place(project, good, "good", "vtop", "c1")
    project = place(project, bad, "bad", "voff", "c2", at=F(1))
    paths = {"good": good, "bad": bad}

    plan = build_plan(project, F(0), None)
    assert {item.clip.asset_id for item in plan.video_items if item.clip} == {"good"}
    result = render(RenderRequest(project=project, asset_paths=paths,
                                  destination=tmp_path / "out.mp4"))
    assert result.produced_frames == plan.total_frames
    # the refused clip really is in the project and would be shown if the track were
    # enabled: what saves this render is the track being off, not the clip being absent
    assert {clip.track_id for clip in project.clips} == {"vtop", "voff"}
    enabled_off = project.track("voff")
    assert enabled_off is not None and not enabled_off.enabled


def test_picture_covered_by_a_higher_track_is_not_participating(tmp_path):
    """Only the topmost enabled clip is shown, so a covered HDR master renders nothing."""
    if not HAS_HEVC:
        pytest.skip("this FFmpeg has no libx265")
    good = make_identity_video(tmp_path, "good.mkv", count=50)
    bad = make_hdr_metadata_h265(tmp_path, "hdr_metadata.mkv")
    project = Project(output=OutputSettings(width=SIZE[0], height=SIZE[1], frame_rate=FPS))
    project = project.with_tracks((
        Track("vtop", TrackKind.VIDEO, "Top"),
        Track("vlow", TrackKind.VIDEO, "Low"),
    ))
    project = place(project, bad, "bad", "vlow", "c2")
    project = place(project, good, "good", "vtop", "c1")
    paths = {"good": good, "bad": bad}

    plan = build_plan(project, F(0), F(2))
    assert {item.clip.asset_id for item in plan.video_items if item.clip} == {"good"}
    result = render(RenderRequest(project=project, asset_paths=paths,
                                  destination=tmp_path / "out.mp4"))
    assert result.produced_frames > 0


def test_picture_outside_the_render_range_does_not_block_it(tmp_path):
    """An export of one shot is not held up by another shot's unusable picture."""
    if not HAS_MPEG2:
        pytest.skip("this FFmpeg has no mpeg2video")
    good = make_identity_video(tmp_path, "good.mkv", count=50)
    bad = make_interlaced_mpeg2(tmp_path, "interlaced.ts")
    project = Project(output=OutputSettings(width=SIZE[0], height=SIZE[1], frame_rate=FPS))
    project = project.with_tracks((Track("v1", TrackKind.VIDEO, "V1"),))
    project = place(project, good, "good", "v1", "c1", at=F(0))
    project = place(project, bad, "bad", "v1", "c2", at=F(2))
    paths = {"good": good, "bad": bad}

    result = render(RenderRequest(project=project, asset_paths=paths,
                                  destination=tmp_path / "out.mp4", out_start=F(0), out_end=F(2)))
    assert result.produced_frames == 50
    # and the range that does show it is refused
    with pytest.raises(RenderError, match="is interlaced"):
        render(RenderRequest(project=project, asset_paths=paths,
                             destination=tmp_path / "later.mp4", out_start=F(2), out_end=F(4)))


def test_the_sound_of_an_unusable_picture_stream_still_renders(tmp_path):
    """A file this build cannot draw picture from may still be used for its sound.

    Only its audio stream has a clip, so the picture check has nothing to look at.
    """
    if not HAS_MPEG2:
        pytest.skip("this FFmpeg has no mpeg2video")
    interlaced = make_interlaced_mpeg2(tmp_path, "interlaced_sound.ts", with_sound=True)
    project = Project(
        output=OutputSettings(width=SIZE[0], height=SIZE[1], frame_rate=FPS,
                              sample_rate=48000, channel_layout="mono")
    )
    project = project.with_tracks((Track("a1", TrackKind.AUDIO, "A1"),))
    project = place(project, interlaced, "src", "a1", "c1", stream_index=1)
    result = render(RenderRequest(project=project, asset_paths={"src": interlaced},
                                  destination=tmp_path / "sound.mp4"))
    assert result.produced_audio_samples >= 48000  # a second of the tone is there


def test_a_muted_video_track_still_shows_its_picture(tmp_path):
    """Mute silences sound; it does not remove picture.

    So a muted video track still *participates*, and unusable picture on one is still
    refused. This is the boundary of "unused", and it is stated here because it is the
    reading a user could otherwise get wrong.
    """
    if not HAS_MPEG2:
        pytest.skip("this FFmpeg has no mpeg2video")
    path = make_interlaced_mpeg2(tmp_path, "interlaced.ts")
    project, paths = one_clip_project(
        path, tracks=(Track("v1", TrackKind.VIDEO, "V1", muted=True),)
    )
    plan = build_plan(project, F(0), F(2))
    assert {item.clip.asset_id for item in plan.video_items if item.clip} == {"src"}
    with pytest.raises(RenderError, match="is interlaced"):
        render(RenderRequest(project=project, asset_paths=paths,
                             destination=tmp_path / "out.mp4"))


# --------------------------------------------------------- what the check measures


def test_a_project_that_recorded_nothing_about_the_picture_is_still_refused(tmp_path):
    """The render measures the file it is about to decode.

    A project document written before these fields existed — or a hand-built project
    like this one, which holds no ``Asset`` record at all — records no field order and
    no colour transfer. Reading "nothing recorded" as "progressive SDR" would be the
    silent wrong-media bug, so the preflight re-reads the asset instead of trusting the
    document.
    """
    if not HAS_MPEG2:
        pytest.skip("this FFmpeg has no mpeg2video")
    path = make_interlaced_mpeg2(tmp_path, "interlaced.ts")
    project, paths = one_clip_project(path)
    # the domain record a hand-built project holds is a StreamSource: bounds, rates and
    # stream position only. Nothing in it can say what this picture is coded as.
    assert not {"field_order", "color_transfer"} & set(StreamSource.__dataclass_fields__)
    with pytest.raises(RenderError, match="field_order 'tt'"):
        render(RenderRequest(project=project, asset_paths=paths,
                             destination=tmp_path / "out.mp4"))


def test_the_project_records_the_same_tags_the_render_reads(tmp_path):
    """Import-time metadata agrees with the render-time probe, on both sides.

    It is displayed from one and decided from the other: a disagreement between them
    would mean the bin is describing a different picture than the one being rendered.
    """
    from video_editor.media.capability import probe_picture_format
    from video_editor.media.ffprobe import probe_file

    path = make_hdr10(tmp_path)
    recorded = probe_file(path).metadata.stream(0)
    measured = probe_picture_format(path, 0, from_time=F(0))
    assert recorded.field_order == measured.field_order == "progressive"
    assert recorded.color_transfer == measured.color_transfer == "smpte2084"
    assert recorded.color_primaries == measured.color_primaries == "bt2020"
    assert recorded.bit_depth == measured.bit_depth == 10


def test_stepping_the_playhead_does_not_probe_every_frame(tmp_path, monkeypatch):
    """Five stills inside one second: still one header read and one picture window.

    The still path runs on every playhead move, so an uncached probe would add two
    subprocesses per frame stepped.
    """
    from video_editor.media import capability

    path = make_identity_video(tmp_path, "identity.mkv", count=50)
    project, paths = one_clip_project(path)

    probes: list[list[str]] = []
    real = subprocess.run

    def counting(args, *rest, **kwargs):
        text = " ".join(str(item) for item in args)
        # the capability probe's own two option signatures; the still decode's ffmpeg
        # call carries neither, so what is counted here is the probe and nothing else
        if "field_order,color_range" in text or "interlaced_frame" in text:
            probes.append([str(item) for item in args])
        return real(args, *rest, **kwargs)

    monkeypatch.setattr(capability.subprocess, "run", counting)
    for time in (F(0), F(2, 25), F(10, 25), F(18, 25), F(24, 25)):
        assert not extract_composition_still(project, paths, time).black
    assert len(probes) == 2, probes  # one header read and one picture window, in total


def test_a_source_picture_the_render_refuses_can_still_be_inspected(tmp_path):
    """The bin's source inspector keeps working on an unusable picture.

    Refusal belongs to the paths that produce the composition's own media; a file that
    merely exists in the project must stay viewable, or the application would hide the
    very material a user has to look at in order to decide what to do with it.
    """
    path = make_field_coded_h264(tmp_path)
    project, paths = one_clip_project(path)
    with pytest.raises(RenderError, match="is interlaced"):
        render(RenderRequest(project=project, asset_paths=paths,
                             destination=tmp_path / "out.mp4"))
    still = extract_source_still(project, paths, "c1", F(1), F(1))
    assert not still.black
    assert (still.width, still.height) == SIZE
    # the picture is the one at that source time: picture 25 of a 25 fps stream, and the
    # same colour the source file itself decodes to there
    source = picture_facts(path)
    assert source.identities[25] == 25  # the fixture is what it claims to be
    got = centre_of(still.rgb)
    expected = source.centres[25]
    assert max(abs(a - b) for a, b in zip(got, expected, strict=True)) <= 8, (got, expected)


def test_a_file_that_cannot_be_probed_keeps_the_error_that_names_the_problem(tmp_path):
    """A gone file and a file that is not media are not format problems.

    Both already had precise messages in the still and export paths before this
    preflight existed, and a probe failure here would otherwise talk over them: the
    measurement said nothing about the format, so it may only say "unknown".
    """
    good = make_identity_video(tmp_path, "good.mkv", count=50)
    project, _paths = one_clip_project(good)
    with pytest.raises(StillError, match="is not there"):
        extract_composition_still(project, {"src": tmp_path / "gone.mkv"}, F(1))

    broken = make_garbage(tmp_path)
    # a stream source built by hand: this file cannot be probed, which is the point
    broken_project = Project(output=OutputSettings(width=64, height=48, frame_rate=FPS))
    broken_project = RegisterSourceCommand(
        StreamSource("broken", 0, TrackKind.VIDEO, F(0), F(1), frame_rate=FPS,
                     width=64, height=48, stream_position=0)
    ).apply(broken_project)
    broken_project = broken_project.with_tracks((Track("v1", TrackKind.VIDEO, "V1"),))
    broken_project = AddClipCommand("broken", 0, "v1", F(0), source_in=F(0),
                                    source_out=F(1), clip_id="c1").apply(broken_project)
    with pytest.raises(StillError, match="cannot decode the frame"):
        extract_composition_still(broken_project, {"broken": broken}, F(0))
    # and the preflight's own answer for that stream is a warning, never a refusal
    plan = build_plan(broken_project, F(0), F(1))
    warnings = check_picture_capabilities(broken_project, [plan], {"broken": broken}, None)
    assert any("could not be checked" in warning for warning in warnings), warnings
    assert any(broken.name in warning for warning in warnings), warnings


def test_a_black_frame_reads_no_source_and_cannot_be_refused(tmp_path):
    """Black is the composition's own picture: no stream is consulted for it.

    So a gap inside a composition that also contains refused picture shows black, while
    the same playhead over that picture is refused. The check sits after the black
    early return for exactly this reason.
    """
    if not HAS_MPEG2:
        pytest.skip("this FFmpeg has no mpeg2video")
    path = make_interlaced_mpeg2(tmp_path, "interlaced.ts")
    project, paths = one_clip_project(path)
    # move the only clip out to [2, 4): the first two seconds are a gap, which is black
    clip = project.clips[0]
    project = project.with_clips((replace(clip, output_start=F(2)),))

    still = extract_composition_still(project, paths, F(1))
    assert still.black
    with pytest.raises(RenderError, match="is interlaced"):
        render(RenderRequest(project=project, asset_paths=paths,
                             destination=tmp_path / "out.mp4", out_start=F(2), out_end=F(4)))


def centre_of(rgb: bytes, size: tuple[int, int] = SIZE) -> tuple[int, int, int]:
    """The middle pixel of a raw RGB24 picture, the same spot ``picture_facts`` reads."""
    width, height = size
    offset = (height // 2) * width * 3 + (width // 2) * 3
    return rgb[offset], rgb[offset + 1], rgb[offset + 2]
