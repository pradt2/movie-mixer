"""Export GUI tests: dialog validation, worker signals, cancellation, snapshots.

Nothing here calls the GUI's own file dialogs: the window's export entry point
is exercised with the dialog's ``exec`` replaced, so tests stay deterministic.
"""

from __future__ import annotations

import threading
from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox

from media_fixtures import HAS_FFMPEG, make_colour_steps, make_tone
from video_editor.core.commands import RegisterSourceCommand
from video_editor.core.model import OutputSettings, Project, StreamSource, Track, TrackKind
from video_editor.diagnostics import MediaToolStatus, ToolStatus
from video_editor.media.assets import Asset
from video_editor.project.document import EditorDocument
from video_editor.render.exporter import (
    RenderError,
    RenderRequest,
    RenderResult,
)
from video_editor.render.graph import MP4_H264_AAC
from video_editor.ui import main_window as window_module
from video_editor.ui import render_worker as worker_module
from video_editor.ui.export_dialog import (
    RANGE_CUSTOM,
    RANGE_FROM_PLAYHEAD,
    RANGE_WHOLE,
    ExportDialog,
)
from video_editor.ui.main_window import MainWindow
from video_editor.ui.render_worker import RenderController

F = Fraction


def export_project() -> Project:
    """4 s composition: picture 0-4 s, audio contribution 1-3 s."""
    p = Project(output=OutputSettings(128, 96, F(25), 48000, "stereo"))
    p = RegisterSourceCommand(
        StreamSource("steps", 0, TrackKind.VIDEO, F(0), F(6), frame_rate=F(25),
                     stream_position=0)
    ).apply(p)
    p = RegisterSourceCommand(
        StreamSource("t660", 0, TrackKind.AUDIO, F(0), F(6), sample_rate=48000,
                     stream_position=0)
    ).apply(p)
    p = p.with_tracks((Track("v", TrackKind.VIDEO, "V1"), Track("a", TrackKind.AUDIO, "A1")))
    p = _add(p, "vid", "steps", 0, "v", F(0), F(4))
    p = _add(p, "aud", "t660", 0, "a", F(1), F(2))
    return p


def _add(
    p: Project, clip_id: str, asset_id: str, stream: int, track: str, start, duration
) -> Project:
    from video_editor.core.commands import AddClipCommand

    return AddClipCommand(
        asset_id, stream, track, start, source_in=F(0), source_out=duration, clip_id=clip_id
    ).apply(p)


def _asset(path: Path, asset_id: str) -> Asset:
    return Asset(
        id=asset_id,
        path=path,
        size_bytes=path.stat().st_size,
        mtime_ns=path.stat().st_mtime_ns,
        metadata=None,
    )


def register_asset(window: MainWindow, path: Path, asset_id: str) -> None:
    """Record a file in the document, as a finished import would."""
    from gui_helpers import import_file

    import_file(window, path, asset_id)


# ---------------------------------------------------------------- the dialog


@pytest.fixture()
def dialog(qapp_instance, tmp_path):
    dlg = ExportDialog(None, export_project(), tmp_path / "out.mp4", playhead=F(2))
    yield dlg
    dlg.deleteLater()


def test_dialog_reports_what_will_be_rendered(dialog: ExportDialog):
    text = dialog.summary.toPlainText()
    assert "Duration: 4.000s" in text
    assert "100 frames @ 25/1 fps" in text
    assert "192000 samples @ 48000 Hz" in text
    assert "Audio: 1 contribution(s)" in text
    assert "no colour-space conversion" in text


def test_dialog_range_modes_use_the_playhead(dialog: ExportDialog):
    assert dialog.range_box.currentText() == RANGE_WHOLE
    assert dialog.choices().out_start == F(0)
    dialog.range_box.setCurrentText(RANGE_FROM_PLAYHEAD)
    assert dialog.choices().out_start == F(2)
    dialog.range_box.setCurrentText(RANGE_CUSTOM)
    dialog.start_edit.setText("0:00:01.500")
    dialog.end_edit.setText("3")
    choices = dialog.choices()
    assert choices.out_start == Fraction(3, 2)
    assert choices.out_end == F(3)


def test_ok_is_disabled_for_an_invalid_range(dialog: ExportDialog):
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    assert ok.isEnabled()
    dialog.range_box.setCurrentText(RANGE_CUSTOM)
    dialog.start_edit.setText("10")
    dialog.end_edit.setText("2")  # end before start
    assert not ok.isEnabled()
    assert "Invalid" in dialog._error_label.text() or "before" in dialog._error_label.text()
    dialog.start_edit.setText("1")
    assert ok.isEnabled()


def test_ok_is_disabled_for_unparseable_time(dialog: ExportDialog):
    ok = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
    dialog.range_box.setCurrentText(RANGE_CUSTOM)
    dialog.start_edit.setText("not a time")
    assert not ok.isEnabled()
    assert "Invalid range" in dialog._error_label.text()


def test_dialog_adds_the_preset_extension(dialog: ExportDialog, tmp_path):
    dialog.destination.setText(str(tmp_path / "movie"))
    assert dialog.choices().destination.suffix == ".mp4"


def test_dialog_warns_about_existing_destination(dialog: ExportDialog, tmp_path):
    (tmp_path / "out.mp4").write_bytes(b"x")
    dialog.range_box.setCurrentText(RANGE_CUSTOM)  # forces re-validation
    assert "exists" in dialog._error_label.text()
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()


def test_dialog_warns_about_missing_directory(qapp_instance, tmp_path):
    dlg = ExportDialog(None, export_project(), tmp_path / "nope" / "out.mp4")
    assert "does not exist" in dlg._error_label.text()
    assert not dlg.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()


def test_dialog_shows_plan_warnings(qapp_instance, tmp_path):
    p = Project(output=OutputSettings(128, 96, F(25), 48000, "stereo"))
    p = RegisterSourceCommand(
        StreamSource(
            "steps", 0, TrackKind.VIDEO, F(0), F(4), conservative_end=True, frame_rate=F(25),
            stream_position=0
        )
    ).apply(p)
    p = p.with_tracks((Track("v", TrackKind.VIDEO, "V1"),))
    p = _add(p, "c", "steps", 0, "v", F(0), F(4))
    dlg = ExportDialog(None, p, tmp_path / "out.mp4")
    assert "Warning" in dlg.summary.toPlainText()


# ---------------------------------------------------------------- the worker


def test_worker_reports_success_result(qapp_instance, tmp_path):
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg")
    controller = RenderController()
    results: list[RenderResult] = []
    controller.finished.connect(results.append)
    destination = tmp_path / "worker.mp4"
    assert controller.start(
        RenderRequest(
            project=export_project(),
            asset_paths={
                "steps": make_colour_steps(tmp_path),
                "t660": make_tone(tmp_path, "tone660.wav", 660),
            },
            destination=destination,
            out_end=F(1),
        )
    )
    assert controller.wait_finished(120_000)
    assert len(results) == 1
    assert destination.is_file()
    assert not controller.running


def _request_stub(destination: Path) -> RenderRequest:
    return RenderRequest(project=Project(), asset_paths={}, destination=destination)


def test_worker_reports_cancellation_without_writing(qapp_instance, tmp_path, monkeypatch):
    """Deterministic cancel path: a fake render that waits for the cancel event."""

    def fake_render(request, on_progress, cancel_event):
        on_progress(0.1)
        cancel_event.wait(5)  # the controller cancels immediately
        from video_editor.render.exporter import RenderCancelled

        raise RenderCancelled("cancelled")

    monkeypatch.setattr(worker_module, "render", fake_render)
    events: list[str] = []
    controller = RenderController()
    controller.cancelled.connect(lambda: events.append("cancelled"))
    controller.failed.connect(lambda message: events.append(f"failed:{message}"))
    controller.finished.connect(lambda result: events.append("finished"))
    assert controller.start(_request_stub(tmp_path / "nope.mp4"))
    assert controller.wait_finished(20_000)
    assert events == ["cancelled"]
    assert not (tmp_path / "nope.mp4").exists()


def test_worker_cancel_during_run(qapp_instance, tmp_path, monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def fake_render(request, on_progress, cancel_event):
        entered.set()
        cancel_event.wait(5)
        from video_editor.render.exporter import RenderCancelled

        raise RenderCancelled("cancelled")

    monkeypatch.setattr(worker_module, "render", fake_render)
    controller = RenderController()
    assert controller.start(_request_stub(tmp_path / "x.mp4"))
    assert entered.wait(5)
    controller.cancel()
    assert controller.wait_finished(20_000)
    release.set()


def test_worker_reports_failure_message(qapp_instance, tmp_path, monkeypatch):
    def fake_render(request, on_progress, cancel_event):
        raise RenderError("ffmpeg failed: bogus filter")

    monkeypatch.setattr(worker_module, "render", fake_render)
    controller = RenderController()
    failures: list[str] = []
    controller.failed.connect(failures.append)
    assert controller.start(_request_stub(tmp_path / "x.mp4"))
    assert controller.wait_finished(20_000)
    assert failures == ["ffmpeg failed: bogus filter"]


def test_worker_reports_progress_bounded(qapp_instance, tmp_path, monkeypatch):
    values: list[float] = []

    def fake_render(request, on_progress, cancel_event):
        for step in range(101):
            on_progress(step / 100)
        return RenderResult(path=request.destination, duration=F(1), produced_duration=F(1))

    monkeypatch.setattr(worker_module, "render", fake_render)
    controller = RenderController()
    controller.progress.connect(values.append)
    assert controller.start(_request_stub(tmp_path / "x.mp4"))
    assert controller.wait_finished(20_000)
    assert values and values[0] == pytest.approx(0.0)
    assert values[-1] == pytest.approx(1.0)
    assert all(0.0 <= v <= 1.0 for v in values)


def test_controller_refuses_concurrent_exports(qapp_instance, tmp_path, monkeypatch):
    release = threading.Event()

    def fake_render(request, on_progress, cancel_event):
        release.wait(5)
        return RenderResult(path=request.destination, duration=F(1), produced_duration=F(1))

    monkeypatch.setattr(worker_module, "render", fake_render)
    controller = RenderController()
    assert controller.start(_request_stub(tmp_path / "a.mp4"))
    assert not controller.start(_request_stub(tmp_path / "b.mp4"))
    release.set()
    assert controller.wait_finished(20_000)


# ------------------------------------------------------- window integration


@pytest.fixture()
def window_with_media(qapp_instance, tmp_path):
    window = MainWindow()
    steps = make_colour_steps(tmp_path) if HAS_FFMPEG else _silent_file(tmp_path)
    tone = (
        make_tone(tmp_path, "tone660.wav", 660) if HAS_FFMPEG else _silent_file(tmp_path, "t.wav")
    )
    register_asset(window, steps, "steps")
    register_asset(window, tone, "t660")
    window.install_document(
        EditorDocument(project=export_project(), assets=window.session.document.assets)
    )
    yield window, tmp_path, steps
    window.close()


def _silent_file(tmp_path: Path, name: str = "placeholder.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"not media")
    return path


def _accepted(self) -> int:
    """Stand-in for ExportDialog.exec (patched on the importing module)."""
    return int(QDialog.DialogCode.Accepted)


def test_window_export_start_uses_a_snapshot(window_with_media, monkeypatch):
    window, tmp_path, _steps = window_with_media
    started: dict[str, RenderRequest] = {}

    def fake_start(request: RenderRequest) -> bool:
        started["request"] = request
        return True

    monkeypatch.setattr(window.render_controller, "start", fake_start)
    monkeypatch.setattr(window_module.ExportDialog, "exec", _accepted)

    clips_before = len(window.session.project.clips)
    window._export_composition()
    request = started["request"]
    assert len(request.project.clips) == clips_before
    assert window.export_action.isEnabled() is False
    assert window.cancel_export_action.isEnabled() is True
    assert window.export_progress.isVisibleTo(window)

    window._on_export_finished(
        RenderResult(path=tmp_path / "out.mp4", duration=F(4), produced_duration=F(4))
    )
    assert window.export_action.isEnabled()
    assert window.cancel_export_action.isEnabled() is False
    assert "Exported" in window.statusBar().currentMessage()
    assert window.export_progress.isVisibleTo(window) is False


def test_window_export_requests_overwrite_confirmation(window_with_media, monkeypatch):
    window, tmp_path, _steps = window_with_media
    existing = tmp_path / "out.mp4"
    existing.write_bytes(b"keep me")
    asked: list[str] = []
    started: dict[str, RenderRequest] = {}

    monkeypatch.setattr(
        window_module.ExportDialog,
        "exec",
        lambda self: (self.destination.setText(str(existing)), _accepted(self))[1],
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: asked.append(a[2]) or _no()),
    )
    def fake_start(request: RenderRequest) -> bool:
        started["request"] = request
        return True

    monkeypatch.setattr(window.render_controller, "start", fake_start)

    window._export_composition()
    assert asked and "Replace" in asked[0]
    assert started == {}  # refused: nothing started
    assert existing.read_bytes() == b"keep me"


def _no() -> int:
    return int(QMessageBox.StandardButton.No)


def test_window_export_reuses_the_current_project_not_stale_state(window_with_media, monkeypatch):
    window, _tmp_path, _steps = window_with_media
    started: list[RenderRequest] = []
    monkeypatch.setattr(
        window.render_controller,
        "start",
        lambda request: started.append(request) or True,
    )
    monkeypatch.setattr(window_module.ExportDialog, "exec", _accepted)
    window._export_composition()
    assert started[0].out_end is None  # whole composition
    assert started[0].preset is MP4_H264_AAC
    assert set(started[0].asset_paths) >= {"steps", "t660"}


def test_window_export_failure_is_reported(window_with_media, monkeypatch):
    window, _tmp_path, _steps = window_with_media
    monkeypatch.setattr(window.render_controller, "start", lambda request: True)
    monkeypatch.setattr(window_module.ExportDialog, "exec", _accepted)
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[2]))
    )
    window._export_composition()
    window._on_export_failed("ffmpeg is not available on PATH")
    assert warnings and "ffmpeg" in warnings[0]
    assert "Export failed" in window.statusBar().currentMessage()
    assert window.export_action.isEnabled()


def test_window_export_cancellation_clears_ui(window_with_media, monkeypatch):
    window, _tmp, _steps = window_with_media
    monkeypatch.setattr(window.render_controller, "start", lambda request: True)
    monkeypatch.setattr(window_module.ExportDialog, "exec", _accepted)
    window._export_composition()
    window._on_export_cancelled()
    assert "cancelled" in window.statusBar().currentMessage()
    assert window.export_progress.isVisibleTo(window) is False
    assert window.export_action.isEnabled()


def test_window_export_warning_is_shown_after_success(window_with_media, monkeypatch):
    window, tmp_path, _steps = window_with_media
    monkeypatch.setattr(window.render_controller, "start", lambda request: True)
    monkeypatch.setattr(window_module.ExportDialog, "exec", _accepted)
    window._export_composition()
    window._on_export_finished(
        RenderResult(
            path=tmp_path / "out.mp4",
            duration=F(4),
            produced_duration=F(4),
            warnings=("audio may clip",),
        )
    )
    assert "audio may clip" in window.statusBar().currentMessage()


def test_window_refuses_export_without_media(qapp_instance, tmp_path):
    window = MainWindow(
        media_status=MediaToolStatus(
            ffmpeg=ToolStatus("ffmpeg", None, None, "not found"),
            ffprobe=ToolStatus("ffprobe", None, None, "not found"),
        )
    )
    window.install_document(EditorDocument(project=export_project()))
    window._export_composition()
    assert "ffmpeg" in window.statusBar().currentMessage().lower()
    window.close()


def test_window_export_requires_clips(qapp_instance):
    window = MainWindow()
    window._export_composition()
    assert "empty" in window.statusBar().currentMessage()
    window.close()


def test_window_refuses_missing_asset(window_with_media, monkeypatch):
    window, _tmp_path, steps = window_with_media
    monkeypatch.setattr(window_module.ExportDialog, "exec", _accepted)
    steps.unlink()  # the file goes; the document still names it
    window._export_composition()
    message = window.statusBar().currentMessage()
    assert "Rendering is refused until these sources are available" in message
    assert "steps" in message  # names the asset rather than failing generically


def test_real_export_through_the_controller(tmp_path):
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg")
    steps = make_colour_steps(tmp_path)
    tone = make_tone(tmp_path, "tone660.wav", 660)
    destination = tmp_path / "gui_export.mp4"
    controller = RenderController()
    failures: list[str] = []
    controller.failed.connect(failures.append)
    assert controller.start(
        RenderRequest(
            project=export_project(),
            asset_paths={"steps": steps, "t660": tone},
            destination=destination,
            out_end=F(2),
            preset=MP4_H264_AAC,
        )
    )
    assert controller.wait_finished(120_000)
    assert failures == []
    assert destination.is_file()
    assert destination.stat().st_size > 1000


def test_worker_thread_is_the_only_caller_of_render(qapp_instance, tmp_path, monkeypatch):
    """The GUI thread must never run FFmpeg (render runs on the QThread)."""
    import PySide6.QtWidgets as qt

    main_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    def fake_render(request, on_progress, cancel_event):
        seen.append(threading.current_thread())
        return RenderResult(path=request.destination, duration=F(1), produced_duration=F(1))

    monkeypatch.setattr(worker_module, "render", fake_render)
    controller = RenderController()
    assert controller.start(_request_stub(tmp_path / "x.mp4"))
    assert controller.wait_finished(20_000)
    assert seen and seen[0] is not main_thread
    assert qt.QApplication.instance() is not None


def test_controller_is_idle_after_each_run(qapp_instance, tmp_path, monkeypatch):
    def fake_render(request, on_progress, cancel_event):
        return RenderResult(path=request.destination, duration=F(1), produced_duration=F(1))

    monkeypatch.setattr(worker_module, "render", fake_render)
    controller = RenderController()
    assert not controller.running
    for name in ("a.mp4", "b.mp4"):
        assert controller.start(_request_stub(tmp_path / name))
        assert controller.wait_finished(20_000)
        assert not controller.running
