# Request usage events

## Overview

A request usage event records that someone used a REST route or an MCP
tool, and how it ended. It is telemetry about the request boundary. It is
not an accountable action, so it is stored apart from `audit_log`, in the
`request_usage_event` table (#3734).

What is true today:

- Collection is **off** unless `REQUEST_USAGE_RETENTION_DAYS` is set.
  When it is off, nothing is written and no request is slowed or blocked.
- When it is on, every authenticated HTTP request (REST and `POST /mcp`)
  writes exactly one `request_finished` event.
- A daily job deletes events older than the retention window.
- By default the wrapper `audit_log` rows for REST and MCP are still
  written too. Set `AUDIT_WRAPPER_ROWS_RETIRED=true` to stop the routine
  ones (#3757). See [Retiring the wrapper rows](#retiring-the-wrapper-rows).
- The retrieval usage, retirement checklist and reflex adoption reports
  read these events (`source="request_usage"`). Their REST and console
  routes switched to them in #3747. When collection is off, those
  reports show zero activity.

## What an event holds (and what it never holds)

`meho_backplane.request_usage.schema.RequestUsageEventData` is the only
way an event is built. It allows:

| Field | Meaning |
|---|---|
| `id`, `occurred_at` | Stamped by the server. |
| `tenant_id`, `principal_sub` | Who made the request. Opaque ids only. |
| `session_id`, `run_id`, `request_id` | Opaque correlation ids, when known. |
| `transport` | `rest` or `mcp`. |
| `route` | A REST route template (`/api/v1/targets/{target_id}`), or an MCP label: `tool:<name>`, `resource:<uri template>`, `method:<json-rpc method>`. |
| `outcome` | `ok`, `client_error`, `denied`, `not_found`, `rate_limited`, `server_error`. |
| `duration_ms` | How long the request took. |
| `correlation_id` | Only on `context_selected` events. |

The schema refuses anything else (`extra="forbid"`). So an event can never
hold a request or response body, a token, a query string, tool arguments,
a prompt, a target name, or an error message. A refused request is only the
`denied` outcome; the reason is not stored. A principal value that looks
like a token is refused too.

The route must be in a `RouteCatalog`: the route templates of the running
app plus the registered MCP methods, tools and resource templates. A
concrete path (`/api/v1/targets/prod-db`) is never in the catalog, so it
is never stored. A request with no known route is stored as `<unmatched>`.

## Control flow

1. `AuditMiddleware.__call__` calls `open_request_usage()` at the start of
   every HTTP request. It returns `None` when collection is off.
2. The boundary is kept in a `ContextVar`. If a boundary is already open
   (a nested middleware layer or handler), `open_request_usage()` returns
   `None`. Only the code that opened the boundary closes it, so a request
   can never write two `request_finished` events.
3. Code inside the request only adds facts to the open boundary:
   - `mcp/server.py` labels the request `method:<name>`;
   - `mcp/handlers.py` narrows it to `tool:<name>` after the tool lookup
     succeeds, or `resource:<template>` after a template matches;
   - `mcp/audit.py::write_mcp_audit_row` records the projected MCP status
     (MCP answers HTTP 200 even when a call fails), and 500 if the audit
     commit fails;
   - `mcp/server.py::_note_request_usage_outcome` runs after the dispatch.
     When the response is a JSON-RPC error envelope and no handler
     recorded a status, it records the status the error code stands for
     (parse error, invalid request and invalid params are 400, method not
     found is 404, rate limited is 429, internal error is 500). So a
     malformed body, an unknown method or a rate-limited `initialize` is
     never counted as `ok`. A status a tool handler recorded wins;
   - `note_context_selected()` (optional) records that context was picked.
4. The middleware wraps `send`, so the boundary sees the status the client
   really got (including the audit-failure 500).
5. In a `finally`, `RequestUsageBoundary.close()` builds the events and
   writes them in one short transaction. Unauthenticated requests write
   nothing. Any error is logged with the exception class only and
   swallowed: it never changes the response, the audit row, or a policy
   decision.

## Retiring the wrapper rows

`AUDIT_WRAPPER_ROWS_RETIRED=true` (#3757) makes the audit log hold only
actions and decisions. Routine REST and MCP activity is then recorded only
as usage events. `backend/src/meho_backplane/audit_cutover.py` decides,
for each REST request and each MCP tool call or resource read:

| The request | What goes into `audit_log` |
|---|---|
| A classified writer already wrote a row in the caller's name (the dispatcher, an approval decision, a topology edit, a runner revocation, ...) | Nothing more. That row is the record. A row under another identity (a system job's name) does not count, so the wrapper row stays and the record still says who called. |
| Refused: HTTP 401 / 403, an MCP role / capability / surface / add-on gate, or an MCP handler that raises `McpPermissionDeniedError` (the caller may not use this target, memory scope, docs collection, tenant, built-in connector or doc collection, runbook template or run, or another session's audit trail). REST answers 403 for the same checks. | One `policy_decision` row. |
| Routine: a `GET`, a read-only `POST` (search, query, preview, heartbeat, runner results), an MCP `read` or `audit_query` tool, a resource read | Nothing. |
| An operation call (`POST /api/v1/operations/call`, MCP `call_operation`) | Nothing more. The dispatcher writes the execution or policy row, also for a connector error. An async run writes it when the run finishes. |
| Any other 4xx (validation failure, not found, conflict), or an MCP handler that rejects its arguments with a plain `McpInvalidParamsError` | Nothing. |
| Anything else: a MEHO change with no record of its own (create a target, enable a connector, an MCP `write` tool), or one that failed with 5xx, or an MCP handler that raises `McpExecutionFailedError` (the change failed in Keycloak or Vault; REST answers 502 or 503 for the same failure) | One `operation_execution` row. |

A request with no row also publishes no broadcast event. Every request
still writes its usage event.

The wrapper row carried the request's lineage: the agent session, the
delegated actor, the parent record, the request id and the work
reference. Many domain writers (a connector edit, a topology edit, ...)
leave those columns empty. After the cutover, a record written during a
request gets each of them filled from the request context when its writer
left it empty; a value the writer set is kept. So a connector edit made
over MCP still shows up in its session's replay, and its request id
matches the request's usage event. Records written outside a request (a
system job, an async run that finishes after the response) are not
changed.

The routine `POST` routes and the operation routes are two lists in
`audit_cutover.py` (`ROUTINE_POST_ROUTES`, `DISPATCH_ROUTES`). A new
read-only `POST` route must be added to the first list; otherwise, after
the cutover, each call writes an `operation_execution` row.

The cutover is fail closed:

- The setting needs `REQUEST_USAGE_RETENTION_DAYS`. Without it, routine
  activity would leave no record at all, so the pod refuses to start with
  a message that names both settings.
- `tests/test_audit_wrapper_cutover.py` checks the code side: the two
  wrapper writers take the class as an argument, no writer except the
  `approval.result` read row hard-codes `legacy_request`, the three
  reports read usage events, and the route lists match the app. It also
  runs the same cases through REST and MCP.
- With the setting off (the default), nothing changes: every
  authenticated request still writes one `legacy_request` row.

Before you turn it on, set `REQUEST_USAGE_RETENTION_DAYS`. Old rows are
not deleted or reclassified.

The reflex advisory ("did this agent session read the broadcast feed?")
reads both the `meho_broadcast_recent` wrapper row and the session's
`tool:meho_broadcast_recent` usage event, so it works before and after the
cutover. Migration `0111` adds the `session_id` index it uses.

## Context selection

`note_context_selected(correlation_id=None)` adds one `context_selected`
event to the current request, linked by an opaque correlation id (a new
one is made when none is given). It is optional: nothing calls it yet; the
evaluation work uses it later. The event only says context was picked for
this request. It never says the agent used or followed it, and the schema
has no field that could say so. At most 16 are kept per request.

## Reading

- `reader.summarize_request_usage(...)` returns counts per transport,
  route and outcome for one tenant and window. It resolves the tenant with
  `authorize_tenant_scope`: the caller's own tenant, and another tenant
  only for a platform admin (anyone else gets HTTP 403). It never returns
  single events.
- `reader.select_finished_events(...)` is the row source the three reports
  use for `source="request_usage"`. Its columns are labelled like the
  `audit_log` columns those reports read, so the report logic is shared.
  The report routes authorize the tenant before they call it.

## Retention

`request_usage/retention.py` runs once a day when retention is set. It
deletes events with `occurred_at` older than the window, using the
`request_usage_event_occurred_at_idx` index, and writes one `INTERNAL`
summary audit row on a system sentinel tenant. A failed tick is logged and
the loop goes on.

## Key files

- `backend/src/meho_backplane/request_usage/` — schema, collector, reader,
  retention.
- `backend/src/meho_backplane/db/models.py` — `RequestUsageEvent`.
- `backend/alembic/versions/0107_create_request_usage_event.py` — table,
  CHECK constraints, indexes.
- `backend/alembic/versions/0111_add_request_usage_session_index.py` — the
  `session_id` index for the reflex advisory.
- `backend/src/meho_backplane/audit_cutover.py` — which wrapper rows are
  still written (#3757).
- `backend/src/meho_backplane/settings.py` — `request_usage_retention_days`,
  `audit_wrapper_rows_retired`.
- Tests: `backend/tests/test_request_usage.py`,
  `backend/tests/test_audit_wrapper_cutover.py`, plus the
  `source="request_usage"` cases in `test_retrieval_usage.py`,
  `test_retrieval_retire.py` and `test_reflex_adoption.py`.

## Known limits

- There is no default retention window on purpose. The operator picks it.
- The MCP route label is "last one wins". Today no handler calls another
  handler, so the label is the tool or resource of the request.
- Events written before retention was set do not exist, so a report that
  reads usage events only covers the time since collection started.
