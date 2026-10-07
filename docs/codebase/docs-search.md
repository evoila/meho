# search_docs / ask_docs (the meho-docs add-on)

## Overview

`search_docs` is the federated vendor-document retrieval surface of the
`meho-docs` add-on (Initiative #1518). Unlike `search_memory` /
`search_knowledge` — which read MEHO's own Postgres+pgvector substrate
(see [retrieval.md](retrieval.md)) — `search_docs` does **not** ingest
the vendor corpus. It proxies each query through the backplane to the
**external** corpus service the ops team runs, presenting a
deployment-configured corpus service credential (never the caller's
operator JWT — see the SSRF + credential note below, #290).

Routing through the backplane (rather than letting clients hit the
corpus directly) is what buys three properties in one place:

- **Central audit.** Every query lands one `audit_log` row under the
  named op `meho.docs.search`, so `query_audit` / who-touched surface
  it (the raw query is hashed, never stored).
- **Downstream federation handled once.** The screened-`https` dial and
  the corpus service credential live in the T2 client, not in every
  consumer.
- **Mandatory collection scope + per-collection entitlement enforced
  centrally.** A docs query without a `collection` is rejected —
  fail-closed — and a tenant may only search collections it holds the
  `meho-docs:<collection>` capability for, so no caller can run an
  unscoped query or reach a collection it isn't entitled to. `product` /
  `version` are optional refinements within the chosen collection, sent
  to its backend only when the collection opts in, as a soft scope or as
  filters (see *Scope gates* below).

The same `search_docs` service backs four consumers: the REST route
(T3), the MCP tool `search_docs` (T4, #1523), the CLI verb
`meho docs search` (T5, #1524), and the synthesis tool `ask_docs` (T7,
#1526). They share one service so the REQUIRE_FILTERS gate and the
cited-chunk shape are defined exactly once.

`ask_docs` returns one grounded, cited answer, `{answer, citations[]}`.
**Where the answer comes from is chosen per collection** (#3911):

- **Upstream.** A collection whose backend offers its own grounded-answer
  endpoint, and that opts in with `backend.ref["answer"] = "upstream"`, is
  answered by that endpoint (for MEHO Knowledge: `POST /ask?include=hits`).
  One answer call, no search call; the answer and its citations are mapped
  into the same response shape (see `answer_docs_question` under Key
  types). The backplane's own model is not called for it.
- **Local.** Every other collection runs the backplane's own pipeline,
  described next. This is the default: no collection is switched by
  shipping the code.

The local pipeline runs a corpus-aware **expand** step, retrieves per
expanded variant, RRF-merges the chunks, and then composes one grounded
answer over them. The pipeline is
**expand → retrieve (per variant) → RRF-merge → synthesize** (#1916). The
grounding contract is enforced in code, not just in the prompt — no claim
survives without a citation that resolves to a retrieved chunk, an empty
retrieval returns a deterministic "no grounded answer" (never a guess), and
an unconfigured **expand or synthesis** model fails closed rather than
degrading to an un-expanded / ungrounded answer. The expand step is the
answer-pipeline's job only — `search_docs` (the raw-chunks tool) is
unchanged.

`ask_docs` is exposed over **three** faces, all composing their answer
through **one** in-process seam,
`meho_backplane.docs_search.answer.answer_docs_question` (#3911): the MCP
`ask_docs` tool (T7, #1526), the REST `POST /api/v1/ask_docs` route (T2,
#1917 — the synthesis sibling of `POST /api/v1/search_docs`), and the
`/ui/corpus` **Ask mode** (T2, #1917 — a toggle alongside the original
retrieve mode). The seam returns an `AskPipelineOutcome` (the answer, or the
classified #1918 `AskDocsAnswerError` plus the chunks retrieved before it,
and the `answer_source`). The REST route calls `run_ask_pipeline` (the thin
raising wrapper in `meho_backplane.api.v1.ask_docs`), the UI BFF calls
`run_ask_pipeline_capturing_retrieval` (the seam under its established
name) so it can fail open on a leg failure, and the MCP handler calls the
seam directly — the MCP module no longer carries its own copy of the
pipeline. `ask_docs` is single-collection only on every face (no
`collections` fan-out field).

## Key types

### `search_corpus(...)` (`meho_backplane.auth.corpus`, T2 #1520)

The transport. An async `httpx` client that screens the corpus URL
(`https` + a public, allowlist-aware host) and then POSTs a search
request to it carrying the deployment-configured corpus service
credential (`settings.corpus_service_token`), bounded by
`settings.corpus_timeout_seconds`. The URL and RFC 8707 audience are
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

**Page identity, score direction and the upstream link (#3913).** Each
`CorpusChunk` also keeps six optional fields:

- `filename`, `breadcrumb` (the page's place in its document tree,
  `>`-separated) and `heading_path` (the headings above the chunk): the
  page identity a citation title is derived from (`derive_chunk_title`,
  below);
- `score_kind`: the direction of `score`, `distance` (lower is better) or
  `similarity` (higher is better);
- `upstream_url` and `upstream_page`: the backend's public `http(s)` link to
  the hit's source page or document (for a PDF with a known page it already
  ends in `#page=N`) and that page, counted in the whole source document.

They label and link a hit; they never decide whether it is grounded. So an
unusable value reads as absent instead of failing the parse: a non-string
name is `""`, `heading_path` keeps only its strings, an unknown
`score_kind` is `None` (never a guessed direction), `upstream_url` must be an
`http(s)` URL with a host and no space, control or bidi character (≤ 2048
characters; `javascript:`, `gs://` and a URL that does not parse are
dropped), and `upstream_page` must be a whole number from 1 to 1,000,000 (a
larger number is no real page, and a huge one would break the CLI's decoding
of the whole response). `upstream_url` has one rule, `web_link_or_none`:
the parse and the citation reference (`normalize_source_ref`) both use it.

The search request sends **no** `with_rerank`: how hits are ranked is the
backend's decision, on search as on the answer call.

Fail-closed by construction: an unconfigured (no URL), unreachable,
non-2xx, or malformed-response corpus all collapse to one typed
`CorpusUnavailable`. The exception carries the upstream HTTP status (when
the failure was a non-2xx response) but **never** the response body — a
corpus error page cannot leak through.

**Destination screen + downstream credential (#290).** The corpus URL is
read from a *tenant-configurable* `backend.ref`, so two controls stop a
`tenant_admin` from turning it into a credential-capture + SSRF sink:

- Every dial (search **and** the `corpus_status` readiness probe) requires
  the `https` scheme and screens the resolved host through the shared
  target SSRF guard (`assert_public_destination_async`, the same guard the
  connector target dial uses), so a corpus endpoint cannot be pointed at
  `http://`, loopback, RFC 1918 space, or `169.254.169.254`. An on-prem
  corpus on private space is opted in via `MEHO_TARGET_SSRF_ALLOWLIST`. The
  same endpoint is also screened at collection **create** time
  (`docs_collections.service`) so a bad endpoint is a 422, never persisted.
- The bearer presented to the corpus is `settings.corpus_service_token` —
  a deployment-owned, corpus-scoped service credential, **not** the
  caller's inbound operator JWT. Replaying the operator's Vault-capable
  bearer to the tenant-configurable URL was the capture vector; the JWT is
  no longer forwarded. An empty token sends no `Authorization` header (a
  corpus requiring auth then fails closed). Tradeoff: the corpus's own
  audit attributes the call to the service principal, not the operator;
  MEHO's central `audit_log` is unaffected.

### `ask_corpus(...)` (`meho_backplane.auth.corpus`, #3911)

The answer transport, used only for a collection that opted in to its
backend's grounded answer. It POSTs `{query, top_k}` (plus `audience` when
set) to the answer endpoint with `?include=hits`, behind the same posture as
`search_corpus`: the `https` + SSRF destination screen, the deployment's
corpus service credential (never the operator JWT), and the response body
never echoed. It deliberately sends **no** `with_rerank` (ranking policy is
the backend's). The product / version refinements ride the body only as the
collection's scope gates decide (*Scope gates* below, #3912): the soft
scope as `scope`, the hard filters as `filters` (the answer endpoint's name
for search's `metadata_filters`). Each key is omitted when there is nothing
to send, so with both gates off the body is exactly `{query, top_k}` (plus
`audience`), the body the answer call sent before the gates were wired. Its
own bound is
`CORPUS_ANSWER_TIMEOUT_SECONDS` (default **60**; the search bound stays
`CORPUS_TIMEOUT_SECONDS`, default 10), kept below any proxy read timeout in
front of the corpus.

The response is parsed by `UpstreamAnswer`: the `answer` text, the
`citations` (`chunk_index` + `chunk_id`), the `hits` (each a `CorpusChunk`,
the search-hit shape with the page identity, `score_kind` and upstream link
fields above) and the backend `timing` (`total_ms` / `llm_ms`). `hits` is
**required**, so a 2xx without them fails parse loudly. A transport failure
(unconfigured, blocked, unreachable, timeout) is `CorpusUnavailable` as on
search; a response that is not a usable answer is `CorpusAnswerError` with a
`kind`: `rejected` (4xx), `answer_unavailable` (503 with the backend's
`llm_unavailable` code), `rate_limited` (503 with `llm_rate_limited`, or a
429; carries the `Retry-After` in seconds), `server_error` (any other 5xx) or
`malformed` (a 2xx body that does not parse). Only those two backend error
codes are ever read from an error body. `Retry-After` is read in either RFC
9110 form (ASCII delta-seconds or an HTTP-date) and clamped to
`_RETRY_AFTER_MAX_S` = 3600 s; anything else (blank, signed, non-ASCII, an
unparseable or overflowing date) is dropped as `None`, never raised, so a
garbled header cannot turn the typed rate-limited error into a 500.

### Backend-agnostic search router (`meho_backplane.docs_search.backends`, T2 #1551)

The `collection → backend` router that keeps MEHO a backplane, not a
vector DB: one collection can sit on a managed RAG and another on the
JWT-forward corpus **behind the same `search_docs`**, and the agent never
sees which backend answered. Four pieces, modelled on the connector
registry (`connectors/registry.py`) **minus the version tie-break ladder**
(a collection binds to exactly one backend by construction, #1548):

- `SearchBackend` (`backends/base.py`) — the adapter ABC. One required
  `async search(operator, query, *, backend_ref, metadata_filters,
  soft_scope, limit) -> CorpusSearchResponse` (the same shape as the
  re-homed transport, so the seam swap is behaviour-preserving;
  `soft_scope` is the #3912 soft scope) plus a `probe()` forward seam
  for the readiness probe (T6 #1555) that defaults to raising rather than
  claiming "ready". A class-level `backend_type` string is the routing
  discriminator. The optional **answer** seam (#3911) follows the `probe()`
  precedent: `supports_answer(backend_ref) -> bool` (default `False`) and
  `async answer(operator, query, *, backend_ref, filters, soft_scope, limit)
  -> UpstreamAnswer` (default raises `NotImplementedError`; `filters` /
  `soft_scope` are the #3912 gate's output, as on `search`). A backend
  without an answer endpoint keeps the local answer pipeline.
- `CorpusHttpBackend` (`backends/corpus_http.py`,
  `backend_type="corpus-http"`) — the **first** concrete adapter. It
  wraps `search_corpus` (the well-tested transport, not a copy of the
  httpx body) and resolves the per-collection endpoint / audience from
  the collection's `backend.ref` (keys `endpoint`/`url` and `audience`),
  falling back to the legacy `corpus_url` / `corpus_audience` globals for
  an unmigrated single-collection deploy. It fronts whatever the ops
  corpus proxies; a direct managed-RAG adapter with its own
  service-account auth is a deliberate **later Task**, not built here.
  `supports_answer` is true only for `backend.ref["answer"] == "upstream"`
  (exact match). `answer()` calls `ask_corpus` against
  `backend.ref["answer_endpoint"]` when set, else the resolved search
  endpoint with its last path segment replaced by `ask`
  (`…/search` → `…/ask`, `…/v1/search` → `…/v1/ask`; `derive_answer_url`),
  handing it the gated `filters` / `soft_scope` unchanged.
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
the chosen collection: present, they reach the backend only through the
scope gates (below), as a soft scope or as `as_filters()`; absent, the
collection alone scopes the query. Blank-after-strip values are treated
as absent so `collection=" "` cannot smuggle past the gate. Returns a
frozen `DocsScope` carrying `collection_key` plus the optional
refinements; `as_filters()` renders **only** the refinements into the
`{key: scalar}` `metadata_filters` shape — a **binary containment
scope**, never a ranking weight (the #1178 / #1177 decision). The soft
scope (#3912) is the separate ranking form, built from the same values.
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
`backend.search(...)` with the optional product/version refinements as a
soft `scope` or as `metadata_filters` when the collection opts in (the
gates below), projects the backend's `CorpusChunk`s into MEHO's own
`DocsChunk` surface (chunk text + source citation + title + score), and
propagates `CorpusUnavailable` unchanged. The backend id never appears in the request
or the projected response (the backend-agnostic contract). It logs
`docs_search_completed` with the *requested* `product` / `version`,
`scope_forwarded` (how they reached the backend: `"soft"`, `"filters"` or
`"none"`) and the hit fields (`hit_count`, `hit_chunk_ids`,
`hit_source_refs`; see [Per-call logs](#per-call-logs-3915)).

**The projection (`_project_chunk`, #3913).** Every `DocsChunk` — each
`search_docs` hit, each fan-out hit and each `ask_docs` citation on both
answer paths — is born here, so all faces agree:

- `title` is the corpus's own `title`, else the derived title
  (`derive_chunk_title`): the chunk's section (its last heading, else the
  breadcrumb tail) joined with its page name (the humanised filename stem),
  `What's New — product 2 1 release notes`. A section alone says which part of
  a page a chunk is from, not which page or release; the page name says
  that. When only one of the two exists it is the title; when both say the
  same thing (case and punctuation ignored) the section is the title; with
  neither it is `None`. Only the file's own name is read, never a full
  object path.
- `source_url` is the normalised reference (`normalize_source_ref`, below):
  the hit's own `https` link, else the backend's `upstream_url`, else a
  link derived from the object path, else `meho://docs/<collection>/<chunk_id>`.
- `score_kind`, `upstream_url` and `upstream_page` pass through unchanged,
  `null` when the backend did not send them (never defaulted). `chunk_id` is
  always kept, so the `meho://docs` chunk resource still finds a hit whose
  `source_url` is now a public link.

The REST and MCP `search_docs` payloads are the `DocsChunk` fields, so both
carry these keys; the CLI table is unchanged (`--json` shows them).

### Scope gates (`forwarded_scope`, `backend.ref.scope` / `backend.ref.scope_filters`, #3912)

`product` / `version` reach a collection's backend only when the
collection opts in. Its `backend.ref` holds two independent gates, both
off by default:

| `backend.ref` | What reaches the backend | Request key (search) | Request key (upstream answer, #3911) |
|---|---|---|---|
| neither key (the default) | nothing: the collection alone scopes the query | — | — |
| `"scope": "soft"` | a **soft scope**, `{"product": …, "version": …, "source": "caller"}`, with the values exactly as the agent gave them; a key the agent did not give is omitted | `scope` | `scope` |
| `"scope_filters": true`, no `scope` key | **hard filters**, `{"product": …, "version": …}`: an exact-match pre-filter | `metadata_filters` | `filters` |
| both | the soft scope wins: `scope` is sent and no filters are | `scope` | `scope` |

Only the exact string `"soft"` turns the soft gate on, and only the JSON
boolean `true` turns the filter gate on: `"SOFT"`, `"true"` or `1` count
as off. When the agent gives neither `product` nor `version`, nothing is
sent, whatever the gates. The cross-collection fan-out sends neither form.

**The soft scope (owner decision D1, 2026-10-04).** On the shared VMware
collection, product and version are ranking signals, never default hard
filters (evoila-bosnia/MEHO.Knowledge#512). A product + version filter
derived from the question lost far more answers than it gained: many
answers sit in version-agnostic articles, advisories and identifier pages
that a filter drops, a strict filter can return nothing, and a
`MAJOR.MINOR` filter cannot tell a maintenance release (`9.1.1`) from its
sibling (`9.1.0`). The backend's soft `scope` ranks the asked release
first and labels its answer, never filters, and normalises the values
itself: product names (`vcenter` to its own token) and any release form
(`9.1.1`, `8.0 U3`, `8.0.3.00400`). So MEHO sends the values unchanged
and does no mapping of its own.

**The hard filters.** A backend that honours `metadata_filters` applies
them as an exact-match pre-filter over its own per-document metadata, so
the values must be the ones the collection stamps. On the shared
`vmware` collection they mostly are not what an agent would guess:
vCenter and ESXi are stamped `vsphere`, SDDC Manager `vcf`, Aria
Operations `vcf-operations`, NSX ALB `avi`, SRM `live-recovery`; versions
are stamped `MAJOR.MINOR`, `<major>.x` or `n/a`, and most documents (every
KB article and security advisory among them) carry no version at all.
evoila-bosnia/MEHO.Knowledge#509 normalises the filters (a version filter
keeps version-less documents, `vcenter` → `vsphere`, patch versions →
`MAJOR.MINOR`). The filter gate stays off for the shared collection; it
is for a collection whose backend applies exact-match filters usefully.

**Why gates at all.** The backend refuses unknown request keys
(evoila-bosnia/MEHO.Knowledge#496), so `scope` must never reach a backend
release that does not accept it. With the soft gate off, the request body
is exactly what it was before the gate existed: `search_corpus` adds the
`scope` key only when it has a soft scope to send. The backplane must not
depend on the backend release landing first, so each collection opts in
once its backend is ready.

**Where it applies.** `forwarded_scope(scope, backend_ref)`
(`docs_search/service.py`, exported from `docs_search`) is the one rule.
It returns a frozen `ForwardedScope(mode, filters, soft_scope)`: `mode` is
`"soft"`, `"filters"` or `"none"`, and at most one of the two dicts is
non-empty. `search_docs` applies it and passes
`metadata_filters=forwarded.filters or None` and
`soft_scope=forwarded.soft_scope or None` to `SearchBackend.search`;
`CorpusHttpBackend` hands both to `search_corpus`, which sends the soft
scope as the body's `scope` object. That covers every single-collection
retrieval: the REST route, the MCP `search_docs` tool, the CLI verb, the
per-variant retrieval of the local `ask_docs` pipeline, the docs-chunk
resource's re-search and the UI corpus search. The cross-collection
fan-out passes `metadata_filters=None` and `soft_scope=None`.

The **upstream answer call** (#3911) goes through the same helper.
`answer_docs_question` applies `forwarded_scope` to the collection's
`backend.ref` and passes `filters=forwarded.filters or None` and
`soft_scope=forwarded.soft_scope or None` to `SearchBackend.answer`;
`CorpusHttpBackend` hands both to `ask_corpus`, which sends the hard
filters as the answer body's `filters` (the key the backend's answer
endpoint reads) and the soft scope as its `scope`. Same precedence, same
look-alike rule; with both gates off the answer body is byte-for-byte the
#3911 body, `{query, top_k}` (plus `audience`). That covers the REST and
MCP `ask_docs` faces, which pass `product` / `version` through; the UI Ask
form has no such fields, so it never sends either.

**What is recorded.** The REST audit row keeps the requested `product` /
`version`, whatever the gates. The `docs_search_completed` and
`docs_ask_completed` logs keep them too, next to `scope_forwarded`
(`"soft"`, `"filters"` or `"none"`: what was actually sent, so `"none"`
also when nothing was requested), and so does the
`docs_search_scoped_zero_hits` warning; `docs_ask_upstream_failed`
carries `scope_forwarded` as well.

**Vocabulary.** The tool descriptions tell the agent to use the
collection's own product tokens (`products` in `list_doc_collections`)
and to give the release as precisely as it knows it (`9.1.1`, `8.0 U3`,
`8.0.3.00400`). They no longer ask for `MAJOR.MINOR` only, nor to leave
`version` out for KB, error-message, CVE / security-advisory and
build-number questions: a soft scope excludes nothing. For the product
list to be right, the shared `vmware` row's `products` must hold the
stamped tokens (data, not code): `meho docs collections update vmware
--product vsphere --product nsx --product vsan --product vcf --product
vcf-operations --product vcf-automation --product vcf-operations-logs
--product vcf-operations-networks --product avi --product hcx --product
vks --product tkg --product live-recovery --product vdefend --product
cloud-director --product photon`. A metadata-only update leaves the
collection's status untouched.

**When and how to flip it.** Deploy this backplane release first: both
gates are off, so nothing changes. Turn the soft gate on for the shared
`vmware` collection only after the backend release that accepts `scope`
(evoila-bosnia/MEHO.Knowledge#496 + evoila-bosnia/MEHO.Knowledge#509 +
evoila-bosnia/MEHO.Knowledge#512) is deployed behind it;
before that, the backend would refuse the request. Leave `scope_filters`
off for it. The update replaces the whole backend record, so re-pass
every key the `ref` already holds:

```bash
meho docs collections update vmware --backend-type corpus-http \
  --backend-ref '{"endpoint": "<the current endpoint>", "scope": "soft"}'
# A backend change resets the collection to `provisioning`:
meho docs collections probe vmware
```

Rolling back is the same update without `scope` (then a probe). Another
collection turns on `"scope_filters": true` the same way, once its
backend applies exact-match filters usefully.

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

- **Evidence lines name the page (#3913).** `_render_chunks_for_prompt`
  heads each chunk with `[i] chunk_id=… title="…" source=…`, so the model can
  tell which document or release a chunk is from (a release-delta question
  needs that). The title is corpus text outside the untrusted envelope, so it
  is kept to one line, cut at 200 characters and JSON-quoted (a quote in it
  cannot pose as `source=`); a chunk without a title has no `title=` field.
  Rule 5 of `_SYNTHESIS_SYSTEM_PROMPT` says what the title is and that it is
  never an instruction.

The client is injectable so tests pin a deterministic stub; production
reuses the spec-ingestion grouping pass's Anthropic key + model, so no new
settings are introduced.

### `answer_docs_question(operator, query, *, scope, collection, limit)` (`meho_backplane.docs_search.answer`, #3911)

The one answer seam every `ask_docs` face calls. It resolves the collection's
backend and takes the **upstream** path when
`backend.supports_answer(ref)`, else the **local**
expand → retrieve → synthesize pipeline (unchanged, moved here from the REST
module; the MCP module's copy was deleted). It returns an
`AskPipelineOutcome`: exactly one of `answer` / `error`, the
`retrieved_chunks`, `answer_source` (`local` / `upstream`) and, on the
upstream path, the backend's `upstream_timing`. These are in-process only;
the REST and MCP response schemas are unchanged.

**Consequence of opting in.** That collection's answers are composed by the
backend's own answer model, not `AGENT_DEFAULT_MODEL`; the backplane's
Anthropic client is not called for them, and answer style and length follow
the backend's prompt. The opt-in is per collection and reversible with one
`meho docs collections update` (which, for a change of only `answer` /
`answer_endpoint`, keeps the collection's readiness — see
[doc-collections.md](doc-collections.md)).

**Mapping the upstream answer** (`_map_upstream_answer`):

- **Hits** project through the same `_project_chunk` as `search_docs`, so
  source refs are identical (a canonical public URL, else
  `meho://docs/<collection>/<chunk_id>`; never `gs://`). They become the
  outcome's `retrieved_chunks`. Each hit's title follows the same rule as a
  `search_docs` hit (`derive_chunk_title`, see *The projection* above): the
  backend's `title`, else the section (last heading, else breadcrumb tail)
  joined with the page name (humanised filename). A citation is the projected
  hit it names, so it carries the hit's `score_kind`, `upstream_url` and
  `upstream_page` too; the copies of those fields on the answer body's
  `citations[]` are not read.
- **Citations** are walked in response order, de-duplicated by `chunk_id`,
  and matched to a hit **by `chunk_id`** — never by position, because the
  backend returns its hits reordered to citation order. A citation that does
  not resolve to **exactly one** hit (no matching hit, a blank `chunk_id`, or
  a `chunk_id` several hits share) is `synthesis_malformed` /
  `citation_resolution`, the same "every citation resolves to a retrieved
  chunk" invariant as the local path; it never guesses the first of several
  hits. The error message carries counts, never an upstream-chosen id.
- **Answer markers.** The backend marks claims `[N]`, `N` being the 0-based
  `chunk_index` of the chunk its model saw. Each becomes `[k]`, the 1-based
  position of that chunk in the returned `citations`; a marker with no
  matching citation is dropped (with the spaces before it). The UI numbers
  its citation cards with the same `k`.
- **No hits** (and no citations) is the deterministic `NO_GROUNDED_ANSWER`
  with empty citations, as on the local path. Hits without citations keep
  the backend's text (markers dropped) with no citations.

**Errors** reuse the four legs with new `cause` values (no new taxonomy).
The REST status is chosen per (leg, cause):

| Upstream outcome | leg / cause | REST | MCP | UI Ask |
|---|---|---|---|---|
| Transport error / timeout | `corpus_unavailable` / `corpus_unavailable` | 503 | -32603 | banner + one search |
| 503 `llm_unavailable` (no answer model) | `model_unavailable` / `upstream_answer_unavailable` | 503 | -32603 | banner + one search |
| 503 `llm_rate_limited` (or 429) | `model_unavailable` / `upstream_rate_limited` | 503 + `Retry-After` forwarded, `detail.retry_after` | -32603, `data.retry_after` | banner + one search |
| Other 5xx | `corpus_unavailable` / `upstream_error` (`upstream_status`) | 503 | -32603 | banner + one search |
| 4xx | `corpus_unavailable` / `upstream_rejected` (`upstream_status`) | **502** | -32603 | banner + one search |
| 2xx, malformed body | `synthesis_malformed` / `parse` | 502 | -32603 | banner + one search |
| Citation outside the hits, blank, or shared by several hits | `synthesis_malformed` / `citation_resolution` | 502 | -32603 | banner + one search |

A 4xx is a 502, not a 503: a rejected request is a contract or configuration
fault, not an outage. `upstream_status` / `retry_after` appear on the
envelope only for an upstream failure, so the local envelope is unchanged.

**Scope gates.** The upstream answer call carries the collection's
`forwarded_scope` decision (#3912): the soft `scope`, the hard `filters`,
or neither (see *Scope gates* above). The local path's searches apply the
same rule per variant.

**Logs.** Success logs one `docs_ask_completed` event with `collection_key`,
the requested `product` / `version`, `scope_forwarded`, and the ids-only
ask fields of `ask_log_fields` (#3915; see
[Per-call logs](#per-call-logs-3915)): `answer_source`, `hit_count`,
`hit_chunk_ids`, `hit_source_refs`, `citation_count`, `cited_chunk_ids`
and, upstream, the backend's `upstream_total_ms` / `upstream_llm_ms`. An
upstream failure logs `docs_ask_upstream_failed` (leg, cause,
`scope_forwarded`, `upstream_status`, `retry_after`), plus the hit fields
when the backend returned hits that failed to map
(`citation_resolution`).
Never the query, chunk text or answer text. The `meho.docs.ask` audit row,
its query hash and collection binding are unchanged.

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
  knows the pipeline position) can place it. The answer seam's local path
  wraps each leg and passes `llm_unavailable_leg` accordingly; the leg's own
  typed shapes are unaffected.
- **Upstream answer failures** (#3911) are classified from the transport's
  `CorpusAnswerError.kind` into the existing legs: `rejected` →
  `corpus_unavailable` / `upstream_rejected`, `server_error` →
  `corpus_unavailable` / `upstream_error`, `answer_unavailable` →
  `model_unavailable` / `upstream_answer_unavailable`, `rate_limited` →
  `model_unavailable` / `upstream_rate_limited`, `malformed` →
  `synthesis_malformed` / `parse` (table above).
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
  seams, so a hit renders by its human title instead of a raw id. When the
  corpus sends no title, the projection derives one from the hit's page
  identity (#3913, *The projection* above); MEHO still ingests nothing
  (federation-only, #1864 → #2049).
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

- When the hit's own `source_url` is an `https` link, it is the reference.
- Otherwise, when the backend sent an `upstream_url` (#3913) — its public
  `http(s)` link to the source, for a PDF often ending in `#page=N` — that
  link is the reference. The backend knows the public source of its own
  objects, so its link wins over one derived from the object path here (a
  KB path included). An `upstream_url` that fails `web_link_or_none` (the
  parse rule above) is ignored, never an error.
- Otherwise, when `resolve_citation_link(source_url)` derives a **canonical public URL**
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
and there is no fixed cutoff. Its direction is the chunk's `score_kind`
(#3913) when the backend sends one: `distance` means **lower is better**,
`similarity` higher is better; `null` means the backend did not say. `grounded` is therefore **presence-based**, not
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
409), then runs `run_ask_pipeline` (the raising wrapper over the one answer
seam: the backend's answer for an opted-in collection, else the in-process
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
distinct from it being unreachable). On the upstream path the status is
per (leg, cause): `upstream_rejected` (a 4xx from the backend) is **502**,
and `upstream_rate_limited` forwards the backend's `Retry-After` header
(clamped to 3600 s). The
structured `{detail, leg, cause, message}` envelope rides
`HTTPException.detail` byte-identical to the MCP `error.data` member. The answer stays fail-closed
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

On an **upstream-answer** collection (#3911) two things differ. A successful
answer's citation cards carry their number (`[1]`, `[2]`, … — the `k` the
answer's markers cite). An upstream failure shows the same leg banner and
then runs **one** plain `search_docs` call, rendering its chunks under the
banner (best effort: if that search also fails, the banner stands alone).

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
resource for the full text of a hit on a later turn. It also says how to
read `score` (#3913): with `score_kind`, where `distance` means lower is
better, so an agent does not take a distance for a similarity.

### `ask_docs` MCP tool (`meho_backplane.mcp.tools.docs`, T7 #1526)

The synthesis sibling, registered alongside `search_docs` in the **same**
module and carrying the **same** `required_capability="meho-docs"` gate,
the same `operator` role minimum, the same per-collection entitlement, and
the same strict `inputSchema` (`additionalProperties: false`, required
`[query, collection]`, product/version optional, `limit` default 10 / cap
50). It is absent from `tools/list` and 403-class on `tools/call` for an
unprovisioned tenant exactly like `search_docs`.

The handler answers through the one answer seam (`answer_docs_question`,
#3911) — the backend's answer for an opted-in collection, else the
**expand → retrieve-per-variant → RRF → synthesize** pipeline (#1916); the
module no longer carries its own copy of the pipeline. A leg failure is a
structured `-32603` whose `error.data` is the #1918 envelope (plus
`upstream_status` / `retry_after` on an upstream failure). On the upstream
path the `answer` text itself is wrapped in the untrusted envelope too (it
was composed by an external model over untrusted corpus text, and that
model's prompt has no injection guard yet). The description says the answer
is composed by the collection's backend when it offers an answer endpoint
and that `limit` may be capped by the backend. It mirrors `search_docs`'s
error arms plus the expand + synthesis arms: `build_docs_scope` + the shared gate enforce the collection
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
  `corpus_timeout_seconds` / `corpus_answer_timeout_seconds` (#3911) /
  `corpus_require_filters` (`CORPUS_*` env vars).

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

## Per-call logs (#3915)

Every docs call logs which chunks came back and which of them an answer
cited, so a wrong answer can be reconstructed from the logs (did retrieval
miss the right page, or did the answer leg find it too thin?). The fields
are built in one place, `docs_search/call_log.py`, so every event below
uses the same keys:

- `hit_count`, `hit_chunk_ids` (rank order, capped at 50) and
  `hit_source_refs`: the `source_url` of each listed hit, in the same
  order. A `source_url` is normalised when the chunk is projected
  (`normalize_source_ref`, #132): a public URL such as a KB article, or an
  opaque `meho://docs/<collection>/<chunk_id>` ref, never a `gs://` path.
  A fan-out over several collections also lists `hit_collections`, the
  source collection of each listed hit: a chunk id is unique only within
  its collection, so the same id can appear twice in one fused list.
- On an ask: `answer_source` (`local` or `upstream`), `citation_count` and
  `cited_chunk_ids`; on the upstream answer path also `upstream_total_ms` /
  `upstream_llm_ms` when the backend reports them (`ask_log_fields`).
- Next to the *requested* `product` / `version`: `scope_forwarded`, how
  the collection's scope gates sent them to the backend (`"soft"`,
  `"filters"` or `"none"`; see *Scope gates* above, #3912).

| Event | Level | Emitted by | Fields |
| --- | --- | --- | --- |
| `docs_search_completed` | info | `search_docs` (every single-collection backend search: REST, MCP, UI Search mode, each `ask_docs` variant) | `operator_sub`, `collection_key`, `product`, `version`, `scope_forwarded`, hit fields |
| `docs_search_fanout_completed` | info | `search_docs_fanout` | `collections`, hit fields of the fused list, `hit_collections` |
| `docs_search_multi_query_completed` | info | `retrieve_multi_query` | `collection_key`, `variant_count`, hit fields of the merged list |
| `docs_ask_query_expanded` | info | `expand_docs_query` | `collection_key`, `variant_count` (no variant strings) |
| `docs_ask_synthesized` | info | `synthesize_docs_answer` (local answer) | `answer_source="local"`, hit fields of the chunks the answer was composed over, `citation_count`, `cited_chunk_ids` |
| `docs_ask_no_grounding` | info | `synthesize_docs_answer` (empty retrieval) | the same fields, all empty |
| `docs_ask_completed` | info | `answer_docs_question` (every successful `ask_docs`, either path: REST, MCP, UI Ask mode) | `operator_sub`, `collection_key`, `product`, `version`, `scope_forwarded`, `answer_source` (`local` or `upstream`), hit fields of the chunks the answer was composed over, `citation_count`, `cited_chunk_ids`; upstream also `upstream_total_ms` / `upstream_llm_ms` |
| `docs_ask_upstream_failed` | warning | `answer_docs_question` (a failed upstream answer) | `operator_sub`, `collection_key`, `answer_source="upstream"`, `scope_forwarded`, `leg`, `cause`, `upstream_status`, `retry_after`; hit fields only when the backend returned hits that failed to map (`citation_resolution`) |
| `docs_search_scoped_zero_hits` | warning | `search_docs` | `operator_sub`, `collection_key`, `product`, `version`, `scope_forwarded` |
| `docs_query_text` | debug | `log_query_text`, only with `DOCS_DEBUG_LOG_QUERY_TEXT=true` | `source`, `collection_key`, `queries` |

**Never logged:** chunk text, answer text and query text. The question is
hashed into the audit row (SHA-256) and nowhere else.

**Query text behind an opt-in flag.** `DOCS_DEBUG_LOG_QUERY_TEXT`
(`settings.docs_debug_log_query_text`, default `false`) is the only way a
query string reaches the logs. When it is true, `expand_docs_query` writes
one `docs_query_text` record with `source="expansion"` and the variants it
retrieved on (the operator's question first). The upstream answer path
(#3911) does not log query text yet, even with the flag on: the backend
does not return its rewritten query. Once the MEHO Knowledge service
returns it (evoila-bosnia/MEHO.Knowledge#513), that query goes through the
same helper with `source="upstream_rewrite"`. The record is debug
severity, but the backplane's log floor is INFO (`configure_logging`), so
it is written through a logger of its own with a DEBUG floor: the flag,
not the process log level, decides whether it appears. Turn it on only
while debugging retrieval, and off again afterwards.

**Scoped zero hits.** A search that requested `product` or `version` and
returned no chunks logs the `docs_search_scoped_zero_hits` warning and
increments `docs_search_scoped_zero_hits_total`, whatever the collection's
scope gates did with the values. It counts backend searches, not
calls: a local `ask_docs` runs one search per expansion variant, and each
variant search that finds nothing counts once. A scoped ask that finds
nothing at all therefore counts up to 4 (`MAX_QUERY_VARIANTS`), and two
such asks reach the alert's default threshold of 5. An upstream ask runs
no search, so it never counts, even when the gates forward its product or
version to the answer call. A run of them with `scope_forwarded="filters"`
means the filter vocabulary callers send does not match the corpus's
(`vcenter` against a corpus that stamps `vsphere`); with `"soft"` or
`"none"` the backend did not filter on the values, so the search found
nothing for another reason. The counter carries no labels, because
`/metrics` can be unauthenticated; the warning line names the collection,
product, version and `scope_forwarded`. The chart's optional PrometheusRule alerts on it (`MehoDocsScopedZeroHits`, `prometheusRule.docsScopedZeroHits`; see
[devops.md](devops.md) § Metrics scrape wiring).

## Untrusted read-boundary guard (#304)

Corpus `chunk.content` is **federated, externally-controlled** text — a
third party's KB article can carry planted instructions. Every LLM-facing
read boundary wraps it in the positional
`<<UNTRUSTED_AGENT_TEXT … END_UNTRUSTED_AGENT_TEXT>>` envelope
(`meho_backplane.untrusted_text.wrap_untrusted_text`), matching the kb /
memory surfaces (evoila-bosnia/meho-internal#154, extended here by #304):

- `search_docs` payload — `_search_chunk_payload` (`mcp/tools/docs.py`).
- `ask_docs` citations — `_citation_payload` (`mcp/tools/docs.py`).
- `ask_docs` answer, **upstream path only** — `_ask_docs_handler`
  (`mcp/tools/docs.py`, #3911): the answer was composed by the collection
  backend's model over untrusted corpus text, outside the backplane's own
  prompt guard, so it is framed as untrusted too. The local answer is not
  wrapped (its synthesis prompt already carries the guard).
- `ask_docs` synthesis prompt — `_render_chunks_for_prompt`
  (`docs_search/synthesis.py`); `_SYNTHESIS_SYSTEM_PROMPT` carries the
  matching provenance advisory.
- `meho://docs/...` resource — `_docs_chunk_handler`
  (`mcp/resources/docs.py`).

The wrap is applied only at these boundaries, **never** at the shared
`_project_chunk` projection (`docs_search/service.py`) — that projection
also feeds non-LLM sinks (the CLI `meho docs search` and REST faces render
to a human/HTTP caller), which must not inherit the envelope. The guard is
structural (delimit + label), not content-based: no filtering, scoring, or
injection detection. See `docs/codebase/untrusted-text-envelope.md`.

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
- On an **upstream-answer** collection (#3911) the backend owns the answer:
  its model, prompt (answer length, language), retrieval depth (it may cap
  `limit`, e.g. at its own ask ceiling) and ranking. `product` / `version`
  reach the answer call only through the per-collection scope gates
  (#3912): a soft `scope` the backend ranks with, or hard `filters`.
  Streaming (`/ask/stream`) is not used.

## References

- Route: `backend/src/meho_backplane/api/v1/search_docs.py`.
- Service (router seam + the scope gates `forwarded_scope`):
  `backend/src/meho_backplane/docs_search/service.py`.
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
