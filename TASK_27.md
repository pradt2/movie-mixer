# TASK_27 — Stop mixing one full-duration padded buffer per audio clip

Prerequisite: TASK_26. Scope: audio graph only; keep source processing, fades and output sample semantics from TASK_11.

## Work
On each audio track concatenate the already processed range-local clip contributions in chronological order plus only its actual gaps and needed tail; mix resulting equal-span track streams. Same-track overlap is illegal already. No per-clip globally padded stream feeding amix. Integer output-sample positions, exact gaps, plain sum across tracks; ensure FFmpeg concatenation/rebase cannot shift sample counts. Keep track order and clipping warning behaviour. Use tested old graph as reference on short fixtures.

## Acceptance
Decoded PCM differential: staggered clips, silence, fades, overlap across tracks, varying source sample rates, fractional positions and retime; matching sample counts and measured samples within documented processing tolerance. Dense short clips (100+) over long synthetic range: compare actual graph branch count/time and memory, confirm no O(items*whole_duration) silence work. Run audio timing/render graph and export tests. Don't change chunk partition yet.
