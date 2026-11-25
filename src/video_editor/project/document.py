"""The persistent document: the composition **and** the media catalogue.

A project file does not only hold tracks and clips — it also names the files
the clips read, with the identity and probe metadata recorded for them. So the
thing that is saved, and therefore the thing undo has to restore, is both. When
only the composition was under history (the state before TASK_10), importing a
file could change the saved document without setting the dirty mark, and a
relink could leave the catalogue and the registered streams describing
different files after an undo.

Layering: this module composes pure :class:`~video_editor.core.model.Project`
values and the :class:`~video_editor.core.commands` that edit them with the
media records from :mod:`video_editor.media.assets`. It contains no Qt, no
subprocess calls and no filesystem writes — probing happens in ``media``,
publishing files happens in :mod:`video_editor.project.storage`.

The transaction API for future assisted edits:

* :class:`EditorDocument` — the immutable snapshot: composition + catalogue.
* :class:`DocumentCommand` — anything with ``apply(document) -> document`` and
  ``describe() -> str``. It must return a complete new document or raise; it
  never mutates its input and never touches disk.
* :class:`CompositeDocumentCommand` — several document commands as one
  transaction: computed on a candidate, published once, one history entry.
* :class:`DocumentSession` — the single baseline: history, dirty state and the
  "is this content saved?" question for the whole document.

Anything that is *not* persistent stays outside the document: playhead, zoom,
scroll, selection, import jobs in flight, and what the disk currently says
about a file (availability is recomputed, never trusted from the snapshot).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from ..core.commands import Command, CommandError, RelinkAssetCommand, validate_project
from ..core.history import EditHistory
from ..core.model import Project
from ..media.assets import Asset
from ..media.availability import same_file
from ..media.relink import RelinkPlan


@dataclass(frozen=True)
class EditorDocument:
    """Everything a project file holds, in one immutable value."""

    project: Project = field(default_factory=Project)
    #: import order is preserved: the bin lists sources as peers, and a
    #: re-sorted catalogue would quietly suggest a preferred edition
    assets: tuple[Asset, ...] = ()

    def asset(self, asset_id: str) -> Asset | None:
        for asset in self.assets:
            if asset.id == asset_id:
                return asset
        return None

    def with_project(self, project: Project) -> EditorDocument:
        return replace(self, project=project)

    def with_assets(self, assets: tuple[Asset, ...]) -> EditorDocument:
        return replace(self, assets=assets)


class DocumentCommand(Protocol):
    """A whole-document transaction: compute the result, or raise and change nothing.

    Composition commands (:class:`~video_editor.core.commands.Command`) are
    wrapped by :class:`EditComposition`; catalogue and combined edits implement
    this protocol directly. :class:`DocumentSession` records exactly one entry
    per accepted command, which is what makes "one action, one undo" true for
    edits that touch both halves of the document.
    """

    def apply(self, document: EditorDocument) -> EditorDocument: ...

    def describe(self) -> str: ...


@dataclass(frozen=True)
class EditComposition:
    """Apply a composition command as part of the document.

    The bridge that lets every existing core command participate in document
    history unchanged — including the composite placement commands, so
    "place this stream" is one entry even when it also registers a stream and
    allocates a track. It is deliberately *not* a subclass of ``core.Command``:
    it takes a document, and pretending otherwise would let a document
    transaction be handed a bare composition by mistake.
    """

    command: Command

    def apply(self, document: EditorDocument) -> EditorDocument:
        return document.with_project(self.command.apply(document.project))

    def describe(self) -> str:
        return self.command.describe()


@dataclass(frozen=True)
class AddAssetCommand:
    """Record a probed file in the catalogue (one import).

    An import is part of the saved document, so it belongs in history: it sets
    the dirty mark, it is undone by undo, and New/Close must ask about it. A
    failed import never gets here — nothing about a file that could not be read
    is recorded.
    """

    asset: Asset

    def apply(self, document: EditorDocument) -> EditorDocument:
        existing = document.asset(self.asset.id)
        if existing is not None:
            raise CommandError(f"Asset {self.asset.id} is already in this project")
        for other in document.assets:
            if same_file(Path(other.path), Path(self.asset.path)):
                raise CommandError(
                    f"'{self.asset.path}' is already imported as "
                    f"'{other.display_name()}'"
                )
        return document.with_assets((*document.assets, self.asset))

    def describe(self) -> str:
        return f"import '{self.asset.display_name()}'"


@dataclass(frozen=True)
class ReplaceAssetCommand:
    """Point an existing asset id at a newly probed file (the media half of a relink).

    The id does not change: clips, saved references and the preview cache key
    all speak of the asset by id, and a relink that invented a new id would
    orphan every clip that uses it.
    """

    asset: Asset

    def apply(self, document: EditorDocument) -> EditorDocument:
        current = document.asset(self.asset.id)
        if current is None:
            raise CommandError(f"No asset {self.asset.id} in this project")
        if current == self.asset:
            return document
        return document.with_assets(
            tuple(self.asset if a.id == self.asset.id else a for a in document.assets)
        )

    def describe(self) -> str:
        return f"relink '{self.asset.display_name()}'"


@dataclass(frozen=True)
class RelinkDocumentCommand:
    """One relink, one transaction: new file in the catalogue, new stream records.

    Both halves change together or not at all, so undo/redo restore the path,
    the recorded identity and the registered bounds as the set they are — a
    composition-only undo would otherwise pair the old streams with the new
    file, and a catalogue-only undo would do the reverse.

    *plan* must already be validated by ``media.relink.relink_asset``: probing
    and validating is filesystem work and belongs to ``media``, while this
    command only publishes the result of that work.
    """

    plan: RelinkPlan

    def apply(self, document: EditorDocument) -> EditorDocument:
        updated = EditComposition(
            RelinkAssetCommand(self.plan.asset.id, self.plan.sources)
        ).apply(document)
        return ReplaceAssetCommand(self.plan.asset).apply(updated)

    def describe(self) -> str:
        return f"relink '{self.plan.asset.display_name()}'"


@dataclass(frozen=True)
class CompositeDocumentCommand:
    """Several document commands as one intentional action.

    The steps run against a candidate document; if any step raises, nothing is
    published and the session is untouched — no half-applied relink, no clip
    whose asset never made it into the catalogue. This is the seam the assisted
    alignment work (TASK_16+, not implemented here) will use to propose a set of
    correspondences and apply them as one undoable edit.
    """

    steps: tuple[DocumentCommand, ...]
    description: str = "change"

    def apply(self, document: EditorDocument) -> EditorDocument:
        if not self.steps:
            return document
        candidate = document
        for step in self.steps:
            candidate = step.apply(candidate)
        return candidate

    def describe(self) -> str:
        return self.description


class DocumentSession(EditHistory[EditorDocument]):
    """The one session the application uses: history over the whole document.

    ``is_dirty`` compares documents, so an import, a relink that changed only
    the file an asset points at, or a composition edit all set the mark — and
    undoing them all clears it again. There is exactly one saved baseline per
    session, which is what Save, the title's ``*``, the New/Close prompt and
    the recovery snapshot all consult.
    """

    snapshot_label = "Document"

    def __init__(self, document: EditorDocument | None = None, *, saved: bool = True) -> None:
        super().__init__(document if document is not None else EditorDocument(), saved=saved)

    @property
    def document(self) -> EditorDocument:
        return self.snapshot

    @document.setter
    def document(self, value: EditorDocument) -> None:
        self.snapshot = value

    def check(self, candidate: EditorDocument) -> None:
        """Refuse a document that does not describe a coherent project.

        A command is expected to validate its own inputs, and this is not a
        substitute for that: it is the net that stops an edit which would leave
        the composition pointing at a media record the catalogue does not have
        (or any other structural break) from becoming the baseline undo returns
        to. The edit is refused *before* it is adopted, so the session, its
        history and its dirty mark are untouched.
        """
        problems = validate_document(candidate)
        if problems:
            raise CommandError(
                "the edit would leave an invalid project: " + "; ".join(problems)
            )

    @property
    def project(self) -> Project:
        """Convenience for callers that only need the composition."""
        return self.snapshot.project

    def apply(self, command):
        """Apply a document transaction; a composition command is wrapped.

        Passing a bare ``core`` command here would otherwise fail deep inside
        that command with an attribute error on a document, so it is taken to
        mean "edit the composition part of the document" and wrapped — the same
        thing :class:`EditComposition` says out loud.
        """
        if isinstance(command, Command):
            command = EditComposition(command)
        return super().apply(command)

    def replace_document(self, document: EditorDocument, *, keep_dirty: bool = False) -> None:
        self.replace(document, keep_dirty=keep_dirty)


def validate_document(document: EditorDocument) -> list[str]:
    """Structural problems with the whole document (empty list = sound).

    Split deliberately from availability: an asset whose file is missing or
    replaced is a **sound** reference with an unusable file behind it, and the
    composition stays openable, editable and saveable. What is refused here is
    a document that does not describe a coherent composition: duplicate ids,
    a stream that names an asset the catalogue does not have, source intervals
    outside the recorded stream bounds, or two clips on one track at the same
    time. Precise wording matters: the message has to say which field or which
    clip is wrong, because the fix is done by hand.
    """
    problems: list[str] = []

    seen_asset_ids: set[str] = set()
    for asset in document.assets:
        if asset.id in seen_asset_ids:
            problems.append(f"duplicate asset id {asset.id}")
        seen_asset_ids.add(asset.id)
        if not str(asset.path).strip():
            problems.append(f"asset {asset.id} has no recorded path")

    registered = {source.asset_id for source in document.project.sources}
    unrecorded = sorted(registered - seen_asset_ids)
    for asset_id in unrecorded:
        streams = sorted(
            source.stream_index
            for source in document.project.sources
            if source.asset_id == asset_id
        )
        listed = ", ".join(str(index) for index in streams)
        problems.append(
            f"streams {listed} of asset {asset_id} are registered, but that asset is "
            "not in the media catalogue"
        )

    problems.extend(validate_project(document.project))
    return problems
