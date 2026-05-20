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
- **CLAUDE.md postulate 5** — the agent surface is a narrow waist. Operators get five CLI verbs (per-shape conveniences); agents get one MCP tool (`query_audit`). The conveniences collapse into filter combinations on the same substrate handler.
- **Decision #3** ([v0.2-decisions.md](../planning/v0.2-decisions.md)) — `query_audit` is privacy-sensitive: a free-text broadcast of "operator X queried for principal Y on target Z" would leak the investigation intent. The G6.1 sensitivity classifier renders every `op_class="audit_query"` event as aggregate-only `{op_id, result_status, row_count}`; filter contents stay local to the operator.

## Module shape

The substrate lives in [`backend/src/meho_backplane/audit_query/`](../../backend/src/meho_backplane/audit_query/):

| File | What it owns |
|---|---|
| [`schemas.py`](../../backend/src/meho_backplane/audit_query/schemas.py) | `AuditQueryFilters` (input filter — frozen Pydantic v2), `AuditEntry` (one row), `AuditQueryResult` (page + forward-only `next_cursor`). `tenant_id` is deliberately **not** on `AuditQueryFilters` — the handler takes it as a mandatory keyword-only argument so an operator-controllable filter object cannot smuggle the tenant boundary. |
| [`cursor.py`](../../backend/src/meho_backplane/audit_query/cursor.py) | Opaque base64-encoded JSON `{"ts": "<ISO-8601>", "id": "<uuid>"}` cursor. Forward-only. `decode_cursor` raises `InvalidCursorError` on tamper or corruption; tests round-trip + reject malformed input. |
| [`query.py`](../../backend/src/meho_backplane/audit_query/query.py) | `query_audit(filters, *, tenant_id, session) -> AuditQueryResult` — the substrate handler T2 (REST), T3 (CLI), T4 (MCP) all dispatch through. Tenant-scoping baked in (first WHERE clause); LEFT JOIN to `targets` for `target_name` denormalization is *also* scoped on `Target.tenant_id` so a cross-tenant `target_id` resolves to `target_name=None` rather than leaking the other tenant's name. |
| [`duration.py`](../../backend/src/meho_backplane/audit_query/duration.py) | `parse_duration("24h" \| "7d" \| ISO-8601, *, now) -> datetime` for the router layer. Grammar wider than `retrieval/usage.py::parse_since` (which accepts `d` / `h` only) because audit forensics often wants sub-minute resolution and multi-week windows; unifying the two parsers is a v0.2.next surface. |

The engineering-facing companion doc — [`docs/codebase/audit_query.md`](../codebase/audit_query.md) — owns the field-by-field schema reconciliation (which `AuditEntry` fields are real columns, which are computed at query time, which are v0.2 placeholders), the control-flow diagram, and the catalogue of known v0.2 gaps.

## Surfaces

The substrate has three consumer-facing surfaces, all dispatching through `query_audit` with `tenant_id` injected from the operator JWT:

### REST — four routes under `/api/v1/audit/*` ([T2 #466](https://github.com/evoila/meho/issues/466))

| Route | Filter shape | Notes |
|---|---|---|
| `POST /api/v1/audit/query` | Body is `AuditQueryRequest`; `since` / `until` are strings parsed at the router via `parse_duration`. Client-supplied `tenant_id` is silently dropped by Pydantic's default `extra="ignore"`. | Full-filter surface. |
| `GET /api/v1/audit/who-touched/{target}` | Path param → `filters.target`; `since` query defaults to `"24h"`. | Pre-canned shortcut. |
| `GET /api/v1/audit/my-recent` | `filters.principal = operator.sub`; `since` defaults to `"24h"`. | Pre-canned shortcut — operator can never see another operator's `my-recent` through this route. |
| `GET /api/v1/audit/show/{audit_id}` | `filters.audit_id = <path>`, `limit=1`. Cross-tenant lookups → 404 (never 403, so existence never leaks). | Single-row fetch. |

Every route binds two audit-override contextvars **before** the substrate call — `audit_op_id="meho.audit.query"` and `audit_op_class="audit_query"` — so the audit row the chassis middleware writes for this request carries the canonical op_id and the broadcast event ships as aggregate-only per decision #3. `audit_row_count` is bound after the substrate returns so the broadcast event's `row_count` reflects the returned cardinality. The pattern mirrors [`api/v1/retrieve_usage.py`](../../backend/src/meho_backplane/api/v1/retrieve_usage.py).

### CLI — five verbs under `meho audit ...` ([T3 #467](https://github.com/evoila/meho/issues/467))

The CLI ([`cli/internal/cmd/audit/`](../../cli/internal/cmd/audit/)) ships `query`, `recent`, `show`, `who-touched`, and `my-recent`. Each verb wraps exactly one REST route and renders the response as either a tabular summary (default) or raw JSON (`--json`). Authentication piggybacks on the token `meho login` wrote — same pattern as `meho targets` and `meho retrieval`. The operator-facing recipe with worked examples is in the [cross-repo runbook](../cross-repo/audit-query.md).

### MCP — one meta-tool `query_audit` ([T4 #468](https://github.com/evoila/meho/issues/468))

The agent surface is a single tool registered against the [G0.5 MCP server](../../backend/src/meho_backplane/mcp/server.py) in [`mcp/tools/audit.py`](../../backend/src/meho_backplane/mcp/tools/audit.py). The per-shape CLI conveniences collapse into filter combinations on this tool per [CLAUDE.md](../../CLAUDE.md) postulate 5; no `audit.show` / `audit.who_touched` / `audit.my_recent` MCP tools exist.

Hand-built `inputSchema` (not `AuditQueryFilters.model_json_schema()`) because the substrate filter uses `datetime` for `since` / `until` and the agent passes duration shorthand — a `format: date-time` constraint on the substrate-derived schema would reject `"24h"` at the jsonschema layer. `additionalProperties: false` rejects smuggled `tenant_id` before the handler runs. Substrate / parser errors (`DurationParseError`, `InvalidCursorError`, `UnsupportedFilterError`, residual `pydantic.ValidationError`) are caught in the handler and re-raised as `McpInvalidParamsError` → JSON-RPC `-32602`.

## Tenant boundary

The tenant-scoping invariant is enforced redundantly at two layers:

1. **Substrate** — `query_audit(filters, *, tenant_id, session)` takes `tenant_id` as a mandatory keyword-only argument. `AuditQueryFilters` has no `tenant_id` field at all. The first SQL WHERE clause is always `audit_log.tenant_id = :tenant_id`; the LEFT JOIN to `targets` for `target_name` denormalization is *also* scoped on `Target.tenant_id`, so a cross-tenant `target_id` (allowed today because `audit_log.target_id` keeps no FK in v0.2 per the soft-FK discipline) resolves to `target_name=None` rather than leaking the other tenant's target name.
2. **Surface** — every route, every CLI verb, every MCP handler pulls `tenant_id` from `operator.tenant_id` (the JWT claim resolved by `verify_jwt_and_bind` / `verify_mcp_jwt_and_bind`) and passes it into the substrate. The REST POST body's Pydantic model has no `tenant_id` field, so a client-supplied value is silently dropped. The MCP tool's `inputSchema` has `additionalProperties: false`, so an agent that tries to smuggle `tenant_id` is rejected by the dispatcher's jsonschema layer with `-32602` before reaching the handler.

There is no admin escape hatch. Cross-tenant queries are structurally impossible, by design and by code. The `show` route surfaces 404 (not 403) for cross-tenant audit-ids so a probing operator cannot even distinguish "row doesn't exist" from "row exists in another tenant".

## Cursor pagination

Forward-only, opaque base64 of `{ts, id}`. The substrate orders rows `(occurred_at DESC, id DESC)` and uses an `N+1` fetch to detect whether more pages exist; if `N+1` rows come back, the (N+1)th row is dropped from the returned page and its `(occurred_at, id)` is encoded as `next_cursor`. The next-page query applies the lex-compare `(occurred_at, id) < (cursor.ts, cursor.id)` so the next row is strictly older than the cursor's row.

The cursor is **correct under concurrent insert load**: rows inserted between page N and page N+1 do not appear inside a cursor-paginated continuation (they're newer than the cursor's `(ts, id)`); they are visible only on a fresh page-1 call. This is the snapshot semantics operators expect from a forensic-reconstruction surface — what was true at the time the cursor was issued stays the page the cursor reads back.

`decode_cursor` raises `InvalidCursorError` on tamper or corruption; the surfaces map it to 400 (REST) or `-32602` (MCP).

## Decision #3 alignment — aggregate-only audit broadcasts

The `audit_log` row this surface produces feeds into the [G6.1 sensitivity classifier](https://github.com/evoila/meho/issues/228), which renders every `op_class="audit_query"` event as aggregate-only on the per-tenant Valkey broadcast stream:

```json
{"op_id": "meho.audit.query", "op_class": "audit_query", "result_status": "ok", "row_count": 47}
```

The filter contents — which principal you queried, which target, which op_id glob — are **never** broadcast. The full audit row remains queryable (via this same surface, with the appropriate role on the appropriate tenant), but the live SSE / Slack feed sees only the aggregate.

### Two known chain gaps for the MCP path

T5 (acceptance integration, [#469](https://github.com/evoila/meho/issues/469)) is the gate that verifies the chain end-to-end. The MCP dispatch path ships with two known gaps that T5 will surface and either fix in place or escalate to standalone Tasks:

1. **MCP broadcast publisher does not honor `defn.op_class`.** [`mcp/handlers.py::_publish_mcp_event`](../../backend/src/meho_backplane/mcp/handlers.py) re-runs `classify_op(op_id)` instead of using the `op_class` the audit-write step already set from `defn.op_class`. For `query_audit` (`op_id="query_audit"` per the MCP tool-name-as-op-id dispatcher convention), `classify_op` returns `"other"` — so the broadcast event ships with the full audit payload instead of the aggregate-only shape decision #3 requires. The chassis HTTP publisher already honors the override ([`audit.py::_publish_broadcast_event:346-357`](../../backend/src/meho_backplane/audit.py)); the MCP publisher needs the same ~3-line change.
2. **MCP dispatcher's `audit_payload` is built statically.** No mechanism for the handler to surface `audit_row_count` to the audit row / broadcast event, so the MCP broadcast event carries `row_count: None` even when the handler returns N rows. The HTTP chassis path uses contextvar enrichment via `_resolve_audit_payload`; the MCP dispatcher would need an equivalent shim.

Both gaps are downstream of T4 and disclosed in #468's PR body (#505). T5's acceptance scope covers the integration verification that surfaces them.

### op_id asymmetry between REST and MCP

The REST surface emits `op_id="meho.audit.query"` (the canonical, bound via the `audit_op_id` contextvar override the chassis honors). The MCP surface emits `op_id="query_audit"` (the tool name verbatim, per the MCP dispatcher convention at [`mcp/handlers.py:199`](../../backend/src/meho_backplane/mcp/handlers.py)). Same logical operation; different identifiers per dispatch path. Operators forensically searching `audit_log` for "all audit-query calls regardless of surface" today need to OR both op_ids. Unifying via a `ToolDefinition.audit_op_id` override field or a tool rename is a v0.2.next surface.

## Audit-row channel convention (HTTP / MCP / INTERNAL)

The `audit_log.method` column is the **channel** the row was written from; `audit_log.path` is the **op identifier within that channel**. The split keeps audit-query filters partitionable by surface without joining on `path`, and stays auditable as new background-process writers land.

| `method` literal | Channel | Writer | Example `path` |
|---|---|---|---|
| `GET` / `POST` / `PATCH` / `DELETE` / etc. | Chassis HTTP | [`audit.py::_write_audit_row`](../../backend/src/meho_backplane/audit.py) (called by `AuditMiddleware`) | The HTTP route, e.g. `/api/v1/memory/{scope}/{slug}` |
| `MCP` | MCP JSON-RPC dispatch | [`mcp/audit.py::write_mcp_audit_row`](../../backend/src/meho_backplane/mcp/audit.py) (called by the per-op handler) | `/mcp/tools/call/{tool_name}` or `/mcp/resources/read/{uri}` |
| `INTERNAL` | Background-process / system | [`memory/audit.py::write_internal_audit_row`](../../backend/src/meho_backplane/memory/audit.py) (called by lifespan-owned tasks) | The op identifier, e.g. `memory.expire` |

`operator_sub` on every `INTERNAL` row is a stable synthetic identity (e.g. `"system"` for the G5.2-T1 memory-expiry sweeper; future writers should pick a stable name like `"system:retention-sweeper"`). Audit-query filters partition system-driven rows from operator-driven rows by `method = 'INTERNAL'` rather than by parsing `operator_sub`.

### Known `INTERNAL` `path` values

- `memory.expire` — the G5.2-T1 memory-expiry sweeper (#623). One row per affected tenant per sweep tick; payload is `{"expired_count": <int>, "scopes": ["memory-user", ...]}`. Forward reference for [G8.2 #219](https://github.com/evoila/meho/issues/219): `meho audit query --op memory.expire` will pick these rows up by path when the verb ships.

The convention is closed-set on the *channel* axis (HTTP / MCP / INTERNAL is the trichotomy) and open-set on the *op-id* axis (every new background writer reserves a new `path` value, documented in this section before the writer lands). The G6.1 sensitivity classifier treats `method = 'INTERNAL'` rows as non-sensitive by default (a system row reaping expired memory carries no operator intent to leak); writers handling tenant-sensitive payloads should request an `op_class` override via the same contextvar mechanism the HTTP path uses.

## Sibling Initiative

[G8.2 (#377)](https://github.com/evoila/meho/issues/377) — audit replay (`meho audit replay <session-id>`) — builds a recursive-CTE traversal on top of the `parent_audit_id` filter shipped in [T1 (#465)](https://github.com/evoila/meho/issues/465). G8.1 ships the filter (which raises `UnsupportedFilterError` in v0.2 because the column hasn't landed yet); G8.2 ships the schema column, the recursive traversal, and the operator-facing `replay` verb. The split keeps each Initiative shippable on its own clock.

## References

- Implementation Initiative: [G8.1 #334](https://github.com/evoila/meho/issues/334).
- Substrate tasks: [T1 #465](https://github.com/evoila/meho/issues/465) (substrate), [T2 #466](https://github.com/evoila/meho/issues/466) (REST), [T3 #467](https://github.com/evoila/meho/issues/467) (CLI), [T4 #468](https://github.com/evoila/meho/issues/468) (MCP), [T5 #469](https://github.com/evoila/meho/issues/469) (acceptance), [T6 #470](https://github.com/evoila/meho/issues/470) (this doc + the operator runbook).
- Companion docs: [`docs/cross-repo/audit-query.md`](../cross-repo/audit-query.md) (operator runbook), [`docs/codebase/audit_query.md`](../codebase/audit_query.md) (engineering-facing internal).
- Substrate primitives: [`broadcast.events.classify_op`](../../backend/src/meho_backplane/broadcast/events.py) (op_id → op_class lookup, shared with the broadcast publish path), [`broadcast.events.redact_payload`](../../backend/src/meho_backplane/broadcast/events.py) (aggregate-only redaction for `op_class="audit_query"`).
- Write paths: [`audit.py`](../../backend/src/meho_backplane/audit.py) (chassis HTTP `AuditMiddleware`), [`mcp/audit.py`](../../backend/src/meho_backplane/mcp/audit.py) (MCP `write_mcp_audit_row`).
- Schema: [`db/models.py`](../../backend/src/meho_backplane/db/models.py) (`AuditLog`, `Target`).
- Migrations: [`alembic/versions/0001_create_audit_log.py`](../../backend/alembic/versions/0001_create_audit_log.py), [`0002_create_tenant_and_audit_tenant_id.py`](../../backend/alembic/versions/0002_create_tenant_and_audit_tenant_id.py), [`0004_create_targets_and_audit_target_id.py`](../../backend/alembic/versions/0004_create_targets_and_audit_target_id.py), [`0006_add_audit_log_parent_audit_id.py`](../../backend/alembic/versions/0006_add_audit_log_parent_audit_id.py).
- Sibling Initiative: [G8.2 #377](https://github.com/evoila/meho/issues/377) (audit replay).
- Decision: [`v0.2-decisions.md` decision #3](../planning/v0.2-decisions.md) (PII defaults / aggregate-only audit broadcasts).
- Consumer needs: §G8 of [`consumer-needs.md`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/docs/meho-coordination/consumer-needs.md) L214-234.
