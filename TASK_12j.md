# TASK_12j — Make whole and independently rendered ranges choose the same pictures

Prerequisite: TASK_12i. Scope: render/plan.py picture-sampling fields and render/graph.py picture filter only, plus integration wiring/tests.

## Work
Correct the *global phase*, not merely the seek's local container tick. The current fps=start_time is expressed relative to a different seek for each range; rounding its grid on each seek can shift the selected frame. Reuse TASK_12i's smallest failing case.

Start with a fixed clip origin and one global output index lattice: retime on a clip/global clock before fps, cut the desired output indices after sampling. Use an exact representable common time base where feasible (verify setpts/time-base precision and rounding with showinfo). The slow fixed-origin reference is a useful fallback for small ranges/short fixtures; it is not permission to decode a two-hour clip from its start during every scrub. For fast range seeks retain enough preceding legal decoded picture(s), restore that **same** absolute phase on the filter clock, then trim to the exact frame count. Use decoded index evidence for sparse VFR; fixed nominal-rate preroll alone is not sufficient. Explicitly bound and document fallback/refusal if phase or legal frame cannot be established. Do not spend this task optimising graph count or rewriting the exporter. Do not alter audio.

Concrete small experiment before modifying production: with a seek at asset-local time S, decoded PTS measured as seconds after that seek, clip in-point I, rate r and output start O, use the transformed absolute timestamp

    output_seconds = ((PTS * TB + S - I) / r) + O

before the fps filter, not PTS/r on the seek-relative clock. In a high-resolution filter time base this is a setpts expression of the form `((PTS*TB+S-I)/r+O)/TB` (substitute rational constants; confirm the actual PTS origin with showinfo first). For a requested global first index k, fps start_time is k/R, **not** (wanted_source-S)/r. Trim to the planned index count and rebase to zero only after the pick. E.g. O=2, I=1, r=25/24: source stamp 1 maps to 2 s whether input seeks at 1 or earlier. Use an exact common representable time base or verify a sufficiently fine one; calculate the worst conversion error at any tie. Enforce legal source bounds by an explicit source-axis trim/selected decoded stamps before resampling; input -t alone is packet-granular, not proof of the exclusive out-point. Do not add a second head pad if fps already holds the first legal frame. Compare this experiment against the fixed-origin reference before integrating. These are candidate implementation mechanics, not a claim that a specific FFmpeg command already passed the matrix.

Keep ceil boundary, legal half-open source slice, edge hold and previous-picture contract from TASK_12d/i. Prefer no offset residual. If FFmpeg's discrete clock requires a tie policy, document numerical tolerance and require range/full equality even in ties. Bump cache semantics.

## Acceptance
- Run TASK_12i's reference comparison and the corrected range-vs-full test; include nonzero slice, nonzero output start, multiple arbitrary splits, fast/slow and tiny fractional rates, VFR and gap/cut. No mismatched picture identities between independent ranges and whole at the same global indices, including the reviewer reproduction (25/24; previously 3/136 differed on FFmpeg 7.1.5).
- Verify legal bounds, exact number of output frames, known tail/empty-source refusal. Do not accept a one-frame disagreement between render paths.
- Measure actual decode work of a short range near the end of a long synthetic source. If it remains slow, report it (TASK_28c addresses bounded reuse), and do not claim responsive scrubbing. Run picture/graph/render-plan tests. If blocked, give the smallest repro, do not proceed to TASK_12k.
