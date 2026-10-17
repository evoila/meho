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
  `handle.sample_rows`, #134). When the list sat under a dict key, the
  payload's other top-level scalars stay top-level too, bounded to 32 keys
  and 1 KB of JSON text in total; a single value over 512 bytes is not
  copied (#3633). Skipped keys are counted under `omitted_scalar_count`,
  so the summary stays smaller than the 4 KB threshold that caused the
  reduction. The skipped values are not lost: they are stored with the
  rows, and `result_query` reads one back with `query={"path":
  "/<field name>"}` (or all of them with `path: ""`). The drill-in
  rationale names that read when the count is above zero and the store
  kept the fields. When they leave no room for the rows under the
  per-record cap, the store drops them, keeps the rows, and reports
  `document_stored=False`; the rationale then says the fields are not
  retrievable from the handle instead of naming a read that fails.
  Credential-class ops do not get this default copy;
  they keep only the scalars their `result_scalars` hint lists (#3084).
  Lists and objects are never copied by default (objects need the
  bounded `result_objects` hint); and
- a `ResultHandle` on `OperationResult.handle` carrying `summary_md`,
  `schema_`, `total_rows`, `sample_rows`, and the self-documenting
  `fetch_more` envelope. `schema_` comes from the result field catalog
  built over every captured row (next section), not from the sample.

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

## Result field catalog (#3631)

The captured JSON is the source of truth for a reduced result. The
reducer does not guess field names or types from a DuckDB table or from
the first row. After the admission check, it builds the result field
catalog (`build_catalog` in `jsonflux/query/result_catalog.py`) from
**every** captured row:

- One entry per exact top-level key. Names are case-sensitive (`Name`
  and `name` are two fields). Nested values are not flattened.
- Each entry records the JSON kinds seen (`boolean`, `integer`,
  `number`, `string`, `array`, `object`, `null`), whether the field is
  nullable (null in some row, or missing from some row), and whether it
  is queryable, with a `reason` when it is not.
- Entries are sorted by field name, and the generated column names
  (`_rr_v_0000`, ...) follow that order. So the catalog does not depend
  on row order.

The catalog drives three things on the handle:

- `schema_`: a JSON Schema (Draft 2020-12) array of objects with one
  property per field, in name order. A field seen with several kinds
  lists them all as a `type` array.
- the field list in `summary_md`;
- the inline preview. `sample_rows` are raw captured rows, so a sample
  row can lack a field that a later row has. The schema keys are the
  union of the keys of all rows, not the keys of the first row.

`result_query` builds the same catalog again from the stored rows when a
query runs (see "Field catalog and raw output" below). When the store
kept every row, the two catalogs are the same.

Tests: `tests/test_result_catalog.py` pins the rules.
`tests/test_result_reduction_properties.py` runs seeded property tests
over many row orders of generated JSON: the catalog and the schema order
never change, and paging, a flat query, and a `select` of fields that
cannot be queried return the captured values byte for byte.

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
**after** the row cap, with an actionable over-budget error; and the whole
read runs under a `RESULT_QUERY_TIMEOUT_SECONDS` (default 10) wall-time
budget. The budget covers the store fetch and the worker job (decode of
the stored record, catalog, compile, register, `SELECT`, encode, and the
wire-size check on the finished response). On
expiry the core calls `conn.interrupt()` and returns a recoverable timeout
error at once. Paging and `path` reads run under the same budget; their
timeout error names the way out for that surface (a smaller `limit`, or a
smaller part of the document).

`RESULT_QUERY_MAX_OUTPUT_BYTES` is the query-domain shaping budget: it
checks the `rows` before encoding. A second, terminal check (#3641) runs on
the finished response of all three read modes (query, paging and `path`
reads): `RESULT_QUERY_RESPONSE_WIRE_MAX_BYTES`
(default 2 MiB) against the size the MCP `tools/call` wire really carries
(text block plus `structuredContent`, escaping included, measured by
`operations/delivery_budget.py`). A response over it is refused with the same
recoverable `output_too_large` error, now carrying `delivery=unavailable`
(MCP `data`, REST `detail`). Nothing is cut; the caller narrows the read.

Because the spill itself is capped at `RESULT_HANDLE_MAX_SPILL_ROWS`, and
one stored record is capped at `RESULT_HANDLE_MAX_RECORD_BYTES` (default
16 MiB, measured on the encoded record), a query may run over a subset of
the true collection. Under the byte cap the store keeps the longest run of
rows from the start that fits. It never skips a row in the middle, and
`stored_rows` says how many it kept. The result carries
`coverage`: `complete` when `stored_rows == total_rows`, else `partial`
plus a `coverage_note` — so a `COUNT` over a capped spill is labelled as
covering the stored subset, **never** presented as a whole-inventory
total.

### Admission and the worker pool (#3640)

Every read path (paging, query, and a `path` read) must be admitted
**before** the store is read. A refused read makes no Valkey `GET`. The
checks live in
`operations/result_read_admission.py` and run in this order:

1. **Rate check** — a fixed-window `INCR` + `EXPIRE` counter per
   `(tenant, principal, minute)`, key
   `meho:ratelimit:result_read:{tenant}:{sub}:{bucket}`, on the broadcast
   Valkey. Paging and query share one counter.
   `RESULT_QUERY_RATE_PER_MINUTE` (default `0` = off, no Valkey call).
2. **Worker slot** — a process-wide gate sized to the dedicated worker
   pool, `RESULT_QUERY_EXECUTOR_WORKERS` (default 4). It does not queue:
   when every slot is busy the read is refused at once.
3. **Tenant lease** — at most `RESULT_QUERY_TENANT_CONCURRENCY` (default
   4) reads of one tenant at the same time in this process. It only binds
   when it is lower than the worker count.

A refused read raises `ResultReadRateLimitError` (`reason=rate_limited`)
or `ResultReadCapacityError` (`reason=capacity_exceeded`). MCP answers
with the recoverable `-32000` rate-limited error and
`data={reason, retry_after_seconds}`; REST answers `429` with a
`Retry-After` header.

After admission, only the Valkey `GET` runs on the event loop
(`ResultHandleStore.fetch_record`). Everything after it is one job on the
dedicated pool (`thread_name_prefix=result-read`), not on the event loop's
default pool: decoding the record and checking its owner
(`decode_window` / `decode_rows` / `decode_document`), then the shaping
for that path (a page slice and its size check; the query pipeline; a
JSON Pointer resolve), and last the terminal wire check (#3641) on the
finished response, which encodes it again. So a large record cannot block
the loop while it decodes or while its response is measured, and the
deadline above bounds both. The app lifespan
sizes the pool from settings at startup and stops it at shutdown; until
then (unit tests that call the cores directly) the `Settings` field
defaults apply.

**A worker that outlives its request.** A running thread cannot be
cancelled. When the deadline passes or the caller goes away, the core
interrupts DuckDB and returns, but the slot and the tenant lease stay held
until the worker thread finishes; the worker's done-callback releases
them. A keeper task renews the tenant lease while the worker runs, and the
request awaits that keeper through `asyncio.shield`, so a cancelled request
never releases the lease early. Each lease has an owner token (a stale
release cannot free a newer lease) and a 60 s TTL that reclaims a lease
nobody renews any more. There is no hard kill of a running worker.

## Document records and `path` reads (#3633)

Some large results have no single list to reduce: a detail object with
several lists (`k8s.pod.info`), a dict of scalars (`k8s.exec`), or a
scalar root. When such a payload serializes past the byte threshold,
the reducer keeps it **whole** as a document record under a handle:

- The inline summary is small and fixed in size: `result_record:
  "document"`, `root_type`, `document_bytes`, a `top_level_fields`
  outline (names and kinds, never values; 64 fields and 2 KB of JSON
  text at most), and the small top-level scalars (same bounded default
  rule as above). Every budget measures escaped JSON text, the way the
  MCP transport sends it, so a field name full of control characters
  cannot push the summary past its bound.
- `drill_in.example_call` uses `query={"path": "/<first field>"}`.
- The record is written whole or not at all. Over
  `RESULT_HANDLE_MAX_RECORD_BYTES` it is not stored
  (`reason=result_store_unavailable`); over the admission work limit it
  is not stored either (`reason=admission_limit_exceeded`).
- Credential-class ops and reduces without an `op_id` pass through
  unchanged, as before.

Read it with `result_query` and `query={"path": "<RFC 6901 JSON
Pointer>"}`. The pointer rules follow RFC 6901: `""` is the whole
document, each `/` starts one step, and in a step `~1` means `/` and `~0`
means `~` (in that order, so `/a~1b~0c` reads the key `a/b~c`). An array
index has no leading zero, and `-` never resolves. On a rows handle, the
document is the set of top-level fields left out of the inline summary
(see Overview). `path` cannot be combined with any relational field (a
`ValidationError` at spec construction). An array value pages with the
call's `offset` / `limit` (items that are not objects come back as
`{"value": item}`); any other value comes back whole in `value`, with
`value_type` naming its JSON type. A pointer that does not resolve
(missing member, `-`, a leading-zero or out-of-range index) is a
recoverable `QueryContractError`. So is a `path` read on a rows handle
that kept no fields beside its rows, or paging / a relational query on a
document handle. The same output
byte budget applies, and so does the terminal wire check (#3641).

## Completed-operation receipt (`audit_id`, #3636)

`OperationResult.audit_id` is the id of the dispatch audit row for the
call. It is set only after that row commits. A null `audit_id` means the
call gives no receipt: for example, the optional audit write of a safe
read failed, and the read still returned.

The receipt matters most when shaping the response fails after the
operation already ran:

- If the handler finished but the reducer failed, the result stays
  `status="ok"`. It has no `result` and no `handle`, and
  `delivery="unavailable"`. The remediation text says the operation
  already ran and must not be run again. The call is never relabelled
  `connector_error`, so a completed write never looks safe to repeat.
- This is not the same as a failed audit write. If the required audit
  row of a write does not commit, the call still returns an error, as
  before.
- The caller uses `audit_id` to find what happened (for example
  `meho audit show <audit-id>`).
- `delivery` is about shaping the response, not about how many rows a
  handle kept: `complete`, `partial` (the response was too large, but the
  `handle` can still be read with `result_query`), or `unavailable`. The
  details are in [`mcp.md`](mcp.md#response-wire-ceiling-3641).

A reduced result carries both the `handle` and the `audit_id`. The handle
is how to read the rows; the receipt is how to prove the call ran.

## Key types

| Symbol | Where | Role |
|---|---|---|
| `JsonFluxReducer._spill` / `_SpillOutcome` | `backend/src/meho_backplane/operations/jsonflux_reducer.py` | Persists the materialized rows; reports `stored_rows` **or** a machine-readable `skip_reason` |
| `FetchMoreDrillIn.reason` / `DrillInUnavailableReason` | `backend/src/meho_backplane/connectors/schemas.py` | The machine-readable no-spill cause on the wire |
| `ResultHandleStore.spill` / `fetch_window` / `fetch_rows` | `backend/src/meho_backplane/connectors/result_handle_store.py` | Fail-open Valkey persistence + operator/tenant-scoped read-back; `fetch_rows` returns the full authorized set for the query surface (#3366) |
| `ResultHandleStore.spill_document` / `fetch_document` / `ResultRecordKindError` | `backend/src/meho_backplane/connectors/result_handle_store.py` | Document records (#3633): stored whole or not at all; a read of the wrong record kind is a recoverable contract error |
| `parse_json_pointer` / `resolve_json_pointer` | `backend/src/meho_backplane/jsonflux/query/json_pointer.py` | RFC 6901 pointer syntax and resolution for `path` reads (#3633) |
| `document_outline` and friends | `backend/src/meho_backplane/operations/result_document.py` | The bounded inline view of a document record (#3633) |
| `read_result_window` / `run_result_query` / `ResultHandleNotFoundError` | `backend/src/meho_backplane/operations/result_query.py` | Transport-neutral windowed-read core (#3179) + bounded query core (#3366) both surfaces wrap; misses raise the recoverable not-found error |
| `build_catalog` / `ResultFieldCatalog` / `ResultField` / `AdmissionGuard` | `backend/src/meho_backplane/jsonflux/query/result_catalog.py` | The result field catalog built from every captured row; drives `schema_` at reduce time and field checks at query time (#3631, #3635) |
| `OperationResult.audit_id` / `OperationResult.delivery` | `backend/src/meho_backplane/connectors/schemas.py` | The committed-audit receipt and the response-shaping state of a call (#3636, #3641) |
| `compile_query` / `ResultQuerySpec` / `QueryContractError` / `OutputPlan` | `backend/src/meho_backplane/jsonflux/query/contract.py` | Compiles the validated query grammar against the result field catalog to one parameterized read-only `SELECT` plus an output plan (#3366, #3635) |
| `result_query` (MCP) | `backend/src/meho_backplane/mcp/tools/result_query.py` | MCP read surface; misses surface as recoverable `handle_not_found` (`-32602`) |
| `POST /api/v1/operations/result-query` | `backend/src/meho_backplane/api/v1/operations.py` | REST read surface; misses surface as a `404` with `reason=handle_not_found` (#3179) |
| `_reduce_or_error` | `backend/src/meho_backplane/operations/dispatcher.py` | Builds `reducer_context` (`tenant_id`, `operator_sub`, `op_id`, hints) from the authenticated `Operator` |
| `handle_owner_sub_var` | `backend/src/meho_backplane/operations/reducer.py` | Optional handle owner `sub`; the approval resume binds the parking principal so it owns the spill (#3548) |

## Control flow (dispatch path)

1. `dispatch()` runs the op, redacts, and calls the reducer with
   `reducer_context` — `operator.sub` always, `str(operator.tenant_id)`
   whenever the operator has a tenant. `Operator.tenant_id` and
   `Operator.sub` are **required** fields on the auth model, so an
   authenticated MCP/REST dispatch always carries both.
   One exception: an approval resume runs under the approver, but binds
   `handle_owner_sub_var` to the parking principal. Then `operator_sub` is
   the parker's `sub`, so the parker can read the handle that
   `GET /api/v1/approvals/{id}/result` gives it (#3548). See
   [`approvals.md`](approvals.md).
2. The reducer validates the captured payload and derives schema, summary,
   and preview directly from raw rows, then `_spill()`:
   - an admission-limit breach returns an unprofiled handle with no spill,
     reason `admission_limit_exceeded`;
   - no usable `tenant_id`/`operator_sub` (absent or non-UUID) →
     skip, reason `no_tenant_context`;
   - `ResultHandleStore.spill()` reports `stored_rows == 0` (Valkey
     unreachable, write rejected, or the guard `ttl/max_rows <= 0` /
     empty rows) → skip, reason `result_store_unavailable`;
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
  `ResultHandleStore.spill()` stored no rows — Valkey (the broadcast
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
   `handle_not_found`). For an approved op, the handle belongs to the
   principal that parked it, not the approver (#3548).

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
- Rows records and document records follow the same rule: both
  `spill` and `spill_document` write where the writer flag says.
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

## Release verification

Before a release that touches result reduction, the stored rows, or
`result_query`, check that these suites pass. Do not delete an assertion
to make one pass. If a fuller catalog shows more fields, update the
expected value on purpose and say why in the commit.

1. Reference pages are fresh (no drift, nothing internal):

   ```bash
   cd backend && uv run python scripts/generate_reference_docs.py && cd ..
   make -C cli cli-docs
   git diff --exit-code docs-site/reference/connectors.md \
     docs-site/reference/mcp-tools.md docs-site/reference/cli.md
   cd backend && uv run pytest tests/test_reference_docs_drift.py -q
   ```

2. Seeded property tests over many row orders:

   ```bash
   cd backend && uv run pytest tests/test_result_reduction_properties.py -q
   ```

3. Forced-handle acceptance, the `result_query` contract, and MCP
   conformance. The six acceptance suites start vendor simulators in
   containers. Without Docker they skip, so they only count when they run
   (CI runs them):

   ```bash
   cd backend && uv run pytest \
     tests/acceptance/test_g35_harbor_jsonflux_force_handle.py \
     tests/acceptance/test_g35_nsx_jsonflux_force_handle.py \
     tests/acceptance/test_g35_sddc_jsonflux_force_handle.py \
     tests/acceptance/test_g36_vrops_jsonflux_force_handle.py \
     tests/acceptance/test_g37_robot_jsonflux_force_handle.py \
     tests/acceptance/test_vmware_rest_jsonflux_force_handle.py \
     tests/test_jsonflux_query_contract.py \
     tests/test_operations_result_query_filter.py \
     tests/test_mcp_tool_result_query.py \
     tests/test_mcp_output_schema_conformance.py \
     tests/test_mcp_structured_content.py \
     tests/test_mcp_surface_conformance.py -q
   ```

4. Optional smoke against a real deployment. **Read-only:** use a list
   operation with no side effects, and never a write. It does not block
   a merge.
   - Run a read that returns more than 50 rows or 4 KB through the
     backplane: `meho operation call <connector_id> <op_id> --target
     <target> --json`.
   - Check the response: `status="ok"`, a `handle` with
     `fetch_more.drill_in.available=true`, and an `audit_id`.
   - Page the whole handle with `meho operation result-query <handle_id>
     --offset <n> --limit <m> --json`, and run one query with `--select`.
   - The rows from paging must match the rows from the query for the
     same fields, and `total_rows` must match the vendor's count.

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
  (256 KB) / `RESULT_QUERY_TIMEOUT_SECONDS` (10) bound a single query's
  output rows, serialized size, and wall time (#3366, #3640); all
  validated `> 0`.
- `RESULT_QUERY_EXECUTOR_WORKERS` (4) / `RESULT_QUERY_TENANT_CONCURRENCY`
  (4), validated `> 0`, and `RESULT_QUERY_RATE_PER_MINUTE` (0 = off),
  validated `>= 0`, bound how many result reads run at once and how often
  one principal may read (#3640).
- `RESULT_QUERY_RESPONSE_WIRE_MAX_BYTES` (2 MiB) bounds a whole read-back
  response on the wire (#3641); validated `> 0`.
- `RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES` (256 KiB) bounds a completed
  `call_operation` response the same way (#3641); validated `>= 2048`
  (`RESULT_COMPLETE_RESPONSE_WIRE_MIN_BYTES`) so the receipt always fits.
  A call whose receipt could not fit (an unusually long or non-ASCII
  `op_id`) is refused before it runs with `error_code="receipt_too_large"`.
  Over the ceiling, `delivery="partial"` keeps the handle only when its
  `fetch_more.drill_in.available` is true; a handle whose spill was skipped
  or failed is dropped and the response is `delivery="unavailable"`. Details
  in `docs/codebase/mcp.md`, "Response wire ceiling".
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
- #3548 — an approval resume's handle belongs to the parking principal.
- #3631 — the result field catalog built from every captured row.
- #3633 — document records and RFC 6901 `path` reads.
- #3635 — catalog-checked queries and raw-row output.
- #3636 — the `audit_id` receipt on a completed operation.
- RFC 6901 (JSON Pointer): https://www.rfc-editor.org/rfc/rfc6901
- `docs/architecture/jsonflux.md` — engine provenance + full reducer
  contract.
