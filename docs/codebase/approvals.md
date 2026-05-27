# Approvals (G11.2-T4 + T5)

**Initiative:** #803 — G11.2 Agent identity + RBAC + approval
**Tasks:** #817 (T4 — durable approval queue) + #818 (T5 — operator surfacing channel)

## Overview

The approval substrate parks `requires_approval` / `caution` / `dangerous`
agent dispatches durably and lets an operator approve or reject them
across **REST, MCP, and CLI** transports. Two layers, one source of
truth:

- **T4 owns the queue lifecycle** (`backend/src/meho_backplane/operations/approval_queue.py`):
  `create_pending_request`, `approve_request`, `reject_request`,
  `expire_stale_requests`. Every mutation writes a synchronous audit
  row in the same transaction (the "request" row on create; the
  "decision" row on approve/reject/expire).
- **T5 wires the operator surfaces on top:** `GET /api/v1/approvals/{id}`
  (inspect), `meho.approvals.{list,get,approve,reject}` MCP tools, and
  `meho approvals {list,show,approve,reject}` CLI verbs. T5 also adds
  the broadcast notifications: each `create_pending_request` /
  `approve_request` / `reject_request` publishes an
  `approval.{pending,approved,rejected}` event via
  `broadcast.publisher.publish_event` so a `broadcast_watch` operator
  session learns of pending requests without polling.

The durable state is one `approval_request` row (table created by
`backend/alembic/versions/0023_create_approval_request.py`, T4 #817).

## Transports

### REST (`backend/src/meho_backplane/api/v1/approvals.py`)

| Verb | Path | Role | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/approvals` | operator | List, filtered by `status` (default: `pending`). |
| `GET` | `/api/v1/approvals/{id}` | operator | **Inspect one request** (T5). 404 on cross-tenant. |
| `POST` | `/api/v1/approvals/{id}/approve` | operator | Approve. Requires `params` (hash-verified) → re-dispatches the approved op with `dispatch(..., _approved=True)`. |
| `POST` | `/api/v1/approvals/{id}/reject` | operator | Reject. The op never executes. |

### MCP (`backend/src/meho_backplane/mcp/tools/approvals.py`)

Four `meho.approvals.*` tools (all `TenantRole.OPERATOR`-gated):

- `meho.approvals.list` — `status` filter (`pending` default), `limit`/`offset`.
- `meho.approvals.get` — full detail by id.
- `meho.approvals.approve` — operator-decision path (status flip + audit +
  `approval.approved` broadcast; **no `params` required** —
  `approve_request` skips the hash check when called without params, because
  the operator decision path does not have the agent's params). The
  agent's REST path retains the hash check and is what re-dispatches.
- `meho.approvals.reject` — same shape; optional `reason`.

RBAC is enforced at two layers: the MCP registry filter hides write
tools from non-admins in `tools/list`, and the dispatcher re-checks
`required_role` at `tools/call`.

### CLI (`cli/internal/cmd/approvals/`)

`meho approvals list / show <id> / approve <id> / reject <id> [--reason]`
verbs that hit the REST surface via the generated typed client.

## Broadcast events (T5)

`approval_queue.publish_approval_event` publishes one event per
lifecycle step, fail-open (a broadcast outage never blocks the durable
decision). Each call site lifts the decision row's
`request._audit_id` and publishes **after** the transaction commits, so
a phantom event cannot outlive a failed transaction:

| Stage | `op_id` | When | Call site |
|---|---|---|---|
| Create | `approval.pending` | `create_pending_request` (dispatcher parks a `needs-approval` verdict). | `operations/dispatcher.py::_handle_needs_approval` |
| Approve | `approval.approved` | `approve_request` succeeds. | `api/v1/approvals.py` (REST), `mcp/tools/approvals.py` (MCP) |
| Reject | `approval.rejected` | `reject_request` succeeds. | `api/v1/approvals.py` (REST), `mcp/tools/approvals.py` (MCP) |
| Expire | `approval.expired` | `expire_stale_requests` (sweeper / CLI sweep) per expired row. | sweeper caller (commits then publishes per returned row's `_audit_id`) |

Payload: `approval_request_id`, `decision`, `connector_id`,
`approval_op_id`. The event's `audit_id` field is the decision row's
primary key (FK to `audit_log.id`); subscribers that want the full row
query `audit_log` by this id.

## Audit rows

Two rows per request lifecycle (the synchronous-audit invariant):

| Row | `path` | `status_code` | Written by |
|---|---|---|---|
| Request | `approval.request` | `202` | `create_pending_request` |
| Decision | `approval.decision` | `200` (approved) / `403` (rejected) / `410` (expired) | `approve_request` / `reject_request` / `expire_stale_requests` |

## MCP elicitation URL-mode (forward-looking)

When an in-loop agent hits a `needs-approval` verdict, the agent
runtime can use the row's `id` (returned from `meho.approvals.get`) to
construct an elicitation URL of the form
`meho://approvals/{request_id}/decide`. MCP-2025-11-25 hosts that
support elicitation URL-mode can open this URL in the operator's
decision UI; until that lands, the operator approves/rejects via the
explicit `meho.approvals.{approve,reject}` tools.

## Agent-runtime resume on `approval.{approved,rejected}` (T9 #1117)

The operator/agent split: operator paths capture the decision durably and
publish a broadcast event; the agent runtime subscribes and resumes (or
surfaces the rejection) from its own side. Each path:

| Path | Decision capture | Re-dispatch |
|---|---|---|
| REST `/approve` with `params` | inline | inline — the human operator's call re-dispatches with `_approved=True` (the human-driven express lane). |
| REST `/decide`, MCP, CLI | durable row + broadcast | **agent runtime**: the wrapped `call_operation` in `meho_backplane/agent/toolset.py` + `meho_backplane/agent/run.py` blocks on `meho_backplane.agent.approval_wait.wait_for_approval_decision` and re-invokes the dispatcher via `call_operation_with_approval` on approval, or surfaces the rejection to the model. |

See [agent-runtime.md § Awaiting-approval resume](agent-runtime.md#awaiting-approval-resume-t9-1117)
for the substrate's full shape, including the wait timeout
(`Settings.agent_approval_wait_timeout_seconds`, default 30 min), the
fail-open semantics on broadcast outage, and the security tradeoff that
keeps `params` off the approval row.

## References

- `backend/src/meho_backplane/operations/approval_queue.py` — queue lifecycle (T4) + read helpers + broadcast (T5).
- `backend/src/meho_backplane/api/v1/approvals.py` — REST routes.
- `backend/src/meho_backplane/mcp/tools/approvals.py` — MCP tools.
- `cli/internal/cmd/approvals/` — CLI verbs.
- `backend/alembic/versions/0023_create_approval_request.py` — schema.
- `backend/src/meho_backplane/agent/approval_wait.py` — agent-runtime resume substrate (T9 #1117): broadcast subscription + re-dispatch on approval.
- `backend/src/meho_backplane/operations/meta_tools.py` — `call_operation_with_approval` (the gate-bypass re-dispatch entry point).
