"""GUI editing flows: drag move/trim, snapping, inspector edits, undo/redo."""

from fractions import Fraction
from unittest.mock import patch

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog

from gui_helpers import install_document, offline_asset
from video_editor.core.commands import (
    AddClipCommand,
    AddTrackCommand,
    InsertClipCommand,
    LinkClipsCommand,
    MoveClipCommand,
    RegisterSourceCommand,
)
from video_editor.core.model import Clip, Project, StreamSource, Track, TrackKind
from video_editor.ui.main_window import MainWindow
from video_editor.ui.timeline import ROW_HEIGHT, RULER_HEIGHT

F = Fraction

VIDEO_SRC = StreamSource("va", 0, TrackKind.VIDEO, F(0), F(3600), frame_rate=F(25))
AUDIO_SRC = StreamSource("aa", 1, TrackKind.AUDIO, F(0), F(3600), sample_rate=48000)


def build_scene(window: MainWindow) -> None:
    """Two video clips [0,10)+[20,30) on V1, one audio [0,10) on A1.

    The media records are offline on purpose: the edits under test are about
    the composition, and a record with no file behind it is a valid document
    state (rendering that material would be refused, editing it is not).
    """
    project = Project()
    project = RegisterSourceCommand(VIDEO_SRC).apply(project)
    project = RegisterSourceCommand(AUDIO_SRC).apply(project)
    project = project.with_tracks(
        (Track("vt", TrackKind.VIDEO, "V1"), Track("at", TrackKind.AUDIO, "A1"))
    )
    project = AddClipCommand(
        "va", 0, "vt", F(0), source_in=F(0), source_out=F(10), clip_id="v1"
    ).apply(project)
    project = AddClipCommand(
        "va", 0, "vt", F(20), source_in=F(20), source_out=F(30), clip_id="v2"
    ).apply(project)
    project = AddClipCommand(
        "aa", 1, "at", F(0), source_in=F(0), source_out=F(10), clip_id="a1"
    ).apply(project)
    install_document(window, project, (offline_asset("va"), offline_asset("aa")))


def setup_view(window: MainWindow) -> None:
    window.timeline.set_view(F(0), F(25))  # 25 px per second
    window.timeline.snap_enabled = False


def press(timeline, x: int, y: int, modifiers=Qt.KeyboardModifier.NoModifier):
    pos = QPoint(x, y)
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, pos, timeline.viewport().mapToGlobal(pos),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, modifiers,
    )
    timeline.mousePressEvent(event)


def move(timeline, x: int, y: int):
    pos = QPoint(x, y)
    event = QMouseEvent(
        QMouseEvent.Type.MouseMove, pos, timeline.viewport().mapToGlobal(pos),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    timeline.mouseMoveEvent(event)


def release(timeline, x: int, y: int):
    pos = QPoint(x, y)
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, pos, timeline.viewport().mapToGlobal(pos),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    )
    timeline.mouseReleaseEvent(event)


def press_escape(timeline) -> None:
    event = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
    )
    timeline.keyPressEvent(event)


def row_y(timeline, row: int) -> int:
    return RULER_HEIGHT + row * ROW_HEIGHT + ROW_HEIGHT // 2


@pytest.fixture()
def scene(app_qt: QApplication):
    window = MainWindow()
    window.resize(1200, 800)
    window.show()
    build_scene(window)
    setup_view(window)
    yield window
    window.close()


@pytest.fixture()
def app_qt(qapp_instance: QApplication) -> QApplication:
    return qapp_instance


# ---------------------------------------------------------------- drag moves


def test_drag_moves_clip_one_command_on_release(scene: MainWindow):
    tl = scene.timeline
    clip = scene.session.project.clip("v1")
    x = int(tl.pixel_at_time(clip.output_start + F(5)))
    press(tl, x, row_y(tl, 0))
    move(tl, x + 50, row_y(tl, 0))  # 2 s right at 25 px/s
    release(tl, x + 50, row_y(tl, 0))
    moved = scene.session.project.clip("v1")
    assert moved.output_start == F(2)
    assert moved.output_end == F(12)
    # v2 untouched (no ripple)
    assert scene.session.project.clip("v2").output_start == F(20)
    # one history entry; undo restores
    scene._undo()
    assert scene.session.project.clip("v1").output_start == F(0)


def test_drag_to_compatible_track(scene: MainWindow):
    tl = scene.timeline
    # add a second video track below
    scene._apply(AddTrackCommand(TrackKind.VIDEO, "V2"))
    scene.timeline.set_project(scene.session.project)
    second_row = next(i for i, t in enumerate(scene.session.project.tracks) if t.name == "V2")
    clip = scene.session.project.clip("v1")
    x = int(tl.pixel_at_time(clip.output_start + F(5)))
    press(tl, x, row_y(tl, 0))
    move(tl, x, row_y(tl, second_row))
    release(tl, x, row_y(tl, second_row))
    moved = scene.session.project.clip("v1")
    target = next(t.id for t in scene.session.project.tracks if t.name == "V2")
    assert moved.track_id == target
    scene._undo()
    assert scene.session.project.clip("v1").track_id == "vt"


def test_drag_onto_same_track_overlap_rejected_unchanged(scene: MainWindow):
    tl = scene.timeline
    messages = []
    tl.statusMessage.connect(messages.append)
    status_msgs = []
    scene.statusBar().messageChanged.connect(status_msgs.append)
    clip = scene.session.project.clip("v2")
    x = int(tl.pixel_at_time(clip.output_start + F(1)))
    press(tl, x, row_y(tl, 0))
    move(tl, x - 300, row_y(tl, 0))  # 12 s left -> [8,18) overlaps v1 [0,10)
    release(tl, x - 300, row_y(tl, 0))
    assert scene.session.project.clip("v2").output_start == F(20)  # unchanged
    assert any("refused" in m.lower() for m in status_msgs)


def test_escape_cancels_drag(scene: MainWindow):
    tl = scene.timeline
    clip = scene.session.project.clip("v1")
    x = int(tl.pixel_at_time(clip.output_start + F(5)))
    press(tl, x, row_y(tl, 0))
    move(tl, x + 100, row_y(tl, 0))
    assert tl._gesture is not None and tl._gesture.started
    press_escape(tl)
    assert tl._gesture is None
    release(tl, x + 100, row_y(tl, 0))
    assert scene.session.project.clip("v1").output_start == F(0)  # nothing applied


def test_drag_snaps_to_clip_boundary_within_pixel_tolerance(scene: MainWindow):
    tl = scene.timeline
    tl.snap_enabled = True
    clip = scene.session.project.clip("v2")  # out [20,30)
    x = int(tl.pixel_at_time(clip.output_start + F(1)))  # t=21, grab offset 1 s
    # release so the tentative start lands 3 px past t=10 (tolerance 8 px)
    target_x = int(tl.pixel_at_time(F(11))) + 3
    press(tl, x, row_y(tl, 0))
    move(tl, target_x, row_y(tl, 0))
    release(tl, target_x, row_y(tl, 0))
    assert scene.session.project.clip("v2").output_start == F(10)  # exact snap
    assert scene.session.project.clip("v2").output_end == F(20)


def test_drag_linked_group_cannot_pass_time_zero(scene: MainWindow):
    """Ghost must stop at 0 so the release cannot promise a rejected edit."""
    scene._apply(LinkClipsCommand(("v2", "a1")))  # a1 starts at 0
    tl = scene.timeline
    clip = scene.session.project.clip("v2")
    x = int(tl.pixel_at_time(clip.output_start + F(1)))
    press(tl, x, row_y(tl, 0))
    move(tl, x - 250, row_y(tl, 0))  # want -10 s: impossible, a1 is at 0
    assert tl._gesture.tentative  # ghost painted at the clamped position
    starts = [v[0] for v in tl._gesture.tentative.values()]
    assert min(starts) == F(0)
    msgs = []
    scene.statusBar().messageChanged.connect(msgs.append)
    release(tl, x - 250, row_y(tl, 0))
    assert scene.session.project.clip("v2").output_start == F(20)  # nothing moved
    assert scene.session.project.clip("a1").output_start == F(0)
    assert not any("refused" in m.lower() for m in msgs)
    # the only history entry is the link itself: the no-op drag added none
    scene._undo()
    assert scene.session.project.clip("v2").link_group is None
    assert not scene.session.can_undo


def test_drag_linked_pair_moves_both(scene: MainWindow):
    scene._apply(LinkClipsCommand(("v1", "a1")))
    tl = scene.timeline
    clip = scene.session.project.clip("v1")
    x = int(tl.pixel_at_time(clip.output_start + F(5)))
    press(tl, x, row_y(tl, 0))
    move(tl, x + 25, row_y(tl, 0))
    release(tl, x + 25, row_y(tl, 0))
    assert scene.session.project.clip("v1").output_start == F(1)
    assert scene.session.project.clip("a1").output_start == F(1)


# --------------------------------------------------------------------- trims


def test_trim_right_drag_shortens_source_exact(scene: MainWindow):
    tl = scene.timeline
    clip = scene.session.project.clip("v1")
    edge_x = int(tl.pixel_at_time(clip.output_end)) - 2  # inside clip, edge zone
    press(tl, edge_x, row_y(tl, 0))
    move(tl, edge_x - 48, row_y(tl, 0))  # boundary lands exactly on t=8
    release(tl, edge_x - 48, row_y(tl, 0))
    trimmed = scene.session.project.clip("v1")
    assert trimmed.source_out == F(8)
    assert trimmed.output_end == F(8)
    assert trimmed.output_start == F(0)
    scene._undo()
    assert scene.session.project.clip("v1").source_out == F(10)


def test_trim_left_drag_preserves_output_end(scene: MainWindow):
    tl = scene.timeline
    clip = scene.session.project.clip("v2")  # out [20,30), src [20,30)
    edge_x = int(tl.pixel_at_time(clip.output_start))
    press(tl, edge_x, row_y(tl, 0))
    move(tl, edge_x + 25, row_y(tl, 0))  # 1 s right
    release(tl, edge_x + 25, row_y(tl, 0))
    trimmed = scene.session.project.clip("v2")
    assert trimmed.source_in == F(21)
    assert trimmed.output_start == F(21)
    assert trimmed.output_end == F(30)


# ------------------------------------------------------------------ inspector


def test_inspector_populates_and_edits_exact_values(scene: MainWindow):
    tl = scene.timeline
    tl.set_clip_selection(("v2",))  # triggers inspector via signal
    insp = scene.inspector
    assert insp.source_in.text() == "0:00:20.000"
    assert insp.output_start.text() == "0:00:20.000"
    insp.output_start.setText("0:01:02.500")
    insp._on_apply()
    moved = scene.session.project.clip("v2")
    assert moved.output_start == Fraction("62.5")
    scene._undo()
    assert scene.session.project.clip("v2").output_start == F(20)


def test_inspector_rate_and_gain_exact(scene: MainWindow):
    tl = scene.timeline
    tl.set_clip_selection(("v1",))
    insp = scene.inspector
    insp.rate.setText("2")
    insp.gain_db.setText("-6.0")
    insp._on_apply()
    clip = scene.session.project.clip("v1")
    assert clip.rate == F(2)
    assert clip.gain == Fraction("0.501187")  # 10^(-6/20) to 6 dp
    assert clip.output_duration == F(5)  # 10 source seconds at 2x


def test_inspector_rejects_invalid_input_without_editing(scene: MainWindow):
    tl = scene.timeline
    tl.set_clip_selection(("v1",))
    insp = scene.inspector
    errors = []
    insp.validationError.connect(errors.append)
    insp.rate.setText("banana")
    insp._on_apply()
    assert errors and "rate" in errors[0]
    assert scene.session.project.clip("v1").rate == F(1)
    assert insp.rate.styleSheet() != ""  # red marker


def test_inspector_link_and_unlink(scene: MainWindow):
    tl = scene.timeline
    tl.set_clip_selection(("v1", "a1"))
    scene.inspector.show_multi_selection(2)
    assert scene.inspector.link_button.isEnabled()
    scene._on_link_selected()
    v1 = scene.session.project.clip("v1")
    assert v1.link_group is not None
    tl.set_clip_selection(("v1",))
    assert scene.inspector.unlink_button.isEnabled()
    scene._on_unlink("v1")
    assert scene.session.project.clip("v1").link_group is None


# -------------------------------------------------- duplicate + insert ripple


def test_duplicate_block_with_explicit_destination(scene: MainWindow):
    tl = scene.timeline
    tl.set_clip_selection(("v1", "v2"))
    with patch.object(QInputDialog, "getText", return_value=("0:01:00.000", True)):
        scene._duplicate_selected()
    clones = [c for c in scene.session.project.clips if c.id not in {"v1", "v2", "a1"}]
    assert len(clones) == 2
    # the destination names where the copied block *starts*: [0,30) -> [60,90)
    assert sorted(c.output_interval for c in clones) == [(F(60), F(70)), (F(80), F(90))]
    scene._undo()
    assert len(scene.session.project.clips) == 3


def test_duplicate_rejected_when_landing_on_existing_clip(scene: MainWindow):
    tl = scene.timeline
    tl.set_clip_selection(("v1",))
    msgs = []
    scene.statusBar().messageChanged.connect(msgs.append)
    with patch.object(QInputDialog, "getText", return_value=("0:00:05.000", True)):
        scene._duplicate_selected()
    assert len(scene.session.project.clips) == 3  # unchanged
    assert any("overlap" in m.lower() for m in msgs)


def test_insert_ripples_only_target_track(scene: MainWindow):
    tl = scene.timeline
    tl.set_playhead(F(15))  # inside the gap between v1 [0,10) and v2 [20,30)
    before_v2 = scene.session.project.clip("v2").output_start
    scene._apply(
        InsertClipCommand("va", 0, "vt", F(15), source_in=F(50), source_out=F(56), clip_id="ins")
    )
    proj = scene.session.project
    assert proj.clip("v1").output_start == F(0)  # before insert: untouched
    assert proj.clip("v2").output_start == before_v2 + F(6)  # explicit ripple
    assert proj.clip("ins").output_interval == (F(15), F(21))
    assert proj.clip("a1").output_start == F(0)  # other tracks untouched
    scene._undo()
    assert scene.session.project.clip("v2").output_start == before_v2


def test_insert_inside_existing_clip_is_rejected(scene: MainWindow):
    msgs = []
    scene.statusBar().messageChanged.connect(msgs.append)
    scene._apply(
        InsertClipCommand("va", 0, "vt", F(5), source_in=F(50), source_out=F(56), clip_id="ins")
    )
    assert scene.session.project.clip("ins") is None
    assert scene.session.project.clip("v2").output_start == F(20)  # no ripple happened
    assert any("inside clip v1" in m for m in msgs)


def test_insert_shifts_linked_mates_on_other_track(scene: MainWindow):
    scene._apply(LinkClipsCommand(("v2", "a1")))
    scene._apply(
        InsertClipCommand("va", 0, "vt", F(15), source_in=F(50), source_out=F(56), clip_id="ins")
    )
    proj = scene.session.project
    assert proj.clip("v2").output_start == F(26)
    assert proj.clip("a1").output_start == F(6)  # linked mate moves as a block
    assert proj.clip("ins").output_interval == (F(15), F(21))


# ------------------------------------------------------------- selection state


def test_selection_survives_undo_and_track_removal(scene: MainWindow):
    tl = scene.timeline
    tl.set_clip_selection(("v1",))
    scene._apply(AddTrackCommand(TrackKind.AUDIO, "A2"))
    scene._undo()
    assert tl.selected_clips == ("v1",)
    from video_editor.core.commands import RemoveTrackCommand

    scene._apply(RemoveTrackCommand("vt"))
    assert tl.selected_clips == ()  # v1 gone: selection sanitized
    scene._undo()
    assert scene.session.project.clip("v1") is not None


# ------------------------------------------- real Qt event pipeline (QTest)


def test_qtest_drag_and_trim_go_through_real_events(scene: MainWindow):
    """Gestures must work through posted Qt events, not only direct calls."""
    tl = scene.timeline
    tl.show()
    app_qt = QApplication.instance()
    app_qt.processEvents()
    clip = scene.session.project.clip("v1")
    x = int(tl.pixel_at_time(clip.output_start + F(5)))
    y = row_y(tl, 0)
    QTest.mousePress(
        tl.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x, y)
    )
    QTest.mouseMove(tl.viewport(), QPoint(x + 30, y))
    QTest.mouseMove(tl.viewport(), QPoint(x + 50, y))
    QTest.mouseRelease(
        tl.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x + 50, y)
    )
    assert scene.session.project.clip("v1").output_start == F(2)

    # Escape through a real key event cancels the next gesture
    QTest.mousePress(
        tl.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(x + 50, y)
    )
    QTest.mouseMove(tl.viewport(), QPoint(x + 100, y))
    assert tl._gesture is not None and tl._gesture.started
    QTest.keyClick(tl, Qt.Key.Key_Escape)
    assert tl._gesture is None
    QTest.mouseRelease(
        tl.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        QPoint(x + 100, y),
    )
    assert scene.session.project.clip("v1").output_start == F(2)  # unchanged


def test_hover_cursor_previews_gesture_kind(scene: MainWindow):
    tl = scene.timeline
    tl.show()
    QApplication.instance().processEvents()
    clip = scene.session.project.clip("v1")
    body_x = int(tl.pixel_at_time(clip.output_start + F(5)))
    edge_x = int(tl.pixel_at_time(clip.output_end)) - 2
    QTest.mouseMove(tl.viewport(), QPoint(body_x, row_y(tl, 0)))
    assert tl.viewport().cursor().shape() == Qt.CursorShape.SizeAllCursor
    QTest.mouseMove(tl.viewport(), QPoint(edge_x, row_y(tl, 0)))
    assert tl.viewport().cursor().shape() == Qt.CursorShape.SizeHorCursor
    QTest.mouseMove(tl.viewport(), QPoint(body_x, RULER_HEIGHT - 5))
    assert tl.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor


def test_snap_toggle_from_menu(scene: MainWindow):
    tl = scene.timeline
    assert scene.snap_action.isChecked() is True  # snapping is on by default
    tl.snap_enabled = True
    scene.snap_action.trigger()  # off
    assert tl.snap_enabled is False
    # without snapping the same drag lands at the raw pixel time
    clip = scene.session.project.clip("v2")
    x = int(tl.pixel_at_time(clip.output_start + F(1)))
    target_x = int(tl.pixel_at_time(F(11))) + 3
    press(tl, x, row_y(tl, 0))
    move(tl, target_x, row_y(tl, 0))
    release(tl, target_x, row_y(tl, 0))
    start = scene.session.project.clip("v2").output_start
    assert start != F(10)  # no snap
    assert start == F(253, 25)  # whole pixel at 25 px/s -> exact 1/25 s steps
    scene.snap_action.trigger()
    assert tl.snap_enabled is True


# ------------------------------------------------------- track inspector edits


def test_track_rename_and_reorder_via_inspector(scene: MainWindow):
    scene._apply(AddTrackCommand(TrackKind.AUDIO, "A2"))
    tl = scene.timeline
    tl.set_project(scene.session.project)
    tl.selected_track = "at"
    track = scene.session.project.track("at")
    scene.inspector.show_track(scene.session.project, track)
    scene.inspector.track_name.setText("Commentary")
    scene._on_track_rename("at", "Commentary")
    assert scene.session.project.track("at").name == "Commentary"
    scene._undo()
    assert scene.session.project.track("at").name == "A1"
    # reorder: audio track A1 above the video track
    before = [t.id for t in scene.session.project.tracks]
    scene._on_track_move("at", -1)
    assert [t.name for t in scene.session.project.tracks] == ["A1", "V1", "A2"]
    scene._undo()
    assert [t.id for t in scene.session.project.tracks] == before
    # moving past the top edge is refused rather than silently clamped
    scene._on_track_move("vt", -1)
    assert [t.id for t in scene.session.project.tracks] == before


# ----------------------------------------------------- long timeline behaviour


def test_hundreds_of_clips_stay_responsive(scene: MainWindow, qapp_instance: QApplication):
    """Viewport culling: repaint cost must not grow with total clip count."""
    import time

    session = scene.session
    clips = list(session.project.clips)
    tracks = list(session.project.tracks)
    for n in range(8):  # 8 audio tracks x 60 clips = 480 clips over 2 hours
        tid = f"ax{n}"
        tracks.append(Track(tid, TrackKind.AUDIO, f"A{n}"))
        for k in range(60):
            start = F(k * 120, 1)
            clips.append(
                Clip(
                    id=f"c{n}_{k}",
                    asset_id="aa",
                    stream_index=1,
                    track_id=tid,
                    source_in=F(0),
                    source_out=F(60),
                    output_start=start,
                )
            )
    # installed as a document, because the media these clips name has to be part
    # of the same consistent state a saved file would hold
    scene.install_document(
        scene.session.document.with_project(
            scene.session.project.with_tracks(tuple(tracks)).with_clips(tuple(clips))
        )
    )
    tl = scene.timeline
    tl.set_view(F(0), F(4))  # very zoomed out: many clips visible
    assert len(tl.project.clips) == 483

    tl.grab()  # warm up caches
    started = time.perf_counter()
    for step in range(10):
        tl.set_view(F(step * 600), F(4))
        tl.grab()
    elapsed = time.perf_counter() - started
    assert elapsed < 3.0, f"10 repaints of a long project took {elapsed:.2f}s"

    # one drag at the far end of a 2-hour timeline is still exact
    tl.snap_enabled = False
    tl.set_view(F(7000), F(2))
    row = next(i for i, t in enumerate(scene.session.project.tracks) if t.id == "ax3")
    far = max(
        (c for c in scene.session.project.clips if c.track_id == "ax3"),
        key=lambda c: c.output_start,
    )
    x = int(tl.pixel_at_time(far.output_start + F(5)))
    y = row_y(tl, row)
    press(tl, x, y)
    move(tl, x + 10, y)
    release(tl, x + 10, y)
    moved = scene.session.project.clip(far.id)
    assert moved.output_start == far.output_start + F(5)  # 10 px at 2 px/s
    assert moved.output_start.denominator == 1


def test_undo_and_redo_offer_the_documented_chords(scene: MainWindow):
    """README promises Ctrl+Z / Ctrl+Shift+Z; the other standard chord is kept too.

    Two traps, both of which broke history silently rather than loudly:

    * binding Ctrl+Z *and* the platform Undo standard key (identical on this
      platform) makes the shortcut ambiguous and Qt triggers neither;
    * `StandardKey.Redo` is **not** always Ctrl+Y — under a Wayland/Gtk theme it
      resolves to Ctrl+Shift+Z, so adding it beside the documented chord made one
      action claim the same chord twice: "Ambiguous shortcut overload", and redo
      stopped working under the desktop check while looking fine offscreen.

    So the chords are compared by their text, which is what Qt compares when it
    decides a shortcut is ambiguous, and Ctrl+Y is listed explicitly to keep the
    other standard chord available wherever the standard key points.
    """
    from PySide6.QtGui import QKeySequence

    undo = [sequence.toString() for sequence in scene.undo_action.shortcuts()]
    redo = [sequence.toString() for sequence in scene.redo_action.shortcuts()]
    assert QKeySequence(QKeySequence.StandardKey.Undo).toString() in undo
    assert "Ctrl+Shift+Z" in redo and "Ctrl+Y" in redo, f"redo chords: {redo}"
    assert len(undo) == len(set(undo)), f"undo claims a chord twice: {undo}"
    assert len(redo) == len(set(redo)), f"redo claims a chord twice: {redo}"
    assert not set(undo) & set(redo), f"undo and redo share a chord: {undo} {redo}"


def test_keyboard_undo_and_redo_act_on_the_project(
    scene: MainWindow, app_qt: QApplication
):
    """Keyboard history is reachable without the mouse, from the timeline focus."""
    before = scene.session.project
    assert scene._apply(MoveClipCommand("v1", output_start=F(3)))
    assert scene.session.project.clip("v1").output_start == F(3)

    scene.timeline.setFocus()
    scene.activateWindow()  # window-context shortcuts only fire for the active window
    app_qt.processEvents()
    assert app_qt.activeWindow() is scene
    assert scene.focusWidget() is scene.timeline
    QTest.keyClick(scene, Qt.Key_Z, Qt.ControlModifier)
    app_qt.processEvents()  # synthesized key events are queued, not synchronous
    assert scene.session.project == before

    QTest.keyClick(scene, Qt.Key_Z, Qt.ControlModifier | Qt.ShiftModifier)
    app_qt.processEvents()
    assert scene.session.project.clip("v1").output_start == F(3)


def test_shortcuts_stay_disabled_while_history_is_empty(scene: MainWindow):
    assert not scene.undo_action.isEnabled()
    scene._apply(AddTrackCommand(TrackKind.AUDIO, "A2"))
    assert scene.undo_action.isEnabled()
    scene._undo()
    assert not scene.undo_action.isEnabled()
    assert scene.redo_action.isEnabled()
