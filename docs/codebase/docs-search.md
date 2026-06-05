# search_docs (the meho-docs add-on)

## Overview

`search_docs` is the federated vendor-document retrieval surface of the
`meho-docs` add-on (Initiative #1518). Unlike `search_memory` /
`search_knowledge` — which read MEHO's own Postgres+pgvector substrate
(see [retrieval.md](retrieval.md)) — `search_docs` does **not** ingest
the vendor corpus. It proxies each query through the backplane to the
**external** corpus service the ops team runs, forwarding the operator's
JWT so the corpus authenticates and audits the call as the operator.

Routing through the backplane (rather than letting clients hit the
corpus directly) is what buys three properties in one place:

- **Central audit.** Every query lands one `audit_log` row under the
  named op `meho.docs.search`, so `query_audit` / who-touched surface
  it (the raw query is hashed, never stored).
- **JWT federation handled once.** The operator JWT forwarding lives in
  the T2 client, not in every consumer.
- **Mandatory product/version posture enforced centrally.** A docs query
  without a binary product+version scope is rejected — fail-closed — so
  no caller can accidentally run an unfiltered corpus-wide query.

The same `search_docs` service backs three consumers: the REST route
(this task, T3), the MCP tool `search_docs` (T4, #1523), and the CLI
verb `meho docs search` (T5, #1524). They share one service so the
REQUIRE_FILTERS gate and the cited-chunk shape are defined exactly once.

## Key types

### `search_corpus(...)` (`meho_backplane.auth.corpus`, T2 #1520)

The transport. An async `httpx` client that POSTs a search request to
`settings.corpus_url` carrying `Authorization: Bearer <operator.raw_jwt>`,
bounded by `settings.corpus_timeout_seconds`. Models the corpus's
response behind a small frozen Pydantic adapter (`CorpusChunk` /
`CorpusSearchResponse`, `extra="ignore"` so additive corpus fields are
absorbed silently while a dropped consumed field fails loudly).

Fail-closed by construction: an unconfigured (`corpus_url` unset),
unreachable, non-2xx, or malformed-response corpus all collapse to one
typed `CorpusUnavailable`. The exception carries the upstream HTTP
status (when the failure was a non-2xx response) but **never** the
response body — a corpus error page cannot leak through.

### `build_docs_scope(product, version)` (`meho_backplane.docs_search.service`)

The REQUIRE_FILTERS gate. When `settings.corpus_require_filters` is on
(the default), both `product` and `version` must be non-blank; either
missing raises `MissingDocsFilter` (HTTP 422 at the route). With the
gate off, the scope degrades to optional — present keys still scope the
query, absent keys widen it (the corpus owns the policy in that mode).
Blank-after-strip values are treated as absent so `product=" "` cannot
smuggle past the gate. Returns a frozen `DocsScope` whose `as_filters()`
renders the `{key: scalar}` `metadata_filters` shape — a **binary
containment scope**, never a ranking weight (the #1178 / #1177
decision).

### `search_docs(operator, query, *, scope, limit)` (`meho_backplane.docs_search.service`)

The shared service. Calls `search_corpus` with the binary scope and
projects the corpus's `CorpusChunk`s into MEHO's own `DocsChunk` surface
(chunk text + source citation + score), decoupling the public response
from the corpus wire contract. Propagates `CorpusUnavailable` unchanged.

### `POST /api/v1/search_docs` (`meho_backplane.api.v1.search_docs`, T3 #1521)

The REST face. `operator` role minimum (`read_only` → 403). Validates
the scope first (422 before any audit binding), then binds the audit
contextvars and calls the service.

## Control flow (the REST route)

1. `require_role(TenantRole.OPERATOR)` gates the request (`read_only` →
   403, unauthenticated → 401) before the handler runs.
2. `build_docs_scope(product, version)` enforces REQUIRE_FILTERS. A
   `MissingDocsFilter` → HTTP 422; the corpus is never called. (A 422
   here binds no audit context — it does not imply a corpus call
   happened.)
3. The handler binds the `audit_*` contextvars **before** the corpus
   call: `audit_op_id="meho.docs.search"`, `audit_op_class="read"`,
   `audit_query_hash` (SHA-256 of the UTF-8 query — the raw query is
   never bound), `audit_product`, `audit_version`. `AuditMiddleware`
   strips the `audit_` prefix and merges these into `audit_log.payload`,
   so a handler exception still produces an attributable row.
4. `search_docs(...)` forwards the operator JWT to the corpus and
   returns the cited chunks. `CorpusUnavailable` → HTTP 503
   (fail-closed; never an empty 200).
5. On success, `audit_hit_count` is bound and the cited chunks are
   returned as `SearchDocsResponse`.

### Why `op_class="read"` is safe for the broadcast feed

`read` is not in the sensitive op-class set
(`credential_read` / `credential_mint` / `credential_write` /
`audit_query`), so `redact_payload` publishes the **full** payload to
the per-tenant broadcast feed. That is safe here because the bound
payload is only the query *hash*, the binary product/version scope, and
the hit count — the **raw query is never bound**. (Contrast
`retrieve/eval`, which binds `op_class="audit_query"` to force
aggregate-only broadcast precisely because its payload could carry
operator-sensitive query intent.) `meho.docs.search` ends in `.search`,
which `classify_op` would also map to `read` — the explicit override
just makes the op name canonical for `query_audit` filtering.

## Dependencies

- `meho_backplane.auth.corpus` — the T2 federation transport.
- `meho_backplane.auth.operator.Operator` — carries `raw_jwt` (forwarded
  to the corpus) and `tenant_id` (the tenant boundary).
- `meho_backplane.auth.rbac.require_role` — the OPERATOR gate.
- `meho_backplane.audit` (`AuditMiddleware`) — lifts the `audit_*`
  contextvars into the `audit_log` row.
- `meho_backplane.settings` — `corpus_url` / `corpus_audience` /
  `corpus_timeout_seconds` / `corpus_require_filters`
  (`CORPUS_*` env vars).

## Known issues / boundaries

- The corpus request/response contract is a **consumer-side** dependency
  (the corpus is owned by the ops team). The `CorpusChunk` adapter pins
  only the fields MEHO consumes; a corpus that drops a consumed field
  fails closed as `CorpusUnavailable` rather than returning a partial
  result.
- No local indexing — federation only. MEHO gains no Qdrant dependency
  and does not absorb the corpus into its own substrate.
- `ask_docs` (a synthesized answer over the cited chunks) is a
  fast-follow (T7, #1526), not part of this surface.

## References

- Route: `backend/src/meho_backplane/api/v1/search_docs.py`.
- Service: `backend/src/meho_backplane/docs_search/service.py`.
- Transport: `backend/src/meho_backplane/auth/corpus.py`.
- Audit binding precedent: `backend/src/meho_backplane/api/v1/retrieve.py`
  (query-hash privacy), `retrieve_eval.py` (op_id / op_class override).
- Binary-filters-not-weights decision: #1178 / #1177; PG JSONB
  containment <https://www.postgresql.org/docs/16/datatype-json.html#JSON-CONTAINMENT>.
