# async-governed-dispatch — durable run handle (202 + poll) for long operations

## Overview

Async governed dispatch (#3079) lets a caller submit a governed operation
without holding the HTTP connection for the operation's full duration.
`POST /api/v1/operations/call` with `async: true` (and
`POST /api/v1/approvals/{id}/approve` with `async: true`) creates a durable
`operation_run` row, launches the governed dispatch on a background
`asyncio` task, and returns **HTTP 202 + a run handle** immediately. The
caller polls / cancels via the handle; the completed
`OperationResult` envelope is persisted on the row, so it survives a dropped
response.

Motivating incident: an 83s vendor call (OVF library-item deploy) held the
dispatch connection open; the client's network path dropped the 200
response, and the successful result envelope was lost — the audit log
records lifecycle events but not the full composite result envelope, so a
dropped response meant a lost result. Async mode eliminates that class: the
envelope is durable and re-readable via the handle.

Sync mode remains the default and is byte-identical to the pre-#3079 path —
async is strictly opt-in per request.

This reuses the *shape* of the `agent_run` durable-execution substrate
(durable row + lease/heartbeat + reaper — see
[`agent-run.md`](agent-run.md)) applied to a single governed dispatch
instead of an LLM tool-use loop. Two differences from `agent_run` are
load-bearing:

- **No `resume` in-flight policy.** A governed op can wrap a non-idempotent
  vendor write. Re-dispatching a half-executed write on pod death would
  double-execute it, so an orphaned run is **never** re-dispatched — the
  reaper drives it to `failed` (an audited terminal state). This is the safe
  half of the acceptance criterion "survives pod restart via lease/reaper
  semantics **or** terminates into an audited terminal state — never
  silently lost."
- **Raw params are not persisted — only a `params_hash`.** A persisted
  params blob would be a new secret surface (the same posture `audit_log`
  takes), and because the run never resumes, nothing needs them
  re-hydrated. The running task holds params in memory for the one dispatch.

## Key types

- `OperationRun` (`db/models.py`) — one durable row per async governed
  dispatch. Columns: `id` (the handle), `tenant_id` (FK), `identity_sub` /
  `identity_act`, `origin` (`direct` | `approval_resume`), `connector_id`,
  `op_id`, `target_name`, `params_hash`, `approval_request_id` (soft-FK for
  resume runs), `status`, `result` (the persisted envelope JSON), `error`
  (run-crash / reaper reason), `lease_owner` / `lease_expires_at`, and the
  `created_at` / `started_at` / `ended_at` timestamps. Table created by
  migration `0079`.
- `OperationRunStatus` / `OperationRunOrigin` — closed enums backed by DB
  `CHECK` constraints; drift-guarded against the migration's frozen tuples in
  `tests/test_operation_run_lifecycle.py`.
- `operations/operation_run.py` — the lifecycle service (a trim of
  `operations/agent_run.py`): create / get / list, the enforced state
  machine (`transition`, `ALLOWED_TRANSITIONS`), lease (`claim_lease`,
  `heartbeat`, `release_lease`), and the terminal helpers `succeed_run`
  (persists the envelope) / `fail_run` (records the crash reason) /
  `cancel_run` (operator-authorized). Every mutator flushes; the caller owns
  the commit.
- `operations/operation_run_service.py` — `OperationRunService`, a
  process-wide singleton holding the in-process task store. `submit_call` /
  `submit_approval_resume` create the row + claim a lease + launch the
  background task; `poll` / `list` / `cancel` are the read/cancel surface.
- `api/v1/operation_runs.py` — the `GET /runs`, `GET /runs/{handle}`,
  `POST /runs/{handle}/cancel` routes. Registered **before** the operations
  router in `main.py` so the literal `/runs` list route is not shadowed by
  the operations router's `/{descriptor_id}` catch-all.
- `operations/operation_run_reaper.py` — the expired-lease reclaim sweep
  (single fail-into-audit policy).

## Control flow

Async submit (`POST /operations/call`, `async: true`):

1. `post_call` strips the transport-only `async_` control and calls
   `OperationRunService.submit_call(operator, arguments)`.
2. `submit_call` computes a `params_hash`, inserts a `pending` row, and
   claims a lease under this worker's `"<hostname>:<pid>"` owner (one
   committed transaction), then launches a background task and returns the
   run id. The route answers `202` + `{run_id, status: "pending", async: true}`.
3. The background task (`_run_to_completion`) starts a heartbeat sidecar,
   transitions the row `pending → running`, then awaits `call_operation` —
   the **same** governed-dispatch path a sync request runs (identical policy
   gate, identical synchronous audit write). On return it persists the
   envelope and transitions `running → succeeded` (`succeed_run`). An
   unexpected raise (defence-in-depth) transitions `running → failed`.
4. The caller reads the durable outcome via `GET /operations/runs/{handle}`.

Async approve (`POST /approvals/{id}/approve`, `async: true`): the decision
is recorded + audited synchronously (unchanged), then
`submit_approval_resume` launches `resume_dispatch_after_approval` on the
background substrate and the route returns 202 + the handle. The
exactly-one-resumer claim (#2293) still guards the single execution, now won
inside the background task.

State machine (`pending → running → succeeded | failed`, plus
`→ cancelled` from any non-terminal state):

- `succeeded` means the **run** completed and its envelope is durable —
  even when the envelope's own `status` is `error` / `denied` /
  `needs-approval`. The dispatch outcome lives in the persisted envelope,
  not the run status.
- `failed` is reserved for a run that never produced an envelope (worker
  died / reaped, or the dispatch raised).
- `cancelled` is best-effort: the durable intent is recorded; an in-flight
  task loses its lease on the next heartbeat and its result is discarded
  when it finalises against the now-terminal row (the dispatch's own
  synchronous audit row remains the record of what executed).

## Reaper

`operation_run_reaper` scans `status='running' AND lease_expires_at < now()`
on a fixed cadence (advisory-lock leader election per tick, `FOR UPDATE SKIP
LOCKED` claim, per-row savepoint isolation, one commit per tick — the same
discipline as the agent-run reaper) and drives each orphaned run to `failed`
with a stable interruption reason, writing an internal audit row
(`operator_sub='system:operation-run-reaper'`, `run_id` = the operation-run
id, migration-`0034` audit correlation) in the same transaction. It never
re-dispatches. Gated on `OPERATION_RUN_REAPER_ENABLED`; started/stopped in
the `main.py` lifespan alongside the other background sweeps.

## Dependencies

- `operations/meta_tools.call_operation` — the governed dispatch the direct
  async path runs in the background.
- `operations/approval_queue.resume_dispatch_after_approval` — the resume the
  async approve path runs in the background.
- `operations/agent_run.py` + `agent/reaper.py` — the pattern reference (the
  durable-row / lease / reaper shape this substrate mirrors).
- `db/advisory.advisory_lock`, `db/engine.get_sessionmaker`,
  `settings` (`operation_run_*` knobs), `main.py` lifespan wiring.

## Known issues / notes

- **No CLI verbs yet.** The generated Go client (`cli/api/openapi.json` /
  `client.gen.go`) carries the new paths after `make snapshot-openapi &&
  make generate`, but no `meho operation runs …` CLI command is wired.
  Adding operator CLI verbs is a follow-up.
- **Pending rows are not reaped.** The reaper only reclaims `running` rows
  (a `pending` row has no lease window that matters). The task transitions
  `pending → running` within milliseconds of the row insert, so the window
  is negligible; mirrors the agent-run reaper's posture.
- **In-process task store is per-replica.** The durable row is the source of
  truth, so `poll` works from any replica, but the launching replica holds
  the live task. If that replica dies, the reaper (any replica) reclaims the
  row into `failed`.

## References

- Issue #3079 (async governed dispatch).
- v0.1-spec §6 (audit is synchronous + append-only — preserved
  per-operation: a run is `succeeded` only after the dispatch's audit row
  commits).
- [`agent-run.md`](agent-run.md) — the substrate this mirrors.
- Migration `0079` — the `operation_run` table.
