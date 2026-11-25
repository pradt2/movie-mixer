"""Media bin: lists imported assets and their streams.

The bin controller receives probe results from worker threads through Qt
signals (auto queued connection), so completed imports are inserted on the
main thread only — no races, stable IDs, no re-sorting that would suggest a
preferred source. Assets and languages are shown in import order as peers.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QTreeWidget,
    QTreeWidgetItem,
)

from ..media.assets import Asset, StreamMetadata
from ..media.importer import ImportController, ImportJob
from .format import format_time, stream_details

_ROLE_ASSET_ID = ROLE_ASSET_ID = Qt.UserRole + 1
_ROLE_STREAM_INDEX = ROLE_STREAM_INDEX = Qt.UserRole + 2

_MUTED_BRUSH = QBrush(QColor(128, 128, 128))


class MediaBinController(QObject):
    """Owns the imported assets; bridges import worker callbacks to the UI thread."""

    job_started = Signal(str)  # job id
    job_finished = Signal(str, object, object, object)  # job id, path, asset|None, error|None
    status_message = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.assets: list[Asset] = []
        self._by_path: dict[Path, str] = {}  # resolved path -> asset id ("" = probing)
        self._job_paths: dict[str, Path] = {}
        self.importer = ImportController(
            on_started=lambda job: self.job_started.emit(job.id),
            on_finished=self._worker_finished,
        )

    # -- worker-thread callback: only emits a signal, touches no shared state --
    def _worker_finished(self, job: ImportJob, asset: Asset | None, error: str | None) -> None:
        self.job_finished.emit(job.id, job.path, asset, error)

    def request_import(self, paths: list[Path]) -> None:
        fresh: list[Path] = []
        for path in paths:
            resolved = path.resolve()
            if resolved in self._by_path:
                self.status_message.emit(f"Already imported: {path}")
            else:
                self._by_path[resolved] = ""  # placeholder; finalised on completion
                fresh.append(path)
        if fresh:
            jobs = self.importer.import_paths(fresh)
            for job, path in zip(jobs, fresh, strict=True):
                self._job_paths[job.id] = path
            if len(fresh) == 1:
                self.status_message.emit(f"Probing {fresh[0].name}…")
            else:
                self.status_message.emit(f"Probing {len(fresh)} files…")

    def cancel_all_imports(self) -> None:
        self.importer.cancel_all()

    def reset(self) -> None:
        """Forget every asset (New/Open/restore). Running imports are cancelled."""
        self.importer.cancel_all()
        self.assets.clear()
        self._by_path.clear()
        self._job_paths.clear()

    def add_external(self, asset: Asset) -> None:
        """Register an asset that came from a project file rather than an import.

        It is indexed by resolved path so that importing the same file again is
        still refused as a duplicate.
        """
        self.assets.append(asset)
        self._index_path(asset)

    def _index_path(self, asset: Asset) -> None:
        with contextlib.suppress(OSError):
            # unreadable path on a dead volume: simply not indexable yet
            self._by_path[Path(asset.path).resolve()] = asset.id

    def reindex(self, asset: Asset) -> None:
        """Move a relinked asset's path index entry to its new location.

        The caller replaces the record in ``assets`` in place; this only keeps
        the path index honest so a later import of the old path is not silently
        accepted as a new asset.
        """
        for path, asset_id in list(self._by_path.items()):
            if asset_id == asset.id:
                del self._by_path[path]
        self._index_path(asset)

    @property
    def pending_imports(self) -> int:
        return self.importer.pending_count()

    def finalize_finished(
        self, job_id: str, path: Path, asset: Asset | None, error: str | None
    ) -> tuple[Asset | None, str | None, str | None]:
        """Runs on the UI thread. Returns (asset, error, duplicate_note)."""
        resolved = path.resolve()
        if error == "cancelled":
            if self._by_path.get(resolved) == "":
                self._by_path.pop(resolved, None)
            self._job_paths.pop(job_id, None)
            return None, "cancelled", None
        if asset is None:
            if self._by_path.get(resolved) == "":
                self._by_path.pop(resolved, None)
            self._job_paths.pop(job_id, None)
            return None, error or "Import failed", None
        if self._by_path.get(resolved):
            # defensive: placeholder already finalised by another completion
            self._job_paths.pop(job_id, None)
            return None, None, f"Already imported: {path}"
        self.assets.append(asset)
        self._by_path[resolved] = asset.id
        self._job_paths.pop(job_id, None)
        return asset, None, None


class MediaBin(QTreeWidget):
    """Two-level tree: assets with expandable stream children."""

    def __init__(self, controller: MediaBinController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setColumnCount(2)
        self.setHeaderLabels(["Source", "Details"])
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._items_by_asset: dict[str, QTreeWidgetItem] = {}
        controller.job_started.connect(self._on_job_started)
        controller.job_finished.connect(self._on_job_finished)

    def _on_job_started(self, job_id: str) -> None:
        pass  # progress is reported through status messages

    def _on_job_finished(
        self, job_id: str, path: Path, asset: Asset | None, error: str | None
    ) -> None:
        asset, error, duplicate = self.controller.finalize_finished(job_id, path, asset, error)
        if asset is not None:
            self.add_asset(asset)
            note = "" if asset.editable else " — no editable streams"
            self.controller.status_message.emit(f"Imported {asset.path.name}{note}")
        elif error == "cancelled":
            self.controller.status_message.emit(f"Import cancelled: {path.name}")
        elif error:
            self.controller.status_message.emit(f"Import failed for {path.name}: {error}")
        elif duplicate:
            self.controller.status_message.emit(duplicate)

    def add_asset(self, asset: Asset) -> None:
        item = QTreeWidgetItem([asset.display_name(), ""])
        item.setData(0, _ROLE_ASSET_ID, asset.id)
        item.setToolTip(0, str(asset.path))
        if asset.probe_error or asset.metadata is None:
            item.setText(1, f"not readable: {asset.probe_error or 'probe failed'}")
            item.setForeground(0, _MUTED_BRUSH)
        else:
            item.setText(1, f"length {format_time(asset.metadata.container_duration)}")
            if not asset.editable:
                item.setForeground(0, _MUTED_BRUSH)
            for stream in asset.metadata.streams:
                child = QTreeWidgetItem([self._stream_label(stream), ""])
                child.setData(0, _ROLE_ASSET_ID, asset.id)
                child.setData(0, _ROLE_STREAM_INDEX, stream.stream_index)
                child.setText(1, stream_details(stream))
                note = stream.editability_note
                if note:
                    child.setToolTip(1, note)
                    child.setForeground(0, _MUTED_BRUSH)
                item.addChild(child)
        self.addTopLevelItem(item)
        item.setExpanded(True)
        self._items_by_asset[asset.id] = item

    def rebuild(self, assets: list[Asset]) -> None:
        """Redraw the whole tree (after Open/New/restore)."""
        self.clear()
        self._items_by_asset.clear()
        for asset in assets:
            self.add_asset(asset)

    def refresh_asset(self, asset: Asset) -> None:
        """Replace one asset's row after a relink (new path, new streams).

        The row moves to the end of the list: the bin shows assets in the order
        they became known, and a relinked asset is deliberately re-listed rather
        than patched in place, because its streams may have changed.
        """
        item = self._items_by_asset.pop(asset.id, None)
        if item is not None:
            self.takeTopLevelItem(self.indexOfTopLevelItem(item))
        self.add_asset(asset)

    @staticmethod
    def _stream_label(stream: StreamMetadata) -> str:
        kind = stream.kind.value.capitalize()
        suffix = f" [{stream.language}]" if stream.language else ""
        return f"    {kind}{suffix}"

    def selected_asset(self) -> Asset | None:
        items = self.selectedItems()
        if not items:
            return None
        asset_id = items[0].data(0, _ROLE_ASSET_ID)
        for asset in self.controller.assets:
            if asset.id == asset_id:
                return asset
        return None

    def selected_stream(self) -> tuple[Asset, StreamMetadata] | None:
        items = self.selectedItems()
        if not items:
            return None
        index = items[0].data(0, _ROLE_STREAM_INDEX)
        asset = self.selected_asset()
        if asset is None or asset.metadata is None or index is None:
            return None
        stream = asset.metadata.stream(int(index))
        if stream is None:
            return None
        return asset, stream
