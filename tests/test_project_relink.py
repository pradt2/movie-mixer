"""Relinking tests: probing is stubbed, the *decisions* are what is measured.

A relink must be explicit, must validate the replacement against the material
the project already uses, must never retime a clip, and must be undoable. FFmpeg
probing itself is covered by the media tests; here the replacement is a
constructed asset record so every case is deterministic.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from video_editor.core.commands import CommandError, RelinkAssetCommand
from video_editor.core.history import ProjectSession
from video_editor.core.model import Clip, Project, StreamSource, Track, TrackKind
from video_editor.media.assets import (
    Asset,
    AssetMetadata,
    StreamKind,
    StreamMetadata,
)
from video_editor.media.relink import RelinkError, relink_asset

F = Fraction


def metadata(
    video: tuple[Fraction, Fraction] | None = None,
    audio: tuple[Fraction, Fraction] | None = None,
    *,
    size: tuple[int, int] = (720, 480),
    frame_rate: Fraction | None = None,
    sample_rate: int | None = None,
    container_start: Fraction | None = None,
) -> AssetMetadata:
    """Probe metadata for a two-stream file: picture 0, sound 1, 25 fps, 48 kHz."""
    video = video if video is not None else (F(0), F(10))
    audio = audio if audio is not None else (F(0), F(10))
    frame_rate = frame_rate if frame_rate is not None else F(25)
    sample_rate = sample_rate if sample_rate is not None else 48000
    container_start = container_start if container_start is not None else F(0)
    streams: list[StreamMetadata] = []
    if video is not None:
        streams.append(
            StreamMetadata(
                stream_index=0, kind=StreamKind.VIDEO, codec="h264", language=None,
                frame_rate=frame_rate, nominal_frame_rate=frame_rate,
                width=size[0], height=size[1], sample_rate=None, channels=None,
                channel_layout=None, start_time=F(0), duration=video[1] - video[0],
                time_base=F(1, 25), nb_frames=None,
            )
        )
    if audio is not None:
        streams.append(
            StreamMetadata(
                stream_index=1, kind=StreamKind.AUDIO, codec="aac", language="eng",
                frame_rate=None, nominal_frame_rate=None, width=None, height=None,
                sample_rate=sample_rate, channels=2, channel_layout="stereo",
                start_time=F(0), duration=audio[1] - audio[0], time_base=F(1, 48000),
                nb_frames=None,
            )
        )
    end = max((s.start_time + (s.duration or F(0))) for s in streams) if streams else None
    return AssetMetadata(
        format_name="matroska", container_duration=end, container_start=container_start,
        streams=tuple(streams),
    )


def an_asset(path, asset_id: str = "A", *, size_bytes: int = 1000, mtime_ns: int = 1,
             **probe) -> Asset:
    return Asset(
        id=asset_id, path=path, size_bytes=size_bytes, mtime_ns=mtime_ns,
        metadata=metadata(**probe), probe_error=None, label="Edition A",
    )


def a_project() -> Project:
    sources = (
        StreamSource("A", 0, TrackKind.VIDEO, origin=F(0), end=F(10), frame_rate=F(25),
                     width=720, height=480, input_start=F(0), stream_position=0),
        StreamSource("A", 1, TrackKind.AUDIO, origin=F(0), end=F(10), sample_rate=48000,
                     channel_layout="stereo", input_start=F(0), stream_position=0),
    )
    clips = (
        Clip("v1", "A", 0, "vid", source_in=F(1), source_out=F(9), output_start=F(0)),
        Clip("a1", "A", 1, "aud", source_in=F(1), source_out=F(9), output_start=F(0)),
    )
    return Project(
        sources=sources,
        tracks=(Track("vid", TrackKind.VIDEO, "Picture"), Track("aud", TrackKind.AUDIO, "Sound")),
        clips=clips,
    )


# ------------------------------------------------------------------- validation


def test_a_compatible_replacement_keeps_the_asset_id_and_clip_timing(tmp_path):
    project = a_project()
    original = an_asset(tmp_path / "a.mkv")
    replacement = an_asset(tmp_path / "a-copy.mkv", size_bytes=999, mtime_ns=2)
    plan = relink_asset(
        original, tmp_path / "a-copy.mkv", project.sources, project.clips,
        replacement=replacement,
    )
    assert plan.asset.id == original.id  # stable id survives: clips still point here
    assert plan.asset.path == tmp_path / "a-copy.mkv"
    assert plan.asset.display_name() == "Edition A"
    assert plan.notes == ()
    by_index = {source.stream_index: source for source in plan.sources}
    assert by_index[0].end == F(10) and by_index[1].end == F(10)
    # the project's clips are untouched by validation
    assert project.clips == a_project().clips


def test_a_missing_stream_index_is_refused_with_the_alternatives(tmp_path):
    project = a_project()
    original = an_asset(tmp_path / "a.mkv")
    # the replacement has audio where the project has picture
    swapped = Asset(
        id="A", path=tmp_path / "b.mkv", size_bytes=1, mtime_ns=1,
        metadata=AssetMetadata(
            format_name="matroska", container_duration=F(10), container_start=F(0),
            streams=(
                StreamMetadata(
                    stream_index=0, kind=StreamKind.AUDIO, codec="aac", language=None,
                    frame_rate=None, nominal_frame_rate=None, width=None, height=None,
                    sample_rate=48000, channels=2, channel_layout="stereo",
                    start_time=F(0), duration=F(10), time_base=F(1, 48000), nb_frames=None,
                ),
            ),
        ),
        probe_error=None, label="",
    )
    with pytest.raises(RelinkError) as exc:
        relink_asset(original, tmp_path / "b.mkv", project.sources, project.clips,
                     replacement=swapped)
    message = str(exc.value)
    assert "audio stream there" in message
    assert "0: audio" in message  # what the replacement actually has


def test_material_the_replacement_cannot_supply_is_refused(tmp_path):
    project = a_project()
    original = an_asset(tmp_path / "a.mkv")
    shorter = an_asset(tmp_path / "short.mkv", video=(F(0), F(5)))
    with pytest.raises(RelinkError) as exc:
        relink_asset(original, tmp_path / "short.mkv", project.sources, project.clips,
                     replacement=shorter)
    message = str(exc.value)
    assert "clip v1 uses source 1-9s" in message
    assert "its stream covers 0-5s" in message
    assert "not adjusted automatically" in message


def test_a_different_rate_is_refused_as_a_different_timing_base(tmp_path):
    project = a_project()
    original = an_asset(tmp_path / "a.mkv")
    PAL = an_asset(tmp_path / "film-pal.mkv", frame_rate=F(24))
    with pytest.raises(RelinkError) as exc:
        relink_asset(original, tmp_path / "film-pal.mkv", project.sources, project.clips,
                     replacement=PAL)
    assert "frame rate 24 in the replacement, 25 in this project" in str(exc.value)


def test_a_different_picture_size_needs_an_explicit_acceptance(tmp_path):
    project = a_project()
    original = an_asset(tmp_path / "a.mkv")
    hd = an_asset(tmp_path / "hd.mkv", size=(1920, 1080))
    with pytest.raises(RelinkError) as exc:
        relink_asset(original, tmp_path / "hd.mkv", project.sources, project.clips,
                     replacement=hd)
    assert "picture size 1920x1080" in str(exc.value)
    plan = relink_asset(
        original, tmp_path / "hd.mkv", project.sources, project.clips,
        replacement=hd, allow_geometry_change=True,
    )
    assert plan.sources and {s.stream_index for s in plan.sources} == {0, 1}


def test_renumbered_streams_are_reported_and_used_for_addressing(tmp_path):
    """A replacement whose stream order differs must not be addressed wrongly."""
    from dataclasses import replace as dc_replace

    recorded = tuple(
        dc_replace(source, stream_position=1) for source in a_project().sources
    )
    project = Project(
        sources=recorded, tracks=a_project().tracks, clips=a_project().clips
    )
    original = an_asset(tmp_path / "a.mkv")
    reordered = Asset(
        id="A", path=tmp_path / "reordered.mkv", size_bytes=1, mtime_ns=1,
        metadata=AssetMetadata(
            format_name="matroska", container_duration=F(10), container_start=F(0),
            streams=(
                StreamMetadata(
                    stream_index=1, kind=StreamKind.AUDIO, codec="aac", language=None,
                    frame_rate=None, nominal_frame_rate=None, width=None, height=None,
                    sample_rate=48000, channels=2, channel_layout="stereo",
                    start_time=F(0), duration=F(10), time_base=None, nb_frames=None,
                ),
                StreamMetadata(
                    stream_index=0, kind=StreamKind.VIDEO, codec="h264", language=None,
                    frame_rate=F(25), nominal_frame_rate=F(25), width=720, height=480,
                    sample_rate=None, channels=None, channel_layout=None,
                    start_time=F(0), duration=F(10), time_base=None, nb_frames=None,
                ),
            ),
        ),
        probe_error=None, label="",
    )
    plan = relink_asset(original, tmp_path / "reordered.mkv", project.sources,
                        project.clips, replacement=reordered)
    assert any("stream number 0" in note and "audio" in note for note in plan.notes)
    audio = next(s for s in plan.sources if s.kind is TrackKind.AUDIO)
    assert audio.stream_position == 0  # rendering addresses it by the new position


def test_an_unprobeable_replacement_is_refused_not_assumed(tmp_path):
    project = a_project()
    original = an_asset(tmp_path / "a.mkv")
    broken = Asset(
        id="A", path=tmp_path / "broken.mkv", size_bytes=1, mtime_ns=1, metadata=None,
        probe_error="ffprobe: invalid data found", label="",
    )
    with pytest.raises(RelinkError) as exc:
        relink_asset(original, tmp_path / "broken.mkv", project.sources, project.clips,
                     replacement=broken)
    assert "could not be probed" in str(exc.value)


def test_a_container_offset_is_reported_without_changing_source_times(tmp_path):
    project = a_project()
    original = an_asset(tmp_path / "a.mkv")
    offset = an_asset(tmp_path / "offset.mkv", container_start=F(2, 5))
    plan = relink_asset(original, tmp_path / "offset.mkv", project.sources, project.clips,
                        replacement=offset)
    assert any("presentation timeline starts at 2/5s" in note for note in plan.notes)
    assert all(source.input_start == F(2, 5) for source in plan.sources)
    assert all(source.origin == F(0) for source in plan.sources)


# ------------------------------------------------------- the composition update


def test_relink_updates_the_project_and_undoes_cleanly(tmp_path):
    session = ProjectSession(a_project())
    original = an_asset(tmp_path / "a.mkv")
    replacement = an_asset(tmp_path / "b.mkv", video=(F(0), F(12)), audio=(F(0), F(12)))
    plan = relink_asset(original, tmp_path / "b.mkv", session.project.sources,
                        session.project.clips, replacement=replacement)
    before = session.project
    assert session.apply(RelinkAssetCommand("A", plan.sources))
    assert session.project.source("A", 0).end == F(12)
    assert session.project.clips == before.clips  # timing untouched
    assert session.is_dirty
    session.undo()
    assert session.project == before
    assert not session.is_dirty


def test_the_command_refuses_a_partial_or_impossible_relink():
    project = a_project()
    half = (project.sources[0],)
    with pytest.raises(CommandError) as exc:
        RelinkAssetCommand("A", half).apply(project)
    assert "every registered stream" in str(exc.value)

    from dataclasses import replace as dc_replace

    too_short = tuple(dc_replace(source, end=F(3)) for source in project.sources)
    with pytest.raises(CommandError) as exc:
        RelinkAssetCommand("A", too_short).apply(project)
    assert "clip v1 uses source 1-9s" in str(exc.value)

    unknown = tuple(dc_replace(source, asset_id="Z") for source in project.sources)
    with pytest.raises(CommandError):
        RelinkAssetCommand("A", unknown).apply(project)
    with pytest.raises(CommandError):
        RelinkAssetCommand("nope", ()).apply(project)
