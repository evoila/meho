# Approval Surfacing Channel (G11.2-T5)

**Initiative:** #803 — G11.2 Agent identity + RBAC + approval  
**Task:** #818 — T5 approval surfacing channel

## Overview

The approval surfacing channel is the operator-facing layer for **pending
approval requests**: the mechanism by which a paused agent run communicates
"I need a human decision before I can proceed."

When the policy gate (G11.2-T3, `policy_gate` in
`operations/_validate.py`) issues a `needs-approval` verdict on an operation,
the T4 component (#817) writes a durable `approval_request` row and
pauses the agent run (`agent_run.status = awaiting_approval`). T5 (this
document) is the delivery channel: how the operator learns of pending
requests, inspects the proposed effect, and posts approve / reject decisions.

## Architecture

```
policy_gate (T3)
    │  needs-approval verdict
    ▼
T4 approval queue (pending row + awaiting_approval run status)
    │  approval_request row visible
    ▼
T5 surfacing layer (REST + MCP + CLI + broadcast)
    │  operator decision
    ▼
T4 resume API (run continues or aborts)
```

T5 provides a **read-only + decision surface** over T4's table — it does
not own the queue semantics or the resume mechanics.

## Key Types

### `ApprovalStatus` (ORM enum, `db/models.py`)

| Value | Meaning |
|---|---|
| `pending` | Request created, awaiting decision. Initial state. |
| `approved` | Operator approved; T4 resume path called. |
| `rejected` | Operator rejected; T4 abort path called. |
| `expired` | Not acted on before `expires_at`; expiry sweep closed it. |

### `ApprovalRequest` (ORM model, `db/models.py`)

One row per approval gate pause. Key fields:

- `id` — UUID primary key; used as the `elicitation_url` path segment.
- `tenant_id` — real FK to `tenant.id` (tenant-scoped, no cross-tenant leaks).
- `agent_run_id` — soft-FK to `agent_run.id` (NULL for direct REST-created rows).
- `principal_sub` / `principal_act` — RFC 8693 delegation pair (who triggered it).
- `connector_id` / `op_id` / `target_id` / `params_hash` — the proposed operation.
- `proposed_effect` — JSONB; human-readable operation preview for the decision UI.
- `status` — closed enum; CHECK-constrained.
- `reviewed_by` / `decided_at` — stamped on decision.
- `expires_at` — optional expiry deadline.
- `request_audit_id` / `decision_audit_id` — soft-FKs to `audit_log.id`
  (the two synchronous audit rows the T4 approval invariant requires).

### `ApprovalRequestService` (`approvals/service.py`)

Stateless, method-scoped service. The single code path REST routes, MCP
tools, and CLI verbs dispatch through:

- `list_()` — paginated list, optional status filter, newest-first.
- `get()` — single row; raises `ApprovalNotFoundError` for absent/cross-tenant.
- `approve()` — flips to `approved`, stamps reviewer, calls T4 resume stub,
  publishes `approval.approved` broadcast event.
- `reject()` — flips to `rejected`, stamps reviewer, calls T4 resume stub,
  publishes `approval.rejected` broadcast event.

## REST Routes (`api/v1/approvals.py`)

| Verb | Path | Role | Description |
|---|---|---|---|
| `GET` | `/api/v1/approvals` | operator | List (with `?status=pending` filter) |
| `GET` | `/api/v1/approvals/{id}` | operator | Show detail + `elicitation_url` |
| `POST` | `/api/v1/approvals/{id}/approve` | operator | Approve |
| `POST` | `/api/v1/approvals/{id}/reject` | operator | Reject |
| `POST` | `/api/v1/approvals/{id}/decide` | operator | MCP elicitation URL-mode endpoint |

## MCP Tools (`mcp/tools/approvals.py`)

| Tool | Op class | Description |
|---|---|---|
| `meho.approvals.list` | read | List pending requests |
| `meho.approvals.get` | read | Inspect one request |
| `meho.approvals.approve` | write | Approve |
| `meho.approvals.reject` | write | Reject |

All tools mirror the REST routes and drive the same
`ApprovalRequestService`.

## CLI Verbs (`cli/internal/cmd/approvals/`)

```
meho approvals list [--status pending] [--limit N] [--offset N] [--json]
meho approvals show <id> [--json]
meho approvals approve <id> [--reason TEXT] [--json]
meho approvals reject <id> [--reason TEXT] [--json]
```

Role: operator for all verbs. Tenant scoping enforced server-side.

## MCP Elicitation URL-Mode (Forward Format)

Per the MCP 2025-11-25 specification
([workos.com/blog/mcp-elicitation](https://workos.com/blog/mcp-elicitation)),
when an in-loop agent encounters a `needs-approval` pause it can surface
the approval request to the MCP host application using
**elicitation URL mode**:

```json
{
  "method": "elicitation/create",
  "params": {
    "message": "Approval required: vmware-rest-9.0 / vcenter.vm.delete",
    "requestedSchema": {
      "type": "object",
      "properties": {
        "decision": {
          "type": "string",
          "enum": ["approve", "reject"],
          "description": "Your decision on the proposed operation."
        },
        "reason": {
          "type": "string",
          "description": "Optional rationale."
        }
      },
      "required": ["decision"]
    }
  }
}
```

The **elicitation URL** is `<backplane_base>/api/v1/approvals/{id}/decide`.
It is exposed on every `GET /api/v1/approvals/{id}` response in the
`elicitation_url` field, and returned by the `meho.approvals.get` MCP
tool. An MCP host that supports elicitation URL mode can open the
operator's browser / decision UI to this address, or the operator can
`curl`/`meho approvals approve` directly.

Why URL mode (not form mode): the decision is simple (approve/reject +
optional reason), the payload is structured, and the URL-mode approach
lets MCP clients with a browser-capable host skip building a custom form.

## Broadcast Notification

On approve or reject, `ApprovalRequestService` publishes a fail-open
broadcast event (`approval.approved` / `approval.rejected`) to the
tenant's Valkey stream so an operator's `broadcast_watch` session or the
G10 wall monitor learns of decisions without polling. The event payload
includes `approval_request_id`, `decision`, `connector_id`, and
`approval_op_id`.

Pending requests are also announced through broadcast when T4 creates them
(T4's responsibility, not T5's). The R4 local-Claude pattern described in
the initiative uses `meho approvals list --status pending` as the polling
channel.

## Dependencies

- **T4 (#817):** The `approval_request` table and the resume API. This doc
  was written while T4 is in-flight; the `_resume_run` stub in
  `approvals/service.py` is clearly marked for wiring once T4's
  `AgentRunResumeService` is importable.
- **G11.2-T3 (#820):** The `policy_gate` verdict resolution that issues
  `needs-approval` — T5 operates on rows T3 routes to T4 to create.

## Known Issues

- `_resume_run` in `approvals/service.py` is a stub (logs only). Full
  integration with T4's resume path activates when #817 merges.
- `decision_audit_id` is pre-allocated on the REST path but the `audit_log`
  row is written by the audit middleware, not by the service. The service
  passes the pre-allocated UUID so the two stay in sync via the
  `preallocated_audit_id` contextvar. The MCP and CLI paths do not
  pre-allocate; their audit rows land under the middleware's default UUID.

## References

- MCP elicitation URL mode spec: https://workos.com/blog/mcp-elicitation
- T4 durable approval queue: issue #817
- T3 policy gate / verdict resolution: issue #820
- `operations/_validate.py` — the `policy_gate` seam T5 operates downstream of
- `db/models.py` — `ApprovalRequest`, `ApprovalStatus`
- `alembic/versions/0021_create_approval_request.py` — DB migration
