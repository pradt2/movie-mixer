# TASK_28b — Avoid repeatedly processing whole audio clips for short ranges

Prerequisite: TASK_28. Scope: audio slice reuse or bounded audio preroll; no picture/cache-key/chunk-partition rewrite.

## Decision
Reference is the already correct contiguous clip-start processing for supported PCM. Prefer caching/reusing one contiguous lossless processed slice within a render job, then cut exact integer output samples for each chunk. Across independent preview jobs use a verified bounded preroll/overlap policy only if it meets measured join tolerance; if not, clearly label and cap the slow fallback or refuse overly expensive requests. Do not silently sacrifice sync or fades for speed.

## Work
Use TASK_28's smallest measured failure/slow example. Preserve global phase, sample offsets, fade envelope and gain. Remove repeated whole-clip processing for chunks where supported. If using reusable slice, it's a job-owned temporary with bounded lifetime, not a new user media asset; cancellation/cleanup from TASK_16 still applies. If a codec/rate cannot safely use fast path, choose conservative fallback/refusal and give precise actionable limitation. Do not implement a new audio engine.

## Acceptance
Compare PCM and lossy joins/late ranges with TASK_28 reference. Exact count and fade placement, no extra gaps/priming; retain PCM exact regression, document verified numeric lossy tolerance. Measure actual extent repeated over multiple late ranges, disk/time cost, effect of cancellation. Preview returns promptly or honestly refuses known costly case. If the strategy itself remains unresolved, pause with evidence and ask reviewer rather than iterate speculative rewrites.
