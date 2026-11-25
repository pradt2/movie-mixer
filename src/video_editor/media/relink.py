"""Explicit relinking: re-pointing an asset record at a different file.

Rules (AGENTS.md): relinking is always a user decision, never an automatic
substitution; the replacement must be *probed and validated*; clip timing is
never adjusted to make a mismatch go away; and no media file is modified.

Validation answers one question: **can this file serve the material this
project already uses?** For every stream this project uses from the asset:

* a stream with the same container index must exist, and be the same kind;
* its recorded bounds must cover every source interval the clips take from it
  (otherwise a clip would render something other than what it names);
* its nominal frame rate / sample rate must match the registered ones, because
  a different rate changes which source material sits under an output time;
* picture size must match unless the caller passes ``allow_geometry_change``
  (a knowingly different encode: the composition stays legal, the picture is
  scaled by the renderer);
* the bounds must be known at all.

Anything else is returned as a precise, itemised reason. A failed relink leaves
the asset exactly as it was.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from ..core.model import Clip, StreamSource
from .assets import Asset, AssetMetadata
from .ffprobe import ProbeResult, probe_file


class RelinkError(Exception):
    """The relink could not even be attempted (unreadable replacement, ...)."""


@dataclass(frozen=True)
class RelinkPlan:
    """What a relink would change, if the validation passes."""

    asset: Asset
    #: replacement records for the project's registered sources
    sources: tuple[StreamSource, ...]
    #: non-fatal notes worth telling the user (positions renumbered, ...)
    notes: tuple[str, ...] = ()


def validate_replacement(
    asset: Asset,
    replacement: Asset,
    sources: tuple[StreamSource, ...],
    clips: tuple[Clip, ...],
    *,
    allow_geometry_change: bool = False,
) -> tuple[tuple[StreamSource, ...], tuple[str, ...], tuple[str, ...]]:
    """Pure validation: returns ``(new_sources, notes, problems)``.

    ``replacement`` must already carry fresh probe metadata and stat identity.
    """
    problems: list[str] = []
    notes: list[str] = []
    if replacement.metadata is None:
        return (
            (),
            (),
            (
                f"{replacement.path.name} could not be probed"
                f" ({replacement.probe_error or 'no metadata'}); nothing can be "
                "verified about it",
            ),
        )
    metadata = replacement.metadata
    new_sources: list[StreamSource] = []
    for source in sources:
        if source.asset_id != asset.id:
            continue
        stream = metadata.stream(source.stream_index)
        where = f"{asset.display_name()} stream {source.stream_index}"
        if stream is None:
            problems.append(
                f"{where}: {replacement.path.name} has no stream with that container "
                f"index (it has: {_stream_summary(metadata)}). Streams are never "
                "substituted by guesswork."
            )
            continue
        if stream.kind.value != source.kind.value:
            problems.append(
                f"{where}: the replacement has a {stream.kind.value} stream there, "
                f"but the project uses it as {source.kind.value.lower()}"
            )
            continue
        end = stream.end_time
        if end is None:
            problems.append(
                f"{where}: the replacement's duration is unknown, so the clips that "
                "use it cannot be verified"
            )
            continue
        attached = tuple(
            clip
            for clip in clips
            if clip.asset_id == asset.id and clip.stream_index == source.stream_index
        )
        for clip in attached:
            if clip.source_in < stream.origin or clip.source_out > end:
                problems.append(
                    f"{where}: clip {clip.id} uses source "
                    f"{clip.source_in}-{clip.source_out}s, which the replacement does "
                    f"not contain (its stream covers {stream.origin}-{end}s). Clip "
                    "timing is not adjusted automatically."
                )
        if source.frame_rate and stream.frame_rate and source.frame_rate != stream.frame_rate:
            problems.append(
                f"{where}: frame rate {stream.frame_rate} in the replacement, "
                f"{source.frame_rate} in this project — different timing base"
            )
        if source.sample_rate and stream.sample_rate and source.sample_rate != stream.sample_rate:
            problems.append(
                f"{where}: sample rate {stream.sample_rate} in the replacement, "
                f"{source.sample_rate} in this project"
            )
        if (
            not allow_geometry_change
            and source.width
            and stream.width
            and (source.width != stream.width or source.height != stream.height)
        ):
            problems.append(
                f"{where}: picture size {stream.width}x{stream.height} in the "
                f"replacement, {source.width}x{source.height} in this project. Choose "
                "the same encode, or accept the geometry change knowingly."
            )
        position = metadata.position_of(stream.stream_index)
        if position is None:  # pragma: no cover - stream was found above
            problems.append(f"{where}: its position among {source.kind.value} streams is unknown")
            continue
        if source.stream_position is not None and source.stream_position != position:
            notes.append(
                f"{where}: it is now stream number {position} of "
                f"{source.kind.value.lower()} streams in that file (was "
                f"{source.stream_position}); rendering addresses it by the new position"
            )
        new_sources.append(
            replace(
                source,
                origin=stream.origin,
                end=end,
                conservative_end=stream.bounds_are_conservative,
                frame_rate=stream.frame_rate,
                sample_rate=stream.sample_rate,
                channel_layout=stream.channel_layout,
                width=stream.width,
                height=stream.height,
                input_start=metadata.container_start or 0,
                stream_position=position,
            )
        )
    if metadata.container_start is not None and sources:
        old_starts = {
            source.input_start for source in sources if source.asset_id == asset.id
        }
        if old_starts and metadata.container_start not in old_starts:
            notes.append(
                f"the replacement's presentation timeline starts at "
                f"{metadata.container_start}s (this project used "
                f"{', '.join(str(value) for value in sorted(old_starts))}s); source "
                "times are kept exactly as saved"
            )
    return tuple(new_sources), tuple(notes), tuple(problems)


def _stream_summary(metadata: AssetMetadata | None) -> str:
    if metadata is None:
        return "no readable streams"
    return ", ".join(
        f"{stream.stream_index}: {stream.kind.value}" for stream in metadata.streams
    ) or "no streams"


def probe_replacement(path: Path) -> Asset:
    """Probe a candidate file into an asset record (identity from the file)."""
    path = Path(path)
    if not path.is_file():
        raise RelinkError(f"{path} is not a readable file")
    result: ProbeResult = probe_file(path)
    identity = _stat_identity(path)
    return Asset(
        id="",  # the existing asset keeps its stable id; this is only a carrier
        path=path,
        size_bytes=identity[0],
        mtime_ns=identity[1],
        metadata=result.metadata,
        probe_error=result.error,
    )


def _stat_identity(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError:
        return 0, 0
    return stat.st_size, stat.st_mtime_ns


def relink_asset(
    asset: Asset,
    new_path: Path,
    sources: tuple[StreamSource, ...],
    clips: tuple[Clip, ...],
    *,
    allow_geometry_change: bool = False,
    replacement: Asset | None = None,
) -> RelinkPlan:
    """Probe and validate; raises :class:`RelinkError` with all reasons combined.

    The returned plan keeps the asset's **stable id** and label, so every clip
    keeps pointing at the same asset after a relink.
    """
    candidate = replacement if replacement is not None else probe_replacement(new_path)
    replacement_asset = replace(
        candidate,
        id=asset.id,
        path=Path(candidate.path),
        label=asset.label,
    )
    new_sources, notes, problems = validate_replacement(
        asset,
        replacement_asset,
        sources,
        clips,
        allow_geometry_change=allow_geometry_change,
    )
    if problems:
        raise RelinkError(
            f"cannot relink {asset.display_name()} to {candidate.path.name}:\n  - "
            + "\n  - ".join(problems)
        )
    return RelinkPlan(asset=replacement_asset, sources=new_sources, notes=notes)
