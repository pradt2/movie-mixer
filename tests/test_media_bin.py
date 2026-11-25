"""Offscreen GUI tests: media bin displays assets/streams; window survives imports."""

import time

import pytest
from PySide6.QtWidgets import QApplication

from media_fixtures import HAS_FFMPEG, make_garbage, make_mixed
from video_editor.ui.media_bin import MediaBinController

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffprobe on PATH")


def wait_imports(app: QApplication, controller: MediaBinController, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while controller.pending_imports and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert controller.pending_imports == 0
    # completion signals arrive through queued connections: drain them
    drain_deadline = time.monotonic() + 2.0
    while time.monotonic() < drain_deadline:
        app.processEvents()
        time.sleep(0.01)


def test_window_with_media_bin_launches(qapp_instance: QApplication):
    from video_editor.ui.main_window import MainWindow

    window = MainWindow()
    window.show()
    assert window.media_bin is not None
    assert window.import_action.isEnabled()
    window.close()


def test_import_populates_tree_and_handles_failure(qapp_instance: QApplication, tmp_path):
    from video_editor.ui.main_window import MainWindow

    window = MainWindow()
    good = make_mixed(tmp_path)
    bad = make_garbage(tmp_path)
    messages: list[str] = []
    window.bin_controller.status_message.connect(messages.append)
    window.bin_controller.request_import([good, bad])
    wait_imports(qapp_instance, window.bin_controller)

    assert len(window.bin_controller.assets) == 1
    asset = window.bin_controller.assets[0]
    assert asset.metadata.streams and asset.editable

    tree = window.media_bin
    assert tree.topLevelItemCount() == 1
    top = tree.topLevelItem(0)
    assert top.childCount() == 3  # video + two audio streams
    details = [top.child(i).text(1) for i in range(3)]
    assert any("eng" in details[i] for i in range(3)) or any("[eng]" in d for d in details)
    assert any("fre" in d for d in details)
    assert any("failed" in m for m in messages)

    # duplicate import rejected with a message, no new assets
    window.bin_controller.request_import([good])
    qapp_instance.processEvents()
    assert len(window.bin_controller.assets) == 1
    assert any("Already imported" in m for m in messages)
    window.close()
