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

This document is current as of T3 (#315). Sibling tasks T4-T5 will
extend it with the preamble assembler and seed details as they land.

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

T2 (this task) ships the **6 HTTP routes** + Pydantic schemas. T1
shipped the schema; T3-T5 layer CLI / preamble / seed on top.

1. **POST /api/v1/conventions** (T2 #314) -- `tenant_admin` role
   required. Inserts one row into `tenant_conventions` and one row
   into `tenant_convention_history` (with `body_before=NULL`) inside
   the same transaction. The audit middleware writes its own row
   into `audit_log`; the route handler pre-allocates the audit row's
   uuid via
   [`bind_preallocated_audit_id`](../../backend/src/meho_backplane/audit.py)
   so the middleware reuses it, and the history row's `audit_id`
   soft-FK references that same uuid -- a single audit row joins
   to the history row by exact-match uuid.

2. **PATCH /api/v1/conventions/{slug}** (T2) -- `tenant_admin` role
   required. Looks up the existing row by `(tenant_id, slug)`,
   updates `body` (and/or `title` / `priority`), inserts a history
   row with the previous body in `body_before` and the new body in
   `body_after`. Same single-transaction discipline as POST.
   Priority-only or title-only PATCHes still write a history row
   (the operation happened; the diff trail is the causal record).

3. **DELETE /api/v1/conventions/{slug}** (T2) -- `tenant_admin` role
   required. Inserts a history row with `body_after=<final body>`
   (a legible last-known state for audit forensics) before deleting
   the convention row from `tenant_conventions`. The lifecycle
   distinction lives in the audit row's `method='DELETE'`, not in
   `tenant_convention_history`.

4. **GET /api/v1/conventions** (T2) -- list all conventions for the
   operator's tenant. Filters by `tenant_id` (resolved from the JWT
   claim by G0.1's contextvar binding). Optional `?kind=operational`
   query param mirrors the CLI's `meho conventions list --kind`
   verb. Ordering is `priority DESC, created_at ASC` -- the same
   key T4's preamble assembler will use, so the list view surfaces
   conventions in the order T4 considers them.

5. **GET /api/v1/conventions/{slug}** (T2) -- single-row lookup by
   `(tenant_id, slug)`; the unique index makes it a btree probe.

6. **GET /api/v1/conventions/{slug}/history** (T2) -- list of
   history rows for the convention ordered `ts DESC` (newest first
   per the issue's "documented v0.2 ordering" decision), with
   optional cross-reference to `audit_log` via `audit_id`.

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

## Write-time 422 validation (T2)

`POST /api/v1/conventions` and `PATCH /api/v1/conventions/{slug}`
both run a write-time over-budget gate: if the submitted body is
`kind='operational'` and its token estimate alone exceeds the
preamble budget (`DEFAULT_MAX_PREAMBLE_TOKENS = 600`), the route
rejects with **422** + a detail message naming `estimated` vs
`budget`. PATCH evaluates against the **existing** kind (the
PATCH surface deliberately cannot change `kind` -- see
[`ConventionUpdate`](../../backend/src/meho_backplane/conventions/schemas.py)).

The estimator is `meho_backplane.conventions.schemas.estimate_tokens`
-- a chars-per-token heuristic (`ceil(len / 3.3)`) the
`v0.1-spec §"Memory / context layer"` lines 457-487 baselines. T4
(#316) reuses the same function for its priority-ranked packer,
so the two sites cannot drift -- a divergence would silently let a
write through the API only for the preamble packer to drop it at
every future assembly (the "`kubectl apply --dry-run=server`
discipline" the issue body names).

`workflow` and `reference` conventions are not preamble-bound and
are exempt from the 422 -- a `workflow` convention of arbitrary
size is accepted.

## Audit row + history row in one transaction (T2)

Every write route (POST / PATCH / DELETE) writes:

1. The convention mutation (INSERT for POST, UPDATE for PATCH,
   INSERT-history-then-DELETE for DELETE).
2. One `tenant_convention_history` row carrying the
   `(body_before, body_after, actor_sub, ts, audit_id)` tuple.
3. The chassis `audit_log` row (the
   [`AuditMiddleware`](../../backend/src/meho_backplane/audit.py)
   inserts this after the handler returns).

All three commit or roll back together: the convention mutation +
history row land in the same `session.begin()` block opened by
`get_session`; the audit row commits in the same response cycle
via the middleware. The history row's `audit_id` soft-FK
references the audit row by exact-match uuid -- the route handler
pre-allocates the uuid via `bind_preallocated_audit_id` and the
middleware honors the contextvar instead of minting its own. G8's
audit-query path joins `tenant_convention_history` on `audit_log`
by `audit_id` to answer "who edited which rule when" with one
SQL join.

The pre-allocation primitive is a small, additive chassis change
(opt-in contextvar; when unset, the middleware falls back to the
v0.1 fresh-uuid behaviour). The alternative -- having the route
write its own audit row (the topology-nodes pattern) -- would
double-audit because the middleware also fires per HTTP request.

## CLI surface (T3)

T3 (#315) ships the `meho conventions ...` cobra subcommand tree
[`cli/internal/cmd/conventions/`](../../cli/internal/cmd/conventions/).
Each verb wraps exactly one T2 route; the audit log row + history
row are written server-side, so the CLI is a thin HTTP client over
the same JWT auth + bearer-refresh path the sibling `meho kb` /
`meho agent` trees use.

Six verbs:

- **`meho conventions list [--kind K] [--json]`** -- GET
  `/api/v1/conventions`. Renders a `SLUG | KIND | PRIORITY |
  UPDATED | TITLE` table by default; `--json` emits the raw
  `ConventionListResponse` envelope. `--kind` narrows by
  `operational | workflow | reference` (CLI-side validation
  rejects typos before the round-trip).
- **`meho conventions show <slug> [--json]`** -- GET
  `/api/v1/conventions/{slug}`. Writes the Markdown body to stdout
  for `glow` / `bat -l md` pipelines; `--json` wraps the full
  `Convention` shape.
- **`meho conventions create --slug S --kind K --title T --body @file
  [--priority N] [--json]`** -- POST `/api/v1/conventions`. `--body`
  accepts inline text, `@<path>` to read a file, or `@-` for stdin;
  the realistic shape is `@<path>` with a Markdown rule file. A
  duplicate `(tenant, slug)` returns 409 with detail
  `convention_already_exists`; an over-budget operational body
  returns 422 with `estimated=X, budget=Y` surfaced verbatim.
  `--priority` is omitted from the JSON body when unset so the
  backend's default-0 server_default applies.
- **`meho conventions edit <slug> [--title T] [--body @file]
  [--priority N] [--json]`** -- PATCH `/api/v1/conventions/{slug}`,
  two modes:
  1. **Flag-driven PATCH** (any of `--title` / `--body` /
     `--priority` set) -- sends only the explicitly-set fields,
     mirroring pydantic's `model_fields_set` semantics on the
     backend.
  2. **`$EDITOR` interactive** (no field flag set) -- fetches the
     current body (GET `/api/v1/conventions/{slug}`), opens
     `$EDITOR` (or `$VISUAL`, fallback `vi`) on a `.md` tempfile
     seeded with that body, and submits the saved content as a
     `body`-only PATCH. Editor failure, empty saved buffer, or an
     unchanged save aborts without an API call. A 422 over-budget
     response surfaces inline (the operator sees `estimated=X,
     budget=Y` before the buffer is discarded -- so they can
     re-edit and retry without losing the work).
- **`meho conventions delete <slug> [--confirm] [--json]`** --
  DELETE `/api/v1/conventions/{slug}`. y/N prompt on stdin by
  default; `--confirm` skips for scripted use. The substrate's
  `body_after=<final body>` write into history preserves the
  deleted convention for audit forensics.
- **`meho conventions history <slug> [--limit N] [--json]`** -- GET
  `/api/v1/conventions/{slug}/history`. Renders unified-diff
  rendering of `body_before` -> `body_after` per row (the diff
  shows what changed in that single edit; the CREATE row has no
  body_before and renders the initial body as a `+`-block).
  `--limit N` is a client-side cap; the route returns the full
  trail. `--json` emits the raw history rows for `jq` pipelines or
  for piping into a real `diff -u` if the unified view's
  presence-set diff (a simplified renderer; see code comment for
  why we don't ship Myers) isn't precise enough.

Exit codes mirror the sibling verb trees:

- `0` -- ok (including zero rows on `list`, declined prompt on
  `delete`, no history on `history`).
- `2` -- `auth_expired` (no stored token, refresh failed, bearer
  rejected after refresh).
- `3` -- `unreachable` (transport error against the backplane).
- `4` -- `unexpected_response` (4xx / 5xx -- includes 404
  `convention_not_found`, 409 `convention_already_exists`, 422
  invalid / over-budget).
- `5` -- `insufficient_role` (403 on write verbs without the
  `tenant_admin` claim; the backend's detail naming the required
  role surfaces in the error message).

The CLI does **not** generate or mutate audit_log rows itself --
those land server-side from the T2 routes via the audit middleware.

**Dropped-slug warning (deferred to T4).** The issue body's
acceptance criterion for `list` to surface "lowest-priority slugs
that will be dropped when the tenant's operational set exceeds the
preamble budget" depends on T4's preamble assembler returning
`dropped_slugs` as part of an assembly pass. The T2 list endpoint
returns only the `ConventionListResponse` envelope (entries +
forward-compat fields); there is no `dropped_slugs` field on the
GET surface today, so the CLI cannot synthesise the warning from a
list call alone. T4 will either (a) add a query param to the list
endpoint that runs a dry-run packing pass and returns
`dropped_slugs`, or (b) expose a separate
`GET /api/v1/conventions/preamble?dry_run=true` endpoint the CLI
can call after `list`. The CLI verb's structural code path is
ready -- once T4 ships the API surface, wiring the warning is a
small additive PR (see `printOverBudgetWarning` placeholder
discussion in `list.go`'s docstring).

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

- **Content validation lands at the API layer (T2), not the DB
  layer.** T2's
  [`ConventionCreate`](../../backend/src/meho_backplane/conventions/schemas.py)
  bounds `slug` to lowercase-ASCII + hyphen (URL-safe), `title` to
  200 chars, `body` to 64 KB, `priority` to the SmallInteger
  range; over-budget single-entry `operational` rejection happens
  here too. A row reaching the DB through any other path (a future
  CLI tool, a migration, manual psql) bypasses these gates --
  v0.2.next may add CHECK constraints to the table once the
  validation contract has settled across all callers.

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
