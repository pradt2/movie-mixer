"""Drive the real editor window for unattended desktop acceptance runs.

Coordinates always come from the widgets' own geometry (``pixel_at_time``,
row layout constants, ``mapTo``), never from hand-picked screen positions: a
point that lands in the ruler moves the playhead instead of selecting a clip,
which would silently invalidate a check. Events go to the concrete child widget
with widget-local coordinates, and no human interaction is required: modal
dialogs are located and answered programmatically.
"""

import contextlib
import os
import tempfile
from fractions import Fraction as F
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

from video_editor.ui.main_window import MainWindow
from video_editor.ui.timeline import ROW_HEIGHT, RULER_HEIGHT

#: Scratch area for these checks; override with VIDEO_EDITOR_CHECK_DIR.
WORKDIR = Path(os.environ.get(
    "VIDEO_EDITOR_CHECK_DIR", Path(tempfile.gettempdir()) / "video_editor_desktop_check"
))
FIXTURES = WORKDIR / "media"
ORANGE_RGB = (230, 150, 60)
BLUE_RGB = (60, 120, 200)
BLACK_RGB = (16, 16, 18)

_app = None
_win = None


def app() -> QApplication:
    global _app
    if _app is None:
        _app = QApplication.instance() or QApplication([])
    return _app


def window(offer_recovery: bool = False) -> MainWindow:
    """A window in this run's environment.

    Recovery is offered only when the check asks for it, exactly as the real
    application offers it from start-up and not from every window: several
    windows are created during the project checks and none of them should
    interrupt the run with a prompt the check has not arranged for.
    """
    global _win
    if _win is None:
        app()  # Qt requires the QApplication before any widget exists
        # a real desktop session prompts before losing work; this check answers
        # those dialogs itself, so nothing needs a human
        MainWindow.quit_confirm_required = True
        _win = MainWindow()
        _win.resize(1280, 800)
        _win.show()
    else:
        _win.show()
    pump(300)
    if offer_recovery:
        _win.offer_startup_recovery()
    return _win


def adopt_window(win: MainWindow) -> None:
    """Remember a window the check made itself, so shutdown still owns it."""
    global _win
    _win = win


def close_window() -> None:
    """Close the current window properly (workers, caches) and forget it.

    Project checks need a *second* window to reopen a saved project in, and a
    window must not be dropped without stopping its render/decode threads.
    """
    global _win
    if _win is None:
        return
    # Teardown is the check's own decision, never a prompt: a window with unsaved
    # scratch work would otherwise answer its own "Unsaved work?" question (or
    # block waiting for one) and stay open, corrupting every later leg. Legs that
    # *are* about that prompt call `_confirm_quit()` themselves.
    previous = MainWindow.quit_confirm_required
    MainWindow.quit_confirm_required = False
    try:
        _win._shutdown_preview()
        _win.close()
    finally:
        MainWindow.quit_confirm_required = previous
    pump(200)
    _win = None


_deadline = None


def set_deadline(seconds: float) -> None:
    """Give up rather than wait for a click, and say what was blocking.

    A modal the poller cannot see (a native dialog, or a window the check never
    expected) would otherwise sit there forever holding a window open on
    someone's desktop. Timers still fire inside a modal's nested event loop, so
    this always gets a turn: it prints what was blocking and exits.
    """
    global _deadline
    if _deadline is not None:
        _deadline.stop()
    timer = QTimer()
    timer.setSingleShot(True)

    def expire() -> None:
        modal = None
        with contextlib.suppress(RuntimeError):  # application already gone
            modal = app().activeModalWidget()
        refused = getattr(_answerer, "unexpected", []) if _answerer else []
        print(f"\nTIMEOUT after {seconds:.0f} s: this run will not wait for a click.",
              flush=True)
        if modal is not None:
            text = " ".join(part for part in (modal.windowTitle(), modal.text()) if part)
            print(f"  blocked on: {text[:200]}", flush=True)
        if refused:
            print(f"  dialogs it refused: {refused}", flush=True)
        os._exit(3)

    timer.timeout.connect(expire)
    timer.start(int(seconds * 1000))
    _deadline = timer


def pump(ms: int = 120) -> None:
    """Process events for about *ms* of real time (never a blocking sleep)."""
    timer = QTimer()
    timer.setSingleShot(True)
    timer.start(ms)
    guard = QTimer()
    guard.setSingleShot(True)
    guard.start(ms + 2000)
    while timer.isActive() and guard.isActive():
        app().processEvents()


def repaint() -> None:
    win = window()
    for widget in (win.timeline.viewport(), win.output_viewer, win.source_viewer,
                   win.viewer_stack):
        widget.repaint()
    win.repaint()
    pump(250)


# ------------------------------------------------------------------- bin/media


def import_files(paths: list[Path]) -> None:
    """Exactly the Ctrl+I path, minus the file dialog itself."""
    win = window()
    win.bin_controller.request_import(list(paths))
    for _ in range(300):
        if win.bin_controller.pending_imports == 0:
            break
        pump(100)
    pump(400)


def select_streams(asset_name: str, stream_indices: tuple[int, ...]) -> None:
    """Select stream rows in the media bin, as a user's click would.

    Assets are chosen by displayed name: parallel import jobs finish in
    arbitrary order, so a top-level index would not identify a given file.
    """
    bin_widget = window().media_bin
    bin_widget.clearSelection()
    top = None
    for row in range(bin_widget.topLevelItemCount()):
        item = bin_widget.topLevelItem(row)
        if asset_name in item.text(0):
            top = item
            break
    if top is None:  # pragma: no cover - driver misuse
        raise LookupError(f"no bin asset matching {asset_name!r}")
    for row in range(top.childCount()):
        child = top.child(row)
        if int(child.data(0, Qt.UserRole + 2)) in stream_indices:
            child.setSelected(True)
    pump(200)


def set_playhead(t: F) -> None:
    window().timeline.set_playhead(t, from_user=True)
    pump(200)


def place_at_playhead() -> None:
    # A modal that has just closed leaves the window inactive under the offscreen
    # platform, and Qt only fires window shortcuts for the active window.
    ensure_active()
    QTest.keyClick(window(), Qt.Key_G, Qt.ControlModifier)
    pump(300)


def output_mode() -> None:
    window().viewer_mode.setCurrentIndex(1)
    pump(300)


def status_text() -> str:
    labels = window().statusBar().findChildren(QLabel)
    parts = [window().statusBar().currentMessage()] + [x.text() for x in labels if x.text()]
    return " | ".join(p for p in parts if p)


# ---------------------------------------------------------------- timeline ops


def row_of(track_id: str) -> int:
    tracks = window().session.project.tracks
    return next(i for i, t in enumerate(tracks) if t.id == track_id)


def project():
    return window().session.project


def clip_span(clip_id: str) -> tuple[F, F] | None:
    clip = project().clip(clip_id)
    return None if clip is None else (clip.output_start, clip.output_end)


def _tl_point(when: F, track_row: int, dx: int = 0) -> QPoint:
    tl = window().timeline
    return QPoint(int(tl.pixel_at_time(when)) + dx,
                  RULER_HEIGHT + track_row * ROW_HEIGHT + ROW_HEIGHT // 2)


def click_timeline(when: F, track_row: int, dx: int = 0) -> None:
    QTest.mouseClick(window().timeline.viewport(), Qt.LeftButton, Qt.NoModifier,
                     _tl_point(when, track_row, dx))
    pump(200)


def drag_timeline(when_from: F, row_from: int, when_to: F, row_to: int,
                  dx_from: int = 0, dx_to: int = 0) -> None:
    """Press on a clip, move, release: the same gesture a user makes."""
    tl = window().timeline
    start = _tl_point(when_from, row_from, dx_from)
    end = _tl_point(when_to, row_to, dx_to)
    QTest.mousePress(tl.viewport(), Qt.LeftButton, Qt.NoModifier, start)
    pump(120)
    mid = QPoint((start.x() + end.x()) // 2, (start.y() + end.y()) // 2)
    QTest.mouseMove(tl.viewport(), mid)
    pump(120)
    QTest.mouseMove(tl.viewport(), end)
    pump(120)
    QTest.mouseRelease(tl.viewport(), Qt.LeftButton, Qt.NoModifier, end)
    pump(300)


def ensure_active() -> bool:
    """Re-activate the window after a modal closed.

    Under the offscreen platform ``QApplication.activeWindow()`` becomes None once
    a modal dialog closes, and Qt only fires window-context shortcuts for the
    active window — so keyboard checks would otherwise measure a focus artifact of
    the harness rather than the application.
    """
    win = window()
    win.activateWindow()
    pump(200)
    return app().activeWindow() is win


def key_press(key_: Qt.Key, mods=Qt.NoModifier, target=None) -> None:
    QTest.keyClick(target or window(), key_, mods)
    pump(250)


def shortcut_keys(action):
    """An action's shortcut as QTest arguments, so keyboard checks stay honest."""
    from PySide6.QtGui import QKeySequence

    combination = QKeySequence(action.shortcut())[0]
    return Qt.Key(combination.key()), combination.keyboardModifiers()


# -------------------------------------------------------------- modal dialogs


#: the answerer currently polling for modal dialogs (one at a time)
_answerer: "ModalAnswerer | None" = None


class ModalAnswerer:
    """Finds modal dialogs and answers them; records what was seen."""

    def __init__(self, destination: Path | None = None, overwrite: str = "Yes") -> None:
        self.destination = destination
        self.overwrite_answer = overwrite
        self.exports: list = []
        self.summary_text = ""
        self.questions: list[str] = []
        self.rejections: list[str] = []
        self.unexpected: list[str] = []
        #: project dialogs are expected during the project checks; the index says
        #: which button to press (0 = leftmost custom button)
        self.project_answers: dict[str, str] = {}
        self.project_dialogs: dict[str, list[str]] = {
            "Unsaved work": [],
            "Missing or changed media": [],
            "Relink refused": [],
            "Project could not be opened": [],
            "Restore refused": [],
            "Unsaved work was found": [],
            "Project saved with unavailable media": [],
            "Save failed": [],
            "Replace existing project file?": [],
            "Media status": [],
            "Recovery snapshots": [],
        }
        # Handled dialogs are remembered as *objects*. Remembering only their
        # ``id()`` is unsafe: CPython reuses the address as soon as the closed
        # dialog is collected, so the next identical prompt (the second "replace
        # this file?" question, say) looked already handled and was never
        # answered — which blocked the run waiting for a click.
        self._handled: list = []
        self._timer = QTimer()
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        global _answerer
        if _answerer is not None and _answerer is not self:
            # two pollers would race to answer the same dialog, and the one that
            # wins knows nothing about the other's destination or answers
            raise RuntimeError("another ModalAnswerer is already running")
        _answerer = self
        self._timer.start()

    def stop(self) -> None:
        global _answerer
        if _answerer is self:
            _answerer = None
        self._timer.stop()

    def _handled_already(self, modal) -> bool:
        return any(modal is done for done in self._handled)

    def _mark_handled(self, modal) -> None:
        """Keep a reference while remembering it, so its id cannot be recycled."""
        self._handled.append(modal)
        if len(self._handled) > 64:
            del self._handled[:-64]

    def _tick(self) -> None:
        modal = app().activeModalWidget()
        if modal is None or self._handled_already(modal):
            return
        from PySide6.QtWidgets import QDialogButtonBox

        from video_editor.ui.export_dialog import ExportDialog

        if isinstance(modal, ExportDialog):
            self._mark_handled(modal)
            self.exports.append(modal)
            self.summary_text = modal.summary.toPlainText()
            if self.destination is not None:
                modal.destination.setText(str(self.destination))
            accept = modal.buttons.button(QDialogButtonBox.StandardButton.Ok)
            if accept.isEnabled():
                accept.click()  # the user's "Start export" click
            else:
                self.rejections.append(modal._error_label.text())
                modal.reject()
            return
        if isinstance(modal, QMessageBox) and modal.windowTitle() in self.project_dialogs:
            # project dialogs (unsaved work, missing media, relink refusal, project
            # overwrite) use custom buttons, and Qt orders buttons by role and
            # style, so the answer names the button rather than its position
            self._mark_handled(modal)
            title = modal.windowTitle()
            self.project_dialogs[title].append(
                " ".join(part for part in (modal.text(), modal.informativeText()) if part)
            )
            wanted = self.project_answers.get(title, "OK")
            buttons = modal.buttons()
            labels = [button.text().replace("&", "") for button in buttons]
            if wanted in labels:
                buttons[labels.index(wanted)].click()
                return
            self.unexpected.append(f"{title}: no button {wanted!r} among {labels}")
            modal.reject()
            return
        if isinstance(modal, QMessageBox) and "Replace existing" in modal.windowTitle():
            self._mark_handled(modal)
            self.questions.append(modal.text())
            # Match by standard-button role: the visible text is "&Yes" and the
            # accelerator differs by translation, so text matching is unreliable.
            wanted = QMessageBox.StandardButton.Yes if self.overwrite_answer == "Yes" \
                else QMessageBox.StandardButton.No
            button = modal.button(wanted)
            if button is not None:
                button.click()
                return
            modal.reject()
            return
        self._mark_handled(modal)
        self.unexpected.append(f"{type(modal).__name__}: {modal.windowTitle()}")
        modal.reject()


class SignalSpy:
    """Records a render controller's signals; nothing is asserted by the spy."""

    def __init__(self, controller) -> None:
        self.progress: list[float] = []
        self.finished: list[object] = []
        self.failed_messages: list[str] = []
        self.cancelled = 0
        controller.progress.connect(self.progress.append)
        controller.finished.connect(self.finished.append)
        controller.failed.connect(self.failed_messages.append)
        controller.cancelled.connect(self._cancel)

    def _cancel(self) -> None:
        self.cancelled += 1

    def started(self) -> bool:
        return bool(self.progress or self.finished or self.failed_messages)


def press_button(widget, text: str) -> bool:
    for button in widget.findChildren(QPushButton):
        label = button.text().replace("&", "")
        if label == text or button.text() == text:
            button.click()
            return True
    return False
