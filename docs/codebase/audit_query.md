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
| `parent_audit_id` | `UUID \| None` | **Raises `UnsupportedFilterError`** — the flat filter stays gated (un-gating is out of scope for #377). The column itself (#398) *is* read onto the returned row and is walked by the replay CTE. |
| `agent_session_id` | `UUID \| None` | `audit_log.agent_session_id = :value`. Un-gated by G8.2-T3 (#1011); the column landed with G8.2-T1 (#1009). |
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
| `op_class` | `payload['op_class']` if a string, else `classify_op(op_id)` from `broadcast.events`. For the MCP `call_operation` outer-wrapper row this is `"tool_call"` (G0.15-T3 #1212) — the inner DISPATCH row carries the domain `read` / `write` class. |
| `result_status` | Derived from `status_code` — 401/403 → `"denied"`, 4xx/5xx else → `"error"`, otherwise `"ok"`. |
| `principal_name` | `payload['principal_name']` when present (MCP rows since G0.15-T3 #1212; `write_mcp_audit_row` merges `Operator.name` from the validated JWT). HTTP-chassis rows remain `None` — the `verify_jwt_and_bind` middleware does not bind `name` to contextvars, so the audit middleware sees no source for it. |
| `parent_audit_id` | `audit_log.parent_audit_id` — composite-operation lineage column (G0.6-T7 #398, migration `0006`). Surfaced on the row since G8.2-T3 (#1011); the flat *filter* on it stays gated. |
| `agent_session_id` | `audit_log.agent_session_id` — MCP-session correlation column (G8.2-T1 #1009, migration `0014`). Surfaced + filterable since G8.2-T3 (#1011). |
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
  ├─ Validate filters: parent_audit_id → UnsupportedFilterError (flat filter
  │                     stays gated; agent_session_id is now a usable filter)
  │
  ├─ Build SELECT audit_log, targets.name
  │     OUTER JOIN targets ON audit_log.target_id = targets.id
  │                       AND targets.tenant_id   = :tenant_id   ← JOIN-scope defence
  │     WHERE audit_log.tenant_id = :tenant_id      ← always first
  │       [+ audit_id = :audit_id]
  │       [+ agent_session_id = :agent_session_id]
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
  │   ├─ principal_name = payload['principal_name'] if str
  │   │                  else None   # MCP rows since G0.15-T3 #1212
  │   │                              # carry it in payload; HTTP-chassis
  │   │                              # rows still have no source
  │   └─ AuditEntry(... real cols ..., op_id, op_class, result_status,
  │                 parent_audit_id=row.parent_audit_id,
  │                 agent_session_id=row.agent_session_id,
  │                 principal_name=principal_name, broadcast_event_id=None)
  │
  └─ next_cursor = encode_cursor(CursorPosition(ts=last.ts, id=last.id))
                   if has_more else None
```

## Per-session replay (G8.2-T3 #1011)

`replay.py` adds `replay_session(session_id, *, tenant_id, session,
max_depth=20) -> list[ReplayNode]` — the read brain behind `meho audit
replay <session-id>` (T5 CLI), the REST replay route (T4), and the MCP
replay surface (T6). It reconstructs one agent session as a chronologically
ordered parent/child forest.

`ReplayNode` subclasses `AuditEntry` (so it carries every audit field
verbatim — forward-compatible with the v0.2.next compliance-export contract)
and adds two structural fields: `depth` (0 for roots) and
`children: list[ReplayNode]`. The self-reference is resolved with a
module-level `ReplayNode.model_rebuild()`; both models stay `frozen=True`.

Two-step shape:

1. **Fetch the closure (recursive CTE — the first in the codebase).** The
   non-recursive arm seeds on `agent_session_id = :session_id AND tenant_id =
   :tenant_id`; the recursive arm joins child rows on
   `child.parent_audit_id = closure.id` *and* re-asserts `child.tenant_id =
   :tenant_id`. The tenant predicate on the recursive arm is load-bearing —
   without it a forged cross-tenant `parent_audit_id` would widen the closure
   into another tenant's rows. The CTE carries only ids; the full `AuditLog`
   rows + denormalized `target_name` are fetched in one follow-up
   `WHERE id IN (SELECT DISTINCT id FROM closure)`. `DISTINCT` collapses ids
   the CTE re-emits along more than one path (an anchor row that is also a
   descendant; a node in a cycle).

   The recursive CTE is belt-and-suspenders over a flat
   `WHERE agent_session_id = :id`: in v0.2 contextvars propagate the session id
   down nested `dispatch_child` calls, so session rows generally all carry the
   id — but a child whose id didn't propagate (NULL `agent_session_id`) is
   still pulled in via its `parent_audit_id` link to an anchored parent.

2. **Assemble the tree (Python).** Rows bucket by `parent_audit_id`; a row is a
   *root* when its parent is NULL, points outside the fetched set, or equals
   its own id (self-loop). Each bucket — and the root list — is ordered by
   `(occurred_at, id)`. A depth-first walk assigns `depth`, drops back-edges
   via a root-to-node path set, and caps at `max_depth` (a node at the cap
   keeps its row but `children` is truncated). A self-referential row or a
   multi-row cycle therefore terminates instead of recursing forever.

`replay_session` never raises `UnsupportedFilterError` — it is a positive
query, not a filtered list. It reuses `query._build_audit_entry` so a replay
node and a `query_audit` row for the same audit id agree field-for-field.

## REST surface (G8.1-T2 #466, G8.2-T4 #1012)

The five routes under `backend/src/meho_backplane/api/v1/audit.py` are the
operator-facing entry into the substrate. The first four dispatch through
`query_audit`; the fifth (replay) dispatches through `replay_session`. All
pass `tenant_id=operator.tenant_id` (from the JWT) — the substrate's
tenant-scoping invariant is enforced one layer up.

| Route | Filter shape | Notes |
|---|---|---|
| `POST /api/v1/audit/query` | Body is `AuditQueryRequest`; `since` / `until` are strings parsed at the router via `parse_duration` (`"24h"` / `"7d"` / ISO-8601). Client-supplied `tenant_id` (or any other unknown field) is rejected with 422 `extra_forbidden` (`AuditQueryRequest.model_config` sets `extra="forbid"`, G0.9-T2 / #729); the route never reads tenant from the body — it always passes `operator.tenant_id` from the JWT to the substrate. | Full-filter surface. |
| `GET /api/v1/audit/who-touched/{target}` | Path param becomes `filters.target`; `since` query defaults to `"24h"`. | Pre-canned shortcut. |
| `GET /api/v1/audit/my-recent` | `filters.principal = operator.sub`; `since` query defaults to `"24h"`. | Pre-canned shortcut. |
| `GET /api/v1/audit/show/{audit_id}` | `filters.audit_id = <path>`, `limit=1`. Substrate returns 0 rows for cross-tenant lookups → router raises **404** (not 403) so existence never leaks. | Single-row fetch. |
| `GET /api/v1/audit/sessions/{session_id}/replay` | Dispatches `replay_session(session_id, tenant_id=operator.tenant_id, ...)`. 200 body is `AuditReplayResult` (`{root: [ReplayNode], session_id, tenant_id, row_count}`). Unknown / foreign session → `root=[]` / `row_count=0` (**not** 404 — same non-leakage as `show`). `row_count > 10_000` → **413** `{"detail": "session_too_large", "row_count": n}` from a count-first guard run *before* the recursive tree build. | Per-session replay (G8.2-T4). |

The replay route's **413 cap** is a cheap tenant-scoped
`SELECT count(*) WHERE agent_session_id = :id AND tenant_id = :tid` (the
`_count_session_rows` helper, hitting `audit_log_agent_session_id_idx`),
evaluated before `replay_session` runs — a runaway session is rejected on
the count alone, never materializing a 10k-deep tree. The same count is
the `row_count` echoed in the 200 body, so a 200 reports the same number
its over-cap sibling would report at 413. NULL-session lineage children
pulled into `root` by the closure CTE are present in the tree but are not
counted — "session rows" are defined by the `agent_session_id` anchor.

Every route binds two audit-override contextvars **before** the substrate
call — `audit_op_class="audit_query"` (always) plus an `audit_op_id`:
`"meho.audit.query"` for the four query routes and `"meho.audit.replay"`
for the replay route (so operators can tell replay usage apart from
flat-query usage in `audit_log`). The shared `audit_query` op_class flips
the broadcast event to aggregate-only (`{op_id, result_status,
row_count}` only, never the request filter or the replayed `ReplayNode`
tree). The `audit_row_count` contextvar is bound after the substrate
returns — and on the 413 path before raising — so the broadcast event's
`row_count` field reflects the actual cardinality. The shape mirrors
`api/v1/retrieve_usage.py`.

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
* `UnsupportedFilterError` (substrate, the flat `parent_audit_id`
  filter, still gated in v0.2) — same mapping with the column-name
  message. The `agent_session_id` filter is **un-gated** (G8.2-T3
  #1011) and no longer raises.
* Anything else propagates; the dispatcher's outer try/except turns
  it into JSON-RPC `-32603` Internal Error.

RBAC: `operator` role minimum. `read_only` callers find the tool
absent from `tools/list` and get `-32602` on a direct `tools/call`
attempt (the dispatcher's per-call RBAC re-check, not a transport
401 — JSON-RPC has no 403 analogue).

### `shape="tree"` self-session replay (G8.2-T6 #1014)

`query_audit` grows a `shape` enum (`"flat"` default / `"tree"`). With
`shape="tree"` the handler short-circuits the flat filter path and
reconstructs the caller's session as a `ReplayNode` forest via
`replay_session`, returning `{root, session_id, tenant_id, row_count}`
(same envelope as the admin tool below).

The tree path is **self-session only** and intentionally stricter than
the flat path (which already returns other in-tenant principals' rows):
`agent_session_id` must be present **and** equal to the caller's own
bound MCP session id — the `mcp_session_id` structlog contextvar the
transport binds from the inbound `Mcp-Session-Id` header (G8.2-T2
#1010). Any mismatch — a different session id, an absent
`agent_session_id`, or no session header at all (the transport leaves
the contextvar unbound after G0.14-T6 #1147 decoupled capture from
enforcement — there is no synthetic uuid4 fallback for the client to
match against) — is rejected with `-32602`. Cross-session forensic
replay is the `tenant_admin`-gated `meho.audit.replay` tool, not this
path.

## `meho.audit.replay` admin tool (G8.2-T6 #1014)

A dedicated `tenant_admin` meta-tool in the `meho.*` admin namespace
(alongside `meho.broadcast.overrides.*`), registered in the same
`mcp/tools/audit.py` module. It is the cross-session escalation: an
admin replays *another* agent's session, where `query_audit`'s
`shape="tree"` path replays only *your own*.

* `inputSchema`: `{session_id: uuid (required), max_depth: int
  (1–100, default 20)}`, `additionalProperties: false`.
* Handler: count-first 10k guard (see below), then
  `replay_session(session_id, tenant_id=operator.tenant_id,
  session=…, max_depth=…)`. Tenant scope is the JWT's — never an
  argument — so an admin cannot replay another tenant's session (a
  foreign session id yields an empty `root`).
* Returns `{root: [ReplayNode…], session_id, tenant_id, row_count}`
  via `model_dump(mode="json")`; `row_count` is the total node count
  in the returned tree.
* `op_class="audit_query"`, so — via the matching `meho.audit.` arm
  in `classify_op` — the MCP broadcast event is aggregate-only
  (`{op_class, result_status, row_count}`), never the `ReplayNode`
  payload.

**Count-first 10k guard.** Both replay surfaces share
`_build_replay_response`, which counts the tenant-scoped anchor rows
(`agent_session_id = :id`) *before* the recursive walk + tree
assembly. The anchor count is a sound lower bound on the closure size
(the CTE only adds lineage descendants), so an over-cap session is
rejected with `-32602` carrying the `session_too_large` token — the
MCP analogue of T4's REST 413, since the JSON-RPC transport has no
streaming body for a partial response. The cap (`_REPLAY_MAX_ROWS =
10_000`) matches T4 so operators see the same boundary on both
surfaces.

### `meho.audit.` broadcast-classifier arm

The MCP broadcast path derives `op_class` from `classify_op(op_id)`
with the **tool name verbatim** — it does not honor
`ToolDefinition.op_class`. `classify_op` therefore grew a
`meho.audit.` prefix arm next to the existing `audit.` arm: without
it, `meho.audit.replay` (prefix `meho.audit.`) would fall through to
`other` and broadcast its full `ReplayNode` tree instead of the
aggregate-only view. (The literal tool name `query_audit` has the same
MCP-path classification gap today — a pre-existing G8.1 concern, out
of scope for #377.)

## Dependencies

* `meho_backplane.db.models.AuditLog` — read schema; this package never writes.
* `meho_backplane.db.models.Target` — denormalization source for `target_name`.
* `meho_backplane.broadcast.events.classify_op` — op_id → op_class lookup;
  shared with the broadcast publish path so audit-query and broadcast classify
  identically.

Reverse dependencies:

* `meho_backplane.api.v1.audit` (T2 #466) — REST router for the four
  consumer-facing routes; dispatches every call through `query_audit`.
* `meho_backplane.mcp.tools.audit` (T4 #468, extended G8.2-T6 #1014) —
  the `query_audit` MCP meta-tool plus the `meho.audit.replay` admin
  tool; dispatches flat queries through `query_audit` and replay
  (admin tool + `shape="tree"`) through `replay_session`.
* T3 #467 (CLI) follows.

## BFF (operator UI) audit coverage (G0.15-T7 #1216)

Every authenticated ``/ui/<surface>`` GET / HEAD writes one ``audit_log`` row
attributed to the BFF session's operator. The binding lives in
:func:`meho_backplane.ui.auth.middleware.require_ui_session` — the FastAPI
dependency every UI route declares (directly or transitively via
``require_ui_admin``). On entry the dependency calls
:func:`meho_backplane.ui.audit.bind_ui_view_audit`, which binds four
structlog contextvars the chassis :class:`AuditMiddleware` reads on the
response side:

| Contextvar | Value | Lands on `audit_log` as |
|---|---|---|
| ``operator_sub`` | Session's stable subject id | typed ``operator_sub`` column |
| ``tenant_id`` | ``str(session.tenant_id)`` | typed ``tenant_id`` column |
| ``audit_op_id`` | ``ui.view.<surface>`` (see table below) | ``payload.op_id`` |
| ``audit_op_class`` | ``"ui_view"`` (constant ``UI_AUDIT_OP_CLASS``) | ``payload.op_class`` |

Surface mapping (single source of truth in
``backend/src/meho_backplane/ui/audit.py``):

| URL prefix | Surface | op_id |
|---|---|---|
| ``/ui/`` | dashboard | ``ui.view.dashboard`` |
| ``/ui/broadcast`` (+ subpaths) | broadcast | ``ui.view.broadcast`` |
| ``/ui/connectors`` (+ subpaths) | connectors | ``ui.view.connectors`` |
| ``/ui/kb`` (+ subpaths) | kb | ``ui.view.kb`` |
| ``/ui/memory`` (+ subpaths) | memory | ``ui.view.memory`` |
| ``/ui/topology`` (+ subpaths) | topology | ``ui.view.topology`` |

The ``op_class="ui_view"`` is a new class (the consumer's
``claude-rdc-hetzner-dc#753`` v0.7.0 closed-loop dogfood "Option B")
distinct from the agent path's ``read`` / ``write``. Operators who want
UI page views in their forensic timeline query
``op_class=ui_view``; operators who want to prune them filter them
out — and a retention policy can drop them independently of the
governance-load-bearing agent dispatch trail.

Target-scoped page views (e.g. ``/ui/connectors/<name>``) also populate
the typed ``audit_log.target_id`` column. That binding is unchanged
from G0.3-T4 — :func:`meho_backplane.targets.resolver.resolve_target`
binds ``target_id`` into structlog at its single exit point, and the
audit middleware reads it into the row. The JOIN against ``targets``
in :func:`query_audit` then surfaces ``target_name`` on the returned
``AuditEntry``.

Skipped paths:

* ``/ui/auth/*`` — login / callback / logout. Unauthenticated by design;
  the session middleware bypasses the audit-thread binding and the
  AuditMiddleware's general no-``operator_sub``-skip applies.
* ``/ui/static/*`` — vendored JS + compiled CSS. Bypassed at the session
  middleware so unauthenticated browsers render styled login pages.
* POST / PATCH / DELETE on ``/ui/*`` — service-layer functions
  (``create_target``, ``update_target``, ``forget_memory``, etc.) write
  their own audit row under the canonical ``<surface>.<verb>`` op_id /
  ``op_class=write`` discipline. Binding ``ui_view`` here would
  double-attribute every state change. ``operator_sub`` / ``tenant_id``
  are still bound on non-GET so a write-path route that happens to
  bypass the service-layer writer still produces a row (under the
  default ``http.<method>:<path>`` op_id) rather than disappearing.

Pre-fix gap (closed by G0.15-T7): the chassis audit middleware skips
requests with no ``operator_sub`` contextvar, and only
``require_ui_admin`` — a dependency a small subset of write surfaces
chain — bound it. Every read GET through ``require_ui_session`` left
zero audit footprint, so an operator browsing 5 surfaces generated
zero rows under their sub. The substrate now has full BFF coverage
parity with the ``/api/v1/*`` chassis path and the MCP transport path.

## Known issues / v0.2 gaps

* **`principal_name` populates for MCP rows; HTTP-chassis rows still
  return `None`.** Since G0.15-T3 (#1212) the MCP audit writer
  (`mcp/audit.py:write_mcp_audit_row`) merges `Operator.name` into
  `payload['principal_name']` whenever the JWT carries it, and the
  audit-query handler reads that key off the JSON column into the
  returned `AuditEntry`. The HTTP audit middleware
  (`meho_backplane.audit._write_audit_row`) reads `operator_sub` from
  contextvars but never the JWT `name` claim, so HTTP rows remain
  `principal_name=None`. Closing the HTTP gap is a small write-path
  follow-up: bind `audit_principal_name` in `verify_jwt_and_bind` and
  let the `_AUDIT_PAYLOAD_PREFIX` machinery push it into `payload`;
  the handler already reads it out, so no read-side change is needed.

* **`parent_audit_id` flat filter stays gated.** The column is real (#398,
  migration `0006`) and is now read onto the returned row + walked by the
  replay CTE, but the *flat* `query_audit(parent_audit_id=...)` filter still
  raises `UnsupportedFilterError` — un-gating it was deliberately out of scope
  for G8.2 (#377) to keep the diff surgical. A future task can drop the arm and
  add a `WHERE parent_audit_id = :parent_audit_id` clause; the column read is
  already wired.

* **`agent_session_id` is live.** The column landed with G8.2-T1 (#1009 —
  nullable + indexed); G8.2-T2 writes it from the MCP `Mcp-Session-Id`
  header; G8.2-T3 (#1011) un-gated the filter, surfaced the column on the
  returned row, and added `replay.py` for the per-session tree query.
  G0.14-T6 (#1147) decoupled capture from enforcement: any
  `Mcp-Session-Id` the client sends is captured into `agent_session_id`
  regardless of the `MCP_REQUIRE_SESSION_ID` env var (which now strictly
  gates the missing-header reject), so the replay-tree filter has data
  to walk on default deploys. Calls with no header (or a malformed one)
  land `agent_session_id` as NULL — the recursive CTE filters those out
  of the session walk naturally (no synthetic per-call uuid4 polluting
  the search). Operators can confirm the deploy's mode at
  `GET /api/v1/health`'s `mcp_session_id_capture` field
  (`"always"` / `"enforced"`).

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
  [G8.1-T3 #467](https://github.com/evoila/meho/issues/467) (CLI verbs),
  [G8.1-T4 #468](https://github.com/evoila/meho/issues/468) (MCP
  meta-tool),
  [G8.1-T6 #470](https://github.com/evoila/meho/issues/470)
  (architecture + operator runbook docs).
* Companion docs:
  [`docs/architecture/audit.md`](../architecture/audit.md) (canonical
  architecture reference for the substrate + decision-#3 alignment),
  [`docs/cross-repo/audit-query.md`](../cross-repo/audit-query.md)
  (operator-facing runbook with the five CLI verbs and forensic
  example queries).
* Write paths: `backend/src/meho_backplane/audit.py`,
  `backend/src/meho_backplane/mcp/audit.py`.
* Op-class classifier: `backend/src/meho_backplane/broadcast/events.py:172`
  (`classify_op`).
* Schema: `backend/src/meho_backplane/db/models.py:285-372` (`AuditLog`),
  `backend/src/meho_backplane/db/models.py:602-...` (`Target`).
* Migrations: `backend/alembic/versions/0001_create_audit_log.py`,
  `backend/alembic/versions/0002_create_tenant_and_audit_tenant_id.py`,
  `backend/alembic/versions/0004_*` (target_id on audit_log).
