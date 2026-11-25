"""Undo/redo history and dirty-state tracking.

Projects are immutable snapshots, so history can safely store before/after
references (structural sharing makes this cheap, and snapshots are safe to
hand to a concurrent renderer). Undo/redo restore whole transactions;
selection and playhead are UI state and never appear here.

Stable IDs survive undo/redo because snapshots carry the original ids.
"""

from __future__ import annotations

from dataclasses import dataclass

from .commands import Command
from .model import Project


class HistoryError(Exception):
    """Requested history operation is not possible."""


@dataclass(frozen=True)
class HistoryEntry:
    before: Project
    after: Project
    description: str


class ProjectSession:
    """Mutable holder of the current project plus edit history."""

    def __init__(self, project: Project | None = None, *, saved: bool = True) -> None:
        self.project = project if project is not None else Project()
        self._undo: list[HistoryEntry] = []
        self._redo: list[HistoryEntry] = []
        # the content as it exists in a file (None: nothing matches it yet, which
        # is how recovered or newly created work starts out unsaved)
        self._saved = self.project if saved else None

    # ------------------------------------------------------------------ edits
    def apply(self, command: Command) -> bool:
        """Run a command atomically. Raises CommandError without changes.

        Returns True when the project actually changed (and history grew).
        """
        after = command.apply(self.project)
        if after == self.project:
            return False
        self._undo.append(HistoryEntry(self.project, after, command.describe()))
        self._redo.clear()
        self.project = after
        return True

    # ----------------------------------------------------------------- history
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> Project:
        if not self._undo:
            raise HistoryError("Nothing to undo")
        entry = self._undo.pop()
        if self.project is not entry.after:
            # external mutation would invalidate linear history; refuse rather
            # than silently corrupt state
            self._undo.append(entry)
            raise HistoryError("Project changed outside history; undo unavailable")
        self.project = entry.before
        self._redo.append(entry)
        return self.project

    def redo(self) -> Project:
        if not self._redo:
            raise HistoryError("Nothing to redo")
        entry = self._redo.pop()
        if self.project is not entry.before:
            self._redo.append(entry)
            raise HistoryError("Project changed outside history; redo unavailable")
        self.project = entry.after
        self._undo.append(entry)
        return self.project

    def clear_history(self) -> None:
        self._undo.clear()
        self._redo.clear()

    # ------------------------------------------------------------- dirty state
    @property
    def is_dirty(self) -> bool:
        """True when no file holds this composition as it now stands.

        Compared against the *content* saved last, not against an edit counter:
        undoing back to the saved content clears the mark, redoing it returns.
        A session with no saved baseline at all (recovered work) is always dirty
        until it is written.
        """
        return self._saved is None or self.project != self._saved

    def mark_saved(self) -> None:
        self._saved = self.project

    def replace_project(self, project: Project, *, keep_dirty: bool = False) -> None:
        """Load a new project (New/Open/recovery). History does not survive;
        this is documented behaviour (docs/architecture.md).

        ``keep_dirty=True`` means the incoming content has no file behind it —
        a recovered snapshot — so the baseline is cleared rather than adopted.
        """
        self.project = project
        self._undo.clear()
        self._redo.clear()
        self._saved = None if keep_dirty else project
