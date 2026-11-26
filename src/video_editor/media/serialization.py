"""Asset records in JSON: identity and probe metadata, exactly as recorded.

Asset metadata is stored rather than re-probed on load, for two reasons:
a project whose media is on another disk must still be inspectable, and the
recorded values are the baseline that "this file changed under me" is measured
against. Loading therefore never silently re-probes and adopts new numbers.

Every key written here is read back, and unknown keys are rejected (see
:mod:`video_editor.project.serialize` for the schema and migration policy). The
picture-format keys (``field_order``, the colour tags, ``pix_fmt``,
``side_data``, ``bit_depth``) are **optional and additive**: a document written
before they existed omits them and decodes with them unknown, so such a document
is not evidence that its media is progressive or SDR — the render path measures
the file again (see :mod:`video_editor.media.capability`).
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

from ..core.model import StreamSource
from ..project.serialize import (
    ProjectFileError,
    decode_fraction,
    encode_fraction,
    reject_unknown_fields,  # shared strictness: unknown fields are refused
)
from .assets import Asset, AssetMetadata, StreamKind, StreamMetadata

_STREAM_FIELDS = {
    "stream_index",
    "kind",
    "codec",
    "language",
    "frame_rate",
    "nominal_frame_rate",
    "width",
    "height",
    "sample_rate",
    "channels",
    "channel_layout",
    "start_time",
    "duration",
    "time_base",
    "nb_frames",
    "duration_source",
    "end_horizon",
    # optional additive keys (schema v1): a document written before they existed
    # simply omits them, and an omitted value decodes as unknown, never as a
    # default. See ``video_editor.project.serialize`` "Migration policy".
    "field_order",
    "color_transfer",
    "color_primaries",
    "color_space",
    "pix_fmt",
    "side_data",
    "bit_depth",
}
_METADATA_FIELDS = {"format_name", "container_duration", "container_start", "streams"}
_ASSET_FIELDS = {
    "id",
    "path_mode",
    "path",
    "absolute_path",
    "size_bytes",
    "mtime_ns",
    "label",
    "probe_error",
    "metadata",
}


# ----------------------------------------------------------------- small helpers


def _int(raw: dict, key: str, where: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectFileError(f"{where}.{key} must be an integer, got {value!r}")
    return value


def _int_or_none(raw: dict, key: str, where: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    return _int(raw, key, where)


def _string_or_none(raw: dict, key: str, where: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProjectFileError(f"{where}.{key} must be a string, got {value!r}")
    return value


def _string_tuple(raw: dict, key: str, where: str) -> tuple[str, ...]:
    """Side-data names: an absent key is "none were recorded", never "unknown".

    A stored document holds what was seen; the render-time preflight re-measures,
    so nothing here may be read back as a claim that a stream is safe to draw.
    """
    value = raw.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ProjectFileError(f"{where}.{key} must be a list of strings, got {value!r}")
    return tuple(value)


def _fraction(raw: dict, key: str, where: str) -> Fraction | None:
    value = raw.get(key)
    if value is None:
        return None
    return decode_fraction(value, f"{where}.{key}")


# --------------------------------------------------------------------- streams


def encode_stream(stream: StreamMetadata) -> dict[str, Any]:
    return {
        "stream_index": stream.stream_index,
        "kind": stream.kind.value,
        "codec": stream.codec,
        "language": stream.language,
        "frame_rate": encode_fraction(stream.frame_rate),
        "nominal_frame_rate": encode_fraction(stream.nominal_frame_rate),
        "width": stream.width,
        "height": stream.height,
        "sample_rate": stream.sample_rate,
        "channels": stream.channels,
        "channel_layout": stream.channel_layout,
        "start_time": encode_fraction(stream.start_time),
        "duration": encode_fraction(stream.duration),
        "time_base": encode_fraction(stream.time_base),
        "nb_frames": stream.nb_frames,
        "duration_source": stream.duration_source,
        "end_horizon": encode_fraction(stream.end_horizon),
        "field_order": stream.field_order,
        "color_transfer": stream.color_transfer,
        "color_primaries": stream.color_primaries,
        "color_space": stream.color_space,
        "pix_fmt": stream.pix_fmt,
        "side_data": list(stream.side_data),
        "bit_depth": stream.bit_depth,
    }


def decode_stream(raw: Any, where: str) -> StreamMetadata:
    if not isinstance(raw, dict):
        raise ProjectFileError(f"{where} must be an object")
    reject_unknown_fields(raw, _STREAM_FIELDS, where)
    kind_value = raw.get("kind")
    if kind_value not in {kind.value for kind in StreamKind}:
        raise ProjectFileError(
            f"{where}.kind must be one of video/audio/other, got {kind_value!r}"
        )
    try:
        return StreamMetadata(
            stream_index=_int(raw, "stream_index", where),
            kind=StreamKind(kind_value),
            codec=_string_or_none(raw, "codec", where),
            language=_string_or_none(raw, "language", where),
            frame_rate=_fraction(raw, "frame_rate", where),
            nominal_frame_rate=_fraction(raw, "nominal_frame_rate", where),
            width=_int_or_none(raw, "width", where),
            height=_int_or_none(raw, "height", where),
            sample_rate=_int_or_none(raw, "sample_rate", where),
            channels=_int_or_none(raw, "channels", where),
            channel_layout=_string_or_none(raw, "channel_layout", where),
            start_time=_fraction(raw, "start_time", where),
            duration=_fraction(raw, "duration", where),
            time_base=_fraction(raw, "time_base", where),
            nb_frames=_int_or_none(raw, "nb_frames", where),
            duration_source=_string_or_none(raw, "duration_source", where),
            end_horizon=_fraction(raw, "end_horizon", where),
            field_order=_string_or_none(raw, "field_order", where),
            color_transfer=_string_or_none(raw, "color_transfer", where),
            color_primaries=_string_or_none(raw, "color_primaries", where),
            color_space=_string_or_none(raw, "color_space", where),
            pix_fmt=_string_or_none(raw, "pix_fmt", where),
            side_data=_string_tuple(raw, "side_data", where),
            bit_depth=_int_or_none(raw, "bit_depth", where),
        )
    except (ValueError, ZeroDivisionError) as exc:
        raise ProjectFileError(f"{where} is invalid: {exc}") from exc


def encode_metadata(metadata: AssetMetadata) -> dict[str, Any]:
    return {
        "format_name": metadata.format_name,
        "container_duration": encode_fraction(metadata.container_duration),
        "container_start": encode_fraction(metadata.container_start),
        "streams": [encode_stream(stream) for stream in metadata.streams],
    }


def decode_metadata(raw: Any, where: str) -> AssetMetadata:
    if not isinstance(raw, dict):
        raise ProjectFileError(f"{where} must be an object")
    reject_unknown_fields(raw, _METADATA_FIELDS, where)
    streams = raw.get("streams")
    if not isinstance(streams, list):
        raise ProjectFileError(f"{where}.streams must be a list")
    return AssetMetadata(
        format_name=_string_or_none(raw, "format_name", where),
        container_duration=_fraction(raw, "container_duration", where),
        container_start=_fraction(raw, "container_start", where),
        streams=tuple(
            decode_stream(item, f"{where}.streams[{position}]")
            for position, item in enumerate(streams)
        ),
    )


# ---------------------------------------------------------------------- assets


def encode_asset(asset: Asset, path_mode: str, path: str, absolute_path: str) -> dict[str, Any]:
    """One asset: stable id, reference, stat identity and recorded metadata."""
    return {
        "id": asset.id,
        "path_mode": path_mode,
        "path": path,
        "absolute_path": absolute_path,
        "size_bytes": asset.size_bytes,
        "mtime_ns": asset.mtime_ns,
        "label": asset.label or None,
        "probe_error": asset.probe_error,
        "metadata": encode_metadata(asset.metadata) if asset.metadata else None,
    }


def decode_asset(raw: Any, where: str) -> tuple[Asset, str, str]:
    """Decode one asset record.

    Returns ``(asset, path_mode, path)``: the asset carries the *absolute*
    reference as recorded, while the storage layer resolves ``path_mode``/``path``
    against the project directory and replaces it if the reference differs.
    """
    if not isinstance(raw, dict):
        raise ProjectFileError(f"{where} must be an object")
    reject_unknown_fields(raw, _ASSET_FIELDS, where)
    path_mode = raw.get("path_mode")
    if path_mode not in {"relative", "absolute"}:
        raise ProjectFileError(
            f"{where}.path_mode must be 'relative' or 'absolute', got {path_mode!r}"
        )
    for key in ("id", "path", "absolute_path"):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            raise ProjectFileError(f"{where}.{key} must be a non-empty string")
    metadata_raw = raw.get("metadata")
    probe_error = raw.get("probe_error")
    if probe_error is not None and not isinstance(probe_error, str):
        raise ProjectFileError(f"{where}.probe_error must be a string or null")
    try:
        asset = Asset(
            id=str(raw["id"]),
            path=Path(str(raw["absolute_path"])),
            size_bytes=_int_or_none(raw, "size_bytes", where),
            mtime_ns=_int_or_none(raw, "mtime_ns", where),
            metadata=decode_metadata(metadata_raw, f"{where}.metadata")
            if metadata_raw is not None
            else None,
            probe_error=probe_error,
            label=_string_or_none(raw, "label", where) or "",
        )
    except (ValueError, ZeroDivisionError) as exc:
        raise ProjectFileError(f"{where} is invalid: {exc}") from exc
    return asset, str(path_mode), str(raw["path"])


# --------------------------------------------------------------------- sources


def encode_source(source: StreamSource) -> dict[str, Any]:
    """A registered clip-material record (kept separate from probe metadata)."""
    return {
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
