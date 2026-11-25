"""Command line entry point.

``video-editor`` and ``python -m video_editor`` start the same GUI
application. ``video-editor doctor`` reports the environment (Python,
PySide6, ffmpeg/ffprobe) without opening a window.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__


def _doctor() -> int:
    from .diagnostics import check_media_tools

    status = check_media_tools()
    print(f"video-editor {__version__}")
    print(f"python: {sys.version.split()[0]} ({sys.executable})")
    try:
        import PySide6

        print(f"PySide6: {PySide6.__version__}")
    except ImportError as exc:  # pragma: no cover - only when deps missing
        print(f"PySide6: NOT INSTALLED ({exc})")
    for tool in (status.ffmpeg, status.ffprobe):
        if tool.path is None:
            print(f"{tool.name}: not found on PATH")
        elif tool.error:
            print(f"{tool.name}: {tool.path} — {tool.error}")
        else:
            print(f"{tool.name}: {tool.path} (version {tool.version})")
    return 0 if status.available else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="video-editor",
        description="Non-destructive editor for mixing audio and video across film editions.",
    )
    parser.add_argument("--version", action="version", version=f"video-editor {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("doctor", help="report environment and media backend availability")
    sub.add_parser("gui", help="start the graphical editor (default)")
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor()
    from .app import run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
