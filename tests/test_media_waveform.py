"""Integration tests: real FFmpeg envelope extraction (no user films)."""

import time
from fractions import Fraction

import pytest

from media_fixtures import (
    HAS_FFMPEG,
    make_garbage,
    make_silent_and_tone_streams,
    make_tone,
    make_tone_window,
    source_from_probe,
)
from video_editor.core.model import StreamSource, TrackKind
from video_editor.media.waveform import (
    MAX_BUCKETS,
    MAX_WINDOW_SECONDS,
    Cancelled,
    WaveformError,
    audio_envelope,
    bucket_samples_for,
)

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg required")

F = Fraction

#: one output frame per bucket: fine enough to place a cut by eye
BUCKET = F(1, 25)
#: these synthetic tones sit around -18 dBFS, so "loud" is relative to that
LOUD = 0.05
QUIET = 1e-3


def test_a_tone_measures_loud_and_silence_is_a_measurement_too(tmp_path):
    """Loud where the tone is, quiet where it is not, at the right asset-local time."""
    path = make_tone_window(tmp_path, "gap.mkv", 660, start=2, end=4, total=6)
    source = source_from_probe(path, stream_index=1)
    assert source.kind is TrackKind.AUDIO

    envelope = audio_envelope(path, source, start=F(0), duration=F(6), bucket=BUCKET)
    assert envelope.source_key == (source.asset_id, source.stream_index)
    assert envelope.start == F(0)
    # the bucket is what was actually measured, as an exact rational of the rate
    assert envelope.bucket == Fraction(bucket_samples_for(envelope.sample_rate, BUCKET),
                                       envelope.sample_rate)
    assert abs(envelope.bucket - BUCKET) <= Fraction(1, envelope.sample_rate)
    assert envelope.count == pytest.approx(6 / envelope.bucket, abs=2)

    loud = [at for at in (F(9, 4), F(5, 2), F(7, 2), F(15, 4)) if envelope.peak_at(at) > LOUD]
    quiet = [at for at in (F(1, 4), F(1), F(5), F(11, 2)) if envelope.peak_at(at) < QUIET]
    assert len(loud) == 4, [str(at) for at in loud]
    assert len(quiet) == 4, [str(at) for at in quiet]
    assert not envelope.is_silent
    assert envelope.peak_level / QUIET > 50


def test_the_edge_of_the_sound_sits_where_the_media_says_it_does(tmp_path):
    """Bucket index times bucket width recovers the tone's start, exactly."""
    path = make_tone_window(tmp_path, "edge.mkv", 440, start=2, end=4, total=6)
    source = source_from_probe(path, stream_index=1)
    envelope = audio_envelope(path, source, start=F(0), duration=F(6), bucket=BUCKET)

    first_loud = next(i for i, peak in enumerate(envelope.peaks) if peak > LOUD)
    onset = envelope.start + first_loud * envelope.bucket
    assert abs(onset - F(2)) <= envelope.bucket, f"onset {onset}, bucket {envelope.bucket}"
    last_loud = max(i for i, peak in enumerate(envelope.peaks) if peak > LOUD)
    offset = envelope.start + (last_loud + 1) * envelope.bucket
    assert abs(offset - F(4)) <= 2 * envelope.bucket


def test_extraction_addresses_the_stream_by_position_not_by_guess(tmp_path):
    """A file with a loud stream and a silent one: each position measures its own."""
    path = make_silent_and_tone_streams(tmp_path, "two_audio.mkv", frequency=880)
    loud = source_from_probe(path, stream_index=1)
    silent = source_from_probe(path, stream_index=2)
    assert (loud.stream_position, silent.stream_position) == (0, 1)

    loud_envelope = audio_envelope(path, loud, start=F(0), duration=F(4), bucket=BUCKET)
    silent_envelope = audio_envelope(path, silent, start=F(0), duration=F(4), bucket=BUCKET)
    assert loud_envelope.peak_level > LOUD
    assert silent_envelope.is_silent, silent_envelope.peaks[:10]
    assert loud_envelope.source_key != silent_envelope.source_key


def test_measured_material_is_clipped_to_the_stream_itself(tmp_path):
    """Asking past the end of the stream shortens the window; it never invents it."""
    path = make_tone(tmp_path, "tone.wav", 660, seconds=3)
    source = source_from_probe(path, stream_index=0)
    envelope = audio_envelope(path, source, start=F(2), duration=F(10), bucket=BUCKET)
    assert envelope.duration <= source.end - F(2) + BUCKET
    assert envelope.end <= source.end + BUCKET


def test_a_decimated_overview_says_it_is_decimated(tmp_path):
    """An overview may be measured on a decimated copy — and records that it was."""
    path = make_tone_window(tmp_path, "gap2.mkv", 660, start=2, end=4, total=6)
    source = source_from_probe(path, stream_index=1)
    envelope = audio_envelope(
        path, source, start=F(0), duration=F(6), bucket=F(1, 10), sample_rate=4000
    )
    assert envelope.sample_rate == 4000
    assert envelope.decimated
    assert envelope.bucket == F(1, 10)
    assert envelope.peak_at(F(3)) > LOUD and envelope.peak_at(F(1)) < QUIET


def test_a_video_stream_is_refused_rather_than_guessed(tmp_path):
    path = make_tone_window(tmp_path, "gap.mkv", 660, start=1, end=2, total=3)
    video = source_from_probe(path, stream_index=0)
    with pytest.raises(WaveformError, match="not an audio stream"):
        audio_envelope(path, video, start=F(0), duration=F(1), bucket=BUCKET)


def test_an_unrecorded_stream_position_is_refused(tmp_path):
    """Addressing has to be known: guessing could measure a different soundtrack."""
    path = make_tone(tmp_path, "tone.wav", 660, seconds=3)
    source = source_from_probe(path, stream_index=0)
    unknown = StreamSource(**{
        **{name: getattr(source, name) for name in source.__dataclass_fields__},
        "stream_position": None,
    })
    with pytest.raises(WaveformError, match="no recorded position"):
        audio_envelope(path, unknown, start=F(0), duration=F(1), bucket=BUCKET)


def test_material_that_is_not_there_is_refused(tmp_path):
    path = make_tone(tmp_path, "tone.wav", 660, seconds=3)
    source = source_from_probe(path, stream_index=0)
    with pytest.raises(WaveformError, match="outside"):
        audio_envelope(path, source, start=source.end + F(1), duration=F(1), bucket=BUCKET)


def test_the_request_size_is_bounded(tmp_path):
    path = make_tone(tmp_path, "tone.wav", 660, seconds=6)
    source = source_from_probe(path, stream_index=0)
    tiny = Fraction(1, 4000)  # 24 000 buckets for six seconds
    assert -(-F(6) // tiny) > MAX_BUCKETS
    with pytest.raises(WaveformError, match="buckets exceeds the limit"):
        audio_envelope(path, source, start=F(0), duration=F(6), bucket=tiny)


def test_an_absurd_window_is_refused_before_anything_runs(tmp_path):
    """A stream with unknown end must not turn into a whole-film measurement."""
    source = StreamSource("unknown", 0, TrackKind.AUDIO, F(0), None,
                          sample_rate=48000, stream_position=0)
    nowhere = tmp_path / "never-read.wav"
    with pytest.raises(WaveformError, match="exceeds the"):
        audio_envelope(nowhere, source, start=F(0),
                       duration=F(MAX_WINDOW_SECONDS * 4), bucket=F(1))
    assert not nowhere.exists()


def test_a_bucket_spanning_minutes_is_refused():
    with pytest.raises(WaveformError, match="more than a minute"):
        bucket_samples_for(48000, F(600))


def test_a_file_that_is_not_media_is_reported(tmp_path):
    """Nothing is decoded: the failure is reported, an empty envelope is not."""
    path = make_garbage(tmp_path)
    source = StreamSource("broken", 0, TrackKind.AUDIO, F(0), F(10),
                          sample_rate=48000, stream_position=0)
    with pytest.raises(WaveformError, match=r"could not decode|no audio was measured"):
        audio_envelope(path, source, start=F(0), duration=F(1), bucket=BUCKET)


def test_cancelling_immediately_produces_nothing(tmp_path):
    path = make_tone(tmp_path, "long.wav", 440, seconds=60)
    source = source_from_probe(path, stream_index=0)
    calls: list[int] = []

    def cancel() -> bool:
        calls.append(1)
        return True

    with pytest.raises(Cancelled):
        audio_envelope(path, source, start=F(0), duration=F(60), bucket=F(1, 10),
                       cancel=cancel)
    assert calls, "the cancel predicate was never consulted"


def test_cancelling_midway_stops_promptly(tmp_path):
    path = make_tone(tmp_path, "long2.wav", 440, seconds=120)
    source = source_from_probe(path, stream_index=0)
    seen: list[int] = []

    def cancel() -> bool:
        seen.append(1)
        return len(seen) > 200  # let a little arrive, then stop

    started = time.monotonic()
    with pytest.raises(Cancelled):
        audio_envelope(path, source, start=F(0), duration=F(120), bucket=F(1, 5),
                       cancel=cancel)
    assert time.monotonic() - started < 20, "cancellation did not stop the extraction"
