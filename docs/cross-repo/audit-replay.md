<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Replaying an agent session from the MEHO audit log — operator runbook

> Operator-facing runbook for the G8.2 audit replay surface. It is the sibling of [`audit-query.md`](./audit-query.md): query answers "what happened, by filter?"; replay answers "what did *this one session* do, in order, with what parent/child structure?". Architecture sits in [`docs/architecture/audit.md`](../architecture/audit.md).

## What replay answers (and how it differs from query)

[`meho audit query`](./audit-query.md) is a filter over the whole audit log: target, principal, op-id glob, op-class, result-status, time window. It returns a flat page of rows newest-first. That is the right tool for "who patched rdc-nsx on Tuesday?" — a fan-out across many sessions and many operators.

`meho audit replay` is the opposite shape. You hand it **one** `agent_session_id` and it reconstructs that session's entire run as a **parent/child tree**: every operation the session issued, in chronological order, with composite operations (a runbook that dispatched child operations) nested under their parent. That is the forensics / debugging / compliance shape — "show me everything session `<id>` did, in order, with the lineage between operations" (the Motivation from Initiative [#377](https://github.com/evoila/meho/issues/377)).

| | `meho audit query` | `meho audit replay` |
|---|---|---|
| Question | "What happened (by filter)?" | "What did *this session* do, in order?" |
| Input | Any combination of filters | Exactly one `agent_session_id` (UUID) |
| Output | Flat page of rows + forward cursor | Parent/child `ReplayNode` tree |
| Ordering | Newest-first | Chronological, roots then nested children |
| Spans | Many sessions / operators | One session |

Like the query surface, replay reads the **WORM-grade** (write-once, read-many) audit log written synchronously by the backplane chassis on every authenticated request. There is no operator-facing edit path — replay reconstructs what was logged, never mutates it.

## How to get a session id

Replay is keyed on `agent_session_id` — the MCP-session correlation id (the `Mcp-Session-Id` the client sent, bound onto each audit row that request writes). There are two ways to find one:

### From an audit row

Every audit row carries `agent_session_id` (it is part of the `AuditEntry` shape every query returns). Pull it out with `--json` and `jq`:

```bash
meho audit query --target rdc-vcenter --since 24h --json \
  | jq -r '.rows[].agent_session_id'
```

Filter out the NULLs and dedupe to get the distinct sessions that touched a target:

```bash
meho audit query --target rdc-vcenter --since 24h --json \
  | jq -r '.rows[].agent_session_id | select(. != null)' | sort -u
```

### From MCP client logs

The session id is the `Mcp-Session-Id` header the MCP client established on its first call to the backplane. It appears in the client's own logs (Claude Desktop, MCP Inspector, Cline, …) and on the broadcast feed.

### Why some rows have a NULL session id

`agent_session_id` is populated **only on MCP-originated rows** — the MCP audit writer ([`mcp/audit.py`](../../backend/src/meho_backplane/mcp/audit.py)) reads the bound `Mcp-Session-Id` and writes it onto the row. Plain HTTP requests through the chassis middleware ([`audit.py`](../../backend/src/meho_backplane/audit.py)) do **not** carry an MCP session, so those rows have `agent_session_id = NULL` by design. A session id you can replay therefore always traces back to an MCP-originated row. CLI verbs themselves dispatch over HTTP, so a `meho audit ...` invocation's own audit row has no session id.

## CLI usage

`meho audit replay` is the sixth verb under `meho audit ...`, alongside the five query verbs documented in [`audit-query.md`](./audit-query.md). It wraps the [G8.2-T4 REST route](../../backend/src/meho_backplane/api/v1/audit.py) `GET /api/v1/audit/sessions/{session_id}/replay`.

```bash
meho audit replay <session-id> [--json] [--max-depth N] [--backplane <url>]
```

`<session-id>` is required and must be a UUID — the CLI validates it client-side before any network call, so a typo returns `replay requires a valid UUID <session-id>; "<value>" is not a UUID` immediately rather than after a round-trip.

### The ASCII tree

The default output is an ASCII tree, one line per node, children indented under their parent with `├──` / `└──` connectors. Each line has the shape:

```
<occurred_at> <op_id> [<result_status>] (<duration_ms>ms)
```

A worked session — a VM migration that dispatched a power-off, which in turn waited on a task, plus a later list call:

```text
$ meho audit replay 11111111-1111-1111-1111-111111111111
├── 2026-05-13T10:00:00Z vsphere.vm.migrate [ok] (120.5ms)
│   └── 2026-05-13T10:00:01Z vsphere.vm.power_off [ok] (40ms)
│       └── 2026-05-13T10:00:02Z vsphere.task.wait [error] (-ms)
└── 2026-05-13T10:05:00Z vsphere.vm.list [ok] (8ms)
```

Roots are emitted in chronological order; children are ordered by `(occurred_at, id)`. A node with no recorded duration renders `(-ms)` so the column stays present and grep-friendly. An unknown, foreign, or empty session prints `no audit rows in this session` and exits cleanly (exit code 0) — a session that belongs to another tenant is indistinguishable from one that never existed (see [Tenant boundary](#tenant-boundary-and-the-10000-row-cap)).

### `--json`

`--json` emits the raw `AuditReplayResult` envelope verbatim — the server bytes, re-marshalled losslessly so every audit column on every node survives the round-trip. This is the shape compliance exports build against (see [`ReplayNode` shape](#replaynode-shape-and-the-v02next-compliance-export-contract)):

```bash
meho audit replay 11111111-1111-1111-1111-111111111111 --json \
  | jq '.row_count, (.root[].op_id)'
```

### `--max-depth`

`--max-depth N` (default 20) folds nodes deeper than level `N`. It is a **rendering-only** knob: the server caps on row count, not depth, so `--max-depth` controls only how much of the tree the CLI prints. Folded subtrees collapse into a single marker:

```text
$ meho audit replay 11111111-1111-1111-1111-111111111111 --max-depth 1
├── 2026-05-13T10:00:00Z vsphere.vm.migrate [ok] (120.5ms)
│   └── 2026-05-13T10:00:01Z vsphere.vm.power_off [ok] (40ms)
│       └── … 1 more node(s) below depth 1 (raise --max-depth to expand)
└── 2026-05-13T10:05:00Z vsphere.vm.list [ok] (8ms)
```

`--json` is unaffected by `--max-depth` — the JSON envelope is always the full server tree.

### Drilling into the flat rows with `meho audit query --session-id`

Replay's flat companion is the `--session-id` filter on `meho audit query`. Where replay gives you the tree, `--session-id` gives you the same session's rows as a paginated flat list — every query filter (op-class, result-status, time window) and the forward cursor apply on top:

```bash
meho audit query --session-id 11111111-1111-1111-1111-111111111111 --result-status error
```

`--session-id` is also a UUID, validated client-side the same way. It is the escape hatch for sessions too large to replay as a tree (next section).

### The 413 redirect for huge sessions

A session that exceeds the server's row cap cannot be replayed as a single tree — the route returns 413 from a count-first guard (it counts the anchor rows *before* building the tree, so a runaway session never materializes a multi-megabyte response just to be rejected). The CLI turns the 413 into an actionable redirect and exits non-zero:

```text
$ meho audit replay 11111111-1111-1111-1111-111111111111
session 11111111-1111-1111-1111-111111111111 has 12345 rows (cap 10000); use: meho audit query --session-id 11111111-1111-1111-1111-111111111111
```

The flat `--session-id` query paginates rows the over-cap tree can't render — that is the intended path for a pathological session.

## MCP agent surface

There are two MCP entry points for replay, split by role and scope. The split is deliberate — replaying *your own* session is an operator-level self-service; replaying *someone else's* session is a privileged forensic act.

| Tool | Role | Scope | Shape |
|---|---|---|---|
| `query_audit({agent_session_id, shape:"tree"})` | `operator` | **Self-session only** | `{root, session_id, tenant_id, row_count}` |
| `meho.audit.replay({session_id})` | `tenant_admin` | **Cross-session** (any session in the tenant) | `{root, session_id, tenant_id, row_count}` |

### `query_audit` with `shape:"tree"` (operator, self-session-only)

The narrow-waist [`query_audit`](./audit-query.md#mcp-agent-surface) tool grows a `shape` argument (enum `"flat"` (default) / `"tree"`). With `shape:"tree"` it reconstructs the agent's **own** session as a `ReplayNode` forest instead of a flat page:

```json
{
  "name": "query_audit",
  "arguments": {
    "agent_session_id": "<your own MCP session id>",
    "shape": "tree"
  }
}
```

The tree path is strictly self-session: `agent_session_id` must be present **and** equal to the caller's own bound MCP session id. Any other value — or absence — is rejected with JSON-RPC `-32602`. This is intentionally stricter than the flat path (which already returns other in-tenant principals' rows): an operator can replay their own trace, but replaying another session needs the admin tool.

### `meho.audit.replay` (tenant_admin, cross-session)

`meho.audit.replay` is the `tenant_admin` escalation for replaying **any** session in the tenant — the forensic-investigation tool:

```json
{
  "name": "meho.audit.replay",
  "arguments": {
    "session_id": "<the session to investigate>",
    "max_depth": 20
  }
}
```

`session_id` is required (UUID). `max_depth` is optional (integer 1–100, default 20) — a defensive cap on tree depth, where a node at the cap keeps its own row but its children are truncated.

### When to use which

- **Replaying your own run** (an agent debugging its own trace) → `query_audit` with `shape:"tree"`. Operator role, no escalation.
- **Investigating someone else's session** (an admin reviewing what an agent did after the fact) → `meho.audit.replay`. Requires `tenant_admin`.
- **A flat filtered list rather than the tree** → `query_audit` with the default `shape:"flat"` (or omit `shape`) and the `agent_session_id` filter — the MCP equivalent of `meho audit query --session-id`.

Both tree paths reject an over-cap session with `-32602` (`session_too_large`) — the MCP analogue of the CLI/REST 413, since the MCP transport has no streaming body for a partial response.

## `ReplayNode` shape and the v0.2.next compliance-export contract

`--json` (and both MCP tree tools) return an `AuditReplayResult` envelope:

```json
{
  "root": [ /* ReplayNode forest, chronological roots */ ],
  "session_id": "11111111-1111-1111-1111-111111111111",
  "tenant_id": "22222222-2222-2222-2222-222222222222",
  "row_count": 4
}
```

Each node in `root` (and recursively in every node's `children`) is a `ReplayNode` — defined in [`audit_query/schemas.py`](../../backend/src/meho_backplane/audit_query/schemas.py). A `ReplayNode` is **a full audit row plus its position in the session graph**: it subclasses `AuditEntry` (so it carries every audit column verbatim) and adds two structural fields:

- `depth` — distance from the session root (`0` for roots).
- `children` — the node's direct children, ordered by `(occurred_at, id)`. Self-referential, so the tree nests to arbitrary depth.

The per-node field set (inherited from `AuditEntry`, plus the two structural fields):

| Field | Meaning |
|---|---|
| `id` | Audit row id (UUID). |
| `ts` | When the operation occurred (`audit_log.occurred_at`). |
| `tenant_id` | The owning tenant. |
| `principal_sub` | The operator's JWT subject. |
| `principal_name` | Operator display name. `null` in v0.2 (not yet captured at write time). |
| `target_id` / `target_name` | The target the operation hit, if any. |
| `method` / `path` | The underlying HTTP method + path. |
| `status_code` | The HTTP status code. |
| `request_id` | The chassis `X-Request-Id` correlation id. |
| `duration_ms` | Operation latency (quoted decimal string, or `null`). |
| `payload` | The operation payload dict (op metadata for MCP/typed rows). |
| `op_id` / `op_class` / `result_status` | Computed at query time — the same trichotomy the broadcast classifier uses. |
| `parent_audit_id` | The lineage edge — the audit row this node hangs under. |
| `agent_session_id` | The MCP session correlation id (the replay key). |
| `broadcast_event_id` | `null` in v0.2 (the FK runs the other way: `BroadcastEvent.audit_id` points at the row). |
| `depth` | Distance from the session root. |
| `children` | Direct children (`list[ReplayNode]`). |

A worked `--json` tree (truncated to the rendering-relevant fields; every `AuditEntry` column above is present on each node):

```json
{
  "root": [
    {
      "ts": "2026-05-13T10:00:00Z",
      "op_id": "vsphere.vm.migrate",
      "result_status": "ok",
      "duration_ms": "120.5",
      "agent_session_id": "11111111-1111-1111-1111-111111111111",
      "parent_audit_id": null,
      "depth": 0,
      "children": [
        {
          "ts": "2026-05-13T10:00:01Z",
          "op_id": "vsphere.vm.power_off",
          "result_status": "ok",
          "duration_ms": "40",
          "depth": 1,
          "children": [
            {
              "ts": "2026-05-13T10:00:02Z",
              "op_id": "vsphere.task.wait",
              "result_status": "error",
              "duration_ms": null,
              "depth": 2,
              "children": []
            }
          ]
        }
      ]
    },
    {
      "ts": "2026-05-13T10:05:00Z",
      "op_id": "vsphere.vm.list",
      "result_status": "ok",
      "duration_ms": "8",
      "depth": 0,
      "children": []
    }
  ],
  "session_id": "11111111-1111-1111-1111-111111111111",
  "tenant_id": "22222222-2222-2222-2222-222222222222",
  "row_count": 4
}
```

> **`row_count` semantics.** On the **CLI / REST** envelope, `row_count` is the count of *anchor* rows in the session (rows whose `agent_session_id` equals `session_id`). It is the same number the 413 guard evaluates — NULL-session lineage children pulled into the tree are present but not counted. On the **MCP** tools, `row_count` is the total *assembled node count* (post depth-cap), so it reflects what the caller actually receives. Both are documented on the surfaces above; use `row_count` as a session-size signal, not a strict tree-node count across surfaces.

### Forward-compat contract

The `ReplayNode` shape is the **stable substrate for the v0.2.next compliance exports** (SOC 2 / ISO 27001 / HIPAA projections). A compliance export treats a replay node as exactly that — an audit row plus its position in the session graph — which is why `ReplayNode` carries every audit field verbatim rather than a reduced projection. The `--json` output is emitted losslessly through every surface (the CLI writes the server bytes verbatim; it does not round-trip through a render struct) precisely so this contract is not narrowed by a display surface. **Consumers can build against the `ReplayNode` / `AuditReplayResult` shape now** — the compliance-export *templates* themselves are v0.2.next, but the shape they project from is shipped and stable.

## Tenant boundary and the 10000-row cap

### Tenant boundary

Replay is tenant-scoped exactly like every other audit surface — there is **no cross-tenant escape hatch**:

- The route passes `operator.tenant_id` (lifted from the JWT) to the substrate as the mandatory keyword-only `tenant_id`; client input never sets the tenant.
- A `session_id` that belongs to another tenant — or one that does not exist — yields `root=[]` / `row_count=0`, **never a 404**. A foreign session is indistinguishable from an empty one, so existence never leaks across tenants (the same non-leakage posture `meho audit show` takes for cross-tenant audit ids).
- The `tenant_id` in the echoed envelope is a confirmation of the boundary the replay ran under, not a value the caller chose.

To replay another tenant's session you need an operator JWT for that tenant — there is no cross-tenant capability.

### The 10000-row cap

A single replay may reconstruct at most **10000 anchor rows**. A session above that returns 413 (CLI/REST) or `-32602` `session_too_large` (MCP) from a count-first guard that runs *before* the recursive tree build, so a runaway session never materializes an unbounded response. This is the same JSONFlux discipline that keeps any set-shaped result from blowing an agent's context window ([CLAUDE.md](../../CLAUDE.md) postulate 6).

The escape hatch is the flat drill-down: `meho audit query --session-id <id>` paginates the same session's rows under the standard forward cursor, so an over-cap session is still fully inspectable — just page by page instead of as one tree.

## Related

- [`audit-query.md`](./audit-query.md) — the sibling query runbook (the five flat-query verbs, filter semantics, cross-tenant boundary, audit-on-audit broadcast posture). Replay's flat companion `--session-id` is documented there too.
- [`docs/architecture/audit.md`](../architecture/audit.md) — canonical architecture reference for the audit module (substrate, surfaces, decision #3 alignment).
- [`docs/codebase/audit_query.md`](../codebase/audit_query.md) — engineering-facing internal doc covering schemas, cursor format, and control flow.
- [Initiative #377 (G8.2)](https://github.com/evoila/meho/issues/377) — the audit replay Initiative (substrate, REST, CLI, MCP, acceptance, this doc).
- [#805 (G11.4)](https://github.com/evoila/meho/issues/805) and the v0.2.next compliance-export Goal — forward-compat consumers of the `ReplayNode` shape.
- [CLAUDE.md](../../CLAUDE.md) postulate 5 (narrow-waist agent surface) and postulate 6 (JSONFlux / result-handle discipline) — the rules behind the `query_audit` `shape` argument and the 10000-row cap.
