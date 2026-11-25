# TASK_15 — Stabilisation gate and a maintainable extension seam

Prerequisite: TASK_14. Stop before assisted alignment. The purpose is to leave a reliable foundation for the next feature phase, not to build it.

## Review findings (baseline 0c6b8f1)
- `ui/main_window.py` is ~2,275 lines and owns project lifecycle, media records, commands, viewer/cache state, render orchestration and UI widgets. It is becoming the natural place for every feature. Future matching logic must not be embedded there or invent a second edit path.
- No .github/workflows test workflow is present in the reviewed tree. Existing automated tests and desktop scripts are substantial; the baseline commit reports 537 tests, but docs/progress.md honestly records unresolved teardown, synthetic-only A/B workflow and lack of real-film/long-export perceptual checks. Preserve that distinction. Static review did not execute these checks.

## Implement
1. After the transaction and worker corrections, identify remaining responsibilities in MainWindow. Extract only cohesive, testable orchestration already required by the editor (e.g. document/session controller, playback/preview coordinator, command dispatcher). Keep widgets and signal wiring in UI; maintain dependency direction. Prefer passing existing immutable snapshots and small callbacks over a service locator, plugin system, or broad architectural rewrite. Move code in small steps with tests; preserve user-visible interactions and shortcuts.
2. Document a minimal extension seam for the next phase: read-only access to a document snapshot, stable source/stream IDs, exact source-to-output mapping, bounded cancellable background operations, and application of a *proposed* edit via the same atomic command boundary with stale-snapshot detection. Demonstrate the seam with a trivial existing manual edit (e.g. move/split), not a placeholder alignment algorithm or new persistent correspondence format. Never infer a preferred edition.
3. Add reproducible Linux CI/check instructions for Python >=3.12, FFmpeg/FFprobe, Qt offscreen and required codecs. Run pytest and ruff; ensure dependency-sensitive tests explicitly report skips. Include generated fixture integration tests, repeated shutdown subprocess check, and a short reproducible A/B test. Separate real-desktop checks from offscreen CI; record environment/version and failures. Do not hide failing tests by broad xfail/skip. Keep expensive long-film profiling optional.
4. Re-run full manual acceptance recipe in TASK_08 after all corrective milestones. Include edge cases actually addressed in TASK_09–14: source safety, dirty/undo/relink, linked split, fractional rates, differing sample rates, cut boundaries, VFR, long timelines, cancellation and reload. Add end-to-end recovery test: save baseline, autosave unsaved changes, terminate child process, restart, offer recovery and verify original saved file unchanged.
5. Prepare an honest readiness report in docs/progress.md: defects closed with regression references, outstanding technical limitations, measured performance, automated vs observed desktop results. Run the real-film smoke check if media/desktop are available; otherwise leave an explicit manual checklist for the human, not a claim of readiness by perception.

## Acceptance / stop condition
- All new and existing relevant checks pass, or blockers are precisely named and not papered over. Report commands, counts, skips, environment and actual results.
- A manual edit can be invoked programmatically through the same validated transaction API as the UI, undone in one step and previewed; a stale proposed edit is refused, not silently applied to a different snapshot.
- No new alignment feature, mandatory reference source, new editing mode or speculative plugin architecture. Existing project files reopen and original media remains untouched.
- Human reviews the manual editor on representative actual editions before TASK_16 or any matching work begins.
