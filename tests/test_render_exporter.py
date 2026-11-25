"""Exporter unit tests: argv selection, chunking, guards (no FFmpeg needed)."""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from core_helpers import add_clip, make_base
from video_editor.core.commands import AddClipCommand
from video_editor.core.model import Track, TrackKind
from video_editor.fs.identity import Identity
from video_editor.fs.protection import DestinationGuard, ProtectedFile
from video_editor.render.exporter import (
    RenderCancelled,
    RenderError,
    RenderRequest,
    _chunk_ranges,
    estimate_intermediate_bytes,
    probe_duration,
    render,
)
from video_editor.render.graph import LOSSLESS_INTERMEDIATE, MP4_H264_AAC
from video_editor.render.plan import build_plan

F = Fraction
ZERO = F(0)
def real_paths(tmp_path) -> dict[str, Path]:
    """Placeholder files: the exporter only checks that inputs exist."""
    out = {}
    for asset_id, name in (("va", "video.mkv"), ("aa", "audio.wav")):
        path = tmp_path / name
        path.write_bytes(b"placeholder input, never read by these tests")
        out[asset_id] = path
    return out


def request_for(project, destination, **kwargs) -> RenderRequest:
    return RenderRequest(
        project=project,
        asset_paths=dict(kwargs.pop("asset_paths", {})),
        destination=destination,
        ffmpeg=kwargs.pop("ffmpeg", "/usr/bin/ffmpeg"),
        ffprobe=kwargs.pop("ffprobe", "/usr/bin/ffprobe"),
        **kwargs,
    )


# ------------------------------------------------------------------- planning


def test_long_timeline_plans_without_rendering(tmp_path):
    """A two-hour composition plans exactly (no film required)."""
    from video_editor.core.commands import RegisterSourceCommand
    from video_editor.core.model import Project, StreamSource

    hours = F(7200)
    p = Project()
    p = RegisterSourceCommand(
        StreamSource("va", 0, TrackKind.VIDEO, ZERO, hours, frame_rate=F(25), stream_position=0)
    ).apply(p)
    p = RegisterSourceCommand(
        StreamSource("aa", 1, TrackKind.AUDIO, ZERO, hours, sample_rate=48000,
                     stream_position=0)
    ).apply(p)
    p = p.with_tracks((Track("vt", TrackKind.VIDEO, "V1"), Track("at", TrackKind.AUDIO, "A1")))
    p = add_clip(p, "v1", "vt", ZERO, hours)
    plan = build_plan(p, ZERO, hours)
    assert plan.total_frames == 7200 * 25
    assert plan.total_samples == 7200 * 48000
    assert plan.duration == hours


def test_chunk_ranges_cover_the_plan_exactly_once(tmp_path):
    p = make_base()
    for index in range(30):
        p = add_clip(p, f"v{index}", "vt", F(index * 2, 10), F(2, 10))  # 5 frames each
    plan = build_plan(p, ZERO, F(6))
    pieces = _chunk_ranges(plan, 4)
    assert len(pieces) > 1
    total_frames = 0
    total_samples = 0
    previous_end = plan.output_start
    for start, end in pieces:
        assert start == previous_end, "chunks must be contiguous"
        piece = build_plan(p, start, end)
        total_frames += piece.total_frames
        total_samples += piece.total_samples
        previous_end = end
    assert pieces[-1][1] == plan.output_end
    assert total_frames == plan.total_frames
    assert total_samples == plan.total_samples


def test_chunk_ranges_replan_identical_boundaries(tmp_path):
    """Replanning a chunk from the same snapshot gives the same quantisation."""
    p = make_base()
    for index in range(12):
        p = add_clip(p, f"a{index}", "at", F(index, 2), F(1, 2), asset="aa", stream=1)
    plan = build_plan(p, ZERO, F(6))
    pieces = _chunk_ranges(plan, 3)
    for start, end in pieces:
        piece = build_plan(p, start, end)
        assert piece.quantisation.frame_rate == plan.quantisation.frame_rate
        assert piece.quantisation.sample_rate == plan.quantisation.sample_rate
        assert piece.channel_layout == plan.channel_layout


def test_single_range_when_under_the_limit(tmp_path):
    plan = build_plan(add_clip(make_base(), "v1", "vt", ZERO, F(4)), ZERO, F(4))
    assert _chunk_ranges(plan, 96) == [(plan.output_start, plan.output_end)]


def test_audio_heavy_compositions_are_chunked_by_total_items(tmp_path):
    """Many audio contributions must not defeat the per-chunk bound."""
    p = make_base()
    for index in range(24):
        p = add_clip(p, f"a{index}", "at", F(index, 4), F(1, 4), asset="aa", stream=1)
    plan = build_plan(p, ZERO, F(6))
    assert len(plan.audio_items) == 24
    pieces = _chunk_ranges(plan, 4)
    assert len(pieces) >= 5
    per_chunk = []
    for start, end in pieces:
        piece = build_plan(p, start, end)
        per_chunk.append(len(piece.video_items) + len(piece.audio_items))
        assert len(piece.video_items) <= 4 and len(piece.audio_items) <= 4, per_chunk
    assert sum(build_plan(p, s, e).total_samples for s, e in pieces) == plan.total_samples


def test_render_inputs_are_bounded_per_graph(tmp_path):
    """One input per distinct (asset, stream, seek): bounded, and shared where
    the same material is re-read, so decoders cannot grow without limit."""
    from video_editor.core.commands import AddClipCommand
    from video_editor.render.graph import MP4_H264_AAC, build_job
    from video_editor.render.plan import build_plan as plan_of

    paths = {"va": Path("/media/video.mkv"), "aa": Path("/media/audio.wav")}

    def job_for(project, end):
        plan = plan_of(project, ZERO, end)
        sources = {(s.asset_id, s.stream_index): s for s in project.sources}
        return plan, build_job(plan, paths, sources, Path("/tmp/out.mp4"), "ffmpeg",
                               MP4_H264_AAC)

    p = make_base()
    for index in range(10):
        p = add_clip(p, f"v{index}", "vt", F(index, 2), F(1, 2))
    plan, job = job_for(p, F(5))
    # items re-reading the same material share one seeked input
    distinct = {(i.clip.asset_id, i.clip.stream_index, i.source_start)
                for i in plan.video_items if not i.is_black}
    assert 1 <= len(distinct) < len(plan.video_items)
    assert job.args.count("-i") == len(distinct) <= len(plan.video_items)

    # different source positions need separate, separately seeked inputs
    q = make_base()
    for index in range(10):
        q = AddClipCommand("va", 0, "vt", F(index), source_in=F(index + 1),
                           source_out=F(index + 2), clip_id=f"v{index}").apply(q)
    plan_q, job_q = job_for(q, F(10))
    assert job_q.args.count("-i") == len(plan_q.video_items) == 10


# --------------------------------------------------------------------- guards


@pytest.fixture()
def tools_ok(monkeypatch):
    """Encoder probing succeeds; the tests below are about the other guards."""
    monkeypatch.setattr("video_editor.render.exporter.encoder_available", lambda *a: True)


def test_missing_ffmpeg_is_reported_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    request = RenderRequest(
        project=add_clip(make_base(), "v1", "vt", ZERO, F(1)),
        asset_paths=real_paths(tmp_path),
        destination=tmp_path / "out.mp4",
        ffmpeg=None,
        ffprobe=None,
    )
    with pytest.raises(RenderError, match="ffmpeg is not available"):
        request.resolve_tools()


def test_missing_encoder_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr("video_editor.render.exporter.encoder_available", lambda *a: False)
    request = request_for(
        add_clip(make_base(), "v1", "vt", ZERO, F(1)),
        tmp_path / "out.mp4",
        asset_paths=real_paths(tmp_path),
    )
    with pytest.raises(RenderError, match="cannot encode"):
        request.resolve_tools()


def test_existing_destination_is_refused(tmp_path, tools_ok):
    destination = tmp_path / "out.mp4"
    destination.write_bytes(b"precious")
    project = add_clip(make_base(), "v1", "vt", ZERO, F(1))
    request = request_for(project, destination, asset_paths=real_paths(tmp_path))
    with pytest.raises(RenderError, match="already exists"):
        request.resolve_tools()
    assert destination.read_bytes() == b"precious"
    # explicit consent releases it
    assert request_for(
        project, destination, overwrite=True, asset_paths=real_paths(tmp_path)
    ).resolve_tools()


def test_missing_asset_path_is_reported(tmp_path, tools_ok):
    request = request_for(
        add_clip(make_base(), "v1", "vt", ZERO, F(1)),
        tmp_path / "out.mp4",
        asset_paths=real_paths(tmp_path),
    )
    request = replace(request, asset_paths={})  # asset not relinked
    # sources are checked against the material the range actually reads, which is
    # decided from the plan, so the check is its own step rather than part of
    # locating the tools
    with pytest.raises(RenderError, match="no file is recorded"):
        request.check_sources({"va"})


def test_unusable_source_names_its_status(tmp_path, tools_ok):
    """MISSING and CHANGED are distinguished, and both refuse the render."""
    paths = real_paths(tmp_path)
    project = add_clip(make_base(), "v1", "vt", ZERO, F(1))
    missing = request_for(project, tmp_path / "out.mp4", asset_paths=paths)
    missing = replace(missing, asset_paths={"va": tmp_path / "moved-away.mkv"})
    with pytest.raises(RenderError, match="is not there"):
        missing.check_sources({"va"})

    changed = request_for(project, tmp_path / "out.mp4", asset_paths=paths)
    changed = replace(
        changed, asset_identities={"va": Identity(size_bytes=99, mtime_ns=1)}
    )
    with pytest.raises(RenderError, match="not the file recorded"):
        changed.check_sources({"va"})


def test_a_range_that_does_not_use_an_offline_asset_still_renders(tmp_path, tools_ok):
    """Scoping is honest: only the material this range reads has to be present."""
    project = add_clip(make_base(), "v1", "vt", ZERO, F(1))
    paths = real_paths(tmp_path)
    paths["aa"] = tmp_path / "gone.mkv"  # audio asset, unused by this video-only range
    request = request_for(project, tmp_path / "out.mp4", asset_paths=paths)
    used = request.assets_used([request.plan()])
    assert used == {"va"}
    request.check_sources(used)  # 'aa' is offline and unread: nothing to refuse


def test_missing_output_directory_is_reported(tmp_path, tools_ok):
    request = request_for(
        add_clip(make_base(), "v1", "vt", ZERO, F(1)),
        tmp_path / "nope" / "out.mp4",
        asset_paths=real_paths(tmp_path),
    )
    with pytest.raises(RenderError, match="does not exist"):
        request.resolve_tools()


def test_empty_range_is_refused(tmp_path, tools_ok):
    from video_editor.render.exporter import render

    request = request_for(
        add_clip(make_base(), "v1", "vt", ZERO, F(4)),
        tmp_path / "out.mp4",
        out_start=F(2),
        out_end=F(2),
        asset_paths=real_paths(tmp_path),
    )
    with pytest.raises(RenderError, match="Nothing to render"):
        render(request)


# ------------------------------------------------------------- temp space math


def test_intermediate_estimate_scales_with_frames_and_geometry():
    plan = build_plan(add_clip(make_base(), "v1", "vt", ZERO, F(4)), ZERO, F(4))
    small = estimate_intermediate_bytes(plan)
    wide = estimate_intermediate_bytes(
        replace(plan, width=plan.width * 2, height=plan.height * 2)
    )
    assert wide > small * 3
    longer = estimate_intermediate_bytes(
        build_plan(add_clip(make_base(), "v1", "vt", ZERO, F(8)), ZERO, F(8))
    )
    assert longer > small


def test_chunked_render_refuses_when_the_disk_is_full(tmp_path, monkeypatch):
    class Usage:
        free = 10 * 1024 * 1024  # 10 MiB

    monkeypatch.setattr("shutil.disk_usage", lambda path: Usage())
    request = request_for(
        add_clip(make_base(), "v1", "vt", ZERO, F(60)),
        tmp_path / "out.mp4",
        asset_paths=real_paths(tmp_path),
    )
    with pytest.raises(RenderError, match="temporary space"):
        request.check_temp_space(request.plan())


def test_chunked_render_passes_with_room_to_spare(tmp_path, monkeypatch):
    class Usage:
        free = 200 * 1024**3

    monkeypatch.setattr("shutil.disk_usage", lambda path: Usage())
    request = request_for(
        add_clip(make_base(), "v1", "vt", ZERO, F(4)),
        tmp_path / "out.mp4",
        asset_paths=real_paths(tmp_path),
    )
    request.check_temp_space(request.plan())


# --------------------------------------------------------------- probe parsing


def test_probe_duration_parses_exactly(tmp_path, monkeypatch):
    payload = json.dumps({"format": {"duration": "123.456000"}})
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, payload, "")
    )
    assert probe_duration("ffprobe", tmp_path / "x.mp4") == F(123456, 1000)


def test_probe_duration_reports_unusable_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, '{"format": {}}', ""),
    )
    with pytest.raises(RenderError, match="did not report a duration"):
        probe_duration("ffprobe", tmp_path / "x.mp4")


def test_probe_duration_reports_probe_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "bad file")
    )
    with pytest.raises(RenderError, match="cannot probe"):
        probe_duration("ffprobe", tmp_path / "x.mp4")


# ------------------------------------------------------------- job properties


def test_staging_file_uses_the_preset_extension(tmp_path):
    request = request_for(add_clip(make_base(), "v1", "vt", ZERO, F(1)), tmp_path / "movie.mkv",
                          preset=LOSSLESS_INTERMEDIATE)
    assert request.preset.extension == "mkv"
    assert request.preset is LOSSLESS_INTERMEDIATE
    assert MP4_H264_AAC.extension == "mp4"


def test_render_range_defaults_to_the_whole_composition(tmp_path):
    plan = request_for(make_base(), tmp_path / "out.mp4", asset_paths=real_paths(tmp_path)).plan()
    assert plan.duration == ZERO and plan.total_frames == 0


def test_tracks_do_not_change_output_duration_when_disabled(tmp_path):
    project = add_clip(make_base(), "v1", "vt", ZERO, F(4))
    project = project.with_tracks(
        tuple(
            Track(t.id, t.kind, t.name, enabled=(t.id != "vt")) for t in project.tracks
        )
    )
    request = request_for(project, tmp_path / "out.mp4")
    full = build_plan(add_clip(make_base(), "v1", "vt", ZERO, F(4)), ZERO, None)
    assert request.plan().duration == full.duration


def test_audio_only_composition_still_plans_video_black(tmp_path):
    project = make_base()
    project = AddClipCommand("aa", 1, "at", ZERO, source_in=ZERO, source_out=F(3),
                             clip_id="a1").apply(project)
    plan = build_plan(project, ZERO, None)
    assert plan.duration == F(3)
    assert all(item.is_black for item in plan.video_items)
    assert len(plan.audio_items) == 1


# ------------------------------------------------- destination and source safety


def guard_for(tmp_paths) -> DestinationGuard:
    """The guard the UI would build from these media files."""
    entries = [
        ProtectedFile.of(path, f"imported media {path.name}") for path in tmp_paths
    ]
    return DestinationGuard(entries)


def fake_ffmpeg(monkeypatch, during=None, produced=None):
    """Replace the FFmpeg run and the verification probe.

    ``during`` stands for what happens between starting the render and finishing
    it — the window in which a file can appear, change or be cancelled.
    """

    def run(job, on_progress=None, cancel_event=None):
        if on_progress is not None:
            on_progress(0.5)
        if during is not None:
            during(job, cancel_event)

    monkeypatch.setattr("video_editor.render.exporter._run", run)
    monkeypatch.setattr(
        "video_editor.render.exporter.probe_duration",
        lambda ffprobe, path: F(1) if produced is None else produced,
    )


def test_exporting_onto_an_asset_is_refused_even_with_overwrite(tmp_path, tools_ok):
    """Confirming a replacement is never consent to destroy a source."""
    paths = real_paths(tmp_path)
    film = paths["va"]
    original = film.read_bytes()
    project = add_clip(make_base(), "v1", "vt", ZERO, F(1))
    request = request_for(
        project, film, overwrite=True, asset_paths=paths, guard=guard_for(paths.values())
    )
    with pytest.raises(RenderError, match="imported media"):
        request.resolve_tools()
    with pytest.raises(RenderError, match="same file"):
        request.check_destination()
    assert film.read_bytes() == original  # bytes untouched, and unchanged by refusal
    assert list(tmp_path.glob("*.partial.*")) == []


def test_a_link_to_an_asset_is_equally_refused(tmp_path, tools_ok):
    """Symlink, hard link or another spelling: the guard compares files, not names."""
    paths = real_paths(tmp_path)
    film = paths["va"]
    original = film.read_bytes()
    link = tmp_path / "alias.mkv"
    link.symlink_to(film)
    twin = tmp_path / "twin.mkv"
    twin.hardlink_to(film)
    project = add_clip(make_base(), "v1", "vt", ZERO, F(1))
    for alias in (link, twin):
        request = request_for(
            project, alias, overwrite=True, asset_paths=paths,
            guard=guard_for(paths.values()),
        )
        with pytest.raises(RenderError, match="same file"):
            request.resolve_tools()
    assert film.read_bytes() == original


def test_an_unused_asset_is_still_protected(tmp_path, tools_ok):
    """The guard covers the catalogue, not just the streams in this render."""
    paths = real_paths(tmp_path)
    unused = paths["aa"]  # a video-only composition never reads this one
    original = unused.read_bytes()
    project = add_clip(make_base(), "v1", "vt", ZERO, F(1))
    request = request_for(
        project, unused, overwrite=True, asset_paths=paths, guard=guard_for(paths.values())
    )
    with pytest.raises(RenderError, match="imported media"):
        request.resolve_tools()
    assert unused.read_bytes() == original


def test_a_file_created_during_the_render_survives_untouched(tmp_path, tools_ok, monkeypatch):
    """Preflight said "nothing there"; something appeared; nothing is clobbered."""
    paths = real_paths(tmp_path)
    out = tmp_path / "out.mp4"
    project = add_clip(make_base(), "v1", "vt", ZERO, F(1))

    def appear(job, cancel_event):
        out.write_text("another program finished first", encoding="utf-8")

    fake_ffmpeg(monkeypatch, during=appear)
    request = request_for(project, out, asset_paths=paths)
    with pytest.raises(RenderError, match=r"already exists|appeared"):
        render(request)
    assert out.read_text(encoding="utf-8") == "another program finished first"
    assert list(tmp_path.glob("*.partial.*")) == []  # our staging is cleaned up


def test_a_destination_replaced_after_consent_is_refused(tmp_path, tools_ok, monkeypatch):
    """The approval is tied to the bytes that existed when it was given."""
    paths = real_paths(tmp_path)
    out = tmp_path / "out.mp4"
    out.write_text("the output the user agreed to replace", encoding="utf-8")
    project = add_clip(make_base(), "v1", "vt", ZERO, F(1))

    def swap(job, cancel_event):
        out.write_text("replaced while we were rendering, and longer", encoding="utf-8")

    fake_ffmpeg(monkeypatch, during=swap)
    request = request_for(project, out, overwrite=True, asset_paths=paths)
    with pytest.raises(RenderError, match="not the file that was approved"):
        render(request)
    assert out.read_text(encoding="utf-8").startswith("replaced while")


def test_a_source_replaced_mid_render_is_refused_before_publish(
    tmp_path, tools_ok, monkeypatch
):
    """Two different encodes must never be cut into one output file."""
    paths = real_paths(tmp_path)
    out = tmp_path / "out.mp4"
    project = add_clip(make_base(), "v1", "vt", ZERO, F(1))
    identities = {asset_id: Identity.of(path) for asset_id, path in paths.items()}

    def swap(job, cancel_event):
        paths["va"].write_bytes(b"a completely different file at the same path")

    fake_ffmpeg(monkeypatch, during=swap)
    request = request_for(project, out, asset_paths=paths, asset_identities=identities)
    with pytest.raises(RenderError, match="after rendering"):
        render(request)
    assert not out.exists()
    assert list(tmp_path.glob("*.partial.*")) == []


def test_cancelling_after_the_last_progress_publishes_nothing(
    tmp_path, tools_ok, monkeypatch
):
    """Cancellation is honoured again after FFmpeg reports completion."""
    paths = real_paths(tmp_path)
    out = tmp_path / "out.mp4"
    project = add_clip(make_base(), "v1", "vt", ZERO, F(1))

    def cancel_just_after(job, cancel_event):
        cancel_event.set()  # the user pressed Cancel during the final flush

    fake_ffmpeg(monkeypatch, during=cancel_just_after)
    request = request_for(project, out, asset_paths=paths)
    with pytest.raises(RenderCancelled):
        render(request, cancel_event=threading.Event())
    assert not out.exists()
    assert list(tmp_path.glob("*.partial.*")) == []
