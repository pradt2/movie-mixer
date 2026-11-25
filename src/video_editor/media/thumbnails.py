"""Small pictures for the timeline: one bounded FFmpeg pass per requested strip.

A strip is a row of thumbnails sampled on a regular grid of *asset-local* time:
frame *i* stands for the picture in effect at ``start + i * interval``. The
timeline places it with the clip's own in-point, rate and output start, so a
trimmed or retimed clip shows the material it actually uses, and the grid
spacing follows the current zoom rather than the source frame rate — a two-hour
film at a glance is a few dozen pictures, not two hundred thousand.

Each frame is a real decoded picture. FFmpeg's ``fps`` filter chooses the source
frame in effect at each grid point, which on variable-frame-rate material is not
exactly on the grid; the strip reports the grid, and the timeline is a
navigation aid, not a frame-accurate viewer (that is what still-frame stepping
is for). Nothing is drawn for a stream that cannot be decoded, and a strip that
runs out early says so instead of repeating the last picture.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ..core.model import StreamSource, TrackKind
from ..core.timeutils import ZERO
from .framegrab import decimal
from .waveform import CancelCheck, Cancelled

#: a strip is a handful of small pictures, never a frame dump
MAX_FRAMES = 120
MAX_BYTES = 6 << 20
#: thumbnail boxes stay small whatever the timeline is wide
MAX_WIDTH = 240
MAX_HEIGHT = 135

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


class ThumbnailError(Exception):
    """No strip could be produced; the message says why."""


@dataclass(frozen=True)
class ThumbStrip:
    """JPEG payloads sampled on ``start + i * interval``."""

    #: asset-local time the first thumbnail represents
    start: Fraction
    #: exact grid spacing
    interval: Fraction
    width: int
    height: int
    frames: tuple[bytes, ...]
    #: True when the grid continued but extraction stopped at a limit
    truncated: bool
    source_key: tuple[str, int]

    @property
    def count(self) -> int:
        return len(self.frames)

    @property
    def end(self) -> Fraction:
        """Asset-local time covered by the frames that really arrived."""
        return self.start + self.count * self.interval

    @property
    def nbytes(self) -> int:
        return sum(len(frame) for frame in self.frames) + 128

    def time_at(self, index: int) -> Fraction:
        return self.start + index * self.interval

    def frame_at(self, at: Fraction) -> bytes | None:
        """Thumbnail representing an asset-local time, None if outside."""
        if not self.frames or self.interval <= 0 or not self.start <= at < self.end:
            return None
        index = int((at - self.start) // self.interval)
        return self.frames[index] if index < len(self.frames) else None


def video_strip(
    path: Path,
    source: StreamSource,
    *,
    start: Fraction,
    duration: Fraction,
    interval: Fraction,
    width: int,
    height: int,
    cancel: CancelCheck | None = None,
    timeout: float = 120.0,
) -> ThumbStrip:
    """Decode thumbnails of ``[start, start + duration)`` on an ``interval`` grid.

    Raises :class:`WaveformError`-style :class:`ThumbnailError` when the request
    is outside the stream's known material or FFmpeg cannot decode it, and
    :class:`Cancelled` when ``cancel`` says the caller no longer wants it.
    """
    if source.kind is not TrackKind.VIDEO:
        raise ThumbnailError(
            f"stream {source.stream_index} of {source.asset_id} is not a video stream"
        )
    if source.stream_position is None:
        raise ThumbnailError(
            f"stream {source.stream_index} of {source.asset_id} has no recorded position "
            "in its container; refusing to address a different stream"
        )
    if duration <= 0 or interval <= 0:
        raise ThumbnailError("a strip needs a positive duration and interval")
    if width <= 0 or height <= 0:
        raise ThumbnailError("a strip needs a positive picture size")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ThumbnailError("ffmpeg is not available")

    window_start = max(ZERO, source.origin, start)
    window_end = window_start + duration
    if source.end is not None:
        window_end = min(window_end, source.end)
    if window_end <= window_start:
        raise ThumbnailError(
            f"[{start}, {start + duration}) of stream {source.stream_index} lies outside "
            f"its material [{source.origin}, {source.end})"
        )
    window = window_end - window_start
    wanted = -(-window // interval)  # ceiling division
    if wanted > MAX_FRAMES:
        raise ThumbnailError(
            f"{wanted} thumbnails exceeds the limit of {MAX_FRAMES}; "
            "widen the interval or shorten the window"
        )

    box_w = max(1, min(MAX_WIDTH, width))
    box_h = max(1, min(MAX_HEIGHT, height))
    # FFmpeg's input -ss is relative to the container's own start, not to 0
    input_seek = max(window_start - source.input_start, ZERO)
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", decimal(input_seek),
        "-i", str(path),
        "-t", decimal(window),
        "-map", f"0:v:{source.stream_position}",
        "-an",
        "-vf", f"fps={_rate_text(interval)},scale={box_w}:{box_h}",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "6", "-",
    ]
    frames: list[bytes] = []
    truncated = False
    status = 0
    stderr_text = ""
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0
        )
    except OSError as exc:
        raise ThumbnailError(f"could not start ffmpeg: {exc}") from exc
    buffer = b""
    try:
        while True:
            if cancel is not None and cancel():
                _stop(process)
                raise Cancelled("thumbnail extraction cancelled")
            chunk = process.stdout.read(1 << 16)
            if not chunk:
                break
            buffer += chunk
            frames.extend(_complete_jpegs(buffer))
            buffer = _tail(buffer)
            if len(frames) >= MAX_FRAMES:
                truncated = len(frames) > wanted or _has_more(buffer, process)
                frames = frames[:MAX_FRAMES]
                _stop(process)
                break
            if sum(len(frame) for frame in frames) > MAX_BYTES:
                truncated = True
                _stop(process)
                raise ThumbnailError(
                    f"thumbnails exceeded {MAX_BYTES} bytes; refusing to keep decoding"
                )
        if status == 0:
            status = process.wait(timeout=10)
        if process.stderr is not None:
            stderr_text = process.stderr.read().decode("utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        _stop(process)
        raise ThumbnailError("ffmpeg did not finish decoding the pictures") from None
    except (OSError, ValueError) as exc:
        _stop(process)
        raise ThumbnailError(f"could not read the decoded pictures: {exc}") from exc
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    if status != 0 and not frames:
        raise ThumbnailError(
            f"ffmpeg could not decode stream {source.stream_index} of {source.asset_id}"
            + (f": {stderr_text[-300:]}" if stderr_text else "")
        )
    if buffer.strip():
        truncated = True  # a partial picture at the limit was dropped
    if not frames:
        raise ThumbnailError(
            f"no pictures were decoded for [{window_start}, {window_end}) of stream "
            f"{source.stream_index}"
        )
    return ThumbStrip(
        start=window_start,
        interval=interval,
        width=box_w,
        height=box_h,
        frames=tuple(frames),
        truncated=truncated,
        source_key=(source.asset_id, source.stream_index),
    )


def _rate_text(interval: Fraction) -> str:
    """``fps=`` filter value: one sample per ``interval`` seconds, exactly."""
    rate = Fraction(1) / interval
    return f"{rate.numerator}/{rate.denominator}"


def _complete_jpegs(buffer: bytes) -> list[bytes]:
    """Every whole JPEG in *buffer*, in order."""
    found: list[bytes] = []
    position = buffer.find(SOI)
    while position != -1:
        end = buffer.find(EOI, position)
        if end == -1:
            break
        found.append(buffer[position:end + len(EOI)])
        position = buffer.find(SOI, end + len(EOI))
    return found


def _tail(buffer: bytes) -> bytes:
    """Keep only what could still become a frame."""
    if not buffer:
        return b""
    last_start = buffer.rfind(SOI)
    last_end = buffer.rfind(EOI)
    if last_end != -1 and (last_start == -1 or last_end > last_start):
        return b""
    if last_start != -1:
        return buffer[last_start:]
    return b""


def _has_more(buffer: bytes, process: subprocess.Popen) -> bool:
    """True when output is still arriving at the moment the limit was hit."""
    return bool(buffer) or process.poll() is None


def _stop(process: subprocess.Popen) -> None:
    """Stop an extraction nobody wants any more; never leave a process behind."""
    try:
        process.terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        with contextlib.suppress(OSError):
            process.kill()
