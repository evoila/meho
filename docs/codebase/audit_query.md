# audit_query — the read-side substrate for audit-log queries

## Overview

`backend/src/meho_backplane/audit_query/` is the substrate G8.1's REST routes
(T2 #466), CLI verbs (T3 #467), and MCP meta-tool (T4 #468) dispatch through.
It exposes one async handler — `query_audit(filters, *, tenant_id, session)` —
and the three Pydantic value types around it: `AuditQueryFilters` (input),
`AuditEntry` (one row), `AuditQueryResult` (page + forward-only cursor).

The substrate is **read-only**. Audit-log rows are written by
`meho_backplane.audit.AuditMiddleware` (HTTP chassis routes) and
`meho_backplane.mcp.audit.write_mcp_audit_row` (MCP tool/resource handlers);
this package never inserts, updates, or deletes.

## Key types

### `AuditQueryFilters` — frozen Pydantic v2

| Field | Type | Backend |
|---|---|---|
| `target` | `str \| None` | Matched against `targets.name` for the same tenant (alias resolution is the T2 router's job). |
| `principal` | `str \| None` | `audit_log.operator_sub ILIKE %value%`. |
| `op_id` | `str \| None` | Glob (`*` ↔ `%`). Matched against `payload->>'op_id'` (MCP rows) **OR** derived `http.<method>:<path>` (HTTP rows). |
| `op_class` | `str \| None` | Exact match against `payload->>'op_class'`. |
| `result_status` | `str \| None` | One of `"ok"` / `"error"` / `"denied"`. Maps to status-code ranges. |
| `since` / `until` | `datetime \| None` | `audit_log.occurred_at` bracket. **Absolute datetimes only** — `"24h"` / `"7d"` shorthand is parsed in the T2 / T3 router. |
| `audit_id` | `UUID \| None` | Exact-id lookup. |
| `parent_audit_id` | `UUID \| None` | **Raises `UnsupportedFilterError`** in v0.2 — column lands with G0.6-T7 (#398). |
| `agent_session_id` | `UUID \| None` | **Raises `UnsupportedFilterError`** in v0.2 — column not on any current roadmap. |
| `limit` | `int` (1-1000) | Default 100. |
| `cursor` | `str \| None` | Opaque forward-only cursor produced by a prior page. |

`tenant_id` is **not** on this model. The handler takes it as a mandatory
keyword-only argument so an operator-controllable filter object cannot smuggle
a tenant boundary. The first WHERE clause is always
`audit_log.tenant_id = :tenant_id`.

### `AuditEntry` — one row

Field-to-source mapping:

| `AuditEntry` field | Source |
|---|---|
| `id` | `audit_log.id` |
| `ts` | `audit_log.occurred_at` |
| `tenant_id` | `audit_log.tenant_id` |
| `principal_sub` | `audit_log.operator_sub` |
| `target_id` | `audit_log.target_id` |
| `target_name` | LEFT JOIN `targets.name ON audit_log.target_id = targets.id AND targets.tenant_id = :tenant_id`. The tenant-id half of the ON clause is defence-in-depth: `audit_log.target_id` has no FK in v0.2 (soft column per chassis discipline) so a cross-tenant value resolves to `target_name=None` rather than leaking another tenant's name. |
| `method` / `path` / `status_code` / `request_id` / `duration_ms` / `payload` | Columns of the same name on `audit_log`. |
| `op_id` | `payload['op_id']` if a string, else `f"http.{method.lower()}:{path}"`. |
| `op_class` | `payload['op_class']` if a string, else `classify_op(op_id)` from `broadcast.events`. |
| `result_status` | Derived from `status_code` — 401/403 → `"denied"`, 4xx/5xx else → `"error"`, otherwise `"ok"`. |
| `principal_name` | **None in v0.2** — JWT `name` claim is not captured by either write path. |
| `parent_audit_id` | **None in v0.2** — column lands with G0.6-T7 (#398). |
| `agent_session_id` | **None in v0.2** — no roadmap column. |
| `broadcast_event_id` | **None in v0.2** — FK direction is reversed: `BroadcastEvent.audit_id` points at the audit row. |

The three computed fields use exactly the same rules
`meho_backplane.audit._publish_broadcast_event` applies on the publish side,
so a row returned by the query API and a `BroadcastEvent` observed on the SSE
feed for the same `audit_id` agree on the `(op_id, op_class, result_status)`
trio.

### `AuditQueryResult` — page

`rows: list[AuditEntry]` plus `next_cursor: str | None`. `next_cursor` is None
when fewer than `limit` rows were available on the page (the query reached the
end of the matching set under the current filter).

## Control flow

```text
query_audit(filters, tenant_id, session)
  │
  ├─ Validate filters: parent_audit_id / agent_session_id → UnsupportedFilterError
  │
  ├─ Build SELECT audit_log, targets.name
  │     OUTER JOIN targets ON audit_log.target_id = targets.id
  │                       AND targets.tenant_id   = :tenant_id   ← JOIN-scope defence
  │     WHERE audit_log.tenant_id = :tenant_id      ← always first
  │       [+ audit_id = :audit_id]
  │       [+ operator_sub ILIKE :principal]
  │       [+ occurred_at >= :since]
  │       [+ occurred_at <= :until]
  │       [+ target_id IN (SELECT id FROM targets WHERE tenant_id = :tenant_id
  │                        AND name = :target)]
  │       [+ (payload->>'op_id' LIKE :pattern OR 'http.' || lower(method) || ':' || path LIKE :pattern)]
  │       [+ payload->>'op_class' = :op_class]
  │       [+ status_code <predicate for result_status>]
  │       [+ (occurred_at, id) < (:cursor.ts, :cursor.id)]    ← cursor lex compare
  │     ORDER BY occurred_at DESC, id DESC
  │     LIMIT :limit + 1                                       ← N+1 to detect "more"
  │
  ├─ Fetch rows; has_more = (len > limit); page = rows[:limit]
  │
  ├─ For each (audit_log, target_name) row:
  │   ┌─ payload = dict(row.payload or {})
  │   ├─ op_id = payload['op_id'] if str else f"http.{method.lower()}:{path}"
  │   ├─ op_class = payload['op_class'] if str else classify_op(op_id)
  │   ├─ result_status = _derive_result_status(status_code)
  │   └─ AuditEntry(... real cols ..., op_id, op_class, result_status,
  │                 principal_name=None, parent_audit_id=None,
  │                 agent_session_id=None, broadcast_event_id=None)
  │
  └─ next_cursor = encode_cursor(CursorPosition(ts=last.ts, id=last.id))
                   if has_more else None
```

## REST surface (G8.1-T2 #466)

The four routes under `backend/src/meho_backplane/api/v1/audit.py` are the
operator-facing entry into the substrate. All four dispatch through
`query_audit` with `tenant_id=operator.tenant_id` (from the JWT) — the
substrate's tenant-scoping invariant is enforced one layer up.

| Route | Filter shape | Notes |
|---|---|---|
| `POST /api/v1/audit/query` | Body is `AuditQueryRequest`; `since` / `until` are strings parsed at the router via `parse_duration` (`"24h"` / `"7d"` / ISO-8601). Client-supplied `tenant_id` is silently dropped by Pydantic's default `extra="ignore"` — the route never reads tenant from the body. | Full-filter surface. |
| `GET /api/v1/audit/who-touched/{target}` | Path param becomes `filters.target`; `since` query defaults to `"24h"`. | Pre-canned shortcut. |
| `GET /api/v1/audit/my-recent` | `filters.principal = operator.sub`; `since` query defaults to `"24h"`. | Pre-canned shortcut. |
| `GET /api/v1/audit/show/{audit_id}` | `filters.audit_id = <path>`, `limit=1`. Substrate returns 0 rows for cross-tenant lookups → router raises **404** (not 403) so existence never leaks. | Single-row fetch. |

Every route binds two audit-override contextvars **before** the substrate
call — `audit_op_id="meho.audit.query"` and `audit_op_class="audit_query"`
— so the audit row written by `AuditMiddleware` carries the canonical
op_id and the broadcast event ships as aggregate-only (`{op_id,
result_status, row_count}` only, never the request filter). The
`audit_row_count` contextvar is bound after the substrate returns so the
broadcast event's `row_count` field reflects the actual returned
cardinality. The shape mirrors `api/v1/retrieve_usage.py`.

RBAC: `operator` role minimum. `read_only` → 403; `tenant_admin` → 200.

Error mapping at the router boundary: `DurationParseError` (router-side),
`InvalidCursorError` (substrate-side), `UnsupportedFilterError`
(substrate-side) all surface as 400 with the underlying message.

## MCP tool surface (G8.1-T4 #468)

The agent-facing entry is a single tool — `query_audit` — registered in
`backend/src/meho_backplane/mcp/tools/audit.py` against the G0.5 tool
registry. The CLAUDE.md narrow-waist postulate (#5) says the agent
sees exactly one audit tool; the REST per-shape shortcuts
(`who-touched` / `my-recent` / `show`) collapse into filter
combinations on `query_audit`.

The handler:

1. Receives the jsonschema-validated arguments dict.
2. Parses `since` / `until` via the same
   `meho_backplane.audit_query.parse_duration` the REST surface uses, so
   the agent can pass either ISO-8601 or duration shorthand.
3. Builds `AuditQueryFilters` via `model_validate` (Pydantic coerces
   UUID strings; `additionalProperties: false` on the schema already
   rejected unknown keys at the dispatcher).
4. Calls `await query_audit(filters, tenant_id=operator.tenant_id,
   session=session)`.
5. Returns `result.model_dump(mode="json")` — the dispatcher wraps it
   in the MCP `content` array.

Tenant scoping mirrors the REST surface: `operator.tenant_id` comes
from the validated JWT (resolved by `verify_mcp_jwt_and_bind`); the
arguments dict never carries `tenant_id` (rejected by the schema).
Cross-tenant probes are structurally impossible.

Audit + broadcast contract:

* The MCP dispatcher (`mcp/handlers.py`) writes one `audit_log` row
  per `tools/call`. For this tool the row's `op_id` is the tool name
  verbatim — `"query_audit"` — not the REST surface's
  `"meho.audit.query"`. The asymmetry is the dispatcher's convention
  (HTTP middleware reads a contextvar override; MCP dispatcher uses
  the tool name). Unifying the two op_ids is a v0.2.next surface.
* `op_class="audit_query"` on the `ToolDefinition` is the declarative
  contract for the broadcast classifier's aggregate-only redaction
  per `broadcast/events.py::redact_payload`. T5 (#469) is the
  end-to-end acceptance gate for the wiring; T4 ships the declarative
  registration only.

Error mapping at the tool boundary:

* `DurationParseError` (`since` / `until` shorthand) — caught in the
  handler, raised as `McpInvalidParamsError` → JSON-RPC `-32602`.
* `pydantic.ValidationError` (post-jsonschema, e.g. malformed UUID
  slipping past `format: uuid`) — same mapping.
* `InvalidCursorError` (substrate, tampered cursor) — same mapping.
* `UnsupportedFilterError` (substrate, `parent_audit_id` /
  `agent_session_id` in v0.2) — same mapping with the column-name
  message.
* Anything else propagates; the dispatcher's outer try/except turns
  it into JSON-RPC `-32603` Internal Error.

RBAC: `operator` role minimum. `read_only` callers find the tool
absent from `tools/list` and get `-32602` on a direct `tools/call`
attempt (the dispatcher's per-call RBAC re-check, not a transport
401 — JSON-RPC has no 403 analogue).

## Dependencies

* `meho_backplane.db.models.AuditLog` — read schema; this package never writes.
* `meho_backplane.db.models.Target` — denormalization source for `target_name`.
* `meho_backplane.broadcast.events.classify_op` — op_id → op_class lookup;
  shared with the broadcast publish path so audit-query and broadcast classify
  identically.

Reverse dependencies:

* `meho_backplane.api.v1.audit` (T2 #466) — REST router for the four
  consumer-facing routes; dispatches every call through `query_audit`.
* `meho_backplane.mcp.tools.audit` (T4 #468) — single MCP meta-tool
  registered against the G0.5 registry; dispatches every call through
  `query_audit`.
* T3 #467 (CLI) follows.

## Known issues / v0.2 gaps

* **`principal_name` never populates.** The HTTP audit middleware
  (`meho_backplane.audit._write_audit_row`) reads `operator_sub` from
  contextvars but never the JWT `name` claim; the MCP audit writer
  (`mcp/audit.py:write_mcp_audit_row`) takes an `Operator` value object that
  has a `name` field but does not persist it. Closing this is a small write-
  path follow-up: bind `audit_principal_name` in `verify_jwt_and_bind` and
  let the `_AUDIT_PAYLOAD_PREFIX` machinery push it into `payload`. The
  audit-query handler then reads it out and stops returning None.

* **`parent_audit_id` waits on G0.6-T7 (#398).** Once the column lands and the
  composite-operation dispatcher (Initiative #388) populates it, the
  audit-query handler drops the `UnsupportedFilterError` arm and adds a
  `WHERE parent_audit_id = :parent_audit_id` clause. `AuditEntry` already
  exposes the field — only the column read needs to wire up.

* **`agent_session_id` has no roadmap column.** Field is on `AuditEntry`
  as None and the filter raises. If consumer demand crystallises, the
  follow-up is a column + write-path binding + the same one-line filter add.

* **`op_id` / `op_class` glob filtering is JSON-path-based.** On PostgreSQL
  the `payload->>'op_id'` lookup runs over the JSONB column without an index
  in v0.2; for the consumer's typical 7-day-window queries, the row count
  is small enough that the index gap is acceptable. A functional GIN index
  on `(payload->'op_id')` becomes economic at v0.2.next scale.

* **`target` filter resolves names only, not aliases.** The router layer
  (T2 / T3) is expected to resolve a name-or-alias input via the G0.3 helpers
  before constructing the filter object. The substrate filter stays portable
  across PG (`TEXT[]`) and SQLite (JSON array) by keeping alias handling
  outside the SQL.

* **In-memory PG fixture.** AC4 in the Task body asks for "in-memory PG
  fixture"; the chassis pattern is `sqlite+aiosqlite` for unit tests +
  testcontainers PG for integration. T1 unit tests use SQLite per the
  chassis convention. PG-specific behaviour (JSONB ops, `TEXT[]` aliases)
  is covered by the T5 (#469) acceptance integration.

## References

* Initiative: [G8.1 #334](https://github.com/evoila/meho/issues/334).
* Tasks: [G8.1-T1 #465](https://github.com/evoila/meho/issues/465) (substrate),
  [G8.1-T2 #466](https://github.com/evoila/meho/issues/466) (REST routes +
  duration parser),
  [G8.1-T4 #468](https://github.com/evoila/meho/issues/468) (MCP
  meta-tool).
* Write paths: `backend/src/meho_backplane/audit.py`,
  `backend/src/meho_backplane/mcp/audit.py`.
* Op-class classifier: `backend/src/meho_backplane/broadcast/events.py:172`
  (`classify_op`).
* Schema: `backend/src/meho_backplane/db/models.py:285-372` (`AuditLog`),
  `backend/src/meho_backplane/db/models.py:602-...` (`Target`).
* Migrations: `backend/alembic/versions/0001_create_audit_log.py`,
  `backend/alembic/versions/0002_create_tenant_and_audit_tenant_id.py`,
  `backend/alembic/versions/0004_*` (target_id on audit_log).
