"""Multitrack timeline widget: ruler, tracks, clips, playhead, zoom, selection.

Drawing is viewport-culled: only clips intersecting the visible time range
are painted, so multi-hour projects with hundreds of clips stay responsive
and no per-frame widgets exist. Pixel geometry uses floats but every time
that leaves the widget is an exact ``Fraction`` derived from the integer
pixel and the rational zoom.

State that never enters edit history: playhead, zoom/scroll, selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QAbstractScrollArea, QSizePolicy

from ..core.model import Clip, Project, Track, TrackKind
from ..ui.format import format_time
from .decorations import thumb_box
from .gestures import snap_candidates, snap_time, trim_source_time

F = Fraction
ZERO = F(0)

HEADER_WIDTH = 190
RULER_HEIGHT = 30
ROW_HEIGHT = 52
CLIP_INSET = 8
MIN_PPS = Fraction(1, 400)  # 400 s per pixel floor (multi-hour films)
MAX_PPS = Fraction(4000)  # 4000 px per second ceiling
#: the view that "zoom 1.0" means, so saved navigation means the same next time
BASE_PPS = Fraction(100)
RULER_CLICKZONE = RULER_HEIGHT
EDGE_ZONE_PX = 7
DRAG_START_PX = 3

#: Chords this widget handles itself, i.e. the ones that are **not** `QAction`
#: shortcuts and so cannot be read off the menu bar. The window's Help dialog
#: lists these, which is why the list lives next to `keyPressEvent`/`wheelEvent`
#: rather than being retyped somewhere it can silently drift from.
WIDGET_KEYS: tuple[tuple[str, str], ...] = (
    ("Plus / Equals", "Zoom in"),
    ("Minus", "Zoom out"),
    ("Home", "Playhead to the start of the output"),
    ("Left / Right", "Nudge the playhead by one ruler tick"),
    ("Escape", "Cancel a move or trim in progress"),
    ("Ctrl+wheel", "Zoom around the pointer"),
    ("Wheel", "Scroll the timeline"),
)


@dataclass
class _Gesture:
    kind: str  # "move" | "trim-left" | "trim-right"
    clip_id: str
    member_ids: tuple[str, ...]
    press_time: Fraction
    grab_offset: Fraction
    #: tentative placement while dragging: clip_id -> (start, end)
    tentative: dict[str, tuple[Fraction, Fraction]] = field(default_factory=dict)
    target_track_id: str | None = None
    snap_marker: Fraction | None = None
    started: bool = False
    press_x: int = 0
    press_y: int = 0


class Timeline(QAbstractScrollArea):
    """Timeline view of an immutable project snapshot."""

    playheadChanged = Signal(object)  # Fraction
    clipSelectionChanged = Signal(object)  # tuple[str, ...]
    trackSelected = Signal(object)  # str | None
    trackToggleRequested = Signal(str, str)  # track_id, "enable"|"mute"
    statusMessage = Signal(str)
    clipMoveRequested = Signal(str, object, object)  # clip_id, new_start, track_id|None
    clipTrimRequested = Signal(str, str, object)  # clip_id, "left"|"right", source_time

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.project: Project = Project()
        self.asset_labels: dict[str, str] = {}  # asset_id -> display name
        self.pps: Fraction = Fraction(100)  # pixels per second
        self.offset: Fraction = ZERO  # time at viewport left edge
        self.playhead: Fraction = ZERO
        self.selected_clips: tuple[str, ...] = ()
        self.selected_track: str | None = None
        self.snap_enabled: bool = True
        self._gesture: _Gesture | None = None
        # optional thumbnail/waveform layer (injected); None means the timeline
        # shows geometry only, which is a complete view of the edit
        self._decorations = None
        self._deco_material: dict = {}

        self.setMouseTracking(True)
        self.horizontalScrollBar().setSingleStep(50)
        self.verticalScrollBar().setSingleStep(ROW_HEIGHT)
        self.horizontalScrollBar().valueChanged.connect(self._on_scroll)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self.viewport().update())
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(400, 160)
        self.setFocusPolicy(Qt.StrongFocus)

    # ------------------------------------------------------------- data/view
    def set_project(self, project: Project) -> None:
        self.project = project
        self._update_scroll_ranges()
        self._schedule_decorations()
        self.viewport().update()

    def set_asset_labels(self, labels: dict[str, str]) -> None:
        self.asset_labels = labels
        self._schedule_decorations()
        self.viewport().update()

    # ------------------------------------------------------------- decorations
    def set_decoration_source(self, provider, material: dict) -> None:
        """Give the timeline its optional thumbnail / waveform layer.

        ``provider`` may be None and ``material`` empty — no pictures, no
        waveforms — and the timeline is still a complete, honest view: clip
        geometry is the edit, decorations are a hint about its contents.
        """
        self._decorations = provider
        self._deco_material = dict(material)
        self._schedule_decorations()
        self.viewport().update()

    def _decoration_box(self) -> tuple[int, int]:
        """Thumbnail box for this row height, at the source's own aspect ratio."""
        height = ROW_HEIGHT - 2 * CLIP_INSET
        return thumb_box(int(height * 16 / 9), height)

    def _schedule_decorations(self) -> None:
        """Ask for what is on screen now. Cheap when the cache already has it."""
        if self._decorations is None or not self._deco_material:
            return
        start, end = self.view_range()
        self._decorations.request(
            self.project, self._deco_material, start, end, self.pps, self._decoration_box()
        )

    def tracks_area_height(self) -> int:
        return max(1, len(self.project.tracks)) * ROW_HEIGHT

    # ------------------------------------------------------ coordinate math
    def time_at_pixel(self, x: int) -> Fraction:
        """Exact timeline time at a viewport x pixel."""
        return self.offset + F(x - HEADER_WIDTH) / self.pps

    def pixel_at_time(self, t: Fraction) -> float:
        return HEADER_WIDTH + float((t - self.offset) * self.pps)

    def view_range(self) -> tuple[Fraction, Fraction]:
        width = max(1, self.viewport().width() - HEADER_WIDTH)
        start = self.offset
        return start, start + F(width) / self.pps

    def set_view(self, offset: Fraction, pps: Fraction) -> None:
        self.offset = max(ZERO, offset)
        self.pps = min(MAX_PPS, max(MIN_PPS, pps))
        self._sync_scrollbar_from_view()
        self._schedule_decorations()
        self.viewport().update()

    def zoom_at(self, factor: float, center_time: Fraction) -> None:
        new_pps = min(MAX_PPS, max(MIN_PPS, self.pps * F(factor)))
        left = center_time - (center_time - self.offset) * self.pps / new_pps
        self.pps = new_pps
        self.set_view(left, new_pps)

    def zoom_in(self) -> None:
        center = self._viewport_center_time()
        self.zoom_at(2.0, center)

    def zoom_out(self) -> None:
        center = self._viewport_center_time()
        self.zoom_at(0.5, center)

    def fit_whole(self) -> None:
        end = max(self.project.output_end(), F(1))
        width = max(1, self.viewport().width() - HEADER_WIDTH)
        self.set_view(ZERO, F(width, int(end) + 1))

    def scroll_to_playhead(self) -> None:
        start, end = self.view_range()
        if self.playhead < start or self.playhead > end:
            center = self.playhead - (end - start) / 2
            self.set_view(max(ZERO, center), self.pps)

    def _viewport_center_time(self) -> Fraction:
        start, end = self.view_range()
        return (start + end) / 2

    # ------------------------------------------------------------- playhead
    def set_playhead(self, t: Fraction, *, from_user: bool = False) -> None:
        t = max(ZERO, t)
        if t != self.playhead:
            self.playhead = t
            self.viewport().update()
            # always announced: ``from_user`` records *how* it moved (for messages),
            # but anything looking at output time - the output viewer's still-frame
            # inspection, for instance - must follow every move, not just drags
            self.playheadChanged.emit(t)

    # ------------------------------------------------------------ selection
    def set_clip_selection(self, clip_ids: tuple[str, ...]) -> None:
        if clip_ids != self.selected_clips:
            self.selected_clips = clip_ids
            self.viewport().update()
            self.clipSelectionChanged.emit(clip_ids)

    def clip_at_pixel(self, x: int, y: int) -> Clip | None:
        if x < HEADER_WIDTH or y < RULER_HEIGHT:
            return None
        row = (y - RULER_HEIGHT) // ROW_HEIGHT
        if row >= len(self.project.tracks):
            return None
        track = self.project.tracks[row]
        t = self.time_at_pixel(x)
        for clip in self.project.clips:
            if clip.track_id == track.id and clip.output_start <= t < clip.output_end:
                return clip
        return None

    # ------------------------------------------------------------- scrollbar
    def _update_scroll_ranges(self) -> None:
        content_px = int(self.project.output_end() * self.pps) + self._content_pad_px()
        view_px = max(1, self.viewport().width() - HEADER_WIDTH)
        hbar = self.horizontalScrollBar()
        hbar.blockSignals(True)
        hbar.setRange(0, max(0, content_px - view_px))
        hbar.setPageStep(view_px)
        hbar.setValue(min(hbar.value(), hbar.maximum()))
        hbar.blockSignals(False)
        vbar = self.verticalScrollBar()
        view_h = max(1, self.viewport().height() - RULER_HEIGHT)
        vbar.blockSignals(True)
        vbar.setRange(0, max(0, self.tracks_area_height() - view_h))
        vbar.setPageStep(view_h)
        vbar.blockSignals(False)

    def _content_pad_px(self) -> int:
        return 600

    def _sync_scrollbar_from_view(self) -> None:
        hbar = self.horizontalScrollBar()
        hbar.blockSignals(True)
        hbar.setValue(int(self.offset * self.pps))
        hbar.blockSignals(False)
        self._update_scroll_ranges()

    def _on_scroll(self, value: int) -> None:
        self.offset = F(value) / self.pps
        self.viewport().update()

    # -------------------------------------------------------------- painting
    def paintEvent(self, _event) -> None:
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.Antialiasing)
        start, end = self.view_range()
        top = -self.verticalScrollBar().value()

        self._paint_background(painter)
        self._paint_tracks(painter, start, end, top)
        self._paint_gesture(painter, top)
        self._paint_ruler(painter, start, end)
        self._paint_header(painter, top)
        self._paint_playhead(painter)
        painter.end()

    def _paint_background(self, painter: QPainter) -> None:
        painter.fillRect(self.viewport().rect(), QColor(36, 38, 42))

    def _tick_step(self) -> Fraction:
        """Nice tick step so that major ticks are at least 90 px apart."""
        nice_steps = [
            F(1, 1000), F(1, 4), F(1, 2), F(1), F(2), F(5), F(10), F(30),
            F(60), F(120), F(300), F(600), F(900), F(1800), F(3600), F(7200), F(21600),
        ]
        for step in nice_steps:
            if step * self.pps >= 90:
                return step
        return F(43200)

    def _paint_ruler(self, painter: QPainter, start: Fraction, end: Fraction) -> None:
        rect = QRectF(HEADER_WIDTH, 0, self.viewport().width() - HEADER_WIDTH, RULER_HEIGHT)
        painter.fillRect(rect, QColor(52, 54, 58))
        step = self._tick_step()
        first = (start // step) * step
        t = first
        painter.setPen(QPen(QColor(190, 190, 190)))
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        minor = step / 5
        tm = first
        while tm <= end:
            x = self.pixel_at_time(tm)
            if HEADER_WIDTH <= x <= self.viewport().width():
                painter.drawLine(QPointF(x, RULER_HEIGHT - 6), QPointF(x, RULER_HEIGHT))
            tm += minor
        t = first
        while t <= end:
            x = self.pixel_at_time(t)
            if HEADER_WIDTH <= x <= self.viewport().width():
                painter.drawLine(QPointF(x, RULER_HEIGHT - 12), QPointF(x, RULER_HEIGHT))
                label = self._ruler_label(t)
                painter.drawText(QRectF(x + 3, 0, 120, 14), Qt.AlignLeft, label)
            t += step
        # row separators under ruler
        painter.setPen(QPen(QColor(70, 72, 76)))
        painter.drawLine(QPointF(0, RULER_HEIGHT), QPointF(self.viewport().width(), RULER_HEIGHT))

    @staticmethod
    def _ruler_label(t: Fraction) -> str:
        if t >= 3600 or t <= -3600:
            return format_time(t)
        if t >= 60:
            minutes, secs = divmod(t, 60)
            if secs.denominator == 1:
                return f"{int(minutes)}:{int(secs):02d}"
            return format_time(t)
        if t.denominator == 1:
            return f"{int(t)}s"
        return format_time(t)

    def _paint_tracks(self, painter: QPainter, start: Fraction, end: Fraction, top: int) -> None:
        for row, track in enumerate(self.project.tracks):
            y = RULER_HEIGHT + row * ROW_HEIGHT + top
            if y + ROW_HEIGHT < RULER_HEIGHT or y > self.viewport().height():
                continue  # row fully outside the viewport
            self._paint_track_row(painter, track, y, start, end, row)
        # content beyond last track
        bottom = RULER_HEIGHT + self.tracks_area_height() + top
        if bottom < self.viewport().height():
            painter.fillRect(QRectF(HEADER_WIDTH, bottom, self.viewport().width() - HEADER_WIDTH,
                                    self.viewport().height() - bottom), QColor(30, 32, 35))

    def _paint_track_row(
        self,
        painter: QPainter,
        track: Track,
        y: int,
        start: Fraction,
        end: Fraction,
        row: int,
    ) -> None:
        width = self.viewport().width() - HEADER_WIDTH
        base = QColor(46, 48, 52) if track.kind is TrackKind.VIDEO else QColor(42, 46, 50)
        if not track.enabled:
            base = base.darker(140)
        if track.id == self.selected_track:
            base = base.lighter(125)
        painter.fillRect(QRectF(HEADER_WIDTH, y, width, ROW_HEIGHT), base)
        painter.setPen(QPen(QColor(70, 72, 76)))
        painter.drawLine(QPointF(HEADER_WIDTH, y), QPointF(self.viewport().width(), y))

        for clip in self.project.clips:
            if clip.track_id != track.id:
                continue
            x0 = self.pixel_at_time(clip.output_start)
            x1 = self.pixel_at_time(clip.output_end)
            if x1 < HEADER_WIDTH or x0 > self.viewport().width():
                continue  # viewport culling
            if x0 < HEADER_WIDTH:
                x0 = float(HEADER_WIDTH)
            self._paint_clip(painter, clip, track, QRectF(x0, y + CLIP_INSET, max(2.0, x1 - x0),
                                    ROW_HEIGHT - 2 * CLIP_INSET))

    def _paint_clip(self, painter: QPainter, clip: Clip, track: Track, rect: QRectF) -> None:
        selected = clip.id in self.selected_clips
        if track.kind is TrackKind.VIDEO:
            fill = QColor(70, 100, 160) if track.enabled else QColor(58, 66, 80)
        else:
            fill = QColor(60, 130, 110) if track.enabled else QColor(52, 70, 66)
        if not track.enabled:
            fill = fill.darker(160)
        if track.muted:
            fill = fill.darker(150)
        painter.setBrush(QBrush(fill))
        if selected:
            painter.setPen(QPen(QColor(250, 210, 90), 2))
        else:
            painter.setPen(QPen(fill.lighter(140), 1))
        painter.drawRect(rect)

        decorated = False
        if self._decorations is not None and self._deco_material:
            decorated = self._paint_decoration(painter, clip, track, rect)
        if not decorated and track.kind is TrackKind.AUDIO and rect.width() > 10:
            # a striped body stands in for a waveform that has not arrived, so a
            # clip never looks like a different kind of clip because of decoding
            painter.setClipRect(rect)
            painter.setPen(QPen(QColor(255, 255, 255, 45), 1, Qt.DotLine))
            step = max(6.0, 6.0 * float(self.pps) / 60)
            x = rect.left() + 4
            while x < rect.right():
                painter.drawLine(QPointF(x, rect.top() + 3), QPointF(x, rect.bottom() - 3))
                x += step
            painter.setClipping(False)
        if not decorated:
            self._paint_decoration_note(painter, clip, rect)

        if rect.width() > 70:
            font = QFont()
            font.setPointSize(8)
            painter.setFont(font)
            painter.setPen(QPen(QColor(235, 235, 235)))
            kind_tag = "V" if track.kind is TrackKind.VIDEO else "A"
            label = f"{kind_tag} {self.asset_labels.get(clip.asset_id, clip.asset_id)}"
            extras = []
            if clip.rate != 1:
                extras.append(f"rate {float(clip.rate):g}")
            if clip.fade_in or clip.fade_out:
                extras.append("fade")
            if clip.link_group:
                extras.append("link")
            if not track.enabled:
                extras.append("off")
            if track.muted:
                extras.append("mute")
            text = label + (" · " + " ".join(extras) if extras else "")
            painter.drawText(rect.adjusted(5, 1, -4, 0), Qt.AlignLeft | Qt.AlignVCenter, text)

    # --------------------------------------------------- decoration painting
    def _paint_decoration(self, painter: QPainter, clip: Clip, track: Track,
                          rect: QRectF) -> bool:
        """Draw the thumbnails or waveform already delivered for this clip.

        Nothing is fetched or computed here: painting only shows what the
        background extraction has already produced, so the timeline never blocks
        on a decode and a decoration that has not arrived is simply absent.
        """
        matter = self._deco_material.get((clip.asset_id, clip.stream_index))
        if matter is None:
            return False
        start, end = self.view_range()
        if track.kind is TrackKind.AUDIO:
            envelope = self._decorations.envelope_for(clip, matter, start, end, self.pps)
            return envelope is not None and self._paint_waveform(painter, clip, rect,
                                                                 envelope)
        strip = self._decorations.strip_for(clip, matter, start, end, self.pps,
                                            self._decoration_box())
        return strip is not None and self._paint_thumbnails(painter, clip, rect, strip)

    def _output_time(self, clip: Clip, source_time: Fraction) -> Fraction:
        """Where a source time of this clip lands in output time.

        The clip's own in-point and rate do the mapping, so a trimmed or retimed
        clip shows the same material stretched, shifted or cut exactly as it
        sounds: the decoration follows the edit, not the other way round.
        """
        return clip.output_start + (source_time - clip.source_in) / clip.rate

    def _paint_thumbnails(self, painter: QPainter, clip: Clip, rect: QRectF,
                          strip) -> bool:
        drawn = 0
        painter.setClipRect(rect)
        step_pixels = float(strip.interval / clip.rate * self.pps)
        for index in range(strip.count):
            left = self.pixel_at_time(self._output_time(clip, strip.time_at(index)))
            if left + step_pixels < rect.left() or left > rect.right():
                continue
            image = QImage.fromData(strip.frames[index], "JPEG")
            if image.isNull():  # a frame that will not decode is left out, not faked
                continue
            painter.drawImage(
                QRectF(left, rect.top(), max(1.0, step_pixels), float(rect.height())),
                image,
            )
            drawn += 1
        if drawn and step_pixels >= 6:
            # seams between pictures, so a strip reads as separate frames
            painter.setPen(QPen(QColor(18, 20, 24), 1))
            x = self.pixel_at_time(self._output_time(clip, strip.start))
            while x < rect.right():
                painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
                x += step_pixels
        painter.setClipping(False)
        return drawn > 0

    def _paint_waveform(self, painter: QPainter, clip: Clip, rect: QRectF,
                        envelope) -> bool:
        """Peak envelope at full-scale height: comparable between clips."""
        mid = rect.center().y()
        half = rect.height() / 2 - 1
        step_pixels = float(envelope.bucket / clip.rate * self.pps)
        painter.setClipRect(rect)
        drawn = 0
        for index in range(envelope.count):
            at = envelope.start + index * envelope.bucket
            left = self.pixel_at_time(self._output_time(clip, at))
            if left + step_pixels < rect.left():
                continue
            if left > rect.right():
                break
            height = half * min(1.0, max(0.0, envelope.peaks[index]))
            x = min(max(left, rect.left()), rect.right())
            if height < 1.0:
                painter.setPen(QPen(QColor(205, 215, 210, 150), 1))
                painter.drawPoint(QPointF(x, mid))
            else:
                painter.setPen(QPen(QColor(225, 240, 235, 215), 1))
                painter.drawLine(QPointF(x, mid - height), QPointF(x, mid + height))
            drawn += 1
        painter.setClipping(False)
        return drawn > 0

    def _paint_decoration_note(self, painter: QPainter, clip: Clip, rect: QRectF) -> None:
        """Say what could not be decoded, instead of quietly drawing nothing."""
        reason_for = getattr(self._decorations, "reason_for", None)
        if reason_for is None or rect.width() < 150:
            return
        reason = reason_for(clip.id)
        if not reason:
            return
        font = QFont()
        font.setPointSize(7)
        font.setItalic(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(220, 190, 190)))
        short = reason if len(reason) <= 70 else reason[:67] + "..."
        painter.drawText(rect.adjusted(5, rect.height() - 14, -4, -2),
                         Qt.AlignLeft | Qt.AlignBottom, f"no picture: {short}")

    def _paint_header(self, painter: QPainter, top: int) -> None:
        header_rect = QRectF(0, RULER_HEIGHT, HEADER_WIDTH, self.viewport().height())
        painter.fillRect(header_rect, QColor(52, 54, 58))
        painter.setPen(QPen(QColor(70, 72, 76)))
        painter.drawLine(QPointF(HEADER_WIDTH, 0), QPointF(HEADER_WIDTH, self.viewport().height()))
        for row, track in enumerate(self.project.tracks):
            y = RULER_HEIGHT + row * ROW_HEIGHT + top
            if y + ROW_HEIGHT < RULER_HEIGHT or y > self.viewport().height():
                continue
            self._paint_track_header(painter, track, y, row)

    def _paint_track_header(self, painter: QPainter, track: Track, y: int, row: int) -> None:
        if track.id == self.selected_track:
            painter.fillRect(QRectF(0, y, HEADER_WIDTH, ROW_HEIGHT), QColor(72, 84, 104))
        kind = "VIDEO" if track.kind is TrackKind.VIDEO else "AUDIO"
        state_bits = [kind]
        if not track.enabled:
            state_bits.append("[hidden]")
        if track.kind is TrackKind.AUDIO and track.muted:
            state_bits.append("[muted]")
        font = QFont()
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(235, 235, 235) if track.enabled else QColor(150, 150, 150)))
        painter.drawText(QRectF(8, y + 4, HEADER_WIDTH - 16, 18), Qt.AlignLeft, track.name)
        font.setBold(False)
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QPen(QColor(180, 180, 180)))
        painter.drawText(
            QRectF(8, y + 22, HEADER_WIDTH - 16, 14), Qt.AlignLeft, " ".join(state_bits)
        )
        # toggle affordances (drawn icons + letters, not colour only)
        self._paint_toggle(painter, 8 + HEADER_WIDTH - 76, y + 30, "E", track.enabled)
        if track.kind is TrackKind.AUDIO:
            self._paint_toggle(painter, 8 + HEADER_WIDTH - 52, y + 30, "M", not track.muted)

    def _paint_toggle(self, painter: QPainter, x: int, y: int, letter: str, active: bool) -> None:
        rect = QRectF(x, y, 18, 16)
        painter.setBrush(QBrush(QColor(90, 140, 90) if active else QColor(60, 60, 64)))
        painter.setPen(QPen(QColor(200, 200, 200)))
        painter.drawRect(rect)
        painter.drawText(rect, Qt.AlignCenter, letter)

    def _paint_playhead(self, painter: QPainter) -> None:
        x = self.pixel_at_time(self.playhead)
        if x < HEADER_WIDTH or x > self.viewport().width():
            return
        painter.setPen(QPen(QColor(235, 80, 80), 1))
        painter.drawLine(QPointF(x, 0), QPointF(x, self.viewport().height()))
        painter.setBrush(QBrush(QColor(235, 80, 80)))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(
            QPolygonF([QPointF(x - 5, 0), QPointF(x + 5, 0), QPointF(x, 8)])
        )

    def _paint_gesture(self, painter: QPainter, top: int) -> None:
        """Tentative drag/trim geometry + snap marker (visible feedback)."""
        gesture = self._gesture
        if gesture is None or not gesture.started:
            return
        for clip_id, (start_t, end_t) in gesture.tentative.items():
            clip = self.project.clip(clip_id)
            if clip is None:
                continue
            track_id = gesture.target_track_id if clip_id == gesture.clip_id else clip.track_id
            row = next(
                (i for i, t in enumerate(self.project.tracks) if t.id == track_id),
                None,
            )
            if row is None:
                continue
            y = RULER_HEIGHT + row * ROW_HEIGHT + top
            x0 = max(float(HEADER_WIDTH), self.pixel_at_time(start_t))
            x1 = self.pixel_at_time(end_t)
            rect = QRectF(x0, y + CLIP_INSET, max(2.0, x1 - x0), ROW_HEIGHT - 2 * CLIP_INSET)
            painter.setBrush(QBrush(QColor(250, 210, 90, 90)))
            painter.setPen(QPen(QColor(250, 210, 90), 2, Qt.DashLine))
            painter.drawRect(rect)
        if gesture.snap_marker is not None:
            x = self.pixel_at_time(gesture.snap_marker)
            if HEADER_WIDTH <= x <= self.viewport().width():
                painter.setPen(QPen(QColor(120, 220, 255), 2, Qt.DashLine))
                painter.drawLine(QPointF(x, RULER_HEIGHT),
                                 QPointF(x, self.viewport().height()))

    # ------------------------------------------------------------ interaction
    def mousePressEvent(self, event) -> None:
        pos = event.position()
        x, y = int(pos.x()), int(pos.y())
        if x < HEADER_WIDTH and y >= RULER_HEIGHT:
            self._header_click(x, y)
            return
        if y < RULER_HEIGHT and x >= HEADER_WIDTH:
            self.set_playhead(self.time_at_pixel(x), from_user=True)
            return
        clip = self.clip_at_pixel(x, y)
        if clip is not None:
            additive = event.modifiers() & Qt.ControlModifier
            if additive:
                ids = list(self.selected_clips)
                if clip.id in ids:
                    ids.remove(clip.id)
                else:
                    ids.append(clip.id)
                self.set_clip_selection(tuple(ids))
            else:
                if clip.id not in self.selected_clips:
                    self.set_clip_selection((clip.id,))
            self._begin_gesture(clip, x, y)
        else:
            self.set_clip_selection(())
            row = (y - RULER_HEIGHT) // ROW_HEIGHT
            if row < len(self.project.tracks):
                self.selected_track = self.project.tracks[int(row)].id
            else:
                self.selected_track = None
            self.viewport().update()
            self.trackSelected.emit(self.selected_track)

    def _hit_edge(self, clip: Clip, x: int) -> str | None:
        left_x = self.pixel_at_time(clip.output_start)
        right_x = self.pixel_at_time(clip.output_end)
        if abs(x - left_x) <= EDGE_ZONE_PX:
            return "left"
        if abs(x - right_x) <= EDGE_ZONE_PX:
            return "right"
        return None

    def _begin_gesture(self, clip: Clip, x: int, y: int) -> None:
        members = self.project.linked_clips(clip.id)
        member_ids = tuple(m.id for m in members)
        edge = self._hit_edge(clip, x)
        kind = f"trim-{edge}" if edge else "move"
        self._gesture = _Gesture(
            kind=kind,
            clip_id=clip.id,
            member_ids=member_ids,
            press_time=self.time_at_pixel(x),
            grab_offset=self.time_at_pixel(x) - clip.output_start,
            press_x=x,
            press_y=y,
        )

    def mouseMoveEvent(self, event) -> None:
        gesture = self._gesture
        pos = event.position()
        x, y = int(pos.x()), int(pos.y())
        if gesture is None:
            self._update_hover_cursor(x, y)
            return
        if not gesture.started:
            if (
                abs(x - gesture.press_x) < DRAG_START_PX
                and abs(y - gesture.press_y) < DRAG_START_PX
            ):
                return
            gesture.started = True
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        self._update_gesture(x, y)

    def _update_hover_cursor(self, x: int, y: int) -> None:
        """Cursor shape previews what a drag would do (not colour-only)."""
        clip = self.clip_at_pixel(x, y) if y >= RULER_HEIGHT else None
        if clip is None:
            shape = Qt.CursorShape.ArrowCursor
        elif self._hit_edge(clip, x) is not None:
            shape = Qt.CursorShape.SizeHorCursor
        else:
            shape = Qt.CursorShape.SizeAllCursor
        if self.viewport().cursor().shape() != shape:
            self.viewport().setCursor(shape)

    def _update_gesture(self, x: int, y: int) -> None:
        gesture = self._gesture
        assert gesture is not None
        anchor = self.project.clip(gesture.clip_id)
        if anchor is None:  # pragma: no cover - project changed under us
            self._gesture = None
            return
        t = self.time_at_pixel(x)
        if gesture.kind == "move":
            tentative_start = t - gesture.grab_offset
            candidates = snap_candidates(self.project, frozenset(gesture.member_ids), self.playhead)
            snap = snap_time(tentative_start, candidates, self.pps, self.snap_enabled)
            delta = snap.time - anchor.output_start
            marker = snap.marker
            members = [m for m in (self.project.clip(mid) for mid in gesture.member_ids) if m]
            # A group (linked clips included) stops at time 0 rather than
            # previewing a position the command would reject.
            earliest = min(m.output_start for m in members)
            if earliest + delta < 0:
                delta = -earliest
                marker = ZERO
            gesture.tentative = {}
            for member in members:
                start = member.output_start + delta
                gesture.tentative[member.id] = (start, start + member.output_duration)
            gesture.snap_marker = marker
            gesture.target_track_id = self._compatible_track_at(y, anchor)
        else:
            edge = "left" if gesture.kind == "trim-left" else "right"
            candidates = snap_candidates(self.project, frozenset(gesture.member_ids), self.playhead)
            snap = snap_time(t, candidates, self.pps, self.snap_enabled)
            boundary = snap.time
            if edge == "left":
                boundary = max(ZERO, min(boundary, anchor.output_end - self._min_slice()))
                gesture.tentative = {anchor.id: (boundary, anchor.output_end)}
            else:
                boundary = max(anchor.output_start + self._min_slice(), boundary)
                gesture.tentative = {anchor.id: (anchor.output_start, boundary)}
            gesture.snap_marker = snap.marker
            gesture.target_track_id = None
        self.viewport().update()

    def _min_slice(self) -> Fraction:
        """Smallest visible sliver allowed when trimming (one pixel in time)."""
        return F(1) / self.pps

    def _compatible_track_at(self, y: int, anchor: Clip) -> str | None:
        row = (y - RULER_HEIGHT) // ROW_HEIGHT
        if row < 0 or row >= len(self.project.tracks):
            return None
        track = self.project.tracks[int(row)]
        anchor_track = self.project.track(anchor.track_id)
        if anchor_track is not None and track.kind is not anchor_track.kind:
            return None
        if track.id == anchor.track_id:
            return None
        return track.id

    def mouseReleaseEvent(self, event) -> None:
        gesture = self._gesture
        self._gesture = None
        pos = event.position()
        x, y = int(pos.x()), int(pos.y())
        self._update_hover_cursor(x, y)
        if gesture is None or not gesture.started or not gesture.tentative:
            self.viewport().update()
            return
        anchor = self.project.clip(gesture.clip_id)
        if anchor is None:  # pragma: no cover
            return
        if gesture.kind == "move":
            new_start, _ = gesture.tentative[anchor.id]
            self.clipMoveRequested.emit(gesture.clip_id, new_start, gesture.target_track_id)
        else:
            edge = "left" if gesture.kind == "trim-left" else "right"
            start, end = gesture.tentative[anchor.id]
            boundary = start if edge == "left" else end
            source_time = trim_source_time(anchor, edge, boundary)
            self.clipTrimRequested.emit(gesture.clip_id, edge, source_time)
        self.viewport().update()

    def cancel_gesture(self) -> None:
        if self._gesture is not None:
            self._gesture = None
            self.statusMessage.emit("Gesture cancelled")
            self.viewport().update()

    def _header_click(self, x: int, y: int) -> None:
        row = (y - RULER_HEIGHT) // ROW_HEIGHT
        if row >= len(self.project.tracks):
            return
        track = self.project.tracks[int(row)]
        top = RULER_HEIGHT + row * ROW_HEIGHT - self.verticalScrollBar().value()
        enable_rect_x = 8 + HEADER_WIDTH - 76
        mute_rect_x = 8 + HEADER_WIDTH - 52
        toggle_y = top + 30
        if toggle_y <= y <= toggle_y + 16:
            if enable_rect_x <= x <= enable_rect_x + 18:
                self.trackToggleRequested.emit(track.id, "enable")
                return
            if track.kind is TrackKind.AUDIO and mute_rect_x <= x <= mute_rect_x + 18:
                self.trackToggleRequested.emit(track.id, "mute")
                return
        self.selected_track = track.id
        self.viewport().update()
        self.trackSelected.emit(track.id)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.ControlModifier:
            pos_x = int(event.position().x())
            center = self.time_at_pixel(max(HEADER_WIDTH, pos_x))
            self.zoom_at(1.25 if delta > 0 else 0.8, center)
        else:
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - (delta // 120) * bar.singleStep() * 3)
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.cancel_gesture()
            return
        if event.key() == Qt.Key_Plus or event.key() == Qt.Key_Equal:
            self.zoom_in()
        elif event.key() == Qt.Key_Minus:
            self.zoom_out()
        elif event.key() == Qt.Key_Home:
            self.set_playhead(ZERO, from_user=True)
        elif event.key() == Qt.Key_Right:
            step = self._nudge_step()
            self.set_playhead(self.playhead + step, from_user=True)
        elif event.key() == Qt.Key_Left:
            step = self._nudge_step()
            self.set_playhead(max(ZERO, self.playhead - step), from_user=True)
        else:
            super().keyPressEvent(event)
        self.scroll_to_playhead()

    def _nudge_step(self) -> Fraction:
        """One major ruler tick — at least a frame's worth at this zoom."""
        return self._tick_step() / 5
