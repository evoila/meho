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
  liveness; the readiness probe (G4.6-T6, #1555) writes these from the
  backend on a successful probe. See [Readiness probe + lifecycle](#readiness-probe--lifecycle-g46-t6-1555).

This is registry substrate plus the readiness/lifecycle layer. The
backend-agnostic search router (T2, #1551), collection-scoped
`search_docs` / `ask_docs` (T3, #1552), and the `list_doc_collections`
catalogue tool and CLI (T4, #1553) build on this surface but are out of
scope here.

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

## Readiness probe + lifecycle (G4.6-T6 #1555)

The liveness layer that makes the catalogue carry **backend readiness**
so the router hides managed-RAG operational footguns from the agent. A
managed-RAG ANN index answers searches only once it has been explicitly
(re)built, and rebuilds serialize per project; the probe reflects that
state onto the row so the search path can fail typed instead of returning
a silent empty result.

### The `probe()` seam (`meho_backplane.docs_search.backends`)

`SearchBackend.probe(operator, *, backend_ref=None) -> BackendReadiness`
is the readiness sibling of `search()`. The row's `backend{type, ref}` is
resolved to `backend_ref` by the router before the call, so the adapter
depends only on the backend routing detail, never on the ORM shape. The
base method raises `NotImplementedError` (an adapter without a liveness
check fails loud rather than claiming "ready").

`BackendReadiness` (frozen Pydantic) carries `reachable`, `index_built`,
`doc_count`, `last_ingested_at`, and a free-form `detail` mapping the
`readiness` column stores verbatim. `index_built=False` is the
managed-RAG footgun: reachable but the index is not yet answerable.

`CorpusHttpBackend.probe` reads the corpus's readiness via
`corpus_status` (a GET to the corpus `/status` endpoint derived from the
search URL by `derive_status_url`, forwarding the operator JWT, bounded
by `corpus_timeout_seconds`, failing closed to one `CorpusUnavailable`).
The **per-project rebuild serialization** lives inside the adapter: a
`defaultdict[str, asyncio.Lock]` keyed on the resolved corpus endpoint,
held across the corpus round-trip, so two concurrent probes against the
same project's backend serialize while different projects run
concurrently. This is in-adapter, not a substrate scheduler (substrate
minimalism, #1177); the serialized state surfaces via `status='rebuilding'`.

### The lifecycle state machine (`meho_backplane.docs_collections.lifecycle`)

The `status` column's four states form a guarded machine:

- **Probe transitions** (`PROBE_TRANSITIONS`): `provisioning` →
  `{ready, rebuilding}`, `ready` → `rebuilding`, `rebuilding` → `ready`.
  A probe never touches a `disabled` row — operator intent outranks a
  liveness signal. `status_for_readiness` maps a `BackendReadiness` to
  the target status (index built → `ready`, else `rebuilding`).
- **Operator transitions** (`OPERATOR_TRANSITIONS`): `disable` from any
  live state; `enable` from `disabled` back to `provisioning` (a probe
  then promotes it). A same-state re-call is the idempotent no-op.

A forbidden move raises `DocCollectionStateError` (HTTP 409). The
search-time guard `ensure_collection_searchable(collection_key, status)`
is the **mechanism T3 (#1552) wires into the search path**: `ready`
passes, `provisioning` / `rebuilding` → `DocCollectionNotReadyError`
(409, retryable), `disabled` → `DocCollectionDisabledError` (403); an
unknown status fails closed as not-ready. T6 ships the guard; T3 calls it.

### The probe write-back service (`meho_backplane.docs_collections.service`)

`probe_collection(session, operator, collection)` resolves the backend,
reads `BackendReadiness`, and — **on success only** — writes `readiness`
/ `doc_count` / `last_ingested_at` and transitions `status`. A raising
probe leaves the row untouched (the route's `session.begin()` rolls back,
the `probe_target` / `Target.fingerprint` write-back split).
`set_collection_enabled(session, collection, enabled)` is the
guarded, idempotent enable/disable transition.

### REST routes (`meho_backplane.api.v1.doc_collections`)

Three **tenant_admin-gated** routes (mirroring the connector
enable/disable gate):

- `POST /api/v1/doc_collections/{key}/probe` → 200 `BackendReadiness`
  (row written back), 404 unknown key, 409 forbidden transition, 503
  backend unavailable (row untouched).
- `POST /api/v1/doc_collections/{key}/enable` / `.../disable` → 204,
  idempotent, 409 on a forbidden move.

### `/ready` backend reachability (`meho_backplane.docs_search.readiness_probe`)

`docs_backends_readiness_probe` is a coarse, synchronous, credential-free
`/ready` check registered in the lifespan: it reports `ok` only when
every registered adapter's `is_configured()` is true (for `corpus-http`,
`settings.corpus_url` set), naming the unconfigured ones. It deliberately
does **not** issue a live round-trip — that is the explicit per-collection
probe route's job.

### CLI (`cli/internal/cmd/docs/collections.go`)

`meho docs collections probe|enable|disable <key>` — the lifecycle face
of the catalogue, tenant_admin-gated and capability-gated like
`meho docs search`. `probe` renders the `BackendReadiness` block;
`enable` / `disable` confirm the transition. The catalogue `list` verb is
a sibling Task (T4 #1553) that adds to this same parent command.

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

- **Liveness is probe-written, not auto-refreshed.** The probe route
  (G4.6-T6 #1555) writes `status` / `last_ingested_at` / `doc_count` /
  `readiness`, but only when an operator (or ops automation) calls it —
  there is no background poller. A consumer must not assume a collection
  is answerable from registry presence alone; the cached `status` /
  `readiness` reflect the *last* probe.
- **Ingest / rebuild is not triggered here.** The probe reflects the
  backend's state; the heavy ingest / index rebuild is the backend / ops
  side (out of scope per #1555). `enable` returns a collection to
  `provisioning`, not to `ready` — a probe confirms the index before
  `ready`.
- **The search-time status check is wired by T3.** T6 ships
  `ensure_collection_searchable`; the call site in the `search_docs`
  route is T3's (#1552). Until T3 lands, the search path does not yet
  fail typed on a not-ready collection.
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
- Initiative #1548; registry task #1550; readiness/lifecycle task #1555.
  Predecessor add-on: [docs-search.md](docs-search.md) (G4.5
  single-corpus `meho-docs`).
- Probe write-back precedent: `probe_target` + `Target.fingerprint`
  (`backend/src/meho_backplane/api/v1/targets.py`); lifecycle / 409
  transition precedent: connector enable/disable
  (`api/v1/connectors_ingest.py`, `InvalidStateTransitionError`);
  `/ready` probe registry: `backend/src/meho_backplane/health.py`.
