<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Audit query (G8)

> Reads [CLAUDE.md](../../CLAUDE.md) postulates 5 (narrow-waist agent surface) and 7 (audit is synchronous, append-only, WORM-grade). Sister to [operations-substrate.md](operations-substrate.md) — that doc owns the dispatcher and the `endpoint_descriptor` table the audit log records dispatches against; this doc owns the read-side substrate operators and agents use to query those records.
>
> Covers the implementation that landed under [Initiative #334 G8.1](https://github.com/evoila/meho/issues/334).

## What this surface does

One sentence: queryable interface to the WORM-grade `audit_log` table that the chassis middleware writes synchronously on every authenticated dispatch, replacing the `git log` + ticket-thread + Slack-search archaeology operators previously stitched together when answering "who did X to Y and when?".

The substrate operationalises three contracts:

- **CLAUDE.md postulate 7** — every authenticated dispatch writes one audit row, append-only, before the response yields back. G8.1 ships the read surface that turns those rows into investigation answers.
- **CLAUDE.md postulate 5** — the agent surface is a narrow waist. Operators get nine CLI verbs under `meho audit` (per-shape conveniences, replay, counts and traces). Agents get two MCP tools: `query_audit` and the `tenant_admin`-gated `meho_audit_replay`. The query conveniences collapse into filter combinations on the same substrate handler.
- **Decision #3** ([locked-decisions.md](../decisions/locked-decisions.md)) — an audit query is privacy-sensitive: its filters show what an operator is looking into. Every audit-query row carries `op_class="audit_query"`. MEHO posts no broadcast event for it: the broadcast feed carries only agent announcements (see [Audit queries and the broadcast feed](#audit-queries-and-the-broadcast-feed)). Filter contents stay on the audit row.

## Module shape

The substrate lives in [`backend/src/meho_backplane/audit_query/`](../../backend/src/meho_backplane/audit_query/):

| File | What it owns |
|---|---|
| [`schemas.py`](../../backend/src/meho_backplane/audit_query/schemas.py) | `AuditQueryFilters` (input filter — frozen Pydantic v2), `AuditEntry` (one row), `AuditQueryResult` (page + forward-only `next_cursor`). `tenant_id` is deliberately **not** on `AuditQueryFilters` — the handler takes it as a mandatory keyword-only argument so an operator-controllable filter object cannot smuggle the tenant boundary. |
| [`cursor.py`](../../backend/src/meho_backplane/audit_query/cursor.py) | Opaque base64-encoded JSON `{"ts": "<ISO-8601>", "id": "<uuid>"}` cursor. Forward-only. `decode_cursor` raises `InvalidCursorError` on tamper or corruption; tests round-trip + reject malformed input. |
| [`query.py`](../../backend/src/meho_backplane/audit_query/query.py) | `query_audit(filters, *, tenant_id, session) -> AuditQueryResult` — the substrate handler T2 (REST), T3 (CLI), T4 (MCP) all dispatch through. Tenant-scoping baked in (first WHERE clause); LEFT JOIN to `targets` for `target_name` denormalization is *also* scoped on `Target.tenant_id` so a cross-tenant `target_id` resolves to `target_name=None` rather than leaking the other tenant's name. |
| [`replay.py`](../../backend/src/meho_backplane/audit_query/replay.py) | `replay_session` — rebuilds one agent session as a parent/child tree with a recursive CTE. Backs `meho audit replay`, the REST replay route and the MCP replay tools. |
| [`policy_replay.py`](../../backend/src/meho_backplane/audit_query/policy_replay.py) | Runs the recorded redaction policy again on a row's stored raw payload, to check that it still gives the same redacted view. |
| [`loose_rows.py`](../../backend/src/meho_backplane/audit_query/loose_rows.py) | Read-only list of loose rows: step rows whose parent row never arrived, because MEHO stopped during a multi-step operation. |
| [`duration.py`](../../backend/src/meho_backplane/audit_query/duration.py) | `parse_duration("24h" \| "7d" \| ISO-8601, *, now) -> datetime` for the router layer. Grammar wider than `retrieval/usage.py::parse_since` (which accepts `d` / `h` only) because audit forensics often wants sub-minute resolution and multi-week windows; unifying the two parsers is a v0.2.next surface. |

The engineering-facing companion doc — [`docs/codebase/audit_query.md`](../codebase/audit_query.md) — owns the field-by-field schema reconciliation (which `AuditEntry` fields are real columns, which are computed at query time, which are v0.2 placeholders), the control-flow diagram, and the catalogue of known v0.2 gaps.

## Surfaces

The substrate has three consumer-facing surfaces, all dispatching through `query_audit` with `tenant_id` injected from the operator JWT:

### REST — routes under `/api/v1/audit/*` ([T2 #466](https://github.com/evoila/meho/issues/466))

| Route | Filter shape | Notes |
|---|---|---|
| `POST /api/v1/audit/query` | Body is `AuditQueryRequest`; `since` / `until` are strings parsed at the router via `parse_duration`. Client-supplied `tenant_id` (or any other unknown field) is rejected at 422 `extra_forbidden` — `AuditQueryRequest` sets `extra="forbid"` (G0.9-T2 #729). | Full-filter surface. |
| `GET /api/v1/audit/who-touched/{target}` | Path param → `filters.target`; `since` query defaults to `"24h"`. | Pre-canned shortcut. |
| `GET /api/v1/audit/by-work-ref/{ref}` | Path param → `filters.work_ref` (exact match). | Pre-canned shortcut: every write done under one change ticket. |
| `GET /api/v1/audit/my-recent` | `filters.principal_sub = operator.sub` (exact match); `since` defaults to `"24h"`. Answers the `{items, next_cursor}` list envelope. | Pre-canned shortcut — operator can never see another operator's `my-recent` through this route. |
| `GET /api/v1/audit/show/{audit_id}` | `filters.audit_id = <path>`, `limit=1`. Cross-tenant lookups → 404 (never 403, so existence never leaks). | Single-row fetch. |
| `GET /api/v1/audit/{audit_id}/trace` | One dispatch's flight-recorder trace. | Read-only. See [`dispatch-flight-recorder.md`](../decisions/dispatch-flight-recorder.md). |
| `GET /api/v1/audit/sessions/{session_id}/replay` | One agent session as a parent/child tree, through `replay_session`. Needs `tenant_admin`. An unknown or foreign session answers an empty tree, not 404. | Cross-session replay. |
| `POST /api/v1/audit/class-counts` | The `query` filters; answers one row count per event class ([`audit_counts.py`](../../backend/src/meho_backplane/api/v1/audit_counts.py)). | Counts only. |
| `GET /api/v1/audit/reflex` | Reflex-adoption numbers per surface, read from request usage events, not `audit_log` ([`audit_reflex.py`](../../backend/src/meho_backplane/api/v1/audit_reflex.py)). | Empty when `REQUEST_USAGE_RETENTION_DAYS` is off. |

Every route binds two audit-override contextvars **before** the substrate call — `audit_op_id="meho.audit.query"` and `audit_op_class="audit_query"` — so the audit row the chassis middleware writes for this request carries the canonical op_id and the `audit_query` class. The replay route writes under `op_id="meho_audit_replay"`. `audit_row_count` is bound after the substrate returns, so the row records how many rows the query returned. The pattern mirrors [`api/v1/retrieve_usage.py`](../../backend/src/meho_backplane/api/v1/retrieve_usage.py).

### CLI — verbs under `meho audit ...` ([T3 #467](https://github.com/evoila/meho/issues/467))

The CLI ([`cli/internal/cmd/audit/`](../../cli/internal/cmd/audit/)) ships `query`, `count`, `recent`, `show`, `who-touched`, `my-recent`, `replay`, `reflex`, and `trace` (`trace list` / `trace show`). Each verb wraps a REST route and renders the response as either a tabular summary (default) or raw JSON (`--json`). Authentication piggybacks on the token `meho login` wrote — same pattern as `meho targets` and `meho retrieval`. The operator-facing recipe with worked examples is in the [cross-repo runbook](../cross-repo/audit-query.md).

### MCP — `query_audit` and `meho_audit_replay` ([T4 #468](https://github.com/evoila/meho/issues/468))

The agent surface is two tools registered against the [G0.5 MCP server](../../backend/src/meho_backplane/mcp/server.py) in [`mcp/tools/audit.py`](../../backend/src/meho_backplane/mcp/tools/audit.py). `query_audit` is `operator`-gated; with `shape="tree"` it replays only the caller's own session. `meho_audit_replay` replays any session in the tenant and needs `tenant_admin`. Both are operator-plane tools, not on the default working surface ([`docs/codebase/mcp.md`](../codebase/mcp.md)). The per-shape CLI conveniences collapse into filter combinations on `query_audit` per [CLAUDE.md](../../CLAUDE.md) postulate 5; no `audit.show` / `audit.who_touched` / `audit.my_recent` MCP tools exist.

Hand-built `inputSchema` (not `AuditQueryFilters.model_json_schema()`) because the substrate filter uses `datetime` for `since` / `until` and the agent passes duration shorthand — a `format: date-time` constraint on the substrate-derived schema would reject `"24h"` at the jsonschema layer. `additionalProperties: false` rejects smuggled `tenant_id` before the handler runs. Substrate / parser errors (`DurationParseError`, `InvalidCursorError`, `UnsupportedFilterError`, residual `pydantic.ValidationError`) are caught in the handler and re-raised as `McpInvalidParamsError` → JSON-RPC `-32602`.

## Tenant boundary

The tenant-scoping invariant is enforced redundantly at two layers:

1. **Substrate** — `query_audit(filters, *, tenant_id, session)` takes `tenant_id` as a mandatory keyword-only argument. `AuditQueryFilters` has no `tenant_id` field at all. The first SQL WHERE clause is always `audit_log.tenant_id = :tenant_id`; the LEFT JOIN to `targets` for `target_name` denormalization is *also* scoped on `Target.tenant_id`, so a cross-tenant `target_id` (allowed today because `audit_log.target_id` keeps no FK in v0.2 per the soft-FK discipline) resolves to `target_name=None` rather than leaking the other tenant's target name.
2. **Surface** — every route, every CLI verb, every MCP handler pulls `tenant_id` from `operator.tenant_id` (the JWT claim resolved by `verify_jwt_and_bind` / `verify_mcp_jwt_and_bind`) and passes it into the substrate. The REST POST body's Pydantic model has no `tenant_id` field, and a client-supplied value (like any unknown field) is rejected at 422 `extra_forbidden` per the model's `extra="forbid"` config (G0.9-T2 #729). The MCP tool's `inputSchema` has `additionalProperties: false`, so an agent that tries to smuggle `tenant_id` is rejected by the dispatcher's jsonschema layer with `-32602` before reaching the handler.

There is no admin escape hatch. Cross-tenant queries are structurally impossible, by design and by code. The `show` route surfaces 404 (not 403) for cross-tenant audit-ids so a probing operator cannot even distinguish "row doesn't exist" from "row exists in another tenant".

### Tenant-broadcast read surfaces ([#2084](https://github.com/evoila/meho/issues/2084))

Within a tenant, the audit ledger is deliberately **not** principal-scoped. `POST /api/v1/audit/query` — with or without a `principal` filter — and `GET /api/v1/audit/show/{audit_id}` return rows for **every** principal in the calling tenant. Any `operator` or `tenant_admin` in the tenant reads all principals' audit rows (the RBAC gate on these routes is `operator`-minimum; `read_only` gets 403), and there is **no per-principal role check** on `query` / `show`: filtering on another operator's `principal` sub is a supported forensic lookup, not an escalation. Only `GET /api/v1/audit/my-recent` is self-scoped — it pins `filters.principal = operator.sub` from the JWT, so it never returns another principal's rows.

This tenant-broadcast visibility is by design, on the same shelf as the `who-touched` shortcut (which likewise surfaces every principal that touched a target) and the aggregate-only broadcast posture above — and the same category-2 doc-resolution shape as the kb same-slug cross-principal write semantics (#1327, [`docs/codebase/kb.md`](../codebase/kb.md)): a forensics surface has to be readable cross-principal for the operator investigating an incident, and the principals a tenant contains (operators, the service account, `system:*` writers) are mutually trusted within that tenant. Two boundaries still hold:

- **Cross-tenant isolation stays enforced.** The substrate's first WHERE clause is always `audit_log.tenant_id = :tenant_id`, and `show` surfaces 404 (never 403) on a foreign `audit_id` — see the module and `show` docstrings in [`api/v1/audit.py`](../../backend/src/meho_backplane/api/v1/audit.py).
- **`params_hash` scrubbing keeps payloads protected.** Dispatch audit rows carry a keyed hash of the canonicalised operation params ([`operations/_validate.py::compute_audit_params_hash`](../../backend/src/meho_backplane/operations/_validate.py)): HMAC-SHA256 with a server key, with secret-named values such as `password` left out. The raw params never land on the audit row in v0.2. A row visible cross-principal therefore reveals *that* an operation ran (op_id, target, status), never its sensitive argument values — the tenant-broadcast behavior does not expose the payload. How the key is made: [`docs/codebase/approvals.md`](../codebase/approvals.md#the-params-hash-on-requests-and-audit-rows).

If a finer role gradation lands (e.g. a `tenant_member` with limited audit visibility), this scoping decision is revisited at that point.

## Cursor pagination

Forward-only, opaque base64 of `{ts, id}`. The substrate orders rows `(occurred_at DESC, id DESC)` and uses an `N+1` fetch to detect whether more pages exist; if `N+1` rows come back, the (N+1)th row is dropped from the returned page and its `(occurred_at, id)` is encoded as `next_cursor`. The next-page query applies the lex-compare `(occurred_at, id) < (cursor.ts, cursor.id)` so the next row is strictly older than the cursor's row.

The cursor is **correct under concurrent insert load**: rows inserted between page N and page N+1 do not appear inside a cursor-paginated continuation (they're newer than the cursor's `(ts, id)`); they are visible only on a fresh page-1 call. This is the snapshot semantics operators expect from a forensic-reconstruction surface — what was true at the time the cursor was issued stays the page the cursor reads back.

`decode_cursor` raises `InvalidCursorError` on tamper or corruption; the surfaces map it to 400 (REST) or `-32602` (MCP).

## Audit queries and the broadcast feed

Decision #3 asks that the filters of an audit query stay private. MEHO meets this because it posts no broadcast event for an audit query at all. The broadcast feed carries only deliberate agent announcements; MEHO posts no automatic event for any operation, REST call or MCP tool call (the `tools/call` writer in [`mcp/handlers.py`](../../backend/src/meho_backplane/mcp/handlers.py) and `AuditMiddleware` in [`audit.py`](../../backend/src/meho_backplane/audit.py) say so in their comments). The audit row is the record. It keeps `op_class="audit_query"` and the filter contents, and only this same surface reads it back, with the right role in the right tenant.

### op_id asymmetry between REST and MCP

The REST surface emits `op_id="meho.audit.query"` (the canonical, bound via the `audit_op_id` contextvar override the chassis honors). The MCP surface emits `op_id="query_audit"` (the tool name verbatim: the MCP dispatcher seeds the row's `op_id` with the tool name in [`mcp/handlers.py`](../../backend/src/meho_backplane/mcp/handlers.py)). Same logical operation; different identifiers per dispatch path. A tool handler can bind `audit_op_id` to give its MCP row a dotted op_id (the docs tools do, for example `meho.docs.search`), but `query_audit` does not. So a search of `audit_log` for "all audit-query calls regardless of surface" needs to OR both op_ids.

## Audit-row channel convention (HTTP / MCP / AGENT / INTERNAL)

The `audit_log.method` column is the **channel** the row was written from; `audit_log.path` is the **op identifier within that channel**. The split keeps audit-query filters partitionable by surface without joining on `path`, and stays auditable as new background-process writers land.

| `method` literal | Channel | Writer | Example `path` |
|---|---|---|---|
| `GET` / `POST` / `PATCH` / `DELETE` / etc. | Chassis HTTP | [`audit.py::_write_audit_row`](../../backend/src/meho_backplane/audit.py) (called by `AuditMiddleware`) | The HTTP route, e.g. `/api/v1/memory/{scope}/{slug}` |
| `MCP` | MCP JSON-RPC dispatch | [`mcp/audit.py::write_mcp_audit_row`](../../backend/src/meho_backplane/mcp/audit.py) (called by the per-op handler) | `/mcp/tools/call/{tool_name}` or `/mcp/resources/read/{uri}` |
| `AGENT` | Hosted agent tool call | [`agent/tool_audit.py::write_agent_tool_row`](../../backend/src/meho_backplane/agent/tool_audit.py) (called by the `ToolCallAudit` capability on every agent run) | `/agent/tools/call/{tool_name}` |
| `INTERNAL` | Background-process / system | [`memory/audit.py::write_internal_audit_row`](../../backend/src/meho_backplane/memory/audit.py) (called by lifespan-owned tasks) | The op identifier, e.g. `memory.expire` |

With `AUDIT_WRAPPER_ROWS_RETIRED=true` (#3757) the chassis HTTP and MCP writers stop writing routine rows. Reads, searches, dry runs, operation calls and validation failures leave only a request usage event; a refused request writes a `policy_decision` row; a MEHO change with no committed record of its own (including a change whose own record rolled back with it) writes an `operation_execution` row. The rules and the startup check are in [`docs/codebase/request-usage.md`](../codebase/request-usage.md#retiring-the-wrapper-rows). The `AGENT` writer is not affected: hosted agent tool calls keep their row (#4234).

`operator_sub` on every `INTERNAL` row is a stable synthetic identity (e.g. `"system"` for the G5.2-T1 memory-expiry sweeper; future writers should pick a stable name like `"system:retention-sweeper"`). Audit-query filters partition system-driven rows from operator-driven rows by `method = 'INTERNAL'` rather than by parsing `operator_sub`.

### Known `INTERNAL` `path` values

- `memory.expire` — the G5.2-T1 memory-expiry sweeper (#623). One row per affected tenant per sweep tick; payload is `{"expired_count": <int>, "scopes": ["memory-user", ...]}`. `meho audit query --op-id '*memory.expire'` finds these rows: the `op_id` filter also matches `http.<method>:<path>`, here `http.internal:memory.expire`.
- `topology.history.prune` — the G9.3-T6 (#858) topology-history retention prune. One row per non-no-op tick (default weekly cadence), `operator_sub='system:topology-history-retention'`, `tenant_id` is the per-deploy sentinel `00000000-0000-0000-0000-0000000858a1` (system-wide op, not attributable to a real tenant — it relies on the `audit_log.tenant_id` soft-FK so the sentinel writes without a matching `tenant` row). Payload is `{"dropped_node_rows": N, "dropped_edge_rows": M, "retention_days": D, "cutoff": <iso-ts>}`. `TOPOLOGY_HISTORY_RETENTION_DAYS=0` is the opt-out "keep forever" sentinel: the prune loop still runs as a heartbeat but every tick is a no-op and **no audit row is written** for the no-op case (weekly empty-payload rows would flood `audit_log`). Any other run writes its row, also when it deleted 0 rows: the prune reads the newest row as its last run, so a pod restart does not start its interval again (see [`docs/codebase/topology.md`](../codebase/topology.md#history-retention-prune-g93-t6-858)). The sentinel tenant is not an operator's tenant, so `meho audit query` (always scoped to the caller's tenant) does not return these rows; read them in the database.
- `flight_recorder.trace.prune` — the #3212 dispatch flight-recorder retention reaper (F4 of [`docs/decisions/dispatch-flight-recorder.md`](../decisions/dispatch-flight-recorder.md)). One row per **non-empty** sweep (default hourly cadence), `operator_sub='system:flight-recorder-retention'`, `tenant_id` is the per-deploy sentinel `00000000-0000-0000-0000-000000003212` (system-wide op — same soft-FK use as the topology sentinel). Payload is `{"dropped_trace_headers": N, "dropped_trace_spans": M, "cutoff": <iso-ts>}`. An empty sweep writes **no** audit row (hourly `dropped=0` rows would flood `audit_log`). The reaper deletes only `dispatch_trace` headers + `dispatch_trace_span` children whose stamped `expires_at` has passed; the referenced dispatch `audit_log` row — the record of account — is **never** touched. The sentinel tenant is not an operator's tenant, so `meho audit query` (always scoped to the caller's tenant) does not return these rows; read them in the database.
- `audit.raw_payload.prune` — the security-review S19 (#307) raw-payload age-off sweeper ([`audit_retention.py`](../../backend/src/meho_backplane/audit_retention.py)). One row per run (default weekly cadence), also when the run emptied 0 rows, `operator_sub='system:audit-raw-payload-retention'`, `tenant_id` is the per-deploy sentinel `00000000-0000-0000-0000-000000000307` (system-wide op — same soft-FK use as the topology / flight-recorder sentinels). Payload is `{"nulled_raw_payload_rows": N, "retention_days": D, "cutoff": <iso-ts>}`. The sweeper runs a bounded `UPDATE audit_log SET raw_payload = NULL WHERE occurred_at < cutoff AND raw_payload IS NOT NULL`: it NULLs **only** the pre-redaction `raw_payload` body (G11.4-T2 #1071) so a rotated secret echoed in a connector response does not outlive its rotation forever; the redacted `payload` (carrying `redaction_policy_id`), the `redaction_manifest`, and the `audit_log` row itself — the record of account — are **never** removed (the row is updated in place, not deleted). `RAW_PAYLOAD_RETENTION_DAYS=0` is the opt-out "keep forever" sentinel (the loop runs as a heartbeat but every tick is a no-op) — un-redacted bodies then live in `raw_payload` and every backup indefinitely, the documented security tradeoff. Only the `0` sentinel writes **no** audit row (nothing ran). A run that nulled zero rows still writes its row with `nulled_raw_payload_rows: 0`, because the row is the record of the run that the schedule reads (see the next point). The sentinel tenant is not an operator's tenant, so `meho audit query` (always scoped to the caller's tenant) does not return these rows; read them in the database.
  - **When it runs.** The interval (`RAW_PAYLOAD_PRUNE_INTERVAL_SECONDS`, default 7 days) counts from the **last recorded run**, not from pod start. So a pod restart (an update, a crash, a node drain) does not start the interval again:
    - After a pod starts, the age-off waits one minute, so it does not run in the middle of startup.
    - Then it reads the time of the newest run of this job within the last interval. A row counts only if it has the system tenant id above, `method='INTERNAL'`, `operator_sub='system:audit-raw-payload-retention'` and the `audit.raw_payload.prune` path. So a dispatch audit row with the same tenant id and path never counts.
    - If there is no such run (none within the last interval, or a fresh install), it runs at once. Otherwise it waits only for the rest of the interval.
    - After that it runs once per interval.
    - A last-run time in the future (a pod with a fast clock wrote it) counts as "due now", so it never pushes the schedule back. If the read fails, it logs `audit_raw_payload_retention_last_run_read_failed` and runs at once. The wait is never longer than one interval.
    - Each start logs `audit_raw_payload_retention_scheduled` with `last_run_at` (the newest run within the last interval, or `null`) and `next_run_in_seconds`, so you can check the schedule after a restart.
    - Running twice is safe: a second run finds no rows left to empty. Two pods may run it at the same time; each writes its own row.
    - The read looks only at rows from the last interval. PostgreSQL either uses the `audit_log_tenant_id_idx` index (only this job writes its system tenant id, so there are few such rows) or reads the `occurred_at` index over the last interval only. So the read stays cheap on a large table.
  - On PostgreSQL the `audit_log` append-only trigger (migration `0103`) allows only one kind of `UPDATE`: one that sets `raw_payload` from a value to SQL `NULL` and leaves every other column value unchanged, byte for byte. This is not limited to the sweeper: any role with `UPDATE` on `audit_log` can do it, on any row of any age. Every other `UPDATE` and every `DELETE` is rejected. The trigger does not stop a role that owns the table (in the default chart, the app role), a superuser, or a role with the `TRIGGER` or `TRUNCATE` right on `audit_log` (it can add its own trigger or empty the table). Treat those two rights like ownership and do not grant them (`GRANT ALL` includes both). All of this was already true before `0103`. Before `0103`, the trigger rejected the age-off too, so `raw_payload` was never emptied on PostgreSQL. A failed sweep logs `audit_raw_payload_retention_tick_failed` at error level with `error_type`. See [`docs/codebase/migrations.md`](../codebase/migrations.md) for the full table.

The convention is closed-set on the *channel* axis (HTTP / MCP / AGENT / INTERNAL, the four rows of the table) and open-set on the *op-id* axis (every new background writer reserves a new `path` value, documented in this section before the writer lands).

## Sibling Initiative

[G8.2 (#377)](https://github.com/evoila/meho/issues/377) — audit replay (`meho audit replay <session-id>`) — has shipped. It builds a recursive-CTE traversal ([`replay.py`](../../backend/src/meho_backplane/audit_query/replay.py)) on the `parent_audit_id` column. The `replay` CLI verb, the REST replay route and the `meho_audit_replay` MCP tool all use it. The flat `parent_audit_id` filter of [T1 (#465)](https://github.com/evoila/meho/issues/465) is an exact match that returns the direct children of one row. The operator recipe is in [`docs/cross-repo/audit-replay.md`](../cross-repo/audit-replay.md).

## References

- Implementation Initiative: [G8.1 #334](https://github.com/evoila/meho/issues/334).
- Substrate tasks: [T1 #465](https://github.com/evoila/meho/issues/465) (substrate), [T2 #466](https://github.com/evoila/meho/issues/466) (REST), [T3 #467](https://github.com/evoila/meho/issues/467) (CLI), [T4 #468](https://github.com/evoila/meho/issues/468) (MCP), [T5 #469](https://github.com/evoila/meho/issues/469) (acceptance), [T6 #470](https://github.com/evoila/meho/issues/470) (this doc + the operator runbook).
- Companion docs: [`docs/cross-repo/audit-query.md`](../cross-repo/audit-query.md) (operator runbook), [`docs/codebase/audit_query.md`](../codebase/audit_query.md) (engineering-facing internal).
- Substrate primitives: [`broadcast.events.classify_op`](../../backend/src/meho_backplane/broadcast/events.py) (op_id → op_class lookup, shared with the broadcast publish path), [`broadcast.events.redact_payload`](../../backend/src/meho_backplane/broadcast/events.py) (aggregate-only redaction for `op_class="audit_query"`).
- Write paths: [`audit.py`](../../backend/src/meho_backplane/audit.py) (chassis HTTP `AuditMiddleware`), [`mcp/audit.py`](../../backend/src/meho_backplane/mcp/audit.py) (MCP `write_mcp_audit_row`).
- Schema: [`db/models.py`](../../backend/src/meho_backplane/db/models.py) (`AuditLog`, `Target`).
- Migrations: [`alembic/versions/0001_create_audit_log.py`](../../backend/alembic/versions/0001_create_audit_log.py), [`0002_create_tenant_and_audit_tenant_id.py`](../../backend/alembic/versions/0002_create_tenant_and_audit_tenant_id.py), [`0004_create_targets_and_audit_target_id.py`](../../backend/alembic/versions/0004_create_targets_and_audit_target_id.py), [`0006_add_audit_log_parent_audit_id.py`](../../backend/alembic/versions/0006_add_audit_log_parent_audit_id.py).
- Sibling Initiative: [G8.2 #377](https://github.com/evoila/meho/issues/377) (audit replay).
- Decision: [`locked-decisions.md` decision #3](../decisions/locked-decisions.md) (PII defaults / aggregate-only audit broadcasts).
- Consumer needs: §G8 of `consumer-needs.md` L214-234 (an internal document).
