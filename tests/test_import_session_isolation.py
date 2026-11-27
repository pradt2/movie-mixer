"""TASK_15: an import completion can only reach the project session that asked for it.

The defect this pins down is a timing one that timing cannot test: a probe started
for project A answering after the window has moved on to project B. Every test
here hands the completion over **by hand**, so the order of events is written in
the test rather than inferred from how long something took — there is no
``time.sleep`` in this file and no assertion depends on a duration.

How a late completion is delivered
----------------------------------
``MediaBinController.importer`` is replaced by :class:`FakeImportController`, which
implements what the bin calls (``import_paths``, ``cancel_all``, ``pending_count``,
``capacity``) and nothing else: no threads, no processes. A job answers only when
the test calls :meth:`FakeImportController.deliver`, which is the same entry point
a real worker uses (``MediaBinController._worker_finished``, the ``on_finished``
callback the real controller is built with); on the test thread that *is* the UI
thread, which is where a Qt queued connection delivers a real worker's completion.
:meth:`FakeImportController.cancel_all` only records the request, leaving the test
to decide what that cancelled job finally reports — "cancelled", a success the
probe had already reached, or a failure. All three are reports a worker can still
put on the queue after the UI has moved on. One test also sets
``report_on_cancel``, which makes that cancellation answer **inside** the call:
the real controller completes a queued job synchronously on the cancelling
thread, so this is how a completion can arrive in the middle of New or Open.

What is asserted
----------------
the new session's placeholder and new asset are untouched by an old job; the old
job's cancellation cannot reach the new job; a valid job is published exactly once
and undo puts the catalogue back; both an **empty** and a **populated** target
project behave the same; a duplicate or a document-refused import leaves history
length and the dirty mark alone; and a written project file contains no job or
placeholder state at all.

The media is plain generated files plus hand-made ``Asset`` records: the probe is
what the fake replaces, so no FFmpeg is needed and every catalogue entry is known
exactly.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from gui_helpers import media_record
from video_editor.core.model import Project
from video_editor.media.assets import Asset
from video_editor.media.ffprobe import CANCELLED_MESSAGE
from video_editor.media.importer import ImportSubmission
from video_editor.project.document import EditorDocument
from video_editor.ui.main_window import MainWindow
from video_editor.ui.media_bin import MediaBinController

#: keys a project file would have to grow in order to remember import work
_FORBIDDEN_KEYS = {
    "job",
    "jobs",
    "job_id",
    "epoch",
    "session_epoch",
    "placeholder",
    "placeholders",
    "import",
    "imports",
    "pending",
    "probe",
    "probing",
}


# --------------------------------------------------------------------- plumbing
def media_file(tmp_path: Path, name: str) -> Path:
    """A real file, so path resolution and recorded identity behave normally."""
    path = tmp_path / name
    path.write_bytes(b"\x00\x11" * 256)
    return path


def document_keys(node: object) -> set[str]:
    """Every mapping key anywhere inside a built document, however deeply nested."""
    if isinstance(node, dict):
        found = set(node)
        for value in node.values():
            found |= document_keys(value)
        return found
    if isinstance(node, (list, tuple)):
        nested: set[str] = set()
        for value in node:
            nested |= document_keys(value)
        return nested
    return set()


def history_depth(session) -> int:
    """How many transactions the session holds, measured through the public API.

    ``EditHistory`` publishes no length, so the depth is counted the way a user
    would experience it: undo back to the beginning, redo forward to the end, and
    the document must be exactly where it started afterwards.
    """
    start = session.document
    depth = 0
    while session.can_undo:
        session.undo()
        depth += 1
    while session.can_redo:
        session.redo()
    assert session.document == start, "walking the history did not return to the start"
    return depth


class FakeJob:
    """The two fields of an ``ImportJob`` that the bin actually reads."""

    def __init__(self, path: Path) -> None:
        self.id = uuid.uuid4().hex
        self.path = path


class FakeImportController:
    """Stands in for ``media.importer.ImportController`` with the clock removed.

    Nothing about it is automatic: a job stays live until the test answers it, and
    ``cancel_all`` only records that the bin asked. The test then delivers whatever
    that cancellation finally reports, which is how an old job's success, failure
    or cancel report can be placed exactly after a New, an Open or a close.
    """

    #: the bin names this in its "queue is full" message
    capacity = 4

    def __init__(self, owner: MediaBinController) -> None:
        self.owner = owner
        self.submissions: list[list[Path]] = []
        self.jobs: dict[str, FakeJob] = {}
        self.delivered: list[tuple[str, str]] = []
        self.cancel_all_calls = 0
        self.live: list[str] = []
        #: when set, ``cancel_all`` answers every live job with this asset *inside*
        #: the call, which is what the real controller does for a queued job: its
        #: completion is emitted synchronously, on the cancelling thread
        self.report_on_cancel: Asset | None = None

    # -- what the bin calls ---------------------------------------------------
    def import_paths(self, paths) -> ImportSubmission:
        requested = list(paths)
        self.submissions.append(requested)
        accepted = tuple(FakeJob(path) for path in requested)
        for job in accepted:
            self.jobs[job.id] = job
            self.live.append(job.id)
        return ImportSubmission(accepted=accepted, refused=())

    def cancel_all(self) -> None:
        self.cancel_all_calls += 1
        if self.report_on_cancel is not None:
            for job_id in list(self.live):
                self.deliver(job_id, asset=self.report_on_cancel)

    def pending_count(self) -> int:
        return len(self.live)

    # -- what the test calls --------------------------------------------------
    def ids(self) -> tuple[str, ...]:
        """The job ids handed out so far, oldest first."""
        return tuple(job.id for job in self.jobs.values())

    def outcomes_of(self, job_id: str) -> list[str]:
        return [outcome for delivered, outcome in self.delivered if delivered == job_id]

    def deliver(
        self, job_id: str, *, asset: Asset | None = None, error: str | None = None
    ) -> None:
        """Reports one job's outcome through the bin's own worker callback."""
        job = self.jobs[job_id]
        if job_id in self.live:
            self.live.remove(job_id)
        outcome = "cancelled" if error == CANCELLED_MESSAGE else ("asset" if asset else "error")
        self.delivered.append((job_id, outcome))
        self.owner._worker_finished(job, asset, error)


class PromptLog(list):
    """The prompts a window tried to show. Nothing here should show any."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []


@pytest.fixture()
def prompts(monkeypatch) -> PromptLog:
    """Record any prompt instead of showing it, so nothing can block this run."""
    log = PromptLog()

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
        log.append(title)
        log.texts.append(f"{text} {detail}")
        return Answer(buttons[0][0])

    monkeypatch.setattr(MainWindow, "_message_box", record)
    return log


@pytest.fixture()
def armed(qapp_instance, tmp_path, monkeypatch):
    """A window whose bin talks to the fake import controller, plus its messages."""
    monkeypatch.setenv("VIDEO_EDITOR_PREVIEW_DIR", str(tmp_path / "previews"))
    monkeypatch.setenv("VIDEO_EDITOR_RECOVERY_DIR", str(tmp_path / "recovery"))
    monkeypatch.setenv("VIDEO_EDITOR_AUTOSAVE_SECONDS", "0")
    win = MainWindow()
    win.show()
    qapp_instance.processEvents()
    fake = FakeImportController(owner=win.bin_controller)
    win.bin_controller.importer = fake
    messages: list[str] = []
    win.bin_controller.status_message.connect(messages.append)
    try:
        yield win, tmp_path, fake, messages
    finally:
        win.close()


def write_project(tmp_path: Path, name: str, assets: tuple[Asset, ...]) -> Path:
    """A real project file naming *assets*, written as the application writes one."""
    window = MainWindow()
    try:
        window.install_document(EditorDocument(project=Project(), assets=assets))
        return window._write_project(tmp_path / name)
    finally:
        window._shutdown_preview()
        window.close()


def asset_ids(win: MainWindow) -> tuple[str, ...]:
    return tuple(asset.id for asset in win.session.document.assets)


# ------------------------------------------------------------------ New project
def test_a_late_success_from_a_replaced_project_adds_nothing(armed, prompts):
    """An old probe answers with a good asset after New: the new project is untouched.

    The order is written down, not waited for: submit, replace the session, then
    answer the old job with a complete success.
    """
    win, tmp, fake, messages = armed
    start = win.bin_controller.session_epoch
    film = media_file(tmp, "old-film.mkv")
    win.bin_controller.request_import([film])
    (old_job,) = fake.ids()
    assert win.bin_controller.session_epoch == start, "asking for an import changed the session"

    win._new_project()
    assert prompts == [], prompts.texts
    assert win.bin_controller.session_epoch == start + 1, "New did not open a new session"
    assert fake.cancel_all_calls == 1, "New did not cancel the previous session's probes"

    # the old worker answers as if nothing had happened
    fake.deliver(old_job, asset=media_record(film, "old-asset"))
    assert asset_ids(win) == (), "a replaced project's import published into this one"
    assert win.media_bin.topLevelItemCount() == 0
    assert history_depth(win.session) == 0, "a late import grew the new session's history"
    assert not win.session.is_dirty, "a late import dirtied a project that never saw it"
    assert not any("Imported" in message for message in messages), messages


def test_a_session_change_cancels_only_after_the_epoch_has_moved(armed, prompts):
    """The cancellation of a session change answers *inside* that session change.

    This is the ordering test: the real controller finishes a queued job in
    ``cancel()``, on the thread that asked, so a completion can be emitted while
    New or Open is still running. If the epoch had not already moved, this asset
    would land in the project that never asked for it.
    """
    win, tmp, fake, messages = armed
    film = media_file(tmp, "queued.mkv")
    win.bin_controller.request_import([film])
    (old_job,) = fake.ids()
    fake.report_on_cancel = media_record(film, "queued-record")

    win._new_project()
    assert prompts == [], prompts.texts
    assert fake.cancel_all_calls == 1
    assert fake.outcomes_of(old_job) == ["asset"], "the cancellation did not answer inside itself"
    assert asset_ids(win) == (), "a job of the replaced project published during the session change"
    assert win.media_bin.topLevelItemCount() == 0
    assert history_depth(win.session) == 0
    assert not win.session.is_dirty
    assert not any("Imported" in message for message in messages), messages


def test_an_open_over_a_populated_project_cancels_only_after_the_epoch_has_moved(armed, prompts):
    """The same ordering with a *populated* target: the adopted catalogue stays as saved."""
    win, tmp, fake, _messages = armed
    keeper = media_file(tmp, "keeper.mkv")
    adopted = write_project(tmp, "adopted.vep", (media_record(keeper, "keeper"),))
    film = media_file(tmp, "queued.mkv")
    win.bin_controller.request_import([film])
    (old_job,) = fake.ids()
    fake.report_on_cancel = media_record(film, "queued-record")

    assert win._open_project(adopted) is True
    assert prompts == [], prompts.texts
    assert fake.outcomes_of(old_job) == ["asset"], "the cancellation did not answer inside itself"
    assert asset_ids(win) == ("keeper",), "the adopted project gained a stranger's media"
    assert history_depth(win.session) == 0
    assert not win.session.is_dirty


def test_a_late_failure_and_cancel_report_from_a_replaced_project_say_nothing(armed, prompts):
    """The old session's bad news is dropped too: no message, no state, no history."""
    win, tmp, fake, messages = armed
    film = media_file(tmp, "old-film.mkv")
    win.bin_controller.request_import([film])
    (old_job,) = fake.ids()

    win._new_project()
    assert prompts == [], prompts.texts

    fake.deliver(old_job, asset=None, error="ffprobe failed: truncated answer")
    assert not any("Import failed" in message for message in messages), messages
    assert "ffprobe failed: truncated answer" not in " ".join(messages), messages

    # and the cancellation the session change itself asked for reports nothing either
    fake.deliver(old_job, asset=None, error=CANCELLED_MESSAGE)
    assert not any("Import cancelled" in message for message in messages), messages
    assert asset_ids(win) == ()
    assert history_depth(win.session) == 0
    assert not win.session.is_dirty


def test_a_reimport_of_the_same_path_survives_the_old_jobs_reports(armed, prompts):
    """Same file, new session, then the old job answers — twice.

    Sequence: probe ``film`` (job A) → New (A's placeholder goes with its session)
    → probe ``film`` again (job B, placeholder owned by B) → A answers with a
    success, then with a cancellation. Neither may clear B's placeholder nor
    publish B's file early; B then completes normally.
    """
    win, tmp, fake, messages = armed
    film = media_file(tmp, "film.mkv")
    win.bin_controller.request_import([film])
    (old_job,) = fake.ids()

    win._new_project()
    assert prompts == [], prompts.texts
    win.bin_controller.request_import([film])
    assert fake.submissions == [[film], [film]], fake.submissions
    new_job = fake.ids()[-1]

    fake.deliver(old_job, asset=media_record(film, "stale-record"))
    assert asset_ids(win) == (), "the old session's record landed in the new project"
    fake.deliver(old_job, asset=None, error=CANCELLED_MESSAGE)

    # the new session's placeholder is still standing: asking again is a duplicate,
    # not a third probe
    messages.clear()
    win.bin_controller.request_import([film])
    assert fake.submissions == [[film], [film]], "an old job released the new session's placeholder"
    assert any("Already imported" in message for message in messages), messages

    fake.deliver(new_job, asset=media_record(film, "fresh-record"))
    assert asset_ids(win) == ("fresh-record",)
    assert win.media_bin.topLevelItemCount() == 1
    assert history_depth(win.session) == 1
    assert win.session.is_dirty


def test_cancelling_the_old_session_cannot_touch_the_new_session_job(armed, prompts):
    """A is cancelled by the session change; its report cannot reach B in any form."""
    win, tmp, fake, messages = armed
    old = media_file(tmp, "old.mkv")
    fresh = media_file(tmp, "fresh.mkv")
    win.bin_controller.request_import([old])
    (old_job,) = fake.ids()

    win._new_project()
    assert prompts == [], prompts.texts
    win.bin_controller.request_import([fresh])
    new_job = fake.ids()[-1]

    # the old job's cancellation is reported after the new import was submitted
    fake.deliver(old_job, asset=None, error=CANCELLED_MESSAGE)
    assert fake.pending_count() == 1, "an old cancellation removed the new job from the queue"
    assert asset_ids(win) == ()
    assert not any("Import cancelled" in message for message in messages), messages

    fake.deliver(new_job, asset=media_record(fresh, "fresh-record"))
    assert asset_ids(win) == ("fresh-record",)
    assert win.session.is_dirty

    # and every later report for that old id — cancelled, then a success — cannot
    # remove what the new session has already published
    fake.deliver(old_job, asset=None, error=CANCELLED_MESSAGE)
    fake.deliver(old_job, asset=media_record(old, "stale-record"))
    assert asset_ids(win) == ("fresh-record",)
    assert history_depth(win.session) == 1


# ----------------------------------------------------------------- Open project
def test_a_late_import_after_opening_a_populated_project_adds_nothing(armed, prompts):
    """The target project is *not* empty: an adopted file with one asset of its own.

    The old session's in-flight probe answers after Open. The adopted catalogue
    must stay exactly as the file said, the adopted session must not become dirty,
    and the old file must be importable again in the new session — because its
    placeholder went with its session. That import then lands once, and undo puts
    the adopted catalogue back.
    """
    win, tmp, fake, messages = armed
    keeper = media_file(tmp, "keeper.mkv")
    project_file = write_project(tmp, "adopted.vep", (media_record(keeper, "keeper"),))

    straggler = media_file(tmp, "straggler.mkv")
    win.bin_controller.request_import([straggler])
    (old_job,) = fake.ids()

    assert win._open_project(project_file) is True
    assert prompts == [], prompts.texts
    adopted = win.session.document.assets
    assert asset_ids(win) == ("keeper",)
    assert not win.session.is_dirty

    fake.deliver(old_job, asset=media_record(straggler, "stale-record"))
    assert asset_ids(win) == ("keeper",), "the replaced project's import joined the adopted one"
    assert history_depth(win.session) == 0
    assert not win.session.is_dirty, "a dropped import dirtied the adopted document"
    assert not any("stale" in message or "Imported" in message for message in messages), messages

    # the same file, asked for again in the new session, is a fresh job
    win.bin_controller.request_import([straggler])
    assert len(fake.submissions) == 2, fake.submissions
    new_job = fake.ids()[-1]
    fake.deliver(new_job, asset=media_record(straggler, "adopted-second"))
    assert asset_ids(win) == ("keeper", "adopted-second")
    assert win.session.is_dirty

    win._undo()
    assert win.session.document.assets == adopted
    assert win.media_bin.topLevelItemCount() == 1
    assert not win.session.is_dirty


# ----------------------------------------------------------------------- close
def test_closing_the_window_ends_the_session_so_a_late_import_publishes_nothing(armed):
    """Close bumps the epoch as well, and doing it twice must not bump it twice."""
    win, tmp, fake, _messages = armed
    start = win.bin_controller.session_epoch
    film = media_file(tmp, "closing.mkv")
    win.bin_controller.request_import([film])
    (old_job,) = fake.ids()

    win._shutdown_preview()
    assert win.bin_controller.session_epoch == start + 1, "close did not end the session"
    assert fake.cancel_all_calls == 1
    win._shutdown_preview()
    assert win.bin_controller.session_epoch == start + 1, "the second close was not a no-op"
    assert fake.cancel_all_calls == 1

    fake.deliver(old_job, asset=media_record(film, "late-record"))
    assert asset_ids(win) == ()
    assert history_depth(win.session) == 0
    assert not win.session.is_dirty


def test_the_epoch_moves_at_new_open_and_close_and_nowhere_else(armed, prompts):
    """Where the counter moves — and that asking for an import does not move it."""
    win, tmp, fake, _messages = armed
    start = win.bin_controller.session_epoch

    win.bin_controller.request_import([media_file(tmp, "one.mkv")])
    assert win.bin_controller.session_epoch == start

    win._new_project()
    assert win.bin_controller.session_epoch == start + 1

    keeper = media_file(tmp, "keeper.mkv")
    adopted = write_project(tmp, "adopted.vep", (media_record(keeper, "keeper"),))
    assert win._open_project(adopted) is True
    assert win.bin_controller.session_epoch == start + 2

    win._shutdown_preview()
    assert win.bin_controller.session_epoch == start + 3
    assert prompts == [], prompts.texts
    assert fake.cancel_all_calls == 3, "a session change left the previous session's jobs running"


# ------------------------------------------------- the valid job, exactly once
def test_the_current_job_completes_exactly_once_and_undo_restores_an_empty_catalogue(armed):
    win, tmp, fake, messages = armed
    film = media_file(tmp, "film.mkv")
    win.bin_controller.request_import([film])
    (job,) = fake.ids()

    fake.deliver(job, asset=media_record(film, "film-record"))
    assert asset_ids(win) == ("film-record",)
    assert history_depth(win.session) == 1
    assert win.session.is_dirty
    assert sum(1 for message in messages if "Imported film.mkv" in message) == 1

    # a second report for the same id is no longer this session's business
    fake.deliver(job, asset=media_record(film, "film-record"))
    assert asset_ids(win) == ("film-record",)
    assert history_depth(win.session) == 1
    assert sum(1 for message in messages if "Imported film.mkv" in message) == 1

    win._undo()
    assert win.session.document.assets == ()
    assert win.media_bin.topLevelItemCount() == 0
    assert not win.session.is_dirty
    # undo also frees the file for import again in this same session
    win.bin_controller.request_import([film])
    assert fake.submissions[-1] == [film]


def test_the_current_job_completes_exactly_once_in_a_populated_project(armed):
    """The same guarantee when the project already holds media, and undo keeps it."""
    win, tmp, fake, _messages = armed
    keeper = media_file(tmp, "keeper.mkv")
    keep = media_record(keeper, "keeper")
    win.install_document(EditorDocument(project=Project(), assets=(keep,)))
    assert asset_ids(win) == ("keeper",)
    assert history_depth(win.session) == 0

    second = media_file(tmp, "second.mkv")
    win.bin_controller.request_import([second])
    (job,) = fake.ids()
    fake.deliver(job, asset=media_record(second, "second-record"))
    assert asset_ids(win) == ("keeper", "second-record")
    assert history_depth(win.session) == 1

    fake.deliver(job, asset=media_record(second, "second-record"))
    assert asset_ids(win) == ("keeper", "second-record")
    assert history_depth(win.session) == 1

    win._undo()
    assert win.session.document.assets == (keep,)
    assert not win.session.is_dirty


# ------------------------------------------------- duplicate and refused imports
def test_a_duplicate_import_changes_no_history_and_no_dirty_mark(armed):
    """A file the project already holds is refused before any job exists."""
    win, tmp, fake, messages = armed
    film = media_file(tmp, "film.mkv")
    win.bin_controller.request_import([film])
    (job,) = fake.ids()
    fake.deliver(job, asset=media_record(film, "film-record"))
    saved = win._write_project(tmp / "saved.vep")
    assert saved == tmp / "saved.vep"
    assert not win.session.is_dirty
    depth = history_depth(win.session)
    assert depth == 1

    win.bin_controller.request_import([film])
    assert any("Already imported" in message for message in messages), messages
    assert len(fake.submissions) == 1, "a duplicate started another probe"
    assert asset_ids(win) == ("film-record",)
    assert history_depth(win.session) == depth
    assert not win.session.is_dirty


def test_an_import_the_document_refuses_changes_no_history_and_no_dirty_mark(armed):
    """The same file under two names: the bin cannot see it, the document can.

    A hard link is a second spelling of one file, so the bin submits it and
    :class:`AddAssetCommand` refuses it — ``same_file`` compares device and inode.
    The transaction is refused whole: no history entry, no dirty mark, and the
    path is left free rather than claimed by an import that never happened.
    """
    win, tmp, fake, _messages = armed
    original = media_file(tmp, "film.mkv")
    twin = tmp / "film-hardlink.mkv"
    os.link(original, twin)
    keep = media_record(original, "film-record")
    win.install_document(EditorDocument(project=Project(), assets=(keep,)))
    assert not win.session.is_dirty
    assert history_depth(win.session) == 0

    win.bin_controller.request_import([twin])
    (job,) = fake.ids()
    assert fake.submissions == [[twin]], fake.submissions

    fake.deliver(job, asset=media_record(twin, "twin-record"))
    assert asset_ids(win) == ("film-record",), "a refused import still reached the catalogue"
    assert history_depth(win.session) == 0, "a refused import grew the history"
    assert not win.session.is_dirty, "a refused import dirtied the document"
    assert "refused" in win.statusBar().currentMessage().lower(), win.statusBar().currentMessage()

    # nothing is left claiming that path: it can be asked for again
    win.bin_controller.request_import([twin])
    assert fake.submissions == [[twin], [twin]], fake.submissions


# ------------------------------------------------------ nothing of it is saved
def test_a_saved_project_file_holds_no_import_job_or_placeholder_state(armed):
    """A probe in flight must leave the written document exactly as it was.

    The job id and the file being probed are both searched for by name in the
    built document *and* in the file actually written, and the file's top-level
    shape must not change. Then the same job lands and the next save does name
    that file — so the first absence is about the job, not about the media.
    """
    win, tmp, fake, _messages = armed
    keeper = media_file(tmp, "keeper.mkv")
    win.install_document(
        EditorDocument(project=Project(), assets=(media_record(keeper, "keeper"),))
    )

    baseline = tmp / "quiet.vep"
    assert win._write_project(baseline) == baseline

    probing = media_file(tmp, "still-probing.mkv")
    win.bin_controller.request_import([probing])
    (job,) = fake.ids()

    built = win._project_document()
    assert job not in json.dumps(built), "a job id reached the document being built"
    assert not (_FORBIDDEN_KEYS & document_keys(built)), sorted(document_keys(built))

    in_flight = tmp / "in-flight.vep"
    assert win._write_project(in_flight) == in_flight
    written = json.loads(in_flight.read_text())
    assert job not in json.dumps(written), "the saved project file names an import job"
    assert "still-probing" not in json.dumps(written), "a file only being probed was saved as media"
    assert len(written["assets"]) == 1, written["assets"]
    unchanged_shape = "saving with a job in flight changed the document's shape"
    assert set(written) == set(json.loads(baseline.read_text())), unchanged_shape

    fake.deliver(job, asset=media_record(probing, "probed-record"))
    later = tmp / "after.vep"
    assert win._write_project(later) == later
    after = json.loads(later.read_text())
    assert len(after["assets"]) == 2, "the completed import did not reach the next save"
    assert job not in json.dumps(after), "the saved file kept naming a finished import job"


# ---------------------------------------------------------- the gate itself
def test_an_unknown_or_superseded_job_id_is_inert(qapp_instance, tmp_path):
    """The registry gate without a window: only a job of this epoch can act."""
    controller = MediaBinController()
    fake = FakeImportController(owner=controller)
    controller.importer = fake
    path = media_file(tmp_path, "any.mkv")
    controller.request_import([path])
    (live_job,) = fake.ids()

    # an id this session never issued changes nothing
    assert controller.finalize_finished("never-a-job-id", path, None, "boom") == (None, None, None)
    # the session moves while the job is live: its later completion is inert too
    controller.reset()
    assert controller.finalize_finished(live_job, path, media_record(path, "record"), None) == (
        None,
        None,
        None,
    )

    # a job of the current epoch is honoured, once, and then unknown again
    controller.request_import([path])
    current = fake.ids()[-1]
    assert controller.finalize_finished(current, path, media_record(path, "record"), None) == (
        media_record(path, "record"),
        None,
        None,
    )
    assert controller.finalize_finished(current, path, media_record(path, "record"), None) == (
        None,
        None,
        None,
    )
