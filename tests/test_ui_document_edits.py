"""Window-level document edits (TASK_10): import, place, split, relink, undo.

These drive the real window offscreen, because the point of the task is that
the *editor* keeps one consistent state: the media catalogue belongs to the
saved document, placement is one action, a split leaves usable groups, and
nothing is left behind when an edit is refused. Prompts are recorded rather
than shown, so the run stays unattended.

Fixtures are synthetic generated media, never a user's files.
"""

from __future__ import annotations

import json
import time
from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QApplication, QFileDialog

from media_fixtures import HAS_FFMPEG, make_colour_steps, make_garbage, make_mixed
from video_editor.core.commands import (
    AddTrackCommand,
    LinkClipsCommand,
    MoveClipCommand,
    RegisterSourceCommand,
)
from video_editor.core.model import Project, StreamSource, TrackKind
from video_editor.project.recovery import RecoveryStore, session_key
from video_editor.project.serialize import Navigation
from video_editor.project.storage import build_document
from video_editor.ui.main_window import MainWindow
from video_editor.ui.media_bin import ROLE_ASSET_ID, ROLE_STREAM_INDEX

F = Fraction

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixtures")


# ------------------------------------------------------------------ plumbing


def import_wait(app: QApplication, window: MainWindow, paths: list[Path]) -> None:
    window.bin_controller.request_import(list(paths))
    deadline = time.monotonic() + 60
    while window.bin_controller.pending_imports and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    for _ in range(150):  # drain the queued completion signals
        app.processEvents()
        time.sleep(0.01)
    assert window.bin_controller.pending_imports == 0


def stream_item(window: MainWindow, asset_id: str, stream_index: int):
    for i in range(window.media_bin.topLevelItemCount()):
        top = window.media_bin.topLevelItem(i)
        if top.data(0, ROLE_ASSET_ID) != asset_id:
            continue
        for j in range(top.childCount()):
            child = top.child(j)
            if int(child.data(0, ROLE_STREAM_INDEX)) == stream_index:
                return child
    raise AssertionError(f"stream {asset_id}#{stream_index} is not in the bin")


def select_only(window: MainWindow, items) -> None:
    selection = window.media_bin.selectionModel()
    selection.clearSelection()
    for item in items:
        selection.select(
            window.media_bin.indexFromItem(item),
            QItemSelectionModel.Select | QItemSelectionModel.Rows,
        )


def asset_by_path(window: MainWindow, path: Path):
    for asset in window.bin_controller.assets:
        if Path(asset.path) == Path(path):
            return asset
    raise AssertionError(f"{path} was not imported")


class PromptLog(list):
    """The prompts a window tried to show, and how this run answers them.

    Nothing modal may block an unattended run, so both the message box and the
    save dialog are replaced: ``answer`` names the button, ``save_target`` is the
    path a "Save…" answer writes.
    """

    def __init__(self, save_target: str) -> None:
        super().__init__()
        self.answer = "Save…"
        self.save_target = save_target
        #: the body of each prompt, so a test can check the *reason* it was refused
        self.texts: list[str] = []


@pytest.fixture()
def prompts(monkeypatch, tmp_path):
    """Record prompts instead of showing them, and answer them on the test's behalf."""
    log = PromptLog(str(tmp_path / "prompt-save.vep"))

    class Answer:
        def __init__(self, label: str) -> None:
            self._label = label

        def exec(self) -> int:
            return 0

        def clickedButton(self):
            return self

        def text(self) -> str:
            return self._label

    def record(self, title, text, detail, buttons, default):
        log.append(title)
        log.texts.append(f"{text} {detail}")
        return Answer(log.answer if log.answer in [label for label, _ in buttons]
                      else buttons[0][0])

    monkeypatch.setattr(MainWindow, "_message_box", record)
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *a, **k: (log.save_target, "")
    )
    return log


@pytest.fixture()
def window(qapp_instance, tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("VIDEO_EDITOR_RECOVERY_DIR", str(tmp_path / "recovery"))
    monkeypatch.setenv("VIDEO_EDITOR_AUTOSAVE_SECONDS", "0")
    win = MainWindow()
    win.show()
    qapp_instance.processEvents()
    yield win, tmp_path
    win.close()


# --------------------------------------------------------------------- import


def test_an_import_with_nothing_placed_is_saved_work(window, qapp_instance, prompts):
    """A file in the bin is part of the project, even with no clip using it."""
    win, tmp = window
    film = make_colour_steps(tmp)
    assert not win.session.is_dirty
    import_wait(qapp_instance, win, [film])

    assert win.session.is_dirty  # the saved document would now differ
    assert win.save_project_action.isEnabled()
    assert "*" in win.windowTitle()
    assert len(win.session.document.assets) == 1
    assert win.session.project.clips == ()

    # one undo removes the import — bin row included — and redo restores it
    win._undo()
    assert win.session.document.assets == ()
    assert win.media_bin.topLevelItemCount() == 0
    assert not win.session.is_dirty
    win._redo()
    assert len(win.session.document.assets) == 1
    assert win.media_bin.topLevelItemCount() == 1
    assert win.session.is_dirty

    # unsaved media is unsaved work: starting a new project has to ask, and the
    # answer "Save…" has to write the media list, not just the composition
    project_file = tmp / "media-only.vep"
    prompts.save_target = str(project_file)
    win._new_project()
    assert prompts[0] == "Unsaved work"
    assert project_file.is_file(), "the Save… answer wrote nothing"
    assert win.session.document.assets == ()  # the window really is new now
    assert win.media_bin.topLevelItemCount() == 0
    assert not win.session.is_dirty

    # and what was saved reopens as media, not only as a composition
    second = MainWindow()
    assert second._open_project(project_file)
    assert len(second.session.document.assets) == 1
    assert Path(second.session.document.assets[0].path) == film
    assert second.session.project.clips == ()
    assert not second.session.is_dirty
    second.close()


def test_a_failed_or_cancelled_import_does_not_dirty_the_project(window, qapp_instance):
    win, tmp = window
    bad = make_garbage(tmp)
    import_wait(qapp_instance, win, [bad])
    assert win.session.document.assets == ()
    assert not win.session.is_dirty
    assert win.media_bin.topLevelItemCount() == 0


def test_an_import_lands_in_the_recovery_snapshot(window, qapp_instance):
    """Autosave covers the catalogue: an interrupted session keeps its media."""
    win, tmp = window
    film = make_colour_steps(tmp)
    import_wait(qapp_instance, win, [film])
    win._take_recovery_snapshot("autosave")
    found = RecoveryStore(session_key(win.project_path)).list()
    assert found, "no snapshot was written for an import-only change"


# ------------------------------------------------------------------- placing


def test_one_undo_reverses_a_whole_placement(window, qapp_instance):
    """Registration, lane and clip: one action, one undo, no leftovers."""
    win, tmp = window
    film = make_colour_steps(tmp)
    import_wait(qapp_instance, win, [film])
    asset = asset_by_path(win, film)
    before_tracks = len(win.session.project.tracks)
    before_sources = len(win.session.project.sources)

    win.timeline.set_playhead(F(0))
    select_only(win, [stream_item(win, asset.id, 0)])
    win._place_selected_streams()
    assert len(win.session.project.clips) == 1
    assert len(win.session.project.tracks) == before_tracks + 1  # a lane was needed
    assert len(win.session.project.sources) == before_sources + 1  # registered
    clip = win.session.project.clips[0]

    win._undo()  # one undo, not three
    assert win.session.project.clips == ()
    assert len(win.session.project.tracks) == before_tracks
    assert len(win.session.project.sources) == before_sources
    assert win.session.project != win.session.project.with_clips((clip,))

    win._redo()
    assert len(win.session.project.clips) == 1
    assert win.session.project.clips[0].id == clip.id  # stable id survives redo


def test_a_refused_insert_leaves_no_lane_and_no_registration(window, qapp_instance):
    """The gesture that failed is not a partial one."""
    win, tmp = window
    film = make_colour_steps(tmp)
    import_wait(qapp_instance, win, [film])
    asset = asset_by_path(win, film)
    select_only(win, [stream_item(win, asset.id, 0)])
    win._place_selected_streams()
    placed = win.session.project.clips[0]

    tracks = len(win.session.project.tracks)
    sources = len(win.session.project.sources)
    # a second picture dropped *inside* the placed one: that lane is occupied
    # there and ripple-insert cannot make room, so the whole gesture is refused
    second = make_colour_steps(tmp, name="second.mkv")
    import_wait(qapp_instance, win, [second])
    other = asset_by_path(win, second)
    win.timeline.set_playhead(placed.output_start + F(1, 2))  # inside the placed clip
    select_only(win, [stream_item(win, other.id, 0)])
    win._insert_selected_streams()

    assert len(win.session.project.clips) == 1  # nothing was inserted
    assert len(win.session.project.tracks) == tracks  # and no lane was created for it
    assert len(win.session.project.sources) == sources  # nor a stream registered
    assert "refused" in win.statusBar().currentMessage().lower()


def test_a_group_placement_is_one_action(window, qapp_instance):
    win, tmp = window
    both = make_mixed(tmp)
    import_wait(qapp_instance, win, [both])
    asset = asset_by_path(win, both)
    indexes = [stream.stream_index for stream in asset.metadata.streams]
    tracks = len(win.session.project.tracks)

    select_only(win, [stream_item(win, asset.id, index) for index in indexes])
    win._place_selected_streams()
    assert len(win.session.project.clips) == len(indexes)
    win._undo()
    assert win.session.project.clips == ()
    assert len(win.session.project.tracks) == tracks


# --------------------------------------------------------------- linked splits


def test_a_split_leaves_the_two_halves_independently_editable(window, qapp_instance):
    """The behaviour the review flagged: all four halves used to stay linked."""
    win, tmp = window
    both = make_mixed(tmp)
    import_wait(qapp_instance, win, [both])
    asset = asset_by_path(win, both)
    video = next(s.stream_index for s in asset.metadata.streams if s.kind.value == "video")
    audio = next(s.stream_index for s in asset.metadata.streams if s.kind.value == "audio")

    select_only(win, [stream_item(win, asset.id, video), stream_item(win, asset.id, audio)])
    win._place_selected_streams()
    picture, sound = win.session.project.clips
    win._apply(LinkClipsCommand((picture.id, sound.id)))
    group = win.session.project.clip(picture.id).link_group
    assert group is not None

    win.timeline.set_playhead(F(2))
    win.timeline.set_clip_selection((picture.id,))
    win._split_selected_at_playhead()
    left = {clip.id for clip in win.session.project.clips if clip.output_start == F(0)}
    right_ids = {clip.id for clip in win.session.project.clips if clip.output_start == F(2)}
    assert picture.id in left and len(left) == 2 and len(right_ids) == 2
    # the left pair keeps the original link, the right pair has its own
    assert all(win.session.project.clip(cid).link_group == group for cid in left)
    right_groups = {win.session.project.clip(cid).link_group for cid in right_ids}
    assert len(right_groups) == 1 and group not in right_groups

    # moving the right picture does not drag the left one
    right_picture = next(
        clip for clip in win.session.project.clips
        if clip.id in right_ids and clip.track_id == picture.track_id
    )
    win._apply(MoveClipCommand(right_picture.id, output_start=F(20)))
    assert win.session.project.clip(picture.id).output_start == F(0)
    assert win.session.project.clip(right_picture.id).output_start == F(20)

    # and the right pair can be split again on its own: the split follows the
    # link, so it makes two more pieces and one more group, and the left pair
    # (the original group) is untouched
    win.timeline.set_playhead(F(22))
    win.timeline.set_clip_selection((right_picture.id,))
    win._split_selected_at_playhead()
    assert len(win.session.project.clips) == 6
    groups = {clip.link_group for clip in win.session.project.clips}
    assert len(groups) == 3 and None not in groups  # left, right-left, right-right
    assert group in groups

    # undoing the split, the move and the split before it restores one group
    for _ in range(3):
        win._undo()
    assert len(win.session.project.clips) == 2
    assert {clip.link_group for clip in win.session.project.clips} == {group}


def test_a_split_of_an_unlinked_clip_leaves_both_halves_unlinked(window, qapp_instance):
    win, tmp = window
    film = make_colour_steps(tmp)
    import_wait(qapp_instance, win, [film])
    asset = asset_by_path(win, film)
    select_only(win, [stream_item(win, asset.id, 0)])
    win._place_selected_streams()
    clip = win.session.project.clips[0]
    win.timeline.set_playhead(F(1))
    win.timeline.set_clip_selection((clip.id,))
    win._split_selected_at_playhead()
    assert all(c.link_group is None for c in win.session.project.clips)


# ------------------------------------------------------------------- relink


def test_a_relink_of_identical_streams_is_saved_work_and_undoes_wholly(
    window, qapp_instance
):
    """Media moved, composition did not: still dirty, still one undo."""
    win, tmp = window
    film = make_colour_steps(tmp)
    import_wait(qapp_instance, win, [film])
    asset = asset_by_path(win, film)
    select_only(win, [stream_item(win, asset.id, 0)])
    win._place_selected_streams()
    win._write_project(tmp / "film.vep")
    assert not win.session.is_dirty

    clips_before = win.session.project.clips
    sources_before = win.session.project.sources
    replacement = make_colour_steps(tmp, name="renamed.mkv")
    assert win._relink_asset_to(asset, replacement) is True

    assert win.session.is_dirty  # the document now names a different file
    assert win.session.project.clips == clips_before  # composition untouched
    assert win.session.project.sources == sources_before
    assert Path(win.session.document.assets[0].path) == replacement
    assert win.session.document.assets[0].size_bytes == replacement.stat().st_size
    assert win.media_bin.topLevelItem(0).toolTip(0) == str(replacement)

    win._undo()
    assert Path(win.session.document.assets[0].path) == film  # path back…
    assert (
        win.session.document.assets[0].size_bytes == film.stat().st_size
    )  # …and the identity recorded for it
    assert win.session.project.sources == sources_before
    assert not win.session.is_dirty

    win._redo()
    assert Path(win.session.document.assets[0].path) == replacement
    assert win.session.is_dirty


def test_saving_after_history_navigation_writes_what_history_selects(window, qapp_instance):
    """Save must write the document on screen, not the last edit made."""
    win, tmp = window
    film = make_colour_steps(tmp)
    import_wait(qapp_instance, win, [film])
    asset = asset_by_path(win, film)
    select_only(win, [stream_item(win, asset.id, 0)])
    win._place_selected_streams()
    replacement = make_colour_steps(tmp, name="other.mkv")
    win._relink_asset_to(asset, replacement)
    win._undo()  # back to the original file, with the clip still placed

    project_file = tmp / "chosen.vep"
    assert win._write_project(project_file) == project_file
    second = MainWindow()
    assert second._open_project(project_file)
    assert Path(second.session.document.assets[0].path) == film
    assert len(second.session.project.clips) == 1
    assert not second.session.is_dirty
    second.close()


def test_a_new_project_empties_the_bin_as_well_as_the_timeline(window, prompts):
    win, _tmp = window
    assert win._apply(AddTrackCommand(TrackKind.VIDEO, "V1"))
    prompts.answer = "Discard"
    win._new_project()
    assert win.session.project.tracks == ()
    assert win.session.document.assets == ()
    assert win.media_bin.topLevelItemCount() == 0
    assert not win.session.is_dirty


def test_a_project_file_that_names_unrecorded_media_is_refused(window, prompts):
    """The two halves of a document have to agree, so a file that breaks that
    rule is not loaded at all — and the work in the window is untouched."""
    win, tmp = window
    project = RegisterSourceCommand(
        StreamSource("ghost", 0, TrackKind.VIDEO, F(0), F(10), frame_rate=F(25),
                     stream_position=0)
    ).apply(Project())
    broken = tmp / "broken.vep"
    # written exactly as the application writes a file, minus the media record
    # the composition names: a real file shape, not a hand-typed guess
    broken.write_text(json.dumps(
        build_document(project, (), Navigation(), "0.0-test", broken)
    ))
    assert win._open_project(broken) is False
    assert prompts == ["Project could not be opened"]
    assert "not in the media catalogue" in prompts.texts[0]
    assert win.session.document.assets == ()
    assert win.session.project.tracks == ()


def test_a_corrupt_project_file_leaves_the_open_document_alone(window, qapp_instance, prompts):
    """A file the loader cannot accept cannot damage the document in the window."""
    win, tmp = window
    film = make_colour_steps(tmp)
    import_wait(qapp_instance, win, [film])
    win._write_project(tmp / "work.vep")
    assert not win.session.is_dirty

    for payload in ('{"schema_version": 99}', "not json at all"):
        bad = tmp / "bad.vep"
        bad.write_text(payload)
        assert win._open_project(bad) is False

    assert len(win.session.document.assets) == 1
    assert Path(win.session.document.assets[0].path) == film
    assert not win.session.is_dirty
    assert prompts == ["Project could not be opened", "Project could not be opened"]
    assert "schema version 99" in prompts.texts[0]
    assert "not readable JSON" in prompts.texts[1]
