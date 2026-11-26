# TASK_12c — Correct square-pixel display geometry

Prerequisite: TASK_12b. Fix only display geometry in the shared picture chain; no timestamp, audio or worker edits.

The reviewer reproduced the failing anamorphic case with FFmpeg 7.1.5: scaling 720x576 SAR 64:45 with force_original_aspect_ratio=decrease yields 900x720 SAR 64:45. Immediately applying setsar=1 destroys the display shape and padding adds 190-pixel bars. This is not a mysterious seek defect.

## Work
In render/graph.py calculate fitted, even square-pixel dimensions from **display** aspect ratio (DAR), not coded width/height. A portable scale expression for ordinary legal even output dimensions is:
```
scale=w='trunc(min(OW,OH*dar)/2)*2':h='trunc(min(OH,OW/dar)/2)*2',setsar=1,pad=OW:OH:(ow-iw)/2:(oh-ih)/2
```
Substitute OW/OH as numbers; guard extreme ratios/invalid dimensions. Alternatively use a verified reset_sar-capable filter with an explicit supported-version rule. Keep this one shared chain. Do not silently crop/stretch; existing black borders intrinsic to source are retained. Bump cache semantics version.

## Acceptance
- Measured 720x576 SAR 64:45 fits 1280x720 exactly without pillarboxes. A 4:3 source in that output fits 960x720 with 160-pixel pillarboxes. A wider source letterboxes. Check centre and edge pixel locations and output SAR, not just output geometry.
- Check both still and lossless render using real frames; test ordinary square-pixel sources too.
- Run geometry-specific tests, relevant graph/plan tests, ruff. Other baseline picture failures stay visible. No other Task 12 defects in this commit.
