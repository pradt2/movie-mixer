# TASK_00 — Bootstrap a runnable project

Prerequisite: none. Read AGENTS.md. Deliver a minimal Python desktop app and reliable developer workflow, not an editor yet.

## Implement
- Inspect existing repository. Establish `pyproject.toml`, `src/video_editor/` package, tests, README, docs/architecture.md and docs/progress.md. Keep unused components as documented future directories rather than elaborate stubs.
- Configure Python >=3.12, PySide6, pytest, ruff; provide installation instructions with a virtual environment and editable install including dev extras. Provide a command such as `video-editor` and `python -m video_editor` that start the same app.
- Launch a Qt main window with a title, empty central area, File > Quit and useful startup error handling. No fake timeline or media controls.
- Detect FFmpeg/ffprobe availability and report missing executables without making the empty window unusable. Add a small dependency-check command or startup diagnostic; record actual binary versions. Document Linux installation, offscreen Qt testing and media backend limitations.
- Set up test configuration and lint. Add a GUI smoke test (offscreen where supported) and pure import test. Do not download large sample files.

## Acceptance
- Fresh environment can install and launch the app using documented commands.
- `pytest` and `ruff check` run from repository root. Missing FFmpeg produces a clear diagnostic, not a crash.
- No media editing, alignment, rendering or speculative plugin system implemented.

Record verification and remaining environmental limitations in docs/progress.md.
