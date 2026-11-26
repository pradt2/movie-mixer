"""Preview range selection and cache-aware preparation (no Qt here).

A preview is an ordinary render with two additions: it is rendered at a reduced
picture size (``geometry_scale``) with a faster encoder preset, and it is written
into a bounded cache instead of to a user-chosen destination. Composition
semantics — stacking, gaps, black/silence fill, mixing, quantisation — come from
the same planner as export, which is the whole point of a preview.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ..core.model import Project
from ..fs.identity import Identity
from ..fs.protection import DestinationGuard
from .cache import PreviewCache, render_fingerprint
from .exporter import RenderRequest
from .graph import PREVIEW_H264_FAST, CodecPreset
from .plan import Quantisation, RenderPlan, build_plan

F = Fraction

#: Default length of the window rendered around the playhead. Long enough to hear
#: a cut and see the next shot, short enough to render quickly.
DEFAULT_PREVIEW_SECONDS = F(10)
#: Upper bound the UI is expected to offer: a preview is not a full render.
MAX_PREVIEW_SECONDS = F(120)
#: Below this a preview would be a single frame and tell you nothing.
MIN_PREVIEW_FRAMES = 2

#: Default preview picture scale: half size stays legible while rendering quickly.
HALF = Fraction(1, 2)


class PreviewError(Exception):
    """No sensible preview range exists for this request."""


@dataclass(frozen=True)
class PreviewRange:
    """Half-open output range, snapped to whole output frames."""

    start: Fraction
    end: Fraction

    @property
    def duration(self) -> Fraction:
        return self.end - self.start


@dataclass(frozen=True)
class PreviewJob:
    """Everything needed to show or render one preview range."""

    request: RenderRequest
    key: str
    plan: RenderPlan
    #: True when an identical preview is already cached: nothing to render
    reused: bool


def preview_range(
    playhead: Fraction,
    project: Project,
    duration: Fraction = DEFAULT_PREVIEW_SECONDS,
    centre: bool = True,
) -> PreviewRange:
    """A short window around ``playhead``, clamped to the composition.

    Boundaries are snapped to whole output frames so the range edge coincides with
    a rendered frame: preview boundaries then agree with export quantisation
    instead of introducing their own rounding.
    """
    if playhead < 0:
        raise PreviewError("Playhead must be non-negative")
    if duration <= 0:
        raise PreviewError(f"Preview duration {duration} must be positive")
    if duration > MAX_PREVIEW_SECONDS:
        raise PreviewError(
            f"Preview duration {float(duration):.1f}s exceeds the "
            f"{float(MAX_PREVIEW_SECONDS):.0f}s preview limit; export that range instead"
        )
    rate = project.output.frame_rate
    horizon = project.output_end()
    if horizon <= 0:
        raise PreviewError("The composition is empty: there is nothing to preview")

    quant = Quantisation(rate, project.output.sample_rate)
    # any positive output length yields at least one output frame (counts round
    # up), so there is always something honest to preview once the length is > 0
    horizon_frames = quant.frame_count(horizon)
    raw_start = playhead - duration / 2 if centre else playhead
    start_frame = max(0, quant.frame_index(raw_start))
    wanted = max(MIN_PREVIEW_FRAMES, quant.frame_count(duration))
    if start_frame + wanted > horizon_frames:
        # the window would run past the end: walk it back so the requested length
        # is kept, unless the composition itself is shorter than the window
        start_frame = max(0, horizon_frames - wanted)
    end_frame = min(start_frame + wanted, horizon_frames)
    if end_frame - start_frame < MIN_PREVIEW_FRAMES:
        start_frame = max(0, end_frame - MIN_PREVIEW_FRAMES)
    return PreviewRange(start=quant.frame_time(start_frame), end=quant.frame_time(end_frame))


def playback_offset(
    range_start: Fraction, output_time: Fraction, range_end: Fraction
) -> Fraction | None:
    """Where ``output_time`` sits inside a rendered preview (file-local seconds).

    ``None`` when the time is outside the rendered range: the preview file cannot
    show it, and pretending otherwise would play the wrong part of the composition
    while claiming to be at the playhead.
    """
    if output_time < range_start or output_time >= range_end:
        return None
    return output_time - range_start


def prepare_preview(
    project: Project,
    asset_paths: dict[str, Path],
    out_start: Fraction,
    out_end: Fraction,
    cache: PreviewCache,
    preset: CodecPreset = PREVIEW_H264_FAST,
    geometry_scale: Fraction = HALF,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
    asset_identities: dict[str, Identity] | None = None,
    guard: DestinationGuard | None = None,
) -> PreviewJob:
    """Resolve one preview: reuse a cached file, or hand back a render request.

    The cache key covers the composition snapshot, asset identity, range, picture
    scale and preset, so a cached file is reused only for a request that would
    produce the same media. When the render is chunked, sub-ranges are re-planned
    through the same request, so a chunked preview is the same composition as a
    single-pass one. *asset_identities* and *guard* carry the same source checks
    an export gets: a preview of a replaced file is refused rather than shown.
    """
    identities = dict(asset_identities or {})
    protection = guard if guard is not None else DestinationGuard()
    if out_end <= out_start:
        raise PreviewError("Preview range end is before its start")
    key = render_fingerprint(
        project, asset_paths, out_start, out_end, geometry_scale, preset
    )
    plan = build_plan(project, out_start, out_end, geometry_scale)
    if plan.total_frames < 1:
        # a range shorter than one output frame, or one that lies past the end of
        # the composition, has no picture to show: saying so beats rendering a
        # file that looks like a preview and plays nothing
        raise PreviewError(
            f"nothing to preview in {float(out_start):.3f}s to {float(out_end):.3f}s: "
            f"that range holds no output frame at {project.output.frame_rate} fps"
        )
    cached = cache.lookup(key)
    if cached is not None:
        request = RenderRequest(
            project=project,
            asset_paths=asset_paths,
            destination=cached,
            out_start=out_start,
            out_end=out_end,
            preset=preset,
            geometry_scale=geometry_scale,
            overwrite=True,  # cache-owned file; never re-asked, never a user file
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            cache=cache,
            asset_identities=identities,
            guard=protection,
        )
        return PreviewJob(request=request, key=key, plan=plan, reused=True)
    destination = cache.path_for(key)
    request = RenderRequest(
        project=project,
        asset_paths=asset_paths,
        destination=destination,
        out_start=out_start,
        out_end=out_end,
        preset=preset,
        geometry_scale=geometry_scale,
        overwrite=True,  # staging + publication rules in the exporter protect this file
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        cache=cache,
        asset_identities=identities,
        guard=protection,
    )
    return PreviewJob(request=request, key=key, plan=plan, reused=False)
