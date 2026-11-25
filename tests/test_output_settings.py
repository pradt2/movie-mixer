"""Output settings: the composition owns its size, rates and layout explicitly.

AGENTS.md requires the output settings to be explicit project state, never
inherited from an asset, and editable as an ordinary undoable transaction. The
core accepts any structurally valid value; the render layer is what refuses a
layout it cannot encode — that split of responsibility is pinned down here too.
"""

from fractions import Fraction

import pytest

from core_helpers import add_clip, make_base
from video_editor.core.commands import (
    CommandError,
    SetOutputSettingsCommand,
    validate_project,
)
from video_editor.core.history import ProjectSession
from video_editor.core.model import OutputSettings, Project
from video_editor.render.graph import RenderConfigError, normalise_layout
from video_editor.ui.export_dialog import ExportDialog
from video_editor.ui.main_window import MainWindow

F = Fraction


def test_settings_are_stored_exactly_including_rational_rate():
    project = SetOutputSettingsCommand(720, 576, F(24000, 1001), 44100, "5.1").apply(Project())
    out = project.output
    assert (out.width, out.height) == (720, 576)
    assert out.frame_rate == F(24000, 1001)  # never approximated as a float
    assert (out.sample_rate, out.channel_layout) == (44100, "5.1")
    assert validate_project(project) == []


def test_output_settings_are_not_inherited_from_any_asset():
    """A 128x96 @ 25 fps asset does not make that the composition's settings."""
    project = make_base()
    assert project.output == OutputSettings(1920, 1080, F(25), 48000, "stereo")
    assert (project.output.width, project.output.height) != (128, 96)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"width": 0},
        {"height": -8},
        {"frame_rate": F(0)},
        {"frame_rate": F(-1, 3)},
        {"sample_rate": 0},
        {"channel_layout": "   "},
    ],
)
def test_invalid_settings_are_rejected_atomically(kwargs):
    command = SetOutputSettingsCommand(
        width=kwargs.get("width", 640),
        height=kwargs.get("height", 360),
        frame_rate=kwargs.get("frame_rate", F(25)),
        sample_rate=kwargs.get("sample_rate", 48000),
        channel_layout=kwargs.get("channel_layout", "stereo"),
    )
    project = Project()
    with pytest.raises(CommandError):
        command.apply(project)
    assert project == Project()  # untouched


def test_repeating_the_current_settings_is_refused():
    project = SetOutputSettingsCommand(640, 360, F(25), 48000, "mono").apply(Project())
    with pytest.raises(CommandError, match="already those values"):
        SetOutputSettingsCommand(640, 360, F(25), 48000, "mono").apply(project)


def test_layout_encodability_is_the_render_layers_decision():
    """The core stores any non-empty layout; encoding it is refused where it happens."""
    project = SetOutputSettingsCommand(640, 360, F(25), 48000, "1.0").apply(Project())
    assert project.output.channel_layout == "1.0"
    with pytest.raises(RenderConfigError, match="not supported for export"):
        normalise_layout(project.output.channel_layout)
    assert normalise_layout("5.1(side)") == "5.1"  # documented normalisation, not a guess


def test_settings_change_is_undoable_and_redoable():
    session = ProjectSession()
    session.apply(SetOutputSettingsCommand(1920, 1080, F(24), 48000, "stereo"))
    session.apply(SetOutputSettingsCommand(640, 480, F(25), 44100, "5.1"))
    assert session.project.output.sample_rate == 44100
    session.undo()
    assert session.project.output == OutputSettings(1920, 1080, F(24), 48000, "stereo")
    session.redo()
    assert session.project.output.channel_layout == "5.1"
    session.undo()
    session.undo()
    assert session.project == Project()


def test_changed_settings_do_not_move_or_retime_any_clip():
    project = add_clip(make_base(), "c1", "vt", F(1), F(2))
    changed = SetOutputSettingsCommand(320, 240, F(30), 44100, "mono").apply(project)
    assert [(c.id, c.output_start, c.output_end, c.track_id) for c in changed.clips] == [
        (c.id, c.output_start, c.output_end, c.track_id) for c in project.clips
    ]
    assert [t.id for t in changed.tracks] == [t.id for t in project.tracks]


# ------------------------------------------------------------------------ GUI


@pytest.fixture()
def window(qapp_instance):
    win = MainWindow()
    yield win
    win.close()
    win.deleteLater()


def test_inspector_shows_the_projects_output_settings(window):
    window.inspector.show_output(window.session.project)
    assert window.inspector.out_width.text() == "1920"
    assert window.inspector.out_frame_rate.text() == "25"
    assert window.inspector.out_sample_rate.text() == "48000"
    assert window.inspector.out_layout_box.currentText() == "stereo"
    # the choice offered is exactly what the renderer can encode
    layouts = [window.inspector.out_layout_box.itemText(i)
               for i in range(window.inspector.out_layout_box.count())]
    assert layouts == ["2.1", "3.0", "5.0", "5.1", "7.0", "7.1", "mono", "stereo"]


def test_apply_from_the_inspector_is_an_undoable_project_edit(window):
    window.inspector.show_output(window.session.project)
    window.inspector.out_width.setText("720")
    window.inspector.out_height.setText("576")
    window.inspector.out_frame_rate.setText("24000/1001")
    window.inspector.out_sample_rate.setText("44100")
    window.inspector.out_layout_box.setCurrentText("5.1")
    window.inspector.output_apply.click()
    out = window.session.project.output
    assert (out.width, out.height, out.frame_rate, out.sample_rate) == (
        720, 576, F(24000, 1001), 44100,
    )
    assert out.channel_layout == "5.1"
    assert window.session.can_undo
    window._undo()
    assert window.session.project.output == Project().output
    # fields follow the project again instead of keeping the typed text
    assert window.inspector.out_frame_rate.text() == "25"


def test_bad_output_input_is_marked_and_applies_nothing(window):
    problems: list[str] = []
    window.inspector.validationError.connect(problems.append)
    window.inspector.show_output(window.session.project)
    window.inspector.out_frame_rate.setText("25/")
    window.inspector.out_width.setText("0")
    window.inspector.output_apply.click()
    assert window.session.project == Project()
    assert not window.session.can_undo
    assert len(problems) == 1
    assert "Output settings not applied" in problems[0]
    assert "frame rate" in problems[0] and "width" in problems[0]
    assert "border" in window.inspector.out_frame_rate.styleSheet()
    # correcting the fields applies everything at once
    window.inspector.show_output(window.session.project)
    window.inspector.out_frame_rate.setText("30000/1001")
    window.inspector.output_apply.click()
    assert window.session.project.output.frame_rate == F(30000, 1001)


def test_export_summary_reflects_the_current_output_settings(qapp_instance, tmp_path):
    project = add_clip(make_base(), "c1", "vt", F(0), F(2))
    project = SetOutputSettingsCommand(320, 240, F(30), 44100, "mono").apply(project)
    dialog = ExportDialog(None, project, tmp_path / "out.mp4", playhead=F(0))
    summary = dialog.summary.toPlainText()
    assert "320x240, mono" in summary
    assert "60 frames @ 30/1 fps" in summary
    assert "88200 samples @ 44100 Hz" in summary
    dialog.deleteLater()
