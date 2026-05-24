# Tenant conventions (Layer 1 server-side rules)

Initiative #229 (G7.1) ships a two-layer tenant-conventions surface.
**Layer 1** is the database-backed table of operational / workflow /
reference rules MEHO auto-loads into every authenticated agent's
session preamble. **Layer 2** is the local `CLAUDE.md` template
consumer repos copy in to teach their local Claude sessions to prefer
MEHO features. This document covers Layer 1 -- the schema, ORM
models, and access patterns. Layer 2 lives in
[docs/examples/consumer-onboarding/](../examples/consumer-onboarding/)
(landed by sibling task #318).

This document is current as of T1 (#313). Sibling tasks T2-T5 will
extend it with the API, CLI, preamble assembler, and seed details as
they land.

## Overview

A **tenant convention** is a single named rule, scoped to one tenant,
that the agent's session preamble incorporates at connect time. Each
convention has:

- a **slug** -- operator-visible identifier (`rbac-canonical`,
  `secret-handling`) used in URLs, CLI commands, and audit log
  references;
- a **title** -- short display label;
- a free-form Markdown **body** -- the rule text the agent sees;
- a **kind** discriminator (`operational` / `workflow` /
  `reference`) -- only `operational` conventions are packed into the
  preamble; the others are reference material the operator surfaces
  on demand;
- a **priority** (`SMALLINT`) -- the ranking key the preamble
  assembler uses to pack highest-priority-first and drop
  lowest-priority entries whole when over the token budget.

Conventions are **per-tenant**. Two tenants can declare the same
slug independently; one tenant cannot have two conventions with the
same slug (enforced by the unique composite index on
`(tenant_id, slug)`).

Every edit writes both a current-state row in `tenant_conventions`
and an audit row in `tenant_convention_history`, in the same DB
transaction.

## Key types

### `tenant_conventions` table

```
id              UUID    PK    -- gen_random_uuid() on PG, uuid4() on SQLite
tenant_id       UUID    NOT NULL
slug            TEXT    NOT NULL
title           TEXT    NOT NULL
body            TEXT    NOT NULL
kind            TEXT    NOT NULL   -- 'operational' | 'workflow' | 'reference'
priority        SMALLINT NOT NULL DEFAULT 0
created_by_sub  TEXT    NULL
created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
```

Indexed by `tenant_conventions_tenant_slug_idx` -- unique composite
btree on `(tenant_id, slug)`.

### `tenant_convention_history` table

```
id              UUID    PK    -- gen_random_uuid() on PG, uuid4() on SQLite
convention_id   UUID    NOT NULL    -- soft FK to tenant_conventions.id
body_before     TEXT    NULL        -- NULL for CREATE events
body_after      TEXT    NOT NULL
actor_sub       TEXT    NOT NULL
ts              TIMESTAMPTZ NOT NULL DEFAULT now()
audit_id        UUID    NULL        -- soft FK to audit_log.id; nullable for seeds
```

Indexed by `tenant_convention_history_convention_idx` -- composite
btree on `(convention_id, ts)`.

### `TenantConvention` (SQLAlchemy 2.x ORM)

Defined in
[`backend/src/meho_backplane/db/models.py`](../../backend/src/meho_backplane/db/models.py).
Standard `Mapped[...]` annotations; no helper methods. T2's CRUD
module is the only writer.

### `TenantConventionHistory` (SQLAlchemy 2.x ORM)

Same module. Write-once, read-mostly. T3's `meho conventions history
<slug>` verb is the only consumer in v0.2.

## Control flow

T1 (this task) ships **schema only** -- no read or write paths exist
yet. The end-to-end flow lands across T2-T5:

1. **POST /api/v1/conventions** (T2 #314) -- `tenant_admin` role
   required. Inserts one row into `tenant_conventions` and one row
   into `tenant_convention_history` (with `body_before=NULL`) inside
   the same transaction. The audit middleware writes its own row
   into `audit_log`; T2 captures the audit row id and stores it in
   the history row's `audit_id` column so G8's audit-replay can
   join.

2. **PATCH /api/v1/conventions/{slug}** (T2) -- `tenant_admin` role
   required. Looks up the existing row by `(tenant_id, slug)`,
   updates `body` (and/or `title`), inserts a history row with the
   previous body in `body_before` and the new body in `body_after`.
   Same single-transaction discipline as POST.

3. **DELETE /api/v1/conventions/{slug}** (T2) -- `tenant_admin` role
   required. Deletes the row from `tenant_conventions`; the history
   row gets `body_after=<final body>` (a legible last-known state)
   rather than a sentinel marker. The lifecycle distinction lives in
   the audit row, not in `tenant_convention_history`.

4. **GET /api/v1/conventions** (T2) -- list all conventions for the
   operator's tenant. Filters by `tenant_id` (resolved from the JWT
   claim by G0.1's contextvar binding).

5. **GET /api/v1/conventions/{slug}** (T2) -- single-row lookup by
   `(tenant_id, slug)`; the unique index makes it a btree probe.

6. **GET /api/v1/conventions/{slug}/history** (T2) -- chronological
   list of history rows for the convention, with optional
   cross-reference to `audit_log` via `audit_id`.

7. **Session preamble** (T4 #316) -- on MCP `initialize`, MEHO loads
   all `kind='operational'` conventions for the tenant, packs them
   highest-priority-first into a budget-bounded Markdown block, and
   emits the result as the spec-optional `instructions` field on the
   `initialize` response. Over-budget entries are dropped whole
   (never mid-entry truncation of an operational rule), and the
   dropped-slug list flows back to callers so `meho conventions
   list` can surface it.

8. **MCP resource** `meho://tenant/<id>/conventions` (T4) -- the same
   data is also exposed as an MCP resource collection. When (and
   only when) `capabilities.resources.subscribe: true`, edits emit
   `notifications/resources/updated` so subscribing clients refresh
   mid-session.

## Dependencies

This task's dependencies (resolved by T1):

- **G0.1-T1 (#231)** -- needs the `tenant` table for `tenant_id`
  column types. Soft FK; no `REFERENCES` clause until v0.2.next.

Downstream consumers (lands in sibling tasks):

- **T2 (#314)** -- Pydantic schemas + 6 API routes.
- **T3 (#315)** -- CLI verbs (`meho conventions list / show / edit /
  history`).
- **T4 (#316)** -- session-preamble assembler + MCP resource.
- **T5 (#317)** -- seed migration for `rdc-internal` tenant.
- **T6 (#318)** -- Layer 2 starter doc
  (`docs/examples/consumer-onboarding/CLAUDE.md`).

## Migration

The schema is materialised by
[`backend/alembic/versions/0015_create_tenant_conventions.py`](../../backend/alembic/versions/0015_create_tenant_conventions.py).
Purely additive (no DROP / RENAME / SET NOT NULL on existing
columns); the
[CI guard](../../scripts/ci/check_migration_compat.py) verifies this.
Reversibility is at the table level: `downgrade()` drops both new
tables and their indexes.

The migration follows the dialect-portability discipline established
by 0001-0014:

- `gen_random_uuid()` server defaults on PG; ORM `default=uuid.uuid4`
  on SQLite.
- `now()` server defaults on PG; ORM `default=lambda: datetime.now(UTC)`
  on SQLite.
- `priority` server default `0` on PG; ORM `default=0` for SQLite.
- Soft FKs throughout (no `REFERENCES` clauses per the issue body's
  explicit choice).

## Known issues

- **No FK enforcement.** Per the issue body's explicit choice, both
  tables use soft FKs (column types match the referenced tables but
  no `REFERENCES ...` clauses). T2's CRUD enforces referential
  integrity at insert time. A v0.2.next tightening migration may
  introduce real FKs once cascade-policy decisions are exercised in
  production -- specifically, what should happen to conventions
  and history rows when a tenant is deleted, and to history rows
  when a convention is deleted.

- **No DB-level enum on `kind`.** Per the issue body's Out of scope,
  `kind` is free-form text; Pydantic at the API layer (T2) bounds it
  to `operational` / `workflow` / `reference`. A regression that
  bypassed the Pydantic layer could land an invalid `kind`; the API
  layer's validation is the single line of defence in v0.2.

- **No content validation.** T1 does not enforce length, format, or
  budget limits on `body`. T2 will add over-budget write-time 422
  validation (a single `operational` convention whose token estimate
  exceeds the preamble budget is rejected at POST/PATCH time, not
  silently dropped at every future preamble assembly).

- **No backref from `Tenant`.** Querying "all conventions for tenant
  X" goes through the application layer, not via a SQLAlchemy
  relationship. Same discipline as the audit-log <-> tenant link;
  see the `Tenant` docstring in
  [`models.py`](../../backend/src/meho_backplane/db/models.py) for
  the rationale.

## References

- Parent Initiative: [#229](https://github.com/evoila/meho/issues/229).
- This task: [#313](https://github.com/evoila/meho/issues/313).
- Existing migration to mirror:
  [`backend/alembic/versions/0001_create_audit_log.py`](../../backend/alembic/versions/0001_create_audit_log.py)
  (dialect portability),
  [`backend/alembic/versions/0002_create_tenant_and_audit_tenant_id.py`](../../backend/alembic/versions/0002_create_tenant_and_audit_tenant_id.py)
  (unique-index discipline).
- Decision #4 (G7 partition):
  [`docs/planning/v0.2-decisions.md`](../planning/v0.2-decisions.md).
- MCP spec 2025-06-18 -- `initialize`:
  https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
  (the spec-optional `instructions` field on the response carries
  the assembled preamble in T4).
- MCP spec 2025-06-18 -- resources:
  https://modelcontextprotocol.io/specification/2025-06-18/server/resources
  (`resources/subscribe` + `notifications/resources/updated` gated on
  `capabilities.resources.subscribe`; the priority-ranked packing
  mirrors the native resource `priority`/`audience` annotation
  model).
