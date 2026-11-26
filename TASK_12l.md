# TASK_12l — Explicit handling of unimplemented interlaced and HDR inputs

Prerequisite: TASK_12k. Scope: format capability preflight and user-facing refusal; no deinterlacer, tone mapper or new delivery codec.

## Work
Probe video field_order, color_transfer and relevant HDR/colour flags. Preserve metadata where practical without breaking schema v1 (optional fields or reprobe during capability preflight). Explicitly refuse known interlaced material in rendering/derived stills/preview until deinterlacing is implemented; refuse known HDR (PQ/SMPTE2084 or HLG/ARIB-STD-B67) in the SDR yuv420p delivery pipeline until supported. Source import may show the metadata and explain why processing is unavailable. An unknown flag is not proof of SDR/progressive; expose uncertainty (warning unless cannot decode safely). Keep ordinary SDR square-pixel/anamorphic renders unaffected. Do not add gamma/tone-map assumptions or treat 10-bit alone as proof of HDR.

## Acceptance
Generate short progressive SDR, interlaced and HDR-tagged synthetic fixtures, independently confirm metadata tags before asserting behaviour. Render/preview/still of unsupported participating picture fail with clear reason; unused unsupported streams do not block a valid range. Reopening existing saved documents still works or prompts for reprobe, not silent downgrade. Run metadata and render preflight tests.
