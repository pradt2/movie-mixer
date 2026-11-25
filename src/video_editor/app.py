"""Compatibility shim: the window now lives in :mod:`video_editor.ui.main_window`."""

from __future__ import annotations

from .diagnostics import MediaToolStatus, check_media_tools
from .ui.main_window import MainWindow, run_app

__all__ = ["MainWindow", "MediaToolStatus", "check_media_tools", "run_app"]
