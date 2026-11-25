"""Core domain tests: exact math, invariants, queries — no GUI, no FFmpeg."""

import pytest

from core_helpers import HUNDRED, ONE, T23976, TEN, ZERO, F, audio_source, video_source
from video_editor.core.model import (
    Clip,
    OutputSettings,
    Project,
    StreamSource,
    Track,
    TrackKind,
    envelope_gain,
)
from video_editor.core.timeutils import overlaps, parse_fraction


def clip(cid="c1", tid="vt", start=ZERO, duration=TEN, rate=ONE, **kw):
    return Clip(
        id=cid,
        asset_id=kw.pop("asset_id", "va"),
        stream_index=kw.pop("stream_index", 0),
        track_id=tid,
        source_in=kw.pop("source_in", ZERO),
        source_out=kw.pop("source_out", duration * rate),
        output_start=start,
        rate=rate,
        **kw,
    )


# ------------------------------------------------------------------ fractions


def test_exact_rate_math_23976():
    c = clip(duration=F(4), rate=T23976, source_out=T23976 * 4)
    assert c.source_duration == T23976 * 4
    assert c.output_duration == F(4)  # exact, not 3.9999...
    assert c.output_end == F(4)
    assert c.source_time_at(F(2)) == T23976 * 2


def test_rate_semantics_source_seconds_per_output_second():
    # rate 2 => 2 source seconds consumed per output second: played at 2x speed
    c = clip(duration=F(5), rate=F(2), source_out=F(10))
    assert c.output_duration == F(5)
    # rate 1/2 => half speed: output takes longer than the source interval
    c2 = clip(rate=F(1, 2), source_in=ZERO, source_out=F(10))
    assert c2.output_duration == F(20)


def test_long_timeline_no_float_drift():
    # ~2 hours of 24000/1001 frames accumulated exactly
    frame = F(1001, 24000)  # one frame duration at 23.976
    frames = 24000 * 2 * 60 // 24
    total = F(0)
    for _ in range(frames):
        total += frame
    assert total == frame * frames  # exact accumulation, no drift
    c = clip(start=total, rate=T23976, source_out=T23976)
    assert c.output_start == total  # exact at 2 h+
    assert c.output_end == total + 1
    assert c.source_time_at(total + F(1, 2)) == T23976 / 2


def test_parse_fraction_exact():
    assert parse_fraction("24000/1001") == T23976
    assert parse_fraction("0.5") == F(1, 2)
    assert parse_fraction(" 25 ") == F(25)
    with pytest.raises(ValueError):
        parse_fraction("banana")


# ------------------------------------------------------------------- geometry


def test_half_open_coverage_boundaries():
    c = clip(start=ZERO, duration=F(10))
    assert c.covers(ZERO)
    assert c.covers(F(10) - F(1, 1000))
    assert not c.covers(F(10))
    assert not c.covers(F(-1))


def test_overlaps_helper_half_open():
    assert not overlaps(ZERO, F(10), F(10), F(20))
    assert not overlaps(F(10), F(20), ZERO, F(10))
    assert overlaps(ZERO, F(10), F(9), F(20))
    assert overlaps(F(5), F(6), ZERO, F(100))


def test_output_end_counts_muted_and_disabled():
    p = Project(
        sources=(video_source(), audio_source()),
        tracks=(
            Track(id="vt", kind=TrackKind.VIDEO, name="V1"),
            Track(id="at", kind=TrackKind.AUDIO, name="A1", enabled=False, muted=True),
        ),
        clips=(
            clip(cid="c1", tid="vt", start=ZERO, duration=F(5)),
            clip(cid="c2", tid="at", start=F(20), duration=F(3), asset_id="aa", stream_index=1),
        ),
    )
    assert p.output_end() == F(23)
    assert p.video_clip_at(F(21)) is None  # track disabled: no picture
    assert p.audio_clips_at(F(21)) == ()  # disabled track makes no sound
    assert p.output_end() != ZERO  # duration unchanged by the toggle


def test_video_stacking_topmost_wins():
    top = Track(id="top", kind=TrackKind.VIDEO, name="top")
    bottom = Track(id="bot", kind=TrackKind.VIDEO, name="bot")
    hidden = Track(id="off", kind=TrackKind.VIDEO, name="off", enabled=False)
    p = Project(
        tracks=(top, bottom, hidden),
        clips=(
            clip(cid="b", tid="bot", start=ZERO, duration=F(10)),
            clip(cid="t", tid="top", start=F(4), duration=F(2)),
            clip(cid="h", tid="off", start=ZERO, duration=F(10)),
        ),
    )
    assert p.video_clip_at(F(2)).id == "b"
    assert p.video_clip_at(F(5)).id == "t"
    assert p.video_clip_at(F(7)).id == "b"
    assert p.video_clip_at(F(11)) is None  # black beyond clips


def test_audio_sums_all_unmuted_tracks():
    a1 = Track(id="a1", kind=TrackKind.AUDIO, name="A1")
    a2 = Track(id="a2", kind=TrackKind.AUDIO, name="A2", muted=True)
    a3 = Track(id="a3", kind=TrackKind.AUDIO, name="A3")
    p = Project(
        tracks=(a1, a2, a3),
        clips=(
            clip(cid="x", tid="a1", start=ZERO, duration=F(10), asset_id="aa", stream_index=1),
            clip(cid="y", tid="a2", start=ZERO, duration=F(10), asset_id="aa", stream_index=1),
            clip(cid="z", tid="a3", start=F(5), duration=F(10), asset_id="aa", stream_index=1),
        ),
    )
    at_two = p.audio_clips_at(F(6))
    assert {c.id for c in at_two} == {"x", "z"}
    assert p.audio_clips_at(F(20)) == ()  # gaps are silent


def test_interval_intersection_query():
    p = Project(
        clips=(
            clip(cid="a", start=ZERO, duration=F(5)),
            clip(cid="b", start=F(10), duration=F(5)),
        )
    )
    assert {c.id for c in p.clips_in_interval(F(4), F(11))} == {"a", "b"}
    assert p.clips_in_interval(F(5), F(10)) == ()  # half-open touches do not intersect
    assert {c.id for c in p.clips_in_interval(F(9), F(11))} == {"b"}


def test_stream_source_placeable_rules():
    known = StreamSource("a", 0, TrackKind.VIDEO, F(2), HUNDRED)
    assert known.placeable
    unknown = StreamSource("a", 0, TrackKind.VIDEO, F(2), None)
    assert not unknown.placeable
    empty = StreamSource("a", 0, TrackKind.VIDEO, F(2), F(2))
    assert not empty.placeable


# -------------------------------------------------------------------- envelope


def test_envelope_linear_fades_exact():
    c = clip(start=F(2), duration=F(10), fade_in=F(2), fade_out=F(4), gain=F(3, 2))
    assert envelope_gain(c, c.output_start) == F(0)
    assert envelope_gain(c, c.output_start + F(1)) == F(1, 2)
    assert envelope_gain(c, c.output_start + F(2)) == F(1)
    assert envelope_gain(c, c.output_end - F(2)) == F(1, 2)  # 2 of 4 remaining
    assert envelope_gain(c, c.output_end - F(1, 2)) == F(1, 8)
    assert envelope_gain(c, c.output_end) == F(0)  # half-open
    assert envelope_gain(c, c.output_start - F(1)) == F(0)


def test_output_settings_validation():
    OutputSettings(width=1920, height=1080, frame_rate=T23976, sample_rate=48000)
    with pytest.raises(ValueError):
        OutputSettings(frame_rate=F(0))
    with pytest.raises(ValueError):
        OutputSettings(width=-5)


def test_clip_construction_validation():
    with pytest.raises(ValueError):
        Clip("x", "a", 0, "t", F(5), F(5), ZERO)  # zero-length interval
    with pytest.raises(ValueError):
        Clip("x", "a", 0, "t", ZERO, F(5), F(-1))  # negative output start
    with pytest.raises(ValueError):
        Clip("x", "a", 0, "t", ZERO, F(5), ZERO, rate=F(0))
