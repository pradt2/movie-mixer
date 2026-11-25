"""Undo/redo history and dirty-state tracking over an immutable snapshot.

The snapshot is deliberately a type parameter. The composition (``core.Project``)
is one kind of snapshot; the *whole persistent document* — composition plus the
media catalogue that has to be saved with it — is another (see
:mod:`video_editor.project.document`). History itself only ever stores, compares
and swaps opaque immutable values, so it works for both and does not learn
anything about media, files or Qt.

Snapshots are immutable, so history can safely hold before/after references
(structural sharing makes this cheap, and a snapshot is safe to hand to a
concurrent renderer). Undo/redo restore whole transactions; selection, playhead,
zoom and what is currently online on disk are UI state and never appear here.

Stable IDs survive undo/redo because snapshots carry the original ids.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .model import Project


class HistoryError(Exception):
    """Requested history operation is not possible."""


class SnapshotCommand[S](Protocol):
    """What :meth:`EditHistory.apply` accepts: a pure snapshot-to-snapshot edit.

    ``core.commands.Command`` satisfies it for ``Project`` snapshots, and
    ``project.document.DocumentCommand`` satisfies it for whole documents. A
    command must compute the complete result and return it, or raise without
    returning anything: it may not mutate its input or partially apply.
    """

    def apply(self, snapshot: S) -> S: ...

    def describe(self) -> str: ...


@dataclass(frozen=True)
class HistoryEntry[S]:
    before: S
    after: S
    description: str


class EditHistory[S]:
    """One snapshot, the transactions that produced it, and its saved baseline.

    ``apply`` is the only way the snapshot grows: the command computes a whole
    new snapshot (or raises without effect), and exactly one history entry
    records it. That is what makes "one intentional action, one undo" true for
    composite edits as well as single ones.
    """

    #: how the snapshot is named in refusal messages
    snapshot_label = "Project"

    def __init__(self, snapshot: S, *, saved: bool = True) -> None:
        self._snapshot = snapshot
        self._undo: list[HistoryEntry[S]] = []
        self._redo: list[HistoryEntry[S]] = []
        # the content as it exists in a file (None: nothing matches it yet, which
        # is how recovered or newly created work starts out unsaved)
        self._saved: S | None = snapshot if saved else None

    @property
    def snapshot(self) -> S:
        return self._snapshot

    @snapshot.setter
    def snapshot(self, value: S) -> None:
        """Install a snapshot **without** recording an edit.

        This is not an editing route: New/Open and the test fixtures use it, and
        doing it between history entries is exactly what :meth:`undo` refuses to
        paper over (a session whose snapshot moved outside history cannot be
        rewound safely).
        """
        self._snapshot = value

    # ------------------------------------------------------------------ edits
    def check(self, candidate: S) -> None:
        """Hook for subclasses: raise to refuse a snapshot that must not be adopted.

        The default accepts everything — a bare composition is validated by the
        commands that produce it. :class:`~video_editor.project.document.DocumentSession`
        uses this to keep a structurally unsound *document* out of history, so
        undo cannot be asked to restore a state that could never be saved.
        """

    def apply(self, command: SnapshotCommand[S]) -> bool:
        """Run a command atomically. Raises its error type without changes.

        Returns True when the snapshot actually changed (and history grew).
        """
        after = command.apply(self._snapshot)
        self.check(after)
        if after == self._snapshot:
            return False
        self._undo.append(HistoryEntry(self._snapshot, after, command.describe()))
        self._redo.clear()
        self._snapshot = after
        return True

    # ----------------------------------------------------------------- history
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> S:
        if not self._undo:
            raise HistoryError("Nothing to undo")
        entry = self._undo.pop()
        if self._snapshot is not entry.after:
            # external mutation would invalidate linear history; refuse rather
            # than silently corrupt state
            self._undo.append(entry)
            raise HistoryError(
                f"{self.snapshot_label} changed outside history; undo unavailable"
            )
        self._snapshot = entry.before
        self._redo.append(entry)
        return self._snapshot

    def redo(self) -> S:
        if not self._redo:
            raise HistoryError("Nothing to redo")
        entry = self._redo.pop()
        if self._snapshot is not entry.before:
            self._redo.append(entry)
            raise HistoryError(
                f"{self.snapshot_label} changed outside history; redo unavailable"
            )
        self._snapshot = entry.after
        self._undo.append(entry)
        return self._snapshot

    def clear_history(self) -> None:
        self._undo.clear()
        self._redo.clear()

    # ------------------------------------------------------------- dirty state
    @property
    def is_dirty(self) -> bool:
        """True when no file holds this content as it now stands.

        Compared against the *content* saved last, not against an edit counter:
        undoing back to the saved content clears the mark, redoing it returns.
        A session with no saved baseline at all (recovered work) is always dirty
        until it is written.
        """
        return self._saved is None or self._snapshot != self._saved

    def mark_saved(self) -> None:
        self._saved = self._snapshot

    def replace(self, snapshot: S, *, keep_dirty: bool = False) -> None:
        """Load a new snapshot (New/Open/recovery). History does not survive;
        this is documented behaviour (docs/architecture.md).

        ``keep_dirty=True`` means the incoming content has no file behind it —
        a recovered snapshot — so the baseline is cleared rather than adopted.
        """
        self._snapshot = snapshot
        self._undo.clear()
        self._redo.clear()
        self._saved = None if keep_dirty else snapshot


class ProjectSession(EditHistory[Project]):
    """Composition-only session: a convenience for core-level work and tests.

    The application uses the document session instead, because the media
    catalogue is part of what a project file holds and therefore part of what
    undo has to restore.
    """

    def __init__(self, project: Project | None = None, *, saved: bool = True) -> None:
        super().__init__(project if project is not None else Project(), saved=saved)

    @property
    def project(self) -> Project:
        return self.snapshot

    @project.setter
    def project(self, value: Project) -> None:
        self.snapshot = value

    def replace_project(self, project: Project, *, keep_dirty: bool = False) -> None:
        self.replace(project, keep_dirty=keep_dirty)


__all__ = ["EditHistory", "HistoryEntry", "HistoryError", "ProjectSession"]
