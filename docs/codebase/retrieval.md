# Retrieval substrate

## Overview

Hybrid BM25 + cosine similarity retrieval over the `documents` table,
fused with Reciprocal Rank Fusion (RRF, Microsoft 2009, `k=60`). Two
independent signals (Postgres FTS + pgvector) each return their top
`CANDIDATE_LIMIT=50` candidates; the in-process fuser merges them
down to the caller's `limit`. Every query is tenant-scoped — there is
no API surface that accepts a cross-tenant query, and the underlying
SQL filters by `tenant_id` in both candidate statements.

This is the shared read path for:

- **G4 `meho kb search`** — `retrieve(source="kb")`, surfaced via
  `KbService.search_entries` and the `search_knowledge` MCP tool.
- **G5 `meho recall`** — `retrieve(source="memory", kind="memory-user")`,
  surfaced via the memory service's `search_memory` path.
- **`POST /api/v1/retrieve`** — operator-facing diagnostic surface +
  cross-language consumer entrypoint.

## Key types

### `retrieve(...)` (`meho_backplane.retrieval.retriever`)

The substrate entrypoint. Async function, tenant-scoped, returns a
list of `RetrievalHit` ordered by `fused_score` descending.

Signature:

```python
async def retrieve(
    tenant_id: uuid.UUID,
    query: str,
    source: str | None = None,
    kind: str | None = None,
    limit: int = 10,
    session: AsyncSession | None = None,
    metadata_filters: dict[str, Any] | None = None,
) -> list[RetrievalHit]
```

Filter parameters narrow the candidate set in both signals symmetrically:

- `source` / `kind` — scalar equality against the `source` / `kind`
  columns.
- `metadata_filters` — flat `{key: scalar}` dict; translates to
  `documents.metadata @> :filters_jsonb` containment. Missing keys
  exclude rows (PG `@>` semantics); multi-key dicts behave as an
  intersection. `None` (default) skips the predicate. `{}` normalises
  to `None` at the boundary so the SQL never emits a no-op
  `@> '{}'::jsonb` predicate.

### `RetrievalHit`

Frozen Pydantic v2 model. Carries the projected `Document` row
fields (`document_id`, `tenant_id`, `source`, `source_id`, `kind`,
`body`, `doc_metadata`, `created_at`, `updated_at`) plus the
per-signal scores + ranks (`bm25_score`, `bm25_rank`, `cosine_score`,
`cosine_rank`) and the `fused_score`. Per-signal fields are `None`
when the document only appeared in one signal's candidate set.

### `RetrieveRequest` (`meho_backplane.api.v1.retrieve`)

Pydantic request body for `POST /api/v1/retrieve`. Frozen, `extra="forbid"`.

| Field | Type | Notes |
|---|---|---|
| `query` | `str` | `min_length=1`, `max_length=2000` |
| `source` | `str \| None` | `max_length=64` |
| `kind` | `str \| None` | `max_length=64` |
| `limit` | `int` | `ge=1`, `le=50` |
| `metadata_filters` | `dict[str, Any] \| None` | `max_length=20` (key cap); values must be JSON scalars (str / int / float / bool / None); a `field_validator` rejects nested objects + arrays with 422 |

## Control flow

```
retrieve()
  ├── empty-dict normalisation (metadata_filters={} → None)
  ├── session resolution (caller-owned vs helper-owned)
  └── _retrieve_in_session()
        ├── embedding compute (encode_one — one fastembed forward-pass)
        ├── _vector_literal (Python list[float] → pgvector text literal)
        ├── metadata_filters JSON serialisation (sort_keys=True)
        ├── parallel-style: _bm25_candidates + _cosine_candidates
        │     (raw SQL — pgvector / PG-FTS operators have no SQLAlchemy ORM)
        ├── _rrf_fuse (pure function, in-process)
        └── _hydrate_hits (single IN-fetch for the top-limit ids)
```

The two candidate SQL statements share the same WHERE shape — every
filter parameter (`source` / `kind` / `metadata_filters`) is applied
symmetrically so the fused list is the intersection of two equally-
scoped sets.

### API surface (`POST /api/v1/retrieve`)

```
operator JWT validated → require_role(OPERATOR) →
  RetrieveRequest validated →
    bind audit_query_hash + audit_source + audit_kind + audit_metadata_filters_keys →
      await retrieve(...) →
        bind audit_hit_count →
          return RetrieveResponse(hits, query_duration_ms)
```

The four `audit_*` contextvars are read by `_resolve_audit_payload`
inside `AuditMiddleware` and written to `audit_log.payload`. The
payload deliberately omits the raw query (only `query_hash` is
stored — SHA-256 hex of UTF-8) and the metadata filter values (only
the sorted key list is stored as `metadata_filters_keys`). Values
may carry tenant-shaped identifiers; storing keys-only preserves
auditability without leaking content.

### MCP `search_knowledge` tool surface

The handler (`_search_knowledge_handler` in
`meho_backplane.mcp.tools.knowledge`) forwards `arguments["filters"]`
to `KbService.search_entries`, which splits the dict:

- `filters["kind"]` → substrate `kind` parameter.
- Everything else → substrate `metadata_filters` parameter (empty
  residual → `None`).

So a caller sending
`filters={"kind": "kb-entry", "source_kind": "evoila-distilled"}`
reaches the substrate as `kind="kb-entry"` plus
`metadata_filters={"source_kind": "evoila-distilled"}`.

## Dependencies

- **pgvector** — `vector(384)` column on `documents.embedding`; the
  `<=>` operator is cosine distance. IVFFlat index
  `documents_embedding_idx` (lists=100, `vector_cosine_ops`) backs
  the cosine candidate query.
- **PG full-text search** — `to_tsvector('english', body)` + `@@`
  with `plainto_tsquery`. GIN expression index `documents_body_fts_idx`
  (migration `0003`).
- **JSONB containment** — `documents.metadata @>` (containment
  operator); GIN index `documents_metadata_gin_idx` (migration `0033`,
  default `jsonb_ops` opclass) backs the lookup.
- **fastembed** — the `EmbeddingService` wraps a `BAAI/bge-small-en-v1.5`
  ONNX model (384-dim).
- **SQLAlchemy 2.0 async** — `AsyncSession` for both candidate queries
  + the hydration `IN` fetch. Raw `text()` for the two candidate SQL
  statements because `@@` / `<=>` / `@>` have no first-class ORM
  representation.

## Known issues

- **SQLite path is undefined.** `to_tsvector` / `plainto_tsquery` / `<=>`
  / `@>` have no SQLite analogue. Unit tests mock at the candidate-
  helper boundary or run against testcontainer-pg in the integration
  suite. The substrate's behaviour against an actual SQLite engine is
  not contractual.
- **IVFFlat empty-table caveat.** Building IVFFlat against an empty
  table produces zero centroids. The expected remediation is `REINDEX
  INDEX documents_embedding_idx` after the first backfill batch.
- **Per-query re-embed.** Every retrieval calls `encode_one(query)`;
  there is no embedding cache in v0.2. v0.2.next may add an LRU once
  cache-hit ratios are measured.
- **Deferred capabilities.** Per-source RRF weighting is filed as a
  v0.2.next ticket gated on a measured ranking-quality regression
  that filters alone cannot resolve (see #1177 Out-of-scope and the
  Initiative body for #1178). Reranking (BAAI/bge-reranker, ColBERT)
  is the v0.2.next escape hatch if RRF under-performs at scale.

## References

- Substrate Initiative: [#225 G0.4 Retrieval substrate](https://github.com/evoila/meho/issues/225) (Done).
- Filter-extension Initiative: [#1178 G4.4 Retrieval enhancements](https://github.com/evoila/meho/issues/1178).
- Filter substrate Task: [#1177 G4.4-T1 retrieve metadata_filters](https://github.com/evoila/meho/issues/1177).
- Memory recall push-down Task: [#1179 G4.4-T2](https://github.com/evoila/meho/issues/1179) (follow-up).
- PG JSONB containment + GIN indexing: <https://www.postgresql.org/docs/16/datatype-json.html#JSON-CONTAINMENT> and <https://www.postgresql.org/docs/16/datatype-json.html#JSON-INDEXING>.
- pgvector: <https://github.com/pgvector/pgvector>.
