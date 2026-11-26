"""Exporter unit tests: argv selection, chunking, guards (no FFmpeg needed)."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from core_helpers import add_clip, make_base
from video_editor.core.commands import AddClipCommand
from video_editor.core.model import StreamSource, Track, TrackKind
from video_editor.fs.identity import Identity
from video_editor.fs.protection import DestinationGuard, ProtectedFile
from video_editor.media.framegrab import PictureTail, StillError
from video_editor.render.exporter import (
    PICTURE_TAIL_SECONDS,
    SOURCE_TAIL_TOLERANCE,
    OutputFacts,
    RenderCancelled,
    RenderError,
    RenderRequest,
    StreamFacts,
    _chunk_ranges,
    estimate_intermediate_bytes,
    probe_duration,
    probe_source_material,
    probe_streams,
    render,
    validate_output,
)
from video_editor.render.graph import LOSSLESS_INTERMEDIATE, MP4_H264_AAC
from video_editor.render.plan import build_plan

F = Fraction
ZERO = F(0)
ONE = F(1)
#: one picture of the 25 fps stream the helper projects below describe
FRAME = F(1, 25)


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


# ------------------------------------------------ what the output must contain


def a_plan():
    """4 s of 25 fps / 48 kHz stereo: 100 frames, 192000 samples."""
    return build_plan(add_clip(make_base(), "v1", "vt", ZERO, F(4)), ZERO, None)


def matching_facts(plan):
    """A produced file that satisfies the plan, used as the thing to break."""
    return (
        StreamFacts("video", "h264", plan.total_frames, plan.duration,
                    width=plan.width, height=plan.height),
        StreamFacts("audio", "aac", 4, plan.duration, position=1,
                    sample_rate=plan.quantisation.sample_rate, channels=2,
                    channel_layout=plan.channel_layout),
    )


def test_a_produced_file_that_matches_the_plan_is_accepted():
    plan = a_plan()
    # must not raise: exactly what the plan asked for, nothing measured away
    validate_output(plan, matching_facts(plan), plan.duration)


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda v, a: (None, a), "no picture stream"),
        (lambda v, a: (v, None), "no sound stream"),
        (lambda v, a: (replace(v, width=640), a), "640x1080"),
        (lambda v, a: (replace(v, packets=v.packets - 12), a), "picture has 88 frame"),
        (lambda v, a: (v, replace(a, packets=0)), "holds no packets"),
        (lambda v, a: (v, replace(a, sample_rate=44100)), "44100 Hz"),
        (lambda v, a: (v, replace(a, channels=6, channel_layout="5.1")),
         "composition asks for stereo"),
    ],
    ids=[
        "no video", "no audio", "wrong geometry", "frames missing", "empty sound stream",
        "wrong sample rate", "wrong channel count",
    ],
)
def test_output_validation_refuses_what_the_plan_did_not_ask_for(change, message):
    """Each rule fires on its own, with a message naming the disagreement."""
    plan = a_plan()
    video, audio = matching_facts(plan)
    broken = tuple(s for s in change(video, audio) if s is not None)
    with pytest.raises(RenderError, match=message):
        validate_output(plan, broken, plan.duration)


def test_output_validation_allows_only_the_encoders_own_padding():
    """The slack is what encoders really add, measured, not a codec assumption.

    A lossless PCM render decodes to exactly the planned samples; a 2 s AAC render
    was measured at +256 samples at 48 kHz and +888 at 44.1 kHz (priming plus tail
    padding). Anything up to ``AUDIO_PADDING_SECONDS`` over is therefore accepted.
    """
    plan = a_plan()
    rate = plan.quantisation.sample_rate
    for extra in (0, 1, 256, 888, 2048):
        seconds = F(plan.total_samples + extra, rate)
        validate_output(plan, matching_facts(plan), seconds)


@pytest.mark.parametrize(
    "audio_seconds, message",
    [
        (None, "cannot be decoded"),
        (F(191040, 48000), "decodes to 191040 samples"),  # 20 ms of the sound missing
        (F(196800, 48000), "decodes to 196800 samples"),  # 100 ms of invented extra
    ],
    ids=["undecodable", "short", "long"],
)
def test_the_length_of_the_rendered_sound_is_measured_not_counted_out(
    audio_seconds, message
):
    """The sound is checked by decoding it, because packet size proves nothing.

    Counting packets times a per-codec frame size was tried first and is wrong:
    PCM in Matroska writes 4096-sample blocks and AAC 1024, so that arithmetic
    refused a perfect lossless render (25 packets read as 25 samples) and could
    have accepted a real shortfall. What is compared is what decodes.
    """
    plan = a_plan()
    with pytest.raises(RenderError, match=message):
        validate_output(plan, matching_facts(plan), audio_seconds)


def test_probe_streams_reports_a_damaged_file_rather_than_nothing(tmp_path):
    """No streams at all is a failure, not an empty list to be skipped over."""
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is definitely not a video file" * 10)
    with pytest.raises(RenderError):
        probe_streams("ffprobe", broken)


# ------------------------------------------- sources that end before they claim


def end_to_end_project():
    """An audio clip that runs exactly to its stream's recorded end."""
    project = make_base()
    return AddClipCommand("aa", 1, "at", ZERO, source_in=ZERO, source_out=F(100),
                          clip_id="a1").apply(project)


def sources_of(project) -> dict:
    return {(s.asset_id, s.stream_index): s for s in project.sources}


@pytest.mark.parametrize(
    "available, expect",
    [
        (F(2), None),                       # the material is all there
        (F(2) - F(1, 50), "before its recorded end"),   # 20 ms: allowed padding
        (F(1), "before its recorded end"),              # a whole second missing
        (None, "could not be measured"),                # not measurable -> say so
    ],
    ids=["complete", "tiny shortfall", "missing material", "unmeasurable"],
)
def test_the_end_of_a_stream_a_clip_depends_on_is_measured(
    tmp_path, monkeypatch, available, expect
):
    """Silence at the end is either honest padding or a broken source.

    The graph always produces the sample count the plan promises, so a file that
    stops early is indistinguishable from an intentional ending in the output.
    The difference is measured against the source instead of guessed at, and the
    two sizes of shortfall are reported differently: a refused render and a
    warning that says how much was filled with silence.
    """
    project = end_to_end_project()
    plan = build_plan(project, ZERO, None)
    request = request_for(project, tmp_path / "out.mp4", asset_paths=real_paths(tmp_path))
    monkeypatch.setattr(
        "video_editor.render.exporter.probe_source_material",
        lambda ffmpeg, path, source, start, span: available,
    )
    if expect is None:
        assert request.check_source_tails("/usr/bin/ffmpeg", plan, sources_of(project)) == []
        return
    if available is not None and available < F(2) - SOURCE_TAIL_TOLERANCE:
        with pytest.raises(RenderError, match=expect):
            request.check_source_tails("/usr/bin/ffmpeg", plan, sources_of(project))
        return
    warnings = request.check_source_tails("/usr/bin/ffmpeg", plan, sources_of(project))
    assert len(warnings) == 1, warnings
    assert expect in warnings[0], warnings[0]


def test_a_clip_that_never_reaches_the_end_of_its_stream_is_not_probed(tmp_path, monkeypatch):
    """The measurement costs a short decode; it is not paid unless something needs it."""
    project = make_base()
    project = AddClipCommand("aa", 1, "at", ZERO, source_in=ZERO, source_out=F(10),
                             clip_id="a1").apply(project)
    request = request_for(project, tmp_path / "out.mp4", asset_paths=real_paths(tmp_path))
    calls: list[int] = []
    monkeypatch.setattr(
        "video_editor.render.exporter.probe_source_material",
        lambda *args: calls.append(1) or F(2),
    )
    assert request.check_source_tails("/usr/bin/ffmpeg", request.plan(), sources_of(project)) == []
    assert calls == []


def test_a_range_that_stops_before_the_end_of_a_clip_is_not_probed(tmp_path, monkeypatch):
    """A preview of the middle of a clip ending at EOF presents no end, so no probe.

    The check is about material that could reach the output as invented silence,
    not about whatever the graph happens to read.
    """
    project = end_to_end_project()  # clip runs to the stream's recorded end
    request = request_for(project, tmp_path / "window.mp4", asset_paths=real_paths(tmp_path))
    calls: list[int] = []
    monkeypatch.setattr(
        "video_editor.render.exporter.probe_source_material",
        lambda *args: calls.append(1) or F(2),
    )
    window = request.plan_for(F(40), F(60))
    assert request.check_source_tails("/usr/bin/ffmpeg", window, sources_of(project)) == []
    assert calls == []
    # the same clip rendered to its end is probed
    assert request.check_source_tails(
        "/usr/bin/ffmpeg", request.plan(), sources_of(project)
    ) == []
    assert calls == [1]


# ------------------------------------------------- the end of a picture stream


def picture_project(rate: F = ONE):
    """A video clip that runs exactly to its stream's recorded end (100 s here)."""
    project = make_base()
    return AddClipCommand(
        "va", 0, "vt", ZERO, source_in=ZERO, source_out=F(100), rate=rate, clip_id="v1"
    ).apply(project)


def fake_picture_tail(
    monkeypatch,
    missing: F = ZERO,
    frame_duration: F | None = FRAME,
    decoded: int = 50,
    inside: int = 50,
):
    """Stand in for the bounded decode, answering relative to the window requested.

    What is under test is the relationship between the source time a clip samples
    and where the decoded pictures stop, so the fake reports its answer against
    the window the exporter actually asked for - and that window is asserted to be
    bounded, because a probe that scans the whole title is not a probe.
    """
    calls: list[tuple[F, F]] = []

    def probe(path, stream_index, start, stop, *args, **kwargs):
        calls.append((start, stop))
        reference = stop - PICTURE_TAIL_SECONDS  # the furthest source time sampled
        last_pts = reference - missing - (frame_duration or ZERO)
        end = reference - missing if frame_duration is not None else None
        return PictureTail(
            decoded=decoded, inside=inside, last_pts=last_pts,
            frame_duration=frame_duration, end=end,
        )

    monkeypatch.setattr("video_editor.render.exporter.tail_picture_extent", probe)
    return calls


@pytest.mark.parametrize(
    "missing, frame_duration, expect",
    [
        (ZERO, F(1, 25), None),                       # every picture is there
        (F(1, 1000), F(1, 25), "within"),             # 1 ms: boundary rounding
        (F(1, 10), F(1, 25), "hold that one picture"),  # 0.1 s of frozen frame
        (F(1, 25), None, "runs out of pictures"),     # end known only to within one
    ],
    ids=["complete", "one millisecond", "a tenth of a second", "no frame duration"],
)
def test_a_picture_tail_is_measured_and_decided_on(
    tmp_path, monkeypatch, missing, frame_duration, expect
):
    """A frozen last frame is an ending nobody chose, so the tail gets measured.

    The audio check refuses invented silence; picture carries the same evidence in
    a better disguise, because a held frame looks exactly like an ending. The
    answer comes from decoded frames and the last frame's own recorded duration,
    never from packets or a nominal rate, and a shortfall is reported at two
    sizes: refused, or warned because the plan's boundary rounding covers it.
    """
    project = picture_project()
    request = request_for(project, tmp_path / "out.mp4", asset_paths=real_paths(tmp_path))
    calls = fake_picture_tail(monkeypatch, missing=missing, frame_duration=frame_duration)
    plan = build_plan(project, ZERO, None)
    if expect is None:
        assert request.check_picture_tails("/usr/bin/ffprobe", plan, sources_of(project)) == []
        # bounded, and centred on the source time the plan really samples:
        # 99.96 s is the last sample of a 100 s clip at 25 fps, 2 s either side
        assert calls == [(F(2449, 25), F(2549, 25))], calls
        return
    if "hold that one" in expect:
        with pytest.raises(RenderError, match=expect):
            request.check_picture_tails("/usr/bin/ffprobe", plan, sources_of(project))
        return
    warnings = request.check_picture_tails("/usr/bin/ffprobe", plan, sources_of(project))
    assert len(warnings) == 1, warnings
    assert expect in warnings[0], warnings[0]


def test_a_picture_stream_that_stops_short_of_its_own_bound_is_refused(
    tmp_path, monkeypatch
):
    """The truncated-file shape: pictures exist, they just stop 3 s early."""
    project = picture_project()
    request = request_for(project, tmp_path / "out.mp4", asset_paths=real_paths(tmp_path))
    monkeypatch.setattr(
        "video_editor.render.exporter.tail_picture_extent",
        lambda *args, **kwargs: PictureTail(6, 0, F(97), F(1, 25), F(388, 4)),
    )
    with pytest.raises(RenderError, match=r"stops \d+\.\d+s before the"):
        request.check_picture_tails(
            "/usr/bin/ffprobe", build_plan(project, ZERO, None), sources_of(project)
        )


def test_a_picture_stream_with_nothing_decoding_at_its_end_is_refused(
    tmp_path, monkeypatch
):
    """An empty window where the plan needs one is proven absence, not a shrug."""
    project = picture_project()
    request = request_for(project, tmp_path / "out.mp4", asset_paths=real_paths(tmp_path))
    monkeypatch.setattr(
        "video_editor.render.exporter.tail_picture_extent",
        lambda *args, **kwargs: PictureTail(0, 0, None, None, None),
    )
    with pytest.raises(RenderError, match="no picture that decodes"):
        request.check_picture_tails(
            "/usr/bin/ffprobe", build_plan(project, ZERO, None), sources_of(project)
        )


def test_a_picture_tail_that_cannot_be_measured_is_warned_not_guessed(
    tmp_path, monkeypatch
):
    """No answer from the probe is never read as "the media is fine"."""
    project = picture_project()
    request = request_for(project, tmp_path / "out.mp4", asset_paths=real_paths(tmp_path))

    def fail(*args, **kwargs):
        raise StillError("ffprobe could not decode this stream")

    monkeypatch.setattr("video_editor.render.exporter.tail_picture_extent", fail)
    warnings = request.check_picture_tails(
        "/usr/bin/ffprobe", build_plan(project, ZERO, None), sources_of(project)
    )
    assert len(warnings) == 1, warnings
    assert "could not be measured" in warnings[0], warnings[0]


def test_a_tempo_moves_the_picture_tail_ruling(tmp_path, monkeypatch):
    """The harm is time on the output clock, so the clip's tempo is part of it.

    A shortfall of 1/25 source second is a half output frame at double speed
    (allowed) and two output frames at half speed (refused) - the same number of
    missing pictures, judged on what the viewer would actually watch.
    """
    for rate, expect in ((F(2), None), (F(1, 2), "hold that one picture")):
        project = picture_project(rate)
        request = request_for(
            project, tmp_path / f"rate{rate}.mp4", asset_paths=real_paths(tmp_path)
        )
        fake_picture_tail(monkeypatch, missing=F(1, 25))
        plan = build_plan(project, ZERO, None)
        if expect is None:
            warnings = request.check_picture_tails(
                "/usr/bin/ffprobe", plan, sources_of(project)
            )
            assert len(warnings) == 1 and "within" in warnings[0], warnings
        else:
            with pytest.raises(RenderError, match=expect):
                request.check_picture_tails(
                    "/usr/bin/ffprobe", plan, sources_of(project)
                )


def test_a_picture_clip_that_never_reaches_its_streams_end_is_not_probed(
    tmp_path, monkeypatch
):
    """The bounded decode is paid for by renders that could show the end."""
    project = make_base()
    project = AddClipCommand("va", 0, "vt", ZERO, source_in=ZERO, source_out=F(10),
                             clip_id="v1").apply(project)
    request = request_for(project, tmp_path / "out.mp4", asset_paths=real_paths(tmp_path))
    calls = fake_picture_tail(monkeypatch)
    assert request.check_picture_tails(
        "/usr/bin/ffprobe", request.plan(), sources_of(project)
    ) == []
    assert calls == []


def test_the_probe_measures_the_window_it_is_given(tmp_path):
    """A real measurement against a real file, so the arithmetic is not folklore."""
    from media_fixtures import make_tone

    media = make_tone(tmp_path, "tone.wav", 660, seconds=6)
    source = StreamSource("t", 0, TrackKind.AUDIO, ZERO, F(6), sample_rate=48000,
                          stream_position=0)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:  # pragma: no cover - environment without ffmpeg
        pytest.skip("requires ffmpeg")
    assert probe_source_material(ffmpeg, media, source, F(1), F(2)) == F(2)
    # past where the file stops: only what exists comes back
    assert probe_source_material(ffmpeg, media, source, F(5), F(2)) < F(2)


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
    # these tests are about what happens *around* the render, not about the
    # produced file: no FFmpeg runs, so there is nothing real to probe
    monkeypatch.setattr(
        "video_editor.render.exporter.verify_output",
        lambda ffprobe, ffmpeg, path, plan: OutputFacts(
            video=StreamFacts("video", "h264", plan.total_frames, plan.duration,
                              width=plan.width, height=plan.height),
            audio=StreamFacts("audio", "aac", 1, plan.duration,
                              sample_rate=plan.quantisation.sample_rate,
                              channels=2, channel_layout=plan.channel_layout),
            audio_samples=plan.total_samples,
        ),
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
