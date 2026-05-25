# Agent Permission Grants

G11.2-T6 (#819) under Initiative #803 (the P3 agent identity + RBAC + approval gate).

## Overview

A newly-registered agent starts with **no** `agent_permission` rows. Without an explicit grant, the agent operates under `safety_level`-based defaults: safe ops auto-execute, caution ops need approval, dangerous ops are denied. Admin-class ops are never auto-granted.

Operators (`tenant_admin`) issue grants to extend agent access for a specific op-pattern on an optional target scope. Grants are permanent by default; supply `expires_at` to create a **time-bounded elevation** (change window) that reverts automatically: the permission resolver (T3 `auth/permissions.py`) ignores rows past their `expires_at` **immediately at expiry**, and the expiry sweeper deletes them on its periodic tick (the sweep is the durable cleanup; the resolver filter makes the revert exact).

## Key types

### `AgentPermission` (ORM model, `db/models.py`)

One row per per-(principal, op-pattern, target-scope) permission grant.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `tenant_id` | UUID FK→tenant | Cross-tenant isolation |
| `principal_sub` | text | JWT `sub` of the principal |
| `op_pattern` | text | fnmatch glob (`*`, `vault.kv.*`, `GET:/api/vcenter/*`) |
| `target_scope` | text nullable | UUID string, `*`, or NULL (= any target) |
| `verdict` | text | `auto-execute` \| `needs-approval` \| `deny` |
| `created_by_sub` | text | JWT `sub` of the issuing `tenant_admin` |
| `expires_at` | timestamptz nullable | NULL = permanent; non-null = elevation |
| `created_at` / `updated_at` | timestamptz | |

### `PermissionVerdict` (StrEnum)

Closed vocabulary of three values: `AUTO_EXECUTE`, `NEEDS_APPROVAL`, `DENY`. DB-layer CHECK constraint prevents drift.

### `AgentGrantService` (`agents/grants.py`)

Stateless, async, session-per-method. Public API:

- `grant(tenant_id, created_by_sub, payload)` → `AgentGrantRead`
- `revoke(tenant_id, grant_id)` → `bool`
- `get(tenant_id, grant_id)` → `AgentGrantRead | None`
- `list_(tenant_id, *, principal_sub, include_expired, limit, offset)` → `list[AgentGrantRead]`

Raises `GrantValidationError` when:
- `expires_at` is in the past or timezone-naive
- `target_scope` is neither `None`, `"*"`, nor a valid UUID string

## Control flow

```
tenant_admin POST /api/v1/agents/grants
         │
         ▼
api/v1/agent_grants.py → require_role(TENANT_ADMIN)
         │
         ▼
AgentGrantService.grant()
  ├── _validate_op_pattern()
  ├── _validate_target_scope()
  ├── _validate_expires_at()
  └── INSERT agent_permission row
         │
         ▼
AuditMiddleware writes audit_log row (audit_op_id=agent.grant.create)
```

For time-bounded elevations: same path, plus `expires_at` set on the row. The grant-expiry sweeper (`agents/grant_expiry.py`) runs every 5 minutes (configurable) and deletes rows where `expires_at < now()`.

## Default-deny model

| Agent state | Writes | Admin-class ops |
|---|---|---|
| No grants (new agent) | Safety-level default | Deny (dangerous = deny default) |
| Permanent grant on `*` | Verdict from grant row | Only if explicit auto-execute grant (dangerous ops still capped) |
| Elevation active | As granted until expiry | Only if explicit grant (same cap) |
| Elevation expired | Reverts to baseline | Denied |

## Elevation sweeper (`agents/grant_expiry.py`)

Background `asyncio.Task` registered in `main.py` lifespan, gated on `GRANT_EXPIRY_ENABLED` (default: true). Cadence: `GRANT_EXPIRY_TICK_INTERVAL_SECONDS` (default: 300 s).

Tick logic:
1. `SELECT id, tenant_id FROM agent_permission WHERE expires_at IS NOT NULL AND expires_at < now()`
2. `DELETE WHERE id IN (candidates)`
3. One audit row per affected tenant (`/internal/agent-permission/expire`)

Mirrors the G5.2 memory-expiry sweeper pattern verbatim.

## REST surface

| Verb | Path | Role | Notes |
|---|---|---|---|
| `GET` | `/api/v1/agents/grants` | tenant_admin | List; `?principal_sub=`, `?include_expired=` |
| `GET` | `/api/v1/agents/grants/{id}` | tenant_admin | Show one |
| `POST` | `/api/v1/agents/grants` | tenant_admin | Create (permanent or elevation) |
| `POST` | `/api/v1/agents/grants/elevate` | tenant_admin | Create elevation (`expires_at` required) |
| `DELETE` | `/api/v1/agents/grants/{id}` | tenant_admin | Revoke |

## MCP surface

Five tools under `meho.agents.grant.*`: `list`, `show`, `create`, `elevate`, `revoke`. Registered via `register_mcp_tool` (auto-discovered by `eager_import_mcp_modules`).

## CLI surface

```
meho agent grant list [--principal <sub>] [--include-expired]
meho agent grant show <grant-id>
meho agent grant create --principal <sub> --op <pattern> --verdict V [--target T] [--expires ISO8601]
meho agent grant elevate --principal <sub> --op <pattern> --verdict V --expires ISO8601 [--target T]
meho agent grant revoke <grant-id> [--confirm]
```

All verbs require `tenant_admin`.

## Dependencies

- `db/models.py` — `AgentPermission`, `PermissionVerdict` ORM model (owned by T3 #1052; T6 adds the `expires_at` column + index)
- `alembic/versions/0024_add_agent_permission_expires_at.py` — additive ALTER: `expires_at` column + `agent_permission_expires_at_idx` (the table itself is created by T3's `0022_create_agent_permission`)
- `memory/audit.py` — `write_internal_audit_row` (used by expiry sweeper)
- `memory/expiry.py` — pattern precedent for sweeper design
- `settings.py` — `grant_expiry_enabled`, `grant_expiry_tick_interval_seconds`

## Known issues

- `expires_at` comparison uses Python-side `datetime.now(UTC)` which is close to (but not exactly) the tick start time. A row that expires mid-tick may be included or excluded depending on sub-second timing — acceptable for change-window use cases where minutes matter, not milliseconds.
- The verdict resolver (G11.2-T3) is a parallel PR (#1052). Until it lands, `agent_permission` rows are inserted but the dispatcher still uses the default-allow `policy_gate` from v0.2.

## References

- G11.2-T3 (#820): per-agent permission model + resolver (parallel PR #1052)
- G11.2-T6 (#819): this task (grant surface)
- `memory/expiry.py`: sweeper precedent
- `agents/service.py`: service layer precedent (session-per-method pattern)
