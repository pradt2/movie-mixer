"""Recovery snapshots: an app-owned safety net, separate from the user's project file.

Snapshots exist so that a crash, a power cut or a mistaken quit costs minutes of
work rather than a session. They are deliberately **not** the normal project
file:

* they live in an application directory (``$VIDEO_EDITOR_RECOVERY_DIR``, else
  ``$XDG_DATA_HOME/video-editor/recovery``, else ``~/.local/share/...``), never
  beside media or beside the user's project file;
* they are never opened implicitly — restoring is an explicit user choice after
  the application offers it;
* they carry a header explaining where they came from and when, so the offer can
  say "saved 4 minutes ago, before the last export" instead of showing a blob;
* they never overwrite an existing project file. If the project file on disk
  changed *after* the snapshot was taken, restoring is refused until the user
  decides what to do with that file (see :func:`disk_ahead_of_snapshot`).

Undo history is **not** persisted: history is a live session feature, while a
snapshot is the whole document — composition plus the media catalogue — together
with navigation state. This is documented in ``docs/architecture.md``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .serialize import SCHEMA_VERSION, ProjectFileError
from .storage import (
    LoadedDocument,
    RecoveryMeta,
    Snapshot,
    decode_payload,
)

log = logging.getLogger(__name__)

#: snapshots kept per project (oldest are pruned)
DEFAULT_KEEP = 15
SNAPSHOT_PREFIX = "snapshot-"
SNAPSHOT_SUFFIX = ".vep.json"
_HEADER = "recovery"


def recovery_root() -> Path:
    """The application-owned directory holding recovery snapshots."""
    override = os.environ.get("VIDEO_EDITOR_RECOVERY_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "video-editor" / "recovery"


def session_key(project_path: Path | None) -> str:
    """Stable grouping key: one per project file, plus one for unsaved work."""
    if project_path is None:
        return "unsaved"
    try:
        resolved = str(Path(project_path).resolve())
    except OSError:  # pragma: no cover - exotic filesystem errors
        resolved = str(project_path)
    return "p-" + hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


class RecoveryStore:
    """Writes and reads recovery snapshots for one session key."""

    def __init__(
        self, key: str, root: Path | None = None, keep: int = DEFAULT_KEEP
    ) -> None:
        if not key:
            raise ValueError("recovery session key must not be empty")
        self.key = key
        self.root = Path(root) if root is not None else recovery_root()
        self.keep = keep

    # ------------------------------------------------------------------ write
    def write(
        self,
        document: dict[str, Any],
        *,
        reason: str,
        project_path: Path | None = None,
        project_mtime_ns: int | None = None,
        label: str = "",
    ) -> Path:
        """Store one snapshot atomically; returns its path.

        A snapshot that cannot be written is reported through the return value's
        absence (an exception) — it never affects the user's project file.
        """
        directory = self.root / self.key
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProjectFileError(f"cannot create recovery directory {directory}: {exc}") from exc
        created_ns = time.time_ns()
        header = RecoveryMeta(
            created_ns=created_ns,
            reason=reason,
            project_path=str(project_path) if project_path is not None else None,
            project_mtime_ns=project_mtime_ns,
            label=label,
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            _HEADER: header.to_dict(),
            "document": document,
        }
        name = f"{SNAPSHOT_PREFIX}{created_ns}-{uuid.uuid4().hex[:8]}{SNAPSHOT_SUFFIX}"
        destination = directory / name
        staging = directory / f".{name}.tmp"
        try:
            with staging.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2, ensure_ascii=False))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staging, destination)
        except OSError as exc:
            raise ProjectFileError(f"could not write recovery snapshot: {exc}") from exc
        finally:
            staging.unlink(missing_ok=True)
        self.prune()
        return destination

    # ------------------------------------------------------------------- read
    def list(self) -> list[Snapshot]:
        """Snapshots for this key, newest first. Unreadable ones are skipped."""
        directory = self.root / self.key
        if not directory.is_dir():
            return []
        found: list[Snapshot] = []
        for path in directory.iterdir():
            if not path.name.startswith(SNAPSHOT_PREFIX) or not path.name.endswith(
                SNAPSHOT_SUFFIX
            ):
                continue  # never touch anything that is not ours
            snapshot = self._read(path)
            if snapshot is not None:
                found.append(snapshot)
        return sorted(found, key=lambda item: item.meta.created_ns, reverse=True)

    def _read(self, path: Path) -> Snapshot | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.warning("ignoring unreadable recovery snapshot %s: %s", path, exc)
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get(_HEADER), dict):
            log.warning("ignoring recovery snapshot without a header: %s", path)
            return None
        header = payload[_HEADER]
        document = payload.get("document")
        if not isinstance(document, dict):
            log.warning("ignoring recovery snapshot without a document: %s", path)
            return None
        meta = RecoveryMeta(
            created_ns=int(header.get("created_ns") or 0),
            reason=str(header.get("reason") or "unknown"),
            project_path=header.get("project_path"),
            project_mtime_ns=header.get("project_mtime_ns"),
            label=str(header.get("label") or ""),
        )
        return Snapshot(meta=meta, document=document, path=path)

    def load_latest(self) -> tuple[LoadedDocument, Snapshot] | None:
        """Newest valid snapshot, decoded and validated, or None.

        A snapshot whose document this build cannot read is reported as an error
        by the caller-visible ``list()`` walk rather than half-applied.
        """
        for snapshot in self.list():
            recorded = snapshot.meta.project_path
            base_dir = Path(recorded).parent if recorded else None
            try:
                document = decode_payload(snapshot.document, base_dir=base_dir)
            except ProjectFileError as exc:
                log.warning("recovery snapshot %s is not restorable: %s", snapshot.path, exc)
                continue
            return document, snapshot
        return None

    # ------------------------------------------------------------------ prune
    def prune(self) -> int:
        """Delete this key's oldest snapshots beyond ``keep``; returns the count."""
        snapshots = self.list()
        if len(snapshots) <= self.keep:
            return 0
        removed = 0
        for snapshot in snapshots[self.keep :]:
            try:
                snapshot.path.unlink()
                removed += 1
            except OSError:  # pragma: no cover - concurrent cleanup
                continue
        return removed

    def discard(self) -> int:
        """Delete every snapshot of this key (the user chose 'discard')."""
        removed = 0
        for snapshot in self.list():
            try:
                snapshot.path.unlink()
                removed += 1
            except OSError:  # pragma: no cover
                continue
        directory = self.root / self.key
        with contextlib.suppress(OSError):
            # succeeds only if nothing else lives in there: never delete blindly
            directory.rmdir()
        return removed


def disk_ahead_of_snapshot(snapshot: Snapshot) -> str | None:
    """Why restoring this snapshot would be unsafe, or None if it is safe.

    Restoring replaces the composition in the window; if the project file on
    disk has been written *after* the snapshot was taken, that newer file would
    be silently lost the moment the user saved. So it is refused, with the two
    timestamps spelled out.
    """
    if snapshot.meta.project_path is None:
        return None
    disk = Path(snapshot.meta.project_path)
    try:
        stat = disk.stat()
    except OSError:
        return None  # no file on disk: restoring cannot overwrite anything
    recorded = snapshot.meta.project_mtime_ns
    if recorded is not None and stat.st_mtime_ns > recorded:
        return (
            f"{disk} was written after this snapshot was taken "
            f"(file mtime {stat.st_mtime_ns} ns vs recorded {recorded} ns). Open that "
            "file instead, or discard the snapshot; restoring would overwrite it."
        )
    if recorded is None and stat.st_mtime_ns > snapshot.meta.created_ns:
        return (
            f"{disk} is newer than this snapshot. Open that file instead of "
            "restoring, unless you are sure the snapshot is the later work."
        )
    return None


def describe_snapshot(snapshot: Snapshot) -> str:
    """One-line human description used in the restore offer."""
    age_seconds = max(0, (time.time_ns() - snapshot.meta.created_ns) // 10**9)
    minutes, seconds = divmod(age_seconds, 60)
    age = f"{minutes} min {seconds:02d} s" if minutes else f"{seconds} s"
    where = snapshot.meta.project_path or "an unsaved project"
    label = f" — {snapshot.meta.label}" if snapshot.meta.label else ""
    return f"{age} ago, {snapshot.meta.reason}, for {where}{label}"
