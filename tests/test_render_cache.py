"""Preview cache tests: what invalidates a key, and how both caches stay bounded."""

import os
from dataclasses import replace
from fractions import Fraction

import pytest

from core_helpers import add_clip, make_base
from video_editor.core.commands import (
    RenameTrackCommand,
    SetFadesCommand,
    SetGainCommand,
    SetTrackMutedCommand,
)
from video_editor.core.model import OutputSettings
from video_editor.render import cache
from video_editor.render.cache import CachedFrame, FrameCache, PreviewCache, render_fingerprint
from video_editor.render.graph import MP4_H264_AAC, PREVIEW_H264_FAST

F = Fraction
ZERO = Fraction(0)
HALF = Fraction(1, 2)
FOUR = Fraction(4)


@pytest.fixture()
def media(tmp_path):
    created = {}
    for name in ("va", "aa"):
        path = tmp_path / f"{name}.mkv"
        path.write_bytes(b"seed" * 256)
        created[name] = path
    return created


def key_of(project, media, start=ZERO, end=FOUR, scale=HALF, preset=PREVIEW_H264_FAST):
    return render_fingerprint(project, media, start, end, scale, preset)


# ------------------------------------------------------------------- fingerprint


def test_the_same_request_fingerprints_identically(media):
    project = add_clip(make_base(), "c1", "vt", F(0), F(4))
    assert key_of(project, media) == key_of(project, media)


def test_media_written_by_an_older_renderer_is_never_served_as_current(media, monkeypatch):
    """A cache hit is a promise that the media is what this renderer would make.

    Fade anchors used to follow the clip's own stream clock rather than output
    time, so previews rendered before that fix contain no fade at all. The
    project and the files on disk are unchanged since, so no other part of the
    key would notice — the semantics version is what makes those old entries miss
    instead of silently being shown as if they were current.
    """
    project = add_clip(make_base(), "c1", "vt", F(0), F(4))
    current = key_of(project, media)
    monkeypatch.setattr(cache, "SEMANTICS_VERSION", "1")
    assert key_of(project, media) != current


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p, m: replace(p, output=OutputSettings(640, 360, F(25), 48000, "stereo")),
        lambda p, m: replace(p, output=replace(p.output, frame_rate=F(24))),
        lambda p, m: replace(p, output=replace(p.output, sample_rate=44100)),
        lambda p, m: replace(p, output=replace(p.output, channel_layout="5.1")),
        lambda p, m: SetTrackMutedCommand("at", True).apply(p),
        lambda p, m: SetGainCommand("c1", F(1, 2)).apply(p),
        lambda p, m: SetFadesCommand("c1", F(1, 4), F(0)).apply(p),
        lambda p, m: add_clip(p, "c2", "vt", F(4), F(2)),
    ],
)
def test_anything_that_changes_the_media_changes_the_key(mutate, media):
    project = add_clip(make_base(), "c1", "vt", F(0), F(4))
    changed = mutate(project, media)
    assert changed != project  # the mutation really did something
    assert key_of(project, media) != key_of(changed, media)


def test_view_state_and_track_names_do_not_invalidate(media):
    """Renaming a track cannot change the rendered media, so the cache stays valid."""
    project = add_clip(make_base(), "c1", "vt", F(0), F(4))
    renamed = RenameTrackCommand("vt", "Picture from A").apply(project)
    assert renamed != project
    assert key_of(project, media) == key_of(renamed, media)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"end": F(6)},
        {"start": F(1)},
        {"scale": F(1)},
        {"preset": MP4_H264_AAC},
    ],
)
def test_range_picture_size_and_preset_are_part_of_the_key(kwargs, media):
    project = add_clip(make_base(), "c1", "vt", F(0), F(4))
    args = {"start": ZERO, "end": FOUR, "scale": HALF, "preset": PREVIEW_H264_FAST}
    args.update(kwargs)
    assert key_of(project, media) != key_of(project, media, **args)


def test_replacing_a_file_on_disk_invalidates_the_cache(tmp_path):
    project = add_clip(make_base(), "c1", "vt", F(0), F(4))
    media = {"va": tmp_path / "va.mkv", "aa": tmp_path / "aa.mkv"}
    for path in media.values():
        path.write_bytes(b"original")
    before = key_of(project, media)
    os.utime(media["va"], ns=(1_000_000_000, 1_000_000_000))
    media["va"].write_bytes(b"re-cut, longer" * 40)
    assert key_of(project, media) != before


def test_a_missing_asset_never_reuses_an_existing_key(tmp_path):
    project = add_clip(make_base(), "c1", "vt", F(0), F(4))
    present = {"va": tmp_path / "va.mkv", "aa": tmp_path / "aa.mkv"}
    for path in present.values():
        path.write_bytes(b"x")
    missing = {"va": tmp_path / "gone.mkv", "aa": present["aa"]}
    assert key_of(project, present) != key_of(project, missing)


# ------------------------------------------------------------------ disk cache


def test_commit_then_lookup_and_ownership(tmp_path):
    foreign = tmp_path / "someone-elses-export.mp4"
    foreign.write_bytes(b"a finished export, not ours")
    cache = PreviewCache(tmp_path / "previews", max_bytes=10_000, max_entries=4)
    path = cache.path_for("abc123abc123abc123ab12")
    assert cache.lookup("abc123abc123abc123ab12") is None
    path.write_bytes(b"y" * 500)
    entry = cache.commit(path)
    assert entry.size == 500
    assert cache.lookup("abc123abc123abc123ab12") == path
    assert cache.total_bytes == 500
    assert not cache.discard(foreign)
    assert not cache.owns(foreign)
    assert foreign.exists() and foreign.read_bytes().startswith(b"a finished")


def test_byte_budget_evicts_least_recently_touched(tmp_path):
    cache = PreviewCache(tmp_path / "previews", max_bytes=1_000, max_entries=99)
    keys = [f"k{i:06d}k{i:06d}k{i:06d}k{i:06d}" for i in range(5)]
    for key in keys:
        path = cache.path_for(key)
        path.write_bytes(b"z" * 400)
        cache.commit(path)
    kept = [e.key for e in cache.entries()]
    assert cache.total_bytes <= 1_000
    assert kept == keys[3:]  # three oldest dropped, two 400-byte entries kept
    assert not any(cache.path_for(k).exists() for k in keys[:3])
    assert all(cache.path_for(k).exists() for k in keys[3:])


def test_touching_an_entry_saves_it_from_eviction(tmp_path):
    cache = PreviewCache(tmp_path / "previews", max_bytes=1_000, max_entries=99)
    first = cache.path_for("a" * 32)
    first.write_bytes(b"x" * 400)
    cache.commit(first)
    second = cache.path_for("b" * 32)
    second.write_bytes(b"x" * 400)
    cache.commit(second)
    assert cache.lookup("a" * 32) == first  # marks it recently used
    third = cache.path_for("c" * 32)
    third.write_bytes(b"x" * 400)
    cache.commit(third)
    assert first.exists() and not second.exists()


def test_entry_count_budget_also_bounds(tmp_path):
    cache = PreviewCache(tmp_path / "previews", max_bytes=10_000_000, max_entries=2)
    for i in range(5):
        path = cache.path_for(f"{i:032d}")
        path.write_bytes(b"q" * 10)
        cache.commit(path)
    assert len(cache.entries()) == 2


def test_files_surviving_a_restart_are_adopted_and_bounded(tmp_path):
    root = tmp_path / "previews"
    root.mkdir()
    for i in range(4):
        (root / f"preview-{'f' * 24}{i}.mp4").write_bytes(b"m" * 700)
    cache = PreviewCache(root, max_bytes=1_500, max_entries=99)
    assert cache.total_bytes <= 1_500
    assert len(cache.entries()) == 2
    assert sum(1 for _ in root.iterdir()) == 2


def test_prune_removes_interrupted_render_leftovers_only(tmp_path):
    root = tmp_path / "previews"
    root.mkdir()
    leftovers = [
        root / "preview-abc.9f3c.partial.mp4",
        root / "preview-abc.chunk000.mkv",
    ]
    for path in leftovers:
        path.write_bytes(b"junk")
    keep = root / ("preview-" + "a" * 24 + ".mp4")
    keep.write_bytes(b"real preview")
    foreign = root / "README.txt"
    foreign.write_bytes(b"do not delete me")
    cache = PreviewCache(root, max_bytes=10_000, max_entries=8)
    removed = cache.prune_orphans()
    assert {p.name for p in removed} == {p.name for p in leftovers}
    assert not any(p.exists() for p in leftovers)
    assert keep.exists() and foreign.exists()


def test_clear_and_close_release_owned_files(tmp_path):
    cache = PreviewCache(tmp_path / "previews", max_bytes=10_000, max_entries=4)
    path = cache.path_for("d" * 24)
    path.write_bytes(b"v" * 100)
    cache.commit(path)
    removed = cache.close()
    assert path in removed
    assert not path.exists()
    assert cache.total_bytes == 0


def test_budgets_must_be_positive(tmp_path):
    with pytest.raises(ValueError, match="budgets must be positive"):
        PreviewCache(tmp_path, max_bytes=0)
    with pytest.raises(ValueError, match="budgets must be positive"):
        PreviewCache(tmp_path, max_entries=0)


# ----------------------------------------------------------------- frame cache


def test_frame_cache_is_bounded_by_bytes_not_entries():
    cache = FrameCache(max_bytes=1_000)
    big = CachedFrame(key=("p", 0), width=100, height=100, rgb=b"\x00" * 900)
    assert cache.put(big) == 0
    assert cache.put(CachedFrame(key=("p", 1), width=10, height=10, rgb=b"\x00" * 300)) == 1
    assert cache.get(("p", 0)) is None
    assert cache.total_bytes == 300
    assert len(cache) == 1


def test_frame_cache_clear_drops_everything_and_keeps_the_callers_object():
    cache = FrameCache(max_bytes=10_000)
    cache.put(CachedFrame(key=("p", 0), width=4, height=4, rgb=b"\x00" * 48, payload="still-0"))
    cache.put(CachedFrame(key=("p", 1), width=4, height=4, rgb=b"\x00" * 48, payload="still-1"))
    assert cache.get(("p", 0)).payload == "still-0"  # the UI gets its own frame back
    assert cache.clear() == 2
    assert len(cache) == 0 and cache.total_bytes == 0
    assert cache.get(("p", 1)) is None


def test_frame_cache_reuses_and_refreshes():
    cache = FrameCache(max_bytes=10_000)
    cache.put(CachedFrame(key=("p", 3), width=4, height=4, rgb=b"\x01" * 48))
    again = CachedFrame(key=("p", 3), width=4, height=4, rgb=b"\x02" * 48)
    cache.put(again)
    assert len(cache) == 1 and cache.total_bytes == 48
    assert cache.get(("p", 3)).rgb == b"\x02" * 48


def test_frame_cache_can_drop_every_frame_of_one_preview():
    cache = FrameCache(max_bytes=10_000)
    for index in range(5):
        cache.put(CachedFrame(key=("previewA", index), width=2, height=2, rgb=b"\x00" * 12))
    cache.put(CachedFrame(key=("previewB", 0), width=2, height=2, rgb=b"\x00" * 12))
    assert cache.forget(("previewA",)) == 5
    assert len(cache) == 1
    assert cache.get(("previewB", 0)) is not None


def test_frame_budget_must_be_positive():
    with pytest.raises(ValueError, match="budget must be positive"):
        FrameCache(max_bytes=0)
