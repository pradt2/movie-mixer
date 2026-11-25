"""Export dialog: destination, preset and render range, with plan feedback.

The dialog shows what the plan *will* do (frame/sample counts, warnings such as
conservative stream ends or possible audio overload) before anything runs, and
refuses to start on an unparseable range or a missing output directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.model import Project
from ..render.graph import MP4_H264_AAC, CodecPreset
from ..render.plan import RenderPlanError, build_plan
from .format import parse_time_text

F = Fraction
ZERO = F(0)

PRESETS: tuple[CodecPreset, ...] = (MP4_H264_AAC,)

RANGE_WHOLE = "Whole composition"
RANGE_FROM_PLAYHEAD = "From playhead to end"
RANGE_CUSTOM = "Custom range"


@dataclass(frozen=True)
class ExportChoices:
    destination: Path
    out_start: Fraction
    out_end: Fraction | None
    preset: CodecPreset


class ExportDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        project: Project,
        suggested_destination: Path,
        playhead: Fraction = ZERO,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export composition")
        self.project = project
        self.playhead = playhead
        self._error_label = QLabel("", self)
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color: #b04040;")

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.destination = QLineEdit(str(suggested_destination), self)
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.destination)
        row.addWidget(browse)
        form.addRow("Output file", row)

        self.preset_box = QComboBox(self)
        for preset in PRESETS:
            self.preset_box.addItem(preset.display, preset.name)
        form.addRow("Format", self.preset_box)

        self.range_box = QComboBox(self)
        self.range_box.addItems([RANGE_WHOLE, RANGE_FROM_PLAYHEAD, RANGE_CUSTOM])
        form.addRow("Range", self.range_box)

        self.start_edit = QLineEdit(self)
        self.start_edit.setPlaceholderText("H:MM:SS.mmm, seconds, or 24000/1001")
        self.end_edit = QLineEdit(self)
        self.end_edit.setPlaceholderText("leave empty for the end of the composition")
        form.addRow("From", self.start_edit)
        form.addRow("To", self.end_edit)
        layout.addLayout(form)

        self.summary = QPlainTextEdit(self)
        self.summary.setReadOnly(True)
        self.summary.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        group = QGroupBox("What will be rendered", self)
        group_layout = QVBoxLayout(group)
        group_layout.addWidget(self.summary)
        layout.addWidget(group)
        layout.addWidget(self._error_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        for widget in (self.destination, self.start_edit, self.end_edit):
            widget.textChanged.connect(self._refresh)
        self.range_box.currentIndexChanged.connect(self._refresh)
        self._refresh()
        self.resize(560, 460)

    # ---------------------------------------------------------------- helpers
    def _browse(self) -> None:
        suggested = Path(self.destination.text().strip() or "composition.mp4")
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Export to", str(suggested), "MP4 (H.264/AAC) (*.mp4)"
        )
        if chosen:
            self.destination.setText(chosen)

    def _range(self) -> tuple[Fraction, Fraction | None]:
        mode = self.range_box.currentText()
        if mode == RANGE_WHOLE:
            return F(0), None
        if mode == RANGE_FROM_PLAYHEAD:
            return self.playhead, None
        start = parse_time_text(self.start_edit.text() or "0")
        end = parse_time_text(self.end_edit.text()) if self.end_edit.text().strip() else None
        return start, end

    def choices(self) -> ExportChoices:
        start, end = self._range()
        preset = next(p for p in PRESETS if p.name == self.preset_box.currentData())
        destination = Path(self.destination.text().strip())
        if destination.suffix == "":
            destination = destination.with_suffix(f".{preset.extension}")
        return ExportChoices(destination, start, end, preset)

    # ------------------------------------------------------------ validation
    def _refresh(self) -> None:
        self._error_label.setText("")
        try:
            chosen = self.choices()
        except ValueError as exc:
            self._show_error(f"Invalid range: {exc}")
            return
        try:
            plan = build_plan(self.project, chosen.out_start, chosen.out_end)
        except RenderPlanError as exc:
            self._show_error(str(exc))
            return
        if chosen.destination.parent != Path() and not chosen.destination.parent.is_dir():
            self._show_error(f"Output directory does not exist: {chosen.destination.parent}")
            return
        if chosen.destination.exists():
            self._error_label.setText(
                f"{chosen.destination} exists; you will be asked before it is replaced."
            )
        q = plan.quantisation
        lines = [
            f"Duration: {float(plan.duration):.3f}s  "
            f"({plan.total_frames} frames @ "
            f"{q.frame_rate.numerator}/{q.frame_rate.denominator} fps, "
            f"{plan.total_samples} samples @ {q.sample_rate} Hz)",
            f"Picture: {len(plan.video_items)} item(s), "
            f"{sum(1 for i in plan.video_items if i.is_black)} black",
            f"Audio: {len(plan.audio_items)} contribution(s); mix is a plain sum "
            "(no normalisation)",
            f"Geometry: {plan.width}x{plan.height}, {plan.channel_layout}, "
            "aspect preserved with padding, yuv420p",
            "Colour: no colour-space conversion or HDR handling is applied",
        ]
        lines += [f"Warning: {warning}" for warning in plan.warnings]
        self.summary.setPlainText("\n".join(lines))
        self._set_accept_enabled(True)

    def _show_error(self, message: str) -> None:
        self._error_label.setText(message)
        self.summary.setPlainText("Cannot start this export.")
        self._set_accept_enabled(False)

    def _set_accept_enabled(self, enabled: bool) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(enabled)

    def _on_accept(self) -> None:
        try:
            self.choices()
        except ValueError as exc:
            self._show_error(f"Invalid range: {exc}")
            return
        if not self.destination.text().strip():
            self._show_error("Choose an output file.")
            return
        self.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)
