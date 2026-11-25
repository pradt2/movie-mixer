#!/usr/bin/env python3
"""Unattended acceptance run for the two-edition (A/B) workflow.

Builds two synthetic editions in a scratch directory, drives the real editor
window through the workflow a person would use — import both, place edition A's
picture, place edition B's sound somewhere else, align it by numbers, undo it —
then renders a composition that mixes the two and measures the rendered file
shot by shot: the colour of A's picture and the frequency of B's tone at the
times the editor chose.

    python scripts/ab_workflow_check.py            # offscreen, no display needed
    QT_QPA_PLATFORM=xcb python scripts/ab_workflow_check.py   # on a real desktop

Nothing here needs or touches a real film, and nothing is written outside the
scratch directory (override with VIDEO_EDITOR_CHECK_DIR).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

scratch = Path(os.environ.get(
    "VIDEO_EDITOR_CHECK_DIR", Path(tempfile.gettempdir()) / "video_editor_desktop_check"
)) / "ab_run"
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
import editions  # noqa: E402


def main() -> int:
    from video_editor.diagnostics import check_media_tools

    status = check_media_tools()
    print(f"scratch: {G.WORKDIR}")
    print(f"media tools: {status.diagnostic()}")
    if not status.available:
        print("this check needs working ffmpeg and ffprobe")
        return 2
    G.app()  # the watchdog is a Qt timer, so it needs the application first
    G.set_deadline(900)  # never wait on a click: abort and say what blocked
    return editions.run()


if __name__ == "__main__":
    raise SystemExit(main())
