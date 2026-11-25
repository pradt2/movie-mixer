"""Viewer area with strictly separated source and output (composition) modes.

Source mode plays media files with Qt Multimedia. Playback always states *what*
is being played: the whole file (backend-chosen elementary streams) or an
FFmpeg-generated preview of one specific stream.

Output mode shows the composition: a rendered preview range (same planner and
composition semantics as export, reduced picture size), still frames of exact
output frames with the source PTS they came from, and a plain-text state line
that says whether the rendered range is current, stale, rendering or failed.
Composition playback is never simulated with source playback: if the preview
file cannot be played, the failure is reported and the still frame stays up.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core.model import Project
from ..core.timeutils import ONE, ZERO
from ..media.assets import Asset, StreamMetadata
from ..media.availability import check_asset
from ..media.extractor import StreamPreview, extract_stream_window
from ..media.framegrab import StillFrame
from ..render.preview import DEFAULT_PREVIEW_SECONDS
from .format import format_time, parse_time_text

F = Fraction
HALF = Fraction(1, 2)
QUARTER = Fraction(1, 4)
EIGHTH = Fraction(1, 8)


class SourceViewer(QWidget):
    """Source-mode playback with honest stream provenance labelling."""

    statusMessage = Signal(str)
    positionChanged = Signal(object)  # asset-local Fraction while playing

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.asset: Asset | None = None
        self.stream: StreamMetadata | None = None
        self.preview: StreamPreview | None = None

        self.player = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_out)

        self.provenance = QLabel("No source selected.")
        self.provenance.setWordWrap(True)
        self.provenance.setObjectName("viewer_provenance")

        self.time_label = QLabel("--:--:--.--- / unknown")
        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)

        self.play_button = QPushButton("Play")
        self.play_button.setEnabled(False)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.step_back_button = QPushButton("◀ frame")
        self.step_fwd_button = QPushButton("frame ▶")
        for b in (self.step_back_button, self.step_fwd_button):
            b.setEnabled(False)
            b.setToolTip("Approximate single-frame nudge (based on nominal frame rate)")
        self.preview_button = QPushButton("Play selected stream (FFmpeg preview)")
        self.preview_button.setEnabled(False)
        self.whole_file_button = QPushButton("Play whole file")
        self.whole_file_button.setEnabled(False)

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorChanged.connect(self._on_error)

        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        controls.addWidget(self.play_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.step_back_button)
        controls.addWidget(self.step_fwd_button)
        controls.addStretch(1)
        controls.addWidget(self.time_label)
        layout.addLayout(controls)
        layout.addWidget(self.seek_slider)
        buttons = QHBoxLayout()
        buttons.addWidget(self.whole_file_button)
        buttons.addWidget(self.preview_button)
        layout.addLayout(buttons)
        layout.addWidget(self.provenance)

        self.play_button.clicked.connect(self._toggle_play)
        self.stop_button.clicked.connect(self.player.stop)
        self.step_back_button.clicked.connect(lambda: self._step(-1))
        self.step_fwd_button.clicked.connect(lambda: self._step(1))
        self.preview_button.clicked.connect(self._make_stream_preview)
        self.whole_file_button.clicked.connect(self._play_whole_file)

    # ------------------------------------------------------------- selection
    def set_source(self, asset: Asset | None, stream: StreamMetadata | None) -> None:
        """Update the navigation context (what source timebase we are in)."""
        self.player.stop()
        self.preview = None
        self.asset = asset
        self.stream = stream
        self._update_buttons()
        self._update_labels()

    def _update_buttons(self) -> None:
        has_asset = self.asset is not None
        self.play_button.setEnabled(has_asset)
        self.whole_file_button.setEnabled(has_asset)
        self.preview_button.setEnabled(
            has_asset and self.stream is not None and self.stream.bounds_known
        )
        has_rate = self.stream is not None and self.stream.frame_rate not in (None, 0)
        self.step_back_button.setEnabled(has_rate)
        self.step_fwd_button.setEnabled(has_rate)

    def _update_labels(self) -> None:
        if self.asset is None:
            self.provenance.setText("No source selected.")
            return
        parts = [f"Source: {self.asset.display_name()}"]
        if self.stream is not None:
            parts.append(
                f"navigated stream: {self.stream.kind.value} #{self.stream.stream_index}"
            )
        parts.append("timebase: asset-local (probe origin)")
        self.provenance.setText(" · ".join(parts))

    # ------------------------------------------------------------- playback
    def _source_is_usable(self) -> bool:
        """False (and said why) when this asset's file is not the recorded one.

        Playing a replaced file would be playing *something*: the timings, stream
        indexes and bounds this project recorded may not describe it.
        """
        entry = check_asset(self.asset)
        if entry.usable:
            return True
        self.statusMessage.emit(f"Cannot play this source: {entry.describe()}")
        return False

    def _play_whole_file(self) -> None:
        if self.asset is None:
            return
        if not self._source_is_usable():
            return
        self.preview = None
        self.player.setSource(QUrl.fromLocalFile(str(self.asset.path)))
        self._update_provenance_whole_file()
        self.player.play()

    def _update_provenance_whole_file(self) -> None:
        assert self.asset is not None
        self.provenance.setText(
            f"Playing WHOLE FILE {self.asset.display_name()} — the playback backend "
            "chooses which elementary streams are audible/visible; picture is not "
            "guaranteed for a specific video stream."
        )

    def _make_stream_preview(self) -> None:
        if self.asset is None or self.stream is None:
            return
        if not self._source_is_usable():
            return
        start = self.stream.origin
        end = self.stream.end_time
        duration = (end if end is not None else start + 60) - start
        result = extract_stream_window(
            self.asset.path,
            self.stream.stream_index,
            kind=self.stream.kind.value,
            start=start,
            duration=duration,
        )
        if result is None:
            self.statusMessage.emit(
                "FFmpeg could not extract a preview of this stream; nothing was played "
                "in its place."
            )
            return
        self.preview = result
        self.player.setSource(QUrl.fromLocalFile(str(result.path)))
        lang = f" [{self.stream.language}]" if self.stream.language else ""
        self.provenance.setText(
            f"Playing FFmpeg PREVIEW of {self.stream.kind.value} stream "
            f"#{self.stream.stream_index}{lang} from {self.asset.display_name()}, "
            f"asset-local {format_time(result.window_start)} to "
            f"{format_time(result.window_start + result.duration)}."
        )
        self.statusMessage.emit(
            f"Stream preview ready: {result.path.name} "
            f"({format_time(result.duration)})"
        )
        self.player.play()

    def _toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        elif (
            self.preview is None
            and self.asset is not None
            and not self.player.source().toString()
        ):
            self._play_whole_file()
        else:
            self.player.play()

    def _on_seek_moved(self, value: int) -> None:
        self.player.setPosition(value)

    def _current_window_start(self) -> Fraction:
        return self.preview.window_start if self.preview else F(0)

    def _on_position(self, ms: int) -> None:
        window_start = self._current_window_start()
        asset_local = window_start + F(ms, 1000)
        duration = self.player.duration()
        total = (
            window_start + F(duration, 1000) if duration > 0 else F(0)
        )
        self.seek_slider.blockSignals(True)
        self.seek_slider.setRange(0, max(0, duration))
        self.seek_slider.setValue(ms)
        self.seek_slider.blockSignals(False)
        self.time_label.setText(
            f"{format_time(asset_local)} / {format_time(total) if duration > 0 else 'unknown'}"
        )
        self.positionChanged.emit(asset_local)

    def _on_duration(self, ms: int) -> None:
        self.seek_slider.setRange(0, max(0, ms))

    def _on_state(self, state) -> None:
        self.play_button.setText("Pause" if state == QMediaPlayer.PlayingState else "Play")

    def _on_error(self, error) -> None:
        if error == QMediaPlayer.NoError:
            return
        if self.asset is not None:
            self.statusMessage.emit(
                f"Playback backend cannot play {self.asset.display_name()}: "
                f"{self.player.errorString()}. Try 'Play selected stream (FFmpeg preview)'."
            )

    def _step(self, direction: int) -> None:
        if self.stream is None or not self.stream.frame_rate:
            return
        frame = F(self.stream.frame_rate.denominator, self.stream.frame_rate.numerator)
        ms = self.player.position()
        new_ms = int(float(ms + direction * frame * 1000))
        self.player.setPosition(max(0, new_ms))
        self.statusMessage.emit(
            f"Approximate frame step ({float(self.stream.frame_rate):g} fps nominal); "
            "exact PTS stepping arrives with composition preview."
        )

    def stop(self) -> None:
        self.player.stop()


class OutputViewer(QWidget):
    """Composition (output) preview: rendered range plus still-frame inspection.

    Two clearly separated things happen here:

    * **Range preview** — a user-selected output range is rendered by the same
      planner/encoder path as export (at reduced picture size) and can be played.
      It is always labelled with the exact range and picture size it represents.
    * **Still-frame inspection** — the picture at one output frame, decoded through
      the composition rules, with the source PTS it came from.

    The state line is explicit in words (never colour alone): *no preview*,
    *rendering*, *current*, *stale* or *failed*. A stale preview is never presented
    as current output.
    """

    previewRequested = Signal(object, object, object)  # start, end, scale (Fractions)
    cancelPreviewRequested = Signal()
    freeCacheRequested = Signal()
    playheadStepped = Signal(object)  # Fraction: new output time (timeline moves)
    sourceStepRequested = Signal(int)  # -1/+1 neighbour of the shown source frame
    sourceMarkRequested = Signal(str)  # "in"/"out": set clip edge to shown source PTS
    playbackRequested = Signal()
    pauseRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._range: tuple[Fraction, Fraction] | None = None
        self._scale: Fraction | None = None
        self._file: Path | None = None
        self._frame: StillFrame | None = None
        self._playing = False
        self._range_edited = False

        header = QLabel("OUTPUT (composition) timebase")
        header.setObjectName("output_viewer_header")

        self.state = QLabel("No preview rendered yet.")
        self.state.setObjectName("output_viewer_state")

        self.still = QLabel("No frame shown: move the playhead or render a preview.")
        self.still.setObjectName("output_viewer_still")
        self.still.setAlignment(Qt.AlignCenter)
        self.still.setMinimumSize(320, 180)
        scroll = QScrollArea(self)
        scroll.setWidget(self.still)
        scroll.setWidgetResizable(True)

        self.time_label = QLabel("Playhead: 0:00:00.000 (output time)")
        self.provenance = QLabel("")
        self.provenance.setWordWrap(True)
        self.provenance.setObjectName("output_viewer_provenance")

        # ---- range controls
        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("Preview range from"))
        self.range_start = QLineEdit()
        self.range_start.setPlaceholderText("start")
        self.range_start.setObjectName("preview_range_start")
        self.range_end = QLineEdit()
        self.range_end.setPlaceholderText("end")
        self.range_end.setObjectName("preview_range_end")
        for box in (self.range_start, self.range_end):
            box.setMaximumWidth(120)
            range_row.addWidget(box)
        range_row.addWidget(QLabel("scale"))
        self.scale_box = QComboBox()
        for label, scale in (("1:1", ONE), ("1:2", HALF), ("1:4", QUARTER), ("1:8", EIGHTH)):
            self.scale_box.addItem(label, scale)
        self.scale_box.setCurrentIndex(1)
        self.scale_box.setToolTip(
            "Preview picture size. Timing, stacking, gaps and the audio mix are "
            "identical to export at any size."
        )
        range_row.addWidget(self.scale_box)
        self.render_button = QPushButton("Preview around playhead")
        self.render_button.setObjectName("preview_render_button")
        self.render_button.setToolTip(
            "Render the selected range (or a window around the playhead if the "
            "range fields are empty) into the preview cache."
        )
        range_row.addWidget(self.render_button)
        # The range fields select an **output interval** for review, and that is
        # said where the interval is typed: the preview is otherwise easy to read
        # as "some approximation of the whole thing".
        interval = QLabel(
            f"the fields choose an output interval; empty means "
            f"{float(DEFAULT_PREVIEW_SECONDS):.0f} s around the playhead"
        )
        interval.setObjectName("preview_range_hint")
        interval.setToolTip(
            "Whatever range is rendered is a real render of the composition: same "
            "planner, stacking, trims, rates and mix as export, only smaller "
            "pictures. Cost grows with the range, so review boundaries with a "
            "short interval and use Export for long ones."
        )
        range_row.addWidget(interval)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("preview_cancel_button")
        self.cancel_button.setEnabled(False)
        range_row.addWidget(self.cancel_button)
        self.free_cache_button = QPushButton("Free preview cache")
        self.free_cache_button.setObjectName("preview_free_cache_button")
        range_row.addWidget(self.free_cache_button)
        range_row.addStretch(1)

        # ---- inspection / transport controls
        transport = QHBoxLayout()
        self.play_button = QPushButton("Play preview")
        self.play_button.setObjectName("preview_play_button")
        self.play_button.setEnabled(False)
        transport.addWidget(self.play_button)
        self.step_back_button = QPushButton("◀ frame")
        self.step_fwd_button = QPushButton("frame ▶")
        for button in (self.step_back_button, self.step_fwd_button):
            button.setToolTip("Step one output frame (at the project frame rate)")
            transport.addWidget(button)
        self.source_back_button = QPushButton("◀ source frame")
        self.source_fwd_button = QPushButton("source frame ▶")
        for button in (self.source_back_button, self.source_fwd_button):
            button.setEnabled(False)
            button.setToolTip(
                "Step to the neighbouring *source* frame using its real presentation "
                "timestamp (works with variable frame rate material)"
            )
            transport.addWidget(button)
        self.mark_in_button = QPushButton("Set clip in")
        self.mark_out_button = QPushButton("Set clip out")
        for button in (self.mark_in_button, self.mark_out_button):
            button.setEnabled(False)
            button.setToolTip("Set the inspected clip's source edge to the shown source PTS")
            transport.addWidget(button)
        transport.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addWidget(self.state)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.time_label)
        layout.addWidget(self.provenance)
        layout.addLayout(range_row)
        layout.addLayout(transport)

        self.render_button.clicked.connect(self._on_render_clicked)
        for box in (self.range_start, self.range_end):
            box.textEdited.connect(lambda _text: self.mark_range_edited())
        self.cancel_button.clicked.connect(self.cancelPreviewRequested)
        self.free_cache_button.clicked.connect(self.freeCacheRequested)
        self.play_button.clicked.connect(self._on_play_clicked)
        self.step_back_button.clicked.connect(lambda: self._step_output(-1))
        self.step_fwd_button.clicked.connect(lambda: self._step_output(+1))
        self.source_back_button.clicked.connect(lambda: self.sourceStepRequested.emit(-1))
        self.source_fwd_button.clicked.connect(lambda: self.sourceStepRequested.emit(+1))
        self.mark_in_button.clicked.connect(lambda: self.sourceMarkRequested.emit("in"))
        self.mark_out_button.clicked.connect(lambda: self.sourceMarkRequested.emit("out"))

    # ------------------------------------------------------------- range state
    def sync_render_button(self) -> None:
        """Label the render button for the range that is actually in the fields.

        The fields arrive prefilled with the whole composition, so a button that
        always read "Preview around playhead" described an action it would not
        take. The label follows the input instead of the other way round.
        """
        typed = bool(self.range_start.text().strip() and self.range_end.text().strip())
        self.render_button.setText(
            "Preview typed range" if typed else "Preview around playhead"
        )

    def set_output(self, project: Project) -> None:
        """Track the composition extent, without overwriting what the user typed.

        The range fields are only re-filled while they are still automatic, so
        navigating the timeline keeps the offered range sensible but an explicitly
        typed range survives.
        """
        end = project.output_end()
        if not self._range_edited:
            self.range_start.setText(format_time(ZERO))
            self.range_end.setText(format_time(end))
        self.sync_render_button()

    def mark_range_edited(self) -> None:
        self._range_edited = True
        self.sync_render_button()

    def requested_range(self) -> tuple[Fraction, Fraction] | None:
        """Parsed range fields; None if empty (use the default window) or invalid.

        An invalid field leaves a message in the state line rather than silently
        rendering some other range.
        """
        if not self.range_start.text().strip() or not self.range_end.text().strip():
            return None
        try:
            start = parse_time_text(self.range_start.text())
            end = parse_time_text(self.range_end.text())
        except ValueError as exc:
            self.set_state(f"Preview range not understood: {exc}", level="error")
            return None
        if end <= start:
            self.set_state("Preview range end must be after its start.", level="error")
            return None
        return start, end

    def current_scale(self) -> Fraction:
        return self.scale_box.currentData()

    def _on_render_clicked(self) -> None:
        chosen = self.requested_range()
        if chosen is None and self.range_start.text().strip() and self.range_end.text().strip():
            return  # invalid text: the state line already says why
        start, end = chosen or (None, None)
        self.previewRequested.emit(start, end, self.current_scale())

    # ------------------------------------------------------------ status lines
    def set_state(self, text: str, level: str = "info") -> None:
        """One visible state line, in words. ``level`` never changes colour alone."""
        prefix = {"info": "", "busy": "", "stale": "STALE — ", "error": "FAILED — "}[level]
        self.state.setText(f"{prefix}{text}")

    def preview_started(self, start: Fraction, end: Fraction, scale: Fraction) -> None:
        self._range = (start, end)
        self._scale = scale
        self._file = None
        self.cancel_button.setEnabled(True)
        self.play_button.setEnabled(False)
        self.set_state(f"Rendering preview {format_time(start)}-{format_time(end)} …", "busy")

    def preview_progress(self, fraction: float) -> None:
        if self._range is not None:
            start, end = self._range
            self.set_state(
                f"Rendering preview {format_time(start)}-{format_time(end)}: "
                f"{fraction * 100:.0f}%",
                "busy",
            )

    def preview_ready(self, path: Path, start: Fraction, end: Fraction, scale: Fraction) -> None:
        self._range = (start, end)
        self._scale = scale
        self._file = path
        self.cancel_button.setEnabled(False)
        self.play_button.setEnabled(True)
        self.set_state("Preview is current for this composition.", "info")
        self._update_provenance()

    def preview_reused(self, path: Path, start: Fraction, end: Fraction, scale: Fraction) -> None:
        self.preview_ready(path, start, end, scale)
        self.set_state("Preview reused from cache (composition unchanged).", "info")

    def preview_cancelled(self) -> None:
        self._file = None
        self.cancel_button.setEnabled(False)
        self.play_button.setEnabled(False)
        self.set_state("Preview cancelled; nothing was added to the cache.", "info")

    def preview_failed(self, message: str) -> None:
        self.cancel_button.setEnabled(False)
        self.set_state(message, "error")

    def clear_preview_state(self) -> None:
        """No preview belongs to this window any more (New/Open/restore)."""
        self._range = None
        self._scale = None
        self._file = None
        self.cancel_button.setEnabled(False)
        self.play_button.setEnabled(False)
        self.set_state("No preview rendered for this project yet.", "info")
        self._update_provenance()

    def mark_stale(self, reason: str) -> None:
        """Composition changed: the rendered range must not look current."""
        self._file = None
        self.play_button.setEnabled(False)
        self.set_state(f"{reason}; play the still frame or render a new preview.", "stale")
        self._update_provenance()

    def _update_provenance(self) -> None:
        if self._range is None or self._scale is None:
            self.provenance.setText("")
            return
        start, end = self._range
        where = f" from {self._file.name}" if self._file else " (not available)"
        self.provenance.setText(
            f"Preview represents output {format_time(start)}-{format_time(end)} at "
            f"1:{1 / self._scale:g} picture size{where}. It is the same composition the "
            "export path renders; only the picture size and encoder speed differ."
        )

    # ------------------------------------------------------------ still frames
    def show_still(self, frame: StillFrame, source_note: str = "") -> None:
        """Show one decoded output frame (copied, the caller's buffer may go away).

        The frame line is deliberately separate from the preview state line: a still
        is decoded for inspection while a preview render keeps its own status, and
        neither may overwrite the other.
        """
        image = QImage(
            frame.rgb, frame.width, frame.height, frame.width * 3, QImage.Format_RGB888
        )
        self.still.setPixmap(QPixmap.fromImage(image.copy()))
        self._frame = frame
        self.time_label.setText(
            f"Frame {frame.frame_index} · output {format_time(frame.output_time)}"
        )
        detail = "black (no picture here)" if frame.black else f"source PTS {frame.source_pts}"
        notes = [detail]
        if source_note:
            notes.append(source_note)
        self.provenance.setText(
            f"Still frame decoded from the composition: {' · '.join(notes)}. This is "
            "the same picture the export path would render at this output frame."
        )
        self._update_source_buttons()

    def still_pending(self, output_time: Fraction) -> None:
        self.time_label.setText(f"Decoding the output frame at {format_time(output_time)} …")

    def still_failed(self, message: str) -> None:
        self.time_label.setText(f"Still frame failed: {message}")

    def _update_source_buttons(self) -> None:
        has_source = self._frame is not None and not self._frame.black
        for button in (
            self.source_back_button,
            self.source_fwd_button,
            self.mark_in_button,
            self.mark_out_button,
        ):
            button.setEnabled(has_source)

    def shown_source_pts(self) -> Fraction | None:
        if self._frame is None or self._frame.source_pts is None:
            return None
        return self._frame.source_pts

    # --------------------------------------------------------------- transport
    def _step_output(self, direction: int) -> None:
        self.playheadStepped.emit(F(direction))  # interpreted as a frame delta

    def set_playhead(self, t: Fraction) -> None:
        self.time_label.setText(f"Playhead: {format_time(t)} (output time)")

    def set_playing(self, playing: bool) -> None:
        self._playing = playing
        self.play_button.setText("Pause preview" if playing else "Play preview")

    def is_playing(self) -> bool:
        """True while a preview file is playing in this viewer."""
        return self._playing

    def _on_play_clicked(self) -> None:
        if self._playing:
            self.pauseRequested.emit()
        else:
            self.playbackRequested.emit()

    def stop(self) -> None:
        """Playback stopped (mode switch or shutdown); stills remain visible."""
        self.set_playing(False)
