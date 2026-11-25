"""``ffprobe`` JSON adapter.

Runs ffprobe with a subprocess argument list (never a shell string), parses
the JSON defensively, and converts the typed model in
``video_editor.media.assets``. Never raises for bad media: callers receive
``ProbeResult.error`` and may still display the file.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from .assets import AssetMetadata, StreamKind, StreamMetadata

PROBE_TIMEOUT_SECONDS = 120


class ProbeCancelled(Exception):
    """Raised internally when a probe is cancelled; surfaces as an error string."""


@dataclass(frozen=True)
class ProbeResult:
    path: Path
    metadata: AssetMetadata | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.metadata is not None and self.error is None


def _to_fraction(value: Any) -> Fraction | None:
    """Parse ffprobe rationals/decimals exactly. 'N/A', '' and junk -> None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        # ffprobe emits strings for rationals; floats only appear from odd
        # backends. Decimal representation keeps the value exact as printed.
        return Fraction(str(value))
    text = str(value).strip()
    if not text or text in {"N/A", "none", "0/0", "inf", "-inf"}:
        return None
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _language(raw: dict[str, Any]) -> str | None:
    tags = raw.get("tags") or {}
    lang = tags.get("language") or raw.get("language")
    if isinstance(lang, str) and lang.strip() and lang.strip().lower() not in {"und", "unknown"}:
        return lang.strip()
    return None


def parse_probe_document(document: Any) -> AssetMetadata:
    """Convert a parsed ffprobe JSON document to ``AssetMetadata``.

    Raises ``ValueError`` when the document is structurally unusable (not a
    dict, streams missing). Individual stream problems skip or downgrade that
    stream instead of failing the whole asset.
    """
    if not isinstance(document, dict):
        raise ValueError("ffprobe output is not a JSON object")
    raw_streams = document.get("streams")
    if not isinstance(raw_streams, list):
        raise ValueError("ffprobe output has no 'streams' list")

    fmt = document.get("format") or {}
    container_duration = _to_fraction(fmt.get("duration"))
    container_start = _to_fraction(fmt.get("start_time"))
    format_name = fmt.get("format_name") or None
    # Conservative horizon: no packet can present after the container ends.
    end_horizon: Fraction | None = None
    if container_duration is not None and container_duration > 0:
        end_horizon = (container_start or Fraction(0)) + container_duration

    streams: list[StreamMetadata] = []
    for raw in raw_streams:
        if not isinstance(raw, dict):
            continue
        index = _to_int(raw.get("index"))
        if index is None:
            continue
        codec_type = (raw.get("codec_type") or "").lower()
        kind = {
            "video": StreamKind.VIDEO,
            "audio": StreamKind.AUDIO,
        }.get(codec_type, StreamKind.OTHER)

        start = _to_fraction(raw.get("start_time"))
        duration = _to_fraction(raw.get("duration"))
        duration_source = "probe" if duration is not None else None

        time_base = _to_fraction(raw.get("time_base"))
        nb_frames = _to_int(raw.get("nb_frames"))
        frame_rate = _to_fraction(raw.get("avg_frame_rate"))
        nominal = _to_fraction(raw.get("r_frame_rate"))

        sample_rate = _to_int(raw.get("sample_rate"))
        nb_samples = _to_int(raw.get("nb_samples"))

        if duration is None:
            # Derive only from reliable counts, never from the container.
            if kind is StreamKind.VIDEO and nb_frames and frame_rate and frame_rate > 0:
                duration = Fraction(nb_frames) / frame_rate
                duration_source = "derived:nb_frames/rate"
            elif kind is StreamKind.AUDIO and nb_samples and sample_rate:
                duration = Fraction(nb_samples, sample_rate)
                duration_source = "derived:nb_samples/rate"

        streams.append(
            StreamMetadata(
                stream_index=index,
                kind=kind,
                codec=raw.get("codec_name"),
                language=_language(raw),
                frame_rate=frame_rate if frame_rate and frame_rate > 0 else None,
                nominal_frame_rate=nominal if nominal and nominal > 0 else None,
                width=_to_int(raw.get("width")),
                height=_to_int(raw.get("height")),
                sample_rate=sample_rate,
                channels=_to_int(raw.get("channels")),
                channel_layout=raw.get("channel_layout"),
                start_time=start,
                duration=duration,
                time_base=time_base,
                nb_frames=nb_frames,
                duration_source=duration_source,
                end_horizon=end_horizon,
            )
        )

    if not streams:
        raise ValueError("ffprobe found no streams")

    return AssetMetadata(
        format_name=format_name,
        container_duration=container_duration,
        container_start=container_start,
        streams=tuple(streams),
    )


def parse_probe_output(stdout: str) -> ProbeResult | None:
    """Parse raw ffprobe stdout. Returns None when JSON is malformed."""
    try:
        document = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        metadata = parse_probe_document(document)
    except ValueError as exc:
        return ProbeResult(path=Path(), metadata=None, error=f"Unusable probe data: {exc}")
    return ProbeResult(path=Path(), metadata=metadata, error=None)


def build_probe_command(ffprobe_path: str, path: Path) -> list[str]:
    return [
        ffprobe_path,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]


def probe_file(path: Path, ffprobe_path: str = "ffprobe") -> ProbeResult:
    """Probe one file. Always returns a result; never raises for bad media."""
    import shutil

    exe = shutil.which(ffprobe_path)
    if exe is None:
        return ProbeResult(path=path, metadata=None, error=f"{ffprobe_path} not found on PATH")
    cmd = build_probe_command(exe, path)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(path=path, metadata=None, error="ffprobe timed out")
    except OSError as exc:
        return ProbeResult(path=path, metadata=None, error=f"Could not run ffprobe: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        hint = detail[-1] if detail else f"exit status {proc.returncode}"
        return ProbeResult(path=path, metadata=None, error=f"ffprobe failed: {hint}")
    parsed = parse_probe_output(proc.stdout)
    if parsed is None:
        return ProbeResult(path=path, metadata=None, error="ffprobe returned malformed JSON")
    return ProbeResult(path=path, metadata=parsed.metadata, error=parsed.error)
