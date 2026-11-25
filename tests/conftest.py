"""Shared pytest fixtures.

Qt tests run offscreen by default so the suite works headless. Set
``VIDEO_EDITOR_TEST_XCB=1`` to force the xcb platform for manual GUI runs.
"""

from __future__ import annotations

import gc
import os

import pytest

if os.environ.get("VIDEO_EDITOR_TEST_XCB") == "1":
    os.environ["QT_QPA_PLATFORM"] = "xcb"
elif "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(autouse=True)
def _no_modal_by_default(qapp_instance, monkeypatch):
    """Keep unit tests unattended: modals must be provoked deliberately.

    A test that wants the real prompt (unsaved-work quit, relink refusal,
    overwrite confirmation) sets ``quit_confirm_required``/``ask`` for itself.
    Without this, a window closed after an edit would open a modal that nobody
    answers, and the suite would block instead of reporting.
    """
    from video_editor.ui.main_window import MainWindow

    monkeypatch.setattr(MainWindow, "quit_confirm_required", False, raising=False)


@pytest.fixture(scope="session")
def qapp_instance():
    """One QApplication for the session (Qt requires this ordering)."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="session", autouse=True)
def _no_window_outlives_the_session(qapp_instance):
    """No window, and so no background thread, may outlive the test session.

    A test that forgets to close its window leaves behind the preview/still/render
    threads and the decoration worker it owns. When the C++ ``QApplication`` dies
    first, those objects are torn down in the wrong order and the process aborts
    with "pure virtual method called" or a segfault — *after* pytest has already
    printed "N passed", so a suite that is green still looks broken (and is: work
    can be cut off mid-write). Closing each window runs its real shutdown path,
    which is what the application does too.

    Closing is not enough on its own. A closed window is still alive: parentage,
    signal connections and lambdas leave reference cycles, so the wrapper (and its
    C++ object) normally dies only during interpreter shutdown — after PySide has
    begun its own ``destroyQCoreApplication()`` teardown. With more than one window
    in a session that ordering crashes the process (measured:
    ``tests/test_viewer.py`` alone aborted on 8 runs out of 8, with "N passed"
    printed first, while every individual test in it was clean on its own).
    Collecting here destroys the windows while ``QApplication`` is still alive and
    in control, which is also the order a real quit produces.
    """
    yield
    from video_editor.ui.decorations import shutdown_all

    for widget in list(qapp_instance.topLevelWidgets()):
        renderer = getattr(widget, "render_controller", None)
        if renderer is not None and getattr(renderer, "running", False):
            renderer.cancel()
            renderer.wait_finished(5_000)
        shutdown = getattr(widget, "_shutdown_preview", None)
        if callable(shutdown):  # stops worker threads and frees owned cache files
            shutdown()
        if widget.isWindow():
            widget.close()
    shutdown_all()
    # `close()` only hides a window: the object, its threads and its media players
    # stay alive, and then die during Qt's own exit teardown in an order that
    # aborts the process. Scheduling the deletion and letting the event loop
    # perform it kills them here, while `QApplication` is still in charge.
    for widget in list(qapp_instance.topLevelWidgets()):
        widget.deleteLater()
    for _ in range(5):
        qapp_instance.processEvents()
    gc.collect()
