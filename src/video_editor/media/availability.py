"""Availability and protection applied to :class:`Asset` records.

The policy itself is in :mod:`video_editor.fs` and knows nothing about assets;
this module is the thin layer that reads an asset's recorded path and identity
and hands callers a result they can act on:

* :func:`check_asset` / :func:`check_catalogue` — is this asset's file still the
  one recorded, and (for a whole catalogue) **one ``stat`` per asset**, never per
  clip;
* :func:`protected_files` — the paths a write must never land on, for
  :class:`~video_editor.fs.protection.DestinationGuard`;
* :func:`ensure_usable` — raise :class:`MediaUnavailableError` with an
  actionable message, for code that is about to decode something.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ..fs.identity import AssetStatus, Availability, Identity, check
from ..fs.protection import ProtectedFile, aliases
from .assets import Asset


class MediaUnavailableError(Exception):
    """The file behind this asset cannot be used as it is recorded."""

    def __init__(self, message: str, availability: Availability) -> None:
        super().__init__(message)
        self.availability = availability
        self.status = availability.status


def recorded_identity(asset: Asset) -> Identity | None:
    """What this asset record says its file looked like (``None`` if unprovable)."""
    if asset.size_bytes is None or asset.mtime_ns is None:
        return None
    return Identity(size_bytes=asset.size_bytes, mtime_ns=asset.mtime_ns)


def check_asset(asset: Asset) -> Availability:
    """Check one asset record against the file it names."""
    label = asset.display_name() if hasattr(asset, "display_name") else None
    return check(Path(asset.path), recorded_identity(asset), label)


def check_catalogue(assets: Iterable[Asset]) -> dict[str, Availability]:
    """Check every asset once, keyed by asset id.

    Interaction paths (playhead moves, paints, render preflight) must use this:
    a long timeline has hundreds of clips over a handful of files, and stat-ing
    the same file once per clip was measured at ~5 ms per playhead move.
    """
    return {asset.id: check_asset(asset) for asset in assets}


def ensure_usable(asset: Asset, action: str = "read this source") -> Availability:
    """Return the availability, or raise :class:`MediaUnavailableError`."""
    entry = check_asset(asset)
    if not entry.usable:
        raise MediaUnavailableError(
            f"cannot {action}: {entry.describe()}", availability=entry
        )
    return entry


def protected_files(assets: Iterable[Asset]) -> tuple[ProtectedFile, ...]:
    """Every media file of this catalogue, as a path no write may target.

    Every asset counts, including ones that are muted, disabled or not placed on
    the timeline at all: an unused asset is still the user's source media.
    """
    entries: list[ProtectedFile] = []
    seen: set[str] = set()
    for asset in assets:
        if asset.id in seen:
            continue
        seen.add(asset.id)
        name = asset.display_name() if hasattr(asset, "display_name") else str(asset.path)
        entries.append(ProtectedFile.of(Path(asset.path), f"imported media {name}"))
    return tuple(entries)


def status_label(entry: Availability) -> str:
    """Short state word for lists and dialogs."""
    return entry.status.value


def same_file(one: Path, other: Path) -> bool:
    """Do these two spellings address the same file (path, symlink or hard link)?"""
    return bool(aliases(Path(one)) & aliases(Path(other)))


#: the states that mean "this source cannot be used as recorded"
UNAVAILABLE = tuple(s for s in AssetStatus if s is not AssetStatus.ONLINE)
