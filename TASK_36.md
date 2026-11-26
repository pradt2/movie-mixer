# TASK_36 — Verify recovery across an actual process crash

Prerequisite: TASK_35. Scope: end-to-end recovery test and minimal isolated fixes if demonstrated.

## Work
Create a subprocess-driven synthetic project test: save baseline project, make unsaved transaction, wait until actual autosave/recovery snapshot written via normal path, SIGKILL child (not graceful close), launch second process, detect recovery offer, choose restore, verify recovered document matches unsaved state while original saved project bytes unchanged. Include asset import-only and relink changes from TASK_10. Use isolated temp directory/cache; avoid production files. Test discard path and that autosave snapshot is not mistaken for normal saved baseline. Test timeout/failure reports child output.

## Acceptance
Successful restore and explicit normal saved original unchanged by byte comparison; repeated start/discard safe, no leftover child. Missing/corrupt recovery handles with actionable status. Run recovery/storage and GUI verification, report actual exit codes. If root cause broad, stop and request narrow corrective task; no general storage rewrite.
