"""Actual decoded picture timestamps of one video stream, read in a bounded window.

A packet's ``pts`` is a **seek hint**: the demuxer's answer to "where do I start
reading to get a picture near *t*". It is not evidence that a picture exists, and
it is not the picture the composition shows: a stream can hold packets that never
produce a frame, duplicate a frame onto two timestamps, or put its first decodable
picture somewhere other than the first packet. The composition shows *decoded*
pictures, so :mod:`video_editor.media.frameindex` asks this module, which runs
``ffprobe -show_frames`` — a real decode of the window, one record per picture —
and reports both the stamps and what the read actually proved.

Measured on FFmpeg 8.1.3 (Matroska and MP4, H.264 with real B-frames, FFV1,
constant-rate, variable-rate, offset and byte-truncated fixtures):

* the read **starts earlier than asked**: it seeks to the keyframe at or before
  ``from_time``, so a window asked for ``2.2%4.2`` reports frames from 2.000 s on;
* **its ends are not its bounds.** A read normally stops at ``to_time``, but measured
  behaviour is that it may stop short of it - a 2 s read leaves one or two pictures
  unreported just below ``to_time`` on B-frame and variable-rate material, 0.08 s of
  reorder distance - and it may run past it when the window it was asked for holds
  nothing: asked 4-6 s of a file whose pictures stop at 3.96 s and resume at 7.92 s,
  it reported the picture at 7.92 s as well. Where the window's own bounds do not say
  what was walked, the answer has to come from what was reported;
* **what comes back is the complete enumeration of the ground the read walked**:
  every picture it decoded is in the records, so between the first and the last
  reported stamp nothing is missing. Outside that span nothing is proved. Measured:
  asked 10-12, 20-22 and 38-40 s of a file whose pictures stop at 3.96 s and resume
  at 40.04 s, it reports only 3.00-3.96 s in every case - the same answer it gives at
  the real end of the stream, and no evidence of one;
* asked **past the end of the stream it answers with the tail**: ``20%40`` on a
  6 s clip returned 25 frames spanning 5.000-5.960 s and nothing inside the window
  asked for — the same behaviour ``-show_packets`` shows, and the reason a window
  that found nothing inside is *not* proof that the stream ends there;
* a window entirely **before** the first picture returns no records at all
  (asking ``0%2`` of a file whose presentation starts at 2.5 s), which is proof
  that that span holds no picture;
* ``-show_frames`` no longer prints a per-frame ``time_base``, so every stamp is
  the integer ``best_effort_timestamp`` multiplied by the **stream** ``time_base``
  (``-show_streams``), never ffprobe's printed decimals — a millisecond container
  prints 8.083 for a frame that really sits at 8.0833…;
* record order is presentation order in these fixtures, but nothing guarantees
  it, so the stamps are sorted and deduplicated here.

Both bounds are required: there is deliberately no unbounded form of this probe,
because decoding a whole title to answer one question is the thing this module
exists to prevent.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .framegrab import (
    StillError,
    StreamTiming,
    _ffprobe,
    _run,
    decimal,
    stream_timing,
)

F = Fraction


class FrameProbeError(StillError):
    """The decoded-frame probe could not be run or its answer could not be read."""


@dataclass(frozen=True)
class PictureWindow:
    """One bounded decode of ``[read_from, read_to)`` and what it proved.

    ``stamps`` are the picture starts found. What the read *enumerated* is decided
    by :meth:`enumerated`, because it is not the same as the window that was asked
    for: the read seeks to an earlier keyframe, it stops short of its end when the
    stream's reorder distance does, and it runs on when its window holds nothing.
    """

    read_from: Fraction
    read_to: Fraction
    #: picture starts the decode produced: ascending, deduplicated, asset-local
    stamps: tuple[Fraction, ...]
    #: ``frame`` records seen, before sorting and deduplication
    reported: int
    #: records that had no ``best_effort_timestamp`` and were addressed by ``pts``
    #: instead (measured: FFmpeg fills it in for every picture of these fixtures)
    pts_fallback: int
    #: the stream's own tick size the integer counters were multiplied by
    time_base: Fraction

    @property
    def decoded(self) -> int:
        """Distinct pictures this read produced."""
        return len(self.stamps)

    @property
    def inside(self) -> tuple[Fraction, ...]:
        """The pictures that really lie in the window that was asked for."""
        return tuple(p for p in self.stamps if self.read_from <= p < self.read_to)

    @property
    def proven_from(self) -> Fraction:
        """Earliest time this read enumerated."""
        return min(self.read_from, self.stamps[0]) if self.stamps else self.read_from

    def enumerated(self, claimed_end: Fraction | None) -> tuple[Fraction, Fraction]:
        """The span this read enumerated, and nothing more than that.

        Between the first and last picture it reported the enumeration is complete,
        so a question in there can be answered. Above the last reported picture the
        read is only trusted up to the end it was asked for **when that end lies
        past what the container claims its content is**: such a read had to demux
        the rest of the stream, so its silence is evidence. Any other read that
        came back with pictures stops at its last picture, because measured
        behaviour is that it may stop short of its window (B-frame reorder) or run
        past it (a short hole) and never says which.

        A window that reported nothing at all enumerated its own whole window:
        measured, a window before the first picture of a stream produces no records.
        """
        if not self.stamps:
            return (self.read_from, self.read_to)
        lo = min(self.read_from, self.stamps[0])
        licensed = claimed_end is not None and self.read_to >= claimed_end
        hi = max(self.read_to, self.stamps[-1]) if licensed else self.stamps[-1]
        return (lo, hi)

    @property
    def started_earlier(self) -> bool:
        """The seek landed on a keyframe before the window, so preroll frames came back."""
        return bool(self.stamps) and self.stamps[0] < self.read_from

    @property
    def read_past(self) -> bool:
        """The read produced pictures at or beyond the window's end."""
        return bool(self.stamps) and self.stamps[-1] >= self.read_to

    @property
    def tail_only(self) -> bool:
        """Pictures came back, but none of them were inside the window asked for.

        Measured behaviour at both a stream's end and a presentation hole: the read
        answers with what precedes the window. It proves the window is empty; it
        proves nothing about what lies beyond it.
        """
        return bool(self.stamps) and not self.inside


def picture_stamps(records: Iterable[object], time_base: Fraction) -> tuple[Fraction, ...]:
    """Turn ``ffprobe -show_frames`` records into exact, ascending, unique stamps.

    Kept apart from the subprocess call so the rule can be checked without media:
    integer ``best_effort_timestamp`` times the *stream* time base (never the
    printed ``best_effort_timestamp_time``, which rounds), ``pts`` only as a
    fallback for a record that carries no best-effort stamp, records that are not
    pictures skipped, records with no usable stamp skipped rather than guessed, and
    duplicates collapsed because an index that counts one picture twice will step
    past its neighbour.
    """
    if time_base is None or time_base <= 0:
        return ()
    stamps: list[Fraction] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("media_type") not in (None, "video"):
            continue  # e.g. a subtitle or picture-data record
        raw = record.get("best_effort_timestamp")
        if raw is None or raw == "N/A":
            raw = record.get("pts")
            if raw is None or raw == "N/A":
                continue
        try:
            ticks = int(raw)
        except (TypeError, ValueError):
            continue
        stamps.append(F(ticks) * time_base)
    return tuple(sorted(dict.fromkeys(stamps)))


@dataclass(frozen=True)
class DeclaredEnd:
    """What the container *claims* about where its presentation stops.

    A claim, never a measurement: FFmpeg keeps the duration a file was written
    with even when its bytes stop earlier, and the container's own duration is a
    *per-track* figure in some families. It is therefore taken loosely — the
    later of the container's end and the stream's start plus that duration — so a
    stream's end may be reported as no earlier than the file says it is, and never
    earlier than reality.
    """

    end: Fraction | None
    #: what was read, for reports: None when the container says nothing usable
    detail: str


def declared_end(
    path: Path,
    stream_start: Fraction,
    ffprobe: str | None = None,
    timeout: float = 30,
) -> DeclaredEnd:
    """The container's claimed end of this file, or None when it does not say."""
    tool = _ffprobe(ffprobe)
    proc = _run(
        [tool, "-v", "error", "-show_entries", "format=duration,start_time",
         "-of", "json", str(path)],
        timeout,
        f"read the declared length of {path.name}",
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise FrameProbeError(f"unparseable ffprobe output for {path.name}: {exc}") from exc
    format_fields = payload.get("format", {})
    duration = _exact_time(format_fields.get("duration"))
    if duration is None or duration <= 0:
        return DeclaredEnd(None, f"{path.name}: the container gives no duration")
    start = _exact_time(format_fields.get("start_time")) or F(0)
    end = max(start, stream_start) + duration
    return DeclaredEnd(end, f"container start {start}, duration {duration}")


def _exact_time(value: object) -> Fraction | None:
    """Exact rational from ffprobe's decimal text; 'N/A' and junk are None."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text in {"N/A", "none", "inf", "-inf"}:
        return None
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None


def decoded_picture_times(
    path: Path,
    stream_index: int,
    from_time: Fraction,
    to_time: Fraction,
    ffprobe: str | None = None,
    timeout: float = 60,
    timing: StreamTiming | None = None,
) -> PictureWindow:
    """Decode one bounded window and report the stream's real picture times.

    Times are asset-local presentation seconds — the same axis the domain model,
    the render graph and :func:`video_editor.media.framegrab` use — and packet or
    frame timestamps already sit on it, so no stream ``start_time`` is added here
    (measured for whole-asset ``-output_ts_offset`` and per-stream ``-itsoffset``
    alike: adding it would double the offset).

    *from_time* and *to_time* are both required: an unbounded decode of a title is
    never what a single question needs. Pass the ``StreamTiming`` a caller already
    holds so the metadata probe is not repeated for every window.
    """
    if to_time <= from_time:
        raise FrameProbeError(f"probe window [{float(from_time)}, {float(to_time)}) is empty")
    if from_time < 0:
        raise FrameProbeError(f"probe window starts before the asset axis at {float(from_time)}s")
    timing = timing or stream_timing(path, stream_index, ffprobe, timeout)
    tool = _ffprobe(ffprobe)
    proc = _run(
        [
            tool,
            "-v",
            "error",
            "-select_streams",
            f"v:{timing.position}",
            "-show_entries",
            "frame=media_type,best_effort_timestamp,pts",
            "-of",
            "json",
            # an interval read: it seeks to the keyframe at or before from_time and
            # stops at to_time, so a long title is never decoded
            "-read_intervals",
            f"{decimal(from_time)}%{decimal(to_time)}",
            str(path),
        ],
        timeout,
        f"read the pictures of {path.name} at {float(from_time):.3f}-{float(to_time):.3f}s",
    )
    if proc.returncode != 0:
        raise FrameProbeError(
            f"cannot read the pictures of {path.name}: {proc.stderr.strip()[:300]}"
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise FrameProbeError(f"unparseable ffprobe output for {path.name}: {exc}") from exc
    records = payload.get("frames", [])
    stamps = picture_stamps(records, timing.time_base)
    fallback = sum(
        1
        for record in records
        if isinstance(record, dict)
        and record.get("media_type") in (None, "video")
        and record.get("best_effort_timestamp") in (None, "N/A")
        and record.get("pts") not in (None, "N/A")
    )
    return PictureWindow(
        read_from=from_time,
        read_to=to_time,
        stamps=stamps,
        reported=sum(1 for record in records if isinstance(record, dict)),
        pts_fallback=fallback,
        time_base=timing.time_base,
    )
