"""Unattended desktop acceptance driver for the composition export path.

Real window, real import queue, real commands, real modal dialogs, real FFmpeg,
real VLC. Nothing here asks a human to click: modal dialogs are located through
``QApplication.activeModalWidget`` and answered programmatically. Coordinates
for gestures come from the timeline's own geometry (``pixel_at_time`` and the
row layout constants), never from hand-picked screen positions — a point that
lands in the ruler moves the playhead instead of selecting a clip and would
silently invalidate a check.

    python scripts/desktop_checks/export_check.py          # headless: no windows shown
    QT_QPA_PLATFORM=xcb DISPLAY=:0 python scripts/desktop_checks/export_check.py show

The first form forces ``QT_QPA_PLATFORM=offscreen`` itself, so a run can never
put a window or a dialog on the desktop it is being watched from.

Passing "show" leaves the window open afterwards for a human look. Scratch files
live under $VIDEO_EDITOR_CHECK_DIR (default: "video_editor_desktop_check" inside
the system temp directory).
"""

import json
import math
import os
import shutil
import subprocess
import sys
import time
from array import array
from fractions import Fraction as F
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import driver as G
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QDialogButtonBox

from video_editor.core.commands import MoveClipCommand

FIX = G.FIXTURES
TMP = G.WORKDIR / "out"
OUT = TMP / "composition.mp4"
RATE = F(25)
SR = 48000
RESULTS: list[tuple[bool, str, str]] = []

#: preview renders must stay inside the scratch area of this run, and the window
#: reads this before it builds its cache, so it is set here rather than in main()
os.environ.setdefault("VIDEO_EDITOR_PREVIEW_DIR", str(G.WORKDIR / "previews"))
#: recovery snapshots must stay inside the scratch area too: this check creates
#: and discards them, and must never touch a real user's recovery directory
os.environ.setdefault("VIDEO_EDITOR_RECOVERY_DIR", str(G.WORKDIR / "recovery"))


def check(label: str, ok: object, detail: str = "") -> None:
    RESULTS.append((bool(ok), label, detail))
    print(f"{'OK  ' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""), flush=True)


def ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                          capture_output=True, text=True, timeout=600)


def frame_at(path: Path, index: int, x: int = 700, y: int = 500) -> tuple[int, int, int]:
    """RGB of frame *index*, chosen by decoded frame index (never an input -ss).

    The crop is 2x2 with even offsets because 4:2:0 chroma cannot address an odd
    single pixel; the first decoded pixel of that crop is the sample.
    """
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vf",
         f"select=eq(n\\,{index}),crop=2:2:{x}:{y}", "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, timeout=600)
    assert proc.returncode == 0 and len(proc.stdout) >= 6, proc.stderr.decode()[-300:]
    return (proc.stdout[0], proc.stdout[1], proc.stdout[2])


def pcm(path: Path, start: F, seconds: F) -> list[float]:
    """Mono float samples of ``[start, start+seconds)``, trimmed after decoding."""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vn", "-af",
         f"atrim=start={float(start):.9f}:end={float(start + seconds):.9f}",
         "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True, timeout=600)
    assert proc.returncode == 0, proc.stderr.decode()[-300:]
    return list(array("f", proc.stdout))


def rms(samples: list[float]) -> float:
    return math.sqrt(sum(x * x for x in samples) / len(samples)) if samples else 0.0


def goertzel(samples: list[float], freq: float) -> float:
    if not samples:
        return 0.0
    coeff = 2 * math.cos(2 * math.pi * freq / SR)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s1, s2 = s0, s1
    return math.sqrt(max(0.0, s1 * s1 + s2 * s2 - coeff * s1 * s2)) / len(samples)


def tone_share(path: Path, start: F, seconds: F, freq: float = 660.0) -> float:
    """Fraction of a window's energy sitting at one frequency."""
    samples = pcm(path, start, seconds)
    noise = rms(samples)
    return goertzel(samples, freq) / noise if noise else 0.0


def close_to(a, b, tol: int = 24) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def make_fixtures() -> dict:
    FIX.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    video = FIX / "colours_1440x1080.mkv"
    tone = FIX / "tone660.wav"
    assert ffmpeg("-f", "lavfi", "-i", "color=c=red:size=1440x1080:rate=25:duration=1",
                  "-f", "lavfi", "-i", "color=c=green:size=1440x1080:rate=25:duration=1",
                  "-f", "lavfi", "-i", "color=c=blue:size=1440x1080:rate=25:duration=1",
                  "-f", "lavfi", "-i", "color=c=magenta:size=1440x1080:rate=25:duration=1",
                  "-filter_complex", "[0][1][2][3]concat=n=4:v=1:a=0",
                  "-pix_fmt", "yuv420p", "-c:v", "libx264", "-g", "1", str(video)).returncode == 0
    assert ffmpeg("-f", "lavfi", "-i", "sine=frequency=660:duration=2:sample_rate=48000",
                  "-ac", "2", str(tone)).returncode == 0
    palette = {name: frame_at(video, index * 25 + 12)
               for index, name in enumerate(("red", "green", "blue", "magenta"))}
    return {"video": video, "tone": tone, "palette": palette}


def centre_rgb(frame) -> tuple[int, int, int]:
    """Centre pixel of a decoded :class:`StillFrame`'s RGB buffer."""
    offset = (frame.height // 2) * frame.width * 3 + (frame.width // 2) * 3
    return frame.rgb[offset], frame.rgb[offset + 1], frame.rgb[offset + 2]


def wait_preview(win, limit: int = 1200) -> None:
    while win.preview_controller.running and limit > 0:
        G.pump(100)
        limit -= 1


def preview_checks(win, palette: dict, exported: Path, video_clip) -> None:
    """Composition preview, still-frame inspection and cache lifetime, in the window.

    The measured rule is the one the whole task rests on: a preview frame, a still
    frame and an exported frame at the same output time must be the same picture.
    """
    root = Path(os.environ["VIDEO_EDITOR_PREVIEW_DIR"])
    if not str(root.resolve()).startswith(str(G.WORKDIR.resolve())):
        raise SystemExit(
            f"refusing to run: VIDEO_EDITOR_PREVIEW_DIR={root} is outside the scratch "
            f"area {G.WORKDIR}; this check deletes preview files and must not touch "
            "anything else"
        )
    shutil.rmtree(root, ignore_errors=True)  # this run's scratch, incl. an earlier crash's debris
    events: dict[str, list] = {"progress": [], "finished": [], "failed": [], "cancelled": []}
    win.preview_controller.progress.connect(lambda gen, v: events["progress"].append(v))
    win.preview_controller.finished.connect(lambda gen, res: events["finished"].append(res))
    win.preview_controller.failed.connect(lambda gen, msg: events["failed"].append(msg))
    win.preview_controller.cancelled.connect(lambda gen: events["cancelled"].append(gen))

    G.ensure_active()
    win.output_viewer.range_start.setText("0:00:01.000")
    win.output_viewer.range_end.setText("0:00:03.000")
    win.output_viewer.mark_range_edited()
    G.key_press(*G.shortcut_keys(win.preview_action))
    wait_preview(win)
    entry = win._preview
    preview_file = None if entry is None else entry[4]
    check(f"{win.preview_action.shortcut().toString()} rendered the typed range at the "
          "chosen scale",
          entry is not None and preview_file is not None and entry[1] == F(1)
          and entry[2] == F(3) and entry[3] == F(1, 2),
          f"entry={None if entry is None else (str(entry[1]), str(entry[2]), str(entry[3]))} "
          f"state={win.output_viewer.state.text()[:70]} progress={len(events['progress'])}")
    if preview_file is None:
        check("preview media exists to measure", False, "nothing was rendered")
        return
    check("the preview file is inside the application's own cache directory",
          preview_file.is_file() and preview_file.parent == win.preview_cache.root
          and str(preview_file).startswith(str(G.WORKDIR)), str(preview_file))

    ff = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:stream=codec_type,codec_name,width,height,"
                            "sample_rate,r_frame_rate", "-of", "default=nw=1", str(preview_file)],
                        capture_output=True, text=True, timeout=120)
    fields = " ".join(ff.stdout.split())
    check("preview is H.264/AAC video with an audio track like the export",
          "codec_name=h264" in fields and "codec_name=aac" in fields, fields[:150])
    check("preview picture is the reduced size, output rate unchanged",
          "width=640" in fields and "height=360" in fields and "r_frame_rate=25/1" in fields,
          fields[:150])
    lines = ff.stdout.splitlines()
    durations = [ln for ln in lines if ln.startswith("duration=")]
    produced = (F(durations[0].split("=", 1)[1]).limit_denominator(10 ** 6)
                if durations else None)
    check("preview covers the requested 2 s range",
          produced is not None and abs(produced - F(2)) <= F(1, 25) + F(2048, SR),
          f"{float(produced) if produced is not None else None}s")
    check("preview picture matches the composition: first frame of the range is the "
          "source's first colour",
          close_to(frame_at(preview_file, 0, x=320, y=180), palette["red"], 26),
          str(frame_at(preview_file, 0, x=320, y=180)))
    check("preview picture matches the composition 1 s later",
          close_to(frame_at(preview_file, 25, x=320, y=180), palette["green"], 26),
          str(frame_at(preview_file, 25, x=320, y=180)))
    check("preview and export agree at the same output time",
          close_to(frame_at(preview_file, 25, x=320, y=180), frame_at(exported, 50), 26),
          f"preview {frame_at(preview_file, 25, x=320, y=180)} vs export "
          f"{frame_at(exported, 50)}")
    check("the 660 Hz tone is in the preview mix as it is in the export",
          tone_share(preview_file, F(1, 4), F(1, 2)) > 0.5, preview_file.name)

    # ------------------------------------------------- still frames at the playhead
    G.set_playhead(F(2))
    settled = win.still_controller.wait_idle(60_000)
    frame = win._source_shown
    check("moving the playhead decodes the exact output frame under it",
          settled and frame is not None and frame.frame_index == 50,
          None if frame is None else f"index={frame.frame_index} t={frame.output_time}")
    check("the still frame is the same picture the export writes at that index",
          frame is not None and close_to(centre_rgb(frame), frame_at(exported, 50), 22),
          None if frame is None else str(centre_rgb(frame)))
    check("the frame line states the output frame number and time",
          "Frame 50" in win.output_viewer.time_label.text(),
          win.output_viewer.time_label.text())
    check("the source PTS under the frame is exact for constant-rate material",
          frame is not None and frame.source_pts == F(1)
          and frame.decoded_source_pts == F(1),
          None if frame is None else f"{frame.source_pts} / {frame.decoded_source_pts}")
    check("the viewer states where the picture came from",
          "composition" in win.output_viewer.provenance.text()
          and "clip" in win.output_viewer.provenance.text(),
          win.output_viewer.provenance.text()[:90])

    playhead_before = win.timeline.playhead
    G.key_press(*G.shortcut_keys(win.step_fwd_action))
    win.still_controller.wait_idle(60_000)
    stepped = win._source_shown
    check(f"{win.step_fwd_action.shortcut().toString()} advances exactly one output frame",
          win.timeline.playhead == playhead_before + F(1, 25),
          f"{playhead_before} -> {win.timeline.playhead}")
    check("the stepped output frame is decoded and reported",
          stepped is not None and stepped.frame_index == 51
          and "Frame 51" in win.output_viewer.time_label.text(),
          None if stepped is None else str(stepped.frame_index))

    source_before = stepped.decoded_source_pts if stepped else None
    win.output_viewer.source_fwd_button.click()
    # the source decode is asynchronous, and this fixture is 1440x1080: wait for the
    # result to be *delivered* to the window rather than guessing a pump duration
    win.source_step_controller.wait_idle(60_000)
    moved = win._source_shown
    # This check authored the stream itself: 100 pictures at exactly 1/25 s, every
    # one a keyframe. That is the independent list of real source frames, so the
    # picture a step lands on can be judged against the file, not against whatever
    # the application believes the file contains.
    authored = {F(n, 25) for n in range(100)}
    check("source stepping shows a real source picture and states its own measured PTS",
          moved is not None and source_before is not None
          and moved.decoded_source_pts is not None
          and moved.decoded_source_pts > source_before
          and moved.decoded_source_pts in authored and moved.grid_exact,
          f"{source_before} -> {None if moved is None else moved.decoded_source_pts} "
          f"grid_exact={None if moved is None else moved.grid_exact}")
    check("the frame under a source step is unchanged: stepping chooses an edge, not a cut",
          moved is not None and stepped is not None and moved.frame_index == stepped.frame_index,
          None if moved is None else str(moved.frame_index))

    # -------------------------------------------------- staleness, reuse, freeing
    G.click_timeline(F(3), G.row_of(video_clip.track_id))
    check("clicking the clip selected it for the inspector",
          win.timeline.selected_clips == (video_clip.id,), str(win.timeline.selected_clips))
    from video_editor.core.commands import RenameTrackCommand

    track_id = video_clip.track_id
    win._apply(RenameTrackCommand(track_id, "Picture from edition A"))
    check("a rename that cannot change the rendered media does not go stale",
          win.output_viewer.state.text().startswith("Preview is current"),
          win.output_viewer.state.text()[:70])

    win.inspector.output_start.setText("1.2")
    win.inspector.apply_button.click()
    check("an edit that changes the picture marks the preview stale instead of "
          "presenting the old render",
          win.output_viewer.state.text().startswith("STALE"),
          win.output_viewer.state.text()[:80])
    check("a stale preview cannot be played as if it were current",
          not win.output_viewer.play_button.isEnabled(),
          f"play enabled={win.output_viewer.play_button.isEnabled()}")

    undo_keys = G.shortcut_keys(win.undo_action)
    G.key_press(*undo_keys)
    events["finished"].clear()
    G.key_press(*G.shortcut_keys(win.preview_action))
    wait_preview(win)
    check("undoing the edit and re-rendering reuses the cached file (no second render)",
          not events["finished"] and win._preview is not None
          and win._preview[4] == preview_file,
          f"renders={len(events['finished'])} file={win._preview[4] if win._preview else None} "
          f"state={win.output_viewer.state.text()[:60]}")

    # a bigger request, so the cancel path is genuinely exercised rather than raced:
    # full-size picture over the whole composition
    win.output_viewer.scale_box.setCurrentIndex(0)  # 1:1
    win.output_viewer.range_start.setText("0:00:00.000")
    win.output_viewer.range_end.setText("0:00:05.000")
    win.output_viewer.mark_range_edited()
    G.key_press(*G.shortcut_keys(win.preview_action))
    G.pump(250)
    was_running = win.preview_controller.running
    if was_running:
        # the panel's own Cancel button: this action has no shortcut by design
        win.output_viewer.cancel_button.click()
        wait_preview(win)
    check("cancelling a running preview stops it and says so",
          was_running and events["cancelled"]
          and not win.output_viewer.cancel_button.isEnabled(),
          f"was_running={was_running} cancelled={events['cancelled']} "
          f"state={win.output_viewer.state.text()[:60]}")
    leftovers = sorted(p.name for p in root.rglob("*")
                       if ".partial." in p.name or ".chunk" in p.name)
    check("a cancelled preview leaves no staging files behind", not leftovers, str(leftovers))

    win.output_viewer.free_cache_button.click()
    G.pump(300)
    remaining = sorted(p.name for p in root.rglob("*.mp4"))
    check("freeing the preview cache deletes the files this application created",
          not remaining and win._preview is None, str(remaining))
    check("the composition is still the exported arrangement",
          G.clip_span(video_clip.id) == (F(1), F(5)), str(G.clip_span(video_clip.id)))


def _save_to(win, destination: Path) -> bool:
    """Save through the window's own code.

    With no project file yet the destination comes from the file chooser, and
    only the chooser is replaced — a native dialog no unattended run can answer.
    Once the window knows a file, Save does not open a chooser at all, so the
    destination is handed over directly. Everything from that point on is the
    production path, including the overwrite prompt.
    """
    from PySide6.QtWidgets import QFileDialog

    if win.project_path is not None:
        return win._save_project(path=destination)

    def chosen(*_args, **_kwargs):
        return (str(destination), "")

    original = QFileDialog.getSaveFileName
    QFileDialog.getSaveFileName = staticmethod(chosen)
    try:
        return win._save_project()
    finally:
        QFileDialog.getSaveFileName = original


def project_checks(win, fixtures: dict, palette: dict, exported: Path, saved):
    """Returns the window that is live afterwards (it replaces windows as it goes)."""
    """Project files, relinking and recovery, in the real window.

    The measured rules: a saved project must come back *identically*, media that
    moved must still be found (or be reported and relinkable), and unsaved work
    must be recoverable without ever overwriting a newer project file.
    """
    from video_editor.project import recovery as recovery_module
    from video_editor.project.storage import AssetState

    (saved_project, saved_clip_ids, saved_asset_ids,
     saved_playhead, saved_pps, video_asset_id) = saved
    recovery_root = Path(os.environ["VIDEO_EDITOR_RECOVERY_DIR"]).resolve()
    if not str(recovery_root).startswith(str(G.WORKDIR.resolve())):
        raise SystemExit(
            f"refusing to run: VIDEO_EDITOR_RECOVERY_DIR={recovery_root} is outside "
            f"the scratch area {G.WORKDIR}; this check creates and deletes recovery "
            "snapshots and must not touch anything else"
        )
    shutil.rmtree(recovery_root, ignore_errors=True)

    answerer = G.ModalAnswerer()
    answerer.start()
    try:
        project_file = TMP / "film.vep"

        # ---------------------------------------------------- save over an existing file
        project_file.write_text("somebody else's file", encoding="utf-8")
        before_bytes = project_file.read_bytes()
        answerer.project_answers["Replace existing project file?"] = "No"
        check("the project is dirty before saving (the gestures edited it)",
              win.session.is_dirty, f"title={win.windowTitle()}")
        check("Save As refuses to replace an existing file without consent",
              not _save_to(win, project_file) and project_file.read_bytes() == before_bytes,
              "answered 'No'")
        check("the window still has no project file after a declined Save As",
              win.project_path is None, str(win.project_path))
        answerer.project_answers["Replace existing project file?"] = "Yes"
        check("Save As replaces an existing project file after consent",
              _save_to(win, project_file) and project_file.is_file(), str(project_file))
        check("the saved file is a real project document",
              json.loads(project_file.read_text(encoding="utf-8"))["schema_version"] == 1,
              project_file.read_text(encoding="utf-8")[:60].replace("\n", " "))

        # ---------------------------------------------------- reopen in another window
        G.close_window()
        win = G.window()
        check("a second window reopened the project file",
              win._open_project(project_file) and win.project_path == project_file,
              G.status_text()[:90])
        detail = [(c.id[:8], str(c.output_start), str(c.source_in), str(c.source_out),
                   str(c.rate)) for c in win.session.project.clips]
        check("the reopened project is identical, down to ids and exact times",
              win.session.project == saved_project, str(detail))
        check("clip/track/asset ids are the ones the composition used",
              {c.id for c in win.session.project.clips} == saved_clip_ids
              and {a.id for a in win.bin_controller.assets} == saved_asset_ids,
              str(sorted(a.id[:8] for a in win.bin_controller.assets)))
        check("the reopened project is unmodified and has no undo history",
              not win.session.is_dirty and not win.session.can_undo,
              f"dirty={win.session.is_dirty} can_undo={win.session.can_undo}")
        check("media references resolve from the saved document, and the media are there",
              all(r.state is AssetState.ONLINE and r.resolved_path.exists()
                  for r in win._media_records),
              str([f"{r.state.value}:{r.resolved_path}" for r in win._media_records]))
        modes = [a["path_mode"] for a in json.loads(project_file.read_text(
            encoding="utf-8"))["assets"]]
        check("references are absolute here because the media are outside the "
              "project folder (relative would escape it)",
              set(modes) == {"absolute"}, str(modes))
        check("navigation came back exactly (playhead and zoom)",
              win.timeline.playhead == saved_playhead
              and win.timeline.pps == saved_pps,
              f"playhead={win.timeline.playhead} pps={win.timeline.pps}")

        # ---------------------------- export the reopened project and compare pictures
        REOPENED = TMP / "after-reopen.mp4"
        # only one poller may answer dialogs at a time: the project answerer
        # knows nothing about the export destination and would reject the dialog
        answerer.stop()
        reopen_answerer = G.ModalAnswerer(destination=REOPENED, overwrite="Yes")
        reopen_answerer.start()
        win._export_composition()
        reopen_answerer.stop()
        answerer.start()
        for _ in range(600):
            if not win.render_controller.running:
                break
            G.pump(100)
        check("the reopened project exports", REOPENED.is_file(), str(REOPENED))
        same = all(
            close_to(frame_at(REOPENED, index), frame_at(exported, index), 8)
            for index in (0, 25, 50, 100)
        )
        check("the reopened export is the same picture as the original, frame by frame",
              same,
              str([frame_at(REOPENED, i) for i in (25, 50, 100)]))
        check("the reopened export still matches the source colours",
              close_to(frame_at(REOPENED, 25), palette["red"], 22)
              and close_to(frame_at(REOPENED, 100), palette["magenta"], 22),
              f"{frame_at(REOPENED, 25)} vs {palette['red']}")
        check("the reopened export still carries the audio clip where it belongs",
              tone_share(REOPENED, F(1, 4), F(1, 2)) > 0.5
              and tone_share(REOPENED, F(9, 4), F(1, 2)) < 0.05,
              f"in={tone_share(REOPENED, F(1, 4), F(1, 2)):.3f} "
              f"gap={tone_share(REOPENED, F(9, 4), F(1, 2)):.3f}")

        # ------------------------------------------- project and media moved together
        # saved *inside* the media folder: that is the only layout where relative
        # references make sense, and the one that lets a folder move as a unit
        package = FIX / "package.vep"
        check("a project saved among its media stores relative references",
              _save_to(win, package)
              and all(a["path_mode"] == "relative" for a in json.loads(
                  package.read_text(encoding="utf-8"))["assets"]),
              str([f"{a['path_mode']}:{a['path']}" for a in json.loads(
                  package.read_text(encoding="utf-8"))["assets"]]))
        moved = G.WORKDIR / "moved"
        shutil.rmtree(moved, ignore_errors=True)
        moved.mkdir(parents=True)
        shutil.copy2(package, moved / package.name)
        for name in ("video", "tone"):
            shutil.copy2(fixtures[name], moved / fixtures[name].name)
        moved_project = moved / package.name
        G.close_window()
        win = G.window()
        check("a moved project folder still finds its media by relative reference",
              win._open_project(moved_project)
              and all(r.state is AssetState.ONLINE for r in win._media_records),
              str([str(r.resolved_path) for r in win._media_records]))
        check("nothing is referenced inside the original media directory any more",
              all(not str(r.resolved_path).startswith(str(FIX.resolve()))
                  for r in win._media_records),
              str([r.resolved_path.name for r in win._media_records]))

        # -------------------------------------- media moved away: offline, relink, refuse
        relocated = moved / "relocated.mkv"
        video_path = next(
            r.resolved_path for r in win._media_records
            if r.asset.path.name.startswith("colours")
        )
        shutil.move(str(video_path), str(relocated))
        G.close_window()
        win = G.window()
        answerer.project_answers["Missing or changed media"] = "Keep as is"
        check("a missing asset is detected on open using its recorded identity",
              win._open_project(moved_project)
              and any(r.state is AssetState.MISSING for r in win._media_records),
              str([(r.asset.display_name(), r.state.value) for r in win._media_records]))
        check("the offline composition is intact and inspectable",
              win.session.project == saved_project,
              f"clips={len(win.session.project.clips)}")
        refusal = win._preview_state_ok()
        check("rendering that material is refused with a precise diagnostic",
              bool(refusal) and "Rendering is refused" in refusal and "missing" in refusal
              and str(video_path) in refusal,
              (refusal or "")[:160])
        project_offline = moved / "offline.vep"
        answerer.project_answers["Project saved with unavailable media"] = "OK"
        check("an offline project can still be saved",
              _save_to(win, project_offline) and project_offline.is_file(),
              str(project_offline))
        target = next(a for a in win.bin_controller.assets if a.id == video_asset_id)
        check("relinking to the moved file rebinds the same asset id and stream",
              win._relink_asset_to(target, relocated)
              and all(r.state is AssetState.ONLINE for r in win._media_records)
              and win._preview_state_ok() is None,
              G.status_text()[:120])
        check("a relink is an ordinary undoable edit",
              win.session.is_dirty and win.session.can_undo,
              f"dirty={win.session.is_dirty}")
        win._undo()
        check("undo puts the asset's previous stream bounds back",
              win.session.project == saved_project,
              f"clips={len(win.session.project.clips)}")
        picture_records = [r for r in win._media_records if r.asset.id == video_asset_id]
        # the media half is restored with the composition half: the asset is at
        # the path this project file names again — so it is missing, because that
        # is the file the check moved — and not at the file the undone relink chose
        check("undo also restores the recorded path and identity, not just the streams",
              all(str(r.resolved_path) == str(video_path)
                  and r.state is AssetState.MISSING for r in picture_records),
              str([(r.state.value, str(r.resolved_path)) for r in picture_records]))
        answerer.project_answers["Relink refused"] = "OK"
        wrong = fixtures["tone"]  # a sound file where the picture should be
        refused_before = len(answerer.project_dialogs.get("Relink refused") or [])
        check("an incompatible replacement is refused, never substituted",
              not win._relink_asset_to(target, wrong)
              and win.session.project == saved_project
              and len(answerer.project_dialogs.get("Relink refused") or [])
              == refused_before + 1,
              (answerer.project_dialogs.get("Relink refused") or [""])[-1][:200])
        check("the refusal says which stream was the wrong kind of media",
              "audio" in (answerer.project_dialogs["Relink refused"][-1]).lower()
              and "video" in (answerer.project_dialogs["Relink refused"][-1]).lower(),
              answerer.project_dialogs["Relink refused"][-1][:200])
        # a refused relink changes neither the composition nor the catalogue: the
        # asset still names the path this project file was saved with
        check("the refused relink left the asset pointing at the file it is saved as",
              all(str(r.resolved_path) == str(video_path) for r in
                  [x for x in win._media_records if x.asset.id == video_asset_id]),
              str([str(r.resolved_path) for r in win._media_records]))

        # ------------------------------------------------------------------- recovery
        win._relink_asset_to(target, relocated)  # a known-good, dirty composition
        playhead = win.timeline.playhead
        # what the snapshot must bring back, recorded as it is at capture time:
        # this composition deliberately differs from the saved one (the relink
        # re-registered the streams), and comparing against `saved_project` here
        # would be testing the wrong thing
        at_snapshot = win.session.project
        # a snapshot belongs to the file this window is saved as, which the
        # offline save above moved
        snapshot_project = win.project_path
        win._take_recovery_snapshot(reason="acceptance run")
        store = recovery_module.RecoveryStore(recovery_module.session_key(snapshot_project))
        check("unsaved work takes a recovery snapshot in the app's own directory",
              bool(store.list()),
              str(sorted(path.name for path in (recovery_root / recovery_module.session_key(
                  snapshot_project)).iterdir())))
        snapshots = store.list()
        check("a recovery snapshot was stored under the application directory",
              bool(snapshots) and all(str(s.path).startswith(str(recovery_root))
                                      for s in snapshots),
              f"{len(snapshots)} snapshot(s)")
        check("recovery snapshots are never written beside the project file",
              not list(moved.glob("*.json")), str(sorted(p.name for p in moved.iterdir())))
        check("a snapshot names what it is and where it came from",
              snapshots and "acceptance run" in recovery_module.describe_snapshot(snapshots[0])
              and str(snapshot_project.name) in recovery_module.describe_snapshot(snapshots[0]),
              recovery_module.describe_snapshot(snapshots[0]) if snapshots else "none")
        G.close_window()
        win = G.window()
        candidate = win._startup_recovery_candidate()
        check("a window does not interrupt its user: recovery is only offered when asked",
              candidate is not None and not win.session.project.clips,
              recovery_module.describe_snapshot(candidate) if candidate else "none")
        answerer.project_answers["Unsaved work was found"] = "Restore"
        on_disk_before = snapshot_project.read_bytes()
        check("the recovery offer restores the recovered work exactly",
              win.offer_startup_recovery()
              and win.session.project == at_snapshot
              and win.timeline.playhead == playhead,
              G.status_text()[:120])
        # the snapshot names a project file, so restoring reclaims that path —
        # but the work is still unsaved, because nothing on disk holds it, and
        # restore itself must not write anything
        check("recovered work is treated as unsaved, not as the saved file",
              win.session.is_dirty and win.project_path == snapshot_project,
              f"dirty={win.session.is_dirty} path={win.project_path}")
        check("restoring a snapshot never writes to the project file",
              snapshot_project.read_bytes() == on_disk_before, str(snapshot_project))
        # the file on disk is newer than the snapshot: restore must be refused
        store = recovery_module.RecoveryStore(recovery_module.session_key(snapshot_project))
        stat = snapshot_project.stat()
        os.utime(snapshot_project, ns=(stat.st_atime_ns, time.time_ns() + 10_000_000_000))
        stale = store.list()
        refusal = recovery_module.disk_ahead_of_snapshot(stale[0]) if stale else None
        check("restore is refused when the project file on disk is newer than the snapshot",
              bool(refusal) and "written after this snapshot" in refusal,
              (refusal or "")[:160])
        discarded = store.discard()
        check("discarding snapshots removes the application's own files and nothing else",
              discarded >= 1 and not store.list() and project_file.is_file()
              and snapshot_project.is_file() and moved.is_dir(),
              f"discarded={discarded} left={len(store.list())}")

        # ------------------------------------------------- the unsaved-work quit prompt
        win._apply(MoveClipCommand(saved_project.clips[0].id, output_start=F(1, 2)))
        on_disk = snapshot_project.read_bytes()
        answerer.project_answers["Unsaved work"] = "Cancel"
        check("closing with unsaved work is refused until the user decides",
              not win._confirm_quit() and win.isVisible() and win.session.is_dirty,
              G.status_text()[:80])
        check("a refused quit wrote nothing to the project file",
              snapshot_project.read_bytes() == on_disk, str(snapshot_project))
        answerer.project_answers["Unsaved work"] = "Discard"
        check("discarding unsaved work lets the window close without writing",
              win._confirm_quit() and win.session.is_dirty,
              f"dirty={win.session.is_dirty}")
        win.close()
        G.pump(200)
        check("the window is gone and its unsaved edit is still only in memory",
              not win.isVisible() and snapshot_project.read_bytes() == on_disk,
              str(snapshot_project))
        check("no unexpected modal dialogs appeared during the project checks",
              not answerer.unexpected, str(answerer.unexpected))
        answerer.project_answers["Unsaved work"] = "Discard"
    finally:
        answerer.stop()
    G.adopt_window(win)  # closed above; shutdown must still own it
    return win


def main() -> int:
    # "no human needed" includes showing nothing: unless a desktop run was asked
    # for ("show") or the platform was chosen explicitly, the window is drawn by
    # the offscreen plugin, so this run cannot leave a dialog on someone's screen
    if len(sys.argv) <= 1 or sys.argv[1] != "show":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    print(f"platform: {os.environ.get('QT_QPA_PLATFORM') or 'desktop default'}", flush=True)
    G.set_deadline(1500)  # a dialog nobody answers must abort with a reason, not hang
    fixtures = make_fixtures()
    palette = fixtures["palette"]
    win = G.window()

    # ------------------------------------------------------------------ import
    G.import_files([fixtures["video"], fixtures["tone"]])
    assets = win.bin_controller.assets
    check("two assets imported through the real bin queue", len(assets) == 2,
          str([a.path.name for a in assets]))
    video_asset = next(a for a in assets if a.path.name.startswith("colours"))
    streams = video_asset.metadata.streams
    check("1440x1080 source streams are editable",
          all(s.editable for s in streams),
          str([(s.stream_index, s.kind.value, str(s.duration)) for s in streams]))

    # ------------------------------------------------------- composition via UI
    G.set_playhead(F(1))
    G.select_streams("colours", (0,))
    G.place_at_playhead()
    G.set_playhead(F(0))
    G.select_streams("tone660", (0,))
    G.place_at_playhead()
    clips = list(win.session.project.clips)
    check("Ctrl+G placed 2 clips at the chosen playheads", len(clips) == 2,
          str([(c.track_id, str(c.output_start), str(c.output_end)) for c in clips]))
    video_clip = next(c for c in clips if c.asset_id == video_asset.id)
    audio_clip = next(c for c in clips if c.asset_id != video_asset.id)
    check("picture starts at 1 s while sound starts at 0 s (independent tracks)",
          video_clip.output_start == F(1) and audio_clip.output_start == F(0),
          f"video {video_clip.output_start}, audio {audio_clip.output_start}")
    check("picture and sound are on different tracks",
          video_clip.track_id != audio_clip.track_id,
          f"{video_clip.track_id} / {audio_clip.track_id}")

    # ------------------------------------ output settings, set through the inspector
    win.inspector.out_width.setText("1280")
    win.inspector.out_height.setText("720")
    win.inspector.out_frame_rate.setText("25")
    win.inspector.out_sample_rate.setText("48000")
    win.inspector.out_layout_box.setCurrentText("stereo")
    win.inspector.output_apply.click()
    out_settings = win.session.project.output
    check("output geometry/rates set explicitly, not taken from an asset",
          (out_settings.width, out_settings.height, out_settings.frame_rate) == (1280, 720, F(25)),
          str(out_settings))

    # ------------------------------------------- discoverability: help and chords
    help_text = win._help_text()
    bound = [chords for _menu, _command, chords in win._chord_rows()]
    check("the in-app help lists every chord this build binds",
          len(bound) >= 15 and all(ch.split(",")[0] in help_text for ch in bound),
          f"{len(bound)} bound chords, workflow steps: "
          f"{len(help_text.splitlines())} lines")
    win.help_action.trigger()
    G.pump(60)
    box = getattr(win, "_help_box", None)
    check("the help dialog opens without holding the window",
          box is not None and not box.isModal() and win.isEnabled(),
          box.windowTitle() if box else "no dialog opened")
    if box is not None:
        box.close()

    # ------------------------------------ explicit output interval for review
    from PySide6.QtWidgets import QLabel

    viewer = win.output_viewer
    hints = [c.text() for c in viewer.findChildren(QLabel)
             if c.objectName() == "preview_range_hint"]
    check("the preview fields say they choose an output interval",
          len(hints) == 1 and "output interval" in hints[0],
          hints[0] if hints else "no explanation next to the range fields")
    viewer.range_start.setText("")
    viewer.sync_render_button()
    check("empty fields mean the playhead window, and the button says so",
          viewer.requested_range() is None
          and viewer.render_button.text() == "Preview around playhead",
          viewer.render_button.text())
    viewer.range_start.setText("0:00:01.000")
    viewer.range_end.setText("0:00:03.000")
    viewer.sync_render_button()
    chosen = viewer.requested_range()
    check("a typed review interval is read back exactly",
          chosen == (F(1), F(3)) and viewer.render_button.text() == "Preview typed range",
          f"{chosen} / {viewer.render_button.text()}")
    viewer.range_start.setText("0:00:00.000")
    viewer.range_end.setText("")
    viewer.sync_render_button()

    # --------------------------------- timeline decorations: waveforms, thumbnails
    from video_editor.ui.timeline import BASE_PPS

    win.timeline.set_view(F(0), BASE_PPS * 4)  # wide enough to be worth decorating
    win.refresh_decorations()
    G.pump(1500)
    material = win.decoration_material()
    check("decoration material is read from the imported streams, not invented",
          len(material) == 2, str(sorted(material)))
    check("a waveform or thumbnail actually arrived for the visible clips",
          win.decorations.cached_bytes > 0,
          f"{win.decorations.cached_bytes} bytes cached, {win.decorations.pending} still queued")
    win.timeline.set_view(F(0), BASE_PPS)

    # ------------------------------- export dialog: what it states and refuses
    from video_editor.ui.export_dialog import RANGE_CUSTOM, RANGE_WHOLE, ExportDialog

    dlg = ExportDialog(win, win.session.project, TMP / "probe.mp4", win.timeline.playhead)
    ok_button = dlg.buttons.button(QDialogButtonBox.StandardButton.Ok)
    summary = dlg.summary.toPlainText()
    check("dialog states duration with exact frame and sample counts",
          "Duration: 5.000s" in summary and "125 frames @ 25/1 fps" in summary
          and f"{125 * SR // 25} samples @ {SR} Hz" in summary,
          summary.splitlines()[0] if summary else "")
    check("dialog states the geometry it will use and the mix policy",
          "1280x720, stereo" in summary and "plain sum" in summary,
          next((ln for ln in summary.splitlines() if "Geometry" in ln), ""))
    check("dialog states its colour handling limits", "colour" in summary.lower(),
          next((ln for ln in summary.splitlines() if "colour" in ln.lower()), ""))
    picture_line = next((ln for ln in summary.splitlines() if "Picture:" in ln), "")
    check("dialog counts picture items and the black fill it adds",
          "2 item(s)" in picture_line and "1 black" in picture_line, picture_line)
    check("Start is enabled for the whole composition", ok_button.isEnabled(), ok_button.text())
    dlg.range_box.setCurrentIndex(dlg.range_box.findData(RANGE_CUSTOM))
    dlg.start_edit.setText("3")
    dlg.end_edit.setText("1")
    check("a reversed render range disables Start and says why",
          not ok_button.isEnabled() and "before its start" in dlg._error_label.text(),
          dlg._error_label.text())
    dlg.start_edit.setText("1")
    dlg.end_edit.setText("3")
    check("a valid custom range re-enables Start", ok_button.isEnabled(), dlg._error_label.text())
    dlg.start_edit.setText("abc")
    check("unparseable range input disables Start", not ok_button.isEnabled(),
          dlg._error_label.text())
    dlg.range_box.setCurrentIndex(dlg.range_box.findData(RANGE_WHOLE))
    dlg.deleteLater()

    # ------------------------------------------------------- export through GUI
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(b"placeholder: must only be replaced after explicit consent")
    answerer = G.ModalAnswerer(destination=OUT, overwrite="Yes")
    spy = G.SignalSpy(win.render_controller)
    answerer.start()
    win._export_composition()  # blocks in the modal loop; the answerer clicks Start
    answerer.stop()
    dialog = answerer.exports[0] if answerer.exports else None
    check("export dialog opened by the Ctrl+E action and Start was clicked",
          dialog is not None and not answerer.rejections,
          (answerer.rejections or [""]) [0])
    for _ in range(600):
        if not win.render_controller.running:
            break
        G.pump(100)
    check("the dialog warned that the destination already exists",
          "exists" in (answerer.exports[0]._error_label.text() if answerer.exports else ""),
          answerer.exports[0]._error_label.text()[:80] if answerer.exports else "")
    check("progress reached 100 % and the result matches the plan",
          bool(spy.progress) and max(spy.progress) >= 0.999 and len(spy.finished) == 1
          and spy.finished[0].duration == F(5),
          f"max progress={max(spy.progress) if spy.progress else None} "
          f"result={getattr(spy.finished[0], 'duration', None) if spy.finished else None} "
          f"status={G.status_text()[:60]}")
    check("the export reported no failure or cancellation",
          not spy.failed_messages and not spy.cancelled, str(spy.failed_messages))
    check("no unexpected modal dialogs appeared", not answerer.unexpected,
          str(answerer.unexpected))
    check("replacing an existing file required explicit consent",
          len(answerer.questions) == 1, (answerer.questions[0][:70] if answerer.questions else ""))
    check("export produced the file", OUT.exists(),
          str(OUT.stat().st_size) + " bytes" if OUT.exists() else "missing")

    ff = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:stream=codec_type,codec_name,width,height,channels,"
                            "sample_rate,r_frame_rate", "-of", "default=nw=1", str(OUT)],
                           capture_output=True, text=True, timeout=120)
    fields = ff.stdout
    check("container is MP4/H.264/AAC as offered", "codec_name=h264" in fields
          and "codec_name=aac" in fields, " ".join(fields.split())[:120])
    check("output geometry and rates are the project's own",
          "width=1280" in fields and "height=720" in fields
          and "sample_rate=48000" in fields and "channels=2" in fields,
          " ".join(fields.split())[:160])
    duration_lines = [ln for ln in fields.splitlines() if ln.startswith("duration=")]
    check("ffprobe reports a duration for the produced file", bool(duration_lines),
          " ".join(fields.split())[:160] or ff.stderr[:160])
    produced = (F(duration_lines[0].split("=", 1)[1]).limit_denominator(10 ** 6)
                if duration_lines else None)
    planned = max(c.output_end for c in win.session.project.clips)
    check("produced duration matches the plan within 1 frame + AAC padding",
          produced is not None and abs(produced - planned) <= F(1, 25) + F(2048, 48000),
          f"plan {float(planned)}s, produced {float(produced) if produced is not None else None}s")
    check("status bar reports the finished export",
          OUT.name in G.status_text() or "Exported" in G.status_text(), G.status_text()[:100])

    if produced is None:
        check("exported media is measurable", False, "no readable export file")
    else:
        # ---------------------------- picture correctness, measured per frame
        check("frame 0 is black: no picture exists before output 1 s",
              close_to(frame_at(OUT, 0), (0, 0, 0), 16), str(frame_at(OUT, 0)))
        first = frame_at(OUT, 25)
        check("output frame 25 (t=1.000s) is the source's first colour",
              close_to(first, palette["red"], 22), f"{first} vs {palette['red']}")
        check("output frame 50 (t=2.000s) is the source's second colour",
              close_to(frame_at(OUT, 50), palette["green"], 22), str(frame_at(OUT, 50)))
        check("output frame 100 (t=4.000s) is the source's fourth colour",
              close_to(frame_at(OUT, 100), palette["magenta"], 22), str(frame_at(OUT, 100)))
        check("the 16:9 output letterboxes the 4:3 source with black side bars",
              close_to(frame_at(OUT, 50, x=40, y=500), (0, 0, 0), 16)
              and not close_to(frame_at(OUT, 50, x=700, y=500), (0, 0, 0), 16),
              "bar vs centre")
        check("output is constant frame rate", "r_frame_rate=25/1" in fields,
              next((ln for ln in fields.splitlines() if "r_frame_rate" in ln), ""))

        # ----------------------- audio: the tone where its clip is, silence in the gap
        inside = tone_share(OUT, F(1, 4), F(1, 2))
        gap = tone_share(OUT, F(9, 4), F(1, 2))
        check("the 660 Hz tone is in the mix while its clip runs",
              inside > 0.5, f"share={inside:.3f}")
        check("the audio gap after that clip is silent, not feedback from the picture",
              gap < 0.05, f"share={gap:.3f}")
        check("the tone reaches the mix at its clip gain", rms(pcm(OUT, F(1), F(1))) > 0.05,
              f"rms={rms(pcm(OUT, F(1), F(1))):.3f}")

    # --------------------------------------------- gestures: click, keys, drags
    window_is_active = G.ensure_active()
    check("the window is active, so window shortcuts can be tested honestly",
          window_is_active, f"activeWindow={G.app().activeWindow() is win}")
    row_v = G.row_of(video_clip.track_id)
    G.click_timeline(F(3), row_v)
    check("clicking a clip in the timeline selects it",
          win.timeline.selected_clips == (video_clip.id,), str(win.timeline.selected_clips))
    snapshot = win.session.project
    playhead_before = win.timeline.playhead
    G.key_press(Qt.Key_Right, target=win.timeline)
    check("arrow keys move the playhead and never edit the composition",
          win.timeline.playhead > playhead_before and win.session.project == snapshot,
          f"playhead {float(playhead_before)} -> {float(win.timeline.playhead)}, "
          f"project unchanged={win.session.project == snapshot}")

    win.inspector.output_start.setText("1.4")
    win.inspector.apply_button.click()
    placed = win.session.project.clip(video_clip.id)
    check("inspector numeric entry places the clip exactly",
          placed.output_start == F(14, 10) and placed.source_out - placed.source_in == F(4),
          f"start={placed.output_start} src={placed.source_in}..{placed.source_out}")
    # A chord claimed twice is *ambiguous* to Qt, and an ambiguous shortcut is
    # silently never triggered. This happens by platform: `StandardKey.Redo` is
    # Ctrl+Shift+Z under a Wayland/Gtk theme, i.e. the same chord as the
    # documented one. Named here so the failure reads "ambiguous chord" instead of
    # appearing as a mysteriously dead key later in the run.
    chords: dict[str, str] = {}
    ambiguous: list[str] = []
    for action in list(win.actions()) + win.findChildren(QAction):
        for sequence in action.shortcuts():
            text = sequence.toString()
            if not text:
                continue
            owner = chords.get(text)
            if owner is not None and owner != action.text():
                ambiguous.append(f"{text}: {owner} / {action.text()}")
            chords[text] = action.text()
    check("no shortcut chord is claimed by two different actions", not ambiguous,
          "; ".join(ambiguous))
    check("the documented chords are the ones the window really has",
          chords.get("Ctrl+Shift+Z") == win.redo_action.text()
          and chords.get("Ctrl+Z") == win.undo_action.text(),
          f"Ctrl+Shift+Z -> {chords.get('Ctrl+Shift+Z')} · "
          f"Ctrl+Z -> {chords.get('Ctrl+Z')}")
    undo_keys = G.shortcut_keys(win.undo_action)
    redo_keys = G.shortcut_keys(win.redo_action)
    G.key_press(*undo_keys)
    check(f"{win.undo_action.shortcut().toString()} undoes that placement",
          win.session.project.clip(video_clip.id).output_start == F(1),
          f"start={win.session.project.clip(video_clip.id).output_start} "
          f"undo enabled={win.undo_action.isEnabled()} can_undo={win.session.can_undo} "
          f"focus={type(win.focusWidget()).__name__}")
    G.key_press(*redo_keys)
    check(f"{win.redo_action.shortcut().toString()} re-applies it",
          win.session.project.clip(video_clip.id).output_start == F(14, 10),
          str(win.session.project.clip(video_clip.id).output_start))
    G.key_press(*undo_keys)

    row_a = G.row_of(audio_clip.track_id)
    before = G.clip_span(audio_clip.id)
    mid = (before[0] + before[1]) / 2
    G.drag_timeline(mid, row_a, mid + F(7, 2), row_a)
    after = G.clip_span(audio_clip.id)
    shift = None if after is None else after[0] - before[0]
    check("dragging an audio clip's body moves it and keeps its length",
          shift is not None and shift > F(3) and after[1] - after[0] == before[1] - before[0],
          f"{before} -> {after}")
    G.key_press(*undo_keys)
    check("undo restores the dragged clip exactly", G.clip_span(audio_clip.id) == before,
          str(G.clip_span(audio_clip.id)))

    video_row = G.row_of(video_clip.track_id)
    state_before = win.session.project
    G.drag_timeline(mid, row_a, mid, video_row)
    stayed = win.session.project.clip(audio_clip.id)
    check("dragging an audio clip onto the video track keeps it where it was",
          stayed is not None and stayed.track_id == audio_clip.track_id
          and win.session.project == state_before,
          f"clip on {stayed.track_id if stayed else None}, project unchanged="
          f"{win.session.project == state_before}")

    video_span = G.clip_span(video_clip.id)
    G.drag_timeline(video_span[0], row_v, video_span[0] + 1, row_v, dx_from=4)
    edge = win.session.project.clip(video_clip.id)
    check("dragging a clip edge trims instead of moving",
          edge is not None and edge.source_in > video_clip.source_in
          and edge.output_start > video_span[0] and edge.output_end == video_span[1],
          f"in={edge.source_in if edge else None} start={edge.output_start if edge else None} "
          f"end={edge.output_end if edge else None} (was {video_span})")
    G.key_press(*undo_keys)

    G.set_playhead(F(3, 2))
    audio_span = G.clip_span(audio_clip.id)
    # dx is inward (-4): a right-edge grab 4 px *past* the clip is outside the
    # clip's rect, so the hit test finds nothing and no gesture starts.
    G.drag_timeline(audio_span[1], row_a, F(3, 2), row_a, dx_from=-4, dx_to=3)
    snapped = win.session.project.clip(audio_clip.id)
    check("a trimmed edge released near the playhead snaps exactly onto it",
          snapped is not None and snapped.output_end == F(3, 2),
          f"audio out={snapped.output_end if snapped else None} (target 3/2, was {audio_span})")
    G.key_press(*undo_keys)
    check("composition is back to the exported state",
          G.clip_span(video_clip.id) == (F(1), F(5)) and G.clip_span(audio_clip.id) == (F(0), F(2)),
          f"video {G.clip_span(video_clip.id)} audio {G.clip_span(audio_clip.id)}")

    # ------------------------------------- hiding a track shortens nothing
    track = win.session.project.track(video_clip.track_id)
    win._on_track_toggle(track.id, "enable")
    check("disabling the picture track does not change output duration",
          max(c.output_end for c in win.session.project.clips) == F(5),
          str(max(c.output_end for c in win.session.project.clips)))
    win._on_track_toggle(track.id, "enable")

    # ------------------------------- composition preview, stills and cache lifetime
    preview_checks(win, palette, OUT, video_clip)

    # --------------------------- project files, relinking and recovery (TASK_07)
    saved = (
        win.session.project,
        {c.id for c in win.session.project.clips},
        {a.id for a in win.bin_controller.assets},
        win.timeline.playhead,
        win.timeline.pps,
        video_asset.id,
    )
    try:
        win = project_checks(win, fixtures, palette, OUT, saved)
    except Exception as exc:
        check("the project checks ran to the end without an exception", False,
              f"{type(exc).__name__}: {exc}")
    G.pump(300)

    # ------------------------------------------------------------- cleanup
    # the unsaved-work prompt was already exercised, with its own answerer; the
    # final close must not wait for a dialog nobody is going to answer
    win.quit_confirm_required = False
    G.pump(400)
    leftovers = sorted(p.name for p in TMP.iterdir()
                       if ".partial." in p.name or ".chunk" in p.name)
    check("no temporary render files left behind", not leftovers, str(leftovers))

    # ------------------------------------------------------ third-party playback
    log = subprocess.run(["cvlc", "--play-and-exit", "--intf", "dummy", "--vout", "dummy",
                          "--aout", "dummy", "--verbose", "2", str(OUT)],
                         capture_output=True, text=True, timeout=180)
    text = (log.stderr + log.stdout).lower()
    check("VLC demuxed the MP4 with its own demuxer", "mp4 demux" in text,
          next((ln for ln in text.splitlines() if "mp4 demux" in ln), "")[:100])
    check("VLC started the H.264 picture decoder", "codec (h264) started" in text
          or "h264 decoder" in text,
          next((ln for ln in text.splitlines() if "h264" in ln and "decoder" in ln), "")[:100])
    check("VLC played through to the end", "end of playlist" in text or "end of stream" in text,
          f"exit {log.returncode}")
    check("VLC reported no corrupt/decode errors",
          "corrupt" not in text and "decode error" not in text, "")

    if len(sys.argv) > 1 and sys.argv[1] == "show":
        win.setWindowTitle("Movie Mixer — acceptance run finished (inspect me)")
        G.pump(90_000)
    else:
        win.close()
        G.pump(400)

    failed = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} acceptance checks passed", flush=True)
    for _, label, detail in failed:
        print(f"  FAILED: {label} [{detail}]", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
