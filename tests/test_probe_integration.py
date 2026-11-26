"""Integration tests: real ffprobe against generated fixtures."""

import json
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from media_fixtures import (
    FFPROBE,
    HAS_FFMPEG,
    make_audio_only,
    make_garbage,
    make_identity_mp4,
    make_identity_offset,
    make_identity_vfr,
    make_identity_video,
    make_mixed,
    make_per_stream_offset,
    make_stream_offset,
    make_truncated_identity_offset,
    make_video_only,
    make_whole_asset_offset,
)
from video_editor.media.assets import StreamKind
from video_editor.media.ffprobe import probe_file

pytestmark = pytest.mark.skipif(
    not HAS_FFMPEG, reason="ffprobe/ffmpeg required for media integration tests"
)

TOL = Fraction(1, 10)  # aac priming/muxer rounding tolerance


def approx(value: Fraction | None, expected: Fraction) -> bool:
    return value is not None and abs(value - expected) <= TOL


def test_mixed_file_streams_and_ids(tmp_path):
    path = make_mixed(tmp_path)
    result = probe_file(path)
    assert result.ok, result.error
    meta = result.metadata
    kinds = {s.stream_index: s.kind for s in meta.streams}
    assert kinds == {0: StreamKind.VIDEO, 1: StreamKind.AUDIO, 2: StreamKind.AUDIO}
    video = meta.stream(0)
    assert video.frame_rate == Fraction(25)
    assert (video.width, video.height) == (128, 96)
    # mkv omits per-stream durations: bounds come from the container horizon
    assert approx(video.end_time, Fraction(6))
    audio1, audio2 = meta.stream(1), meta.stream(2)
    assert audio1.language == "eng"
    assert audio2.language == "fre"
    assert audio1.sample_rate == 44100
    assert approx(audio1.end_time, Fraction(6))


def test_audio_only_file_is_editable(tmp_path):
    path = make_audio_only(tmp_path)
    result = probe_file(path)
    assert result.ok, result.error
    (stream,) = result.metadata.streams
    assert stream.kind.value == "audio"
    assert approx(stream.duration, Fraction(3))
    assert stream.origin == Fraction(0)
    assert stream.editable


def test_video_only_has_no_audio_streams(tmp_path):
    path = make_video_only(tmp_path)
    result = probe_file(path)
    assert result.ok, result.error
    assert all(s.kind.value == "video" for s in result.metadata.streams)
    video = result.metadata.stream(0)
    # 24000/1001 must survive as an exact rational
    assert video.frame_rate == Fraction(24000, 1001)


def test_whole_asset_nonzero_origin(tmp_path):
    path = make_whole_asset_offset(tmp_path)
    result = probe_file(path)
    assert result.ok, result.error
    for stream in result.metadata.streams:
        assert stream.start_time is not None
        assert approx(stream.origin, Fraction("2.5"))
    assert approx(result.metadata.container_start, Fraction("2.5"))


def test_stream_level_offset_preserved(tmp_path):
    path = make_stream_offset(tmp_path)
    result = probe_file(path)
    assert result.ok, result.error
    video = next(s for s in result.metadata.streams if s.kind.value == "video")
    audio = next(s for s in result.metadata.streams if s.kind.value == "audio")
    assert approx(video.origin, Fraction(0))
    assert approx(audio.origin, Fraction(1))
    # stream bounds account for the offset
    assert audio.end_time is not None
    assert approx(audio.end_time, Fraction(3))


def test_garbage_file_reports_error_not_crash(tmp_path):
    path = make_garbage(tmp_path)
    result = probe_file(path)
    assert not result.ok
    assert result.metadata is None
    assert result.error


# ------------------------------------------- recorded bounds against real frames
#
# The bounds a probe records are a claim about a file; the only witness is what
# decodes from it. These tests measure the last picture (or sound frame) of a
# fixture themselves - integer stamps multiplied by the stream's exact
# ``time_base``, never ffprobe's printed decimals - and compare the recorded
# bound with that. An expectation never comes from the parser under test.


def probe_json(args: list[str]) -> dict:
    """``ffprobe`` as a measurement tool: a failure raises, it never answers empty."""
    proc = subprocess.run(
        [FFPROBE, "-hide_banner", "-v", "error", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {args[-1]}: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout)


def decoded_extent(path: Path, selector: str) -> tuple[int, Fraction, Fraction]:
    """``(frames, last presentation time, that frame's own duration)`` for a stream.

    The duration is read from the last frame the decode produced, not computed
    from a nominal frame rate: for variable-rate material the two disagree, and a
    rate is a claim about a stream rather than a fact about one frame.
    """
    stream = probe_json(
        ["-select_streams", selector, "-show_entries", "stream=time_base",
         "-of", "json", str(path)]
    )["streams"][0]
    time_base = Fraction(stream["time_base"])
    frames = probe_json(
        ["-select_streams", selector,
         "-show_entries", "frame=best_effort_timestamp,pts,duration",
         "-of", "json", str(path)]
    )["frames"]
    stamps = sorted(
        (
            Fraction(int(frame["best_effort_timestamp"])) * time_base,
            Fraction(int(frame["duration"])) * time_base,
        )
        for frame in frames
        if frame.get("best_effort_timestamp") not in (None, "N/A")
        and frame.get("duration") not in (None, "N/A")
    )
    if not stamps:
        raise RuntimeError(f"{path.name}: nothing decoded for {selector}")
    last_pts, duration = stamps[-1]
    return len(stamps), last_pts, duration


def test_offset_matroska_end_is_the_material_not_start_plus_duration(tmp_path):
    """The reported defect, measured end to end.

    100 pictures at 25 fps whose presentation starts at 5/2 s: they end at 323/50
    and the last one runs 1/25 further, so the file holds material up to 13/2 s.
    ``ffprobe`` reports container start 2.5 and duration 6.5, and reading those as
    a span gives 9 s - which is where the illegal selection used to look legal.
    """
    path = make_identity_offset(tmp_path, count=100)
    count, last_pts, last_duration = decoded_extent(path, "v:0")
    assert count == 100
    assert last_pts == Fraction(323, 50) and last_duration == Fraction(1, 25)
    material_end = last_pts + last_duration
    assert material_end == Fraction(13, 2)

    stream = probe_file(path).metadata.stream(0)
    assert stream.origin == Fraction(5, 2)
    assert stream.duration is None, "Matroska gives no per-stream duration field here"
    assert stream.end_time == material_end, (
        f"recorded {stream.end_time}s but the file ends at {material_end}s"
    )
    assert stream.end_time != 9, "start_time + duration is not how this family reads it"
    assert stream.bounds_are_conservative, "a muxer's claim is still not a measurement"


def test_zero_origin_matroska_and_mp4_keep_the_measured_end(tmp_path):
    """Regression: the ordinary case must not move, in either family."""
    mkv = make_identity_video(tmp_path, name="zero_origin.mkv", count=100)
    count, last_pts, last_duration = decoded_extent(mkv, "v:0")
    assert (count, last_pts + last_duration) == (100, 4)
    video = probe_file(mkv).metadata.stream(0)
    assert video.origin == 0 and video.end_time == 4
    assert video.bounds_are_conservative  # Matroska: no per-stream duration

    mp4 = make_identity_mp4(tmp_path, name="zero_origin.mp4", count=100)
    count, last_pts, last_duration = decoded_extent(mp4, "v:0")
    assert (count, last_pts + last_duration) == (100, 4)
    video = probe_file(mp4).metadata.stream(0)
    assert video.origin == 0 and video.end_time == 4
    assert not video.bounds_are_conservative, "MP4 records this stream's own duration"


def test_offset_mp4_end_includes_its_own_start(tmp_path):
    """No universal subtraction: in MP4 the duration really is a span.

    Measured for this fixture: container and stream start 2.52, stream duration
    4.0, and 100 pictures from 63/25 to 162/25 with a 1/25 last frame - so the
    material ends at 163/25 and reading that 4.0 as an absolute end would cut
    2.52 s of picture the file really holds.
    """
    path = make_identity_mp4(tmp_path, name="offset.mp4", count=100, offset="2.5")
    count, last_pts, last_duration = decoded_extent(path, "v:0")
    assert count == 100
    assert last_pts == Fraction(162, 25) and last_duration == Fraction(1, 25)

    stream = probe_file(path).metadata.stream(0)
    assert stream.origin == Fraction(63, 25)
    assert stream.duration == 4
    assert stream.end_time == last_pts + last_duration == Fraction(163, 25)
    assert not stream.bounds_are_conservative


def test_per_stream_offsets_bound_each_stream_on_its_own_terms(tmp_path):
    """Picture and sound offset differently end differently; the bounds know that."""
    path = make_per_stream_offset(tmp_path)
    metadata = probe_file(path).metadata
    _, video_end_pts, video_duration = decoded_extent(path, "v:0")
    _, audio_end_pts, audio_duration = decoded_extent(path, "a:0")
    video, audio = metadata.stream(0), metadata.stream(1)
    assert video.end_time == video_end_pts + video_duration
    assert audio.end_time is not None
    assert audio.end_time >= audio_end_pts + audio_duration
    assert audio.end_time - (audio_end_pts + audio_duration) <= Fraction(1, 100), (
        f"sound bound {audio.end_time} vs measured {audio_end_pts + audio_duration}: "
        "a bound may be generous, but not by a second of commentary"
    )
    assert audio.end_time < video.end_time, (
        "the sound ended 1.5 s before the picture; inheriting the container's end "
        "would offer material the file does not hold"
    )
    assert video.bounds_are_conservative and audio.bounds_are_conservative
    assert video.origin == Fraction(63, 25) and audio.origin == 1


def test_variable_rate_material_is_bounded_by_its_last_frames_own_duration(tmp_path):
    """A nominal frame rate is not evidence about a tail.

    Measured for ``make_identity_vfr`` (100 pictures at 25 fps, then 50 at about
    12): the last picture sits at 8083/1000 s and *its own* recorded duration is
    83/1000 s, so the material ends at 4083/500 = 8.166 s. That is also what the
    container and its track tag claim, so the bound is right here - but only
    because the bound is read as an end, not because of any frame rate: multiplying
    the nominal 25/1 onto the last picture would put the tail at 8.123 s, 43 ms
    inside a file that really runs further.
    """
    path = make_identity_vfr(tmp_path)
    count, last_pts, last_duration = decoded_extent(path, "v:0")
    assert count == 150
    assert last_pts == Fraction(8083, 1000) and last_duration == Fraction(83, 1000)
    stream = probe_file(path).metadata.stream(0)
    assert stream.end_time == last_pts + last_duration == Fraction(4083, 500)
    assert stream.bounds_are_conservative
    assert last_pts + Fraction(1, 25) < stream.end_time, (
        "the nominal 25 fps picture length is shorter than the real last frame: "
        "rate is not evidence about a tail"
    )


def test_a_truncated_file_keeps_a_conservative_bound_for_the_render_to_prove(tmp_path):
    """A damaged header is not repaired by the probe; it is marked and measured later.

    Cutting 45% of the offset fixture's bytes leaves a file that still claims to
    run to 13/2 s while only 54 pictures decode. The recorded bound stays the
    claim (an upper bound, marked conservative) rather than being promoted to an
    exact end or silently tightened - the exporter's tail check is what refuses it.
    """
    path = make_truncated_identity_offset(tmp_path, count=100)
    count, last_pts, last_duration = decoded_extent(path, "v:0")
    assert count == 54, f"the cut file should hold part of the picture: {count}"
    material_end = last_pts + last_duration
    assert material_end == Fraction(233, 50)

    stream = probe_file(path).metadata.stream(0)
    assert stream.end_time == Fraction(13, 2)
    assert stream.bounds_are_conservative
    assert stream.end_time > material_end, (
        "the claim is looser than the data, which is exactly why it is marked"
    )
