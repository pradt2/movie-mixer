"""Typed asset and stream metadata model.

Time base — the asset-local presentation-time axis
--------------------------------------------------
All source timestamps are seconds expressed as exact ``Fraction`` values on
the *asset-local presentation-time axis*: the same raw presentation timeline
that ``ffprobe`` reports (``start_time``, ``duration``, ``pts_time``) and
that FFmpeg filter graphs see by default. ``t = 0`` is the zero point of the
container's presentation timeline; streams that begin later carry a nonzero
``start_time`` relative to that origin.

A missing ``start_time`` (common for WAV/MP3 style containers without
timestamp metadata) is defined to be ``0`` on this axis: the first sample is
the origin. This is a documented convention, not fabricated data.

A missing *duration* is genuinely unknown: ``duration`` stays ``None``. Two
things can still bound the end, and they are kept apart deliberately:

* ``end_horizon`` - a **conservative** end read out of the container's Duration
  (with the convention that container family actually uses, and its per-track
  Duration tag where that is tighter). Streams are editable against it, but the
  status survives every hop (``bounds_are_conservative`` ->
  ``StreamSource.conservative_end``) so nothing downstream may treat that number
  as measured. A render that reads up to it proves the tail first, or refuses.
* nothing at all - ``end_time`` is ``None``, and such a stream can be inspected
  but not edited (``editable`` is False with an explanatory note) until reliable
  bounds are determined.

Unknown bounds are never treated as zero, and a conservative bound is never
quietly promoted to an exact one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from pathlib import Path


class StreamKind(Enum):
    VIDEO = "video"
    AUDIO = "audio"
    OTHER = "other"


@dataclass(frozen=True)
class StreamMetadata:
    """One stream of a container, identified by its actual container stream index."""

    stream_index: int
    kind: StreamKind
    codec: str | None
    language: str | None
    #: nominal frame rate from ``avg_frame_rate`` (video); None if unknown/unparseable
    frame_rate: Fraction | None
    #: ``r_frame_rate`` hint (may differ for VFR); None if unknown
    nominal_frame_rate: Fraction | None
    width: int | None
    height: int | None
    sample_rate: int | None
    channels: int | None
    channel_layout: str | None
    #: start on the asset-local axis; ``None`` means the container gave no
    #: start time, which the axis convention defines as 0 (see module docstring)
    start_time: Fraction | None
    #: ``None`` means genuinely unknown — never assume zero
    duration: Fraction | None
    #: container time base for this stream, when known (needed to convert
    #: raw packet/frame ticks to seconds)
    time_base: Fraction | None
    nb_frames: int | None
    #: how ``duration`` was obtained: "probe", "derived" (nb_frames/samples) or None
    duration_source: str | None = None
    #: conservative absolute end bound on the asset-local axis, for a stream whose
    #: own ``duration`` was never recorded: the container's Duration read with the
    #: convention that family actually uses, or that family's per-track Duration
    #: tag when it is tighter (see ``ABSOLUTE_DURATION_FORMATS`` in
    #: :mod:`video_editor.media.ffprobe`). It is a claim the muxer wrote, never a
    #: measurement, so it is used only as an upper bound and stays marked
    #: conservative by ``duration is None``. ``None`` when even this is unknown.
    end_horizon: Fraction | None = None
    # ------------------------------------------------------------------ picture
    #
    # What the container says about *how the picture is made*, recorded verbatim
    # (lower-cased) so the application can say what it cannot do before it
    # produces wrong media. Each of these is ``None`` when the container said
    # nothing, or said "unknown"/"unspecified": an absent value is stored as
    # absent, never as a plausible default. They are recorded for display and
    # for the project document; the render-time capability preflight measures the
    # file it is about to decode instead of trusting what was stored here
    # (see :mod:`video_editor.media.capability`).
    #: ``field_order`` as ffprobe printed it: ``progressive``, one of the field
    #: orders ``tt``/``bb``/``tb``/``bt``, or ``None`` when it was not stated
    field_order: str | None = None
    #: ``color_transfer`` (the EOTF): ``bt709``, ``smpte2084`` (PQ),
    #: ``arib-std-b67`` (HLG), … or ``None`` when not stated
    color_transfer: str | None = None
    #: ``color_primaries`` (the gamut), or ``None`` when not stated
    color_primaries: str | None = None
    #: ``color_space`` (the YCbCr matrix), or ``None`` when not stated
    color_space: str | None = None
    #: ``pix_fmt`` as ffprobe printed it, or ``None`` when not stated
    pix_fmt: str | None = None
    #: names of the side-data records the container reported for this stream,
    #: e.g. ``("Content light level metadata",)``. Empty means none were
    #: reported, which is not proof that none exist.
    side_data: tuple[str, ...] = ()
    #: bits per sample of the decoded picture. Taken from the container's own
    #: ``bits_per_raw_sample`` when it states one, otherwise read off the
    #: ``pix_fmt`` name (``yuv420p10le`` -> 10); ``None`` when neither says.
    #: Sample depth is **not** an HDR indicator: 10-bit SDR is ordinary.
    bit_depth: int | None = None

    @property
    def origin(self) -> Fraction:
        """Effective stream start (missing container start_time means 0 by convention)."""
        return self.start_time if self.start_time is not None else Fraction(0)

    @property
    def end_time(self) -> Fraction | None:
        if self.duration is not None:
            return self.origin + self.duration
        if self.end_horizon is not None and self.end_horizon > self.origin:
            return self.end_horizon
        return None

    @property
    def bounds_known(self) -> bool:
        return self.end_time is not None

    @property
    def bounds_are_conservative(self) -> bool:
        """True when the end bound is only a container-level upper bound."""
        return self.duration is None and self.end_time is not None

    @property
    def bounds_note(self) -> str | None:
        if self.bounds_are_conservative:
            return "length is a container-derived upper bound; the stream may end earlier"
        if not self.bounds_known:
            return "length unknown"
        return None

    @property
    def editable(self) -> bool:
        return self.bounds_known and self.kind in (StreamKind.VIDEO, StreamKind.AUDIO)

    @property
    def editability_note(self) -> str | None:
        """Why this stream cannot be placed on the timeline, if it cannot."""
        if self.kind is StreamKind.OTHER:
            return "Only video and audio streams can be edited onto the timeline."
        if not self.bounds_known:
            return (
                "Duration could not be determined reliably; editing of this "
                "stream is disabled until reliable bounds are available."
            )
        return None


@dataclass(frozen=True)
class AssetMetadata:
    """Probe-derived metadata for one media file."""

    format_name: str | None
    #: overall container duration hint (display only — never substituted for
    #: per-stream durations), None if unknown
    container_duration: Fraction | None
    #: container start hint on the asset-local axis, None if unknown
    container_start: Fraction | None
    streams: tuple[StreamMetadata, ...]

    def stream(self, stream_index: int) -> StreamMetadata | None:
        for s in self.streams:
            if s.stream_index == stream_index:
                return s
        return None

    def position_of(self, stream_index: int) -> int | None:
        """Zero-based position among streams of the same kind (FFmpeg specifier).

        FFmpeg filtergraph inputs address streams as ``input:v:N`` / ``input:a:N``,
        i.e. by type and position, not by absolute container stream index.
        """
        target = self.stream(stream_index)
        if target is None:
            return None
        position = 0
        for s in self.streams:
            if s.stream_index == stream_index:
                return position
            if s.kind is target.kind:
                position += 1
        return None

    @property
    def editable_streams(self) -> tuple[StreamMetadata, ...]:
        return tuple(s for s in self.streams if s.editable)

    @property
    def any_editable(self) -> bool:
        return bool(self.editable_streams)


@dataclass(frozen=True)
class Asset:
    """An imported media asset: stable ID + file identity + probe metadata.

    Media is referenced, never copied or modified. File identity is stat
    based (size + mtime_ns); content hashing is deliberately avoided for
    multi-gigabyte films.
    """

    id: str
    path: Path
    size_bytes: int
    mtime_ns: int
    metadata: AssetMetadata | None
    #: populated when probing failed or was unsupported; metadata is None then
    probe_error: str | None = None
    label: str = field(default="")

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    @property
    def editable(self) -> bool:
        return self.metadata is not None and self.metadata.any_editable

    def display_name(self) -> str:
        return self.label or self.path.name
