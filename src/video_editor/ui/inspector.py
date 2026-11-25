"""Inspector: read-only details plus precise numeric editing controls.

All edits are emitted as parsed exact values; the window turns them into
core commands. Precision entry never snaps. Invalid input is reported
in-place (red field + message), never via modal dialogs, and nothing is
applied until every non-empty field parses.
"""

from __future__ import annotations

import math
from fractions import Fraction

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.model import Clip, Project, Track
from ..render.graph import SUPPORTED_LAYOUTS
from .format import format_time, parse_time_text

_BAD_STYLE = "border: 1px solid #c04040;"


def gain_to_db(gain: Fraction) -> str:
    if gain <= 0:
        return "-inf"
    return f"{20 * math.log10(float(gain)):.3f}"


def db_to_gain(db_text: str) -> Fraction:
    """dB -> rational gain (decimal approximation at 1 µ, documented)."""
    db = Fraction(db_text)
    linear = 10 ** (float(db) / 20)
    return Fraction(f"{linear:.6f}")


class Inspector(QWidget):
    clipFieldsApply = Signal(object)  # (clip_id, dict[str, Fraction])
    linkSelected = Signal()
    unlinkRequested = Signal(str)
    trackRename = Signal(str, str)
    trackMove = Signal(str, int)
    outputSettingsApply = Signal(object)  # (width, height, frame_rate, sample_rate, layout)
    validationError = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self.details = QTreeWidget(self)
        self.details.setColumnCount(2)
        self.details.setHeaderLabels(["Property", "Value"])
        self.details.setAlternatingRowColors(True)
        layout.addWidget(self.details, 2)

        # ------------------------------------------------------- clip editor
        self.clip_group = QGroupBox("Clip (exact values)", self)
        form = QFormLayout(self.clip_group)
        self.source_in = QLineEdit()
        self.source_out = QLineEdit()
        self.output_start = QLineEdit()
        self.rate = QLineEdit()
        self.gain_db = QLineEdit()
        self.fade_in = QLineEdit()
        self.fade_out = QLineEdit()
        self.source_in.setPlaceholderText("H:MM:SS.mmm or seconds")
        self.rate.setPlaceholderText("e.g. 24000/1001")
        self.gain_db.setPlaceholderText("dB (0 = unchanged)")
        for key, widget in (
            ("Source in", self.source_in),
            ("Source out", self.source_out),
            ("Output start", self.output_start),
            ("Rate (src s/out s)", self.rate),
            ("Gain dB", self.gain_db),
            ("Fade in (s)", self.fade_in),
            ("Fade out (s)", self.fade_out),
        ):
            form.addRow(key, widget)
        self.apply_button = QPushButton("Apply")
        form.addRow(self.apply_button)
        layout.addWidget(self.clip_group)

        link_row = QHBoxLayout()
        self.link_button = QPushButton("Link selected clips")
        self.unlink_button = QPushButton("Unlink selected")
        link_row.addWidget(self.link_button)
        link_row.addWidget(self.unlink_button)
        layout.addLayout(link_row)

        # ------------------------------------------------------ track editor
        self.track_group = QGroupBox("Track", self)
        track_layout = QFormLayout(self.track_group)
        self.track_name = QLineEdit()
        rename_row = QHBoxLayout()
        rename_row.addWidget(self.track_name)
        self.rename_button = QPushButton("Rename")
        rename_row.addWidget(self.rename_button)
        track_layout.addRow("Name", rename_row)
        move_row = QHBoxLayout()
        self.move_up = QPushButton("Move up")
        self.move_down = QPushButton("Move down")
        move_row.addWidget(self.move_up)
        move_row.addWidget(self.move_down)
        track_layout.addRow("Order", move_row)
        layout.addWidget(self.track_group)

        # ---------------------------------------------------- output settings
        # The composition's own size/rate/layout: never inherited from an asset.
        self.output_group = QGroupBox("Output (composition)", self)
        out_form = QFormLayout(self.output_group)
        self.out_width = QLineEdit()
        self.out_height = QLineEdit()
        self.out_frame_rate = QLineEdit()
        self.out_sample_rate = QLineEdit()
        self.out_layout_box = QComboBox()
        self.out_layout_box.addItems(sorted(SUPPORTED_LAYOUTS))
        self.out_width.setPlaceholderText("pixels")
        self.out_height.setPlaceholderText("pixels")
        self.out_frame_rate.setPlaceholderText("e.g. 24000/1001")
        self.out_sample_rate.setPlaceholderText("Hz")
        out_form.addRow("Width", self.out_width)
        out_form.addRow("Height", self.out_height)
        out_form.addRow("Frame rate", self.out_frame_rate)
        out_form.addRow("Sample rate", self.out_sample_rate)
        out_form.addRow("Channel layout", self.out_layout_box)
        self.output_apply = QPushButton("Apply output settings")
        out_form.addRow(self.output_apply)
        layout.addWidget(self.output_group)
        layout.addStretch(1)

        self._clip_id: str | None = None
        self._track_id: str | None = None
        self._selection_size = 0

        self.apply_button.clicked.connect(self._on_apply)
        self.link_button.clicked.connect(self.linkSelected.emit)
        self.unlink_button.clicked.connect(self._on_unlink)
        self.rename_button.clicked.connect(self._on_rename)
        self.move_up.clicked.connect(lambda: self._on_move(-1))
        self.move_down.clicked.connect(lambda: self._on_move(1))
        self.output_apply.clicked.connect(self._on_output_apply)
        self.set_selection(None, None, 0)

    # ------------------------------------------------------------------ state
    def set_selection(self, clip: Clip | None, track: Track | None, selection_size: int) -> None:
        self._clip_id = clip.id if clip else None
        self._track_id = track.id if track else None
        self._current_track = track
        self._selection_size = selection_size
        self.clip_group.setEnabled(clip is not None)
        self.track_group.setEnabled(track is not None)
        self.link_button.setEnabled(selection_size >= 2)
        self.unlink_button.setEnabled(clip is not None and clip.link_group is not None)
        if clip is not None:
            self.source_in.setText(format_time(clip.source_in))
            self.source_out.setText(format_time(clip.source_out))
            self.output_start.setText(format_time(clip.output_start))
            self.rate.setText(str(clip.rate))
            self.gain_db.setText(gain_to_db(clip.gain))
            self.fade_in.setText(format_time(clip.fade_in))
            self.fade_out.setText(format_time(clip.fade_out))
        for widget in (
            self.source_in,
            self.source_out,
            self.output_start,
            self.rate,
            self.gain_db,
            self.fade_in,
            self.fade_out,
            self.track_name,
        ):
            widget.setStyleSheet("")
        if track is not None:
            self.track_name.setText(track.name)

    def clear_details(self) -> None:
        self.details.clear()

    # ---------------------------------------------------------------- details
    def show_clip(self, project: Project, clip: Clip) -> None:
        self.details.clear()
        track = project.track(clip.track_id)
        source = project.source(clip.asset_id, clip.stream_index)
        self._add("Selection", "clip")
        self._add("Clip id", clip.id)
        self._add("Asset", clip.asset_id)
        self._add("Stream index", str(clip.stream_index))
        self._add(
            "Track",
            f"{track.name if track else '?'} ({track.kind.value if track else '?'})",
        )
        self._add("Source in", format_time(clip.source_in))
        self._add("Source out", format_time(clip.source_out))
        self._add("Output start", format_time(clip.output_start))
        self._add("Output end", format_time(clip.output_end))
        self._add("Output duration", format_time(clip.output_duration))
        self._add("Rate (src s / out s)", f"{float(clip.rate):.4f} [{clip.rate}]")
        self._add("Gain", f"{clip.gain} [{gain_to_db(clip.gain)} dB]")
        self._add("Fade in / out", f"{format_time(clip.fade_in)} / {format_time(clip.fade_out)}")
        self._add("Link group", clip.link_group or "none")
        if clip.link_group is not None:
            members = project.linked_clips(clip.id)
            self._add("Linked with", f"{len(members) - 1} clip(s): edits apply as a group")
        if source is not None and source.conservative_end:
            self._add("Note", "stream end is a container-derived upper bound")
        self.set_selection(clip, track, max(self._selection_size, 1))

    def show_track(self, project: Project, track: Track) -> None:
        self.details.clear()
        self._add("Selection", "track")
        self._add("Track id", track.id)
        self._add("Name", track.name)
        self._add("Kind", track.kind.value)
        self._add("Enabled", "yes" if track.enabled else "no")
        if track.kind.value == "audio":
            self._add("Muted", "yes" if track.muted else "no")
        self._add("Clips", str(len(project.clips_on_track(track.id))))
        self.set_selection(None, track, self._selection_size)

    def show_asset(self, asset) -> None:
        self.details.clear()
        self._add("Selection", "asset")
        self._add("Name", asset.display_name())
        self._add("Path", str(asset.path))
        if asset.metadata is not None:
            self._add("Format", asset.metadata.format_name or "unknown")
            self._add("Container length", format_time(asset.metadata.container_duration))
            self._add("Streams", str(len(asset.metadata.streams)))

    def show_output(self, project: Project) -> None:
        """Show the composition's current output settings (project state)."""
        out = project.output
        self.out_width.setText(str(out.width))
        self.out_height.setText(str(out.height))
        self.out_frame_rate.setText(str(out.frame_rate))
        self.out_sample_rate.setText(str(out.sample_rate))
        index = self.out_layout_box.findText(out.channel_layout)
        if index >= 0:
            self.out_layout_box.setCurrentIndex(index)
        for widget in (self.out_width, self.out_height, self.out_frame_rate,
                       self.out_sample_rate):
            widget.setStyleSheet("")

    def show_multi_selection(self, count: int) -> None:
        self.details.clear()
        self._add("Selection", f"{count} clips (linked clips edit as a group)")
        self.set_selection(None, self._current_track, count)

    def _add(self, key: str, value: str) -> None:
        self.details.addTopLevelItem(QTreeWidgetItem([key, value]))

    # ----------------------------------------------------------------- edits
    def _on_apply(self) -> None:
        if self._clip_id is None:  # pragma: no cover - group disabled
            return
        fields: dict[str, Fraction] = {}
        errors: list[str] = []

        def parse_time(widget: QLineEdit, name: str, key: str) -> None:
            text = widget.text().strip()
            if not text:
                return
            try:
                fields[key] = parse_time_text(text)
            except ValueError:
                widget.setStyleSheet(_BAD_STYLE)
                errors.append(name)

        parse_time(self.source_in, "source in", "source_in")
        parse_time(self.source_out, "source out", "source_out")
        parse_time(self.output_start, "output start", "output_start")
        parse_time(self.fade_in, "fade in", "fade_in")
        parse_time(self.fade_out, "fade out", "fade_out")
        text = self.rate.text().strip()
        if text:
            try:
                rate = Fraction(text)
                if rate <= 0:
                    raise ValueError
                fields["rate"] = rate
            except ValueError:
                self.rate.setStyleSheet(_BAD_STYLE)
                errors.append("rate (must be a positive rational)")
        text = self.gain_db.text().strip()
        if text:
            try:
                fields["gain"] = db_to_gain(text)
            except (ValueError, ZeroDivisionError):
                self.gain_db.setStyleSheet(_BAD_STYLE)
                errors.append("gain dB")
        if errors:
            self.validationError.emit("Invalid inspector input: " + ", ".join(errors))
            return
        if fields:
            self.clipFieldsApply.emit((self._clip_id, fields))

    def _on_unlink(self) -> None:
        if self._clip_id is not None:
            self.unlinkRequested.emit(self._clip_id)

    def _on_rename(self) -> None:
        if self._track_id is not None:
            self.trackRename.emit(self._track_id, self.track_name.text())

    def _on_output_apply(self) -> None:
        """Parse the output fields as a unit: all or nothing, marked in place."""
        errors: list[str] = []

        def positive_int(widget: QLineEdit, name: str) -> int | None:
            try:
                value = int(widget.text().strip())
                if value <= 0:
                    raise ValueError
                return value
            except ValueError:
                widget.setStyleSheet(_BAD_STYLE)
                errors.append(name)
                return None

        def positive_rate(widget: QLineEdit, name: str) -> Fraction | None:
            try:
                value = Fraction(widget.text().strip())
                if value <= 0:
                    raise ValueError
                return value
            except (ValueError, ZeroDivisionError):
                widget.setStyleSheet(_BAD_STYLE)
                errors.append(name)
                return None

        width = positive_int(self.out_width, "width (whole pixels)")
        height = positive_int(self.out_height, "height (whole pixels)")
        frame_rate = positive_rate(self.out_frame_rate, "frame rate (positive rational)")
        sample_rate = positive_int(self.out_sample_rate, "sample rate (whole Hz)")
        layout = self.out_layout_box.currentText()
        if layout not in SUPPORTED_LAYOUTS:
            self.out_layout_box.setStyleSheet(_BAD_STYLE)
            errors.append(f"channel layout {layout} cannot be encoded")
        if errors:
            self.validationError.emit(
                "Output settings not applied: " + ", ".join(errors)
                + ". Frame rate is exact as entered (e.g. 24000/1001)."
            )
            return
        self.out_layout_box.setStyleSheet("")
        self.outputSettingsApply.emit((width, height, frame_rate, sample_rate, layout))

    def _on_move(self, delta: int) -> None:
        if self._track_id is not None:
            self.trackMove.emit(self._track_id, delta)
