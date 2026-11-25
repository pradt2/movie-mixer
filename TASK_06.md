# TASK_06 — Composition preview and precise inspection

Prerequisite: TASK_05. Read AGENTS.md. Enable iterative editing with truthful audiovisual feedback.

## Implement
- Output viewer supports rendering and playing a user-selected range (default a short window around playhead), using the same render plan and semantics as export with lower-resolution preview encoding. Map preview-relative playback time to absolute output time. Include black/silence and actual mixed audio. Render the current snapshot only; clearly mark a preview stale when composition changes and never present stale frames as current output.
- Provide precise paused output-frame inspection at playhead using a bounded asynchronous decoder/render request. Resolve the winning video clip and map output time to asset-local PTS; account for output frame quantisation so the displayed image agrees with export. Frame stepping uses explicit output FPS. Source frame stepping uses actual neighbouring presentation timestamps (including VFR); expose the source PTS and allow precise source-in/out selection. Document any decoder limitations.
- Add frame/range preview controls, play/pause, seeking and cancel. Keep source and composition viewer modes clearly labelled. Use generation IDs or equivalent to drop obsolete asynchronous results when seeking rapidly. Do not launch a new full-film render on every edit.
- Cache preview files and decoded frames with bounded disk/RAM budgets; key by asset identity, settings and relevant snapshot or plan. Clean up owned files on eviction and shutdown. Show actionable errors if playback backend rejects a generated preview.
- Add visible stale/loading/error state and maintain UI responsiveness during render and seek. Playback does not mutate the composition; no reliance on switching between source players for simulated mixed audio.

## Acceptance
- Seek to intervals with pictures from A and B, black gaps, and mixed audio. Preview a range spanning a cut and compare sampled picture/audio with the exported equivalent at matching time. Verify frame stepping with a VFR fixture and nonzero timestamps.
- Modify a clip during preview generation; stale results are discarded or explicitly labelled, and rebuilding reflects the edit. Cancel and close safely. Rapid scrubbing remains responsive and cache size bounded.
- Verify a short preview manually with actual audio if the environment permits. This is a rendered-range preview, not a claim of real-time NLE playback. Document latency and limitations honestly.
