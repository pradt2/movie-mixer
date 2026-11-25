"""Shared builders for core domain tests (exact fractions everywhere)."""

from fractions import Fraction

from video_editor.core.commands import AddClipCommand, RegisterSourceCommand
from video_editor.core.model import Project, StreamSource, Track, TrackKind

F = Fraction
ZERO = Fraction(0)
ONE = Fraction(1)
TEN = Fraction(10)
HUNDRED = Fraction(100)
T25 = Fraction(25)
T23976 = Fraction(24000, 1001)


def video_source(asset_id: str = "va", end: Fraction = HUNDRED, origin: Fraction = ZERO):
    """First video stream of its container (container index 0)."""
    return StreamSource(
        asset_id, 0, TrackKind.VIDEO, origin, end, frame_rate=T25, width=128, height=96,
        stream_position=0,
    )


def audio_source(asset_id: str = "aa", end: Fraction = HUNDRED, origin: Fraction = ZERO):
    """Second stream of the container, but the first *audio* stream."""
    return StreamSource(
        asset_id, 1, TrackKind.AUDIO, origin, end, sample_rate=48000, stream_position=0
    )


def make_base() -> Project:
    """Project with va#0 (video) and aa#1 (audio) registered, tracks vt/at."""
    p = Project()
    p = RegisterSourceCommand(video_source("va")).apply(p)
    p = RegisterSourceCommand(audio_source("aa")).apply(p)
    vtrack = Track(id="vt", kind=TrackKind.VIDEO, name="V1")
    atrack = Track(id="at", kind=TrackKind.AUDIO, name="A1")
    return p.with_tracks((vtrack, atrack))


def add_clip(
    p: Project,
    cid: str,
    tid: str,
    start: Fraction,
    duration: Fraction,
    asset: str = "va",
    stream: int = 0,
    rate: Fraction = ONE,
    source_in: Fraction | None = None,
) -> Project:
    origin = source_in if source_in is not None else p.source(asset, stream).origin
    return AddClipCommand(
        asset_id=asset,
        stream_index=stream,
        track_id=tid,
        output_start=start,
        source_in=origin,
        source_out=origin + duration * rate,
        rate=rate,
        clip_id=cid,
    ).apply(p)
