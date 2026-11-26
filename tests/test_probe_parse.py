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


# ------------------------------------------- what a container's duration means
#
# Both documents are the fields ``ffprobe`` 8.1.3 really printed for two of this
# repository's fixtures (raw JSON kept in the TASK_12e record), trimmed to what
# the parser reads. They live in the pure suite so the rule is pinned without
# executables; ``tests/test_probe_integration.py`` re-checks the same numbers
# against the files and against what actually decodes.

#: ``make_identity_offset``: 100 frames of 25 fps, whole presentation shifted by
#: ``-output_ts_offset 2.5``. It decodes 100 pictures from 5/2 to 323/50 s, so the
#: material ends at 13/2 s - while a span reading of the same fields (2.5 + 6.5)
#: claims 9 s.
OFFSET_MATROSKA = {
    "format": {
        "format_name": "matroska,webm",
        "start_time": "2.500000",
        "duration": "6.500000",
    },
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "ffv1",
            "width": 64,
            "height": 48,
            "r_frame_rate": "25/1",
            "avg_frame_rate": "25/1",
            "time_base": "1/1000",
            "start_pts": 2500,
            "start_time": "2.500000",
            "tags": {"ENCODER": "Lavc62.28.103 ffv1", "DURATION": "00:00:06.500000000"},
        }
    ],
}

#: ``make_per_stream_offset``: picture offset by 2.5 s, sound by 1.0 s on one
#: asset axis. Measured ends of the real material: picture 163/25 s, sound
#: 4999/1000 s; the container's own duration (6.52 s) is only the later of them.
PER_STREAM_MATROSKA = {
    "format": {
        "format_name": "matroska,webm",
        "start_time": "1.000000",
        "duration": "6.520000",
    },
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "ffv1",
            "time_base": "1/1000",
            "start_pts": 2520,
            "start_time": "2.520000",
            "avg_frame_rate": "25/1",
            "r_frame_rate": "25/1",
            "tags": {"DURATION": "00:00:06.520000000"},
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "pcm_s16le",
            "sample_rate": "48000",
            "channels": 1,
            "time_base": "1/1000",
            "start_pts": 1000,
            "start_time": "1.000000",
            "tags": {"DURATION": "00:00:05.000000000"},
        },
    ],
}

#: An MP4 written with ``-itsoffset 2.5`` before the input and ``-c copy`` (an
#: offset Matroska remuxed to MP4). Here ``duration`` *is* a span from the
#: stream's own start - the file decodes 5/2 .. 13/2 s - and the stream carries it
#: exactly. ``make_identity_mp4(offset="2.5")`` measures the same convention with
#: different numbers (start 63/25, duration 4.0, material to 163/25).
OFFSET_MP4 = {
    "format": {
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "start_time": "2.500000",
        "duration": "4.000000",
    },
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "ffv1",
            "width": 64,
            "height": 48,
            "time_base": "1/16000",
            "start_pts": 40000,
            "start_time": "2.500000",
            "duration": "4.000000",
            "nb_frames": "100",
            "avg_frame_rate": "25/1",
            "r_frame_rate": "25/1",
        }
    ],
}


def test_matroska_duration_names_an_end_not_a_span_from_the_stream_start():
    """The case that started this: 2.5 + 6.5 invents 2.5 s of picture.

    Measured ground truth for this document: 100 pictures from 5/2 to 323/50, the
    last 1/25 s long, so the file's material ends at 13/2 s.
    """
    meta = parse_probe_document(OFFSET_MATROSKA)
    video = meta.stream(0)
    assert video.origin == Fraction("2.5")
    assert video.duration is None  # no per-stream duration field in this container
    assert video.end_time == Fraction(13, 2)
    # what the container *would* have supported read as a span, recorded here so
    # the difference is visible rather than implied
    assert (meta.container_start or 0) + meta.container_duration == 9
    assert video.end_time != 9
    assert video.bounds_are_conservative  # a claim, not a measurement
    assert video.editable


def test_matroska_track_tag_bounds_each_stream_on_its_own_terms():
    """A 4 s sound under a 6.5 s picture must not inherit the picture's end.

    Measured: the picture's material ends at 163/25 s and the sound's at
    4999/1000 s; the per-track Duration tags (6.52 and 5.0, absolute on the axis
    like the container's) put each just above the reality it belongs to.
    """
    meta = parse_probe_document(PER_STREAM_MATROSKA)
    video, audio = meta.stream(0), meta.stream(1)
    assert video.origin == Fraction("2.52") and video.end_time == Fraction(163, 25)
    assert audio.origin == 1 and audio.end_time == 5
    assert audio.end_time < video.end_time, "the bound is per stream, not per file"
    assert video.bounds_are_conservative and audio.bounds_are_conservative
    assert video.editable and audio.editable


def test_mp4_span_is_kept_as_a_span():
    """The same shift in MP4 means the opposite thing, and must stay that way.

    Subtracting the start here (4.0 instead of 2.5 + 4.0) would refuse the last
    2.5 s of a file that decodes them: measured, this file runs 5/2 .. 13/2.
    """
    meta = parse_probe_document(OFFSET_MP4)
    video = meta.stream(0)
    assert video.duration == 4 and video.duration_source == "probe"
    assert video.end_time == Fraction(13, 2)
    assert not video.bounds_are_conservative, "MP4 carries this stream's own duration"


def test_mp4_without_its_own_duration_is_still_bounded_by_the_span():
    doc = {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "start_time": "2.500000",
            "duration": "4.000000",
        },
        "streams": [
            {"index": 0, "codec_type": "video", "time_base": "1/16000",
             "start_time": "2.500000"},
        ],
    }
    video = parse_probe_document(doc).stream(0)
    assert video.duration is None
    assert video.end_time == Fraction(13, 2)
    assert video.bounds_are_conservative


def test_unmeasured_container_family_keeps_the_looser_span_bound():
    """No folklore about families that were never measured.

    The span reading is kept for ``mpegts``: it is the looser bound, so it can
    make the render path measure a tail, but it cannot refuse material that is in
    the file. Tightening it would need the same kind of measurement the Matroska
    rule has, which has not been done for TS.
    """
    doc = {
        "format": {"format_name": "mpegts", "start_time": "2.500000", "duration": "4.000000"},
        "streams": [{"index": 0, "codec_type": "video", "start_time": "2.500000"}],
    }
    stream = parse_probe_document(doc).stream(0)
    assert stream.end_time == Fraction(13, 2)
    assert stream.bounds_are_conservative


def test_conservative_bound_is_never_written_as_a_measured_duration():
    """The status distinction survives: an upper bound never becomes a length."""
    video = parse_probe_document(OFFSET_MATROSKA).stream(0)
    assert video.duration is None
    assert video.duration_source is None
    assert video.bounds_known and video.bounds_are_conservative
    assert "upper bound" in video.bounds_note


def test_a_timecode_tag_is_parsed_as_an_exact_rational():
    """Matroska prints durations as ``HH:MM:SS.nnnnnnnnn``; no floats involved."""
    doc = {
        "format": {"format_name": "matroska,webm"},
        "streams": [
            {
                "index": 0,
                "codec_type": "audio",
                "sample_rate": "48000",
                "start_time": "1.5",
                "tags": {"DURATION": "00:01:00.500000000"},
            }
        ],
    }
    stream = parse_probe_document(doc).stream(0)
    assert stream.end_time == Fraction(121, 2)  # 60.5, exactly
    assert stream.bounds_are_conservative

