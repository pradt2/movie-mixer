"""Pure unit tests for the ffprobe JSON parser (no executables needed)."""

from fractions import Fraction

import pytest

from video_editor.media.assets import StreamKind
from video_editor.media.ffprobe import parse_probe_document, parse_probe_output

MINIMAL = {
    "format": {"format_name": "matroska,webm", "duration": "6.000000", "start_time": "0.000000"},
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 128,
            "height": 96,
            "avg_frame_rate": "24000/1001",
            "r_frame_rate": "24000/1001",
            "time_base": "1/1000",
            "start_time": "1.466000",
            "duration": "6.006000",
            "tags": {"language": "eng"},
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "sample_rate": "44100",
            "channels": 2,
            "channel_layout": "stereo",
            "time_base": "1/1000",
            "start_time": "N/A",
            "duration": "5.984000",
            "tags": {"language": "und"},
        },
    ],
}


def test_parses_streams_with_exact_rationals():
    meta = parse_probe_document(MINIMAL)
    video = meta.stream(0)
    audio = meta.stream(1)
    assert video.frame_rate == Fraction(24000, 1001)
    assert video.start_time == Fraction("1.466")
    assert video.duration == Fraction("6.006")
    assert video.end_time == Fraction("1.466") + Fraction("6.006")
    assert video.language == "eng"
    # audio: language 'und' is treated as unknown, not a real language
    assert audio.language is None
    assert audio.sample_rate == 44100
    # missing start_time is *defined* as 0 on the asset-local axis
    assert audio.start_time is None
    assert audio.origin == Fraction(0)
    assert audio.bounds_known
    assert audio.editable


def test_missing_duration_stays_unknown_not_zero():
    doc = {
        "format": {},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "mpeg2video", "start_time": "0.5"},
        ],
    }
    meta = parse_probe_document(doc)
    stream = meta.stream(0)
    assert stream.duration is None
    assert stream.end_time is None
    assert not stream.bounds_known
    assert not stream.editable
    assert "Duration could not be determined" in stream.editability_note
    assert meta.container_duration is None  # never substituted for stream duration


def test_container_end_horizon_makes_mkv_like_streams_editable():
    # MKV/TS containers often omit per-stream durations.
    doc = {
        "format": {"duration": "6.0", "start_time": "0.0"},
        "streams": [
            {"index": 0, "codec_type": "video", "start_time": "0.000000"},
            {"index": 1, "codec_type": "audio", "start_time": "1.000000"},
        ],
    }
    meta = parse_probe_document(doc)
    video, audio = meta.stream(0), meta.stream(1)
    assert video.duration is None
    assert video.end_time == Fraction(6)
    assert audio.end_time == Fraction(6)
    assert video.bounds_are_conservative and audio.bounds_are_conservative
    assert video.editable and audio.editable


def test_duration_derived_from_nb_frames():
    doc = {
        "format": {"duration": "999"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "avg_frame_rate": "25/1",
                "nb_frames": "150",
            }
        ],
    }
    meta = parse_probe_document(doc)
    stream = meta.stream(0)
    assert stream.duration == Fraction(6)
    assert stream.duration_source == "derived:nb_frames/rate"


def test_duration_derived_from_nb_samples_for_audio():
    doc = {
        "format": {},
        "streams": [
            {"index": 0, "codec_type": "audio", "sample_rate": "48000", "nb_samples": "144000"}
        ],
    }
    meta = parse_probe_document(doc)
    assert meta.stream(0).duration == Fraction(3)


def test_zero_over_zero_rate_is_unknown():
    doc = {
        "format": {},
        "streams": [{"index": 0, "codec_type": "video", "avg_frame_rate": "0/0", "duration": "5"}],
    }
    meta = parse_probe_document(doc)
    assert meta.stream(0).frame_rate is None


def test_nonzero_start_and_time_base_preserved():
    meta = parse_probe_document(MINIMAL)
    video = meta.stream(0)
    assert video.time_base == Fraction(1, 1000)
    assert video.origin == Fraction("1.466")


def test_unsupported_stream_kinds_flagged():
    doc = {
        "format": {},
        "streams": [
            {"index": 0, "codec_type": "subtitle", "codec_name": "subrip", "duration": "6"},
            {"index": 1, "codec_type": "data", "codec_name": "unknown"},
        ],
    }
    meta = parse_probe_document(doc)
    assert all(s.kind is StreamKind.OTHER for s in meta.streams)
    assert all("Only video and audio" in s.editability_note for s in meta.streams)
    assert not meta.any_editable


def test_malformed_documents_rejected():
    for bad in ([], {}, {"streams": "nope"}, {"streams": []}, {"streams": [42]}):
        with pytest.raises(ValueError):
            parse_probe_document(bad)


def test_malformed_json_returns_none():
    assert parse_probe_output("{not json") is None


def test_structurally_bad_json_returns_error_result():
    result = parse_probe_output('{"streams": "broken"}')
    assert result is not None and not result.ok
    assert "Unusable probe data" in result.error
