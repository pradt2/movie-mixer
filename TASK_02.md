# TASK_02 — Non-destructive timeline domain and commands

Prerequisite: TASK_01. Read AGENTS.md. Implement the core without Qt or FFmpeg dependencies.

## Implement
- Define Project, OutputSettings, Track, Clip, LinkGroup (or equivalent), exact time helpers and validation. Asset and stream references resolve to the imported metadata. Enforce AGENTS.md semantics including stream bounds, track kinds, overlap rules, rate, gaps, output end and ordering.
- Commands: add/remove/reorder/rename track; add/remove clip; move clip (including compatible track changes); split at output time; trim start/end; set rate with source interval fixed and output start fixed; set enabled/mute, audio gain and fades; link/unlink clips. Add grouped import as an explicit atomic operation creating independent audio/video clips at their correct relative stream offsets. No implicit ripple. Deleting a populated track requires explicit command and is undoable.
- Define exact split/trim math. Split source position = source in + (split output time - output start) * rate. Left trim preserves original output end and source out; right trim preserves start and source in. Allow extending trims only within known source bounds. Split/trim fade behavior must preserve envelopes across unaffected material or explicitly reject unsupported cases; no silent envelope jumps. Define linked-operation eligibility: if a split/trim is incompatible with a linked member, reject transaction or require unlink; never partially apply a linked command.
- History supports undo/redo of atomic edits, redo invalidation on new edit, stable IDs and dirty-state support. Project snapshots are safe for concurrent rendering. Provide query APIs for active clips at time, interval intersections, output extent and resolved stream time.
- Document invariants and decisions in docs/architecture.md. Include examples of rate adjustment and stream offset mapping.

## Acceptance
- Unit tests on exact fractions (including 24000/1001 and 25 fps), long timelines, boundaries, nonzero stream offsets, split/trim/rate, gap preservation, invalid overlaps, linked/unlinked commands, atomic failure, undo/redo and stable IDs.
- Create a composition with picture from one source and sound from another; independently move and edit the sound. Core tests run without a GUI or media executables.
- No UI gestures or rendering yet; do not introduce alignment state.
