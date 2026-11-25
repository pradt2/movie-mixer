"""Formatting helpers. UI-side rounding happens only here, at the boundary."""

from __future__ import annotations

import math
from fractions import Fraction

from ..media.assets import StreamKind, StreamMetadata


def format_time(seconds: Fraction | None) -> str:
    """Exact rational seconds -> ``H:MM:SS.mmm`` (display rounding only)."""
    if seconds is None:
        return "unknown"
    scaled = seconds * 1000
    total_ms = math.floor(scaled) + (1 if scaled - math.floor(scaled) >= Fraction(1, 2) else 0)
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours}:{minutes:02d}:{secs:02d}.{millis:03d}"


def format_rate(rate: Fraction | None) -> str:
    """Exact rational rate, shown as a decimal but kept exact internally."""
    if rate is None:
        return "rate unknown"
    value = float(rate)
    if value * 1000 == int(value * 1000):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{value:.4f}".rstrip("0").rstrip(".")


def parse_time_text(text: str) -> Fraction:
    """Parse user time input: ``H:MM:SS[.mmm]``, plain seconds or an exact
    rational such as ``24000/1001``. Raises ValueError with guidance."""
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("empty time")
    if "/" in cleaned or ("." in cleaned and ":" not in cleaned):
        return Fraction(cleaned)
    parts = cleaned.split(":")
    if len(parts) == 1:
        return Fraction(parts[0])
    if len(parts) > 3:
        raise ValueError("expected H:MM:SS, M:SS, SS or a plain number")
    seconds = Fraction(0)
    for i, part in enumerate(reversed(parts)):
        multiplier = 60**i
        seconds += Fraction(part) * multiplier
    if seconds < 0:
        raise ValueError("time must be non-negative")
    return seconds


def stream_details(stream: StreamMetadata) -> str:
    """Human-readable one-line stream description for the media bin."""
    parts: list[str] = [f"stream {stream.stream_index}"]
    if stream.codec:
        parts.append(stream.codec)
    if stream.kind is StreamKind.VIDEO:
        if stream.width and stream.height:
            parts.append(f"{stream.width}x{stream.height}")
        parts.append(format_rate(stream.frame_rate))
        if (
            stream.frame_rate is not None
            and stream.nominal_frame_rate is not None
            and stream.frame_rate != stream.nominal_frame_rate
        ):
            parts.append("(frame rate varies)")
    elif stream.kind is StreamKind.AUDIO:
        if stream.sample_rate:
            parts.append(f"{stream.sample_rate} Hz")
        if stream.channel_layout:
            parts.append(stream.channel_layout)
        elif stream.channels:
            parts.append(f"{stream.channels} ch")
    if stream.language:
        parts.append(f"[{stream.language}]")
    start = stream.origin
    end = stream.end_time
    bounds_note = stream.bounds_note
    if bounds_note == "length unknown":
        parts.append("length unknown")
    else:
        parts.append(f"{format_time(start)} to {format_time(end)}")
        if bounds_note:
            parts.append(f"({bounds_note})")
    return " · ".join(parts)
