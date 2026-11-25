# TASK_01 — Import and inspect media

Prerequisite: TASK_00. Read AGENTS.md. Make the media bin useful before implementing the timeline.

## Implement
- Add a typed asset/stream metadata model and ffprobe JSON adapter. Import one or more files via a file dialog; support audio-only and video-only as well as multiplexed files. Detect stream type, index, codec, language, rational frame rate, video dimensions, audio sample rate and channels, start time and duration where available. Do not equate container duration to each stream duration blindly.
- Define asset-local presentation-time origin and stream bounds; store raw probe fields needed to convert to backend timestamps. Handle missing duration/PTS, malformed files, unsupported streams and duplicate imports deliberately. Unknown bounds must not be treated as zero. If reliable bounds cannot be determined, permit inspection but disable editing that asset with an explanation until supported.
- Run probe in a background worker, show progress/error, allow cancellation and avoid race conditions when multiple imports complete. Keep stable IDs; do not copy media by default.
- Media bin lists assets, expands video/audio streams, and displays relevant details. Show path, duration and missing/unsupported stream status. No assumptions about which asset or language is preferable.
- Write test fixture helpers to generate short media with FFmpeg (different colours/tones, multi-audio streams, audio-only, nonzero stream offsets). Store fixtures in temp directories.

## Acceptance
- Import two distinguishable local assets and inspect their separate streams; one contains multiple audio streams. Cancel import and import an invalid file without losing the successful assets.
- Unit tests exercise parsing of missing fields, rational rates, nonzero timestamps and malformed JSON; integration tests check actual ffprobe output and stream IDs against fixture metadata.
- GUI is responsive during probe; no media files modified. No timeline editing yet.
