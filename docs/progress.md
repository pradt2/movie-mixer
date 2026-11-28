# Progress

Task status, deviations and outstanding checks. Verification is only claimed
when actually executed.

Environment used for development: Fedora 44 (Workstation), Python 3.14.10,
PySide6 6.12.0, ffmpeg/ffprobe 8.1.3, pytest 9.2.0, ruff 0.17.6.

## TASK_00 — Bootstrap a runnable project — DONE

Implemented:

- `pyproject.toml` (hatchling, `src/` layout, `video-editor` console script,
  `dev` extra with pytest + ruff), package `video_editor`, README,
  docs/architecture.md, docs/progress.md.
- `video_editor.app`: Qt main window with title, empty central tab area,
  File > Quit, Help > About, status bar startup diagnostic, global
  excepthook logging. No media controls.
- `video_editor.diagnostics`: ffmpeg/ffprobe availability + version parsing;
  never raises; actionable install hint when missing.
- `video_editor.cli`: `video-editor`, `python -m video_editor`,
  `video-editor doctor`.
- Tests: pure import test, offscreen GUI smoke test (launch, Quit action,
  missing-FFmpeg diagnostic), doctor test.

Verified:

- `pip install -e ".[dev]"` succeeds in a fresh venv (uv, Python 3.14).
- `pytest` passes (see task notes below for run output summary).
- `ruff check .` passes.
- `python -m video_editor doctor` reports ffmpeg 8.1.3 / ffprobe 8.1.3 on PATH.
- Offscreen smoke test executed; both `python -m video_editor` and the
  `video-editor` console script additionally launched under `QT_QPA_PLATFORM=xcb`
  on the desktop session (ran until a 4 s `timeout` killed them; exit 124,
  no crash). Result: 5 passed, ruff clean.

## TASK_01 — Import and inspect media — DONE

Implemented:

- `media/assets.py`: `Asset` (stable uuid, path, size+mtime identity, probe
  metadata), `StreamMetadata` (container stream index, kind, codec, language,
  exact rational frame rates, dimensions, sample rate/channels/layout,
  start/duration/time_base/nb_frames, conservative `end_horizon`),
  editability flags and notes.
- `media/ffprobe.py`: JSON adapter, defensive parsing, exact Fraction
  parsing, duration derivation rules, never raises for bad media.
- `media/importer.py`: per-file threads, per-job/global cancellation via
  subprocess kill, duplicate-safe bookkeeping left to the UI controller.
- `ui/media_bin.py`: `MediaBinController` (Qt-signal bridge from worker
  threads → main thread; duplicate detection; stable IDs) + `MediaBin`
  tree (asset → expandable streams, details, muted styling + tooltips for
  uneditable/conservative streams).
- `ui/main_window.py` (window moved to `ui/`; `app.py` re-exports):
  Import Media… (Ctrl+I), Cancel Import, status-bar progress/errors.
- `ui/format.py`: exact-rational time/rate formatting at the UI boundary.
- `tests/media_fixtures.py`: ffmpeg lavfi fixtures — multiplexed 3-colour
  video + 2 audio streams (eng/fre), audio-only WAV, video-only MP4 @
  24000/1001, whole-asset +2.5 s origin offset (mkv `-output_ts_offset`),
  per-stream +1.0 s audio offset (mkv `-itsoffset`), garbage file.

Verified (`pytest`, offscreen GUI where relevant): 31 passed, ruff clean.

- Parser unit tests: missing fields, `0/0` rates, nonzero `start_time`,
  `N/A`, malformed JSON/structs, derived durations, unknown-vs-horizon
  bounds, `und` language.
- Integration tests against real ffprobe 8.1.3 fixtures: stream kinds and
  indices, languages eng/fre on separate audio streams, exact
  `24000/1001` rate, whole-asset origin ≈2.5 s, audio stream origin
  ≈1.0 s with end ≈3 s, WAV editable with origin 0, garbage → error.
- Importer tests: valid+invalid batch keeps successes, missing file,
  cancellation (killed probe → "cancelled"), three parallel imports each
  complete exactly once with distinct IDs.
- Offscreen GUI test: two-level tree populated, 3 stream children,
  duplicate import rejected with message, failed import reported.

Fixes made during the task (regression-tested): `Fraction` has no `to_int`
(use exact floor+half-up in `format_time`); `wait_imports` must drain queued
signals before asserting.

Notes / deviations:

- MKV/TS containers report no per-stream durations → introduced the
  documented *container horizon* upper bound (see architecture.md) instead
  of disabling editing for every mkv film; UI labels it conservative.
- ffmpeg 8.1.3 reports MKV `nb_frames`/`nb_samples` as absent; the horizon
  path covers this.

## TASK_02 — Non-destructive timeline domain and commands — DONE

Implemented (`core/`, standard library only):

- `core/model.py`: `Project`, `OutputSettings`, `Track`, `Clip`,
  `StreamSource`, `envelope_gain()`, query APIs (video stacking topmost-wins,
  audio contributions at time, interval intersections, output extent,
  exact resolved stream time via `Clip.source_time_at` /
  `output_time_for_source`).
- `core/commands.py`: register source; add/remove/reorder/rename track;
  enable/mute; add/remove/duplicate/move clip (with compatible track
  changes); split; trim (in/out within bounds); set rate/gain/fades;
  link/unlink; atomic `ImportGroupCommand` preserving stream offsets;
  `validate_project()`. All atomic — failures raise `CommandError` with no
  state change. Fade-aware split/trim rejection (no silent envelope jumps).
- `core/history.py`: `ProjectSession` with snapshot undo/redo, redo
  invalidation, no-op skipping, dirty-vs-saved tracking, external-mutation
  guard.
- `core/timeutils.py`: exact parse ("24000/1001"), half-open overlap.

Verified: 104 tests pass (`pytest`), ruff clean. New core coverage (51 core
tests): exact 24000/1001 split/trim/rate math, ~2 h frame accumulation with
zero drift, half-open boundary rules (split at edges rejected, touching
clips allowed), nonzero stream origins (source_in before origin rejected),
gap preservation on move/delete (no ripple), invalid overlaps incl. atomic
rollback identity checks, linked move/split/rate all-or-nothing, linked
trim/split eligibility rejection, unlink independence, fade fit/cut-through
rules, grouped import offsets (incl. audio origin 1.5 s and anchoring at
minimum origin), muted/disabled duration stability, undo/redo round trips
with stable ids, redo invalidation, dirty tracking through undo, external
mutation refusal, track removal cascade + undo, and a picture-A/sound-B
composition edited independently end-to-end.

Notes / decisions (see architecture.md): trim may extend within known
stream bounds; linked trims are inward-only; fade edits are rejected rather
than partially reshaped; history snapshots (not inverse commands).

## TASK_03 — Source viewer and timeline navigation — DONE

Implemented:

- Layout: media bin dock, viewer (Source/Output modes with explicit mode
  combo + provenance labels) above the timeline (QSplitter), inspector dock.
- `ui/viewer.py`: `SourceViewer` (Qt Multimedia play/pause/seek/step, slider,
  exact asset-local readout; stream-specific playback via FFmpeg preview;
  honest backend labels; approximate frame-step labelled as such; backend
  errors reported with the preview fallback suggested). `OutputViewer` is a
  truthful placeholder showing output-time playhead only.
- `media/extractor.py`: bounded (≤60 s) single-stream FFmpeg preview
  extraction; returns None on failure — callers never substitute media.
- `ui/timeline.py`: ruler with adaptive nice-step ticks, headers with
  kind/state text + E/M toggle buttons, viewport-culled clip painting,
  playhead, Ctrl+wheel zoom-at-cursor, wheel scroll, keyboard nudge, exact
  Fraction pixel↔time conversion, selection (Ctrl-click additive),
  clip/track hit testing.
- `ui/inspector.py`: read-only clip/track/asset details incl. exact
  rational rate with its `Fraction` form.
- Placement `Ctrl+G`, `Go to Time…` (accepts `H:MM:SS.mmm`, plain seconds,
  or rationals like `24000/1001`), fit/zoom, add track, remove-empty-track,
  split at playhead (S), delete (Delete), undo/redo actions. `ui/format.py`
  `parse_time_text` for precise entry.

Verified: 125 tests pass, ruff clean.

- New tests: exact pixel↔time roundtrips incl. fractional pps and 2 h
  offsets; zoom centring; zoom clamps; fit; hit testing incl. gaps; ruler
  click sets playhead; selection signals; culling contract with 300 clips;
  mode switching (`viewer_stack` page changes), placeholder honesty text,
  asset-local provenance label; inspector rows; grouped placement preserving
  ~1.0 s stream offset from a real `-itsoffset` fixture; two-asset placement
  to separate tracks with full undo; overlapping placement creating an
  extra track with `validate_project()` clean; extractor tests proving the
  preview contains *only* the selected stream (language `fre` survives),
  window offsets, half-open `covers`, missing source → None.
- Manual desktop checks (`QT_QPA_PLATFORM=xcb`, real session): Qt Multimedia
  (FFmpeg backend) played the 3-stream mkv fixture — PlayingState, 82
  position updates in 4 s, zero errors; full editor window launched with
  Import/Place/Undo actions present.

Notes:

- Source frame stepping is deliberately labelled approximate until the
  preview decoder exists (TASK_06); no VFR PTS stepping claimed yet.
- A placement can create one extra "register source" history entry before
  the clip command; undoing a placement may therefore need 2 undos. Cosmetic
  history granularity issue, safe by LIFO ordering (documented above).

Limitations / notes:

- GUI playback not exercised by tests (offscreen platform has no media
  backend); manual checks happen when a viewer exists (TASK_03+).
- Not a git repository commit workflow: nothing committed (AGENTS.md: do not
  commit unless asked).

## TASK_04 — Manual editing interactions — DONE

Implemented:

- `ui/gestures.py` (pure, widget-free): `snap_candidates`, `snap_time`,
  `drag_delta_from_pixels`, `trim_source_time`, public `SNAP_TOLERANCE_PX = 8`.
- `ui/timeline.py` gesture state machine: press on clip body = move drag,
  press within `EDGE_ZONE_PX` (7) of an edge = trim drag, `DRAG_START_PX` (3)
  threshold so click-to-select never creates an edit, dashed ghost geometry +
  snap marker while dragging, `Esc` cancels, release emits exactly one
  `clipMoveRequested` / `clipTrimRequested`. Track-under-cursor changes are
  limited to same-kind tracks. Hover cursors: `SizeAllCursor` (move),
  `SizeHorCursor` (trim), `ClosedHandCursor` while dragging.
- Snapping: 8 **pixel** tolerance through the rational zoom (constant reach on
  screen), attracts to playhead and to boundaries of clips outside the dragged
  group; on by default, toggled from the Timeline menu; gestures only.
- `core/commands.py`: `DuplicateClipsCommand` (block copy by uniform delta,
  links inside the block regrouped, atomic placement validation) and
  `InsertClipCommand` (explicit ripple on the target track for clips starting
  at/after the insert point, linked mates shifted as a block, insert *inside*
  an existing clip refused with an explanation).
- `ui/inspector.py` is now editable: exact source in/out, output start,
  rational rate, gain (dB), fades, Apply; link/unlink buttons; track rename +
  move up/down. All non-empty fields are parsed before anything is applied;
  invalid input is marked in place and applies nothing.
- `ui/main_window.py`: gesture signals → single core commands; `Ctrl+D`
  duplicate with numeric destination; `Ctrl+Shift+G` ripple insert; snapping
  toggle; track rename/reorder; selection sanitised after every history
  operation (stale clip ids dropped).
- Bug fixes found by the new tests:
  - Negative destinations/positions raised the model's `ValueError` instead of
    `CommandError` in `AddClipCommand`, `InsertClipCommand`,
    `DuplicateClipCommand`, `DuplicateClipsCommand` — the UI's single
    `except CommandError` would have crashed. Guarded + regression tested.
  - `test_placement` depended on `bin_controller.assets` order, which is
    import-completion (thread) order. Now assets are looked up by path.

Verified: 169 tests pass, `ruff check .` clean.

- New pure tests (`tests/test_gestures.py`): candidate exclusion, pixel-defined
  tolerance at two very different zooms, disabled snapping, nearest-candidate
  choice, exact pixel→time delta at `24000/1001`, trim source math with rate 2.
- New core tests (`tests/test_core_commands.py`): block duplicate (uniform
  shift, fresh ids, originals untouched, link regrouping, cross-track
  preservation), bad destinations, ripple confined to one track, fractional
  ripple length at `24000/1001`, insert-inside-clip rejection, linked-mate
  shifting and post-shift conflict aborting the whole insert, kind/bounds
  rejection, and `CommandError`-not-`ValueError` for negative positions.
- New GUI tests (`tests/test_editing_gui.py`, 23 tests): drag move commits one
  command and is one undo step; drag to another compatible track (+undo);
  same-track overlap refused with unchanged project and a status message;
  Escape cancels; snapping lands exactly on a clip boundary; linked pair moves
  together; right/left trim drags give exact source values (+undo); inspector
  populate/apply, exact rate and dB gain, invalid input refused with red field,
  link/unlink; duplicate block with destination and refused overlap; ripple
  insert incl. linked mates; selection survival across undo and track removal;
  gestures driven through **real Qt events** (`QTest` mouse/key), hover cursor
  shapes, snapping toggle changes the landing time, track rename/reorder with
  out-of-range refusal, and a 483-clip / 2-hour project that repaints 10×
  within 3 s while a far-end drag stays exactly `+5 s`.

Manual desktop checks (`QT_QPA_PLATFORM=xcb`, `DISPLAY=:0`, real editor
window, 3-stream `mixed.mkv` synthetic fixture, gestures driven through the
real Qt event pipeline with `QTest`; 18/18 checks passed, no failures):

- Import → place video stream 0 at t=0 and French audio stream 2 at t=2;
  output intervals shown as exact fractions.
- `Esc` during a drag cancels and leaves the project unchanged.
- Drag with snapping on: released 2 px short of the audio clip's start, landed
  **exactly** on it (`output_start == 2`).
- Trim-right drag shortened the clip, kept `output_start`, produced exact
  fractions (`[2, 73/10)` output, `src 0–53/10`).
- Undo of trim = one step, undo of drag = one step, redo restores the drag.
- Inspector: `0:00:07.375` applied as `59/8`; `25 fps` refused with the field
  marked and no edit; `24000/1001` applied with the exact rational output
  duration.
- Dragging the audio clip over the video **row** does not change its track
  (the widget only offers kind-compatible drop targets); the clip moved in
  time only.
- Placing a second video clip on the same track, then dragging it onto the
  first: refused with "Overlaps clip … clips on one track must not overlap
  (use separate tracks)", clip unmoved, nothing added to history.

## TASK_05 — Composition rendering and export — DONE

Implemented:

- `render/plan.py` — pure plan from an immutable `Project` snapshot + half-open
  output interval. `Quantisation` (global frame/sample indices, `round_half_up`,
  item lengths as index differences), `VideoItem`/`AudioItem`, stacking
  resolution (topmost enabled wins), explicit black and silence, per-item source
  ranges with rate, fade anchors in output time, conservative-end warnings, and
  a worst-case peak-level warning for summed audio. No Qt, no FFmpeg.
- `render/graph.py` — plan → one FFmpeg invocation: `[in:v:N]`/`[in:a:N]`
  stream addressing, `-ss` relative to the container's own start, exact
  `trim=end_frame` / `apad`+`atrim=end_sample` counts, aspect-preserving
  scale + pad + `setsar` + `format`, chained `atempo`, `volume=eval=frame`
  envelopes matching `core.model.envelope_gain`, `amix=normalize=0`,
  `normalise_layout()` (refuses layouts outside the documented set),
  `encoder_available()`, presets `MP4_H264_AAC` and `LOSSLESS_INTERMEDIATE`.
- `render/exporter.py` — staging file + rename on success, non-overwrite guard,
  post-render ffprobe duration check against the plan
  (1 frame + 2048/48000 s tolerance), progress from `-progress pipe:1`,
  cancellation that terminates FFmpeg and deletes owned temp files, chunked
  rendering for plans above 96 items (sequential lossless chunks joined by the
  concat demuxer into a single lossy encode) with a disk-space preflight.
- `StreamSource` grew `stream_position` and `input_start` (see
  docs/architecture.md): FFmpeg addresses streams by type + position and measures
  input `-ss` from the container's start, neither of which is the container
  stream index used as domain identity. `media.assets.AssetMetadata.position_of()`
  derives the position, `ui.main_window.core_source_for()` records both, and the
  renderer refuses rather than guessing when they are missing.
- `ui/export_dialog.py` — destination + browse, format, whole/playhead/custom
  range with exact time parsing, live plan summary (duration, frame/sample
  counts, item and black counts, geometry, colour limits, planner warnings),
  invalid range disables Start with the reason in place.
- `ui/render_worker.py` — `QThread` subclass running the render off the UI
  thread, queued progress/finished/failed/cancelled signals, cancel, `__del__`
  safety, `wait_finished()` that pumps the event loop for shutdown/tests.
- `ui/main_window.py` — File ▸ Export Composition… (`Ctrl+E`), Cancel Export,
  status-bar progress bar, overwrite confirmation, plan warnings surfaced in the
  status bar and log, export disabled with a message when ffmpeg is missing, the
  composition is empty, or an asset is missing on disk, closeEvent asks before
  abandoning a running export.

Verified: 298 tests pass (0 skipped), `ruff check .` clean.

- New pure planner tests (`tests/test_render_plan.py`, 26): half-up rounding on
  the frame and sample grids, non-frame-aligned boundaries resolved once by
  global index, stacking/black/silence, ranges, fades anchored absolutely,
  fractional rates (`24000/1001`, `25/24`), conservative ends, kind mismatch,
  unregistered streams, 2-hour plans.
- New graph tests (`tests/test_render_graph.py`, 21): argv shape, exact
  `trim`/`atrim` counts, `fps` normalisation, chained atempo staying in range
  with exact product, envelope expression vs `envelope_gain` at sampled times,
  `amix=normalize=0`, layout refusal, seek relative to container start,
  type+position addressing, refusal when position is unknown, black fill.
- New exporter unit tests (`tests/test_render_exporter.py`, 22): chunk ranges
  contiguous with exact frame/sample totals (picture-heavy and audio-heavy),
  per-chunk item bound, input deduplication bounded by items, missing
  ffmpeg/encoder/asset/directory guards, non-overwrite, temp-space preflight,
  `probe_duration` exact rational parsing and its failure paths, 2-hour planning.
- New FFmpeg integration tests (`tests/test_render_export.py`, 10 and
  `tests/test_render_ffmpeg_features.py`, 7): exports are **decoded back** and
  measured — frame colours at exact frame indices (index-selected, not seeked,
  which measured the neighbouring frame), spectral magnitude of each tone via
  Goertzel over sample-accurately trimmed windows, silence in gaps, fade level
  ramp, half-gain contribution, output stream parameters, render-range exports,
  chunked export frame-for-frame identical to single-pass, cancellation leaving
  no file and no temp files, non-overwrite protection, container-offset
  (`start_time 2.5 s`) and stream-offset fixtures, jittered/irregular PTS
  (VFR-like) sources rebased to constant output rate, a `24000/1001` source at
  25 fps output, and selecting the second audio stream of a 3-stream file with
  resampling from 44.1 kHz.
- **Corrected during TASK_08 validation.** The "fade level ramp" check above only
  covered a clip beginning at the render range start, where FFmpeg's filter clock
  and composition output time agree. A fade on a clip placed anywhere else
  rendered with **no fade at all**: the envelope was applied to the contribution
  *before* the leading silence that positions it, so the clock it was written in
  restarted at 0 and the anchors were never reached. Fixed in `render/graph.py`
  (the envelope is now applied to the positioned stream), measured in a rendered
  file by `test_fades_are_anchored_to_output_time_not_to_the_clip_local_stream`,
  pinned at graph level by `test_the_envelope_is_applied_to_the_positioned_contribution`,
  and old cached previews are invalidated by `render/cache.SEMANTICS_VERSION`.
  Discovered by the two-edition workflow check (`scripts/ab_workflow_check.py`),
  which fades a clip starting at output 2 s.
- New GUI tests (`tests/test_export_gui.py`, 26): dialog plan summary, range
  modes incl. playhead, invalid/unparseable ranges disabling Start and
  re-enabling on correction, missing directory, existing destination warning,
  planner warnings shown, preset extension; worker success/failure/cancel/
  progress signals (deterministic via a fake render), one render at a time,
  render never on the GUI thread; window wiring — modal dialog path, overwrite
  confirmation required before replacement, snapshot stability while editing,
  progress/cancel UI state, failure and cancellation messages, refusals for
  missing ffmpeg, empty composition and missing assets, and a real export
  through the controller writing a real MP4.
- `SetOutputSettingsCommand` (core) and the inspector's **Output (composition)**
  group: width, height, rational frame rate, sample rate and channel layout are
  project state, changed by one undoable edit and never inferred from an asset.
  The core rejects non-positive/empty values and no-ops; encodability is decided
  in the render layer (`normalise_layout` refuses layouts outside the documented
  set) and the inspector offers only those layouts. Field parsing is
  all-or-nothing with the offending field marked.
  (`tests/test_output_settings.py`, 16 tests, including that a settings change
  retimes no clip and that the export summary follows the current settings.)

Manual desktop check — **unattended**, `QT_QPA_PLATFORM=offscreen` (no windows on
the desktop, no human clicks; every modal dialog is located through
`QApplication.activeModalWidget` and answered by the script). Script:
`scripts/desktop_checks/export_check.py`. **53/53 checks passed, 0 failures.**
(The same script run from its scratch location and then from the repository copy
gave the identical 53/53 result.)
Fixtures generated for the run: a 4×1 s colour-block mkv (1440×1080, all-intra,
whose video stream has no container-reported duration → conservative end) and a
2 s 660 Hz stereo wav.

- Import: both files went through the real bin import queue; both reported
  editable streams.
- Composition built purely by UI interaction: Ctrl+G at playhead 1 s (picture)
  and 0 s (sound) → 2 clips on 2 different tracks.
- Output settings (1280×720, 25 fps, 48 kHz, stereo) applied through the
  inspector, i.e. chosen by the project and not inherited from the 1440×1080
  source.
- `ExportDialog` validated by driving it directly: summary said
  "Duration: 5.000s (125 frames @ 25/1 fps, 240000 samples @ 48000 Hz)",
  "Geometry: 1280x720, stereo, aspect preserved with padding, yuv420p",
  "Picture: 2 item(s), 1 black", the plain-sum mix policy and the colour
  non-conversion statement; a reversed range disabled Start with "Render range
  end is before its start"; unparseable input disabled it with the Fraction
  error; a valid custom range re-enabled it.
- Real modal export flow: Ctrl+E → dialog shown and Start clicked by the script
  → existing destination produced the "Replace existing file?" question, answered
  only then → progress reached 1.0 → `RenderResult.duration == 5` → status bar
  "Exported composition.mp4 (5.00s output)" together with the planner's
  conservative-end warning. No failure/cancellation signals, no unexpected modal
  dialogs, no `.partial.`/`.chunk` files left behind.
- Produced file measured: ffprobe reports h264 + aac, 1280×720, 48000 Hz, 2
  channels, `r_frame_rate=25/1`, duration 5.000 s (within 1 frame + AAC padding).
  Frames decoded and sampled by index: frame 0 black (no picture before 1 s),
  frame 25 = source red, 50 = green, 100 = magenta (source palette sampled the
  same way), and the 4:3 source is letterboxed inside the 16:9 frame (black side
  bar at x=40, coloured at x=700). Audio decoded and measured: 660 Hz energy
  share 0.707 while its clip runs, 0.000 in the gap after it, rms 0.088.
- Live gestures and keyboard: click selects the clip; ←/→ move the playhead and
  leave the project object unchanged; the inspector placed the clip at exactly
  7/5 s as one undoable edit; keyboard Undo then Redo worked; body drag moved the
  audio clip (0,2) → (7/2,11/2) with its length preserved and undo restored it;
  dropping an audio clip on the video track left it on its own track with the
  project unchanged; an edge drag trimmed (source_in increased, output end
  unchanged); a right-edge trim released 3 px from the playhead snapped exactly
  to 3/2 s.
- Disabling the picture track did not change the output duration (still 5 s).
- VLC played the result with dummy video/audio outputs at `--verbose 2`: its own
  `mp4 demux` module, "codec (h264) started", reached end of stream/playlist, and
  reported no corrupt/decode errors. **No human eyeballed the frames**; colour
  and audio correctness are asserted by the decoded-frame and spectral
  measurements above. Re-run with `QT_QPA_PLATFORM=xcb … show` to look yourself.

Findings recorded while writing that check (all now covered by tests):

- Keyboard shortcuts only fire for the window that Qt considers active; after a
  modal closes under the offscreen platform `QApplication.activeWindow()` is
  `None`, so the driver re-activates the window before keyboard checks. A real
  desktop does this by itself.
- Binding `Ctrl+Z` *and* `QKeySequence.StandardKey.Undo` (identical on Linux)
  yields "QAction::event: Ambiguous shortcut overload: Ctrl+Z" and Qt then
  triggers **neither** — undo died silently. Undo now has one binding, and redo
  carries both the documented `Ctrl+Shift+Z` and the platform standard (`Ctrl+Y`),
  which the README previously overstated.
- A drag that should trim must start *inside* the clip's rectangle: pressing at
  or past the boundary pixel finds no clip, so no gesture starts. The 7 px trim
  zone is measured inward from the edge.

## TASK_06 — Composition preview and precise inspection — DONE

Implemented:

- `render/plan.py` — `plan_geometry(project, scale)` plus
  `RenderRequest.geometry_scale`, so a preview is the same plan at a smaller
  picture; `Quantisation.frame_count(seconds)` (**round up**: how many whole
  output frames an output interval presents) deliberately distinct from
  `frame_index()` (the half-up *name* of the frame covering a time).
- `render/graph.py` — `PREVIEW_H264_FAST`: same container, codecs, pixel format
  and picture chain as export, encoder options tuned for speed only.
- `render/cache.py` — `render_fingerprint(project, asset_paths, start, end, scale,
  preset)`; `PreviewCache` (owns its directory, adopts pre-existing files by key,
  LRU bounded by entries and bytes, writes `…​.partial.mp4` and renames on
  success, `close()`/`prune_orphans()` delete only files it owns); `FrameCache`
  and `FrameTimesCache` bound decoded frames and probed PTS lists.
- `render/preview.py` — `preview_range()` (window around the playhead, snapped to
  whole output frames with the planner's own quantisation, clamped and walked
  back at the end, at least 2 frames, refused above the preview limit with
  "export that range instead"), `prepare_preview()` (cache hit → reused job, no
  render) and `PreviewJob`.
- `media/framegrab.py` — `extract_composition_still()` (one-frame plan, the
  exporter's picture chain and stream addressing), `still_plan_for()`,
  `picture_filter_chain()`, `source_frame_times()` (times rebuilt as integer
  `pts × time_base + start_time`, never from ffprobe's truncated `pts_time`),
  `frames_around()`/`nearest_frame()`/`presented_frame()`, and `StillFrame`
  carrying output frame index, exact output time, black flag, clip/asset/stream,
  planned `source_pts` and the real `decoded_source_pts`.
- `ui/render_worker.py` — `PreviewController` (one preview at a time, generation
  tags, a superseding request cancels and waits for the previous FFmpeg,
  `wait_finished()`); `StillWorker` + `StillController` + `WorkerThread` (a
  mailbox worker on its own `exec()` event loop, `QMetaObject.invokeMethod` to
  wake it, newest request wins, superseded results dropped, `wait_idle()`,
  `shutdown()`).
- `ui/viewer.py` — preview panel in the **Output** viewer (range fields, picture
  size 1:1…1:8, Render/Cancel/Free preview cache, Play preview, output-frame and
  source-frame stepping, Set clip in/out), three separate lines so
  preview status, inspected frame and provenance never overwrite each other, and
  explicit rendering / current / STALE / pending / failed / cancelled states.
- `ui/main_window.py` — Preview menu (Render Output Preview `Ctrl+R`, Cancel
  Pre&view, Step Output Frame ◀/▶ `Ctrl+←`/`Ctrl+→`, Free Preview Cache),
  fingerprint-based staleness marking, still requests on playhead moves, source
  stepping by real PTS, source-edge marking as ordinary trims, playback of the
  cached preview with preview-relative → output-time mapping (`playback_offset()`:
  playback starts at the playhead, moving the playhead while playing seeks the
  file instead of decoding stills, a playhead outside the rendered range is stated
  rather than silently mis-played), cache freed on
  close unless `VIDEO_EDITOR_KEEP_PREVIEWS=1`, `closeEvent` stopping render and
  decode threads, and `_invalidate_stills()` on every composition or asset change
  (the viewer cannot keep showing a picture the composition no longer makes).

Verified: 388 tests pass (0 skipped), `ruff check .` clean. The unattended desktop
check (`scripts/desktop_checks/export_check.py`, offscreen, every dialog answered by
the script) reports **80/80**, which includes all TASK_05 checks re-run on top of the
preview/still/cache checks listed below.

- New cache tests (`tests/test_render_cache.py`, 29): fingerprint sensitivity to
  timeline values, range, scale, preset and asset identity (and that a track
  rename does *not* change it), staged commit, budget eviction deleting only owned
  files, adoption of stray files, refusal to delete files it does not own,
  `close()`/`prune_orphans()`, frame cache byte budget, the cache handing back the
  caller's own frame object, `clear()` dropping everything, and PTS-list reuse.
- New preview tests (`tests/test_render_preview.py`, 15): range snapping to whole
  frames, default window around the playhead, walking back at the end, minimum 2
  frames and sub-frame compositions, `playback_offset()` mapping output time into
  the rendered range exactly (half-open end, 24000/1001-style grids), refusal on
  empty/over-long ranges, gaps
  counting towards the preview length, `prepare_preview` cache reuse (render
  skipped entirely) and a *different* key after an edit, preview request carrying
  the reduced geometry with untouched timing/frame counts, chunked previews,
  cache files never landing next to a user export and budgets being enforced,
  planning a request without rendering it, and the acceptance comparison: a preview
  of output 1–7 s (spanning the rate-2 overlay's cut, the end of the picture and
  the silent tail) is decoded back and its frames at indices 12/24/25/62/112 match
  the named source colours **in both files**, 137/149 are black in both, and its
  measured audio matches the export window by window (660 Hz present early with 990
  absent, both contributing later at the documented half-gain ratio, tail silent).
- New frame-inspection tests (`tests/test_framegrab.py`, 21), measured against
  real FFmpeg output: a still's sampled colour equals the frame the **exporter**
  writes at the same index across eight output times, frame index equals the
  full-range planner's `frame_index`, reduced `geometry_scale` changes pixels but
  not time, black gaps produce real black without a decoder, oversized geometry
  refused, missing/unregistered assets and unreadable streams reported rather than
  substituted, the second video stream addressed by position, `presented_frame`
  exact-vs-next behaviour, and on a VFR fixture (8 s @25 fps + 4 s @12 fps,
  Matroska `time_base=1/1000`) that 248 real PTS values are recovered, that
  8.4 s is **not** a frame time while 8.333 s and 8.417 s are, that stepping
  offers the real neighbouring frames on either side (and direction 0 returns the
  frame sitting exactly on the time), and that the reported source PTS is the frame
  the decoder really produces. An offset fixture (whole presentation starting at 2.5 s) pins
  that packet timestamps are already asset-local: 50 frames from 2.5 s with
  `1/25` steps, neighbours of 3.02 s being 2.98 s and 3.06 s, and a still of that
  asset reporting `source_pts == decoded_source_pts == 127/50`.
- New GUI tests (`tests/test_preview_gui.py`, 22): range fields track the
  composition until typed over, reversed/unparseable ranges refused without
  rendering, render/cancel/failure messages, cache reuse producing no second
  render, staleness after an edit vs after a rename, playback mapping preview-
  relative time to output time, playback **starting at the playhead** and a
  playhead move seeking the file instead of decoding stills (with a pause ending
  the seeking and an out-of-range playhead being reported), the playhead decoding
  the exact output frame
  (frame 8 at 0.3 s ⇒ source PTS 8/25), decoded-frame reuse instead of a second
  decode, source stepping by real PTS with `Set clip in/out` producing ordinary
  trims, stepping stopping at the end of a stream, black frames offering no source
  stepping, obsolete results dropped during rapid scrubbing, cache freeing deleting
  only cache files, the decoded-frame cache staying inside its RAM budget, and
  shutdown leaving no running thread.
- Unattended desktop check extended (see below): `Ctrl+R` renders the typed range
  at 1:2 into the cache; ffprobe reports h264 + aac at 640×360, `r_frame_rate=25/1`
  and a 2 s duration; sampled preview frames equal the source colours and equal
  the **exported** file's picture at the same output time ((0,127,0) both); the
  660 Hz tone is in the preview mix; moving the playhead decodes frame 50 whose
  centre pixel equals the export's frame 50; `Ctrl+→` moves exactly 1/25 s and
  decodes frame 51; source stepping 26/25 → 27/25 lands on a real probed frame and
  leaves the output frame unchanged; a track rename keeps the preview current
  while a clip move marks it STALE and disables playback; undo + re-render reuses
  the cached file with **zero** new renders; a 1:1 whole-composition request is
  cancelled mid-render, reports the cancellation, and leaves no `.partial.` file;
  Free preview cache deletes every cached file the run created.

Findings recorded while building this (all now covered by tests):

- PySide6 `QThread` has **no public `wake()`**, and a moved-in worker receives
  nothing unless `run()` enters an event loop: the still worker uses `exec()` plus
  `QMetaObject.invokeMethod(..., QueuedConnection)`.
- PySide silently **truncates** signal arguments when the slot declares fewer
  parameters: connecting `failed = Signal(int, str)` to `def _on_preview_failed(
  message)` passed the *generation* as the message, so failures displayed "FAILED —
  1". Every preview/still handler now takes the full argument list.
- Emitting a signal through a helper (`signal.emit(*args)`) raises
  `TypeError: progress(int,double) needs 2 argument(s), 1 given!`; each signal is
  emitted explicitly with the generation bound as a default argument.
- `timeline.set_playhead()` used to emit `playheadChanged` only for
  user-initiated moves, so programmatic moves (and the still frame under them)
  went unnoticed. The signal now always fires; `from_user` only records *how* the
  playhead moved for status messages.
- A still request made before the first asset import (or before any edit) means a
  window whose playhead sits at 0 shows nothing at all; composition changes and
  completed imports now refresh the decoded frames.
- Reporting `at_or_after` (the *next* frame) as the frame a seek produces was an
  off-by-one: `presented_frame()` prefers the frame exactly at the planned time and
  only falls back to the next one, which the VFR fixture shows is 8.417 s for a
  planned 8.4 s.
- Cancelling a preview leaves nothing behind only if the FFmpeg process has really
  exited first (`_terminate()` waits), and the desktop check now clears its own
  scratch cache directory first so an earlier crashed run's debris cannot be
  mistaken for a leak.
- **Real PTS are already asset-local.** An earlier revision computed them as
  `pts × time_base + start_time`, which double-counted the offset: a file whose
  whole presentation starts at 2.5 s reported its first frame at 5 s, so source
  stepping offered times with no frame at them and an edge marked from one was
  seconds away from the picture on screen. Measured on Matroska and MP4 with both
  whole-asset and per-stream offsets, the fix is `pts × time_base` alone, with a
  regression test on the offset fixture (frame at 2.54 s, neighbours of 3.02 s are
  2.98 s and 3.06 s).
- A test session that leaves a window open leaves its still-frame thread running,
  and Qt aborts the interpreter at exit ("QThread: Destroyed while thread is still
  running", then a core dump) *after* printing the results, which made an otherwise
  green run look broken. `tests/conftest.py` now closes leftover windows and stops
  their render/decode workers at session end, and the suite exits cleanly.

Measured latency (this machine, system FFmpeg 8.1.3, CPU decode; not a benchmark,
so the UI can be judged honestly — note Qt Multimedia logs its own bundled FFmpeg
9.0.1, which is not the copy used for renders): one composition still costs about
**0.40 s** at 128×96 output and **0.47–0.51 s** at 1280×720 (1:1 or 1:2) —
dominated by starting a decoder process, not by pixel count. Listing a stream's
real PTS costs about **0.24 s** for 100 packets (cached per stream, so stepping
after the first step is immediate). Still decodes are asynchronous and
newest-wins, so scrubbing stays responsive; each decode is one frame, never a
range.

Limitations, stated rather than hidden:

- Preview playback is a rendered range through Qt Multimedia, not real-time NLE
  playback: starting a preview costs a full render of that range, seeking inside
  it is the media backend's seek, and the picture is deliberately lower
  resolution. The default window is short and the range is capped; longer ranges
  should be exported.
- A decoded still is one frame at a time; there is no continuous output-mode
  playback of the composition and no timeline scrubbing preview.
- Still frames are decoded with the exporter's picture chain, so the same colour
  handling limitations apply (no colour space/transfer conversion, no HDR).
- `Set clip in/out` writes the *planned* asset-local time (the model is
  continuous); the viewer states the real frame the decoder produced alongside it.
- Preview cache files are ordinary MP4/H.264/AAC files owned by the application and
  are deleted only by eviction, Free preview cache, or window close.

## TASK_07 — Project files, recovery and relinking — DONE

Implemented:

- `project/serialize.py` — the versioned document schema (documented in the
  module docstring and README), exact `"num/den"` rational encoding, strict
  decoding that **refuses unknown fields and unknown versions** with diagnostics
  naming the field/version, duplicate-id detection, and the migration policy
  (`SCHEMA_VERSION`, `MIGRATIONS`). `Navigation` (playhead/zoom/scroll/selection)
  is optional, clamped on load and never part of dirty tracking.
- `project/storage.py` — `build_document` / `decode_payload` / `save_document` /
  `load_document`; `relative_reference` (relative only when it stays inside the
  project directory); atomic write (sibling `.tmp`, `flush`+`fsync`,
  `os.replace`, no debris on failure); `AssetState` (`online`/`missing`/
  `changed`) from recorded size + `mtime_ns`; validation that combines schema,
  cross-references, `validate_project` and non-fatal warnings when saved clip
  intervals no longer fit the recorded stream bounds or the frame rate differs.
- `project/recovery.py` — `RecoveryStore` (app-owned directory
  `$VIDEO_EDITOR_RECOVERY_DIR` else XDG, session key = hash of the project path
  or `unsaved`, headers, pruning, corrupt-snapshot skipping, `discard()` that
  removes only its own files), `disk_ahead_of_snapshot()`, `describe_snapshot()`.
- `media/serialization.py` — asset/`StreamMetadata`/`StreamSource` records,
  encoded and decoded with the same strictness, so recorded probe metadata is
  preserved exactly and a project can be inspected offline.
- `media/relink.py` — `probe_replacement` + `validate_replacement` +
  `relink_asset`: same index/kind, bounds covering every clip interval, same
  frame/sample rate, picture size unless knowingly accepted, notes for
  renumbered streams and shifted container start.
- `core/commands.RelinkAssetCommand` — atomic replacement of an asset's
  registered sources; refuses a partial relink or one that would leave a clip
  outside its stream; undoable like any other edit.
- `ui/main_window.py` — Project menu (New/Open/Save/Save As, Media Status…,
  Relink Missing or Changed Asset…, Recovery Snapshots…), dirty-state title with
  `*`, `_confirm_replace`/`_confirm_quit` unsaved-work prompts, `_write_project`
  (atomic, then dirty baseline + snapshot), relink offer/dialog/refusal, offline
  rendering refusal naming asset + state + path, autosave timer
  (`VIDEO_EDITOR_AUTOSAVE_SECONDS`, default 120 s, `0` off), snapshot-on-close
  for unsaved work, startup restore/discard **only** from `run_app`, restore
  refused when the project file is newer, `_missing_asset_names()` made precise.
- `ui/media_bin.py` — `reset`/`add_external`/`reindex`/`rebuild`/`refresh_asset`
  so the bin follows a loaded project and a relink.
- `ui/viewer.py` — `clear_preview_state()` (a preview belongs to one project).
- `ui/timeline.py` — `BASE_PPS` so a saved zoom means the same thing next time.

Verified as closed: `pytest -q` under `QT_QPA_PLATFORM=offscreen` — **517 passed,
0 failed, 0 skipped** in 139 s, repeated three times with the process exiting
cleanly each time; `ruff check .` clean. The unattended desktop
acceptance run passed **126/126** checks (`exit=0`, no crash at exit): import
queue, gestures, export dialog and overwrite consent, measured export frames and
tones, composition preview and still-frame inspection, cache lifetime, then the
project legs — save over an existing file with and without consent, reopen in a
second window and re-export comparing frames/tones, relative references after a
move, missing-asset detection, offline save, relink, refusal of an incompatible
replacement, recovery snapshot → restore → refuse-when-disk-is-ahead → discard,
the unsaved-work quit prompt, and VLC playback. The suites written for this task
are 83 tests: `tests/test_project_storage.py` 36,
`tests/test_project_relink.py` 10, `tests/test_project_save_gui.py` 26 and
`tests/test_core_history.py` 11 (the last two cover the dirty-baseline rules this
task added).

- Storage tests cover: round-trip of every domain value (odd fractions, rates,
  link groups, fades, gains, muted/hidden tracks, unknown stream end staying
  `None`), exact string rationals on disk, optional+clamped navigation, relative
  references only inside the project directory, a moved project+media pair
  resolving again, absolute references staying absolute, missing/changed/probe-
  failed asset states, "recorded metadata is the baseline" (nothing re-probed or
  adopted), warnings when clips no longer fit, future schema refusal naming the
  version and keys and promising the file was not modified, unknown-field refusal,
  missing-version refusal, no-migration refusal for older versions, corrupt/
  truncated/binary/directory inputs, decimal times rejected as inexact, domain
  invariant validation on load, duplicate ids, and atomicity: `os.replace`
  failing leaves the old file byte-identical with no `.*tmp*` debris, a missing
  directory is reported, three successive saves leave exactly one file.
- Relink tests cover: stable asset id and label preserved, timing untouched,
  missing/kind-swapped stream refused with the replacement's actual stream list,
  material the replacement cannot supply refused with the exact clip interval,
  different frame rate refused as a different timing base, picture size needing
  explicit acceptance, renumbered streams reported and used for addressing,
  unprobeable replacement refused, container offset reported without changing
  source times, and the command's atomicity/undo plus its refusals.
- GUI tests cover: save → reopen in a second window with exact project equality
  (ids, order, fractions) plus navigation restore and clean/undo-free state,
  dirty tracking through save/undo/redo, navigation never dirtying the project,
  Save As rewiring references, the overwrite confirmation on both paths, five
  kinds of unreadable document leaving the current work intact, future-schema
  files not rewritten, New/quit unsaved prompts (Cancel keeps the work),
  a failed save leaving the file byte-identical and the session still dirty,
  relative paths after moving the folder, offline load → refusal → save → relink
  → undo → refusal of an incompatible replacement → a relink whose streams match
  exactly completing as a media change without an edit, media status listing,
  autosave firing on a timer (once, not repeatedly) and restore bringing back the
  exact composition + playhead as **unsaved** work, restore refused when the disk
  file is newer, and startup recovery never interrupting a plain window.

Findings recorded while building this (all now covered by tests or documented):

- **The overwrite consent prompt was inverted.** `_confirm_save_target` returned
  `not (path.exists() and self._confirm_overwrite(path))`, which *refused* the
  write when the user answered "Yes" and *performed* it when they answered "No".
  Reading the test result for the "No" case (`_save_project` returned True) is
  what exposed it. Now: proceed unless the file exists and the user refused.
- **Message-box buttons must be named, never counted.** Qt lays a `QMessageBox`
  out by role and platform style, so a Save/Discard/Cancel box really orders as
  Save…, Cancel, Discard. Code that mapped the clicked button to a position in
  `box.buttons()` therefore read "Cancel" as "Discard" — pressing Cancel on the
  unsaved-work prompt destroyed the work. `_ask` now matches the clicked button
  to the *caller's own* button list by label and returns that index; the GUI
  tests and the desktop check answer dialogs by label for the same reason. A
  suppressed or Escape-closed prompt returns "no choice", which every caller
  treats as a refusal, never as consent.
- **`replace_project(keep_dirty=True)` could not make a new session dirty**,
  because the baseline had already been copied from the incoming content at
  construction. Recovered work therefore looked saved, so closing the window or
  starting a New project would drop it without a word. The baseline is now
  `None` when nothing is behind the content (`ProjectSession(saved=False)`),
  and `is_dirty` is true until the first write.
- **A QTimer interval of 0 means "as fast as possible", not "off".** The
  documented `VIDEO_EDITOR_AUTOSAVE_SECONDS=0` produced a continuous snapshot
  loop; in a test session it also let a window left open by one test write into
  another test's recovery directory. The timer is simply not started for 0.
- **The first save wrote absolute media references** because the document was
  built from `self.project_path` (still `None`) instead of the destination that
  was handed to the save. Relative references are now resolved against the
  directory the file will really live in.
- **Opening an unreadable file used to ask you to discard unsaved work before
  reading it**, so a bad click cost the work and then failed anyway. The file is
  now read and validated first; the unsaved-work prompt only appears when the
  work is genuinely about to be replaced.
- `decode_project` is given the **whole document**, so its "unknown field" set
  must include the sections other decoders own (`schema_version`, `application`,
  `assets`, `navigation`); otherwise every real file is rejected as if it were
  from a newer schema. Caught by the round-trip test, not by inspection.
- The unsaved-work prompt turned every existing GUI test that closes a window
  after an edit into a modal that nobody answers — the suite blocked instead of
  reporting. The prompt is therefore gated by `MainWindow.quit_confirm_required`
  (on for `run_app` and for the desktop check, which answers it; off in the test
  fixture), and tests that want the real prompt provoke it deliberately. A
  blocking offscreen `QMessageBox` does not time out: it just hangs.
- Faking a modal in tests cannot be done by replacing `clickedButton` (Shiboken
  ignores a Python attribute of that name) — the chosen button has to be
  `click()`ed, which is also what a user does. And `QMessageBox.exec` has to be
  replaced with a plain function in the class dict, so Qt passes the box it
  opened; a bound method would be handed back unbound and receive no box.
- Recovery snapshots must be keyed **per project** and scanned for both the
  project's key and `unsaved`; a snapshot with no project path still has to be
  findable after a crash, which is the whole point.
- Dirty tracking is value equality against the saved baseline, so undoing back to
  saved content clears the mark — the title's `*` is honest in both directions.
  The title was also not being refreshed on history changes at all, so the mark
  appeared only after a save; `_refresh_history_actions` now refreshes it.
- **A relink to a file whose streams match the recorded ones exactly was reported
  as a failure.** `_relink_asset_to` ran the composition half through `_apply`,
  which returns "the project did not change" — and the window treated that as a
  refused edit, so the media half (bin row, recorded path) was never updated and
  nothing was said. A replacement is allowed to change no stream information at
  all: the relink now completes, states that the composition is unchanged, and
  leaves undo history alone, because a no-op is not an edit. Found while
  connecting the TASK_08 decoration layer to a relink, and covered by
  `test_a_relink_to_an_identical_file_changes_the_media_not_the_composition`.
- **Recovery discovery was keyed so narrowly that it could not do its job.**
  Snapshots are stored per project, and a window only looked under *its own* key
  plus the never-saved one — but after an abnormal exit the application starts
  with no project open, so the snapshot of the film you were editing sat in the
  recovery directory invisible. A window with no project now finds them (and
  restoring reclaims the project file they belong to), while a window that does
  have a project open still sees only its own work and the unsaved one. Pinned by
  `test_a_start_up_window_finds_the_snapshot_of_the_project_that_was_open` and
  `test_an_open_project_is_not_off_another_project_s_recovery_work`.
- **`StandardKey.Redo` is not always Ctrl+Y.** Under a Wayland/Gtk theme it
  resolves to Ctrl+Shift+Z — the same chord the README documents — so the Redo
  action claimed one chord twice, Qt reported "Ambiguous shortcut overload" and
  the shortcut silently stopped working. It looked fine in the offscreen test run
  and failed on the desktop. The chords are now de-duplicated by their text (what
  Qt compares) and Ctrl+Y is listed explicitly, so both documented chords work on
  either platform; the desktop check additionally fails loudly if any two actions
  claim one chord.

The acceptance harness needed fixing too — each of these was found by *running*
it unattended, not by reading it:

- **The documented "headless" invocation was not headless.** It inherited the
  desktop platform, so the run opened a real window on the user's screen and sat
  in front of a modal asking for a click. The script now forces
  `QT_QPA_PLATFORM=offscreen` unless a desktop run was asked for (`show`) or the
  platform was set explicitly.
- **Answered dialogs were remembered by `id()`.** CPython reuses an address as
  soon as the closed dialog is collected, so the *second* identical prompt (the
  second "replace this project file?") looked already handled and was never
  answered — the run blocked waiting for a human. Dialogs are now remembered as
  objects.
- **A step read `assets[].reference` where the document writes `assets[].path`,**
  a `KeyError` that aborted the project leg precisely where it had never
  previously reached. It is fixed, and the legs behind it are now exercised.
- **Teardown closed windows through the same unsaved-work prompt the legs were
  testing,** so a refused close left a window open and every later leg inherited
  the mess. Teardown now states its own decision instead of asking.
- **A dialog the poller cannot see would hang forever.** There is now a watchdog
  that prints which dialog was blocking and exits non-zero: an unattended check
  must fail loudly rather than wait for someone to click.
- **A green suite was also aborting the process.** After printing "517 passed",
  pytest sometimes died with a segfault or "pure virtual method called" (only
  when stdout was a pipe, so it looked intermittent). Cause: tests that drop a
  `MainWindow` without closing it leave its preview/still/render threads and the
  TASK_08 decoration worker alive while the C++ `QApplication` is torn down first
  — and destroying a running `QThread` aborts the process. The session fixture
  now closes every open window through the window's real shutdown path and stops
  any remaining extraction thread (`decorations.shutdown_all()`, backed by a weak
  registry so a forgotten layer is still reachable). Three consecutive full runs
  now finish `exit=0`; the crash was timing-dependent, so one clean run was not
  evidence.

Limitations, stated rather than hidden:

- Recovery is a snapshot safety net, not a session journal: it restores
  composition + navigation, not undo history, selection of docks, or which
  viewer was open.
- Asset identity is `size + mtime_ns` (no content hashing of films), so a
  replacement with the same size *and* mtime is indistinguishable; relinking
  probes and validates instead of trusting identity alone.
- A relink never adjusts clip timing, so a genuinely different edition needs
  manual re-cutting (which is the product's purpose, not a bug) — it will refuse
  rather than silently retimes.
- Project files are readable JSON but not hand-edited-friendly at film scale
  (one asset record per file, full probe metadata); no compression is applied.

## TASK_08 — Manual editor completion and realistic workflow validation — DONE

Implemented:

- `media/waveform.py` — `audio_envelope(path, stream, start, duration, buckets)`:
  peak buckets for one stream over an asset-local range through FFmpeg
  (`asetnsamples` → `astats` → `ametadata=print`), reading only the requested
  window, refusing (`WaveformError`) on a bad range, an unknown stream, more
  buckets than `MAX_BUCKETS`, or output that cannot be parsed. No Qt, no
  fabricated peaks.
- `media/thumbnails.py` — `video_strip(...)`: one `image2pipe`/MJPEG contact
  strip per clip range at the requested granularity (`MAX_FRAMES` cap), with the
  frame times it actually produced, so a decoration can be placed by real PTS.
- `ui/decorations.py` — `ClipDecorations`: plans what the **visible interval**
  needs, on a fixed granularity ladder (`ladder_step`, `thumb_box`), caches per
  asset + stream + **file identity** in byte budgets (`ENVELOPE_BUDGET` 2 MB,
  `STRIP_BUDGET` 6 MB, LRU), queues at most `MAX_QUEUED_JOBS` 32 jobs on **one**
  worker thread per window, delivers by signal, remembers failures
  (`MAX_TRACKED_FAILURES`) instead of retrying every repaint, and has a real
  `shutdown()` (a live `QThread` must never be dropped). `shutdown_all()` exists
  for test/process teardown.
- `ui/timeline.py` — paints delivered envelopes/strips inside the clip shape,
  always draws geometry/labels/selection/playhead, schedules decorations on view,
  selection and project changes, and declares `WIDGET_KEYS` (its own chords)
  beside the handler. Clips narrower than `MIN_CLIP_PIXELS` and clips outside the
  view are not scheduled at all.
- `ui/main_window.py` — `decoration_material()` (asset + stream + resolved file
  for each clip, `None` for material that cannot be read),
  `refresh_decorations()`, `_forget_decorations()` on project/relink changes, and
  decoration failures surfaced in the status bar rather than hidden.
- Help ▸ Keyboard and Workflow… (`help_action`, `_chord_rows()`, `_help_text()`,
  `WORKFLOW_STEPS`): the chord list is read from the live actions, the
  application states its own manual workflow, and About now points there instead
  of carrying a second hand-kept list.
- `ui/viewer.py` — the preview range fields are labelled as the **output interval
  under review** (`preview_range_hint`), and the render button's own label states
  which range clicking it would render.
- `scripts/make_ab_editions.py` — generates the two synthetic editions and prints
  the manual recipe (clip, track, source file/stream, source in/out, output start,
  rate, gain, fade) plus the properties the result must show, all derived from the
  fixture table rather than prose. Refuses to overwrite without `--force`.
- `scripts/ab_workflow_check.py` + `scripts/desktop_checks/editions.py` — the
  A/B acceptance harness: one table (`PICTURE`, `UNDERNEATH`, `SOUND`) both builds
  the composition and states what the rendered file must contain.
- `docs/ab-workflow.md` — the manual recipe a person follows (fixture table, the
  14 clips with exact times, what must and must not appear) and what the automated
  run measures instead.
- `scripts/long_project_check.py` + `scripts/desktop_checks/long_project.py` — the
  long-timeline profile.

Bugs found by this task's validation and fixed, each with a regression test:

- **Fade envelopes were never applied to a clip that did not start at the render
  range start.** `render/graph.py` put `volume=…:eval=frame` on the clip's own
  sliced stream, before the leading silence that positions the clip inside the
  range, while the expression is written in time relative to the range start: the
  filter clock restarted at 0 and the anchors were never reached, so the export
  contained no fade at all. The envelope is now applied to the positioned stream.
  The earlier export test passed because its clip starts at 0, where both clocks
  agree. Measured in a rendered file by
  `test_fades_are_anchored_to_output_time_not_to_the_clip_local_stream`, pinned at
  graph level by `test_the_envelope_is_applied_to_the_positioned_contribution`, and
  `render/cache.SEMANTICS_VERSION` (now 2) stops an old preview being served as
  current. Found by the A/B check, which fades a clip placed at output 2 s.
- **Every playhead move re-stat-ed a file once per clip.**
  `MainWindow._missing_asset_names()` looped over clips and called `is_file()` for
  each, ~5 ms per move at 843 clips pointing at two files; it is a guard on the
  still-frame path, so it ran constantly. Now checked once per asset — playhead
  moves over a 2 h timeline measured **1191 ms per 200 moves before, 38 ms after**.
  Pinned by `test_a_long_timeline_checks_each_asset_once_not_once_per_clip`
  (counts the reads, not the timing).
- **Decoration scheduling consulted every clip in the project** — file check and
  plan arithmetic included — 9 ms per view change at 843 clips. The visible
  interval is now tested first, and the scan costs a comparison per hidden clip.
  Pinned by `test_only_clips_inside_the_view_are_consulted`.
- **The exporter's missing-source message named no file** (`Asset 6ed750c5… is
  missing on disk`): an asset id is not something a user can act on. It now names
  the recorded path, separately for "no path recorded" and "path not readable".
- Two harness defects in the A/B check itself: `Project.output_end` read as a
  property (it is a method, and a truthy bound method made the check pass while
  asserting nothing), and bin assets looked up by filename after a leg had renamed
  the file (now keyed by resolved path). A check that prints a constant detail on a
  PASS was also made honest.

Verified: `pytest -q` under `QT_QPA_PLATFORM=offscreen` — **537 passed, 0 failed,
0 skipped**, process exit 0; `ruff check .` clean. The three unattended desktop
checks each pass with **0 failed checks**: the window acceptance run (**133/133**),
`scripts/ab_workflow_check.py` (**197 checks**) and
`scripts/long_project_check.py`. Results are reported from single runs; where a
result is intermittent it is recorded as an open defect below rather than settled by
repetition.

Measured on this machine by one run of the long-project profile (2 h output,
843 clips, 3 tracks, mixed rates, 128×96, 25 fps):

- whole-timeline planning: 180 150 output frames, 721 picture + 482 sound items
  in **0.77 s**; a 10 s window in the middle of the same timeline in **5.1 ms**
  (3 items — the window does not pay for the timeline);
- a still frame decoded 1.5 h into the timeline: **0.37 s**;
- timeline repaint (median of 3 grabs within that run): **30.9 ms** with the whole
  2 h in view at 0.25 px/s, 16.3 ms at 1 px/s, **10.0 ms** at 50 px/s; **5 child
  widgets for 843 clips**, so the timeline is painted rather than populated;
- 154 clip hit-tests in 88.7 ms; 200 playhead moves in **36.9 ms**; an edit plus
  its undo across 843 clips in 17.6 ms;
- decoration caches after refreshing a long view: **146 kB held against an 8 MB
  budget**, 24 jobs queued against the cap of 32; 15 OS threads in the process
  (one decoration worker, the rest Qt's); peak RSS for the whole check
  **150 MiB**; at most **2** FFmpeg child processes during a render;
- a 290 kB project saved in 0.01 s and read back in 0.03 s with all 843 clips.

These are single-run figures; the checks assert budgets (a repaint under 500 ms, a
cache inside its budget), not these numbers, because timings depend on the machine
and what else is running on it.

What the A/B check measures (12 s output, 128×96, 24 fps, 4 tracks, 14 clips):
the colour on screen at every output frame against the intended cut (including
that covered material on a lower track is never seen and B's white frame never
appears); each tone's presence and level through Goertzel windows, including the
summed 550 Hz/770 Hz region measured as a 0.50/0.50 share, the half-gain clip at
ratio 0.499, the fade-out ramp measured **inside** the ramp, and silence in the
9-10 s gap; a 5-7 s interval rendered on its own and measured; a range outside the
composition refused; a cancelled render leaving no output and no `.partial.`
temporary; rendering refused after a source was moved, then accepted after
relinking; and the saved project reopened with the same composition.

Findings recorded while building this (all now covered by tests or documented):

- **The decoration layer was reporting failures nobody listened to.** It emits
  `complained` with the reason a clip has no picture or waveform, and the timeline
  writes that reason on a wide enough clip, but the window never connected the
  signal — while the documentation promised the status bar would say so. The
  connection and `_on_decoration_failure` now exist, tested by
  `test_a_decoration_failure_is_stated_not_hidden`.
- **OPEN DEFECT — the suite (and possibly the application) can abort during exit
  teardown.** Symptom: the run prints its result and *then* the process dies —
  `Segmentation fault`, `Bus error`, or `QThread: Destroyed while thread
  'clip-decorations' is still running` followed by a core dump — with the failure
  varying between runs of identical code. Cause: a window that is only **closed**
  stays alive, so its threads, media players and child widgets are destroyed while
  PySide6 is already running its own `destroyQCoreApplication()` cleanup at
  interpreter exit, and that ordering is not safe. It reproduced reliably in
  isolation (8 of 8 runs of `tests/test_viewer.py`, which holds three windows,
  while each test in it was clean alone: 0 of 8) and intermittently for the whole
  suite. What was changed: `MainWindow._shutdown_preview()` is idempotent and
  releases the media sources it held; `ClipDecorations.__del__` stops its thread
  even when the worker is unreachable; `run_app()` and the test session fixture now
  destroy the window (`deleteLater()` + processing events) while `QApplication` is
  still alive. Regression tests added for the two window-level guarantees
  (`test_shutting_a_window_down_twice_is_safe`,
  `test_a_layer_dropped_without_shutdown_does_not_take_the_process_with_it`).
  **Status: not proven eliminated.** It has not been reproduced since those
  changes, but a defect that depends on teardown order and object lifetimes cannot
  be declared fixed by the runs that happened to be performed, so it stays listed
  under *Outstanding environment checks* rather than being closed here. Repeating
  the suite was used only to reproduce and then bound the problem during diagnosis;
  it is **not** this project's verification method, and a task is now closed on a
  single reported run with its exit code. Records above that mention three
  consecutive runs describe what was done at the time, not the standing practice.
- A check that only reads back *structure* hides real defects: the fade bug lived
  behind a passing export test because that test's fixture happened to place the
  faded clip at the range start. Fixture tables should state expected **media**,
  and the measurement should be taken somewhere the bug could hide.
- The A/B fixtures need no attenuation for the mix warning to be meaningful:
  lavfi `sine` peaks ≈0.124 (−18 dBFS), so two overlapping contributions measured
  0.2488 after the plain sum — the plan's "can sum to 2.00× full scale" is a
  conservative gain bound, not observed clipping.
- `threading.enumerate()` cannot see Qt worker threads, so a thread-count check
  written against it asserts nothing; the long-project check counts the process's
  own `/proc/self/task` entries instead.
- The timeline's own chords (`+`/`-`, `Home`, arrows, `Esc`, Ctrl+wheel) are not
  `QAction` shortcuts, so a generated chord inventory needs them declared
  explicitly — and an old module docstring claiming `Ctrl+Plus` was wrong.

Limitations, stated rather than hidden:

- Decorations are validated by measurement (bucket counts, byte budgets, PTS
  placement, "nothing painted before delivery") and by the window check confirming
  something arrives; nobody has judged on screen whether a strip is *useful* at a
  given zoom ladder rung. That is a human judgement left for the review.
- The long-project profile uses two 2-minute synthetic sources reused across 843
  clips. It measures timeline scaling, not decode cost of a real feature-length
  file; still-frame seek latency on long-GOP real sources remains unmeasured.
- No full-length render was performed: full-duration *planning* and selected
  windows are what TASK_08 requires, and the chunked path is exercised in tests.
- The real-film smoke-test procedure (README, *Real-film smoke test*) has not been
  carried out: no copyrighted media is bundled and none was used. Perceptual
  checks — glitches at cuts, drift over minutes, how a preview or thumbnail looks
  and sounds — still need a human with real films.

## TASK_09 — Protect source media and enforce asset identity — DONE

Corrective milestone on the review of `0c6b8f1`. No new editing feature.

Findings confirmed against the code, and their fixes:

| Finding (baseline) | Fix |
| --- | --- |
| `RenderRequest.resolve_tools` accepted destination == an imported media path when `overwrite=True`, and `render()` ended in `staging.replace(destination)`; the UI's overwrite prompt claimed imported assets are never modified | `fs.protection.DestinationGuard` is checked in `resolve_tools()`, again before publication, and in `save_document`; a collision raises before anything is staged, regardless of `overwrite` |
| Save As accepted an arbitrary existing filename; `save_document` replaced it | `save_document(..., protected=..., consent=...)`; media collision refused, replacement needs a consent captured from that exact file |
| Existence checked, identity not: a different encode at the same path was used with stale bounds/indexes during a session | `fs.identity` states `ONLINE/MISSING/CHANGED/UNREADABLE`; `MainWindow._availability()` re-checks per asset and feeds media status, stills, stream previews, decorations and render preflight |
| Preflight/validation happened long before publication; an unconditional replace could clobber a file created meanwhile; a cancel after the last progress event could still publish | `fs.protection.publish` (no-clobber `os.link` / consented `os.replace`), `Consent.capture` at preflight re-checked immediately before publishing, and a cancellation check after verification |
| Identity/availability existed only in `project/storage.py`; the UI used `is_file()` | One policy in `video_editor/fs/`, asset helpers in `media/availability.py`, `storage.AssetState` now an alias of `fs.identity.AssetStatus` |

Design notes:

- New lowest package `video_editor/fs/` (stdlib only, imports nothing from this
  project) so `media`, `render`, `project` and `ui` share one rule set; the
  asset-typed layer is `media/availability.py`. Dependency edges added:
  `render → fs`, `project → fs`.
- Scope is honest in both directions: source checks cover the assets the
  rendered **range** reads (`RenderRequest.assets_used(plans)` →
  `check_sources`), destination protection covers **every** asset of the
  catalogue including unplaced/muted ones, plus the project file for renders.
- Sources are validated before decoding **and** again before publication, and
  per asset rather than per clip (the decoration layer likewise: the
  `Job` carries the *recorded* identity and re-checks it at decode time, so a
  file swapped after scheduling cannot be drawn as this project's media).
- Identity is size + mtime_ns — the same policy the project format has always
  written. A file rewritten with an identical size *and* mtime is not
  detectable and the docs say so; what is caught is a re-encode, a replacement
  or a truncated copy.
- Publication rules: nothing there → `os.link` (an `EEXIST` race is a refusal,
  not an overwrite); something there → `Consent.capture`d identity re-verified,
  else refuse; cache-owned destinations → `Consent.as_owner`, allowed only after
  `PreviewCache.owns()`. The previous file is never removed before a successful
  publish; staging leftovers are always cleaned.
- Wording changes users will see: MISSING now says "…is not there: it was moved,
  renamed or is on a volume that is not mounted", CHANGED says "…is not the file
  recorded here: it was X, it is now Y… may be a different encode". Tests that
  asserted the old sentences were updated deliberately.

Verified (single runs):

- `pytest -q` offscreen: **580 passed, 0 failed, 0 skipped**, exit 0
  (537 before this task: 43 new tests in `tests/test_media_protection.py` and
  `tests/test_ui_source_safety.py`, plus additions to the storage, exporter,
  export and framegrab test modules).
- `ruff check .`: clean.
- Unattended desktop checks after the change, each one run: window acceptance
  **133/133** (`exit=0`), two-edition A/B workflow **197 checks / 0 failed**
  (`exit=0`), long-project profile **21 checks / 0 failed** (`exit=0`).
  The A/B check initially failed on this task: it asserts that a moved-source
  refusal *names the state and the file*, and the new wording had dropped the
  state word. The exporter message now carries it
  (`… cannot be used before rendering [missing]: edition_b.mkv (…) is not
  there…`), which is what the check — and the user — needs.

Regression tests added (all with synthetic files, never user media):

- `tests/test_media_protection.py`: identity states (including a directory and
  an uninspectable path as UNREADABLE, and "no recorded identity claims
  nothing"); guard by path, relative alias, symlink, hard link and an
  *outgoing* symlinked directory; `publish` no-clobber, consented replacement,
  changed-after-consent refusal, vanished-then-recreated refusal, protected
  media refused despite consent, owner-authorised cache replacement, directory
  destination.
- `tests/test_render_exporter.py`: export onto an asset (overwrite=True) refused
  with the bytes unchanged and nothing staged; symlink/hard-link aliases;
  an **unused** asset still protected; a destination that appears mid-render
  survives byte-identical; a destination replaced after consent is refused; a
  source swapped mid-render is refused **before publication**; a cancel set
  after the last progress event publishes nothing. Range scoping: an offline
  asset the range never reads does not block the render.
- `tests/test_project_storage.py`: Save onto an asset refused (message names the
  asset, no temporary debris, media bytes unchanged), symlink/hard-link targets
  refused, a document created after the consent is never clobbered, and the two
  existing atomic-save tests now carry the consent they imply.
- `tests/test_framegrab.py`: a still of a replaced source is refused by the
  identity check (proven by decoding the *same bytes* when no identity is
  recorded).
- `tests/test_ui_source_safety.py` (window-level, prompts recorded not shown):
  CHANGED vs MISSING wording; 42 clips cost 3 availability checks; export onto
  imported media refused with the film byte-identical; hard/symlink alias and
  unused-asset protection; export onto the project's own `.vep` refused; Save As
  onto media refused with no debris; saving the project document still works;
  timeline pictures withheld for a changed asset and restored by a validated
  relink; relink refuses an unreadable target and changes nothing.

Deliberate deviations: none. Requirement 3's "do not stat every clip on every
paint" is enforced by the once-per-asset catalogue check and asserted by
`test_availability_is_checked_once_per_asset_not_per_clip`.

## TASK_10 — One transactional project document and usable linked editing — DONE

Corrective milestone on the review of `2205d37`. No new editing feature: the
editing was already there, but its *bookkeeping* was split in two, and that
split was observable as data risk (unsaved imports, half-restored relinks) and
as unusable linked editing (a split left all four halves glued together).

Findings confirmed against the code, and their fixes:

| Finding (baseline) | Fix |
| --- | --- |
| `ProjectSession` tracked only `core.Project`; `_project_document()` serialized a separate mutable `bin_controller.assets`, so an import with no clip changed the saved document without setting dirty (and Save / New / Close then behaved as if nothing had happened) | `project/document.py:EditorDocument` (composition **plus** catalogue) is the snapshot; `DocumentSession` holds the single saved baseline, so import, relink and placement all dirty, undo and autosave together |
| `_relink_asset_to` applied stream changes through history but patched the bin record separately: undo could pair old streams with the new file, and an identical-stream relink left the document un-dirtied while `test_a_relink_to_an_identical_file_changes_the_media_not_the_composition` accepted that | `RelinkDocumentCommand` = `ReplaceAssetCommand` + `RelinkAssetCommand` in one transaction; the test's *distinction* (composition unchanged, media moved) is preserved and its document-level expectation reversed: that relink is dirty and undone as one step |
| `_ensure_sources` applied several commands per gesture and `_find_free_track` added a track *before* placing the clip, so one drop made several history entries and left orphan tracks/registrations when the clip was refused | `PlaceStreamsCommand` + `StreamPlacement` + `TrackChoice` compute the whole result on a candidate and publish once; `group_placements()` is the single group-offset rule shared with `ImportGroupCommand` |
| `SplitClipCommand` gave every member's both halves the **same** `link_group`, so all four stayed linked: moving the right half moved the left, and a second split could be refused | left pieces keep the original id, all right pieces share one new id, a one-member group normalizes to unlinked; `validate_project` reports a lone group member |
| `validate_project` checked clip ids but not duplicate track ids / source keys, or whether a registered stream had a catalogue record; an invalid loaded state became a history baseline | `validate_project` extended; new `validate_document` cross-check; run at **load** (`storage.decode_payload`) and at **commit** (`EditHistory.check` hook) |

Design notes:

- History became generic (`EditHistory[S]`, `SnapshotCommand[S]`) instead of the
  document being special-cased, so the composition commands were reused as they
  are through `EditComposition(...)`. `ProjectSession` stays as a core-level
  convenience; the application never uses it.
- What stays **outside** the document, deliberately: playhead, zoom, scroll,
  selection, import jobs in flight, and disk availability. `check_asset` re-reads
  identity after undo/redo, so an undone relink cannot inherit the other file's
  state word.
- `AddAssetCommand` refuses a second record for a file the project already has —
  same asset id, same resolved path, or the same file under another name (TASK_09
  `same_file`), with wording that names the existing entry.
- Placement never *re-describes* a stream that is already registered: the
  project's own record wins, and the refusal tells the user to relink if the
  file really has changed. Re-description would silently invalidate bounds that
  existing clips were validated against.
- `TrackChoice.ALLOCATE` reuses the topmost free compatible lane (so a long film
  does not gain a track per clip); `FRESH` always adds one and is what whole-file
  import uses, so each imported edition keeps its own lanes.
- The bin is a projection (`MediaBinController.assets` reads the document,
  `MediaBin.sync` redraws). The "already imported" path index is resynced from
  the document on every accepted change (`MainWindow._sync_bin`), so undoing an
  import frees the file for import again and an undone relink re-claims the old
  path; `MediaBinController.reindex` (a manual patch of one entry) is gone, and
  `finalize_finished` no longer claims a path the document might refuse.
- Availability is recomputed at that same moment — one `stat` per asset, never
  per clip — so an undone relink cannot inherit the state word of the file it no
  longer points at. `_media_records` is refreshed by `_sync_bin`, which runs on
  install, import, relink, undo and redo.
- Recovery snapshots and autosave therefore cover the media list as well; a
  project with imported-but-unplaced media is offered as unsaved work at startup.
- **Schema v1 unchanged** — the format already held both halves, so no migration
  was needed. What changed is that the loader no longer accepts the two halves
  disagreeing, and a snapshot with registered streams but no asset record is
  refused instead of warned about.

Wording/behaviour changes users and tests will see (tests updated deliberately):

- A project file whose registered streams name an asset it does not record is
  **refused**: `streams 0, 1 of asset X are registered, but that asset is not in
  the media catalogue`. It used to load with a warning and produce clips that
  could never render.
- A command whose result would be structurally unsound is refused with
  `the edit would leave an invalid project: …` rather than becoming a baseline.
- History refuses a session whose document was replaced outside it:
  `Document changed outside history; undo unavailable`.
- Re-importing a file the project already has says `… is already imported as
  '…'` and records nothing.
- Duplicate-id/key breaks are named at the field level by the decoder
  (`tracks[4].id 'vtop' is duplicated in this file`) before the validator's
  broader `invalid project` report gets a chance.
- The old `test_linked_split_splits_all_members_atomic` expectation (one group
  for all four halves) was **rewritten**, because it asserted the bug.

Verified (single runs):

- `pytest -q` offscreen: **622 passed, 0 failed, 0 skipped**, exit 0 (580 before
  this task: 42 new tests in `tests/test_document_session.py`,
  `tests/test_ui_document_edits.py` and the new corrupt-file cases in
  `tests/test_project_storage.py`).
- `ruff check .`: clean.
- Unattended desktop checks, each one run after the change: window acceptance
  **134/134** (`exit=0`, one check added — see below), two-edition A/B workflow
  **197 checks / 0 failed** (`exit=0`), long-project profile **21 checks / 0
  failed** (`exit=0`).
- The cost of validating the whole document at commit is measured, not assumed:
  an edit plus its undo on the 843-clip / two-hour project stays at
  **25.6 ms** in the long-project check, and the whole-timeline plan, repaint and
  playhead budgets are unchanged.
- Two desktop-check expectations were **rewritten because they encoded the old
  behaviour**: `export_check.py` asserted that after undoing a relink the asset
  still pointed at the relinked file (exactly the half-restored state TASK_10
  removes). It now asserts the opposite and new — undo puts back the path *and*
  the recorded identity, which makes that asset **missing** again, and a
  *refused* relink leaves that untouched. One check was added for it.
- Nothing in the suite may open or block on a window. One new test did exactly
  that during development: the unsaved-work prompt answered "Save…" reached a
  real `QFileDialog.getSaveFileName` and hung the run. Diagnosed with a
  `SIGABRT` faulthandler dump of the wedged pytest, then fixed in the test
  fixture (see *Regression / acceptance tests added*): no modal path in these
  tests is left real.

Regression / acceptance tests added:

- `tests/test_document_session.py` (pure, no Qt, no FFmpeg): import with nothing
  placed is undoable, dirtying, stable-id-preserving work; a failed import
  dirties nothing; duplicate id / same path / hard-link alias refused; relink
  with **changed** stream metadata restores path + identity + bounds together on
  undo, and a path-only relink is dirty while `project` compares equal;
  `PlaceStreamsCommand` registers + allocates + creates as one entry and a
  refused placement leaves no registration, no lane, no history entry; a group
  keeps the container's own offsets (picture starting 2 s into the file);
  `ImportGroupCommand` and `group_placements` agree exactly;
  `CompositeDocumentCommand` publishes once or not at all (the seam a later
  assisted edit will use); `validate_document` names duplicate asset/track/
  source/clip ids, overlaps, out-of-bounds intervals and lone link-group
  members; offline media is **not** a structural problem.
- `tests/test_ui_document_edits.py` (window, generated media, prompts recorded):
  import-only project is dirty, Save-able, undone/redone, autosaved, prompted
  for at New, written by the "Save…" answer, and reopened as media; a garbage
  import does not dirty; one undo reverses a whole placement (clip + lane +
  registration) and redo restores the same clip id; a refused insert leaves no
  lane and no registration; a group placement is one action; a linked split
  leaves two independently movable and re-splittable pairs with three group
  ids, and undo restores the original one; a relink of identical streams is
  dirty, saves, reopens and undoes with path **and** recorded identity; saving
  after history navigation writes what history selected; a file naming
  unrecorded media, an unknown schema version and a non-JSON file are each
  refused without touching the open document.
- `tests/test_project_storage.py`: eight parametrised corrupt-file cases
  (duplicate track/clip/source, unknown stream, bounds beyond the stream,
  source before origin, kind mismatch, missing track), the unrecorded-catalogue
  refusal, and that offline-only media still loads.
- `tests/gui_helpers.py`: the supported way for a fixture to give a window a
  document (`install_document`, `media_record`, `offline_asset`, `import_file`),
  replacing the old "poke the session and append to the bin" pattern that could
  manufacture a state the application itself cannot reach.

Deliberate deviations: none. The task's "retain the current project schema if
possible" is met by keeping v1 unchanged; the task's "reuse `ImportGroupCommand`
where practical" is met by sharing its offset rule rather than routing the UI
through the older command, so the UI gets the candidate-then-publish behaviour.

## TASK_11 — Exact audio slicing, sample-rate conversion, fades and output validation — DONE

Corrective milestone on the review of `6a09c8b`. No new user feature: what audio
was asked for was already in the project, but the render path never *stated* the
slice it would produce, fades were applied at filter-frame resolution rather than
sample resolution, and a finished export was judged by nothing but its container
duration. Everything below is measured on decoded samples or counted packets, on
synthetic fixtures with distinct tones per second.

| Finding (baseline `6a09c8b`) | Fix |
| --- | --- |
| one `atrim …:end_sample=<output samples>` cap, written at a point in the chain where the samples being counted were whatever rate FFmpeg had negotiated there; the input was also read to the end of the file (`-ss` with no `-t`) | `AudioItem` states the whole contract (source slice in exact source seconds, `clip_samples`, range-clipped `start_sample`/`samples`/`pre_samples`, fades in output samples); the graph converts rate and layout **before** any counting, bounds the input with `-ss`/`-t`, and caps the read in **source** samples (`_source_sample_cap`) |
| fades as `volume=expr:eval=frame`, recomputed once per filter frame, and written in render-range time | `afade=t=in\|out:curve=tri:start_sample=…:nb_samples=N` over the whole clip, anchored to the clip rather than the range, then the range is cut |
| no bound on what a contribution could read, no statement of which axis a count belonged to | the chain order in `docs/architecture.md`; `_check_source_slice` refuses a clip reaching past a recorded stream end |
| an export was accepted when `format.duration` was close enough | frames counted per stream (`-count_packets`) and compared exactly with `plan.total_frames`; sound decoded and measured (`measure_decoded_span`) and compared with `plan.total_samples`; sample rate and channel count checked; a produced file with no packets or an undecodable stream is refused |
| a source whose container overstates its own end produced invented silence at the end of a clip and nobody said so | `RenderRequest.check_source_tails`: the last 2 s of the sound a clip presents up to its recorded end is decoded before encoding; more than `SOURCE_TAIL_TOLERANCE` (1/24 s) short → refused, less → warned, unmeasurable → warned |
| `normalise_layout("5.1(side)") == "5.1"` — side speakers renamed to rear speakers | `SIDE_LAYOUTS` refused with the reason; `plan._check_layout` warns where a remap is a guess (unrecorded layout or `…(side)` source into a surround output) |

Measured on FFmpeg 8.1.3 (all of it re-runnable through the tests named below):

- **Old fades did not exist at audio resolution.** A 40 ms fade-in of a constant
  0.5 signal came out as exactly two levels — `0.0`, then `0.5` from sample 4096
  — a worst per-sample error of **0.42656** of full scale against
  `core.model.envelope_gain`. With `afade` the same ramp is right to **< 1e-6**
  per sample (measured with `pcm_f32le`: `pcm_s16le` quantises at 1/65536, the
  same size as the error under test).
- **The old extent happened to be right, and that is not a pass.** The review
  described a 4 s item at 44.1→48 kHz requesting 192 000 *input* samples. That
  exact shape was rendered through `6a09c8b` (an 8 s stream of 2 s sections at
  330/660/990/1320 Hz, selection `[1, 5)`): the result held 330→660→990 in the
  right places, exactly 4 s, with silence after the clip rather than the 1320 Hz
  block, and 44.1→48, 96→44.1 and 48→48 two-second selections behaved the same.
  FFmpeg auto-inserts `aresample` before the old `atrim` when `aformat` is the
  only rate hint, so the promise belonged to negotiation rather than to the graph.
  It is now written out and locked by `test_a_selection_is_heard_throughout_its_whole_extent`
  (12 cases: 44.1/48/96 kHz sources × 44.1/48 kHz output × rates 1 and 25/24,
  measured at the first, middle and last quarter of the contribution) and
  `test_nothing_outside_the_selection_leaks_into_the_following_gap`.
- **Chunk and preview joins are exact for lossless material.** Rendering
  `[0, x)` + `[x, end)` and concatenating the decoded samples differs from one
  render by exactly **0.0** at rates 1, 25/24 and 24/25, including a boundary in
  the middle of a 1 s fade and inside a 12 s retimed clip (`CHUNK_JOIN_EXACT`).
- **`atrim` time options are broken here**: `end_time`/`end_duration` returned 0
  or 42 samples for a 2 s window, while `end_sample=100000` cut an AAC source at
  an exact sample mid-frame. Only whole-sample addressing is used.
- **A PTS rebase was needed after the range cut**: measured before it, exporting
  output `[2, 6)` produced a container of **3.0 s for a 2.0 s plan**, because
  `atrim` keeps the timestamps of what it keeps.
- **Packet sizes are a container detail, not a codec constant.** PCM in Matroska
  writes multi-sample blocks — 25 packets for a 96 000-sample render, i.e. 3840
  samples each — while AAC writes 1024. The first version of the output check multiplied packets by a per-preset
  constant and therefore refused a perfect lossless render ("25 samples at
  48 000 Hz, but the plan calls for 96 000"). Replaced by measurement: decoded
  audio is **+0 samples** for the lossless intermediate, **+256 samples at
  48 kHz** and **+888 at 44.1 kHz** for AAC (allowed slack `2048/48000` s).
  Picture frames are counted exactly: 50 packets for 50 planned frames at
  2 s/25 fps, single-pass and chunked.
- **A truncated file does admit nothing.** A Matroska rendered whole and then cut
  to 70 % of its bytes still reports `duration 6.000000` per stream and per
  format (Cues are written at the end, so the copy lost its index too); decoding
  `-ss 4 -t 2` from it yields **1.81167 s**. That is refused by
  `check_source_tails`; the same cut applied to our *own* MP4 output is refused by
  `verify_output`, even though `probe_duration` on it still returns a nonzero
  duration.

Verified (single runs, `QT_QPA_PLATFORM=offscreen`, nothing left to repeat):

- `pytest -q`: **672 passed, 0 failed, 0 skipped**, exit 0 in a single run
  (622 before this task: 50 new tests, 21 of them the real-FFmpeg measurements in
  `tests/test_render_audio_timing.py`). `ruff check .`: clean.
- Unattended desktop checks, each re-run after the final change: window
  acceptance **134/134**, two-edition A/B workflow **0 failed**, long-project
  profile **0 failed** (peak memory 150 MiB, 843-clip project 290 023 bytes).
- No new delivery codec was added: the delivery preset, the preview preset and
  the lossless chunk intermediate are the same three as before; only the graph
  and the checks around them changed.
- `render/cache.SEMANTICS_VERSION` is now `"3"`, so preview files made by the old
  chain are never reused.

Regression / acceptance tests added or deliberately rewritten:

- `tests/test_render_audio_timing.py` (new, 21 FFmpeg cases): selection heard
  through its whole extent across sample rates and clip rates; nothing outside
  the selection leaking into the following gap; fade-in and fade-out straight
  lines at sample resolution; a range cut *inside* a fade continuing the ramp
  (three rates); a retimed clip across a chunk boundary staying within the
  documented (measured zero) deviation while staying free of holes; two tracks
  from 44.1 kHz and 96 kHz streams, one of them a stream that starts at 1 s,
  placed at `1/3 s` with a 20 ms fade, `1/5` and `2/5` gains — checked for
  silence before the first clip, both tones present in the overlap at the gain
  ratio, the surviving tone after the other clip ends, and silence after the
  timeline.
- `tests/test_render_graph.py`: `test_fades_are_sample_accurate_afade_stages`
  and `test_fades_are_anchored_to_the_clip_then_cut_to_the_range` **replace**
  `test_fades_become_frame_evaluated_volume_envelopes` and
  `test_the_envelope_is_applied_to_the_positioned_contribution`, which asserted
  the frame-stepped envelope and its range-relative clock — those assertions
  encoded the defect. New: `test_the_source_slice_is_bounded_on_the_streams_own_axis`
  (input `-t`, source-sample cap before `aresample`, output count after),
  `test_a_range_inside_a_clip_still_builds_the_whole_clip` (including the PTS
  rebase), `test_side_speakers_are_not_treated_as_rear_speakers`.
- `tests/test_render_exporter.py`: the tail measurement (complete / 20 ms short /
  a whole second short / unmeasurable), that a clip which never reaches its
  stream's end pays no probe, `probe_source_material` against a real file, and
  the output-validation rules one at a time — missing stream, wrong geometry,
  missing frames, empty sound stream, wrong sample rate, wrong channel count,
  undecodable/short/long measured sound — plus the encoder-padding edges.
- `tests/test_render_export.py`: counted frames and measured samples for a range
  render, for a chunked render, and for the lossless intermediate (which must
  measure exactly the plan, the case packet arithmetic got wrong); a truncated
  output refused although it reports a duration; a source shorter than its own
  header refused rather than padded, and a clip that stops short of the damaged
  end rendering normally.
- `tests/test_output_settings.py`: the `5.1(side)` expectation **reversed** — the
  core still stores whatever the file said, the render layer refuses.
- `tests/media_fixtures.py:make_truncated_audio` — a lossless Matroska written
  whole and cut short, whose container still claims its full length.

Deliberate deviations: two, both recorded rather than quietly taken.

1. The task asks for an audio tolerance "derived from sample rate and codec" when
   validating the output. The first implementation did exactly that — a per-preset
   `audio_frame_samples` multiplied by counted packets — and it was wrong in
   practice: PCM in Matroska writes 3840-sample packets, so a perfect lossless
   render was refused as holding 25 samples. The check now *measures* the decoded
   sound and allows a time-based slack (`AUDIO_PADDING_SECONDS`) that the measured
   encoder padding (+256 samples at 48 kHz, +888 at 44.1 kHz) sits inside. A codec
   constant is not needed and the `audio_frame_samples` field was removed.
2. The task allows "bounded preroll/overlap" to keep tempo phase across chunks.
   Chosen instead: each range rebuilds the clip from that clip's own start and cuts
   the range afterwards, which is the "render contiguous tempo-processed slices
   once" option and measured as a zero difference. No preroll or overlap window
   exists, so there is nothing extra to tune; the remaining limit (lossy sources
   seek to their own boundaries) is listed under *Outstanding environment checks*.

Notes in the spirit of recording rather than hiding: the first output-validation
implementation in this very task used packet arithmetic and was replaced after
being measured wrong (PCM in Matroska), which is why both the measurement and its
regression test are in the suite; and the tail probe is a guard rather than a
proof — on a file whose index was itself destroyed the measured window can start
earlier than asked (the truncated Matroska above measured 1.81167 s rather than
the true 0.2 s), though still short enough to be refused.

## TASK_12 — Truthful frame inspection and a common picture sampling contract — IN PROGRESS, NOT ACCEPTED

Corrective milestone on the review of `0c6b8f1`. Work was **deliberately paused
with failures standing** so the reviewer can rule on two questions that change
what the composition promises (`TASK_12.md` §2 and §5). The full record — decisions,
measured tables, the defects found, the failing test names and the process lessons —
is `TASK_12.md` §1–§8; the binding decisions are in `docs/architecture.md`
▸ *Truthful frame inspection and one picture sampling contract (TASK_12)*.
Nothing in this section was verified through a real GUI window
(`QT_QPA_PLATFORM=offscreen` only).

| Finding (baseline `0c6b8f1`) | State at this commit |
| --- | --- |
| source stepping changed `source_pts` while keeping the old picture, and a cut was then trimmed with that timestamp | asynchronous source stepping (`ui/render_worker.SourceStepController`): newest request wins, rapid clicks accumulate, stale completions are discarded, `Mark In`/`Mark Out` are disabled while a frame is pending, and the cut uses the *measured* PTS of the picture on screen |
| every packet PTS probed synchronously on the GUI thread, a full `SourceFrame` list rebuilt per click | `media/frameindex.FrameIndex`: bounded 20 s windows, ≤4 probes per question, ≤6 cached windows, `bisect` neighbours; the cost is asserted in `tests/test_frame_index.py` (one question, one probe) |
| `extract_composition_still` inferred a decoded timestamp from neighbouring *packet* timestamps after a separate decode | `showinfo` taps the still decode itself, before resampling; `StillFrame.decoded_source_pts` is measured from that decode and snapped to the container's tick grid, `None` (with a stated reason) when it cannot be measured |
| a fresh one-frame plan at an unquantised time; `fps` restarted at each seek, so stills, short ranges and whole exports could disagree at the same output index | one ceil lattice (`Quantisation.frame_index == frame_count == ceil(t·rate)`) and one `PictureSampling` contract (grid, tick-aligned seek, read bound, head/tail holds); one `picture_filter_chain` shared by export, chunks, preview and stills; `SEMANTICS_VERSION` → `"4"` |
| the picture chain did not implement its own stated rule | `fps=round=up`, chosen by measurement (777/2410 frames wrong with the previous `round=near`, 26/2410 now); the 26 are a documented residual awaiting a reviewer decision |
| rounding could sample outside `[clip.source_in, clip.source_out)`; edge policy implicit | sampling clamped to the legal slice; where the lattice asks for material the slice lacks, `tpad` clones the nearest legal picture; a still at/after `project.output_end()` is refused by name while export ranges keep the TASK_05 explicit-black rule; `prepare_preview` refuses a zero-frame range |
| SAR reset without a display-aspect contract; anamorphic/interlaced/HDR unaddressed | **open**: the anamorphic case is now tested and fails (black borders from a 16:9-DAR source into a 16:9 output, cause not established); interlaced and HDR still have no refusal and no fixture |

Measured at this commit:

* `ruff check .` clean. `tests/test_frame_index.py` + `tests/test_framegrab.py`
  **26 passed in 20.62 s**. Render/cache/exporter scope
  (`test_render_graph`, `test_render_plan`, `test_render_preview`,
  `test_render_export`, `test_frame_index`, `test_render_cache`,
  `test_render_exporter`) **180 passed in 68.9 s**.
* `tests/test_picture_identity.py` (the acceptance suite for this task, new
  identity-colour fixtures: constant rate, B-frames, nonzero start, VFR,
  anamorphic): **9 passed / 10 failed in 186.13 s**. The failures are listed by
  name in `TASK_12.md` §6; one was investigated and is a genuine chain question,
  nine are undiagnosed.
* Three defects found and fixed while chasing a test that never finished — an
  infinite loop in `FrameIndex._search` that presented itself as a hung export,
  a `frames_in` lower bound of a whole second, and `ffprobe -read_intervals`
  answering past-the-end requests with the stream's tail
  (`tests/test_frame_index.py` is the regression net). Separately, the
  `showinfo` parser matched nothing at all, so **no** path had ever measured a
  source position, and no test had noticed because the expectations asserted the
  implementation's `None` (`TASK_12.md` §5).
* The anamorphic fixture itself was wrong — `-vf setsar=64:45` is parsed as two
  options, so the file carried SAR `45:1` / DAR `225:4` (measured with
  `ffprobe`). Corrected to `setsar=64/45`; the test still fails, now for a real
  reason (`TASK_12.md` §6).

Not run before this commit, and therefore unverified: the whole suite,
`scripts/desktop_checks/export_check.py`, `scripts/ab_workflow_check.py`,
`scripts/long_project_check.py`, and the acceptance item "measure navigation
responsiveness on a long synthetic source" beyond the bounded-cost tests above.
The GUI-thread `ffprobe`/`ffmpeg` calls in `_probe_asset` and
`extract_stream_window` are still there by design: cancellation, bounded
executors and shutdown hardening are TASK_13's scope.



## TASK_12b — Repair the picture acceptance tests and their fixtures — DIAGNOSTIC

Scope was the test and its fixtures only: no production file, no tolerance, no skip.
Baseline and final runs are `/tmp/opencode/12b_baseline.log` (9 passed / 10 failed,
169.15 s) and `/tmp/opencode/12b_final.log`; commands and numbers below.

- **Observation.** Eight of the ten red tests were red because the test itself was
  wrong (two CommandErrors from confusing source frame numbers with seconds, the
  missing `RenderPlan.total_frames` member, and five wrong bounds or arithmetic); the
  other two were genuine production defects, and repairing the still/export test
  exposed a third defect that its `AttributeError` had been hiding. The oracle was also
  circular: `planned()` asked `picture_sampling`/
  `Quantisation` what the plan intended and compared the render to that. Measured
  specifics: the fast-forward
  bound said "at least 40" for a composition of `ceil(125/4)` = 32 frames; the cut test
  placed clip B at source `[100, 102)` **seconds** of a 6 s stream (a `CommandError`,
  frames confused with seconds) and the gap test did the same with `[50, 51)` of a 4 s
  stream; the VFR test sliced `[3, 9)` of a stream whose last picture is at 8083/1000 s;
  the nonzero-origin test sliced `[3.5, 7.5)` where the pictures end at 6.5 s exclusive;
  the past-the-end test expected 60 frames, first picture 37, where `ceil(1.5·25) = 38`
  and `ceil(3·25) = 75` give 37 frames; the tail test asserted a forced last picture 49
  for a grid that stops at 1.95 s; `quant.total_frames` does not exist (`RenderPlan`).
  The fixture measured as containing **no B-frames** at all: `-qp 0 -bf 2` is x264
  lossless and its census is 33 I + 67 P. Two failures were genuine production defects
  (the range join, the display aspect), and repairing the still/export test exposed a
  third that its `AttributeError` had been hiding.
- **Change.** Expectation rebuilt as hand arithmetic in the test: integer global output
  frame index → `index/frame_rate` → the clip whose output interval covers it → source
  time → `bisect` over the fixture's measured decoded PTS. New
  `media_fixtures.picture_facts()` measures each fixture once per file (path+size+mtime):
  PTS from `best_effort_timestamp × time_base` as integers, `pict_type` census, SAR/DAR,
  declared vs decoded frame count, gaps, and the identity colour of every decoded frame,
  decoded with `-fps_mode passthrough` (without it `rawvideo`'s 25 fps duplicates a VFR
  stream's frames: 204 outputs for 150 pictures). `make_identity_bframes` now encodes
  `-crf 4 -g 5 -x264-params bframes=3:b-adapt=0`, measured 22 I + 40 B + 38 P with every
  identity read back exactly. Bounded residual checks (`allow_one_frame`, budgets 0 and
  24) are unchanged; the corrected slice for the origin case is `[7/2, 13/2)`, the VFR
  slice `[3, 8)`, the cut `[4, 6)` at output 2 s, the gap B `[2, 3)` at output 2 s, and
  the tail sampling case keeps its exact table `[40, 41, 42, 43, 45, 46, 47, 48]` with a
  new separate held-last-frame case `[48, 48, 49, 49]`.
- **Evidence.** Fixture facts as measured (FFmpeg/ffprobe 8.1.3): 150/100/50-frame
  fixtures decode to exactly that many pictures at 1/25 s with identities read back
  0..n-1 and frame 0 at (8,8,8) (not black); the offset fixture runs 5/2 … 323/50 s;
  the VFR fixture is 150 pictures with gaps 1/25, 83/1000, 21/250 and last picture
  8083/1000 s (container 8.166 s), and ffprobe still calls it 25/1 nominal *and*
  average; the anamorphic fixture is 720×576 SAR 64:45 DAR 16:9. With the independent
  oracle, the documented residual reproduces exactly: the 1000/1001 case differs from
  the arithmetic in 24 frames, all of them one picture, so the existing budget of 24 is
  confirmed rather than stretched, and every other tempo is 0. The per-frame `ffprobe`
  calls removed from the oracle took the file from 169 s to 26 s.
- **Remaining problem (red, and legitimately so).** `test_a_range_render_shows_exactly_
  the_frames_of_the_whole_render`: joined ranges and whole render disagree at output 87,
  111, 135 (98/123/148 against 99/124/149); the ranges agree with the arithmetic there.
  `test_a_still_is_the_frame_the_export_writes_at_that_index`: the last frame of the
  composition (output 119, sampling 143/24 s) raises `StillError` because the still
  path's one-frame window decodes nothing ("Output file is empty"), while the export
  writes that frame. `test_anamorphic_picture_is_placed_by_its_display_aspect`: the
  16:9-DAR source is scaled to 900×720 inside a 1280×720 output — the lit row spans
  columns 190..1089 — so the recorded display aspect is ignored. None of the three was
  touched here. Separately recorded and not fixed: this fixture's container claims an
  end at 9 s (`start_time` 2.5 plus `duration` 6.5) for 4 s of pictures — TASK_12e.
- **Status: DIAGNOSTIC.** `tests/test_picture_identity.py` 17 passed / 3 failed in
  26.35 s, `ruff check .` clean, `test_framegrab.py` + `test_frame_index.py` still
  26 passed in 19.48 s. No file under `src/` was modified.


## TASK_12c — Correct square-pixel display geometry — DONE

- **Observation.** Measured on FFmpeg 8.1.3 (the reviewer reproduced it on 7.1.5):
  `scale=W:H:force_original_aspect_ratio=decrease` on 720×576 SAR 64:45 yields
  900×720 **with SAR still 64:45**, and the chain's next stage `setsar=1` then
  destroyed the display shape, after which `pad` added 190-px side bars. So a
  16:9 display-aspect source lit only columns 190..1089 of a 1280×720 output, and
  a marker box painted 256×256 in the source decoded as **226×320** — the picture
  was being squashed, not merely bordered.
- **Change.** `render/graph.picture_scale_fit()` computes the fitted size from the
  **display** ratio:
  `scale=w='trunc(min(OW,OH*dar)/2)*2':h='trunc(min(OH,OW/dar)/2)*2',setsar=1,pad=OW:OH:(ow-iw)/2:(oh-ih)/2`.
  `dar` was verified to be the *input display* aspect by making `scale` print it as
  a coded width: 1777 for SAR 64:45, 1333 for SAR 16:15, 1000 when the container
  records no SAR. A `max(2, …)` clamp is in the expression because a computed `0`
  is not an error to `scale` — it means "use the input size" (measured: 3×2000 came
  back 3×720, shape inherited from the file); `plan_geometry` guarantees the output
  box is even and ≥16 px, so the clamp can never exceed the box.
  `render/cache.SEMANTICS_VERSION` `"4"` → `"5"` (the pixels change).
- **Evidence** (decoded pixels, composition still and lossless FFV1 render agree in
  every case; output `ffprobe` SAR 1:1 / DAR 16:9 throughout): 720×576 SAR 64:45 →
  fills 1280×720, all four corners content, marker 320×320; SAR 16:15 → 960×720 with
  symmetric 160-px bars; 1280×600 → 60 rows letterboxed; square-pixel 1280×720 fills;
  640×480 → 160-px bars; 8 exact-fit cases land on the box and never 2 px inside; a
  source carrying its **own** painted borders keeps them exactly ((16,16,16) inside
  the picture, (0,0,0) in the bars) with nothing cropped or stretched. Test-the-test:
  reverting only the chain line makes the new geometry tests fail (6 failed / 3
  passed), so they test the change and not each other.
- **Runs.** Baseline 3 failed / 77 passed (26.22 s) → final **2 failed / 85 passed**
  (40.44 s, re-run independently by the orchestrator) over
  `tests/test_picture_identity.py` + `test_render_graph.py` (28 passed) +
  `test_render_plan.py` (33 passed); `ruff check .` clean. Logs under
  `/tmp/opencode/m12c/`.
- **Remaining problem** (unchanged in kind and value, deliberately still red):
  `test_a_range_render_shows_exactly_the_frames_of_the_whole_render` — 3 of 136
  frames differ between joined ranges and the whole render: (87, 98 vs 99),
  (111, 123 vs 124), (135, 148 vs 149); and
  `test_a_still_is_the_frame_the_export_writes_at_that_index` — the last frame of a
  clip whose slice ends at the stream end raises `StillError`. These belong to
  TASK_12j and TASK_12k.
- **Logged, not fixed** (outside the assigned boundary):
  `media/thumbnails.py` builds its own `scale=box_w:box_h` with no SAR handling and
  no pad, so media-bin thumbnails of anamorphic sources are still stretched; at the
  `plan_geometry` floor (a 16-px-tall box) `scale` writes a compensating output SAR
  that `setsar=1` then flattens (~12 % vertical squash, pre-existing, reachable only
  at `geometry_scale` ≈ 1/80); and a clamped 2-px-wide strip sits one column apart
  between the RGB still and the 4:2:0 export, because 2 px leaves no room for
  symmetric chroma.



## TASK_12d — Playhead inspection separated from render-range quantisation — DONE

- **Observation.** One ceil helper was answering two different questions. The still
  path used the render-range boundary, so a playhead at `0.001` s asked for frame 1
  — footage 39 ms in the future. The UI then keyed its still cache with a **third**
  rounding, half-up `int((t·R·2+1)//2)` (`ui/main_window.py`), while frames are
  stored under the still's own `frame_index`: at a 0.3 s playhead the lookup asked
  for `(8, 0)` and the stored frame was `(7, 0)`, so every repeat inspection
  re-decoded, and a still decoded while a *later* playhead was showing could be
  served at an earlier one.
- **Change.** `Quantisation.render_range_frames(a, b)` (renamed from `frame_span`;
  half-open ceil bounds `ceil(a·R)` … `ceil(b·R)-1`) and a new
  `Quantisation.frame_at_playhead(t)` (`floor(t·R)`) in `render/plan.py`, each
  documented as answering exactly one question. `still_plan_for` takes
  `i = frame_at_playhead(t)` and builds the plan over that frame's **own lattice
  interval** `[i/R, (i+1)/R)`, refusing at/after the horizon and naming the last
  frame the composition has. `preview_range` computes its window in global frame
  indices capped to the horizon; `prepare_preview` refuses a subrange containing no
  frame, in index terms. `MainWindow._still_cache_index` now uses the floor helper,
  so lookup meets the store. `SEMANTICS_VERSION` `"5"` → `"6"`.
- **Evidence** (25 fps, measured): `[0, 0.01)` → index 0; `[0.001, 0.02)` → no
  frame; `[1.5, 3)` → 38…74 (37 frames); a cut at `1/3` s switches at index 9;
  playhead `0.039` → 0, `0.04` → 1, `0.079` in a `0.08` s composition → 1, and `0.08`
  refused: `no output frame at 0.080000s: the composition ends at 0.080000s; the last
  frame it has is at 0.040000s`. A clip under a higher enabled clip never wins a
  frame; disabled-track and gap cases are asserted index-exactly. The cache-key fix
  is tested both ways: revisiting a playhead does not re-request a decode
  (`generation` unchanged, `len(_stills) == 2`), and a 0.3 s playhead is never served
  the still keyed `(8, 0)`.
- **Runs.** `test_render_plan.py` 33 → 38, `test_render_preview.py` 15 → 18,
  `test_framegrab.py` 21 → 24 (80 passed), `test_render_cache.py` 30, and
  `test_render_audio_timing.py` + `test_render_graph.py` + `test_render_exporter.py`
  130 passed unchanged — the audio lattice and the picture chain did not move.
  Test-the-test: rewriting the still test's probe table to hand-worked floor
  expectations moved its failure from a rule disagreement
  (`expected frame 1, the viewer chose 0` at `t = 0.001` s) to the real defect at the
  twelfth probe (frame 119, sampling `143/24` = 5.958333 s, empty decode).
  Orchestrator re-runs: 110 passed (plan+preview+framegrab+cache) in 34.21 s; then
  `test_preview_gui.py` 3 failed / 22 passed, and picture-identity plus the three
  suites 104 passed / 2 failed in 75.94 s; `ruff` clean.
- **Remaining problem.** The two named picture failures stay red (TASK_12j,
  TASK_12k). Three GUI stepping failures in `tests/test_preview_gui.py` are
  pre-existing at `52f586e` (measured in an isolated worktree) and are assigned to
  TASK_12h: the test calls `MainWindow._timestamps_for`, which the window no longer
  has; a refused out-of-horizon playhead leaves `_source_shown` as `None`; and
  stepping is blocked with "This picture's source position could not be measured…".
  Also logged: `still_plan_for`'s refusal for an **empty** composition says "the last
  frame it has is at 0.000000s" when there is no frame at all (pre-existing wording).


## TASK_12e — Truthful source bounds for nonzero-origin video — DONE

- **Observation** (raw `ffprobe` JSON plus decoded frames, in `/tmp/opencode/m12e/`):
  the Matroska offset fixture reports `start_time 2.5`, container `duration 6.5` and a
  per-track `DURATION` tag of 6.5, and its last real picture starts at `323/50` with a
  duration of `1/25` — material ends at `13/2` s. The parser recorded **9 s**
  (`start + duration`), inventing 2.5 s. MP4 measures differently: `-itsoffset 2.5`
  gives stream `start_time 2.52` and an exact `duration 4.0`; the `-c copy` variant
  starts at exactly 2.5. Per-stream offsets (video and audio offset apart) give
  container 1.0/6.52 with tags 6.52 and 5.0 against measured `163/25` and `4999/1000`.
  A Matroska with 45 % of its tail bytes cut still claims 6.5 s while only 54 frames
  decode (to `233/50`) — which is why an upper bound must stay an upper bound.
- **Change.** `media/ffprobe.py` now treats a Matroska/WebM container `Duration` as an
  **end on the asset-local axis**, takes the tighter of container and per-track tag,
  and keeps `start_time + duration` for every other family (never tighter, no
  universal subtraction). Container-derived bounds are marked
  `bounds_are_conservative`; nothing promotes them to exact. `render/plan.py` refuses a
  clip reaching past a proven end (`Source out (15/2) exceeds the stream end (13/2)`;
  `Clip late needs 1.000000s past the end of asset off stream 0 …`), with **zero**
  tolerance for picture and TASK_11's one sample for audio. New
  `RenderRequest.check_picture_tails` probes a bounded window either side of the last
  sampled source time per stream and refuses a shortage larger than one output frame
  (`Asset cut stream 0 stops 1.800s before the 6.46s its clip v samples: the last
  picture that really decodes starts at 4.62s and runs to 4.66s…`), warns within it,
  warns when the tail cannot be measured, and refuses an empty window. No project
  format change; `SEMANTICS_VERSION` stays `"6"` because the preview key already
  carries the end and the conservative marker.
- **Evidence.** `[3.5, 6.5)` is legal and renders 75 frames with no tail warning;
  `[3.5, 7.5)` is refused before publication. Probe cost measured at 0.23–0.26 s per
  picture stream, 0.50 s for 60 s of 1280×720 H.264 with a 10 s GOP, and zero for a
  clip that never reaches its stream's end. An old project still loads with its loose
  9 s record and is refused at render time; relinking re-probes to `5/2 → 13/2`, and
  refuses if a clip still reads past the corrected end (`clip v uses source 5/2-9s,
  which the replacement does not contain…`).
- **Runs.** Per file: `test_probe_parse.py` 11 → 18, `test_probe_integration.py` 6 → 12,
  `test_importer.py` 4 → 5, `test_placement.py` 5 → 6, `test_core_commands.py` 62 → 63,
  `test_render_plan.py` 38 → 40, `test_render_export.py` 19 → 22,
  `test_render_exporter.py` 51 → 60, `test_render_audio_timing.py` 21 unchanged.
  Orchestrator re-run: **225 passed in 39.89 s** across probe/importer/placement/core/
  plan/exporter/audio-timing; `tests/test_picture_identity.py` unchanged at
  **2 failed / 24 passed** in 41.07 s; `ruff check .` clean. A full-suite run was made
  as extra evidence: **6 failed / 744 passed** in 320.2 s.
- **Remaining problem.** The two designated picture failures stay red (TASK_12j/12k),
  as do the three pre-existing GUI stepping failures (TASK_12h). The autosave GUI test
  is load-sensitive (see *Outstanding environment checks*). Matroska's per-track
  `DURATION` remains an upper bound (audio tag 5 s vs data 4.999 s; truncated file
  overstates by 1.84 s) — deliberately never promoted to exact.


## TASK_12f — A bounded index of actual decoded presentation timestamps — DONE

- **Observation.** The index answered from **packet** PTS, which is a seek hint.
  Measurement falsified the working assumption that a bounded probe covers what it
  was asked for: a read seeks to the preceding keyframe (preroll pictures come back),
  stops **1–2 pictures short of `to_time`** on B-frame/VFR material (0.08 s of
  reorder: asking `colour_steps` 1.92→3.92 s never reported 3.88 s), and can run
  **past** `to_time` across a short hole (`hole.mkv` asked 4–6 s also returned 7.92 s).
  Decisive: `longhole.mkv` (pictures 0–3.96 s, then 40.04–44 s) answered windows at
  10–12, 20–22 and 38–40 s with only 3.00–3.96 s — byte-identical in shape to a
  past-the-end read — so a tail-only read proves nothing about what lies beyond.
- **Change.** New `media/frameprobe.py` (`decoded_picture_times` requires both
  bounds; `PictureWindow.enumerated(claimed_end)`; `declared_end`; pure
  `picture_stamps`) and a rebuilt `media/frameindex.py` answering
  `FOUND / NO_PICTURE / UNKNOWN`. A span is trusted only to its last decoded picture;
  reads overlap by 1 s so spans join; absence is licensed only by the asset-axis
  origin (earlier) or a read asked past the container's declared end (later); the axis
  floor is the axis origin, not a declared stream start, so a misreporting container
  cannot buy a false absence. A search ends when a read enumerates no ground farther
  in the searched direction; windows are never re-asked; unanswered questions are
  cached (`MAX_UNKNOWN_ANSWERS = 32`, cleared by any read). `DEFAULT_WINDOW` is 10 s
  because width is nearly free (see cost below). Callers unchanged: `at_or_after`,
  `at_or_before`, `step`, `frames_in`, plus `answer_*` variants exposing the status.
- **Evidence.** Side-by-side old/new on CFR, real B-frame, VFR, offset, hole,
  truncated and 15-minute fixtures: **every returned timestamp identical**; only the
  claim changes (`longhole` at 20 s: old `None` in 1 probe → new `UNKNOWN` in 3).
  A span query: old 4 identical probes, 500 of 1500 stamps, no statement → new 4
  probes, 925 stamps, `complete=False`, coverage `(100.0, 136.96)`. Cost: decoded read
  0.23–0.29 s at any width 2–20 s on the 15-minute title (packet 0.20–0.22 s); cold
  single-frame question 0.36–0.44 s vs 0.33 s; warm 0.15–0.18 s vs 0.22 s; 2000
  sequential steps 1.53 s in 10 probes; export anchoring unchanged (3–4 reads, same
  first frames). New fixture `make_presentation_hole` (202 pictures, 44 s with a 36 s
  break) plus 15 new index tests and 8 probe tests driven by fake probe results
  (preroll, partial coverage, eviction, budget/termination, identity invalidation,
  distinct windows).
- **Runs.** `test_frame_index.py` 5 → 20 and `test_framegrab.py` 24 → 32:
  **52 passed in 45.98 s** (orchestrator re-run) / 52 in 48.9 s (task run); caller
  suites 5 failed / 128 passed, unchanged in name and value; full suite (unloaded)
  **5 failed / 768 passed, 0 skipped** in 333.2 s; `ruff check .` clean.
- **Remaining problem.** The five designated reds stand. `ui/` still consumes the
  plain `at_or_after`, which collapses `NO_PICTURE` and `UNKNOWN`, so the GUI words an
  unread gap as "no neighbouring source frame" — the distinction is available via
  `answer_at_or_after` and belongs to TASK_12h. Probe cancellation/timeout stays
  TASK_17.


## TASK_12g — Decode one source picture with its own actual timestamp — DONE

- **Observation.** `extract_source_still` built a **composition still plan** to read a
  source picture: an `fps`-resampled path with a pre-`fps` `showinfo` tap and a looked-up
  guess about which picture resampling had chosen. Measured, the reconstruction rule that
  path relied on is unsound: without `-copyts` the tap's stamps are relative to a rebase
  point FFmpeg reaches by rounding the seek text through microseconds *and* the stream
  grid, and the containers round differently — a 9-decimal seek text just under half a
  tick below a frame was stamped **one tick late** on the MP4 fixture (`time_base`
  1/12800) while Matroska (1/1000) stamped `0`.
- **Change.** Source inspection decodes directly: accurate input seek placed just above
  the wanted grid point (`+ min(1/100000, tick/4)`), **`-copyts`**, one frame
  (`-frames:v 1`, verified by exact byte count), geometry only
  (`picture_scale_fit` → `setsar=1` → pad → `format=rgb24`), no `fps`/`setpts`/`tpad`/
  `trim` and no composition horizon check — the stream's own bounds decide. A stamp is
  accepted only on the container tick grid and ≥ the request; otherwise the picture is
  returned and `decoded_source_pts` is `None` with `grid_exact=False`. Requests outside
  the stream are refused (`… is outside asset src stream 0 (which runs 2.500000s to
  6.500000s on its own axis)`); a request inside the bounds with no decodable material
  says so (`… holds no picture at or after 6.490000s asset-local …`); times past the
  clip's cut but inside the stream are allowed by design.
- **Evidence** (cross-checked against `media_fixtures.picture_facts`, never against the
  code under test): exact PTS and identity on the real-B-frame MP4 (including
  half-tick-past-a-frame: `25601/25600` → picture `26/25`), asset-local PTS on the
  offset stream (`151/50`, `323/50`), in-between landings on VFR (`4.002` → `4083/1000`),
  and **byte-identical** pictures for source PTS 47/25 through clips at rates 1, 25/24, 4
  and 1/4 (9216 bytes, same hash). Three crafted unreadable taps (no `showinfo` line,
  `pts: N/A`, no `config in time_base`) return the picture with an unknown position,
  while the normal fixture returns a verified PTS with byte-equal centre pixels.
  Measured semantics change to state plainly: for a source frame, `output_time`/
  `frame_index` label the playhead's floor frame **whether or not the composition covers
  it** — the old "no output frame at …" refusal no longer applies to source inspection.
- **Runs.** `tests/test_framegrab.py` 32 → **42 passed**;
  `tests/test_frame_index.py` 20 passed; the four caller files together
  **190 passed / 5 failed** (orchestrator re-run: framegrab+index+preview_gui
  84 passed / 3 failed in 84.13 s; picture-identity 2 failed / 24 passed in 41.84 s);
  `ruff check .` clean. The five failures are byte-identical to baseline.
- **Remaining problem.** The two designated picture failures stand (the composition
  still at a slice's last frame — `picture_preroll`/head-preroll at the stream end is the
  suspect — and the range-join lag at frames 87/111/135, TASK_12j/12k). The three
  `test_preview_gui.py` stepping failures are untouched (TASK_12h): the test calls
  `MainWindow._timestamps_for`, which no longer exists, and the UI's wording for an
  unmeasurable position differs from what the test expects.


## TASK_12h — Source stepping and marking made truthful in the UI — DONE

- **Observation.** The three tests red since `52f586e` were red for three different
  real reasons: one called `MainWindow._timestamps_for`, an internal of the
  composition-resampled stepping path TASK_12g had removed; one fabricated a
  `StillFrame` with no measured PTS — a state the viewer cannot be in any more — and
  expected wording the code never produced; one asserted on
  `win._source_shown.black` at a playhead past the composition, where the still is
  *refused* and nothing is shown, so the `AttributeError` was the honest symptom.
- **Change** (`ui/main_window.py`, `ui/render_worker.py`, `ui/viewer.py` only). One
  function answers "is this a visible, verified source frame?"
  (`_source_position_block`: shown, not a truthful black frame, names a clip/stream,
  position measured from that decode, on the container's own tick grid), and stepping,
  both cut buttons and the viewer's state derive from it, so they cannot disagree.
  Requested target and displayed picture are stored apart, so rapid steps accumulate in
  order and one decode serves them (a net-zero burst decodes nothing). Every delivery —
  picture **or** failure — passes `_accept_source_delivery` (this window's outstanding
  request, controller's newest, clip still exists with the same asset/stream, the
  asset's recorded path *and* identity unchanged). The worker now carries a structured
  `SourceStepProblem` instead of a string, so a proven absence and an unread stretch
  stay separate to the status bar. Mark Out is labelled
  "Set clip out (before this frame)" and the exclusion is stated after the edit.
- **Evidence.** `tests/test_preview_gui.py` 22 passed / **3 failed** → **30 passed / 0
  failed**; `tests/test_viewer.py` 5 → 6 (the viewer's own gating contract, no media).
  Deterministic mocking: a `ParkedStepWorker` on latches (never sleeps) plus a scripted
  index, exercising +3/−1 accumulation, a cancelled burst, a stale success, a stale
  failure, a **relinked** asset dropping a decode made against the old file, `UNKNOWN`
  neighbours, an unmeasured position and an off-grid position. Mark In / exclusive Mark
  Out are checked numerically through undo (the clip compares equal to its exact prior
  `Clip`; the other clip stays byte-identical), including the documented way to include
  a picture: step next, then mark out. Real fixtures: a 2× retimed VFR clip stepping by
  `83/1000` (not `1/25`, not one output frame), and `big_hole.mkv` where the next real
  picture is past the probe budget and the message says "not read far enough … not a
  proven end" while marking stays refused.
- **Runs.** Eight suites in fixed order: **2 failed / 181 passed in 177.10 s**
  (orchestrator-confirmed), the two failures being the designated picture defects with
  unchanged names and values; `ruff check .` clean.
- **Disclosed test change.** `test_source_stepping_stops_at_the_end_of_a_stream` was
  removed and replaced by
  `test_a_proven_edge_says_there_is_no_neighbouring_source_frame`, which walks both real
  edges with decoded pictures and asserts each wording *excludes* the other's phrase.
  The old test could only pass by lying. No other test name was dropped; nothing was
  skipped or loosened.
- **Remaining problem.** The two designated picture failures stand (TASK_12j/12k).
  A superseded source-step decode still runs to completion — only its delivery is gated
  — because real cancellation is TASK_13/17. `media/framegrab.py` still carries a stale
  docstring claim that the stepping worker passes `first_frame` (it no longer does);
  `media/**` was out of this task's boundary. The `grid_exact=False` refusal is
  exercised by a mocked decode only — no fixture in this repo produces an unreadable
  time base on a source step.


## TASK_12i — A slow independent picture reference and a phase diagnostic — DIAGNOSTIC

- **Observation.** The oracle for the TASK_12i contract (`t = i/R`,
  `wanted = source_in + (t - output_start) * rate`, last decoded PTS `<= wanted`
  inside `[source_in, source_out)`) was written from the decoded PTS list and exact
  `Fraction` arithmetic only, and production then disagrees with it in two distinct
  ways: the fixed-origin render itself answers some global indices one picture late,
  and an independently sought sub-range loses its **own last** output frame. Those
  are different defects, and the reviewer's two red tests mix them.
- **Change** (tests and a developer diagnostic only; nothing under `src/`).
  `tests/picture_reference.py`: the contract as a stdlib-only oracle
  (`Fraction`, `bisect`; it never imports `render/`, `picture_sampling`, the graph or
  FFmpeg's `fps`). `tests/test_picture_reference.py`: 16 hand-worked checks of the
  oracle itself (rate 4, 25/24, tail, held tail, fractional head, gap→black,
  nonzero origin, VFR, empty slice, overlap refusal, classification).
  `scripts/diagnostics/picture_phase.py`: measures *oracle* against **A** (one render
  from the clip origin, read by global index) and **B** (independently sought ranges
  mapped back to the same global indices by the oracle's own `ceil` lattice), over 18
  cases on 5 reused fixtures (rates 1, 25/24, 24/25, 1000/1001, 4, 1/4, fractional
  in/out, B-frame MP4, VFR, nonzero origin). `tests/test_picture_identity.py` is
  byte-identical (sha256 `26ebcfaa…`); no assertion was weakened or skipped.
- **Evidence.** 18 cases, 1,727 output frames, 108 renders: clean in 8 cases.
  A deviates in two: `cfr-1000_1001` at indices 1..24 of 126 and
  `cfr-frac-25_24` (`[1/3, 6)` at 25/24) at 15, 39, 63, 87, 111, 135 of 136 — always
  exactly one picture late, tie distance exactly `1/600` s. B loses its own last
  frame in 7 cases (13 frames, one short, or four short at rate 4) and answers
  10 mid-range frames differently from A. Plan frame counts equal the `ceil` lattice
  everywhere, and no head hold is wrong, so neither the lattice nor the edge rule is
  the problem. `--showinfo` measures the mechanism: `setpts=PTS/<rate>` carries the
  retiming only at the source tick (1000/1001 stamps enter `fps` as
  0, 40, …, 960, 1001 instead of 0.04004 steps; 25/24 as 39/38 ticks, not 38.4), and
  the seek is rounded (`-ss 0.334` for `1/3`, graph compensating with
  `start_time=-0.000640000`) — every range that shares that seek repeats the same
  wrong frames, ranges with other seeks do not.
- **Reviewer question answered.** A stable origin plus global-index trim removes the
  *inconsistency between ranges* (A equals the oracle at every index in 16 of 18
  cases) but does not remove the mismatches overall: at 87/111/135 of the reviewer's
  case the whole render writes 99/124/149 where the contract's arithmetic gives
  98/123/148, and the independently sought range gives 98/123/148. Ties were
  quantified by timestamp distance only (never by failed-frame counts): the 1000/1001
  deviations sit `1/25025`..`24/25025` s from a real frame timestamp (0.04-0.96 of
  that container's tick), the 25/24 ones `1/600` s (1.67 ticks). TASK_12i's permitted
  residual additionally requires all production paths to pick the **same** frame at a
  global index, which is measurably false at every deviating index, so no residual
  applies.
- **Runs.** `pytest -p no:randomly -o faulthandler_timeout=180
  tests/test_picture_reference.py -q` → 16 passed, exit 0. Same flags over
  `tests/test_picture_identity.py tests/test_framegrab.py tests/test_frame_index.py`
  → **2 failed / 86 passed in 99.87 s**, exit 1, the two failures unchanged in name
  and value from the `52f586e` baseline (2 failed / 24 passed in 40.22 s).
  `python scripts/diagnostics/picture_phase.py --case all --stills` → exit 0, 65.1 s.
  `ruff check .` clean. Logs under `/tmp/opencode/m12i/`.
- **Remaining problem (not fixed here, by design).** `render/plan.py` +
  `render/graph.py` express the grid at container-tick resolution and round the seek,
  which is TASK_12j; `media/framegrab.py` raises `StillError` at the composition's
  **last** frame in 9 of 89 still probes at exactly the index where the export writes
  a picture, which is TASK_12k. `black-vs-picture` was not exercised because every
  case here is one clip with no uncovered output region (the oracle covers gaps).
- **Orchestrator check.** Independent re-run: `ruff check .` clean;
  `tests/test_picture_reference.py tests/test_picture_identity.py
  tests/test_framegrab.py tests/test_frame_index.py` → **2 failed / 102 passed in
  109.20 s** (the two designated failures only). Re-ran
  `picture_phase.py --case cfr-frac-25_24 --brief` (exit 0, 2.8 s) and confirmed by
  hand arithmetic that the oracle, not the render, is right: at global index 87 with
  `source_in = 1/3`, rate 25/24, `wanted = 1/3 + 87/24 = 95/24 = 3.958333 s`, the real
  stamps either side are `98/25 = 3.92` and `99/25 = 3.96`, so the contract answer is
  **98** at a tie distance of `1/600 s`, while the fixed-origin render writes 99. Join
  mismatches reproduced at 63/87/111/135. `git status` plus mtimes confirm nothing under
  `src/` was touched (newest `src/` mtime 15:21:41, from TASK_12h; this task ran
  15:31–15:56) and `tests/test_picture_identity.py` is byte-identical
  (sha256 `26ebcfaa…`). This milestone wrote its own record above, which the
  orchestrator verified against the logs instead of rewriting.


## TASK_12j — Whole renders and independently sought ranges choose the same pictures — DONE

- **Observation.** TASK_12i's measurement located the defect in how the plan *states*
  the grid: `setpts=PTS/<decimal rate>` carries retiming only at the source tick (a
  1000/1001 slowdown fed `fps` the stamps 0, 40, …, 960, 1001 instead of 40.04 steps),
  and the grid phase rode on `fps=…:start_time=<decimal>`, which FFmpeg rounds onto the
  output grid — with a rounded seek (`-ss 0.334` for a `1/3` slice edge compensated by
  `start_time=-0.000640000`) that displaces the whole grid by ~1/1500 s, so every range
  sharing a seek was wrong at the same indices. Separately, sub-ranges lost their own
  **last** frame because input `-t` is packet-granular.
- **Change.** `render/plan.py` proves, per picture item, a filter `tick` on which every
  retimed timestamp and the grid's first sampling time are whole numbers
  (`picture_tick`, `MAX_PICTURE_TICK`/`MAX_PICTURE_TICK_PRODUCT` refusals) plus the grid
  `phase`; `render/graph.py` writes
  `settb=expr=1/25000, select='lt(pts,141650)', setpts=PTS*24/25+16,
  fps=round=up:fps=25/1:start_time=0, tpad=…stop, setpts=PTS-STARTPTS, …,
  trim=start_frame=0:end_frame=N`, so FFmpeg's choice becomes integer equality and the
  phase — not a rounded `start_time` — places the grid. Reviewer's join case before →
  after: whole `setpts=PTS/1.041666667,fps=…:start_time=-0.000640000` →
  `settb=expr=1/25000,select='lt(pts,141650)',setpts=PTS*24/25+16,…:start_time=0`;
  half 2 `…start_time=0.240640000` → `…select='lt(pts,77100)',setpts=PTS*24/25-6016`.
  The half-open slice is enforced on the **source axis** by `select`, off-grid edges
  written as ceiling integer ticks (`[1/3, 19/3)` → `lt(pts,149984)` — a fractional
  bound used to be emitted), the trim to the promised count happens after the pick, and
  head `tpad` is gone on this path. Read windows now come from decoded-picture evidence
  (`previous_frame` replaces the nominal preroll); the tick-size refusal measures the
  slice edge, not only the reading window. `SEMANTICS_VERSION` `"6" → "7"` because the
  pixels themselves change.
- **Evidence.** The referee (`scripts/diagnostics/picture_phase.py --case all --stills`,
  exit 0, ~65 s) goes from 8/18 defective to **18/18 `identical / identical / 0`, "no
  mismatch of any class"**, with all 89 still probes decoding; reviewer reproduction at
  global indices 87/111/135 now writes **98/123/148 on every path**. Two bugs were found
  by writing the new tests rather than loosening any: the fractional `select` bound above,
  and the tick refusal ignoring the slice edge. Ties: exact chain error **zero**
  (integer equality); the fallback's worst error is a whole container tick (1 ms on a
  1/1000 stream vs 40 ms spacing) and is *reported* via `RenderJob.inexact_picture` plus
  an export warning, never silently applied. Long-title cost measured on a 15 min
  1280×720 title (10 s GOPs, 539 MB): a 100-frame range at output `[858,862)` renders in
  **0.45–0.52 s** against **64.19 s** for the whole 21,600-frame composition; decode-only
  medians 0.218 s bare / 0.222 s nominal preroll / 0.216 s measured evidence / 0.148 s
  seek floor, and the cold frame-index probe costs 0.775 s (0.000 s warm) — the evidence
  window pays while scrubbing, not on one cold render.
- **Runs.** Orchestrator-confirmed: `ruff check .` clean;
  `tests/test_picture_identity.py` alone **27 passed in 42.47 s** (was 2 failed / 24
  passed — both designated defects now green, assertions untouched); render suites
  (plan, graph, preview, export, cache, audio_timing, exporter, ffmpeg_features)
  **241 passed in 106.02 s**; GUI (`test_preview_gui.py`, `test_viewer.py`,
  `test_export_gui.py`) **62 passed in 52.97 s**; picture+oracle+frame suites
  **105 passed**. Referee files are byte-identical to TASK_12i
  (sha256 `f44e22a8` / `78a92d51` / `14ae5db9`). One existing graph assertion was
  re-expressed from `start_time=<grid_start>` onto `settb`/phase/`start_time=0` — the
  same behavioural claim stated on the new mechanism, plus
  `job.inexact_picture == frozenset()`.
- **Remaining problem.** The still path's *claim* is still not honest: `_decode_picture`
  treats a `showinfo` stamp taken upstream of `fps` as identifying the returned picture,
  which TASK_12k's decision calls a false association (TASK_12k). `FrameIndex._answer`
  can answer `UNKNOWN` for a question its own read just answered when a span lies further
  left (repro in `/tmp/opencode/m12j/NOTES.md`), so an export item far from the clip edge
  uses nominal preroll — correct pictures, ~10 extra decoded pictures per item.
  Interactive GUI playback has still not been verified by hand, only offscreen tests.


## TASK_12k — Composition stills agree with the render and their source timestamp is honest — DONE

- **Observation.** `_decode_picture` read `showinfo` stamps from a tap placed *upstream*
  of `settb`/`select`/`setpts`/`fps` and claimed the greatest stamp ≤ the sampling instant
  identified the returned picture — a false association, with an unbounded
  `absolute[0]` fallback. Measured on a fine-timescale fixture (identity MP4 with
  `-video_track_timescale 50000000`, so `picture_tick` refuses an exact tick and the chain
  takes its container-quantised fallback): frame 26 shows picture #50 whose real PTS is
  `2`, reported `100000001/50000000` = 2.00000002 s; frame 124 shows #148 at `148/25`,
  reported `296000047/50000000` = 5.92000094 s. **Neither value is any picture's
  timestamp** — a truncated chain makes every such claim wrong by up to a container tick.
- **Change.** `_decoded_timestamps` is gone. `_shown_picture_stamp` carries each measured
  arrival through *this item's own chain arithmetic in integers*: tap counter × the base
  that tap reported (usable only if `-ss`'s text re-reads exactly as the requested seek) →
  arrival must be a whole number of `settb` ticks → must satisfy the same `select`
  bounds the graph writes → shown picture is the kept arrival with greatest `PTS`
  satisfying `PTS*b <= phase*a` (integer comparison, no rounding), or the first kept
  arrival for the documented head hold; the stamp is `sampling.seek_source + offset`,
  exactly. It returns `None` — and the UI says so — when there is no exact tick/phase,
  the item is unanchored, the tap's base contradicts the container grid, an arrival is
  not a whole tick, or nothing survives the slice. `source_pts` stays as the plan's
  **prediction**, now labelled that way: viewer reads `· source {pts}s measured` or
  `· source position not measured (asks for {prediction}s)`, and the window status names
  the missing exact filter tick rather than "container time base unreadable". Gaps decode
  nothing. `SEMANTICS_VERSION` stays `"7"` deliberately: that key fingerprints rendered
  preview media, stills are never written to it, and 12k changed only what a still *says*.
- **Evidence.** Orchestrator-confirmed: **168 passed in 191.15 s** across
  `test_picture_identity.py` (27 → 31), `test_framegrab.py` (42 → 44),
  `test_picture_reference.py` (16), `test_frame_index.py` (20), `test_render_preview.py`
  (19), `test_viewer.py` (6 → 7), `test_preview_gui.py` (30 → 31); `ruff check .` clean;
  the referee re-run unchanged at **18/18 `identical / identical / 0`, "no mismatch of any
  class"**, with 0 of its still probes newly reporting `None`. The reviewer's rate case is
  pinned: at 1000/1001, global index 2 shows picture **#26** and reports **`26/25`**
  (measured), prediction `1081/1001` — #26 is never labelled 1 s. Agreement was checked
  four independent ways at **57** global indices (still pixels → identity, lossless export
  at the same index, referee identity+PTS, the shown picture's own decoded PTS) plus
  **792** index-by-index probes over 8 diagnostic cases including 9 held frames: 0
  disagreements. Gap proof: a subprocess recorder that raises on any call records
  `calls == []` while the black still is returned with `clip_id is None`.
- **Two defect reports refuted during verification** (recorded so they are not chased
  again): harnesses claimed the new rule answers the *sampling instant* for held frames;
  an independent index-by-index audit of all 792 frames shows the reported PTS equals the
  **held picture's own** timestamp in every held row — the harnesses were comparing against
  `pick.pts`/the prediction column and one lacked a `still.frame_index == index` assertion.
  No production change resulted.
- **Named production defect, reproduced by the orchestrator, not fixed (out of boundary).**
  On a fine-timescale stream the picture chain takes its approximate fallback, and a
  **tail hold on a one-frame item emits nothing**: clip `[1, 6)` rate 1000/1001 on a
  50 MHz-timescale MP4 gives 126 output frames, the lossless export writes them ending at
  #149, but the still for global index 125 (sample instant `6001/1001`) raises
  `StillError`, and the same happens with the association rule stubbed out — so it is
  pre-existing at `5b55c87`, not caused by 12k. Cause: the fallback chain is
  `setpts=PTS/0.999000999,fps=round=up:fps=25/1:start_time=0.250250015,tpad=…stop=1`, the
  tap sees 6 arrivals all strictly before that decimal `start_time`, FFmpeg emits
  `frame=0`, so `tpad` has nothing to clone. Repro `/tmp/opencode/m12k/fine_tail.py`
  (orchestrator run: exit 0, export 126 frames ending `[147, 148, 149]`, still 123/124
  honestly `None`, still 125 raises). Needs a small corrective task: an exact tick for
  fine-timescale streams, or a fallback chain whose hold survives a one-frame item. No
  test in the suite covers it today.
- **Unverified observation, logged.** A source-stepping survey (a TASK_12n direction, not a
  task that exists) reported 0 of 300 single-frame seeks landing on a frame the index could
  name, with 14 of 300 in a gap, and stopped for review rather than proceeding. It is not
  corroborated by anything in the suite — TASK_12g's 44 `test_framegrab.py` tests cross-check
  real single-frame seeks against independently decoded PTS on B-frame, VFR and offset
  fixtures — so the survey's own mapping is suspect, but the claim is recorded because if it
  were real it would be serious. Nothing under `media/` was changed.


## TASK_12l — Interlaced and HDR input handled explicitly — DONE

- **Observation** (raw `ffprobe` JSON + `-show_frames`, in `/tmp/opencode/m12l/`): the
  pipeline claimed nothing about HDR/interlaced and would happily write SDR `yuv420p` code
  values. Measured fixture shapes disagree with each other, so a single rule is wrong:
  `-x264-params tff=1` leaves the container `field_order: progressive` while **8 decoded
  frames report `interlaced_frame=1`**; `-field_order tt` on a progressive file tags a file
  that is not field-coded (frames say progressive) and must *not* refuse; x265's
  `max-cll` appears only as frame side data `Content light level metadata`, while its MDCV
  record surfaces as the generic `H.26[45] User Data Unregistered SEI message` that **every**
  x264/x265 file carries; MPEG-2 records no `bits_per_raw_sample`; `-color_trc`/
  `-color_primaries` on the command line do not reach `ffprobe` for H.264/HEVC in
  MKV/MP4/TS (bitstream values do).
- **Change.** New `media/capability.py`: one shared preflight
  (`check_picture_capabilities`) called by `exporter.render` before any chunk or `.partial`
  exists, by `preview.prepare_preview` **before** `cache.lookup`, and by
  `extract_composition_still` after the explicit-black return, each re-raising as
  `RenderError`/`PreviewError`/`StillError` with byte-identical text. Refuses
  `field_order ∈ {tt,bb,tb,bt}` **or** any sampled picture reporting `interlaced_frame=1`;
  refuses `color_transfer` `smpte2084`/`arib-std-b67` and named HDR side data; warns and
  renders otherwise (10-bit alone is not HDR; absent/unspecified flags say what is unknown,
  and the colour warning is suppressed for ordinary 8-bit non-BT.2020 media). Import is
  never blocked; the bin shows the caveat on the stream row and dims a row **only** for a
  stream that cannot be placed at all. Preflight re-probes the file it will decode — schema
  v1 stores none of these fields, so absent must not mean "progressive SDR" — and v1 stays
  v1 with seven optional additive stream keys decoding as unknown. `SEMANTICS_VERSION` stays
  `"7"` (the refusal runs before any lookup).
- **Regression found by the acceptance run, then fixed at the cause.** The first pass broke
  six pre-existing tests (`test_missing_asset_is_reported_not_substituted`,
  `test_garbage_source_fails_rather_than_showing_something_else`, four
  `test_render_exporter.py` destination/mid-render/cancellation tests): a *probe failure* was
  being reported as a capability refusal, overwriting messages that already owned those cases
  (`'is not there'` → `'cannot read the picture format of gone.mkv: …'`). One change: a stream
  that cannot be measured at all is an unknown-format warning, never a refusal, so the
  existing resolution/decode error survives — pinned by
  `test_a_file_that_cannot_be_probed_keeps_the_error_that_names_the_problem`.
- **Evidence.** Orchestrator-confirmed: new suites **92 passed in 60.14 s** with **0 skips**
  (mpeg2video and libx265 both present, so all five refusal shapes ran end to end);
  probe/import/bin/storage/relink/settings **117 passed in 13.90 s**; picture group
  **168 passed in 213.81 s** (baseline 168 in 191.15 s — +24 s is the two bounded probes per
  range, and `test_stepping_the_playhead_does_not_probe_every_frame` pins one probe set per
  range, not per frame); render group **222 passed in 88.27 s**; the referee re-run is still
  **18/18 `identical / identical / 0`**; `ruff check .` clean. Refusal texts quoted in
  `docs/architecture.md`/the task report; three paths assert identical strings. Ordinary SDR
  (`identity.mkv`, `progressive_sdr.mkv`, `identity.mp4`) and anamorphic
  `identity_720x576.mkv` render unchanged (50 frames, no capability message, stills keep their
  measured PTS). Reopen proved both ways: a document written by this build round-trips every
  value, and one with all seven keys deleted reopens with each coming back unknown.
- **Not proven end to end (authoring limits, recorded not faked):** DOVI side data
  (`-dovi_profile 5` → `Unrecognized option`), HDR10+ (`-x265-params dhdr10-info=…` aborts
  inside x265, 0-byte output) and a *named* `Mastering display metadata` record — those rules
  are covered by name-level tests only. Also logged: `-read_intervals "<t>%+#N"` misbehaves on
  MPEG-PES/TS (start ignored or empty), so the head window is the trusted one there; and a
  BT.709-tagged fixture decodes with a limited→full-range lift (painted 8 read 26), so the
  renderability tests compare against the source file's own decoded centres (tolerance 8 codes,
  measured worst 3, against a 51-code step) — that reference cannot detect a systematic
  colour-range shift, and colour handling remains out of scope.
- **Untouched, proved by mtime and grep:** `render/graph.py` (16:52), `render/plan.py`
  (16:53), `render/cache.py` (16:23), `media/frameindex.py` (14:10),
  `media/frameprobe.py` (14:05), `core/**`, `ui/timeline.py`, and both referee files. Still
  open and not fixed here: the fine-timescale one-frame tail hold, and `FrameIndex._answer`'s
  false `UNKNOWN`.


## TASK_12m — Picture correctness checkpoint — DONE (run by the orchestrator, verification only)

Environment: FFmpeg/ffprobe 8.1.3, Python 3.14.10, pytest 9.2.0, `QT_QPA_PLATFORM=offscreen`,
`-p no:randomly -o faulthandler_timeout=180`. Logs and drivers under `/tmp/opencode/m12m/`
(`checkpoint.log` holds every step's exit code and wall time). No real film was used
anywhere: every number below comes from generated synthetic fixtures in disposable
scratch. No interactive desktop session was driven by hand — the desktop checks are
unattended offscreen runs, so "the GUI works" is still not a claim.

- **Full suite: 926 passed, 0 failed, 0 skipped in 506.31 s** (exit 0) — the first green
  full run in this roadmap. `ruff check .` clean. The two picture defects that group A
  started with are gone, and no test was skipped or weakened to get there. The
  load-sensitive autosave test
  (`tests/test_project_save_gui.py::test_autosave_snapshots_unsaved_work_and_restore_brings_it_back`)
  passed unloaded, consistent with the flake record above.
- **Unattended workflows.** `scripts/ab_workflow_check.py`: **exit 0, 35 s, "OK: 0 failed
  check(s)"**. `scripts/desktop_checks/export_check.py`: first run **aborted (exit 134)**
  with `AttributeError: 'MainWindow' object has no attribute '_timestamps_for'` at
  `export_check.py:259` — the script still called an internal that TASK_12g removed, the
  same stale coupling that made the deleted `test_preview_gui` test lie. Fixed **the script**
  (product untouched): the source-stepping check now compares the shown picture's **measured**
  PTS against the frames this check authored itself (100 pictures at exactly `1/25`, every
  one a keyframe) and waits for delivery with `source_step_controller.wait_idle` instead of a
  guessed 200 ms pump. Two runs then: **134/134 checks, exit 0** (60–61 s), e.g.
  `source stepping shows a real source picture and states its own measured PTS [26/25 ->
  27/25 grid_exact=True]`. The `QThread: Destroyed while thread is still running` abort
  disappeared with the exception, so it was a consequence, not the teardown defect.
- **Not green, and not papered over:** `scripts/long_project_check.py` is **flaky on one
  check** — "rendering uses a bounded number of FFmpeg processes" (`<= 2`): peak 3 →
  **exit 1** in the checkpoint run and in a rerun, peak 2 → **exit 0** in another (10–11 s
  each, everything else passing: 843 clips, 7206 s timeline, planning 0.76 s whole / 5.0 ms
  for a 250-frame window, repaint medians 9.4–30.1 ms, 152 MiB peak). Instrumenting the
  counter to record *which* children were alive
  (`/tmp/opencode/m12m/child_probe.py`) shows the concurrency is the encode overlapping
  incidental background work — a thumbnail `-ss … -t 25` decode, `ffmpeg -encoders`,
  a capability `-show_streams` probe, an index `-show_frames` probe — not one process per
  clip. So the bound is measuring scheduling luck, not a leak. Reviewer decision requested:
  restate it as "at most one encode process plus a bounded number of background probes", or
  assign it to group B/C. Not changed here, because loosening a threshold to fit a run is
  exactly what the roadmap forbids.
- **Measured long-source navigation** (15 min 25 fps 1280×720 H.264, 539 MB, container end
  900 s recorded as *conservative*, `time_base 1/1000`; probe window 10 s, ≤4 probes per
  question, ≤6 cached windows; `/tmp/opencode/m12m/navigation.py`): first question near the
  end **0.360 s in 1 probe**, the same question again **0.1 ms**, a neighbour question
  **0.1 ms**, **300 single-frame steps over 11.96 s of ground in 0.379 s (1.26 ms/step) using
  1 additional probe**. `preview_range` planning **0.05 ms**, preview-cache lookup **0.07 ms
  (miss) / 0.01 ms (hit)**; encoding that 250-frame window near the end takes **3.51 s**, so
  an uncached window is not scrub-speed — recorded as latency, not claimed as responsive.
  Total read for every question above: at most 2 index reads × 10 s = 20 s, **2.2% of the
  title**, which is the whole-film-scan check.
- **Named unresolved items handed to the next group.** (1) The fine-timescale picture-chain
  fallback cannot tail-hold a one-frame item — `StillError` where the export writes the
  picture (repro `/tmp/opencode/m12k/fine_tail.py`); no test covers it, no task owns it.
  (2) The long-project child-process bound above. (3) **A GUI-thread subprocess scan still
  exists**: `ui/viewer.py:217` calls `extract_stream_window(...)` synchronously with
  `duration = stream.end_time - origin`, i.e. potentially the whole stream, on the UI thread
  (TASK_12 §6 item 6, still TASK_13's scope — not solved, not claimed solved). (4)
  `FrameIndex._answer`'s false `UNKNOWN` (group C). (5) A validation gap found by my own
  measurement script: `render/plan.ceil_to_time_base(Fraction, float)` dies with
  `AttributeError: 'float' object has no attribute 'numerator'` rather than refusing a float
  by name — the domain rule is exact rationals, so the rejection should say so (a
  `core`/`render` validation item, found while calling it with `duration - 4 - 1/25`).
- **Disclosed changes made by the checkpoint itself:** `scripts/desktop_checks/export_check.py`
  only (two checks: the stale internal, the async wait). No `src/`, `tests/`, `docs/` or
  `README.md` file was changed by this task beyond the records being written now, and the
  picture referee (`tests/picture_reference.py`, `scripts/diagnostics/picture_phase.py`) is
  byte-identical to TASK_12i.


- **Post-checkpoint decisions (orchestrator, same day).** (1) `scripts/desktop_checks/long_project.py`'s
  child-process assertion was **restated rather than loosened**: it now counts children by
  role, asserting the invariant that mattered — **one process writes a render, however many
  clips it spans** (measured peak 1) — plus a small constant cap on the total (measured peak
  2) and naming the tools in the report. Matching is on the destination **stem**, because a
  render writes `name.<token>.partial.<ext>` before publishing. Three consecutive runs of the
  identical scenario: **exit 0 each** (10 s), no FAIL lines. The old `<= 2` total-count
  assertion measured scheduling luck (peak 2 or 3 depending on which background probe
  overlapped), which is why it flaked. (2) **TASK_12n.md was written** for the
  fine-timescale tail-hold defect and added to `TASK_PLAN.md` group A, marked as added at this
  checkpoint from the defect it named; `TASK_PLAN.md`'s next-assignment line now points at it.
  (3) Group A was committed as a single commit, at the reviewer's instruction.


## TASK_12n — Fine-timescale picture gets an exact tempo proved from real stamps, or a refusal by name — DONE

Environment as before (FFmpeg/ffprobe 8.1.3, Python 3.14.10, pytest 9.2.0, offscreen,
`-p no:randomly -o faulthandler_timeout=180`). The task's own runs and measurement
scripts are under `/tmp/opencode/m12c2/`; the orchestrator's verification runs,
independent harnesses and the `37f4f39` comparison worktree are under
`/tmp/opencode/m12n_verify/`. Synthetic fixtures only; no real film; no hand-driven
desktop session.

**Process fault, recorded so it is not repeated.** The first 12n worker was cut short by
a transport error *while editing*, and its `before_*` logs for `test_preview_gui.py` and
`test_picture_capability.py` were taken during that edit, so they show 15 and 6 failures
of the form `PictureSampling.__init__() got an unexpected keyword argument 'tick'` — its
own half-applied change, not pre-existing red. Those logs were discarded as baselines;
the checkpoint numbers measured at `37f4f39` (preview_gui 31, probe+picture capability
92, full suite 926) stand as the clean baseline, and every number below was taken with
all edits finished.

**What was actually wrong.** 12m's item (a) is the visible half: on a fine-timescale
stream the tail-hold *still* failed outright — at `37f4f39`, `clip [1, 6)` at tempo
`1000/1001` on a `1/50000000` MP4 gave `StillError: cannot decode the frame … at
asset-local 5.995005s` at output index 125, and indices 123/124 answered the right
picture with `decoded_source_pts=None, grid_exact=False` (re-measured by the
orchestrator, `/tmp/opencode/m12n_verify/still_vs_head.py`, with picture identity read
back from pixels of a colour-coded fixture and the expected pictures worked out by hand:
`n <= 25 + 1000k/1001`, so k=123/124/125 → 147/148/149). Cause: 12j's tick rule assumed a
tick exact for *any* stamp, which on that container at that tempo wants 50 000 000 000
filter ticks per second — past `MAX_PICTURE_TICK` and past an `AVRational` denominator —
so the item fell to the decimal fallback, whose first grid point can sit past the reach
of its own arrivals (`setpts=PTS/0.999000999,fps=…:start_time=0.250250015` → `frame=0`).
Separately, `-ss` carries whole microseconds: a seek to `5.74500498` s on that container
rebases by `5.745004` s (49 container ticks), so a grid could be placed where the chain's
own seek cannot put it, while on a `1/12800` container the same truncation is harmless.

**What is now true.** `plan.picture_tempo` returns one item's exact retiming or a reason
that names the numbers, and the chain spells it in one of two exact ways
(`PictureTempo`): the factored `setpts=PTS*1001/1000-3` when every stamp is already a
whole multiple of the tempo's numerator — the rule provable without knowing the stream's
stamps, which is why an ordinary stream's chain text does not move one character — or the
combined `setpts=(PTS*1001-3003)/1000`, where only the difference between a picture and a
grid point has to divide. The combined rule is tried **only** with measured stamps
(`window_pictures`, from the exporter's existing `FrameIndex.answer_frames_in` when the
span is complete, and from one bounded `frameprobe.decoded_picture_times` read in the
still path, and only when the stamp-free rule found nothing): on the fine MP4 the
stamp-free rule needs 5e10 ticks/s while every stamp of the window is already exact on
the container's own tick. The reading window is decided once, in
`plan.picture_read_window`, which also moves the seek back to a point both the microsecond
and container grids can state (`seek_is_reproducible`, `seek_granularity`) or marks the
item unanchored. Two shapes are now refused before any media is written, with the numbers
in the message: a window the stream leaves empty, and (approximate chain only) a first
grid point out of reach of its own arrivals — the threshold `5/8` sits inside a measured
transition, 7 pictures at `+0.62` output frame periods past the last arrival and **0** at
`+0.63`. `media/frameindex.py` is unchanged; `render/cache.SEMANTICS_VERSION` is `"8"`
because the fallback shifts stamps by up to a container tick, which decides a tie.

**Measured before/after** (the task's own logs; the composition sizes are theirs):
a 1 000 000-frame `25/1001` stream at tempo `1000/1001`, 25 fps out — whole render
640/640 against 639/640 at `37f4f39`; tail window `[625, 640)` 15/15 against 14/15;
one-frame range 1/1 against `RenderError: … expected 1 picture, produced 0`.

**Verification actually run at the finished tree** (`/tmp/opencode/m12n_verify/`):
full suite **940 passed, 0 failed, 0 skipped in 531.61 s** (exit 0), `ruff check .` clean;
focused groups — picture group
(identity 34 + framegrab 46 + frame index 20 + picture reference 16) **116 passed**,
render group **250 passed**, `test_preview_gui.py` + `test_viewer.py` **38 passed**,
probe+picture capability **92 passed**, probe/import/bin/storage **93 passed**, all exit 0.
`ruff check .` clean. The independent pixel referee
(`scripts/diagnostics/picture_phase.py --case all --stills`) answers **18/18
`identical / identical / 0`**, "no mismatch of any class in the cases that ran", with all
108 renders freshly written by that run. A separate harness written for this check
(`fine_independent.py`, colour-coded fixture, expectations from hand arithmetic, pictures
identified by nearest match against the *decoded source* palette) confirms **151/126-frame
wholes, one-frame and three-frame ranges, and the exact source picture each output frame
shows** through the exporter and through `build_job` with the application's lookups.
Chain text at `37f4f39` for the same item reads `fps=round=up:fps=25/1:start_time=0.035040020`;
through this code `…:start_time=0.035041001` — the 981 ns (49 container ticks) the seek
rule now removes.

**Evidence stated honestly.** The export short-write above was measured by the task's run
and is **not** reproduced by the orchestrator's own shapes: on the same fixture builder
(`make_identity_fine_timescale`, 150 pictures) at `37f4f39` the export wrote 151/1/126/1
pictures where the plan promised 151/1/126/1 for four shapes, none short. What the
orchestrator reproduced independently is the *still* failure and the missing
`decoded_source_pts` at 123/124 (above). Two of the three new picture-identity tests are
red at `37f4f39` because of the new API (`build_job() got an unexpected keyword argument
'window_pictures_of'`), not because of a media assertion, so their red-at-HEAD is
API-shaped; the third (`test_a_fine_timescale_stream_without_stream_measurements_is_reported_not_silent`)
fails at HEAD on its chain-text assertion for a real reason.
`tests/picture_reference.py` and `scripts/diagnostics/picture_phase.py` are unmodified.

**New tests (14).** `test_render_plan.py`: `test_a_fine_container_tick_is_proved_from_the_pictures_the_window_really_holds`
(factored tempo for 25/24, `1/50000000` tick and `offset=3003` for the fine case, refusal
quoting `1/50050000000` for 1001/1000), `test_the_ordinary_chain_text_does_not_move_when_the_window_is_measured`,
`test_a_fine_timescale_stream_is_retimed_by_one_exact_division`, `test_an_item_whose_grid_cannot_reach_a_picture_is_refused_by_name`,
`test_an_item_whose_reading_window_the_stream_leaves_empty_is_refused_by_name`,
`test_a_seek_is_moved_to_a_point_ffmpeg_puts_the_timestamps_back_on`,
`test_the_reading_window_of_a_fine_stream_starts_where_the_stamps_land`,
`test_the_approximate_chain_is_refused_only_past_its_measured_reach`.
`test_picture_identity.py`: `test_a_fine_timescale_stream_holds_its_last_picture`
(126 pictures, picks 25/25/26/147/148/149), `test_a_one_frame_range_of_a_fine_timescale_stream_writes_that_one_frame`,
`test_a_fine_timescale_stream_without_stream_measurements_is_reported_not_silent`.
`test_framegrab.py`: `test_the_last_frame_of_a_fine_timescale_stream_reports_the_picture_it_holds`,
`test_a_slice_the_stream_leaves_empty_is_refused_with_its_reason`.
Existing tests were re-expressed, not loosened: the honesty test
`test_a_stream_without_an_exact_tick_keeps_its_picture_and_loses_its_claim` moved from
tempo 25/24 (which this code now proves exact, so it no longer demonstrates the fallback)
to 1001/1000, and gained assertions that measuring the window does not rescue that tempo,
that the reason quotes `1/50050000000` and "expanded 1001 times", and a measured stamp
table (`419580` against an exact `419580.419580`) for why no position may be claimed.

**Left open, by name.** (1) For a fine-timescale stream the probe's declared end exceeds
the last measured picture by more than one container tick, so the plan believes `4/25` s
of material exists past the last picture and builds a tail hold, and `inexact_picture`
then says "sampled inexactly" when the chain sampled exactly. Fixing the metadata would
shorten the composition, so this needs one cross-group decision (probe/plan/UX) rather
than a local patch — put to the group C/D review. (2) The still and preview paths read a
window's picture stamps per item, which that run describes as roughly three times the
picture decode work per window; not measured by the orchestrator, and belongs to the
cache/scaling group (TASK_24–32) where the decode-cost budget lives.

---

## TASK_13 — One cancellable subprocess runner, no callers moved — DONE

**Single outcome:** `src/video_editor/media/process.py` (new, stdlib only) plus
`tests/test_process_runner.py` (new). No caller was migrated, no tracked file changed,
`SEMANTICS_VERSION` stays `"8"`. Group B starts here.

**What the primitive guarantees** (each rule in the module docstring is tied to a number
measured for it): synchronous `run_process(argv, *, timeout, cancel, on_stdout_line,
capture, terminate_grace)`, callable from a worker thread; a command string is refused by
name; exactly two drain threads per job (`process-runner-stdout`/`-stderr`), both joined
before it returns; the wait loop only ever polls, so cancellation and the deadline behave
identically for a silent child and for one flooding both pipes; capture bounded by default
to one measured pipe-full (65 536 bytes) per stream with `Keep.TAIL` (keep the end, count
drops — where an FFmpeg failure message lives) or `Keep.STRICT` (refuse the job as soon
as an explicit limit is passed, because half a JSON probe answer or half a JPEG is worse
than a named failure); a line callback sees complete lines in stream order plus a final
line with no newline, and a stream with no newline is cut at `MAX_LINE_BYTES` rather than
accumulated; termination is `SIGTERM` → bounded grace → `SIGKILL` → `wait()` on every
path; one terminal outcome — a `ProcessResult` (a non-zero exit is an answer) or one of
`ProcessCancelled(launched=…)`, `ProcessTimeout`, `ProcessOverflow`,
`ProcessCallbackFailed(callback_error=…)`, `ProcessLaunchError`, `ProcessError`. It owns
the process and its two pipes and nothing else: caller staging files and destinations stay
the caller's.

**Measured basis** (`/tmp/opencode/m13/pipe_probe.log`, `term_probe.log`): Linux pipe
capacity 65 536 bytes (`select.PIPE_BUF` is 4 096 and is not the limit); a plain child
dies 1.3 ms after `SIGTERM`, a handling child answers in 3.3 ms, a child that ignores
`SIGTERM` is killed 501.8 ms after the stop request with status `-9`.

**A flake found and fixed at the cause, not with a bigger number.** One of four early
runs reported `-15` where `-9` was expected. Diagnosis: the test fixture wrote its
pid-ready marker *before* installing `SIG_IGN`, so a cancellation landing in that
sub-millisecond window reached a child that still had `SIGTERM`'s default disposition.
Proven by a separate probe (`/tmp/opencode/m13/fixture_order_probe.log`): with that
ordering and the window widened to 60 ms the result is `-15` in 10/10 runs; with the
disposition installed first it is `-9` in 10/10 runs even with the widened window. The
fixture now installs the disposition before reporting its pid (the handler-based test had
the same latent race).

**Runs.** Task's own: 14 passed, exit 0, 2.43 s, 5 clean repeats plus 3 concurrent
runs, `ruff` clean (`/tmp/opencode/m13/`). Orchestrator's own repeats of the same file:
**14 passed in 2.08 / 2.09 / 2.03 s**, all exit 0, and `.venv/bin/ruff check .` clean
(`/tmp/opencode/m13_verify/test_run{1,2,3}.log`). Independently re-measured with a
harness written from outside the task's tests (`/tmp/opencode/m13_verify/independent_runner.py`,
log `independent.log`): cancel of a silent 30 s sleeper returns in **0.307 s** with
`-15`; **300 KiB on stderr with stdout idle** returns in 0.042 s, stdout intact
(`b"late\n"`), stderr kept at exactly 65 536 bytes with `dropped == 234 464` matching the
arithmetic; a `SIGTERM`-ignoring child with `terminate_grace=0.3` dies with `-9` after
**0.506 s**; a callback raising at the third line preserves `RuntimeError('independent
boom')` as `callback_error`, delivered exactly `line-0..line-2`, and reaped the child
(`-15`); cancellation before launch leaves no marker file and `launched is False`; a
0.4 s deadline on a silent child fires at **0.402 s** with `-15`; no `process-runner-*`
thread and no leftover child after any of it.

**Not run** (per the task's acceptance): the rest of the suite, the desktop checks and the
diagnostics — this task changes no behaviour any existing test observes. Note also that
`pytest-randomly` is **not installed** in this venv (verified with `find_spec`), so
`-p no:randomly` is a no-op guard and collection order is the default; the earlier
roadmap's "fixed order" runs were fixed order for that reason.

---

## TASK_14 — Media imports are bounded and genuinely cancellable — DONE

**Single outcome:** `media/importer.py` and the probe adapter `media/ffprobe.py` (plus one
minimal UI adjustment recorded as a scope exception) and a new `tests/test_import_jobs.py`.
`media/process.py` was not touched, nothing in `render/` changed, `SEMANTICS_VERSION`
stays `"8"`.

**Observation.** One daemon thread was started per file with no bound, `ImportJob.process`
was declared but **never assigned**, so `cancel()` had no process to kill: cancellation was
a flag the probe never looked at, and a hung `ffprobe` held a worker forever. The registry
also kept finished jobs, so `pending_count()` counted work that was already over.

**Change.** A bounded pool — **2 workers, 64 waiting, capacity 66** per controller, stated
in the module docstring — on a single `Condition` guarding the registry, the pending queue
and the hand-off, so "is this job still queued?" and "is it still in the queue?" cannot
disagree. Each job owns a `threading.Event` handed to `probe_file(..., cancel=...)` and
from there to `run_process`, which checks it before launching and while waiting and then
terminates and reaps the child. Queued cancellation removes the job and completes it in the
same call, so no probe starts for it. `import_paths` now returns
`ImportSubmission(accepted, refused)`: work past the capacity is **refused by name** and
starts nothing. `_finish` flips `settled` and pops the registry in one critical section and
returns True for exactly one caller, so a job emits exactly one completion (success,
failure or cancelled) and the registry always holds only live jobs. A worker catches every
fault of its job, and the loop catches anything else, so a broken job cannot strand the
queue. `wait(timeout)` is the owner's join. A probe result is published only when it is
complete **and** error-free; `Keep.STRICT` on a 4 MiB JSON ceiling means an over-long
ffprobe answer is refused by name rather than parsed from truncated bytes (measured: 5 501
bytes for this project's three-stream fixture, ~1.8 kB per stream).

**Scope exception, recorded.** `ui/media_bin.py::request_import` did
`zip(jobs, fresh, strict=True)`, which raises the moment a submission is refused, and left
an "already imported" placeholder for a path nothing would ever report on. 18 lines now
record accepted jobs by `job.path`, release the placeholders of refused paths and emit one
status message naming the limit. `tests/test_media_bin.py` + `tests/test_ui_document_edits.py`
cover the rest unchanged.

**Runs.** The task's own: 117 passed in 22.59 s (its canonical run over the final source),
its new file 16 passed ×5 with a 2.786–2.809 s spread, real-ffprobe cancel 0.005–0.018 s,
stubborn-child cancel 2.008–2.020 s (`/tmp/opencode/m14/`). Orchestrator's own affected
suites (`/tmp/opencode/m14_verify/`): imports (jobs, importer, imports, probe parse, probe
integration, probe capability, process runner) **117 passed / 23 s**; UI
(media bin, document edits, decorations ×2, smoke, editing, preview, viewer) **114 passed /
97 s**; picture group **116 passed / 157 s** — unchanged from TASK_12n, so the probe change
moved no picture behaviour; render group **250 passed / 121 s**; storage **93 passed /
15 s**; **full suite 970 passed / 0 failed / 0 skipped in 556.98 s** (exit 0); `ruff`
clean; the independent pixel referee **18/18 cases with "no mismatch of any class"**
(68.6 s).

**Independently re-measured** with a harness outside the task's tests
(`/tmp/opencode/m14_verify/independent_imports.py`): with `max_workers=1, max_pending=2` a
submission of 5 real files accepts exactly 3 and refuses exactly 2 **by name**; each
accepted job gets exactly one completion, all with metadata; the registry is empty after
`wait()`; `cancel()` on a finished or unknown id answers `False` and emits nothing;
cancelling a **queued** job completes in **0.0000 s** with no asset and releases its budget
so a later submission fits; `cancel_all()` + `wait()` drains with one completion per job;
and cancelling a **real ffprobe** child reading a FIFO nobody writes to answers `cancelled`
in **0.409 s** with `metadata is None` and no surviving `ffprobe` process — a case no
cancellation checked between stdout lines could ever pass.

**Checked rather than assumed.** `probe_file` now fails where `metadata` and `error` are
both set; that cannot happen — `parse_probe_output` returns either no metadata with an
error, or metadata with `error=None` — so the tightening is not a regression. Stdout is now
decoded UTF-8 with `replace` instead of the locale's `text=True` decoding; no test or
caller depends on the old behaviour.

**Left open, by name.** Worker threads are daemon threads parked on the queue: `wait()`
drains **jobs**, not threads, so every controller that has imported keeps 2 live
`import-worker-*` threads (a full-suite run accumulates ~23 across controllers; the
independent harness reproduced 2 with the names both called `import-worker-1`). Their
stop/join contract is TASK_22's. Ten of the eleven named `subprocess.run` sites are still
unmigrated (TASK_16/17/17c), as are the `thumbnails`/`waveform` kill-without-reap defects
(TASK_17b). `MediaBinController._job_paths` is now written and popped but never read
(removed in TASK_15).

---

## TASK_15 — Import completions belong to the project session that asked for them — DONE

**Single outcome:** `ui/media_bin.py` plus two wiring lines in `ui/main_window.py`, and one
new test file. Nothing in `media/`, `render/`, `core/` or `project/` changed;
`SEMANTICS_VERSION` stays `"8"`.

**Observation.** `MediaBinController.reset()` cancelled in-flight probes and cleared the
path index, but a completion already travelling through the Qt queued signal was still
applied to whatever document the window held when it landed: a probe started by the
previous project could add an asset to a project the user had just opened. Placeholders
also lived in the same `_by_path` map as asset truth, keyed only by resolved path, so an
old job's completion could unclaim a file the new session was busy probing.

**Change.** The bin owns a **session epoch** (`session_epoch`), incremented only by
`_advance_epoch()`, which is reached from `reset()` (New, Open, restore, recovery) and the
new `close_session()` (window release). `_advance_epoch` orders three steps deliberately:
move the epoch first, then release the placeholders of exactly the jobs that were live
(each placeholder is keyed by its **owning job id**, so a same-path reimport in the new
session cannot be unclaimed by the old job), and only then `importer.cancel_all()` —
because a queued job's cancellation completes synchronously on the cancelling thread, so
that completion must already be stale when it arrives. Every job is registered as
`_ImportReference(job_id, epoch, path)`, and `finalize_finished` — the only mutation
point — returns `(None, None, None)` unless the id is registered **in the current epoch**,
so unknown, cancelled and late jobs touch neither catalogue, history, dirty flag nor a
path. Asset truth now lives alone in `_by_path` (maintained only by `index()`), which is
why `index()` can no longer disturb a live probe, and the written-but-never-read
`_job_paths` bookkeeping recorded as dead in TASK_14 is gone. All of it is transient: a
saved project document names assets, never jobs.

**Wiring.** Two call sites only: `MainWindow._apply_fresh_session` (reached by New, Open
and recovery restore — the only places the session object is replaced after construction)
and `MainWindow._shutdown_preview` (inside the existing `_media_released` idempotence
guard, so a double close bumps once).

**Runs.** The task's own: acceptance 69 passed / 42.72 s, three repeats of the changed
surface at 40.57 / 40.83 / 40.38 s, GUI smoke + editing 30 passed, `ruff` clean
(`/tmp/opencode/m15/`). Orchestrator's own (`/tmp/opencode/m15_verify/`): the isolation and
import surface **74 passed / 45 s**; the whole GUI surface (smoke, editing, export,
preview, viewer, decorations ×2, help, source safety, project save) **173 passed / 122 s**;
storage **82 passed / 6 s**; **full suite 985 passed / 0 failed / 0 skipped in 541.29 s**
(exit 0); `scripts/ab_workflow_check.py` **exit 0, 39 s, "OK: 0 failed check(s)"**;
`scripts/desktop_checks/export_check.py` and `ruff` reported below.

**Independently re-measured** with a harness outside the task's tests
(`/tmp/opencode/m15_verify/independent_epoch.py`), delivering completions by calling the
bin's own worker callback so nothing sleeps or races: after `reset()`, an old session's
**success** returns `(None, None, None)` and leaves nothing registered; a same-path
reimport's placeholder is owned by the **new** job id and an old job's late success and
late cancellation leave it intact; the current job still publishes its asset and releases
only its own placeholder; a duplicate request while a probe is pending starts no new probe,
says why, and leaves the placeholder with the first job; an unknown id is inert; and after
`close_session()` a delivered success is dropped, the closed session's placeholders are
gone and its probes were cancelled.

**Left open, by name.** `job_started` is not epoch-gated: `MainWindow._refresh_import_actions`
still reacts to a superseded session's start/finish signals, which can only re-enable the
"Cancel imports" action from `pending_imports` — no document mutation — so it was left as
the minimal change and belongs with the worker stop/join contract (TASK_22), where the
parked `import-worker-*` threads are closed too. `finalize_finished` still compares the
literal `"cancelled"` rather than `media.ffprobe.CANCELLED_MESSAGE` (pre-existing). A
completion for a job that was never registered releases nothing, which with the real
controller cannot happen but a foreign controller could produce.

---

## TASK_16 — Every child the render job starts is the runner's child — DONE

**Single outcome:** `render/exporter.py` and the one subprocess helper in
`render/graph.py`, plus `tests/test_render_cancellation.py` (new, 19 tests). `media/`
(including `media/process.py`), `render/plan.py`, `render/cache.py`, `core/`, `project/`
and `ui/` are untouched, `SEMANTICS_VERSION` stays `"8"`, no filter-graph text, frame
count, duration rule or progress percentage moved.

**Observation (the defect, confirmed by reading HEAD).** `exporter._run` started
`subprocess.Popen` and read `proc.stdout` line by line with the cancel check *inside*
that loop, and only touched `proc.stderr` after `proc.wait()`. A child writing more than
one pipe-full (65 536 bytes, measured in TASK_13) to stderr while stdout stayed idle
blocked in `write` forever, and a cancel request was never looked at again; its
`_terminate` waited a fixed 5 s + 5 s whether or not the child was listening. Five other
sites in the same module used `subprocess.run(..., capture_output=True, timeout=…)` with
no token and no size bound, and `measure_decoded_span` buffered a whole film's
`-progress` text in one string to split it afterwards.

**Change.** All of it goes through `media/process.py::run_process` — encoder, lossless
chunk encodes, the concat join, `encoder_available`, the source-tail measurement
(`measure_decoded_span`), `probe_duration`, `probe_streams` and `verify_output` — with
`_check_cancelled` honouring the same token between the non-child steps. The token
travels from `render(..., cancel_event=)` into `resolve_tools`, `check_source_tails`,
`check_picture_tails`, the four graph measurement lookups (closed over the event by
`_StreamMeasureLookups`, since `build_job` takes plain callables), every encode and the
join, then the duration probe and the final verification. Cancelled and failed stay
different answers: `ProcessCancelled` → `RenderCancelled`, a non-zero exit, an
over-deadline child or a cut-short capture → `RenderError` carrying a bounded diagnostic
that now says how much stderr the limit dropped. A **cancelled probe never degrades into
"could not be measured"**: `measure_decoded_span` raises `RenderCancelled` rather than
returning `None`, and a cancelled capability probe is re-read as a cancellation instead of
answering "this FFmpeg cannot encode libx264". Stated bounds, all justified by measurement
in `/tmp/opencode/m16/speed_probe.log`: encode deadline `max(600 s, 20 s per output
second)` (whole exports measured at 0.17–0.66 s per output second), probe deadlines named
at their previous values, ffprobe JSON ceiling 4 MiB **STRICT** (measured 1 939 bytes for
the two-stream fixture), encoder list 256 KiB STRICT (measured real answer 14 085 bytes —
strict because a list missing its head would deny an encoder that exists). Progress stdout
is `stdout_limit=0`: drained, counted, streamed to the callback, never kept.
`_terminate` is gone; `import subprocess` is gone from both modules.

**Preserved, and re-quoted in the record:** `finally: for path in owned:
path.unlink(missing_ok=True)` with `staging` in `owned` from the start; the final
`if cancel_event.is_set(): raise RenderCancelled("render cancelled before the file was
written")` before `publish(...)`; consent, `check_destination()`, `check_sources(...,
when="after rendering")` and the identity guard unchanged; progress rule (an `out_time_us`
line ÷ planned duration, capped at 1.0) unchanged.

**The old defect, proved to have been real (negative control).**
`/tmp/opencode/m16/old_run_probe.py` runs `_run`/`_terminate` extracted verbatim from
`git show HEAD` against the same child shapes: stderr flood with stdout idle → *still
running after 15 s with the child alive*; a cancel set while a silent child runs →
*returned only after the child's own 20 s*; a flooding hang → *still running after 15 s*.
I re-ran the same shape in my own harness (`/tmp/opencode/m16_verify/independent_render.py`,
block A): the token really was set at 2 s (`TOKEN-SET` observed), the loop never reached
the check, and the child had still not finished its flood when killed.

**Runs.** The task's own (`/tmp/opencode/m16/`): new file 19 passed, three repeats at
9.30 / 9.21 / 9.11 s (spread 0.19 s), the six pre-existing render files 161 passed both
before (95.96 s) and after (97.37 s) its edits, combined 180 passed, affected consumers 99
passed, `ruff` clean. Orchestrator's own (`/tmp/opencode/m16_verify/`): new file **19
passed / 9 s**; render group **250 passed / 116 s** (unchanged); picture group **116
passed / 153 s** (unchanged from TASK_12n — no picture behaviour moved); capability 42;
export/preview GUI + viewer 64; storage 82; **full suite 1 004 passed / 0 failed / 0
skipped in 533.95 s** (exit 0); `ruff` clean; the independent pixel referee **18/18 cases,
"no mismatch of any class"**; `ab_workflow_check.py` exit 0; `desktop_checks/export_check.py`
**134/134**. `scripts/long_project_check.py` exits 1 on one check — see the next section:
it is a TASK_14 regression, reproduced identically without TASK_16.

**Independently re-measured** with a harness written outside the task's tests
(`/tmp/opencode/m16_verify/independent_render.py`, 25 checks, all OK, log
`independent.log`): 268 910 bytes of stderr with stdout idle are drained in **0.144 s**;
the failing child's message keeps my fixture's last stderr line *and* states
"203374 earlier stderr bytes were dropped", which is exactly my own byte count minus one
measured pipe-full (268 910 − 65 536); a silent 60 s sleeper is cancelled in **0.310 s**
and `os.kill(pid, 0)` says it is gone; a 1 s deadline on a hung child fires at **1.005 s**
and is reported as a failure with "did not finish within", not as a cancellation, child
reaped; a real FFmpeg encode cancelled from its first progress line leaves in **0.263 s**;
the progress rule still answers `[0.5, 1.0, 1.0]` for `out_time_us=2000000` and
`out_time_us=9000000` on a 4 s job (hand arithmetic: 2/4, then capped); a hanging
capability probe answers `False` in 0.308 s while real FFmpeg still reports libx264; a
token set before the call starts no child at all; a 4 249 589-byte probe answer is refused
naming the 4 194 304-byte limit rather than parsed; a cancelled tail measurement raises
`RenderCancelled` instead of returning `None`; and a **real export** cancelled mid-encode
publishes nothing, leaves no `*.partial.*`, and leaves a neighbouring user file
byte-identical — with no `process-runner-*` thread and no child process surviving.

**Regression named, not fixed here — introduced by TASK_14, not TASK_16.**
`scripts/long_project_check.py` now fails one check: "background work does not grow one
thread per clip or per track" measures 18 OS threads against the bound of 16, and names
the two extra as `import-worker-1`/`import-worker-2`. Proven by running the same script in
worktrees over two revisions with `PYTHONPATH` pointed at each (`/tmp/opencode/m16_verify/
long_pre14_{1,2}.log`, `long_at14_{1,2}.log`): at `6ae4e10` (before TASK_14) 16 OS threads,
**0** Python threads, exit 0, twice; at `ea87f54` (TASK_14) 18 with the two parked import
workers, exit 1, twice. The bound was calibrated when the import path spawned a thread per
file and none stayed; the bounded pool parks two per controller forever. The fix is the
stop/join (or idle-exit) contract that TASK_22 owns — **the bound has not been widened** to
make the check pass. Everything else in that script still passes (peak 1 render process,
peak 2 background children, cache and queue budgets, repaint latency).

**Left open, by name.** (1) `media/framegrab.py` (`tail_picture_extent` and its two
`-f rawvideo` reads), `media/frameindex.py`'s index probe and `media/capability.py` still
run their own `subprocess.run` children with no token: the exporter honours the token
*between* those calls, so a picture-tail decode already in flight is only bounded by its
own deadline (TASK_17). (2) `RenderRequest.stream_index_for` calls `resolve_tools()` per
stream, so `ffmpeg -encoders` runs once per stream — the background-children noise already
recorded; the peak stayed at 2 against a bound of 4. (3) A silent encode is bounded only
by a generous wall clock (20 s per output second); an idle-progress watchdog would need
runner support. (4) `docs/*` were not touched by the task, so this record and the
architecture note are the orchestrator's.

---

## TASK_17 — Still decodes, decoded-PTS probes and index reads are cancellable — DONE

**Single outcome:** `media/framegrab.py`, `media/frameindex.py`, `media/frameprobe.py`
(`cancel` pass-through only), the two render call sites that use them, the still and
source-step workers' active-token signalling, `tests/test_framegrab.py` (seam migration)
and `tests/test_picture_cancellation.py` (new, 17 tests). `media/process.py`,
`render/plan.py`, `render/cache.py`, `core/`, `project/`, `docs/*`, both referee files
untouched; **`SEMANTICS_VERSION` stays `"8"`**.

**Observation.** Every still decode, timing probe, packet-list read, decoded-window read
and tail read ran through `subprocess.run(capture_output=True)`: uncancellable, unbounded
(a whole film's `-show_packets` JSON or a rawvideo pipe kept in memory), and a superseded
viewer request or a worker stop could only wait out the deadline. The still and step
workers already dropped a superseded result by generation, but the child kept running.

**Change.** All of it is now `run_process`-owned. `cancel: threading.Event | None` threads
through `stream_timing`, `stream_position_of`, `source_frame_times`, `tail_picture_extent`,
`decoded_picture_times`, `declared_end`, `_picture_stream`, `_window_pictures`,
`_decode_picture`, `_source_picture_decode` and both public still entry points;
`FrameIndex` gains a `cancel` **and** `set_cancel`, because a cached index outlives the
request that built it, so the token must follow the question being asked now. New
`StillCancelled(StillError)`: it is re-raised before any path that turns `StillError` into
"not measured", a warning or a picture, so a stopped request never becomes a finding about
the media (the same class of bug TASK_16 removed in the render path). Capture is bounded
everywhere, each limit measured against real answers at `dd99cf2`
(`/tmp/opencode/m17/measure_head.log`): a raw still decode to **exactly
`width × height × 3`** of the already-validated geometry (9 216 B for 64×48, `Keep.STRICT`,
so an over-long answer is refused by name rather than truncated); window probe 8 MiB
(measured 19 998 B, 133 B/record); packet list 32 MiB (measured 50 B/record ≈ 670 000
records ≈ 7.5 h of picture); metadata 1 MiB (measured 1 041 B and 91 B); decode log 4 MiB
(measured at most 4 564 B). New honesty rule: **if the decode log reached its limit, the
decoded position is answered unknown** (`decoded_source_pts=None`, `grid_exact=False`)
rather than paired from a `showinfo` tap whose earliest lines were cut away. The workers
own an `_active` token set per request; a newer request or `stop()` sets it, and
`StillCancelled` delivers nothing — no picture and no failure message for what the user
caused themselves.

**A worker session died mid-task and that is recorded here.** Its last run left
`cancel=None,  # MUTATION-A` in two places in `framegrab.py` (the deliberate mutation
control it was running). I restored the file from its own
`/tmp/opencode/m17/framegrab.final.py` snapshot after checking that the **only** difference
was those two lines, confirmed zero mutation markers remained, and re-ran everything below
against the restored tree. Its logs are labelled by state: `new_run1.log` (17 passed,
clean) versus `mutation_a_nocancel.log` (**9 failed / 8 passed** with `cancel=cancel`
replaced by `cancel=None`) — the mutation control proves the new tests detect propagation,
not just presence.

**Picture behaviour did not move — proved value by value, not by green tests.** A capture
harness (`/tmp/opencode/m17/capture_picture.py`, a recorder with no expectations) was run
against a worktree at `dd99cf2` before any edit (framegrab md5-identical to that commit)
and again against the finished tree: **758 records each, 757 identical**; the single
differing line is the recorded module path
(`/tmp/opencode/m17/base-tree/src/...` vs `/home/p/.../src/...`). The records are decoded
picture identities (560), decoded PTSes, geometry, index answers (42), spans, tail answers,
packets, timing, declared ends, plan values and error texts over seven fixture kinds —
identity, offset, VFR, fine-timescale, B-frames, colour steps and whole-asset offset. The
recorder is deterministic: a second run at the same tree gave 758 records with **0**
differing. Independent arithmetic quoted rather than read from code: at `3/10 s` the
identity fixture's picture is frame 7 (`floor(0.3 × 25)`) and the first grid point at or
after it is `8/25`.

**Orchestrator's own runs** (`/tmp/opencode/m17_verify/`): new file **17 passed** three
times at 5.47 / 5.51 / 5.52 s; the focused set (cancellation, framegrab, frame index,
picture identity, viewer, preview GUI, render cancellation) **174 passed / 211 s**; picture
group **116 passed / 160 s** (unchanged); capability 42 / 53 s; render group **250 / 119 s**
(unchanged); GUI group 173 / 114 s; **full suite 1 021 passed / 0 failed / 0 skipped in
543.25 s** (exit 0); `ruff` clean; the independent pixel referee **18/18 cases identical /
identical / 0 with "no mismatch of any class"**; `ab_workflow_check.py` exit 0;
`desktop_checks/export_check.py` 134/134. `scripts/long_project_check.py` still exits 1 on
the one known TASK_14 thread-budget check (18 OS threads against 16) — same count as at
TASK_16, so this task neither caused nor fixed it.

**Independently re-measured** with a harness written outside the task's tests
(`/tmp/opencode/m17_verify/independent_picture.py`, **26 checks, 0 failures**): a hanging
still-decode child is cancelled in **0.266 s** and `os.kill(pid, 0)` says it is gone; 300 KB
of stderr with stdout idle drains in 0.286 s and, because that is *inside* the 4 MiB log
limit, the honest stamp survives (`decoded_source_pts = 9/25`, `grid_exact` true — the
correct answer, which is why my first expectation was wrong); a **5 MB** log is drained in
0.270 s, the picture is still 9 216 real bytes, and the position is `None` with
`grid_exact=False`; a child answering 4 MB of rawvideo for a 64×48 still is refused naming
"9216", not truncated; a cancelled index read raises `StillCancelled` in 0.264 s rather
than becoming `FrameIndexError` or `UNKNOWN`, and a token that is already set stops the
needed read in **0.0003 s** with `probes` unchanged; a question that needs no read is still
answered correctly with the token set (148/25 = 5.92, the first `k/25 ≥ 5.9`) — cancellation
stops reads, never proved answers; the same token stops a packet-list read; and a **real
export** cancelled while its probes run leaves `RenderCancelled`, no destination and no
`*.partial.*`, with no runner thread or child process surviving.

**Left open, by name.** (1) The capability preflight a still goes through is still
`media/capability.py`'s own `subprocess.run`, so a still's cancellation lands before that
preflight, not inside it (named in the entry point's own docstring; TASK_17b/17c territory).
(2) `thumbnails.py`, `waveform.py`, `extractor.py` and `diagnostics.py` remain unmigrated
(TASK_17b/17c). (3) `StillWorker._supersede()`/`SourceStepWorker._supersede()` read
`self._active` from the GUI thread while only the worker thread assigns it, so a supersede
racing a token assignment may miss that one decode; the controller's generation check still
decides what reaches the screen, so the worst case is one wasted decode, never a wrong
picture. (4) Worker threads still have no stop/join contract (TASK_22), which is also what
keeps `long_project_check.py`'s thread check red.

---

## Outstanding environment checks

- **Group A (TASK_12b–12n) is complete; TASK_12 as such remains unaccepted.** The picture
  baseline is now green: `tests/test_picture_identity.py` 34 passed, the whole suite
  **940 passed / 0 failed / 0 skipped in 531.61 s**, `ruff` clean, and the two unattended
  workflows that were green stayed green (`ab_workflow_check.py` exit 0;
  `desktop_checks/export_check.py` 134/134 after the *script* was updated off a removed
  internal). `long_project_check.py` is flaky on its child-process bound — see the item at
  the end of this list. Interlaced and HDR now have explicit refusals and fixtures
  (TASK_12l). See `TASK_12.md` §6–§8 for the historical record of what the unaccepted
  commit left behind.
- **Two named follow-ups from TASK_12n, deliberately not fixed there.** (1) On a
  fine-timescale stream the probe's declared end can exceed the last measured picture by
  more than one container tick (measured: 150 pictures ending at `149/25` in a stream
  declared `6` s), so the plan builds a tail hold over material that is not there and
  then reports that item as sampled inexactly. Correcting the metadata shortens the
  composition, so this needs one decision across probe, plan and UI rather than a local
  patch — raised at the group C/D review. (2) The still and preview paths read a reading
  window's picture stamps per item, described in that task's report as roughly three
  times the picture decode work per window; not re-measured by the orchestrator, and it
  belongs with the cache/scaling budget of TASK_24–32.
- **Named subprocess defects that TASK_13's runner now exists to remove** (confirmed by
  reading the code, deliberately not fixed in TASK_13 because it was primitive-only).
  Migrated so far: `media/ffprobe.py` (TASK_14); all of `render/exporter.py` plus
  `render/graph.py::encoder_available` (TASK_16), which removed the worst of them —
  `exporter._run` could only notice cancellation **inside** `for line in proc.stdout` and
  never read `proc.stderr` until after `proc.wait()`, so a child whose stderr exceeded
  65 536 bytes while stdout was idle blocked forever and a cancel request was never seen;
  and `media/framegrab.py`, `media/frameindex.py` and `media/frameprobe.py` (TASK_17),
  which also bounded the rawvideo still decodes to exactly `width × height × 3` and made a
  cancelled picture read an answer of its own.
  Still standing: (1) `media/thumbnails.py::_stop` and `media/waveform.py::_stop` do
  `terminate(); wait(timeout=2)` then `kill()` with **no `wait()` after `kill()`**, so the
  child is reaped only when the `Popen` object is collected (TASK_17b). (2) Three
  `subprocess.run(..., capture_output=True, timeout=…)` sites — `media/extractor.py:80`,
  `media/capability.py:293` (the picture-capability preflight a still and an export both go
  through, so a cancellation there is still noticed only between steps) and
  `diagnostics.py:68` — have no cancellation and no size bound (TASK_17b, TASK_17c).
- **Open: the import pool's parked threads break the long-project thread budget.**
  `scripts/long_project_check.py` fails "background work does not grow one thread per clip
  or per track": 18 OS threads against a bound of 16, the extra two named
  `import-worker-1`/`import-worker-2`. Measured in worktrees with `PYTHONPATH` per
  revision: `6ae4e10` (before TASK_14) 16 threads / 0 Python threads / exit 0 twice;
  `ea87f54` (TASK_14) 18 threads / exit 1 twice; identical at TASK_16's HEAD, so the
  render migration neither caused nor hides it. The bound predates the bounded pool, which
  parks two daemon threads per controller forever (a full-suite run accumulates ~23 across
  controllers). TASK_22 owns the stop/join or idle-exit contract; the bound has **not**
  been widened to make the check pass.
  `media/ffprobe.py` was the first site moved onto the runner (TASK_14).
- **Resolved in TASK_12h: the three GUI stepping tests that were red at `52f586e`.**
  `tests/test_preview_gui.py` is now green (30 passed). They were: the stale
  `MainWindow._timestamps_for` call, a wording expectation for an unmeasurable position
  that could only be met by lying, and an `AttributeError` on a refused out-of-horizon
  playhead. Recorded because the unaccepted TASK_12 commit shipped them red — it never
  ran the full suite — and they sat unnoticed for six milestones.
- **Open: one autosave GUI test is load-sensitive.**
  `tests/test_project_save_gui.py::test_autosave_snapshots_unsaved_work_and_restore_brings_it_back`
  failed in both full-suite runs the 12e worker made (`assert 10 == 1` snapshots after a
  2 s pump at a 0.05 s interval) and passed when the file is run alone — measured
  27 passed in 24.80 s in this tree and 27 passed in 24.33 s in a clean worktree at
  `52f586e`. It has also failed under concurrent benchmark load in a TASK_12f run
  (`assert 4 == 1`) and passed in the unloaded full suite (5 failed / 768 passed), so it
  is a timing-dependent test, not a regression from 12b–12f, but it will pollute every
  future full-suite checkpoint. It needs a fake clock or an explicit pump rather than a
  longer sleep; that belongs with TASK_12m (the first checkpoint that runs the whole
  suite) or TASK_35's CI work.
- **Open: `scripts/long_project_check.py` has a brittle resource bound.** "rendering uses a
  bounded number of FFmpeg processes" asserts `peak_children <= 2`; measured across three
  runs of the identical scenario: peak 3 (fail), peak 2 (pass), peak 3 (fail), everything
  else passing each time. Instrumenting the counter
  (`/tmp/opencode/m12m/child_probe.py`) shows the peak is the encode overlapping *incidental*
  background children — a thumbnail decode, `ffmpeg -encoders`, a capability
  `-show_streams` probe, an index `-show_frames` probe — so it measures scheduling luck, not
  a process leak. Needs restating as "one encode process at a time plus a bounded number of
  background probes", or folding into the group B/C lifecycle work. Left as-is at the
  checkpoint rather than widened to fit a run. Distinct from the current failure in the same
  script, which is the import pool's two parked threads against a 16-thread budget (see the
  thread-budget item above); at TASK_16 every process check in this script passes.
- **Open: a GUI-thread subprocess scan.** `ui/viewer.py` builds a stream preview with a
  synchronous `extract_stream_window(...)` sized to the whole stream (`end - origin`) on the
  UI thread. Named in TASK_12 §6 item 6, still unresolved, assigned to the lifecycle group
  (TASK_13) — measured navigation costs above are for the *worker-backed* paths only.
- **Open: a float reaching the rational core dies late.**
  `render/plan.ceil_to_time_base(Fraction, float)` raises
  `AttributeError: 'float' object has no attribute 'numerator'`. The domain rule is exact
  rationals, so a float should be refused by name at the boundary; found by the checkpoint's
  own measurement script (`duration - 4 - 1/25`), not by a test.
- **Open: the repository is not `ruff format`-clean.** `ruff format --check` would
  reformat 63 pre-existing files. Tasks so far keep `ruff check` clean and match
  surrounding style; deciding whether to make formatting enforced (and in CI, TASK_35)
  is a reviewer call, not something to slip into a corrective task.
- **Open: the audio path has never been *heard*.** TASK_11 measured slice extent,
  resampling, fades, chunk joins and mix levels on decoded samples, and the
  desktop checks measure tones and levels in the rendered file; nobody has
  listened to a resampled or retimed clip on real speakers. What only a human can
  judge is whether FFmpeg's resampler and `atempo` sound acceptable on speech and
  music (as opposed to on sine tones), and whether a 44.1 kHz source inside a
  48 kHz mix is audibly worse than an ordinary resample.
- **Open: chunk joins are proven for lossless material only.** The measured
  zero-deviation result covers PCM sources; a *lossy* source (AC3/AAC/DTS) read
  from two chunks seeks to a keyframe/priming boundary, and that difference has
  never been measured. The documented promise is a bounded tolerance, and no test
  asserts it for a lossy source.
- **Open: the new output checks are timed only on short fixtures.** Decoding the
  produced sound and the two-second source-tail probes are cheap here; the cost on
  a feature-length export (and on many clips that each end at their stream's end)
  is unmeasured.
- **Open: exit-time abort in GUI runs.** A test run (and, on the same code path, a
  real quit) can die *after* its result is known: SIGSEGV, bus error, or
  `QThread: Destroyed while thread 'clip-decorations' is still running` followed by
  a core dump, while destroying Qt objects during interpreter shutdown. It was
  reproducible at will in one configuration and intermittent elsewhere; the shutdown
  paths were made idempotent, media sources released on close, and windows destroyed
  while `QApplication` is still alive (see TASK_08 findings). It has not been seen
  since, and it has not been proven gone: nothing in this project repeatedly runs the
  suite to hunt for it, so treat it as unresolved. What would settle it is a
  deterministic teardown — windows destroyed by the code that created them, no
  QObject outliving its owner — rather than a teardown order that happens to work.
- The two-edition workflow (TASK_08) has been validated on synthetic editions
  only: the fixtures are measurable, but nobody has yet run the recipe on two real
  cuts of one film. The procedure for that is README ▸ *Real-film smoke test*;
  the perceptual judgements it asks for (glitches at cuts, drift over minutes,
  whether a thumbnail or envelope is readable at a given zoom) cannot be automated
  away. See the TASK_08 limitations above.
- Preview and still-frame correctness was verified by measurement (container
  parameters, index-selected frame colours compared with the export, spectral
  audio share, playback position mapping in tests). What has **not** been verified
  is perceptual: the checks run with dummy video/audio outputs, so nobody has
  watched or listened to a preview on real speakers, and the visual quality of the
  speed-tuned preview preset and Qt Multimedia's seeking inside a preview file are
  unverified by eye and ear. Run
  `QT_QPA_PLATFORM=xcb DISPLAY=:0 python scripts/desktop_checks/export_check.py show`
  on a desktop to do that.
- The still-frame latency figures above come from short synthetic fixtures. A real
  feature-length source with long GOPs can force a longer decode before the
  requested frame (input `-ss` decodes and discards from the previous keyframe);
  that cost has not been measured on real films.
- Preview cache budgets were exercised in tests with small budgets, not by a long
  editing session over real films.
- Qt Multimedia's FFmpeg playback backend was verified manually in TASK_04
  (source mode: PlayingState, position updates, no errors). What has *not* been
  verified is long-form real-film playback or seeking under load.
- Export was verified end-to-end (GUI → worker → FFmpeg → VLC). Not yet
  exercised on real feature-length films, HDR sources, or >2-channel output;
  those remain untested and are expected to fail clearly rather than work.
- Chunked rendering is exercised by tests with a low item limit, not by a real
  feature-length export that exceeds the 96-item graph bound.
- Project save/reopen, relinking and recovery are verified on synthetic fixtures
  and in the unattended desktop check (including a second window reopening a
  saved project and re-exporting it). Not yet exercised: a multi-hour session over
  real films with several discs/paths, a project file on a network share or
  removable volume (where `mtime_ns` granularity and `os.replace` semantics can
  differ), and Windows path handling (`relpath` across drives is coded to fall
  back to absolute references but has never been run there).
- Recovery after an *actual* crash (SIGKILL, power loss) has not been simulated
  end-to-end; the tests write snapshots through the same code path the timer
  uses, and the restore path is exercised in-window, but no test has killed the
  process and restarted the application.
