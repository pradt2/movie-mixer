"""Viewer mode separation + provenance labelling, and inspector basics."""

import time
from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from media_fixtures import HAS_FFMPEG
from video_editor.core.commands import AddTrackCommand
from video_editor.core.model import TrackKind
from video_editor.media.framegrab import StillFrame
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


def test_cut_buttons_follow_the_picture_actually_on_screen(qapp_instance: QApplication):
    """What the viewer will and will not let the user cut, and where it says why.

    The viewer owns the buttons the user presses, so its own rules are checked here
    without any media: a step in flight makes the picture on screen *not* the one
    asked for, a failed step never showed the frame that was asked for, a black frame
    names no clip, and a picture whose source position was not measured cannot be cut
    at. Every refusal states its reason in the control that is disabled.
    """
    def frame(**changes) -> StillFrame:
        values = dict(
            width=4, height=4, rgb=b"\x11" * 48, output_time=F(0), frame_index=0,
            source_pts=F(1, 25), decoded_source_pts=F(1, 25),
            clip_id="vid", asset_id="steps", stream_index=0,
        )
        values.update(changes)
        return StillFrame(**values)

    viewer = OutputViewer()
    viewer.show()
    # nothing on screen: nothing to step from, nothing to cut, and the labels say
    # what a cut would do — the out point excludes the picture under it
    assert not viewer.source_fwd_button.isEnabled()
    assert not viewer.mark_in_button.isEnabled()
    assert viewer.mark_in_button.text() == "Set clip in (this frame)"
    assert viewer.mark_out_button.text() == "Set clip out (before this frame)"
    assert "EXCLUDING this picture" in viewer.mark_out_button.toolTip()
    assert "first frame" in viewer.mark_in_button.toolTip()

    viewer.show_still(frame())
    assert viewer.source_fwd_button.isEnabled()
    assert viewer.mark_in_button.isEnabled()
    assert viewer.shown_source_pts() == F(1, 25)

    # a step in flight: cutting now would trim a frame the user is not looking at
    viewer.source_step_pending(2)
    assert not viewer.mark_in_button.isEnabled()
    assert "not the one" in viewer.mark_in_button.toolTip()
    assert "2 frame(s) later" in viewer.time_label.text()
    # the step buttons stay available: further clicks accumulate in order
    assert viewer.source_fwd_button.isEnabled()

    viewer.source_step_abandoned()  # later clicks cancelled the requested move
    assert viewer.mark_out_button.isEnabled()

    # a step that failed leaves the earlier real picture up, but the frame that was
    # asked for was never shown, so no cut may be taken from here
    viewer.source_step_failed(
        "Could not find the next source frame: not read far enough to know"
    )
    assert viewer.shown_source_pts() == F(1, 25), "the honest picture was thrown away"
    assert "not read far enough" in viewer.time_label.text()
    assert not viewer.mark_in_button.isEnabled()
    assert "not read far enough" in viewer.mark_out_button.toolTip()
    assert viewer.source_fwd_button.isEnabled()
    viewer.set_mark_block(None)
    assert viewer.mark_in_button.isEnabled()

    # an unmeasured position is not a verified one
    viewer.show_still(frame(decoded_source_pts=None))
    assert viewer.shown_source_pts() is None
    assert not viewer.source_fwd_button.isEnabled()
    assert not viewer.mark_out_button.isEnabled()

    # a truthful black frame names no clip to trim
    viewer.show_still(frame(black=True, clip_id=None, asset_id=None))
    assert not viewer.mark_in_button.isEnabled()
    assert "black here" in viewer.mark_in_button.toolTip()

    # a refused decode drops the stale image instead of leaving it looking current
    viewer.show_still(frame())
    viewer.still_failed("no output frame at 5s: the composition ends at 4s")
    assert viewer.still.text().startswith("No frame shown")
    assert viewer.shown_source_pts() is None
    assert not viewer.mark_out_button.isEnabled()

    viewer.show_still(frame())
    viewer.clear_shown_frame("Preview cache freed")
    assert viewer.still.text().startswith("No frame shown")
    assert not viewer.source_back_button.isEnabled()
    assert not viewer.mark_in_button.isEnabled()
    viewer.close()


def test_a_composition_still_says_when_its_source_frame_was_not_measured(qapp_instance):
    """A predicted source time is shown as a prediction, never as an observation.

    The composition's still path can be certain which picture it decoded while saying
    honestly that it cannot tie that picture to an input timestamp (no exact filter
    tick for this stream and tempo). The viewer has to keep those two statements apart:
    the planned time stays visible, labelled as what it is, and the cut controls stay
    shut - because a mark taken from a prediction would land at a frame the user is
    not looking at.
    """
    def still(**changes) -> StillFrame:
        values = dict(
            width=4, height=4, rgb=b"\x11" * 48, output_time=F(40, 25), frame_index=40,
            source_pts=F(8, 3), decoded_source_pts=None, grid_exact=False,
            clip_id="vid", asset_id="src", stream_index=0,
        )
        values.update(changes)
        return StillFrame(**values)

    viewer = OutputViewer()
    viewer.show()
    viewer.show_still(still())
    provenance = viewer.provenance.text()
    assert "not measured" in provenance, provenance
    assert "actual source frame not measured" in provenance
    assert "source frame at PTS" not in provenance, "an unmeasured frame was claimed"
    assert "8/3" in provenance, "the predicted time disappeared instead of being labelled"
    assert "8/3" in viewer.time_label.text()
    assert "not measured" in viewer.time_label.text()
    assert viewer.shown_source_pts() is None
    assert not viewer.source_fwd_button.isEnabled()
    assert not viewer.mark_in_button.isEnabled()
    assert not viewer.mark_out_button.isEnabled()
    assert "could not be measured" in viewer.mark_in_button.toolTip()

    # the same frame with a stamp that *was* established: both numbers are said
    viewer.show_still(still(decoded_source_pts=F(66, 25), grid_exact=True))
    provenance = viewer.provenance.text()
    assert "66/25" in provenance and "8/3" in provenance, provenance
    assert "established from this decode" in provenance
    assert "not measured" not in provenance
    assert "66/25" in viewer.time_label.text()
    viewer.close()


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
