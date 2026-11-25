"""The document transaction API (TASK_10): one snapshot, one baseline, one undo.

A project file stores the composition *and* the media it names, so those two
halves must be saved, dirtied and undone together. These tests are pure — no
Qt, no FFmpeg — because the guarantee is a property of the model, not of the
window that happens to display it. The window-level behaviour (bin rows, Save,
prompts) is covered in ``test_project_save_gui.py`` and
``test_ui_source_safety.py``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core_helpers import F, add_clip, make_base
from video_editor.core.commands import (
    AddClipCommand,
    CommandError,
    ImportGroupCommand,
    LinkClipsCommand,
    PlaceStreamsCommand,
    RegisterSourceCommand,
    RemoveClipsCommand,
    StreamPlacement,
    TrackChoice,
    group_placements,
    validate_project,
)
from video_editor.core.history import HistoryError
from video_editor.core.model import Clip, Project, StreamSource, Track, TrackKind
from video_editor.media.assets import Asset
from video_editor.media.relink import RelinkPlan
from video_editor.project.document import (
    AddAssetCommand,
    CompositeDocumentCommand,
    DocumentSession,
    EditComposition,
    EditorDocument,
    RelinkDocumentCommand,
    ReplaceAssetCommand,
    validate_document,
)


def record(asset_id: str, name: str = "film.mkv") -> Asset:
    """A catalogue record. The file need not exist: offline media is a valid state."""
    return Asset(
        id=asset_id, path=Path("/media") / name, size_bytes=4096, mtime_ns=1000,
        metadata=None,
    )


def base_document() -> EditorDocument:
    """make_base()'s composition plus the two media records it references."""
    return EditorDocument(
        project=make_base(), assets=(record("va"), record("aa", "sound.wav"))
    )


def live_session() -> DocumentSession:
    return DocumentSession(base_document())


def place_va(clip_id: str = "placed", start: F | None = None) -> AddClipCommand:
    return AddClipCommand("va", 0, "vt", F(0) if start is None else start,
                          source_in=F(0), source_out=F(10), clip_id=clip_id)


def unregistered_source() -> StreamSource:
    """A stream the project has never heard of: placement must register it."""
    return StreamSource(
        "brand-new", 0, TrackKind.VIDEO, F(0), F(30), frame_rate=F(25), width=128,
        height=96, stream_position=0,
    )


# ------------------------------------------------------------- the catalogue


def test_an_import_is_an_undoable_edit_of_the_document():
    """A file can be in the project without any clip using it yet."""
    live = live_session()
    assert not live.is_dirty
    assert live.apply(AddAssetCommand(record("extra", "unused.mkv")))
    assert live.is_dirty  # the saved document would now differ
    assert len(live.document.assets) == 3
    assert live.project.clips == ()  # nothing was placed
    assert live.snapshot_label == "Document"

    live.undo()
    assert len(live.document.assets) == 2
    assert not live.is_dirty  # back to exactly what a file holds
    live.redo()
    assert len(live.document.assets) == 3
    assert live.is_dirty
    assert live.document.assets[-1].id == "extra"  # stable id survives redo


def test_importing_the_same_file_twice_is_refused(tmp_path):
    """A second name for one file is one asset, not two."""
    film = tmp_path / "film.mkv"
    film.write_bytes(b"picture")
    twin = tmp_path / "twin.mkv"
    twin.hardlink_to(film)
    stat = film.stat()
    live = live_session()
    first = Asset(id="one", path=film, size_bytes=7, mtime_ns=stat.st_mtime_ns, metadata=None)
    assert live.apply(AddAssetCommand(first))

    same_id = Asset(id="one", path=film, size_bytes=7, mtime_ns=stat.st_mtime_ns,
                    metadata=None)
    with pytest.raises(CommandError, match="already in this project"):
        live.apply(AddAssetCommand(same_id))
    alias = Asset(id="two", path=twin, size_bytes=7, mtime_ns=stat.st_mtime_ns, metadata=None)
    with pytest.raises(CommandError, match="already imported"):
        live.apply(AddAssetCommand(alias))

    assert len(live.document.assets) == 3  # the two base records plus 'one'
    assert live.can_undo  # history grew exactly once


def test_a_failed_import_leaves_the_session_untouched():
    """Nothing about a file that could not be read is recorded, and nothing dirties."""
    live = live_session()
    with pytest.raises(CommandError):
        live.apply(AddAssetCommand(record("va")))  # duplicate id
    assert live.document == base_document()
    assert not live.is_dirty
    assert not live.can_undo


# ---------------------------------------------------------------- relinking


def test_a_relink_changes_file_and_stream_records_as_one_edit():
    live = live_session()
    updated = Asset(id="va", path=Path("/media/new-encode.mkv"), size_bytes=99,
                    mtime_ns=2000, metadata=None)
    source = StreamSource("va", 0, TrackKind.VIDEO, F(0), F(120), frame_rate=F(24),
                          width=1920, height=1080, stream_position=0)
    assert live.apply(RelinkDocumentCommand(RelinkPlan(asset=updated, sources=(source,))))

    assert live.document.asset("va").path == Path("/media/new-encode.mkv")
    assert live.document.asset("va").size_bytes == 99
    registered = live.project.source("va", 0)
    assert registered.end == F(120) and registered.frame_rate == F(24)

    live.undo()
    # both halves come back together: the old file *and* the old stream record,
    # which is what the old composition-only undo could not promise
    assert live.document.asset("va").path == Path("/media/film.mkv")
    restored = live.project.source("va", 0)
    assert restored.end == F(100) and restored.frame_rate == F(25)
    assert not live.is_dirty

    live.redo()
    assert live.document.asset("va").size_bytes == 99
    assert live.project.source("va", 0).frame_rate == F(24)


def test_a_path_only_relink_is_dirty_even_though_the_composition_is_unchanged():
    """The distinction the review asked to keep: media moved, composition did not."""
    live = live_session()
    before_project = live.project
    same_streams = RelinkPlan(
        asset=Asset(id="va", path=Path("/media/renamed.mkv"), size_bytes=4096, mtime_ns=1000,
                    metadata=None),
        sources=(live.project.source("va", 0),),
    )
    assert live.apply(RelinkDocumentCommand(same_streams))
    assert live.project == before_project  # composition untouched
    assert live.is_dirty  # but the saved document would name a different file
    assert live.document.asset("va").path == Path("/media/renamed.mkv")
    live.undo()
    assert live.document.asset("va").path == Path("/media/film.mkv")
    assert not live.is_dirty


def test_replacing_an_asset_that_is_not_there_is_refused():
    live = live_session()
    with pytest.raises(CommandError, match="No asset nope"):
        live.apply(ReplaceAssetCommand(record("nope")))


# -------------------------------------------------- the composite placement


def test_placing_a_stream_is_one_action_even_when_it_registers_and_allocates():
    live = live_session()
    # the file has to be in the catalogue first: placing a clip whose media the
    # document does not record would be a clip that could never be rendered
    assert live.apply(AddAssetCommand(record("brand-new", "new.mkv")))
    before = live.project
    command = PlaceStreamsCommand(
        (StreamPlacement(source=unregistered_source(), output_start=F(4)),)
    )
    assert live.apply(command)
    project = live.project
    assert len(project.sources) == len(before.sources) + 1  # registered
    assert len(project.clips) == 1
    clip = project.clips[0]
    assert (clip.asset_id, clip.stream_index) == ("brand-new", 0)
    assert clip.output_start == F(4)
    # the project's own video lane was free at 4 s, so it was reused rather than
    # adding a track per placement (a 2 h film does not need a lane per clip)
    assert project.track(clip.track_id).kind is TrackKind.VIDEO
    assert len(project.tracks) == len(before.tracks)

    live.undo()
    # one undo reverses the whole gesture: no orphan registration, no empty track
    assert live.project == before
    assert len(live.document.assets) == 3  # only the placement was undone
    live.undo()  # the import that made this file known
    assert live.document == base_document()
    assert not live.is_dirty


def test_a_refused_placement_leaves_no_registration_no_track_and_no_history():
    """The candidate is thrown away whole, so a bad drop cannot leave litter."""
    live = live_session()
    assert live.apply(AddAssetCommand(record("brand-new", "new.mkv")))
    assert live.apply(EditComposition(place_va()))
    before = live.project
    occupied = live.project.clips[0].track_id
    blocked = PlaceStreamsCommand(
        (StreamPlacement(source=unregistered_source(), output_start=F(0), track_id=occupied),)
    )
    with pytest.raises(CommandError, match="Overlaps"):
        live.apply(blocked)
    assert live.project == before  # no registration, no track, no clip
    live.undo()  # the last thing in history is the accepted placement, not this one
    assert len(live.project.clips) == 0
    assert len(live.document.assets) == 3
    live.undo()  # …and one more undo for the import: the refusal added nothing
    assert live.document == base_document()
    assert not live.is_dirty


def test_placement_never_substitutes_different_metadata_for_a_known_stream():
    """Stream records are what existing clips' bounds were validated against."""
    live = live_session()
    changed = StreamSource("va", 0, TrackKind.VIDEO, F(0), F(999), frame_rate=F(24),
                           width=128, height=96, stream_position=0)
    with pytest.raises(CommandError, match="different recorded bounds"):
        live.apply(PlaceStreamsCommand((StreamPlacement(source=changed, output_start=F(0)),)))
    assert live.project == base_document().project
    assert not live.can_undo


def test_allocation_reuses_a_free_lane_and_the_fresh_policy_does_not():
    live = live_session()
    live.apply(EditComposition(place_va()))
    first = live.project.clips[0].track_id

    registered = live.project.source("va", 0)
    allocated = live.apply(
        PlaceStreamsCommand((
            StreamPlacement(source=registered, output_start=F(50), source_in=F(0),
                            source_out=F(5)),
        ))
    )
    assert allocated
    assert live.project.clips[1].track_id == first  # the lane was free at 50 s

    fresh = live.apply(
        PlaceStreamsCommand(
            (StreamPlacement(source=registered, output_start=F(50), source_in=F(0),
                             source_out=F(2)),),
            track_choice=TrackChoice.FRESH,
        )
    )
    assert fresh
    assert live.project.clips[2].track_id != first
    assert len(live.project.tracks) == 3


def staggered_session() -> DocumentSession:
    """One file whose picture starts 2 s into the container and whose sound does not."""
    project = Project()
    picture = StreamSource("va", 0, TrackKind.VIDEO, F(2), F(60), frame_rate=F(25),
                           width=128, height=96, stream_position=0, input_start=F(2))
    sound = StreamSource("va", 1, TrackKind.AUDIO, F(0), F(60), sample_rate=48000,
                         stream_position=1, input_start=F(0))
    project = RegisterSourceCommand(picture).apply(project)
    project = RegisterSourceCommand(sound).apply(project)
    return DocumentSession(EditorDocument(project=project, assets=(record("va"),)))


def test_a_group_keeps_the_offsets_the_file_gives_it():
    """Streams of one asset are placed as one action, at their real offsets."""
    live = staggered_session()
    before = live.project
    picture = live.project.source("va", 0)
    sound = live.project.source("va", 1)
    command = PlaceStreamsCommand(
        group_placements((picture, sound), F(10)), track_choice=TrackChoice.FRESH
    )
    assert live.apply(command)
    assert sorted(clip.output_start for clip in live.project.clips) == [F(10), F(12)]
    assert sorted(clip.source_in for clip in live.project.clips) == [F(0), F(2)]
    live.undo()
    assert live.project == before
    assert not live.is_dirty


def test_import_group_command_and_placement_agree_about_group_alignment():
    """One rule for group offsets, in one place (the review asked for reuse)."""
    project = base_document().project.with_tracks(
        (*base_document().project.tracks, Track("v2", TrackKind.VIDEO, "V2"),
         Track("a2", TrackKind.AUDIO, "A2"))
    )
    grouped = ImportGroupCommand("va", (0,), F(7), track_ids=("v2",)).apply(project)
    placed = PlaceStreamsCommand(
        group_placements((project.source("va", 0),), F(7)), track_choice=TrackChoice.FRESH
    ).apply(project)
    assert [c.output_start for c in grouped.clips] == [c.output_start for c in placed.clips]


def test_a_composite_document_transaction_publishes_once_or_not_at_all():
    """The seam a later assisted edit uses: many steps, one undo, no half-states."""
    live = live_session()
    assert live.apply(AddAssetCommand(record("second", "b.mkv")))
    before = live.document

    assisted = CompositeDocumentCommand(
        (
            AddAssetCommand(record("third", "c.mkv")),
            EditComposition(RemoveClipsCommand(("does-not-exist",))),
        ),
        description="assisted pass",
    )
    with pytest.raises(CommandError):
        live.apply(assisted)
    assert live.document == before  # the first step was not published either
    live.undo()
    assert live.document == base_document()  # only the accepted import was in history

    usable = CompositeDocumentCommand(
        (
            AddAssetCommand(record("third", "c.mkv")),
            EditComposition(place_va("v1")),
        ),
        description="import and place",
    )
    assert live.apply(usable)
    assert live.apply(EditComposition(RemoveClipsCommand(("v1",))))
    assert live.can_undo
    live.undo()
    assert live.can_undo  # the composite is still one entry to unwind
    live.undo()
    assert live.document == base_document()  # import + placement reversed together


# ------------------------------------------------------- validation at commit


def test_a_missing_file_is_not_a_structural_problem():
    """Offline media is reported by availability, never as corruption."""
    document = base_document()
    assert not Path("/media/film.mkv").exists()
    assert validate_document(document) == []
    assert validate_project(document.project) == []


def test_a_stream_without_a_media_record_is_refused_at_commit():
    """The catalogue and the composition are one document; both have to agree."""
    live = live_session()
    orphan = RegisterSourceCommand(
        StreamSource("ghost", 0, TrackKind.VIDEO, F(0), F(10), frame_rate=F(25),
                     stream_position=0)
    ).apply(live.project)
    with pytest.raises(CommandError, match="not in the media catalogue"):
        live.apply(EditComposition(RegisterSourceCommand(
            StreamSource("ghost2", 0, TrackKind.VIDEO, F(0), F(10), frame_rate=F(25),
                         stream_position=0)
        )))
    assert validate_document(live.document.with_project(orphan))  # named, not silent
    assert live.project == base_document().project  # the refused edit changed nothing


def test_validation_names_the_break_precisely():
    document = base_document()

    duplicated_asset = document.with_assets((*document.assets, record("va", "again.mkv")))
    assert any("duplicate asset id va" in problem
               for problem in validate_document(duplicated_asset))

    with_duplicate_track = document.project.with_tracks(
        (*document.project.tracks, document.project.tracks[0])
    )
    assert any("duplicate track id vt" in problem
               for problem in validate_document(document.with_project(with_duplicate_track)))

    with_duplicate_source = replace(
        document.project, sources=(*document.project.sources, document.project.sources[0])
    )
    assert any("registered more than once" in problem
               for problem in validate_document(document.with_project(with_duplicate_source)))

    # these fixtures are built directly: a command would refuse them, and the
    # point is that validation finds a break that arrived from elsewhere (a
    # hand-edited or corrupted project file)
    def clip(clip_id: str, start: F, end: F, source_out: F | None = None) -> Clip:
        return Clip(
            id=clip_id, asset_id="va", stream_index=0, track_id="vt", source_in=F(0),
            source_out=F(500) if source_out is None else source_out, output_start=start,
        )

    overlapping = document.project.with_clips(
        (*document.project.clips, clip("x1", F(0), F(5)), clip("x2", F(2), F(7)))
    )
    assert any("overlap" in problem for problem in
               validate_document(document.with_project(overlapping)))

    over_end = document.project.with_clips((*document.project.clips, clip("x3", F(0), F(1))))
    assert any("source_out exceeds stream end" in problem for problem in
               validate_document(document.with_project(over_end)))

    # an empty source interval or a zero rate cannot even be built: Clip refuses
    # them, and a project file carrying one is refused while decoding, so the
    # validator's checks there are a backstop rather than a reachable state


def test_a_single_member_link_group_is_not_sound():
    """A link joins at least two clips: a lone member is a leftover, not a link."""
    project = add_clip(base_document().project, "v1", "vt", F(0), F(10))
    project = add_clip(project, "a1", "at", F(0), F(10), asset="aa", stream=1)
    project = LinkClipsCommand(("v1", "a1")).apply(project)
    document = base_document().with_project(project)
    assert validate_document(document) == []

    partner = next(clip for clip in project.clips if clip.id == "a1")
    half = project.with_clips(tuple(clip for clip in project.clips if clip is not partner))
    problems = validate_document(document.with_project(half))
    assert any("link group" in problem and "only clip" in problem for problem in problems)


def test_history_refuses_a_session_whose_document_moved_outside_it():
    live = live_session()
    live.apply(AddAssetCommand(record("extra", "unused.mkv")))
    live.document = base_document()  # an external replacement, not an edit
    with pytest.raises(HistoryError, match="Document changed outside history"):
        live.undo()


def test_a_document_is_immutable_and_keeps_asset_order():
    """Order is import order: re-sorting it would quietly rank one edition first."""
    document = base_document()
    assets = document.assets
    assert assets[0].id == "va" and assets[1].id == "aa"
    assert document.asset("nope") is None
    with pytest.raises(TypeError):
        document.assets[0] = record("va")  # the snapshot is not a mutable list
    assert isinstance(validate_document(document), list)
    assert isinstance(document.project, Project)
