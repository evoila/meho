# `meho_backplane.agents` — agent-definition storage + admin CRUD

G11.1-T2 (#809) under Initiative #802 (the P1 agent runtime). This is
the durable map of the agent-definition surface: the ORM table, the
Pydantic schemas, the stateless CRUD service, and the three operator
fronts (REST, MCP, CLI) that manage it.

## Overview

An *agent definition* is a first-class, tenant-scoped record MEHO
stores and the runtime (T1, #808) loads to run an LLM agent: the
identity it runs as, a logical model tier, a system prompt, a toolset
spec, a turn budget, an optional structured-output schema, and an
enabled flag. Storing the definition as a typed row — not an ad-hoc API
payload — makes agents listable, versionable, auditable objects.

This Task ships only the storage + management plane. Running a
definition (T1 #808 / T4 #811) and resolving the toolset against the
identity's permissions (T3 #810) are out of scope; the identity itself
(G11.2-T1) is a separate table, so this record stores only an opaque
reference to it.

The dedicated-table choice is the load-bearing departure from kb /
memory, which wrap the shared `documents` retrieval substrate. An agent
definition is a *structured* record with typed columns (an integer turn
budget, a bounded model tier, a JSON toolset spec), not a retrievable
text blob. The `BroadcastOverride` precedent — a dedicated
tenant-scoped CRUD table with a real FK to `tenant.id` — is the shape
this surface copies.

## Key types

### `AgentDefinition` (`db/models.py`)

The ORM table `agent_definition`. Columns: `id` (UUID PK), `tenant_id`
(UUID, real FK to `tenant.id`), `name` (per-tenant slug), `identity_ref`
(soft reference to the G11.2 agent principal — no FK at the schema
level; the service validates against `agent_principal.keycloak_client_id`
at write boundaries, see G11.2-T8 #1099 below), `model_tier`,
`system_prompt`, `toolset` (portable JSON → JSONB, default `{}`),
`turn_budget` (int), `output_schema` (nullable JSON), `enabled` (bool,
default true), `created_by_sub`, `created_at`, `updated_at`. A single
unique composite index `agent_definition_tenant_name_idx` on
`(tenant_id, name)` is the natural key for the CRUD lookup and drives
the tenant-scoped list query.

### `AgentModelTier` (`agents/schemas.py`)

A `StrEnum` of `standard` / `fast` / `deep`. The tier is *logical* —
G11.5's multi-provider resolver maps it to a concrete backend at run
time. The bounded set is enforced as a Pydantic `Literal`-equivalent
(a typed enum field), not a DB `CHECK`, so a future tier lands without a
migration (the forward-compat argument `BroadcastOverride.scope_field`
makes).

### `AgentDefinitionCreate` / `AgentDefinitionUpdate` / `AgentDefinitionRead` (`agents/schemas.py`)

The write / partial-update / read shapes. Create and Update set
`extra="forbid"` so an unknown / mistyped field is a 422 at the
boundary. `name` is constrained to the safe-URL alphabet
(`NAME_PATTERN`, mirroring memory's slug pattern) because it is a URL
path segment. `turn_budget` is bounded `1..1000`. Update is partial via
`model_dump(exclude_unset=True)`; `name` is not updatable (it is the
natural key — renaming is delete + recreate). Read uses
`from_attributes=True` so handlers hand back the ORM row directly.

### `AgentDefinitionService` (`agents/service.py`)

The stateless, tenant-scoped CRUD service — the single code path REST,
MCP, and CLI all dispatch through. Each method opens its own
`AsyncSession`, commits, and closes (the memory / kb session-per-method
shape). Every query starts with `WHERE tenant_id = :tenant_id`, so
cross-tenant rows are structurally invisible: `get` / `update` /
`delete` against another tenant's definition return `None` / `False`
(the 404 the boundary renders). A duplicate `create` raises
`AgentDefinitionExistsError` (narrowed from the unique-index
`IntegrityError`).

**`identity_ref` validation (G11.2-T8 #1099, refined in G11.2-T9
#1112)**: `create` and any `update` that touches `identity_ref`
validate the value against `agent_principal.keycloak_client_id`
scoped to the operator's tenant and require `revoked=False` — raising
`AgentIdentityRefInvalidError` otherwise (REST: 422
`identity_ref_unknown`; MCP: Invalid Params `identity_ref_unknown`).
The validator (`meho_backplane.agents.identity_ref.validate_identity_ref`,
re-exported from `service` as `_validate_identity_ref`) runs inside
the caller's session, so the SELECT and the write share one
transaction. The chassis does not configure `REPEATABLE READ` —
PostgreSQL defaults to `READ COMMITTED`, where each statement gets
its own snapshot — so a revoke that lands between the SELECT and the
write *is* visible to the write statement: the TOCTOU window is
small but real. The authoritative gate against a principal being
revoked between validation and use is the runtime check in G11.3's
`run_scheduled`, which enforces `identity_ref == agent_client_id`
under the `client_credentials` grant. The write-boundary validator
is the hygiene check that keeps a typo'd or never-existed
`identity_ref` from ever landing in the first place.
The reason (`unknown` / `revoked`) is collapsed into one boundary
code so cross-tenant existence isn't leaked; operators see the
precise reason on the `identity_ref_invalid` structlog `warning`
event emitted before each raise (carries `identity_ref`, `reason`,
`tenant_id`). A PATCH that doesn't include `identity_ref` skips the
validation. Validating at the write boundary makes the G11.2-T2
(#816) contracts — `actor_sub = identity_ref` on user-initiated runs,
`identity_ref == agent_client_id` enforcement on `run_scheduled` —
well-formed by construction: a typo'd `identity_ref` can never
produce a meaningless `actor_sub` or a confusing scheduled-run
rejection later.

## Control flow

### Write path — create

`POST /api/v1/agents` → `create_agent` (tenant_admin gate, audit
contextvars bound) → `AgentDefinitionService.create` → insert + flush.
A unique-index violation is narrowed to `AgentDefinitionExistsError`
and rendered as 409 `agent_already_exists`. The MCP `meho.agents.create`
tool and the `meho agent create` CLI verb dispatch through the same
service method.

### Write path — partial update

`PATCH /api/v1/agents/{name}` → `edit_agent` → `service.update`. Only
fields the caller set (`exclude_unset`) are applied; `model_tier`
round-trips through the enum's `.value` so the column stores the wire
string. Absent / cross-tenant name returns `None` → 404. The CLI
`edit` verb uses `cmd.Flags().Changed(...)` to send only the changed
fields, mirroring `exclude_unset`.

### Read path — list / show

`GET /api/v1/agents` (name-sorted, paginated) and
`GET /api/v1/agents/{name}` are operator-level. A cross-tenant /
absent name on show returns 404 `agent_not_found` — never 403, so
existence is not leaked across the tenant boundary.

### Delete path

`DELETE /api/v1/agents/{name}` (tenant_admin) → `service.delete` using
`DELETE ... RETURNING name` to detect the no-row case. Absent /
cross-tenant returns `False` → 404. The CLI `delete` verb prompts for
confirmation unless `--confirm` is passed.

The delete is a **bulk Core** `DELETE`, so a definition's dependent
`scheduled_trigger` rows are removed by the DB-level
`ON DELETE CASCADE` on `scheduled_trigger.agent_definition_id`
(migration `0035`, #1480), not by an ORM relationship cascade (which a
bulk delete bypasses). This covers a cancelled trigger the scheduler
retains for audit too — before 0035 such a row left an FK violation
that surfaced as an opaque `-32603 "internal error: IntegrityError"`
(MCP) / unhandled 500 (REST), making a once-scheduled definition
undeletable via the API. `agent_run` history is a nullable soft-FK with
no `ForeignKey` clause, so runs never block deletion and survive it.

## RBAC

Reads (`list` / `show`) require `operator`; writes (`create` / `edit` /
`delete`) require `tenant_admin`. The REST routes use
`Depends(require_role(...))`; the MCP tools declare `required_role` on
the `ToolDefinition` (the registry filter hides write tools from
non-admins in `tools/list`, and the dispatcher re-checks at call time);
the CLI relies on the backend's 403, rendered as `insufficient_role`.
The service itself does not enforce roles — it assumes the caller has
validated the tenant role, keeping it callable from unattended contexts.

## Audit + broadcast

Every route / MCP handler binds `audit_op_id` (`agent.list` /
`agent.show` / `agent.create` / `agent.edit` / `agent.delete`) and
`audit_op_class` (`read` for list/show, `write` for the rest) before
the service call; mutations also bind `audit_agent_name`. The chassis
audit middleware strips the `audit_` prefix and merges these into the
audit-log `payload`, so a mutation produces an audit row + broadcast
event regardless of transport (REST vs MCP).

## Dependencies

- `db/models.py` — the `AgentDefinition` ORM table + the shared
  `_PORTABLE_JSON` (JSON → JSONB) column type.
- `alembic/versions/0015_create_agent_definition.py` — the additive
  migration creating the table + index (down_revision `0014`).
- `auth/operator.py` + `auth/rbac.py` — `Operator`, `TenantRole`,
  `require_role`.
- `db/engine.py` — `get_sessionmaker` (the session-per-method source).
- `mcp/registry.py` — `register_mcp_tool` (auto-discovered at startup
  via `eager_import_mcp_modules`).
- CLI: `cli/internal/cmd/agent/` (cobra package) + `cli/internal/backplane`
  (shared backplane resolution). The verbs call the generated
  oapi-codegen typed client (`cli/internal/api/client.gen.go`) via
  `api.AuthedClient` for bearer injection + one-shot 401-refresh; a
  small per-package `retryOn401` helper runs the same retry contract
  per typed endpoint that `AuthedClient.GetHealth` runs for `/health`.
  The package-local `renderRequestError` / `renderHTTPStatus` pair
  preserves the agents REST surface's status-code → category mapping
  (401 → `auth_expired`, 403 → `insufficient_role`, 404 →
  `agent_not_found`, 409 / 422 → backend detail, etc.). G0.12-T3
  (#1261, Initiative #1118) flipped the verbs off the per-verb
  hand-rolled `doAuthedRequest` onto this typed-client transport.

## Known issues

- `output_schema` cannot be *cleared* through `PATCH` — under
  `exclude_unset`, a `None` value is indistinguishable from "field
  omitted". Clearing a structured-output schema is a delete + recreate
  in v0.2; documented on `AgentDefinitionUpdate`. A future shape (a
  sentinel / discriminated value) can lift this.
- The CLI MCP-tool surface intentionally keeps agent definitions out of
  the agent-facing meta-tool waist beyond the admin CRUD verbs —
  *running* an agent is T1/T4's `call_operation`-adjacent surface, not
  this Task's.

## References

- Initiative #802 (G11.1 agent runtime) / Goal #800 (G11).
- Precedent: `BroadcastOverride` (`db/models.py`,
  `api/v1/broadcast_overrides.py`, `mcp/tools/broadcast_overrides.py`,
  `cli/internal/cmd/broadcast/`).
- Session-per-method service precedents: `memory/service.py`,
  `kb/service.py`.
- Migration FK + dialect-portability discipline:
  `alembic/versions/0008_create_broadcast_override.py`.
- Delete cascade onto `scheduled_trigger` (#1480):
  `alembic/versions/0035_scheduled_trigger_fk_cascade.py` — dialect-split
  FK rebuild (PG online `ALTER`, SQLite `batch_alter_table` table
  recreate with a `naming_convention`).
