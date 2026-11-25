# TASK_04 — Manual editing interactions

Prerequisite: TASK_03. Read AGENTS.md. Expose core editing operations through a small set of direct timeline interactions and precise inspector controls.

## Implement
- Select single/multiple clips; drag to move in time or to compatible tracks. Trim either edge, split at playhead, delete selected clips leaving gaps, duplicate to explicit destination, and insert selected source intervals. Add keyboard shortcuts and undo/redo actions. Show tentative drag/trim geometry during gesture; commit one command on release, or reject with a useful explanation. Escape cancels. Selection must not change project content.
- Inspector supports exact source in/out, output start, rate (including rational input), gain in dB and audio fade durations. Provide pitch-preserving rate adjustment as default audio intent; renderer will implement it. Track controls: add/delete/reorder, rename, mute/enable; link/unlink A/V; make linked selection behaviour visible and offer independent edits via unlink.
- Provide optional snapping to playhead and clip boundaries with visible feedback, an on/off toggle and predictable tolerance in screen pixels. Precision entry bypasses snapping. No implicit ripple; explicit gap removal can be considered later.
- Operations must share core command APIs; no duplicated edit math in widgets. Preserve valid selection after edits, undo/redo and track removal. Show validation errors non-modally where possible.

## Acceptance
- Manually build a mixed timeline: video from A, audio from B; remove one piece, split another, move sound independently, change speed, add alternative footage, undo and redo. No render required yet.
- Test representative GUI actions against domain state, drag cancellation, snapping, invalid overlaps, grouped edits, numeric inspector validation, undo/redo and boundary math.
- Timeline remains responsive with hundreds of clips and long asset durations. Every visible control works or is absent; no decorative editing buttons.
