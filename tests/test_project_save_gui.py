"""Project files through the real window: save, reopen, relink, recovery.

Headless (offscreen), synthetic fixtures only, and nothing here waits on a
human: every dialog is answered programmatically, and a modal that a test did
not intend is reported as a failure rather than blocking the suite.

The behaviours under test are the ones that decide whether a long session is
safe: does a saved project come back *exactly*, does a bad file damage the
work in progress, does a moved/renamed film still open, and does the recovery
net catch work that was never saved.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from gui_helpers import media_record, offline_asset
from media_fixtures import HAS_FFMPEG, make_colour_steps, make_tone
from test_export_gui import register_asset
from video_editor.core.commands import (
    AddClipCommand,
    AddTrackCommand,
    LinkClipsCommand,
    MoveClipCommand,
    RegisterSourceCommand,
    SetOutputSettingsCommand,
)
from video_editor.core.model import OutputSettings, Project, StreamSource, Track, TrackKind
from video_editor.project import recovery, storage
from video_editor.project.document import EditorDocument
from video_editor.project.serialize import Navigation
from video_editor.ui import main_window
from video_editor.ui.main_window import MainWindow
from video_editor.ui.timeline import BASE_PPS

F = Fraction

# ---------------------------------------------------------------- test plumbing


class Boom:
    """A modal the test did not arrange for: fail loudly instead of blocking."""


class Answerer:
    """Answers ``QMessageBox`` dialogs by button **label**, recording what they said.

    Labels, not positions: Qt reorders buttons by role and platform style (a
    Save/Discard/Cancel box really lays out as Save…, Cancel, Discard), so a test
    that pressed "button 1" could mean something different next release. The real
    ``QMessageBox`` is still constructed — only its modal loop is replaced, by
    clicking the chosen button and returning, which is what a user does.
    """

    def __init__(self, monkeypatch, *choices: str) -> None:
        self.choices = list(choices)
        self.seen: list[tuple[str, list[str], str]] = []
        # a plain function in the class dict, so Qt hands us the box it opened
        # (a bound method would be returned as-is and receive no arguments)
        monkeypatch.setattr(
            QMessageBox, "exec", lambda box, _me=self: _me.answer(box)
        )

    def answer(self, box) -> int:
        buttons = box.buttons()
        labels = [button.text().replace("&", "") for button in buttons]
        title = box.windowTitle()
        text = " ".join(part for part in (box.text(), box.informativeText()) if part)
        if not self.choices:
            raise AssertionError(
                f"unexpected modal: {title} [{', '.join(labels)}] - {text[:160]}"
            )
        choice = self.choices.pop(0)
        if choice not in labels:
            raise AssertionError(f"{title}: buttons are {labels}, "
                                 f"the test asked to press {choice!r}")
        self.seen.append((title, labels, text))
        buttons[labels.index(choice)].click()
        # click() already recorded clickedButton(); the code under test reads
        # that, not this return value
        return 0

    @property
    def remaining(self) -> list[str]:
        """Answers the test arranged but the application never asked for."""
        return list(self.choices)

    @property
    def titles(self) -> list[str]:
        return [title for title, _buttons, _text in self.seen]

    @property
    def all_text(self) -> str:
        return "\n".join(text for _title, _buttons, text in self.seen)


def _pump(app, seconds: float = 1.0) -> None:
    """Process events for a while (never a blocking sleep): lets timers fire."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


# ------------------------------------------------------------------ compositions


def load_offline(window: MainWindow, project: Project) -> None:
    """Install *project* with records for the media its streams name.

    The files are deliberately absent: a project can be opened, edited and
    saved with offline media, and that is what several of these tests are about.
    """
    window.install_document(
        EditorDocument(
            project=project, assets=(offline_asset("steps"), offline_asset("tone"))
        )
    )


def load_online(window: MainWindow, tmp_path: Path, project: Project) -> None:
    """Install *project* with placeholder files for the media its streams name.

    Some of these tests are about what a *saved* project says about its media, so
    the files have to exist: offline records would make every save report
    unavailable media and prompt about it.
    """
    records = []
    for source in project.sources:
        path = tmp_path / f"{source.asset_id}.mkv"
        if not path.exists():
            path.write_bytes(b"placeholder media")
        records.append(media_record(path, source.asset_id))
    window.install_document(EditorDocument(project=project, assets=tuple(records)))


def mixed() -> Project:
    """Two tracks, exact odd fractions, two assets: picture and its own sound."""
    p = Project(output=OutputSettings(128, 96, F(25), 48000, "stereo"))
    p = RegisterSourceCommand(
        StreamSource("steps", 0, TrackKind.VIDEO, F(0), F(6), frame_rate=F(25),
                     width=128, height=96, stream_position=0)
    ).apply(p)
    p = RegisterSourceCommand(
        StreamSource("tone", 0, TrackKind.AUDIO, F(0), F(6), sample_rate=48000,
                     stream_position=0)
    ).apply(p)
    p = p.with_tracks((Track("v", TrackKind.VIDEO, "Picture"),
                       Track("a", TrackKind.AUDIO, "Dub")))
    p = AddClipCommand("steps", 0, "v", F(0), source_in=F(1, 3), source_out=F(10, 3),
                       clip_id="vid").apply(p)
    # 3 source seconds in 2 output seconds: rate = source s / output s = 3/2
    p = AddClipCommand("tone", 0, "a", F(1, 7), source_in=F(0), source_out=F(3, 2),
                       rate=F(3, 2), clip_id="aud").apply(p)
    return p


@pytest.fixture()
def saved_window(qapp_instance, tmp_path, monkeypatch):
    """A window with real media, a linked pair, an odd rate and a placed playhead."""
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("VIDEO_EDITOR_RECOVERY_DIR", str(tmp_path / "recovery"))
    monkeypatch.setenv("VIDEO_EDITOR_AUTOSAVE_SECONDS", "0")
    steps = make_colour_steps(tmp_path) if HAS_FFMPEG else (tmp_path / "steps.mkv")
    tone = make_tone(tmp_path, "tone.wav", 660) if HAS_FFMPEG else (tmp_path / "tone.wav")
    for path in (steps, tone):
        if not path.exists():
            path.write_bytes(b"placeholder")
    win = MainWindow()
    register_asset(win, steps, "steps")
    register_asset(win, tone, "tone")
    win.install_document(
        EditorDocument(project=mixed(), assets=win.session.document.assets)
    )
    win._apply(SetOutputSettingsCommand(128, 96, F(24000, 1001), 48000, "stereo"))
    win._apply(LinkClipsCommand(("vid", "aud")))
    win._refresh_timeline()
    win.timeline.set_playhead(F(7, 3))
    win.timeline.set_view(F(3), BASE_PPS * 3)
    win.timeline.set_clip_selection(("vid", "aud"))
    yield win, tmp_path, steps, tone
    win.close()


# ------------------------------------------------------------- save and reopen


def test_save_then_reopen_restores_the_composition_exactly(saved_window, qapp_instance):
    win, tmp, steps, tone = saved_window
    project_file = tmp / "film.vep"
    assert win._save_project(path=project_file)
    assert win.project_path == project_file
    assert not win.session.is_dirty
    assert win._project_name() in win.windowTitle() and "*" not in win.windowTitle()

    payload = json.loads(project_file.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["output"]["frame_rate"] == "24000/1001"
    assert payload["clips"][1]["rate"] == "3/2"
    assert payload["clips"][0]["source_in"] == "1/3"
    assert {clip["link_group"] for clip in payload["clips"]} == {payload["clips"][0]["link_group"]}
    assert all(asset["path_mode"] == "relative" for asset in payload["assets"])

    second = MainWindow()
    assert second._open_project(project_file)
    assert second.session.project == win.session.project  # exact: Fractions, ids, order
    assert second.project_path == project_file
    assert not second.session.is_dirty
    assert not second.session.can_undo  # history does not cross a load (documented)
    assert [track.name for track in second.session.project.tracks] == ["Picture", "Dub"]
    assert second.session.project.clip("aud").link_group is not None
    assert second.session.project.source("tone", 0).sample_rate == 48000
    assert second.timeline.playhead == F(7, 3)  # navigation came back
    assert second.timeline.pps == BASE_PPS * 3
    assert second.timeline.selected_clips == ("vid", "aud")
    assert {record.asset.id for record in second._media_records} == {"steps", "tone"}
    assert all(r.state is storage.AssetState.ONLINE for r in second._media_records)
    assert {a.id for a in second.bin_controller.assets} == {"steps", "tone"}
    assert second.media_bin.topLevelItemCount() == 2
    expected = {steps.resolve(), tone.resolve()}
    assert {r.resolved_path for r in second._media_records} == expected
    second.close()


def test_dirty_state_tracks_undo_and_redo_against_the_saved_content(saved_window):
    win, tmp, _steps, _tone = saved_window
    project_file = tmp / "dirty.vep"
    win._save_project(path=project_file)
    assert not win.session.is_dirty and "*" not in win.windowTitle()

    assert win._apply(MoveClipCommand("vid", output_start=F(1)))
    assert win.session.is_dirty and "*" in win.windowTitle()

    win._undo()
    assert not win.session.is_dirty  # back to exactly what is on disk
    assert "*" not in win.windowTitle()

    # further back: the link and output-settings edits happened before the save,
    # so the baseline sits *ahead* of them in the history stack
    win._undo()
    assert win.session.is_dirty

    win._redo()  # step forward into the saved content again
    assert not win.session.is_dirty
    win._redo()  # past it, to the moved clip
    assert win.session.is_dirty
    win._undo()
    assert not win.session.is_dirty

    assert win._save_project(path=tmp / "dirty2.vep")
    assert not win.session.is_dirty


def test_navigation_alone_never_makes_a_project_dirty(saved_window):
    win, tmp, _steps, _tone = saved_window
    project_file = tmp / "nav.vep"
    win._save_project(path=project_file)
    win.timeline.set_playhead(F(1, 5))
    win.timeline.set_view(F(1, 2), BASE_PPS * 5)
    win.timeline.set_clip_selection(("aud",))
    assert not win.session.is_dirty  # UI state is not composition data

    assert win._save_project(path=project_file)
    reopened = MainWindow()
    assert reopened._open_project(project_file)
    assert reopened.timeline.playhead == F(1, 5)
    assert reopened.timeline.pps == BASE_PPS * 5
    assert reopened.timeline.selected_clips == ("aud",)
    reopened.close()


def test_save_as_switches_file_and_rewires_relative_references(saved_window, monkeypatch):
    win, tmp, _steps, _tone = saved_window
    other_dir = tmp / "other"
    other_dir.mkdir()
    target = other_dir / "copy.vep"
    target.write_text("existing", encoding="utf-8")
    answers = Answerer(monkeypatch, "Yes")
    assert win._save_project(path=target)
    assert answers.titles == ["Replace existing project file?"]
    assert win.project_path == target
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert all(asset["path_mode"] == "absolute" for asset in payload["assets"])
    reopened = MainWindow()
    assert reopened._open_project(target)
    assert {r.state for r in reopened._media_records} == {storage.AssetState.ONLINE}
    reopened.close()


def test_the_save_action_prompts_before_replacing_an_existing_file(
    saved_window, monkeypatch
):
    win, tmp, _steps, _tone = saved_window
    project_file = tmp / "action.vep"
    win._save_project(path=project_file)
    win._apply(MoveClipCommand("vid", output_start=F(2)))

    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: (str(project_file), "")),
    )
    before = project_file.read_bytes()

    answers = Answerer(monkeypatch, "No")
    assert not win._save_project(save_as=True)
    assert answers.titles == ["Replace existing project file?"]
    assert project_file.read_bytes() == before  # "No" means do not write
    assert win.project_path == project_file
    assert win.session.is_dirty  # the unsaved edit is still there, unwritten

    answers = Answerer(monkeypatch, "Yes")
    assert win._save_project(save_as=True)
    assert project_file.read_bytes() != before  # "Yes" means write it
    assert json.loads(project_file.read_text(encoding="utf-8"))["clips"][0]["output_start"] == "2/1"
    assert not win.session.is_dirty


# ------------------------------------------------------------------- bad files


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("not a project file at all", "not readable JSON"),
        ('{"schema_version": 1, "output": {', "not readable JSON"),
        (
            '{"schema_version": 99, "output": {}, "assets": [], "sources": [], '
            '"tracks": [], "clips": [], "hologram_tracks": [1, 2]}',
            "schema version 99",
        ),
        (
            '{"schema_version": 1, "application": {"name": "other-nle", "version": "9"}, '
            '"output": {}, "assets": [], "sources": [], "tracks": [], "clips": []}',
            "not a video-editor project file",
        ),
        (
            '{"schema_version": 1, "output": {"width": 1, "height": 2, '
            '"frame_rate": "25/1", "sample_rate": 48000, "channel_layout": "stereo"}, '
            '"assets": [], "sources": [], "tracks": [], "clips": [], '
            '"clips_are_easy": true}',
            "does not understand",
        ),
    ],
)
def test_a_bad_project_file_leaves_the_current_work_intact(
    saved_window, monkeypatch, text, expected
):
    win, tmp, _steps, _tone = saved_window
    broken = tmp / "broken.vep"
    broken.write_text(text, encoding="utf-8")
    win._apply(MoveClipCommand("vid", output_start=F(1)))
    playhead = win.timeline.playhead
    clips_before = win.session.project.clips
    sources_before = win.session.project.sources

    answers = Answerer(monkeypatch, "OK")
    assert not win._open_project(broken)
    assert answers.titles == ["Project could not be opened"]
    assert expected in answers.all_text
    assert "unchanged" in answers.all_text  # the dialog says the work is safe
    assert win.session.project.clips == clips_before  # nothing was replaced
    assert win.session.project.sources == sources_before
    assert win.timeline.playhead == playhead
    assert win.session.is_dirty  # the unsaved edit is still here
    assert len(win.bin_controller.assets) == 2


def test_a_future_schema_version_is_named_and_not_rewritten(saved_window, monkeypatch):
    win, tmp, _steps, _tone = saved_window
    future = tmp / "future.vep"
    future.write_text(
        json.dumps({"schema_version": 7, "output": {}, "assets": [], "sources": [],
                    "tracks": [], "clips": [], "scene_graph": {"new": True}}),
        encoding="utf-8",
    )
    before = future.read_text(encoding="utf-8")
    Answerer(monkeypatch, "OK")
    assert not win._open_project(future)
    assert future.read_text(encoding="utf-8") == before  # never "repaired"


# ------------------------------------------------------------ unsaved prompts


def test_new_and_quit_prompt_about_unsaved_work(saved_window, monkeypatch):
    win, tmp, _steps, _tone = saved_window
    # the suite normally closes windows without a prompt; this test asks for the
    # prompt a real application start-up turns on
    win.quit_confirm_required = True
    win._apply(MoveClipCommand("vid", output_start=F(3)))
    assert win.session.is_dirty

    answers = Answerer(monkeypatch, "Cancel")
    win._new_project()
    assert answers.titles == ["Unsaved work"]
    assert win.session.project.clips  # still here, still unsaved

    answers = Answerer(monkeypatch, "Discard")
    win._new_project()
    assert not win.session.project.clips
    assert not win.session.project.tracks
    assert win.project_path is None
    assert win.bin_controller.assets == ()
    assert win.media_bin.topLevelItemCount() == 0

    # a fresh edit on the new project is unsaved work again…
    assert win._apply(AddTrackCommand(TrackKind.VIDEO, "Fresh"))
    answers = Answerer(monkeypatch, "Cancel")  # keeps the window open
    assert not win._confirm_quit()
    assert answers.titles == ["Unsaved work"]
    assert win.session.project.tracks

    # …and choosing Save writes it before the window may close
    project_file = tmp / "kept.vep"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(project_file), ""))
    )
    answers = Answerer(monkeypatch, "Save…")
    assert win._confirm_quit()
    assert project_file.is_file()
    assert not win.session.is_dirty
    assert win.project_path == project_file


def test_the_quit_prompt_is_off_for_programmatic_window_closing(qapp_instance, monkeypatch):
    """Unit tests close windows after edits; that must not raise a modal."""
    monkeypatch.setenv("VIDEO_EDITOR_RECOVERY_DIR", "/tmp/opencode/unused-recovery")
    answers = Answerer(monkeypatch)  # any modal is a failure
    win = MainWindow()
    load_offline(win, mixed())
    win._refresh_timeline()
    assert win._apply(MoveClipCommand("vid", output_start=F(1)))
    assert win.session.is_dirty  # real unsaved work, and closing is still quiet
    win.close()
    assert answers.seen == []


# ------------------------------------------------------------------- atomicity


def test_a_failed_save_leaves_the_previous_file_byte_identical(saved_window, monkeypatch):
    win, tmp, _steps, _tone = saved_window
    project_file = tmp / "atomic.vep"
    win._save_project(path=project_file)
    before = project_file.read_text(encoding="utf-8")

    def explode(_src, _dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage.os, "replace", explode)
    win._apply(MoveClipCommand("vid", output_start=F(4)))
    answers = Answerer(monkeypatch, "OK")
    assert not win._save_project(path=project_file)
    assert answers.titles == ["Save failed"]
    assert "No space left on device" in answers.all_text
    assert project_file.read_text(encoding="utf-8") == before
    assert win.session.is_dirty  # still unsaved, nothing silently dropped
    assert list(tmp.glob(".*tmp*")) == []  # no staging debris
    assert win.project_path == project_file


def test_save_refuses_a_missing_directory_without_debris(saved_window, monkeypatch):
    win, tmp, _steps, _tone = saved_window
    answers = Answerer(monkeypatch, "OK")
    assert not win._save_project(path=tmp / "gone" / "sub.vep")
    assert answers.titles == ["Save failed"]
    assert win.project_path is None
    assert list(tmp.rglob(".*tmp*")) == []


# ------------------------------------------------------- moved media, relinking


def test_relative_references_follow_a_moved_project_and_media(saved_window, monkeypatch):
    win, tmp, steps, tone = saved_window
    project_file = tmp / "moved.vep"
    win._save_project(path=project_file)
    payload = json.loads(project_file.read_text(encoding="utf-8"))
    assert all(asset["path_mode"] == "relative" for asset in payload["assets"])

    moved = tmp / "moved"
    moved.mkdir()
    project_file.rename(moved / project_file.name)
    steps.rename(moved / steps.name)
    tone.rename(moved / tone.name)

    reopened = MainWindow()
    assert reopened._open_project(moved / "moved.vep")
    states = {r.asset.id: r.state for r in reopened._media_records}
    assert set(states.values()) == {storage.AssetState.ONLINE}
    assert reopened._missing_asset_names() == []
    reopened.close()


def test_a_long_timeline_checks_each_asset_once_not_once_per_clip(saved_window, monkeypatch):
    """Feature-length timelines make a per-clip file check a per-playhead cost.

    ``_missing_asset_names`` guards rendering and runs whenever a still frame is
    requested, i.e. on every playhead move. Reading the same handful of files once
    per clip measured ~5 ms per move for 843 clips pointing at two files.
    """
    from dataclasses import replace

    win, _tmp, steps, _tone = saved_window
    project = win.session.project
    original = next(c for c in project.clips if c.id == "vid")
    span = original.output_duration
    crowded = tuple(
        replace(original, id=f"many{index}", output_start=index * span)
        for index in range(300)
    )
    # same online catalogue, far more clips: what matters here is that a long
    # timeline still checks each asset once, not once per clip
    win.install_document(win.session.document.with_project(
        project.with_clips(crowded + project.clips)
    ))
    assert len(win.session.project.clips) == 302

    stats: list[str] = []
    real_path = main_window.Path

    class CountingPath(real_path):
        def is_file(self):
            stats.append(str(self))
            return real_path.is_file(self)

    monkeypatch.setattr(main_window, "Path", CountingPath)
    assert win._missing_asset_names() == []
    assert len(stats) <= 2, f"{len(stats)} file reads for 301 clips: each asset must be " \
        "read once, not once per clip"

    steps.unlink()
    named = win._missing_asset_names()
    assert len(named) == 1, f"one missing asset must be reported once: {named}"
    assert "steps.mkv" in named[0] and str(steps) in named[0]


def test_a_moved_asset_offers_relink_and_rendering_stays_refused_until_it_happens(
    saved_window, monkeypatch, tmp_path
):
    if not HAS_FFMPEG:  # the relink needs a real probe to validate against
        pytest.skip("requires ffprobe fixtures")
    win, tmp, steps, tone = saved_window
    project_file = tmp / "relink.vep"
    win._save_project(path=project_file)

    media_dir = tmp / "media"
    media_dir.mkdir()
    shutil.copy2(steps, media_dir / steps.name)
    shutil.copy2(tone, media_dir / tone.name)
    steps.unlink()  # the film was moved out of the project folder

    second = MainWindow()
    answers = Answerer(monkeypatch, "Keep as is")
    assert second._open_project(project_file)
    assert answers.titles == ["Missing or changed media"]
    offline = {r.asset.id: r.state for r in second._media_records}
    assert offline["steps"] is storage.AssetState.MISSING
    assert offline["tone"] is storage.AssetState.ONLINE
    assert second.session.project == win.session.project  # composition untouched
    refusal = second._preview_state_ok()
    assert refusal and "Rendering is refused" in refusal and "missing" in refusal
    assert second._missing_asset_names()

    target = next(
        asset for asset in second.bin_controller.assets if asset.id == "steps"
    )
    assert second._relink_asset_to(target, media_dir / steps.name)
    assert {r.asset.id: r.state for r in second._media_records}["steps"] is (
        storage.AssetState.ONLINE
    )
    assert second._preview_state_ok() is None
    assert second.session.is_dirty  # a relink is an ordinary undoable edit
    second._undo()
    assert second.session.project == win.session.project  # undo restores the old bounds
    second.close()


def test_an_incompatible_replacement_is_refused_with_reasons(
    saved_window, monkeypatch, tmp_path
):
    if not HAS_FFMPEG:
        pytest.skip("requires ffprobe fixtures")
    win, tmp, steps, tone = saved_window
    project_file = tmp / "wrong.vep"
    win._save_project(path=project_file)
    tone.unlink()  # the dub is gone

    second = MainWindow()
    Answerer(monkeypatch, "Keep as is")
    assert second._open_project(project_file)
    target = next(a for a in second.bin_controller.assets if a.id == "tone")
    wrong = tmp / "wrong-picture.mkv"
    shutil.copy2(steps, wrong)  # a *video* file where the dub should be

    answers = Answerer(monkeypatch, "OK")
    assert not second._relink_asset_to(target, wrong)
    assert answers.titles == ["Relink refused"]
    assert {r.asset.id: r.state for r in second._media_records}["tone"] is (
        storage.AssetState.MISSING
    )
    assert second.session.project == win.session.project  # nothing was changed


def test_a_relink_to_an_identical_file_changes_the_media_not_the_composition(
    saved_window, tmp_path
):
    """A byte-for-byte replacement: no edit happened, and the app must say so.

    The composition half of a relink can legitimately change nothing, because the
    replacement reports exactly the streams that were already recorded. It is
    still a media change — the asset now reads from another file — so the window
    must complete the relink, report which half changed, and leave the undo
    history alone instead of reporting a phantom edit (or a silent failure).
    """
    if not HAS_FFMPEG:
        pytest.skip("requires ffprobe fixtures")
    win, tmp, _steps, tone = saved_window
    media = tmp / "media"
    media.mkdir()
    shutil.copy2(tone, media / tone.name)  # same content, different place

    before = win.session.project
    undoable = win.session.can_undo
    target = next(a for a in win.bin_controller.assets if a.id == "tone")
    assert win._relink_asset_to(target, media / tone.name)

    assert win.session.project == before, "an identical replacement must not edit the project"
    assert win.session.can_undo == undoable, "a no-op relink must not enter history"
    message = win.statusBar().currentMessage()
    assert "Relinked" in message and "composition is unchanged" in message, message
    assert win._asset_paths()["tone"] == media / tone.name
    assert not win._missing_asset_names()


def test_media_status_lists_what_the_disk_says_now(saved_window, monkeypatch):
    win, tmp, _steps, tone = saved_window
    project_file = tmp / "status.vep"
    win._save_project(path=project_file)
    tone.unlink()
    second = MainWindow()
    # "Relink…" would open a file chooser this test cannot answer
    answers = Answerer(monkeypatch, "Keep as is", "OK")
    second._open_project(project_file)
    assert answers.titles == ["Missing or changed media"]
    second._show_media_status()  # opens, lists, and does not block
    assert answers.titles[-1] == "Media status"
    listing = answers.all_text
    assert "missing" in listing and str(tone) in listing
    assert "online" in listing  # the picture is still where it was saved
    second.close()


# -------------------------------------------------------------------- recovery


def test_autosave_snapshots_unsaved_work_and_restore_brings_it_back(
    qapp_instance, tmp_path, monkeypatch
):
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("VIDEO_EDITOR_RECOVERY_DIR", str(tmp_path / "recovery"))
    monkeypatch.setenv("VIDEO_EDITOR_AUTOSAVE_SECONDS", "0.05")
    steps = make_colour_steps(tmp_path) if HAS_FFMPEG else (tmp_path / "steps.mkv")
    tone = make_tone(tmp_path, "tone.wav", 660) if HAS_FFMPEG else (tmp_path / "tone.wav")
    for path in (steps, tone):
        if not path.exists():
            path.write_bytes(b"placeholder")
    win = MainWindow()
    register_asset(win, steps, "steps")
    register_asset(win, tone, "tone")
    win.install_document(
        EditorDocument(project=mixed(), assets=win.session.document.assets)
    )
    win._refresh_timeline()
    win._apply(MoveClipCommand("vid", output_start=F(1)))
    playhead = win.timeline.playhead

    store = recovery.RecoveryStore("unsaved")
    _pump(qapp_instance, 2.0)
    assert len(store.list()) == 1, "the autosave timer did not produce a snapshot"
    snapshot = store.list()[0]
    assert snapshot.meta.reason == "autosave"
    assert snapshot.meta.project_path is None

    # an unchanged session does not keep piling up snapshots
    _pump(qapp_instance, 0.5)
    assert len(store.list()) == 1
    win.close()

    second = MainWindow()  # a window does not offer recovery by itself…
    assert second._startup_recovery_candidate() is not None
    assert not second.session.project.clips
    # …but it can restore when asked
    answers = Answerer(monkeypatch, "Restore")
    assert second.offer_startup_recovery()
    assert answers.titles == ["Unsaved work was found"]
    assert second.session.project == win.session.project
    assert second.timeline.playhead == playhead
    assert second.session.is_dirty  # restored work is unsaved by definition
    assert second.project_path is None  # it must be saved deliberately
    second.close()


def test_an_autosave_interval_of_zero_writes_nothing(qapp_instance, tmp_path, monkeypatch):
    """"0 disables" has to mean disabled.

    QTimer treats an interval of 0 as "as fast as possible", so a naive reading
    of the environment variable turns the safety net into a disk hammer.
    """
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("VIDEO_EDITOR_RECOVERY_DIR", str(tmp_path / "recovery"))
    monkeypatch.setenv("VIDEO_EDITOR_AUTOSAVE_SECONDS", "0")
    win = MainWindow()
    load_offline(win, mixed())
    win._refresh_timeline()
    assert win._apply(MoveClipCommand("vid", output_start=F(1)))
    _pump(qapp_instance, 1.0)
    store = recovery.RecoveryStore(recovery.session_key(None))
    assert store.list() == [], "autosave is switched off but wrote a snapshot"
    win.close()
    # closing with unsaved work is still one deliberate safety net
    assert [snapshot.meta.reason for snapshot in store.list()] == ["closed with unsaved work"]


def test_a_snapshot_never_silently_replaces_a_newer_project_file(
    qapp_instance, tmp_path, monkeypatch
):
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("VIDEO_EDITOR_RECOVERY_DIR", str(tmp_path / "recovery"))
    monkeypatch.setenv("VIDEO_EDITOR_AUTOSAVE_SECONDS", "0")
    win = MainWindow()
    load_online(win, tmp_path, mixed())
    project_file = tmp_path / "race.vep"
    win._save_project(path=project_file)
    win._apply(MoveClipCommand("vid", output_start=F(5)))
    win._take_recovery_snapshot(reason="autosave")

    stat = project_file.stat()
    future_ns = int(time.time_ns() + 10 * 10**9)  # the file is newer than the snapshot
    os.utime(project_file, ns=(stat.st_atime_ns, future_ns))

    store = recovery.RecoveryStore(recovery.session_key(project_file))
    snapshot = store.list()[0]
    refusal = recovery.disk_ahead_of_snapshot(snapshot)
    assert refusal and "written after this snapshot" in refusal

    answers = Answerer(monkeypatch, "OK")
    assert not win._restore_snapshot(snapshot)
    assert answers.titles == ["Restore refused"]
    assert snapshot.path.is_file()  # nothing was deleted or rewritten
    win.close()


def test_a_start_up_window_finds_the_snapshot_of_the_project_that_was_open(
    saved_window, monkeypatch
):
    """Recovery must survive the crash it exists for.

    Snapshots are keyed per project, but after an abnormal exit the application
    starts with **no** project open. If discovery only looked under the current
    key, the snapshot of the film you were editing would sit unreadable in the
    recovery directory. So a window with no project finds them, and restoring one
    also reclaims the project file it was taken for — while the work is still
    shown as unsaved, because nothing on disk holds it.
    """
    win, tmp, _steps, _tone = saved_window
    project_file = tmp / "crash.vep"
    assert win._save_project(path=project_file)
    assert win._apply(AddTrackCommand(TrackKind.AUDIO, "Rescore"))  # an unsaved edit
    win._take_recovery_snapshot(reason="before the crash")
    win.close()

    second = MainWindow()  # a fresh start-up window: no project open yet
    candidate = second._startup_recovery_candidate()
    assert candidate is not None, "the snapshot of the project that was open is invisible"
    assert "before the crash" in recovery.describe_snapshot(candidate)

    answers = Answerer(monkeypatch, "Restore")
    assert second.offer_startup_recovery()
    assert answers.titles == ["Unsaved work was found"]
    assert second.project_path == project_file, "restore must reclaim the project file"
    assert second.session.is_dirty, "recovered work is never presented as saved"
    assert any(track.name == "Rescore" for track in second.session.project.tracks)
    second.close()


def test_an_open_project_is_not_off_another_project_s_recovery_work(
    saved_window, monkeypatch
):
    """Cross-project discovery is for a start-up window, not for mixing work."""
    win, tmp, _steps, _tone = saved_window
    mine = tmp / "mine.vep"
    assert win._save_project(path=mine)
    win._apply(AddTrackCommand(TrackKind.AUDIO, "Rescore"))
    win._take_recovery_snapshot(reason="mine")

    other_file = tmp / "other.vep"
    other = MainWindow()
    assert other._save_project(path=other_file)
    assert other._startup_recovery_candidate() is None, (
        "a project's window was offered another project's unsaved work"
    )
    other.close()
    win.close()


def test_startup_recovery_is_off_for_windows_and_on_for_the_application(
    qapp_instance, tmp_path, monkeypatch
):
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("VIDEO_EDITOR_RECOVERY_DIR", str(tmp_path / "recovery"))
    monkeypatch.setenv("VIDEO_EDITOR_AUTOSAVE_SECONDS", "0")
    store = recovery.RecoveryStore("unsaved")
    store.write(
        storage.build_document(
            mixed(), (offline_asset("steps"), offline_asset("tone")), Navigation(),
            "0.0-test", None,
        ),
        reason="autosave",
        label="untitled",
    )
    quiet = MainWindow()
    assert quiet._startup_recovery_candidate() is not None
    Answerer(monkeypatch)  # any modal from this window is a failure
    assert not quiet.session.project.clips
    quiet.close()
