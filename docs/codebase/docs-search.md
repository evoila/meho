# search_docs / ask_docs (the meho-docs add-on)

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
- **Mandatory collection scope + per-collection entitlement enforced
  centrally.** A docs query without a `collection` is rejected —
  fail-closed — and a tenant may only search collections it holds the
  `meho-docs:<collection>` capability for, so no caller can run an
  unscoped query or reach a collection it isn't entitled to. `product` /
  `version` are optional refinements within the chosen collection.

The same `search_docs` service backs four consumers: the REST route
(T3), the MCP tool `search_docs` (T4, #1523), the CLI verb
`meho docs search` (T5, #1524), and the synthesis tool `ask_docs` (T7,
#1526). They share one service so the REQUIRE_FILTERS gate and the
cited-chunk shape are defined exactly once.

`ask_docs` is the **synthesis fast-follow**: where `search_docs` returns
the raw cited chunks, `ask_docs` runs the *same* retrieval and then
composes one grounded answer over those chunks, returning
`{answer, citations[]}`. The grounding contract is enforced in code, not
just in the prompt — no claim survives without a citation that resolves
to a retrieved chunk, an empty retrieval returns a deterministic "no
grounded answer" (never a guess), and an unconfigured synthesis model
fails closed rather than degrading to an ungrounded answer.

## Key types

### `search_corpus(...)` (`meho_backplane.auth.corpus`, T2 #1520)

The transport. An async `httpx` client that POSTs a search request to a
corpus URL carrying `Authorization: Bearer <operator.raw_jwt>`, bounded
by `settings.corpus_timeout_seconds`. The URL and RFC 8707 audience are
optional overrides (`corpus_url=` / `audience=`); `None` falls back to
the global `settings.corpus_url` / `settings.corpus_audience` — the
seam the `corpus-http` backend uses to pass a per-collection endpoint
(see the router below). Models the corpus's response behind a small
frozen Pydantic adapter (`CorpusChunk` / `CorpusSearchResponse`,
`extra="ignore"` so additive corpus fields are absorbed silently while a
dropped consumed field fails loudly).

Fail-closed by construction: an unconfigured (no URL), unreachable,
non-2xx, or malformed-response corpus all collapse to one typed
`CorpusUnavailable`. The exception carries the upstream HTTP status (when
the failure was a non-2xx response) but **never** the response body — a
corpus error page cannot leak through.

### Backend-agnostic search router (`meho_backplane.docs_search.backends`, T2 #1551)

The `collection → backend` router that keeps MEHO a backplane, not a
vector DB: one collection can sit on a managed RAG and another on the
JWT-forward corpus **behind the same `search_docs`**, and the agent never
sees which backend answered. Four pieces, modelled on the connector
registry (`connectors/registry.py`) **minus the version tie-break ladder**
(a collection binds to exactly one backend by construction, #1548):

- `SearchBackend` (`backends/base.py`) — the adapter ABC. One required
  `async search(operator, query, *, backend_ref, metadata_filters, limit)
  -> CorpusSearchResponse` (the same shape as the re-homed transport, so
  the seam swap is behaviour-preserving) plus a `probe()` forward seam
  for the readiness probe (T6 #1555) that defaults to raising rather than
  claiming "ready". A class-level `backend_type` string is the routing
  discriminator.
- `CorpusHttpBackend` (`backends/corpus_http.py`,
  `backend_type="corpus-http"`) — the **first** concrete adapter. It
  wraps `search_corpus` (the well-tested transport, not a copy of the
  httpx body) and resolves the per-collection endpoint / audience from
  the collection's `backend.ref` (keys `endpoint`/`url` and `audience`),
  falling back to the legacy `corpus_url` / `corpus_audience` globals for
  an unmigrated single-collection deploy. It fronts whatever the ops
  corpus proxies; a direct managed-RAG adapter with its own
  service-account auth is a deliberate **later Task**, not built here.
- the registry (`backends/registry.py`) — a `dict[str, SearchBackend]`
  with `register_backend(type, impl)` / `get_backend(type)` /
  `all_backends()`. Importing the package self-registers `corpus-http`.
  Duplicate registration of a type raises (a programming bug, not a
  runtime condition).
- `resolve_backend(collection)` / `resolve_backend_or_label(collection)`
  (`backends/resolver.py`) — the router. Reads `collection.backend["type"]`
  and does a direct dict lookup. The raising form drops into the
  `search_docs` seam (an unknown / malformed type → `CorpusUnavailable`,
  the **existing** 503 arm — no new error taxonomy, and the backend id
  never reaches the agent). The `(impl, label, msg)` labelled form is the
  non-raising sibling (mirroring `resolve_connector_or_label`) the T5
  fan-out and T6 readiness probe branch on. `collection=None` routes to
  `corpus-http` with no ref — the legacy single-collection path.

### `build_docs_scope(collection, product=None, version=None)` (`meho_backplane.docs_search.service`)

The binary-scope gate (G4.6-T3 #1552, the **scope inversion**).
`collection` is the **mandatory** binary scope — a missing or blank value
raises `MissingDocsFilterError` (HTTP 422 at the route, `-32602` at the
MCP face), **unconditionally** (it is no longer gated by
`settings.corpus_require_filters`, which governed the old product+version
gate). `product` / `version` demote to **optional refinements** within
the chosen collection: present, they ride `as_filters()`; absent, the
collection alone scopes the query. Blank-after-strip values are treated
as absent so `collection=" "` cannot smuggle past the gate. Returns a
frozen `DocsScope` carrying `collection_key` plus the optional
refinements; `as_filters()` renders **only** the refinements into the
`{key: scalar}` `metadata_filters` shape — a **binary containment
scope**, never a ranking weight (the #1178 / #1177 decision).
`collection_key` is deliberately **excluded** from `as_filters()`: it is
a router / entitlement key, not a per-chunk metadata field.

### `resolve_entitled_ready_collection(session, operator, collection_key)` (`meho_backplane.docs_search.collection_access`)

The **shared gate** every collection-scoped surface (the REST route, the
`search_docs` / `ask_docs` tools, the docs-chunk resource) runs after
parsing the `collection` key and before calling `search_docs`. Three
policies, defined once so they cannot drift per surface:

- **Resolution** — `resolve_doc_collection` (T1 #1550, tenant-first) turns
  the key into its registry row. An unknown key → `UnknownCollectionError`
  (carrying the catalogue of visible keys for a "did you mean…?" hint),
  mapped to 422 / `-32602`.
- **Per-collection entitlement** (reuses the G4.5-T1 capability substrate,
  zero new tables) — the operator must carry the
  `meho-docs:<collection_key>` capability key (built by
  `collection_capability_key`). The static `required_capability="meho-docs"`
  gate still governs *visibility* (tool / template absence when the add-on
  isn't provisioned); this finer gate governs *which collections* an
  entitled tenant may query. A miss → `CollectionForbiddenError`, mapped to
  403 / `-32602` (the 403-projected dispatcher path).
- **Readiness** — a collection whose registry `status` is not `"ready"`
  → `CollectionNotReadyError`, mapped to 409 (REST) / `-32603` (MCP,
  server-side condition). The richer reachability *probe* is T6 (#1555);
  T3 reads only the `status` column.

The checks run resolve → entitle → readiness so the rejection is the most
specific true one. Returns the frozen `DocCollection` read shape.

### `search_docs(operator, query, *, scope, collection, limit=10)` (`meho_backplane.docs_search.service`)

The shared service and the **router seam**. `collection` is now
**required**: the caller (route / handler) has already resolved + entitled
+ readiness-checked it via `resolve_entitled_ready_collection`. It
resolves the backend via `resolve_backend(collection)`, calls
`backend.search(...)` with the optional product/version refinements as
`metadata_filters`, projects the backend's `CorpusChunk`s into MEHO's own
`DocsChunk` surface (chunk text + source citation + score), and propagates
`CorpusUnavailable` unchanged. The backend id never appears in the request
or the projected response (the backend-agnostic contract).

### `synthesize_docs_answer(query, retrieval, *, llm_client=None)` (`meho_backplane.docs_search.synthesis`, T7 #1526)

The synthesis step `ask_docs` runs *after* `search_docs` retrieval. It
never retrieves — it composes a grounded answer over the chunks the shared
service already returned. Three invariants, each a code-enforced
acceptance criterion:

- **No claim without a real citation.** The model is asked to return a
  strict JSON object `{answer, cited_chunk_ids[]}` rather than prose with
  parsed inline markers, so the grounding check is machine-enforceable.
  Every `cited_chunk_id` is validated against the retrieved set; an id
  outside it raises `DocsSynthesisError` (an invented citation is rejected,
  not silently dropped). Returned `citations` follow retrieval ranking and
  de-duplicate.
- **Empty retrieval → no model call.** Zero retrieved chunks short-circuit
  to the deterministic `NO_GROUNDED_ANSWER` constant *without* invoking the
  model — the one answer path produced with no LLM call, precisely so it
  cannot hallucinate.
- **Fail-closed synthesis client.** The default client is
  `build_anthropic_ingest_llm_client` (the #1386 Anthropic-Messages
  adapter, reused via the shared `LlmClient` Protocol). No
  `ANTHROPIC_API_KEY` raises `LlmClientUnavailable`; a model that runs but
  breaks the JSON / citation contract raises `DocsSynthesisError`. Neither
  is caught in the handler — both bubble to `-32603` (the MCP analogue of
  503). The synthesis model is never relaxed into an ungrounded answer.

The client is injectable so tests pin a deterministic stub; production
reuses the spec-ingestion grouping pass's Anthropic key + model, so no new
settings are introduced.

### `POST /api/v1/search_docs` (`meho_backplane.api.v1.search_docs`, T3 #1552)

The REST face. `operator` role minimum (`read_only` → 403). Validates
the `collection` scope first (422 before any audit binding), binds the
audit contextvars (including `audit_collection`), runs the shared
`resolve_entitled_ready_collection` gate (unknown → 422, not entitled →
403, not ready → 409), then calls the service. Takes a
`Depends(get_session)` DB session for the resolve.

### `meho docs search` (`cli/internal/cmd/docs`, T5 / T3 #1552)

The operator-facing CLI verb. `meho docs search <query> --collection <c>
[--product <p>] [--version <v>] [--limit N] [--json]` POSTs to
`/api/v1/search_docs` via the shared generated authed client (bearer +
lazy 401-refresh), mirrors the route's collection gate client-side (a
missing `--collection` is rejected before the round-trip; `--product` /
`--version` are optional refinements), maps the 403 (not entitled) / 409
(not ready) / 422 (unknown collection) statuses, and renders the cited
chunks as a text table or raw JSON. It consumes the generated
`api.SearchDocsRequest` / `api.SearchDocsResponse` / `api.DocsChunk`
types directly — no hand-typed copies of the backend schemas.

**Gating — true absence when unprovisioned.** The `meho docs` tree
compiles into every CLI binary, but it is gated on the tenant's
`meho-docs` capability (the same capability T1 gates the MCP tool on).
The CLI reads the `capabilities` claim from the stored bearer JWT at
command-tree-build time and:

- shows `meho docs` in `meho --help` and runs its verbs only when the
  claim contains `meho-docs`;
- otherwise marks the parent `Hidden` and makes every verb refuse with
  a typed `addon_not_provisioned` error (exit 5) before any network
  call.

The claim is decoded **unverified** — the CLI holds no realm signing
key and needs none. This is a visibility affordance, not a security
boundary: the backplane re-validates the JWT on every request and the
corpus federation enforces the real boundary, so a forged claim can
change only what the CLI *shows*, never what the server *allows*.
Reading an unverified claim is safe precisely because the gate never
grants access on its own; it is fail-closed (no login / unreadable
store / malformed token → not provisioned), mirroring the backend's
fail-closed `_extract_capabilities`.

**Why not the server-driven discovery channel.** True per-tenant
absence via `discovery.Fetch` → `GET /api/v1/commands` was the
preferred shape on paper, but the discovery channel is anonymous by
design (it never imports `internal/api` / `internal/auth` and fetches
before login produces a token) and its `Register` only grafts *stub*
commands ("not yet implemented locally") — it cannot toggle the
visibility of a real compiled-in implementation per tenant. A
tenant-filtered manifest would contradict that anonymous contract and
require a new authenticated backend route plus an OpenAPI snapshot
regen. The compiled-in + claim-probe shape achieves the same operator-
visible outcome (absent from `--help`, non-runnable) without a backend
change.
### `search_docs` MCP tool (`meho_backplane.mcp.tools.docs`, T4 #1523)

The agent-facing face. Registered against the G0.5 MCP registry,
auto-discovered by `eager_import_mcp_modules` (no manifest edit).
Carries a **second** gate beyond the `operator` role gate:
`required_capability="meho-docs"` (G4.5-T1, #1519). A tenant that hasn't
provisioned the `meho-docs` add-on never sees the tool in `tools/list`
(true absence) and a `tools/call` naming it directly is rejected
403-class before the handler runs — the gate is enforced at list time
(`all_tools_for`) and again at call time (`handle_tools_call`).

The `inputSchema` is strict JSON Schema 2020-12: `additionalProperties:
false`, required `[query, collection]` (product/version demoted to
optional). That `required` list is the **first** line of the
collection-scope defence — a schema-validating client never reaches the
service-side `build_docs_scope` check. When a non-validating client does
reach it, `MissingDocsFilterError` maps to `McpInvalidParamsError` →
JSON-RPC `-32602` (the MCP analogue of a 422). The handler then runs the
shared `resolve_entitled_ready_collection` gate: an unknown / not-entitled
collection maps to `-32602` (the per-collection entitlement is enforced at
**call** time, since the collection key is a tool argument, not known at
list time), and a not-ready collection bubbles to `-32603`.
A `CorpusUnavailable` is **not** caught — a well-formed request against a
down upstream is a server fault, so it bubbles to the dispatcher's
generic catch as `-32603` Internal Error (the MCP analogue of the route's
503). One `audit_log` row per call is written by the dispatcher with
`op_id="meho.docs.search"` (the handler binds the `audit_op_id` contextvar
and the dispatcher lifts it into the persisted row, so the op id is the
canonical, uniform token across REST / CLI / MCP — G4.5-T8 #1549),
`op_class="read"`, and the raw arguments hashed into `params_hash` — never
the query in the clear. The bare tool name still drives the broadcast
`classify_op` path, so broadcast sensitivity is unchanged.

The tool description is load-bearing routing UX (it is a prompt): it
names the sibling tools so the agent learns the boundary — `search_docs`
for VENDOR REFERENCE, `search_knowledge` for how THIS team does X,
`search_memory` for cross-session state — and points to the companion
resource for the full text of a hit on a later turn.

### `ask_docs` MCP tool (`meho_backplane.mcp.tools.docs`, T7 #1526)

The synthesis sibling, registered alongside `search_docs` in the **same**
module and carrying the **same** `required_capability="meho-docs"` gate,
the same `operator` role minimum, the same per-collection entitlement, and
the same strict `inputSchema` (`additionalProperties: false`, required
`[query, collection]`, product/version optional, `limit` default 10 / cap
50). It is absent from `tools/list` and 403-class on `tools/call` for an
unprovisioned tenant exactly like `search_docs`.

The handler mirrors `search_docs`'s error arms and adds the synthesis arm:
`build_docs_scope` + the shared gate enforce the collection scope
(`MissingDocsFilterError` / unknown / not-entitled → `-32602`);
`CorpusUnavailable` from retrieval and a not-ready collection bubble to
`-32603`; and the
synthesis failures (`LlmClientUnavailable` for an unconfigured model,
`DocsSynthesisError` for a broken grounding contract) also bubble to
`-32603` — never an ungrounded 200. It stays `op_class="read"`: it
composes over retrieved chunks, it never mutates the corpus. The
dispatcher writes one `audit_log` row per call with `op_id="meho.docs.ask"`
(the handler binds `audit_op_id`, lifted into the persisted row — uniform
across REST / CLI / MCP per G4.5-T8 #1549; the bare tool name still feeds
`classify_op`, which leaves it as `other` while the tool definition pins
the row's `op_class="read"`) and the raw query hashed into `params_hash` —
the same privacy posture as `search_docs`.

The description routes the agent between the answer-shaped tool and the
chunks-shaped one: `ask_docs` for a composed grounded answer, `search_docs`
for the raw chunks to read itself, `search_knowledge` / `search_memory`
for the non-vendor corpora.

### `meho://docs/{collection}/{product}/{version}/{chunk_id}` resource (`meho_backplane.mcp.resources.docs`, T3 #1552)

The fetch-by-citation companion, gated by the **same**
`required_capability="meho-docs"` plus the **per-collection**
`meho-docs:<collection>` entitlement (enforced in the handler via the
shared gate). The backend (T2) is search-only — there is no
fetch-chunk-by-id endpoint — so the handler recovers a chunk by
**re-issuing a scoped search** through the shared service and selecting
the hit whose `chunk_id` matches the URI. That is why the URI carries the
leading `collection` segment (plus the optional `product` / `version`):
`collection` is the mandatory binary scope the re-search needs to route +
entitle, and encoding it lets `build_docs_scope` enforce the same
collection posture (belt-and-suspenders, since a blank segment can't match
the `[^/]+` template). A blank / unknown / not-entitled collection →
`-32602`; a `chunk_id` absent from the re-search collapses to `-32602`
"not found" without distinguishing "empty scope" from "no such id", so the
resource is not a collection-contents oracle.

## Control flow (the REST route)

1. `require_role(TenantRole.OPERATOR)` gates the request (`read_only` →
   403, unauthenticated → 401) before the handler runs.
2. `build_docs_scope(collection, product, version)` enforces the
   mandatory `collection` scope. A `MissingDocsFilterError` → HTTP 422;
   no backend is called. (A 422 here binds no audit context.)
3. The handler binds the `audit_*` contextvars **before** the gate /
   backend call: `audit_op_id="meho.docs.search"`, `audit_op_class="read"`,
   `audit_query_hash` (SHA-256 of the UTF-8 query — the raw query is
   never bound), `audit_collection`, `audit_product`, `audit_version`.
   `AuditMiddleware` strips the `audit_` prefix and merges these into
   `audit_log.payload`, so an entitlement / readiness / backend exception
   still produces an attributable row.
4. `resolve_entitled_ready_collection(session, operator, collection_key)`
   resolves + entitles + readiness-checks the collection: unknown → 422,
   not entitled → 403, not ready → 409.
5. `search_docs(..., collection=...)` routes to the collection's backend
   and returns the cited chunks. `CorpusUnavailable` → HTTP 503
   (fail-closed; never an empty 200).
6. On success, `audit_hit_count` is bound and the cited chunks are
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
- `ask_docs` is **single-shot** Q→cited-A only — no multi-turn /
  conversational follow-up, and no re-ranking or weighting beyond what T3
  retrieval returns (binary scope only, per #1177).

## References

- Route: `backend/src/meho_backplane/api/v1/search_docs.py`.
- Service (router seam): `backend/src/meho_backplane/docs_search/service.py`.
- Collection-access gate (resolve + entitle + readiness, T3 #1552):
  `backend/src/meho_backplane/docs_search/collection_access.py`.
- Doc-collections registry + resolver (T1 #1550):
  `backend/src/meho_backplane/docs_collections/`.
- Backend router (T2 #1551): `backend/src/meho_backplane/docs_search/backends/`
  (`base.py` ABC, `corpus_http.py` first adapter, `registry.py`,
  `resolver.py`). Registry/ABC/resolver precedent:
  `backend/src/meho_backplane/connectors/` (registry + base + resolver).
- Synthesis (`ask_docs`): `backend/src/meho_backplane/docs_search/synthesis.py`.
- MCP tools (`search_docs` + `ask_docs`): `backend/src/meho_backplane/mcp/tools/docs.py`.
- Fail-closed LLM client precedent (#1386):
  `backend/src/meho_backplane/operations/ingest/anthropic_client.py`.
- MCP resource: `backend/src/meho_backplane/mcp/resources/docs.py`.
- Capability gate (T1): `backend/src/meho_backplane/mcp/registry.py`
  (`required_capability`, `capability_satisfied`, `all_tools_for`).
- Transport: `backend/src/meho_backplane/auth/corpus.py`.
- Audit binding precedent: `backend/src/meho_backplane/api/v1/retrieve.py`
  (query-hash privacy), `retrieve_eval.py` (op_id / op_class override).
- Binary-filters-not-weights decision: #1178 / #1177; PG JSONB
  containment <https://www.postgresql.org/docs/16/datatype-json.html#JSON-CONTAINMENT>.
