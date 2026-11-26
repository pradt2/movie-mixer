"""Main editor window.

Layout: media bin dock (left), viewer above the timeline (centre),
inspector dock (right). The composition is owned by a core
``ProjectSession``; every mutation goes through an undoable command.
Playhead, zoom/scroll and selection are UI state and never enter history.

Shortcut map (see also README):
    Ctrl+I import · Ctrl+G place selected streams at playhead
    Ctrl+Z undo · Ctrl+Shift+Z redo · Ctrl+D duplicate clip
    Delete remove selected clips · S split at playhead
    Plus / Minus zoom · F fit timeline · Home playhead to 0
    Left/Right nudge playhead

The authoritative list is not this comment: Help ▸ Keyboard and Workflow… reads
the chords off the live actions and off ``timeline.WIDGET_KEYS``, so what is
documented inside the application cannot fall out of step with what is bound.
"""


from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Sequence
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core.commands import (
    AddTrackCommand,
    Command,
    CommandError,
    DuplicateClipsCommand,
    LinkClipsCommand,
    MoveClipCommand,
    PlaceStreamsCommand,
    RemoveClipsCommand,
    RemoveTrackCommand,
    RenameTrackCommand,
    ReorderTrackCommand,
    SetFadesCommand,
    SetGainCommand,
    SetOutputSettingsCommand,
    SetRateCommand,
    SetTrackEnabledCommand,
    SetTrackMutedCommand,
    SplitClipCommand,
    StreamPlacement,
    TrackChoice,
    TrimClipCommand,
    TrimEdge,
    UnlinkClipCommand,
    group_placements,
)
from ..core.history import HistoryError
from ..core.model import StreamSource, TrackKind
from ..core.timeutils import ZERO
from ..diagnostics import MediaToolStatus, check_media_tools
from ..fs.identity import Availability, Identity, check
from ..fs.protection import Consent, DestinationGuard, DestinationSafetyError, ProtectedFile
from ..media.assets import Asset, StreamKind
from ..media.availability import (
    check_catalogue,
    protected_files,
    recorded_identity,
    same_file,
)
from ..media.framegrab import StillFrame
from ..media.relink import RelinkError, relink_asset
from ..project.document import (
    AddAssetCommand,
    DocumentCommand,
    DocumentSession,
    EditComposition,
    EditorDocument,
    RelinkDocumentCommand,
)
from ..project.recovery import (
    RecoveryStore,
    Snapshot,
    decode_payload,
    describe_snapshot,
    disk_ahead_of_snapshot,
    recovery_root,
    session_key,
)
from ..project.serialize import SCHEMA_VERSION, Navigation, ProjectFileError
from ..project.storage import (
    AssetRecord,
    AssetState,
    LoadedDocument,
    build_document,
    load_document,
    project_file_mtime,
    save_document,
)
from ..render.cache import (
    CachedFrame,
    FrameCache,
    PreviewCache,
    render_fingerprint,
)
from ..render.exporter import RenderRequest
from ..render.graph import PREVIEW_H264_FAST
from ..render.plan import RenderPlanError
from ..render.preview import PreviewError, playback_offset, prepare_preview, preview_range
from .decorations import ClipDecorations, Decorations
from .export_dialog import ExportDialog
from .format import format_time, parse_time_text
from .inspector import Inspector
from .media_bin import ROLE_STREAM_INDEX, MediaBin, MediaBinController
from .render_worker import (
    PreviewController,
    RenderController,
    SourceStepController,
    StillController,
    describe_result,
)
from .timeline import BASE_PPS, WIDGET_KEYS, Timeline
from .viewer import OutputViewer, SourceViewer

log = logging.getLogger(__name__)

def _autosave_interval_seconds() -> float:
    """Minutes-of-work safety net; 0 disables it (documented in README)."""
    raw = os.environ.get("VIDEO_EDITOR_AUTOSAVE_SECONDS", "120")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 120.0


_MEDIA_FILTER = (
    "Media files (*.mp4 *.mkv *.mov *.avi *.ts *.m4a *.mp3 *.wav *.flac *.mxf)"
    ";;All files (*)"
)


def core_source_for(
    asset: Asset, stream, stream_position: int | None = None
) -> StreamSource | None:
    """Translate probed media metadata into a core ``StreamSource``."""
    if not stream.bounds_known:
        return None
    kind = TrackKind.VIDEO if stream.kind is StreamKind.VIDEO else TrackKind.AUDIO
    return StreamSource(
        asset_id=asset.id,
        stream_index=stream.stream_index,
        kind=kind,
        origin=stream.origin,
        end=stream.end_time,
        conservative_end=stream.bounds_are_conservative,
        frame_rate=stream.frame_rate,
        sample_rate=stream.sample_rate,
        channel_layout=stream.channel_layout,
        width=stream.width,
        height=stream.height,
        stream_position=stream_position,
        input_start=(asset.metadata.container_start or ZERO) if asset.metadata else ZERO,
    )


#: The manual workflow, in the order the editor expects to be used. Stated here
#: as well as in README so the application can explain itself, and so a test can
#: check the steps still name the operations they depend on.
WORKFLOW_STEPS: tuple[str, ...] = (
    "1. Import both editions (Ctrl+I). Project ▸ Media Status… shows every stream "
    "in each file: kind, container stream index, rate or sample rate, channels, "
    "start offset and length. No file is treated as the reference.",
    "2. Choose the output yourself in the inspector: size, rational frame rate "
    "(24000/1001 works), sample rate and channel layout. These belong to the "
    "project, never to one edition.",
    "3. Add the tracks you need (Timeline ▸ Add Video/Audio Track). Select a "
    "stream in the bin and place it at the playhead (Ctrl+G), or insert it with "
    "ripple (Ctrl+Shift+G). Picture and sound are chosen independently: any clip "
    "may come from any stream of any file.",
    "4. Align by looking and listening: zoom with +/-, read the waveform "
    "envelopes and thumbnails, seek exactly with G or by typing a time, then move "
    "or trim with the inspector's exact numbers, or set a clip edge from a real "
    "source frame with Ctrl+Left / Ctrl+Right and Set clip in/out.",
    "5. Work around extra, missing or reordered footage: split at the playhead "
    "(S), trim edges, delete a clip (Delete, leaving a gap), place another "
    "edition's material underneath. Every step is one undoable edit "
    "(Ctrl+Z / Ctrl+Shift+Z).",
    "6. Review an interval you chose: type its start and end in the output viewer "
    "and render a preview (Ctrl+R). A preview is a real render of the same "
    "composition at smaller picture size, so it is evidence about the output.",
    "7. Export the whole composition or a selected interval (Ctrl+E), and check "
    "the produced file against the plan the dialog showed before it ran.",
    "8. Save the project (Ctrl+S). Sources are only ever referenced: moving the "
    "media means relinking it, and nothing is written next to your files.",
)


class MainWindow(QMainWindow):
    """Top level editor window."""

    #: class default; the application start-up path turns this on
    quit_confirm_required = False

    def __init__(
        self,
        media_status: MediaToolStatus | None = None,
        *,
        # recovery is offered by the application start-up path (``run_app``), not
        # by every window that happens to exist: a test or a script opening a
        # second window must not be interrupted by a modal it did not ask for
        offer_recovery: bool = False,
    ) -> None:
        super().__init__()
        self.setWindowTitle(f"Video Editor — {__version__}")
        self.resize(1280, 800)

        self.media_status = media_status or check_media_tools()
        # one session, one baseline: the document (composition + media catalogue)
        # is what history, the dirty mark, Save and the recovery snapshot share
        self.session = DocumentSession()
        self.bin_controller = MediaBinController(
            catalogue=lambda: self.session.document.assets
        )
        # project file identity + the media state recorded when it was loaded
        self.project_path: Path | None = None
        self._media_records: list[AssetRecord] = []
        self._last_snapshot_document: dict | None = None
        self.render_controller = RenderController(self)

        self._build_central()
        self._build_docks()
        # preview UI first: the menu wiring needs the controllers it creates
        self._build_preview_ui()
        self._create_actions()
        self._build_export_ui()

        self.bin_controller.status_message.connect(self.statusBar().showMessage)
        self.timeline.statusMessage.connect(self.statusBar().showMessage)
        self.source_viewer.statusMessage.connect(self.statusBar().showMessage)
        self.statusBar().showMessage(self.media_status.diagnostic())
        if not self.media_status.available:
            log.warning(self.media_status.diagnostic())

        self._refresh_history_actions()
        self._refresh_timeline()
        self._build_project_state()
        if offer_recovery:
            self.offer_startup_recovery()

    # ------------------------------------------------------------- project state
    def _build_project_state(self) -> None:
        """Project file identity, dirty title and the recovery autosave timer."""
        self.recovery_timer = QTimer(self)
        interval = _autosave_interval_seconds()
        if interval > 0:
            # QTimer treats 0 as "as fast as possible", which is the opposite of
            # the documented "0 disables autosave"
            self.recovery_timer.setInterval(int(interval * 1000))
            self.recovery_timer.timeout.connect(self._take_recovery_snapshot)
            self.recovery_timer.start()
        self._update_title()

    def _project_name(self) -> str:
        return self.project_path.name if self.project_path is not None else "untitled"

    def _update_title(self) -> None:
        mark = "*" if self.session.is_dirty else ""
        self.setWindowTitle(
            f"Video Editor — {mark}{self._project_name()} (schema v{SCHEMA_VERSION})"
        )
        self.save_project_action.setEnabled(self.session.is_dirty or self.project_path is None)

    def _project_navigation(self) -> Navigation:
        """Current UI navigation, for saving and for recovery snapshots."""
        return Navigation(
            playhead=self.timeline.playhead,
            zoom=float(self.timeline.pps / BASE_PPS),
            scroll_x=self.timeline.offset,
            selected_clips=self.timeline.selected_clips,
        )

    def _project_document(self, save_target: Path | None = None) -> dict:
        """The document as it would be written.

        *save_target* decides how media is referenced: relative references are
        only meaningful from the directory the file will really live in, so the
        first save to ``/movies/film.vep`` must resolve against ``/movies`` even
        though this window has no project path yet.
        """
        return build_document(
            self.session.project,
            self.session.document.assets,
            self._project_navigation(),
            __version__,
            save_target if save_target is not None else self.project_path,
        )

    def _unavailable_assets(self) -> list[AssetRecord]:
        """Assets whose files are missing, replaced or unreadable **right now**.

        Checked against the disk, not against what the document recorded at load:
        a film swapped on disk after opening must be visible here and must stop a
        render, which is why the same observation feeds the render refusal.
        """
        self._availability()
        return [
            record for record in self._media_records if record.state is not AssetState.ONLINE
        ]

    def _show_media_status(self) -> None:
        self._availability()  # the disk, as of this moment
        records = list(self._media_records)
        lines = [
            f"{record.asset.display_name()}  [{record.state.value}]"
            f"\n    {record.resolved_path}"
            + (f"\n    {record.note}" if record.note else "")
            for record in records
        ]
        if not lines:
            lines = ["No assets in this project."]
        if not self.media_status.available:
            lines.append(self.media_status.diagnostic())
        self._ask(
            "Media status",
            "Media is referenced, never copied or modified. Every asset used by the "
            "timeline is listed with what the disk says about it now. A file of the "
            "same name but different size or timestamp is reported as 'changed': it "
            "may be a different encode, so its recorded stream bounds are not trusted.",
            "\n\n".join(lines),
        )

    # ------------------------------------------------------------- new/open/save
    def _ask(
        self,
        title: str,
        text: str,
        detail: str = "",
        buttons: tuple[tuple[str, QMessageBox.ButtonRole], ...] = (
            ("OK", QMessageBox.ButtonRole.AcceptRole),
        ),
        default: int = 0,
    ) -> tuple[bool, int]:
        """One modal message box, with the buttons this situation needs.

        Returns ``(shown, clicked_index)``; ``shown`` is False when the dialog
        was suppressed (a suppressed prompt must never be read as consent).
        ``_message_box`` is the injection point tests use, so the suite never
        depends on intercepting Qt internals — an unattended run must not be
        able to block on a modal it cannot see.
        """
        box = self._message_box(title, text, detail, buttons, default)
        if box is None:
            return False, -1
        box.exec()
        clicked = box.clickedButton()
        if clicked is None:
            # closed with Escape or the window manager: no button was clicked,
            # which is never read as consent by the callers below
            return True, -1
        # Qt lays buttons out by role and platform style, so the index a caller
        # gets back is its *own* button order, matched by label — never a
        # position in Qt's list
        label = clicked.text().replace("&", "")
        for index, (text, _role) in enumerate(buttons):
            if text.replace("&", "") == label:
                return True, index
        return True, -1

    def _message_box(
        self,
        title: str,
        text: str,
        detail: str,
        buttons: tuple[tuple[str, QMessageBox.ButtonRole], ...],
        default: int,
    ) -> QMessageBox | None:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        if detail:
            box.setInformativeText(detail)
        added = [box.addButton(label, role) for label, role in buttons]
        if 0 <= default < len(added):
            box.setDefaultButton(added[default])
        return box

    def _confirm_replace(self, action: str) -> bool:
        """Prompt about unsaved work before New/Open/Quit; False = user abstained."""
        if not self.session.is_dirty:
            return True
        _shown, choice = self._ask(
            "Unsaved work",
            f"{action}: '{self._project_name()}' has unsaved changes.",
            "Save first? Nothing is written unless you choose Save, and your current "
            "edits stay in this window if you choose Cancel.",
            (
                ("Save…", QMessageBox.ButtonRole.AcceptRole),
                ("Discard", QMessageBox.ButtonRole.DestructiveRole),
                ("Cancel", QMessageBox.ButtonRole.RejectRole),
            ),
        )
        if choice == 2 or choice < 0:  # Cancel (or the prompt was suppressed)
            return False
        if choice == 0:
            return self._save_project()
        return True  # discard

    def _confirm_quit(self) -> bool:
        """Never lose unsaved work silently on quit.

        ``quit_confirm_required`` is True for the application and for the
        unattended desktop check (which answers the dialog itself). Unit tests
        close windows programmatically after edits, so the fixture turns it off
        rather than letting a modal block the run.
        """
        if not self.quit_confirm_required or not self.session.is_dirty:
            return True
        _shown, choice = self._ask(
            "Unsaved work",
            f"'{self._project_name()}' has unsaved changes.",
            "A recovery snapshot is kept either way, but the project file itself is "
            "only written if you choose Save.",
            (
                ("Save…", QMessageBox.ButtonRole.AcceptRole),
                ("Discard", QMessageBox.ButtonRole.DestructiveRole),
                ("Cancel", QMessageBox.ButtonRole.RejectRole),
            ),
        )
        if choice == 2 or choice < 0:
            return False
        if choice == 0:
            return self._save_project()
        return True

    def _new_project(self) -> None:
        if not self._confirm_replace("New project"):
            return
        self.install_document(EditorDocument())
        self.statusBar().showMessage("New project (empty composition, empty media bin).")

    def install_document(self, document: EditorDocument, *, mark_saved: bool = True) -> None:
        """Make *document* this window's content (Open, restore, scripted harnesses).

        One entry point for replacing everything at once, so nothing can end up
        half-loaded: composition, media catalogue, bin, path index, preview and
        playhead are all reset together.
        """
        self._apply_fresh_session(DocumentSession(document), mark_saved=mark_saved)

    def _apply_fresh_session(
        self, session: DocumentSession, *, mark_saved: bool = True
    ) -> None:
        """Swap in a session and everything derived from it (New, Open, restore).

        The media bin, the path index that refuses duplicate imports, the media
        records and the timeline all come from the same document, so a fresh
        session cannot leave the bin describing the previous project.
        """
        self.session = session
        if not mark_saved:
            # a recovered snapshot is unsaved work: it must not look saved
            session.replace_document(session.document, keep_dirty=True)
        self.project_path = None
        self._media_records = []
        self._last_snapshot_document = None
        self.bin_controller.reset()
        self._sync_bin()
        self.preview_cache.prune_orphans()
        self._preview = None
        self._preview_fingerprint = None
        self._stop_preview_playback()
        self.output_viewer.clear_preview_state()
        self.timeline.set_playhead(ZERO)
        self.timeline.set_clip_selection(())
        self._refresh_timeline()
        self._refresh_history_actions()
        self._update_title()

    def _open_project(self, path: Path | None = None) -> bool:
        """Open a file, reading and validating it *before* asking to replace work.

        Order matters: an unreadable or refused file must leave this window
        exactly as it was, so the unsaved-work prompt only appears when the work
        is genuinely about to be replaced — never as the price of a bad click.
        """
        if path is None:
            start = self.project_path.parent if self.project_path else Path.home()
            chosen, _ = QFileDialog.getOpenFileName(
                self, "Open project", str(start), "Video editor project (*.vep);;All files (*)"
            )
            if not chosen:
                return False
            path = Path(chosen)
        try:
            document = load_document(path)
        except ProjectFileError as exc:
            self._ask(
                "Project could not be opened",
                str(exc),
                "Your current project is unchanged. Nothing in this file was repaired "
                "or rewritten.",
            )
            return False
        if not self._confirm_replace("Open project"):
            return False
        self._adopt_document(document, path)
        return True

    def _adopt_document(
        self, document: LoadedDocument, path: Path, *, mark_saved: bool = True
    ) -> None:
        """Replace the window's content with a successfully validated document."""
        editor = EditorDocument(
            project=document.project,
            assets=tuple(record.asset for record in document.assets),
        )
        self.install_document(editor, mark_saved=mark_saved)
        self.project_path = path
        self._media_records = list(document.assets)
        self._restore_navigation(document)
        self.mark_preview_stale("A different project is now open")
        self._forget_decorations()
        self._update_title()
        unavailable = self._unavailable_assets()
        details = list(document.warnings)
        details.extend(record.note or "" for record in unavailable)
        if unavailable:
            self.statusBar().showMessage(
                f"Opened '{path.name}': {len(unavailable)} asset(s) are unavailable — "
                "the timeline is intact; relink them to render."
            )
            self._relink_offer(unavailable, details)
        else:
            name = path.name if path is not None else "recovered work"
            note = f"Opened '{name}'."
            if details:
                note += " " + details[0]
            self.statusBar().showMessage(note)

    def _restore_navigation(self, document: LoadedDocument) -> None:
        """Playhead/zoom/selection are convenience: restored when they are usable."""
        navigation = document.navigation
        self.timeline.set_view(navigation.scroll_x, BASE_PPS * Fraction(navigation.zoom))
        valid = {clip.id for clip in document.project.clips}
        selected = tuple(cid for cid in navigation.selected_clips if cid in valid)
        self.timeline.set_playhead(navigation.playhead)
        self.timeline.set_clip_selection(selected)

    def _confirm_save_target(self, path: Path, *, save_as: bool) -> bool:
        """Ask before replacing a file this project does not already live in.

        Plain Save writes the very file the project was loaded from, so a file
        being there is expected and must not be a prompt every time. Writing
        anywhere else — Save As, or a path handed to this method — replaces
        somebody's document and always asks, whoever chose the path.
        """
        if save_as or path != self.project_path:
            # proceed when there is nothing there, or when the user agreed to
            # replace it — answering "No" must stop the write, not cause one
            return not path.exists() or self._confirm_overwrite(path)
        return True

    def _save_project(self, save_as: bool = False, path: Path | None = None) -> bool:
        """Write the project file atomically; on failure nothing has changed."""
        if path is None:
            if not save_as and self.project_path is not None:
                path = self.project_path
            else:
                start = self.project_path.parent if self.project_path else Path.home()
                suggested = start / self._project_name()
                chosen, _ = QFileDialog.getSaveFileName(
                    self,
                    "Save project as",
                    str(suggested.with_suffix(".vep")),
                    "Video editor project (*.vep);;All files (*)",
                )
                if not chosen:
                    return False
                path = Path(chosen)
        # the chooser only guards its own overwrite on some platforms; this one
        # always runs, so the answer never depends on which dialog was used
        if not self._confirm_save_target(path, save_as=save_as):
            return False
        try:
            self._write_project(path)
        except ProjectFileError as exc:
            self._ask("Save failed", str(exc))
            return False
        return True

    def _confirm_overwrite(self, path: Path) -> bool:
        _shown, choice = self._ask(
            "Replace existing project file?",
            f"{path} already exists. Replace it?\n\nThe write is atomic: if it fails, "
            "the existing file is left exactly as it is.",
            buttons=(
                ("Yes", QMessageBox.ButtonRole.AcceptRole),
                ("No", QMessageBox.ButtonRole.RejectRole),
            ),
            default=1,
        )
        return choice == 0

    def _write_project(self, path: Path) -> Path:
        document = self._project_document(save_target=path)
        try:
            # the guard is the media catalogue only: replacing this project's own
            # file is what saving means, and the consent below is what authorises it
            save_document(
                document,
                path,
                protected=self._media_guard(),
                consent=Consent.capture(path),
            )  # raises before touching *path* on any failure
        except DestinationSafetyError as exc:
            raise ProjectFileError(str(exc)) from exc
        self.project_path = path
        self.session.mark_saved()
        self._last_snapshot_document = document
        self._update_title()
        self._take_recovery_snapshot(reason="saved")
        message = (
            f"Saved {path.name} "
            f"({len(self.session.project.clips)} clips, "
            f"{len(self.session.project.tracks)} tracks, "
            f"{len(self.bin_controller.assets)} assets)."
        )
        unavailable = self._unavailable_assets()
        if unavailable:
            message += f" {len(unavailable)} asset(s) are unavailable."
            self.statusBar().showMessage(message)
            self._ask(
                "Project saved with unavailable media",
                "The composition was saved exactly as it is, but these sources are not "
                "readable:\n\n"
                + "\n".join(
                    f"\u2022 {record.note or record.asset.display_name()}"
                    for record in unavailable
                )
                + "\n\nRendering is refused for that material until you relink it.",
            )
        else:
            self.statusBar().showMessage(message)
        return path

    # ------------------------------------------------------------------- relink
    def _relink_offer(self, unavailable: list[AssetRecord], details: list[str]) -> None:
        """Explicit, never automatic: the user decides whether to relink."""
        listing = "\n".join(
            f"• {record.asset.display_name()} — {record.state.value}" for record in unavailable
        )
        lines = [note for note in details if note]
        _shown, choice = self._ask(
            "Missing or changed media",
            "These sources could not be found as saved:\n\n" + listing
            + "\n\nThe timeline is intact and stays exactly as saved — you can inspect "
            "and save it. Rendering that material is refused until it is available.",
            "\n".join(lines[:6]),
            (
                ("Relink…", QMessageBox.ButtonRole.AcceptRole),
                ("Keep as is", QMessageBox.ButtonRole.RejectRole),
            ),
        )
        if choice == 0:
            self._relink_dialog(unavailable[0].asset.id)

    def _relink_missing_assets(self) -> None:
        candidates = self._unavailable_assets()
        if not candidates:
            names = [
                record for record in self._media_records if record.asset.probe_error
            ]
            if not names:
                self.statusBar().showMessage(
                    "Every asset is present and matches the identity recorded in the "
                    "project file."
                )
                return
            candidates = names
        ids = [record.asset.id for record in candidates]
        labels = [record.asset.display_name() for record in candidates]
        index, ok = QInputDialog.getItem(
            self, "Relink asset", "Which asset?", labels, 0, False
        )
        if not ok:
            return
        self._relink_dialog(ids[labels.index(index)])

    def _relink_dialog(self, asset_id: str) -> bool:
        asset = next(
            (a for a in self.bin_controller.assets if a.id == asset_id), None
        )
        if asset is None:
            self.statusBar().showMessage(f"No asset with id {asset_id} in this project.")
            return False
        start = Path(asset.path).parent if Path(asset.path).parent else Path.home()
        chosen, _ = QFileDialog.getOpenFileName(
            self, f"Relink '{asset.display_name()}' to a new file", str(start), _MEDIA_FILTER
        )
        if not chosen:
            return False
        return self._relink_asset_to(asset, Path(chosen))

    def _relink_asset_to(self, asset: Asset, new_path: Path) -> bool:
        """Probe, validate, then apply — never partially, never silently."""
        new_path = Path(new_path)
        entry = check(new_path, None, new_path.name)
        if not entry.usable:
            self._ask(
                "Relink refused",
                entry.describe(),
                "Nothing was changed: this asset still points at the file the project "
                "was saved with.",
            )
            return False
        sharing = [
            other.display_name()
            for other in self.bin_controller.assets
            if other.id != asset.id and same_file(Path(other.path), new_path)
        ]
        self.statusBar().showMessage(f"Probing {new_path.name} …")
        replacement = self._probe_asset(new_path)
        try:
            plan = relink_asset(
                asset,
                new_path,
                tuple(
                    source
                    for source in self.session.project.sources
                    if source.asset_id == asset.id
                ),
                self.session.project.clips,
                replacement=replacement,
            )
        except RelinkError as exc:
            self._ask(
                "Relink refused",
                str(exc),
                "Nothing was changed: this asset still points at the file the project "
                "was saved with, and no clip was retimed.",
            )
            return False
        before_project = self.session.project
        try:
            changed = self.session.apply(RelinkDocumentCommand(plan))
        except (CommandError, HistoryError) as exc:
            # refused whole: nothing is changed, and the reason is not a guess
            self.statusBar().showMessage(f"Relink refused: {exc}")
            self._ask(
                "Relink refused",
                str(exc),
                "Nothing was changed: this asset still points at the file the project "
                "was saved with, and no clip was retimed.",
            )
            return False
        # the catalogue row, the clip labels and the media records all follow the
        # document now, so there is nothing separate to patch here
        self._sync_bin()
        self._refresh_timeline()
        self._refresh_history_actions()
        self.mark_preview_stale("An asset now points at a different file")
        self._invalidate_stills()
        self._forget_decorations()  # the old file's pictures are not this file's
        note = f"Relinked '{asset.display_name()}' to {new_path.name}."
        if sharing:
            # not an error — but two assets reading one file is worth saying,
            # because a later edit of one is visibly an edit of the other
            note += f" Note: this file is also imported as {', '.join(sharing)}."
        if changed and self.session.project == before_project:
            # a replacement whose streams match the recorded ones exactly changes
            # the media, not the composition: say which, and be clear that the
            # saved document — and undo — still moved (TASK_10)
            note += (" The replacement's streams match what was recorded, so the "
                     "composition is unchanged; the project now points at this file.")
        elif not changed:
            note = (f"'{new_path.name}' is the file this asset already points at, "
                    "with the same streams: nothing changed.")
        if plan.notes:
            note += " " + plan.notes[0]
        self.statusBar().showMessage(note)
        return True

    def _probe_asset(self, path: Path) -> Asset:
        """Probe synchronously for a relink candidate (one file, sub-second)."""
        from ..media.relink import probe_replacement

        return probe_replacement(path)

    # ---------------------------------------------------------------- recovery
    def _recovery_store(self) -> RecoveryStore:
        return RecoveryStore(session_key(self.project_path))

    def _take_recovery_snapshot(self, reason: str = "autosave") -> None:
        """Periodic safety net in the application's own directory."""
        if not self.session.is_dirty and self.project_path is not None:
            return
        document = self._project_document()
        if document == self._last_snapshot_document:
            return  # nothing has changed since the last snapshot
        try:
            self._recovery_store().write(
                document,
                reason=reason,
                project_path=self.project_path,
                project_mtime_ns=project_file_mtime(self.project_path),
                label=self._project_name(),
            )
        except ProjectFileError as exc:
            self.recovery_timer.stop()  # do not keep hammering a bad directory
            log.warning("recovery snapshots are unavailable: %s", exc)
            self.statusBar().showMessage(f"Recovery snapshots unavailable: {exc}")
            return
        self._last_snapshot_document = document

    def _find_recovery_snapshots(self) -> list[Snapshot]:
        """Snapshots this window could meaningfully restore.

        Snapshots are keyed per project, but the situation recovery exists for is
        "the application died while some project was open": a start-up window has
        no project yet, so it must still find them — restoring one also reclaims
        the project path it was taken for. Once a project *is* open, only that
        project's snapshots and the nameless (never-saved) ones qualify: work for
        another file must never be offered as if it belonged here.
        """
        root = recovery_root()
        keys: set[str] = {session_key(self.project_path), session_key(None)}
        if root.is_dir():
            keys.update(
                directory.name for directory in root.iterdir() if directory.is_dir()
            )
        found: list[Snapshot] = []
        for key in keys:
            for snapshot in RecoveryStore(key).list():
                recorded = snapshot.meta.project_path
                if (self.project_path is None or not recorded
                        or recorded == str(self.project_path)):
                    found.append(snapshot)
        seen: set[Path] = set()
        unique = []
        for snapshot in sorted(found, key=lambda item: item.meta.created_ns, reverse=True):
            if snapshot.path not in seen:
                seen.add(snapshot.path)
                unique.append(snapshot)
        return unique

    def _startup_recovery_candidate(self) -> Snapshot | None:
        """The newest snapshot that is actually restorable."""
        for snapshot in self._find_recovery_snapshots():
            try:
                self._decoded_snapshot(snapshot)
            except ProjectFileError:
                continue  # already logged by the store; keep looking
            return snapshot
        return None

    @staticmethod
    def _decoded_snapshot(snapshot: Snapshot) -> LoadedDocument:
        """Decode a snapshot's document against the directory of its project file."""
        recorded = snapshot.meta.project_path
        base = Path(recorded).parent if recorded else None
        return decode_payload(snapshot.document, base_dir=base)

    def offer_startup_recovery(self) -> bool:
        """Offer restore/discard after an abnormal exit. Never restores implicitly."""
        snapshot = self._startup_recovery_candidate()
        if snapshot is None:
            return False
        conflict = disk_ahead_of_snapshot(snapshot)
        _shown, choice = self._ask(
            "Unsaved work was found",
            "This application did not close normally, and a recovery snapshot was "
            "found:\n\n" + describe_snapshot(snapshot),
            conflict or "",
            (
                ("Restore", QMessageBox.ButtonRole.AcceptRole),
                ("Discard snapshot", QMessageBox.ButtonRole.DestructiveRole),
                ("Cancel", QMessageBox.ButtonRole.RejectRole),
            ),
            default=0 if not conflict else 2,
        )
        if choice == 1:
            for key in {session_key(self.project_path), session_key(None)}:
                RecoveryStore(key).discard()
            self.statusBar().showMessage("Recovery snapshot discarded.")
            return False
        if choice == 0:
            return self._restore_snapshot(snapshot)
        return False

    def _restore_snapshot(self, snapshot: Snapshot) -> bool:
        conflict = disk_ahead_of_snapshot(snapshot)
        if conflict:
            self._ask("Restore refused", conflict)
            return False
        try:
            document = self._decoded_snapshot(snapshot)
        except ProjectFileError as exc:
            self._ask(
                "Snapshot could not be restored",
                f"That snapshot cannot be read by this version:\n\n{exc}",
            )
            return False
        recovered_path = Path(snapshot.meta.project_path) if snapshot.meta.project_path else None
        self._adopt_document(document, recovered_path, mark_saved=False)
        if recovered_path is not None and not recovered_path.exists():
            # the file it belonged to is gone: keep the name in the note, but the
            # window's own path stays empty so Save asks where to write
            self.project_path = None
        self.statusBar().showMessage(
            "Recovered work restored (shown as unsaved). Save it to write it to its "
            "project file."
        )
        return True

    def _show_recovery_status(self) -> None:
        self._take_recovery_snapshot(reason="manual")
        snapshots = self._find_recovery_snapshots()
        root = recovery_root()
        if not snapshots:
            text = f"No recovery snapshots (nothing to recover yet).\nSaved under {root}"
        else:
            text = (
                "Snapshots live in the application's own directory, never beside your "
                f"media or your project file:\n{root}\n\n"
                + "\n".join(describe_snapshot(snapshot) for snapshot in snapshots[:8])
            )
        _shown, choice = self._ask(
            "Recovery snapshots",
            text,
            buttons=(
                ("Restore newest", QMessageBox.ButtonRole.AcceptRole),
                ("Discard all", QMessageBox.ButtonRole.DestructiveRole),
                ("Close", QMessageBox.ButtonRole.RejectRole),
            ),
            default=2,
        )
        if choice == 0 and snapshots:
            self._restore_snapshot(snapshots[0])
        elif choice == 1:
            for key in {session_key(self.project_path), session_key(None)}:
                RecoveryStore(key).discard()
            self.statusBar().showMessage("Recovery snapshots discarded.")

    # ------------------------------------------------------------------ layout
    def _build_central(self) -> None:
        splitter = QSplitter(Qt.Vertical, self)

        viewer_box = QWidget(splitter)
        vb_layout = QVBoxLayout(viewer_box)
        vb_layout.setContentsMargins(4, 4, 4, 0)
        mode_row = QWidget(viewer_box)
        mode_layout = QHBoxLayout(mode_row)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.addWidget(QLabel("Viewer mode:"))
        self.viewer_mode = QComboBox(mode_row)
        self.viewer_mode.addItem("Source (media file)", "source")
        self.viewer_mode.addItem("Output (composition)", "output")
        self.viewer_mode.currentIndexChanged.connect(self._on_viewer_mode)
        mode_layout.addWidget(self.viewer_mode)
        mode_layout.addStretch(1)
        vb_layout.addWidget(mode_row)

        self.viewer_stack = QStackedWidget(viewer_box)
        self.source_viewer = SourceViewer(self.viewer_stack)
        self.output_viewer = OutputViewer(self.viewer_stack)
        self.viewer_stack.addWidget(self.source_viewer)
        self.viewer_stack.addWidget(self.output_viewer)
        vb_layout.addWidget(self.viewer_stack)

        self.timeline = Timeline(splitter)
        splitter.addWidget(viewer_box)
        splitter.addWidget(self.timeline)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        self.setCentralWidget(splitter)

        self.timeline.playheadChanged.connect(self._on_playhead)
        self.timeline.clipSelectionChanged.connect(self._on_clip_selection)
        self.timeline.trackSelected.connect(self._on_track_selected)
        self.timeline.trackToggleRequested.connect(self._on_track_toggle)
        self.timeline.clipMoveRequested.connect(self._on_move_requested)
        self.timeline.clipTrimRequested.connect(self._on_trim_requested)
    def _build_docks(self) -> None:
        self.media_bin = MediaBin(self.bin_controller, self)
        bin_dock = QDockWidget("Media", self)
        bin_dock.setObjectName("media_bin_dock")
        bin_dock.setWidget(self.media_bin)
        self.addDockWidget(Qt.LeftDockWidgetArea, bin_dock)
        self.media_bin_dock = bin_dock

        self.inspector = Inspector(self)
        insp_dock = QDockWidget("Inspector", self)
        insp_dock.setObjectName("inspector_dock")
        insp_dock.setWidget(self.inspector)
        self.addDockWidget(Qt.RightDockWidgetArea, insp_dock)

        self.inspector.clipFieldsApply.connect(self._on_inspector_apply)
        self.inspector.linkSelected.connect(self._on_link_selected)
        self.inspector.unlinkRequested.connect(self._on_unlink)
        self.inspector.trackRename.connect(self._on_track_rename)
        self.inspector.trackMove.connect(self._on_track_move)
        self.inspector.outputSettingsApply.connect(self._on_output_settings)
        self.inspector.validationError.connect(self.statusBar().showMessage)

        self.media_bin.itemSelectionChanged.connect(self._on_bin_selection)

    # ----------------------------------------------------------------- actions
    def _create_actions(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.import_action = QAction("&Import Media…", self)
        self.import_action.setShortcut(QKeySequence("Ctrl+I"))
        self.import_action.triggered.connect(self._on_import_media)
        file_menu.addAction(self.import_action)
        self.cancel_import_action = QAction("&Cancel Import", self)
        self.cancel_import_action.setEnabled(False)
        self.cancel_import_action.triggered.connect(self.bin_controller.cancel_all_imports)
        file_menu.addAction(self.cancel_import_action)
        file_menu.addSeparator()
        self.export_action = QAction("&Export Composition…", self)
        self.export_action.setShortcut(QKeySequence("Ctrl+E"))
        self.export_action.triggered.connect(self._export_composition)
        file_menu.addAction(self.export_action)
        self.cancel_export_action = QAction("Cancel E&xport", self)
        self.cancel_export_action.setEnabled(False)
        self.cancel_export_action.triggered.connect(self.render_controller.cancel)
        file_menu.addAction(self.cancel_export_action)
        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # Project menu: work is a *document* the user can save, reopen and
        # recover; media is only ever referenced, never written.
        project_menu = self.menuBar().addMenu("&Project")
        self.new_project_action = QAction("&New Project", self)
        self.new_project_action.setShortcut(QKeySequence("Ctrl+Shift+N"))
        self.new_project_action.triggered.connect(self._new_project)
        project_menu.addAction(self.new_project_action)
        self.open_project_action = QAction("&Open Project…", self)
        self.open_project_action.setShortcut(QKeySequence("Ctrl+O"))
        self.open_project_action.triggered.connect(lambda: self._open_project())
        project_menu.addAction(self.open_project_action)
        self.save_project_action = QAction("&Save Project", self)
        self.save_project_action.setShortcut(QKeySequence("Ctrl+S"))
        self.save_project_action.triggered.connect(lambda: self._save_project())
        project_menu.addAction(self.save_project_action)
        self.save_as_action = QAction("Save Project &As…", self)
        self.save_as_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.save_as_action.triggered.connect(lambda: self._save_project(save_as=True))
        project_menu.addAction(self.save_as_action)
        project_menu.addSeparator()
        self.media_status_action = QAction("&Media Status…", self)
        self.media_status_action.triggered.connect(self._show_media_status)
        project_menu.addAction(self.media_status_action)
        self.relink_action = QAction("&Relink Missing or Changed Asset…", self)
        self.relink_action.triggered.connect(self._relink_missing_assets)
        project_menu.addAction(self.relink_action)
        project_menu.addSeparator()
        self.recovery_status_action = QAction("Recovery S&napshots…", self)
        self.recovery_status_action.triggered.connect(self._show_recovery_status)
        project_menu.addAction(self.recovery_status_action)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.undo_action = QAction("&Undo", self)
        # One binding only: registering Ctrl+Z and the platform Undo standard key
        # (which is Ctrl+Z here) would make the shortcut ambiguous and Qt would
        # then refuse to trigger either.
        self.undo_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Undo))
        self.undo_action.triggered.connect(self._undo)
        edit_menu.addAction(self.undo_action)
        self.redo_action = QAction("&Redo", self)
        # The documented chord plus the platform standard. `StandardKey.Redo` is
        # Ctrl+Shift+Z on this Qt/platform, i.e. *the same chord*: one action
        # claiming a sequence twice makes it ambiguous and Qt then triggers
        # neither, so the list is de-duplicated by text before being installed.
        candidates = [
            QKeySequence("Ctrl+Shift+Z"),
            QKeySequence(QKeySequence.StandardKey.Redo),
            QKeySequence("Ctrl+Y"),
        ]
        unique: list[QKeySequence] = []
        seen: set[str] = set()
        for sequence in candidates:
            text = sequence.toString()
            if text and text not in seen:
                seen.add(text)
                unique.append(sequence)
        self.redo_action.setShortcuts(unique)
        self.redo_action.triggered.connect(self._redo)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        self.delete_action = QAction("&Delete Selected Clips", self)
        self.delete_action.setShortcut(QKeySequence.Delete)
        self.delete_action.triggered.connect(self._delete_selected_clips)
        edit_menu.addAction(self.delete_action)
        self.duplicate_action = QAction("&Duplicate Selected Clips…", self)
        self.duplicate_action.setShortcut(QKeySequence("Ctrl+D"))
        self.duplicate_action.triggered.connect(self._duplicate_selected)
        edit_menu.addAction(self.duplicate_action)
        edit_menu.addSeparator()
        self.link_action = QAction("&Link Selected Clips", self)
        self.link_action.triggered.connect(self._on_link_selected)
        edit_menu.addAction(self.link_action)
        self.unlink_action = QAction("&Unlink Selected Clip", self)
        self.unlink_action.triggered.connect(self._on_unlink)
        edit_menu.addAction(self.unlink_action)

        timeline_menu = self.menuBar().addMenu("&Timeline")
        self.place_action = QAction("&Place Selected Streams at Playhead", self)
        self.place_action.setShortcut(QKeySequence("Ctrl+G"))
        self.place_action.triggered.connect(self._place_selected_streams)
        timeline_menu.addAction(self.place_action)
        self.insert_action = QAction("&Insert Selected Streams at Playhead (ripple)", self)
        self.insert_action.setShortcut(QKeySequence("Ctrl+Shift+G"))
        self.insert_action.triggered.connect(self._insert_selected_streams)
        timeline_menu.addAction(self.insert_action)
        self.split_action = QAction("&Split Clip at Playhead", self)
        self.split_action.setShortcut(QKeySequence("S"))
        self.split_action.triggered.connect(self._split_selected_at_playhead)
        timeline_menu.addAction(self.split_action)
        timeline_menu.addSeparator()
        self.snap_action = QAction("&Snapping", self)
        self.snap_action.setCheckable(True)
        self.snap_action.setChecked(True)
        self.snap_action.toggled.connect(lambda on: setattr(self.timeline, "snap_enabled", on))
        timeline_menu.addAction(self.snap_action)
        timeline_menu.addSeparator()
        add_video = QAction("Add &Video Track", self)
        add_video.triggered.connect(lambda: self._apply(AddTrackCommand(TrackKind.VIDEO)))
        timeline_menu.addAction(add_video)
        add_audio = QAction("Add &Audio Track", self)
        add_audio.triggered.connect(lambda: self._apply(AddTrackCommand(TrackKind.AUDIO)))
        timeline_menu.addAction(add_audio)
        self.remove_track_action = QAction("Remove Selected &Empty Track", self)
        self.remove_track_action.triggered.connect(self._remove_selected_track)
        timeline_menu.addAction(self.remove_track_action)
        timeline_menu.addSeparator()
        go_action = QAction("&Go to Time…", self)
        go_action.setShortcut(QKeySequence("G"))
        go_action.triggered.connect(self._go_to_time)
        timeline_menu.addAction(go_action)
        fit_action = QAction("&Fit Timeline", self)
        fit_action.setShortcut(QKeySequence("F"))
        fit_action.triggered.connect(self.timeline.fit_whole)
        timeline_menu.addAction(fit_action)

        preview_menu = self.menuBar().addMenu("&Preview")
        self.preview_action = QAction("Render Output &Preview", self)
        # Ctrl+R only: Ctrl+P is the platform Print standard key, and binding two
        # chords that resolve to the same key makes Qt trigger neither.
        self.preview_action.setShortcut(QKeySequence("Ctrl+R"))
        self.preview_action.triggered.connect(lambda: self.output_viewer.render_button.click())
        preview_menu.addAction(self.preview_action)
        self.cancel_preview_action = QAction("Cancel Pre&view", self)
        self.cancel_preview_action.setEnabled(False)
        self.cancel_preview_action.triggered.connect(self.preview_controller.cancel)
        preview_menu.addAction(self.cancel_preview_action)
        self.step_back_action = QAction("Step Output Frame ◀", self)
        self.step_back_action.setShortcut(QKeySequence("Ctrl+Left"))
        self.step_back_action.triggered.connect(lambda: self._step_output_frames(-1))
        preview_menu.addAction(self.step_back_action)
        self.step_fwd_action = QAction("Step Output Frame ▶", self)
        self.step_fwd_action.setShortcut(QKeySequence("Ctrl+Right"))
        self.step_fwd_action.triggered.connect(lambda: self._step_output_frames(1))
        preview_menu.addAction(self.step_fwd_action)
        free_cache = QAction("&Free Preview Cache", self)
        free_cache.triggered.connect(self._free_preview_cache)
        preview_menu.addAction(free_cache)

        help_menu = self.menuBar().addMenu("&Help")
        self.help_action = QAction("&Keyboard and Workflow…", self)
        self.help_action.triggered.connect(self._show_help)
        help_menu.addAction(self.help_action)
        about_action = QAction("&About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

        self.bin_controller.job_started.connect(self._refresh_import_actions)
        self.bin_controller.job_finished.connect(self._refresh_import_actions)
        # an imported file is part of the saved document, so it is committed
        # through the session: clip labels, the output viewer and the dirty mark
        # all follow from the document, not from a separate bin list
        self.bin_controller.job_finished.connect(self._on_import_finished)

    # ------------------------------------------------------------ edit plumbing
    def _apply(self, command: Command) -> bool:
        """Run a composition command as a document transaction.

        The wrapper is the point: an edit to the timeline and an edit to the
        media catalogue are recorded by the same history and judged by the same
        dirty mark, so "one action, one undo" holds across both halves of the
        document. Failures are reported in the status bar (non-modal) and leave
        everything — selection, clips, tracks, catalogue, history — as it was.
        """
        return self._apply_document(EditComposition(command))

    def _apply_document(self, command: DocumentCommand) -> bool:
        """Apply a whole-document transaction, reporting refusal in the status bar."""
        try:
            changed = self.session.apply(command)
        except CommandError as exc:
            self.statusBar().showMessage(f"Edit refused: {exc}")
            return False
        except HistoryError as exc:  # pragma: no cover - defensive
            self.statusBar().showMessage(str(exc))
            return False
        if changed:
            self._after_document_change(command.describe())
        return changed

    def _sync_bin(self) -> None:
        """Show the catalogue the document holds, and index its paths.

        The bin rows and the "already imported" index are both views of the same
        state, so they are refreshed together: an undone import must free its file
        for import again, and an undone relink must re-claim the old path.

        The disk is re-read at the same moment. Availability belongs to the file,
        not to the snapshot, so an undone relink must not inherit the state word
        of the file it no longer points at (TASK_10). One check per asset, never
        per clip, as everywhere else.
        """
        assets = self.session.document.assets
        self.bin_controller.index(assets)
        self.media_bin.sync(assets)
        self._availability()

    def _after_document_change(self, description: str) -> None:
        """One place for what every accepted document change has to redo."""
        self._sync_bin()
        self._refresh_timeline()
        self._refresh_history_actions()
        self.mark_preview_stale("Composition changed since this preview was rendered")
        self.statusBar().showMessage(
            f"{description} ({'modified' if self.session.is_dirty else 'matching saved'})"
        )

    def _undo(self) -> None:
        try:
            self.session.undo()
        except HistoryError as exc:
            self.statusBar().showMessage(str(exc))
            return
        # the document may have changed on either side: an undone relink puts the
        # old file and the old stream records back together, an undone import
        # empties a bin row, and availability is always re-read from the disk
        self._sync_bin()
        self._refresh_timeline()
        self._refresh_history_actions()
        self.mark_preview_stale("Undo changed the composition after this preview")

    def _redo(self) -> None:
        try:
            self.session.redo()
        except HistoryError as exc:
            self.statusBar().showMessage(str(exc))
            return
        self._sync_bin()
        self._refresh_timeline()
        self._refresh_history_actions()
        self.mark_preview_stale("Redo changed the composition after this preview")

    def _refresh_history_actions(self) -> None:
        self.undo_action.setEnabled(self.session.can_undo)
        self.redo_action.setEnabled(self.session.can_redo)
        # the title carries the dirty mark, so every history change refreshes it
        self._update_title()

    def _refresh_timeline(self) -> None:
        self.output_viewer.set_output(self.session.project)
        labels = {}
        for asset in self.bin_controller.assets:
            labels[asset.id] = asset.display_name()
        self.timeline.set_asset_labels(labels)
        self.timeline.set_project(self.session.project)
        self.refresh_decorations()
        self.inspector.show_output(self.session.project)
        # keep selection pointing at clips that still exist (undo/track removal)
        valid = {c.id for c in self.session.project.clips}
        current = self.timeline.selected_clips
        kept = tuple(cid for cid in current if cid in valid)
        if kept != current:
            self.timeline.set_clip_selection(kept)
        self._invalidate_stills()

    # ------------------------------------------------------------- decorations
    def decoration_material(self) -> dict[tuple[str, int], Decorations]:
        """What each registered stream is, and which file it comes from now.

        Keyed by (asset id, stream index) exactly as clips name their material,
        and built from the project's own source records: a picture is asked for
        on the same terms an edit is validated against. The file's identity goes
        into the cache key, so replacing a file on disk cannot leave the old
        file's frames drawn over the new file's clips.

        An asset whose file is missing, replaced or unreadable gets **no**
        material: its clips are drawn as shapes, and the reason is said in the
        status bar. A thumbnail decoded from a different encode would look
        exactly like a correct thumbnail of this project's media.
        """
        material: dict[tuple[str, int], Decorations] = {}
        availability = self._availability()
        by_id = {asset.id: asset for asset in self.bin_controller.assets}
        unusable: list[str] = []
        for source in self.session.project.sources:
            asset = by_id.get(source.asset_id)
            if asset is None:
                continue
            entry = availability.get(source.asset_id)
            if entry is None or not entry.usable:
                if entry is not None and entry.note and entry.note not in unusable:
                    unusable.append(entry.note)
                continue
            material[(source.asset_id, source.stream_index)] = Decorations(
                source=source,
                path=entry.path,
                identity=(entry.found.size_bytes, entry.found.mtime_ns),
                recorded=(asset.size_bytes, asset.mtime_ns)
                if asset.size_bytes is not None and asset.mtime_ns is not None
                else None,
            )
        if tuple(unusable) != self._decoration_warnings:
            self._decoration_warnings = tuple(unusable)
            for note in unusable:
                self._on_decoration_failure(note)
        return material

    def refresh_decorations(self) -> None:
        """Re-point the timeline's optional pictures at the current material."""
        self.timeline.set_decoration_source(self.decorations, self.decoration_material())

    def _forget_decorations(self) -> None:
        """A different document, or a file that is not the one it was read from."""
        self.decorations.invalidate()
        self.refresh_decorations()

    def _on_decoration_failure(self, reason: str) -> None:
        """A decoration that could not be made is said out loud.

        The clip still draws its shape, and the timeline writes the reason on the
        clip when there is room, but a silent gap where a waveform should be reads
        like the media rather than like a failure to read it.
        """
        self.statusBar().showMessage(f"Timeline picture unavailable: {reason}", 10_000)
        log.info("decoration unavailable: %s", reason)

    def _on_import_finished(
        self, job_id: str, path: Path, asset: Asset | None, error: str | None
    ) -> None:
        """Commit one probed file to the document — or say why nothing was committed.

        A successful probe becomes part of the project here and nowhere else: it
        enters history, sets the dirty mark, and the bin shows it because the
        document now holds it. A failed or cancelled probe records nothing, so a
        file that could not be read cannot make the project unsaved.
        """
        committed, error, duplicate = self.bin_controller.finalize_finished(
            job_id, path, asset, error
        )
        if committed is None:
            if error == "cancelled":
                self.bin_controller.status_message.emit(f"Import cancelled: {path.name}")
            elif error:
                self.bin_controller.status_message.emit(f"Import failed for {path.name}: {error}")
            elif duplicate:
                self.bin_controller.status_message.emit(duplicate)
            return
        if not self._apply_document(AddAssetCommand(committed)):
            # the document refused it (an id or file already in the project):
            # nothing was recorded, and the path is free again for one reason:
            # it really is not in this project
            return
        note = "" if committed.editable else " — no editable streams"
        self.bin_controller.status_message.emit(f"Imported {committed.path.name}{note}")

    def _refresh_import_actions(self, *_args) -> None:
        self.cancel_import_action.setEnabled(self.bin_controller.pending_imports > 0)

    # ------------------------------------------------------------- UI handlers
    def _on_import_media(self) -> None:
        if not self.media_status.available:
            self.statusBar().showMessage(self.media_status.diagnostic())
            return
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import media", str(__import__("pathlib").Path.home()), _MEDIA_FILTER
        )
        if files:
            self.bin_controller.request_import([__import__("pathlib").Path(f) for f in files])

    def _on_bin_selection(self) -> None:
        selected = self.media_bin.selected_stream()
        asset = self.media_bin.selected_asset()
        if selected is not None:
            sel_asset, stream = selected
            self.source_viewer.set_source(sel_asset, stream)
            self.inspector.show_asset(sel_asset)
        elif asset is not None:
            self.source_viewer.set_source(asset, None)
            self.inspector.show_asset(asset)

    def _on_viewer_mode(self) -> None:
        mode = self.viewer_mode.currentData()
        # both players are stopped on a mode switch: nothing may keep playing audio
        # while the other mode claims to show something else
        self._stop_preview_playback()
        self.source_viewer.stop()
        if mode == "source":
            self.viewer_stack.setCurrentIndex(0)
        else:
            self.viewer_stack.setCurrentIndex(1)
            self.output_viewer.set_playhead(self.timeline.playhead)

    def _on_playhead(self, t: Fraction) -> None:
        self.output_viewer.set_playhead(t)
        if self.output_viewer.is_playing():
            # playing: the preview file is the picture, so seek it rather than
            # start a still decode for every intermediate position
            self._seek_preview(t)
            return
        # paused inspection: decode the exact output frame under the playhead
        self._request_still(t)

    def _on_clip_selection(self, clip_ids: tuple[str, ...]) -> None:
        project = self.session.project
        if len(clip_ids) == 1:
            clip = project.clip(clip_ids[0])
            if clip is not None:
                self.inspector.show_clip(project, clip)
        elif len(clip_ids) > 1:
            self.inspector.show_multi_selection(len(clip_ids))
        else:
            self.inspector.clear_details()
            track = (
                project.track(self.timeline.selected_track)
                if self.timeline.selected_track
                else None
            )
            self.inspector.set_selection(None, track, 0)

    def _on_track_selected(self, track_id: str | None) -> None:
        if track_id is not None:
            track = self.session.project.track(track_id)
            if track is not None:
                self.inspector.show_track(self.session.project, track)

    # ------------------------------------------------------ gesture handlers
    def _on_move_requested(self, clip_id: str, new_start: Fraction, track_id: str | None) -> None:
        self._apply(MoveClipCommand(clip_id, output_start=new_start, track_id=track_id))

    def _on_trim_requested(self, clip_id: str, edge: str, source_time: Fraction) -> None:
        edge_const = TrimEdge.LEFT if edge == "left" else TrimEdge.RIGHT
        self._apply(TrimClipCommand(clip_id, edge_const, source_time))

    # ---------------------------------------------------- inspector handlers
    def _on_inspector_apply(self, payload) -> None:
        clip_id, fields = payload
        project = self.session.project
        clip = project.clip(clip_id)
        if clip is None:
            return
        if "source_in" in fields:
            self._apply(TrimClipCommand(clip_id, TrimEdge.LEFT, fields["source_in"]))
        if "source_out" in fields:
            self._apply(TrimClipCommand(clip_id, TrimEdge.RIGHT, fields["source_out"]))
        if "output_start" in fields:
            self._apply(MoveClipCommand(clip_id, output_start=fields["output_start"]))
        if "rate" in fields:
            self._apply(SetRateCommand(clip_id, fields["rate"]))
        if "gain" in fields:
            self._apply(SetGainCommand(clip_id, fields["gain"]))
        if "fade_in" in fields or "fade_out" in fields:
            clip = self.session.project.clip(clip_id) or clip
            self._apply(
                SetFadesCommand(
                    clip_id,
                    fields.get("fade_in", clip.fade_in),
                    fields.get("fade_out", clip.fade_out),
                )
            )

    def _on_link_selected(self) -> None:
        self._apply(LinkClipsCommand(self.timeline.selected_clips))

    def _on_unlink(self, clip_id: str) -> None:
        self._apply(UnlinkClipCommand(clip_id))

    def _on_track_rename(self, track_id: str, name: str) -> None:
        self._apply(RenameTrackCommand(track_id, name))

    def _on_track_move(self, track_id: str, delta: int) -> None:
        tracks = self.session.project.tracks
        index = next((i for i, t in enumerate(tracks) if t.id == track_id), None)
        if index is None:
            return
        self._apply(ReorderTrackCommand(track_id, index + delta))

    def _on_output_settings(self, payload) -> None:
        """Project size/rate/layout are an ordinary undoable edit."""
        width, height, frame_rate, sample_rate, layout = payload
        self._apply(
            SetOutputSettingsCommand(width, height, frame_rate, sample_rate, layout)
        )

    def _on_track_toggle(self, track_id: str, which: str) -> None:
        track = self.session.project.track(track_id)
        if track is None:
            return
        if which == "enable":
            self._apply(SetTrackEnabledCommand(track_id, not track.enabled))
        else:
            self._apply(SetTrackMutedCommand(track_id, not track.muted))

    # ------------------------------------------------------- navigation/place
    def _go_to_time(self) -> None:
        current = format_time(self.timeline.playhead)
        text, ok = QInputDialog.getText(
            self, "Go to time", "Time (H:MM:SS.mmm or seconds):", text=current
        )
        if not ok:
            return
        try:
            t = parse_time_text(text)
        except ValueError as exc:
            self.statusBar().showMessage(f"Cannot parse time: {exc}")
            return
        self.timeline.set_playhead(t, from_user=True)
        self.timeline.scroll_to_playhead()

    def _stream_sources(
        self, asset: Asset, indices: Sequence[int]
    ) -> tuple[StreamSource, ...] | None:
        """Translate bin selections into the core's stream records.

        Returns None (with the reason in the status bar) for a stream this
        application will not edit: unknown bounds mean the clip's source
        interval could not be validated, so placing it would be a guess.
        """
        sources: list[StreamSource] = []
        for index in indices:
            stream = asset.metadata.stream(index) if asset.metadata else None
            if stream is None:
                name = asset.display_name()
                self.statusBar().showMessage(f"Media bin has no stream {index} of {name}")
                return None
            if not stream.editable:
                self.statusBar().showMessage(
                    f"Cannot place stream {index}: reliable bounds are unknown, editing "
                    "is disabled for it."
                )
                return None
            source = core_source_for(
                asset, stream, asset.metadata.position_of(index) if asset.metadata else None
            )
            if source is None:
                self.statusBar().showMessage(
                    f"Cannot place stream {index}: its bounds are not known."
                )
                return None
            sources.append(source)
        return tuple(sources)

    def _place_selected_streams(self) -> None:
        """Place the bin's selection as one action, on tracks chosen for it.

        Registering the streams, allocating a track when none is free and
        creating the clips are one transaction: one undo reverses the whole
        gesture, and a refusal leaves no half-placed group and no unused track
        behind (TASK_10).
        """
        items = self.media_bin.selectedItems()
        if not items:
            self.statusBar().showMessage("Select streams in the media bin first (Ctrl+G).")
            return
        asset = self.media_bin.selected_asset()
        if asset is None or asset.metadata is None:
            return
        indices = []
        for item in items:
            index = item.data(0, ROLE_STREAM_INDEX)
            if index is not None:
                indices.append(int(index))
        if not indices:
            self.statusBar().showMessage("Select one or more *streams* (expand an asset).")
            return
        sources = self._stream_sources(asset, indices)
        if sources is None:
            return
        playhead = self.timeline.playhead
        if len(sources) == 1:
            source = sources[0]
            command = PlaceStreamsCommand(
                (
                    StreamPlacement(
                        source=source,
                        output_start=playhead,
                        source_in=source.origin,
                        source_out=source.end,
                    ),
                )
            )
        else:
            # several streams of one file keep the offsets the file gives them,
            # and each gets its own lane so later edits stay independent
            command = PlaceStreamsCommand(
                group_placements(sources, playhead), track_choice=TrackChoice.FRESH
            )
        self._apply(command)

    def _split_selected_at_playhead(self) -> None:
        if len(self.timeline.selected_clips) != 1:
            self.statusBar().showMessage("Select exactly one clip to split.")
            return
        self._apply(SplitClipCommand(self.timeline.selected_clips[0], self.timeline.playhead))

    def _delete_selected_clips(self) -> None:
        if not self.timeline.selected_clips:
            return
        self._apply(RemoveClipsCommand(self.timeline.selected_clips))

    def _duplicate_selected(self) -> None:
        ids = self.timeline.selected_clips
        if not ids:
            self.statusBar().showMessage("Select clips to duplicate (Ctrl+D).")
            return
        clips = [c for c in (self.session.project.clip(cid) for cid in ids) if c is not None]
        if not clips:
            return
        block_start = min(c.output_start for c in clips)
        block_end = max(c.output_end for c in clips)
        text, ok = QInputDialog.getText(
            self,
            "Duplicate clips",
            "Destination output start (where the copy begins):",
            text=format_time(block_end),
        )
        if not ok:
            return
        try:
            destination = parse_time_text(text)
        except ValueError as exc:
            self.statusBar().showMessage(f"Cannot parse time: {exc}")
            return
        self._apply(DuplicateClipsCommand(ids, destination - block_start))

    def _insert_selected_streams(self) -> None:
        """Ripple-insert the selected stream at the playhead, in one transaction.

        The stream is registered, a lane is found or made, and later clips of
        that lane shift right — all as one command, so one undo puts the
        timeline and the registration back exactly as they were.
        """
        asset = self.media_bin.selected_asset()
        selected = self.media_bin.selected_stream()
        if asset is None or selected is None or asset.metadata is None:
            self.statusBar().showMessage("Select exactly one stream in the bin first.")
            return
        _asset, stream = selected
        if not stream.editable:
            self.statusBar().showMessage(
                "Cannot insert this stream: reliable bounds are unknown."
            )
            return
        source = core_source_for(asset, stream, asset.metadata.position_of(stream.stream_index))
        if source is None:
            self.statusBar().showMessage(
                "Cannot insert this stream: its bounds are not known."
            )
            return
        kind_value = stream.kind.value
        self._apply(
            PlaceStreamsCommand(
                (
                    StreamPlacement(
                        source=source,
                        output_start=self.timeline.playhead,
                        source_in=stream.origin,
                        source_out=stream.end_time,
                        # an insert goes on the track of that kind the project
                        # already has, rippling it; only a project with none
                        # gets a fresh lane
                        track_id=self._first_track_of_kind(kind_value),
                    ),
                ),
                track_choice=TrackChoice.FRESH,
                ripple=True,
            )
        )

    def _first_track_of_kind(self, kind_value: str) -> str | None:
        kind = TrackKind.VIDEO if kind_value == "video" else TrackKind.AUDIO
        for track in self.session.project.tracks:
            if track.kind is kind:
                return track.id
        return None

    def _remove_selected_track(self) -> None:
        track_id = self.timeline.selected_track
        if track_id is None:
            self.statusBar().showMessage("Select a track header first.")
            return
        clips = self.session.project.clips_on_track(track_id)
        if clips:
            self.statusBar().showMessage(
                "Track is not empty: delete its clips first (undo will bring "
                "everything back)."
            )
            return
        self._apply(RemoveTrackCommand(track_id))
        self.timeline.selected_track = None

    def _show_about(self) -> None:
        box = QPlainTextEdit(self)
        box.setReadOnly(True)
        box.setPlainText(
            f"Video Editor {__version__}\n\n"
            f"FFmpeg backend:\n  ffmpeg: {self.media_status.ffmpeg.path}\n"
            f"  ffprobe: {self.media_status.ffprobe.path}\n\n"
            "A non-destructive editor for combining audio and video from "
            "different editions of a film.\n\n"
            "Help ▸ Keyboard and Workflow… lists every shortcut as this running "
            "build defines them, and the order of work the editor expects."
        )
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.setWindowTitle("About")
        box.resize(560, 320)
        box.show()
        self._about_box = box  # keep alive

    # -------------------------------------------------------------------- help
    def _chord_rows(self) -> list[tuple[str, str, str]]:
        """(menu, command, chords) for every shortcut this window actually binds.

        Read from the live actions on purpose. A hand-written list is how
        ``Ctrl+Shift+Z`` came to be documented while redo was silently dead
        (``docs/progress.md``, TASK_07): the documentation and the binding drifted
        apart and nothing noticed.
        """
        rows: list[tuple[str, str, str]] = []
        for entry in self.menuBar().actions():
            menu = entry.menu()
            if menu is None:
                continue
            title = menu.title().replace("&", "")
            for action in menu.actions():
                if action.isSeparator():
                    continue
                chords = ", ".join(
                    sequence.toString()
                    for sequence in action.shortcuts()
                    if not sequence.isEmpty()
                )
                if chords:
                    rows.append((title, action.text().replace("&", ""), chords))
        return rows

    def _help_text(self) -> str:
        lines = ["Manual workflow — mixing two editions without automated matching", ""]
        lines += [f"  {step}" for step in WORKFLOW_STEPS]
        lines += ["", "Shortcuts as this build binds them", ""]
        for menu, command, chords in self._chord_rows():
            lines.append(f"  {chords:<22} {command}  [{menu}]")
        lines += ["", "Handled by the timeline itself", ""]
        for chord, meaning in WIDGET_KEYS:
            lines.append(f"  {chord:<22} {meaning}")
        lines += [
            "",
            "Every time can be typed exactly: H:MM:SS.mmm, plain seconds, or a",
            "rational such as 24000/1001. Numeric entry never snaps; snapping",
            "(Timeline ▸ Snapping) only attracts drags, within 8 screen pixels.",
            "Track headers carry enable/hide buttons; a hidden or muted track still",
            "counts towards the output length, so toggling it cannot shift anything.",
            "Audio needs no video selected: pick an audio stream in the bin and",
            "place it on any audio track, and mute or hide tracks independently.",
        ]
        return "\n".join(lines)

    def _show_help(self) -> None:
        box = QPlainTextEdit(self)
        box.setObjectName("help_dialog")
        box.setReadOnly(True)
        box.setPlainText(self._help_text())
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.setWindowTitle("Keyboard and workflow")
        box.resize(640, 520)
        box.show()
        self._help_box = box  # keep alive

    # ------------------------------------------------------------------ export
    def _build_export_ui(self) -> None:
        self.export_progress = QProgressBar(self)
        self.export_progress.setRange(0, 1000)
        self.export_progress.setTextVisible(False)
        self.export_progress.setMaximumWidth(220)
        self.export_progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.export_progress)
        self.render_controller.progress.connect(self._on_export_progress)
        self.render_controller.finished.connect(self._on_export_finished)
        self.render_controller.failed.connect(self._on_export_failed)
        self.render_controller.cancelled.connect(self._on_export_cancelled)

    def _asset_paths(self) -> dict[str, Path]:
        return {asset.id: Path(asset.path) for asset in self.bin_controller.assets}

    def _recorded_identities(self) -> dict[str, Identity]:
        """What each asset record says its file looked like when it was read."""
        return {
            asset.id: identity
            for asset in self.bin_controller.assets
            if (identity := recorded_identity(asset)) is not None
        }

    def _availability(self) -> dict[str, Availability]:
        """Live disk state of every asset — **one check per asset**, never per clip.

        Identity is not only checked at load: a film re-encoded at the same path
        between sessions, or while this window is open, must be reported as
        CHANGED rather than cut together with the old stream bounds. The result
        also refreshes ``_media_records``, so the media-status view and the export
        refusal always speak from the same observation.
        """
        report = check_catalogue(self.bin_controller.assets)
        self._media_records = [
            AssetRecord(
                asset=asset,
                state=report[asset.id].status,
                resolved_path=report[asset.id].path,
                note=report[asset.id].note,
            )
            for asset in self.bin_controller.assets
        ]
        return report

    def _media_guard(self) -> DestinationGuard:
        """Every imported file of this project, as paths no *document* may hit."""
        return DestinationGuard(protected_files(self.bin_controller.assets))

    def _write_guard(self) -> DestinationGuard:
        """What a rendered file may never be written over: the media, and this project.

        The project's own file is excluded from :meth:`_media_guard` because
        replacing it is precisely what saving means; for an export it is just as
        much a document to protect.
        """
        entries = list(protected_files(self.bin_controller.assets))
        if self.project_path is not None:
            entries.append(ProtectedFile.of(self.project_path, "this project's own file"))
        return DestinationGuard(entries)

    def _missing_asset_names(self) -> list[str]:
        """Assets the timeline uses whose files cannot be used right now.

        A different encode at the same path is reported as **changed**, separately
        from a missing one: the fix and the risk differ. Checked once per asset,
        not once per clip — this runs on every still-frame request (so every
        playhead move), and a long timeline has hundreds of clips over a few files.
        """
        availability = self._availability()
        by_id = {asset.id: asset for asset in self.bin_controller.assets}
        records = {record.asset.id: record for record in self._media_records}
        used: dict[str, None] = {}  # insertion order: first clip of each asset
        for clip in self.session.project.clips:
            used.setdefault(clip.asset_id, None)
        missing: list[str] = []
        for asset_id in used:
            entry = availability.get(asset_id)
            if entry is not None and entry.usable:
                continue
            asset = by_id.get(asset_id)
            record = records.get(asset_id)
            path = entry.path if entry is not None else (
                record.resolved_path if record else None
            )
            label = asset.display_name() if asset else asset_id
            state = "not in this project" if entry is None else entry.status.value
            entry_text = f"{label} [{state}: {path}]" if path else f"{label} [{state}]"
            if entry_text not in missing:
                missing.append(entry_text)
        return missing

    def _suggested_output_path(self) -> Path:
        for asset in reversed(self.bin_controller.assets):
            parent = Path(asset.path).parent
            if parent.is_dir():
                return parent / "composition.mp4"
        return Path("composition.mp4")

    def _export_composition(self) -> None:
        if self.render_controller.running:
            self.statusBar().showMessage("An export is already running.")
            return
        if not self.media_status.available:
            self.statusBar().showMessage(
                "Export needs ffmpeg/ffprobe: " + self.media_status.diagnostic()
            )
            return
        if not self.session.project.clips:
            self.statusBar().showMessage("Nothing to export: the composition is empty.")
            return
        missing = self._missing_asset_names()
        if missing:
            self.statusBar().showMessage(
                "Rendering is refused until these sources are available again: "
                + ", ".join(missing)
            )
            return
        dialog = ExportDialog(
            self, self.session.project, self._suggested_output_path(), self.timeline.playhead
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        choices = dialog.choices()
        guard = self._write_guard()
        try:
            guard.check(choices.destination, "export")
        except DestinationSafetyError as exc:
            # named in the status bar, not in a modal: this is a refusal the user
            # can act on immediately by choosing another name
            self.statusBar().showMessage(str(exc), 15_000)
            return
        overwrite = False
        if choices.destination.exists():
            answer = QMessageBox.question(
                self,
                "Replace existing file?",
                f"{choices.destination} already exists. Replace it?\n"
                "(Your imported assets are never modified.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.statusBar().showMessage("Export not started: destination exists.")
                return
            overwrite = True
        request = RenderRequest(
            project=self.session.project,  # immutable snapshot: editing may continue
            asset_paths=self._asset_paths(),
            destination=choices.destination,
            out_start=choices.out_start,
            out_end=choices.out_end,
            preset=choices.preset,
            overwrite=overwrite,
            guard=guard,
            asset_identities=self._recorded_identities(),
        )
        if not self.render_controller.start(request):  # pragma: no cover - race guard
            self.statusBar().showMessage("An export is already running.")
            return
        self.export_progress.setVisible(True)
        self.export_progress.setValue(0)
        self.cancel_export_action.setEnabled(True)
        self.export_action.setEnabled(False)
        self.statusBar().showMessage(f"Exporting to {choices.destination} …")

    def _on_export_progress(self, fraction: float) -> None:
        self.export_progress.setValue(int(fraction * 1000))

    def _export_ui_done(self) -> None:
        self.export_progress.setVisible(False)
        self.cancel_export_action.setEnabled(False)
        self.export_action.setEnabled(True)

    def _on_export_finished(self, result) -> None:
        self._export_ui_done()
        message = describe_result(result)
        if result.warnings:
            message += " · " + "; ".join(result.warnings)
        self.statusBar().showMessage(message)
        for warning in result.warnings:
            log.warning("export warning: %s", warning)

    def _on_export_failed(self, message: str) -> None:
        self._export_ui_done()
        self.statusBar().showMessage(f"Export failed: {message}")
        QMessageBox.warning(self, "Export failed", message)

    def _on_export_cancelled(self) -> None:
        self._export_ui_done()
        self.statusBar().showMessage("Export cancelled; no file was written.")

    # ----------------------------------------------------------- composition preview
    def _build_preview_ui(self) -> None:
        """Preview cache, background renderers and their signal wiring.

        The cache lives in a directory the application owns (temp directory by
        default, overridable with ``VIDEO_EDITOR_PREVIEW_DIR``) and is bounded by
        bytes and entry count; only files it created are ever deleted.
        """
        root = Path(os.environ.get("VIDEO_EDITOR_PREVIEW_DIR") or Path(tempfile.gettempdir()))
        self.preview_cache = PreviewCache(root / "video-editor-previews")
        self.preview_controller = PreviewController(self)
        self.still_controller = StillController(self)
        self.source_step_controller = SourceStepController(self)
        self._stills = FrameCache()  # decoded stills, bounded by picture bytes
        self.decorations = ClipDecorations(self)  # timeline thumbnails and waveforms
        self._decoration_warnings: tuple[str, ...] = ()  # said once per change, not per paint
        self.decorations.complained.connect(self._on_decoration_failure)
        self._source_shown: StillFrame | None = None
        # stepping counts queued against the last picture actually shown, so five
        # clicks mean five frames even if only one decode runs
        self._source_step_base: Fraction | None = None
        self._source_steps = 0
        # (cache key, range start, range end, scale, file or None, generation)
        self._preview: tuple | None = None
        self._preview_fingerprint: str | None = None

        self._media_released = False  # shutdown must be safe to attempt twice
        self.preview_player = QMediaPlayer(self)
        self.preview_audio = QAudioOutput(self)
        self.preview_player.setAudioOutput(self.preview_audio)
        self.preview_player.errorChanged.connect(self._on_preview_playback_error)
        self.preview_player.playbackStateChanged.connect(self._on_preview_playback_state)
        self.preview_player.mediaStatusChanged.connect(self._on_preview_media_status)
        self._pending_seek: Fraction | None = None  # start position, applied on load

        self.preview_progress = QProgressBar(self)
        self.preview_progress.setRange(0, 1000)
        self.preview_progress.setTextVisible(False)
        self.preview_progress.setMaximumWidth(160)
        self.preview_progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.preview_progress)

        self.output_viewer.previewRequested.connect(self._on_preview_requested)
        self.output_viewer.cancelPreviewRequested.connect(self.preview_controller.cancel)
        self.output_viewer.freeCacheRequested.connect(self._free_preview_cache)
        self.output_viewer.playheadStepped.connect(self._step_output_frames)
        self.output_viewer.sourceStepRequested.connect(self._step_source_frame)
        self.output_viewer.sourceMarkRequested.connect(self._mark_source_edge)
        self.output_viewer.playbackRequested.connect(self._play_preview)
        self.output_viewer.pauseRequested.connect(self._stop_preview_playback)

        self.preview_controller.progress.connect(self._on_preview_progress)
        self.preview_controller.finished.connect(self._on_preview_finished)
        self.preview_controller.failed.connect(self._on_preview_failed)
        self.preview_controller.cancelled.connect(self._on_preview_cancelled)
        self.still_controller.finished.connect(self._on_still_finished)
        self.still_controller.failed.connect(self._on_still_failed)
        self.source_step_controller.finished.connect(self._on_source_step_finished)
        self.source_step_controller.failed.connect(self._on_source_step_failed)

    def _preview_state_ok(self) -> str | None:
        """Why a preview cannot be rendered now, or None if it can."""
        if not self.media_status.available:
            return "Preview needs ffmpeg/ffprobe: " + self.media_status.diagnostic()
        if not self.session.project.clips:
            return "Nothing to preview: the composition is empty."
        missing = self._missing_asset_names()
        if missing:
            return ("Rendering is refused until these sources are available again: "
                  + ", ".join(missing))
        return None

    def _on_preview_requested(self, start, end, scale) -> None:
        problem = self._preview_state_ok()
        if problem:
            self.output_viewer.preview_failed(problem)
            self.statusBar().showMessage(problem)
            return
        if start is None or end is None:
            # empty fields: the default short window around the playhead
            try:
                window = preview_range(self.timeline.playhead, self.session.project)
            except PreviewError as exc:
                self.output_viewer.preview_failed(str(exc))
                self.statusBar().showMessage(f"Preview refused: {exc}")
                return
            start, end = window.start, window.end
        try:
            job = prepare_preview(
                self.session.project,
                self._asset_paths(),
                start,
                end,
                self.preview_cache,
                preset=PREVIEW_H264_FAST,
                geometry_scale=scale,
                asset_identities=self._recorded_identities(),
                guard=self._write_guard(),
            )
        except (PreviewError, RenderPlanError) as exc:
            self.output_viewer.preview_failed(str(exc))
            self.statusBar().showMessage(f"Preview refused: {exc}")
            return
        if job.reused:
            self._accept_preview(job.key, start, end, scale, job.request.destination)
            self.output_viewer.preview_reused(
                job.request.destination, start, end, scale
            )
            self.statusBar().showMessage(
                f"Preview reused from cache: {job.request.destination.name}"
            )
            return
        self.output_viewer.preview_started(start, end, scale)
        generation = self.preview_controller.start(job)
        self._preview = (job.key, start, end, scale, None, generation)
        self.cancel_preview_action.setEnabled(True)
        self.preview_progress.setVisible(True)
        self.preview_progress.setValue(0)
        self.statusBar().showMessage(
            f"Rendering preview {format_time(start)}-{format_time(end)} …"
        )

    def _on_preview_progress(self, generation: int, fraction: float) -> None:
        if self._preview is None or generation != self._preview[5]:
            return  # obsolete render: not what the viewer is showing
        self.preview_progress.setValue(int(fraction * 1000))
        self.output_viewer.preview_progress(fraction)

    def _on_preview_finished(self, generation: int, result) -> None:
        if self._preview is None or generation != self._preview[5]:
            return
        self.cancel_preview_action.setEnabled(False)
        key, start, end, scale, _path, _gen = self._preview
        self._accept_preview(key, start, end, scale, result.path)
        self.preview_progress.setVisible(False)
        self.output_viewer.preview_ready(result.path, start, end, scale)
        message = (
            f"Preview ready: {result.path.name} "
            f"({format_time(start)}-{format_time(end)}, "
            f"{result.path.stat().st_size // 1024} KiB)"
        )
        if result.warnings:
            message += " · " + "; ".join(result.warnings)
        self.statusBar().showMessage(message)

    def _on_preview_failed(self, generation: int, message: str) -> None:
        if generation != self.preview_controller.generation:
            return  # a superseded render's failure is not the user's problem
        self.cancel_preview_action.setEnabled(False)
        self.preview_progress.setVisible(False)
        self.output_viewer.preview_failed(message)
        self.statusBar().showMessage(f"Preview failed: {message}")

    def _on_preview_cancelled(self, generation: int) -> None:
        self.cancel_preview_action.setEnabled(False)
        self.preview_progress.setVisible(False)
        if self._preview is not None and generation == self._preview[5]:
            self._preview = None
            self.output_viewer.preview_cancelled()
            self.statusBar().showMessage("Preview cancelled.")

    def _accept_preview(self, key: str, start: Fraction, end: Fraction, scale: Fraction,
                        path: Path) -> None:
        self._preview = (key, start, end, scale, path, -1)
        self._preview_fingerprint = render_fingerprint(
            self.session.project,
            self._asset_paths(),
            start,
            end,
            scale,
            PREVIEW_H264_FAST,
        )

    def mark_preview_stale(self, reason: str) -> None:
        """The composition changed: never present the old render as current."""
        if self._preview is None or self._preview[4] is None:
            return  # nothing rendered to become stale
        key, start, end, scale, _path, _gen = self._preview
        current = render_fingerprint(
            self.session.project,
            self._asset_paths(),
            start,
            end,
            scale,
            PREVIEW_H264_FAST,
        )
        if current == key:
            return  # e.g. a track rename: the rendered media would be identical
        self.output_viewer.mark_stale(reason)
        self._preview = (key, start, end, scale, None, -1)

    def _free_preview_cache(self) -> None:
        removed = self.preview_cache.close()
        self.preview_cache.prune_orphans()
        self._stills.clear()
        self._source_shown = None
        self._source_step_base = None
        self._source_steps = 0
        self._preview = None
        self._preview_fingerprint = None
        self._stop_preview_playback()
        self.output_viewer.set_state(
            f"Preview cache freed ({len(removed)} file(s) deleted).", "info"
        )
        self.statusBar().showMessage(
            f"Preview cache freed: {len(removed)} file(s) deleted (only files this "
            "application created)."
        )

    # ------------------------------------------------------- still-frame inspection
    def _invalidate_stills(self) -> None:
        """A changed composition invalidates every decoded output frame.

        The viewer must never keep showing a picture the composition no longer
        produces, so decoded frames are dropped and the frame under the playhead is
        decoded again (one asynchronous decode, superseded if the user moves on).
        """
        if not hasattr(self, "still_controller"):
            return  # preview panel not built yet: nothing decoded to invalidate
        self._stills.clear()
        self._source_shown = None
        self._source_step_base = None
        self._source_steps = 0
        self._request_still(self.timeline.playhead)

    def _request_still(self, output_time: Fraction) -> None:
        if self._preview_state_ok():
            return
        rate = self.session.project.output.frame_rate
        index = int((output_time * rate * 2 + 1) // 2)
        cached = self._stills.get((index, 0))
        if cached is not None:
            self._show_still(cached.payload)
            return
        self.output_viewer.still_pending(output_time)
        self.still_controller.request(
            self.session.project,
            self._asset_paths(),
            output_time,
            self._recorded_identities(),
        )

    def _show_still(self, frame: StillFrame) -> None:
        """Present one decoded picture, saying what is known about where it came from.

        The source position offered to the user is the one **measured from this
        decode**, not the time the plan asked for: on variable-frame-rate material
        the two differ, and a cut taken from the planned time would not be at the
        picture on screen. When the decode reported no usable timestamp the
        interface says so instead of repeating the planned time as if it had been
        observed.
        """
        self._source_shown = frame
        note = ""
        if frame.clip_id:
            note = f"clip {frame.clip_id} · stream {frame.stream_index}"
            if frame.decoded_source_pts is None:
                note += " (the source position of this picture could not be measured)"
            elif frame.decoded_source_pts != frame.source_pts:
                note += (
                    f" · showing the real frame at {frame.decoded_source_pts}s, the last "
                    f"one at or before the planned {frame.source_pts}s"
                )
            if not frame.grid_exact:
                note += (
                    " · container time base unreadable, so the sampling grid is not "
                    "exactly placed"
                )
            if frame.source_only:
                note += " · stepped to a source frame this output frame does not present"
        self.output_viewer.show_still(frame, note)

    def _on_still_finished(self, generation: int, frame) -> None:
        if generation != self.still_controller.generation:
            return  # superseded while decoding
        self._stills.put(
            CachedFrame(
                key=(frame.frame_index, 0),
                width=frame.width,
                height=frame.height,
                rgb=frame.rgb,
                payload=frame,
            )
        )
        self._show_still(frame)

    def _on_still_failed(self, generation: int, message: str) -> None:
        if generation != self.still_controller.generation:
            return
        self.output_viewer.still_failed(message)
        self.statusBar().showMessage(f"Still frame: {message}")

    def _step_source_frame(self, steps: int) -> None:
        """Step by whole *source* frames: a new picture, decoded, newest wins.

        A source step changes the picture and the reported presentation time
        together, or neither: the neighbour is taken from the stream's own
        timestamps and the picture for it is decoded in the worker thread (reading
        those timestamps is a subprocess too, so it never runs on the GUI thread).

        Repeated clicks add up rather than piling up work: the request asks for
        ``steps`` frames from the last picture actually shown, so five quick clicks
        move five frames even though only one decode happens, and a decode that
        finishes after a newer request was made is discarded by the controller's
        generation check instead of being shown out of order.
        """
        frame = self._source_shown
        if frame is None or frame.black or frame.clip_id is None:
            self.statusBar().showMessage("Inspect a source frame first.")
            return
        if frame.decoded_source_pts is None:
            self.statusBar().showMessage(
                "This picture's source position could not be measured, so stepping by "
                "source frame would guess; jump the playhead to another frame instead."
            )
            return
        base = self._source_step_base or frame.decoded_source_pts
        self._source_step_base = base
        self._source_steps = (self._source_steps or 0) + steps
        self.output_viewer.source_step_pending(steps)
        self.source_step_controller.request(
            self.session.project,
            self._asset_paths(),
            frame.clip_id,
            base,
            self._source_steps,
            frame.output_time,
            self._recorded_identities(),
        )

    def _on_source_step_finished(self, generation: int, frame) -> None:
        if generation != self.source_step_controller.generation:
            return  # a newer step won; this picture is not what the user asked for now
        self._source_step_base = None
        self._source_steps = 0
        self._show_still(frame)
        self.statusBar().showMessage(
            f"Source frame at {frame.decoded_source_pts}s asset-local: picture and "
            "reported time decoded together."
        )

    def _on_source_step_failed(self, generation: int, message: str) -> None:
        if generation != self.source_step_controller.generation:
            return
        self._source_step_base = None
        self._source_steps = 0
        self.output_viewer.still_failed(message)
        self.statusBar().showMessage(f"Source step: {message}")

    def _mark_source_edge(self, which: str) -> None:
        """Move a clip edge onto the source frame actually being shown.

        Refused while a step is in flight or when the picture's source position was
        not measured: setting a cut from a time that is not the one on screen - or
        not known at all - would make the edit somewhere the user is not looking.
        """
        if self.source_step_controller.busy:
            self.statusBar().showMessage(
                "A source frame is still being decoded: not setting a cut from the "
                "picture before it."
            )
            return
        frame = self._source_shown
        if frame is None or frame.clip_id is None:
            self.statusBar().showMessage("Inspect a source frame first.")
            return
        if frame.decoded_source_pts is None:
            self.statusBar().showMessage(
                "The source position of this picture could not be measured: refusing to "
                "set a cut from a time that was not observed."
            )
            return
        edge = TrimEdge.LEFT if which == "in" else TrimEdge.RIGHT
        self._apply(TrimClipCommand(frame.clip_id, edge, frame.decoded_source_pts))

    def _step_output_frames(self, delta_frames: Fraction) -> None:
        rate = self.session.project.output.frame_rate
        self.timeline.set_playhead(
            self.timeline.playhead + delta_frames / rate, from_user=True
        )

    # ------------------------------------------------------------ preview playback
    def _play_preview(self) -> None:
        if self._preview is None or self._preview[4] is None:
            self.statusBar().showMessage("No current preview file to play.")
            return
        _key, start, end, _scale, path, _gen = self._preview
        self._stop_preview_playback()
        # start where the user is looking: the preview file begins at its range
        # start, so the playhead has to be mapped into it
        offset = playback_offset(start, self.timeline.playhead, end)
        if offset is None:
            self.statusBar().showMessage(
                f"Playhead is outside the previewed range "
                f"({format_time(start)}-{format_time(end)}): playing from its start."
            )
        self._pending_seek = offset if offset is not None else ZERO
        self.preview_player.setSource(QUrl.fromLocalFile(str(path)))
        self.output_viewer.set_playing(True)
        self.preview_player.play()

    def _on_preview_media_status(self, status) -> None:
        """Apply the requested start position once the file is actually loaded."""
        if status == QMediaPlayer.MediaStatus.LoadedMedia and self._pending_seek is not None:
            self.preview_player.setPosition(int(float(self._pending_seek) * 1000))
            self._pending_seek = None

    def _seek_preview(self, output_time: Fraction) -> None:
        """While playing, a playhead move seeks the preview instead of decoding a still."""
        if self._preview is None or self._preview[4] is None:
            return
        _key, start, end, _scale, _path, _gen = self._preview
        offset = playback_offset(start, output_time, end)
        if offset is None:
            self.statusBar().showMessage(
                f"The preview covers {format_time(start)}-{format_time(end)}: render a "
                "range around this time to see it move."
            )
            return
        self.preview_player.setPosition(int(float(offset) * 1000))

    def _stop_preview_playback(self) -> None:
        self._pending_seek = None
        self.preview_player.stop()
        self.output_viewer.set_playing(False)

    def _on_preview_playback_error(self, *_args) -> None:
        self.output_viewer.set_playing(False)
        message = (
            f"The playback backend cannot play the generated preview "
            f"({self.preview_player.errorString()}). The file is still on disk and can "
            "be opened in another player; the still frame above is unaffected."
        )
        self.output_viewer.preview_failed(message)
        self.statusBar().showMessage(message)

    def _on_preview_playback_state(self, state) -> None:
        self.output_viewer.set_playing(state == QMediaPlayer.PlayingState)

    def closeEvent(self, event) -> None:
        if not self._confirm_quit():
            event.ignore()
            return
        if self.render_controller.running:
            answer = QMessageBox.question(
                self,
                "Export in progress",
                "An export is still running. Cancel it and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.render_controller.cancel()
            self.render_controller.wait_finished(5000)
        if self.session.is_dirty:
            # last safety net: a window closed with unsaved work leaves a snapshot
            self._take_recovery_snapshot(reason="closed with unsaved work")
        self._shutdown_preview()
        super().closeEvent(event)

    def _shutdown_preview(self) -> None:
        """Cancel background work and release owned cache files.

        Idempotent: a window can legitimately be closed twice (close, then quit),
        and the test harness may reach a window that already closed itself. The
        second pass must do nothing rather than touch objects the first pass
        released.
        """
        if self._media_released:
            return
        self._stop_preview_playback()
        self.source_viewer.stop()
        self.preview_controller.stop()  # terminates FFmpeg, removes staging files
        self.still_controller.shutdown()  # no decoder thread outlives the window
        self.source_step_controller.shutdown()
        self.decorations.shutdown()  # ... and no thumbnail or waveform worker either
        self._release_media_playback()
        self._media_released = True
        if os.environ.get("VIDEO_EDITOR_KEEP_PREVIEWS") != "1":
            self.preview_cache.close()  # only files this cache created
        self.preview_cache.prune_orphans()

    def _release_media_playback(self) -> None:
        """Let the multimedia backends release their files while the window lives.

        A `QMediaPlayer` that still holds a source keeps its backend alive and
        keeps the file open. Releasing it when the window closes is what closing
        means; leaving it for interpreter shutdown also means it dies after Qt has
        begun tearing itself down, which is how a quit can end in an abort. The
        players themselves are left alone — other parts of the window (and tests
        that substitute a fake) still hold them.
        """
        for player in (self.preview_player, self.source_viewer.player):
            player.stop()
            if hasattr(player, "setSource"):
                player.setSource(QUrl())


_ROLE_STREAM_INDEX = Qt.UserRole + 2  # kept for compatibility with media_bin roles


def run_app(argv: list[str] | None = None) -> int:
    """Start the Qt application and return the process exit code."""
    import sys

    from PySide6.QtWidgets import QApplication

    def _hook(kind, value, tb):
        log.critical("Unhandled error", exc_info=(kind, value, tb))

    sys.excepthook = _hook

    app = QApplication.instance() or QApplication(sys.argv[:1] + (argv or []))
    MainWindow.quit_confirm_required = True  # a real session prompts before losing work
    window = MainWindow(offer_recovery=True)  # the only place recovery is offered
    window.show()
    code = app.exec()
    # Quitting ends here, not during interpreter shutdown. Left alone, the window
    # (its render/preview/still/decoration threads and its media players) would be
    # destroyed while Qt is already tearing itself down, and the process aborts
    # after the session ended: an editor that crashes on quitting has lost work the
    # user believes is saved.
    window._shutdown_preview()
    window.deleteLater()
    for _ in range(5):
        app.processEvents()
    return code
