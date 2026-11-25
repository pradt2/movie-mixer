"""Reading and writing project files, plus the state of the media they name.

Responsibilities:

* **Atomic save.** The document is written to a sibling temporary file, flushed,
  then ``os.replace``d onto the destination. A crash or a full disk leaves the
  previous file intact and no partial file behind; nothing is ever written next
  to media files.
* **Explicit path resolution.** Relative references are resolved against the
  directory containing the project file. Absolute references are used as given.
* **Honest media state.** Each asset is checked against the file it names and
  its recorded identity (size + mtime_ns). The result is reported as
  ``online`` / ``missing`` / ``changed``; the project is *not* silently repaired
  and no media is modified. Metadata recorded in the file is kept, so an offline
  project can still be inspected and saved.
* **Validation.** Schema, cross-references (clip → stream → asset, clip →
  track), domain invariants (``core.commands.validate_project``) and a check
  that the material clips reference still fits inside the recorded stream
  bounds. Anything that fails aborts the load: the caller's current project is
  left untouched.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from ..core.commands import validate_project
from ..core.model import Project
from ..media.assets import Asset
from ..media.serialization import decode_asset, encode_asset
from .serialize import (
    Navigation,
    ProjectFileError,
    decode_navigation,
    decode_project,
    decode_version,
    encode_document,
)

PROJECT_EXTENSION = ".vep"
class AssetState(Enum):
    ONLINE = "online"
    MISSING = "missing"
    #: the file at the recorded path has a different size/mtime than recorded
    CHANGED = "changed"


@dataclass(frozen=True)
class AssetRecord:
    """One asset plus what the disk currently says about it."""

    asset: Asset
    state: AssetState
    #: resolved location on disk (even when it does not exist)
    resolved_path: Path
    #: human-readable reason for MISSING/CHANGED, None when online
    note: str | None = None


@dataclass(frozen=True)
class LoadedDocument:
    """A decoded project document, before it is applied to a session."""

    project: Project
    assets: tuple[AssetRecord, ...]
    navigation: Navigation
    schema_version: int
    #: non-fatal observations worth showing the user
    warnings: tuple[str, ...] = ()
    #: project path this document was read from (None for recovery snapshots)
    path: Path | None = None

    def asset(self, asset_id: str) -> AssetRecord | None:
        for record in self.assets:
            if record.asset.id == asset_id:
                return record
        return None

    def unavailable(self) -> tuple[AssetRecord, ...]:
        return tuple(r for r in self.assets if r.state is not AssetState.ONLINE)


# ------------------------------------------------------------------ file names


def project_display_name(path: Path | None) -> str:
    return path.name if path is not None else "untitled"


def suggested_project_path(directory: Path | None, name: str = "project") -> Path:
    """A sensible default for Save As; never inside a media directory check."""
    base = directory if directory is not None else Path.home()
    return base / f"{name}{PROJECT_EXTENSION}"


def relative_reference(path: Path, project_dir: Path) -> tuple[str, str]:
    """``(path_mode, reference)`` for *path* as seen from *project_dir*.

    A relative reference is only used when it stays inside the project
    directory: a ``../..`` chain breaks the moment one folder is renamed, and
    the point of a relative path is that the pair (project, media) can move
    together.
    """
    try:
        rel = os.path.relpath(path, project_dir)
    except ValueError:  # different drives on Windows
        return "absolute", str(path)
    if rel.startswith("..") or os.path.isabs(rel):
        return "absolute", str(path)
    return "relative", rel


# ------------------------------------------------------------------- encoding


def build_document(
    project: Project,
    assets: list[Asset],
    navigation: Navigation,
    application_version: str,
    project_path: Path | None,
) -> dict[str, Any]:
    """Encode a whole document, resolving asset references relative to the file."""
    project_dir = project_path.parent if project_path is not None else None
    encoded: list[dict[str, Any]] = []
    for asset in assets:
        path = Path(asset.path)
        try:
            absolute = str(path.resolve())
        except OSError:  # pragma: no cover - exotic filesystem errors
            absolute = str(path)
        if project_dir is not None:
            mode, reference = relative_reference(path, project_dir)
        else:
            mode, reference = "absolute", str(path)
        encoded.append(encode_asset(asset, mode, reference, absolute))
    return encode_document(project, encoded, navigation, application_version)


def decode_payload(payload: dict[str, Any], base_dir: Path | None) -> LoadedDocument:
    """Validate a decoded JSON object into a :class:`LoadedDocument`."""
    version = decode_version(payload)
    if not isinstance(payload.get("assets"), list):
        raise ProjectFileError("project file has no 'assets' list")

    project = decode_project(payload)
    navigation = decode_navigation(payload.get("navigation"))

    records: list[AssetRecord] = []
    seen_ids: set[str] = set()
    for position, raw in enumerate(payload["assets"]):
        asset, mode, reference = decode_asset(raw, f"assets[{position}]")
        if asset.id in seen_ids:
            raise ProjectFileError(
                f"assets[{position}].id {asset.id!r} is duplicated in this file"
            )
        seen_ids.add(asset.id)
        resolved = _resolve(mode, reference, base_dir, Path(asset.path))
        asset = replace(asset, path=resolved)
        records.append(_check_identity(asset, resolved))

    warnings = list(_check_streams(project, records))
    problems = validate_project(project)
    if problems:
        raise ProjectFileError(
            "project file describes an invalid composition:\n  - "
            + "\n  - ".join(problems)
        )
    warnings.extend(_check_material(project, records))

    return LoadedDocument(
        project=project,
        assets=tuple(records),
        navigation=navigation.clamped(project.output_end()),
        schema_version=version,
        warnings=tuple(warnings),
    )


def _resolve(mode: str, reference: str, base_dir: Path | None, fallback: Path) -> Path:
    if mode == "relative":
        if base_dir is None:
            return fallback
        return (base_dir / reference).resolve()
    return Path(reference)


def _check_identity(asset: Asset, resolved: Path) -> AssetRecord:
    """Compare the file on disk with the identity recorded in the document.

    A document with no recorded size/mtime cannot prove anything changed, so it
    is reported as online rather than inventing a mismatch.
    """
    try:
        stat = resolved.stat()
    except OSError:
        return AssetRecord(
            asset=asset,
            state=AssetState.MISSING,
            resolved_path=resolved,
            note=f"{resolved} is not readable (moved, renamed, or on an unmounted volume)",
        )
    if (
        asset.size_bytes is not None
        and asset.mtime_ns is not None
        and (stat.st_size != asset.size_bytes or stat.st_mtime_ns != asset.mtime_ns)
    ):
        return AssetRecord(
            asset=asset,
            state=AssetState.CHANGED,
            resolved_path=resolved,
            note=(
                f"{resolved.name} is not the file this project was saved with: "
                f"recorded {asset.size_bytes} bytes at mtime {asset.mtime_ns}, "
                f"found {stat.st_size} bytes at mtime {stat.st_mtime_ns}. It may be "
                "a different encode of the same film."
            ),
        )
    return AssetRecord(asset=asset, state=AssetState.ONLINE, resolved_path=resolved)


def _check_streams(project: Project, records: list[AssetRecord]) -> list[str]:
    """Registered sources must be backed by an asset record of this document."""
    by_id = {record.asset.id: record for record in records}
    notes: list[str] = []
    for source in project.sources:
        record = by_id.get(source.asset_id)
        if record is None:
            notes.append(
                f"stream {source.stream_index} is registered for asset "
                f"{source.asset_id!r}, which this file does not describe"
            )
            continue
        if record.asset.metadata is None:
            continue  # asset's own probe failure is reported from its record
        if record.asset.metadata.stream(source.stream_index) is None:
            notes.append(
                f"{record.asset.display_name()} has no stream {source.stream_index} "
                "in the metadata recorded with this project"
            )
    return notes


def _check_material(project: Project, records: list[AssetRecord]) -> list[str]:
    """Do the recorded stream bounds still cover the material the clips use?

    These are warnings, not load failures: the composition is exactly what was
    saved, and the mismatch is a property of the *media*, which the user can
    fix by relinking. Rendering such material is refused later with the same
    precision.
    """
    by_id = {record.asset.id: record for record in records}
    notes: list[str] = []
    for clip in project.clips:
        source = project.source(clip.asset_id, clip.stream_index)
        if source is None:
            continue  # already a hard error via validate_project
        record = by_id.get(clip.asset_id)
        if record is None or record.asset.metadata is None:
            continue
        stream = record.asset.metadata.stream(clip.stream_index)
        if stream is None or stream.end_time is None:
            continue
        if clip.source_out > stream.end_time or clip.source_in < stream.origin:
            notes.append(
                f"{record.asset.display_name()} stream {clip.stream_index} now covers "
                f"{stream.origin}-{stream.end_time}s, but clip {clip.id} uses "
                f"{clip.source_in}-{clip.source_out}s; relink this asset or trim the clip"
            )
        if (
            record.state is AssetState.CHANGED
            and source.frame_rate
            and stream.frame_rate
            and source.frame_rate != stream.frame_rate
        ):
                notes.append(
                    f"{record.asset.display_name()} frame rate differs from the one "
                    f"this project was saved with ({stream.frame_rate} vs "
                    f"{source.frame_rate})"
                )
    return notes


# ---------------------------------------------------------------- read / write


def save_document(document: dict[str, Any], destination: Path) -> Path:
    """Atomically write *document* to *destination*; returns the path written.

    Never touches media files, never leaves a partial project file behind.
    Raises :class:`ProjectFileError` with the reason on any failure.
    """
    destination = Path(destination)
    directory = destination.parent
    if not directory.is_dir():
        raise ProjectFileError(f"cannot save: {directory} is not a directory")
    text = json.dumps(document, indent=2, ensure_ascii=False)
    token = uuid.uuid4().hex[:12]
    staging = directory / f".{destination.name}.{token}.tmp"
    try:
        with staging.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, destination)
    except OSError as exc:
        raise ProjectFileError(f"could not write {destination}: {exc}") from exc
    finally:
        # after a successful rename there is nothing of ours left to remove
        staging.unlink(missing_ok=True)
    return destination


def load_document(path: Path) -> LoadedDocument:
    """Read and validate a project file. On any problem nothing is returned."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ProjectFileError(f"no such file: {path}") from None
    except IsADirectoryError:
        raise ProjectFileError(f"{path} is a directory, not a project file") from None
    except OSError as exc:
        raise ProjectFileError(f"could not read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ProjectFileError(
            f"{path} is not a text project file (it may be a media or backup file)"
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProjectFileError(
            f"{path} is not readable JSON: {exc.msg} at line {exc.lineno} column "
            f"{exc.colno}. The file was not modified; if you have a backup or a "
            "recovery snapshot, restore that instead."
        ) from exc
    document = decode_payload(payload, base_dir=path.parent)
    return replace(document, path=path)


# ------------------------------------------------------------------- recovery


@dataclass(frozen=True)
class RecoveryMeta:
    """Header of a recovery snapshot: what it is and how it got there."""

    created_ns: int
    reason: str
    project_path: str | None = None
    project_mtime_ns: int | None = None
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_ns": self.created_ns,
            "reason": self.reason,
            "project_path": self.project_path,
            "project_mtime_ns": self.project_mtime_ns,
            "label": self.label,
        }


@dataclass(frozen=True)
class Snapshot:
    """One recovery snapshot: header + the same document a project file holds."""

    meta: RecoveryMeta
    document: dict[str, Any]
    path: Path


@dataclass(frozen=True)
class LoadedSnapshot:
    """A snapshot that passed validation, ready to be restored."""

    snapshot: Snapshot
    document: LoadedDocument

    @property
    def meta(self) -> RecoveryMeta:
        return self.snapshot.meta


def project_file_mtime(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return None
