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

`ask_docs` is the **answer pipeline**: where `search_docs` returns the raw
cited chunks, `ask_docs` runs a corpus-aware **expand** step, retrieves per
expanded variant, RRF-merges the chunks, and then composes one grounded
answer over them, returning `{answer, citations[]}`. The pipeline is
**expand → retrieve (per variant) → RRF-merge → synthesize** (#1916). The
grounding contract is enforced in code, not just in the prompt — no claim
survives without a citation that resolves to a retrieved chunk, an empty
retrieval returns a deterministic "no grounded answer" (never a guess), and
an unconfigured **expand or synthesis** model fails closed rather than
degrading to an un-expanded / ungrounded answer. The expand step is the
answer-pipeline's job only — `search_docs` (the raw-chunks tool) is
unchanged.

`ask_docs` is exposed over **three** faces, all composing the same
in-process pipeline: the MCP `ask_docs` tool (T7, #1526), the REST
`POST /api/v1/ask_docs` route (T2, #1917 — the synthesis sibling of
`POST /api/v1/search_docs`), and the `/ui/corpus` **Ask mode** (T2, #1917 —
a toggle alongside the original retrieve mode). The REST route + the UI BFF
share one leg-by-leg composition in `meho_backplane.api.v1.ask_docs` so the
pipeline structure and the #1918 per-leg error classification are defined
once: the REST route calls `run_ask_pipeline` (raises the classified
`AskDocsAnswerError`), and the UI BFF calls the structured sibling
`run_ask_pipeline_capturing_retrieval` (returns an `AskPipelineOutcome`
carrying the chunks retrieval returned alongside the error) so it can fail
open to those chunks on a post-retrieval leg failure — `run_ask_pipeline` is
the thin raising wrapper over it. `ask_docs` is single-collection only on
every face (no `collections` fan-out field).

## Key types

### `search_corpus(...)` (`meho_backplane.auth.corpus`, T2 #1520)

The transport. An async `httpx` client that POSTs a search request to a
corpus URL carrying `Authorization: Bearer <operator.raw_jwt>`, bounded
by `settings.corpus_timeout_seconds`. The URL and RFC 8707 audience are
optional overrides (`corpus_url=` / `audience=`); `None` falls back to
the global `settings.corpus_url` / `settings.corpus_audience` — the
seam the `corpus-http` backend uses to pass a per-collection endpoint
(see the router below). The request sends `top_k` for the hit cap (the
key MEHO.Knowledge's `/search` reads — `limit` was silently ignored,
#1732). Models the corpus's response behind a small frozen Pydantic
adapter (`CorpusChunk` / `CorpusSearchResponse`, `extra="ignore"` so
additive corpus fields are absorbed silently while a dropped consumed
field fails loudly).

The adapter speaks **two wire dialects** via validation aliases (#1732):
the hit list is read from `results` (MEHO.Knowledge) **or** `chunks`, and
each chunk's text/source-link from `text`/`source_uri` (MEHO.Knowledge)
**or** `content`/`source_url`. The consumed names downstream callers read
stay `chunks` / `content` / `source_url` regardless of dialect. Crucially
the hit list is **required** (no default) so a 2xx body that names
*neither* envelope raises `CorpusUnavailable` (→ 503) rather than parsing
to a silent empty list — the original SEV-2 was a `{results:[…5 hits…]}`
200 reading back as zero hits.

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
  403 / `-32602` (the 403-projected dispatcher path). The error carries the
  **missing capability** (`required_capability`) and the **identity it
  checked** (`operator_sub` + `tenant_id`), so every surface renders an
  *actionable* diagnostic — "identity `<sub>` (tenant `<id>`) is missing
  capability `meho-docs:<key>`" — instead of an opaque denial (T2 #1802; see
  the cross-surface diagnosability note below).
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

### Cross-collection fan-out (`meho_backplane.docs_search.fanout`, T5 #1554)

`search_docs` (the surface, **not** `ask_docs`) accepts an opt-in
cross-collection scope alongside the single-collection path: an explicit
`collections=[a, b]` list, or the `collection="all"` sentinel (every
entitled, ready collection). Three pieces back it:

- **`parse_collection_scope(collection, collections)`** classifies the
  request into a single scope, a fan-out (explicit keys or the `all`
  sentinel), or an *empty* scope (which falls through to
  `build_docs_scope`'s mandatory-scope 422). The single and fan-out scopes
  are mutually exclusive — supplying both is a 422 / `-32602`.
- **`resolve_entitled_ready_collections(session, operator, *, requested_keys)`**
  (in `collection_access`) enumerates the tenant-visible collections
  (tenant rows override global on key collision), keeps only those the
  operator is entitled to (`meho-docs:<key>`) **and** that are `ready`, and
  **drops the rest with a logged reason** (`not_entitled` / `not_ready` /
  `unknown`) — no silent total truncation. An empty resolved set raises
  `NoEntitledReadyCollectionError` (→ 403 / `-32602`).
- **`search_docs_fanout(operator, query, *, collections, limit)`** queries
  each collection independently on its own backend (concurrently, bounded
  by a semaphore so a wide `all` fan-out cannot open unbounded backend
  connections), tags every chunk with its source `collection`, and merges
  the per-collection ranked lists with **`rrf_merge`** — reciprocal-rank
  fusion keyed on `(collection, chunk_id)` using the house `RRF_K=60`.
  Raw backend scores are never consulted (they are not comparable across
  backends / embedding models), so the merge is purely rank-based and
  deterministic. A fan-out is fail-closed: any one backend's
  `CorpusUnavailable` fails the whole query (503 / `-32603`) rather than
  returning a partial fused list. The audit row's `audit_collection` is the
  sorted, comma-joined queried set.

`ask_docs` stays single-collection permanently (#1548 decision 2) and
rejects both fan-out shapes before any retrieval.

### `expand_docs_query(query, collection, *, llm_client=None)` (`meho_backplane.docs_search.expansion`, #1916)

The corpus-aware **expand** step the `ask_docs` answer pipeline runs
*before* retrieval (and `search_docs`, the raw-chunks tool, deliberately
does **not** — expansion is the answer-pipeline's job only). A terse /
acronym-heavy operator question ("NSX maximums") under-retrieves against a
corpus that spells the term out ("VMware NSX configuration maximums"), so
this step rewrites the question into a small set of query variants:

- **Bounded N.** The returned list always *leads with the operator's
  original question* and adds model-proposed rewrites, capped at
  `MAX_QUERY_VARIANTS` (4, original + 3). Expansion can only *widen* recall
  — the literal query is never dropped, so a useless model degrades to
  retrieving on the original alone (one backend round-trip, the pre-expand
  cost). Blank / duplicate rewrites and a re-cast of the original are
  deduplicated (case-insensitive, whitespace-collapsed).
- **Corpus-aware.** The collection's manifest fields — `vendor` /
  `products` / `description` / `when_to_use`, read straight off the
  resolved `DocCollection` (no new table, no schema change; the data
  already existed, it was just never put in front of a model) — are framed
  into the expansion prompt, so the model expands acronyms and product
  synonyms in the corpus's own domain terms. Empty optional fields are
  omitted rather than framed as bare `None` lines.
- **Fail-closed, like synthesis.** It reuses the **same** #1386 fail-closed
  Anthropic Messages client (`build_anthropic_ingest_llm_client`) via the
  shared `LlmClient` Protocol. No `ANTHROPIC_API_KEY` raises
  `LlmClientUnavailable`; a model that returns non-JSON or a wrong shape
  raises `DocsQueryExpansionError`. Neither is caught in the handler — both
  bubble to `-32603` (the MCP analogue of 503). The pipeline never silently
  skips expansion and answers on the raw question alone.

`DocsQueryExpansionError` is a **distinct** exception type (not
`DocsSynthesisError`, not a bare `RuntimeError`) so the structured
answer-error envelope (#1918, below) attributes a failure to the `expand`
leg (`expand_failed`) specifically rather than a generic catch-all.

Substrate stays dumb (#1177 / #1178): this module only frames the manifest
+ question into a prompt and validates the returned variants. No DSL, no
per-collection weighting, no tunable knob — the LLM does the expansion and
`MAX_QUERY_VARIANTS` is a fixed constant.

### `retrieve_multi_query(operator, queries, *, scope, collection, limit=10)` (`meho_backplane.docs_search.fanout`, #1916)

The same-collection, multiple-query analogue of `search_docs_fanout`
(which is one-query, multiple-collection). Given the variants
`expand_docs_query` produced, it runs the shared single-collection
`search_docs` retrieval once per variant on the **same** backend
(concurrently, bounded by a semaphore — same posture as the
cross-collection fan-out) and merges the per-variant ranked lists with the
**same `rrf_merge`** the cross-collection path uses (rank-based, house
`RRF_K=60`, never a raw-score sort). Single-collection chunks carry
`collection=None`, so the `(collection, chunk_id)` RRF key collapses to
`(None, chunk_id)` — the same chunk surfaced by several variants is
correctly deduplicated and rank-boosted. A single-variant list degenerates
to one retrieval plus a trivial fuse. `CorpusUnavailable` from the backend
propagates unchanged (fail-closed: one down backend → 503, not a partial
list), exactly like the single-query path.

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
- **JSON is machine-forced, not prompt-hoped (#1999).** The synthesis call
  passes the `_SynthesisOutput` JSON schema as the Messages-API
  `output_config.format` (GA structured outputs on `claude-sonnet-4-6`) via
  the richer `StructuredJsonLlmClient.generate_structured_json` seam — the
  model is constrained to emit schema-valid JSON instead of relying on a
  "return ONLY JSON" prompt sentence. (Assistant-turn `{` prefill is *not*
  used — it 400s on the 4.6+ model family.) The parser is also tolerant as
  defence in depth: the shared `extract_json_object` strips a ```` ```json ````
  fence and a prose preamble before `json.loads`, so a model that still
  frames its output does not 502. The **expand** leg
  (`expansion._parse_expansion_output`) gets the same fence tolerance — it
  shared the original bare-`json.loads` bug and survived only because its
  tiny `{"queries": [...]}` object rarely attracted a preamble.
- **Empty retrieval → no model call.** Zero retrieved chunks short-circuit
  to the deterministic `NO_GROUNDED_ANSWER` constant *without* invoking the
  model — the one answer path produced with no LLM call, precisely so it
  cannot hallucinate.
- **Fail-closed synthesis client.** The default client is
  `build_anthropic_ingest_llm_client` (the #1386 Anthropic-Messages
  adapter, reused via the shared `LlmClient` Protocol). No
  `ANTHROPIC_API_KEY` raises `LlmClientUnavailable`; a model that runs but
  breaks the JSON / citation contract raises `DocsSynthesisError`. Neither
  is caught in the handler — both surface as `-32603` (the MCP analogue of
  503). The synthesis model is never relaxed into an ungrounded answer.
- **`DocsSynthesisError` carries a sub-cause (#1918, #1999).** The
  structurally-distinct synthesis failures the message string previously
  buried are split onto `exc.cause`: `SYNTHESIS_CAUSE_PARSE` (output didn't
  parse into the required `{answer, cited_chunk_ids}` shape — non-JSON or
  shape-violating), `SYNTHESIS_CAUSE_TRUNCATED` (the response was cut off at
  the output-token ceiling — `stop_reason == "max_tokens"`; JSON-shaped but
  incomplete), vs. `SYNTHESIS_CAUSE_CITATION_RESOLUTION` (output parsed but a
  cited id didn't resolve to a retrieved chunk). They point at different
  fixes (prompt / model vs. token ceiling vs. retrieval / index drift), so
  the answer-error envelope surfaces the sub-cause. The synthesis client now
  threads the model's `stop_reason` out (via `LlmJsonResult`), and on any
  parse failure the parser logs `stop_reason` plus a **bounded** head/tail
  of the raw body (`_RAW_LOG_HEAD_TAIL=200` chars each end) — never the full
  response, so corpus content cannot leak into logs. The answer leg's
  output-token ceiling was raised (1024 → 2048) so a normal thorough answer
  is not cut off at the boundary.

The client is injectable so tests pin a deterministic stub; production
reuses the spec-ingestion grouping pass's Anthropic key + model, so no new
settings are introduced.

### `classify_answer_error(exc, *, llm_unavailable_leg=LEG_MODEL)` (`meho_backplane.docs_search.answer_errors`, #1918)

The `ask_docs` answer pipeline runs four legs — **expand**, **retrieve**
(corpus), **model** (synthesis call), **synthesis** (parse + citation
resolution of the output) — each with its own typed failure. Before #1918
all four collapsed to one opaque `-32603` `"internal error: <ClassName>"`
at the MCP dispatcher's generic catch, so a consumer could not tell a
config gap (no `ANTHROPIC_API_KEY`) from a backend outage (corpus down)
from a model-output bug (malformed synthesis) — and mis-diagnosed
(`claude-rdc-hetzner-dc#1407` gap 2). This module is the **one**
framework-agnostic place that maps a raised leg exception onto a
structured envelope naming *which* leg failed.

- **Distinct leg + sub-cause per failure.** `classify_answer_error` returns
  an `AskDocsAnswerError` carrying `leg` (one of `expand_failed` /
  `corpus_unavailable` / `model_unavailable` / `synthesis_malformed`) and a
  leg-scoped `cause`: `DocsQueryExpansionError` → `expand_failed` /
  `expansion_invalid`; `CorpusUnavailable` → `corpus_unavailable`;
  `DocsSynthesisError` → `synthesis_malformed` with its parse /
  truncated / citation-resolution sub-cause carried through;
  `LlmClientUnavailable` →
  `model_unavailable` / `client_unavailable` (or `expand_failed` when the
  caller pins `llm_unavailable_leg=LEG_EXPAND`). A non-leg exception returns
  `None` so the caller falls through to its generic catch — a genuinely
  unexpected fault stays a plain `-32603`, not a mis-attributed leg.
- **The one ambiguous type needs a caller hint.** A bare
  `LlmClientUnavailable` is raised by the *same* #1386 client whether the
  expand leg or the synthesis leg reached it, so only the caller (which
  knows the pipeline position) can place it. The MCP handler's
  `_run_answer_pipeline` wraps each leg and passes `llm_unavailable_leg`
  accordingly; the leg's own typed shapes are unaffected.
- **One envelope, every face.**
  `AskDocsAnswerError.to_error_data()` renders a JSON-safe
  `{detail: "ask_docs_failed", leg, cause, message}` dict — the same shape
  on the MCP `error.data` member (raised as `McpInternalError`, code stays
  `-32603`) and on the REST `POST /api/v1/ask_docs` (#1917)
  `HTTPException.detail` (the route picks the HTTP status per leg: 503 for
  `expand_failed` / `model_unavailable` / `corpus_unavailable`, 502 for
  `synthesis_malformed`). This mirrors
  `operations/ingest/error_envelopes.py`, the connector-ingest dual-surface
  precedent. No corpus body or raw LLM output ever rides the envelope.
- **Fail-closed preserved.** Classifying an error never produces an
  answer — a leg failure surfaces as an error envelope, never a degraded /
  ungrounded answer. The `/ui/corpus` Ask mode (#1917) reads the same `leg`
  to render its fail-open-to-chunks banner (the chunks stay, the answer does
  not) via `corpus_ask_fallback_context`; on a **post-retrieval** leg
  (`synthesis_malformed` / `model_unavailable`) it renders the chunks
  retrieval actually returned, carried out-of-band on the in-process
  `AskPipelineOutcome` (#1939) — never on the wire envelope, which stays
  small / JSON-safe.

### `resolve_citation_link(source_url, *, title, document_id)` (`meho_backplane.docs_search.citation_links`, #1919)

A citation's `source_url` is, for the GCS-backed vendor corpus, a **raw
object path** — `gs://meho-knowledge-vmware-corpus/kb/broadcom-kb/articles/41/414551.html`
or `gs://.../community/williamlam/blog/.../post.md`. A browser has no
handler for the `gs://` scheme, so rendering it as an `href` is a dead link
— yet the source identity is in the path (a Broadcom KB article id, a named
community post). This resolver maps each `source_url` to a `CitationLink`
(`label`, `href`, `kind`, `clickable`): a navigable canonical URL where the
source kind allows, a human `label` for the link text, and a `kind` tag
naming the matched rule.

- **Declarative, no per-document config.** A fixed ordered list of rules
  (`_RULES`) keyed on path *shape*, first match wins: `broadcom_kb`
  (`gs://.../broadcom-kb/.../<id>.html` → `knowledge.broadcom.com/external/article/<id>`),
  `community` (`gs://.../community/...` → title + non-clickable path, since
  the mirror path carries no recoverable original URL), `external` (an
  already-canonical `http(s)` source passes straight through as the href).
  Adding a source kind is appending one rule; the substrate stays dumb.
- **Never a broken `gs://` href (the load-bearing invariant).** A `gs://`
  path no rule claims — or a KB object whose filename is not a clean numeric
  id — degrades to a non-clickable `CitationLink` (`href=None`,
  `clickable=False`) tagged `unknown`/its kind, so the caller renders *title
  + path* rather than a dead anchor. A future `stored_object` arm (a
  signed/proxied object link) needs a signing endpoint and is out of scope
  for #1919.
- **Pure — no network I/O.** Links are derived from the path (via
  `urllib.parse.urlsplit` + `pathlib.PurePosixPath`) or from an already-web
  `source_url`. The label is chosen title-first: explicit `title` →
  `document_id` → humanised filename stem → the raw URL (never empty). The
  `title` rung is fed by the optional `DocsChunk.title` pass-through (#2475):
  an upstream corpus title (top-level `title` or `metadata["title"]`, #1732
  discipline, blank → `None`) threads `CorpusChunk.title` → `DocsChunk.title`
  → the `ask_docs` (`_citation_payload`) and `/ui/corpus` (`_cited_chunks`)
  seams, so a hit renders by its human title instead of a raw id. It is
  `None` until the corpus supplies one — MEHO has no ingest path to derive a
  title (federation-only, #1864 → #2049), so today's corpus is unchanged.
- **One resolver, every face.** `citation_link_payload(...)` is the JSON
  form embedded under each `ask_docs` citation's `link` key (the MCP tool and
  the REST `POST /api/v1/ask_docs` route, #1917 — both reuse it unchanged);
  the `/ui/corpus` render calls `resolve_citation_link(...)` per chunk for the
  anchor href + link text. So KB / community / unknown citations resolve
  identically across the answer payload and the console.

### `normalize_source_ref(source_url, *, collection_key, chunk_id, ...)` (`meho_backplane.docs_search.citation_links`, #132)

The wire `source_url` a consumer sees must never carry the storage backend's
scheme (`gs://`, `qdrant://`) or the corpus's internal bucket/directory
layout — the `doc-corpus` contract promises citations are *backend-agnostic;
the agent never sees the backend*. This function is the **single seam** that
enforces that, called from `_project_chunk` (so every `DocsChunk` — the
`search_docs` response chunks **and** each `ask_docs` citation — is born with
a normalized `source_url`; it also keeps the raw path out of the synthesis
prompt).

Normalization is **Option A (canonical public URL) with an Option B (opaque
MEHO ref) fallback**:

- When `resolve_citation_link(source_url)` derives a **canonical public URL**
  (a Broadcom KB article, or an already-`https` source), that URL *is* the
  reference — the most consumer-useful outcome (a clickable citation), and a
  vendor/web URL exposes no MEHO or backend internals.
- Otherwise (a community/unrecognised `gs://` object with no recoverable
  public URL, or no source at all) the reference is an opaque
  `meho://docs/<collection>/<chunk_id>` — uniform regardless of backend,
  resolvable through MEHO, and **never null** (so a chunk always carries a
  usable reference). MEHO owns the reference→object mapping internally.

The raw corpus object path (`gs://meho-knowledge-.../...`) is **never**
returned — that leak is exactly what this closes. Pure Option B (an opaque
ref for *every* chunk) was rejected: it would drop the clickable KB/web URL
the resolver already derives. A consequence of Option A: because the wire
`source_url` for a KB chunk is now the canonical `https://knowledge.broadcom.com/...`
URL, the `link` an `ask_docs`/MCP citation re-derives from it resolves via the
`external` (pass-through) arm rather than `broadcom_kb` — the `href` and
clickability are identical; only the `kind` tag differs. `CorpusChunk.source_url`
(`auth/corpus.py`) stays the raw corpus value — the normalization is applied at
MEHO's projection boundary, not in the corpus adapter.

### `POST /api/v1/search_docs` (`meho_backplane.api.v1.search_docs`, T3 #1552)

The REST face. `operator` role minimum (`read_only` → 403). Validates
the `collection` scope first (422 before any audit binding), binds the
audit contextvars (including `audit_collection`), runs the shared
`resolve_entitled_ready_collection` gate (unknown → 422, not entitled →
403, not ready → 409), then calls the service. Takes a
`Depends(get_session)` DB session for the resolve.

`SearchDocsResponse` is `{chunks: list[DocsChunk], grounded: bool}`. The
`grounded` field (#133) is the **out-of-corpus discipline signal**: `True`
when retrieval returned ≥1 chunk, `False` when it returned none. A consumer
must treat `grounded=False` as *"the corpus has no answer"* and **not** fall
back to ungrounded generation — the feature contract (*empty/low-score = not
in the corpus; do not silently fall back to training data*). It is computed
server-side by the shared
[`retrieval_is_grounded`](../../backend/src/meho_backplane/docs_search/service.py)
seam — the **same** verdict `ask_docs`'s synthesis uses to short-circuit to
`NO_GROUNDED_ANSWER`, so the two surfaces cannot diverge. The MCP `search_docs`
tool returns the same `grounded` key.

**Score scale + what `grounded` does *not* cover.** Each chunk's `score` is
the corpus's raw relevance score — an **opaque, backend-defined scale** MEHO
neither normalises nor thresholds; it is not comparable across collections
and there is no fixed cutoff. `grounded` is therefore **presence-based**, not
relevance-judged: a query that retrieves topically-irrelevant chunks at
high scores still reports `grounded=True` (out-of-corpus scores have been
observed *higher* than in-corpus ones, so no absolute floor separates them).
Catching that case needs a calibrated per-collection score floor, deferred as
the Option-A follow-on (evoila-bosnia/meho-internal#133); it will refine
`retrieval_is_grounded` in the one place both surfaces read.

### `POST /api/v1/ask_docs` (`meho_backplane.api.v1.ask_docs`, T2 #1917)

The REST face of the **answer** pipeline — the synthesis sibling of
`POST /api/v1/search_docs`. `operator` role minimum. Mirrors `search_docs`'s
collection gate exactly (validate `collection` scope → 422; the shared
`resolve_entitled_ready_collection` gate → unknown / cross-tenant / absent
→ 422, not entitled → 403, disabled → terminal 403, transiently not-ready →
409), then runs `run_ask_pipeline` (the in-process
expand → retrieve-per-variant → RRF-merge → synthesize composition) and
returns `AskDocsResponse{answer, citations[]}`, each citation carrying the
#1919 resolved `link` — the **same** citation shape the MCP tool returns.

**Single-collection only**: the request model has no `collections` field and
`extra="forbid"`, so a fan-out attempt is a 422 (matching the MCP contract).

The #1918 per-leg error model maps onto HTTP status: a leg failure is
classified by the shared `classify_answer_error`, raised as
`AskDocsAnswerError`, and mapped to **503** for `expand_failed` /
`model_unavailable` / `corpus_unavailable` (server-side config /
availability faults — the analogue of the MCP `-32603`) and **502** for
`synthesis_malformed` (the upstream model answered, badly — a bad gateway,
distinct from it being unreachable). The structured
`{detail, leg, cause, message}` envelope rides `HTTPException.detail`
byte-identical to the MCP `error.data` member. The answer stays fail-closed
end to end (an empty retrieval is a normal 200 "no grounded answer", not an
error). Binds the canonical `meho.docs.ask` audit op_id + `read` class
before the pipeline runs, so a leg failure is still attributable.

### `/ui/corpus` Ask mode (`meho_backplane.ui.routes.corpus.routes`, T2 #1917)

The console face. The `/ui/corpus` search surface (#1777) gains a
**Retrieve / Ask** mode toggle on its query form (radio buttons riding the
form, default Retrieve). `mode=ask` calls `run_ask_pipeline_capturing_retrieval`
in-process (the Bearer-gated REST route cannot be authed by a session
cookie — the established BFF pattern) and renders the grounded `answer` +
its citation cards via the `answer` branch of `corpus/_results.html`. On an
`AskDocsAnswerError` leg failure the Ask mode **fails open to chunks**: the
`corpus_ask_fallback_context` seam (#1918) renders the retrieved chunks
under a banner naming the failed leg. A **post-retrieval** leg
(`synthesis_malformed` / `model_unavailable`) renders the chunks retrieval
actually returned — carried back on the `AskPipelineOutcome` channel (#1939)
rather than dropped — so the operator keeps the usable grounding even though
the synthesized answer was rejected; a **pre-retrieval** leg (`expand_failed`
/ `corpus_unavailable`) produced no chunks, so the banner stands alone. Never
an ungrounded answer. Collection-access failures render the same typed
403 / 409 / 422 error card as retrieve mode; an unrecognised `mode` degrades
to retrieve. CSRF double-submit gated like the search fragment.

### `meho docs search` (`cli/internal/cmd/docs`, T5 / T3 #1552)

The operator-facing CLI verb. `meho docs search <query> --collection <c>
[--product <p>] [--version <v>] [--limit N] [--json]` POSTs to
`/api/v1/search_docs` via the shared generated authed client (bearer +
lazy 401-refresh), fails fast on a missing `--collection` before the
round-trip (`--product` / `--version` are optional refinements), maps
the 403 (not entitled) / 409 (not ready) / 422 (unknown collection)
statuses, and renders the cited chunks as a text table or raw JSON. It
consumes the generated `api.SearchDocsRequest` / `api.SearchDocsResponse`
/ `api.DocsChunk` types directly — no hand-typed copies of the backend
schemas.

**Gating — server-side only, CLI ↔ REST parity (#2109).** The `meho
docs` tree compiles into every CLI binary and is **always** visible;
it carries **no client-side capability pre-check**. Access is decided
server-side by the backplane, identically to `POST /api/v1/search_docs`:
the route enforces the per-collection `meho-docs:<collection>`
entitlement (a miss is a 403 `not_entitled` the CLI renders as
`insufficient_role`, exit 5) and the operator / tenant_admin role. The
CLI is a thin shell over the same route the REST surface exposes, so the
same `(query, collection, tenant)` gets the same verdict on either
surface — the unstated CLI ↔ REST parity contract now holds by
construction.

**Why the client-side gate was removed.** An earlier shape (option B)
read the bare `meho-docs` capability out of the stored JWT at
command-tree-build time and, when absent, marked the tree `Hidden` and
refused every verb with a typed `addon_not_provisioned` (exit 5) before
any network call. That gate had **no counterpart on the REST route**:
`POST /api/v1/search_docs` never checks the bare `meho-docs` capability
— only the per-collection `meho-docs:<collection>` one (see
`_resolve_collection_or_http_error` → `resolve_entitled_ready_collection`).
So the two surfaces diverged — a tenant entitled to a collection via
REST could still hit `addon_not_provisioned` on the CLI, and a CLI-shaped
verification probe mis-reported a contract the REST endpoint implemented
correctly. #2109 recorded the operator decision (option A): reconcile to
one server-gated op. The client-side pre-check (the `Capability` const,
`tenantHasDocsCapability`, `capabilitiesFromJWT`, `errNotProvisioned`,
and the `provisioned` / `Provisioned` plumbing) is gone; the backplane
is the single gate. The static `required_capability="meho-docs"` gate on
the **MCP tool** is a separate surface (tool *visibility*) and is
unchanged — it was never part of the REST/CLI asymmetry.

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

The handler runs the **expand → retrieve-per-variant → RRF → synthesize**
pipeline (#1916) and mirrors `search_docs`'s error arms plus the expand +
synthesis arms: `build_docs_scope` + the shared gate enforce the collection
scope (`MissingDocsFilterError` / unknown / not-entitled → `-32602`);
`CorpusUnavailable` from retrieval and a not-ready collection bubble to
`-32603`; and the LLM-leg failures (`LlmClientUnavailable` for an
unconfigured expand *or* synthesis model — both reuse the #1386 client;
`DocsQueryExpansionError` for an unusable expansion;
`DocsSynthesisError` for a broken grounding contract) also bubble to
`-32603` — never an un-expanded / ungrounded 200. It stays `op_class="read"`: it
composes over retrieved chunks, it never mutates the corpus. The
dispatcher writes one `audit_log` row per call with `op_id="meho.docs.ask"`
(the handler binds `audit_op_id`, lifted into the persisted row — uniform
across REST / CLI / MCP per G4.5-T8 #1549; the bare tool name still feeds
`classify_op`, which leaves it as `other` while the tool definition pins
the row's `op_class="read"`) and the raw query hashed into `params_hash` —
the same privacy posture as `search_docs`.

Each returned citation is enriched with a resolved `link` (#1919) via
`citation_link_payload(...)` — `{href, label, kind, clickable}` — so a
consumer renders the human title pointing at the canonical source URL (KB →
`knowledge.broadcom.com`, `http(s)` → pass-through) rather than the raw
`gs://` object path the corpus stores. The raw `source_url` stays on the
citation for provenance. See *the citation-link resolver* above.

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

## Cross-surface entitlement contract + diagnosability (T2 #1802)

All three answerable surfaces — the REST route, the `search_docs` /
`ask_docs` MCP tools, and the `/ui/corpus` BFF — gate on the **one** shared
`resolve_entitled_ready_collection` check, which reads exactly two fields
off the `Operator`: `tenant_id` (scopes the tenant-first resolve) and
`capabilities` (holds `meho-docs:<key>`). The entitlement contract is
**verified consistent** because all three build that `Operator` from the
**same constructor** via `verify_jwt_for_audience` → the chassis JWT chain,
which lifts `tenant_id` from `jwt_tenant_claim_name` and `capabilities` from
`jwt_capabilities_claim_name` — identical claim derivation, no per-surface
divergence. (The UI `UISessionContext.tenant_id` from the session row is
used only for the page-header chip + `operator_sub` display; the
entitlement path reconstructs a full token-derived `Operator` via
`verify_access_token_with_refresh`, so the check never mixes a session-row
`tenant_id` with token capabilities.)

The **one deliberate divergence** is the *audience* each surface validates
the token for: REST and the UI BFF both use `settings.keycloak_audience`
(the HTTP-API audience), while MCP uses `mcp_resource_uri(settings)`
(`<backplane_url>/mcp`). This is intentional and spec-driven (RFC 8707
resource-scoped tokens), but it means a Keycloak realm that mints
**per-audience** tokens can carry a different `meho-docs:*` claim set per
audience — the reported asymmetry where the MCP tool succeeds while REST /
the UI session 403 or render empty. That is a **Keycloak claim-mapper
config gap, not a backend bug**: the fix is to attach the `meho-docs:<key>`
capability claim to *every* audience the operator uses (see
`deploy/values-examples/README.md` § "Docs-corpus entitlement claim
(`meho-docs:*`) is per-audience"). The cross-surface invariant — same
`(tenant_id, capabilities)` source contract, single deliberate audience
divergence — is asserted by `tests/test_docs_entitlement_cross_surface.py`.

Because the divergence is invisible without help, every surface now emits an
**actionable** diagnostic instead of an opaque denial (T2 #1802):

- **REST `POST /api/v1/search_docs`** — the not-entitled 403 is a structured
  body `{"error": "not_entitled", "collection", "required_capability",
  "operator_sub", "tenant_id", "message"}`.
- **`/ui/corpus`** — the search 403 card surfaces the same `message`; and the
  empty collection picker, when the catalogue holds a collection the
  identity cannot see, names the concrete missing `meho-docs:<key>` +
  `operator_sub` + `tenant_id` (distinct from the genuinely-unprovisioned
  "no corpus exists" empty state).
- **MCP `search_docs` / `ask_docs`** — the `-32602` message names the missing
  capability + identity, and `error.data` carries
  `{"reason": "not_entitled", "required_capability"}` for self-correction.

## Known issues / boundaries

- The corpus request/response contract is a **consumer-side** dependency
  (the corpus is owned by the ops team). The `CorpusChunk` adapter pins
  only the fields MEHO consumes; a corpus that drops a consumed field
  fails closed as `CorpusUnavailable` rather than returning a partial
  result.
- No local indexing — federation only. MEHO gains no Qdrant dependency
  and does not absorb the corpus into its own substrate.
- `ask_docs` is **single-shot** Q→cited-A only — no multi-turn /
  conversational follow-up. The corpus-aware expand step (#1916) widens
  recall via bounded multi-query + RRF, but there is still no per-collection
  *weighting* or tunable ranking knob (binary scope + rank-based RRF only,
  per #1177 / #1178) — the LLM does the expansion, the merge is deterministic.

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
- Corpus-aware expand (#1916): `backend/src/meho_backplane/docs_search/expansion.py`
  (`expand_docs_query`, `DocsQueryExpansionError`, `MAX_QUERY_VARIANTS`);
  multi-query retrieve + RRF merge: `retrieve_multi_query` in
  `backend/src/meho_backplane/docs_search/fanout.py`.
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
