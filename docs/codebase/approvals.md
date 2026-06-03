# Approvals (G11.2-T4 + T5; G11.7-T1 policy hardening)

**Initiative:** #803 — G11.2 Agent identity + RBAC + approval;
#1397 — G11.7 Approval-policy hardening
**Tasks:** #817 (T4 — durable approval queue) + #818 (T5 — operator
surfacing channel) + #1401 (G11.7-T1 — queue humans, self-approval
guard, resume-target fix, write-op secret redaction)

## Overview

The approval substrate parks `requires_approval` / `caution` / `dangerous`
dispatches durably and lets an operator approve or reject them
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

## Subject + actor attribution on the request row (#1481)

The `approval_request` row carries the RFC 8693 two-claim attribution
shape:

- `principal_sub` = the subject — `operator.sub`, the party on whose
  behalf the parked op runs (a human, or an autonomous agent acting as
  itself).
- `principal_act` = the actor — the agent principal currently wielding
  the subject's authority on a *delegated* run, or `NULL` when there is
  no separate actor.

`create_pending_request` sources `principal_act` from
`resolve_actor_sub()` (`backend/src/meho_backplane/auth/delegation.py`)
— the same `actor_sub` contextvar the synchronous audit log reads. A
human-initiated agent run binds the acting agent via `actor_delegation()`
(`backend/src/meho_backplane/agent/invocation.py`) for the lifetime of
the run task, so the parked row records `principal_sub=<human>` +
`principal_act=agent:<name>`, keeping the approval row's lineage
in lock-step with the audit log. A direct human call (no delegation
bound) and an autonomous agent run both resolve to `principal_act=NULL`.
(Before #1481 the field read a nonexistent `Operator.identity_act`
attribute and was always `NULL`.)

## MCP audit status for post-gate rejections (#1481)

A `tools/call` that a tool handler rejects *after* the dispatch gates
(name / arguments / unknown-tool / RBAC / schema) — e.g. the
approval-queue's self-approval, `approval_request_not_found`, or
`approval_unauthorized` re-raised as `McpInvalidParamsError` — returns
JSON-RPC `-32602` on the wire. The MCP envelope handler
(`backend/src/meho_backplane/mcp/handlers.py`) projects that whole class
onto audit `status_code=403` ("denied") instead of the init `500`
("error"), so the audit row and the live broadcast event
(`_classify_mcp_status`) classify a clean policy rejection as denied
rather than a fake server crash. The correction sits at the dispatch
boundary (`except McpInvalidParamsError` in both `handle_tools_call` and
`handle_resources_read`), so it covers every post-gate
`McpInvalidParamsError`, not just self-approval; explicit pre-gate
branches that already set `400`/`403`/`404` pass through untouched, and a
genuine handler fault still records `500`.

## G11.7-T1 policy hardening (#1401)

Phase C of the wrapper-retirement effort moves connector **writes**
behind this queue, and ops-team operators run those writes as **human**
(`USER`) principals. Four policy changes harden the gate for that — all
reusing the existing queue/approve/resume substrate (no new table,
model, or resume endpoint):

1. **Queue humans, don't hard-deny.** `policy_gate`
   (`backend/src/meho_backplane/operations/_validate.py`) returns
   `NEEDS_APPROVAL` — not `DENY` — when a non-agent principal hits a
   `requires_approval=True` op. The op is parked + resumable, not
   denied to the operator meant to run it. Non-`requires_approval` ops a
   human has always been able to run still auto-execute (the v0.2
   default-allow is preserved), so no existing human-runnable op
   regresses.
2. **Self-approval guard (requester != approver).** `approve_request`
   raises `SelfApprovalForbiddenError` (→ HTTP 403 `self_approval_forbidden`
   on REST, invalid-params on MCP) when `operator.sub ==
   request.principal_sub`, unless the audited break-glass switch
   `APPROVAL_ALLOW_SELF_APPROVAL=true`
   (`Settings.approval_allow_self_approval`, default `False` =
   fail-closed) is set. Both wire strings keep the
   `self_approval_forbidden` token as a prefix and append the
   exception message, so the `APPROVAL_ALLOW_SELF_APPROVAL` break-glass
   hint reaches the operator-facing REST `detail` and MCP error message
   rather than being dropped to a bare token (#1483). Even under break-glass the self-approval writes
   its decision audit row, so the use is forensically visible. **Reject
   is unguarded** — withdrawing one's own pending request is never a
   privilege escalation. The guard runs after the role check and before
   the params-hash check, so a self-approver gets the precise refusal
   reason.
3. **Resume target re-hydration.** The REST `…/approve` route persists
   only `target_id` on the row, so it re-loads the live `Target` by id
   (`targets.resolver.resolve_target_by_id`, tenant-scoped, `deleted_at
   IS NULL`) before the `_approved=True` re-dispatch. A write op whose
   handler reads `target.host` / `target.name` / `target.fqdn` now
   resolves the correct target instead of `None`. A target soft-deleted
   between request and approval resolves to `None`, so the re-dispatch
   fails closed (structured connector error) rather than reviving a
   tombstone. Tenant-wide ops (no original target) keep `target_id IS
   NULL` → `None`.
4. **Write-op secret redaction.** The broadcast/audit sensitivity
   classifier gained two op-id classes so the new Phase-C write ops'
   secrets never land in an audit row or broadcast frame —
   `credential_write` for request-param secrets (`vault.kv.put`,
   `vault.auth.userpass.write` / `update_password`, `k8s.secret.create`)
   and `credential_mint` extended for response secrets
   (`vault.token.create`, `vault.auth.approle.generate_secret_id`). Both
   collapse to aggregate-only and are **non-upgradeable** — no per-call
   or per-tenant override may surface the credential on the feed. See
   `docs/codebase/broadcast.md`.

The `_approved=True` re-dispatch gate-bypass (the resume authorization)
is unchanged in mechanism but now matters for humans too: with humans
routed to `NEEDS_APPROVAL`, re-running the gate on resume would re-queue
the call instead of executing it, so the bypass is what lets the
approved op run.

## `proposed_effect` builder hook (#1437)

`ApprovalRequest.proposed_effect` holds what the reviewer sees in the
queue. By default `create_pending_request`
(`backend/src/meho_backplane/operations/approval_queue.py`) stores an
identifier-only summary — `{op_id, connector_id, target_id}`. Some ops
can do better: they can compute a **side-effect-free preview** of what
the approved call would do, so the reviewer reads the diff in the queue
rather than only in the post-approval op result.

The per-op preview is opt-in via a builder registry in
`backend/src/meho_backplane/operations/_preview.py`:

- `register_preview_builder(op_id, builder)` registers an
  `async (PreviewContext) -> dict | None` callable for an op-id.
  `PreviewContext` carries the resolved connector instance + descriptor +
  operator + target + params.
- At the park point, `dispatcher._handle_needs_approval` resolves the
  connector instance (same path the execute branch uses) and calls
  `build_proposed_effect`. The result — wrapped as
  `{op_class, preview}` — is passed to `create_pending_request` as
  `proposed_effect`; `None` falls back to the identifier-only default.

Three invariants make the hook safe to wire on the park path:

1. **Opt-in / no regression.** An op with no registered builder yields
   `None` and parks exactly as before.
2. **Fail-soft.** A builder that raises (a dry-run that hits the API and
   errors) degrades to `None`; the park — the safety-relevant action —
   always proceeds. Connector-resolution faults degrade the same way.
3. **Redaction-safe.** `build_proposed_effect` classifies the op via
   `classify_op` (the same single-sourced sensitivity classification used
   for broadcast/audit redaction, #1401) and **suppresses** the preview
   for any credential class (`credential_read` / `credential_mint` /
   `credential_write`) before the builder even runs — a durable row
   never carries secret material. Builders are themselves expected to
   return identity-only summaries.

The only builder wired in #1437 is **`k8s.apply`**: it re-invokes the
`k8s_apply` handler with `dry_run="server"` forced on (the API's
`?dryRun=All`), so nothing persists and the per-document summary
(resource identity + `resourceVersion` + `uid`) is the diff-preview the
reviewer reads. The ArgoCD write ops register their own builders the same
way (#1452): `argocd.app.set` / `argocd.appproject.update` populate
`{before_spec, after_spec}` and `argocd.app.delete` populates
`{cascade_resources}` from read-only GETs against the live
Application / AppProject — wired in
`connectors/argocd/ops_write_preview.py`. Further connectors register
their own builders as needed.

## Transports

### REST (`backend/src/meho_backplane/api/v1/approvals.py`)

| Verb | Path | Role | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/approvals` | operator | List, filtered by `status` (default: `pending`). |
| `GET` | `/api/v1/approvals/{id}` | operator | **Inspect one request** (T5). 404 on cross-tenant. |
| `POST` | `/api/v1/approvals/{id}/approve` | operator | Approve. Requires `params` (hash-verified). Re-hydrates the target by id, then re-dispatches with `dispatch(..., _approved=True)`. 403 `self_approval_forbidden` when the approver is the requester and break-glass is off (G11.7-T1). |
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
