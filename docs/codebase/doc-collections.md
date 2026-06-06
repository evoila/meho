# doc_collections registry (collections-as-data)

## Overview

The `doc_collections` registry is the catalogue substrate of the G4.6
doc-collection catalogue (Initiative #1548). It answers the question
"which documentation corpora can an agent search?" the same way the
`targets` registry answers "which infrastructure can an agent act on?".
One row per corpus (e.g. `vmware`); the row binds a stable
`collection_key` to a backend `{type, ref}` routing record and carries
the metadata an agent needs to pick a collection before searching.

The registry deliberately splits two kinds of field by who writes them
— the same split `targets` rows + `Target.fingerprint` use:

- **Operator-set, authoritative for identity + routing** —
  `collection_key`, `vendor`, `products`, `description`, `when_to_use`,
  and `backend`. These are seeded by an operator (no create/import API
  in v1; operator-managed seed only).
- **Probe-written liveness** — `status`, `last_ingested_at`,
  `doc_count`, and `readiness`. The backend is the source of truth for
  liveness; a later probe (G4.6-T6, #1555) writes these from the
  backend. This task ships the columns; nothing writes them yet.

This is registry substrate only. The backend-agnostic search router
(T2, #1551), collection-scoped `search_docs` / `ask_docs` (T3, #1552),
the `list_doc_collections` catalogue tool and CLI (T4, #1553), and the
readiness probe (T6, #1555) all build on this surface but are out of
scope here. There is no agent-facing surface yet.

## Key types

### `DocCollection` ORM (`meho_backplane.db.models`)

`__tablename__ = "doc_collections"`. Mirrors the `Target` /
`OperationGroup` scaffolding:

- `id` UUID PK; `tenant_id` UUID **nullable** — `NULL` for a global /
  shared collection (every tenant sees it), set for a tenant-curated
  collection.
- `collection_key` Text — the stable operator-chosen id and the binary
  routing + entitlement key the agent passes as `collection=<key>`.
- `vendor` Text; `products` array (`_PORTABLE_ARRAY`: native `TEXT[]`
  on PostgreSQL, JSON array on SQLite); `description` / `when_to_use`
  Text. `when_to_use` mirrors `OperationGroup.when_to_use` — a blurb the
  catalogue tool returns verbatim so an agent picks a collection before
  searching.
- `backend` JSON (`_PORTABLE_JSON`) `= {type, ref}` — the T2 router key,
  operator-set, server-side-only.
- `status` Text CHECK `IN ('provisioning', 'ready', 'rebuilding',
  'disabled')` — the lifecycle enum (copies the `OperationGroup.
  review_status` portable-enum shape); `last_ingested_at` timestamptz;
  `doc_count` int; `readiness` JSON — probe-written liveness, all NULL
  until the probe runs.
- `extras` JSON forward-compat escape hatch; `created_at` /
  `updated_at` timestamptz.

Uniqueness on `collection_key` is enforced by **two partial unique
indexes** (the `OperationGroup` idiom): `doc_collections_global_idx`
`WHERE tenant_id IS NULL` on `(collection_key)`, and
`doc_collections_tenant_idx` `WHERE tenant_id IS NOT NULL` on
`(tenant_id, collection_key)`. A single `UNIQUE (tenant_id,
collection_key)` would not catch two global rows sharing a key — SQL's
`NULL != NULL` semantics mean any number of `tenant_id IS NULL` rows
with the same key would commit. The split lets a global `vmware` and a
tenant-curated `vmware` coexist (the resolver prefers the tenant row).

### `DocCollection` / `DocCollectionSummary` (`meho_backplane.docs_collections.schemas`)

Frozen Pydantic-v2 read models. `DocCollection` maps 1:1 to the table
columns. `DocCollectionSummary` is the short shape for the catalogue
list — it carries the identification + routing-decision fields plus the
operator-facing liveness fields but **omits `backend`** (the backend is
resolved server-side and never appears in a catalogue response, the
backend-agnostic contract from #1548) and `extras`.

There are no `Create` / `Update` write schemas — v1 collections are
operator-managed seed. When an import API lands it adds them here.

### `project_doc_collection_to_summary(...)` (`meho_backplane.docs_collections.schemas`)

The single ORM→wire projection, mirroring
`targets.schemas.project_target_to_summary`. Every surface that lists
collections (catalogue tool, CLI verb, resolver diagnostics) goes
through it so list and detail never drift. Coerces `products` from the
ORM's mutable `list[str]` to the frozen schema's `tuple[str, ...]`.

### `resolve_doc_collection(session, collection_key, tenant_id)` (`meho_backplane.docs_collections.resolver`)

The single entry point that turns an operator-supplied `collection` key
into the registry row that binds it to a backend. **Tenant-first
fallback**: pulls the tenant-curated row and the global row for the key
in one query, prefers the tenant row, falls back to the global row. A
tenant override of a shared collection's backend binding or metadata is
honoured without renaming the key. Mirrors the global-vs-tenant
visibility the operation-registry lookups enforce in
`meho_backplane.operations._lookup`.

An unknown key raises `DocCollectionNotFoundError` (extends
`fastapi.HTTPException`, status 404) carrying the catalogue of keys
visible to the tenant (`detail["known_keys"]`) so the caller can render
suggestions without a second query.

## Control flow (resolution)

1. A caller (T3's collection-scoped `search_docs`, T4's catalogue) holds
   a `collection` key string and the operator's `tenant_id`.
2. `resolve_doc_collection` queries
   `collection_key = :key AND (tenant_id = :tenant OR tenant_id IS NULL)`,
   prefers the tenant row, returns the `DocCollection` ORM row.
3. The caller reads `row.backend` to route the query server-side (T2)
   and `row.status` / `row.readiness` to fail typed against a not-ready
   collection (T3).
4. An unknown key raises `DocCollectionNotFoundError` with the visible
   keys for a "did you mean…?" diagnostic.

## Dependencies

- SQLAlchemy 2.0 typed ORM (`Mapped[...]` / `mapped_column`), the
  `_PORTABLE_JSON` / `_PORTABLE_ARRAY` dialect-portable column aliases
  in `db/models.py`, and the partial-unique-index `postgresql_where` /
  `sqlite_where` pair.
- Pydantic v2 frozen models (`ConfigDict(frozen=True)`).
- Alembic migration `0037_create_doc_collections` (`down_revision =
  '0036'`) — dialect-portable `create_table` + partial-index emission,
  symmetric downgrade, runs clean on both SQLite and PostgreSQL.
- `fastapi.HTTPException` for the typed not-found, `structlog` for
  resolution logging.

## Known issues / boundaries

- **No liveness writer yet.** `status` defaults to `provisioning` and
  the probe-written fields are NULL until G4.6-T6 (#1555) lands the
  readiness probe. A consumer must not assume a collection is
  answerable from registry presence alone — that is what `status` /
  `readiness` are for.
- **No write API.** Collections are operator-managed seed for v1. An
  `import` verb (mirroring `meho targets import`) is a later add if a
  collection needs one.
- **Entitlement is not enforced here.** The per-collection capability
  gate (`meho-docs:<collection>`) and the `audit_collection` binding
  land in T3 (#1552); the registry only stores and resolves rows.

## References

- Mirror: `Target` ORM and `OperationGroup` global+tenant +
  `review_status` in `backend/src/meho_backplane/db/models.py`;
  `targets/{schemas,resolver,__init__}.py`.
- Migration precedent: `0004_create_targets_and_audit_target_id.py`
  (dialect-portable create_table) and `0005_create_endpoint_descriptor.py`
  (partial-unique-index emission).
- Initiative #1548; this task #1550. Predecessor add-on:
  [docs-search.md](docs-search.md) (G4.5 single-corpus `meho-docs`).
