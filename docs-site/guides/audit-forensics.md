# Audit forensics

Every authenticated operation against the backplane — a CLI verb, an
MCP tool call, a REST request — writes exactly one **audit row**, and
that row does not commit until the operation is allowed to report
success. The ledger is append-only and synchronous: if the audit write
fails, the operation fails. That property is what makes the audit log
answer *"who did X to Y, and when?"* with authority rather than
best-effort.

This guide covers the `query_audit` filter surface, how the audit log
relates to the broadcast feed, and how to reconstruct an entire
agent session end to end. For the actual vendor request/response traffic
behind a single dispatch — one level below the audit row — see the
[flight recorder](flight-recorder.md).

!!! note "Prerequisites, roles, and maturity"

    - A running backplane and a connected client
      ([Connect clients](../clients/index.md)).
    - Querying needs the **operator** role — and returns your whole
      tenant's rows, not just your own. Replaying **another** operator's
      or agent's session end to end needs **tenant_admin**.
    - Audit is **GA** — the 1.0 stability promise applies (see the
      [feature-maturity index](../reference/maturity.md#ga-features)).

## What a row records

Each `AuditEntry` carries the forensic fields you filter and pivot on:

| Field | What it is |
|---|---|
| `id` | the audit row's UUID (the `audit_id` filter targets this) |
| `ts` | when the operation occurred |
| `principal_sub` / `principal_name` | the subject that acted (the JWT `sub`) |
| `target_name` | the target it acted on |
| `op_id` / `op_class` | the operation and its sensitivity class |
| `result_status` | `ok` / `error` / `denied`; `pending` when the run stopped to wait for approval (the row stays `pending` after the decision); or `withdrawn` when the sender took its approval request back (the operation never ran) |
| `policy_decision` | the gate verdict: `auto-execute`, `needs-approval`, or `deny` |
| `agent_session_id` | the originating agent's MCP session id — the key for session tracing |
| `parent_audit_id` | the id of the row that caused this one. An approval's decision row and run row point at the approval request row. |
| `work_ref` | an external change-ticket reference, when one was stamped |
| `event_class` | what kind of record it is: `operation_execution`, `policy_decision`, `approval_decision`, `security_event`, `config_change` (an operator changed the settings of one operation; it did not run), or `legacy_request` (every row from before this field existed) |
| `sandbox_mode` | `true` when MEHO ran in sandbox mode when it wrote the row, else `false` |
| `actor_sub` / `actor_name` | the agent that acted for the person in `principal_sub`, in an agent run the person started (`agent:<name>`, and the name alone); empty when nobody acted for another login |
| `method` / `path` / `status_code` / `duration_ms` / `payload` | the request particulars. A refused call keeps why under `payload.error`: a `reason_code` (for example `agent_grant_denied` or `role_insufficient`) and a short `reason` text |

## The filter surface

The agent sees **one** tool, `query_audit` — every forensic shape is a
filter combination, not a separate tool. On MCP, `query_audit` is an
operator-plane tool: a session lists it only when it asked for the
`mcp:admin` scope. The CLI adds pre-canned shortcuts over the same
substrate.

| Filter | `query_audit` arg | CLI |
|---|---|---|
| One row by id | `audit_id` | `meho audit show <id>` |
| Everything on a target | `target` | `meho audit who-touched <target>` |
| Your own recent activity | `principal=<your sub>` | `meho audit my-recent` |
| Arbitrary combination | any of the below | `meho audit query …` |

`query_audit` (and `meho audit query`) accept: `target`, `principal`
(part of the user id or of the name; case does not matter), `op_id`
(glob, e.g. `vault.*`), `op_class` (`read` / `write` /
`credential_read` / `credential_write` / `credential_mint` /
`audit_query` / `approval` / `checks` / `other`), `result_status`,
`since` / `until` (ISO-8601 **or** shorthand: `30m`, `24h`, `7d`, `2w`),
`audit_id`, `parent_audit_id`, `agent_session_id`, `work_ref`, `limit` (default 100, max
1000), and `cursor`. Calling it with **no** filters returns the most
recent 100 rows of your tenant — bounded, never the whole ledger.

`query_audit` and the REST route `POST /api/v1/audit/query` also accept
`event_class` (one of the six record kinds above). It returns only rows
of that kind. Without it you get every kind. The CLI has no
flag for it yet.

`POST /api/v1/audit/class-counts` takes the same filters (except
`event_class`, `limit` and `cursor`) and returns how
many rows of each class match. Without `since` it counts only the last
24 hours. The answer's `since` field says where the counted window starts.
The console audit page shows the same counts, with the same window, and has
a "Record type" filter.

Only an `operation_execution` row has a flight-recorder trace. For any
other class, the trace route answers `trace_present: false`.

```bash
# "Did anyone read a Vault secret before the outage?"
meho audit query --op-class credential_read --since 24h --result-status ok

# "Show me every denied write this week."
meho audit query --op-class write --result-status denied --since 7d

# "What happened because of this approval request?"
# Returns the decision row and the run row. It works even when the
# rows have no agent session.
meho audit query --parent-audit-id <request-row-id>
```

The agent surface returns the same rows, sorted newest-first, in a paged
envelope:

```json
// query_audit {"target": "prod-rke2", "op_class": "write", "since": "7d"}
{
  "rows": [
    {"id": "…", "ts": "2026-07-29T14:03:11Z", "principal_sub": "…",
     "target_name": "prod-rke2", "op_id": "k8s.delete",
     "op_class": "write", "result_status": "ok",
     "policy_decision": "needs-approval", "agent_session_id": "…",
     "work_ref": "gh:evoila/meho#221"}
  ],
  "next_cursor": null
}
```

`next_cursor` round-trips as the next call's `cursor`; `null` means the
page is the end of the matching set. Tenant scoping is automatic — the
JWT sets the boundary, and there is no `tenant_id` argument, so a
cross-tenant probe is structurally impossible.

## The audit log and the broadcast feed

The [broadcast feed](broadcast.md) carries only notes that agents post.
MEHO does not post operation runs to it, and an announcement does not
link to an audit row. To find what ran, query the audit log.

The `AuditEntry` carries a `broadcast_event_id` field. It is always
`null`. Do not filter audit rows by it.

## Tracing an agent session end to end

When an autonomous agent fans out dozens of operations, `agent_session_id`
ties them together. Filter flat:

```bash
meho audit query --session-id <agent_session_id> --since 24h
```

…or reconstruct the whole session as a parent/child tree. Over MCP an
operator replays **their own** session:

```json
// query_audit {"shape": "tree", "agent_session_id": "<your own session id>"}
{"root": [ /* ReplayNode forest, each node = an audit row + depth + children */ ],
 "session_id": "…", "tenant_id": "…", "row_count": 37,
 "excluded_null_session_count": 0}
```

Replaying **someone else's** session is a tenant_admin action —
`meho audit replay <session-id>` (CLI) or the `meho_audit_replay` tool.
Two guard rails worth knowing:

- A session larger than 10,000 rows is refused with `session_too_large`
  rather than returning a context-blowing tree — narrow with `since` /
  `until` and page the flat shape instead.
- `excluded_null_session_count` counts tenant MCP rows that carry **no**
  session id (a client that reached `/mcp` without negotiating a
  session). A non-zero value on an otherwise empty forest means "this
  identity's MCP traffic is un-negotiated and invisible to lineage,"
  not "this session did nothing."

## What you never leak

A `query_audit` call is itself an audited operation (`op_class:
audit_query`). Your query is not posted to the broadcast feed at all,
so an investigator searching for a compromised credential does not
themselves broadcast the credential's path.

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| `broadcast_event_id` is `null` on every row | Working as designed — the field is always `null`. The broadcast feed carries only agent notes, which do not link to audit rows. | Filter the audit log by `target`, `principal`, `op_id` and time instead. |
| `parent_audit_id` returns fewer rows than the full tree | The filter returns direct children only, not grandchildren. | Run it again with a child's `id`, or use `meho audit replay` for a whole session. |
| `shape="tree"` rejected with `-32602` naming your session | The tree path is **self-session only** for operators — the `agent_session_id` must equal your own MCP session id. | To replay another session, use the tenant_admin `meho audit replay <session-id>`. |
| A replay is refused with `session_too_large` | The session exceeds the 10,000-row replay cap. | Narrow with `--since` / `--until` and page the flat `meho audit query --session-id …` shape. |
| `meho audit query --since Tuesday` errors | `since` / `until` want ISO-8601 or the duration shorthand, not prose. | Use `--since 3d` or `--since 2026-07-28T00:00:00Z`. |
| An empty result when you expected rows | The tenant boundary is on your JWT — a target/session in another tenant returns zero rows, not another tenant's data. | Confirm you are authenticated to the right tenant. |

**Next:** [Flight recorder](flight-recorder.md).
