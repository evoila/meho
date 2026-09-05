# Result spill + drill-in read-back (JSONFlux handles)

How a reduced (over-threshold) operation response becomes pageable —
and how to diagnose one that is not. Companion to the broader
[`docs/architecture/jsonflux.md`](../architecture/jsonflux.md) (engine
provenance, reduction thresholds, sample ordering); this doc covers the
**spill / read-back slice** and the no-spill diagnosis runbook added for
#1629.

## Overview

When `JsonFluxReducer.reduce()` materializes a set-shaped payload above
threshold (>50 rows or >4 KB by default), the caller gets back:

- an inline summary `{row_count, total, sample_rows_returned,
  sample_bytes, source_key?}` (the bounded preview itself lives once on
  `handle.sample_rows`, #134; ops registering a `result_scalars` hint
  additionally keep the listed payload scalars top-level, #3084), and
- a `ResultHandle` on `OperationResult.handle` carrying `summary_md`,
  `schema_`, `total_rows`, `sample_rows`, and the self-documenting
  `fetch_more` envelope.

The **full** normalized row list is spilled to the Valkey-backed
`ResultHandleStore` keyed by `(tenant_id, handle_id)`; the `result_query`
read surface pages it back over **both** transports — the MCP meta-tool
and the REST route `POST /api/v1/operations/result-query`, which share the
`read_result_window` core (#3179). Whether that spill happened is reported
on `handle.fetch_more.drill_in`:

| State | `available` | `reason` | What the agent can do |
|---|---|---|---|
| Spilled | `true` | `null` | Call `result_query(handle_id, offset, limit)` until `expires_at` |
| No usable tenant context | `false` | `no_tenant_context` | Re-call with narrower params / native pagination |
| Store did not persist | `false` | `result_store_unavailable` | Re-call with narrower params / native pagination; operator checks the Valkey backend |

The handle itself is minted **unconditionally** on every reduce — a
skipped spill never suppresses the handle, it only flips the drill-in
branch to `available=false`.

## Read-back modes: paging vs. query (#3366)

`result_query` reads a spilled handle back two ways, on both transports
(the MCP tool and `POST /api/v1/operations/result-query`), through one
shared core:

- **Paging** (`handle_id` + `offset` + `limit`) — `read_result_window`
  returns a row window, unchanged since #3179.
- **Query** (`handle_id` + a `query` object) — `run_result_query`
  compiles the structured arguments into **exactly one parameterized,
  read-only `SELECT`** over the handle's rows and returns just the
  matching rows or the per-group aggregates. The `query` grammar is
  filter predicates (≤10; operators `=, !=, <, <=, >, >=, IN, IS NULL`),
  `select` projection, `group_by` (≤4), aggregates
  (`COUNT/SUM/MIN/MAX/AVG`), `order_by` (≤4), and `limit`. There is **no
  raw-SQL argument** on any transport.

The query runs inside the reduce-time `QueryEngine`'s sandbox rebuilt
per call: the full authorized rows come back from `fetch_rows`, are
`register`ed under the table name `result`, and the compiled statement
is the only thing that touches them. Every referenced field is validated
against the handle's known columns (unknown field → rejected); operators
and aggregate functions are fixed allow-lists; caller values bind as
DuckDB prepared-statement parameters, never string-interpolated. See
[`docs/architecture/jsonflux.md`](../architecture/jsonflux.md#query-surface-3366)
for why compiling from a fixed template is a stronger safety property
than sanitizing a caller SQL string (verified on the pinned
`duckdb==1.5.5`).

### Bounds and coverage

Every query result is bounded: output rows are capped at
`RESULT_QUERY_MAX_OUTPUT_ROWS` (default 500) with an explicit
`truncated` flag (grouped results capped identically); the serialized
output is capped at `RESULT_QUERY_MAX_OUTPUT_BYTES` (default 256 KB)
**after** the row cap, with an actionable over-budget error; and each
compiled `SELECT` runs under a `RESULT_QUERY_TIMEOUT_SECONDS` (default 5)
wall-time budget enforced by running `conn.execute()` on a worker thread
and calling `conn.interrupt()` on expiry.

Because the spill itself is capped at `RESULT_HANDLE_MAX_SPILL_ROWS`, a
query may run over a subset of the true collection. The result carries
`coverage`: `complete` when `stored_rows == total_rows`, else `partial`
plus a `coverage_note` — so a `COUNT` over a capped spill is labelled as
covering the stored subset, **never** presented as a whole-inventory
total.

## Key types

| Symbol | Where | Role |
|---|---|---|
| `JsonFluxReducer._spill` / `_SpillOutcome` | `backend/src/meho_backplane/operations/jsonflux_reducer.py` | Persists the materialized rows; reports `stored_rows` **or** a machine-readable `skip_reason` |
| `FetchMoreDrillIn.reason` / `DrillInUnavailableReason` | `backend/src/meho_backplane/connectors/schemas.py` | The two-valued no-spill cause on the wire (#1629) |
| `ResultHandleStore.spill` / `fetch_window` / `fetch_rows` | `backend/src/meho_backplane/connectors/result_handle_store.py` | Fail-open Valkey persistence + operator/tenant-scoped read-back; `fetch_rows` returns the full authorized set for the query surface (#3366) |
| `read_result_window` / `run_result_query` / `ResultHandleNotFoundError` | `backend/src/meho_backplane/operations/result_query.py` | Transport-neutral windowed-read core (#3179) + bounded query core (#3366) both surfaces wrap; misses raise the recoverable not-found error |
| `compile_query` / `ResultQuerySpec` / `QueryContractError` | `backend/src/meho_backplane/jsonflux/query/contract.py` | Compiles the validated query grammar to one parameterized read-only `SELECT` (#3366) |
| `result_query` (MCP) | `backend/src/meho_backplane/mcp/tools/result_query.py` | MCP read surface; misses surface as recoverable `handle_not_found` (`-32602`) |
| `POST /api/v1/operations/result-query` | `backend/src/meho_backplane/api/v1/operations.py` | REST read surface; misses surface as a `404` with `reason=handle_not_found` (#3179) |
| `_reduce_or_error` | `backend/src/meho_backplane/operations/dispatcher.py` | Builds `reducer_context` (`tenant_id`, `operator_sub`, `op_id`, hints) from the authenticated `Operator` |

## Control flow (dispatch path)

1. `dispatch()` runs the op, redacts, and calls the reducer with
   `reducer_context` — `operator.sub` always, `str(operator.tenant_id)`
   whenever the operator has a tenant. `Operator.tenant_id` and
   `Operator.sub` are **required** fields on the auth model, so an
   authenticated MCP/REST dispatch always carries both.
2. The reducer materializes (DuckDB), then `_spill()`:
   - no usable `tenant_id`/`operator_sub` (absent or non-UUID) →
     skip, reason `no_tenant_context`;
   - `ResultHandleStore.spill()` returns `False` (Valkey unreachable,
     write rejected, or the guard `ttl/max_rows <= 0` / empty rows) →
     skip, reason `result_store_unavailable`;
   - otherwise → `stored_rows = min(total_rows,
     RESULT_HANDLE_MAX_SPILL_ROWS)`.
3. `_assemble()` builds the inline summary + handle; the drill-in
   branch carries the outcome (`available` + `reason` + rationale).
4. Every skip logs a structured `jsonflux_spill_skipped` warning with
   `reason`, `op_id`, `handle_id`, `total_rows` (plus boolean
   breadcrumbs for which context key was absent/malformed). A
   store-level exception additionally logs
   `result_handle_spill_failed` with the underlying error string.

## Diagnosis: the RDC cycle-8 `k8s.logs tail=300` finding (#1629)

The consumer reported `k8s.logs tail=300` returning `row_count=300,
total=300`, a 5-row `sample`, `source_key="lines"`, and "`handle:
null`" — filed as a regression of the #1507 spill infrastructure. The
code-level diagnosis at `v0.13.0` (`f6ee330`):

- **Not a #1507 regression and not a k8s.logs-shape gap.** The spill /
  `result_query` / drill-in infrastructure is fully present, and the
  exact handler response shape (`lines` list next to scalar `pod` /
  `namespace` / `container` / `truncated` keys, 300 rows, tail
  ordering) reduces, spills, and flips drill-in available under a
  tenant-scoped context — pinned by
  `test_k8s_logs_shape_with_tenant_context_spills_and_pages` in
  `backend/tests/test_operations_jsonflux_reducer.py`.
- **A literal envelope-level `handle: null` is not producible by this
  reduce path.** `wrap_ok_result` attaches the minted handle whenever
  the summary shape (`row_count`/`total`/`sample`) is present, and the
  MCP `call_operation` tool serializes `OperationResult` verbatim. A
  null handle next to a reduced summary therefore points at the
  *reading surface* (e.g. an audit/broadcast projection, which carry
  only handle metadata by design, or client-side rendering), or at
  operator shorthand for "no usable read-back route".
- **The reachable no-spill branch on a real deploy is the store one.**
  `Operator.tenant_id`/`sub` are required on every authenticated
  dispatch, so `no_tenant_context` cannot fire there (it covers
  non-dispatch reduces and malformed context only). A 5-of-300 sample
  with no working `result_query` route on a live deploy means
  `ResultHandleStore.spill()` returned `False` — Valkey (the broadcast
  service the store piggybacks on, `BROADCAST_REDIS_URL`) unreachable
  from the backplane pod, or the write rejected.
- **What was actually wrong user-side:** before #1629 both skip causes
  collapsed into one ambiguous rationale with no machine-readable
  `reason`, and the tenant-context skip was fully silent in logs — the
  operator saw a truncated sample with no way to page and no stated
  cause. That explainability gap is what #1629 fixes; the deploy-side
  store reachability is verified with the triage below.

### Triage runbook

1. Reproduce the reduce (any over-threshold read works) and inspect
   `handle.fetch_more.drill_in` in the response: `reason` now names the
   branch.
2. `result_store_unavailable` → check backplane logs for
   `result_handle_spill_failed` (carries the Valkey error) and the
   broadcast service health (`BROADCAST_REDIS_URL`; the store reuses
   the broadcast client's pool, so a broken broadcast feed and a
   broken spill store fail together).
3. `no_tenant_context` → the reduce ran outside an authenticated
   dispatch; check the `jsonflux_spill_skipped` warning's
   `has_tenant_id` / `has_operator_sub` / `tenant_id_malformed`
   breadcrumbs for which key was missing.
4. A drill-in `available=true` handle whose `result_query` still
   misses → the handle expired (TTL default 3600 s) or the read ran as
   a different operator (`sub` mismatch is an intentional
   `handle_not_found`).

## Dependencies

- Valkey via the broadcast fast client
  (`meho_backplane.broadcast.client.get_broadcast_client`,
  `BROADCAST_REDIS_URL`) — shared pool, 5 s socket timeouts, fail-fast.
- `RESULT_HANDLE_MAX_SPILL_ROWS` (default 10000, validated `> 0`) caps
  the per-key value size; `ttl_seconds` (reducer default 3600) bounds
  lifetime server-side.
- `RESULT_QUERY_MAX_OUTPUT_ROWS` (500) / `RESULT_QUERY_MAX_OUTPUT_BYTES`
  (256 KB) / `RESULT_QUERY_TIMEOUT_SECONDS` (5) bound a single query's
  output rows, serialized size, and wall time (#3366); all validated
  `> 0`.
- `duckdb==1.5.5` / `pyarrow==25.0.1` (pinned) — the sandboxed
  `QueryEngine` the compiled query runs inside.

## Known issues

- The spill TTL and sample size are reducer-constructor defaults, not
  per-op tunables.
- `no_tenant_context` folds the malformed-UUID case into the
  absent-context case on the wire; the log breadcrumb
  (`tenant_id_malformed=true`) is the only place they differ.

## References

- #1507 — spill store + `result_query` + drill-in (G0.20-T7).
- #3366 — bounded validated query surface over the handle (filter /
  project / group / aggregate, no raw SQL).
- #1629 — no-spill `reason` surfacing + skip logging + this diagnosis
  (G0.23-T3, RDC cycle-8 signal `k8s-logs-mcp-five-row-sample-cap`).
- #1479 — tail sample ordering for log-shaped ops (G0.19-T1).
- `docs/architecture/jsonflux.md` — engine provenance + full reducer
  contract.
