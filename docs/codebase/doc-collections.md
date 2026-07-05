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
  and `backend`. A tenant_admin registers these through the create
  surface (#1739) — `POST /api/v1/doc_collections`, the
  `create_doc_collections` MCP tool, or `meho docs collections create` —
  which validates the `backend.type`, derives `tenant_id` from the JWT,
  defaults `status` to `provisioning`, and audits the write. See
  [Create / register a collection](#create--register-a-collection-1739).
- **Probe-written liveness** — `status`, `last_ingested_at`,
  `doc_count`, and `readiness`. The backend is the source of truth for
  liveness; the readiness probe (G4.6-T6, #1555) writes these from the
  backend on a successful probe. See [Readiness probe + lifecycle](#readiness-probe--lifecycle-g46-t6-1555).

This is registry substrate plus the readiness/lifecycle layer and the
catalogue-discovery surface. The backend-agnostic search router (T2,
#1551) and collection-scoped `search_docs` / `ask_docs` (T3, #1552) build
on this surface but are out of scope here; the `list_doc_collections`
catalogue tool / REST route / CLI verb and the `initialize.instructions`
catalogue band (T4, #1553) are documented under
[Catalogue discovery](#catalogue-discovery-g46-t4-1553).

### Ingest is out of scope by design

Doc collections are **externally curated and prepared for MEHO
elsewhere**; MEHO's role is **attach + search only**. There is
deliberately **no operator/tenant path to ingest document content into a
collection, and none is planned** — the corpus / ops layer owns document
indexing (the managed-RAG bulk import), and MEHO federates the query
afterward. An in-MEHO ingest plane would mean standing up a separate
database + meho-knowledge API deployment, which is out of the substrate
model (dumb substrate, #1177).

The design is federation-only by construction: the only registered
backend is `corpus-http` (federates search / probe to an external
ops-run corpus, `meho_backplane.auth.corpus`), the `SearchBackend`
interface is `search()` + `probe()` only — there is **no `ingest()`
seam** (`docs_search/backends/base.py`) — and `doc_count` /
`last_ingested_at` are **probe-read** from the backend, never
MEHO-written.

Do not confuse **attach** with **ingest**. The create surface
(`POST /api/v1/doc_collections` / `meho docs collections create`, #1739)
registers a *pointer* to an externally-built corpus (`backend.{type,
ref}`) — it stands up no storage and ingests nothing. "Create a
collection" means *attach an existing corpus*, not *populate a new one*.
Asks to add a MEHO-side ingest route were considered and declined as
out-of-architecture (#1864 → Initiative #2049, both closed not-planned).

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

Pydantic-v2 models. The two read models are frozen: `DocCollection` maps
1:1 to the table columns; `DocCollectionSummary` is the short shape for
the catalogue list — it carries the identification + routing-decision
fields plus the operator-facing liveness fields but **omits `backend`**
(the backend is resolved server-side and never appears in a catalogue
response, the backend-agnostic contract from #1548) and `extras`.

The write / response half: `DocCollectionCreate` (#1739) is the create
request body (operator-set identity + routing fields only; `extra="forbid"`
so a smuggled `tenant_id` is rejected), and `DocCollectionCreateResponse`
(#1756) is the REST create reply — `DocCollection` plus a `next_step`
string that names the `create → probe → ready` flow while the row is
`provisioning` (see
[The `create → probe → ready` flow](#the-create--probe--ready-flow-1756)).
`next_step` lives on the create-response model only, never on the shared
`DocCollection` read shape. There is still no `Update` schema — PATCH /
DELETE are a later follow-up.

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
`corpus_status` (a GET to the corpus `/readyz` endpoint — the search
URL's host root plus `/readyz`, derived by `derive_status_url`;
MEHO.Knowledge exposes `/readyz`, not a `/status` sibling, #1732 —
forwarding the operator JWT, bounded by `corpus_timeout_seconds`, failing
closed to one `CorpusUnavailable`). A `/readyz` 200 with no explicit
readiness flag reads as `index_built=True` (the 200 *is* the ready
signal); a body advertising `index_built`/`ready`/`index_ready` `false`
maps to `index_built=False`.
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
search-time readiness rejection is **not** in this module — it lives in
the single `resolve_entitled_ready_collection` access gate (#1567), which
branches on the *kind* of not-ready: `ready` passes, `provisioning` /
`rebuilding` → `CollectionNotReadyError` (409 / `-32603`, retryable),
`disabled` → `CollectionDisabledError` (403 / `-32602`, terminal); any
other value fails closed as not-ready. This `lifecycle` module owns only
the write-side status machine (transitions + probe mapping); the read-side
decision is the access gate's, made exactly once.

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

### `/ready` backend configuredness (`meho_backplane.docs_search.readiness_probe`)

`docs_backends_readiness_probe` is a coarse, synchronous, credential-free
`/ready` check registered in the lifespan. Registered ≠ configured
(#1606): the shipped `corpus-http` adapter self-registers at import even
on deploys that never set `CORPUS_URL`, so the probe filters to the
adapters whose `is_configured()` is true (for `corpus-http`,
`settings.corpus_url` set) and reports `ok=True` unconditionally — the
docs add-on is optional, and a deploy with no docs backend configured
must still become Ready. `detail` carries the configured count (the
observability value of the check). It deliberately does **not** issue a
live round-trip — a corpus outage must not flap the backplane out of
rotation; an unconfigured or unreachable backend instead fails closed at
call time (`CorpusUnavailable` → 503) and at the explicit per-collection
probe route.

### CLI (`cli/internal/cmd/docs/collections.go`)

`meho docs collections list|probe|enable|disable <key>`. `list` (T4
#1553, operator) is the catalogue-discovery verb; `probe` / `enable` /
`disable` (T6 #1555, tenant_admin) are the lifecycle face. All four are
capability-gated like `meho docs search` (hidden + refusing when the
tenant lacks `meho-docs`). `probe` renders the `BackendReadiness` block;
`enable` / `disable` confirm the transition. See
[Catalogue discovery](#catalogue-discovery-g46-t4-1553) for `list`.

## Catalogue discovery (G4.6-T4 #1553)

The discovery face of the catalogue — three sibling fronts on one
backplane plus a session-preamble band — so an agent learns *which*
collections it may search before it searches. Every surface filters to
the collections the operator is **entitled** to: it holds
`meho-docs:<collection_key>` for them (the same per-collection key
`search_docs` enforces at query time), so a listed key is always one
`search_docs` will accept rather than reject with a 403.

### `list_doc_collections` MCP tool (`meho_backplane.mcp.tools.doc_collections`)

`required_role=OPERATOR`, `op_class='read'`,
`required_capability='meho-docs'` — absent from `tools/list` for a tenant
without the add-on, visible once provisioned. The handler reads
`doc_collections` tenant-scoped (global + tenant rows), de-duplicates a
shadowed global key in favour of the tenant row, filters to the entitled
set, and returns `{collections: [...], next_cursor}` keyset-paginated by
`collection_key`. Binds `audit_op_id="meho.docs.collections.list"` (the
`meho.docs.*` family #1549 established). The summary omits the `backend`
record by design (#1548 backend-agnostic contract).

The optional `vendor` filter is applied **after** the tenant-first dedupe
(in Python, over the post-dedupe tenant-wins rows), never in the
pre-dedupe SQL `WHERE` (#1568). A tenant row may shadow a global key under
a *different* vendor; filtering in SQL would drop the tenant row before it
could win and surface the shadowed global row's metadata, so `vendor`
filters the rows the principal actually sees. The keyset cursor
(`collection_key > cursor`) stays in SQL — dedupe and the vendor filter
both preserve `collection_key` ordering, so pagination is unaffected by
the reorder.

### REST `GET /api/v1/doc_collections` (`meho_backplane.api.v1.doc_collections`)

The REST sibling — OPERATOR-gated, same tenant-scope + dedupe +
entitlement filter, keyset by `collection_key`, returns
`list[DocCollectionSummary]`, binds the same canonical audit op_id. An
unprovisioned tenant (no `meho-docs:*` capability) gets an empty list.
`--vendor` is an exact-match query filter.

### CLI `meho docs collections list` (`cli/internal/cmd/docs/collections_list.go`)

The operator-facing verb on the existing `collections` parent, mirroring
`meho targets list` (`--vendor` / `--limit` / `--cursor` / `--json`).
Capability-gated (the typed `addon_not_provisioned` refusal before any
network call); renders a KEY / VENDOR / PRODUCTS / STATUS / DOCS table.

### `initialize.instructions` catalogue band (`meho_backplane.docs_collections.preamble`)

`assemble_doc_catalogue(...)` — the third preamble band, after the tenant
conventions (G7.1-T4 #316) and runbook priming (G12.4-T2 #1316). It
renders a guard-delimited `<<DOC_COLLECTIONS_AVAILABLE>> … >>` block
listing the operator's entitled collections so an agent can pick a
`collection` from the session preamble. Threaded into
`assemble_preamble` / `assemble_preamble_detailed` via an **optional**
`capabilities` keyword (the MCP `initialize` path passes
`operator.capabilities`; the conventions write path omits it). Returns
`text=""` — and the assembler omits the band — when the operator is
entitled to no collection, so a non-docs tenant's preamble is
**byte-identical** to its pre-T4 shape. The band is independently
token-capped (`MAX_CATALOGUE_TOKENS`); over-budget it renders a summary
form pointing at `list_doc_collections` and logs
`doc_catalogue_band_over_budget`, mirroring the priming band. The guard
delimiters are wrapper-emitted (never substituted from row content), so a
malicious `when_to_use` carrying the terminator cannot escape the block.

## Create / register a collection (#1739)

The write half of the registry, mirroring the `create_target` precedent
(`backend/src/meho_backplane/api/v1/targets.py`). It closes the gap Ops
hit on the first co-located corpus round-trip
(claude-rdc-hetzner-dc#975): before #1739 the only way to register a
collection was a raw `INSERT INTO doc_collections`, which bypassed every
guardrail the rest of MEHO's registry writes enforce (backend
routability, tenant scoping, typed conflict, audit).

The service primitive
`create_doc_collection(session, operator, body)`
(`meho_backplane.docs_collections.service`) owns the substrate:

- **`tenant_id` from the operator**, never the body — a cross-tenant
  create is structurally impossible (the `DocCollectionCreate` schema is
  `extra="forbid"`, so a body carrying a `tenant_id` is rejected).
- **`backend.type` validated against the registry** —
  `docs_search.backends.registry.all_backends()` is the source of truth;
  an unregistered type raises `DocCollectionBackendTypeError`, surfaced as
  a structured `422` whose detail enumerates the registered types (the
  `create_target` unknown-product shape). This moves the failure forward
  from a deferred probe/search-time `503` to create time.
- **Server-derived fields** — `id` / `created_at` / `updated_at` are
  generated; `status` defaults to `provisioning` (a follow-up `probe`
  promotes it); the probe-written liveness stays `NULL`.
- **Typed conflict** — a cross-scope `collection_key` collision (either
  partial-unique index) maps `IntegrityError` →
  `DocCollectionConflictError` → `409`, not an opaque `500`.
- **Audit** — binds `op_id="meho.docs.collections.create"` /
  `op_class="write"`, so the registration joins the `op_id="meho.docs.*"`
  who-touched trail every other docs operation shares.

Three fronts forward to the same primitive:

- **REST** — `POST /api/v1/doc_collections` → `201` with the full
  `DocCollection` read shape plus a `next_step` hint
  (`DocCollectionCreateResponse`, #1756; see
  [The `create → probe → ready` flow](#the-create--probe--ready-flow-1756)),
  `tenant_admin`-gated (parity with the lifecycle routes). The route maps
  the two service exceptions to `422` / `409`.
- **MCP** — the `create_doc_collections` write-class tool
  (`meho_backplane.mcp.tools.doc_collections_create`), `tenant_admin` +
  `required_capability="meho-docs"`. The exceptions map to JSON-RPC
  `INVALID_PARAMS` with the structured detail in `error.data`. The tool's
  description already names the probe-to-`ready` step, so it returns the
  bare created collection (no `next_step` field — the REST hint is the
  surface that lacked the cue).
- **CLI** — `meho docs collections create <key> --vendor … --backend-type …
  --backend-ref '{…}'` (or `--from-file <path>`), in
  `cli/internal/cmd/docs/collections_create.go`.

### The `create → probe → ready` flow (#1756)

A created collection is `provisioning`, and the search-time access gate
(`resolve_entitled_ready_collection`) rejects a non-`ready` collection with
`CollectionNotReadyError`. There is no update API and create never
self-probes (auto-promotion is out of scope — it couples to ingest cost),
so **`create → probe → ready` is the only path to a searchable
collection**:

1. `POST /api/v1/doc_collections` registers the row at
   `status=provisioning`.
2. `POST /api/v1/doc_collections/{collection_key}/probe` reads the
   backend's readiness and, on a built index, promotes the row to `ready`.
3. `search_docs` / `ask_docs` now accept the collection.

Before #1756 the create response carried the registry state but no cue
that a probe was required, so an operator hit a confusing not-ready error
on the first search. The REST create response now carries a `next_step`
string (`DocCollectionCreateResponse.next_step`) naming the exact probe
route + `collection_key` to call while the row is `provisioning`
(`None` for any other create status — no path produces one today). The
hint is **create-surface-only**: it lives on `DocCollectionCreateResponse`
(built by `project_doc_collection_create_response`), not on the shared
`DocCollection` read model, so every read surface that returns the bare
read shape (the collection-scoped search path, the `list_doc_collections`
catalogue, the MCP docs tools) is unchanged. The route's `201` OpenAPI
`description` names the same flow.

Out of scope (a later follow-up): PATCH / DELETE, cross-tenant sharing,
and bulk import beyond the single-collection `--from-file` on-ramp.
Auto-promotion / self-probe on create stays out of scope (#1756) — it is a
behaviour change with ingest-cost implications; #1756 is discoverability
only.

## Global-row manifest seed (#1920)

The global (`tenant_id IS NULL`) `vmware` row is provisioned **out-of-band**
(operator DB seed) — the create surface (#1739) always scopes a row to the
caller's tenant, so the shared corpus every tenant sees is never created
through it. #1916's corpus-aware query expansion
(`docs_search.expansion._render_manifest_for_prompt`) injects the
collection's `vendor` / `products` / `description` / `when_to_use` into the
expansion prompt and **omits empty optional fields**, so a `vmware` row
seeded without prose contributes only `collection` / `vendor` / `products`
lines and the model expands on a thin manifest.

Migration `0048` (`backend/alembic/versions/0048_seed_vmware_collection_manifest.py`)
closes that gap. It is a data migration (no schema change) with three
load-bearing properties:

- **`UPDATE`, never `INSERT`.** `backend` is NOT NULL with no default and is
  deploy-specific (the corpus endpoint / RAG corpus path), so a migration
  cannot author a complete row. It enriches the operator-seeded global row;
  on a deploy where the row does not exist yet the `UPDATE` matches zero
  rows — a clean no-op (an unregistered corpus has no manifest to fill).
- **Global scope only.** Narrowed to `collection_key = 'vmware' AND
  tenant_id IS NULL`; a tenant-curated `vmware` row is the operator's own
  content and is left untouched.
- **Fill-only.** `description` / `when_to_use` / `products` are written only
  where the row's current value is empty (the operator-content-wins
  discipline migrations `0018` / `0028` follow); `vendor` (NOT NULL) is only
  upgraded from the bare `vmware` seed shorthand to the catalogue display
  string `VMware by Broadcom`. Re-running is therefore idempotent, and
  `downgrade()` clears only the prose it authored (an operator edit made
  after the seed survives the rollback).

The seeded prose is **hand-authored**, not auto-summarised from a chunk
sample at ingest (substrate stays dumb, #1177; auto-summarisation is
explicitly out of scope per #1920).

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
- **The search-time status check is the access gate's, not a separate
  guard.** `resolve_entitled_ready_collection` (#1552, #1567) is the single
  place readiness is decided on the search path — it resolves the key,
  enforces entitlement, and rejects a not-ready collection, branching the
  terminal `disabled` (403 / `-32602`) from the transient `provisioning` /
  `rebuilding` (409 / `-32603`). There is no second `ensure_collection_searchable`
  duplicate.
- **Create is the write half; update / delete / sharing are not.** A
  tenant_admin registers a collection through the create surface (#1739,
  see [Create / register a collection](#create--register-a-collection-1739));
  there is still no PATCH / DELETE and no cross-tenant sharing API — those
  are a later follow-up (out of scope per #1739). A shared/global row
  (`tenant_id IS NULL`) is still seeded out-of-band, since the create
  always scopes to the caller's tenant. A **data migration** is the
  in-repo mechanism for enriching such a global row's metadata — migration
  `0048` fills the global `vmware` row's `description` / `when_to_use`
  (and a canonical `vendor` / `products` when still unset) so #1916's
  corpus-aware query expansion has a manifest to read; see
  [Global-row manifest seed](#global-row-manifest-seed-1920) below. A
  migration cannot author the NOT-NULL `backend` routing record (it is
  deploy-specific), so it `UPDATE`s the operator-seeded row rather than
  inserting one.
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
