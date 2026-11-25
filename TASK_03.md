# TASK_03 — Source viewer and timeline navigation

Prerequisite: TASK_02. Read AGENTS.md. Make it possible to see and navigate media and a composition before exposing destructive-looking editing gestures.

## Implement
- Organise window into media bin, source/output viewer area, timeline and inspector. Keep source and output navigation conceptually separate; viewer can switch mode and clearly identify which timebase is shown. Output playback is not implemented until TASK_06; show a truthful placeholder for now.
- Source mode: select an asset or stream, play/pause, seek, step, display current time and duration, choose audio stream where the backend supports it. Use Qt Multimedia for playback; if backend cannot play a selected stream reliably, report that fact and offer a bounded FFmpeg-generated preview of that stream rather than silently playing another. Keep source timestamps consistent with probe origin. Frame stepping must use actual presentation timestamps or be clearly labelled approximate for VFR; exact frame inspection is delivered in TASK_06.
- Timeline displays ordered tracks and clip spans from the core. Add time ruler, playhead, horizontal scrolling, zoom centred around playhead/cursor, selection and precise time entry. Timeline scales to multi-hour media; draw visible clips only, no per-frame widgets. Distinguish audio/video, asset names, enabled/muted state and selection using labels/icons as well as colour.
- Provide a simple action to add selected streams to the timeline at playhead, with compatible tracks created when needed and no unexpected overlaps; preserve stream offsets for grouped import. Add/remove empty tracks. Inspector displays selected clip details read-only for now. Keep selection and playhead independent from history.

## Acceptance
- Open a short local file, play/pause, seek, and inspect streams. Add two assets or streams to separate tracks and navigate to arbitrary times; zoom from whole film to seconds without losing position.
- Tests for pixel↔time conversion, selection, track display, source/output navigation separation and insertion actions. Qt smoke tests offscreen; manually test actual playback if available.
- Output viewer must not pretend source playback is composition playback. No automatic matching or timeline drag editing yet.
