# TASK_19 — Move source-window extraction off the GUI thread

Prerequisite: TASK_18. Scope: source stream window preview/extraction call site in main_window.py and worker handoff; no editing/format changes.

## Work
Use an immutable request with asset identity/stream, source interval, session epoch and generation; run extract_stream_window under TASK_13 runner via TASK_17c adapter off-thread. Change selected stream/source/window invalidates in-flight work; cancel and remove only owned staging files. Verify source still current and result belongs to newest request before attaching it to player. Release old player file safely. No GUI thread blocking or repeated polling loop for FFmpeg. Source player transport behaviour unchanged.

## Acceptance
Delayed fake extraction shows UI heartbeat and permits New/Open or switching streams; queued old completion cannot replace newer preview. Failure/cancel reports appropriately and leaves no temporary output. Real video and audio-only streams still preview correctly (synthetic offscreen tests; human listening not claimed). Run extractor, media and viewer tests.
