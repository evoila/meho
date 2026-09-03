# Audit forensics

Every authenticated operation against the backplane — a CLI verb, an
MCP tool call, a REST request — writes exactly one **audit row**, and
that row does not commit until the operation is allowed to report
success. The ledger is append-only and synchronous: if the audit write
fails, the operation fails. That property is what makes the audit log
answer *"who did X to Y, and when?"* with authority rather than
best-effort.

This guide covers the `query_audit` filter surface, how to correlate an
audit row with the live broadcast feed, and how to reconstruct an entire
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
| `result_status` | `ok` / `error` / `denied` |
| `policy_decision` | the gate verdict: `auto-execute`, `needs-approval`, or `deny` |
| `agent_session_id` | the originating agent's MCP session id — the key for session tracing |
| `work_ref` | an external change-ticket reference, when one was stamped |
| `method` / `path` / `status_code` / `duration_ms` / `payload` | the request particulars |

## The filter surface

The agent sees **one** tool, `query_audit` — every forensic shape is a
filter combination, not a separate tool. The CLI adds pre-canned
shortcuts over the same substrate.

| Filter | `query_audit` arg | CLI |
|---|---|---|
| One row by id | `audit_id` | `meho audit show <id>` |
| Everything on a target | `target` | `meho audit who-touched <target>` |
| Your own recent activity | `principal=<your sub>` | `meho audit my-recent` |
| Arbitrary combination | any of the below | `meho audit query …` |

`query_audit` (and `meho audit query`) accept: `target`, `principal`
(operator-sub substring), `op_id` (glob, e.g. `vault.*`), `op_class`
(`read` / `write` / `credential_read` / `audit_query`), `result_status`,
`since` / `until` (ISO-8601 **or** shorthand: `30m`, `24h`, `7d`, `2w`),
`audit_id`, `agent_session_id`, `work_ref`, `limit` (default 100, max
1000), and `cursor`. Calling it with **no** filters returns the most
recent 100 rows of your tenant — bounded, never the whole ledger.

```bash
# "Did anyone read a Vault secret before the outage?"
meho audit query --op-class credential_read --since 24h --result-status ok

# "Show me every denied write this week."
meho audit query --op-class write --result-status denied --since 7d
```

The agent surface returns the same rows, sorted newest-first, in a paged
envelope:

```json
// query_audit {"target": "rdc-vcenter", "op_class": "write", "since": "7d"}
{
  "rows": [
    {"id": "…", "ts": "2026-07-29T14:03:11Z", "principal_sub": "…",
     "target_name": "rdc-vcenter", "op_id": "vsphere.host.maintenance",
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

## Correlating with the broadcast feed

The audit ledger and the [broadcast feed](broadcast.md) are two views of
the same operations — the audit row is the durable forensic record; the
broadcast event is the live, redacted announcement. They are linked, but
mind the direction:

!!! warning "The link lives on the broadcast event, not the audit row"

    The `AuditEntry` carries a `broadcast_event_id` field, but in this
    version it is a **placeholder that is always `null`** — the
    foreign key runs the *other* way. Each **broadcast event** carries
    an `audit_id` pointing back at its audit row.

So the correct pivot is broadcast → audit:

1. You spot an event on the feed (`meho status --watch --json`) and read
   its `audit_id`.
2. You pull the full forensic row with that id:

    ```bash
    meho audit show <audit_id>
    ```

Do **not** try to filter audit rows by `broadcast_event_id` — it is
unpopulated. Going the other way (audit → feed) is not an id lookup
either: match on the operation's attributes instead (`principal`,
`target`, `op_id`, and the timestamp window), remembering that
`credential_read` and `audit_query` events are aggregate-only on the
feed by redaction policy while the audit row keeps the full detail.

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
audit_query`), and the **contents** of your query — which target,
which principal — are never published to the broadcast feed. Only the
`{op_id, result_status, row_count}` aggregate appears, so an
investigator searching for a compromised credential does not themselves
broadcast the credential's path.

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| `broadcast_event_id` is `null` on every row | Working as designed — it is a v0.2 placeholder; the FK lives on the broadcast event's `audit_id`. | Pivot broadcast → audit: read the event's `audit_id`, then `meho audit show <id>`. |
| `query_audit` with `parent_audit_id` returns `-32602` | The composite-subtree filter is not wired in this version. | Use `agent_session_id` + `shape="tree"` (or `meho audit replay`) for lineage instead. |
| `shape="tree"` rejected with `-32602` naming your session | The tree path is **self-session only** for operators — the `agent_session_id` must equal your own MCP session id. | To replay another session, use the tenant_admin `meho audit replay <session-id>`. |
| A replay is refused with `session_too_large` | The session exceeds the 10,000-row replay cap. | Narrow with `--since` / `--until` and page the flat `meho audit query --session-id …` shape. |
| `meho audit query --since Tuesday` errors | `since` / `until` want ISO-8601 or the duration shorthand, not prose. | Use `--since 3d` or `--since 2026-07-28T00:00:00Z`. |
| An empty result when you expected rows | The tenant boundary is on your JWT — a target/session in another tenant returns zero rows, not another tenant's data. | Confirm you are authenticated to the right tenant. |

**Next:** [Runbooks](runbooks.md).
