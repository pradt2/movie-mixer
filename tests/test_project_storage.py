"""Project-file tests: schema, exactness, atomicity, media state, recovery.

Fixtures are synthetic (never user films). The rules under test are the ones
that make a long editing session safe: what is saved must come back exactly,
a bad file must not damage the current work, and media that moved or changed
must be reported rather than quietly substituted.
"""

from __future__ import annotations

import json
import os
from fractions import Fraction
from pathlib import Path

import pytest

from video_editor.core.model import (
    Clip,
    OutputSettings,
    Project,
    StreamSource,
    Track,
    TrackKind,
)
from video_editor.fs.protection import Consent, DestinationGuard
from video_editor.media.assets import (
    Asset,
    AssetMetadata,
    StreamKind,
    StreamMetadata,
)
from video_editor.media.availability import protected_files
from video_editor.project import recovery, serialize, storage

F = Fraction

# --------------------------------------------------------------------- fixtures


def stream(index: int, kind: StreamKind, **overrides) -> StreamMetadata:
    base = {
        "stream_index": index,
        "kind": kind,
        "codec": "h264" if kind is StreamKind.VIDEO else "aac",
        "language": "eng" if kind is StreamKind.AUDIO else None,
        "frame_rate": F(24000, 1001) if kind is StreamKind.VIDEO else None,
        "nominal_frame_rate": F(24) if kind is StreamKind.VIDEO else None,
        "width": 720 if kind is StreamKind.VIDEO else None,
        "height": 480 if kind is StreamKind.VIDEO else None,
        "sample_rate": None if kind is StreamKind.VIDEO else 48000,
        "channels": None if kind is StreamKind.VIDEO else 2,
        "channel_layout": None if kind is StreamKind.VIDEO else "stereo",
        "start_time": F(1, 2),
        "duration": F(12),
        "time_base": F(1, 1000),
        "nb_frames": None,
    }
    return StreamMetadata(**{**base, **overrides})


def asset(path: Path, asset_id: str = "assetA", **overrides) -> Asset:
    """An asset record whose identity matches *path* as it is on disk right now.

    Passing ``size_bytes``/``mtime_ns`` explicitly manufactures an identity
    mismatch, which is how "the file changed under me" is tested.
    """
    stat = path.stat() if path.is_file() else None
    base = {
        "id": asset_id,
        "path": path,
        "size_bytes": stat.st_size if stat else 4096,
        "mtime_ns": stat.st_mtime_ns if stat else 1_700_000_000_000_000_000,
        "metadata": AssetMetadata(
            format_name="matroska,webm",
            container_duration=F(13, 2),
            container_start=F(1, 2),
            streams=(stream(0, StreamKind.VIDEO), stream(1, StreamKind.AUDIO)),
        ),
        "probe_error": None,
        "label": "",
    }
    return Asset(**{**base, **overrides})


def mixed_project() -> Project:
    """Two video tracks (top first), two audio, odd fractions, a link group."""
    output = OutputSettings(
        width=1920, height=1080, frame_rate=F(24000, 1001), sample_rate=48000,
        channel_layout="stereo",
    )
    sources = (
        StreamSource("assetA", 0, TrackKind.VIDEO, origin=F(1, 2), end=F(12),
                     frame_rate=F(24000, 1001), width=720, height=480,
                     input_start=F(1, 2), stream_position=0),
        StreamSource("assetA", 1, TrackKind.AUDIO, origin=F(1, 2), end=F(12),
                     sample_rate=48000, channel_layout="stereo",
                     input_start=F(1, 2), stream_position=0, conservative_end=True),
        StreamSource("assetB", 0, TrackKind.AUDIO, origin=F(0), end=None,
                     sample_rate=44100, input_start=F(0), stream_position=0),
    )
    tracks = (
        Track("vtop", TrackKind.VIDEO, "Edition B picture"),
        Track("vbot", TrackKind.VIDEO, "Edition A picture", enabled=True, muted=False),
        Track("a1", TrackKind.AUDIO, "Dub"),
        Track("a2", TrackKind.AUDIO, "Original", muted=True),
    )
    clips = (
        Clip("c1", "assetA", 0, "vbot", source_in=F(1, 2), source_out=F(10, 3),
             output_start=F(0), rate=F(1001, 1000), gain=F(1),
             fade_in=F(1, 3), fade_out=F(2, 7), link_group="g1"),
        Clip("c2", "assetA", 1, "a1", source_in=F(1, 2), source_out=F(4),
             output_start=F(0), link_group="g1"),
        Clip("c3", "assetB", 0, "a2", source_in=F(0), source_out=F(2),
             output_start=F(7, 11)),
        Clip("c4", "assetA", 0, "vtop", source_in=F(5), source_out=F(6),
             output_start=F(3, 5), gain=F(3, 4)),
    )
    return Project(output=output, sources=sources, tracks=tracks, clips=clips)


def write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ------------------------------------------------------------------- encoding


def test_round_trip_preserves_every_domain_value(tmp_path):
    media = tmp_path / "media.mkv"
    media.write_bytes(b"x" * 10)
    project = mixed_project()
    document = storage.build_document(
        project, [asset(media)], serialize.Navigation(), "0.0-test", tmp_path / "p.vep"
    )
    text = json.dumps(document)
    loaded = storage.decode_payload(json.loads(text), base_dir=tmp_path)

    assert loaded.project == project
    assert loaded.project.output.frame_rate == F(24000, 1001)
    clip = loaded.project.clip("c1")
    assert clip.source_in == F(1, 2) and clip.source_out == F(10, 3)
    assert clip.rate == F(1001, 1000) and clip.fade_in == F(1, 3)
    assert clip.link_group == "g1"
    assert [track.name for track in loaded.project.tracks] == [
        "Edition B picture", "Edition A picture", "Dub", "Original"
    ]  # stacking order preserved, top first
    assert loaded.project.track("a2").muted
    assert loaded.project.source("assetB", 0).end is None  # unknown stays unknown
    assert loaded.project.source("assetA", 1).conservative_end


def test_fractions_are_stored_as_exact_strings(tmp_path):
    media = tmp_path / "media.mkv"
    media.touch()
    document = storage.build_document(
        mixed_project(), [asset(media)], serialize.Navigation(), "0.0-test",
        tmp_path / "p.vep",
    )
    raw = json.dumps(document)
    assert "24000/1001" in raw and "0.999" not in raw
    payload = json.loads(raw)
    assert payload["output"]["frame_rate"] == "24000/1001"
    assert payload["clips"][0]["rate"] == "1001/1000"
    assert payload["schema_version"] == serialize.SCHEMA_VERSION
    assert not any(len(str(value)) > 40 for value in payload["clips"][0].values())


def test_navigation_is_optional_and_clamped(tmp_path):
    media = tmp_path / "media.mkv"
    media.touch()
    document = storage.build_document(
        mixed_project(), [asset(media)],
        serialize.Navigation(playhead=F(10 ** 6), zoom=2.5, scroll_x=F(1),
                             selected_clips=("c1", "c9")),
        "0.0-test", tmp_path / "p.vep",
    )
    loaded = storage.decode_payload(document, base_dir=tmp_path)
    assert loaded.navigation.zoom == 2.5
    assert loaded.navigation.playhead == mixed_project().output_end()  # clamped
    assert loaded.navigation.selected_clips == ("c1", "c9")

    del document["navigation"]
    bare = storage.decode_payload(document, base_dir=tmp_path)
    assert bare.navigation == serialize.Navigation()


def test_unknown_navigation_field_is_refused():
    project = mixed_project()
    body = serialize.encode_project(project)
    body["navigation"] = {"playhead": "0/1", "sidebar_width": 120}
    with pytest.raises(serialize.ProjectFileError) as exc:
        serialize.decode_project(body)
    assert "sidebar_width" in str(exc.value) or "navigation" in str(exc.value)


# ------------------------------------------------------------------- path rules


def test_relative_references_are_used_only_inside_the_project_directory(tmp_path):
    project_dir = tmp_path / "film"
    media = project_dir / "sub" / "media.mkv"
    media.parent.mkdir(parents=True)
    media.touch()
    expected = ("relative", os.path.join("sub", "media.mkv"))
    assert storage.relative_reference(media, project_dir) == expected

    outside = tmp_path / "elsewhere.mkv"
    outside.touch()
    mode, reference = storage.relative_reference(outside, project_dir)
    assert mode == "absolute" and Path(reference) == outside


def test_relative_paths_follow_a_moved_project_and_resolve_against_it(tmp_path):
    original = tmp_path / "a"
    original.mkdir()
    media = original / "media.mkv"
    media.write_bytes(b"x" * 8)
    project_file = original / "p.vep"
    document = storage.build_document(
        mixed_project(), [asset(media)], serialize.Navigation(), "0.0-test", project_file
    )
    storage.save_document(document, project_file)
    assert document["assets"][0]["path_mode"] == "relative"

    moved = tmp_path / "b"
    moved.mkdir()
    project_file.rename(moved / "p.vep")
    media.rename(moved / "media.mkv")

    loaded = storage.load_document(moved / "p.vep")
    record = loaded.asset("assetA")
    assert record.resolved_path == (moved / "media.mkv").resolve()
    assert record.state is storage.AssetState.ONLINE


def test_asset_paths_never_resolve_next_to_the_project_when_absolute(tmp_path):
    elsewhere = tmp_path / "disk2" / "media.mkv"
    elsewhere.parent.mkdir()
    elsewhere.write_bytes(b"x" * 5)
    here = tmp_path / "here"
    here.mkdir()
    document = storage.build_document(
        mixed_project(), [asset(elsewhere)], serialize.Navigation(), "0.0-test",
        here / "p.vep",
    )
    assert document["assets"][0]["path_mode"] == "absolute"
    loaded = storage.decode_payload(document, base_dir=here)
    assert loaded.asset("assetA").resolved_path == elsewhere


# ------------------------------------------------------- media state detection


def test_missing_asset_is_reported_not_repaired(tmp_path):
    gone = tmp_path / "gone.mkv"
    document = storage.build_document(
        mixed_project(), [asset(gone)], serialize.Navigation(), "0.0-test",
        tmp_path / "p.vep",
    )
    loaded = storage.decode_payload(document, base_dir=tmp_path)
    record = loaded.asset("assetA")
    assert record.state is storage.AssetState.MISSING
    assert "is not there" in record.note
    # the composition and its recorded metadata survive: offline inspection works
    assert loaded.project.clips and record.asset.metadata.streams
    assert loaded.unavailable() == (record,)


def test_changed_asset_identity_is_detected(tmp_path):
    media = tmp_path / "media.mkv"
    media.write_bytes(b"x" * 10)
    other = tmp_path / "other.wav"
    other.write_bytes(b"y" * 64)
    recorded = asset(media)  # identity of the file as it was when saved
    companion = asset(
        other,
        asset_id="assetB",
        metadata=AssetMetadata(
            format_name="wav", container_duration=F(4), container_start=None,
            streams=(stream(0, StreamKind.AUDIO, start_time=F(0), duration=F(4)),),
        ),
    )
    document = storage.build_document(
        mixed_project(), [recorded, companion],
        serialize.Navigation(), "0.0-test", tmp_path / "p.vep",
    )
    document["assets"][0]["mtime_ns"] = 1_700_000_000_000_000_000  # an old stat
    loaded = storage.decode_payload(document, base_dir=tmp_path)
    record = loaded.asset("assetA")
    assert record.state is storage.AssetState.CHANGED
    assert "not the file recorded here" in record.note
    # recorded metadata is kept as the baseline: nothing is re-probed or adopted
    assert record.asset.size_bytes == 10
    assert record.asset.metadata == recorded.metadata
    assert loaded.warnings == ()  # same metadata, so nothing more can be claimed


def test_probe_failure_record_is_preserved(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"nope")
    failed = asset(broken, metadata=None, probe_error="ffprobe: not a valid file",
                   size_bytes=4, mtime_ns=broken.stat().st_mtime_ns)
    document = storage.build_document(
        mixed_project(), [failed], serialize.Navigation(), "0.0-test", tmp_path / "p.vep"
    )
    loaded = storage.decode_payload(document, base_dir=tmp_path)
    record = loaded.asset("assetA")
    assert record.state is storage.AssetState.ONLINE  # the file is where it said it was
    assert record.asset.metadata is None
    assert record.asset.probe_error == "ffprobe: not a valid file"


def test_material_that_no_longer_fits_is_warned_about(tmp_path):
    """Recorded metadata can disagree with the saved clips: report, never retime."""
    media = tmp_path / "media.mkv"
    media.touch()
    other = tmp_path / "other.wav"
    other.touch()
    shorter = asset(
        media,
        metadata=AssetMetadata(
            format_name="matroska", container_duration=F(1), container_start=F(1, 2),
            streams=(stream(0, StreamKind.VIDEO, duration=F(1)),
                     stream(1, StreamKind.AUDIO, duration=F(1))),
        ),
    )
    full = asset(
        other,
        asset_id="assetB",
        metadata=AssetMetadata(
            format_name="wav", container_duration=F(4), container_start=None,
            streams=(stream(0, StreamKind.AUDIO, sample_rate=44100, channels=2,
                            channel_layout="stereo", start_time=None, duration=F(4)),),
        ),
    )
    document = storage.build_document(
        mixed_project(), [shorter, full], serialize.Navigation(), "0.0-test",
        tmp_path / "p.vep",
    )
    loaded = storage.decode_payload(document, base_dir=tmp_path)
    assert any("clip c1 uses" in note for note in loaded.warnings)
    assert any("clip c4 uses" in note for note in loaded.warnings)
    assert loaded.project == mixed_project()  # timing never adjusted


# ---------------------------------------------------------------- schema rules


def test_future_schema_version_is_refused_with_diagnostics():
    payload = json.loads(json.dumps({
        "schema_version": serialize.SCHEMA_VERSION + 3,
        "output": {}, "assets": [], "sources": [], "tracks": [], "clips": [],
        "quantum_links": [{"why": "new feature"}],
    }))
    with pytest.raises(serialize.ProjectFileError) as exc:
        storage.decode_payload(payload, base_dir=None)
    message = str(exc.value)
    assert f"schema version {serialize.SCHEMA_VERSION + 3}" in message
    assert "quantum_links" in message and "not modified" in message


def test_unknown_fields_in_a_known_version_are_refused():
    project = mixed_project()
    media = Path("media.mkv")
    document = storage.build_document(
        project, [asset(media)], serialize.Navigation(), "0.0-test", Path("p.vep")
    )
    document["clips"][0]["retimed_by"] = "0/1"
    with pytest.raises(serialize.ProjectFileError) as exc:
        storage.decode_payload(document, base_dir=None)
    assert "retimed_by" in str(exc.value)


def test_older_version_without_a_migration_is_refused():
    document = {"schema_version": 0, "output": {}, "assets": [], "sources": [],
                "tracks": [], "clips": []}
    with pytest.raises(serialize.ProjectFileError) as exc:
        storage.decode_payload(document, base_dir=None)
    assert "no migration" in str(exc.value)


@pytest.mark.parametrize(
    "raw",
    ["", "not json at all", "[]", "{}", '{"schema_version": "one"}', '{"tracks": [}'],
)
def test_corrupt_files_are_reported_clearly(tmp_path, raw):
    path = tmp_path / "p.vep"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(storage.ProjectFileError) as exc:
        storage.load_document(path)
    assert str(exc.value)


def test_missing_required_fields_are_named():
    document = {"schema_version": 1, "output": {"width": 1}, "assets": [],
                "sources": [], "tracks": [], "clips": []}
    with pytest.raises(serialize.ProjectFileError) as exc:
        storage.decode_payload(document, base_dir=None)
    assert "output.height" in str(exc.value) or "height" in str(exc.value)


def test_decimal_time_is_rejected_as_inexact():
    body = serialize.encode_project(mixed_project())
    body["clips"][0]["output_start"] = "1.5"
    with pytest.raises(serialize.ProjectFileError) as exc:
        serialize.decode_project(body)
    assert "exact rational" in str(exc.value)


def test_domain_invariants_are_validated_on_load():
    project = mixed_project()
    body = serialize.encode_project(project)
    # two clips overlapping on the same track
    body["clips"][3]["track_id"] = "vbot"
    body["clips"][3]["output_start"] = "1/10"
    body["clips"][3]["source_in"] = "5/1"
    body["clips"][3]["source_out"] = "6/1"
    body["assets"] = []
    document = {"schema_version": 1, "assets": [], **body}
    with pytest.raises(storage.ProjectFileError) as exc:
        storage.decode_payload(document, base_dir=None)
    assert "invalid composition" in str(exc.value)
    assert "overlap" in str(exc.value)


def test_duplicate_ids_in_one_file_are_refused():
    body = serialize.encode_project(mixed_project())
    body["assets"] = []
    document = {"schema_version": 1, "assets": [], **body}
    document["tracks"].append(document["tracks"][0])
    with pytest.raises(serialize.ProjectFileError) as exc:
        storage.decode_payload(document, base_dir=None)
    assert "duplicated" in str(exc.value)


def test_asset_record_is_decoded_back_field_by_field(tmp_path):
    media = tmp_path / "media.mkv"
    media.touch()
    original = asset(media)
    encoded = json.dumps(json.loads(json.dumps(
        storage.build_document(mixed_project(), [original], serialize.Navigation(),
                               "0.0-test", tmp_path / "p.vep")
    )))
    loaded = storage.decode_payload(json.loads(encoded), base_dir=tmp_path)
    record = loaded.asset("assetA")
    assert record.asset.id == original.id
    assert record.asset.size_bytes == original.size_bytes
    assert record.asset.mtime_ns == original.mtime_ns
    metadata = record.asset.metadata
    assert metadata.format_name == "matroska,webm"
    assert metadata.container_duration == F(13, 2)
    first = metadata.stream(0)
    assert first.frame_rate == F(24000, 1001) and first.nominal_frame_rate == F(24)
    assert first.time_base == F(1, 1000) and first.start_time == F(1, 2)
    assert metadata.stream(1).language == "eng"


# ---------------------------------------------------------------- atomic files


def test_save_then_load_round_trip_through_disk(tmp_path):
    media = tmp_path / "media.mkv"
    media.write_bytes(b"x" * 12)
    project_file = tmp_path / "film.vep"
    document = storage.build_document(
        mixed_project(), [asset(media)],
        serialize.Navigation(playhead=F(3, 5), zoom=4.0), "0.0-test", project_file,
    )
    storage.save_document(document, project_file)
    assert list(tmp_path.glob(".*tmp*")) == []  # no staging debris
    reloaded = storage.load_document(project_file)
    assert reloaded.project == mixed_project()
    assert reloaded.navigation.playhead == F(3, 5)
    assert reloaded.schema_version == serialize.SCHEMA_VERSION


def test_save_failure_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    project_file = tmp_path / "film.vep"
    storage.save_document(
        storage.build_document(mixed_project(), [], serialize.Navigation(), "0.0-test",
                               project_file),
        project_file,
    )
    before = project_file.read_text(encoding="utf-8")

    def explode(src, dst):  # crash between writing the temp file and renaming it
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage.os, "replace", explode)
    with pytest.raises(storage.ProjectFileError) as exc:
        storage.save_document(
            storage.build_document(mixed_project(), [], serialize.Navigation(playhead=F(9)),
                                   "0.0-test", project_file),
            project_file,
            # replacing an existing file needs an approval tied to that file
            consent=Consent.capture(project_file),
        )
    assert "No space left on device" in str(exc.value)
    assert project_file.read_text(encoding="utf-8") == before
    assert list(tmp_path.glob(".*tmp*")) == []


def test_save_into_a_directory_or_missing_directory_reports_clearly(tmp_path):
    a_directory = tmp_path / "sub"
    a_directory.mkdir()
    with pytest.raises(storage.ProjectFileError):
        storage.save_document({"schema_version": 1}, a_directory)
    with pytest.raises(storage.ProjectFileError) as exc:
        storage.save_document({"schema_version": 1}, tmp_path / "nope" / "p.vep")
    assert "not a directory" in str(exc.value)
    assert list(tmp_path.rglob(".*tmp*")) == []


def test_save_replaces_an_existing_file_without_temporary_debris(tmp_path):
    project_file = tmp_path / "film.vep"
    for playhead in (F(0), F(5), F(11, 5)):
        storage.save_document(
            storage.build_document(mixed_project(), [], serialize.Navigation(playhead),
                                   "0.0-test", project_file),
            project_file,
            # each replacement is approved against the file standing there at that
            # moment, which is what saving one's own project file means
            consent=Consent.capture(project_file),
        )
    assert storage.load_document(project_file).navigation.playhead == F(11, 5)
    assert [path.name for path in tmp_path.iterdir()] == ["film.vep"]


def test_saving_over_an_asset_is_refused_and_changes_nothing(tmp_path):
    """Save As must never be a way to destroy a film.

    A project file written over media is data loss; no confirmation in the world
    authorises it, so this is a hard refusal rather than a prompt.
    """
    media = tmp_path / "film.mkv"
    media.write_bytes(b"precious picture bytes" * 40)
    original = media.read_bytes()
    document = storage.build_document(
        mixed_project(), [asset(media)], serialize.Navigation(), "0.0-test", media
    )
    guard = DestinationGuard(protected_files([asset(media)]))
    with pytest.raises(storage.ProjectFileError) as exc:
        storage.save_document(document, media, protected=guard)
    assert "imported media" in str(exc.value)
    assert media.read_bytes() == original
    assert list(tmp_path.glob(".*tmp*")) == []  # refused before staging anything


def test_saving_onto_a_symlink_or_hardlink_of_an_asset_is_refused(tmp_path):
    media = tmp_path / "film.mkv"
    media.write_bytes(b"one and only copy")
    original = media.read_bytes()
    link = tmp_path / "alias.mkv"
    link.symlink_to(media)
    hard = tmp_path / "same-inode.mkv"
    os.link(media, hard)
    guard = DestinationGuard(protected_files([asset(media)]))
    document = storage.build_document(
        mixed_project(), [asset(media)], serialize.Navigation(), "0.0-test",
        tmp_path / "p.vep",
    )
    for alias in (link, hard):
        with pytest.raises(storage.ProjectFileError, match="same file"):
            storage.save_document(document, alias, protected=guard)
    assert media.read_bytes() == original
    assert link.resolve() == media


def test_saving_never_clobbers_a_file_that_appeared_in_the_meantime(tmp_path):
    """Nothing existed at approval time, so publication must not overwrite."""
    project_file = tmp_path / "film.vep"
    document = storage.build_document(
        mixed_project(), [], serialize.Navigation(), "0.0-test", project_file
    )
    # approval given when there was no file, exactly as Save As would do it
    consent = Consent.capture(project_file)
    assert not consent.existed
    project_file.write_text("someone else's document", encoding="utf-8")
    with pytest.raises(storage.ProjectFileError, match="appeared"):
        storage.save_document(document, project_file, consent=consent)
    assert project_file.read_text(encoding="utf-8") == "someone else's document"
    assert list(tmp_path.glob(".*tmp*")) == []


def test_load_reports_a_directory_and_a_binary_file(tmp_path):
    directory = tmp_path / "folder.vep"
    directory.mkdir()
    with pytest.raises(storage.ProjectFileError) as exc:
        storage.load_document(directory)
    assert "directory" in str(exc.value)

    binary = tmp_path / "media.vep"
    binary.write_bytes(b"\xff\xfe\x00\x01\x00\x02")
    with pytest.raises(storage.ProjectFileError) as exc:
        storage.load_document(binary)
    assert "not a text project file" in str(exc.value)

    with pytest.raises(storage.ProjectFileError):
        storage.load_document(tmp_path / "nothing.vep")


# -------------------------------------------------------------------- recovery


@pytest.fixture()
def recovery_env(tmp_path, monkeypatch):
    root = tmp_path / "recovery-root"
    monkeypatch.setenv("VIDEO_EDITOR_RECOVERY_DIR", str(root))
    return root


def test_recovery_root_prefers_the_environment(recovery_env):
    assert recovery.recovery_root() == recovery_env
    assert recovery.session_key(None) == "unsaved"
    assert recovery.session_key(Path("/x/p.vep")) != recovery.session_key(Path("/y/p.vep"))


def test_snapshots_are_listed_newest_first_and_pruned(tmp_path, recovery_env):
    store = recovery.RecoveryStore("unsaved", root=recovery_env, keep=3)
    document = storage.build_document(
        mixed_project(), [], serialize.Navigation(playhead=F(2)), "0.0-test", None
    )
    for index in range(5):
        store.write(document, reason=f"autosave {index}")
    snapshots = store.list()
    assert len(snapshots) == 3
    assert [snap.meta.reason for snap in snapshots] == ["autosave 4", "autosave 3", "autosave 2"]
    loaded = store.load_latest()
    assert loaded is not None
    restored, snapshot = loaded
    assert restored.project == mixed_project()
    assert restored.navigation.playhead == F(2)
    assert "autosave 4" in recovery.describe_snapshot(snapshot)


def test_a_corrupt_snapshot_is_skipped_without_losing_the_good_one(tmp_path, recovery_env):
    store = recovery.RecoveryStore("unsaved", root=recovery_env)
    document = storage.build_document(mixed_project(), [], serialize.Navigation(), "0.0-test", None)
    good = store.write(document, reason="good")
    bad = store.write(document, reason="bad")
    bad.write_text("{ this is not json", encoding="utf-8")
    snapshots = store.list()
    assert [snap.meta.reason for snap in snapshots] == ["good"]
    loaded = store.load_latest()
    assert loaded is not None
    _document, snapshot = loaded
    assert snapshot.path == good
    assert good.is_file()


def test_snapshot_never_touches_files_it_does_not_own(recovery_env):
    store = recovery.RecoveryStore("unsaved", root=recovery_env)
    keeper = recovery_env / "unsaved" / "keep-me.txt"
    keeper.parent.mkdir(parents=True)
    keeper.write_text("user data", encoding="utf-8")
    store.write(storage.build_document(mixed_project(), [], serialize.Navigation(),
                                       "0.0-test", None), reason="x")
    assert store.discard() == 1
    assert keeper.is_file() and keeper.read_text(encoding="utf-8") == "user data"


def test_restore_is_refused_when_the_disk_file_is_newer(tmp_path, recovery_env):
    project_file = tmp_path / "p.vep"
    project_file.write_text("{}", encoding="utf-8")
    store = recovery.RecoveryStore(recovery.session_key(project_file), root=recovery_env)
    snapshot_path = store.write(
        storage.build_document(mixed_project(), [], serialize.Navigation(), "0.0-test",
                               project_file),
        reason="autosave",
        project_path=project_file,
        project_mtime_ns=project_file.stat().st_mtime_ns,
    )
    snapshot = store.list()[0]
    assert recovery.disk_ahead_of_snapshot(snapshot) is None

    project_file.write_text('{"schema_version": 1}', encoding="utf-8")
    reason = recovery.disk_ahead_of_snapshot(snapshot)
    assert reason and "written after this snapshot" in reason
    assert snapshot_path.is_file()


def test_snapshot_relative_media_follow_the_project_file(tmp_path, recovery_env):
    media = tmp_path / "media.mkv"
    media.write_bytes(b"x" * 6)
    store = recovery.RecoveryStore("unsaved", root=recovery_env)
    document = storage.build_document(
        mixed_project(), [asset(media)], serialize.Navigation(), "0.0-test",
        tmp_path / "p.vep",
    )
    store.write(document, reason="autosave", project_path=tmp_path / "p.vep")
    loaded = store.load_latest()
    assert loaded is not None
    record = loaded[0].asset("assetA")
    assert record.state is storage.AssetState.ONLINE
    assert record.resolved_path == media.resolve()
