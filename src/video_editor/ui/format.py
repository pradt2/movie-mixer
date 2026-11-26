"""Formatting helpers. UI-side rounding happens only here, at the boundary."""

from __future__ import annotations

import math
from fractions import Fraction

from ..media.assets import StreamKind, StreamMetadata
from ..media.capability import (
    hdr_side_data,
    hdr_transfer,
    interlaced_field_order,
    plausible_hdr_without_transfer,
)


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


def _picture_format_parts(stream: StreamMetadata) -> list[str]:
    """How a video stream is coded, in the words the container used.

    Only what the container stated is shown: "not stated" is said as plainly as a
    value is, because a missing field order or transfer is not evidence that the
    picture is progressive or SDR.
    """
    parts: list[str] = []
    if interlaced_field_order(stream.field_order):
        parts.append(f"interlaced (field_order {stream.field_order})")
    elif stream.field_order is None:
        parts.append("field order not stated")
    if hdr_transfer(stream.color_transfer):
        parts.append(f"HDR (color_transfer {stream.color_transfer})")
    elif plausible_hdr_without_transfer(
        stream.color_transfer, stream.color_primaries, stream.color_space, stream.bit_depth
    ):
        parts.append("colour transfer not stated")
    if stream.bit_depth and stream.bit_depth > 8:
        parts.append(f"{stream.bit_depth}-bit")
    return parts


def processing_caveat(stream: StreamMetadata) -> str | None:
    """Why this stream cannot be drawn by this build, if it cannot, in words.

    The media bin shows this so an unusable source says so where it is seen,
    instead of failing later in an export dialog. It never makes the stream
    unusable for import, inspection or listening: only a picture the composition
    would actually show is refused, and that refusal happens at render time.
    """
    if stream.kind is not StreamKind.VIDEO:
        return None
    if interlaced_field_order(stream.field_order):
        return (
            f"Interlaced picture (field_order {stream.field_order}): this project has no "
            "deinterlacer, so this stream cannot be rendered, previewed or stepped frame "
            "by frame. It can still be imported and inspected. Deinterlace the source "
            "into a new file if you want to use its picture."
        )
    named_hdr = hdr_transfer(stream.color_transfer)
    if named_hdr:
        return (
            f"HDR picture (color_transfer {stream.color_transfer}: {named_hdr}): this "
            "project renders SDR yuv420p only and performs no colour or tone conversion, "
            "so this stream cannot be rendered, previewed or stepped frame by frame. It "
            "can still be imported, inspected and used for sound. Tone-map the source "
            "into a new file if you want to use its picture."
        )
    if stream.side_data and hdr_side_data(stream.side_data):
        records = ", ".join(hdr_side_data(stream.side_data))
        return (
            f"Picture carries HDR metadata ({records}): this project renders SDR yuv420p "
            "only and performs no colour or tone conversion, so this stream cannot be "
            "rendered, previewed or stepped frame by frame. It can still be imported and "
            "inspected."
        )
    return None


def render_unusable_note(stream: StreamMetadata) -> str | None:
    """One short clause for the status bar: which stream's picture cannot be drawn.

    Says what can still be done with the file, because import, inspection and the
    sound of an unsupported stream all keep working — only the composition's own
    picture is refused.
    """
    caveat = processing_caveat(stream)
    if caveat is None:
        return None
    if interlaced_field_order(stream.field_order):
        reason = f"interlaced, field_order {stream.field_order}"
    elif hdr_transfer(stream.color_transfer):
        reason = f"HDR, color_transfer {stream.color_transfer}"
    else:
        reason = "carries " + ", ".join(hdr_side_data(stream.side_data))
    return (
        f"picture in stream {stream.stream_index} cannot be rendered ({reason}); "
        "inspection and sound still work"
    )


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
        parts += _picture_format_parts(stream)
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
