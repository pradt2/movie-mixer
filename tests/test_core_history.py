"""History: undo/redo atomicity, redo invalidation, dirty state, stable IDs."""

import pytest

from core_helpers import F, add_clip, make_base
from video_editor.core.commands import (
    AddTrackCommand,
    CommandError,
    MoveClipCommand,
    SplitClipCommand,
)
from video_editor.core.history import HistoryError, ProjectSession
from video_editor.core.model import Project, TrackKind


def session_with_clip() -> ProjectSession:
    session = ProjectSession(make_base())
    session.project = add_clip(session.project, "c1", "vt", F(0), F(10))
    session.clear_history()
    session.mark_saved()
    return session


def test_undo_restores_previous_state():
    session = ProjectSession(make_base())
    session.project = add_clip(session.project, "c1", "vt", F(0), F(10))
    session.clear_history()
    assert session.apply(MoveClipCommand("c1", output_start=F(4)))
    assert session.project.clip("c1").output_start == F(4)
    session.undo()
    assert session.project.clip("c1").output_start == F(0)


def test_undo_redo_roundtrip_with_ids_stable():
    session = session_with_clip()
    session.apply(SplitClipCommand("c1", F(4)))
    split_ids = {c.id for c in session.project.clips}
    assert len(split_ids) == 2
    session.undo()
    assert {c.id for c in session.project.clips} == {"c1"}
    session.redo()
    assert {c.id for c in session.project.clips} == split_ids
    session.undo()
    session.redo()  # ids never change between undo/redo cycles
    assert {c.id for c in session.project.clips} == split_ids


def test_new_edit_clears_redo():
    session = session_with_clip()
    session.apply(MoveClipCommand("c1", output_start=F(5)))
    session.undo()
    assert session.can_redo
    session.apply(AddTrackCommand(TrackKind.AUDIO, "A2"))
    assert not session.can_redo


def test_noop_command_does_not_touch_history():
    session = session_with_clip()
    changed = session.apply(MoveClipCommand("c1", output_start=F(0)))
    assert changed is False
    assert not session.can_undo
    assert not session.is_dirty


def test_dirty_state_tracks_saved_content():
    session = session_with_clip()
    assert not session.is_dirty
    session.apply(MoveClipCommand("c1", output_start=F(5)))
    assert session.is_dirty
    session.undo()
    assert not session.is_dirty  # back to saved content
    session.redo()
    assert session.is_dirty


def test_failed_command_leaves_history_untouched():
    session = session_with_clip()
    with pytest.raises(CommandError):
        session.apply(MoveClipCommand("nope", output_start=F(1)))
    assert not session.can_undo
    assert session.project.clip("c1").output_start == F(0)


def test_history_refuses_after_external_mutation():
    session = session_with_clip()
    session.apply(MoveClipCommand("c1", output_start=F(5)))
    # external poke without going through apply() would corrupt linear history
    session.project = add_clip(session.project, "c2", "vt", F(50), F(5))
    with pytest.raises(HistoryError):
        session.undo()


def test_replace_project_resets_history():
    session = session_with_clip()
    session.apply(MoveClipCommand("c1", output_start=F(5)))
    session.replace_project(Project())
    assert not session.can_undo
    assert not session.can_redo
    assert not session.is_dirty


def test_track_removal_undo_restores_clips():
    from video_editor.core.commands import RemoveTrackCommand

    session = session_with_clip()
    session.apply(RemoveTrackCommand("vt"))
    assert session.project.clips == ()
    session.undo()
    assert {c.id for c in session.project.clips} == {"c1"}
    assert session.project.track("vt") is not None


def test_recovered_work_has_no_saved_baseline():
    """Recovery restores unsaved work: there is no file to be 'clean' against.

    A baseline copied from the incoming content would report recovered work as
    saved, so a close or a New project would silently drop it.
    """
    session = ProjectSession(saved=False)
    assert session.is_dirty  # nothing written, even before any edit
    session.apply(AddTrackCommand(TrackKind.VIDEO, "A"))
    assert session.is_dirty
    session.undo()
    assert session.is_dirty  # still nothing on disk behind it
    session.mark_saved()
    assert not session.is_dirty


def test_replace_project_keep_dirty_clears_the_baseline():
    session = session_with_clip()
    recovered = session.project
    session.replace_project(recovered, keep_dirty=True)
    assert session.is_dirty
    assert not session.can_undo  # history still does not cross a load
    session.mark_saved()
    assert not session.is_dirty
