"""Media bin: lists imported assets and their streams.

The bin controller receives probe results from worker threads through Qt
signals (auto queued connection), so completed imports are inserted on the
main thread only — no races, stable IDs, no re-sorting that would suggest a
preferred source. Assets and languages are shown in import order as peers.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
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
from .format import format_time, processing_caveat, stream_details

_ROLE_ASSET_ID = ROLE_ASSET_ID = Qt.UserRole + 1
_ROLE_STREAM_INDEX = ROLE_STREAM_INDEX = Qt.UserRole + 2

_MUTED_BRUSH = QBrush(QColor(128, 128, 128))


class MediaBinController(QObject):
    """Import job bookkeeping, plus the tree's view of the catalogue.

    The catalogue itself is **not** owned here: the persistent document is the
    only authority for which files a project uses (TASK_10), because a project
    file stores them and undo has to restore them. This controller is given a
    callable that returns the current catalogue, so :attr:`assets` is a
    projection of that state — the bin cannot drift out of step with what is
    saved, and there is no second mutable list to keep in sync.

    What it does own is the transient part: probe jobs in flight, and the
    resolved-path index that turns "import this again" into a refusal.
    """

    job_started = Signal(str)  # job id
    job_finished = Signal(str, object, object, object)  # job id, path, asset|None, error|None
    status_message = Signal(str)

    def __init__(self, catalogue: Callable[[], Sequence[Asset]] | None = None) -> None:
        super().__init__()
        self._catalogue = catalogue
        self._by_path: dict[Path, str] = {}  # resolved path -> asset id ("" = probing)
        self._job_paths: dict[str, Path] = {}
        self.importer = ImportController(
            on_started=lambda job: self.job_started.emit(job.id),
            on_finished=self._worker_finished,
        )

    @property
    def assets(self) -> tuple[Asset, ...]:
        """The project's media catalogue, as the document currently holds it."""
        if self._catalogue is None:
            return ()
        return tuple(self._catalogue())

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
        if not fresh:
            return
        submission = self.importer.import_paths(fresh)
        for job in submission.accepted:
            self._job_paths[job.id] = job.path
        if submission.refused:
            # the queue is full: nothing was probed and no completion will ever
            # arrive for these, so release their placeholders here and say so -
            # otherwise the files stay "already imported" for a probe that never ran
            for path in submission.refused:
                if self._by_path.get(path.resolve()) == "":
                    del self._by_path[path.resolve()]
            self.status_message.emit(
                f"Import queue is full: refused {len(submission.refused)} of "
                f"{len(fresh)} files; at most {self.importer.capacity} probes may be "
                "waiting. Try again once the current ones finish."
            )
        accepted = [job.path for job in submission.accepted]
        if len(accepted) == 1:
            self.status_message.emit(f"Probing {accepted[0].name}…")
        elif accepted:
            self.status_message.emit(f"Probing {len(accepted)} files…")

    def cancel_all_imports(self) -> None:
        self.importer.cancel_all()

    def reset(self) -> None:
        """Forget the path index (New/Open/restore). Running imports are cancelled.

        The catalogue itself is not cleared here: it belongs to the document,
        and a new document replaces it as a unit.
        """
        self.importer.cancel_all()
        self._by_path.clear()
        self._job_paths.clear()

    def index(self, assets: Sequence[Asset]) -> None:
        """Make the duplicate-import index match the document's catalogue.

        An adopted project file, an undone import and a relink all change which
        paths the document holds, and the index has to follow: a file whose
        import was undone must be importable again, and a path the project no
        longer uses must not stay claimed by a record that is gone. Files whose
        probe is still running keep their placeholder, so asking for one of them
        again while it is being read is still refused as a duplicate.
        """
        claimed = {asset.id for asset in assets}
        for path, asset_id in list(self._by_path.items()):
            if asset_id and asset_id not in claimed:
                del self._by_path[path]
        for asset in assets:
            self._index_path(asset)

    def _index_path(self, asset: Asset) -> None:
        with contextlib.suppress(OSError):
            # unreadable path on a dead volume: simply not indexable yet
            self._by_path[Path(asset.path).resolve()] = asset.id

    @property
    def pending_imports(self) -> int:
        return self.importer.pending_count()

    def finalize_finished(
        self, job_id: str, path: Path, asset: Asset | None, error: str | None
    ) -> tuple[Asset | None, str | None, str | None]:
        """Runs on the UI thread. Returns (asset, error, duplicate_note).

        Nothing is added to the catalogue here: the caller commits the probed
        asset through the document session, which is what makes an import an
        undoable, dirtying edit. This method only settles the job bookkeeping.
        """
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
            # defensive: this path was already claimed by an asset of the document
            self._job_paths.pop(job_id, None)
            return None, None, f"Already imported: {path}"
        # the path stays unclaimed on purpose: the caller indexes it once the
        # document has accepted the record, so an import the document refuses
        # does not leave the file looking imported when it is not
        self._by_path.pop(resolved, None)
        self._job_paths.pop(job_id, None)
        return asset, None, None


class MediaBin(QTreeWidget):
    """Two-level tree: assets with expandable stream children.

    Redrawn from the document's catalogue (:meth:`sync`), never from a list of
    its own: what the bin shows is what the project file will hold.
    """

    def __init__(self, controller: MediaBinController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.setColumnCount(2)
        self.setHeaderLabels(["Source", "Details"])
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._items_by_asset: dict[str, QTreeWidgetItem] = {}
        self._signature: tuple[tuple[str, str], ...] = ()

    def sync(self, assets: Sequence[Asset]) -> None:
        """Show exactly *assets*, in their order, restoring the selection.

        Cheap by design: the catalogue is one entry per imported file, and a
        composition edit that changes no media redraws nothing at all.
        Selection is restored by (asset id, stream index) because it is the
        input to Place and Insert, and an unrelated edit should not clear it.
        """
        signature = tuple((asset.id, str(asset.path)) for asset in assets)
        if signature == self._signature:
            return
        wanted = self._selection_keys()
        self.clear()
        self._items_by_asset.clear()
        for asset in assets:
            self._add_row(asset)
        self._signature = signature
        self._restore_selection(wanted)

    def _selection_keys(self) -> set[tuple[str, int | None]]:
        keys: set[tuple[str, int | None]] = set()
        for item in self.selectedItems():
            asset_id = item.data(0, _ROLE_ASSET_ID)
            index = item.data(0, _ROLE_STREAM_INDEX)
            if asset_id:
                keys.add((str(asset_id), None if index is None else int(index)))
        return keys

    def _restore_selection(self, keys: set[tuple[str, int | None]]) -> None:
        if not keys:
            return
        self.blockSignals(True)
        try:
            for asset_id, index in keys:
                item = self._items_by_asset.get(asset_id)
                if item is None:
                    continue
                if index is not None:
                    for row in range(item.childCount()):
                        child = item.child(row)
                        if child.data(0, _ROLE_STREAM_INDEX) == index:
                            item.setExpanded(True)
                            child.setSelected(True)
                            break
                    else:
                        item.setSelected(True)
                else:
                    item.setSelected(True)
        finally:
            self.blockSignals(False)

    def _add_row(self, asset: Asset) -> None:
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
                # two different kinds of note, and only one of them dims the row: a
                # stream whose bounds are unknown cannot be placed at all, while a
                # stream this build cannot draw (interlaced, HDR) is still imported,
                # still inspectable and still usable for sound
                notes = [note for note in (stream.editability_note, processing_caveat(stream))
                         if note]
                if notes:
                    child.setToolTip(1, "\n".join(notes))
                if stream.editability_note:
                    child.setForeground(0, _MUTED_BRUSH)
                item.addChild(child)
        self.addTopLevelItem(item)
        item.setExpanded(True)
        self._items_by_asset[asset.id] = item

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
