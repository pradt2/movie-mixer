# TASK_35 — Reproducible Linux CI and verification commands

Prerequisite: TASK_34. Scope: CI/test setup and documentation, not fix unrelated product issues.

## Work
Add reproducible Linux Github Actions or equivalent repo CI: supported Python >=3.12, PySide6 offscreen, FFmpeg/FFprobe with required codecs; print versions, pytest and ruff, capture exit codes and skip reasons. Pure media/core tests should run without requiring a GUI where feasible; don't silently skip Qt suites. Include short synthetic A/B check and repeated shutdown subprocess check from TASK_22c, separate expensive long-project profiling and actual desktop/perceptual checks. Pin/describe tested tool compatibility rather than assuming all FFmpeg versions equivalent. Preserve generated fixtures only. Build docs instructions for local reproduction with explicit PYTHONPATH in worktree (editable venv trap). Logs written to file for long runs.

## Acceptance
Run commands locally on available environment, validate CI configuration, report any unavailable runner honestly. No broad xfail/skip or test count without successful process exit. Record version, duration and tests/skips. No fake CI status.
