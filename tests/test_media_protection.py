"""Filesystem safety: source protection, publication, and file identity.

These are the rules that decide whether the editor can destroy a user's film,
so they are tested directly rather than only through the UI: a write must never
land on media (however that file is spelled or linked), a finished file must be
published without clobbering anything, and "the file is there" must not be
mistaken for "the file is the one we recorded".

Everything here is synthetic: temporary files, symlinks and hard links.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from video_editor.fs.identity import AssetStatus, Identity, check
from video_editor.fs.protection import (
    Consent,
    DestinationExistsError,
    DestinationGuard,
    DestinationProtectedError,
    DestinationReplacedError,
    ProtectedFile,
    publish,
)

# ------------------------------------------------------------------- identity


def test_identity_states_are_explicit(tmp_path):
    """ONLINE / MISSING / CHANGED / UNREADABLE, each with a usable reason."""
    media = tmp_path / "film.mkv"
    media.write_bytes(b"one")
    recorded = Identity.of(media)
    assert check(media, recorded).status is AssetStatus.ONLINE
    assert check(media, recorded).usable

    gone = tmp_path / "gone.mkv"
    missing = check(gone, Identity(size_bytes=1, mtime_ns=1))
    assert missing.status is AssetStatus.MISSING
    assert "is not there" in missing.note

    media.write_bytes(b"a completely different and longer file")
    changed = check(media, recorded)
    assert changed.status is AssetStatus.CHANGED
    assert "not the file recorded here" in changed.note
    assert "different encode" in changed.note

    a_directory = tmp_path / "sub"
    a_directory.mkdir()
    assert check(a_directory).status is AssetStatus.UNREADABLE

    # a path that cannot be inspected at all (a file used as a directory)
    assert check(media / "child.mkv").status is AssetStatus.UNREADABLE


def test_a_record_without_identity_claims_nothing(tmp_path):
    """No recorded size/mtime means no provable mismatch — so: online."""
    media = tmp_path / "film.mkv"
    media.write_bytes(b"x")
    assert check(media, None).status is AssetStatus.ONLINE


def test_identity_tracks_size_or_mtime(tmp_path):
    media = tmp_path / "film.mkv"
    media.write_bytes(b"same length")
    before = Identity.of(media)
    media.touch()  # same size, new timestamp: a re-write is still a change
    assert check(media, before).status is AssetStatus.CHANGED


# ---------------------------------------------------------------- protection


def test_the_same_path_is_refused(tmp_path):
    film = tmp_path / "film.mkv"
    film.write_bytes(b"film")
    guard = DestinationGuard([ProtectedFile.media(film)])
    hit = guard.collision(film)
    assert hit is not None
    with pytest.raises(DestinationProtectedError, match="imported media"):
        guard.check(film, "export")


def test_relative_and_absolute_spellings_are_the_same_file(tmp_path, monkeypatch):
    film = tmp_path / "media" / "film.mkv"
    film.parent.mkdir()
    film.write_bytes(b"film")
    guard = DestinationGuard([ProtectedFile.media(film)])
    monkeypatch.chdir(tmp_path)
    alias = Path("media/film.mkv")  # the same file, spelled relative
    assert guard.collision(tmp_path / "media" / "./film.mkv") is not None
    assert guard.collision(alias.resolve()) is not None
    assert guard.collision(alias) is not None


def test_symlinks_and_hard_links_are_caught(tmp_path):
    film = tmp_path / "film.mkv"
    film.write_bytes(b"film")
    guard = DestinationGuard([ProtectedFile.media(film)])

    link = tmp_path / "alias.mkv"
    link.symlink_to(film)
    assert guard.collision(link) is not None

    hard = tmp_path / "twin.mkv"
    hard.hardlink_to(film)
    assert guard.collision(hard) is not None  # different name, same inode

    # an *outgoing* alias of the destination: writing through it writes the film
    director = tmp_path / "dir"
    director.symlink_to(tmp_path)
    assert guard.collision(director / "film.mkv") is not None


def test_an_unrelated_path_is_allowed(tmp_path):
    film = tmp_path / "film.mkv"
    film.write_bytes(b"film")
    guard = DestinationGuard([ProtectedFile.media(film)])
    assert guard.collision(tmp_path / "out.mp4") is None
    guard.check(tmp_path / "out.mp4", "export")  # no raise


def test_an_empty_guard_protects_nothing(tmp_path):
    guard = DestinationGuard()
    assert not guard
    assert guard.collision(tmp_path / "anything") is None


# --------------------------------------------------------------- publication


def stage(tmp_path, text: str = "rendered bytes") -> tuple[Path, Path]:
    staging = tmp_path / "out.token.partial.mp4"
    staging.write_text(text, encoding="utf-8")
    return staging, tmp_path / "out.mp4"


def test_publishing_without_approval_links_the_file(tmp_path):
    staging, destination = stage(tmp_path)
    assert publish(staging, destination) is None
    assert destination.read_text(encoding="utf-8") == "rendered bytes"
    assert not staging.exists()  # staging consumed by the link


def test_publishing_does_not_clobber_a_file_that_appeared(tmp_path):
    staging, destination = stage(tmp_path)
    destination.write_text("somebody else's movie", encoding="utf-8")
    with pytest.raises(DestinationExistsError):
        publish(staging, destination)
    assert destination.read_text(encoding="utf-8") == "somebody else's movie"
    assert staging.exists()  # the render is still there to inspect or delete


def test_approved_replacement_checks_the_file_it_approved(tmp_path):
    staging, destination = stage(tmp_path)
    destination.write_text("old output", encoding="utf-8")
    consent = Consent.capture(destination)
    assert publish(staging, destination, consent=consent) is None
    assert destination.read_text(encoding="utf-8") == "rendered bytes"
    assert not staging.exists()


def test_replacement_is_refused_when_the_file_changed_after_approval(tmp_path):
    staging, destination = stage(tmp_path)
    destination.write_text("old output", encoding="utf-8")
    consent = Consent.capture(destination)
    destination.write_text("replaced by another program while we rendered", encoding="utf-8")
    with pytest.raises(DestinationReplacedError, match="not the file that was approved"):
        publish(staging, destination, consent=consent)
    assert destination.read_text(encoding="utf-8").startswith("replaced by")


def test_replacement_is_refused_when_the_approved_file_disappeared(tmp_path):
    """Approved file gone, different file there: not the file that was approved."""
    staging, destination = stage(tmp_path)
    destination.write_text("old output", encoding="utf-8")
    consent = Consent.capture(destination)
    destination.unlink()
    destination.write_text("a new unrelated file", encoding="utf-8")
    with pytest.raises(DestinationReplacedError, match="not the file that was approved"):
        publish(staging, destination, consent=consent)
    assert destination.read_text(encoding="utf-8") == "a new unrelated file"


def test_publication_refuses_protected_media_even_with_approval(tmp_path):
    film = tmp_path / "film.mkv"
    film.write_bytes(b"the film")
    original = film.read_bytes()
    staging, _ = stage(tmp_path)
    guard = DestinationGuard([ProtectedFile.of(film, "imported media film.mkv")])
    with pytest.raises(DestinationProtectedError):
        publish(staging, film, guard=guard, consent=Consent.capture(film))
    assert film.read_bytes() == original


def test_an_owners_consent_may_replace_whatever_is_there(tmp_path):
    """A cache file belongs to us: replacing it needs no user approval."""
    staging, destination = stage(tmp_path)
    destination.write_text("previous cached preview", encoding="utf-8")
    publish(staging, destination, consent=Consent.as_owner(destination))
    assert destination.read_text(encoding="utf-8") == "rendered bytes"


def test_publishing_onto_a_directory_says_so(tmp_path):
    staging, destination = stage(tmp_path)
    destination.mkdir()
    from video_editor.fs.protection import DestinationSafetyError

    with pytest.raises(DestinationSafetyError, match="not a regular file"):
        publish(staging, destination)
