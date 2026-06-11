# Raw Controls & Live Ops Monitor — Audit (2026-06-11)

User report: clicking Raw Controls buttons shows only generic progress; monitor showed
"scheduler skipped (duplicate guard): backfill jd ×60".

## A. Per-button truth table

| Button | Handler | Tracking | Verdict |
|---|---|---|---|
| Full Fetch Jobs | TriggerBatchFetchView (views.py:2114) | FetchBatch + progress + audit | ✅ good |
| Quick Fetch 25h | TriggerBatchFetchView:2047 | FetchBatch | ✅ good |
| Run Detection | RunDetectNowView:719 | HarvestOpsRun | ✅ good |
| Backfill JD | RunBackfillDescriptionsView:2407 | HarvestOpsRun (heartbeat, stale @90min) | ⚠ see B |
| Validate Live Links | RunValidateRawUrlsView:874 | HarvestOpsRun (stale @45min) | ✅ |
| Sync Qualified to Vet | RunSyncNowView:773 | HarvestOpsRun | ✅ |
| **Retry Failed** | RunRetryFailedFetchesView:816 | **NONE — fire-and-forget .delay()** | 🔴 invisible failures |
| Cleanup | RunCleanupNowView:934 | HarvestOpsRun | ✅ |
| **Classify Jobs** | ClassifyJobsTriggerView (jobs/views.py) | **cache lock only, NO ops run** | 🔴 invisible to monitor |
| **Force Re-classify** | same, force=1 | same | 🔴 invisible |

## B. The "duplicate guard ×60" mechanics
- `_acquire_ops_singleton` (harvest/tasks.py:71-135): if a RUNNING ops-run exists for the
  operation, the new run is recorded SKIPPED (`reason: duplicate_active_operation`).
- Backfill JD is scheduled hourly; its stale cutoff is 90 min (views.py:1660). A run that
  dies WITHOUT heartbeats blocks ~1-2 cycles. **A hung worker that keeps heartbeating
  blocks forever → ×60 skips = something held the guard for ~2.5 days.**
- Gap: no max-runtime ceiling; heartbeat alone decides liveness.

## C. Monitor feed gaps (OpsRunLiveApiView, views.py:1642-1913)
Returned: status, progress current/total/message, elapsed, completion, stuck_warning.
NOT returned (exists on the model): full `audit_payload` (queue params, stale metadata,
per-phase counts), `triggered_by_user`. Classify runs never appear at all.

## D. Fix plan (next session, in order)
1. **Retry Failed → wrap in HarvestOpsRun** (begin/finish around retry_failed_raw_jobs_task)
   so failures surface. Small, contained.
2. **Classify Jobs / Force Re-classify → create a HarvestOpsRun** (operation CLASSIFY) so
   they appear in the monitor like everything else.
3. **Max-runtime ceiling for the duplicate guard**: if a RUNNING run is older than
   N×expected duration (e.g. 4h for backfill), mark PARTIAL and release the guard even if
   heartbeats continue.
4. **Monitor detail expansion**: per-run expandable row in the Live Ops Monitor showing
   audit_payload (params it ran with, per-phase counts, skip reasons) + triggered_by —
   this is the "show me exactly what it's doing" ask.
5. Check prod now: `HarvestOpsRun.objects.filter(status="RUNNING")` — find what held the
   backfill guard for the ×60 window; if a zombie, kill + let release_stale_jd_locks clear it.
