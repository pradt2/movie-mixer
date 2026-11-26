"""A two-hour, many-hundred-clip project: responsiveness and bounded resources.

The concern this answers is specific: an editor that works on a 12-second A/B
composition can still fall over on a feature-length timeline — a timeline that
repaints every clip in the project, a planner that copies the whole timeline for
one selected window, a cache that grows with the timeline, or one widget per
clip. So this builds a project of the awkward size (two hours of output, hundreds
of clips, three tracks, mixed rates) from real probed media, and measures:

* how long whole-timeline planning takes, and that a **selected render window**
  is planned from that timeline without paying for all of it;
* how long a repaint costs at three zoom levels, and that the timeline is painted
  rather than populated with widgets;
* that clip hit-testing, playhead moves, an edit plus its undo, and saving and
  reopening stay usable;
* that the decoration caches and the job queue stay inside their budgets;
* how many child processes and threads are alive, and the peak memory.

Nothing here renders two hours of film: the full-length *plan* is exercised, plus
one 10-second render window deep in the timeline, which is what a review pass
would actually ask for.

Run it unattended::

    python scripts/long_project_check.py
"""

from __future__ import annotations

import resource
import shutil
import subprocess
import sys
import threading
import time
from fractions import Fraction as F
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import driver as G
from editions import Report, register_streams

from video_editor.core.commands import AddClipCommand, MoveClipCommand
from video_editor.core.model import OutputSettings, Project, Track, TrackKind
from video_editor.media.framegrab import extract_composition_still
from video_editor.project.storage import load_document
from video_editor.render.exporter import RenderRequest, probe_duration
from video_editor.render.plan import build_plan
from video_editor.ui.decorations import ENVELOPE_BUDGET, MAX_QUEUED_JOBS, STRIP_BUDGET

TWO_HOURS = F(7200)
SOURCE_SECONDS = 120
OUTPUT_RATE = F(25)


# ----------------------------------------------------------------- the fixtures
def make_long_media(directory: Path) -> tuple[Path, Path]:
    """Two two-minute sources: enough material for hundreds of clip ranges.

    ``testsrc2`` rather than a solid colour: the decoder has real work to do, so a
    timing measurement here is not measuring a degenerate stream.
    """
    directory.mkdir(parents=True, exist_ok=True)
    film = directory / "long_video.mkv"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=128x96:rate={int(OUTPUT_RATE)}"
                              f":duration={int(SOURCE_SECONDS)}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={int(SOURCE_SECONDS)}"
                              f":sample_rate=48000",
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-g", str(int(OUTPUT_RATE)), "-c:a", "pcm_s16le", str(film)],
        check=True, capture_output=True, timeout=600)
    tone = directory / "long_tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=660:duration="
                              f"{int(SOURCE_SECONDS)}:sample_rate=48000",
         "-c:a", "pcm_s16le", str(tone)],
        check=True, capture_output=True, timeout=600)
    return film, tone


def long_project(win, film: Path, tone: Path) -> tuple[Project, dict[str, Path]]:
    """A two-hour output built from ordinary commands on three tracks."""
    G.import_files([film, tone])
    by_path = {Path(asset.path): asset.id for asset in win.bin_controller.assets}
    film_id, tone_id = by_path[film], by_path[tone]

    project = Project(output=OutputSettings(128, 96, OUTPUT_RATE, 48000, "stereo"))
    project = register_streams(project, [(film_id, film), (tone_id, tone)])
    project = project.with_tracks((
        Track("V1", TrackKind.VIDEO, "V1"),
        Track("A1", TrackKind.AUDIO, "A1 (dub)"),
        Track("A2", TrackKind.AUDIO, "A2 (commentary)"),
    ))

    out, index = F(0), 0
    while out < TWO_HOURS:
        span = F(12 + (index % 7) * 2)  # 12..24 output seconds
        rate = F(25, 24) if index % 17 == 0 else F(1)
        source = F((index * 7) % 80)
        project = AddClipCommand(
            film_id, 0, "V1", out, source_in=source, source_out=source + span * rate,
            rate=rate, clip_id=f"v{index}").apply(project)
        project = AddClipCommand(
            film_id, 1, "A1", out, source_in=source, source_out=source + span,
            clip_id=f"a{index}").apply(project)
        if index % 3 == 0:  # a second audio track, overlapping the first
            project = AddClipCommand(
                tone_id, 0, "A2", out + span / 2, source_in=source,
                source_out=source + span / 2, clip_id=f"b{index}").apply(project)
        out += span + F(2)
        index += 1
    return project, {film_id: film, tone_id: tone}


# ------------------------------------------------------------------- measuring
def timed(action) -> tuple[object, float]:
    started = time.perf_counter()
    result = action()
    return result, time.perf_counter() - started


def median_of(action, runs: int = 3) -> float:
    samples = []
    for _ in range(runs):
        _value, seconds = timed(action)
        samples.append(seconds)
    return sorted(samples)[len(samples) // 2]


def child_commands() -> list[str]:
    """Live child processes of this process, as command lines, read from /proc."""
    commands: list[str] = []
    base = Path("/proc/self/task")
    try:
        threads = list(base.iterdir())
    except (OSError, FileNotFoundError):  # pragma: no cover - non-Linux
        return commands
    for thread in threads:
        try:
            children = (thread / "children").read_text().split()
        except (OSError, FileNotFoundError):
            continue
        for pid in children:
            try:
                raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            except OSError:
                continue
            commands.append(raw.replace(b"\0", b" ").decode(errors="replace").strip())
    return commands


def writers(commands: list[str], *destinations: Path) -> list[str]:
    """Children writing to one of these destinations — a render, not a probe.

    Matched on the destination's **stem** because a render writes
    ``name.<token>.partial.<ext>`` first and only publishes the final name, so a
    full-path match would miss the very process being looked for.

    Counting *every* child measures scheduling luck: a 15-second thumbnail decode,
    the encoder-availability query, the capability preflight's ``ffprobe`` and the
    frame index's ``-show_frames`` reads all overlap a render by design, and how
    many happen to be alive at one instant varies from run to run. What must not
    vary is that **one render is one process**, however many clips it covers, so
    that is what the render checks assert; the total is still capped, because a
    fan-out per clip or track on a 843-clip project would blow through a small
    constant immediately.
    """
    stems = [destination.stem for destination in destinations]
    return [command for command in commands
            if any(stem in command for stem in stems)]


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def check_responsiveness(win, project: Project, report: Report) -> None:
    """Everything the user touches on a long timeline, timed."""
    from PySide6.QtWidgets import QWidget

    timeline = win.timeline
    widgets = len(timeline.findChildren(QWidget))
    report.check("the timeline is painted, not populated with one widget per clip",
                 widgets < 40, f"{widgets} child widgets for {len(project.clips)} clips")

    for offset, pps, label in ((F(0), F(1, 4), "whole two hours in view"),
                               (F(3600), F(1), "one hour in view"),
                               (F(3600), F(50), "24 seconds in view, mid-film")):
        timeline.set_view(offset, pps)
        G.pump(100)
        seconds = median_of(lambda v=timeline: v.viewport().grab())
        shown = timeline.view_range()
        report.check(f"repaint of the timeline stays responsive ({label})", seconds < 0.5,
                     f"median repaint {seconds * 1000:.1f} ms at pps={pps}, "
                     f"view {float(shown[0]):.0f}-{float(shown[1]):.0f} s")

    def hit_test():
        for x in range(120, 1200, 7):
            timeline.clip_at_pixel(x, 40)

    _hits, seconds = timed(hit_test)
    report.check("clip hit-testing across a long timeline stays responsive", seconds < 0.5,
                 f"{(1080 // 7)} hit tests in {seconds * 1000:.1f} ms")

    def move_playhead():
        for frame in range(0, 200):
            timeline.set_playhead(F(frame, 25) + F(5000))

    _value, seconds = timed(move_playhead)
    report.check("moving the playhead across the whole timeline stays responsive",
                 seconds < 1.0, f"200 playhead moves in {seconds * 1000:.1f} ms")

    target = next(c for c in project.clips if c.id == "a100")
    moved = MoveClipCommand(target.id, output_start=target.output_start + F(1, 2))

    def edit_and_undo():
        win._apply(moved)
        win._undo()

    _value, seconds = timed(edit_and_undo)
    report.check("an edit and its undo on a hundreds-of-clips project stays responsive",
                 seconds < 1.0, f"{len(project.clips)} clips, {seconds * 1000:.1f} ms "
                 "for an edit plus its undo")


def check_planning(project: Project, paths: dict[str, Path], report: Report) -> None:
    """Full-length planning is affordable; a selected window must be cheaper."""
    full, full_seconds = timed(lambda: build_plan(project, F(0), None))
    report.check("the whole two-hour timeline can be planned",
                 full.total_frames == int(project.output_end() * OUTPUT_RATE),
                 f"{full.total_frames} frames, {len(full.video_items)} picture items, "
                 f"{len(full.audio_items)} sound items in {full_seconds:.2f} s")
    report.check("whole-timeline planning stays within a sane budget", full_seconds < 60,
                 f"{full_seconds:.2f} s")

    window, window_seconds = timed(lambda: build_plan(project, F(3600), F(3610)))
    report.check("a selected render window mid-timeline is planned from the same "
                 "timeline without paying for all of it",
                 window.total_frames == int(F(10) * OUTPUT_RATE)
                 and len(window.video_items) + len(window.audio_items) <= 30
                 and window_seconds < full_seconds,
                 f"{window.total_frames} frames, "
                 f"{len(window.video_items) + len(window.audio_items)} items in "
                 f"{window_seconds * 1000:.1f} ms (whole timeline: {full_seconds:.2f} s)")

    deep, deep_seconds = timed(
        lambda: extract_composition_still(project, paths, F(5400), geometry_scale=F(1, 2)))
    report.check("a still frame can be inspected an hour and a half into the timeline",
                 bool(deep.rgb) and deep.output_time == F(5400),
                 f"{len(deep.rgb)} pixels in {deep_seconds:.2f} s")


def check_render_window(win, project: Project, paths: dict[str, Path],
                        report: Report) -> None:
    """One 10-second review window deep in the timeline, rendered and measured."""
    target = G.WORKDIR / "long-window.mp4"
    if target.exists():
        target.unlink()
    request = RenderRequest(project=project, asset_paths=paths, destination=target,
                            out_start=F(3600), out_end=F(3610), overwrite=True)
    started = win.render_controller.start(request)
    report.check("a render window mid-timeline is accepted", started,
                 "" if started else "the controller refused the request")
    peak_children = peak_writers = 0
    busiest: list[str] = []
    deadline = 9000
    while win.render_controller.running and deadline:
        live = child_commands()
        peak_writers = max(peak_writers, len(writers(live, target)))
        if len(live) > peak_children:
            peak_children, busiest = len(live), live
        G.pump(100)
        deadline -= 1
    report.check("the mid-timeline window rendered", target.is_file(), str(target))
    if target.is_file():
        produced = probe_duration(shutil.which("ffprobe"), target)
        report.check("the window is the length that was asked for, not the whole timeline",
                     abs(produced - F(10)) < F(1, 4),
                     f"{float(produced):.3f} s produced for a 10 s request")
    report.check("one render is one FFmpeg process however many clips it spans",
                 peak_writers <= 1, f"peak {peak_writers} process(es) writing the window")
    report.check("background children stay bounded however long the timeline is",
                 peak_children <= 4,
                 f"peak {peak_children} child process(es): "
                 f"{[Path(part.split()[0]).name for part in busiest]}")


def check_caches_and_footprint(win, project: Project, report: Report) -> None:
    """Caches are bounded by budget, not by the length of the timeline."""
    win.refresh_decorations()
    G.pump(1200)
    budget = ENVELOPE_BUDGET + STRIP_BUDGET
    report.check("decoration caches stay inside their byte budget on a long timeline",
                 win.decorations.cached_bytes <= budget,
                 f"{win.decorations.cached_bytes} bytes held against a {budget} byte "
                 f"budget, {len(project.clips)} clips")
    report.check("the decoration job queue stays bounded however long the timeline is",
                 win.decorations.pending <= MAX_QUEUED_JOBS,
                 f"{win.decorations.pending} queued against a limit of {MAX_QUEUED_JOBS}")
    # Qt worker threads are invisible to `threading.enumerate()`, so the process's
    # own thread list is counted: the claim being tested is a bounded pool, not
    # "no threads at all".
    task_dir = Path("/proc/self/task")
    os_threads = len(list(task_dir.iterdir())) if task_dir.is_dir() else 0
    python_threads = [t.name for t in threading.enumerate() if t is not threading.main_thread()]
    report.check("background work does not grow one thread per clip or per track",
                 os_threads <= 16,
                 f"{os_threads} OS threads in this process "
                 f"({len(python_threads)} of them Python threads: {python_threads})")
    # Queued decoration jobs may legitimately still be decoding here, so this is not
    # "nothing is running": it is "the pool of background FFmpeg work is a small
    # constant, not one process per clip" — 843 clips would break a cap of 4 at once.
    live = child_commands()
    report.check("background FFmpeg work stays a small constant, not one process per clip",
                 len(live) <= 4,
                 f"{len(live)} child process(es): "
                 f"{[Path(part.split()[0]).name for part in live]}")


def run() -> int:
    report = Report()
    media = G.WORKDIR / "long-media"
    print(f"scratch: {G.WORKDIR}")
    _film_build, build_seconds = timed(lambda: make_long_media(media))
    film, tone = media / "long_video.mkv", media / "long_tone.wav"
    print(f"sources: {film}, {tone} ({int(SOURCE_SECONDS)} s each, "
          f"built in {build_seconds:.1f} s)")

    win = G.window()
    project, paths = long_project(win, film, tone)
    print(f"project: {len(project.clips)} clips, {len(project.tracks)} tracks, output "
          f"{float(project.output_end()):.1f} s ({float(project.output_end()) / 3600:.2f} h)")

    report.check("the project really is long and crowded",
                 len(project.clips) >= 300 and project.output_end() >= TWO_HOURS,
                 f"{len(project.clips)} clips, {float(project.output_end()):.0f} s")

    # the bin already holds the imported editions: the window takes the
    # composition onto that same catalogue, as one document
    win.install_document(win.session.document.with_project(project))
    G.pump(500)

    print("\nplanning")
    check_planning(project, paths, report)

    print("\nresponsiveness")
    check_responsiveness(win, project, report)

    print("\ncaches and footprint")
    check_caches_and_footprint(win, project, report)

    print("\nrender window mid-timeline")
    check_render_window(win, project, paths, report)

    print("\npersistence")
    document = G.WORKDIR / "long-project.vep"
    if document.exists():
        document.unlink()
    saved, save_seconds = timed(lambda: win._save_project(path=document))
    reopened, load_seconds = timed(lambda: load_document(document))
    report.check("a project of this size saves and reads back intact",
                 saved and reopened is not None
                 and len(reopened.project.clips) == len(project.clips),
                 f"saved in {save_seconds:.2f} s, read in {load_seconds:.2f} s, "
                 f"{document.stat().st_size} bytes")

    G.close_window()
    print(f"\npeak memory of this check: {peak_rss_mb():.0f} MiB")
    print(f"{'FAILED' if report.failures else 'OK'}: {report.failures} failed check(s)")
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
