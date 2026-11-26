# TASK_30 — Give lossless staging a bounded, honest disk policy

Prerequisite: TASK_29. Scope: exporter estimate and owned intermediate storage; not new delivery codecs.

## Work
Measure FFV1/PCM intermediate size on short representative synthetic 1080p/4K and multiple channel counts; current whole-film intermediate can be prohibitive. Calculate conservative upper-bound or clearly labelled realistic estimate including picture dimensions/count, sample count/layout, active pieces, final output and filesystem reserve. Never assume compressed bitrate as hard guarantee. Check capacity before work and between pieces; handle ENOSPC by cleanup with actionable diagnostic and preserve old destination. If whole-film staging too costly, use safe bounded batches/streaming to a single final encoder only if validated for phase, progress, cancellation and no lossy independent chunks/AAC priming. A clearly documented refusal is acceptable for insufficient disk; do not ship an untested streaming strategy. Record a practical next strategy if refused.

## Acceptance
Fake disk budgets plus short real forced-chunk render/decode; low disk before stage and exhausted mid-run leave original output untouched, owned files removed. Estimate accounts for multichannel and final output. If bounded strategy implemented, measure peak disk vs whole-staging and prove same frame/sample joins. No feature-length encode required; report extrapolation assumptions and remaining limits.
