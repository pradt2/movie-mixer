"""Background import behaviour: cancellation, failures, duplicates, races."""

import time
from fractions import Fraction
from pathlib import Path

import pytest

from media_fixtures import (
    HAS_FFMPEG,
    make_audio_only,
    make_garbage,
    make_identity_offset,
    make_mixed,
    make_video_only,
)
from video_editor.media.importer import ImportController

pytestmark = pytest.mark.skipif(
    not HAS_FFMPEG, reason="ffprobe/ffmpeg required for import integration tests"
)


def run_sync(paths: list[Path]) -> dict[str, tuple[object, str | None]]:
    """Run the import controller with blocking callbacks (no Qt needed)."""
    results: dict[str, tuple[object, str | None]] = {}
    controller = ImportController(
        on_started=lambda job: None,
        on_finished=lambda job, asset, error: results.__setitem__(str(job.path), (asset, error)),
    )
    controller.import_paths(paths)
    deadline = time.monotonic() + 60
    while controller.pending_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert controller.pending_count() == 0, "imports did not finish"
    return results


def test_import_valid_and_invalid_keeps_successes(tmp_path):
    good_a = make_mixed(tmp_path, "a.mkv")
    good_b = make_audio_only(tmp_path, "b.wav")
    bad = make_garbage(tmp_path, "bad.mp4")
    results = run_sync([good_a, bad, good_b])
    assert results[str(good_a)][1] is None
    assert results[str(good_b)][1] is None
    assert results[str(bad)][0] is None
    assert results[str(bad)][1]  # error message present
    asset_a = results[str(good_a)][0]
    assert asset_a.id  # stable id assigned
    assert asset_a.path == good_a
    assert len(asset_a.metadata.streams) == 3


def test_import_missing_file(tmp_path):
    results = run_sync([tmp_path / "does-not-exist.mp4"])
    asset, error = results[str(tmp_path / "does-not-exist.mp4")]
    assert asset is None
    assert "not found" in error.lower() or "not exist" in error.lower()


def test_cancel_import(tmp_path):
    big = make_mixed(tmp_path, "c.mkv")
    outcomes = {}
    controller = ImportController(
        on_started=lambda job: controller.cancel(job.id),
        on_finished=lambda job, asset, error: outcomes.__setitem__(job.path, (asset, error)),
    )
    controller.import_paths([big])
    deadline = time.monotonic() + 30
    while controller.pending_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    asset, error = outcomes[big]
    assert asset is None
    assert error == "cancelled"


def test_parallel_imports_each_complete_once(tmp_path):
    files = [
        make_mixed(tmp_path, "p1.mkv"),
        make_video_only(tmp_path, "p2.mp4"),
        make_audio_only(tmp_path, "p3.wav"),
    ]
    results = run_sync(files)
    assert set(results) == {str(f) for f in files}
    assert all(asset is not None for asset, _ in results.values())
    ids = {results[str(f)][0].id for f in files}
    assert len(ids) == 3


def test_an_imported_offset_asset_keeps_the_measured_bound(tmp_path):
    """Import is the one place the probe's numbers become the project's.

    Measured for ``make_identity_offset``: picture material runs 5/2 .. 13/2 s
    while ``ffprobe`` prints container start 2.5 and duration 6.5. The record this
    path hands the application holds the material's end, marked as the container's
    claim, and not the 9 s a span reading of those two fields would give.
    """
    media = make_identity_offset(tmp_path, count=100)
    asset, error = run_sync([media])[str(media)]
    assert error is None and asset is not None
    stream = asset.metadata.stream(0)
    assert stream.origin == Fraction(5, 2)
    assert stream.end_time == Fraction(13, 2)
    assert stream.end_horizon == Fraction(13, 2)
    assert stream.bounds_are_conservative
