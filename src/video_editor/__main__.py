"""Allow ``python -m video_editor`` to start the same application as ``video-editor``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
