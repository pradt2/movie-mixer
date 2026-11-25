"""Integration tests: real ffprobe against generated fixtures."""

from fractions import Fraction

import pytest

from media_fixtures import (
    HAS_FFMPEG,
    make_audio_only,
    make_garbage,
    make_mixed,
    make_stream_offset,
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
