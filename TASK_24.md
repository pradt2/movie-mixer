# TASK_24 — Measure the long-project bottlenecks before optimising

Prerequisite: TASK_23. Benchmark and tests only; no optimisations.

## Work
Extend existing long_project_check with deterministic synthetic 2-hour projects of 1k, 5k and 10k clips (mixed rates, stacked tracks, overlaps across tracks) without encoding an entire film. Separate point/range query, plan build, cache fingerprint, timeline paint, worker latency and memory. Include a short actual FFmpeg render of many short audio clips and dense overlaps; measure graph input count/argument size. Include a short preview near end of long audio clip after TASK_11's from-clip-start processing; quantify work relative to returned window. Measure disk estimate vs actual short lossless output. Record reproducible commands, hardware/tool versions and actual values, and profile likely hot functions. Reuse fixtures rather than thousands of independent media files.

## Acceptance / stop
Provide baseline table, profiling command/scripts, and simple brute-force oracle for later comparisons. Establish scaling with operation counts where possible, not fragile timing thresholds. Identify bottlenecks that are actually measured. No claims of speedup and no implementation of later tasks. If a later suggested optimisation is unnecessary, retain its correctness checks and report that fact.
