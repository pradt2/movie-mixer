# Video Editor

A small, usable, **non-destructive** desktop video editor. The motivating
workflow is combining audio and video from **different editions of a film**
whose timings, cuts, ordering and quality may differ. Everything is manual
editing first; content-aware assistance is explicitly out of scope for this
milestone.

All media assets are peers: no asset is inherently the reference, the best
picture, or the correct soundtrack. The output has its own timeline and may
incorporate any portion of any asset. Audio and picture are chosen and edited
independently, and multiple audio tracks can contribute simultaneously.

See `AGENTS.md` for the full contract and `docs/architecture.md` for design
decisions.

## Requirements

- Python **>= 3.12** (tested on 3.14)
- **PySide6** (installed automatically by pip)
- **ffmpeg / ffprobe** on the `PATH` — used for probing, preview and export.
  The application still starts without them but reports a clear diagnostic
  and disables media operations.

On Fedora: `sudo dnf install ffmpeg` (RPM Fusion) or
`sudo dnf install ffmpeg-free ffmpeg-free-devel`.
On Debian/Ubuntu: `sudo apt install ffmpeg`.

## Installation

```bash
python3 -m venv .venv            # or: uv venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # editable install with pytest + ruff
```

## Running

```bash
video-editor                # GUI
python -m video_editor      # identical
video-editor doctor         # print environment + ffmpeg/ffprobe versions, no GUI
```

## Development

```bash
pytest                        # unit, GUI (offscreen) and FFmpeg integration tests
ruff check .                  # lint
```

GUI tests run with `QT_QPA_PLATFORM=offscreen` automatically (set by
`tests/conftest.py`). To watch the GUI during tests use
`VIDEO_EDITOR_TEST_XCB=1 pytest` on a desktop session. Media integration
tests generate short synthetic fixtures with `ffmpeg` in temporary
directories; no sample media is downloaded or committed.

## Status

`TASK_00` … `TASK_10` are implemented: a manual, non-destructive editor —
import and probing, peer streams, tracks, clips with exact rational time,
undo/redo, frame-accurate stills, rendered previews, export, project files,
recovery and relinking, lazy bounded thumbnails and waveforms, source media that
is never a write target, and one undo/redo boundary that covers the composition
*and* the media catalogue together — validated by the unattended checks at the
end of this file, including a two-edition mixing workflow whose result is
measured from the rendered file.

There is **no automated content matching**, by design: assisted alignment is not
started until the manual workflow has been reviewed by a human on real films.
Known limitations are listed under *Known limitations*; the per-task record,
deliberate deviations and the checks that remain manual are in
`docs/progress.md`.

## Interface & shortcuts

Media bin (left) · viewer above the timeline (centre) · inspector (right).
The viewer clearly labels whether it shows **Source** (media file,
asset-local time) or **Output** (composition) time; timeline playhead,
zoom and selection are navigation only and never enter undo history.

| Shortcut | Action |
|---|---|
| `Ctrl+I` | Import media (background, cancellable) |
| `Ctrl+O` / `Ctrl+S` | Open project / Save project |
| `Ctrl+Shift+S` / `Ctrl+Shift+N` | Save As… / New project |
| `Ctrl+E` | Export composition (settings, range, progress, cancellable) |
| `Ctrl+G` | Place bin-selected stream(s) at playhead (no ripple) |
| `Ctrl+Shift+G` | Insert bin-selected stream(s) at playhead (ripples that track only) |
| `Ctrl+Z` / `Ctrl+Shift+Z` (or `Ctrl+Y`) | Undo / Redo |
| `Delete` | Remove selected clips (leaves a gap) |
| `Ctrl+D` | Duplicate selected clips to an explicit destination time |
| `S` | Split selected clip at playhead |
| `G` | Go to exact time (`H:MM:SS.mmm`, seconds, or `24000/1001`) |
| `F` | Fit whole composition |
| `Ctrl+R` | Render an output preview (cached, cancellable) |
| `Ctrl+←` / `Ctrl+→` | Step one **output frame** (decodes that exact frame) |
| `Ctrl+wheel` | Zoom at cursor · wheel: scroll · `+`/`-` zoom |
| `←`/`→`, `Home` | Nudge / reset playhead |
| `Ctrl+click` bin/timeline | Additive selection |
| `Esc` | Cancel the drag/trim gesture in progress |

Timeline gestures: drag a clip body to move it, drag within ~7 px of an
edge to trim (cursor changes to ↔ to show which), drop on another compatible
track to change tracks. Dragging shows a dashed ghost and a snap marker and
commits exactly one undoable edit on release; `Esc` aborts without an edit.
Snapping (Timeline ▸ Snapping, on by default) attracts to the playhead and
other clips' boundaries within 8 **screen** pixels, so its reach is the same
at every zoom. Precision entry never snaps: use the inspector's numeric
fields (exact `Fraction` values, e.g. `24000/1001`, gain in dB) or `G`.
Linked clips move as a group; changing tracks for a linked clip requires
unlinking first.

Track headers show kind, `[hidden]`/`[muted]` state and E/M toggle buttons.

Help ▸ Keyboard and Workflow… lists every shortcut **as the running build binds
them** (read off the actions themselves, plus the timeline's own keys) and the
order of work below. It is generated rather than typed out so that a documented
chord cannot drift away from a bound one — that drift once made redo dead while
the documentation still promised it.

The inspector's **Output (composition)** group holds the project's own output
size, rational frame rate, sample rate and channel layout. These are never
taken from an asset: the 1920×1080 defaults are the project's choice, changing
them is an ordinary undoable edit (`24000/1001`-style input stays exact), and
the layout list contains only what the exporter can actually encode.

## Timeline pictures: thumbnails and waveforms

Clips show small pictures (video) and a peak envelope (audio). Both are
**derived, lazy and bounded**, never part of the composition:

- The envelope is the selected stream's own samples, downsampled to buckets at
  the current zoom through FFmpeg (`asetnsamples` + `astats` + `ametadata`), and
  painted at asset-local times, so a clip trimmed to `1/3 s` shows the peaks of
  the part you kept at the place they belong. Thumbnails are decoded as one
  contact strip per clip range (`image2pipe`/MJPEG) at the granularity the view
  asks for.
- Extraction runs in one background thread per window, from a queue capped at 32
  jobs, and only for clips **inside the visible interval** that are wide enough
  to draw (≥12 px). Results are cached per asset + stream + file identity
  (size/mtime) inside fixed byte budgets (2 MB of peaks, 6 MB of strips, LRU),
  so the memory does not grow with the length of the film.
- The timeline paints a decoration only when it has actually been delivered;
  clip geometry, labels, selection and the playhead are always drawn. A clip
  whose decoration cannot be made (missing file, unknown stream position,
  unreadable container) is drawn as a plain shape and the reason is reported in
  the status bar — no invented waveform, no guessed thumbnail. Replacing a file
  drops the old pictures instead of reusing them.

## Manual workflow: mixing two editions

The application has no automated matching: picture and sound are chosen by hand
from any stream of any file. `python scripts/make_ab_editions.py --dir DIR`
generates two synthetic editions (24 fps and 25 fps, different shot order and
shot lengths, one shot only in B, one dropped from B, both with their own tones)
and prints the composition to build, clip by clip, with exact source and output
times. That recipe is written out in [`docs/ab-workflow.md`](docs/ab-workflow.md),
and the same composition is built and *measured* automatically by
`python scripts/ab_workflow_check.py`.

1. Import both files (`Ctrl+I`). Project ▸ Media Status… shows every stream in
   each file: kind, container stream index, rate or sample rate, channel count,
   start offset and length. Neither file is the reference. The files themselves
   are part of the saved project, so an import is an edit: it is undone by undo
   and it makes the project unsaved until you save.
2. Set the output in the inspector: size, rational frame rate (`24000/1001`
   works), sample rate, layout. These belong to the project.
3. Add tracks (Timeline ▸ Add Video/Audio Track). Select a stream in the bin and
   place it at the playhead (`Ctrl+G`) or insert with ripple
   (`Ctrl+Shift+G`). Audio needs no video selected. A place reuses a track that
   is free at that time, or adds one when nothing suitable is free — either way
   it is one action, and one undo reverses the whole of it.
4. Align: zoom with `+`/`-` or Ctrl+wheel, read the envelopes and thumbnails,
   seek with `G` or by typing a time, then move/trim with the inspector's exact
   numbers, or step to a real source frame (`Ctrl+←`/`Ctrl+→`) and press
   **Set clip in/out**.
5. Work around extra, missing or reordered footage: split at the playhead (`S`),
   trim edges, delete (`Delete`, leaving a gap), put another edition's material
   on a lower track. Each step is one undoable edit. Splitting a linked A/V pair
   leaves two independent pairs: the left halves keep their original link and the
   right halves get a new one, so moving or splitting the right pair never moves
   the left.
6. Review an interval you typed in the output viewer (`Ctrl+R`) — a preview is a
   real render of the same composition, only smaller.
7. Export the whole composition or a selected interval (`Ctrl+E`), then check the
   produced file against the plan the dialog showed before it ran.
8. Save (`Ctrl+S`). Media is only referenced; moved media is relinked.

## Preview and still-frame inspection (Preview menu, `Ctrl+R`)

The output viewer has a preview panel: range fields (`H:MM:SS.mmm`, seconds or a
rational — empty means a short window around the playhead), picture size
(1:1 … 1:8), **Render Output Preview**, **Cancel**, **Free preview cache** and
**Play preview**. A preview is an ordinary render of the same composition through
the same planner and picture chain as export, with two deliberate differences: the
picture is scaled down and an encoder preset tuned for speed is used. Timing,
stacking, gaps, trims, rates and the audio mix are identical, so a preview is
evidence about the composition rather than a separate approximation of it.
**Play preview** starts the file at the playhead (mapped into the rendered range)
and moving the playhead while playing seeks that file; outside the rendered range
the window says so instead of playing the wrong part of the composition.

Previews are written into a cache the application owns, keyed by a fingerprint of
(composition, resolved asset paths, range, scale, preset). Rendering the same thing
again costs nothing; an edit that changes the media marks the preview **STALE** and
refuses playback of the old file, while an edit that cannot change it — renaming a
track — does not. The cache is LRU-bounded (12 files / 1 GiB by default) and lives
in `$VIDEO_EDITOR_PREVIEW_DIR/video-editor-previews`, or
`<system temp>/video-editor-previews`. Only files this cache created are ever
deleted, by **Free preview cache** or at window close (`VIDEO_EDITOR_KEEP_PREVIEWS=1`
keeps them for inspection). Nothing is written next to your media.

Paused inspection is frame accurate and independent of the preview file: moving the
playhead decodes **the composition's frame under it** (one-frame render plan, the
same picture chain as export — so what you see is the picture the exported file will
contain at that frame index) and reports the output frame number, its exact output
time, the clip/stream it came from and the asset-local source PTS. `Ctrl+←` /
`Ctrl+→` step by output frames; **◀ source frame / source frame ▶** step between
*real* source frames using the stream's actual presentation timestamps, so
variable-frame-rate material lands on real pictures rather than on a nominal grid.
**Set clip in / Set clip out** turn the shown source time into an ordinary trim on
the inspected clip. Where the planned time falls between two real frames (usual on
VFR material) the viewer states both the planned time and the real frame the decoder
produces. Decodes are asynchronous, one at a time, superseded by the newest request,
and the decoded pictures are kept in a RAM cache bounded by picture bytes (64 MiB
and 512 frames by default), not by how many you happened to look at.

## Export (File ▸ Export Composition…, `Ctrl+E`)

The dialog shows what the plan **will** do before anything runs — duration with
exact frame/sample counts, how many picture/audio items and black fills, output
geometry, and any planner warnings (a stream whose end is only a container
upper bound, summed audio that can exceed full scale). A range can be the whole
composition, from the playhead, or exact times (`H:MM:SS.mmm`, seconds, or a
rational like `24000/1001`); an invalid range disables Start and says why.

Rendering runs in a background thread (the window stays responsive and can
keep editing — the export uses its own immutable snapshot), is cancellable, and
writes to a temporary sibling file that is **published** only on success. An
existing output is never replaced without an explicit confirmation, and the
confirmation is tied to the file that stood there when you gave it: a file that
appears or changes while the render is running is never overwritten, and
pressing Cancel after the last progress report still publishes nothing.

**Your media can never be an output.** Naming an imported clip's file as the
destination is refused — with the overwrite question answered yes — including
files that are imported but unused, and including a symlink, a hard link or
another spelling of the same file. The project's own `*.vep` is protected from
exports too. Sources are checked against the identity recorded when they were
imported before decoding **and** again before the file is written, so a film
re-encoded at the same path mid-render is refused rather than cut together from
two different files.

After rendering the produced file is probed and its duration compared with the
plan: more than **one output frame + 2048/48000 s** (documented AAC padding) of
difference is reported as a failure instead of being accepted.

**Currently supported:** MP4 with H.264 video and AAC audio (encoder
availability is checked up front), explicit width/height/frame rate/sample
rate/channel layout from the project's output settings, aspect ratio preserved
with padding, `yuv420p`, mono/stereo/2.1/3.0/5.0/5.1/7.0/7.1 layouts
(`5.1(side)` is normalised), pitch-preserving tempo for non-1.0 clip rates,
video-only or audio-only compositions (black and silence fill), VFR or
irregular-PTS sources normalised to the output frame rate, and compositions
beyond one FFmpeg graph rendered as lossless chunks joined in one final encode.

**Not supported and refused rather than approximated:** other containers/codecs,
bit-exact/copy passthrough, HDR or colour-space/transfer conversion, arbitrary
channel layouts, and sources whose FFmpeg stream addressing is unknown (reimport
them). Audio is summed, never normalised, so overlapping loud tracks can clip —
the plan warns before you start.

## Project files, recovery and relinking (Project menu)

A project is a JSON document (`*.vep`) that **references** your media; saving
never copies or modifies a media file. Project ▸ New (`Ctrl+Shift+N`), Open
(`Ctrl+O`), Save (`Ctrl+S`), Save As… (`Ctrl+Shift+S`), Media Status…, Relink
Missing or Changed Asset…, Recovery Snapshots…

- **Exact by construction.** Every time, rate and gain is stored as
  `"numerator/denominator"`, never a decimal, so `24000/1001` and a trim at
  `1/3 s` come back identically. Track order (top first), enabled/muted state,
  link groups, fades, gains, clip ids and asset ids are all preserved.
- **Atomic writes.** The document is written to a sibling temporary file,
  flushed and then published onto the target with a rename. A full disk or a
  crash leaves the previous project file exactly as it was and no partial file
  behind; replacing an existing file always asks first, and a file that appears
  or changes after you answered is never clobbered. Saving *onto* one of your
  media files — or a link to it — is refused outright.
- **One document, one undo boundary.** What a project file holds — tracks, clips
  *and* the media they reference — is also what history and the dirty mark cover.
  Importing a file is an edit: the title bar gains `*`, Save becomes available,
  undo removes the bin row again, and New or Close asks before discarding it.
  A relink that only moves a file to another path is unsaved work too, even
  though no clip moved, and undo brings back the old path, the identity recorded
  for it and its stream bounds together. The media bin is a view of this state,
  never a separate list that could disagree with the timeline.
- **Validated on load.** Unknown or future schema versions, unknown fields,
  non-exact times, duplicate ids, clips pointing at missing streams/tracks,
  overlapping clips on one track and registered streams whose asset is missing
  from the media list are all refused with a message naming the field or
  invariant — and your current work is left untouched. Media that is simply
  absent from disk is *not* refused: the project opens and stays editable, and
  the media states below say what cannot be read.
- **Relative paths where they help.** Media inside the project's own directory
  is stored relative to the project file and resolved against it, so a folder
  containing the project plus its films can be moved wholesale. Anything else
  stays absolute (a `../` chain would break as soon as one folder is renamed).
- **Media that moved or changed is reported, never guessed.** Every asset's
  recorded identity (size + mtime) is checked against the file at its reference
  — at load, and again whenever a still frame, a preview, an export, a
  decoration refresh or Project ▸ Media Status… needs to know. The states are
  named: **online**, **missing** (nothing at that path), **changed** (a file is
  there but it is not the one recorded — possibly a different encode, so its
  recorded stream bounds are not trusted) and **unreadable** (a directory, a
  permission or I/O problem). The timeline stays fully editable and saveable;
  rendering, stills and stream previews of that material are refused with the
  asset name, state and path, and its timeline thumbnails are withheld rather
  than showing the wrong film. Identity is deliberately *not* a content hash:
  a file rewritten with an identical size **and** timestamp cannot be detected.
  **Relink…** probes the file you choose and validates it against the
  material your clips already use — same stream index and kind, bounds that
  cover every clip interval, same frame/sample rate, same picture size unless
  you knowingly accept a change. A relink that fails changes nothing; a
  successful one keeps the asset's stable id and is undone like any other edit.
- **Recovery snapshots** (not your project file) are taken periodically while
  work is unsaved, into a directory the application owns:
  `$VIDEO_EDITOR_RECOVERY_DIR`, else `$XDG_DATA_HOME/video-editor/recovery`.
  After an abnormal exit the application start-up offers **Restore** or
  **Discard**; restored work is marked unsaved so that writing it to its file
  is a deliberate act. A snapshot is never restored over a project file that is
  newer than the snapshot. `VIDEO_EDITOR_AUTOSAVE_SECONDS` sets the interval
  (default 120 s, `0` disables it). Undo history is not persisted: a snapshot is
  the whole document (composition + media) plus navigation, not a saved session.

## Known limitations

- **Export is MP4/H.264/AAC only**, in the layouts listed above. Everything else
  is refused with an explanation rather than silently approximated.
- **Overlapping clips on the same track are rejected** (overlapping clips on
  different tracks are how mixing is done). No composite/blend, no per-pixel
  masking, no keying.
- **No per-clip speed ramps** — one constant rational rate per clip — and no
  filters beyond scaling/padding: no crop, rotation, deinterlace or denoise.
- **Audio is summed, never normalised**, so two loud overlapping tracks can clip;
  the plan warns before rendering starts.
- **Source playback in the bin uses Qt Multimedia**, which cannot open every
  container FFmpeg can decode; the still-frame and preview paths use FFmpeg and
  do work, and a failure to play says so instead of playing something else.
- **Previews and stills are re-rendered, not a live timeline.** There is no
  realtime playback of the composition at full quality; review is by rendered
  interval (bounded cache) and by frame-accurate stills.
- **Undo history is not saved**: a recovery snapshot restores the whole document
  (composition and media references) and navigation, not the edit history.
- **Media identity is size + timestamp, not a content hash.** That catches a
  re-encode, a replacement or a truncated copy at the same path, but a file
  rewritten with an identical size *and* timestamp is indistinguishable.
  Publication rules (no clobbering, consent tied to the file you approved) are
  atomic on a local filesystem; on a network or removable mount another client
  can still race us, so "refused before writing" is what is guaranteed rather
  than a filesystem-wide transaction.
- **Known open defect: the process can abort while exiting**, after the work is
  already on disk — a segfault or `QThread: Destroyed while thread 'clip-decorations'
  is still running` during Qt's shutdown. The window's shutdown path was made
  idempotent, media sources are released on close and the window is destroyed while
  `QApplication` is alive, which stopped the reproduction; it is not proven
  eliminated, so a crash on quit is a known possibility rather than a mystery. See
  *Outstanding environment checks* in `docs/progress.md`.
- Long compositions are planned and edited quickly (see *Unattended checks*
  below for the measured numbers), but a full-length export is a long job and
  there is no hardware acceleration.

## Unattended checks

Three scripts drive the real application and answer every dialog themselves: no
human clicking is required and they fail loudly rather than skipping. All three
force `QT_QPA_PLATFORM=offscreen` themselves, so an unattended run can never
leave a window or a modal on the desktop you are watching; only the desktop check
accepts `show` to put a real window on screen instead. Each keeps every file it
creates inside a scratch directory (`VIDEO_EDITOR_CHECK_DIR`, default
`<system temp>/video_editor_desktop_check`) and carries a watchdog that aborts
naming the dialog that would otherwise wait for a click that is never coming.
They are checks, not part of `pytest`.

```bash
python scripts/desktop_checks/export_check.py        # the whole window, end to end
python scripts/ab_workflow_check.py                  # two editions mixed, then measured
python scripts/long_project_check.py                 # 2 hours, 843 clips, profiling
```

**The desktop check** (`scripts/desktop_checks/export_check.py`) walks the import
queue, timeline gestures, the modal export dialog, overwrite confirmation, the
background render, composition preview, still-frame inspection, cache lifetime,
save/reopen in a second window, moved media, missing-asset relinking and refusal,
recovery snapshot/restore, the unsaved-work prompt and VLC playback. The preview
leg measures a preview file, a still frame and the exported file at the same
output time and requires the same picture; the project leg re-exports the
reopened project and requires the same frames and the same tone in the same place.
It needs `ffmpeg`, `ffprobe` and (for the playback leg) `cvlc`. To watch it
happen: `QT_QPA_PLATFORM=xcb DISPLAY=:0 python scripts/desktop_checks/export_check.py show`.

**The two-edition check** (`scripts/ab_workflow_check.py`) generates editions A
and B, imports them, checks what the interface reports about each stream (24 fps
against 25 fps, lengths, offsets), builds the 14-clip / 4-track composition
through the same commands the UI uses — including a split, a trim, an undo and a
redo — then renders it and **measures the rendered file**: which colour is on
screen in which output second, which tone sounds at which level, the summed
region where both sound tracks play, the gap of silence, the half-gain ratio, the
rate change, and that B's white picture and 880 Hz tone never appear. It then
renders a chosen 2-second interval, refuses an interval outside the composition,
cancels a render and requires that no file and no temporary file is left behind,
refuses to render after a source was moved, relinks it and renders again, and
finally saves and reopens the project. `scripts/make_ab_editions.py --dir DIR`
builds the same fixtures and prints the recipe for doing this by hand.

**The long-project check** (`scripts/long_project_check.py`) assembles a
two-hour output of 843 clips on three tracks with mixed rates, and measures
whole-timeline planning (180 150 output frames in under a second), a 10-second
window planned from the middle of that timeline in ~5 ms, a still frame decoded
1.5 h in (~0.4 s), timeline repaint at three zoom levels (9-29 ms, with five child
widgets for 843 clips), clip hit-testing, playhead moves, an edit plus its undo,
the decoration cache and queue held inside their budgets regardless of timeline
length, thread and child-process counts, peak memory for the whole run (~150 MiB)
and a ~290 kB project saved and read back. It deliberately does **not** render
two hours: full-length *planning* plus one selected render window is what the
editor has to be able to do. Timings move with the machine, so the checks assert
budgets rather than exact numbers.

## Real-film smoke test (for a human)

No copyrighted media is bundled, and none is needed by any automated check. To
judge the tool on films you own — two editions of one title, for instance —
work in a directory of your own and walk through this:

1. Import both files and read Project ▸ Media Status…: do the frame rates, stream
   indices, channel layouts and start offsets match what you know about them? Is
   any file treated as the reference when it should not be?
2. Choose the output on purpose — the frame rate of one edition and the size of
   the other, say — and confirm nothing about your choice is inferred for you.
3. Build 30-60 seconds by hand: picture from one edition, sound from the other,
   one region using both sound tracks at once, one fade, one gap where a shot is
   missing from one edition, and one clip whose rate you corrected. Prefer the
   inspector's exact numbers to dragging, and use the waveforms to align.
4. Review each boundary with a short preview interval (a couple of seconds either
   side of the cut) and step across one cut frame by frame (`Ctrl+←` / `Ctrl+→`).
5. Undo the last few edits and redo them; save; close; reopen, and confirm the
   timeline, clip identities and navigation came back.
6. Export a few minutes rather than the whole film, then compare the file with the
   composition: frame count and duration, the cuts where you put them, the level
   of each source in the mixed region, silence where the gap is. Play it in an
   external player and watch for flashes at cuts, for audio drifting away from
   picture over minutes, and for any fade that restarts.
7. Move one media file and reopen the project: the asset should be reported
   missing, editing should still work, rendering should be refused with the path
   named, and Relink… should accept a file only if it matches what the clips use.

Step 6 is where a defect shows up as audiovisual glitching rather than as a
number, so it cannot be automated away: audio pitch after a rate change, a cut
one frame early, a fade that pops. If you see one, report the project file
(`*.vep`) and the output settings — the media themselves are never required.

## Layout

```
src/video_editor/
  core/        pure domain: timeline model, commands, history   (standard library only)
  fs/          file identity, source write-protection, publishing (standard library only)
  media/       ffprobe/ffmpeg adapters, probing, frames, waveforms,
               thumbnails                                     (no UI)
  render/      render-plan construction, FFmpeg graph, export,
               preview and frame caches                       (no UI)
  project/     project document + session, serialization, relinking,
               recovery                                       (no UI)
  ui/          Qt main window, media bin, viewer, timeline, inspector
  diagnostics.py  ffmpeg/ffprobe availability checks
scripts/
  ab_workflow_check.py     two-edition mixing, rendered and measured
  long_project_check.py    2-hour / 843-clip responsiveness profile
  make_ab_editions.py      generate the two editions + print the manual recipe
  desktop_checks/          the check implementations (not part of pytest)
```

Every layer above is implemented and tested. Nothing here is a scaffold for the
later content-aware assistance: there is no alignment, matching or segmentation
code, and none should be added before the manual workflow has been reviewed.
