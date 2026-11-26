# AGENTS.md — Python video editor

## Mission

Build a small, usable, non-destructive desktop video editor. The motivating workflow is to combine audio and video from different editions of a film, whose timings, cuts, ordering and quality may differ. First make this possible entirely by manual editing. Only afterwards introduce content-aware assistance. This is not a full professional NLE, a dubbing-only utility, or a batch synchronisation script.

All media assets and their streams are peers. No asset is inherently the reference, the best picture or the correct soundtrack. The output has its own timeline and may incorporate any portion of any asset. Audio and picture can be chosen and edited independently; multiple audio tracks can contribute simultaneously. Do not infer a preferred source from resolution, filename, language or import order.

## Agent workflow

- Read this file and the assigned `TASK_XX.md` before changing code. Execute tasks in numerical order unless the human directs otherwise. Inspect the existing repository first; do not replace existing working implementations merely to match this specification. If a contract must change, document why and update its consumers and tests.
- Each task is a complete, bounded implementation milestone. Do not implement later tasks speculatively. If a prerequisite is absent, report it and implement only the minimum needed to satisfy the current task safely. Do not mark a task complete on the basis of stubs, fake media or mocked UI alone.
- Run existing tests plus the new tests; add regression tests for every bug fixed. Run the task's manual acceptance checks where the environment supports them. Report what was actually executed, failures, limitations and files changed. Do not claim GUI playback or rendering was verified without doing so.
- Keep `README.md` accurate. Maintain `docs/architecture.md` for decisions and invariants, `docs/progress.md` for task completion, deviations and outstanding checks. Record deliberate scope changes instead of silently changing requirements. Do not commit unless asked.
- Never overwrite imported assets. Never delete user media or overwrite an existing output without explicit consent. Use disposable synthetic fixtures, not the user's films, in tests.

## Active corrective roadmap (after TASK_12)

The previous TASK_12 was intentionally left unaccepted at `52f586e`. Do not rerun its broad instructions end to end. The historical report in TASK_12.md is evidence; TASK_12b onwards are the current instructions. The old TASK_13–15 have been superseded. Older references to those numbers (including in progress/architecture notes) describe the previous roadmap, not the new assignments. See TASK_PLAN.md for the precise order and coverage.

For the corrective roadmap:

- Work on **one assigned task at a time**. Before editing, state the single outcome, relevant files, smallest reproduction and intended test. Do not start the next task, fix unrelated findings, or alter the plan files to make an assignment easier. Log unrelated findings for review.
- Separate diagnosis from implementation. For a bug, first establish a reproducible failing check whose expectation is independently justified (hand-worked exact arithmetic, measured fixture metadata, decoded media, or a simple reference implementation). Test the test: ensure a fixture actually contains what it claims, compare frame numbers versus seconds, and verify tool versions. Never derive expected output from the same implementation being tested.
- Run focused baseline tests, one minimal change, then focused regression and affected suites. Record commands, actual/expected values, exit codes, elapsed time and skips. Run the complete suite at explicit checkpoints (TASK_12m, TASK_22c, TASK_35, TASK_37), or when a shared interface changes broadly; run the three unattended desktop scripts at TASK_12m and TASK_37, and the shorter A/B and teardown checks at TASK_35. This focused-testing rule supersedes the earlier instruction to run every existing test after each tiny task. The current known-red picture baseline is allowed only up to its designated corrective checkpoint; report remaining failures by name, never hide them through skips or loosened expectations.
- Give brief visible progress updates at diagnosis, first measurement and verification, not just tool activity. Keep the task record in docs/progress.md concise: observation, change, evidence, remaining problem, status (DONE / BLOCKED / DIAGNOSTIC). Record architecture decisions when they change. Do not repeat the full task specification in progress notes.
- If an experiment contradicts the intended contract, stop and explain the contradiction before changing the contract. If two materially different attempted fixes fail or work expands beyond the assigned boundary, stop with a minimal reproducible case, smallest failing test/log, hypotheses already ruled out and one proposed next experiment. Ask for a narrower follow-up or reviewer decision; do not keep rewriting the system. A blocked milestone is not accepted. Diagnostic-only milestones may intentionally leave named production defects red.
- For long runs capture stdout/stderr and exit status in a durable local log, use explicit timeouts and check the actual process. Reproduce hangs with fixed test order and faulthandler. When comparing revisions in a worktree, ensure PYTHONPATH/import path points at that worktree, not an editable install from another checkout. Never use modified tests to mask a genuine regression.
- Preserve TASK_09–11 safety/document/audio behaviour. No source is preferred, no automatic alignment or feature work before the final human review.

## Proposed technical defaults (change only with rationale)

- Python >= 3.12, packaged with `pyproject.toml`, `src/` layout, `pytest` and `ruff`. Desktop UI: PySide6 (Qt Widgets). Media probing and export: system `ffprobe` and `ffmpeg`. Source preview: Qt Multimedia where reliable, with explicit unsupported-format errors. Output preview uses the actual composition renderer, initially via a cancellable temporary rendered preview; interactive still-frame seeking is added separately. Do not implement a custom codec or audio engine.
- Linux first; avoid gratuitously platform-specific paths or commands. No browser, server or cloud requirement. CPU decoding/rendering is the baseline; hardware acceleration is optional future work. No heavy ML or alignment libraries in the manual phase.
- Use subprocess argument lists, never shell strings. Check exit status and capture useful stderr. Long-running probe/decode/render operations must not block the GUI thread. Cancel subprocesses and clean up owned temporary files. Never log private media contents.

## Architecture and dependency direction

Use package `video_editor` (adapt names if an existing project already establishes them).

- `core/`: immutable or explicitly controlled domain records, timeline operations, command history, validation. Standard library only; no Qt, FFmpeg, filesystem-dependent editing logic or global application state.
- `media/`: asset probing, metadata, decoding, preview cache and FFmpeg adapter; no UI dependencies.
- `render/`: pure render-plan construction from a project snapshot, backend invocation, progress and cancellation. The preview and export paths share composition semantics.
- `project/`: project serialization, migrations and asset relinking; no GUI dependencies.
- `ui/`: Qt application, timeline, media bin, viewer, inspector and user actions. UI calls domain commands; it does not mutate clips directly or invent timeline semantics.
- `tests/`: pure unit tests, Qt tests where appropriate, FFmpeg integration tests with generated fixtures. Skip dependency-sensitive tests only with an explicit reason; never count skips as passes.

Avoid needless abstractions: concrete interfaces and dataclasses are sufficient until a second implementation is justified. Do not add a plugin framework, database, custom scripting language or distributed services.

## Domain model and invariants

- `Asset`: stable ID, path/reference, fingerprint or file stat identity, probe metadata. Asset may contain any number of video/audio streams; identify streams by actual container stream index. Keep language, channel count/layout, nominal frame rate, dimensions, start time and duration when known; do not fabricate missing data. Unsupported or uncertain metadata is reported explicitly.
- `Track`: stable ID, kind (video/audio), ordered stacking priority, name and enabled/mute state. Video: topmost enabled clip wins; uncovered areas are black. Audio: sum all unmuted active tracks with clip gain and fade envelopes; gaps are silent. No cross-track exclusivity. Initially reject overlapping clips on the same track; overlapping on different tracks is intentional. A/V links are editing conveniences, never a media restriction.
- `Clip`: stable ID, asset ID, stream index, track ID, source interval `[in,out)`, output start, positive rational playback rate (source seconds per output second), optional audio gain/fades, optional link-group ID. Output duration = source duration / rate. No implicit ripple, gap removal or changes to unrelated tracks. A/V linked edits must be explicit, atomic and preserve relative offsets. Independent editing is always possible via unlink.
- Use exact rational seconds (`fractions.Fraction`) in the core, including frame rates and rates such as 25/24 or 1001/1000; never accumulate floating point timestamps. Persist fractions as numerator/denominator. Half-open intervals. Source times use an explicitly documented asset-local presentation-time origin; preserve stream offsets relative to that origin. VFR frame access uses actual presentation timestamps, not frame number times average FPS. UI rounding and render quantisation occur only at their boundaries. Clip edits must respect available source stream bounds and nonnegative output time. Reject invalid commands atomically.
- The project owns media references, ordered tracks, clips and output settings (size, rational frame rate, audio sample rate, layout); playhead and zoom are UI state. Empty compositions and gaps are legitimate. Output end = maximum end of participating clips (including muted/disabled tracks so toggles do not shift duration); empty output has duration zero. The output settings are explicit, not permanently tied to any asset.
- All edits are transactions with undo/redo; selection/navigation do not enter edit history. Stable IDs survive serialization and undo/redo. A split produces two clips, one retaining the original ID; define link-group split behavior and test it. No negative duration or silent destructive source changes.

## UI and interaction

A familiar, compact editor: media bin, viewer, multitrack timeline and clip/track inspector. Avoid workflow wizards, alignment-only modes and forcing all work through a particular edition. Timeline has an explicit playhead, zoom, scroll, track labels, and independently selectable A/V clips. Provide precise numerical entry as an alternative to dragging. Show errors and unavailable assets clearly. Audio editing must not require selecting video.

Keyboard-accessible basic operations; documented shortcuts. Do not rely on colour alone to distinguish sources or state. Maintain usable behaviour at long-film durations; avoid creating a widget per frame or extracting every frame of a 4K movie. Display thumbnails/waveforms lazily if implemented. Keep background work cancellable and cache bounded.

## Scope boundary / later work

TASK_00–TASK_08 deliver the manual editor. Do not start automated matching until the human has reviewed the manual workflow on real films. Later assistance should be optional and scoped: establish correspondences between arbitrary source intervals/anchors, detect local discrepancies, propose transformations, verify and apply changes as ordinary undoable edits. Relationships must remain distinct from composition choices; no global reference asset or required chronological matching. Do not implement this future data model or algorithms now.

## Quality bar

Tests must cover fractional rates, nonzero stream origins, gaps, independent tracks, clipping boundaries, long timelines, invalid edits and failure/cancellation paths as relevant. Integration fixtures must have distinguishable video and audio so correctness is measurable (e.g. solid-colour sections and sine tones at different frequencies); testing only file existence is insufficient. For real-world format features not implemented, fail clearly rather than silently produce incorrect media. Avoid adding unrelated functionality.
