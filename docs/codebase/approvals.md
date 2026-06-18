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
   request.principal_sub`, unless the audited **emergency break-glass**
   switch `APPROVAL_ALLOW_SELF_APPROVAL=true`
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

   `APPROVAL_ALLOW_SELF_APPROVAL` is an **emergency** escape, not the
   single-operator answer: enabling it posture-wide re-opens, for every
   op, the single-account request+grant hole this guard closes.
   Single-operator tenants should park their four-eyes writes under an
   **agent-requester** (a distinct `principal_kind=agent` `sub`) instead
   — see [Single-operator tenants: use an agent-requester, not
   break-glass](#single-operator-tenants-use-an-agent-requester-not-break-glass-1738)
   below.
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

## Single-operator tenants: use an agent-requester, not break-glass (#1738)

The self-approval guard enforces requester != approver on the stable
`sub` claim. On a tenant with **one** human operator this looks like a
deadlock: that operator parks a `requires_approval` write as a human
(`USER`) principal, then is the only identity available to approve it,
so `approve_request` raises `self_approval_forbidden`. The break-glass
switch `APPROVAL_ALLOW_SELF_APPROVAL` exists for that case — but it is an
**emergency** escape, not the everyday answer. Flipping it posture-wide
re-opens, for every op, the single-account *request + grant* hole #1401
was created to close.

The everyday answer is an **agent-requester**: park the write under an
**agent principal** (`principal_kind=agent`) rather than under the human.
An agent principal is a first-class identity — a Keycloak
`client_credentials` client `agent:<name>` with its own stable `sub`,
minted by `meho agent-principal register` (see
[`keycloak-agent-client.md`](../cross-repo/keycloak-agent-client.md)).
When the agent parks the request, `create_pending_request` sets
`principal_sub=<agent-sub>` (it always reads `operator.sub` —
`approval_queue.py:338`). The human operator who later approves carries a
**distinct** `sub`, so `_check_self_approval`
(`approval_queue.py:816-831`) takes its `operator.sub != request.principal_sub`
early-return and the approval clears — **with no break-glass flag and no
new tunable**, and with the full subject/actor lineage intact (the
decision audit row records the human approver; the request row records
the agent requester).

### How the agent becomes the requester

The agent must be the *subject* of the parked request, not merely an
actor on a human's behalf. That distinction is the RFC 8693 token-
exchange shape MEHO encodes (see [Subject + actor attribution on the
request row](#subject--actor-attribution-on-the-request-row-1481) above),
and it is the difference between the two agent-run entry points in
`backend/src/meho_backplane/agent/invocation.py`:

- **Autonomous / scheduled run** — `AgentInvoker.run_scheduled`
  (`invocation.py:951`) authenticates the agent under its **own**
  `client_credentials` grant and deliberately binds **no**
  `actor_delegation` (`invocation.py:1117-1118`). The agent is the sole
  subject: `operator.sub` resolves to the agent, so the parked row gets
  `principal_sub=<agent-sub>` and `principal_act=NULL`. **This is the
  agent-requester path.** A human operator with a different `sub`
  approves the row and clears the gate.
- **Human-initiated delegated run** — `AgentInvoker.run`
  (`invocation.py:795`) is launched by a human, so it binds the acting
  agent as the RFC 8693 **actor** via `with actor_delegation(...)`
  (`invocation.py:841`). Per RFC 8693 §4.1 the human stays the *subject*
  and the agent is only the *actor*: the parked row gets
  `principal_sub=<human>` + `principal_act=<agent-sub>`. The requester is
  still the human, so that same human **still cannot** approve it. Routing
  a single-operator write through a human-triggered agent run does **not**
  break the deadlock — only the autonomous/scheduled run does.

### Which identity shape clears the gate

| Identity shape | How it parks | `principal_sub` | `principal_act` | Approve by the human op |
|---|---|---|---|---|
| Direct human op | human dispatches a `requires_approval` write as `USER` | the human's `sub` | `NULL` | **Blocked** — `requester == approver` (needs break-glass) |
| Human-initiated delegated agent run (`run`) | human launches an agent; agent binds as RFC 8693 *actor* | the human's `sub` | the agent's `sub` | **Blocked** — RFC 8693 subject is the human; `requester == approver` |
| Autonomous / scheduled agent run (`run_scheduled`) | agent authenticates as itself; **no** delegation bound | the agent's `sub` | `NULL` | **Clears** — distinct `sub`, no break-glass, full audit lineage |

### End-to-end recipe (single-operator four-eyes write)

1. **Register the agent principal** —
   [`keycloak-agent-client.md`](../cross-repo/keycloak-agent-client.md)
   (`meho agent-principal register <name>` → a `agent:<name>`
   `client_credentials` client carrying `principal_kind=agent`).
2. **Author an agent definition whose `identity_ref` is that client** —
   [`agent-definition.md`](agent-definition.md); `identity_ref` is
   validated against `agent_principal.keycloak_client_id`, and
   `run_scheduled` enforces `identity_ref == agent_client_id` so the run
   provably parks under the named agent.
3. **Wire the write as a scheduled trigger under that definition** —
   [`scheduler.md`](scheduler.md); a cron or one-off
   `scheduled_trigger` fires `run_scheduled`, so the
   `requires_approval` op parks with `principal_sub=<agent-sub>`.
4. **The human operator approves the parked row** — via any surface
   (REST `/decide`, `meho approvals approve <id>`, MCP). Their `sub`
   differs from the agent's, so the gate clears and the approved op
   re-dispatches.

### Troubleshooting — `self_approval_forbidden` on a single-operator tenant

| Symptom | Cause | Fix |
|---|---|---|
| `approve` returns `self_approval_forbidden` and you are the tenant's only operator | The write was parked under **your own** `sub` — a direct human op, or a human-initiated delegated agent run (where RFC 8693 keeps you the subject). | Re-park the write under an **agent-requester**: wire it as a scheduled trigger under an agent definition (the four-step recipe above) so `run_scheduled` parks it with `principal_sub=<agent-sub>`. Then approve as yourself — your distinct `sub` clears the gate. Reserve `APPROVAL_ALLOW_SELF_APPROVAL` for genuine emergencies; it re-opens the #1401 single-account hole posture-wide. |

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
  `{op_class, preview}` — becomes the base envelope; when the builder
  declines (returns `None`) the dispatcher's `_build_proposed_effect`
  seam substitutes the identifier-only base. Onto whichever base it has,
  the seam stamps the catalog `descriptor.safety_level` (#1855) before
  handing the envelope to `create_pending_request` as `proposed_effect`
  (see "Catalog `safety_level` on every envelope" below). `_build_proposed_effect`
  returns `None` — and the caller stores its own bare identifier-only
  default — only when connector resolution / hook execution itself
  raises.

Three invariants make the hook safe to wire on the park path:

1. **Opt-in / no regression for the preview.** An op with no registered
   builder gets no `{op_class, preview}` envelope — the per-op
   `build_proposed_effect` returns `None`. As of #1855 the durable row
   is no longer byte-identical to the pre-#1855 identifier-only default,
   though: the dispatcher seam layers `safety_level` on top of the
   identifier base (below), so the parked row always names its severity.
   The *preview* itself is still strictly opt-in.
2. **Fail-soft, never silent.** A builder that raises (a dry-run that
   hits the API and errors, a preview listing read that can't execute)
   never blocks the park — the safety-relevant action always proceeds.
   But the failure is visible (#1628): the hook returns
   `{op_class, preview_unavailable: true, preview_error}` and the
   dispatcher merges that marker onto the identifier fields, so the
   parked row reads "blast-radius unknown: <reason>" instead of a bare
   identifier default a reviewer can't tell from a small action. The
   reason string is the exception's type + message, truncated at 500
   chars. A builder that *declines* (returns `None` — op not
   previewable from these params) still collapses to the identifier-only
   default with no marker, as do connector-resolution faults outside
   the hook.
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

## Catalog `safety_level` on every envelope (#1855)

The per-op preview hook is opt-in, so before #1855 a parked op with no
registered builder carried only `{op_id, connector_id, target_id}` — a
reviewer could not tell a `dangerous` op (e.g. `keycloak.realm.create`)
from a `caution` op (e.g. `keycloak.user.create`) on the row alone.

`dispatcher._build_proposed_effect` now stamps the catalog
`descriptor.safety_level` onto **every** parked op's `proposed_effect`,
alongside `op_class` / `preview` (and `permission_preflight` when that
hook fired). The value (`safe` / `caution` / `dangerous`) is read
straight off the operation descriptor — op-identity metadata, never
recomputed — so the severity on the durable row is exactly what the
catalog declares.

It is layered at the **dispatcher seam**, not inside the per-op
`build_proposed_effect` builder, so it rides three bases uniformly: the
built `{op_class, preview}` envelope, the identifier-only default for
no-builder / declined ops, and the `preview_unavailable` fail-soft
marker. The `op_class` / `preview` / marker envelope built by
`build_proposed_effect` itself is unchanged; `safety_level` is added on
top. The only path that does **not** carry it is the bare identifier
default the caller stores when `_build_proposed_effect` returns `None`
(connector-resolution / hook fault) — that degraded path is unchanged.

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
there is no preview) is the base, the preflight result is attached
under `proposed_effect["permission_preflight"]` when it fired, and the
catalog `safety_level` (#1855, above) is stamped on top. Both hooks are
opt-in and fail-soft — a preflight that raises degrades to no banner;
the park always proceeds.

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

### Operator console (`backend/src/meho_backplane/ui/routes/approvals/`)

A session-BFF surface (cookie session + CSRF double-submit, not the
Bearer REST routes — a browser carrying only the BFF cookie cannot auth
those). Every read + decision derives `tenant_id` from the validated
`UISessionContext` only, and calls the `approval_queue` service
in-process (same in-process-audit binding the REST routes get).

| Verb | Path | Purpose |
|---|---|---|
| `GET` | `/ui/approvals/badge` | Live **pending** count for the app-shell bell. Always `status='pending'` — it counts actionable work, not history. |
| `GET` | `/ui/approvals` | Content-negotiated. A normal navigation (no `HX-Request`) → the **full-page console**: status tabs (Pending / Approved / Rejected / Expired / All), a `work_ref` filter, and the decision-history list. The bell's `hx-get` (`HX-Request: true`) → the existing pending **panel** modal fragment (unchanged). (G10.8-T #1827) |
| `GET` | `/ui/approvals/list` | Decision-history partial — the HTMX swap target for the status tabs / `work_ref` filter / "Load more" offset pager. Reuses `list_pending(status=…, work_ref=…, offset=…)`; `tab=all` passes `status=None`. Pages with a real offset, not the badge's 50-row glance cap. (G10.8-T #1827) |
| `GET` | `/ui/approvals/{id}` | Request-detail modal. A pending row offers Approve/Deny; a **decided** row renders read-only with a decision banner ("Approved/Rejected by X at T"). |
| `POST` | `/ui/approvals/{id}/approve` | Approve in-process + re-dispatch the parked op + fail-open broadcast. |
| `POST` | `/ui/approvals/{id}/reject` | Reject in-process + broadcast; the op never runs. |

The console is **read-only over substrate that already exists** — it adds
no new service call, no `api/v1` Bearer route, no CLI verb, and no
migration. It does, however, register its `/ui/*` routes into the FastAPI
OpenAPI document (the UI routers are not `include_in_schema=False`), so a
new or changed `/ui/approvals*` route — e.g. the `partial=rows` query the
"Load more" pager added — DOES enter `cli/api/openapi.json` and the
generated client. Re-snapshot the OpenAPI doc (`cd cli && make
snapshot-openapi && make generate`) whenever a `/ui/approvals*` route is
added or its signature changes, or the "CLI API snapshot freshness" check
goes red. The internal `params` / `params_hash`
columns are **never** projected onto any UI view, the badge, or a
broadcast frame: the `render.project_request_to_view` projection omits
them by construction. Live updates ride the app-shell bell's body-wide
`meho:approval-bump` / `meho:approval-decided` events — the history list
re-fetches its active tab on them, so a decision made elsewhere drops out
of the open Pending tab without a reload. The decision **reason** lives on
the `audit_log` decision row, not the `ApprovalRequest` row, so the
history view shows who/when (`reviewed_by` / `decided_at`) but not the
free-text reason (an `audit_log` read deferred to a follow-up).

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
