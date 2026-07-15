# Satellite runner — headless push-only deploy mode (Initiative #2415)

## Overview

The **satellite runner** is a second deploy mode of the one backplane
codebase. The central instance runs the FastAPI app
(`uvicorn meho_backplane.main:app`); a runner runs
`python -m meho_backplane.runner` — the third execution mode of the
shared container image, alongside Serve and Migrate
(`backend/Dockerfile`).

A runner exists to give the backplane reach into networks its central pod
cannot dial: targets behind NAT, private control planes, ClusterIP-only
services. The path is one-directional — a runner inside the isolated
network dials the central instance outbound, never the reverse — so the
runner is **push-only**: it initiates every connection; the center is
passive.

A runner is a *dumb executor of centrally-authorized work*. It has **no**
local Postgres, Valkey, UI, MCP, or inbound listener. Each tick it polls
central for its assignment, executes the read-only
(`safety_level == "safe"`) operations locally against the same connector
surface the central instance uses, and reports results back. All
authorization, approval, and audit stay central; the runner never
self-authorizes.

This package (`backend/src/meho_backplane/runner/`, #2497) is the runner
**chassis**: entrypoint, settings, tick loop, poll/report client,
on-disk retry spool, and work-item executor. The central endpoints the
client polls land in #2499; the long-poll command plane in #2498.

## Key types

- **`runner.wire`** — the versioned pydantic models shared verbatim with
  the central endpoints (#2499 imports these; it may widen them here, and
  must not fork a parallel copy — one codebase, one schema):
  - `RunnerAssignment` — `assignment_version` (an opaque content digest
    the runner uses only as a cache key; the digest contract is #2499's)
    plus `items`.
  - `RunnerWorkItem` — one authorized op: `check_ref`, `op_id`,
    `(product, version, impl_id)`, `handler_ref`, `params`,
    `safety_level`, a `RunnerPrincipal`, and an optional
    `ResolvedTargetDescriptor`.
  - `ResolvedTargetDescriptor` — the centrally-resolved target
    attributes a connector handler duck-reads (the runner has no local
    target table). v1 carries `name` / `product` / `version` /
    `fingerprint` / `extras` / `preferred_impl_id`; #2499 widens it with
    the connection-routing set (host/port/secret_ref/TLS).
  - `RunnerResult` / `RunnerResultBatch` — each result carries a
    runner-generated `result_uid` (uuid4) so central ingest can
    deduplicate spool re-posts idempotently. `status` is a runner-level
    tri-state: `ok` (handler ran, returned a payload), `refused` (runner
    declined), `error` (handler raised).
- **`RunnerSettings`** (`runner.settings`) — the `MEHO_RUNNER_*` config,
  a separate model from the chassis `Settings` (which hard-requires
  Keycloak + `DATABASE_URL` env a runner does not have). Resolved once
  via `get_runner_settings()`; a missing/malformed required var raises
  `RunnerConfigError` naming the variable.
- **`RunnerClient`** (`runner.client`) — an `httpx.AsyncClient` wrapper
  for the two calls (`fetch_assignment`, `post_results`). Both raise a
  single `RunnerClientError` on any transport or non-success status; a
  `304` fetch returns the `ASSIGNMENT_UNCHANGED` sentinel.
- **`ResultSpool`** (`runner.spool`) — a directory of un-posted result
  batches, one atomic JSON file per batch, drained oldest-first, bounded
  by `spool_max_files`.
- **`execute_work_item`** (`runner.executor`) — resolves and invokes one
  work item's handler locally.

## Control flow

`python -m meho_backplane.runner` → `runner/__main__.py::main()`:

1. `run_runner()` calls `configure_logging()`, then
   `get_runner_settings()` (a `RunnerConfigError` here propagates to
   `main`, which prints it to stderr and exits 1), then
   `_eager_import_connectors()` (DB-free — imports every connector
   subpackage so registrations land in the in-memory registry), then
   `asyncio.run(_async_main(settings))`.
2. `_async_main` starts the tick loop as a task and wires SIGTERM/SIGINT
   to cancel it. On signal, the task is cancelled, the loop unwinds
   (closing the httpx client via its async context manager), and the
   process exits 0.
3. The tick loop (`_run_loop`) is **sweep-then-sleep** — moulded on the
   in-process interval-tick sweepers (`topology/scheduler.py`,
   `memory/expiry.py`), **not** the DB-session-bound scheduler trigger
   loop. A fresh runner sweeps immediately rather than sleeping a full
   cadence first. Each tick's body is fully guarded: an unexpected error
   logs and waits for the next cadence; `CancelledError` propagates.

Each tick (`run_one_tick`):

1. **Drain the spool** oldest-first, stopping at the first re-post
   failure (a still-down uplink must not spin).
2. **Fetch the assignment**, echoing the cached `assignment_version` as
   `known_version`. A `304` or a fetch failure keeps the cached
   assignment — the runner keeps executing the last assignment while the
   uplink is down.
3. **Execute** each work item through `execute_work_item`.
4. **Post** the result batch; on POST failure, write it to the spool.

`execute_work_item` is fail-closed defence in depth (the real
authorization boundary is central minting, #2500):

1. Refuse any item whose `safety_level != "safe"`.
2. Refuse any `handler_ref` not lexically under
   `meho_backplane.connectors.` — checked **before** import (import has
   module-load side effects) and re-checked on the resolved callable's
   `__module__`.
3. Resolve the handler via `import_handler` (dotted-path import + getattr
   walk, no DB). Rebind a bound-method handler against its connector
   instance via `is_unbound_method` + `get_or_create_connector_instance`
   (the dispatcher's own rebinding steps, minus the DB descriptor lookup;
   the connector class comes from the in-memory registry keyed on the
   payload's `(product, version, impl_id)`). Module-level handlers such
   as `net.*` need no rebinding.
4. Reconstruct the acting `Operator` from the principal context with an
   empty `raw_jwt` (no bearer token for the acting principal exists on
   the runner; the op was authorized centrally). Build the duck-typed
   target from the descriptor (`None` for targetless ops).
5. Invoke `handler(operator, target, params)`. A handler exception
   becomes a structured `error` result — a failed check is a result,
   never a crashed tick.

## Dead-man switch + mandatory heartbeat (#2501)

A runner that dies, wedges, or loses its network path must not leave its
workloads silently reporting last-known-good forever. Two halves make
runner liveness observable and enforced, both on the **central clock** —
a runner's own clock is never consulted.

**Heartbeat (piggybacked, never a dedicated endpoint).** Every
authenticated runner-plane request stamps `runner_principal.last_seen_at
= now()` on the central clock. The stamp lives in the single choke-point
all four runner-plane endpoints pass through —
`auth/runner_guard.py::assert_runner_scope` (#2498's `GET
/gateway/{runner}/next` + `POST /gateway/{runner}/result`, #2499's `GET
/checks/assignment` + `POST /checks/results` all call it, and nothing
else does). It is keyed by the token's unforgeable `runner_id` claim and
reads no request field, so `last_seen_at` is never client-controlled
(the same discipline `web_session.last_seen_at` follows). There is
deliberately **no** `POST /gateway/{runner}/heartbeat`: a healthy idle
runner still issues at least one authenticated request per poll window
(its tick loop fetches the assignment every cadence even with no work),
so the idle work cycle *is* the heartbeat. This is the #1501 lesson — a
dedicated heartbeat loop can stay alive while the work loops are wedged,
which is exactly the zombie state to avoid; stamping the real work
requests measures the liveness that matters.

**Central sweeper (`gateway/deadman.py`).** An in-process interval-tick
loop the FastAPI lifespan owns (mould: `memory/expiry.py`, **not** the
DB-bound scheduler trigger loop). Each tick takes a fixed non-blocking
advisory lock (reaper mould; no-op on SQLite), selects the
`runner_assignments` rows whose runner's `last_seen_at` is behind the
cutoff and whose `stale_at IS NULL`, flips each with a conditional
`UPDATE ... WHERE stale_at IS NULL`, and writes one internal audit row
per flip (`method='INTERNAL'`, `path='gateway.runner.stale'`, payload
`{runner, lapse_seconds}`). The `stale_at IS NULL` predicate + the
`rowcount` gate keep "exactly one audit row per flip" true even when the
advisory lock is a no-op or two replicas race, and make an immediate
second tick a natural no-op.

**Threshold.** `threshold_seconds = gateway_runner_stale_after_multiplier
× GATEWAY_LONGPOLL_MAX_WAIT_SECONDS` — the multiplier (default 3) times
#2498's exported long-poll window (30 s), i.e. the maximum quiet interval
of a healthy idle runner. The number is never re-hardcoded here; it is
imported from the gateway queue package. Default 90 s gives a healthy
runner ~3 windows of slack.

**Recovery is data-driven, never sweeper-driven.** The sweeper only ever
*sets* `stale_at`. An accepted result ingestion (`POST /checks/results`
or `POST /gateway/{runner}/result`) clears it via
`gateway/deadman.py::clear_runner_stale` — the only clear path.
Runner-level derived staleness clears the instant the runner's next
request re-stamps `last_seen_at`.

**Surfacing contract (#2416 / #2506).** `stale_at IS NOT NULL` maps to
the `UNKNOWN` state for every check assigned to that runner in the
five-state rollup #2506 defines (`UNKNOWN → degraded`). This task lands
the marker + audit trail only; it builds no UI and no rollup — until
#2416 lands, the flip is observable on the `runner_assignments` row and
in the `gateway.runner.stale` audit path.

**Settings.** `GATEWAY_DEADMAN_ENABLED` (default `true` — that is what
"mandatory" means: a runner cannot opt out of heartbeating because the
stamp is a request side effect, and central enforcement is on by
default), `GATEWAY_DEADMAN_TICK_INTERVAL_SECONDS` (default 30),
`GATEWAY_RUNNER_STALE_AFTER_MULTIPLIER` (default 3).

## Dependencies

- `httpx` (already a direct backend dependency) for the poll/report
  client; `httpx.MockTransport` stands in for the not-yet-built central
  endpoints in tests.
- `pydantic` v2 for the wire models and settings.
- `structlog` for JSON-to-stdout logging (`configure_logging`).
- Reused DB-free chassis primitives: `logging.configure_logging`,
  `connectors.registry._eager_import_connectors` / `all_connectors_v2`,
  `operations._handler_resolve` (`import_handler`, `is_unbound_method`,
  `get_or_create_connector_instance`), `auth.operator.Operator`, and the
  `net.*` `safe` handlers + their env-read probe allowlist.

## Boundaries / out of scope for #2497

- The central `GET /api/v1/checks/assignment` + `POST /api/v1/checks/results`
  endpoints (#2499) — they reuse and widen `runner/wire.py`.
- The outbound long-poll command plane
  (`GET /gateway/{runner}/next` / `POST /gateway/{runner}/result`) — #2498.
- Single-use capability-command minting + request-id dedup — #2500. The
  runner's `safe`-only executor guard is defence in depth, not the mint
  rule.
- Heartbeat + central stale/unknown flipping — #2501.
- The scoped per-runner service principal + credential scoping — #2502.
  `MEHO_RUNNER_TOKEN` is the seam it fills; this chassis treats it as an
  opaque bearer.
- Change ops over the gateway, any inbound listener, arbitrary TCP
  proxying — out of scope by the initiative's design principles.

## References

- Initiative #2415 (design principles, grounding corrections); parent
  goal #221; first consumer #2416.
- `backend/src/meho_backplane/db/migrate.py` — module-run entrypoint
  mould; `backend/Dockerfile` — the execution-modes contract (Serve /
  Migrate / Runner).
- `backend/src/meho_backplane/topology/scheduler.py`,
  `backend/src/meho_backplane/memory/expiry.py` — the in-process
  interval-tick sweeper moulds; `backend/src/meho_backplane/scheduler/loop.py`
  — the DB-bound trigger loop the runner deliberately does **not** use.
- `backend/src/meho_backplane/operations/_handler_resolve.py`,
  `operations/dispatcher.py` (`_maybe_bind_method`) — DB-free handler
  resolution + the rebinding the executor mirrors.
- `backend/src/meho_backplane/connectors/net/ops.py`,
  `connectors/net/allowlist.py` — the `safe` targetless probe handlers
  the runner executes in v1.
