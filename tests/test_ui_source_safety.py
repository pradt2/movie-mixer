"""Window-level source safety: what the editor says and refuses about media.

These tests drive the real window (offscreen, prompts recorded rather than
shown) rather than the library helpers, because the point of TASK_09 is that no
path into the application may destroy or misread a user's film: exporting,
saving, showing a still frame or drawing a timeline decoration must all speak
from the same availability check.

Fixtures are synthetic generated media, never a user's files.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from media_fixtures import HAS_FFMPEG, make_colour_steps, make_tone
from video_editor.core.commands import AddClipCommand, RegisterSourceCommand
from video_editor.core.model import OutputSettings, Project, StreamSource, Track, TrackKind
from video_editor.fs.identity import AssetStatus
from video_editor.media.assets import Asset
from video_editor.render.graph import MP4_H264_AAC
from video_editor.ui import main_window as window_module
from video_editor.ui.export_dialog import ExportChoices
from video_editor.ui.main_window import MainWindow

F = Fraction


def two_source_project() -> Project:
    """Picture 0-4 s, sound 1-3 s, and one asset imported but never placed."""
    p = Project(output=OutputSettings(128, 96, F(25), 48000, "stereo"))
    for asset_id, kind, sample_rate in (
        ("steps", TrackKind.VIDEO, None),
        ("t660", TrackKind.AUDIO, 48000),
        ("unused", TrackKind.AUDIO, 48000),
    ):
        p = RegisterSourceCommand(
            StreamSource(asset_id, 0, kind, F(0), F(6), frame_rate=F(25),
                         sample_rate=sample_rate, stream_position=0)
        ).apply(p)
    p = p.with_tracks((Track("v", TrackKind.VIDEO, "V1"), Track("a", TrackKind.AUDIO, "A1")))
    p = AddClipCommand("steps", 0, "v", F(0), source_in=F(0), source_out=F(4),
                       clip_id="vid").apply(p)
    p = AddClipCommand("t660", 0, "a", F(1), source_in=F(0), source_out=F(2),
                       clip_id="aud").apply(p)
    return p


def register_asset(window: MainWindow, path: Path, asset_id: str) -> None:
    asset = Asset(
        id=asset_id, path=path, size_bytes=path.stat().st_size,
        mtime_ns=path.stat().st_mtime_ns, metadata=None,
    )
    window.bin_controller.finalize_finished(f"job-{asset_id}", path, asset, None)


@pytest.fixture()
def prompts(monkeypatch):
    """Record every prompt the window would show, and answer its first button.

    A refusal that needs a click is not an unattended check, so the text is
    captured here; answering "Yes"/"OK" lets a test reach the checks that come
    *after* consent — which is exactly where source protection lives.
    """
    asked: list[tuple[str, str, str]] = []

    class Answer:
        def __init__(self, label: str) -> None:
            self._label = label

        def exec(self) -> int:
            return 0

        def clickedButton(self):
            return self

        def text(self) -> str:
            return self._label

    def record(self, title, text, detail, buttons, default):
        asked.append((title, text, detail))
        return Answer(buttons[0][0])

    monkeypatch.setattr(MainWindow, "_message_box", record)
    return asked


@pytest.fixture()
def window(qapp_instance, tmp_path, monkeypatch):
    """A window with two used assets and one imported-but-unused asset."""
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("VIDEO_EDITOR_RECOVERY_DIR", str(tmp_path / "recovery"))
    monkeypatch.setenv("VIDEO_EDITOR_AUTOSAVE_SECONDS", "0")
    steps = make_colour_steps(tmp_path)
    tone = make_tone(tmp_path, "tone660.wav", 660)
    unused = make_tone(tmp_path, "unused.wav", 990)
    win = MainWindow()
    win.session.replace_project(two_source_project())
    register_asset(win, steps, "steps")
    register_asset(win, tone, "t660")
    register_asset(win, unused, "unused")
    win._refresh_timeline()
    win.show()
    qapp_instance.processEvents()
    yield win, tmp_path, steps, tone, unused
    win.close()


pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg required")


# ------------------------------------------------------------------ reporting


def test_a_replaced_file_is_reported_as_changed(window):
    """The distinction that matters: the file is there, and it is the wrong one."""
    win, _tmp, steps, _tone, _unused = window
    assert win._missing_asset_names() == []
    steps.write_bytes(steps.read_bytes() + b" re-encoded")
    report = win._availability()
    assert report["steps"].status is AssetStatus.CHANGED
    listed = win._missing_asset_names()
    assert len(listed) == 1
    assert "[changed:" in listed[0] and "steps.mkv" in listed[0]
    # the media-status records speak from the same observation
    states = {record.asset.id: record.state for record in win._media_records}
    assert states["steps"] is AssetStatus.CHANGED
    assert states["t660"] is AssetStatus.ONLINE


def test_a_moved_file_is_reported_as_missing(window):
    win, _tmp, _steps, tone, _unused = window
    tone.unlink()
    assert any("[missing:" in entry for entry in win._missing_asset_names())


def test_availability_is_checked_once_per_asset_not_per_clip(window, monkeypatch):
    """Forty clips over three files must cost three checks, not forty."""
    win, _tmp, _steps, _tone, _unused = window
    project = win.session.project.with_tracks(
        (*win.session.project.tracks, Track("vmany", TrackKind.VIDEO, "Many"))
    )
    for index in range(40):
        project = AddClipCommand(
            "steps", 0, "vmany", F(index, 10), source_in=F(0), source_out=F(1, 10),
            clip_id=f"many{index}",
        ).apply(project)
    win.session.replace_project(project)
    assert len(project.clips) == 42

    import video_editor.media.availability as availability

    calls: list[Path] = []
    real = availability.check

    def counting(path, recorded=None, label=None):
        calls.append(Path(path))
        return real(path, recorded, label)

    monkeypatch.setattr(availability, "check", counting)
    win._missing_asset_names()
    assert len(calls) == 3
    assert len({call.name for call in calls}) == 3


# --------------------------------------------------------------- write refusal


def _accepted(self) -> int:
    return window_module.QDialog.DialogCode.Accepted


def choose(destination: Path):
    return lambda self: ExportChoices(destination, F(0), F(2), MP4_H264_AAC)


def test_exporting_onto_an_imported_asset_is_refused(window, monkeypatch, prompts):
    """Answering "replace it?" yes is never consent to destroy a source."""
    win, tmp, steps, _tone, _unused = window
    original = steps.read_bytes()
    monkeypatch.setattr(window_module.ExportDialog, "exec", _accepted)
    monkeypatch.setattr(window_module.ExportDialog, "choices", choose(steps))
    win._export_composition()
    message = win.statusBar().currentMessage()
    assert "imported media" in message
    assert "never authorises destroying source media" in message
    assert steps.read_bytes() == original  # the film is exactly what it was
    assert not win.render_controller.running
    assert list(tmp.glob("*.partial.*")) == []  # and nothing was staged


def test_a_link_to_media_is_refused_as_a_destination(window):
    """A hard link or symlink is the same film under another name."""
    win, tmp, steps, _tone, _unused = window
    twin = tmp / "twin.mkv"
    twin.hardlink_to(steps)
    alias = tmp / "alias.mkv"
    alias.symlink_to(steps)
    guard = win._write_guard()
    for candidate in (twin, alias):
        hit = guard.collision(candidate)
        assert hit is not None and hit.path == steps
    with pytest.raises(Exception, match="same file"):
        guard.check(twin, "export")


def test_an_unused_asset_is_still_protected(window):
    """Imported and never placed is still the user's media."""
    win, _tmp, _steps, _tone, unused = window
    assert win._write_guard().collision(unused) is not None
    assert win._media_guard().collision(unused) is not None


def test_export_may_not_overwrite_the_project_file(window, monkeypatch):
    win, tmp, _steps, _tone, _unused = window
    project_file = tmp / "film.vep"
    assert win._write_project(project_file) == project_file
    saved = project_file.read_bytes()
    monkeypatch.setattr(window_module.ExportDialog, "exec", _accepted)
    monkeypatch.setattr(
        window_module.ExportDialog, "choices", choose(project_file)
    )
    win._export_composition()
    assert "project's own file" in win.statusBar().currentMessage()
    assert project_file.read_bytes() == saved  # the document is byte for byte intact
    assert win.render_controller.running is False


def test_save_as_onto_media_is_refused(window, prompts):
    win, tmp, steps, _tone, _unused = window
    original = steps.read_bytes()
    assert win._save_project(save_as=True, path=steps) is False
    assert any("Save failed" in title for title, _text, _detail in prompts)
    assert "imported media" in prompts[-1][1] + prompts[-1][2]
    assert steps.read_bytes() == original
    assert list(tmp.glob(".*tmp*")) == []  # refused before staging anything


def test_saving_the_project_document_is_allowed(window):
    """Protecting media must not break the ordinary act of saving."""
    win, tmp, _steps, _tone, _unused = window
    first = tmp / "film.vep"
    assert win._write_project(first) == first
    again = tmp / "copy.vep"
    assert win._write_project(again) == again  # a document, not media
    assert win.project_path == again


# ---------------------------------------------------------------- decorations


def test_timeline_pictures_are_withheld_for_a_changed_asset(window, qapp_instance):
    """No picture beats a confident picture of the wrong encode."""
    win, _tmp, steps, _tone, _unused = window
    assert ("steps", 0) in win.decoration_material()
    steps.write_bytes(b"a different file with the same name")
    qapp_instance.processEvents()
    assert ("steps", 0) not in win.decoration_material()
    assert "not the file recorded here" in win.statusBar().currentMessage()


def test_relinking_a_changed_asset_brings_it_back(window, qapp_instance):
    """The way out of CHANGED is an explicit, validated relink."""
    win, tmp, steps, _tone, _unused = window
    steps.unlink()
    assert any("[missing:" in entry for entry in win._missing_asset_names())
    replacement = make_colour_steps(tmp, name="steps-relink.mkv")
    assert win._relink_asset_to(win.bin_controller.assets[0], replacement) is True
    qapp_instance.processEvents()
    assert win._missing_asset_names() == []
    assert ("steps", 0) in win.decoration_material()
    assert Path(win.bin_controller.assets[0].path) == replacement


def test_relink_refuses_a_file_that_cannot_be_read(window, prompts):
    win, tmp, steps, _tone, _unused = window
    assert win._relink_asset_to(win.bin_controller.assets[0], tmp / "nowhere.mkv") is False
    assert any("Relink refused" in title for title, _text, _detail in prompts)
    assert "is not there" in prompts[-1][1]
    assert Path(win.bin_controller.assets[0].path) == steps  # nothing changed
