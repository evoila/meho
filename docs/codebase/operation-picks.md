# Operation picks: did the agent call what the search returned?

## Overview

MEHO keeps a short record of every `search_operations` call and of every
`call_operation` call. It links each call to the search it followed (#4750).
A report then shows, per connector and per day, how often a call used an
operation that the search returned, and whether the call worked.

This answers one question from real sessions: "After a search, did the agent
pick an operation from the results, and did it run?" When a job fails, it
also tells two cases apart: the search did not return the right operation,
or it returned it and the agent picked another one.

What is true today:

- The model sees nothing new. No tool, argument, result or tool text changes.
- Recording is **off** unless `REQUEST_USAGE_RETENTION_DAYS` is set, the same
  switch as [request usage events](request-usage.md). It does not depend on
  `AUDIT_WRAPPER_ROWS_RETIRED`.
- A failed write is logged with the exception class only. It never changes
  the answer of the tool.
- The same daily job that deletes old request usage events deletes old
  records, with the same number of days.
- The records are not in `audit_log`, and they are not request usage events.

"The right operation was chosen after a search" here means: the call used an
operation from the search results, and it returned `ok`. This is a signal
from real use, not a proof. The job score of the evaluation runs stays the
main measure.

## What is recorded

Three places record, so the numbers cover every way an agent or a person
searches and calls:

| Where | `transport` |
|---|---|
| The MCP tools `search_operations` and `call_operation` | `mcp` |
| The REST routes `GET /api/v1/operations/search` and `POST /api/v1/operations/call`, which the CLI uses | `rest` |
| The tools of a hosted agent run | `agent` |

Calls that MEHO makes for itself are not recorded: runbook steps, console
pages and the run after an approval. A check of a background run
(`call_operation` with `run_id`) is not a new call and is not recorded.
The background run itself updates the outcome of its call record when it
ends; the link to the search does not change.

### The search record (`operation_search_record`)

One row for each search, in ranked mode and in exact-operation mode.

| Field | Meaning |
|---|---|
| `id` | A new random id. |
| `occurred_at`, `tenant_id`, `principal_sub` | When, which tenant, who. |
| `session_id` | The MCP session id (only when the client sends `Mcp-Session-Id` back and it is a UUID) or the hosted agent session id. Else empty. |
| `request_id` | The request id, when there is one. |
| `transport` | `mcp`, `rest` or `agent`. |
| `connector_id`, `group_key`, `search_limit` | What was searched. `group_key` and `search_limit` are empty in exact mode. |
| `mode` | `ranked` or `exact`. |
| `outcome` | The search's `outcome` field (`matched`, `ambiguous`, `no_match`, ...). `exact_op` for a found operation in exact mode. For a refused search: `rate_limited`, `unknown_op`, `unknown_connector`, `connector_not_ingested` or `error`. |
| `hit_count`, `op_ids` | The returned operation ids, in rank order. |
| `query_hash` | SHA-256 of the query text. Empty in exact mode. |
| `query_text` | The query text, only when `OPERATION_SEARCH_STORE_QUERY_TEXT` is on (at most 2000 characters). |

### The call record (`operation_call_record`)

One row for each call.

| Field | Meaning |
|---|---|
| `id`, `occurred_at`, `tenant_id`, `principal_sub`, `session_id`, `request_id`, `transport`, `connector_id` | As above. |
| `op_id` | The operation the call named. |
| `outcome` | The call's status: `ok`, `error`, `denied`, `pending`, `awaiting_approval`, `rate_limited`, or `other`. `unknown_op` when the operation does not exist. `error` when the call raised. A background call (`async: true`) is written as `pending` before it starts; when the background run ends, the same row gets the call's outcome. It stays `pending` only when the run never ended (cancelled before it started, or the worker stopped). |
| `search_id` | The search the call followed, or empty. |
| `rank` | The place of `op_id` in that search's results (1 is the first), or empty when it was not there. |

**How a call finds its search.** MEHO makes the link itself; the model sends
nothing new. The search is the newest one with the same tenant, the same
caller, the same connector and the same session, in the 5 minutes before the
call. "The same session" means: both have the same session id, or both have
none. So when a client sends no session id, the link is by caller and
connector, as `meho retrieval usage` does. A search in another session or on
another connector never links.

## Privacy

- Operation ids, connector ids, group keys, counts and outcomes are MEHO
  catalogue data. They hold no operator data.
- The query text is operator data: it can show what someone was working on.
  By default MEHO stores only its hash. `OPERATION_SEARCH_STORE_QUERY_TEXT=true`
  also stores the text. It is off by default.
- No log line holds the query text.
- The records are kept for `REQUEST_USAGE_RETENTION_DAYS` days. The daily job
  deletes older records, with any stored query text.
- The report reads one tenant. Another tenant needs platform admin access, as
  for `meho retrieval usage`.

## The report

`GET /api/v1/retrieve/operation-picks` and the CLI verb `meho retrieval picks`.

Parameters: `since` (`30d` by default; `7d`, `24h` or an ISO date), an optional
`connector_id`, and `tenant_filter` (platform admin only). It needs the
`operator` role. The audit row is classed `audit_query` under the op id
`meho.retrieval.operation_picks`.

One bucket per day and connector. Searches count on the day of the search,
calls on the day of the call.

| Number | Meaning |
|---|---|
| `searches` | Searches. |
| `searches_followed_by_call` | Searches that a later call was linked to. A call links to the newest search only, so an earlier search on the same connector does not count. |
| `calls` | Calls. |
| `calls_in_results` | Calls whose `op_id` was in the linked search's results. |
| `calls_at_rank_1`, `calls_in_top_3` | Of those, the ones at rank 1, and in the first three. |
| `calls_in_results_ok` | Calls whose `op_id` was in the results and that returned `ok`. |
| `calls_not_in_results` | Calls after a search whose `op_id` was not in its results. |
| `calls_unknown_op` | Calls to an operation that does not exist, with or without a search. |
| `calls_without_search` | Calls with no search before them. |

## Key files

- `backend/src/meho_backplane/operation_picks/recorder.py` — the two records
  and the link.
- `backend/src/meho_backplane/operation_picks/report.py` — the report.
- `backend/src/meho_backplane/operation_picks/retention.py` — the delete,
  called by `request_usage/retention.py`.
- `backend/src/meho_backplane/api/v1/retrieve_operation_picks.py` — the route.
- `backend/alembic/versions/0146_create_operation_pick_records.py` — the tables.
- `cli/internal/cmd/retrieval/picks.go` — `meho retrieval picks`.
- Tests: `backend/tests/test_operation_picks.py`,
  `backend/tests/migrations/test_migration_0146_create_operation_pick_records.py`,
  `cli/internal/cmd/retrieval/picks_test.go`.

## Known limits

- Records exist only from the time `REQUEST_USAGE_RETENTION_DAYS` was set.
- A call links to one search only. When the agent searched twice on the same
  connector, the call links to the second search.
- A client that sends no `Mcp-Session-Id` is linked by caller and connector.
  Two sessions of the same person on the same connector then look like one.
