"""GUI smoke tests, run with the offscreen Qt platform (see conftest)."""

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication

from video_editor.app import MainWindow
from video_editor.diagnostics import MediaToolStatus, ToolStatus


def test_main_window_launches(qapp_instance: QApplication):
    window = MainWindow()
    window.show()
    assert "Video Editor" in window.windowTitle()
    menu_item_texts: list[str] = []
    for menu_action in window.menuBar().actions():
        menu = menu_action.menu()
        if menu is not None:
            menu_item_texts.extend(a.text() for a in menu.actions())
    assert any("Quit" in text for text in menu_item_texts)
    assert window.statusBar().currentMessage()
    window.close()


def test_missing_ffmpeg_reports_diagnostic_without_crash(qapp_instance: QApplication):
    missing = MediaToolStatus(
        ffmpeg=ToolStatus("ffmpeg", None, None, "ffmpeg not found on PATH"),
        ffprobe=ToolStatus("ffprobe", None, None, "ffprobe not found on PATH"),
    )
    window = MainWindow(media_status=missing)
    window.show()
    message = window.statusBar().currentMessage()
    assert "not found" in message
    assert "install ffmpeg" in message
    window.close()


def test_doctor_reports_environment(capsys):
    from video_editor.cli import _doctor

    code = _doctor()
    out = capsys.readouterr().out
    assert "python:" in out
    assert "ffprobe:" in out
    assert code in (0, 1)
