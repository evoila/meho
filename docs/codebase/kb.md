# `meho_backplane.kb` — tenant-scoped knowledge base service

> Durable map of the knowledge-base subsystem. Update in lock-step
> with code changes; stale entries are bugs.

## Overview

The knowledge-base (`kb`) service is the persistent, tenant-scoped
durable-team-knowledge layer. It backs three transports:

- **REST** under `/api/v1/kb/...` (G4.1-T2).
- **MCP** via the `search_knowledge` + `add_to_knowledge` meta-tools
  in [`mcp/tools/knowledge.py`](../../backend/src/meho_backplane/mcp/tools/knowledge.py)
  (G4.1-T3).
- **CLI** through the `meho kb` verb (G4.1-T4).

All three transports funnel through one
[`KbService`](../../backend/src/meho_backplane/kb/service.py)
instance. Rows live in the shared `documents` table under
`source = 'kb'` — the same table that backs `memory` and any future
indexed corpus — which lets hybrid BM25 + cosine retrieval span the
read path uniformly. The kb-shaped vocabulary (slug, snippet,
`KbEntry`) is owned by this package; the substrate's `(tenant_id,
source, source_id)` natural-key contract is enforced inside the
service so every later transport stays consistent.

The kb is **durable team knowledge** in contrast to `memory` which is
ephemeral session / policy state — the two-system split is
deliberate, and the MCP tool descriptions teach the model when to
write to which (`add_to_knowledge` for generalizable runbooks /
vendor API patterns / post-incident learnings; `add_to_memory` for
session notes and per-target settings).

## Key types

### `KbEntry` (`kb/schemas.py`)

Frozen Pydantic value object that mirrors one `documents` row in
kb-shaped vocabulary: `id`, `tenant_id`, `slug` (renamed from
`source_id`), `body`, `metadata`, `created_at`, `updated_at`. The
substrate columns the kb does not expose (`source`, `kind`,
`body_hash`, `tokens`, `embedding`) stay invisible to callers. Write
attribution rides `metadata` as `created_by_sub` /
`last_updated_by_sub` (see "Cross-principal write & delete semantics"
below) rather than as dedicated columns — no schema migration, and the
keys surface on every read surface that already returns `metadata`.

### `KbEntrySearchHit` (`kb/schemas.py`)

Frozen wrap around the substrate's `RetrievalHit` with the
kb-shaped vocabulary applied (`slug` instead of `source_id`,
`snippet` instead of full body) plus the per-signal retrieval
scores (`fused_score`, `bm25_score`, `cosine_score`, `bm25_rank`,
`cosine_rank`) so callers tuning retrieval can observe the ranking
signals. `snippet` is the first ~200 characters of the body
(substrate constant `_SNIPPET_CHARS`); the full body is recoverable
through `KbService.get_entry(slug)`.

### `KbIngestionResult` (`kb/schemas.py`)

Summary of one `ingest_directory` run. Four counters that partition
every discovered `.md` file into exactly one bucket: `inserted_count`,
`updated_count`, `skipped_count`, `error_count`, plus `errors` (the
list of per-file failure messages).

### `InvalidKbSlugError` (`kb/schemas.py`)

Subclass of `ValueError` raised by `validate_slug` when a slug fails
the `SLUG_PATTERN` shape rule (lowercase letter start, kebab-case,
ends with letter or digit). The route layer (T2) catches it and
translates to HTTP 422; the MCP tool (T3) catches it and re-raises
as `McpInvalidParamsError` (JSON-RPC `-32602`); the CLI (T4) catches
it and exits non-zero.

### `KbService` (`kb/service.py`)

Stateless, method-scoped service. Every public method opens its own
`AsyncSession` via `get_sessionmaker()` and commits synchronously
before returning. Six verbs:

| Verb | What it does | Throws |
|---|---|---|
| `create_entry(tenant_id, slug, body, metadata=None, *, actor_sub=None)` | Insert or re-index one entry; returns `(entry, created)`. Validates slug, stamps attribution, delegates to `index_document`. | `InvalidKbSlugError` on bad slug |
| `get_entry(tenant_id, slug)` | Natural-key SELECT; returns `KbEntry` or `None`. | – |
| `list_entries(tenant_id, filter_pattern=None, limit=100, offset=0)` | List entries; slug-sorted; SQL `LIKE` pattern forwarded verbatim. | `ValueError` on negative `limit`/`offset` |
| `delete_entry(tenant_id, slug)` | Delete by natural key; returns whether a row existed. | – |
| `search_entries(tenant_id, query, filters=None, limit=10)` | Hybrid BM25 + cosine retrieval with `source=KB_SOURCE` pinned. | – |
| `ingest_directory(directory, tenant_id, dry_run=False)` | Walk a directory of `.md` files and ingest each one; idempotent. Per-file failures counted, run continues. | – |

### `walk_kb_directory` (`kb/file_walker.py`)

The directory-walking helper `ingest_directory` calls. Reads
Markdown front-matter via `python-frontmatter` and yields a
`KbFileRecord(path, slug, body, metadata)` for each valid file.
Per-file errors (binary file, unreadable bytes, invalid slug,
malformed front-matter) are caught inside the walker and appended
to the caller's `errors` list rather than raising.

## Control flow

### Write path — `create_entry`

1. `validate_slug(slug)` — fail closed on a bad slug before the
   substrate is touched.
2. One natural-key `get_entry` SELECT — yields the **created-vs-
   overwrite** signal (no prior row ⇒ `created=True`) and the prior
   `created_by_sub` to preserve.
3. `merge_attribution(...)` (in `kb/attribution.py`) folds the
   attribution keys into the metadata to persist.
4. `index_document(tenant_id, source='kb', source_id=slug,
   kind='kb-entry', body, metadata=merged)` — substrate's upsert path.
   The `documents.body_hash` short-circuit means an unchanged body
   pays zero embedding cost (just an `updated_at` bump).
5. Return `(_doc_to_entry(doc), created)`. The REST route maps
   `created` to **HTTP 201** (fresh slug) vs **200** (in-place
   overwrite); the MCP / UI callers discard it.

### Cross-principal write & delete semantics (#1845)

kb rows are **tenant-shared with no per-row ownership check**. Any
`tenant_admin` in a tenant may overwrite or delete **any** other
principal's slug in that tenant — this is **wiki-like and intended**,
not a bug. Operators must not assume per-author ownership: there is no
`403`/`409` on a same-slug cross-principal write, and `DELETE` is not
principal-scoped (it removes whatever row matches the natural key).
This is deliberately different from `memory` DELETE, which **is**
principal-scoped (a cross-principal memory delete is a silent no-op).

To make a row self-describing about authorship without an audit-log
join, every kb write stamps two keys into `documents.doc_metadata`
(no schema migration — they ride the existing JSONB and surface on
every read surface that returns `metadata`):

- **`created_by_sub`** — the OIDC `sub` that first created the row.
  Set once and **preserved verbatim** across every later overwrite,
  including a cross-principal one. A pre-attribution row (created
  before this shipped) is backfilled with the first overwriter's sub.
- **`last_updated_by_sub`** — the OIDC `sub` of whoever last wrote the
  row. Rewritten on every attributed write.

Attribution is derived from the **verified** `Operator.sub`, never
from request JSON: `merge_attribution` strips any caller-supplied
`created_by_sub` / `last_updated_by_sub` from the create-body
`metadata` before stamping, so an operator cannot forge authorship.
An unattended re-index that passes `actor_sub=None` leaves attribution
untouched (it does not erase a previously-attributed row's keys).

The two keys surface on **every** kb read surface:
`POST /api/v1/kb`, `GET /api/v1/kb/{slug}`, the `GET /api/v1/kb` list
preview, and `POST /api/v1/retrieve {source:"kb"}` hits (the retrieve
hit's `doc_metadata` is the same JSONB column).

### Write path — `ingest_directory`

1. `walk_kb_directory(directory, errors=errors)` — yields one record
   per valid `.md` file; per-file walk failures land in `errors`.
2. For each yielded record, `_ingest_one` does:
   - SELECT existing row by natural key.
   - Compute `compute_body_hash(body)` and compare.
   - Classify as `inserted` / `updated` / `skipped`.
   - On `dry_run=False`, call `index_document` with the source path
     enriched into metadata.
3. Per-file commits — a failing file in the middle of the corpus
   does **not** roll back the successful files preceding it. That
   trade-off (per-file commit overhead vs. all-or-nothing) is the
   load-bearing reason the service is stateless.

### Read path — `get_entry` / `list_entries`

Both are natural SQL — no retrieval. `get_entry` is a natural-key
SELECT; `list_entries` is `SELECT ... WHERE tenant_id=? AND
source='kb' ORDER BY source_id LIMIT ? OFFSET ?`, with an optional
`AND source_id LIKE :pattern` clause.

### Read path — `search_entries`

1. Translate optional `filters['kind']` into the substrate's `kind`
   argument; ignore other filter keys (v0.2 reserves them for future
   metadata-field filters).
2. Call `meho_backplane.retrieval.retriever.retrieve` with
   `source=KB_SOURCE`, the operator-supplied query, the resolved
   `kind` filter, and the limit.
3. For each `RetrievalHit`, adapt to `KbEntrySearchHit` via the
   kb-shaped vocabulary projection (`source_id` → `slug`, full
   `body` → `_make_snippet(body)`).

The retrieval substrate enforces tenant scoping at SQL level via the
`documents.tenant_id` filter. Combined with `source=KB_SOURCE` being
pinned inside `search_entries`, cross-tenant or cross-source reads
are structurally impossible.

## Dependencies

Direct:

- `meho_backplane.db.engine.get_sessionmaker` for the async session.
- `meho_backplane.db.models.Document` ORM type.
- `meho_backplane.retrieval.indexer.{index_document, compute_body_hash}`
  for the write path.
- `meho_backplane.retrieval.retriever.retrieve` for `search_entries`.
- `python-frontmatter` (transitive via `kb/file_walker.py`) for
  `.md` front-matter parsing.

Transports that call into this service:

- `meho_backplane.api.v1.kb` (REST, G4.1-T2).
- `meho_backplane.mcp.tools.knowledge` (MCP, G4.1-T3).
- `meho_backplane.cli.kb` (CLI, G4.1-T4).

## Known issues

None outstanding. The kb-shaped vocabulary is stable; the substrate's
metadata-filter surface is reserved for future v0.2.next work — a
caller passing extra keys to `filters` today silently has them
ignored (substrate cap, not a kb-package decision).

## References

- Initiative #331 (G4.1) — kb T1 service shipped via #430.
- [`examples/kb_writeback/`](../../examples/kb_writeback/) and
  [`examples-kb-writeback.md`](./examples-kb-writeback.md) — runnable
  sample showing investigation → kb write-back → retrieval (R3 of
  Initiative #807, G11.6 reference patterns).
- [`memory.md`](./memory.md) — the durable session/policy memory
  service that shares the `documents` substrate but enforces
  different scope encoding + RBAC.
