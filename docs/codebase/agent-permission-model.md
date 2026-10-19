# Agent Permission Model (G11.2-T3)

## Overview

Task #820 (G11.2-T3, Initiative #803) wires a per-(principal, op, target)
permission model into the dispatcher's policy gate. When the calling principal
is an **agent**, every dispatch call resolves an **effective verdict** —
`auto-execute`, `needs-approval`, or `deny` — before a connector is touched.
The verdict is the intersection of three independent gates:

```text
effective = user-role-allows ∩ agent-permission ∩ op-requirement
```

People and service accounts do not use this resolver. They follow a
simpler rule, set by the op's `safety_level` and `requires_approval`; see
[Principal-kind branch](#principal-kind-branch).

The full resolution lives in `meho_backplane/auth/permissions.py`; the gate is
called from `meho_backplane/operations/_validate.py`; the dispatcher branches
on the result in `meho_backplane/operations/dispatcher.py`.

## Safety levels at a glance

This is what happens today when no grant matches. In this table:

- **Agent login** is a token for an agent identity (`principal_kind=agent`).
- **Person** is a person's login (`principal_kind=user`).
- **Service account** is fixed automation that logs in with a client secret
  (`principal_kind=service`).

| Safety level | Agent login, no grant | Person | Service account |
|---|---|---|---|
| `safe` | runs at once | runs at once | runs at once |
| `caution` | waits for approval | runs at once | a change waits for approval unless a standing grant covers it; a read runs at once |
| `dangerous` | refused (a grant can change this to "waits for approval") | runs at once | waits for approval unless a standing grant covers it |
| `destructive` | always refused | waits for approval | always waits for approval |

One sentence per level:

- **`safe`:** an agent login runs it at once, and so does a person.
- **`caution`:** an agent login waits until a person approves it (it is
  not refused), and a person runs it at once.
- **`dangerous`:** an agent login is refused unless a grant allows it, and
  then it still waits for approval; a person runs it at once.
- **`destructive`:** an agent login is always refused, and a person always
  waits for approval. A destructive call must first get a `preview_hash`
  from `preview_operation` and pass it to `call_operation`; without a
  matching hash, MEHO refuses the call and stores no approval request
  (#3197).

An op marked `requires_approval` also waits for approval, for every kind of
login. There are two exceptions. A refusal for an agent login stays a
refusal. A service account's standing grant can still let the op run, but
never a `destructive` one.

"Waits for approval" means MEHO stores an approval request and returns
`awaiting_approval`. The op runs only after someone approves it. See
[approvals.md](approvals.md).

An agent with the `read_only` role never runs anything at once: its best
result is "waits for approval" (see step 5 below).

## Key types

### `PermissionVerdict` (db/models.py)

`StrEnum` with three closed values:

| Value | Meaning |
|---|---|
| `auto-execute` | The op proceeds immediately. |
| `needs-approval` | The op is parked; an operator must approve before execution. |
| `deny` | The op is refused with a structured, agent-readable error. |

The vocabulary is closed at the DB layer via a `CHECK` constraint on
`agent_permission.verdict`.

### `AgentPermission` (db/models.py, migration 0022)

One row = one permission grant for a principal.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `tenant_id` | UUID | FK → `tenant.id` |
| `principal_sub` | Text | JWT `sub` of the grantee |
| `op_pattern` | Text | fnmatch glob (e.g. `"vault.kv.*"`, `"*"`) |
| `target_scope` | Text | UUID string, or `"*"` (= any target). **NOT NULL**, default `"*"` |
| `verdict` | Text | One of the three closed values |
| `created_by_sub` | Text | Tenant-admin who created the row |
| `created_at` / `updated_at` | timestamptz | Server defaults on PG; ORM defaults on SQLite |

- Index `agent_permission_tenant_principal_idx` on `(tenant_id, principal_sub)`
  drives the dominant query (all grants for a given principal in a tenant).
- Unique constraint `uq_agent_permission_grant` on
  `(tenant_id, principal_sub, op_pattern, target_scope)` — the row is *keyed*
  by this tuple. `target_scope` is NOT NULL precisely so the key stays total: a
  `NULL` would let Postgres treat two otherwise-identical any-target rows as
  distinct (`NULL != NULL`) and silently defeat the constraint.

## Control flow

### Principal-kind branch

`policy_gate` (`_validate.py`) branches on `operator.principal_kind` (from
G11.2-T1):

- **Every kind that is not `agent`** (`user`, `service`, `runner`) —
  `_non_agent_verdict` (`_validate.py`). It never returns `deny`. It returns
  `needs-approval` in these cases, in this order:
  1. `safety_level=destructive` — for every person and every service
     account, even when `requires_approval` is false (#3198). No standing
     grant can clear it.
  2. `requires_approval=True` — the op goes to the approval queue, not a
     hard deny (G11.7-T1 #1401).
  3. Service accounts only (`_service_safety_gate_reason`, #3152): a
     `dangerous` op, and a `caution` op that changes something. A
     `caution` read (`GET` / `HEAD`) runs at once. A typed `caution` op
     has no HTTP method, so it always counts as a change.

  A service account with a live standing grant that covers the op runs it
  at once; MEHO records the grant use on the approvals audit ledger (#3151,
  see [service-principal-grants.md](service-principal-grants.md)). Everything
  else returns `auto-execute`.
  **Outside a direct hosted-agent run, the per-agent resolver is never
  consulted for a non-agent token: no `agent_permission` row is loaded on this
  branch, so a grant has zero effect on a `user`/`service`-kind dispatch.** This is deliberate (G11.2-T3) — an op a
  human operator has always been able to run must not silently start
  pending/denying because someone wrote a grant.
- **`agent`** — the full per-(principal, op, target) resolver below. After
  resolution, `requires_approval=True` folds the verdict up to at least
  `needs-approval`, so a connector-flagged op is never auto-executed by an agent
  regardless of its `safety_level`.
- **Any caller inside a direct hosted-agent run** (#4237) — when a caller
  starts a hosted agent run directly, the run binds the definition's agent
  (`identity_ref`, `agent:<name>`) as the actor
  (`auth/delegation.py::actor_delegation`). `policy_gate` checks the bound
  actor before it looks at the caller's kind, and works out two verdicts:
  the hosted agent's (the resolver below, keyed on `agent:<name>`, with the
  caller's role ceiling) and the caller's own (the branch above that matches
  the caller's kind, so an `agent`-kind caller keeps its own grants). The
  stricter one wins. The caller's verdict is worked out only when the hosted
  agent may run the op (agent verdict `auto-execute`). An agent `deny` or
  `needs-approval` returns at once, so a standing grant is recorded as used
  only when the call runs. The rule in one line: a direct run may do only
  what both the caller and the hosted agent may do. See `agent-runtime.md`
  "Whose identity a direct run uses".

Because a grant is consulted **only** on the `agent` branch, a grant whose
`principal_sub` names no registered, non-revoked agent principal can never be
evaluated — no token will authenticate under that sub with
`principal_kind=agent`. `AgentGrantService.grant` rejects such a grant at
create/elevate time (matching `principal_sub` against
`AgentPrincipal.keycloak_client_id`, tenant-scoped) so an unenforceable row is
never written; the grant tool/REST/UI surfaces surface this as a validation
error naming the `principal_kind=agent` enforcement scope. The one relaxation
is `principal_kind=user-agent` (#3795): for a human user authenticating with
`principal_kind=agent` through a public client — whose `sub` is the user id and
has no `AgentPrincipal` row — the registry check is skipped and the grant is
keyed on the bare sub (a deliberate, explicit `tenant_admin` opt-in).

### Enforcement identity: sub vs client id (#3795)

A registered agent's access token carries the service-account **UUID** in
`sub`, while a grant is keyed on the `agent:<name>` OAuth clientId. To bridge
that gap the clientId is surfaced onto `Operator.client_id` from the token's
`azp` / RFC 9068 `client_id` claim (agent and service kinds,
`auth/jwt.py::_extract_client_id`), and the resolver loads rows for
`principal_sub IN {operator.sub, operator.client_id}`. A `user-agent` grant is
matched via `operator.sub` (its `azp` is a public client, not `agent:<name>`).
The clientId is folded in **only** for `principal_kind == agent`, so
`user` / `service` / `runner` principals are unaffected (a `service`
principal's `client_id`, populated from the #3178 username marker or the same
claim for audit parent-linkage (#3446), is never matched against this table).

### Resolution algorithm (`auth/permissions.py::resolve_verdict`)

1. **Load rows.** All `AgentPermission` rows for `(tenant_id, principal_sub ∈
   {operator.sub, agent clientId})` (#3795), ordered for stable logging.
   Expected cardinality is small (tens of rows per principal, not thousands).

2. **Filter.** Keep rows whose `op_pattern` matches `op_id` via `fnmatch` **and**
   whose `target_scope` matches the target (`"*"` = any, exact UUID = exact
   match).

3. **Pick best row (specificity order).** Among matching rows, the one with the
   longest literal prefix before the first glob metacharacter (`*`, `?` or `[`)
   wins. `"vault.kv.read"` (no wildcard, score = 13) beats `"vault.kv.*"`
   (score = 9), which beats `"*"` (score = 0). When several rows **tie** on
   specificity (e.g. two distinct equally-specific patterns that both match),
   the verdict folds to the **most restrictive** of the tied rows — fail-closed
   and independent of row order. When no row matches, the op's `safety_level`
   is the default:

   | `safety_level` | Default verdict |
   |---|---|
   | `safe` | `auto-execute` |
   | `caution` | `needs-approval` |
   | `dangerous` | `deny` |
   | `destructive` | `deny` |
   | unknown | `deny` (fail-closed) |

4. **Apply safety-level ceiling.** A row's verdict can only tighten to the
   op's ceiling, never loosen beyond it:

   | `safety_level` | Ceiling | Effect |
   |---|---|---|
   | `safe` | none | Any row verdict is valid |
   | `caution` | `needs-approval` | `auto-execute` row → `needs-approval` |
   | `dangerous` | `needs-approval` | `auto-execute` row → `needs-approval`; a dangerous op is grantable up to human approval, never auto-executed. The *default* (no grant) stays `deny`. |
   | `destructive` | `deny` | `auto-execute` / `needs-approval` row → `deny`; the tier is **non-grantable for agents** — no `AgentPermission` grant lifts an agent principal off `deny` (#3183). |

   So no grant can make a `caution` or `dangerous` op run at once for an
   agent. The best a grant can give is "waits for approval".

   - `caution`: the default with no grant is already `needs-approval`. The
     op waits for approval; it is **not** refused. A grant cannot loosen
     this. A `deny` grant can tighten it to a refusal.
   - `dangerous`: the default with no grant is `deny`. A grant with
     `auto-execute` or `needs-approval` lifts it to `needs-approval`. This
     is the "deny **unless granted**" rule (#820).
   - `destructive` (#3183): the default and the ceiling are both `deny`, so
     an agent is refused whatever grant exists. Running a destructive op is
     a human decision on the non-agent approval path (#3197 / #3198).

5. **Apply role ceiling.** `READ_ONLY` principals are capped at `needs-approval`;
   `OPERATOR` and `TENANT_ADMIN` are uncapped.

6. **Return `(verdict, reason)`** where `reason` is a short string naming the
   verdict source, any ceilings applied, and the matched pattern(s). Callers log
   it and forward it to the structured error payload so agents can diagnose
   refusals.

### Dispatcher integration (`operations/dispatcher.py`)

```text
verdict, gate_reason = await policy_gate(operator=..., descriptor=..., target=...)
if verdict == DENY:
    → audit row (status="denied") + result_denied(reason)        # 403
elif verdict == NEEDS_APPROVAL:
    → _handle_needs_approval(...)                                # 202
      stores an ApprovalRequest row + its "request" audit row,
      returns result_awaiting_approval (status="awaiting_approval")
elif verdict != AUTO_EXECUTE:
    → audit row (status="denied") + result_denied(...)           # defensive fail-closed
# else AUTO_EXECUTE: continue to connector resolution
```

A `deny` verdict refuses the call. A `needs-approval` verdict usually
stores an approval request and waits: the stored request runs after someone
approves it (#817). In a few cases `_handle_needs_approval` refuses the call
instead and stores nothing:

- a `destructive` op without a matching `preview_hash` from
  `preview_operation`, or without a blast-radius statement
  (`_destructive_binding_refusal`, #3197);
- a write that the permission check before storing says will be denied
  (`_preflight_denied_refusal`, `write_capability_denied`, #3542);
- an `idempotency_key` that a different call already uses
  (`idempotency_key_conflict`, #3486);
- an unexpected error while storing the request (the call is refused, not
  left half-stored).

See [approvals.md](approvals.md) for the queue, the approve and reject
verbs, and the resume path.

`policy_gate` (`_validate.py`) opens its own DB session (same
`get_sessionmaker()` pattern as `audit_and_broadcast_safe`) and calls
`resolve_verdict` for agent principals.

### Audit-query agreement

A `needs-approval` dispatch writes its "request" audit row with the
synthetic status code `202` (`approval_queue.py`). The audit-query read path
(`audit_query/query.py`) derives `pending` from `status_code == 202` and offers
a `result_status="pending"` filter, so a waiting call is not counted as `ok`.

## Dependencies

- `meho_backplane/auth/operator.py` — `Operator`, `TenantRole`, `PrincipalKind`
- `meho_backplane/db/engine.py` — `get_sessionmaker`
- `meho_backplane/db/models.py` — `AgentPermission`, `PermissionVerdict`
- `meho_backplane/operations/_errors.py` — `result_denied`,
  `result_awaiting_approval`, `status_code_for_result` (202 for
  `awaiting_approval`, 403 for denied)
- `meho_backplane/operations/approval_queue.py` — `create_pending_request`

## Known issues

- **Rules for agents will change.** #3980 plans that only `safe` runs at
  once for agents and token calls, and everything else waits for approval.
  This page describes the code that runs today. Update it when that change
  lands.
- **Soft FK to principal.** `principal_sub` has no database FK to the
  agent-principal table (`agent_principal`, G11.2-T1); a tightening migration
  can add it later. `AgentGrantService.grant` now enforces the reference at the
  write boundary (rejecting a `principal_sub` that matches no non-revoked
  `AgentPrincipal.keycloak_client_id` in the tenant), so a typo'd or
  never-registered sub cannot land — but a `revoke` of the principal *after*
  the grant lands leaves the (now inert) row in place until the grant is
  revoked, the same TOCTOU posture as `validate_identity_ref`.

## References

- Task #820 (G11.2-T3), Initiative #803; Task #2489 (create-time principal check)
- #3795 — enforce grants on the token's client identity (`azp`); `principal_kind=user-agent` for user-sub agent principals
- `backend/src/meho_backplane/auth/permissions.py`
- `backend/src/meho_backplane/auth/jwt.py` — `_extract_client_id` (surfaces the agent clientId)
- `backend/src/meho_backplane/operations/_validate.py` — `policy_gate`,
  `_non_agent_verdict`, `_service_safety_gate_reason`
- `backend/src/meho_backplane/operations/dispatcher.py` — `_handle_needs_approval`
- [agent-permission-grants.md](agent-permission-grants.md) — how to list,
  create and revoke grants (REST, CLI, and the operator-plane MCP tools
  `meho_agents_grant_list`, `_show`, `_create`, `_revoke`; G11.2-T6 #819)
- [approvals.md](approvals.md) — the approval queue
- `backend/src/meho_backplane/agents/grants.py` — `AgentGrantService.grant`
  create-time principal check (#2489)
- `backend/src/meho_backplane/agents/identity_ref.py` — the sibling
  write-boundary validator this check mirrors
- `backend/src/meho_backplane/db/models.py` — `AgentPermission`, `PermissionVerdict`
- `backend/alembic/versions/0022_create_agent_permission.py`
- `backend/tests/test_permission_resolver.py`,
  `backend/tests/test_operations_dispatcher.py`
