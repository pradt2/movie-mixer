"""Discoverability: the in-app workflow help and the chords it promises.

The list of shortcuts is generated from the actions that are actually bound, and
the timeline's own keys are declared next to the code that handles them. Both
rules exist because of a real defect: redo was documented as `Ctrl+Shift+Z` while
an ambiguous binding made it do nothing (`docs/progress.md`, TASK_07). A hand
typed list can drift again; a generated one cannot claim a chord that is not
bound.
"""

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from video_editor.ui import main_window as module
from video_editor.ui.main_window import MainWindow
from video_editor.ui.timeline import WIDGET_KEYS


@pytest.fixture()
def window(qapp_instance: QApplication, tmp_path: Path, monkeypatch):
    """A window of its own, with its caches inside this test's directory."""
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("VIDEO_EDITOR_RECOVERY_DIR", str(tmp_path / "recovery"))
    monkeypatch.setenv("VIDEO_EDITOR_AUTOSAVE_SECONDS", "0")
    win = MainWindow()
    yield win
    win.close()


def bound_chords(win: MainWindow) -> list[tuple[str, str]]:
    """(command, chord) pairs for every shortcut the window really binds."""
    pairs = []
    for entry in win.menuBar().actions():
        menu = entry.menu()
        if menu is None:
            continue
        for action in menu.actions():
            for sequence in action.shortcuts():
                if not sequence.isEmpty():
                    pairs.append((action.text().replace("&", ""), sequence.toString()))
    return pairs


# ------------------------------------------------------------------- the list


def test_every_bound_shortcut_is_documented_in_the_help_dialog(window):
    text = window._help_text()
    missing = [
        f"{command} = {chord}"
        for command, chord in bound_chords(window)
        if chord not in text
    ]
    assert not missing, f"bound but undocumented: {missing}"


def test_the_help_dialog_documents_no_chord_that_is_not_bound(window):
    """The generated half of the text cannot invent bindings.

    The timeline's own keys are the explicit exception, and they are declared in
    ``timeline.WIDGET_KEYS`` next to the key handler, not retyped here.
    """
    section = window._help_text().split("Shortcuts as this build binds them")[1]
    section = section.split("Handled by the timeline itself")[0]
    documented = {chord for _command, chord in bound_chords(window)}
    extra = []
    for line in section.splitlines():
        line = line.strip()
        if not line or line.startswith("Handled"):
            continue
        for chord in line[:22].strip().split(", "):
            if chord and chord not in documented:
                extra.append(f"{chord} in {line!r}")
    assert not extra, f"documented but not bound: {extra}"


def test_undo_and_redo_chords_are_both_documented(window):
    """The chord list is where the dead-redo defect would have been noticed."""
    text = window._help_text()
    row = next(line for line in text.splitlines() if "Undo" in line)
    assert "Ctrl+Z" in row
    row = next(line for line in text.splitlines() if "Redo" in line)
    assert "Ctrl+Shift+Z" in row
    assert "Ctrl+Y" in row


def test_zoom_is_documented_as_the_timeline_handles_it(window):
    """Zoom is +/- on the timeline, not Ctrl+Plus: an old comment claimed both."""
    text = window._help_text()
    assert "Ctrl+Plus" not in text and "Ctrl+Minus" not in text
    for chord, meaning in WIDGET_KEYS:
        assert chord in text and meaning in text


# ---------------------------------------------------------------- the workflow


def test_the_workflow_steps_name_the_operations_they_depend_on(window):
    """A recipe is only useful if the operations it mentions are real words."""
    text = "\n".join(module.WORKFLOW_STEPS)
    for mention in (
        "Import", "Media Status", "frame rate", "Add Video/Audio Track", "playhead",
        "Ctrl+G", "ripple", "waveform", "thumbnails", "split", "trim", "delete",
        "gap", "undoable", "preview", "Export", "Save", "relink",
    ):
        assert mention.lower() in text.lower(), f"workflow never mentions {mention!r}"


def test_the_workflow_keeps_picture_and_sound_independent(window):
    text = "\n".join(module.WORKFLOW_STEPS)
    assert "independently" in text
    assert "No file is treated as the reference" in text


def test_the_help_text_covers_the_controls_that_have_no_chord(window):
    """Things you can only find in a menu or on a widget need naming in words."""
    text = window._help_text().lower()
    for mention in ("snapping", "enable", "muted", "typed exactly", "audio needs no video"):
        assert mention in text, f"help never mentions {mention!r}"


# -------------------------------------------------------------------- the dialog


def test_the_help_dialog_is_reachable_from_the_help_menu(window, qapp_instance):
    titles = [action.text() for action in window.menuBar().actions()]
    assert any("Help" in title for title in titles)
    actions = {a.text().replace("&", "") for a in window.menuBar().actions()[-1].menu().actions()}
    assert "Keyboard and Workflow…" in actions

    window.help_action.trigger()
    qapp_instance.processEvents()
    assert window._help_box is not None
    assert window._help_box.objectName() == "help_dialog"
    assert window._help_box.toPlainText().startswith("Manual workflow")
    window._help_box.close()


def test_the_help_dialog_does_not_block_the_window(window, qapp_instance):
    """An unattended run must never be held up by a modal dialog."""
    window.help_action.trigger()
    qapp_instance.processEvents()
    box = window._help_box
    assert not box.isModal()
    assert box.windowModality() is Qt.WindowModality.NonModal
    assert window.isEnabled()
    box.close()


def test_the_about_box_points_at_help_instead_of_its_own_chord_list(window):
    """Two hand kept lists is how the chord documentation drifted once already."""
    window._show_about()
    text = window._about_box.toPlainText()
    assert "Keyboard and Workflow" in text
    assert "Shortcuts:" not in text, "About has grown its own hand-kept chord list again"
    window._about_box.close()


def test_a_new_shortcut_cannot_be_added_without_appearing(window, monkeypatch):
    """The guard runs on the real menu bar, so a new action is covered for free."""
    from PySide6.QtGui import QAction

    extra = QAction("Ne&w Thing", window)
    extra.setShortcut("Ctrl+K")
    window.menuBar().actions()[-1].menu().addAction(extra)
    text = window._help_text()
    assert "New Thing" in text and "Ctrl+K" in text
