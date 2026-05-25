# Agent Permission Model (G11.2-T3)

## Overview

Task #820 (G11.2-T3, Initiative #803) wires a per-(principal, op, target)
permission model into the dispatcher's policy gate. Every dispatch call now
resolves an **effective verdict** — `auto-execute`, `needs-approval`, or `deny`
— before a connector is touched. The verdict is the intersection of three
independent gates:

```
effective = user-role-allows ∩ agent-permission ∩ op-requirement
```

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

### `AgentPermission` (db/models.py, migration 0019)

One row = one permission grant for a principal.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | PK |
| `tenant_id` | UUID | FK → `tenant.id` |
| `principal_sub` | Text | JWT `sub` of the grantee |
| `op_pattern` | Text | fnmatch glob (e.g. `"vault.kv.*"`, `"*"`) |
| `target_scope` | Text \| NULL | UUID string, `"*"`, or `NULL` (= any target) |
| `verdict` | Text | One of the three closed values |
| `created_by_sub` | Text | Tenant-admin who created the row |
| `created_at` / `updated_at` | timestamptz | Server defaults on PG; ORM defaults on SQLite |

Index `agent_permission_tenant_principal_idx` on `(tenant_id, principal_sub)`
drives the dominant query (all grants for a given principal in a tenant).

## Control flow

### Resolution algorithm (`auth/permissions.py::resolve_verdict`)

1. **Load rows.** All `AgentPermission` rows for `(tenant_id, principal_sub)`.
   The expected cardinality is small (tens of rows per principal, not thousands).

2. **Filter.** Keep rows whose `op_pattern` matches `op_id` via `fnmatch` **and**
   whose `target_scope` matches the target (`NULL`/`"*"` = any, exact UUID = exact
   match).

3. **Pick best row (specificity order).** Among matching rows, the one with the
   longest literal prefix before the first `*` wins. `"vault.kv.read"` (no
   wildcard, score = 14) beats `"vault.kv.*"` (score = 9), which beats `"*"`
   (score = 0). When no row matches, the op's `safety_level` is the default:

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
   | `dangerous` | `deny` | Any row verdict → `deny` |

5. **Apply role ceiling.** `READ_ONLY` principals are capped at `needs-approval`;
   `OPERATOR` and `TENANT_ADMIN` are uncapped.

6. **Return `(verdict, reason)`** where `reason` is a short string naming the
   verdict source, any ceilings applied, and the matched pattern. Callers log it
   and forward it to the structured error payload so agents can diagnose refusals.

### Dispatcher integration (`operations/dispatcher.py`)

```
verdict, gate_reason = await policy_gate(operator=..., descriptor=..., target=...)
if verdict == DENY:
    → audit row (status="denied") + result_denied(reason)
if verdict == NEEDS_APPROVAL:
    → audit row (status="pending") + result_pending(reason)   # 202
# else AUTO_EXECUTE: continue to connector resolution
```

`policy_gate` (`_validate.py`) opens its own DB session (same
`get_sessionmaker()` pattern as `audit_and_broadcast_safe`) and calls
`resolve_verdict`.

## Dependencies

- `meho_backplane/auth/operator.py` — `Operator`, `TenantRole`
- `meho_backplane/db/engine.py` — `get_sessionmaker`
- `meho_backplane/db/models.py` — `AgentPermission`, `PermissionVerdict`
- `meho_backplane/operations/_errors.py` — `result_denied`, `result_pending`,
  `status_code_for_result` (202 for pending, 403 for denied)

## Known issues

- **No row-management API.** G11.2-T3 ships the resolver and schema only; CRUD
  endpoints and CLI verbs for `agent_permission` rows are a follow-up task.
- **Approval queue stub.** `needs-approval` writes an audit row and returns HTTP
  202 today; the durable approval-queue mechanics (G11.2-T4, #817) will add the
  real pending row + resume path.
- **Soft FK to principal.** `principal_sub` has no FK to any agent-principal table
  because G11.2-T1 (#815) defines that table. A tightening migration can add the
  FK once T1 lands.

## References

- Task #820 (G11.2-T3), Initiative #803
- `backend/src/meho_backplane/auth/permissions.py`
- `backend/src/meho_backplane/db/models.py` — `AgentPermission`, `PermissionVerdict`
- `backend/alembic/versions/0019_create_agent_permission.py`
- `backend/tests/test_permission_resolver.py`
