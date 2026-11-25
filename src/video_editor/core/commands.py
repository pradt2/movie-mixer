"""Atomic, undoable edit commands over immutable projects.

Every command validates fully before producing a new project: an invalid
command raises ``CommandError`` and changes nothing (transactions are
atomic). Linked clips (``link_group``) participate in split/trim/move/rate
operations as a unit: the operation either applies to every member or is
rejected outright — never partially applied. Independent edits are always
possible after unlinking.

Fade policy (no silent envelope jumps):
- Fades are linear ramps anchored at clip edges, measured in output seconds.
- An edit may not cut *through* a fade region: if it would, the command is
  rejected with an explanation (trim/split outside the fade, or shorten the
  fade first).
- ``fade_in + fade_out`` must never exceed the clip's output duration.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum
from fractions import Fraction
from itertools import pairwise

from .model import Clip, OutputSettings, Project, StreamSource, Track, TrackKind, new_id
from .timeutils import ZERO, overlaps


class CommandError(Exception):
    """The command is invalid for the current project; nothing changed."""


class Command:
    """Base class: subclasses implement :meth:`apply`."""

    def apply(self, project: Project) -> Project:  # pragma: no cover - interface
        raise NotImplementedError

    def describe(self) -> str:  # pragma: no cover - interface
        return self.__class__.__name__


# --------------------------------------------------------------------- helpers


def _require_track(project: Project, track_id: str) -> Track:
    track = project.track(track_id)
    if track is None:
        raise CommandError(f"No such track: {track_id}")
    return track


def _require_clip(project: Project, clip_id: str) -> Clip:
    clip = project.clip(clip_id)
    if clip is None:
        raise CommandError(f"No such clip: {clip_id}")
    return clip


def _require_source(project: Project, asset_id: str, stream_index: int) -> StreamSource:
    source = project.source(asset_id, stream_index)
    if source is None:
        raise CommandError(
            f"Stream {stream_index} of asset {asset_id} is not registered in the project"
        )
    if not source.placeable:
        raise CommandError(
            f"Stream {stream_index} has unknown or empty bounds and cannot be edited"
        )
    return source


def _check_clip_bounds(source: StreamSource, source_in: Fraction, source_out: Fraction) -> None:
    if source_out <= source_in:
        raise CommandError("Clip source interval must be non-empty")
    if source_in < source.origin:
        raise CommandError(
            f"Source in ({source_in}) is before the stream origin ({source.origin})"
        )
    if source.end is not None and source_out > source.end:
        raise CommandError(
            f"Source out ({source_out}) exceeds the stream end ({source.end})"
        )


def _check_placement(
    project: Project,
    track_id: str,
    interval: tuple[Fraction, Fraction],
    ignore_ids: frozenset[str] = frozenset(),
) -> None:
    start, end = interval
    if start < 0:
        raise CommandError("Output start must be non-negative")
    for clip in project.clips:
        if clip.track_id == track_id and clip.id not in ignore_ids and overlaps(
            clip.output_start, clip.output_end, start, end
        ):
            raise CommandError(
                f"Overlaps clip {clip.id} on track {track_id}; clips on one track "
                "must not overlap (use separate tracks)"
            )


def _check_fades_fit(clip: Clip) -> None:
    if clip.fade_in + clip.fade_out > clip.output_duration:
        raise CommandError(
            "Fade durations exceed the clip; shorten the fades or lengthen the clip"
        )


def _linked_group(project: Project, clip_id: str) -> tuple[Clip, ...]:
    clip = _require_clip(project, clip_id)
    if clip.link_group is None:
        return (clip,)
    return tuple(c for c in project.clips if c.link_group == clip.link_group)


def _replace_clips(project: Project, removed: set[str], added: tuple[Clip, ...]) -> Project:
    kept = tuple(c for c in project.clips if c.id not in removed)
    return project.with_clips(kept + added)


# --------------------------------------------------------------------- sources


@dataclass(frozen=True)
class RegisterSourceCommand(Command):
    """Make a probed stream available as clip material (part of import)."""

    source: StreamSource

    def apply(self, project: Project) -> Project:
        existing = project.source(self.source.asset_id, self.source.stream_index)
        if existing == self.source:
            return project
        if existing is not None:
            raise CommandError(
                f"Stream {self.source.stream_index} of asset {self.source.asset_id} "
                "is already registered with different properties"
            )
        return replace(project, sources=(*project.sources, self.source))

    def describe(self) -> str:
        return f"register stream {self.source.stream_index} of {self.source.asset_id}"


# --------------------------------------------------------------------- tracks


@dataclass(frozen=True)
class AddTrackCommand(Command):
    kind: TrackKind
    name: str | None = None
    index: int | None = None  # insert position; None = append at bottom/end

    def apply(self, project: Project) -> Project:
        if self.index is not None and not 0 <= self.index <= len(project.tracks):
            raise CommandError(f"Track index {self.index} out of range")
        name = self.name
        if name is None:
            count = sum(1 for t in project.tracks if t.kind is self.kind) + 1
            name = f"{self.kind.value.capitalize()} {count}"
        track = Track(id=new_id(), kind=self.kind, name=name)
        tracks = list(project.tracks)
        position = len(tracks) if self.index is None else self.index
        tracks.insert(position, track)
        return project.with_tracks(tuple(tracks))

    def describe(self) -> str:
        return f"add {self.kind.value} track"


@dataclass(frozen=True)
class RemoveTrackCommand(Command):
    """Deletes a track and its clips. Populated tracks need explicit intent —
    the UI must confirm; the command (and its undo) is always available."""

    track_id: str

    def apply(self, project: Project) -> Project:
        _require_track(project, self.track_id)
        tracks = tuple(t for t in project.tracks if t.id != self.track_id)
        clips = tuple(c for c in project.clips if c.track_id != self.track_id)
        return replace(project, tracks=tracks, clips=clips)

    def describe(self) -> str:
        return "remove track"


@dataclass(frozen=True)
class ReorderTrackCommand(Command):
    track_id: str
    new_index: int

    def apply(self, project: Project) -> Project:
        _require_track(project, self.track_id)
        if not 0 <= self.new_index < len(project.tracks):
            raise CommandError(f"Track index {self.new_index} out of range")
        tracks = [t for t in project.tracks if t.id != self.track_id]
        track = project.track(self.track_id)
        assert track is not None
        tracks.insert(self.new_index, track)
        return project.with_tracks(tuple(tracks))

    def describe(self) -> str:
        return "reorder track"


@dataclass(frozen=True)
class RenameTrackCommand(Command):
    track_id: str
    name: str

    def apply(self, project: Project) -> Project:
        _require_track(project, self.track_id)
        if not self.name.strip():
            raise CommandError("Track name must not be empty")
        tracks = tuple(
            replace(t, name=self.name.strip()) if t.id == self.track_id else t
            for t in project.tracks
        )
        return project.with_tracks(tracks)

    def describe(self) -> str:
        return "rename track"


@dataclass(frozen=True)
class SetTrackEnabledCommand(Command):
    track_id: str
    enabled: bool

    def apply(self, project: Project) -> Project:
        _require_track(project, self.track_id)
        tracks = tuple(
            replace(t, enabled=self.enabled) if t.id == self.track_id else t
            for t in project.tracks
        )
        return project.with_tracks(tracks)

    def describe(self) -> str:
        return "toggle track enable"


@dataclass(frozen=True)
class SetTrackMutedCommand(Command):
    track_id: str
    muted: bool

    def apply(self, project: Project) -> Project:
        track = _require_track(project, self.track_id)
        if track.kind is not TrackKind.AUDIO:
            raise CommandError("Only audio tracks have a mute state")
        tracks = tuple(
            replace(t, muted=self.muted) if t.id == self.track_id else t for t in project.tracks
        )
        return project.with_tracks(tracks)

    def describe(self) -> str:
        return "toggle track mute"


# --------------------------------------------------------------------- clips


@dataclass(frozen=True)
class AddClipCommand(Command):
    asset_id: str
    stream_index: int
    track_id: str
    output_start: Fraction
    source_in: Fraction | None = None  # default: stream origin
    source_out: Fraction | None = None  # default: stream end
    rate: Fraction = Fraction(1)
    clip_id: str | None = None  # caller may fix the id (tests, future restore); else new

    def apply(self, project: Project) -> Project:
        if self.output_start < 0:
            raise CommandError("Output start must be non-negative")
        track = _require_track(project, self.track_id)
        source = _require_source(project, self.asset_id, self.stream_index)
        if source.kind is not track.kind:
            raise CommandError(
                f"Stream is {source.kind.value} but track {track.name} is {track.kind.value}"
            )
        source_in = self.source_in if self.source_in is not None else source.origin
        source_out = self.source_out if self.source_out is not None else source.end
        assert source_out is not None
        _check_clip_bounds(source, source_in, source_out)
        if self.rate <= 0:
            raise CommandError("Clip rate must be positive")
        clip = Clip(
            id=self.clip_id or new_id(),
            asset_id=self.asset_id,
            stream_index=self.stream_index,
            track_id=self.track_id,
            source_in=source_in,
            source_out=source_out,
            output_start=self.output_start,
            rate=self.rate,
        )
        _check_placement(project, self.track_id, clip.output_interval)
        return project.with_clips((*project.clips, clip))

    def describe(self) -> str:
        return "add clip"


# ------------------------------------------------------------- placement


class TrackChoice(Enum):
    """How a placement picks a track when the caller did not name one.

    ``ALLOCATE`` reuses the first compatible track that is free for the new
    clip's interval (what placing one stream should do — a 2 h film does not
    need 800 tracks). ``FRESH`` always creates a new track, which is what a
    group import has always done: each stream of a group gets its own lane so
    that later independent edits cannot collide.
    """

    ALLOCATE = "allocate"
    FRESH = "fresh"


@dataclass(frozen=True)
class StreamPlacement:
    """One stream's intended contribution: material, interval and destination.

    ``source`` is registered when the project does not have it yet; a different
    record under the same (asset, stream index) is refused rather than quietly
    substituted, because stream metadata is what every existing clip's bounds
    were validated against.
    """

    source: StreamSource
    output_start: Fraction
    source_in: Fraction | None = None  # None: whole stream from its origin
    source_out: Fraction | None = None
    rate: Fraction = Fraction(1)
    track_id: str | None = None  # None: chosen by the command's track policy


def group_placements(
    sources: Sequence[StreamSource], output_start: Fraction
) -> tuple[StreamPlacement, ...]:
    """Placements that keep a set of streams in each other's company.

    Each stream's material starts at its own origin and the earliest origin
    anchors the group at *output_start*, so a picture and the sound recorded
    with it keep exactly the offset the file gives them. Used by
    :class:`ImportGroupCommand` and by the UI's "place these streams" action:
    one rule for group alignment, in one place.
    """
    if not sources:
        raise CommandError("A group needs at least one stream")
    anchor = min(source.origin for source in sources)
    return tuple(
        StreamPlacement(
            source=source,
            output_start=output_start + (source.origin - anchor),
            source_in=source.origin,
            source_out=source.end,
        )
        for source in sources
    )


def _register_source(project: Project, source: StreamSource) -> Project:
    """Add *source* to the project, or confirm the identical one is there."""
    existing = project.source(source.asset_id, source.stream_index)
    if existing is None:
        return RegisterSourceCommand(source).apply(project)
    if existing != source:
        raise CommandError(
            f"Stream {source.stream_index} of asset {source.asset_id} is already "
            "registered with different recorded bounds or format; the project's own "
            "record is used. Relink the asset to change it."
        )
    return project


def _track_slot_name(project: Project, kind: TrackKind) -> str:
    return f"{kind.value.capitalize()} {sum(1 for t in project.tracks if t.kind is kind) + 1}"


def _interval_is_free(project: Project, track_id: str, start: Fraction, end: Fraction) -> bool:
    return not any(
        clip.track_id == track_id and overlaps(clip.output_start, clip.output_end, start, end)
        for clip in project.clips
    )


def _choose_track(
    project: Project, placement: StreamPlacement, choice: TrackChoice
) -> tuple[Project, str]:
    """Resolve the destination track, adding one to the *candidate* if needed.

    Returns the candidate project (which may carry a new track) and the track
    id. Nothing is published here: if a later placement in the same action
    fails, the whole candidate — new tracks included — is thrown away.
    """
    if placement.track_id is not None:
        track = _require_track(project, placement.track_id)
        if track.kind is not placement.source.kind:
            raise CommandError(
                f"Target track {track.name} is {track.kind.value}, "
                f"stream {placement.source.stream_index} is {placement.source.kind.value}"
            )
        return project, track.id
    if choice is TrackChoice.ALLOCATE:
        source = placement.source
        source_start = (
            placement.source_in if placement.source_in is not None else source.origin
        )
        source_end = placement.source_out if placement.source_out is not None else source.end
        # an unknown stream end cannot be allocated around; the clip command
        # below rejects it anyway, so the interval here is only a best guess
        duration = ZERO if source_end is None else (source_end - source_start) / placement.rate
        for track in project.tracks:
            if track.kind is placement.source.kind and _interval_is_free(
                project, track.id, placement.output_start, placement.output_start + duration
            ):
                return project, track.id
    fresh = Track(
        id=new_id(),
        kind=placement.source.kind,
        name=_track_slot_name(project, placement.source.kind),
    )
    return project.with_tracks((*project.tracks, fresh)), fresh.id


def _place_streams(
    project: Project,
    placements: Sequence[StreamPlacement],
    *,
    choice: TrackChoice = TrackChoice.ALLOCATE,
    link: bool = False,
    ripple: bool = False,
) -> Project:
    """Register, allocate tracks and create clips on one candidate project.

    The candidate is returned only when every step succeeded, so a refusal
    leaves no orphan registration, no unused track and no half-placed group.
    """
    if not placements:
        raise CommandError("Placement needs at least one stream")
    working = project
    created: list[str] = []
    for placement in placements:
        working = _register_source(working, placement.source)
        working, track_id = _choose_track(working, placement, choice)
        clip_id = new_id()
        if ripple:
            working = InsertClipCommand(
                asset_id=placement.source.asset_id,
                stream_index=placement.source.stream_index,
                track_id=track_id,
                output_start=placement.output_start,
                source_in=placement.source_in,
                source_out=placement.source_out,
                rate=placement.rate,
                clip_id=clip_id,
            ).apply(working)
        else:
            working = AddClipCommand(
                asset_id=placement.source.asset_id,
                stream_index=placement.source.stream_index,
                track_id=track_id,
                output_start=placement.output_start,
                source_in=placement.source_in,
                source_out=placement.source_out,
                rate=placement.rate,
                clip_id=clip_id,
            ).apply(working)
        created.append(clip_id)
    if link and len(created) > 1:
        group = new_id()
        working = working.with_clips(
            tuple(
                replace(clip, link_group=group) if clip.id in created else clip
                for clip in working.clips
            )
        )
    return working


@dataclass(frozen=True)
class PlaceStreamsCommand(Command):
    """Place one or more streams as a single intentional action.

    One user gesture — "place this selection", "drop here", "insert here" — can
    need a stream registration, a new track and a clip. Doing that as three
    commands means three undo steps and, when placement is refused, leftovers
    behind. This command computes everything on a candidate and publishes it
    once, so one undo reverses the whole action and a refusal changes nothing.
    """

    placements: tuple[StreamPlacement, ...]
    #: what to do when a placement does not name a track
    track_choice: TrackChoice = TrackChoice.ALLOCATE
    #: group the created clips so they move/split as a unit
    link: bool = False
    #: ripple later clips of the target track right (an insertion)
    ripple: bool = False

    def apply(self, project: Project) -> Project:
        return _place_streams(
            project, self.placements, choice=self.track_choice, link=self.link,
            ripple=self.ripple,
        )

    def describe(self) -> str:
        verb = "insert" if self.ripple else "place"
        if len(self.placements) == 1:
            return f"{verb} stream"
        return f"{verb} {len(self.placements)} streams"


@dataclass(frozen=True)
class ImportGroupCommand(Command):
    """Atomic grouped placement of several streams of one asset.

    Each stream's material starts at its own origin; clips are offset on the
    output timeline by their stream origins, so A/V relative offsets are
    preserved exactly. Missing track ids create fresh tracks.
    """

    asset_id: str
    stream_indices: tuple[int, ...]
    output_start: Fraction
    track_ids: tuple[str | None, ...] = ()  # parallel to stream_indices; None = new track

    def apply(self, project: Project) -> Project:
        if not self.stream_indices:
            raise CommandError("Group import needs at least one stream")
        if self.track_ids and len(self.track_ids) != len(self.stream_indices):
            raise CommandError("track_ids must be parallel to stream_indices")
        sources = [_require_source(project, self.asset_id, idx) for idx in self.stream_indices]
        placements = tuple(
            replace(placement, track_id=requested)
            for placement, requested in zip(
                group_placements(sources, self.output_start), self._track_slots(), strict=True
            )
        )
        return _place_streams(project, placements, choice=TrackChoice.FRESH)

    def _track_slots(self) -> tuple[str | None, ...]:
        if not self.track_ids:
            return (None,) * len(self.stream_indices)
        return self.track_ids

    def describe(self) -> str:
        return f"group-import {len(self.stream_indices)} streams"


@dataclass(frozen=True)
class RemoveClipsCommand(Command):
    """Delete the selected clips exactly (no ripple, no link propagation)."""

    clip_ids: tuple[str, ...]

    def apply(self, project: Project) -> Project:
        for clip_id in self.clip_ids:
            _require_clip(project, clip_id)
        wanted = set(self.clip_ids)
        clips = tuple(c for c in project.clips if c.id not in wanted)
        return project.with_clips(clips)

    def describe(self) -> str:
        return f"remove {len(self.clip_ids)} clip(s)"


@dataclass(frozen=True)
class DuplicateClipCommand(Command):
    """Copy one clip to an explicit destination (independent new clip)."""

    clip_id: str
    output_start: Fraction
    track_id: str | None = None  # default: same track

    def apply(self, project: Project) -> Project:
        if self.output_start < 0:
            raise CommandError("Duplicate destination must be non-negative")
        clip = _require_clip(project, self.clip_id)
        track_id = self.track_id or clip.track_id
        track = _require_track(project, track_id)
        if project.track(clip.track_id) is not None:
            source_kind = _require_source(project, clip.asset_id, clip.stream_index).kind
            if source_kind is not track.kind:
                raise CommandError("Duplicate target track kind does not match the stream")
        clone = replace(clip, id=new_id(), track_id=track_id, output_start=self.output_start)
        _check_placement(project, track_id, clone.output_interval)
        return project.with_clips((*project.clips, clone))

    def describe(self) -> str:
        return "duplicate clip"


@dataclass(frozen=True)
class DuplicateClipsCommand(Command):
    """Duplicate a block of clips by a uniform output-time delta (atomic).

    New clips get fresh ids; links inside the block are regrouped so the
    duplicated pair links to itself, not to the originals.
    """

    clip_ids: tuple[str, ...]
    delta: Fraction

    def apply(self, project: Project) -> Project:
        if not self.clip_ids:
            raise CommandError("Duplicate needs at least one clip")
        originals = [_require_clip(project, cid) for cid in self.clip_ids]
        if self.delta == 0:
            raise CommandError("Duplicate destination equals the source position")
        clones: list[Clip] = []
        link_remap: dict[str, str] = {}
        for clip in originals:
            new_start = clip.output_start + self.delta
            if new_start < 0:
                raise CommandError("Duplicate destination must be non-negative")
            new_link = None
            if clip.link_group is not None:
                new_link = link_remap.setdefault(clip.link_group, new_id())
            clone = replace(
                clip, id=new_id(), output_start=new_start, link_group=new_link
            )
            clones.append(clone)
        staging = project.with_clips((*project.clips, *clones))
        for clone in clones:
            _check_placement(staging, clone.track_id, clone.output_interval,
                             ignore_ids=frozenset(c.id for c in clones) | {clone.id})
        return staging

    def describe(self) -> str:
        return f"duplicate {len(self.clip_ids)} clip(s)"


@dataclass(frozen=True)
class InsertClipCommand(Command):
    """Explicit ripple-insert: place a clip on the track and shift *later
    clips of that same track* right by its output duration.

    This ripple is explicit (the command name says insert), confined to the
    target track, and never touches other tracks or gaps before the insert.
    """

    asset_id: str
    stream_index: int
    track_id: str
    output_start: Fraction
    source_in: Fraction | None = None
    source_out: Fraction | None = None
    rate: Fraction = Fraction(1)
    clip_id: str | None = None

    def apply(self, project: Project) -> Project:
        if self.output_start < 0:
            raise CommandError("Insert position must be non-negative")
        track = _require_track(project, self.track_id)
        source = _require_source(project, self.asset_id, self.stream_index)
        if source.kind is not track.kind:
            raise CommandError(
                f"Stream is {source.kind.value} but track {track.name} is {track.kind.value}"
            )
        source_in = self.source_in if self.source_in is not None else source.origin
        source_out = self.source_out if self.source_out is not None else source.end
        assert source_out is not None
        _check_clip_bounds(source, source_in, source_out)
        if self.rate <= 0:
            raise CommandError("Clip rate must be positive")
        clip = Clip(
            id=self.clip_id or new_id(),
            asset_id=self.asset_id,
            stream_index=self.stream_index,
            track_id=self.track_id,
            source_in=source_in,
            source_out=source_out,
            output_start=self.output_start,
            rate=self.rate,
        )
        length = clip.output_duration
        for other in project.clips:
            # A ripple can only move clips that start at or after the insert
            # point. A clip covering that point would be sliced by the insert,
            # which is not something we do silently.
            if (
                other.track_id == self.track_id
                and other.output_start < self.output_start < other.output_end
            ):
                raise CommandError(
                    f"Insert point {self.output_start} lies inside clip {other.id} "
                    f"on track {track.name}; split it first or insert at a boundary"
                )
        shifted: dict[str, Clip] = {}
        for other in project.clips:
            if other.track_id == self.track_id and other.output_start >= self.output_start:
                moved = replace(other, output_start=other.output_start + length)
                # linked members on *other* tracks move together (atomic block)
                shifted[other.id] = moved
                if other.link_group is not None:
                    for mate in project.clips:
                        if mate.link_group == other.link_group and mate.id not in shifted:
                            shifted[mate.id] = replace(
                                mate, output_start=mate.output_start + length
                            )
        changed = project.with_clips(
            (*[shifted.get(c.id, c) for c in project.clips], clip)
        )
        _check_placement(changed, self.track_id, clip.output_interval,
                         ignore_ids=frozenset(shifted) | {clip.id})
        for candidate in shifted.values():
            # linked partners may be shifted onto tracks with other material:
            # verify against everything that did not move
            _check_placement(
                changed,
                candidate.track_id,
                candidate.output_interval,
                ignore_ids=frozenset(shifted),
            )
        return changed

    def describe(self) -> str:
        return "insert clip (ripple on target track)"


@dataclass(frozen=True)
class MoveClipCommand(Command):
    """Move in time and/or to a compatible track. No ripple: other clips,
    gaps included, do not move. Linked groups move together in time."""

    clip_id: str
    output_start: Fraction | None = None
    track_id: str | None = None

    def apply(self, project: Project) -> Project:
        clip = _require_clip(project, self.clip_id)
        if self.output_start is None and self.track_id is None:
            raise CommandError("Move needs a new output start and/or track")
        group = _linked_group(project, self.clip_id)
        if self.track_id is not None and len(group) > 1:
            raise CommandError(
                "Cannot change tracks for a linked clip; unlink first for "
                "independent editing"
            )
        if self.track_id is not None:
            target = _require_track(project, self.track_id)
            source = _require_source(project, clip.asset_id, clip.stream_index)
            if source.kind is not target.kind:
                raise CommandError(
                    f"Cannot move {source.kind.value} clip onto {target.kind.value} track"
                )

        if len(group) == 1:
            new_start = self.output_start if self.output_start is not None else clip.output_start
            new_track = self.track_id or clip.track_id
            if new_start < 0:
                raise CommandError("Move would place the clip before time 0")
            moved = replace(clip, output_start=new_start, track_id=new_track)
            candidates = {clip.id: moved}
        else:
            if self.output_start is None:
                raise CommandError("Linked clips need a common new output start")
            delta = self.output_start - clip.output_start
            candidates = {}
            for member in group:
                if member.output_start + delta < 0:
                    raise CommandError("Move would place a linked clip before time 0")
                candidates[member.id] = replace(member, output_start=member.output_start + delta)

        updated = project.with_clips(
            tuple(candidates.get(c.id, c) for c in project.clips)
        )
        for clip_id, candidate in candidates.items():
            original = project.clip(clip_id)
            assert original is not None
            if candidate.output_start < 0:
                raise CommandError(f"Move would place clip {clip_id} before time 0")
            _check_placement(
                updated,
                candidate.track_id,
                candidate.output_interval,
                ignore_ids=frozenset(candidates),
            )
        if updated == project:
            return project
        return updated

    def describe(self) -> str:
        return "move clip"


@dataclass(frozen=True)
class SplitClipCommand(Command):
    """Split one clip (or an entire linked group) at an output time.

    Source split position = source_in + (split_output_time - output_start) *
    rate, computed exactly. The *left* piece keeps the original clip id and the
    original link group; the right pieces of the whole group share **one new**
    group id. That is what makes the result usable: after splitting an A/V pair
    you can move or split the right-hand pair without dragging the left-hand
    pair along with it, and a second split of the right pair is not refused
    because the pieces no longer all contain the split point.

    A group of one normalizes to unlinked (`link_group=None`): a "group" with a
    single member constrains nothing, and leaving a phantom id behind invites a
    later clip to be linked to something the user never linked.

    All linked members must strictly contain the split time or the whole command
    is rejected. A split may not fall inside a fade region.
    """

    clip_id: str
    output_time: Fraction

    def apply(self, project: Project) -> Project:
        group = _linked_group(project, self.clip_id)
        # every right-hand piece of this split belongs to one new group; a lone
        # clip has nothing to be linked with, so both halves end up unlinked
        right_group = new_id() if len(group) > 1 else None
        left_group = group[0].link_group if len(group) > 1 else None
        replacements: dict[str, tuple[Clip, Clip]] = {}
        for member in group:
            t = self.output_time
            if not (member.output_start < t < member.output_end):
                raise CommandError(
                    f"Split time is outside clip {member.id}; linked clips split as a unit"
                    if len(group) > 1
                    else "Split time must lie strictly inside the clip"
                )
            source_split = member.source_in + (t - member.output_start) * member.rate
            left = replace(
                member,
                source_out=source_split,
                fade_out=ZERO,
                link_group=left_group,
            )
            right = replace(
                member,
                id=new_id(),
                source_in=source_split,
                output_start=t,
                fade_in=ZERO,
                link_group=right_group,
            )
            if member.fade_in > 0:
                if member.fade_in > left.output_duration:
                    raise CommandError("Split would cut through the fade-in; split outside it")
                left = replace(left, fade_in=member.fade_in)
            if member.fade_out > 0:
                if member.fade_out > right.output_duration:
                    raise CommandError("Split would cut through the fade-out; split outside it")
                right = replace(right, fade_out=member.fade_out)
            _check_fades_fit(left)
            _check_fades_fit(right)
            replacements[member.id] = (left, right)

        kept = []
        for clip in project.clips:
            if clip.id in replacements:
                left, right = replacements[clip.id]
                kept.extend((left, right))
            else:
                kept.append(clip)
        return project.with_clips(tuple(kept))

    def describe(self) -> str:
        return "split clip"


class TrimEdge:
    LEFT = "left"
    RIGHT = "right"


@dataclass(frozen=True)
class TrimClipCommand(Command):
    """Trim one edge of a clip (or of every linked member at the same output
    time) to a new asset-local source time.

    - Left trim preserves the original output end and source out.
    - Right trim preserves output start and source in.
    - The edge may move inward *or* outward (reclaiming material) but only
      within known source bounds; it may never cross the opposite edge.
    - For linked groups the trim boundary (mapped through the anchor) must
      lie strictly inside every member — linked material is never extended
      as a side effect, and incompatible members reject the transaction.
    - Trimming *through* a fade region is rejected (no silent envelope
      jumps); trimming to the fade edge or beyond is allowed.
    """

    clip_id: str
    edge: str  # TrimEdge.LEFT / RIGHT
    source_time: Fraction

    def apply(self, project: Project) -> Project:
        if self.edge not in (TrimEdge.LEFT, TrimEdge.RIGHT):
            raise CommandError("Trim edge must be left or right")
        anchor = _require_clip(project, self.clip_id)
        source = _require_source(project, anchor.asset_id, anchor.stream_index)
        if self.source_time < source.origin or (
            source.end is not None and self.source_time > source.end
        ):
            raise CommandError(
                f"Trim target {self.source_time} is outside source bounds "
                f"[{source.origin}, {source.end if source.end is not None else 'unknown'}]"
            )

        group = _linked_group(project, self.clip_id)
        updated: dict[str, Clip] = {}
        if len(group) > 1:
            boundary = anchor.output_time_for_source(self.source_time)
            for member in group:
                if not (member.output_start < boundary < member.output_end):
                    raise CommandError(
                        "Linked trims may not extend material: trim boundary must lie "
                        f"strictly inside every linked clip (clip {member.id} does not)"
                    )
                updated[member.id] = self._trim_member(
                    member, boundary, member.source_time_at(boundary)
                )
        else:
            member = group[0]
            if self.edge == TrimEdge.LEFT:
                if not (source.origin <= self.source_time < member.source_out):
                    raise CommandError(
                        "Trim target would empty the clip or cross its far edge"
                    )
            else:
                if not (member.source_in < self.source_time):
                    raise CommandError(
                        "Trim target would empty the clip or cross its far edge"
                    )
            boundary = member.output_time_for_source(self.source_time)
            updated[member.id] = self._trim_member(member, boundary, self.source_time)

        changed = project.with_clips(tuple(updated.get(c.id, c) for c in project.clips))
        for candidate in updated.values():
            if candidate.output_start < 0:
                raise CommandError("Trim extension would push the clip before time 0")
            _check_placement(
                changed,
                candidate.track_id,
                candidate.output_interval,
                ignore_ids=frozenset(updated),
            )
        return changed

    def _trim_member(self, member: Clip, boundary: Fraction, member_source: Fraction) -> Clip:
        if self.edge == TrimEdge.LEFT:
            if member.fade_in > 0 and (
                member.output_start < boundary < member.output_start + member.fade_in
            ):
                raise CommandError("Trim would cut through the fade-in; trim outside it")
            new_start = member.output_end - (member.source_out - member_source) / member.rate
            if new_start < 0:
                raise CommandError("Trim extension would push the clip before time 0")
            candidate = replace(member, source_in=member_source, output_start=new_start)
        else:
            if member.fade_out > 0 and (
                member.output_end - member.fade_out < boundary < member.output_end
            ):
                raise CommandError("Trim would cut through the fade-out; trim outside it")
            candidate = replace(member, source_out=member_source)
        _check_fades_fit(candidate)
        return candidate

    def describe(self) -> str:
        return f"trim {self.edge}"


@dataclass(frozen=True)
class SetRateCommand(Command):
    """Change playback rate; source interval and output start stay fixed.

    For linked groups every member gets the same rate (e.g. speed change
    with pitch-corrected audio). Fades must still fit; overlap is validated
    per track. Source material is untouched.
    """

    clip_id: str
    rate: Fraction

    def apply(self, project: Project) -> Project:
        if self.rate <= 0:
            raise CommandError("Rate must be a positive rational")
        group = _linked_group(project, self.clip_id)
        updated = {member.id: replace(member, rate=self.rate) for member in group}
        for candidate in updated.values():
            _check_fades_fit(candidate)
        changed = project.with_clips(tuple(updated.get(c.id, c) for c in project.clips))
        for candidate in updated.values():
            _check_placement(
                changed,
                candidate.track_id,
                candidate.output_interval,
                ignore_ids=frozenset(updated),
            )
        return changed

    def describe(self) -> str:
        return f"set rate {self.rate}"


@dataclass(frozen=True)
class SetGainCommand(Command):
    clip_id: str
    gain: Fraction

    def apply(self, project: Project) -> Project:
        clip = _require_clip(project, self.clip_id)
        if self.gain < 0:
            raise CommandError("Gain must be non-negative")
        updated = tuple(
            replace(c, gain=self.gain) if c.id == clip.id else c for c in project.clips
        )
        return project.with_clips(updated)

    def describe(self) -> str:
        return f"set gain {self.gain}"


@dataclass(frozen=True)
class SetFadesCommand(Command):
    clip_id: str
    fade_in: Fraction
    fade_out: Fraction

    def apply(self, project: Project) -> Project:
        clip = _require_clip(project, self.clip_id)
        if self.fade_in < 0 or self.fade_out < 0:
            raise CommandError("Fade durations must be non-negative")
        candidate = replace(clip, fade_in=self.fade_in, fade_out=self.fade_out)
        _check_fades_fit(candidate)
        updated = tuple(
            replace(c, fade_in=self.fade_in, fade_out=self.fade_out) if c.id == clip.id else c
            for c in project.clips
        )
        return project.with_clips(updated)

    def describe(self) -> str:
        return "set fades"


# --------------------------------------------------------------------- links


@dataclass(frozen=True)
class LinkClipsCommand(Command):
    """Group clips (typically one video + one audio) as an editing unit."""

    clip_ids: tuple[str, ...]

    def apply(self, project: Project) -> Project:
        if len(self.clip_ids) < 2:
            raise CommandError("Linking needs at least two clips")
        if len(set(self.clip_ids)) != len(self.clip_ids):
            raise CommandError("Duplicate clip ids in link")
        clips = [_require_clip(project, cid) for cid in self.clip_ids]
        for clip in clips:
            if clip.link_group is not None:
                raise CommandError(
                    f"Clip {clip.id} is already linked; unlink before relinking"
                )
        kinds = set()
        for clip in clips:
            link_track = project.track(clip.track_id)
            if link_track is not None:
                kinds.add(link_track.kind)
        if kinds == {TrackKind.VIDEO}:
            raise CommandError("Links combine different media; use a video and an audio clip")
        group = new_id()
        wanted = set(self.clip_ids)
        updated = tuple(
            replace(c, link_group=group) if c.id in wanted else c for c in project.clips
        )
        return project.with_clips(updated)

    def describe(self) -> str:
        return "link clips"


@dataclass(frozen=True)
class UnlinkClipCommand(Command):
    """Remove one clip from its link group; the group dies when <2 remain."""

    clip_id: str

    def apply(self, project: Project) -> Project:
        clip = _require_clip(project, self.clip_id)
        if clip.link_group is None:
            raise CommandError("Clip is not linked")
        group = clip.link_group
        members = {c.id for c in project.clips if c.link_group == group}
        # a group with fewer than two members left dissolves entirely
        dissolve = len(members) <= 2
        updated = []
        for c in project.clips:
            if c.id == clip.id or (dissolve and c.link_group == group):
                updated.append(replace(c, link_group=None))
            else:
                updated.append(c)
        return project.with_clips(tuple(updated))

    def describe(self) -> str:
        return "unlink clip"


# ------------------------------------------------------------------- validation


@dataclass(frozen=True)
class SetOutputSettingsCommand(Command):
    """Change the composition's output settings (an ordinary undoable edit).

    The project owns size, rational frame rate, sample rate and channel layout:
    they are never inferred from an asset. Only structural validity is checked
    here; whether a layout can actually be encoded is decided by the render
    layer, which refuses unsupported values clearly.
    """

    width: int
    height: int
    frame_rate: Fraction
    sample_rate: int
    channel_layout: str

    def apply(self, project: Project) -> Project:
        if self.width <= 0 or self.height <= 0:
            raise CommandError(f"Output size {self.width}x{self.height} must be positive")
        if self.frame_rate <= 0:
            raise CommandError(f"Output frame rate {self.frame_rate} must be positive")
        if self.sample_rate <= 0:
            raise CommandError(f"Output sample rate {self.sample_rate} must be positive")
        if not self.channel_layout.strip():
            raise CommandError("Channel layout is required")
        if (
            self.width == project.output.width
            and self.height == project.output.height
            and self.frame_rate == project.output.frame_rate
            and self.sample_rate == project.output.sample_rate
            and self.channel_layout == project.output.channel_layout
        ):
            raise CommandError("Output settings are already those values")
        return replace(
            project,
            output=OutputSettings(
                width=self.width,
                height=self.height,
                frame_rate=self.frame_rate,
                sample_rate=self.sample_rate,
                channel_layout=self.channel_layout.strip(),
            ),
        )

    def describe(self) -> str:
        return (
            f"output {self.width}x{self.height} @ {self.frame_rate} fps, "
            f"{self.sample_rate} Hz, {self.channel_layout}"
        )


@dataclass(frozen=True)
class RelinkAssetCommand:
    """Re-point the project's registered streams at a re-probed replacement file.

    This is the composition half of an explicit relink (the media half is
    ``media.relink`` probing and validation). It is undoable like any other
    edit: undo restores the previously registered bounds and addressing. Clip
    timing is never adjusted to accommodate a replacement — if a replacement
    would leave a clip outside its stream, the command is rejected whole.
    """

    asset_id: str
    replacements: tuple[StreamSource, ...]

    def apply(self, project: Project) -> Project:
        registered = {
            source.key
            for source in project.sources
            if source.asset_id == self.asset_id
        }
        if not registered:
            raise CommandError(f"Asset {self.asset_id} has no registered streams here")
        by_key = {source.key: source for source in self.replacements}
        missing = sorted(registered - set(by_key))
        if missing:
            raise CommandError(
                "a relink must replace every registered stream of the asset; missing "
                + ", ".join(f"stream {index}" for _, index in missing)
            )
        for source in self.replacements:
            if source.asset_id != self.asset_id:
                raise CommandError(
                    f"stream {source.stream_index} belongs to asset "
                    f"{source.asset_id!r}, not {self.asset_id!r}"
                )
        for clip in project.clips:
            if clip.asset_id != self.asset_id:
                continue
            replacement = by_key[(clip.asset_id, clip.stream_index)]
            if replacement.end is None:
                raise CommandError(
                    f"clip {clip.id} uses stream {clip.stream_index} whose length is "
                    "unknown in the replacement; it cannot be verified"
                )
            if clip.source_in < replacement.origin or clip.source_out > replacement.end:
                raise CommandError(
                    f"clip {clip.id} uses source {clip.source_in}-{clip.source_out}s, "
                    f"outside the replacement stream's {replacement.origin}-"
                    f"{replacement.end}s; clips are not retimed automatically"
                )
        # the replacements take the places their originals held: stream order is
        # not semantic here (lookup is by asset + index), but shuffling it would
        # make a relink look like a bigger edit than it is, in the file and in
        # any diff of the saved document
        seen = {source.key for source in project.sources}
        replaced = tuple(by_key.get(source.key, source) for source in project.sources)
        extras = tuple(by_key[key] for key in sorted(set(by_key) - seen))
        return replace(project, sources=replaced + extras)

    def describe(self) -> str:
        return f"relink asset {self.asset_id} to a replacement file"


def validate_project(project: Project) -> list[str]:
    """Return a list of invariant violations (empty list = valid).

    Used on project load, as the baseline a session commits against, and as a
    defensive net in tests. It checks *structure*: ids that must be unique,
    references that must resolve, and the placement rules clips must obey. An
    asset whose file is missing on disk is **not** a structural problem — the
    composition is still exactly what it says it is — so availability is a
    separate question, answered by `fs.identity`.
    """
    problems: list[str] = []
    seen_track_ids: set[str] = set()
    for track in project.tracks:
        if track.id in seen_track_ids:
            problems.append(f"duplicate track id {track.id}")
        seen_track_ids.add(track.id)
        if not track.name.strip():
            problems.append(f"track {track.id} has no name")

    seen_source_keys: set[tuple[str, int]] = set()
    for source in project.sources:
        if source.key in seen_source_keys:
            problems.append(
                f"stream {source.stream_index} of asset {source.asset_id} is "
                "registered more than once"
            )
        seen_source_keys.add(source.key)
        if source.end is not None and source.end < source.origin:
            problems.append(
                f"stream {source.stream_index} of asset {source.asset_id} ends "
                f"({source.end}s) before it starts ({source.origin}s)"
            )

    seen_clip_ids: set[str] = set()
    for clip in project.clips:
        if clip.id in seen_clip_ids:
            problems.append(f"duplicate clip id {clip.id}")
        seen_clip_ids.add(clip.id)
        track = project.track(clip.track_id)
        if track is None:
            problems.append(f"clip {clip.id} references missing track {clip.track_id}")
            continue
        source = project.source(clip.asset_id, clip.stream_index)
        if source is None:
            problems.append(f"clip {clip.id} references unknown stream")
            continue
        if source.kind is not track.kind:
            problems.append(
                f"clip {clip.id} kind mismatches track {track.name} "
                f"({source.kind.value} stream on a {track.kind.value} track)"
            )
        if source.end is not None and clip.source_out > source.end:
            problems.append(f"clip {clip.id} source_out exceeds stream end")
        if clip.source_in < source.origin:
            problems.append(f"clip {clip.id} source_in before stream origin")
        if clip.source_out <= clip.source_in:
            problems.append(f"clip {clip.id} has an empty source interval")
        if clip.output_duration <= 0:
            problems.append(f"clip {clip.id} has no output duration")
        if clip.rate <= 0:
            problems.append(f"clip {clip.id} has a non-positive playback rate")
        if clip.fade_in + clip.fade_out > clip.output_duration:
            problems.append(f"clip {clip.id} fades exceed its duration")

    for link_group in {c.link_group for c in project.clips if c.link_group is not None}:
        members = [c for c in project.clips if c.link_group == link_group]
        if len(members) < 2:
            problems.append(
                f"link group {link_group} has only clip {members[0].id}; a link joins "
                "at least two clips"
            )
    for track in project.tracks:
        ordered = project.clips_on_track(track.id)
        for a, b in pairwise(ordered):
            if a.output_end > b.output_start:
                problems.append(f"clips overlap on track {track.name}")
    return problems
