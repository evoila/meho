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

Human operators and service accounts keep the v0.2 contract (default-allow
except `requires_approval`); see [Principal-kind branch](#principal-kind-branch).

The full resolution lives in `meho_backplane/auth/permissions.py`; the gate is
called from `meho_backplane/operations/_validate.py`; the dispatcher branches
on the result in `meho_backplane/operations/dispatcher.py`.

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

- **`user` / `service`** — the v0.2 contract: `requires_approval=True` →
  `deny`; otherwise `auto-execute`. The per-agent resolver is **not** consulted,
  so an op a human operator has always been able to run does not silently start
  pending/denying. (G11.2-T4 routes only agent runs to the pending path; humans
  keep the hard-deny.)
- **`agent`** — the full per-(principal, op, target) resolver below. After
  resolution, `requires_approval=True` folds the verdict up to at least
  `needs-approval`, so a connector-flagged op is never auto-executed by an agent
  regardless of its `safety_level`.

### Resolution algorithm (`auth/permissions.py::resolve_verdict`)

1. **Load rows.** All `AgentPermission` rows for `(tenant_id, principal_sub)`,
   ordered for stable logging. Expected cardinality is small (tens of rows per
   principal, not thousands).

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
   | unknown | `deny` (fail-closed) |

4. **Apply safety-level ceiling.** A row's verdict can only tighten to the
   op's ceiling, never loosen beyond it:

   | `safety_level` | Ceiling | Effect |
   |---|---|---|
   | `safe` | none | Any row verdict is valid |
   | `caution` | `needs-approval` | `auto-execute` row → `needs-approval` |
   | `dangerous` | `needs-approval` | `auto-execute` row → `needs-approval`; a destructive op is grantable up to human approval, never auto-executed. The *default* (no grant) stays `deny`. |

   This is the "destructive = deny **unless granted**" rule (#820): the
   no-grant default is `deny`, but an explicit grant *is* honoured up to the
   `needs-approval` ceiling.

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
    → audit row (status="pending") + result_pending(reason)      # 202
elif verdict != AUTO_EXECUTE:
    → audit row (status="denied") + result_denied(...)           # defensive fail-closed
# else AUTO_EXECUTE: continue to connector resolution
```

`policy_gate` (`_validate.py`) opens its own DB session (same
`get_sessionmaker()` pattern as `audit_and_broadcast_safe`) and calls
`resolve_verdict` for agent principals.

### Audit-query agreement

A `needs-approval` dispatch persists `result_status="pending"` (synthetic HTTP
`202`) on both the audit row and the broadcast event. The audit-query read path
(`audit_query/query.py`) derives `pending` from `status_code == 202` and offers
a `result_status="pending"` filter, so the audit API and the broadcast feed
agree on the same `audit_id` rather than collapsing the 202 (a 2xx) to `ok`.

## Dependencies

- `meho_backplane/auth/operator.py` — `Operator`, `TenantRole`, `PrincipalKind`
- `meho_backplane/db/engine.py` — `get_sessionmaker`
- `meho_backplane/db/models.py` — `AgentPermission`, `PermissionVerdict`
- `meho_backplane/operations/_errors.py` — `result_denied`, `result_pending`,
  `status_code_for_result` (202 for pending, 403 for denied)

## Known issues

- **No row-management API.** G11.2-T3 ships the resolver and schema only;
  grant/revoke CLI + MCP + REST verbs are G11.2-T6 (#819).
- **Approval queue stub.** `needs-approval` writes an audit row and returns HTTP
  202 today; the durable approval-queue mechanics (G11.2-T4, #817) add the real
  pending row + resume path.
- **Soft FK to principal.** `principal_sub` has no FK to the agent-principal
  table (`agent_principal`, G11.2-T1); a tightening migration can add it later.

## References

- Task #820 (G11.2-T3), Initiative #803
- `backend/src/meho_backplane/auth/permissions.py`
- `backend/src/meho_backplane/operations/_validate.py`
- `backend/src/meho_backplane/db/models.py` — `AgentPermission`, `PermissionVerdict`
- `backend/alembic/versions/0022_create_agent_permission.py`
- `backend/tests/test_permission_resolver.py`,
  `backend/tests/test_operations_dispatcher.py`
