# TASK_12i — Build a slow, independent picture reference for diagnosis

Prerequisite: TASK_12h. Scope: tests and a small developer-only diagnostic helper; do not replace the production renderer in this task.

## Fixed contract
For frame index i, compute t=i/R and the winning clip at t. Wanted source time = source_in+(t-output_start)*rate. Choose the last decoded PTS <= wanted within the legal [source_in,source_out) slice. If wanted precedes its first legal PTS, hold the first; after the last legal picture (within genuinely available material), hold the last. A slice with no legal picture is a clear error. Never fill known missing footage with a long accidental hold.

Reviewer decision: keep the preceding-frame rule, not nearest. No blanket 1.1% error allowance. The only permitted mathematical rounding residual is a documented backend timestamp-quantisation tie near a real frame boundary, provided the picture is legal and all production paths pick **the same** frame at the same global index. A tie must be quantified by the timestamp tolerance, not by number of failed tests. Exact source inspection is not subject to this residual.

## Work
Reuse the independently decoded timestamps and identities of TASK_12b. For short fixtures implement a tiny explicit oracle, independent of picture_sampling and FFmpeg fps. It can decode all frames once and select identities with bisect; no full-film decode in production. Also implement a diagnostic comparison: decode/render from one fixed legal clip origin and trim by global frame indices (reference for phase), versus independently sought ranges. Record failing indices, wanted source time, candidate real PTS, raw chain arguments and observed identity. Measure a matrix of rate 1, 25/24, 24/25, 1000/1001, 4 and 1/4, fractional in/out, B-frames, VFR, nonzero origin. Reuse generated fixtures and metadata; do not generate hundreds of redundant files.

## Acceptance / stop
Make the diagnostics fast enough to rerun and verify its expected IDs with hand-worked examples (TASK_12b). Produce small reproductions of actual phase mismatches and distinguish tail holds from retiming error. Report whether using a stable origin+trim removes the mismatches. Do not rewrite the whole renderer or enshrine backend output as the mathematical oracle. This is an evidence milestone; red production output is expected.
