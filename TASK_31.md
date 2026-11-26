# TASK_31 — Key preview renders by the resolved content of their range

Prerequisite: TASK_30. Scope: render/cache.py fingerprint and tests; no eviction or player wiring changes.

## Work
Fingerprint a canonical representation of the resolved range plan: output settings, exact global frame/sample indices, geometry scale, relevant clip/source mapping (including phase/preroll, fades/tempo whole-clip context), renderer semantics version, and explicit (asset ID, recorded + checked identity) pairs. Only ignore labels, link IDs, selection, distant edits and unused media when rendered content is provably unaffected. Include policy/format fields that affect output. Use conservative full-snapshot fallback if unsure. Do not map assets merely by sorted list of identity values. Version change invalidates old keys. Never reuse if participating asset changed/unavailable.

## Acceptance
Same range, distant edit and rename/link edit truly unaffected => same key; overlap, stack/mute/rate/fade/output settings/asset mapping/identity change => changed key or refusal. Swap two identity values across distinct asset IDs and verify different. Boundary/whole-clip-context changes that influence a retimed range invalidate. Partial range independent of unused offline asset. Decode small reference to prove safe reuse. Run cache and preview tests; no forced eviction yet.
