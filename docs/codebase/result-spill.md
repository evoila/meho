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
| Admission work limit exceeded | `false` | `admission_limit_exceeded` | Re-call with narrower params; the captured graph was not profiled or retained |

The handle itself is minted **unconditionally** on every reduce — a
skipped spill never suppresses the handle, it only flips the drill-in
branch to `available=false`.

## Read-back modes: paging vs. query (#3366)

`result_query` reads a spilled handle back two ways, on both transports
(the MCP tool and `POST /api/v1/operations/result-query`), through one
shared core:

- **Paging** (`handle_id` + `offset` + `limit`) — `read_result_window`
  returns a row window. Since #3387 the window is bounded by the same
  `RESULT_QUERY_MAX_OUTPUT_BYTES` ceiling the query path applies: a page that
  serializes past the budget raises the recoverable over-budget error (its
  remediation is a smaller `limit`, since paging has no `select` / `filter`)
  rather than returning a multi-MB window, so no agent ever sees an
  over-budget page.
- **Query** (`handle_id` + a `query` object) — `run_result_query`
  compiles the structured arguments into **exactly one parameterized,
  read-only `SELECT`** over the handle's rows and returns just the
  matching rows or the per-group aggregates. The `query` grammar is
  filter predicates (≤10; operators `=, !=, <, <=, >, >=, IN, IS NULL`),
  `select` projection, `group_by` (≤4), aggregates (≤8;
  `COUNT/SUM/MIN/MAX/AVG`), `order_by` (≤4), and `limit`. There is **no
  raw-SQL argument** on any transport.

The query runs inside the reduce-time `QueryEngine`'s sandbox rebuilt
per call: the full authorized rows come back from `fetch_rows`, and the
compiled statement is the only thing that touches them. Operators and
aggregate functions are fixed allow-lists; caller values bind as DuckDB
prepared-statement parameters, never string-interpolated. See
[`docs/architecture/jsonflux.md`](../architecture/jsonflux.md#query-surface-3366)
for why compiling from a fixed template is a stronger safety property
than sanitizing a caller SQL string (verified on the pinned
`duckdb==1.5.5`).

### Field catalog and raw output (#3635)

The query does not trust DuckDB to name or type the fields. It works
like this:

1. `run_result_query` builds the result field catalog
   (`jsonflux/query/result_catalog.py`) from **every** stored row. A
   field first seen late in the set is still known.
2. `compile_query` checks each field against the catalog by exact,
   case-sensitive name. `Foo` and `foo` are two fields.
3. A field used in `filter`, `order_by`, `group_by`, or `aggregate`
   must be queryable in the catalog. If it is not (mixed JSON kinds,
   objects or lists, only nulls, integers past 64 bits, non-finite
   numbers), the caller gets a `QueryContractError` that names the
   field, the reason, and the way out: return it with `select`, or page
   the raw rows. This happens before any SQL runs. One exception to
   "mixed JSON kinds": a field with whole and decimal numbers (for
   example `0`, `12.5`, `3`) is queried as one decimal-number column,
   as long as every whole number is within ±2^53 (a 64-bit float holds
   these exactly). If one is larger, the field stays not queryable so
   it is never rounded.
4. The SQL never uses caller names. Each queryable field becomes a
   typed column with a generated name (`_rr_v_0000`, ...), and the
   table also has a hidden row number, `_rr_ord`
   (`_project_rows` in `operations/result_query.py`). Filter values are
   checked against the field's type before binding.
5. Every output column gets a generated alias (`_rr_o_0`, ...). The
   `OutputPlan` on `CompiledQuery` maps cursor rows to public names by
   position. Two equal public names (for example `group_by=["count"]`
   with `COUNT(*)`) are rejected before SQL, so no column is silently
   lost.
6. A flat query (no `group_by`, no `aggregate`) selects only
   `_rr_ord`, ordered by the caller's terms and then by stored order.
   The output is copied from the raw stored rows: the whole row, or
   just the `select` keys the row has. Values come back exactly as
   captured (the string `"false"` stays a string), and a missing key
   stays missing. Grouped output comes from DuckDB, but only over
   single-kind scalar fields, so those values are exact too.

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
| `FetchMoreDrillIn.reason` / `DrillInUnavailableReason` | `backend/src/meho_backplane/connectors/schemas.py` | The machine-readable no-spill cause on the wire |
| `ResultHandleStore.spill` / `fetch_window` / `fetch_rows` | `backend/src/meho_backplane/connectors/result_handle_store.py` | Fail-open Valkey persistence + operator/tenant-scoped read-back; `fetch_rows` returns the full authorized set for the query surface (#3366) |
| `read_result_window` / `run_result_query` / `ResultHandleNotFoundError` | `backend/src/meho_backplane/operations/result_query.py` | Transport-neutral windowed-read core (#3179) + bounded query core (#3366) both surfaces wrap; misses raise the recoverable not-found error |
| `compile_query` / `ResultQuerySpec` / `QueryContractError` / `OutputPlan` | `backend/src/meho_backplane/jsonflux/query/contract.py` | Compiles the validated query grammar against the result field catalog to one parameterized read-only `SELECT` plus an output plan (#3366, #3635) |
| `result_query` (MCP) | `backend/src/meho_backplane/mcp/tools/result_query.py` | MCP read surface; misses surface as recoverable `handle_not_found` (`-32602`) |
| `POST /api/v1/operations/result-query` | `backend/src/meho_backplane/api/v1/operations.py` | REST read surface; misses surface as a `404` with `reason=handle_not_found` (#3179) |
| `_reduce_or_error` | `backend/src/meho_backplane/operations/dispatcher.py` | Builds `reducer_context` (`tenant_id`, `operator_sub`, `op_id`, hints) from the authenticated `Operator` |

## Control flow (dispatch path)

1. `dispatch()` runs the op, redacts, and calls the reducer with
   `reducer_context` — `operator.sub` always, `str(operator.tenant_id)`
   whenever the operator has a tenant. `Operator.tenant_id` and
   `Operator.sub` are **required** fields on the auth model, so an
   authenticated MCP/REST dispatch always carries both.
2. The reducer validates the captured payload and derives schema, summary,
   and preview directly from raw rows, then `_spill()`:
   - an admission-limit breach returns an unprofiled handle with no spill,
     reason `admission_limit_exceeded`;
   - no usable `tenant_id`/`operator_sub` (absent or non-UUID) →
     skip, reason `no_tenant_context`;
   - `ResultHandleStore.spill()` returns `False` (Valkey unreachable,
     write rejected, or the guard `ttl/max_rows <= 0` / empty rows) →
     skip, reason `result_store_unavailable`;
   - otherwise → `stored_rows = min(total_rows,
     RESULT_HANDLE_MAX_SPILL_ROWS)`.
3. `_assemble()` builds the inline summary + handle; the drill-in
   branch carries the outcome (`available` + `reason` + rationale).
4. The tenant-context and store-unavailable spill skips log a structured
   `jsonflux_spill_skipped` warning with `reason`, `op_id`, `handle_id`,
   `total_rows` (plus boolean breadcrumbs for which context key was
   absent/malformed). The admission-limit result returns before the spill
   path, so its response reason is the diagnostic. A store-level exception
   additionally logs
   `result_handle_spill_failed` with the underlying error string.

## Non-dispatch reduce: `list_targets` (#3858)

`list_targets` is a direct substrate query, not a dispatched operation,
but it runs its page through the same `JsonFluxReducer` thresholds. Its
handler builds the reducer context itself (the caller's `tenant_id` and
`operator_sub`, `op_id="list_targets"`, a `pagination_hint`, and a
`result_scalars` hint that keeps `next_cursor`), so an over-threshold
page spills and reads back through `result_query` like a dispatched
result. The spill is keyed to the caller even for a cross-tenant
`platform_admin` listing. See [`targets.md`](targets.md).

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
   `result_handle_spill_failed` (carries the Valkey error and the
   `namespace`) and the health of the store it names: `v2` is the
   dedicated result-cache store (`RESULT_CACHE_REDIS_URL`), `legacy` the
   broadcast store (`BROADCAST_REDIS_URL`). A full result-cache store
   (`noeviction` at `maxmemory`) shows as `OOM command not allowed` and a
   rising `result_cache_records_total{outcome="rejected"}`.
3. `no_tenant_context` → the reduce ran outside an authenticated
   dispatch; check the `jsonflux_spill_skipped` warning's
   `has_tenant_id` / `has_operator_sub` / `tenant_id_malformed`
   breadcrumbs for which key was missing.
4. A drill-in `available=true` handle whose `result_query` still
   misses → the handle expired (TTL default 3600 s) or the read ran as
   a different operator (`sub` mismatch is an intentional
   `handle_not_found`).

## Dedicated result-cache store (#3639)

Result handles used to live only in the broadcast Valkey, next to the
governance keys (broadcast, rate limits, approval waits). That Valkey has
no byte ceiling, so many large handles could get it OOM-killed and take
governance down too. Handles now move to their own Valkey (chart subchart
`result-cache`, `maxmemory` + `noeviction`; see
[devops.md](devops.md#result-cache-subchart-chartsresult-cache)).

The move is reader-first, so it is safe during a rolling update:

| Step | Reads | Writes |
|---|---|---|
| `RESULT_CACHE_REDIS_URL` unset | `v2` key, then legacy key — both on the broadcast client | legacy key, broadcast client |
| URL set, `RESULT_CACHE_WRITER_ENABLED=false` | `v2` key on the dedicated client, then legacy key on the broadcast client | legacy key, broadcast client |
| URL set, writer on | same | `meho:reshandle:v2:{tenant}:{handle}`, dedicated client |

- The fallback read uses the caller's own tenant id, so it never reads
  another tenant's record. The operator check still runs on whatever
  record is found.
- There is no dual write. Legacy keys drain by their own TTL; the store
  keeps both clients for that time.
- A failing dedicated client counts as a miss and the read falls back to
  the legacy key.
- `RESULT_CACHE_REDIS_PASSWORD` is the dedicated store's `requirepass`.
  When the URL is unset, the broadcast password is used.

Metrics (aggregate only, no row content, keys, or vendor totals):

- `result_cache_records_total{outcome, namespace}` — writes `admitted` or
  `rejected` (over the byte cap, refused, or failed), per `v2` / `legacy`.
- `result_cache_reads_total{source}` — reads found in `v2`, found by the
  `legacy` fallback, or `miss`.
- `result_cache_memory_bytes{kind}` — `used` and `max` memory of the
  dedicated store, sampled from `INFO memory` after each `v2` write.

## Dependencies

- Valkey via two clients: the broadcast fast client
  (`meho_backplane.broadcast.client.get_broadcast_client`,
  `BROADCAST_REDIS_URL`) for legacy keys, and a dedicated client from
  `RESULT_CACHE_REDIS_URL` for `v2` keys (the same client when the URL is
  unset). Both use 5 s socket timeouts and fail fast.
- `RESULT_HANDLE_MAX_SPILL_ROWS` (default 10000, validated `> 0`) caps
  the per-key value size; `ttl_seconds` (reducer default 3600) bounds
  lifetime server-side.
- `RESULT_QUERY_MAX_OUTPUT_ROWS` (500) / `RESULT_QUERY_MAX_OUTPUT_BYTES`
  (256 KB) / `RESULT_QUERY_TIMEOUT_SECONDS` (5) bound a single query's
  output rows, serialized size, and wall time (#3366); all validated
  `> 0`.
- `duckdb==1.5.5` / `pyarrow==25.0.1` (pinned) — the sandboxed
  `QueryEngine` the lazy compiled-query path runs inside. The reducer does
  not construct an engine for its catalog, summary, or preview.

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
