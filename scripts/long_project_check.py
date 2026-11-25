#!/usr/bin/env python3
"""Unattended profiling run of a two-hour, many-hundred-clip project.

Builds two long synthetic sources, assembles a ~2 h timeline of hundreds of clips
with a window, and measures planning, timeline repaint at several zoom levels,
editing, caching and resource footprint. No real film is used and nothing is
written outside the scratch directory (override with VIDEO_EDITOR_CHECK_DIR).

    python scripts/long_project_check.py           # offscreen, no display needed
    QT_QPA_PLATFORM=xcb python scripts/long_project_check.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

scratch = Path(os.environ.get(
    "VIDEO_EDITOR_CHECK_DIR", Path(tempfile.gettempdir()) / "video_editor_desktop_check"
)) / "long_run"
scratch.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("VIDEO_EDITOR_CHECK_DIR", str(scratch))
# the window deletes files from its own preview/recovery directories: keep them
# inside this scratch area so a check run can never touch anyone's real caches
os.environ.setdefault("VIDEO_EDITOR_PREVIEW_DIR", str(scratch / "previews"))
os.environ.setdefault("VIDEO_EDITOR_RECOVERY_DIR", str(scratch / "recovery"))
os.environ.setdefault("VIDEO_EDITOR_AUTOSAVE_SECONDS", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(HERE / "desktop_checks"))

import driver as G  # noqa: E402
import long_project  # noqa: E402

from video_editor.diagnostics import check_media_tools  # noqa: E402


def main() -> int:
    status = check_media_tools()
    print(f"scratch: {G.WORKDIR}")
    print(f"media tools: {status.diagnostic()}")
    if not status.available:
        print("this check needs working ffmpeg and ffprobe")
        return 2
    G.app()
    G.set_deadline(1800)  # never wait on a click: abort and say what blocked
    try:
        return long_project.run()
    finally:
        from video_editor.ui.decorations import shutdown_all

        shutdown_all()


if __name__ == "__main__":
    raise SystemExit(main())
