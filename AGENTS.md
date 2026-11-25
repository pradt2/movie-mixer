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
