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
CHECK `ck_audit_log_event_class` (migration `0106`; `0137` added
`config_change`). Each writer sets the
class it knows. Nothing is guessed from `method` / `path`.

| Class | Written by |
|---|---|
| `operation_execution` | Dispatch rows that ran (`ok` or `error`), the row of a multi-step operation that stopped part-way because a step waits for approval (`awaiting_approval`) or was denied (`denied`, #4275), runbook abort, gateway command mint / result, satellite effect rows, topology / ingest / add-on writes, sweepers and reapers. |
| `policy_decision` | Dispatch rows a gate refused (`denied`, rate limit, unqualified target version), a call of an ingested licence read that MEHO keeps off (`denied`, `payload["error"]["error_code"] = license_read_off`, #5791), the `approval.request` park row, and the `denied` row of a step inside a multi-step operation. |
| `config_change` | Two settings rows (`method = SERVICE`). The `meho_connector_edit_op` row: an operator turned one operation on or off, or changed or removed its risk level, approval rule, description or guidance (#5791). The `meho_connector_tenant_setting` row: a tenant turned one built-in operation or a whole group on or off for itself, or set it back to the install default (#5864). `payload["op_id"]` names the edited operation (a group switch names `payload["group_key"]`), so a search by op id finds the edit. No operation ran, so a filter on `operation_execution` does not return these rows. The other connector review rows (enable, disable, group edits) stay `operation_execution`. Edit rows written before migration `0137` keep `operation_execution`; no row is changed. |
| `approval_decision` | `approval.decision` rows (approve, reject, expire; an approve row's payload names the `approval_rule` that allowed it), the `approval.withdraw` row (the sender took its request back, #5717), and the standing-grant auto-approval row. |
| `security_event` | Refresh-token replay, broken satellite effect chain, un-reported gateway mint alarm, and the `mcp.session_open` row of a new MCP session (with `AUDIT_CLIENT_IDENTITY` on). A filter on `security_event` therefore also returns session starts. |
| `legacy_request` | Every row written before `0106` (server default; old rows are not reclassified), the REST and MCP request wrapper rows, and the `approval.result` read row. |

A call of an ingested op on a connector class that does not accept
ingested operations also writes a `policy_decision` row (`denied`, code
`ingested_ops_not_accepted`).

A typed or multi-step op called on a target whose connector does not
have its handler writes a `policy_decision` row with result status
`target_connector_mismatch` (#5948).

A refused call's row says why (#5773). `payload["error"]` holds the
`error_code` the caller got, a `reason_code`, and the `reason` text. The
`reason_code` names the rule that refused the call
(`operations/deny_reasons.py`): `agent_grant_denied` (the agent's grants),
`agent_no_grant` (no grant covers the call), `agent_steps_not_declared`
(a multi-step operation lists no steps, so the agent's grants cannot be
checked for each step),
`role_insufficient` (the role is below what the operation or tool needs),
`read_scope_denied` (a service login's read scope), `connector_not_allowed`
(the agent may not use the connector), `agent_reveal_refused` (an agent
asked for hidden values) or `policy_denied` (any other refusal). In an
agent run that a person started, `agent_grant_denied` means the agent's
own grant refused the call, and `role_insufficient` means the role did.
The text names operations, grant patterns, roles and logins only, never a
secret or the call's arguments.

### Text the table cannot store, and the short row (#6452)

The answer of a target can hold text that the `audit_log` table refuses.
The audit writer (`write_audit_row`) changes that text before it stores the
row, in `payload`, `raw_payload` and `redaction_manifest` (keys and values,
at any depth). The caller still gets the answer unchanged.

| Text in the answer | What is stored |
|---|---|
| A NUL character | `<NUL>` |
| A character that is not valid as text (a lone surrogate) | `<invalid character>` |
| A number JSON cannot hold (NaN, Infinity) | The text `nan`, `inf` or `-inf` |
| A `Decimal`, date, time stamp, UUID, bytes or set | Its text form (a set becomes a list) |
| A key that is not text | Its text form, as JSON writes it (`1`, `true`, `null`) |

When the writer replaced a character or a number (the first two rows, or a
key that got a `#2` suffix because two keys became equal), it sets
`"text_changed": true` at the top level of `payload`. Only the writer sets
this key. A target can send the text `<NUL>`, but it cannot set the key. A
value of the last two rows is not counted as a change.

When the insert of the full row fails for any other reason, the writer tries
once more with a **short row**. The call still returns its answer, also a
call that returns hidden values: the short row is its audit row. A short row
holds:

- the fixed columns: who, when, tenant, target, operation, status code,
  ticket id, request id, session, run and step;
- in `payload`, every value that MEHO builds itself: `op_id`, `op_class`,
  `result_status`, `params_hash`, `source_kind`, the connector product,
  version and implementation, `principal_name`, `client`,
  `reveal_secret`, `agent_session_id`, `run_id`, `step_id`,
  `caller_gone_at`, the parent audit id, the result handle (`handle_id`,
  `total_rows`), `redaction_policy_id` and the model data of an agent run;
- a small `error` object that MEHO builds itself: only `error_code` and,
  for a call that waits for approval or was refused,
  `approval_request_id`. This is the same place as in the full row. The
  values come from the result that MEHO's code built, never from an error
  text. Only a UUID and an error code of the usual form are kept;
- `approval_request_id` at the top of `payload` only for the run of an
  approved request, as in the full row (`docs/codebase/approvals.md`);
- `{"full_row_lost": {"error_class": "<name of the error>"}}`.

A short row has no `raw_payload`, no `redaction_manifest`, no other key of
the full `error` and none of the extra keys that a connector handler adds:
these can hold text of the target. A full row never has `full_row_lost` or
`text_changed` from a handler's extra: MEHO drops both keys and sets them
only itself. MEHO logs `audit_full_row_lost` with the audit id, the
operation, the request id and the error class. The same short row is used on
the error path, so a call that failed at the target also has a row.

No short row is written when the database cannot be reached (a connection
fault, a connection timeout, too many connections, a shutdown). Then the
write fails at once, and the call answers `audit_commit_failed`. The same
happens when the short row fails too. A statement that was cancelled or ran
into a statement timeout is not such a case: then the short row is tried.

The write of a call that ran is not lost when the task is cancelled during
the write: the write runs to its end, and the cancel is raised after it.
`duration_ms` is cut to the largest value its column holds (99999999.99).

**How to find these rows:**

- `meho audit query --full-row-lost` (REST: `"full_row_lost": true` in the
  body of `POST /api/v1/audit/query`) lists the short rows.
  `meho audit query --text-changed` (`"text_changed": true`) lists the rows
  where text was changed. Both work with the other filters, for example
  `--principal`.
- Two counters: `meho_audit_full_row_lost_total` (by `error_class`) and
  `meho_audit_text_changed_total`.

A failed audit write is logged as `dispatch_audit_failed`. The line holds the
request id, the target (name and id), the operation and the caller
(`operator_sub`).

The code is in `operations/audit_row_safe.py` and `operations/_audit.py`.

### Find a trace without its audit row (#6452)

`GET /api/v1/audit/traces` searches the stored flight-recorder traces
without reading `audit_log`, so it works when the audit row of a call was
never stored. Query parameters: `request_id` (UUID), `caller` (the exact
`operator_sub`), `since` (default `24h`), `until`, `limit` (1 to 100, default
20). Newest first. 200 body: `{"items": [TraceView, ...]}`, each with its
spans. Migration `0169` added `request_id` and `operator_sub` to
`dispatch_trace`; an older trace has neither and is found by time only.
Access rules are those of the single trace read: a person reads every trace
of the tenant; an agent or service login reads only the traces of its own
calls (MEHO filters on its own `sub` and ignores its `caller`); an agent
reads none unless the tenant lets agents read traces, and never a trace
marked `redaction_uncertain`. A call that ran after a person approved it
stores the requester as its caller (`handle_owner_sub_var`, the same owner
as its result handle), so the agent that asked reads its trace. `since` also limits a search by
`request_id`: it is a filter on the capture time, and it applies together
with `request_id`. To find an older trace by its request id, give a wider
`since`, for example `since=30d`. There is no CLI command for it yet.

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

### A row that cannot be stored is refused before the call

The dispatcher writes the row of a call after the call. If that write
fails, the target may already hold the change (`audit_commit_failed`). So
before it sends a call, the dispatcher checks the part of the row it
already knows. `operations/audit_row_precheck.py` does this
(`audit_row_problem`). It checks the values only and sends no statement
to the database:

- the text columns `operator_sub`, `actor_sub`, `path`, `step_id` and
  `work_ref`: each text can be encoded as UTF-8 and holds no NUL
  character;
- `operator_sub`, `actor_sub` and `work_ref` have an index, so each must
  also stay under `INDEX_TEXT_MAX_BYTES`;
- every key and text in `payload`, built by the same function the writer
  uses (the `client` block and the caller's name included). The caller's
  name is the token's name, or the client id of a machine login, as the
  writer stores it;
- every number in `payload`: NaN and Infinity cannot be stored as JSON, so
  they are refused too.

When the check itself fails (for example on a payload nested too deep to
walk), the call is refused with `audit_row_invalid` and the field
`payload`.

On a problem the call is refused with `audit_row_invalid`. `extras.field`
names the value (never its content). A payload value is named by its path,
for example `payload.principal_name`. A key that cannot be stored is named by
its position (`payload.<key 1>`), so the refusal never holds the bad text. Nothing is sent to the target, no
approval request is made and no row is written. All ways into the
dispatcher pass through this one check. `audit_commit_failed` stays for a
database that is not reachable after the call, and for what is known only
after the call (`raw_payload`, `redaction_manifest`). The registered agent
name is known before the call, but it is in the database, and the check
sends no statement there, so it does not check that name. The module lists how each text and JSON column is covered, and
`tests/test_audit_row_precheck.py` fails when a new column is in no list.

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
  limit=200, recent_window=1 day, now=None, owner_sub=None) -> LooseRowsResult`
  (`audit_query/loose_rows.py`). Read-only: two `SELECT` statements, no
  write. It lists the tenant's loose rows newest first (`LooseRow`: id,
  time, principal, operation, event class, path, parent id), with the
  exact total `count` and `recent_count` (rows younger than the window),
  `recent_since`, and `truncated` when the list is capped (`limit` ≤
  1000). A parent "exists" when a row with that id exists in the same
  tenant, the same rule the replay closure uses. With `owner_sub` set
  (a service login), the rows and both counts hold only that login's
  rows, and only its own rows count as a present parent. `event_class` tells a
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
| `result_status` | `str \| None` | One of `"ok"` / `"error"` / `"denied"` / `"pending"` / `"withdrawn"`. Maps to status-code ranges. `withdrawn` matches the `approval.withdraw` rows (`method = APPROVAL`, `path = approval.withdraw`), and `ok` never does (#5717). `pending` (202) is history: the run stopped to wait for approval, and the row stays `pending` after the decision. It matches the `approval.request` row and, since #4275, the stopped operation's own `operation_execution` row; add `event_class` to pick one. To see what waits now, use the approval queue (`meho approvals list`). |
| `since` / `until` | `datetime \| None` | `audit_log.occurred_at` bracket. **Absolute datetimes only** — `"24h"` / `"7d"` shorthand is parsed in the T2 / T3 router. |
| `audit_id` | `UUID \| None` | Exact-id lookup. |
| `parent_audit_id` | `UUID \| None` | `audit_log.parent_audit_id = :value` — **exact match**, so it returns the direct children of one row, not the whole subtree. Uses the `audit_log_parent_audit_id_idx` index. Main use: find the decision row and the run row of one approval request, which are linked only by this column (#4030). The replay CTE walks the full subtree. |
| `agent_session_id` | `UUID \| None` | `audit_log.agent_session_id = :value`. Un-gated by G8.2-T3 (#1011); the column landed with G8.2-T1 (#1009). |
| `work_ref` | `str \| None` | `audit_log.work_ref IN (cleaned id, text as typed)` — the **whole id**, see "Search filters" below (a change-ticket reference is an opaque identifier, not a search term, so deterministic equality per #1177, not `ILIKE`). Filterable + surfaced since work_ref I1-T3 (#1658); the column landed with work_ref I1-T1 (#1655, migration `0039`). NULL rows are excluded. |
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
| `method` / `path` / `status_code` / `request_id` / `duration_ms` / `payload` | Columns of the same name on `audit_log`. An approval row in the older form shows its `payload` through `approval_payload_view` (`audit_query/approval_payload.py`): `principal_sub` is the row's `operator_sub`, and the requester's id is in `requester_sub`. See "Who the payload names" in [`approvals.md`](approvals.md). |
| `op_id` | `payload['op_id']` if a string, else `f"http.{method.lower()}:{path}"`. |
| `op_class` | `payload['op_class']` if a string, else `classify_op(op_id)` from `broadcast.events`. MCP, dispatch and route-declared HTTP rows store the class the broadcast event carries (`resolve_op_class`, see [broadcast.md](broadcast.md)), so the `op_class` filter finds them. A dispatch row of a hand-written (typed or multi-step) operation stores the class that follows from its risk level: `read` for `safe`, `write` for `caution` and higher (#4979, #5064, see [broadcast.md](broadcast.md)). For the MCP `call_operation` outer-wrapper row this is `"tool_call"` (G0.15-T3 #1212) — the inner DISPATCH row carries the domain `read` / `write` class. |
| `result_status` | An `approval.withdraw` row → `"withdrawn"` (#5717): the operation never ran. Every other row is derived from `status_code` — 401/403 → `"denied"`, 202 → `"pending"`, 4xx/5xx else → `"error"`, otherwise `"ok"`. `pending` is history (the run stopped to wait for approval; it stays `pending` after the decision), so check the approval queue for what waits now. `denied` on an `operation_execution` row means a step was refused; earlier steps may have written (#4275). |
| `principal_name` | `payload['principal_name']` when present. Four writers set it: MCP rows (#1212, `write_mcp_audit_row`), approval request and decision rows (#3300), `DISPATCH` operation run and refusal rows (#4029, `operations/_audit.py:write_audit_row`), and the tool rows of MEHO's own agent runs (`agent/tool_audit.py:write_agent_tool_row`). Each takes the name from `auth/display_name.py:principal_display_name` (#3885): the token's `name` claim, else for an agent token the registered agent name, else for any machine token its client id. The name always belongs to the row's `operator_sub`. On an approved run that is the approver when a person approves through the console, CLI or REST (`resume_dispatch_after_approval`), and the agent when MEHO's own agent waits for the approval and resumes the run itself (`agent/approval_wait.py:_resume_approved_or_already_resumed`). It is `None` when none of these gives a name. HTTP-chassis rows remain `None` — the `verify_jwt_and_bind` middleware does not bind `name` to contextvars, so the audit middleware sees no source for it. |
| `parent_audit_id` | `audit_log.parent_audit_id` — composite-operation lineage column (G0.6-T7 #398, migration `0006`). Surfaced on the row since G8.2-T3 (#1011); exact-match filterable since #4030. |
| `agent_session_id` | `audit_log.agent_session_id` — MCP-session correlation column (G8.2-T1 #1009, migration `0014`). Surfaced + filterable since G8.2-T3 (#1011). |
| `work_ref` | `audit_log.work_ref` — external change-ticket reference (work_ref I1-T1 #1655, migration `0039`). An opaque string (e.g. `"gh:evoila/meho#1"`) correlating a governed operation to the out-of-band change record that authorised it. Surfaced + filterable by the whole ticket id since work_ref I1-T3 (#1658). Bind sources: the request/agent/dispatch boundary (work_ref I1-T2 #1657) and runbook runs (work_ref I3-T1 #1661 — a run's `runbook_runs.work_ref` is bound around each `operation_call` step's dispatch + on the abort row, so the step audit rows inherit the run's change ticket). NULL when no bind source is in scope; system-internal writers leave it NULL by design. The server reads the `Meho-Work-Ref` header as UTF-8, with latin-1 as the fallback for bytes that are not valid UTF-8 (`decode_work_ref_header` in `operations/_audit.py`). So a non-ASCII ticket id such as `Änderung-7` lands on the audit row as the client sent it. |
| `policy_decision` | `audit_log.policy_decision` — the synchronous policy-gate verdict stamped on the row (#130, migration `0051`). One of the closed `PermissionVerdict` set — `"auto-execute"` / `"needs-approval"` / `"deny"` — or `None` where no gate ran (pre-#130 rows, pre-gate usage errors like unknown-op / invalid-params / connector-resolution failures, and system-internal writers). Populated by both audit writers off the dispatch-scoped `policy_decision_var` contextvar: the parked "request" row carries `"needs-approval"`; the approval "decision" row carries `None` (an operator's approve/reject is not a gate verdict — it is recorded via `result_status`). A consumer reads the verdict directly (`WHERE policy_decision = '<verdict>'`) instead of reconstructing it from `method`+`path`+`payload`. **Verdict catalog:** the persisted set is exactly the three-value `PermissionVerdict` enum. `time_window_violation` / `quota_exceeded` are **not** implemented (no such policy classes exist in code); any external catalog listing them overstates the surface. |
| `policy_rule` | `audit_log.policy_rule` (#6044, migration `0168`) — the policy rule that matched the call, as `<source>:<rule id>`, for example `default:person.safe`. Today only the policy evaluator in shadow mode (`POLICY_ENGINE_SHADOW`, see [policy-evaluator.md](policy-evaluator.md)) sets it, so the text ends with ` (shadow)`: the evaluator matched this rule, but today's checks decided the call. Set from `policy_rule_var`, only on rows written after the call's own policy gate ran. `None` with the switch off, where no gate ran (also on a step refused before its own gate), on every approval "decision" row (also an expiry written during a retry), on the run of an approved call (the gate does not run again), and on rows from before the column existed. `meho audit show` prints it. |
| `policy_version` | `audit_log.policy_version` (#6044, migration `0168`) — the version of the policy file the rule came from. `None` while no policy file decides (today always). |
| `event_class` | `audit_log.event_class` — what kind of accountable event the row records (#3733, migration `0106`). See [Event class](#event-class-3733). |
| `sandbox_mode` | `audit_log.sandbox_mode` — `true` when the instance ran in sandbox mode when it wrote the row (#4622, see [decisions/sandbox-mode.md](../decisions/sandbox-mode.md)). Rows from before the column existed read `false`. Every audit answer carries it (#5312), so a person can check the mark of a sandbox test with MEHO alone. Not a filter: one database has one mode. The CLI table shows no column for it; `--json` shows it. |
| `actor_sub` | `audit_log.actor_sub` (#5773) — the agent that acted for the person in `principal_sub`, in an agent run the person started (`agent:<name>`). `None` when nobody acted for another login, for example a person's own call or an agent that runs on its own login. |
| `actor_name` | The agent's name, read from `actor_sub` (the part after `agent:`). `None` when `actor_sub` is `None` or has another form. The CLI shows the actor in `audit show`, in the PRINCIPAL column of `audit query` (`<principal> via <actor>`) and at the end of each `audit replay` line. |
| `secret_read_as` | `audit_log.secret_read_as` (migration `0182`) — the id (`sub`) of a login. The field names the login under which the secret this call used was read from the store, in this call or in an earlier call of the same caller. Empty means: the call used no secret. A call that reads the secret itself names that login. A call that uses a login MEHO kept in memory names the login that read it when it was kept, or the fixed text `kept-login:reader-unknown`. A direct call: the caller's own id. An approved call: the approver's id, because MEHO runs the approved call with the approver's login (the person who asked stays on the approval request). An agent that resumes its own call: the agent's login id. A hosted agent run that a person started: the person's id. A check runner call: the runner's own login id, not the sensor owner in `principal_sub`, and the fixed text `check-runner` when the runner's token has no `sub`. Google Secret Manager: the caller's id with federation, and the fixed text `meho` when the store saw MEHO's own identity. `None` when the call used no secret. Never a token, a store path or a store address. One call has one value; a step inside a multi-step call has its own value and the parent row does not take it. MEHO writes the column on every install, but an answer carries the field only while `AUDIT_SECRET_READ_AS` is on (off by default). `meho audit show` prints it when present. |
| `broadcast_event_id` | **None in v0.2** — FK direction is reversed: `BroadcastEvent.audit_id` points at the audit row. |
| `stored_session_id` | Only in the answer of `GET /api/v1/audit/show/{audit_id}` (`AuditEntryDetail`, #4904); the list, query and replay answers and the MCP audit tools do not have the field. The stored agent session the row belongs to: a hosted run, an uploaded session whose MCP calls MEHO matched to this row's `agent_session_id`, or an uploaded session whose `meho` command wrote this CLI row. Sent only with the switch `AUDIT_SESSION_LINKS` on (off by default), to a person who may read sessions, for a row with a stored session; otherwise the answer has no such field. `meho audit show` prints it as `stored_session:`. See [session-store.md](session-store.md#links-from-audit-rows). |

The three computed fields use the same rules the audit writers apply when
they store the row, so a row returned by the query API and the stored row
agree on the `(op_id, op_class, result_status)` trio. Since #3758 no
automatic Broadcast event is published for a row; the row is the record.

### `secret_read_as`: whose login the secret store accepted

The field names the login under which the secret this call used was read from the store, in this call or in an earlier call of the same caller. Empty means: the call used no secret.

There are three cases:

- **The call read the secret itself.** The field names the login of that
  read.
- **The call used a login that MEHO kept in memory.** A connector keeps a
  target's login after the first store read. The field names the login that
  read the secret when it was kept. When the kept entry does not know who
  read it, the field holds the fixed text `kept-login:reader-unknown`. It
  never names the present caller in that case. The fixed text never takes the
  place of a login the same call has already noted. A second caller always reads
  the secret once with their own login first (`SecretReaders`,
  `connectors/_shared/secret_readers.py`), so a row never names a caller who
  did not read. A read counts for the exact secret reference the target held
  at the read. A connector notes the kept login only when it takes the kept
  entry. An SSH connection that MEHO keeps open (`connectors/adapters/ssh.py`)
  keeps the login id of the store read made before it was opened, and a call
  that uses the open connection again names that login.
- **The call used no secret.** The field is empty.

The fixed texts cannot be taken for a login id: MEHO's own ids start with
`agent:`, `system:` or `__`, and the login server's ids are UUIDs. A check
runner whose token has no `sub` is named `check-runner`, never the Sensor's
owner. A read of a deleted latest version returns no secret data, so it names
no login. The read of the sink secret for the approval card of `secret.move`
names the caller when it returns a secret that is not deleted; the
`approval.request` row of the call that waits for approval holds that value.

Switch: `AUDIT_SECRET_READ_AS` (`Settings.audit_secret_read_as`), off by
default. MEHO writes the column `audit_log.secret_read_as` on every
install. With the switch off, no audit read (REST, CLI, MCP, web console)
shows the field, and every answer is the same as without it. With it on,
each row in an answer carries the field. The measurement of the switch is
tracked in the issue "Task: Measure the AUDIT_SECRET_READ_AS switch (audit
answers name whose login read the secret) before it is on by default". The
switch stays off by default until that measurement is done.

How the value gets to the row: the dispatcher opens one scope for each call
(`auth/secret_read_as.py`), at the start of `dispatch()`, before the first
check. So every row a call writes, a refusal row included, holds only what
that call read. The step gate of a multi-step call (`operations/composite.py`)
opens its own scope too, so the refusal or approval row of a step never holds
the value of the parent call. Each Vault secret read (the target login read in
`connectors/_shared/vault_creds.py`, `vault.kv.read`, the secret copy and
compare reads, the read of the existing sink secret before a `secret.move` with
`merge: true` writes it (noted also when the write then fails, and also for
the second read that tells why the write failed), the pfSense VPN
user read, the KV password read of
`vault.auth.userpass.write` and `vault.kv.generate`) and the Google Secret Manager read
(`connectors/_shared/gsm_creds.py`) note the login id after the store returned
the secret. A login that passed, followed by a refused read or by no read at
all, notes nothing. For Vault the id is the `sub` of the token MEHO sent. The
audit writer reads it when it writes the row. The row is written once, at the
end of the call, and is never updated. The same people see the field who see
the row today: a service login reads only the rows of its own calls.

### The `client` block: which login client made the call

Switch: `AUDIT_CLIENT_IDENTITY` (`Settings.audit_client_identity`), off by
default. With it off, no row gets the block and every row stays as before.

With it on, every audit row gets one payload block `client`:

```json
{"azp": "meho-cli", "channel": "token", "principal_kind": "user", "sid_hash": "3f1c0a9b2d4e5f60"}
```

| Key | What it is |
|---|---|
| `azp` | The login client the token was made for (the `azp` claim). `null` when the token has none. |
| `channel` | `web` for a call from a person's web console session, `token` for every call that came with a token (command line, MCP, REST, a service login). |
| `principal_kind` | `user`, `agent`, `service` or `runner`. |
| `sid_hash` | The first 16 hex characters of the SHA-256 of the `sid` claim (the sign-in session). It groups the rows of one sign-in. MEHO never stores the raw `sid`. `null` when the token has no `sid`. |

One helper builds the block: `auth/client_identity.py`. The writers:

- MCP rows (`mcp/audit.py:write_mcp_audit_row`), operation rows
  (`operations/_audit.py:write_audit_row`) and approval rows
  (`operations/approval_queue.py:_write_audit_row`) take the block from the
  `Operator` they already have. The block always comes from the checked
  login: a `client` key that came another way is replaced.
- The REST request row (`audit.py:_write_audit_row`) is written by the audit
  middleware, which has no `Operator`. The login check of the request
  (`middleware.py:verify_jwt_and_bind`, and for the web console
  `ui/auth/refresh.py:_verify_session_token`) keeps the block in a context
  variable, and the middleware reads it. The middleware resets it at the
  start of each request. A request without a checked login has no block.
- A console page checks its stored token in the session middleware
  (`ui/auth/revalidation.py:revalidate_read_session`), which runs before
  the audit middleware's reset. So that check keeps the block per session,
  next to the time of the last check. The session context
  (`UISessionContext.client`) carries it, and the page dependency
  `require_ui_session` binds it again. A page view that does not check the
  token again (it was checked within the last few minutes) reuses the kept
  block. With the switch on, the first page view after login checks the
  token once to build the block.

`meho audit show` prints it as `via: <client> (<channel>)` (`-` when the row
has no block). The console audit detail shows it as "Via".

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
  │       [+ work_ref IN (:cleaned, :typed)]                    ← whole id
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
  │   ├─ result_status = _derive_result_status(row)
  │   ├─ principal_name = payload['principal_name'] if str
  │   │                  else None   # MCP, approval and DISPATCH rows
  │   │                              # carry it in payload; HTTP-chassis
  │   │                              # rows still have no source
  │   └─ AuditEntry(... real cols ..., op_id, op_class, result_status,
  │                 parent_audit_id=row.parent_audit_id,
  │                 agent_session_id=row.agent_session_id,
  │                 work_ref=row.work_ref,
  │                 policy_decision=row.policy_decision,
  │                 policy_rule=row.policy_rule,
  │                 policy_version=row.policy_version,
  │                 event_class=row.event_class,
  │                 principal_name=principal_name, broadcast_event_id=None)
  │
  └─ next_cursor = encode_cursor(CursorPosition(ts=last.ts, id=last.id))
                   if has_more else None
```

## One rule for a ticket id (work ref)

Every door cleans a ticket id with `normalize_work_ref` in
`meho_backplane/work_ref.py`, so one ticket id is the same text
wherever it enters MEHO. An audit search (`filter-work-ref`) then finds
every row of one ticket, and a run approval covers exactly the calls of
that ticket, whichever door they came through.

The doors: the `Meho-Work-Ref` header (the audit middleware, also for
`/mcp`), the `call_operation` body and MCP input, the MCP agent run and
announce inputs, the REST announce body, a runbook start, a scheduler
trigger, a run approval request, and the console forms.

The rule:

- White space at both ends is removed.
- An empty value is refused.
- A control character (also NUL, a line break and a tab), an invisible
  format character (zero-width and direction marks), a line or paragraph
  separator and a lone surrogate are refused.
- The text is at most 256 characters, counted after NFC.
- The text is stored in Unicode form NFC.

A value that breaks the rule is refused with a message that names the
rule. The check runs before anything else happens with the request:
before the policy gate, before an operation is sent and before a row is
written. Nothing is cut or changed without a word, apart from the strip
and NFC.

- A call without a ticket sends no `Meho-Work-Ref` header. A header that
  is sent but empty or blank is refused with HTTP 400, like any other
  bad value.
- No door checks the length before the rule does. A ticket id sent in
  NFD form, or with spaces at the ends, can be longer than 256 code
  points and still be taken. The body size limit of the server bounds
  the raw input.
- A 422 answer for a bad body, query or path value names the rule and
  the code point. It does not show the refused ticket id: the `input`
  field is left out of the error (`api/request_validation.py`). It is
  also left out of any other error of the same answer whose input holds
  the refused ticket id, for example a missing field, which carries the
  whole body. A header
  that is refused leaves one log line, `work_ref_header_refused`, with
  the request id, the method, the URL path (`path`) and the rule that failed
  (`empty`, `invisible_character` or `too_long`). It never holds the
  value. So an operator can find a client that sends a blank or bad
  header after an upgrade.

Rows stored before this rule are not changed. A ticket id stored before
this release may carry white space at the ends, or another Unicode form,
so a run approval made for the cleaned text does not match such a row.
A search filter still finds it with the exact old text, if that text
follows the rule (see below).

### Search filters

A filter on a ticket id is checked with the same rule as a stored one
(the type `WorkRefFilter`), and a bad value is refused with the same
message (HTTP 422, or invalid params on MCP). A good value matches two
texts (`work_ref_filter_values` in `work_ref.py`): the cleaned id
(stripped and made NFC), so a search finds a ticket id however it is
typed, and the text exactly as typed. So a search finds the exact text
stored before the rule, if that text follows the rule. An old id that
breaks the rule (for example with a tab) can no longer be used as a
filter; such rows still show in a list without a filter. A filter
`"  CHG1  "` finds rows stored as `CHG1` and as `  CHG1  `; a filter
`CHG1` finds only `CHG1`. The filters are:

- the audit search (`POST /api/v1/audit/query`, `POST
  /api/v1/audit/class-counts`, `GET /api/v1/audit/by-work-ref/{ref}`;
  CLI `meho audit query --filter-work-ref`);
- the approvals list (`GET /api/v1/approvals`, MCP `meho_approvals_list`);
- the agent runs list (`GET /api/v1/agents/runs`, MCP
  `meho_agents_list_runs`);
- the scheduler list (`GET /api/v1/scheduler/triggers`, MCP
  `meho_scheduler_list`);
- the runbook runs list (`GET /api/v1/runbooks/runs`, MCP
  `meho_runbook_list_runs`);
- the broadcast history (`GET /api/v1/broadcast/recent`, MCP
  `meho_broadcast_recent` and `meho_broadcast_watch`, key
  `filter.work_ref`).

An empty or blank filter is refused on every REST filter and on the
nested broadcast filter (`filter.work_ref` of `meho_broadcast_recent` and
`meho_broadcast_watch`). Leave the filter out for no filter. On the
top-level filter of the MCP list tools `meho_approvals_list`,
`meho_agents_list_runs`, `meho_scheduler_list` and
`meho_runbook_list_runs`, a blank value means "no filter": MCP drops
blank optional text arguments of read tools before it checks them
(`mcp/handlers.py`). `tests/test_mcp_empty_optional_filter.py` pins this.

The console search boxes of the agent runs, the scheduler, the audit
and the approvals pages, and the approvals "in order" page, check the
value with the same rule (`filter_work_ref` in
`ui/routes/work_ref_form.py`): they clean first and then count the
length. A blank box means no filter. The "in order" page needs a ticket,
so there a blank value is refused too.

A refused value still gets its page, list or window, with no rows and
the rule text in it (`refused_filter_text`). The answer is a 422 with the
header `Meho-Work-Ref-Refused` (`mark_work_ref_refused`). htmx does not
swap an error answer by itself, so the console script
`static/src/app/work-ref-refused.js` swaps in a 422 HTML answer with
that header. So a bookmarked page and a search-as-you-type box both show
why the value was refused.

The "Load more" button of the audit and approvals pages adds its answer
at the end of the rows list. A refused answer there is the whole results
area, so it also carries the htmx headers `HX-Retarget` and `HX-Reswap`.
They make htmx replace the results area instead of adding to the list.

The "in order" window lists the same requests as the Pending tab
filtered with the same text: the cleaned id and the text as typed. The
step numbers on the Pending tab count the same rows. With a filter,
`pending_chain_positions` numbers all the rows it matched as one change
ticket, also when they are stored in two forms (for example `CHG1` and,
from before the rule, `CHG1 ` with a space at the end). So the numbers in
the list and in the window agree.

### How a new door is found

`tests/test_work_ref_doors.py` walks every REST and console request
model field, every query, path, header and form parameter, and every MCP
tool argument named `work_ref`. It fails when one does not use the
shared type (`WorkRef`, or `WorkRefFilter` for a search filter) or the
shared function (`normalize_work_ref`, `work_ref_argument` for MCP),
unless it is on a short list with a reason (`_REST_OTHER_DOORS`, 13
entries):

- the `Meho-Work-Ref` header of the two agent run routes;
- the four console forms that store a ticket id (`form_work_ref`);
- the seven console search boxes and pages that check the value with
  `filter_work_ref` (agent runs, scheduler, audit page, audit results,
  the two approvals search boxes and the approvals "in order" page).

The test fails too when an entry of the list matches no route. The
command line tool has the same rule in `cli/internal/api/workref.go`.
Both tests read one case list, `testdata/work_ref_cases.json`.

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
- A CLI call sends the id of its login session in the
  `Meho-Session-Id` header. When no agent run or MCP session is bound,
  the rows of that request carry it as `agent_session_id`, and their
  payload has `session_from_client: true`. That key tells a CLI login
  session apart from an agent run or an MCP session.
- A call without either header (a plain REST call, an MCP client that
  sends none) still lands with a NULL `agent_session_id`.

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
| `POST /api/v1/audit/class-counts` | Body is `AuditClassCountsRequest`: the `AuditQueryRequest` filters without `event_class`, `has_trace`, `limit` and `cursor` (those fields are a 422). 200 body is `AuditClassCounts` (`{counts, total, since}`), one count per event class (#3747). Without `since` only the last 24 hours count; `since` echoes the window start. | Counters. CLI: `meho audit count` (#4983). |
| `GET /api/v1/audit/who-touched/{target}` | Path param becomes `filters.target`; `since` query defaults to `"24h"`. | Pre-canned shortcut. |
| `GET /api/v1/audit/by-work-ref/{ref}` | Path param (`{ref:path}` converter — a work_ref carries embedded slashes, `#` is percent-encoded) becomes `filters.work_ref` (whole ticket id); `since` query has **no** default window (a change-ticket lookup wants the whole governed history of the ref, not just the last 24h). The "show every write authorised by ticket X" lookup (work_ref I1-T3 #1658). | Pre-canned shortcut. |
| `GET /api/v1/audit/my-recent` | `filters.principal_sub = operator.sub` (exact match on the user id, never the name filter); `since` query defaults to `"24h"`. | Pre-canned shortcut. |
| `GET /api/v1/audit/show/{audit_id}` | `filters.audit_id = <path>`, `limit=1`. Substrate returns 0 rows for cross-tenant lookups → router raises **404** (not 403) so existence never leaks. | Single-row fetch. |
| `GET /api/v1/audit/traces` | Finds stored traces by `request_id`, `caller` and the window `since` / `until`, without the audit row (#6452). See "Find a trace without its audit row". 200 body is `{items: [TraceView]}`. | Flight-recorder trace search. |
| `GET /api/v1/audit/{audit_id}/trace` | The dispatch flight-recorder read. Audit-existence check via the same tenant-scoped substrate (`audit_id`, `limit=1`) → **404** (not 403) on a missing / cross-tenant row, and for a service login on a row of another login, matching `show`. Then `flight_recorder.load_trace(audit_id, tenant_id)` loads the trace header + ordered spans (tenant-scoped again as defence in depth). Only an `operation_execution` row reads a trace; any other class answers `trace_present=false` without a trace read. 200 body is `AuditTraceResult` (`{audit_id, trace_present, trace, event_class}`); an audit row with **no** captured trace is a `200` with `trace_present=false` (capture is opt-in/best-effort, not an error), and `trace.redaction_uncertain` surfaces the F5 degrade flag. A person keeps full access, independent of the agent gate. An agent login (`principal_kind=agent`) gets the agent rules: no trace unless `should_expose_to_agent` is true for the tenant (checked before the load), only the trace of its own call (the trace's `operator_sub` is the agent's `sub`; a trace with no stored caller is never shown to an agent), and never a trace marked `redaction_uncertain`. A service login (`principal_kind=service`, also a paired add-on) gets only the trace of its own call. Each answers `trace_present=false` (`_load_trace_for`). | Flight-recorder trace. |
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
`"meho.audit.query"` for the five query routes (`query`, `who-touched`,
`by-work-ref`, `my-recent`, `show`) and `class-counts`, `"meho_audit_replay"` for
the replay route, and `"meho_audit_trace"` for the flight-recorder trace
route (so operators can tell replay and trace-read usage apart from
flat-query usage in `audit_log`). The shared `audit_query` op_class flips
the broadcast event to aggregate-only (`{op_class, result_status,
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

### Who may read which audit rows and traces

| Caller | Audit rows (list, count, filter, `show`, `by-work-ref`, `who-touched`, the `audit.loose_rows` operation) | Session tree (`shape="tree"`, `meho_audit_replay`, REST replay) | Flight-recorder trace |
|---|---|---|---|
| A person (`principal_kind=user`) | Every row of the tenant. | `shape="tree"`: only the rows of the person's own calls in the session; a session with none answers like a session that does not exist. `meho_audit_replay` and REST replay (tenant_admin): any session of the tenant. | Every trace of the tenant, also one marked `redaction_uncertain`. |
| A service login: an agent, a runner, an add-on, any client-credentials login (also one with no `principal_kind` claim, which MEHO reads as `service`) | Only the rows of its own calls (`operator_sub` equals its own `sub`), whatever role it carries. | Only its own rows of the session, on every tree path. A session of another login answers like a session that does not exist. | Only the trace of its own call: `GET /{audit_id}/trace` needs a row of its own call, or a trace whose stored caller is its own `sub` (a call a person approved and ran: the row names the approver, the trace names the login that asked). The trace's stored caller must be its own `sub` (a trace with no stored caller is never shown); `GET /traces` filters on its own `sub`. An agent also needs the tenant switch, and never gets a `redaction_uncertain` trace. |

How it works: `audit_query.own_rows_sub(operator)` gives `None` for a
person and the login's `sub` for every machine kind
(`auth.operator.is_human_principal`). Every audit read passes it to the
substrate as `owner_sub`: `query_audit`, `count_event_classes`,
`replay_session` (on the anchor and on every lineage step, so a row of
another login is left out with its subtree), `count_loose_rows_in_forest`
(only the login's own rows count as present), `find_loose_rows` (the
`audit.loose_rows` operation: rows and counts of the login's own rows
only; a Sensor runs it as a person, so a check still sees the whole
tenant) and the two session counts
of the replay routes. So no filter, also not `has_trace`, and no count or
error text tells anything about the rows of another login. A row of
another login answers like a row that does not exist: `show` and the
trace route give the same 404, a list gives no row.

The `shape="tree"` path of `query_audit` passes the caller's own `sub` as
`owner_sub` for every caller, a person too. Each stored row names the login
of the checked token. So a session id the client sends, also in the
`Mcp-Session-Id` header, shows no row of another login, even when the
caller wrote rows of its own into that session. A runner never reaches these routes
(`runner_scope_violation`), and an agent acts with at most the operator
role, so it never reaches the tenant_admin replay paths. The rows of a
call that a person approved and ran are the person's rows: the agent that
asked sees its own request row, not the approver's decision and run rows.
The trace route makes one exception for the trace: the agent that asked
reads the trace of the run, and the answer holds no field of the
approver's row besides `event_class`.

**Tenant-broadcast read surfaces for people.** Within a tenant,
`query` and `show` are principal-agnostic for a person: `POST /api/v1/audit/query` (with
or without a `principal` filter) and `GET /api/v1/audit/show/{audit_id}`
return rows for **every** principal in the calling tenant. Any `operator`
/ `tenant_admin` person in the tenant reads all principals' audit rows —
there is **no per-principal role check** on either route, and filtering
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

The MCP entry is one tool, `query_audit`, registered in
`backend/src/meho_backplane/mcp/tools/audit.py` against the G0.5 tool
registry. It is an operator-plane tool (`surface=ToolSurface.OPERATOR`):
it is listed only for a session with the `mcp:admin` elevation, not on
the default working surface (see [mcp.md](mcp.md)). The REST per-shape
shortcuts (`who-touched` / `my-recent` / `show`) collapse into filter
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

The input schema accepts the same filters as the REST body, except
`work_ref` and `has_trace`, and including `event_class` (#4807). Its `enum` is built from `AuditEventClass` plus
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
* `op_class="audit_query"` on the `ToolDefinition` is the class the
  MCP handler uses: it passes the declared class to
  `broadcast/events.py::resolve_op_class` (`mcp/handlers.py`), so the
  audit row and the broadcast event both carry `audit_query`, and the
  event is aggregate-only.

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

The tree path is **self-session only** and stricter than the flat path
(which returns other in-tenant principals' rows to a person):
`agent_session_id` must be present (absent → `-32602`). The tree holds
only the rows of the caller's own calls, for every caller, a person too.
A session with no rows of the caller's own calls answers like a session
that does not exist: an empty tree. Cross-session forensic replay is the
`tenant_admin`-gated `meho_audit_replay` tool, not this path.

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
  foreign session id yields an empty `root`). A service login with
  `tenant_admin` replays only its own rows: a session of another login
  yields an empty `root`.
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

The MCP handler takes the tool's declared `op_class` through
`resolve_op_class(name, defn.op_class)`. `classify_op` also carries a
`meho_audit_` prefix arm next to the `audit.` arm. When `classify_op`
says an op id touches the audit log, that class wins over any declared
class, so a declared class can never turn the `meho_audit_replay`
broadcast into a full `ReplayNode` tree. (The
arm was originally the dotted `meho.audit.` prefix for the pre-#2745
tool name `meho.audit.replay`; the dotted arm is retained because
`classify_op` re-runs at render time on stored op-ids, so historical
rows keep their class.)

## Dependencies

* `meho_backplane.db.models.AuditLog` — read schema; this package never writes.
* `meho_backplane.db.models.Target` — denormalization source for `target_name`.
* `meho_backplane.db.models.Tenant` — join source for `tenant_name`.
* `meho_backplane.db.models.DispatchTrace` — the `has_trace` filter.
* `meho_backplane.broadcast.events.classify_op` — op_id → op_class lookup;
  shared with the broadcast publish path so audit-query and broadcast classify
  identically.

Reverse dependencies:

* `meho_backplane.api.v1.audit` (T2 #466) — REST router for the seven
  audit routes. Flat queries go through `query_audit`, replay through
  `replay_session`, and trace through `flight_recorder.load_trace`.
  `class-counts` lives in `meho_backplane.api.v1.audit_counts`.
* `meho_backplane.mcp.tools.audit` (T4 #468, extended G8.2-T6 #1014) —
  the `query_audit` MCP meta-tool plus the `meho_audit_replay` admin
  tool; dispatches flat queries through `query_audit` and replay
  (admin tool + `shape="tree"`) through `replay_session`.
* The `meho audit` CLI (T3 #467), in `cli/internal/cmd/audit/`.

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

The ``op_class="ui_view"`` is a new class (from a consumer's
v0.7.0 closed-loop dogfood)
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
  put the caller's name into `payload['principal_name']` (the JWT
  `name` claim, else an agent's registered name or a machine token's
  client id, #3885), and the audit-query handler reads that key off the JSON
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
* Op-class classifier: `backend/src/meho_backplane/broadcast/events.py`
  (`classify_op`).
* Schema: `backend/src/meho_backplane/db/models.py` (`AuditLog`,
  `Target`).
* Migrations: `backend/alembic/versions/0001_create_audit_log.py`,
  `backend/alembic/versions/0002_create_tenant_and_audit_tenant_id.py`,
  `backend/alembic/versions/0004_*` (target_id on audit_log).
