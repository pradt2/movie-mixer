"""Viewer mode separation + provenance labelling, and inspector basics."""

import time
from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from media_fixtures import HAS_FFMPEG
from video_editor.core.commands import AddTrackCommand
from video_editor.core.model import TrackKind
from video_editor.ui.main_window import MainWindow
from video_editor.ui.viewer import OutputViewer

F = Fraction


def test_output_viewer_states_what_it_is_showing(qapp_instance: QApplication):
    """No simulation: until a range is rendered it says so in plain words."""
    viewer = OutputViewer()
    viewer.show()
    assert "No preview rendered yet" in viewer.state.text()
    assert "No frame shown" in viewer.still.text()
    assert not viewer.play_button.isEnabled()
    viewer.set_playhead(F(90))
    assert "1:30" in viewer.time_label.text()
    # stale, not current: a rendered range stops being presented as current output
    viewer.preview_ready(Path("/tmp/preview-x.mp4"), F(0), F(4), F(1, 2))
    assert viewer.play_button.isEnabled()
    assert "same composition the export path renders" in viewer.provenance.text()
    viewer.mark_stale("You trimmed a clip")
    assert viewer.state.text().startswith("STALE")
    assert not viewer.play_button.isEnabled()
    viewer.close()


def test_the_preview_range_says_what_it_selects(qapp_instance: QApplication):
    """Explicit output-interval selection, explained where it is typed.

    A preview is an ordinary render of a chosen output interval; that has to be
    legible, or the fields read as decoration next to a button that decides the
    range itself.
    """
    from PySide6.QtWidgets import QLabel

    from video_editor.core.model import OutputSettings, Project
    from video_editor.render.preview import DEFAULT_PREVIEW_SECONDS

    viewer = OutputViewer()
    viewer.show()
    hints = [child.text() for child in viewer.findChildren(QLabel)
             if child.objectName() == "preview_range_hint"]
    assert len(hints) == 1, "the interval fields must be explained exactly once"
    assert f"{float(DEFAULT_PREVIEW_SECONDS):.0f} s around the playhead" in hints[0]

    # the button's own label matches what clicking it would do with these fields
    project = Project(output=OutputSettings(128, 96, F(25), 48000, "stereo"))
    viewer.set_output(project)  # empty composition: fields refilled automatically
    assert viewer.render_button.text() == "Preview typed range"

    # and it follows a *user* edit, not only a programmatic one
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    viewer.range_start.setFocus()
    viewer.range_start.selectAll()
    QTest.keyClick(viewer.range_start, Qt.Key.Key_Backspace)
    assert not viewer.range_start.text()
    assert viewer.render_button.text() == "Preview around playhead"
    QTest.keyClicks(viewer.range_start, "0:00:01.000")
    assert viewer.render_button.text() == "Preview typed range"
    viewer.close()


def test_main_window_mode_switching(qapp_instance: QApplication):
    window = MainWindow()
    window.show()
    assert window.viewer_mode.itemData(0) == "source"
    assert window.viewer_mode.itemData(1) == "output"
    window.viewer_mode.setCurrentIndex(1)
    assert window.viewer_stack.currentWidget() is window.output_viewer
    window.viewer_mode.setCurrentIndex(0)
    assert window.viewer_stack.currentWidget() is window.source_viewer
    window.close()


@pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixture generation")
def test_source_viewer_states_timebase(qapp_instance: QApplication, tmp_path):
    from media_fixtures import make_video_only

    window = MainWindow()
    path = make_video_only(tmp_path)
    # drive the controller for the label assertions
    window.bin_controller.request_import([path])

    deadline = time.monotonic() + 30
    while window.bin_controller.pending_imports and time.monotonic() < deadline:
        qapp_instance.processEvents()
        time.sleep(0.01)
    for _ in range(100):
        qapp_instance.processEvents()
        time.sleep(0.01)
    asset = window.bin_controller.assets[0]
    stream = asset.metadata.stream(0)
    window.source_viewer.set_source(asset, stream)
    text = window.source_viewer.provenance.text()
    assert "asset-local" in text
    assert path.name in text
    # time label updates only from playback; before playback it stays neutral
    assert window.source_viewer.time_label.text().count("/") == 1
    window.close()


def test_inspector_shows_clip_read_only(qapp_instance: QApplication):
    window = MainWindow()
    window._apply(AddTrackCommand(TrackKind.AUDIO, "A1"))
    track = window.session.project.tracks[0]
    window.inspector.show_track(window.session.project, track)
    rows = {
        window.inspector.details.topLevelItem(i).text(0): (
            window.inspector.details.topLevelItem(i).text(1)
        )
        for i in range(window.inspector.details.topLevelItemCount())
    }
    assert rows["Name"] == "A1"
    assert rows["Kind"] == "audio"
    assert rows["Muted"] == "no"
    window.close()
