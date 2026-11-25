"""Project document encoding/decoding: the versioned JSON schema.

Schema (``schema_version = 1``, documented also in ``docs/architecture.md``)
--------------------------------------------------------------------------
A project document is a single JSON object. Every key listed below is required
unless marked optional, and **unknown keys are rejected rather than ignored**:
a field this version does not know about means the file was written by a newer
schema, and silently discarding it would destroy data on the next save.

    schema_version          int, this file's schema version
    application             {"name": "video-editor", "version": str}   (optional)
    output                  {"width": int, "height": int,
                             "frame_rate": fraction, "sample_rate": int,
                             "channel_layout": str}
    assets                  [{"id": str, "path_mode": "relative"|"absolute",
                              "path": str, "absolute_path": str,
                              "size_bytes": int|null, "mtime_ns": int|null,
                              "probe_error": str|null, "label": str|null,
                              "metadata": {...}|null}]        (see media.serialize)
    sources                 [StreamSource fields, keyed by asset_id + stream_index]
    tracks                  [{"id", "kind": "video"|"audio", "name",
                              "enabled", "muted"}]   in stacking order, top first
    clips                   [Clip fields, source/output intervals as fractions]
    navigation              {"playhead": fraction, "zoom": number,
                             "scroll_x": fraction, "selected_clips": [str]}
                            (optional — UI state, never part of dirty tracking)

Exact time: every rational (times, rates, gains) is stored as the string
``"numerator/denominator"`` — never a decimal — so a ``24000/1001`` grid and a
trim at ``1/3`` s survive a save/load byte for byte. Halves are normalised by
``Fraction`` itself; the parser rejects anything that is not exactly
``int/int``. ``null`` means genuinely unknown and is never replaced by 0.

Paths: ``path`` is the reference used on load. With ``path_mode="relative"`` it
is resolved against **the directory containing the project file**; with
``path_mode="absolute"`` it is used as given. ``absolute_path`` records where
the file was when it was saved (diagnostics and a fallback when the relative
reference cannot be found). Relative references are only written when they stay
inside the project directory — a ``../`` escape would break as soon as the
project moves, so such paths stay absolute.

Migration policy
----------------
``SCHEMA_VERSION`` is the highest version this build can read.

* A file with a *higher* version is refused with a diagnostic naming the
  version and the fields it could not interpret. It is never partially loaded,
  never "best effort" repaired and never rewritten.
* A file with a *lower* version is accepted only through an explicit migration
  function registered in ``MIGRATIONS`` (none are needed for the first schema).
  A lower version with no registered migration is refused rather than guessed.
* Additive changes to version 1 (new *optional* keys) are handled by defaults;
  any change to the meaning of an existing key requires a version bump plus a
  migration, and the older form must keep being readable until the next release
  note says otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Any

from ..core.model import Clip, OutputSettings, Project, StreamSource, Track, TrackKind
from ..core.timeutils import ZERO

SCHEMA_NAME = "video-editor-project"
SCHEMA_VERSION = 1
#: versions that need an explicit migration before the current one applies
MIGRATIONS: dict[int, Any] = {}


class ProjectFileError(Exception):
    """The document is not readable by this version of the application."""


# ------------------------------------------------------------------- fractions


def encode_fraction(value: Fraction | None) -> str | None:
    """Exact rational as ``"num/den"`` (never a decimal approximation)."""
    if value is None:
        return None
    value = Fraction(value)
    return f"{value.numerator}/{value.denominator}"


def decode_fraction(raw: object, field: str) -> Fraction:
    """Parse ``"num/den"`` strictly; anything else is a schema violation."""
    if isinstance(raw, str):
        parts = raw.split("/")
        if len(parts) == 2:
            try:
                return Fraction(int(parts[0]), int(parts[1]))
            except ValueError:
                pass
        if raw.strip() and all(part not in ".eE" for part in raw):
            # an integer is a legal exact rational, and unambiguous
            try:
                return Fraction(int(raw))
            except ValueError:
                pass
    raise ProjectFileError(
        f"{field} must be an exact rational stored as 'numerator/denominator', "
        f"got {raw!r}"
    )


def _optional_fraction(raw: object, field: str) -> Fraction | None:
    return None if raw is None else decode_fraction(raw, field)


# ---------------------------------------------------------------- navigation


@dataclass(frozen=True)
class Navigation:
    """Saved UI navigation state (playhead/zoom/scroll/selection).

    This is *not* composition data: moving the playhead never makes a project
    dirty, and restoring it is a convenience, so an absent or out-of-range
    value is clamped rather than treated as corruption.
    """

    playhead: Fraction = ZERO
    zoom: float = 1.0
    scroll_x: Fraction = ZERO
    selected_clips: tuple[str, ...] = ()

    def clamped(self, output_end: Fraction) -> Navigation:
        end = max(ZERO, output_end)
        playhead = min(max(ZERO, self.playhead), end)
        scroll = min(max(ZERO, self.scroll_x), end)
        zoom = self.zoom if self.zoom > 0 else 1.0
        return replace(self, playhead=playhead, scroll_x=scroll, zoom=zoom)


def encode_navigation(nav: Navigation) -> dict[str, Any]:
    return {
        "playhead": encode_fraction(nav.playhead),
        "zoom": float(nav.zoom),
        "scroll_x": encode_fraction(nav.scroll_x),
        "selected_clips": list(nav.selected_clips),
    }


def decode_navigation(raw: object) -> Navigation:
    if raw is None:
        return Navigation()
    if not isinstance(raw, dict):
        raise ProjectFileError("'navigation' must be an object")
    reject_unknown_fields(raw, {"playhead", "zoom", "scroll_x", "selected_clips"}, "navigation")
    selected = raw.get("selected_clips") or []
    if not isinstance(selected, list):
        raise ProjectFileError("navigation.selected_clips must be a list")
    zoom = raw.get("zoom", 1.0)
    if not isinstance(zoom, (int, float)):
        raise ProjectFileError("navigation.zoom must be a number")
    return Navigation(
        playhead=_optional_fraction(raw.get("playhead", "0/1"), "navigation.playhead"),
        zoom=float(zoom),
        scroll_x=_optional_fraction(raw.get("scroll_x", "0/1"), "navigation.scroll_x"),
        selected_clips=tuple(str(item) for item in selected),
    )


# --------------------------------------------------------------------- checks


def reject_unknown_fields(obj: dict, allowed: set[str], where: str) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        raise ProjectFileError(
            f"{where} has field(s) this version does not understand: {', '.join(extra)}. "
            "The file was written by a newer schema; refusing to guess, and not "
            "saving over it, protects that data."
        )


def _require(obj: dict, key: str, where: str) -> Any:
    if key not in obj:
        raise ProjectFileError(f"{where} is missing required field '{key}'")
    return obj[key]


def _require_int(obj: dict, key: str, where: str) -> int:
    value = _require(obj, key, where)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectFileError(f"{where}.{key} must be an integer, got {value!r}")
    return value


def _require_str(obj: dict, key: str, where: str) -> str:
    value = _require(obj, key, where)
    if not isinstance(value, str):
        raise ProjectFileError(f"{where}.{key} must be a string, got {value!r}")
    return value


def _opt_bool(obj: dict, key: str, where: str, default: bool) -> bool:
    value = obj.get(key, default)
    if not isinstance(value, bool):
        raise ProjectFileError(f"{where}.{key} must be true or false, got {value!r}")
    return value


# -------------------------------------------------------------------- project


def encode_project(project: Project) -> dict[str, Any]:
    """Pure structural encoding of tracks, clips and registered sources."""
    out = project.output
    return {
        "output": {
            "width": out.width,
            "height": out.height,
            "frame_rate": encode_fraction(out.frame_rate),
            "sample_rate": out.sample_rate,
            "channel_layout": out.channel_layout,
        },
        "tracks": [
            {
                "id": track.id,
                "kind": track.kind.value,
                "name": track.name,
                "enabled": track.enabled,
                "muted": track.muted,
            }
            for track in project.tracks
        ],
        "clips": [
            {
                "id": clip.id,
                "asset_id": clip.asset_id,
                "stream_index": clip.stream_index,
                "track_id": clip.track_id,
                "source_in": encode_fraction(clip.source_in),
                "source_out": encode_fraction(clip.source_out),
                "output_start": encode_fraction(clip.output_start),
                "rate": encode_fraction(clip.rate),
                "gain": encode_fraction(clip.gain),
                "fade_in": encode_fraction(clip.fade_in),
                "fade_out": encode_fraction(clip.fade_out),
                "link_group": clip.link_group,
            }
            for clip in project.clips
        ],
        "sources": [
            {
                "asset_id": source.asset_id,
                "stream_index": source.stream_index,
                "kind": source.kind.value,
                "origin": encode_fraction(source.origin),
                "end": encode_fraction(source.end),
                "conservative_end": source.conservative_end,
                "frame_rate": encode_fraction(source.frame_rate),
                "sample_rate": source.sample_rate,
                "channel_layout": source.channel_layout,
                "width": source.width,
                "height": source.height,
                "input_start": encode_fraction(source.input_start),
                "stream_position": source.stream_position,
            }
            for source in project.sources
        ],
    }


def decode_project(body: dict[str, Any]) -> Project:
    for section in ("output", "tracks", "clips", "sources"):
        _require(body, section, "document")
    # This takes the whole document: sections owned by other decoders (assets,
    # navigation, application) are listed here so they are not called unknown.
    reject_unknown_fields(
        body,
        {"schema_version", "application", "output", "tracks", "clips", "sources",
         "assets", "navigation"},
        "document",
    )

    application = body.get("application")
    if application is not None:
        if not isinstance(application, dict):
            raise ProjectFileError("'application' must be an object")
        reject_unknown_fields(application, {"name", "version"}, "application")
        name = application.get("name")
        if name != SCHEMA_NAME:
            raise ProjectFileError(
                f"'application.name' is {name!r}, not {SCHEMA_NAME!r}: this is not a "
                "video-editor project file"
            )

    out_raw = body["output"]
    if not isinstance(out_raw, dict):
        raise ProjectFileError("'output' must be an object")
    reject_unknown_fields(
        out_raw,
        {"width", "height", "frame_rate", "sample_rate", "channel_layout"},
        "output",
    )
    try:
        output = OutputSettings(
            width=_require_int(out_raw, "width", "output"),
            height=_require_int(out_raw, "height", "output"),
            frame_rate=decode_fraction(
                _require(out_raw, "frame_rate", "output"), "output.frame_rate"
            ),
            sample_rate=_require_int(out_raw, "sample_rate", "output"),
            channel_layout=_require_str(out_raw, "channel_layout", "output"),
        )
    except (ValueError, ZeroDivisionError) as exc:
        raise ProjectFileError(f"output settings are invalid: {exc}") from exc

    project = Project(output=output)

    tracks_raw = body["tracks"]
    if not isinstance(tracks_raw, list):
        raise ProjectFileError("'tracks' must be a list")
    tracks: list[Track] = []
    seen_tracks: set[str] = set()
    for position, raw in enumerate(tracks_raw):
        where = f"tracks[{position}]"
        if not isinstance(raw, dict):
            raise ProjectFileError(f"{where} must be an object")
        reject_unknown_fields(raw, {"id", "kind", "name", "enabled", "muted"}, where)
        kind_value = _require_str(raw, "kind", where)
        try:
            kind = TrackKind(kind_value)
        except ValueError:
            raise ProjectFileError(
                f"{where}.kind must be 'video' or 'audio', got {kind_value!r}"
            ) from None
        track = Track(
            id=_require_str(raw, "id", where),
            kind=kind,
            name=_require_str(raw, "name", where),
            enabled=_opt_bool(raw, "enabled", where, True),
            muted=_opt_bool(raw, "muted", where, False),
        )
        if track.id in seen_tracks:
            raise ProjectFileError(f"{where}.id {track.id!r} is duplicated in this file")
        seen_tracks.add(track.id)
        tracks.append(track)
    project = replace(project, tracks=tuple(tracks))

    sources_raw = body["sources"]
    if not isinstance(sources_raw, list):
        raise ProjectFileError("'sources' must be a list")
    sources: list[StreamSource] = []
    seen_sources: set[tuple[str, int]] = set()
    for position, raw in enumerate(sources_raw):
        where = f"sources[{position}]"
        if not isinstance(raw, dict):
            raise ProjectFileError(f"{where} must be an object")
        reject_unknown_fields(
            raw,
            {
                "asset_id",
                "stream_index",
                "kind",
                "origin",
                "end",
                "conservative_end",
                "frame_rate",
                "sample_rate",
                "channel_layout",
                "width",
                "height",
                "input_start",
                "stream_position",
            },
            where,
        )
        kind_value = _require_str(raw, "kind", where)
        try:
            kind = TrackKind(kind_value)
        except ValueError:
            raise ProjectFileError(
                f"{where}.kind must be 'video' or 'audio', got {kind_value!r}"
            ) from None
        try:
            source = StreamSource(
                asset_id=_require_str(raw, "asset_id", where),
                stream_index=_require_int(raw, "stream_index", where),
                kind=kind,
                origin=decode_fraction(_require(raw, "origin", where), f"{where}.origin"),
                end=_optional_fraction(raw.get("end"), f"{where}.end"),
                conservative_end=_opt_bool(raw, "conservative_end", where, False),
                frame_rate=_optional_fraction(raw.get("frame_rate"), f"{where}.frame_rate"),
                sample_rate=raw.get("sample_rate"),
                channel_layout=raw.get("channel_layout"),
                width=raw.get("width"),
                height=raw.get("height"),
                input_start=_optional_fraction(
                    raw.get("input_start", "0/1"), f"{where}.input_start"
                )
                or ZERO,
                stream_position=raw.get("stream_position"),
            )
        except (ValueError, ZeroDivisionError) as exc:
            raise ProjectFileError(f"{where} is invalid: {exc}") from exc
        if source.key in seen_sources:
            raise ProjectFileError(
                f"{where} duplicates asset {source.asset_id!r} stream "
                f"{source.stream_index}"
            )
        seen_sources.add(source.key)
        sources.append(source)
    project = replace(project, sources=tuple(sources))

    if "navigation" in body:
        # validated here too, so a caller decoding a bare project body still
        # hears about an unparseable navigation section
        decode_navigation(body["navigation"])
    clips_raw = body["clips"]
    if not isinstance(clips_raw, list):
        raise ProjectFileError("'clips' must be a list")
    clips: list[Clip] = []
    seen_clips: set[str] = set()
    for position, raw in enumerate(clips_raw):
        where = f"clips[{position}]"
        if not isinstance(raw, dict):
            raise ProjectFileError(f"{where} must be an object")
        reject_unknown_fields(
            raw,
            {
                "id",
                "asset_id",
                "stream_index",
                "track_id",
                "source_in",
                "source_out",
                "output_start",
                "rate",
                "gain",
                "fade_in",
                "fade_out",
                "link_group",
            },
            where,
        )
        try:
            clip = Clip(
                id=_require_str(raw, "id", where),
                asset_id=_require_str(raw, "asset_id", where),
                stream_index=_require_int(raw, "stream_index", where),
                track_id=_require_str(raw, "track_id", where),
                source_in=decode_fraction(
                    _require(raw, "source_in", where), f"{where}.source_in"
                ),
                source_out=decode_fraction(
                    _require(raw, "source_out", where), f"{where}.source_out"
                ),
                output_start=decode_fraction(
                    _require(raw, "output_start", where), f"{where}.output_start"
                ),
                rate=_optional_fraction(raw.get("rate", "1/1"), f"{where}.rate") or Fraction(1),
                gain=_optional_fraction(raw.get("gain", "1/1"), f"{where}.gain") or Fraction(1),
                fade_in=_optional_fraction(raw.get("fade_in", "0/1"), f"{where}.fade_in") or ZERO,
                fade_out=_optional_fraction(
                    raw.get("fade_out", "0/1"), f"{where}.fade_out"
                )
                or ZERO,
                link_group=raw.get("link_group"),
            )
        except (ValueError, ZeroDivisionError) as exc:
            raise ProjectFileError(f"{where} is invalid: {exc}") from exc
        if clip.id in seen_clips:
            raise ProjectFileError(f"{where}.id {clip.id!r} is duplicated in this file")
        seen_clips.add(clip.id)
        clips.append(clip)
    return project.with_clips(tuple(clips))


# ------------------------------------------------------------------ documents


def encode_document(
    project: Project,
    assets: list[dict[str, Any]],
    navigation: Navigation,
    application_version: str,
) -> dict[str, Any]:
    """The complete on-disk document (order is fixed, for readable diffs)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "application": {"name": SCHEMA_NAME, "version": application_version},
        **encode_project(project),
        "assets": assets,
        "navigation": encode_navigation(navigation),
    }


def decode_version(raw_document: object) -> int:
    """Read the schema version and decide whether this build can go further."""
    if not isinstance(raw_document, dict):
        raise ProjectFileError("project file must contain a JSON object at the top level")
    if "schema_version" not in raw_document:
        raise ProjectFileError(
            "project file has no 'schema_version': refusing to guess a format"
        )
    version = raw_document["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ProjectFileError(f"'schema_version' must be an integer, got {version!r}")
    if version > SCHEMA_VERSION:
        keys = ", ".join(sorted(raw_document))
        raise ProjectFileError(
            f"project file uses schema version {version}; this build reads up to "
            f"{SCHEMA_VERSION}. Fields present: {keys}. It was not opened and not "
            "modified — use a newer build of the editor."
        )
    if version < SCHEMA_VERSION and version not in MIGRATIONS:
        raise ProjectFileError(
            f"project file uses schema version {version}, for which this build has "
            "no migration; it was not opened and not modified"
        )
    return version
