"""How the decoration layer is wired into the window (TASK_08).

A timeline may be drawn with or without pictures and waveforms: what these tests
pin down is that the window hands it material built from its *own* source
records, that material disappears when the file does, that a relink or a new
document cannot leave the previous file's frames on screen, and that no
extraction thread survives the window.
"""

from __future__ import annotations

import time
from fractions import Fraction
from pathlib import Path

import pytest

from gui_helpers import import_file, install_document, media_record
from media_fixtures import HAS_FFMPEG, make_colour_steps, make_tone
from video_editor.core.commands import AddClipCommand, RegisterSourceCommand
from video_editor.core.model import OutputSettings, Project, StreamSource, Track, TrackKind
from video_editor.media.assets import Asset
from video_editor.project.document import EditorDocument
from video_editor.ui.main_window import MainWindow

F = Fraction


def _asset(path: Path, asset_id: str) -> Asset:
    return Asset(
        id=asset_id, path=path, size_bytes=path.stat().st_size,
        mtime_ns=path.stat().st_mtime_ns, metadata=None,
    )


def register_asset(win: MainWindow, path: Path, asset_id: str) -> None:
    """Record a file in the document, as a finished import would."""
    import_file(win, path, asset_id)


def composition() -> Project:
    """Picture and sound from two different files, one clip each."""
    p = Project(output=OutputSettings(128, 96, F(25), 48000, "stereo"))
    p = RegisterSourceCommand(
        StreamSource("steps", 0, TrackKind.VIDEO, F(0), F(6), frame_rate=F(25),
                     width=128, height=96, stream_position=0)
    ).apply(p)
    p = RegisterSourceCommand(
        StreamSource("tone", 0, TrackKind.AUDIO, F(0), F(6), sample_rate=48000,
                     stream_position=0)
    ).apply(p)
    p = p.with_tracks((Track("v", TrackKind.VIDEO, "Picture"),
                       Track("a", TrackKind.AUDIO, "Dub")))
    p = AddClipCommand("steps", 0, "v", F(0), source_in=F(0), source_out=F(6),
                       clip_id="vid").apply(p)
    p = AddClipCommand("tone", 0, "a", F(0), source_in=F(0), source_out=F(6),
                       clip_id="aud").apply(p)
    return p


@pytest.fixture()
def window(qapp_instance, tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("VIDEO_EDITOR_RECOVERY_DIR", str(tmp_path / "recovery"))
    monkeypatch.setenv("VIDEO_EDITOR_AUTOSAVE_SECONDS", "0")
    steps = make_colour_steps(tmp_path)
    tone = make_tone(tmp_path, "tone.wav", 660)
    win = MainWindow()
    install_document(
        win, composition(), (media_record(steps, "steps"), media_record(tone, "tone"))
    )
    win.show()
    qapp_instance.processEvents()
    yield win, tmp_path, steps, tone
    win.close()


pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg required")


def test_the_window_supplies_material_for_its_own_registered_streams(window):
    win, _tmp, steps, tone = window
    material = win.decoration_material()
    assert set(material) == {("steps", 0), ("tone", 0)}, sorted(material)
    for (asset_id, _index), matter in material.items():
        path = steps if asset_id == "steps" else tone
        stat = path.stat()
        assert matter.path == path
        # the cache key is the file's identity, so it can never be reused for a
        # different file with the same name
        assert matter.identity == (stat.st_size, stat.st_mtime_ns)
        assert matter.source.stream_position is not None
    assert win.timeline._deco_material == material


def test_a_window_with_no_media_has_no_material_but_still_paints(window, qapp_instance):
    win, _tmp, _steps, _tone = window
    assert win.decoration_material()  # media is registered
    win.install_document(EditorDocument())
    win._refresh_timeline()
    qapp_instance.processEvents()
    assert win.decoration_material() == {}
    assert win.timeline.grab() is not None  # a bare timeline is still a complete view


def test_a_missing_file_contributes_no_material(window):
    win, tmp, steps, _tone = window
    moved = tmp / "moved-away.mkv"
    steps.rename(moved)
    assert ("steps", 0) not in win.decoration_material()


def test_a_decoration_really_arrives_for_a_placed_clip(window, qapp_instance):
    """Material -> background extraction -> cache, inside the real window."""
    win, _tmp, _steps, _tone = window
    win.timeline.set_view(F(0), F(60))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and win.decorations.cached_bytes == 0:
        qapp_instance.processEvents()
        time.sleep(0.02)
    assert win.decorations.cached_bytes > 0, (
        "no decoration was ever produced: "
        f"pending={win.decorations.pending} failures={dict(win.decorations._failures)} "
        f"material={sorted(win.decoration_material())} view={win.timeline.view_range()} "
        f"pps={win.timeline.pps}"
    )


class Answerer:
    """Answers the window's modals by button label, so nothing needs a human."""

    def __init__(self, monkeypatch, *choices: str) -> None:
        from PySide6.QtWidgets import QMessageBox

        self.choices = list(choices)
        self.titles: list[str] = []
        monkeypatch.setattr(
            QMessageBox, "exec", lambda box, _me=self: _me.answer(box)
        )

    def answer(self, box) -> int:
        labels = [button.text().replace("&", "") for button in box.buttons()]
        self.titles.append(box.windowTitle())
        choice = self.choices.pop(0)
        if choice not in labels:
            raise AssertionError(f"{box.windowTitle()}: buttons are {labels}")
        box.buttons()[labels.index(choice)].click()
        return 0


def test_relinking_forgets_the_old_file_s_extractions(window, tmp_path, monkeypatch):
    """A relinked asset's frames must come from the new file, never the old one."""
    win, tmp, _steps, tone = window
    project_file = tmp / "film.vep"
    assert win._save_project(path=project_file)
    media = tmp / "media"
    media.mkdir()
    tone.rename(media / tone.name)  # the dub was moved out of the project folder

    answers = Answerer(monkeypatch, "Keep as is")
    second = MainWindow()
    assert second._open_project(project_file)
    assert answers.titles == ["Missing or changed media"]
    # the moved dub has no material to decode; the picture still does
    assert ("tone", 0) not in second.decoration_material()
    assert ("steps", 0) in second.decoration_material()
    assert ("tone", 0) not in second.timeline._deco_material

    fake_key = ("envelope", "tone", 0, (1, 1), F(0), F(1))
    second.decorations._envelopes.put(fake_key, type("Fake", (), {"nbytes": 128})())
    assert second.decorations.cached_bytes > 0

    target = next(asset for asset in second.bin_controller.assets if asset.id == "tone")
    assert second._relink_asset_to(target, media / tone.name)
    assert second.decorations.cached_bytes == 0, "the old file's measurement survived"
    material = second.decoration_material()
    stat = (media / tone.name).stat()
    assert material[("tone", 0)].path == media / tone.name
    assert material[("tone", 0)].identity == (stat.st_size, stat.st_mtime_ns)
    second.close()


def test_opening_another_document_forgets_the_previous_extractions(window, tmp_path):
    win, tmp, _steps, _tone = window
    project_file = tmp / "film.vep"
    assert win._save_project(path=project_file)
    win.decorations._envelopes.put(("envelope", "tone", 0, (1, 1), F(0), F(1)),
                                   type("Fake", (), {"nbytes": 128})())

    second = MainWindow()
    second._open_project(project_file)
    assert second.decorations.cached_bytes == 0
    assert set(second.decoration_material()) == {("steps", 0), ("tone", 0)}
    second.close()


def test_closing_the_window_stops_the_extraction_thread(window):
    win, _tmp, _steps, _tone = window
    assert win.decorations._thread.isRunning()
    win.close()
    assert not win.decorations._thread.isRunning()


def test_a_decoration_failure_is_stated_not_hidden(window, qapp_instance):
    """The wiring that makes the documented behaviour true.

    The layer reports why a clip has no picture or waveform, and the timeline can
    write that on a wide enough clip; the window also has to say it, or a missing
    waveform is indistinguishable from silence.
    """
    win, _tmp, _steps, _tone = window
    win.decorations.complained.emit("tone.wav stream 1: cannot decode the requested range")
    qapp_instance.processEvents()
    message = win.statusBar().currentMessage()
    assert "Timeline picture unavailable" in message
    assert "cannot decode the requested range" in message
