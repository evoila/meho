<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2026 evoila Group
-->

# Knowledge Module

The knowledge subsystem ingests documents (PDF, HTML, DOCX, plain text, URLs),
chunks them, embeds the chunks, writes them into PostgreSQL + pgvector, and
serves them back via hybrid semantic / keyword search to the agent and to the
Knowledge page. This document is the mental-model reference for anyone
touching ingestion, retrieval, or the grounded-answer layer.

## High-level Pipeline

```
Upload (REST)
  -> IngestionJob row (status=pending)
  -> background asyncio task
     -> conversion         (Docling / lightweight converter)
     -> markdown persist   (MinIO .md)
     -> chunking           (HierarchicalChunker | TextChunker)
     -> checkpoint         (MinIO .chunks.json)
     -> summary + prefix   (connector-aware)
     -> per-chunk metadata extraction
     -> embedding          (batched; per-chunk retry)
     -> pgvector insert    (with retrieval_context)
     -> hybrid index       (Postgres FTS / BM25)
     -> complete_job       (status=completed)
```

Every stage reports progress through `IngestionJobRepository.update_stage` so
the frontend can poll `GET /knowledge/jobs/{id}` and render a live bar.

## Document Family & Versioning

A document is modeled as a **family** with one or more **versions**. The
`knowledge_document_family` table owns the family identity (tenant, scope,
connector, knowledge type, display name); each `ingestion_jobs` row points at
the family via `family_id` and carries a user-supplied `doc_version` label
(e.g. `v9`, `1.0.0`).

Uniqueness is enforced at the database level with `NULLS NOT DISTINCT`
indexes:

- `uq_family_scope_name` -- at most one family per `(tenant, scope,
  connector, name)`. Enforces "no two documents with the same display name in
  the same scope".
- `uq_family_version` -- at most one non-deleted job per `(family_id,
  doc_version)`. Prevents a second upload from claiming `v9` of the same
  document.
- `uq_family_hash` -- at most one non-deleted job per `(family_id,
  file_sha256)`. Prevents the same PDF from being uploaded twice under
  different version labels inside a family.

The two upload paths enforce these:

- `POST /knowledge/upload` creates a **new family** plus the first version.
  The handler pre-checks `find_by_name` and still wraps the INSERT in
  `try/except IntegrityError -> 409` so concurrent uploaders can never race
  past `NULLS NOT DISTINCT`.
- `POST /knowledge/documents/{id}/versions` attaches a **new version to an
  existing family**. It pre-checks `has_version` and `has_hash` and wraps the
  INSERT the same way.

Chunks inherit `family_id` and `doc_version` so the Knowledge page can display
a version pill on every search-result card, and the agent can scope queries
to a given version when a caller asks for "the v9 docs only".

## Checkpointing and Resume

Before the embedding phase starts, `IngestionService` writes
`chunks_with_context` to MinIO as `{storage_key}.chunks.json`. If embedding
fails mid-batch the job transitions to `failed` with `error_stage="embedding"`
and `error_chunk_index` set to the index that crashed.

Resume is a two-part contract:

1. **Atomic claim in the route.** `POST /knowledge/jobs/{id}/resume` calls
   `IngestionJobRepository.mark_resuming`, which issues a conditional
   `UPDATE ... WHERE status='failed'`. Exactly one concurrent caller wins;
   all others receive HTTP 409. Only after the claim succeeds do we enqueue
   the background task.
2. **Index-based high-water-mark.** The resume loop restarts at
   `error_chunk_index` (falling back to `len(existing_chunk_ids)` only when
   no index was recorded). Count-based resume silently re-processes chunks
   that were skipped (e.g. too-short chunks), which is why we prefer the
   index.

Checkpoints are deleted on successful completion; failures leave them in
place so operators can retry.

## Grounded Answer (RAG) Contract

`POST /knowledge/search` optionally returns a grounded answer on top of the
ranked chunks. The contract, implemented in
`meho_app/modules/knowledge/answer.py`:

- The model receives the query and numbered context blocks (`[Chunk N]`
  headers with filename, heading path, page range, and source chunk index).
- The model must return valid JSON with `answer` and an array of
  `citations`. Every citation is `{chunk_index, quote}`, and each quote must
  appear verbatim in the referenced chunk.
- `_sanitize_answer` strips citations that fail bounds/deduplication/quote
  verification, then removes any inline `[N]` markers in the answer whose
  index was dropped so the rendered text never points at a missing source.
- If the LLM call itself fails, the search response still returns the ranked
  chunks along with `answer=None` and an `answer_error` message; the API does
  not fail the whole request.

## Retrieval Score Thresholds

Search defaults to `score_threshold=0.0` (see
`KnowledgeStore.search_cross_connector` and friends). The agent prefers to
see the top-K ranked chunks with their reranker scores and decide per chunk
whether they are relevant, rather than getting short-circuited by a hard
similarity cutoff. When the reranker is disabled the ranker still applies an
internal `retrieval_threshold` in `PostgresFTSHybridService`, so noise is
filtered before results reach the API.

## Trust and Access Control

Every chunk carries `(tenant_id, roles, groups, user_id)`. `KnowledgeRepository`
exposes `get_chunks_with_acl(..., UserContext)` which is the only path that
should be used when surfacing chunk content to end users. ACL-less helpers
(`get_chunks_by_ids`) exist for internal pipelines (e.g. job-detail preview
after tenant check) but must never be exposed to unauthenticated callers.

## Cross-worker Cancellation

`POST /knowledge/jobs/{id}/cancel` can only cancel an asyncio task that lives
in the **same** worker process (tracked via `IngestionTaskRegistry`, accessed
through `get_task_registry()` in `meho_app/modules/knowledge/task_registry.py`).
When the running task is on another replica we return HTTP 202 with the job's
current status
and do **not** flip the row to `failed`, because writing a terminal state
from here would race the worker that is still writing progress. A Redis
pub/sub signal is a future improvement.

## File Reference

| File | Responsibility |
| ---- | -------------- |
| `meho_app/modules/knowledge/ingestion.py` | Pipeline orchestration, resume, checkpoints |
| `meho_app/modules/knowledge/knowledge_store.py` | Search facade (semantic + hybrid + ranked) |
| `meho_app/modules/knowledge/hybrid_search.py` | Postgres FTS + semantic fusion |
| `meho_app/modules/knowledge/answer.py` | Grounded-answer prompt, sanitization, citations |
| `meho_app/modules/knowledge/family_repository.py` | Document family lookups and uniqueness helpers |
| `meho_app/modules/knowledge/job_repository.py` | Ingestion job CRUD + atomic `mark_resuming` |
| `meho_app/api/routes_knowledge.py` | REST surface for upload, versions, jobs, search |

## Error handling

Route boundaries in `routes_knowledge.py` raise `InternalError` (from `meho_app/core/errors.py`) instead of `HTTPException(500)` so that the app-level handler in `meho_app/api/errors.py` can attach the OTEL trace ID and produce a structured `{"error": {...}}` response. Background task outer catches and audit side-effects are kept broad and annotated with `# noqa: BLE001`. See `docs/codebase/error-classification.md` for the full pattern.
