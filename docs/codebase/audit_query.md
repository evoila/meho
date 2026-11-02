# audit_query — the read-side substrate for audit-log queries

## Overview

`backend/src/meho_backplane/audit_query/` is the substrate G8.1's REST routes
(T2 #466), CLI verbs (T3 #467), and MCP meta-tool (T4 #468) dispatch through.
It exposes one async handler — `query_audit(filters, *, tenant_id, session)` —
and the three Pydantic value types around it: `AuditQueryFilters` (input),
`AuditEntry` (one row), `AuditQueryResult` (page + forward-only cursor).

The substrate is **read-only**. Audit-log rows are written by
`meho_backplane.audit.AuditMiddleware` (HTTP chassis routes),
`meho_backplane.mcp.audit.write_mcp_audit_row` (MCP tool/resource handlers),
the dispatcher, the approval queue and the domain writers; this package never
inserts, updates, or deletes.

### Event class (#3733)

Every row carries `event_class`, a closed set enforced by the database
CHECK `ck_audit_log_event_class` (migration `0106`). Each writer sets the
class it knows. Nothing is guessed from `method` / `path`.

| Class | Written by |
|---|---|
| `operation_execution` | Dispatch rows that ran (`ok` or `error`), the row of a multi-step operation that stopped part-way because a step waits for approval (`awaiting_approval`) or was denied (`denied`, #4275), runbook abort, gateway command mint / result, satellite effect rows, topology / ingest / add-on writes, sweepers and reapers. |
| `policy_decision` | Dispatch rows a gate refused (`denied`, rate limit, unqualified target version), the `approval.request` park row, and the `denied` row of a step inside a multi-step operation. |
| `approval_decision` | `approval.decision` rows (approve, reject, expire) and the standing-grant auto-approval row. |
| `security_event` | Refresh-token replay, broken satellite effect chain, un-reported gateway mint alarm. |
| `legacy_request` | Every row written before `0106` (server default; old rows are not reclassified), the REST and MCP request wrapper rows, and the `approval.result` read row. |

### Results a handler returns: error, wait, deny (#4274, #4275)

A typed or multi-step handler can return a result instead of raising one.
MEHO writes one `operation_execution` row for it under the operation's own
`audit_id`. So the rows of the steps that already ran (their
`parent_audit_id`), the approval request of a step that waits, and the
flight-recorder trace all point at a row that exists. The caller gets the
same result as before, plus that `audit_id`. The row records the real
elapsed time.

- **Error** (#4274): `result_status="error"`, status code 500, the result's
  `error_code` under `payload.error`.
- **A step waits for approval** (#4275): `result_status="awaiting_approval"`,
  status code 202, `payload.error.approval_request_id`. The step's own
  `approval.request` row (a `policy_decision`) has this row as its parent.
- **A step is denied** (#4275): `result_status="denied"`, status code 403,
  the reason under `payload.error`. The step gets its own `denied`
  `policy_decision` row with this row as its parent. This holds for a plain
  policy "no" and for a step refused before it could wait (#3542).

The step's decision stays on the step's decision row; the operation's row
says that its code ran, even if only partly (#3733). `denied` on an
operation's row does **not** mean that nothing changed: steps before the
refused one may have written. The rows under it, and the trace when one was
recorded, show what ran.

**Saving order.** A step's records are saved when the step ends (its step
row, its deny row, or its approval request together with its
`approval.request` row). The operation's row is saved last, after the
handler returns, in its own transaction, and is never changed later. While
the operation runs, the step records point at an id with no row yet. After
the call returns the row exists, except in two cases:

- The database does not save it. The wait and deny rows use
  `require_audit=True`, like the `ok` row, so the caller gets
  `audit_commit_failed` (`execution_outcome="uncertain"`, `retry=false`,
  no `audit_id`), never `awaiting_approval` or `denied`. For a wait the
  result keeps `approval_request_id`; that request is saved and still
  waits. An error row stays fail-open: the caller gets the error without
  an `audit_id`.
- MEHO stops before the row is saved (crash, restart, cancelled call).
  The step records stay, their parent id never gets a row, and the replay
  view shows them as loose rows. MEHO writes no repair row later. A saved
  approval request stays valid; approving it runs the second run as usual.

**After approval.** The second run gets a new `audit_id`. Its
`parent_audit_id` is the `approval.request` row, on the person's path and
on the agent's path. So the chain is second run → approval request → first
run, with no new column. The first run keeps `awaiting_approval` forever;
an approval never changes it to `ok`. If the second run waits again, it
gets its own `awaiting_approval` row.

**Finding these rows.** The `result_status` filter reads the status code,
so a waiting run shows as `pending` (202). `pending` is history: the row
stays `pending` after the request is approved, rejected or expired. Use
`event_class=operation_execution` to see only the stopped runs, not the
`approval.request` rows. To see what waits now, use the approval queue
(`meho approvals list`, or the console's approvals page). Decision record:
[`docs/decisions/composite-audit-when-a-step-waits.md`](../decisions/composite-audit-when-a-step-waits.md).

### Loose rows: the standing check (#4670)

A **loose row** is an audit record whose `parent_audit_id` names a row
that does not exist. It is what the crash case above leaves behind: the
step rows and the step's `approval.request` row were saved, the
operation's own row was not. It is also what a multi-step operation
looks like while it still runs: the operation saves its own row last, so
its finished steps are loose until it ends. A loose row alone does not
tell the two cases apart. A crash leaves the rows loose for good, and
MEHO never repairs them, so an operator needs a standing check. Three
pieces give it:

- **The query.** `audit_query.find_loose_rows(*, tenant_id, session,
  limit=200, recent_window=1 day, now=None) -> LooseRowsResult`
  (`audit_query/loose_rows.py`). Read-only: two `SELECT` statements, no
  write. It lists the tenant's loose rows newest first (`LooseRow`: id,
  time, principal, operation, event class, path, parent id), with the
  exact total `count` and `recent_count` (rows younger than the window),
  `recent_since`, and `truncated` when the list is capped (`limit` ≤
  1000). A parent "exists" when a row with that id exists in the same
  tenant, the same rule the replay closure uses. `event_class` tells a
  step row (`operation_execution`) and an approval request
  (`policy_decision`, path `approval.request`) apart.
- **The operation.** `audit.loose_rows` on the synthetic connector
  `audit-integrity-1.x` (`connectors/audit/ops.py`) wraps the query as a
  targetless, `safe` typed op so a Sensor can pin it. Params: `limit`,
  `recent_window_seconds`. The result is set-shaped: over the JSONFlux
  threshold the reducer moves `rows` behind a result handle, and the
  op's `result_scalars` hint keeps `count` / `recent_count` /
  `recent_since` / `truncated` inline so an assertion still reads them.
  The op id classifies as `audit_query`, so its broadcast event is
  aggregate-only. Its own dispatch is audited like any operation; that
  row is the record of the check, not a repair.
- **The sensor template.** `connectors/audit/sensor_template.py` holds
  `LOOSE_ROWS_SENSOR_TEMPLATE`, a ready `POST /api/v1/sensors` body
  (`loose_rows_sensor_template(name=...)` returns a copy): once an hour,
  assertion `$.recent_count` with `threshold gt` — `degraded` above 0
  (at least one loose row younger than a day), `critical` above 10;
  `severity=critical` so the dashboard cap does not hide the critical
  band. A sample taken while a multi-step operation runs can show
  `degraded` and clear on the next run. `tests/test_audit_loose_rows.py`
  proves the thresholds on recorded answers.

  ```json
  {
    "name": "audit-loose-rows",
    "connector_id": "audit-integrity-1.x",
    "op_id": "audit.loose_rows",
    "target": null,
    "params": {},
    "assertion": {
      "select": {"path": "$.recent_count"},
      "compare": {"type": "threshold", "op": "gt", "degraded": 0, "critical": 10}
    },
    "cadence_kind": "interval",
    "interval_seconds": 3600,
    "severity": "critical"
  }
  ```

The replay view shows the same thing per session: every replay envelope
(REST, MCP, console, `meho audit replay`) carries `loose_row_count`, the
nodes of the tree whose parent id names no row in the tenant
(`audit_query.count_loose_rows_in_forest`). Such a node renders as a
root, so the count tells it apart from a real start of a chain; a root
whose parent lives in another session is not counted. The console shows
the count as a badge above the tree, and the CLI prints one note after
the tree when the count is above zero.

### Class counters and execution-only traces (#3747)

- `count_event_classes(filters, *, tenant_id, session)` returns one row
  count per class for the same filters as `query_audit`, except
  `event_class`, `cursor` and `limit`. Every class is a key, `0` when it
  has no rows. Old rows count as `legacy_request`.
- Without `since` it counts only the last 24 hours
  (`DEFAULT_COUNT_WINDOW`). The audit log keeps every row, so an open
  count would read the whole tenant history on every console load. The
  result (`EventClassCounts`) carries the `since` it used; the REST
  answer and the console strip show it.
- `POST /api/v1/audit/class-counts` and the console counter strip
  (above the result list on `/ui/audit`) use it. The console also has a
  "Record type" filter and shows each row's class as a badge.
- Only an `operation_execution` row resolves to the flight recorder. The
  trace route answers `trace_present=false` (with `event_class` in the
  body) for any other class, and the console drawer says "not an
  operation run" instead of a trace pane. A decision or history view
  never offers an execution trace.

The column default stays `legacy_request`, so a row from an older MEHO
version during a rolling upgrade is also `legacy_request`. New code never
relies on the default: `tests/test_audit_event_class.py` scans the source
and fails on any `AuditLog(...)` call without `event_class=`. The shared
helpers (`operations._audit.write_audit_row`,
`memory.audit.write_internal_audit_row`, `approval_queue._write_audit_row`)
take `event_class` as a required argument.

## Key types

### `AuditQueryFilters` — frozen Pydantic v2

| Field | Type | Backend |
|---|---|---|
| `target` | `str \| None` | Matched against `targets.name` for the same tenant (alias resolution is the T2 router's job). |
| `principal` | `str \| None` | Part of the user id **or** the name: `audit_log.operator_sub ILIKE %value%` OR `payload->>'principal_name' ILIKE %value%` (#4668). `%` and `_` in the value match literally. |
| `principal_sub` | `str \| None` | `audit_log.operator_sub = value` (exact). Only the "my recent" views set it, from the caller's own token. Not on the REST body or the MCP input schema. |
| `op_id` | `str \| None` | Glob (`*` ↔ `%`). Matched against `payload->>'op_id'` (MCP rows) **OR** derived `http.<method>:<path>` (HTTP rows). |
| `op_class` | `str \| None` | Exact match against `payload->>'op_class'`. |
| `result_status` | `str \| None` | One of `"ok"` / `"error"` / `"denied"` / `"pending"`. Maps to status-code ranges. `pending` (202) is history: the run stopped to wait for approval, and the row stays `pending` after the decision. It matches the `approval.request` row and, since #4275, the stopped operation's own `operation_execution` row; add `event_class` to pick one. To see what waits now, use the approval queue (`meho approvals list`). |
| `since` / `until` | `datetime \| None` | `audit_log.occurred_at` bracket. **Absolute datetimes only** — `"24h"` / `"7d"` shorthand is parsed in the T2 / T3 router. |
| `audit_id` | `UUID \| None` | Exact-id lookup. |
| `parent_audit_id` | `UUID \| None` | `audit_log.parent_audit_id = :value` — **exact match**, so it returns the direct children of one row, not the whole subtree. Uses the `audit_log_parent_audit_id_idx` index. Main use: find the decision row and the run row of one approval request, which are linked only by this column (#4030). The replay CTE walks the full subtree. |
| `agent_session_id` | `UUID \| None` | `audit_log.agent_session_id = :value`. Un-gated by G8.2-T3 (#1011); the column landed with G8.2-T1 (#1009). |
| `work_ref` | `str \| None` | `audit_log.work_ref = :value` — **exact match** (a change-ticket reference is an opaque identifier, not a search term, so deterministic equality per #1177, not `ILIKE`). Filterable + surfaced since work_ref I1-T3 (#1658); the column landed with work_ref I1-T1 (#1655, migration `0039`). NULL rows are excluded. |
| `event_class` | `AuditEventClass \| None` | `audit_log.event_class = :value` — exact match on the closed set (#3733). `None` applies no class filter, so a query without it returns every class, the same rows as before. Old rows match `legacy_request`. |
| `has_trace` | `bool \| None` | `True` keeps only the rows the trace route returns a trace for: `event_class = operation_execution` AND `EXISTS` a `dispatch_trace` row with the same `audit_id` and the caller's `tenant_id` (#4855). `False` keeps the other rows. `None` applies no filter. The unique index `dispatch_trace_audit_id_idx` makes the check cheap. On the REST body (`AuditQueryRequest`) and used by `meho audit trace list`; not on the MCP input schema. |
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
| `tenant_name` | LEFT JOIN `tenant.name ON audit_log.tenant_id = tenant.id` (#5238). Every row is already in `:tenant_id`, so it only names the caller's own tenant. `None` when the tenant row is gone; show `tenant_id` then. Replay nodes carry it too. |
| `principal_sub` | `audit_log.operator_sub` |
| `target_id` | `audit_log.target_id` |
| `target_name` | LEFT JOIN `targets.name ON audit_log.target_id = targets.id AND targets.tenant_id = :tenant_id`. The tenant-id half of the ON clause is defence-in-depth: `audit_log.target_id` has no FK in v0.2 (soft column per chassis discipline) so a cross-tenant value resolves to `target_name=None` rather than leaking another tenant's name. |
| `method` / `path` / `status_code` / `request_id` / `duration_ms` / `payload` | Columns of the same name on `audit_log`. |
| `op_id` | `payload['op_id']` if a string, else `f"http.{method.lower()}:{path}"`. |
| `op_class` | `payload['op_class']` if a string, else `classify_op(op_id)` from `broadcast.events`. MCP, dispatch and route-declared HTTP rows store the class the broadcast event carries (`resolve_op_class`, see [broadcast.md](broadcast.md)), so the `op_class` filter finds them. A dispatch row of a hand-written (typed or multi-step) operation stores the class that follows from its risk level: `read` for `safe`, `write` for `caution` and higher (#4979, #5064, see [broadcast.md](broadcast.md)). For the MCP `call_operation` outer-wrapper row this is `"tool_call"` (G0.15-T3 #1212) — the inner DISPATCH row carries the domain `read` / `write` class. |
| `result_status` | Derived from `status_code` — 401/403 → `"denied"`, 202 → `"pending"`, 4xx/5xx else → `"error"`, otherwise `"ok"`. `pending` is history (the run stopped to wait for approval; it stays `pending` after the decision), so check the approval queue for what waits now. `denied` on an `operation_execution` row means a step was refused; earlier steps may have written (#4275). |
| `principal_name` | `payload['principal_name']` when present. Three writers set it from `Operator.name` (the token's `name` claim): MCP rows (#1212, `write_mcp_audit_row`), approval request and decision rows (#3300), and `DISPATCH` operation run rows (#4029, `operations/_audit.py:write_audit_row`). The name always belongs to the row's `operator_sub`. On an approved run that is the approver when a person approves through the console, CLI or REST (`resume_dispatch_after_approval`), and the agent when MEHO's own agent waits for the approval and resumes the run itself (`agent/approval_wait.py:_resume_approved_or_already_resumed`). It is `None` when the token has no name. HTTP-chassis rows remain `None` — the `verify_jwt_and_bind` middleware does not bind `name` to contextvars, so the audit middleware sees no source for it. |
| `parent_audit_id` | `audit_log.parent_audit_id` — composite-operation lineage column (G0.6-T7 #398, migration `0006`). Surfaced on the row since G8.2-T3 (#1011); exact-match filterable since #4030. |
| `agent_session_id` | `audit_log.agent_session_id` — MCP-session correlation column (G8.2-T1 #1009, migration `0014`). Surfaced + filterable since G8.2-T3 (#1011). |
| `work_ref` | `audit_log.work_ref` — external change-ticket reference (work_ref I1-T1 #1655, migration `0039`). An opaque string (e.g. `"gh:evoila/meho#1"`) correlating a governed operation to the out-of-band change record that authorised it. Surfaced + exact-match-filterable since work_ref I1-T3 (#1658). Bind sources: the request/agent/dispatch boundary (work_ref I1-T2 #1657) and runbook runs (work_ref I3-T1 #1661 — a run's `runbook_runs.work_ref` is bound around each `operation_call` step's dispatch + on the abort row, so the step audit rows inherit the run's change ticket). NULL when no bind source is in scope; system-internal writers leave it NULL by design. |
| `policy_decision` | `audit_log.policy_decision` — the synchronous policy-gate verdict stamped on the row (#130, migration `0051`). One of the closed `PermissionVerdict` set — `"auto-execute"` / `"needs-approval"` / `"deny"` — or `None` where no gate ran (pre-#130 rows, pre-gate usage errors like unknown-op / invalid-params / connector-resolution failures, and system-internal writers). Populated by both audit writers off the dispatch-scoped `policy_decision_var` contextvar: the parked "request" row carries `"needs-approval"`; the approval "decision" row carries `None` (an operator's approve/reject is not a gate verdict — it is recorded via `result_status`). A consumer reads the verdict directly (`WHERE policy_decision = '<verdict>'`) instead of reconstructing it from `method`+`path`+`payload`. **Verdict catalog:** the persisted set is exactly the three-value `PermissionVerdict` enum. `time_window_violation` / `quota_exceeded` are **not** implemented (no such policy classes exist in code); any external catalog listing them overstates the surface. |
| `event_class` | `audit_log.event_class` — what kind of accountable event the row records (#3733, migration `0106`). See [Event class](#event-class-3733). |
| `sandbox_mode` | `audit_log.sandbox_mode` — `true` when the instance ran in sandbox mode when it wrote the row (#4622, see [decisions/sandbox-mode.md](../decisions/sandbox-mode.md)). Rows from before the column existed read `false`. Every audit answer carries it (#5312), so a person can check the mark of a sandbox test with MEHO alone. Not a filter: one database has one mode. The CLI table shows no column for it; `--json` shows it. |
| `broadcast_event_id` | **None in v0.2** — FK direction is reversed: `BroadcastEvent.audit_id` points at the audit row. |

The three computed fields use the same rules the audit writers apply when
they store the row, so a row returned by the query API and the stored row
agree on the `(op_id, op_class, result_status)` trio. Since #3758 no
automatic Broadcast event is published for a row; the row is the record.

### `AuditQueryResult` — page

`rows: list[AuditEntry]` plus `next_cursor: str | None`. `next_cursor` is None
when fewer than `limit` rows were available on the page (the query reached the
end of the matching set under the current filter).

## Control flow

```text
query_audit(filters, tenant_id, session)
  │
  ├─ Build SELECT audit_log, targets.name, tenant.name
  │     OUTER JOIN targets ON audit_log.target_id = targets.id
  │                       AND targets.tenant_id   = :tenant_id   ← JOIN-scope defence
  │     OUTER JOIN tenant  ON audit_log.tenant_id = tenant.id    ← tenant_name (#5238)
  │     WHERE audit_log.tenant_id = :tenant_id      ← always first
  │       [+ audit_id = :audit_id]
  │       [+ parent_audit_id = :parent_audit_id]                ← exact match
  │       [+ agent_session_id = :agent_session_id]
  │       [+ work_ref = :work_ref]                              ← exact match
  │       [+ event_class = :event_class]                        ← exact match
  │       [+ operator_sub ILIKE :principal OR payload->>'principal_name' ILIKE :principal]
  │       [+ operator_sub = :principal_sub]                     ← exact match
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
  ├─ For each (audit_log, target_name, tenant_name) row:
  │   ┌─ payload = dict(row.payload or {})
  │   ├─ op_id = payload['op_id'] if str else f"http.{method.lower()}:{path}"
  │   ├─ op_class = payload['op_class'] if str else classify_op(op_id)
  │   ├─ result_status = _derive_result_status(status_code)
  │   ├─ principal_name = payload['principal_name'] if str
  │   │                  else None   # MCP, approval and DISPATCH rows
  │   │                              # carry it in payload; HTTP-chassis
  │   │                              # rows still have no source
  │   └─ AuditEntry(... real cols ..., op_id, op_class, result_status,
  │                 parent_audit_id=row.parent_audit_id,
  │                 agent_session_id=row.agent_session_id,
  │                 work_ref=row.work_ref,
  │                 policy_decision=row.policy_decision,
  │                 event_class=row.event_class,
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

### Direct MCP calls (#4672)

What anchors the rows of an agent or operator that calls MEHO over MCP
without MEHO's own agent loop (Claude Code, Claude Desktop, any MCP
client).

- The client sends its `Mcp-Session-Id` header on every request. The MCP
  server stores it as the `mcp_session_id` contextvar for that request.
- The MCP tool row (`method=MCP`, for example
  `/mcp/tools/call/call_operation`) carries it as `agent_session_id`.
- The operation rows of that request carry the **same** session id
  (`operations/_audit.py:write_audit_row` reads
  `resolve_agent_session_id()`, which falls back to the MCP session id
  when no agent run is bound). That covers the `DISPATCH` row of a direct
  `call_operation` (`ok`, `error` and `denied` alike) and every operation
  an MCP tool runs inside, for example the `vault.kv.read` of
  `meho_status`. Before #4672 these rows had a NULL `agent_session_id`,
  so the replay showed the tool calls but not what ran against the
  targets.
- The MCP tool row and the operation rows of one request also share one
  `request_id`.
- A call without the header (REST, CLI, an MCP client that sends none)
  still lands with a NULL `agent_session_id` (#3623).

### Client-credential runs (#3446)

What anchors the rows of a run by a client-credential service principal
(a `PrincipalKind.SERVICE` token, for example an automation add-on), and
how an operator replays it.

- Such a caller usually has no MCP session and no in-process agent loop, so
  neither normal source of `agent_session_id` applies.
- The run is named by its `work_ref`. When the caller passes a `work_ref`
  to `call_operation` (the field, or the `Meho-Work-Ref` header on REST or
  MCP), MEHO opens one orchestration run per `(clientId, work_ref)` in the
  caller's tenant (`operations/addon_orchestration.py`). Opening writes
  one start row: `method=ORCHESTRATION`, `path=addon.orchestration`, no
  parent.
- Every call of that run gets the run's `session_id` as its
  `agent_session_id`, and the start row as its `parent_audit_id`. So the
  normal session replay rebuilds the run as one tree. The 10,000 anchor-row
  cap and the 413 `session_too_large` answer apply unchanged.
- This works for every service principal with a recovered clientId, paired
  add-on or not. The clientId comes from the `service-account-<clientId>`
  username marker or, when the token has no marker, from its `azp` /
  `client_id` claim (`auth/jwt.py`). Before #3446 only paired add-ons got
  it, and an unpaired service account's rows landed with a NULL
  `agent_session_id`.
- A call **without** a `work_ref` still lands NULL. There is nothing to group
  it by. Find those rows by principal (`meho audit query --principal`,
  which matches part of the user id or of the name).

How an operator replays a client-credential run:

1. `meho audit query --filter-work-ref <ref> --json` (or
   `GET /api/v1/audit/by-work-ref/{ref}`) — the rows show the shared
   `agent_session_id`. Use `--json`: the default table does not print
   that column.
2. `meho audit replay <session-id>` (or
   `GET /api/v1/audit/sessions/{session_id}/replay`, `tenant_admin`).

Design and tests: [`addon-parent-linkage.md`](addon-parent-linkage.md),
`tests/test_client_credential_replay.py`.

## REST surface (G8.1-T2 #466, G8.2-T4 #1012)

The routes under `backend/src/meho_backplane/api/v1/audit.py` (plus
class-counts in `api/v1/audit_counts.py`) are the operator-facing entry
into the substrate. The query routes dispatch through `query_audit`,
class-counts through `count_event_classes`, and replay through
`replay_session`. All
pass `tenant_id=operator.tenant_id` (from the JWT) — the substrate's
tenant-scoping invariant is enforced one layer up.

| Route | Filter shape | Notes |
|---|---|---|
| `POST /api/v1/audit/query` | Body is `AuditQueryRequest`; `since` / `until` are strings parsed at the router via `parse_duration` (`"24h"` / `"7d"` / ISO-8601). Client-supplied `tenant_id` (or any other unknown field) is rejected with 422 `extra_forbidden` (`AuditQueryRequest.model_config` sets `extra="forbid"`, G0.9-T2 / #729); the route never reads tenant from the body — it always passes `operator.tenant_id` from the JWT to the substrate. | Full-filter surface. |
| `POST /api/v1/audit/class-counts` | Body is `AuditClassCountsRequest`: the `AuditQueryRequest` filters without `event_class`, `limit` and `cursor` (those fields are a 422). 200 body is `AuditClassCounts` (`{counts, total, since}`), one count per event class (#3747). Without `since` only the last 24 hours count; `since` echoes the window start. | Counters. CLI: `meho audit count` (#4983). |
| `GET /api/v1/audit/who-touched/{target}` | Path param becomes `filters.target`; `since` query defaults to `"24h"`. | Pre-canned shortcut. |
| `GET /api/v1/audit/by-work-ref/{ref}` | Path param (`{ref:path}` converter — a work_ref carries embedded slashes, `#` is percent-encoded) becomes `filters.work_ref` (exact match); `since` query has **no** default window (a change-ticket lookup wants the whole governed history of the ref, not just the last 24h). The "show every write authorised by ticket X" lookup (work_ref I1-T3 #1658). | Pre-canned shortcut. |
| `GET /api/v1/audit/my-recent` | `filters.principal_sub = operator.sub` (exact match on the user id, never the name filter); `since` query defaults to `"24h"`. | Pre-canned shortcut. |
| `GET /api/v1/audit/show/{audit_id}` | `filters.audit_id = <path>`, `limit=1`. Substrate returns 0 rows for cross-tenant lookups → router raises **404** (not 403) so existence never leaks. | Single-row fetch. |
| `GET /api/v1/audit/{audit_id}/trace` | The dispatch flight-recorder read (#3215). Audit-existence check via the same tenant-scoped substrate (`audit_id`, `limit=1`) → **404** (not 403) on a missing / cross-tenant row, matching `show`. Then `flight_recorder.load_trace(audit_id, tenant_id)` loads the trace header + ordered spans (tenant-scoped again as defence in depth). Only an `operation_execution` row reads a trace (#3747); any other class answers `trace_present=false` without a trace read. 200 body is `AuditTraceResult` (`{audit_id, trace_present, trace, event_class}`); an audit row with **no** captured trace is a `200` with `trace_present=false` (capture is opt-in/best-effort, not an error), and `trace.redaction_uncertain` surfaces the F5 degrade flag. Operator plane keeps full access independent of the agent gate. | Flight-recorder trace (#3215). |
| `GET /api/v1/audit/sessions/{session_id}/replay` | Dispatches `replay_session(session_id, tenant_id=operator.tenant_id, ...)`. 200 body is `AuditReplayResult` (`{root: [ReplayNode], session_id, tenant_id, row_count, excluded_null_session_count, loose_row_count}`; `loose_row_count` is the nodes whose parent id names no row, #4670). Unknown / foreign session → `root=[]` / `row_count=0` (**not** 404 — same non-leakage as `show`). `row_count > 10_000` → **413** `{"detail": "session_too_large", "row_count": n}` from a count-first guard run *before* the recursive tree build. **`tenant_admin`-gated (#1843)** — see RBAC below. | Per-session replay (G8.2-T4). |

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
`"meho.audit.query"` for the four query routes, `"meho_audit_replay"` for
the replay route, and `"meho_audit_trace"` for the flight-recorder trace
route (so operators can tell replay and trace-read usage apart from
flat-query usage in `audit_log`). The shared `audit_query` op_class flips
the broadcast event to aggregate-only (`{op_id, result_status,
row_count}` only, never the request filter or the replayed `ReplayNode`
tree). The `audit_row_count` contextvar is bound after the substrate
returns — and on the 413 path before raising — so the broadcast event's
`row_count` field reflects the actual cardinality. The shape mirrors
`api/v1/retrieve_usage.py`.

RBAC: the flat / self-scoped operator routes (`query`, `who-touched`,
`by-work-ref`, `my-recent`, `show`, and the flight-recorder `trace`)
require `operator` minimum — `read_only` → 403; `operator` /
`tenant_admin` → 200. These mirror the operator-gated MCP `query_audit`
tool (including its self-session-only `shape="tree"` path). The
flight-recorder trace read stays on the operator plane with **full**
access independent of the agent gate (the agent read is a separate task);
the console pane renders the same trace through the audit-row drawer.

**Tenant-broadcast read surfaces (#2084).** Within a tenant, `query` and
`show` are principal-agnostic by design: `POST /api/v1/audit/query` (with
or without a `principal` filter) and `GET /api/v1/audit/show/{audit_id}`
return rows for **every** principal in the calling tenant. Any `operator`
/ `tenant_admin` in the tenant reads all principals' audit rows — there
is **no per-principal role check** on either route, and filtering
`principal` on another operator's sub is a supported forensic lookup.
Only `my-recent` stays self-scoped: it binds
`filters.principal_sub = operator.sub` (exact match on the user id), so
the caller sees their own rows only. Cross-**tenant** isolation is
unchanged (the substrate's first WHERE clause is always
`audit_log.tenant_id`; `show` → 404 on a foreign
`audit_id`, per the module and `show` docstrings in
`backend/src/meho_backplane/api/v1/audit.py`), and dispatch rows carry
`payload.params_hash` — a keyed hash of the canonicalised params with
secret-named values left out
(`operations/_validate.py::compute_audit_params_hash`) — instead of the raw
params, so a cross-principal-visible row never exposes sensitive
operation arguments. Same shelf as the `who-touched` shortcut and the
aggregate-only broadcast redaction; same category-2 doc-resolution
shape as the kb same-slug cross-principal write semantics (#1327,
`docs/codebase/kb.md`). Revisit if a finer role gradation (e.g.
`tenant_member`) ever lands.

The cross-session **replay** route
(`GET /api/v1/audit/sessions/{session_id}/replay`) requires
`tenant_admin` (#1843): it takes an *arbitrary* `session_id` and
reconstructs another principal's full session trace, a privileged
forensic act. `read_only` / `operator` → 403; `tenant_admin` → 200. This
aligns REST with the MCP posture, where cross-session replay is the
`tenant_admin`-gated `meho_audit_replay` tool — the operator-level
`query_audit` `shape="tree"` path replays only the caller's own session.
Before #1843 the REST route gated replay at `operator`, making the
web/CLI surface more permissive than MCP and `audit-replay.md`. The
gate is orthogonal to cross-tenant isolation (always enforced via the
JWT `tenant_id`), which is unchanged.

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

The input schema accepts the same filters as the REST body, including
`event_class` (#4807). Its `enum` is built from `AuditEventClass` plus
null, so the schema and the substrate cannot drift apart. A test checks
that every filter the tool's help text names is a property of the
schema.

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
* `UnsupportedFilterError` (substrate) — same mapping with the
  column-name message. No filter raises it today: `agent_session_id`
  (G8.2-T3 #1011) and `parent_audit_id` (#4030) are both normal filters.
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
`replay_session`, returning `{root, session_id, tenant_id, row_count,
excluded_null_session_count, loose_row_count}` (same envelope, including
the same `excluded_null_session_count` and `loose_row_count` glosses, as
the admin tool below).

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
replay is the `tenant_admin`-gated `meho_audit_replay` tool, not this
path.

## `meho_audit_replay` admin tool (G8.2-T6 #1014)

A dedicated `tenant_admin` meta-tool in the `meho.*` admin namespace
(alongside `meho_broadcast_overrides_*`), registered in the same
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
* Returns `{root: [ReplayNode…], session_id, tenant_id, row_count,
  excluded_null_session_count, loose_row_count}` via
  `model_dump(mode="json")`; `row_count` is the total node count in the
  returned tree, `excluded_null_session_count` is the tenant-wide tally
  of header-less `method=MCP` NULL-session rows no replay can reach
  (#2700 / #2776), and `loose_row_count` is the nodes whose parent id
  names no row in the tenant (#4670).
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

### `meho_audit_` broadcast-classifier arm

The MCP broadcast path derives `op_class` from `classify_op(op_id)`
with the **tool name verbatim** — it does not honor
`ToolDefinition.op_class`. `classify_op` therefore carries a
`meho_audit_` prefix arm next to the existing `audit.` arm: without
it, `meho_audit_replay` would fall through to `other` and broadcast
its full `ReplayNode` tree instead of the aggregate-only view. (The
arm was originally the dotted `meho.audit.` prefix for the pre-#2745
tool name `meho.audit.replay`; the dotted arm is retained because
`classify_op` re-runs at render time on stored op-ids, so historical
rows keep their class.) (The literal tool name `query_audit` has the same
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
  the `query_audit` MCP meta-tool plus the `meho_audit_replay` admin
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

* **`principal_name` populates for MCP, approval and `DISPATCH` rows;
  HTTP-chassis rows still return `None`.** The MCP audit writer
  (`mcp/audit.py:write_mcp_audit_row`, #1212), the approval row writer
  (`operations/approval_queue.py:_write_audit_row`, #3300) and the
  operation run writer (`operations/_audit.py:write_audit_row`, #4029)
  put `Operator.name` into `payload['principal_name']` whenever the JWT
  carries it, and the audit-query handler reads that key off the JSON
  column into the returned `AuditEntry`. A run row is the only row of an
  approved run, so it must carry the name itself. The name always
  belongs to the row's `operator_sub`: the approver when a person
  approves through the console, CLI or REST, and the agent when
  MEHO's own agent waits for the approval and resumes the run itself. The HTTP audit middleware
  (`meho_backplane.audit._write_audit_row`) reads `operator_sub` from
  contextvars but never the JWT `name` claim, so HTTP rows remain
  `principal_name=None`. Closing the HTTP gap is a small write-path
  follow-up: bind `audit_principal_name` in `verify_jwt_and_bind` and
  let the `_AUDIT_PAYLOAD_PREFIX` machinery push it into `payload`;
  the handler already reads it out, so no read-side change is needed.

* **`parent_audit_id` is a flat filter (#4030).** The column is real (#398,
  migration `0006`), is read onto the returned row, and is walked by the
  replay CTE. The flat `query_audit(parent_audit_id=...)` filter is an exact
  match: it returns direct children only. This is the one way to link an
  approval request to its decision and run rows when those rows carry no
  agent session.

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
  (`"when_negotiated"` / `"enforced"`).

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
