"""FFmpeg single-stream preview extraction (used when Qt cannot address a stream)."""

from fractions import Fraction

import pytest

from media_fixtures import HAS_FFMPEG, make_mixed
from video_editor.media.extractor import extract_stream_window
from video_editor.media.ffprobe import probe_file

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg")

F = Fraction
TOL = Fraction(1, 4)


def test_audio_stream_preview_contains_only_that_stream(tmp_path):
    source = make_mixed(tmp_path)  # video + eng 440 Hz + fre 880 Hz
    preview = extract_stream_window(
        source, 2, kind="audio", start=F(0), duration=F(4), out_dir=tmp_path
    )
    assert preview is not None
    result = probe_file(preview.path)
    assert result.ok, result.error
    kinds = [s.kind.value for s in result.metadata.streams]
    assert kinds == ["audio"], "preview must contain exactly the selected stream"
    lang = result.metadata.streams[0].language
    assert lang == "fre"
    duration = result.metadata.streams[0].duration or result.metadata.container_duration
    assert duration is not None and abs(duration - F(4)) <= TOL
    assert preview.window_start == F(0)


def test_video_stream_preview_window_offset(tmp_path):
    source = make_mixed(tmp_path)
    preview = extract_stream_window(
        source, 0, kind="video", start=F(2), duration=F(3), out_dir=tmp_path
    )
    assert preview is not None
    result = probe_file(preview.path)
    assert result.ok, result.error
    kinds = [s.kind.value for s in result.metadata.streams]
    assert kinds == ["video"]
    assert preview.window_start == F(2)
    assert preview.covers(F(3))
    assert not preview.covers(F(5))  # half-open window


def test_missing_source_returns_none(tmp_path):
    assert (
        extract_stream_window(tmp_path / "nope.mkv", 0, kind="audio", start=F(0), duration=F(5))
        is None
    )
