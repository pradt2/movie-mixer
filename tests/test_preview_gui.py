"""Composition preview through the window: stale marking, reuse, stills, shutdown.

Qt tests stay headless (offscreen platform) and never touch real films: fixtures
are the synthetic solid-colour/sine files from ``media_fixtures``.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from gui_helpers import install_document, media_record, offline_asset
from media_fixtures import HAS_FFMPEG, make_colour_steps, make_tone, picture_facts
from video_editor.core.commands import (
    AddClipCommand,
    MoveClipCommand,
    RegisterSourceCommand,
    RemoveClipsCommand,
    RenameTrackCommand,
    TrimClipCommand,
    TrimEdge,
)
from video_editor.core.model import OutputSettings, Project, StreamSource, Track, TrackKind
from video_editor.media.framegrab import StillFrame
from video_editor.media.frameindex import AnswerStatus, FrameAnswer
from video_editor.project.document import ReplaceAssetCommand
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


def _media_window(monkeypatch, tmp_path, project, assets, name="previews-focused"):
    """A window holding *project* plus real *assets*, for one focused question.

    Separate from the shared ``window`` fixture because these tests need a different
    document (another fixture file, another composition) and must close reliably.
    """
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / name))
    win = MainWindow()
    install_document(win, project, assets)
    win.show()
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
    # What the playhead shows is the frame **already covering** it: floor(0.3*25) = 7.
    # Frame 7 presents 7/25 = 0.28 s and stays on screen until 8/25 = 0.32 s, so
    # frame 8 at this playhead would be a picture the playhead has not reached yet —
    # future footage, not the frame currently showing.
    assert frame.frame_index == 7
    assert frame.output_time == F(7, 25)
    assert frame.source_pts == F(7, 25)
    mid = (frame.height // 2) * frame.width * 3 + (frame.width // 2) * 3
    assert frame.rgb[mid] > 128 and frame.rgb[mid + 2] < 90  # reddish
    assert "Frame 7" in win.output_viewer.time_label.text()
    assert win._stills.get((7, 0)) is not None  # kept under the frame it shows


def test_inspecting_the_same_playhead_twice_reuses_the_decoded_frame(window):
    """Cache-hit question: looking at the same frame again must not decode it twice.

    ``still_controller.generation`` counts the decodes actually asked for, so a
    playhead returning to a frame that is already in ``_stills`` has to leave it
    unchanged. That only happens when the lookup key and the stored key are the same
    rule: the entry is keyed by the frame the playhead shows, floor(0.3*25) = 7.
    """
    win, _tmp, _steps, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    win.timeline.set_playhead(F(3, 10))
    assert win.still_controller.wait_idle(60_000)
    assert win._source_shown.frame_index == 7
    assert win._stills.get((7, 0)) is not None
    here_generation = win.still_controller.generation

    win.timeline.set_playhead(F(1))  # a different frame (25 at 1.0 s): a real decode
    assert win.still_controller.wait_idle(60_000)
    assert win._source_shown.frame_index == 25
    assert win.still_controller.generation > here_generation, "the first move decoded nothing"
    away_generation = win.still_controller.generation

    win.timeline.set_playhead(F(3, 10))  # back: the cached frame answers
    assert win.still_controller.wait_idle(60_000)
    assert win.still_controller.generation == away_generation, "the cached frame was re-decoded"
    assert win._source_shown.frame_index == 7
    assert len(win._stills) == 2  # two frames decoded, not three


def test_a_playhead_is_never_served_a_still_keyed_while_a_later_playhead_showed(window):
    """Key question: the entry for playhead 0.3 s is ``(7, 0)``, never ``(8, 0)``.

    Hand-worked at 25 fps: floor(0.3*25) = 7 and frame 7 spans 7/25 = 0.28 s to
    8/25 = 0.32 s, where frame 8 begins. Rounding the playhead half up (as this
    lookup used to) names 8, which both misses this playhead's own entry and hits
    the frame-8 picture decoded while a **later** playhead was showing — so the
    viewer would present footage the playhead has not reached.
    """
    win, _tmp, _steps, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    assert win._still_cache_index(F(3, 10)) == 7  # not 8: neither ceil nor half-up
    assert win._still_cache_index(F(7, 25)) == 7  # 0.28 s: frame 7's own first instant
    assert win._still_cache_index(F(8, 25)) == 8  # 0.32 s: frame 8 starts here

    win.timeline.set_playhead(F(8, 25))  # a later playhead decodes frame 8
    assert win.still_controller.wait_idle(60_000)
    assert win._source_shown.frame_index == 8
    assert win._stills.get((8, 0)) is not None

    win.timeline.set_playhead(F(3, 10))  # back inside frame 7's own span
    assert win.still_controller.wait_idle(60_000)
    assert win._source_shown.frame_index == 7, "the viewer served the frame-8 still"
    assert win._stills.get((7, 0)) is not None  # the entry this playhead needs
    assert win._stills.get((8, 0)) is not None  # cached, but not what this playhead shows


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
    """Stepping, marking and undo, measured against the fixture's own pictures.

    Every expected number here comes from ``picture_facts`` — an independent decode
    of the fixture file — never from the code under test. The stepping path has to
    agree with that mapping, and the mark buttons have to agree with what the clip's
    half-open source interval ``[source_in, source_out)`` actually holds.
    """
    win, _tmp, steps_path, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    facts = picture_facts(steps_path)
    assert facts.pictures[:3] == (ZERO, F(1, 25), F(2, 25))  # the fixture's own grid

    def shown_pts() -> Fraction:
        return win._source_shown.decoded_source_pts

    def step(direction: int) -> Fraction:
        """One source-frame click, delivered, and the PTS the viewer now shows."""
        button = (win.output_viewer.source_fwd_button if direction > 0
                  else win.output_viewer.source_back_button)
        button.click()
        assert win.source_step_controller.wait_idle(60_000)
        return shown_pts()

    def settle() -> None:
        """Let the composition still that a document change asked for arrive."""
        assert win.still_controller.wait_idle(60_000)

    win.timeline.set_playhead(ZERO)
    settle()
    assert win._source_shown is not None and shown_pts() == facts.pictures[0]
    assert shown_pts() is not None, "the viewer must offer a measured position"
    # what the cut buttons promise, read before anything is in flight
    assert "first frame" in win.output_viewer.mark_in_button.toolTip()
    assert "EXCLUDING" in win.output_viewer.mark_out_button.toolTip()
    # what the buttons promise, before anything is in flight
    assert "EXCLUDING" in win.output_viewer.mark_out_button.toolTip()
    assert "first frame" in win.output_viewer.mark_in_button.toolTip()

    # one step: the next picture this stream really decodes, and its own timestamp
    assert step(+1) == facts.pictures[1] == F(1, 25)
    assert win._source_shown.source_only and win._source_shown.grid_exact
    # and back again: the neighbour is the stream's own, not one output frame wide
    assert step(-1) == facts.pictures[0]
    # one bounded read answered every one of those questions
    index = next(iter(win.source_step_controller._worker._indexes.values()))
    assert index.probes == 1, "each step paid for its own probe instead of reusing one"

    # ---- Mark In: the in point becomes this picture's measured PTS, and the
    # visible picture is the clip's first frame
    assert step(+1) == facts.pictures[1]
    assert step(+1) == facts.pictures[2] == F(2, 25)
    before = win.session.project.clip("vid")
    other = win.session.project.clip("aud")
    win.output_viewer.sourceMarkRequested.emit("in")
    clip = win.session.project.clip("vid")
    assert clip.source_in == facts.pictures[2]
    assert clip.source_in <= facts.pictures[2] < clip.source_out, (
        "the picture that was on screen is not the clip's first frame"
    )
    assert win.session.project.clip("aud") == other, "marking moved another clip"
    assert "first frame" in win.statusBar().currentMessage()

    win._undo()
    assert win.session.project.clip("vid") == before  # exact prior source_in/source_out
    settle()

    # ---- Mark Out is **exclusive**: the out point is this picture's PTS, and the
    # picture itself is left outside the clip
    assert shown_pts() == facts.pictures[0]  # back to the frame at the playhead
    assert step(+1) == facts.pictures[1] == F(1, 25)
    win.output_viewer.sourceMarkRequested.emit("out")
    clip = win.session.project.clip("vid")
    assert clip.source_out == facts.pictures[1]
    assert not clip.source_in <= facts.pictures[1] < clip.source_out, (
        "the picture marked out is still inside the clip"
    )
    assert clip.output_end == clip.output_start + (clip.source_out - clip.source_in)
    assert "excludes" in win.statusBar().currentMessage().lower()
    assert "not in the clip" in win.statusBar().currentMessage()

    win._undo()
    assert win.session.project.clip("vid") == before
    settle()

    # ---- stepping to the next picture and marking out there *includes* the earlier one
    assert step(+1) == facts.pictures[1]
    assert step(+1) == facts.pictures[2]
    win.output_viewer.sourceMarkRequested.emit("out")
    clip = win.session.project.clip("vid")
    assert clip.source_out == facts.pictures[2]
    assert clip.source_in <= facts.pictures[1] < clip.source_out, (
        "the earlier picture should now be inside the clip"
    )
    win._undo()
    assert win.session.project.clip("vid") == before


def test_stepping_a_retimed_vfr_clip_follows_its_real_picture_times(qapp_instance,
                                                                    monkeypatch, tmp_path):
    """Clip tempo and output frame rate play no part in a source step.

    ``make_identity_vfr`` decodes 100 pictures at 1/25 s and then 50 at 83/1000 /
    21/250, and every picture carries its own number as a colour. A clip running at
    2 source-seconds per output second (so its output frame rate, 25 fps, and its
    tempo both disagree with the stream) must still step between the stream's real
    timestamps, and the picture shown must be the one that timestamp names.
    """
    from bisect import bisect_right

    from media_fixtures import identity_at, make_identity_vfr, source_from_probe
    from video_editor.core.model import OutputSettings, Project, Track, TrackKind

    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    source = make_identity_vfr(tmp_path)
    facts = picture_facts(source)
    assert len(facts.spacings) > 1, "the fixture must stay variable-rate"

    project = Project(output=OutputSettings(64, 48, F(25), 48000, "stereo"))
    project = RegisterSourceCommand(source_from_probe(source, 0, "src")).apply(project)
    project = project.with_tracks((Track("v1", TrackKind.VIDEO, "V1"),))
    stream = project.source("src", 0)
    project = AddClipCommand(
        "src", 0, "v1", ZERO, source_in=stream.origin, source_out=stream.end,
        rate=F(2), clip_id="vfr",
    ).apply(project)
    win2 = _media_window(monkeypatch, tmp_path, project, (media_record(source, "src"),))
    try:
        win2.timeline.set_playhead(F(2))  # rate 2: this is source time 4 s
        assert win2.still_controller.wait_idle(60_000)
        shown = win2._source_shown
        assert shown.decoded_source_pts == F(4)
        assert identity_at(shown.rgb, shown.width, shown.height) == 100

        win2.output_viewer.source_fwd_button.click()
        assert win2.source_step_controller.wait_idle(60_000)
        shown = win2._source_shown
        want = bisect_right(facts.pictures, F(4))
        assert shown.decoded_source_pts == facts.pictures[want] == F(4083, 1000)
        assert identity_at(shown.rgb, shown.width, shown.height) == facts.identities[want]
        assert facts.pictures[want] - F(4) == F(83, 1000)  # not one output frame, not 1/25

        win2.output_viewer.source_back_button.click()
        assert win2.source_step_controller.wait_idle(60_000)
        shown = win2._source_shown
        assert shown.decoded_source_pts == F(4)
        assert identity_at(shown.rgb, shown.width, shown.height) == 100
    finally:
        win2.close()


#: What the two kinds of "no next frame" have to sound like, quoted from the window.
#: A **proven** absence names the reading that proved it; an **unread** stretch says
#: it did not read far enough — and neither may borrow the other's words.
PROVEN_EDGE = "No neighbouring source frame"
UNREAD_EDGE = "not read far enough to know"


def test_a_proven_edge_says_there_is_no_neighbouring_source_frame(window):
    """A real end of the stream is reported as an end, in both directions.

    The index may say "this stream has no picture that way" only when it read up to
    a limit beyond which nothing can exist — the start of the asset's timeline, or
    past the end this container claims for its content. Both edges are walked here
    with real pictures, and the wording is checked against the other case's words so
    the two can never be merged again.
    """
    win, _tmp, steps_path, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    facts = picture_facts(steps_path)
    assert facts.last == F(149, 25) and facts.count == 150  # 6 s at 25 fps

    # the clip only holds [0, 4) of a 6 s stream: extend it so its last picture —
    # still inside the stream's recorded bounds — can be stepped to
    assert win._apply(TrimClipCommand("vid", TrimEdge.RIGHT, F(6)))
    document = win.session.project
    win.timeline.set_playhead(F(59, 10))  # 5.9 s: frame 147, source 147/25
    assert win.still_controller.wait_idle(60_000)
    assert win._source_shown.decoded_source_pts == facts.pictures[147]

    win.output_viewer.source_fwd_button.click()
    assert win.source_step_controller.wait_idle(60_000)
    assert win._source_shown.decoded_source_pts == facts.pictures[148]
    win.output_viewer.source_fwd_button.click()
    assert win.source_step_controller.wait_idle(60_000)
    assert win._source_shown.decoded_source_pts == facts.last

    win.output_viewer.source_fwd_button.click()
    assert win.source_step_controller.wait_idle(60_000)
    message = win.statusBar().currentMessage()
    assert PROVEN_EDGE in message, message
    assert UNREAD_EDGE not in message, message
    assert "no picture after" in message, message
    # nothing moved, and no cut may be taken while the asked-for frame never arrived
    assert win._source_shown.decoded_source_pts == facts.last
    assert not win.output_viewer.mark_out_button.isEnabled()
    assert not win.output_viewer.mark_in_button.isEnabled()
    win._mark_source_edge("out")
    assert UNREAD_EDGE not in win.statusBar().currentMessage()
    assert win.session.project == document, "a refused step still changed the document"

    # the other edge: the first picture of the asset axis
    win.timeline.set_playhead(ZERO)
    assert win.still_controller.wait_idle(60_000)
    assert win._source_shown.decoded_source_pts == facts.pictures[0]
    win.output_viewer.source_back_button.click()
    assert win.source_step_controller.wait_idle(60_000)
    message = win.statusBar().currentMessage()
    assert PROVEN_EDGE in message, message
    assert UNREAD_EDGE not in message, message
    assert "no picture before" in message, message
    assert "start of this stream's timeline" in message, message
    assert win._source_shown.decoded_source_pts == facts.pictures[0]


def test_an_unread_stretch_is_not_reported_as_the_end_of_a_stream(qapp_instance,
                                                                  monkeypatch, tmp_path):
    """A picture the index has not read is "unknown", never "the stream ended".

    ``big_hole.mkv`` decodes 0-3.96 s and then nothing until 40.04 s. From the last
    picture before that break, the next picture is real — 40.04 s — but further away
    than the index's probe budget reaches, so the honest answer is that it has not
    read far enough. The same test then steps backwards, where a real neighbour does
    exist, and shows the refusal lifting once a verified picture is on screen.
    """
    from media_fixtures import make_presentation_hole, source_from_probe
    from video_editor.core.model import OutputSettings, Project, Track, TrackKind

    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")
    source = make_presentation_hole(tmp_path, name="big_hole.mkv", seconds=44,
                                    hole_start=4.0, hole_end=40.0)
    facts = picture_facts(source)
    assert facts.pictures[99] == F(99, 25), "the fixture's last picture before the break"
    assert min(p for p in facts.pictures if p > F(4)) == F(1001, 25)

    project = Project(output=OutputSettings(128, 96, F(25), 48000, "stereo"))
    project = RegisterSourceCommand(source_from_probe(source, 0, "hole")).apply(project)
    project = project.with_tracks((Track("v", TrackKind.VIDEO, "V1"),))
    stream = project.source("hole", 0)
    project = AddClipCommand("hole", 0, "v", ZERO, source_in=stream.origin,
                             source_out=stream.end, clip_id="holeclip").apply(project)
    win = _media_window(monkeypatch, tmp_path, project, (media_record(source, "hole"),))
    try:
        win.timeline.set_playhead(F(39, 10))  # 3.9 s: frame 97, source 3.88 s
        assert win.still_controller.wait_idle(60_000)
        assert win._source_shown.decoded_source_pts == facts.pictures[97]

        for want in (facts.pictures[98], facts.pictures[99]):
            win.output_viewer.source_fwd_button.click()
            assert win.source_step_controller.wait_idle(60_000)
            assert win._source_shown.decoded_source_pts == want

        # 3.96 s is the last picture the index has read; the break is wider than its
        # budget, so this is a "not known", not a claimed end
        win.output_viewer.source_fwd_button.click()
        assert win.source_step_controller.wait_idle(60_000)
        message = win.statusBar().currentMessage()
        assert UNREAD_EDGE in message, message
        assert PROVEN_EDGE not in message, message
        assert "not a proven end" in message, message
        assert win._source_shown.decoded_source_pts == facts.pictures[99], (
            "an unknown answer must not move the viewer to a guessed frame"
        )
        assert not win.output_viewer.mark_in_button.isEnabled()
        document = win.session.project
        win._mark_source_edge("out")
        assert UNREAD_EDGE in win.statusBar().currentMessage()
        assert win.session.project == document, "an unknown step still allowed a cut"

        # stepping where a real neighbour is proven works again, and with it marking
        win.output_viewer.source_back_button.click()
        assert win.source_step_controller.wait_idle(60_000)
        assert win._source_shown.decoded_source_pts == facts.pictures[98]
        assert win.output_viewer.mark_in_button.isEnabled()
    finally:
        win.close()


def test_a_black_output_frame_has_no_source_stepping(window):
    """No visible source frame means no stepping and no cut — and nothing crashes.

    Two ways to get there, and the window has to answer both by saying so rather than
    falling over: a playhead the composition does not reach (no frame is decoded at
    all, so nothing is shown), and a genuine black gap inside the composition (a
    truthful black frame is shown, and it belongs to no clip).
    """
    win, _tmp, _steps, _tone = window
    if not HAS_FFMPEG:  # pragma: no cover
        pytest.skip("requires ffmpeg fixtures")

    # ---- a playhead past the composition: the still is refused, so nothing is shown
    win.timeline.set_playhead(F(5))  # the composition ends at 4 s
    assert win.still_controller.wait_idle(60_000)
    assert win._source_shown is None, "a refused still leaves nothing visible"
    assert "No frame shown" in win.output_viewer.still.text()
    assert not win.output_viewer.source_fwd_button.isEnabled()
    assert not win.output_viewer.source_back_button.isEnabled()
    assert not win.output_viewer.mark_in_button.isEnabled()
    document = win.session.project
    # the shortcuts reach these directly even when the buttons are disabled, so the
    # window has to refuse them in words rather than fall over
    win.statusBar().clearMessage()
    win._step_source_frame(1)
    assert "No source frame is shown" in win.statusBar().currentMessage()
    win.statusBar().clearMessage()
    win._mark_source_edge("in")
    assert "No source frame is shown" in win.statusBar().currentMessage()
    assert "Disabled" in win.output_viewer.mark_in_button.toolTip()
    assert win.session.project == document, "nothing was shown, so nothing may be cut"

    # ---- a real black frame inside the composition
    assert win._apply(MoveClipCommand("vid", output_start=F(2)))  # gap at 0-2 s
    win.timeline.set_playhead(F(1))
    assert win.still_controller.wait_idle(60_000)
    assert win._source_shown is not None and win._source_shown.black
    assert win._source_shown.clip_id is None
    assert not win.output_viewer.source_fwd_button.isEnabled()
    assert not win.output_viewer.mark_out_button.isEnabled()
    assert "black here" in win.output_viewer.mark_out_button.toolTip()
    document = win.session.project
    win.statusBar().clearMessage()
    win._step_source_frame(-1)
    assert "no source picture under it" in win.statusBar().currentMessage()
    win.statusBar().clearMessage()
    win._mark_source_edge("in")
    assert "no source picture under it" in win.statusBar().currentMessage()
    assert win.session.project == document, "a black frame named no clip to trim"


# ------------------------------------------------- stepping against a parked worker
#
# The tests above measure real media. The ones below control **when** a source step
# answers, because what they check cannot be produced reliably with real decodes: a
# burst of clicks must count as one accumulated move, and a result that arrives after
# the clip or file it was asked for is gone must never reach the viewer. Only the
# picture decode and the timestamps it is asked about are stood in for; the worker,
# its thread, the controller's generations, the window's delivery gate and the
# viewer's buttons are the real ones. Latches, not sleeps.

#: bytes no real fixture produces, so a stale picture reaching the viewer is visible
MOCK_RGB = b"\x7f" * (8 * 8 * 3)


class ParkedStepWorker(worker_module.SourceStepWorker):
    """The real stepping worker, its work parked behind two events.

    ``entered`` is set when a request reaches the worker's own thread and ``release``
    lets it finish, so a test can pile requests up while a decode is in flight exactly
    as rapid clicking does. The worker's mailbox keeps only the newest request — that
    is production behaviour, under test here — so a burst of clicks costs one decode
    at the accumulated target, and the parked decode answers with whatever the
    document looks like *when it is released*, which is how a stale result is made.
    """

    #: replaced per test: a hand-written answer to the neighbour questions
    INDEX = None

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def _index_for(self, path: Path, stream_index: int):
        index = type(self).INDEX
        return super()._index_for(path, stream_index) if index is None else index

    def process_request(self) -> None:
        if self._pending is None:
            return  # a queued wake-up whose request had already been taken
        self.entered.set()
        assert self.release.wait(30), "the test never released the parked decode"
        super().process_request()


class _ScriptedIndex:
    """Neighbour questions answered from a script, so each gate is met in turn.

    The answers are consumed in the order the worker asks, the last one repeating.
    This is how an ``UNKNOWN`` — "not read far enough", the answer the index gives
    when it has not read that far — is put in front of the window without needing a
    forty-second fixture, next to a real neighbour and a picture whose own position
    cannot be read.
    """

    def __init__(self, *answers: FrameAnswer) -> None:
        self.answers = list(answers)
        self.asked: list[tuple] = []

    @property
    def declared_end(self):
        return SimpleNamespace(end=F(6))

    def answer_step(self, t, direction) -> FrameAnswer:
        self.asked.append((t, direction))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def _fake_decode(calls: list, *, measured: bool = True, exact: bool = True,
                 boom: str | None = None):
    """Stand in for ``extract_source_still``: record the target, echo it back.

    The real decoder reports the decoded picture's own timestamp for a request that
    names a real frame, so echoing the requested PTS is faithful to it. The two other
    honest outcomes it can produce are a picture whose position could not be read
    (``measured=False``) and one read from a container whose tick grid could not be
    established, so the position is not guaranteed to land where it says
    (``exact=False``).
    """

    def extract(project, asset_paths, clip_id, source_pts, output_time, **_kwargs):
        calls.append(source_pts)
        if boom is not None:
            raise worker_module.StillError(boom)
        clip = project.clip(clip_id)
        return StillFrame(
            width=8, height=8, rgb=MOCK_RGB, output_time=output_time, frame_index=0,
            black=False, source_pts=source_pts,
            decoded_source_pts=source_pts if measured else None,
            grid_exact=measured and exact, source_only=True, clip_id=clip_id,
            asset_id=clip.asset_id if clip else None,
            stream_index=clip.stream_index if clip else 0,
        )

    return extract


def _parked_window(qapp_instance, monkeypatch, tmp_path, *, decode=None, index=None):
    """A real window whose source-step decode waits for the test to release it."""
    steps = make_colour_steps(tmp_path)
    tone = make_tone(tmp_path, "t660.wav", 660)
    monkeypatch.setattr(worker_module, "SourceStepWorker", ParkedStepWorker)
    if decode is not None:
        monkeypatch.setattr(worker_module, "extract_source_still", decode)
    if index is not None:
        monkeypatch.setattr(ParkedStepWorker, "INDEX", index)
    win = _media_window(
        monkeypatch, tmp_path, composition(),
        (media_record(steps, "steps"), media_record(tone, "t660")),
        name="previews-parked",
    )
    win.timeline.set_playhead(ZERO)
    assert win.still_controller.wait_idle(60_000)
    return win, steps


def test_rapid_source_steps_accumulate_and_only_the_newest_lands(
    qapp_instance, monkeypatch, tmp_path
):
    """Rapid clicking: one decode at the ordered net, and only that picture lands.

    Three clicks forward and one back is a move of two frames, paid for with a single
    decode, and the viewer shows the net target — not the answer to any earlier click.
    While a step is outstanding no cut may be set, because the picture on screen is
    not the picture being asked for. A burst that cancels itself decodes nothing and
    puts the frame already on screen back in play for cutting; and a decode the window
    has stopped waiting for never replaces what is shown, not even when it answers
    with the current generation.
    """
    win, steps_path = _parked_window(qapp_instance, monkeypatch, tmp_path)
    calls: list[Fraction] = []
    monkeypatch.setattr(
        worker_module, "extract_source_still", _fake_decode(calls)
    )
    try:
        facts = picture_facts(steps_path)
        base = win._source_shown.decoded_source_pts
        assert base == facts.pictures[0]
        document = win.session.project

        for _ in range(3):
            win.output_viewer.source_fwd_button.click()
        worker = win.source_step_controller._worker
        assert worker.entered.wait(5), "no step ever reached the worker thread"

        # the viewer has not moved, and no cut may be taken from the old frame
        assert win._source_shown.decoded_source_pts == base, "a pending step moved it"
        assert not win.output_viewer.mark_in_button.isEnabled()
        assert not win.output_viewer.mark_out_button.isEnabled()
        win._mark_source_edge("in")
        assert win.session.project == document, "a cut was set while a step was pending"
        assert "still being decoded" in win.statusBar().currentMessage()

        # a click back while it is still parked: the target is the ordered net
        win.output_viewer.source_back_button.click()
        assert win._source_step_target == 2  # +1 +1 +1 -1, counted from the shown frame
        assert win._source_step_base == base

        worker.release.set()
        assert win.source_step_controller.wait_idle(60_000)
        assert calls == [facts.pictures[2]], (
            f"expected one decode at the accumulated target, got {calls}"
        )
        assert win._source_shown.decoded_source_pts == facts.pictures[2]
        assert win.output_viewer.shown_source_pts() == facts.pictures[2]
        assert "3 frame(s)" not in win.output_viewer.time_label.text()

        # the outstanding-request block is gone once the requested picture has landed
        assert win.output_viewer.mark_in_button.isEnabled()
        assert win.output_viewer.mark_out_button.isEnabled()

        # a burst that cancels itself: one forward, one back, net nothing asked for.
        # Nothing is decoded, the picture does not move, and because this window is
        # waiting for no picture, the frame on screen can be cut at again - even
        # though the forward decode it no longer wants is still running.
        worker.entered.clear()
        worker.release.clear()
        win.output_viewer.source_fwd_button.click()
        assert win._source_step_target == 1
        win.output_viewer.source_back_button.click()
        assert win._source_step_target == 0
        assert win._source_request is None, "the window still waits for a cancelled step"
        assert "already shown" in win.statusBar().currentMessage()
        assert calls == [facts.pictures[2]], "a cancelled burst paid for a decode"
        assert win._source_shown.decoded_source_pts == facts.pictures[2]
        assert win.output_viewer.mark_out_button.isEnabled()
        other = win.session.project.clip("aud")
        win._mark_source_edge("out")  # the shown frame is exactly the frame to cut at
        clip = win.session.project.clip("vid")
        assert clip.source_out == facts.pictures[2]
        assert not clip.source_in <= facts.pictures[2] < clip.source_out, (
            "the picture marked out is still inside the clip"
        )
        assert win.session.project.clip("aud") == other, "marking moved another clip"

        # the cancelled forward decode answers now (it was already running): its
        # picture must not reach the viewer, which is showing the frame the mark cut
        # asked for instead
        assert win.still_controller.wait_idle(60_000)
        landed = win._source_shown
        assert landed is not None and landed.rgb != MOCK_RGB
        assert landed.decoded_source_pts == facts.pictures[0]  # playhead 0, source 0

        worker.release.set()
        assert win.source_step_controller.wait_idle(60_000)
        assert calls == [facts.pictures[2], facts.pictures[3]], (
            f"expected the landed step plus the cancelled one, got {calls}"
        )
        assert win._source_shown is landed, "a cancelled step's late answer replaced it"
        assert "Source step" not in win.output_viewer.time_label.text()
    finally:
        win.source_step_controller._worker.release.set()
        win.close()


def test_a_late_step_result_never_replaces_a_changed_clip(
    qapp_instance, monkeypatch, tmp_path
):
    """A step whose clip is gone is dropped — whether it comes back good or bad.

    Both a **success** and a **failure** are delivered after the document moved, and
    each carries the still-current generation, so the controller alone would let them
    through: it is the window's identity gate that must refuse a picture of a clip
    that no longer exists, and refuse to blame the current frame for a failure that
    belongs to the old one.
    """
    win, steps_path = _parked_window(qapp_instance, monkeypatch, tmp_path)
    calls: list[Fraction] = []
    monkeypatch.setattr(
        worker_module, "extract_source_still", _fake_decode(calls)
    )
    try:
        facts = picture_facts(steps_path)

        # ---- a stale success: the clip it was decoding is removed while it runs
        win.output_viewer.source_fwd_button.click()
        worker = win.source_step_controller._worker
        assert worker.entered.wait(5)
        assert win._apply(RemoveClipsCommand(("vid",)))
        assert win._apply(
            AddClipCommand("steps", 0, "v", ZERO, source_in=ZERO, source_out=F(4),
                           clip_id="vid2")
        )
        assert win.still_controller.wait_idle(60_000)
        landed = win._source_shown
        assert landed.clip_id == "vid2"
        assert landed.rgb != MOCK_RGB, "the viewer should be showing real fixture bytes"

        worker.release.set()
        assert win.source_step_controller.wait_idle(60_000)
        assert calls == [facts.pictures[1]], "the worker never produced the stale picture"
        assert win._source_shown is landed, "a step for a deleted clip replaced the frame"
        assert win._source_shown.rgb != MOCK_RGB
        assert win._source_shown.clip_id == "vid2"

        # ---- a stale failure: it must not disable or re-caption the current frame
        worker.entered.clear()
        worker.release.clear()
        win.output_viewer.source_back_button.click()
        assert worker.entered.wait(5)
        # refused while in flight, and said to be refused for being in flight
        assert not win.output_viewer.mark_in_button.isEnabled()
        assert "not the one" in win.output_viewer.mark_in_button.toolTip()
        assert "Decoding the source frame" in win.output_viewer.time_label.text()

        # the composition changes under the parked decode, so the window stops
        # waiting for that step at all. A left trim slides the clip: its output end
        # stays at 4 s and its output start moves to 1/25 s, so the playhead has to
        # move with it to stay over a picture.
        assert win._apply(TrimClipCommand("vid2", TrimEdge.LEFT, F(1, 25)))
        win.timeline.set_playhead(F(1, 25))
        assert win.still_controller.wait_idle(60_000)
        document = win.session.project
        assert win._source_request is None, "the window still waits for the moved step"
        assert win.output_viewer.shown_source_pts() == facts.pictures[1]
        assert win.output_viewer.mark_in_button.isEnabled()
        caption = win.output_viewer.time_label.text()

        worker.release.set()  # the answer to the abandoned step arrives now
        assert win.source_step_controller.wait_idle(60_000)
        assert "not read far enough" not in win.statusBar().currentMessage()
        assert "No neighbouring source frame" not in win.statusBar().currentMessage()
        assert win.output_viewer.time_label.text() == caption, (
            "an obsolete failure re-captioned the viewer"
        )
        assert win.output_viewer.mark_in_button.isEnabled()
        assert win._source_shown.decoded_source_pts == facts.pictures[1]
        assert win.session.project == document, "an obsolete result changed the document"

        # and the current frame is still steppable
        worker.entered.clear()
        worker.release.clear()
        win.output_viewer.source_fwd_button.click()
        assert worker.entered.wait(5)
        worker.release.set()
        assert win.source_step_controller.wait_idle(60_000)
        assert win._source_shown.decoded_source_pts == facts.pictures[2]

        # ---- the asset no longer names the file the step was asked against: what a
        # relink publishes (new path, new recorded identity) in one transaction. A
        # picture decoded from the old file is not a frame of this composition, even
        # though its generation is the newest.
        worker.entered.clear()
        worker.release.clear()
        win.output_viewer.source_back_button.click()
        assert worker.entered.wait(5)
        relinked = tmp_path / "steps_relinked.mkv"
        shutil.copyfile(steps_path, relinked)
        asset = next(a for a in win.bin_controller.assets if a.id == "steps")
        assert win._apply_document(
            ReplaceAssetCommand(
                replace(
                    asset, path=relinked,
                    size_bytes=relinked.stat().st_size,
                    mtime_ns=relinked.stat().st_mtime_ns,
                )
            )
        )
        assert win.still_controller.wait_idle(60_000)
        assert win._asset_paths()["steps"] == relinked
        landed_again = win._source_shown
        assert landed_again.rgb != MOCK_RGB

        worker.release.set()  # the answer from the file this project left behind
        assert win.source_step_controller.wait_idle(60_000)
        assert win._source_shown is landed_again, (
            "a step decoded against the old file replaced the shown frame"
        )
        assert win._source_shown.rgb != MOCK_RGB
        assert win.output_viewer.mark_in_button.isEnabled()
    finally:
        win.source_step_controller._worker.release.set()
        win.close()


def test_an_unprovable_step_or_position_disables_marking(
    qapp_instance, monkeypatch, tmp_path
):
    """A frame that cannot be verified as a cut position is shown, never cut from.

    Three kinds, and all three are refused in words rather than approximated: the
    index has not read far enough to know where the next picture is, the decode
    produced a picture whose source position it could not measure, and the decode
    measured one it could not place on the container's own tick grid. In every case
    the picture stays up (it is a real decoded frame), the frame asked for is not
    invented, no cut is taken, and marking returns only when a verified frame is on
    screen again.
    """
    unknown = FrameAnswer(None, AnswerStatus.UNKNOWN, (ZERO, F(2)))
    index = _ScriptedIndex(unknown)
    calls: list[Fraction] = []
    win, _steps = _parked_window(
        qapp_instance, monkeypatch, tmp_path, index=index,
        decode=_fake_decode(calls),
    )
    try:
        base = win._source_shown.decoded_source_pts
        assert base == ZERO, "the composition's own frame carries a measured position"
        document = win.session.project

        # the index has not read past 2 s: the next picture is unknown, not absent
        win.output_viewer.source_fwd_button.click()
        assert win._source_step_target == 1
        win.source_step_controller._worker.release.set()
        assert win.source_step_controller.wait_idle(60_000)
        assert index.asked == [(ZERO, 1)], "the step did not ask the stream's own times"
        assert calls == [], "an unknown neighbour must not decode a guessed frame"
        assert UNREAD_EDGE in win.statusBar().currentMessage()
        assert PROVEN_EDGE not in win.statusBar().currentMessage()
        assert win._source_shown.decoded_source_pts == base, "the viewer moved anyway"
        assert not win.output_viewer.mark_in_button.isEnabled()
        assert not win.output_viewer.mark_out_button.isEnabled()
        win._mark_source_edge("in")
        assert win.session.project == document, "a cut was set from an unknown frame"

        # a real neighbour this time, but the decode cannot say where its picture sits
        monkeypatch.setattr(ParkedStepWorker, "INDEX",
                            _ScriptedIndex(FrameAnswer(F(1, 25), AnswerStatus.FOUND)))
        monkeypatch.setattr(
            worker_module, "extract_source_still", _fake_decode(calls, measured=False)
        )
        worker = win.source_step_controller._worker
        worker.entered.clear()
        worker.release.clear()
        win.output_viewer.source_fwd_button.click()
        assert worker.entered.wait(5)
        worker.release.set()
        assert win.source_step_controller.wait_idle(60_000)
        assert calls == [F(1, 25)]
        assert win._source_shown.decoded_source_pts is None
        assert "could not be measured" in win.statusBar().currentMessage()
        assert not win.output_viewer.mark_in_button.isEnabled()
        win._mark_source_edge("in")
        assert win.session.project == document, "a cut was set from an unmeasured time"

        # stepping on from that picture is refused for the same reason: the window
        # has no measured position to count from, so the index is never asked and no
        # second picture is decoded
        further = _ScriptedIndex(FrameAnswer(F(2, 25), AnswerStatus.FOUND))
        monkeypatch.setattr(ParkedStepWorker, "INDEX", further)
        assert not win.output_viewer.source_fwd_button.isEnabled()
        win.statusBar().clearMessage()
        win._step_source_frame(1)  # the shortcut reaches this even when disabled
        assert further.asked == [], "a step was asked from an unmeasured position"
        assert calls == [F(1, 25)], "a second picture was decoded from a guessed time"
        assert "could not be measured" in win.statusBar().currentMessage(), (
            "the refusal to step was not said"
        )

        # a verified frame is on screen again (the playhead moved to the next output
        # frame), and the step from it decodes a picture the container cannot place on
        # its own tick grid: honest picture, position not guaranteed
        win.timeline.set_playhead(F(1, 25))
        assert win.still_controller.wait_idle(60_000)
        assert win._source_shown.decoded_source_pts == F(1, 25)
        assert win.output_viewer.mark_in_button.isEnabled()

        grid_calls: list[Fraction] = []
        grid_index = _ScriptedIndex(FrameAnswer(F(2, 25), AnswerStatus.FOUND))
        monkeypatch.setattr(ParkedStepWorker, "INDEX", grid_index)
        monkeypatch.setattr(
            worker_module, "extract_source_still",
            _fake_decode(grid_calls, exact=False),
        )
        win.output_viewer.source_fwd_button.click()
        assert win.source_step_controller._worker.entered.wait(5)
        win.source_step_controller._worker.release.set()
        assert win.source_step_controller.wait_idle(60_000)
        assert grid_index.asked == [(F(1, 25), 1)], (
            "the step did not count from the frame actually shown"
        )
        assert grid_calls == [F(2, 25)]
        shown = win._source_shown
        assert shown.decoded_source_pts == F(2, 25) and not shown.grid_exact
        assert "timestamp grid" in win.statusBar().currentMessage()
        assert not win.output_viewer.mark_in_button.isEnabled()
        assert not win.output_viewer.source_fwd_button.isEnabled()
        document = win.session.project
        win.statusBar().clearMessage()
        win._mark_source_edge("out")
        assert "timestamp grid" in win.statusBar().currentMessage()
        win.statusBar().clearMessage()
        win._step_source_frame(1)
        assert "timestamp grid" in win.statusBar().currentMessage(), (
            "stepping on from an ungridded position was not refused in words"
        )
        assert win.session.project == document, "a cut was taken from an ungridded time"

        # a measured, gridded frame arrives (the playhead moved again): marking is
        # offered again, and the cut lands on the position that was observed
        win.timeline.set_playhead(F(2, 25))
        assert win.still_controller.wait_idle(60_000)
        assert win._source_shown.decoded_source_pts == F(2, 25)
        assert win._source_shown.grid_exact
        assert win.output_viewer.mark_out_button.isEnabled()
        win._mark_source_edge("out")
        assert win.session.project.clip("vid").source_out == F(2, 25)
    finally:
        win.source_step_controller._worker.release.set()
        win.close()


@pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg fixture generation")
def test_a_composition_still_that_cannot_place_its_picture_cannot_set_a_cut(
    qapp_instance, window, monkeypatch
):
    """Unknown stream metadata never silently enables source marking.

    The picture on screen is a real decoded frame of the composition, but its source
    position was not established - the container's grid could not be read, so the plan
    has no exact tick and no arrival can be tied to the returned picture. Marking a
    clip edge here would put the edge at the time the *plan* guessed, not at the frame
    the user is looking at, so the window refuses it in the same words it uses for a
    source step that could not be measured.
    """
    from video_editor.media import framegrab

    def unreadable(*_args, **_kwargs):
        raise framegrab.StillError("no time base for this stream in this test")

    win, _tmp, _steps, _tone = window
    monkeypatch.setattr(framegrab, "stream_timing", unreadable)
    win.timeline.set_playhead(F(1, 25))
    assert win.still_controller.wait_idle(60_000)
    shown = win._source_shown
    assert shown is not None and not shown.black, "the picture itself was lost"
    assert shown.decoded_source_pts is None, (
        "a composition prediction was reported as a measurement"
    )
    assert not shown.grid_exact
    assert shown.source_pts == F(1, 25), "the prediction must still be kept"
    block = win._source_position_block(shown)
    assert block is not None and "could not be measured" in block
    assert not win.output_viewer.mark_in_button.isEnabled()
    assert not win.output_viewer.mark_out_button.isEnabled()
    document = win.session.project
    win.statusBar().clearMessage()
    win._mark_source_edge("in")
    assert win.session.project == document, "a cut was set from an unmeasured position"
    assert "could not be measured" in win.statusBar().currentMessage()


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
