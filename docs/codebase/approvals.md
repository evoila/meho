# Approvals (G11.2-T4 + T5; G11.7-T1 policy hardening; G0.20-T3 direct-op execute)

**Initiative:** #803 — G11.2 Agent identity + RBAC + approval;
#1397 — G11.7 Approval-policy hardening; #1500 — G0.20 v0.10.1 dogfood
hardening
**Tasks:** #817 (T4 — durable approval queue) + #818 (T5 — operator
surfacing channel) + #1401 (G11.7-T1 — queue humans, self-approval
guard, resume-target fix, write-op secret redaction) + #1503 (G0.20-T3
— execute a parked direct operator op on approve via every surface)

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

## G0.20-T3 — execute a parked direct op on approve via every surface (#1503)

Before #1503 the **only** execute-after-approve path for a parked
**direct** operator op (an operator calling a `requires_approval` op
directly, not via an agent run) was REST `POST .../approve` carrying the
original `params` in-band. Approving the same parked write via `/decide`
or the MCP/CLI by-id approve surface committed the decision but never
re-dispatched — and simply re-calling the op re-parked it. So an
operator who approved a colleague's parked direct write through the
activity feed / `/decide` / MCP saw it marked approved while the write
never landed.

Two changes close that gap, scoped to the **direct**-op path:

1. **Store the params on the row.** `approval_request.params` (nullable
   JSON, migration
   `backend/alembic/versions/0036_add_approval_request_params.py`) holds
   the original dispatch params, written by `create_pending_request` at
   park time. The row already held `connector_id` / `op_id` /
   `target_id`; adding `params` makes it a complete re-dispatch
   primitive, so a surface that holds only the request id can still
   re-execute the exact original call. The column is **internal
   re-dispatch input only** — it is never serialised onto a read view
   (`_view`, the MCP `_row_to_dict`) or a broadcast frame; the redacted
   `proposed_effect` and the swap-defence `params_hash` remain the
   reviewer-facing fields. (This reverses, for the direct-op path only,
   the "keep params off the row" tradeoff #1117 made for the agent-run
   case — see [agent-runtime.md § Awaiting-approval resume](agent-runtime.md#awaiting-approval-resume-t9-1117).)

2. **Re-dispatch from the approve decision on every surface.** The
   single execute-after-approve entry point is
   `approval_queue.resume_dispatch_after_approval` — it re-hydrates the
   stored target by id (G11.7-T1 fail-closed semantics preserved) and
   re-dispatches with `dispatch(..., _approved=True)`, falling back to
   the stored `request.params` when the surface supplies none. REST
   `/approve` (caller params, hash-verified), REST `/decide`, and the
   MCP `meho.approvals.approve` tool all route through it. `/decide` and
   the MCP tool return the dispatch outcome (`dispatch_*` fields /
   `dispatch` block) alongside the decision.

**The `run_id` gate (no double-execution / agent-run regression
guard).** `/decide` and the MCP approve tool re-dispatch **only when
`run_id IS NULL`** — i.e. a direct operator op. An **agent-run** request
(`run_id` set) is still resumed in-process by the agent runtime off the
`approval.approved` broadcast (the must-not-regress path); re-dispatching
it from `/decide`/MCP too would execute the op twice. So the agent-run
resume path is untouched: it keeps its in-memory params, ignores the new
column, and remains the sole re-dispatcher for `run_id`-bearing requests.

**Double-execution prevention (terminal-state guard).**
`approve_request` flips the row to `approved` (terminal) and commits
*before* the re-dispatch runs. A concurrent second `/decide`/approve on
the same row hits the already-decided guard (409 / `not_pending`), so the
stored params drive exactly one execution.

**Pre-0036 rows.** A row parked before migration 0036 has `params IS
NULL`. Approving it via `/decide`/MCP (no in-band params) finds nothing
to re-dispatch, so `resume_dispatch_after_approval` **fails closed** with
a structured `denied` result naming the gap — the operator resumes it via
REST `/approve` + params, exactly as before 0036.

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
`connectors/argocd/ops_write_preview.py`. The `secret.move` broker op
populates a ref-only `{action, source, sink}` summary with no store I/O
(#1580, `connectors/secret/move_preview.py`). The 8 vmware write
composites register builders in
`connectors/vmware_rest/composites/_write_preview.py` (#1608): the
fan-out composites (`vm.power.bulk`, `host.evacuate`,
`host.detach_from_vds`, `cluster.patch`) resolve their entity set via
the same read-only listing helpers the handlers use — `{..., resolved,
total_resolved}` with the list capped — and the single-entity
composites echo their params; see
[`connectors-vmware-rest.md`](connectors-vmware-rest.md) "Park-time
approval previews". Further connectors register their own builders as
needed.

## Permission preflight hook (#1504)

The `proposed_effect` *preview* above is suppressed for credential-class
ops — but a credential write (`vault.kv.put`) is precisely the op most
likely to be **denied** by Vault *after* a human spends a four-eyes
review approving it (the `meho-mcp` role grants `read` but no
`create`/`update` on the write path). To surface that at park time
without violating the redaction rule, `_preview.py` adds a **second**,
parallel registry: `register_permission_preflight(op_id, preflight)` +
`build_permission_preflight`.

A permission preflight is distinct from a preview:

- A **preview** says *what the write would do* (request/response shape) —
  so it is suppressed for credential-class ops.
- A **permission preflight** says *whether the dispatching identity is
  authorized to perform the write* — it returns only authorization
  metadata (Vault capability names, never a secret value), so it runs
  for **every** registered op regardless of sensitivity class. The
  credential-class suppression that gates previews does **not** apply.

At the park point, `dispatcher._build_proposed_effect` runs **both**
hooks and merges them: the preview (or the identifier-only default when
there is no preview) is the base, and the preflight result is attached
under `proposed_effect["permission_preflight"]`. Both are opt-in and
fail-soft — a preflight that raises degrades to no banner; the park
always proceeds.

The KV-v2 write ops (`vault.kv.put` / `vault.kv.patch` /
`vault.kv.delete`) register a preflight that probes
`POST sys/capabilities-self` on the target `<mount>/data/<path>` and
returns `{check, path, required, granted, will_be_denied, principal_sub}`
— see
[`docs/codebase/connectors-vault.md`](connectors-vault.md) "Park-time
write-capability preflight" and
[`docs/cross-repo/connector-vault-policy.md`](../cross-repo/connector-vault-policy.md)
§6 for the write policy + verify command. **Whose token:** the preflight
runs under the dispatching operator's token, but the approved re-dispatch
runs under the **reviewing** operator's token
(`resume_dispatch_after_approval`); both share the `meho-mcp` role policy,
so the preflight is the right early signal while the reviewer must carry
the same grant.

## Transports

### REST (`backend/src/meho_backplane/api/v1/approvals.py`)

| Verb | Path | Role | Purpose |
|---|---|---|---|
| `GET` | `/api/v1/approvals` | operator | List, filtered by `status` (default: `pending`). |
| `GET` | `/api/v1/approvals/{id}` | operator | **Inspect one request** (T5). 404 on cross-tenant. |
| `POST` | `/api/v1/approvals/{id}/approve` | operator | Approve. Requires `params` (hash-verified). Re-hydrates the target by id, then re-dispatches with `dispatch(..., _approved=True)`. 403 `self_approval_forbidden` when the approver is the requester and break-glass is off (G11.7-T1). |
| `POST` | `/api/v1/approvals/{id}/decide` | operator | Decide (`approved` / `rejected`) by id alone. For an approved **direct** op (`run_id IS NULL`) re-dispatches with the **stored** params (#1503) and returns the outcome in `dispatch_*`; for an agent-run request records the decision only (the agent runtime resumes). |
| `POST` | `/api/v1/approvals/{id}/reject` | operator | Reject. The op never executes. |

### MCP (`backend/src/meho_backplane/mcp/tools/approvals.py`)

Four `meho.approvals.*` tools (all `TenantRole.OPERATOR`-gated):

- `meho.approvals.list` — `status` filter (`pending` default), `limit`/`offset`.
- `meho.approvals.get` — full detail by id.
- `meho.approvals.approve` — operator-decision path (status flip + audit +
  `approval.approved` broadcast; **no `params` required** —
  `approve_request` skips the hash check when called without params). For
  an approved **direct** op (`run_id IS NULL`) it then re-dispatches using
  the stored params (#1503) and returns the outcome under `dispatch`; for
  an agent-run request the in-process agent runtime resumes the op off the
  broadcast, so the tool only records the decision.
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

The operator/agent split, keyed on whether the parked request belongs to
an **agent run** (`run_id` set) or a **direct operator op** (`run_id IS
NULL`):

| Path | Decision capture | Re-dispatch (agent run) | Re-dispatch (direct op) |
|---|---|---|---|
| REST `/approve` with `params` | inline | inline (`_approved=True`, the human-driven express lane) | inline (caller params, hash-verified) |
| REST `/decide`, MCP, CLI | durable row + broadcast | **agent runtime**: the wrapped `call_operation` in `meho_backplane/agent/toolset.py` + `meho_backplane/agent/run.py` blocks on `meho_backplane.agent.approval_wait.wait_for_approval_decision` and re-invokes the dispatcher via `call_operation_with_approval` on approval, or surfaces the rejection to the model. | **inline** (#1503): the decision drives `resume_dispatch_after_approval` with the **stored** params (`run_id`-gated so this never fires for an agent run). |

For an agent-run request the broadcast-driven runtime resume is the only
re-dispatch path; `/decide`/MCP record the decision only. For a direct
operator op (#1503) `/decide`/MCP re-dispatch inline with the stored
params. See [agent-runtime.md § Awaiting-approval resume](agent-runtime.md#awaiting-approval-resume-t9-1117)
for the agent-run substrate's full shape, including the wait timeout
(`Settings.agent_approval_wait_timeout_seconds`, default 30 min) and the
fail-open semantics on broadcast outage.

## References

- `backend/src/meho_backplane/operations/approval_queue.py` — queue lifecycle (T4) + read helpers + broadcast (T5).
- `backend/src/meho_backplane/api/v1/approvals.py` — REST routes.
- `backend/src/meho_backplane/mcp/tools/approvals.py` — MCP tools.
- `cli/internal/cmd/approvals/` — CLI verbs.
- `backend/alembic/versions/0023_create_approval_request.py` — schema.
- `backend/alembic/versions/0036_add_approval_request_params.py` — `params` column for direct-op approve re-dispatch (#1503).
- `backend/src/meho_backplane/operations/approval_queue.py::resume_dispatch_after_approval` — the single execute-after-approve entry point shared by every operator surface (#1503).
- `backend/src/meho_backplane/agent/approval_wait.py` — agent-runtime resume substrate (T9 #1117): broadcast subscription + re-dispatch on approval.
- `backend/src/meho_backplane/operations/meta_tools.py` — `call_operation_with_approval` (the gate-bypass re-dispatch entry point).
