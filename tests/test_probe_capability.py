"""Picture-format probing and the rules that decide what this build can draw.

Expectations here come from two independent places, never from the code under test:

* the tags ``ffprobe`` itself prints for a fixture (``raw_picture_tags``, which shells
  out to the tool with its own option list) plus the explicit value written beside
  each shape below — measured on FFmpeg 8.1.3 and recorded in the task log before
  any assertion was written;
* hand-worked application of the rule to a :class:`PictureFormatFacts` built by hand,
  which is how the shapes this FFmpeg cannot author (Dolby Vision, HDR10+, a named
  mastering-display record) are covered anyway: the record names are the strings this
  build's own ``libavutil`` prints, found in the shared library.

That a render, preview or still is actually *blocked* is shown in
``tests/test_picture_capability.py``; this file is about what is measured and what the
rules say.
"""

from __future__ import annotations

import subprocess
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
    make_interlaced_mpeg2,
    make_long_video,
    make_progressive_sdr,
    make_sdr_10bit,
    make_untagged_avi,
    raw_picture_tags,
    source_from_probe,
)
from video_editor.core.commands import AddClipCommand, RegisterSourceCommand
from video_editor.core.model import OutputSettings, Project, Track, TrackKind
from video_editor.media.capability import (
    HDR_SIDE_DATA_MARKERS,
    INTERLACED_FIELD_ORDERS,
    PICTURE_STRUCTURE_FRAMES,
    PictureCapabilityError,
    PictureFormatFacts,
    assess_picture_format,
    check_picture_capabilities,
    clear_probe_cache,
    hdr_side_data,
    hdr_transfer,
    interlaced_field_order,
    plausible_hdr_without_transfer,
    probe_picture_format,
)
from video_editor.media.ffprobe import probe_file
from video_editor.render.plan import build_plan

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")

#: What FFmpeg 8.1.3 prints for x264/x265 encoder settings. Every H.264 file in this
#: suite carries it, and it says nothing about HDR, so it is the negative control for
#: the metadata rule.
UNREGISTERED_SEI = "H.26[45] User Data Unregistered SEI message"


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    """No measurement may leak between tests: each one probes its own file."""
    clear_probe_cache()
    yield
    clear_probe_cache()


def facts(**overrides) -> PictureFormatFacts:
    """A measured stream as handed to the rules, with only what is asked named."""
    base = {"path": Path("/x/clip.mkv"), "stream_index": 0, "field_order": "progressive",
            "frames_interlaced": False, "frames_read": 8}
    return PictureFormatFacts(**{**base, **overrides})


# ------------------------------------------------ what the probe records, verbatim

# Each row is a fixture and what the container was *measured* to say about it. A row
# whose fixture does not hold what is claimed fails as a fixture bug, which is the
# point of checking the tool's own output as well as the application's record of it.
# ``bit_depth`` is ``None`` where the file states no sample depth and its pixel-format
# name does not say one either (MPEG-2 prints neither).
SHAPES = [
    pytest.param(make_progressive_sdr, {
        "field_order": "progressive", "color_transfer": "bt709",
        "color_primaries": None, "color_space": "bt709",
        "pix_fmt": "yuv420p", "bit_depth": 8, "frames_interlaced": False,
    }, id="progressive-sdr"),
    pytest.param(make_container_flagged_progressive, {
        # built with -field_order tt, measured as progressive in the stream *and* in
        # its frames: what a muxer was told is not what the file reports
        "field_order": "progressive", "color_transfer": None,
        "color_primaries": None, "color_space": None,
        "pix_fmt": "yuv420p", "bit_depth": 8, "frames_interlaced": False,
    }, id="container-flagged"),
    pytest.param(make_field_coded_h264, {
        "field_order": "progressive", "color_transfer": None,
        "color_primaries": None, "color_space": None,
        "pix_fmt": "yuv420p", "bit_depth": 8, "frames_interlaced": True,
    }, id="field-coded-h264"),
    pytest.param(make_sdr_10bit, {
        "field_order": "progressive", "color_transfer": None,
        "color_primaries": None, "color_space": None,
        "pix_fmt": "yuv420p10le", "bit_depth": 10, "frames_interlaced": False,
    }, id="sdr-10-bit"),
    pytest.param(make_hdr10, {
        "field_order": "progressive", "color_transfer": "smpte2084",
        "color_primaries": "bt2020", "color_space": "bt2020nc",
        "pix_fmt": "yuv420p10le", "bit_depth": 10, "frames_interlaced": False,
    }, id="hdr10-pq"),
    pytest.param(make_hlg, {
        "field_order": "progressive", "color_transfer": "arib-std-b67",
        "color_primaries": "bt2020", "color_space": "bt2020nc",
        "pix_fmt": "yuv420p10le", "bit_depth": 10, "frames_interlaced": False,
    }, id="hlg"),
    pytest.param(make_untagged_avi, {
        "field_order": None, "color_transfer": None,
        "color_primaries": None, "color_space": "bt470bg",
        "pix_fmt": "yuvj422p", "bit_depth": 8, "frames_interlaced": False,
    }, id="no-field-order"),
    pytest.param(make_interlaced_mpeg2, {
        "field_order": "tt", "color_transfer": None,
        "color_primaries": None, "color_space": None,
        "pix_fmt": "yuv420p", "bit_depth": None, "frames_interlaced": True,
    }, id="interlaced-mpeg2",
        marks=pytest.mark.skipif(not HAS_MPEG2, reason="this FFmpeg has no mpeg2video")),
    pytest.param(make_hdr_metadata_h265, {
        "field_order": "progressive", "color_transfer": None,
        "color_primaries": None, "color_space": None,
        "pix_fmt": "yuv420p10le", "bit_depth": 10, "frames_interlaced": False,
    }, id="hdr-static-metadata",
        marks=pytest.mark.skipif(not HAS_HEVC, reason="this FFmpeg has no libx265")),
]


@pytest.mark.parametrize("build,expected", SHAPES)
def test_the_probe_records_the_containers_own_words(tmp_path, build, expected):
    """Import keeps every picture-format tag and invents nothing.

    The application's record is compared against ``ffprobe``'s own output for the same
    file, so a parser that quietly substituted a plausible default — ``progressive``
    for an unstated field order, say — fails here instead of surfacing as a render
    that should have been refused.
    """
    path = build(tmp_path)
    raw = raw_picture_tags(path)
    result = probe_file(path)
    assert result.ok, result.error
    stream = result.metadata.stream(0)
    assert stream is not None
    for field in ("field_order", "color_transfer", "color_primaries", "color_space", "pix_fmt"):
        from_tool = raw.get(field)
        assert from_tool is None or isinstance(from_tool, str), f"{field} is not a token"
        assert getattr(stream, field) == expected[field], field
        assert getattr(stream, field) == (from_tool.lower() if from_tool else None), (
            f"{field}: recorded {getattr(stream, field)!r} is not what ffprobe printed "
            f"({from_tool!r})"
        )
    assert stream.bit_depth == expected["bit_depth"]


@pytest.mark.parametrize("build,expected", SHAPES)
def test_the_picture_window_agrees_about_field_coding(tmp_path, build, expected):
    """The bounded decode says the same about field coding as the container does.

    ``frames_read`` also proves the read stayed a window: no fixture here is longer
    than two seconds, and the read is capped at two windows of
    :data:`PICTURE_STRUCTURE_FRAMES` pictures.
    """
    path = build(tmp_path)
    measured = probe_picture_format(path, 0, from_time=F(0))
    assert measured.frames_read > 0, measured.probe_note
    assert measured.frames_interlaced == expected["frames_interlaced"]
    assert measured.frames_read <= 2 * PICTURE_STRUCTURE_FRAMES
    assert measured.read_from[0] == F(0)


def test_a_window_never_decodes_the_whole_title(tmp_path):
    """Twelve seconds of 25 fps picture, and the probe still reads only its windows.

    Still-frame inspection runs on every playhead move, so an unbounded
    ``-show_frames`` would turn stepping through a film into decoding the film.
    """
    path = make_long_video(tmp_path, seconds=12)
    measured = probe_picture_format(path, 0, from_time=F(6))
    assert measured.read_from == (F(0), F(6))
    assert 0 < measured.frames_read <= 2 * PICTURE_STRUCTURE_FRAMES


def test_an_absent_token_is_recorded_as_absent(tmp_path):
    """AVI states no field order; that is stored as unknown, never as progressive."""
    path = make_untagged_avi(tmp_path)
    assert raw_picture_tags(path).get("field_order") is None
    assert probe_file(path).metadata.stream(0).field_order is None
    assert probe_picture_format(path, 0).field_order is None


def test_side_data_names_are_kept_without_their_payloads(tmp_path):
    """The name of each record is kept; its payload is neither stored nor decoded.

    ``Content light level metadata`` arrives on the *frames* for this FFmpeg rather
    than in the stream record, which is why both levels are read.
    """
    path = make_hdr_metadata_h265(tmp_path)
    measured = probe_picture_format(path, 0)
    assert "Content light level metadata" in raw_picture_tags(path)["frame_side_data"]
    assert "Content light level metadata" in measured.frames_side_data
    assert "Content light level metadata" in measured.all_side_data


def test_mpeg_families_are_measured_at_the_head_of_the_stream(tmp_path):
    """Measured FFmpeg behaviour: a counted read interval starting further into a
    transport stream prints nothing at all, so the head window is always read too.
    Without it this fixture — the classic broadcast shape — would be reported as
    unmeasurable rather than interlaced.
    """
    if not HAS_MPEG2:
        pytest.skip("this FFmpeg has no mpeg2video")
    path = make_interlaced_mpeg2(tmp_path)
    starved = probe_picture_format(path, 0, from_time=F(1))
    assert starved.read_from[0] == F(0), "the head window is what answers for this family"
    assert starved.frames_interlaced is True
    if len(starved.read_from) == 1:
        assert starved.probe_note and "at 1.000s" in starved.probe_note


# ------------------------------------------------------------------ the decisions


@pytest.mark.parametrize("order", sorted(INTERLACED_FIELD_ORDERS))
def test_a_stated_field_order_refuses_and_quotes_the_value(order):
    refusal, warnings = assess_picture_format(facts(field_order=order, frames_interlaced=True))
    assert refusal is not None
    assert "is interlaced" in refusal
    assert f"field_order '{order}'" in refusal
    assert "no deinterlacer" in refusal
    assert warnings == ()


def test_field_coding_hidden_from_the_container_still_refuses():
    """The measured x264 case: the stream says progressive, its decoded frames do not."""
    refusal, warnings = assess_picture_format(
        facts(frames_interlaced=True, top_field_first=False, frames_read=16,
              side_data=(UNREGISTERED_SEI,))
    )
    assert refusal is not None and "is interlaced" in refusal
    assert "16 picture(s) decoded from it report interlaced frames" in refusal
    assert warnings == ()


def test_an_unstated_field_order_is_warned_about_and_rendered():
    refusal, warnings = assess_picture_format(facts(field_order=None))
    assert refusal is None
    assert len(warnings) == 1
    assert "does not state a field order" in warnings[0]
    assert "no deinterlacer is implemented" in warnings[0]


@pytest.mark.parametrize("token,words", [
    ("smpte2084", "PQ (HDR10/HDR10+) transfer"),
    ("arib-std-b67", "HLG (ARIB STD-B67) transfer"),
    ("arib_std_b67", "HLG (ARIB STD-B67) transfer"),
])
def test_a_stated_hdr_transfer_refuses_and_quotes_it(token, words):
    refusal, warnings = assess_picture_format(facts(color_transfer=token))
    assert refusal is not None and "has an HDR colour transfer" in refusal
    assert f"color_transfer '{token}'" in refusal
    assert words in refusal
    assert "no colour-space or tone conversion" in refusal
    assert warnings == ()


@pytest.mark.parametrize("name", [
    "Dolby Vision RPU Data",
    "Dolby Vision Metadata",
    "Mastering display metadata",
    "Content light level metadata",
    "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)",
])
def test_hdr_metadata_records_refuse_by_name(name):
    """DOVI and HDR10+ metadata cannot be authored by this FFmpeg at all, so the rule
    is checked against the record names its own ``libavutil`` prints.
    """
    assert hdr_side_data([name]) == (name,)
    refusal, _ = assess_picture_format(facts(side_data=(name,)))
    assert refusal is not None and "carries HDR metadata" in refusal
    assert name in refusal


@pytest.mark.parametrize("name", [
    UNREGISTERED_SEI,
    "CPB properties",
    "AVPanScan",
    "GOP timecode",
])
def test_metadata_that_is_not_an_hdr_claim_is_not_treated_as_one(name):
    """x264 and x265 put their own settings in an unregistered SEI in *every* file.

    A rule matching on "SEI" or "User Data" would refuse ordinary H.264, the most
    common thing in the media bin.
    """
    assert hdr_side_data([name]) == ()
    refusal, _ = assess_picture_format(
        facts(color_transfer="bt709", side_data=(name,))
    )
    assert refusal is None


def test_ten_bit_is_not_treated_as_hdr():
    """10-bit with no transfer stated: warned about, never refused, no tone assumed."""
    refusal, warnings = assess_picture_format(
        facts(pix_fmt="yuv420p10le", bit_depth=10)
    )
    assert refusal is None
    assert len(warnings) == 1
    assert "does not state a colour transfer" in warnings[0]
    assert "It is 10-bit" in warnings[0]
    assert "no tone mapping is implemented" in warnings[0]


def test_ordinary_untagged_sdr_gets_no_colour_warning():
    """An 8-bit file that states nothing is the normal case, not a warning case.

    The value is still shown as "not stated" wherever the stream is listed; warning on
    every render of it would bury the warnings that mean something.
    """
    assert not plausible_hdr_without_transfer(None, None, None, 8)
    refusal, warnings = assess_picture_format(facts(pix_fmt="yuv420p", bit_depth=8))
    assert refusal is None
    assert warnings == ()


def test_wide_gamut_without_a_transfer_is_warned_about():
    assert plausible_hdr_without_transfer(None, "bt2020", "bt2020nc", 8)
    _, warnings = assess_picture_format(
        facts(color_primaries="bt2020", color_space="bt2020nc", bit_depth=8)
    )
    assert len(warnings) == 1
    assert "It uses BT.2020 primaries" in warnings[0]


def test_a_stated_transfer_that_is_sdr_is_not_a_refusal():
    assert hdr_transfer("bt709") is None
    assert hdr_transfer("SMPTE2084") == "PQ (HDR10/HDR10+) transfer"
    assert hdr_transfer(None) is None
    assert hdr_transfer("unknown") is None


def test_a_picture_window_that_could_not_be_read_is_reported_as_a_gap():
    """Nothing decoded is never evidence of progressive SDR."""
    refusal, warnings = assess_picture_format(
        facts(frames_interlaced=None, frames_read=0,
              probe_note="at 1.000s: no picture could be decoded in the window that was read")
    )
    assert refusal is None
    assert len(warnings) == 1
    assert "could not be checked completely" in warnings[0]
    assert "may not have been detected" in warnings[0]


def test_the_rules_are_built_on_stated_tokens_only():
    """A guard on the rule's own vocabulary: nothing here may assume a default."""
    assert set(INTERLACED_FIELD_ORDERS) == {"tt", "bb", "tb", "bt"}
    assert set(HDR_SIDE_DATA_MARKERS) == {
        "dolbyvision", "dovi", "hdr10", "hdrdynamicmetadata", "smpte2094",
        "masteringdisplay", "contentlightlevel",
    }
    assert interlaced_field_order("unknown") is False
    assert interlaced_field_order(None) is False
    assert interlaced_field_order("tt") is True


# ------------------------------------------------------------- the probe itself


def video_project(tmp_path, *paths) -> tuple[Project, dict[str, Path]]:
    """One video track, one clip per file, nothing else: the smallest thing that makes
    a picture *participate* in a render. Sources come from each file's own probe.
    """
    project = Project(output=OutputSettings(width=64, height=48, frame_rate=F(25)))
    project = project.with_tracks((Track("v1", TrackKind.VIDEO, "V1"),))
    asset_paths: dict[str, Path] = {}
    for index, path in enumerate(paths):
        source = source_from_probe(path, 0, asset_id=f"a{index}")
        project = RegisterSourceCommand(source).apply(project)
        project = AddClipCommand(source.asset_id, source.stream_index, "v1",
                                 index * F(2)).apply(project)
        asset_paths[source.asset_id] = path
    return project, asset_paths


def test_a_stream_is_measured_once_per_sampling_window(tmp_path, monkeypatch):
    """Stepping the playhead inside one bucket must not spawn more probes.

    Counted through the public preflight, because that is what the still path calls on
    every playhead move.
    """
    from video_editor.media import capability

    path = make_progressive_sdr(tmp_path)
    project, paths = video_project(tmp_path, path)
    plan = build_plan(project, F(0), F(2))

    calls: list[list[str]] = []
    real = subprocess.run

    def counting(args, *rest, **kwargs):
        calls.append([str(item) for item in args])
        return real(args, *rest, **kwargs)

    monkeypatch.setattr(capability.subprocess, "run", counting)
    check_picture_capabilities(project, [plan], paths)
    first = len(calls)
    assert first == 2, "one header read and one picture window"

    check_picture_capabilities(project, [plan], paths)
    assert len(calls) == first, "the same stream and window is not probed twice"


def test_a_file_replaced_on_disk_is_measured_again(tmp_path):
    """A source swapped for another encode must not be answered from the old one."""
    from video_editor.media import capability

    path = make_progressive_sdr(tmp_path, "one.mkv")
    assert capability._cached_probe(path, 0, None, F(0)).frames_interlaced is False

    replacement = make_field_coded_h264(tmp_path, "two.mkv")
    path.write_bytes(replacement.read_bytes())
    assert capability._cached_probe(path, 0, None, F(0)).frames_interlaced is True, (
        "the probe cache is keyed on file identity, not on the path alone"
    )


def test_an_unprobeable_file_is_named_rather_than_assumed(tmp_path):
    path = make_garbage(tmp_path)
    with pytest.raises(PictureCapabilityError) as caught:
        probe_picture_format(path, 0)
    assert "cannot read the picture format of" in str(caught.value)
    assert path.name in str(caught.value)


def test_the_probe_reads_one_streams_own_record_not_another(tmp_path):
    """A stream that is not in the file is refused by name, never answered from a
    neighbour."""
    path = make_progressive_sdr(tmp_path)
    with pytest.raises(PictureCapabilityError) as caught:
        probe_picture_format(path, 7)
    assert "stream 7 is not in it" in str(caught.value)
