"""Composition preview through the window: stale marking, reuse, stills, shutdown.

Qt tests stay headless (offscreen platform) and never touch real films: fixtures
are the synthetic solid-colour/sine files from ``media_fixtures``.
"""

from __future__ import annotations

import threading
from fractions import Fraction
from pathlib import Path

import pytest

from gui_helpers import install_document, media_record, offline_asset
from media_fixtures import HAS_FFMPEG, make_colour_steps, make_tone
from video_editor.core.commands import (
    AddClipCommand,
    MoveClipCommand,
    RegisterSourceCommand,
    RenameTrackCommand,
)
from video_editor.core.model import OutputSettings, Project, StreamSource, Track, TrackKind
from video_editor.media.framegrab import StillFrame
from video_editor.render.exporter import RenderCancelled, RenderError, RenderRequest
from video_editor.render.preview import PreviewError
from video_editor.ui import render_worker as worker_module
from video_editor.ui.main_window import MainWindow

F = Fraction
ZERO = F(0)


def composition() -> Project:
    """4 s composition: picture 0-4 s (colour steps), 660 Hz tone 1-3 s."""
    p = Project(output=OutputSettings(128, 96, F(25), 48000, "stereo"))
    p = RegisterSourceCommand(
        StreamSource("steps", 0, TrackKind.VIDEO, ZERO, F(6), frame_rate=F(25),
                     width=128, height=96, stream_position=0)
    ).apply(p)
    p = RegisterSourceCommand(
        StreamSource("t660", 0, TrackKind.AUDIO, ZERO, F(6), sample_rate=48000,
                     stream_position=0)
    ).apply(p)
    p = p.with_tracks((Track("v", TrackKind.VIDEO, "V1"), Track("a", TrackKind.AUDIO, "A1")))
    p = AddClipCommand("steps", 0, "v", ZERO, source_in=ZERO, source_out=F(4),
                       clip_id="vid").apply(p)
    p = AddClipCommand("t660", 0, "a", F(1), source_in=ZERO, source_out=F(2),
                       clip_id="aud").apply(p)
    return p


def _stub(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"not media")
    return path


@pytest.fixture()
def window(qapp_instance, tmp_path, monkeypatch):
    """Window whose preview cache is confined to this test's tmp directory."""
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / "previews"))
    win = MainWindow()
    steps = make_colour_steps(tmp_path) if HAS_FFMPEG else _stub(tmp_path, "steps.mkv")
    tone = make_tone(tmp_path, "t660.wav", 660) if HAS_FFMPEG else _stub(tmp_path, "t660.wav")
    # one document: the composition and the media it references, as a saved file
    # would hold them
    install_document(
        win, composition(), (media_record(steps, "steps"), media_record(tone, "t660"))
    )
    win.show()
    yield win, tmp_path, steps, tone
    win.close()


def _bare(qapp_instance, monkeypatch, tmp_path) -> MainWindow:
    """A window with no media: for range-validation and message tests."""
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / "previews-unused"))
    win = MainWindow()
    install_document(
        win, composition(), (offline_asset("steps"), offline_asset("t660"))
    )
    return win


# ------------------------------------------------------------------ range entry


def test_range_fields_track_the_composition_until_edited(qapp_instance, monkeypatch, tmp_path):
    win = _bare(qapp_instance, monkeypatch, tmp_path)
    assert win.output_viewer.range_start.text() == "0:00:00.000"
    assert win.output_viewer.range_end.text() == "0:00:04.000"
    win.output_viewer.range_end.setText("0:00:02.000")
    win.output_viewer.mark_range_edited()
    win._refresh_timeline()  # must not overwrite what the user typed
    assert win.output_viewer.range_end.text() == "0:00:02.000"
    win.close()


def test_reversed_range_is_refused_without_rendering(qapp_instance, monkeypatch, tmp_path):
    win = _bare(qapp_instance, monkeypatch, tmp_path)
    started: list = []
    monkeypatch.setattr(win, "_on_preview_requested", lambda *a: started.append(a))
    win.output_viewer.range_start.setText("0:00:03.000")
    win.output_viewer.range_end.setText("0:00:01.000")
    win.output_viewer._on_render_clicked()
    assert started == []
    assert "must be after its start" in win.output_viewer.state.text()
    win.close()


def test_unparseable_range_says_what_is_wrong(qapp_instance, monkeypatch, tmp_path):
    win = _bare(qapp_instance, monkeypatch, tmp_path)
    win.output_viewer.range_start.setText("nine o clock")
    win.output_viewer._on_render_clicked()
    assert "not understood" in win.output_viewer.state.text()
    win.close()


# ----------------------------------------------------------------------- states


def test_an_edit_marks_the_rendered_preview_stale(window):
    win, _tmp, _steps, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    win.output_viewer.range_start.setText("0:00:01.000")
    win.output_viewer.range_end.setText("0:00:03.000")
    win.output_viewer.mark_range_edited()
    win.output_viewer._on_render_clicked()
    assert win.preview_controller.wait_finished(120_000)
    assert win.output_viewer.state.text().startswith("Preview is current")
    assert win.output_viewer.play_button.isEnabled()

    assert win._apply(MoveClipCommand("vid", output_start=F(1, 25)))
    assert win.output_viewer.state.text().startswith("STALE")
    assert "Composition changed" in win.output_viewer.state.text()
    assert not win.output_viewer.play_button.isEnabled()

    # rebuilding reflects the edit instead of serving the old file again
    win.output_viewer._on_render_clicked()
    assert win.preview_controller.wait_finished(120_000)
    assert win.output_viewer.state.text().startswith("Preview is current")
    assert len(win.preview_cache.entries()) == 2  # old range and edited range coexist


def test_a_rename_that_cannot_change_the_media_does_not_go_stale(window):
    win, _tmp, _steps, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    win.output_viewer.range_start.setText("0:00:00.000")
    win.output_viewer.range_end.setText("0:00:01.000")
    win.output_viewer.mark_range_edited()
    win.output_viewer._on_render_clicked()
    assert win.preview_controller.wait_finished(120_000)
    assert win._apply(RenameTrackCommand("v", "Picture from A"))
    assert win.output_viewer.state.text().startswith("Preview is current")


def test_an_identical_request_is_served_from_the_cache(window):
    win, _tmp, _steps, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    win.output_viewer.range_start.setText("0:00:02.000")
    win.output_viewer.range_end.setText("0:00:03.000")
    win.output_viewer.mark_range_edited()
    win.output_viewer._on_render_clicked()
    assert win.preview_controller.wait_finished(120_000)
    first_path = Path(win._preview[4])
    first_mtime = first_path.stat().st_mtime_ns

    win._apply(MoveClipCommand("vid", output_start=F(2, 25)))  # stale...
    win._apply(MoveClipCommand("vid", output_start=ZERO))  # ...and back again
    win.output_viewer._on_render_clicked()
    assert not win.preview_controller.running  # reused: nothing was rendered
    assert Path(win._preview[4]) == first_path
    assert first_path.stat().st_mtime_ns == first_mtime
    assert win.output_viewer.state.text().startswith("Preview reused from cache")


def test_empty_fields_use_a_window_around_the_playhead(window):
    win, _tmp, _steps, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    win.output_viewer.range_start.setText("")
    win.output_viewer.range_end.setText("")
    win.timeline.set_playhead(F(3))
    win.output_viewer._on_render_clicked()
    assert win.preview_controller.wait_finished(120_000)
    _key, start, end, scale, path, _gen = win._preview
    assert (start, end) == (ZERO, F(4))  # the default window, clamped to the output
    assert scale == F(1, 2)  # the default preview picture scale
    assert path.is_file()


def test_preview_render_failure_is_visible_not_silent(qapp_instance, monkeypatch, tmp_path):
    win = _bare(qapp_instance, monkeypatch, tmp_path)
    win._asset_paths = lambda: {}  # assets are unknown here; the failure is injected
    monkeypatch.setattr(
        worker_module, "render", _raise(RenderError("this FFmpeg cannot encode h264"))
    )
    monkeypatch.setattr(win, "_preview_state_ok", lambda: None)
    win.output_viewer.range_start.setText("0:00:00.000")
    win.output_viewer.range_end.setText("0:00:01.000")
    win.output_viewer.mark_range_edited()
    win.output_viewer._on_render_clicked()
    assert win.preview_controller.wait_finished(20_000)
    assert "FAILED" in win.output_viewer.state.text()
    assert "h264" in win.output_viewer.state.text()
    win.close()


def test_an_unrenderable_range_reports_the_reason(qapp_instance, monkeypatch, tmp_path):
    win = _bare(qapp_instance, monkeypatch, tmp_path)
    monkeypatch.setattr(win, "_preview_state_ok", lambda: None)

    def refuse(*args, **kwargs):
        raise PreviewError("Preview range end is before its start")

    monkeypatch.setattr("video_editor.ui.main_window.prepare_preview", refuse)
    win.output_viewer.range_start.setText("0:00:00.000")
    win.output_viewer.range_end.setText("0:00:01.000")
    win.output_viewer.mark_range_edited()
    win.output_viewer._on_render_clicked()
    assert "FAILED" in win.output_viewer.state.text()
    win.close()


def _raise(exc):
    def fake_render(request, on_progress=None, cancel_event=None):
        raise exc

    return fake_render


def test_cancelling_a_preview_leaves_no_file_and_clears_the_state(window, monkeypatch):
    win, tmp, _steps, _tone = window
    monkeypatch.setattr(worker_module, "render", _raise(RenderCancelled("cancelled")))
    win.output_viewer.range_start.setText("0:00:00.000")
    win.output_viewer.range_end.setText("0:00:02.000")
    win.output_viewer.mark_range_edited()
    win.output_viewer._on_render_clicked()
    assert win.preview_controller.wait_finished(20_000)
    assert win.output_viewer.state.text().startswith("Preview cancelled")
    assert win._preview is None
    assert list((tmp / "previews").glob("preview-*")) == []


# ------------------------------------------------------------------- stills


def test_playing_a_preview_starts_at_the_playhead_and_seeks_when_it_moves(window, monkeypatch):
    """Play, pause and seek act on the rendered range, mapped from output time."""
    from PySide6.QtMultimedia import QMediaPlayer

    win, tmp, _steps, _tone = window
    calls: list[tuple] = []

    class FakePlayer:
        """Stands in for Qt's player: records what the window asks for."""

        def stop(self) -> None:
            calls.append(("stop", None))

        def setSource(self, url) -> None:
            calls.append(("source", url.toLocalFile()))

        def play(self) -> None:
            calls.append(("play", None))

        def setPosition(self, milliseconds: int) -> None:
            calls.append(("position", milliseconds))

    monkeypatch.setattr(win, "preview_player", FakePlayer())
    preview_file = tmp / "preview.mp4"
    preview_file.write_bytes(b"not really media, only a name here")
    win.output_viewer.preview_ready(preview_file, F(1), F(4), F(1, 2))
    win._preview = ("key", F(1), F(4), F(1, 2), preview_file, -1)

    win.timeline.set_playhead(F(5, 2))  # 1.5 s into the rendered range
    win.output_viewer.play_button.click()
    assert ("source", str(preview_file)) in calls and ("play", None) in calls
    # a position can only be applied once the file is loaded (Qt's handshake)
    win._on_preview_media_status(QMediaPlayer.MediaStatus.LoadedMedia)
    assert ("position", 1500) in calls

    calls.clear()
    win.timeline.set_playhead(F(3))  # while playing: seek, do not decode a still
    assert ("position", 2000) in calls
    generation = win.still_controller.generation
    win.timeline.set_playhead(F(7, 2))
    assert win.still_controller.generation == generation  # no decode storm while playing

    win.output_viewer.play_button.click()  # pause
    assert ("stop", None) in calls
    calls.clear()
    win.timeline.set_playhead(F(2))
    assert [c for c in calls if c[0] == "position"] == []  # paused: no more seeking

    # a playhead outside the rendered range is said, not silently mis-played
    win.timeline.set_playhead(F(9))
    win._play_preview()
    assert win._pending_seek == F(0)
    assert "outside the previewed range" in win.statusBar().currentMessage()
    win._stop_preview_playback()


def test_playhead_inspection_shows_the_real_output_frame(window):
    win, _tmp, _steps, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    win.timeline.set_playhead(F(3, 10))  # 0.3 s: still inside the first colour block
    assert win.still_controller.wait_idle(60_000)
    frame = win._source_shown
    assert frame is not None and not frame.black
    assert frame.frame_index == 8  # round_half_up(0.3*25)
    assert frame.source_pts == F(8, 25)
    mid = (frame.height // 2) * frame.width * 3 + (frame.width // 2) * 3
    assert frame.rgb[mid] > 128 and frame.rgb[mid + 2] < 90  # reddish
    assert "Frame 8" in win.output_viewer.time_label.text()
    assert win._stills.get((8, 0)) is not None  # kept for re-inspection


def test_a_decoded_frame_is_reused_instead_of_decoding_again(window):
    win, _tmp, _steps, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    win.timeline.set_playhead(F(3, 10))
    assert win.still_controller.wait_idle(60_000)
    generations = [win._source_shown.frame_index]
    win.timeline.set_playhead(F(1))
    assert win.still_controller.wait_idle(60_000)
    generations.append(win._source_shown.frame_index)
    win.timeline.set_playhead(F(3, 10))
    assert win.still_controller.wait_idle(60_000)
    assert win._source_shown.frame_index == generations[0]
    assert len(win._stills) == 2  # two distinct frames, not three decodes


def test_source_stepping_uses_real_timestamps_and_can_mark_an_edge(window):
    win, _tmp, _steps, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    win.timeline.set_playhead(F(0))
    assert win.still_controller.wait_idle(60_000)
    shown = win._source_shown
    assert shown is not None
    stamps = win._timestamps_for(shown)
    assert stamps and stamps[0] == F(0)

    win.output_viewer.source_fwd_button.click()
    assert win._source_shown.source_pts == F(1, 25)  # the real neighbouring frame
    assert len(win.frame_times) == 1  # the probe result is cached for the next step

    win.output_viewer.sourceMarkRequested.emit("out")
    clip = win.session.project.clip("vid")
    assert clip.source_out == F(1, 25)
    assert win.session.can_undo


def test_source_stepping_stops_at_the_end_of_a_stream(window):
    win, _tmp, _steps, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    win.timeline.set_playhead(F(0))
    assert win.still_controller.wait_idle(60_000)
    first = win._source_shown
    win._source_shown = StillFrame(
        width=first.width, height=first.height, rgb=first.rgb, frame_index=0,
        source_pts=F(0), clip_id="vid", asset_id="steps", stream_index=0,
    )
    win.output_viewer.source_back_button.click()
    assert "No neighbouring source frame" in win.statusBar().currentMessage()


def test_a_black_output_frame_has_no_source_stepping(window):
    win, _tmp, _steps, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    win.timeline.set_playhead(F(5))  # past the only picture clip
    assert win.still_controller.wait_idle(60_000)
    assert win._source_shown.black
    assert not win.output_viewer.source_fwd_button.isEnabled()


# ------------------------------------------------------------- cache lifecycle


def test_the_decoded_frame_cache_stays_within_its_budget(qapp_instance, monkeypatch, tmp_path):
    """Inspected frames are bounded in RAM, not only on disk."""
    from video_editor.render.cache import DEFAULT_FRAME_BUDGET_BYTES, CachedFrame

    win = _bare(qapp_instance, monkeypatch, tmp_path)
    for index in range(400):  # far more pictures than the budget can hold
        win._stills.put(
            CachedFrame(
                key=(index, 0),
                width=256,
                height=256,
                rgb=b"\x00" * (256 * 256 * 3),
                payload=index,
            )
        )
    assert win._stills.total_bytes <= DEFAULT_FRAME_BUDGET_BYTES
    assert len(win._stills) < 400
    assert win._stills.get((0, 0)) is None  # the oldest frames go first
    assert win._stills.get((399, 0)) is not None
    win.close()


def test_freeing_the_cache_deletes_only_its_own_files(window):
    win, tmp, _steps, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    win.output_viewer.range_start.setText("0:00:00.000")
    win.output_viewer.range_end.setText("0:00:01.000")
    win.output_viewer.mark_range_edited()
    win.output_viewer._on_render_clicked()
    assert win.preview_controller.wait_finished(120_000)
    cached = Path(win._preview[4])
    assert cached.is_file()
    foreign = tmp / "my-export.mp4"
    foreign.write_bytes(b"precious")

    win.output_viewer.freeCacheRequested.emit()
    assert not cached.exists()
    assert foreign.read_bytes() == b"precious"
    assert win.output_viewer.state.text().startswith("Preview cache freed")
    assert "only files this application created" in win.statusBar().currentMessage()


def test_closing_the_window_releases_workers_and_owned_files(qapp_instance, tmp_path,
                                                             monkeypatch):
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / "previews"))
    win = MainWindow()
    install_document(win, composition(), (offline_asset("steps"), offline_asset("t660")))
    root = tmp_path / "previews" / "video-editor-previews"
    root.mkdir(parents=True, exist_ok=True)
    leftover = root / ("preview-" + "a" * 24 + ".mp4")
    leftover.write_bytes(b"stale cache entry")
    staging = root / "preview-abc.1234.partial.mp4"
    staging.write_bytes(b"interrupted render leftover")

    win.close()
    assert not leftover.exists()
    assert not staging.exists()
    assert not win.still_controller._thread.isRunning()
    assert not win.preview_controller.running


def test_keep_previews_environment_variable_spares_the_cache(qapp_instance, tmp_path,
                                                             monkeypatch):
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("VIDEO_EDITOR_KEEP_PREVIEWS", "1")
    win = MainWindow()
    root = tmp_path / "previews" / "video-editor-previews"
    root.mkdir(parents=True, exist_ok=True)
    kept = root / ("preview-" + "b" * 24 + ".mp4")
    kept.write_bytes(b"worth keeping")
    win.close()
    assert kept.exists()


# ------------------------------------------------------------- obsolescence


def test_superseded_preview_results_are_never_delivered(qapp_instance, tmp_path, monkeypatch):
    """Rapid re-requesting: only the newest request may reach the viewer."""
    release = threading.Event()
    entered = threading.Event()

    def fake_render(request, on_progress=None, cancel_event=None):
        """Honours cancellation like the real renderer does."""
        from video_editor.render.exporter import RenderResult

        entered.set()
        if cancel_event is not None and cancel_event.wait(1):
            raise RenderCancelled("cancelled")
        release.wait(10)
        if cancel_event is not None and cancel_event.is_set():
            raise RenderCancelled("cancelled")
        return RenderResult(path=request.destination, duration=F(1), produced_duration=F(1))

    monkeypatch.setattr(worker_module, "render", fake_render)
    controller = worker_module.PreviewController()
    events: list[tuple[str, int]] = []
    controller.finished.connect(lambda generation, result: events.append(("finished", generation)))
    controller.cancelled.connect(lambda generation: events.append(("cancelled", generation)))

    first = controller.start(_stub_job(tmp_path / "a.mp4"))
    assert entered.wait(5)
    second = controller.start(_stub_job(tmp_path / "b.mp4"))  # supersedes the first
    assert second > first
    release.set()
    assert controller.wait_finished(20_000)
    assert [g for kind, g in events if kind == "finished"] == [second]
    assert all(kind != "cancelled" for kind, _ in events)  # superseded: dropped
    controller.deleteLater()


def _stub_job(destination: Path):
    from video_editor.render.preview import PreviewJob

    request = RenderRequest(project=Project(), asset_paths={}, destination=destination)
    plan = request.plan()
    return PreviewJob(request=request, key="k", plan=plan, reused=False)


def test_still_results_from_superseded_requests_are_dropped(qapp_instance):
    controller = worker_module.StillController()
    delivered: list[int] = []
    controller.finished.connect(lambda generation, frame: delivered.append(generation))
    current = controller.generation
    controller._deliver(current - 1, object())  # an obsolete decode finished late
    controller._deliver(current, object())
    assert delivered == [current]
    controller.shutdown()
    assert not controller._thread.isRunning()


def test_shutting_a_window_down_twice_is_safe(window):
    """Closing paths may overlap, and the second one must not touch released Qt.

    The window releases its render threads, decode threads and media players when
    it closes. A quit can reach the same window twice (close, then quit), and the
    test harness may find a window that already closed itself — so shutdown has to
    be a no-op the second time rather than a crash on an already-deleted player.
    """
    win, _tmp, _steps, _tone = window
    win._shutdown_preview()
    assert win._media_released
    win._shutdown_preview()  # must not raise
    assert not win.decorations._thread.isRunning()
    assert not win.still_controller._thread.isRunning()
