"""Peak envelopes of an audio stream, for the timeline only.

An envelope belongs to one *asset stream* on the asset-local time axis. The
timeline places it from the clip's own in-point, rate and output start, so the
shape a clip shows is the shape of the material that clip actually uses: trim
it, retime it or move it and the same samples are drawn differently. Nothing
here participates in rendering, gain or fades — this is a picture of the sound.

The measurement is done by FFmpeg (``asetnsamples`` regroups the stream into
exactly ``bucket``-sized frames and ``astats`` reports each frame's peak), so
peaks are real sample peaks at the stream's own rate, and a request costs a
line of text per bucket rather than a copy of the audio. The number of buckets
is bounded, and only the requested window is ever decoded: a two-hour film
zoomed out to an overview costs one pass over the visible part.

When a caller asks for an overview so wide that decoding the native rate would
be wasteful, it may pass ``sample_rate`` to decode a decimated copy instead.
That low-pass filters the material, so very short peaks can be missed; the
envelope records the rate it was measured at, and the timeline says which. If
the material cannot be decoded at all, extraction raises and the timeline draws
the clip without a waveform: a waveform is never invented, and measured silence
is drawn as silence because it was measured.
"""

from __future__ import annotations

import contextlib
import math
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ..core.model import StreamSource, TrackKind
from ..core.timeutils import ZERO
from .framegrab import decimal

#: buckets per request, so a strip stays a picture rather than a dataset
MAX_BUCKETS = 1200
#: one extraction pass covers at most this much material
MAX_WINDOW_SECONDS = 30 * 60
#: linear peaks at or below this count as measured silence
SILENCE = 1e-4

PEAK_KEY = "lavfi.astats.Overall.Peak_level"
PTS_KEY = "lavfi.astats.Overall.pts_time"

CancelCheck = Callable[[], bool]


class WaveformError(Exception):
    """No envelope could be produced; the message says why."""


class Cancelled(Exception):
    """The caller asked for this extraction to stop."""


@dataclass(frozen=True)
class Envelope:
    """Absolute peak per bucket across ``[start, start + duration)``."""

    #: asset-local time of the first bucket
    start: Fraction
    #: what the envelope actually covers (may be shorter than requested)
    duration: Fraction
    #: exact bucket width: ``bucket_samples / sample_rate``
    bucket: Fraction
    #: the rate the material was measured at (native unless decimated on request)
    sample_rate: int
    #: linear peaks (0..1 for full-scale material; may exceed 1 if it is hot)
    peaks: tuple[float, ...]
    #: (asset id, stream index) — what this picture is a picture *of*
    source_key: tuple[str, int]
    #: True when peaks come from a decimated copy and can miss short transients
    decimated: bool = False

    @property
    def count(self) -> int:
        return len(self.peaks)

    @property
    def end(self) -> Fraction:
        return self.start + self.duration

    @property
    def is_silent(self) -> bool:
        """Measured silence (a reading, not a missing waveform)."""
        return all(peak <= SILENCE for peak in self.peaks)

    @property
    def peak_level(self) -> float:
        return max(self.peaks, default=0.0)

    @property
    def nbytes(self) -> int:
        return len(self.peaks) * 8 + 192

    def peak_at(self, at: Fraction) -> float | None:
        """Peak of the bucket covering an asset-local time, None if outside."""
        if self.bucket <= 0 or not self.start <= at < self.end:
            return None
        index = int((at - self.start) // self.bucket)
        return self.peaks[index] if index < len(self.peaks) else None


def bucket_samples_for(rate: int, bucket: Fraction) -> int:
    """Whole samples per bucket at ``rate`` (the bucket is then exact)."""
    if rate <= 0:
        raise WaveformError("measuring needs a sample rate")
    if bucket <= 0:
        raise WaveformError("an envelope needs a positive bucket width")
    samples = max(1, round(rate * bucket))
    if samples > rate * 60:
        raise WaveformError("a bucket may not span more than a minute of audio")
    return samples


def audio_envelope(
    path: Path,
    source: StreamSource,
    *,
    start: Fraction,
    duration: Fraction,
    bucket: Fraction,
    sample_rate: int | None = None,
    cancel: CancelCheck | None = None,
    timeout: float = 180.0,
) -> Envelope:
    """Measure the peak envelope of ``[start, start + duration)`` of a stream.

    Raises :class:`WaveformError` when the request lies outside the stream's
    known material, is too large, or cannot be decoded, and :class:`Cancelled`
    when ``cancel`` reports that the caller no longer wants the result.
    """
    if source.kind is not TrackKind.AUDIO:
        raise WaveformError(
            f"stream {source.stream_index} of {source.asset_id} is not an audio stream"
        )
    if source.stream_position is None:
        raise WaveformError(
            f"stream {source.stream_index} of {source.asset_id} has no recorded position "
            "in its container; refusing to address a different stream"
        )
    if duration <= 0:
        raise WaveformError("an envelope needs a positive duration")
    if bucket <= 0:
        raise WaveformError("an envelope needs a positive bucket width")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise WaveformError("ffmpeg is not available")

    window_start = max(ZERO, source.origin, start)
    window_end = window_start + duration
    if source.end is not None:
        window_end = min(window_end, source.end)
    if window_end <= window_start:
        raise WaveformError(
            f"[{start}, {start + duration}) of stream {source.stream_index} lies outside "
            f"its material [{source.origin}, {source.end})"
        )
    window = window_end - window_start
    if window > MAX_WINDOW_SECONDS:
        raise WaveformError(
            f"{window} s exceeds the {MAX_WINDOW_SECONDS} s window of one extraction"
        )

    native = source.sample_rate or 0
    rate = sample_rate or native
    if rate <= 0:
        raise WaveformError(
            f"stream {source.stream_index} of {source.asset_id} has no known sample rate"
        )
    decimated = bool(sample_rate and native and sample_rate != native)
    samples = bucket_samples_for(rate, bucket)
    exact_bucket = Fraction(samples, rate)
    wanted_buckets = -(-window // exact_bucket)  # ceiling division
    if wanted_buckets > MAX_BUCKETS:
        raise WaveformError(
            f"{wanted_buckets} buckets exceeds the limit of {MAX_BUCKETS}; "
            "widen the bucket or shorten the window"
        )

    # FFmpeg's input -ss is relative to the container's own start, not to 0
    input_seek = max(window_start - source.input_start, ZERO)
    # an overview's decimation belongs *inside* the chain: as an output option it
    # would be applied after the measurement and the recorded rate would lie
    stages: list[str] = []
    if decimated:
        stages.append(f"aresample={rate}")
    stages.append(f"asetnsamples=n={samples}:p=0")
    stages.append("astats=metadata=1:reset=1")
    stages.append(f"ametadata=mode=print:key={PEAK_KEY}:file=-")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", decimal(input_seek),
        "-i", str(path),
        "-t", decimal(window),
        "-map", f"0:a:{source.stream_position}",
        "-vn", "-ac", "1",
        "-af", ",".join(stages),
        "-f", "null", "-",
    ]

    peaks: list[float] = []
    starts: list[Fraction] = []
    status = 0
    stderr_text = ""
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise WaveformError(f"could not start ffmpeg: {exc}") from exc
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if cancel is not None and cancel():
                _stop(process)
                raise Cancelled("envelope extraction cancelled")
            if line.startswith(PEAK_KEY):
                peaks.append(_peak(line))
            elif line.startswith("frame:"):
                starts.append(_frame_time(line))
            if len(peaks) > MAX_BUCKETS:
                _stop(process)
                raise WaveformError(
                    f"more than {MAX_BUCKETS} buckets arrived; refusing to keep reading"
                )
        status = process.wait(timeout=10)
        if process.stderr is not None:
            stderr_text = process.stderr.read().strip()
    except subprocess.TimeoutExpired:
        _stop(process)
        raise WaveformError("ffmpeg did not finish measuring the audio window") from None
    except (OSError, ValueError) as exc:
        _stop(process)
        raise WaveformError(f"could not read the measurement: {exc}") from exc
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()

    if status != 0:
        raise WaveformError(
            f"ffmpeg could not decode stream {source.stream_index} of {source.asset_id}"
            + (f": {stderr_text[-300:]}" if stderr_text else "")
        )
    if not peaks:
        raise WaveformError(
            f"no audio was measured for [{window_start}, {window_end}) of stream "
            f"{source.stream_index}"
        )
    # the envelope's place on the timeline is what FFmpeg says it measured, not
    # what the request assumed
    covered = len(peaks) * exact_bucket
    measured_starts = [value for value in starts if value is not None]
    if measured_starts and len(measured_starts) == len(peaks):
        if measured_starts[0] > exact_bucket:
            raise WaveformError(
                f"measurement started at {measured_starts[0]} s, not at the start of the "
                "window; the envelope cannot be placed where the timeline expects"
            )
        covered = measured_starts[-1] + exact_bucket
    return Envelope(
        start=window_start,
        duration=min(window, covered),
        bucket=exact_bucket,
        sample_rate=rate,
        peaks=tuple(peaks),
        source_key=(source.asset_id, source.stream_index),
        decimated=decimated,
    )


def _frame_time(line: str) -> Fraction | None:
    """The ``pts_time`` of a printed frame, as an exact rational."""
    for part in line.split():
        if part.startswith("pts_time:"):
            text = part.split(":", 1)[1]
            try:
                return Fraction(text)
            except ValueError:
                return None  # 'N/A'
    return None


def _peak(line: str) -> float:
    """astats peak in dB, converted to a linear magnitude ('-inf' is silence)."""
    text = line.split("=", 1)[1].strip()
    try:
        decibels = float(text)
    except ValueError:
        return 0.0  # '-inf' / 'nan': nothing was measured in that bucket
    if math.isnan(decibels) or decibels == float("-inf"):
        return 0.0
    return 10.0 ** (decibels / 20.0)


def _stop(process: subprocess.Popen) -> None:
    """Stop an extraction nobody wants any more; never leave a process behind."""
    try:
        process.terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        with contextlib.suppress(OSError):
            process.kill()
