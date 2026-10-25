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

- **G4 `meho kb search`** — `retrieve(source="kb")`, which ranks kb
  passage rows (see "What search indexes for kb" below), surfaced via
  `KbService.search_entries` and the `search_knowledge` MCP tool.
- **G5 `meho recall --query`** — `retrieve(source="memory",
  memory_eligibility=...)` through `MemoryService.search_memories`
  (REST `POST /api/v1/memory/search` and the `search_memory` MCP tool).
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
    principal_sub: str | None = None,
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
- `principal_sub` — the authenticated caller's OIDC `sub`. When set,
  the substrate enforces a **mandatory, non-overridable** per-principal
  visibility predicate for `source='memory'` user-scoped kinds
  (`memory-user` / `memory-user-tenant` / `memory-user-target`): a
  user-scoped memory row is returned only when its stored
  `metadata->>'user_sub'` equals `principal_sub`. The predicate is
  ANDed into both candidate queries unconditionally and lives at the
  shared boundary (not the caller), so every entry point — the HTTP
  route, the `meho://retrieve/{query}` MCP resource, and any future
  consumer — is protected without trusting client `metadata_filters`,
  and a client-supplied `metadata_filters={"user_sub": "<other>"}` can
  only narrow, never widen, the visible set. Tenant-broadcast kinds
  (`memory-tenant` / `memory-target`) and every non-memory source
  (`kb` / `operations` / `docs`) are unaffected — they carry no
  per-principal `user_sub`. `None` (default) disables the predicate for
  callers that already scope by `user_sub` themselves (e.g.
  `MemoryService.search_memories`, which now also passes it as
  belt-and-suspenders) or that query non-principal sources. This is the
  #1797 cross-principal-leak fix: before it, `POST /api/v1/retrieve
  {source:"memory"}` and the MCP retrieve resource returned other
  principals' user-scoped memories within the same tenant.
- `memory_eligibility` — a frozen `MemoryEligibility` (tenant,
  principal, visible Memory kinds) built by
  `MemoryRbacResolver.eligibility()` (#3741). When set, `source='memory'`
  candidates are limited to its kinds and its principal drives the
  per-principal predicate, inside both candidate queries, before
  `LIMIT`. A tenant or principal that does not match the call raises
  `ValueError`. Generic `POST /api/v1/retrieve`, the MCP retrieve
  resource, and the UI diagnostics page (`POST /ui/retrieval/diagnostics`)
  pass it whenever Memory rows can be candidates (`source="memory"` or
  no source). Other sources pass `None`.
- **Memory expiry is always enforced in SQL.** Every `source='memory'`
  row must have no `expires_at`, or a valid ISO 8601 timestamp later
  than the database `now()`. Malformed values, `now`, `epoch`, and
  `infinity` are excluded through a guarded `CASE` with
  `pg_input_is_valid` (PostgreSQL 16+), so they cannot abort the query.
  Full table in `docs/codebase/memory.md`.

### `list_retrieval_facets(...)` (`meho_backplane.retrieval.retriever`)

Enumerates the distinct `source` / `kind` values retrievable in a
tenant, for populating value-discovery affordances (the `/ui/retrieval`
diagnostics Source / Kind datalists, #2458).

```python
async def list_retrieval_facets(
    tenant_id: uuid.UUID,
    *,
    principal_sub: str | None = None,
    session: AsyncSession | None = None,
) -> RetrievalFacets
```

Runs one `SELECT DISTINCT source, kind FROM documents` scoped to
`tenant_id` and gated by the **same** per-principal visibility predicate
`retrieve` enforces: when `principal_sub` is set, a user-scoped
`source='memory'` row (`memory-user` / `memory-user-tenant` /
`memory-user-target`) contributes its value only to the principal that
wrote it. The result is therefore exactly the set of values a diagnostics
`retrieve` call could actually match — a docs-corpus collection name (a
different substrate `retrieve` never queries) can never appear, and no
cross-tenant or cross-principal value leaks into the suggestions.

Returns a frozen `RetrievalFacets` with sorted, de-duplicated `sources`
and `kinds` tuples. Unlike the candidate queries, the DISTINCT query is
expressed through the ORM (`select(Document.source, Document.kind)` +
`doc_metadata['user_sub'].as_string()`) so it stays portable across the
PG production engine and the SQLite test engine — the raw
`metadata ->> 'user_sub'` text extraction the candidate queries inline is
PG-only.

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
filter parameter (`source` / `kind` / `metadata_filters`), the kb
passage mapping, plus the mandatory per-principal predicate (`principal_sub`, see the parameter
notes above) is applied symmetrically so the fused list is the
intersection of two equally-scoped sets and neither signal can surface
a user-scoped memory row the caller does not own.

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

### What search indexes for kb: current passages (#3761)

A kb entry is searched through **passages**, not through its whole
body. A passage is one heading-aware piece of the entry's **current**
revision, stored as its own `documents` row:

| Column | Value |
|---|---|
| `source` / `kind` | `kb-passage` / `kb-passage` |
| `source_id` | opaque: revision id, indexing generation tag, ordinal |
| `body` | exactly `revision_body[start:end]` |
| `embedding` | made from `heading path + body` |
| `tokens` | the real token count of that text |
| `metadata` | the entry's metadata, plus a `kb_passage` locator |

The `kb_passage` locator holds `entry_id`, `revision_id`, `slug`,
`ordinal`, `passage_count`, `heading_path`, `start`, `end`,
`chunker_version`, `embedding_model` and `index_generation`. Offsets
count Unicode code points.

**How a body is split** (`kb/passages.py`, `chunk_markdown`). Every
Markdown heading outside a code fence starts a section. A heading
title longer than 200 characters is cut to 200. A section that
fits the model window is one passage. A larger one is cut at
paragraphs, then lines, then sentences, then words, then single
characters, and neighbouring pieces are packed while they still fit.
The same body always gives the same passages.

**How tokens are counted** (`retrieval/embedding.py`,
`PassageTokenizer`). The embedding model cuts every text at 512 tokens,
so its own tokenizer cannot tell whether a text is too long. The
service loads a **second** tokenizer from the same installed model
files with fastembed's own loader, reads the window from it, and turns
off cutting and padding on that copy only. Special tokens (`[CLS]`,
`[SEP]`) stay on. Header, text and special tokens together must be at
most the window. The model's own tokenizer is never changed.

**How a write publishes** (`kb/revisions.py`, `write_entry`):

1. Read the entry: head, latest revision, passage generation.
2. Prepare, with no transaction open: split the body, then embed the
   head (if the body changed) and every new passage in one model call
   (`retrieval/indexer.py`, `prepare_index_batch`). Nothing is written.
3. Publish in one short transaction: lock the head (PostgreSQL), check
   the entry did not move, update the head, add the revision, delete
   the old passages, insert the new ones (`PreparedIndexBatch.apply`,
   one use only), check again, commit.

A failure in step 2 or 3 keeps the old head, revision and passages.
When the body is unchanged and the passages already belong to the
current revision and generation, nothing is embedded. The passages are
rebuilt for the same revision when the chunker version or the model
changes (a new **indexing generation**, so new passage ids).

**One mapping for every kb search.** The retriever's
`_KB_PASSAGE_MAPPING_SQL` sits in both candidate statements:

- `source="kb"` also admits `kb-passage` rows; `kind="kb-entry"` also
  admits `kb-passage` rows. `meho kb search`, `search_knowledge` and
  `POST /api/v1/retrieve` with `source="kb"` therefore rank passages.
- A kb head row (`source="kb"`) is not a candidate once its entry has
  passage rows. An entry never appears twice.
- Memory and every other source are not affected.

**What search returns.** `KbService.search_entries` ranks up to 50
passages and folds them into one `KbEntrySearchHit` per entry, best
first. A hit carries up to 3 matching passages (`KbPassageEvidence`):
the whole passage text and its `KbPassageLocator`. The locator `uri`
is `meho://kb/id/{entry}/revision/{revision}`, the exact read of that
revision; `body[start:end]` there is the passage. `coverage` is
`complete` when the passages hold the whole entry. Old revisions stay
readable by that exact read, but they are never searched.

**Bounded delivery.** `search_knowledge` measures its response with
the shared `measure_delivered_wire_bytes` (#3641) against
`RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES`. When it is too large, the
lowest-ranked hits are left out, `delivery` is `partial` and
`omitted_hits` says how many. A passage is never cut. Snippets,
passage texts and heading titles are wrapped as untrusted text.

### Offline KB passage benchmark (#3765)

Before anyone changes a retrieval default (model, prefixes, reranker),
the change must win on a held-out benchmark. The benchmark lives in the
existing eval package and runs only offline. It adds no route, index,
model, migration or setting, and nothing in it can change a production
default.

**Fixtures** (`retrieval/eval/kb_passage_fixtures.yaml`, loaded by
`load_kb_passage_fixtures` in `corpus.py`). Synthetic long Markdown
entries and queries, all made up. The file has a `version`; the loader
refuses a version the code does not know. Each query has a `scenario`
and a `language` (`en`, `de`):

| Scenario | What it checks |
|---|---|
| `middle_passage` / `end_passage` | the answer sits in the middle or at the end of a long body |
| `duplicate` | two entries hold the same answer; either one is correct |
| `conflict` | a current-looking entry contradicts the reviewed one |
| `stale` | an outdated entry gives an old answer |
| `no_match` | the corpus has no answer |

The loader checks the file: every expected entry is current, has the
expected heading and holds every answer term; every harmful entry is
stale or conflicting. A broken fixture fails at load time.

**Candidates** (`passage_bench.py`, `CandidateSpec`). A candidate names
what it changes: `search_method`, `embedding_model`, `query_prefix`,
`passage_prefix`, `reranker`. `search_method` is how it ranks:
`"retrieve"` is the production path (keyword plus vector search with
rank fusion), `"cosine"` ranks by vector similarity only.
`BASELINE_CANDIDATE` is the unchanged default: default model, no
prefixes, no reranker, `search_method="retrieve"`. A cosine-only run of
the default model is a candidate of its own, never the baseline.
`reembed_corpus=True` is required whenever the model or the passage
prefix changes, also for a model with the same vector size (for example
`intfloat/multilingual-e5-small`, 384 dimensions, which needs
`"query: "` / `"passage: "` prefixes and cuts text at 512 tokens).
Vectors from two models are not comparable.

**Searches.** `run_passage_bench` takes `(CandidateSpec, PassageSearch)`
pairs. A `PassageSearch` carries its `method` and the search function;
the run refuses a pair whose spec and search disagree on the method.
The first pair must be the baseline, so its search must be the
production `"retrieve"` path. Two search builders ship:

- `retrieve_search` runs the production `retrieve(source="kb")` and
  reads slug and heading path from the `kb_passage` locator. The
  fixtures must be ingested into the tenant first. Tests inject a fake
  `retrieve_fn` or a scripted search labelled `"retrieve"`.
- `embedding_search` splits every fixture entry with `chunk_markdown`,
  embeds the **whole** corpus with the candidate's model and passage
  prefix, and ranks by cosine only. Its candidate must set
  `search_method="cosine"`. The window shrinks by the prefix's
  tokens, so prefix plus passage still fits. Queries get the query
  prefix. For a real run pass `EmbeddingService(model, cache_dir).encode`
  and its `passage_tokenizer()`. Tests pass a fake encoder, so no model
  is downloaded.

**Scores**, per query, then grouped by `(scenario, language)`:

- `source_citation` / `mrr` — a correct entry in the top `k`, and its rank.
- `selected_reference` — the first correct passage sits under the
  expected heading (the passage an agent would cite).
- `answer_support` — share of answer terms in that passage.
- `harm_rate` — a stale or conflicting entry ranks above the first
  correct answer passage (an expected entry under the expected heading).
  A hit from the right entry but the wrong section does not count as
  correct, so a stale answer below it is still harm. With no correct
  answer passage in the top `k`, any harmful hit counts. For `no_match`,
  any returned evidence counts as harm; retrieval has no score cutoff,
  so the baseline is expected to be 1.0.
- `latency_p95_ms`, `bytes_mean`, `bytes_p95`, `calls`.

The report (`PassageBenchReport`) holds only labels and numbers. It
never holds a query or a body, and it never averages across groups.

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
- **Entries written before passages existed.** Their head stays the
  search candidate (one whole-body hit, no passages) until the entry is
  written again. `meho kb ingest` rewrites every file, also unchanged
  ones, so running it once builds the passages.
- **Retrieval eval counts passages.** `retrieval/eval/runner.py` asks
  for `k` rows and folds passages of one entry into one slug, so fewer
  than `k` slugs can come back.
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
- KB passage index: #3761 (Initiative #3730).
- Offline KB passage benchmark: #3765 (Initiative #3730).
- multilingual-e5-small model card (prefixes, 512-token limit): <https://huggingface.co/intfloat/multilingual-e5-small>.
- fastembed tokenizer loading (0.9.1, `fastembed/common/preprocessor_utils.py`, `load_tokenizer`): <https://github.com/qdrant/fastembed>.
- Hugging Face `tokenizers` (`no_truncation`, `no_padding`, post-processor): <https://huggingface.co/docs/tokenizers/api/tokenizer>.
- BGE small model card (512-token window): <https://huggingface.co/BAAI/bge-small-en-v1.5>.
