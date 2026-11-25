"""Helpers shared by the offscreen window tests.

A window's content is **one document** — the composition plus the media
catalogue — so a test that wants a scene installs a document, the same route
Open uses. Before TASK_10 these tests poked the composition on the session and
appended to a separate bin list, which is exactly the pair that could drift out
of step; the helpers keep a fixture as consistent as a saved file has to be.

Assets here are records, not files: a record whose file is not on disk is a
valid (offline) catalogue entry, which is how a test can talk about a 1 h
stream without generating 1 h of media. Tests that measure real pixels or
tones use the generated fixtures in ``media_fixtures``.
"""

from __future__ import annotations

from pathlib import Path

from video_editor.core.model import Project
from video_editor.media.assets import Asset
from video_editor.project.document import EditorDocument
from video_editor.ui.main_window import MainWindow


def media_record(path: Path, asset_id: str) -> Asset:
    """A catalogue record for *path*, with the identity recorded at import."""
    stat = path.stat()
    return Asset(
        id=asset_id,
        path=path,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        metadata=None,
    )


def offline_asset(asset_id: str, name: str | None = None) -> Asset:
    """A sound record for a file that is deliberately not on disk.

    Missing media is a normal state, not corruption: the composition stays
    editable and saveable, and only rendering that material is refused.
    """
    return Asset(
        id=asset_id,
        path=Path(f"/video-editor-tests/absent/{name or f'{asset_id}.mkv'}"),
        size_bytes=1,
        mtime_ns=1,
        metadata=None,
    )


def install_document(
    window: MainWindow, project: Project, assets: tuple[Asset, ...] = ()
) -> EditorDocument:
    """Give *window* a whole document, marked as saved (the Open route)."""
    document = EditorDocument(project=project, assets=assets)
    window.install_document(document)
    return document


def add_media(window: MainWindow, *assets: Asset) -> None:
    """Record media in the document, as an import would (undoable, dirtying)."""
    from video_editor.project.document import AddAssetCommand

    for asset in assets:
        assert window._apply_document(AddAssetCommand(asset))


def import_file(window: MainWindow, path: Path, asset_id: str) -> Asset:
    """Import *path* through the document (the same call an import worker makes)."""
    from video_editor.project.document import AddAssetCommand

    asset = media_record(path, asset_id)
    # the bin row and the duplicate-import index follow the document, exactly as
    # they do after a real import completes
    assert window._apply_document(AddAssetCommand(asset))
    return asset
