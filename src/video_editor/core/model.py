"""Immutable domain records: Project, Track, Clip, StreamSource.

Semantics (binding, from AGENTS.md):

- Tracks are ordered top-first: ``tracks[0]`` is the *top* of the video
  stack. Video: topmost enabled clip wins; uncovered areas are black.
  Audio: all unmuted active tracks are summed; gaps are silent.
- Clips on the same track never overlap (half-open output intervals);
  overlapping on different tracks is intentional.
- ``rate`` is source seconds per output second (positive rational).
  ``output_duration = source_duration / rate``.
- ``StreamSource`` carries the asset-local bounds resolved at import time so
  that the core stays independent of the media layer and snapshots are safe
  for concurrent rendering (everything here is frozen/structurally shared).
- Output end = maximum end of *all* clips (including muted/disabled tracks)
  so toggles do not shift duration; empty composition has duration zero.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from fractions import Fraction

from .timeutils import ZERO, overlaps


class TrackKind(Enum):
    VIDEO = "video"
    AUDIO = "audio"


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class OutputSettings:
    width: int = 1920
    height: int = 1080
    frame_rate: Fraction = Fraction(25)
    sample_rate: int = 48000
    channel_layout: str = "stereo"

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("output dimensions must be positive")
        if self.frame_rate <= 0:
            raise ValueError("output frame rate must be positive")
        if self.sample_rate <= 0:
            raise ValueError("output sample rate must be positive")


@dataclass(frozen=True)
class StreamSource:
    """An imported stream usable as clip material (resolved from probe data)."""

    asset_id: str
    stream_index: int
    kind: TrackKind
    #: asset-local presentation-time origin of this stream's material
    origin: Fraction
    #: asset-local end of the material; ``None`` = unknown -> not placeable
    end: Fraction | None
    #: True when ``end`` is only a container-derived upper bound
    conservative_end: bool = False
    frame_rate: Fraction | None = None
    sample_rate: int | None = None
    channel_layout: str | None = None
    width: int | None = None
    height: int | None = None
    #: container start time of the file this stream came from (its own
    #: presentation-time origin). FFmpeg's input ``-ss`` is relative to this, not
    #: to 0, so rendering needs it; missing container metadata means 0.
    input_start: Fraction = ZERO
    #: zero-based position of this stream **among streams of the same kind** in
    #: its container. ``stream_index`` is the domain identity, but FFmpeg's
    #: filtergraph specifiers address streams by type and position, so rendering
    #: needs both. Unknown (``None``) means the asset was registered before this
    #: was recorded: rendering refuses rather than guessing a different stream.
    stream_position: int | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.asset_id, self.stream_index)

    @property
    def placeable(self) -> bool:
        return self.end is not None and self.end > self.origin


@dataclass(frozen=True)
class Track:
    id: str
    kind: TrackKind
    name: str
    enabled: bool = True
    muted: bool = False


@dataclass(frozen=True)
class Clip:
    id: str
    asset_id: str
    stream_index: int
    track_id: str
    #: asset-local source interval [source_in, source_out)
    source_in: Fraction
    source_out: Fraction
    #: output time where the clip begins (never negative)
    output_start: Fraction
    #: source seconds per output second
    rate: Fraction = Fraction(1)
    #: linear output gain multiplier
    gain: Fraction = Fraction(1)
    #: linear fade durations (output seconds) anchored at the clip edges
    fade_in: Fraction = ZERO
    fade_out: Fraction = ZERO
    #: A/V link group id (editing convenience, never a media restriction)
    link_group: str | None = None

    def __post_init__(self) -> None:
        if self.source_out <= self.source_in:
            raise ValueError("clip source interval must be non-empty")
        if self.output_start < 0:
            raise ValueError("clip output start must be non-negative")
        if self.rate <= 0:
            raise ValueError("clip rate must be positive")
        if self.gain < 0:
            raise ValueError("clip gain must be non-negative")
        if self.fade_in < 0 or self.fade_out < 0:
            raise ValueError("fade durations must be non-negative")

    @property
    def source_duration(self) -> Fraction:
        return self.source_out - self.source_in

    @property
    def output_duration(self) -> Fraction:
        return self.source_duration / self.rate

    @property
    def output_end(self) -> Fraction:
        return self.output_start + self.output_duration

    @property
    def output_interval(self) -> tuple[Fraction, Fraction]:
        return (self.output_start, self.output_end)

    def covers(self, output_time: Fraction) -> bool:
        return self.output_start <= output_time < self.output_end

    def source_time_at(self, output_time: Fraction) -> Fraction:
        """Asset-local source time corresponding to an output time in this clip."""
        return self.source_in + (output_time - self.output_start) * self.rate

    def output_time_for_source(self, source_time: Fraction) -> Fraction:
        """Output time at which ``source_time`` (asset-local) is presented."""
        return self.output_start + (source_time - self.source_in) / self.rate


def envelope_gain(clip: Clip, output_time: Fraction) -> Fraction:
    """Exact linear fade envelope multiplier (0..1) at an output time.

    Fades ramp linearly from the clip edges; the render and preview paths
    must use this same definition.
    """
    if not clip.covers(output_time):
        return ZERO
    elapsed = output_time - clip.output_start
    remaining = clip.output_end - output_time
    mult = Fraction(1)
    if clip.fade_in > 0 and elapsed < clip.fade_in:
        mult *= elapsed / clip.fade_in
    if clip.fade_out > 0 and remaining < clip.fade_out:
        mult *= remaining / clip.fade_out
    return mult


@dataclass(frozen=True)
class Project:
    """Immutable composition snapshot. Commands return new instances."""

    output: OutputSettings = field(default_factory=OutputSettings)
    sources: tuple[StreamSource, ...] = ()
    tracks: tuple[Track, ...] = ()
    clips: tuple[Clip, ...] = ()

    # ------------------------------------------------------------- lookups
    def track(self, track_id: str) -> Track | None:
        for track in self.tracks:
            if track.id == track_id:
                return track
        return None

    def clip(self, clip_id: str) -> Clip | None:
        for clip in self.clips:
            if clip.id == clip_id:
                return clip
        return None

    def source(self, asset_id: str, stream_index: int) -> StreamSource | None:
        for source in self.sources:
            if source.asset_id == asset_id and source.stream_index == stream_index:
                return source
        return None

    def clips_on_track(self, track_id: str) -> tuple[Clip, ...]:
        on_track = (c for c in self.clips if c.track_id == track_id)
        return tuple(sorted(on_track, key=lambda c: c.output_start))

    def linked_clips(self, clip_id: str) -> tuple[Clip, ...]:
        clip = self.clip(clip_id)
        if clip is None or clip.link_group is None:
            return (clip,) if clip else ()
        return tuple(c for c in self.clips if c.link_group == clip.link_group)

    # ------------------------------------------------------------- queries
    def output_end(self) -> Fraction:
        if not self.clips:
            return ZERO
        return max(c.output_end for c in self.clips)

    def video_clip_at(self, output_time: Fraction) -> Clip | None:
        """Winning picture at ``output_time``: topmost enabled track wins."""
        for track in self.tracks:  # tracks[0] is the top of the stack
            if track.kind is not TrackKind.VIDEO or not track.enabled:
                continue
            for clip in self.clips:
                if clip.track_id == track.id and clip.covers(output_time):
                    return clip
        return None

    def audio_clips_at(self, output_time: Fraction) -> tuple[Clip, ...]:
        """All sounding audio contributions at ``output_time``."""
        result = []
        for clip in self.clips:
            track = self.track(clip.track_id)
            if (
                track is not None
                and track.kind is TrackKind.AUDIO
                and track.enabled
                and not track.muted
                and clip.covers(output_time)
            ):
                result.append(clip)
        return tuple(result)

    def clips_in_interval(
        self, start: Fraction, end: Fraction, track_id: str | None = None
    ) -> tuple[Clip, ...]:
        return tuple(
            c
            for c in self.clips
            if (track_id is None or c.track_id == track_id)
            and overlaps(c.output_start, c.output_end, start, end)
        )

    def with_clips(self, clips: tuple[Clip, ...]) -> Project:
        return replace(self, clips=clips)

    def with_tracks(self, tracks: tuple[Track, ...]) -> Project:
        return replace(self, tracks=tracks)
