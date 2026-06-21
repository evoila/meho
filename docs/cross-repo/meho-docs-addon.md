<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# meho-docs add-on — operator provisioning runbook + routing convention

> Cross-repo handshake between `evoila/meho` (this repo — the backplane
> that **federates** `search_docs` / `ask_docs` queries to a catalogue of
> external doc collections) and two consumer sides: the operator's
> **Keycloak realm** (grants the `meho-docs` add-on + per-collection
> `meho-docs:<collection>` capabilities per tenant) and the ops team's
> **external doc collections** (the corpora the backplane forwards to).
> Neither consumer side lives in this repo, so this page is the
> upstream-side tracker for what each side must do before an operator can
> search a collection.

This page is for a **tenant_admin / deployment operator** who needs to
provision the `meho-docs` add-on for a tenant, seed a doc collection,
entitle the tenant to it, bring its backend to readiness, and prove an
agent can discover and search it. It is not the codebase walkthrough —
for how the route, the MCP tools, the CLI verbs, the catalogue band, and
the backend router are wired internally, see
[`docs/codebase/docs-search.md`](../codebase/docs-search.md).

## What meho-docs is

`meho-docs` is an **optional, tenant-provisioned add-on** that exposes a
retrieval surface over a **catalogue of named doc collections** the ops
team runs. A **collection** is a named corpus (`collection_key`, a
`vendor`, the `products` it covers — e.g. a `vmware` collection covering
`vsphere` / `nsx`) bound to exactly one **backend**. The agent picks a
**collection**, never a backend: `collection` is the binary routing key
*and* the binary entitlement key; the backend that actually answers
(`corpus-http` over the federated corpus today, a managed RAG such as
`vertex-rag` tomorrow) is resolved server-side and never appears in a
request or response.

When a tenant provisions the add-on and is entitled to a collection, its
operators get three faces of the same surface:

- the REST routes `POST /api/v1/search_docs`, `POST /api/v1/ask_docs`,
  and `GET /api/v1/doc_collections` (+ the tenant-admin lifecycle routes
  under `/api/v1/doc_collections/{key}/…`),
- the MCP tools `search_docs`, `ask_docs`, and `list_doc_collections`
  (+ the companion resource
  `meho://docs/{collection}/{product}/{version}/{chunk_id}` for the full
  text of a hit), and
- the CLI verbs `meho docs search`, `meho docs collections list`, and
  the lifecycle verbs `meho docs collections probe|enable|disable`.

**Federated, not ingested.** This is the key distinction from MEHO's own
knowledge layer. A collection's queries are **not** copied into MEHO's
Postgres+pgvector substrate. Each query is proxied through the backplane
to the collection's backend, forwarding the operator's JWT so the backend
authenticates and audits the call as the operator. MEHO holds the
collection's *identity + backend binding* (the `doc_collections`
registry) and *cached liveness* (probe-written), but never a copy of the
corpus and no vector-store dependency on it.

Routing every query through the backplane (rather than letting clients
hit a backend directly) buys three properties in one place:

- **Central audit.** Every query lands one `audit_log` row
  (`op_class=read`), so `meho audit query` / who-touched surface it. The
  op id is **uniform across all faces**: REST, CLI, and MCP audit
  `search_docs` under `meho.docs.search`, `ask_docs` under
  `meho.docs.ask`, and `list_doc_collections` under
  `meho.docs.collections.list`. A `meho audit query op_id=meho.docs.*`
  filter therefore catches every query regardless of the face it came in
  on — including the MCP agent surface (see
  [Audit row visible](#audit-row-visible-who-touched)). The raw query is
  stored only as a SHA-256 hash — never in the clear. Each row also binds
  `audit_collection` (the chosen collection key), so who-touched can
  attribute a hit to the collection it came from.
- **JWT federation handled once.** Operator-JWT forwarding lives in the
  backplane's backend adapters, not in every consumer.
- **Mandatory collection scope + per-collection entitlement enforced
  centrally.** A query without a `collection` is rejected, fail-closed, so
  no caller can run an unscoped corpus-wide query; and a tenant may only
  search a collection it holds the `meho-docs:<collection>` capability
  for, enforced in one shared gate.

### meho-docs vs the lightweight knowledge base (`search_knowledge`)

These are two genuinely different surfaces, and the one-word gap between
them is exactly why this add-on carries the `docs` noun:

| | `search_docs` (meho-docs) | `search_knowledge` (the kb) |
|---|---|---|
| **What it holds** | Vendor-published documentation: product manuals, vendor KB, design/reference guides | THIS team's distilled knowledge: lab conventions, known-good runbooks, post-incident learnings |
| **Source** | External doc collections the ops team maintains | MEHO's own Postgres+pgvector substrate |
| **Shape** | Large, vendor-authored, batch-ingested externally | Small, operator-authored, fast-changing |
| **In MEHO** | **Federated** — proxied per query, never copied in | **Ingested** — lives in MEHO's tenant-scoped store |
| **Availability** | Only when the tenant has provisioned the add-on *and* is entitled to a collection | Always (core retrieval) |

The noun is the routing signal: "what does the vendor's documentation
say" → `search_docs`; "how does this team do it" → `search_knowledge`.
See the [routing convention](#routing-convention) below for the one line
agents carry.

## The collection model

A collection is one row in the `doc_collections` registry (migration
`0037`). The registry is **authoritative for identity + backend binding**
(operator-set) and carries **probe-written liveness** (backend-set) — the
same data split as `targets` rows + `Target.fingerprint`. The
agent-relevant columns:

| Column | Who sets it | Meaning |
|---|---|---|
| `collection_key` | operator (seed) | The stable id the agent passes as `collection=<key>`, e.g. `vmware`. The binary routing + entitlement key. |
| `tenant_id` | operator (seed) | `NULL` → **global / shared** (every tenant sees it); set → **tenant-curated**. A tenant row shadows a global one with the same key (the resolver, the catalogue, and the band all prefer the tenant row). |
| `vendor` | operator (seed) | The vendor the corpus is from (e.g. `VMware by Broadcom`). Shown in the catalogue; an exact-match filter. |
| `products` | operator (seed) | The products the corpus covers (e.g. `["vsphere", "nsx"]`). |
| `description` / `when_to_use` | operator (seed) | `when_to_use` is the agent-facing "pick this collection when…" blurb, surfaced verbatim by `list_doc_collections` and the catalogue band. |
| `backend` | operator (seed) | The `{type, ref}` routing record — see [backend-agnostic routing](#backend-agnostic-routing) below. Server-side only; never in a catalogue / search response. |
| `status` | probe / operator | The lifecycle enum: `provisioning` / `ready` / `rebuilding` / `disabled`. Only `ready` is searchable. |
| `doc_count` / `last_ingested_at` / `readiness` | probe | Cached liveness, `NULL` until the first `probe`. |

`backend` is the one column with no default: every collection must bind to
exactly one backend, so a seed row has to supply `{type, ref}`
explicitly — an empty `backend` is a routing-broken row, not a valid
state.

## Provisioning

Three things must be true before a tenant's operators can search a
collection:

1. the tenant is **granted the `meho-docs` add-on** (consumer-side, in the
   realm) — this turns the tool *surface* on;
2. the collection **exists** in `doc_collections` and the tenant is
   **entitled** to it via `meho-docs:<collection_key>` (consumer-side
   capability + deploy-side seed) — this decides *which collections* the
   tenant sees and may search;
3. the collection's backend is **reachable and its index is built**, so
   its `status` is `ready` (deploy-side, via the probe→enable lifecycle).

### 1. Grant the `meho-docs` add-on + per-collection entitlement (realm side)

The backplane reads a tenant's provisioned add-ons and per-collection
entitlements from a **JWT claim** on the operator's access token
(G4.5-T1, #1519). The claim name is configurable via the backplane
setting `JWT_CAPABILITIES_CLAIM_NAME` (default `capabilities`).

The capability set is a **flat list of string keys**. Two kinds of key
matter here:

- **`meho-docs`** — the **add-on key**. Gates the *surface*: a tenant
  without it never sees `search_docs` / `ask_docs` /
  `list_doc_collections` in `tools/list` (true absence) and `meho docs`
  is hidden from `--help`.
- **`meho-docs:<collection_key>`** — a **per-collection entitlement key**,
  one per collection the tenant may search (e.g. `meho-docs:vmware`).
  Reuses the same capability substrate — zero new tables; the JWT parser
  already accepts arbitrary string keys. This finer key decides *which
  collections* an entitled tenant actually discovers and searches.

So a tenant entitled to the `vmware` collection carries **both**
`meho-docs` and `meho-docs:vmware`:

```json
{
  "capabilities": ["meho-docs", "meho-docs:vmware"]
}
```

Accepted claim shapes (the backplane tolerates all three):

- **list of strings** — the canonical Keycloak multivalued-claim shape,
  `["meho-docs", "meho-docs:vmware"]` (add more `meho-docs:<key>` keys as
  the tenant is entitled to more collections);
- **single string** — `"meho-docs"`, for a realm that emits a scalar
  mapper for a single key (rare once per-collection keys are in play);
- **absent** — the tenant has no add-on; the backplane resolves the
  capability set to empty (fail-closed). Any non-string/array JSON type is
  logged under `malformed_capabilities_claim` and also resolves to empty.

This is the **same realm wiring shape** as the `tenant_id` / `tenant_role`
mappers — see [`keycloak-tenant-claims.md`](keycloak-tenant-claims.md)
for the protocol-mapper recipe (group-attribute or script mapper). Drive
the capabilities claim off a tenant/group attribute so a tenant_admin can
grant or revoke the add-on and individual collections without a code
change.

Fail-closed throughout: an absent, malformed, or empty claim means **not
provisioned**; a missing `meho-docs:<key>` means **not entitled to that
collection** even when the add-on surface is on. The gate never grants
access on its own — the backplane re-validates the JWT and the backend
federation enforces the real boundary, so a forged claim can change only
what a client *shows*, never what the server *allows*.

### 2. Register the collection + bind its backend (deploy side)

A tenant_admin registers a collection through the create surface
(#1739) — pick whichever front fits the deploy tooling:

- **REST** — `POST /api/v1/doc_collections` with the create body below.
- **CLI** — `meho docs collections create <collection_key> --vendor … --product … --backend-type corpus-http --backend-ref '{"endpoint":"…"}'` (or `--from-file <path>` with the JSON body).
- **MCP** — the `create_doc_collections` tool (tenant_admin, `meho-docs`).

The create validates `backend.type` against the search-backend registry
(an unregistered type is a `422`, not a row that fails later at probe /
search), derives `tenant_id` from the JWT (the body cannot set it),
defaults `status` to `provisioning`, and writes an audit row under
`op_id="meho.docs.collections.create"` — so a registration is never
unroutable, never cross-tenant, and never invisible the way a raw
`INSERT INTO doc_collections` could be. A follow-up `probe` promotes the
collection from `provisioning` to `ready` once its index confirms. A
minimal create binds identity + a backend:

| Field | Example | Notes |
|---|---|---|
| `collection_key` | `vmware` | The agent-facing id; must match the `meho-docs:vmware` capability key above. Unique within the scope (global or per-tenant); a collision is a `409`. |
| `tenant_id` | *(derived from the JWT)* | Never supplied in the body — the create always scopes the row to the caller's tenant. A shared/global row is seeded out-of-band (operator DB seed) since cross-tenant sharing is out of the create scope. |
| `vendor` | `VMware by Broadcom` | |
| `products` | `["vsphere", "nsx"]` | |
| `when_to_use` | `Vendor docs for VMware Cloud Foundation…` | The blurb the agent reads to pick the collection. |
| `backend` | `{"type": "corpus-http", "ref": {"endpoint": "https://corpus.example/v1/search"}}` | The routing record — see below. |

The **backend** record is `{type, ref}`:

- `type` selects the adapter from the backend registry. The first shipped
  adapter is **`corpus-http`** (the JWT-forward federated corpus client;
  the G4.5 single-corpus client re-homed). A second concrete backend type
  (e.g. a managed-RAG direct client) registers as a one-line
  `register_backend(...)` call when a collection needs it.
- `ref` is the per-collection config the adapter reads. For `corpus-http`
  that is the corpus search `endpoint` (alias `url`) and an optional
  `audience`. A `corpus-http` collection with **no** `backend.ref`
  endpoint falls back to the legacy global settings below — the
  unmigrated single-collection deploy still routes.

The legacy global corpus settings remain as the `corpus-http` fallback
(env vars in parentheses):

| Setting (env var) | Default | Meaning |
|---|---|---|
| `corpus_url` (`CORPUS_URL`) | `""` | Fallback corpus search URL for a `corpus-http` collection without its own `backend.ref` endpoint. Empty **and** no per-collection endpoint → the backend is unconfigured: a query fails closed as `CorpusUnavailable` → 503 at the route / `-32603` at the MCP face. |
| `corpus_audience` (`CORPUS_AUDIENCE`) | `""` | Optional RFC 8707 resource indicator (`aud`) the backend binds the forwarded token to. Empty forwards no audience. |
| `corpus_timeout_seconds` (`CORPUS_TIMEOUT_SECONDS`) | `10.0` | Bound on the backend HTTP request. A slow backend raises `CorpusUnavailable` rather than blocking the event loop. |
| `corpus_require_filters` (`CORPUS_REQUIRE_FILTERS`) | `true` | Legacy REQUIRE_FILTERS posture. Note: under the catalogue model the **mandatory** scope is `collection`; `product` / `version` are optional refinements within a collection, so the binary anti-drown guarantee now rides `collection`, not product/version. |

### 3. Bring the collection to readiness (probe → enable lifecycle)

Seeding a row does **not** make it searchable. The `status` column is a
four-state lifecycle:

- **`provisioning`** — registered, but the backend is not yet confirmed
  answerable (the initial state of a freshly-seeded row).
- **`ready`** — the backend is reachable and its index is built; the only
  searchable state.
- **`rebuilding`** — reachable but the index is not yet answerable (a
  managed-RAG index rebuild is in flight, or the corpus was registered but
  never ingested). A managed-RAG ANN index answers only once built, and
  rebuilds serialize per project — the lifecycle surfaces that as
  `status` rather than hiding it behind a silent empty search.
- **`disabled`** — hidden from search by an explicit operator action.

A **probe** moves a collection toward readiness. It is an explicit
tenant_admin action (it talks to the backend — latency + serialized
rebuilds) that refreshes the cached liveness the search path then reads
cheaply:

```bash
# Probe the backend: read index readiness / doc count / last ingest, and
# transition status (provisioning|rebuilding -> ready once the index is
# built). Writes liveness back onto the row on SUCCESS ONLY — a failed
# probe (503: backend unconfigured / unreachable / non-2xx) leaves the
# cached liveness untouched.
meho docs collections probe vmware
# collection:   vmware
# reachable:    true
# index built:  true
# doc count:    17234
# last ingest:  2026-06-05T09:12:00Z
```

`enable` / `disable` are the operator toggles:

```bash
# Hide a collection from search (-> disabled). search_docs then fails
# typed (409, "not ready") against it rather than returning an empty
# result -- uniform with provisioning/rebuilding, since disabled is just
# another not-ready status on the wired search path.
meho docs collections disable vmware

# Return a disabled collection to service (-> provisioning); a follow-up
# probe promotes it to ready once its index confirms.
meho docs collections enable vmware
```

Both are **idempotent** (re-enabling a live collection or re-disabling a
disabled one is a no-op, not an error) and **lifecycle-guarded** (a
forbidden transition → 409). A probe never re-enables a `disabled`
collection — an operator's explicit disable outranks a liveness signal.

The same three actions exist as tenant-admin-gated REST routes
(`POST /api/v1/doc_collections/{key}/probe|enable|disable`) and are
implemented once in `docs_collections/lifecycle.py`.

> **Note on the external corpus rename.** The corpus historically branded
> "MEHO.Knowledge" is being renamed on the ops side. That rename is
> **external infra** (the collections/services the ops team runs), not an
> `evoila/meho` change — nothing in this repo was ever named
> "MEHO.Knowledge". It is tracked on the consumer repo
> [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)
> (consumer-side reference: [#1178](https://github.com/evoila/meho/issues/1178)).
> The in-repo names (`meho-docs` add-on key, `meho-docs:<collection>`
> entitlement keys, `search_docs` / `list_doc_collections` surface) are
> greenfield and canonical from commit one, independent of when the
> external rename lands.

## Discovery — how an agent learns which collections exist

An agent never guesses a `collection` key. Two mechanisms surface the
catalogue, both filtered to **only the collections the operator is
entitled to** (`meho-docs:<key>`), so every key shown is one
`search_docs` will accept rather than reject:

- **`list_doc_collections`** (MCP) / **`GET /api/v1/doc_collections`**
  (REST) / **`meho docs collections list`** (CLI). Returns each entitled
  collection's `collection_key`, `vendor`, `products`, `when_to_use`, and
  operator-facing liveness (`status` / `doc_count` / `last_ingested_at`).
  The `backend` record is omitted by design (backend-agnostic contract).
  Keyset-paginated by `collection_key`; `--vendor` is an exact-match
  filter. This is the docs analogue of `list_targets` — `list_targets`
  answers "what infra can I act on?"; `list_doc_collections` answers "what
  docs can I search?".

  ```bash
  meho docs collections list
  meho docs collections list --vendor "VMware by Broadcom" --json
  ```

- **The `initialize.instructions` catalogue band.** At MCP session start,
  the `initialize` preamble carries a guard-delimited
  `<<DOC_COLLECTIONS_AVAILABLE>>` block listing the operator's entitled
  collections (key / vendor / `when_to_use`), so an agent can pick a
  `collection` from the session preamble **without** a
  `list_doc_collections` round-trip. The band is independently
  token-capped (an over-budget catalogue collapses to a summary line
  pointing at `list_doc_collections`) and is **empty for a tenant entitled
  to no collections** — a non-docs tenant's preamble is byte-identical to
  its pre-catalogue shape.

## Search — how an agent searches a collection

`search_docs(query, collection=<key>)` is the retrieval surface;
`ask_docs(query, collection=<key>)` is the synthesis sibling.

- **`collection` is the mandatory binary scope.** It routes the query to
  the collection's backend and gates per-collection entitlement. A query
  with no collection scope is rejected — **422** at the REST route,
  **`-32602`** (INVALID_PARAMS) at the MCP face, **exit 4** at the CLI
  (the CLI fails fast before the round-trip). An **unknown** collection is
  likewise 422 / `-32602` (carrying the catalogue of visible keys so the
  agent can self-correct).
- **`product` / `version` are optional refinements** within a single
  collection (a collection *is* a scoped corpus, so the anti-drown
  guarantee holds on `collection` alone). Omitting them still succeeds.
- **Entitlement** — searching a collection the tenant is not entitled to
  (`meho-docs:<key>` missing) → **403** / `-32602`, even though the tool
  stays visible via the base `meho-docs` gate.
- **Readiness** — a collection whose `status` is not `ready` is rejected,
  branching on the *kind* of not-ready (#1567): a *transient*
  `provisioning` / `rebuilding` collection → **409** / `-32603` (CLI exit
  4, "not ready") — retryable once the rebuild finishes; a `disabled`
  collection → **403** (`detail.error='collection_disabled'`) / `-32602`
  (CLI exit 4, "collection is disabled") — terminal, an operator hid it,
  so a client must not retry. The split is deliberate: a disabled
  collection is *not* an entitlement miss (the entitlement-miss 403 carries
  a plain-string detail), it is a terminal readiness state distinct from
  the retryable rebuild.

```bash
# Single collection — the common path. --collection is mandatory.
meho docs search "config maximums" --collection vmware
# Optional product/version refinements within the collection:
meho docs search "config maximums" --collection vmware --product nsx --version 9.0
# Raw JSON shows the full DocsChunk shape (chunk id, document id, score,
# source url):
meho docs search "config maximums" --collection vmware --json
```

`search_docs` returns ranked **cited chunks**, each carrying the chunk
text, a `source_url`, a `chunk_id`, and a `document_id`. For the full text
of a hit on a later turn (when the agent kept only the citation), read
`meho://docs/{collection}/{product}/{version}/{chunk_id}` via
`resources/read`.

### Cross-collection fan-out (`search_docs` only)

When the agent genuinely does not know which collection holds the answer,
`search_docs` can **fan out** across several collections at once. A single
collection is cheaper and sharper, so a fan-out is the escalation, not the
default:

```bash
# Explicit fan-out across named keys (repeat --collection):
meho docs search "supported snapshot depth" --collection vmware --collection hetzner
# The "all" sentinel — fan out across EVERY entitled, ready collection:
meho docs search "supported snapshot depth" --collection all
```

On the wire that is `collections=[a, b]` (an explicit list) or
`collection="all"` (the sentinel). Each collection is searched
independently on its own backend and the per-collection ranked lists are
merged by **reciprocal-rank fusion** (RRF) — never a raw-score sort, since
scores are not comparable across backends/embedding models. Every returned
chunk is tagged with its source `collection` for provenance. The fan-out
resolves to **only entitled, ready** collections — non-entitled and
not-ready members are dropped (logged, never silently truncated); an empty
resolved set → 403 / `-32602`. A single `collection` and the fan-out scope
are **mutually exclusive** (supplying both → 422 / `-32602`), and
`product` / `version` are ignored on a fan-out (each collection is a
pre-scoped corpus).

### `ask_docs` is single-collection only

`ask_docs` runs the *same* retrieval as `search_docs` (so the collection
scope, entitlement, backend routing, and forwarded-JWT audit are enforced
in one place), then composes one grounded, cited answer over the retrieved
chunks and returns `{answer, citations[]}` — no claim without a citation,
and a "no grounded answer" rather than a guess on an empty retrieval.
`ask_docs` **requires** `collection` and is **permanently
single-collection**: a fan-out attempt (`collections=[…]` or
`collection="all"`) is rejected with `-32602` before any retrieval, so the
grounded-answer contract never has to reconcile chunks from divergent
corpora.

## Backend-agnostic routing

`collection → backend{type, ref}` is resolved **server-side**. The agent
names a `collection`; the backplane's router (`docs_search/backends/`)
maps `backend.type` to a registered adapter and hands it `backend.ref` for
the per-collection config. One collection can sit on a managed RAG and
another on the federated corpus behind the same `search_docs`, and the
backend (`corpus-http` / a future `vertex-rag` / `meho-knowledge`) **never
appears in the request or the response**. meho stays a **backplane**, not
a vector DB. This hides backend-specific footguns (a managed-RAG ANN index
needs an explicit rebuild before it answers, and rebuilds serialize per
project) from the agent, surfacing them only as the collection's lifecycle
`status`.

## Corpus wire contract — what meho REQUIRES of a `corpus-http` backend

The sections above cover how meho is **configured to reach** a corpus
(the `backend.ref` endpoint/audience, the legacy `corpus_*` settings) and
the operator-facing **lifecycle** (probe → enable). They do **not** state
the HTTP shapes a `corpus-http` backend must speak. This section does: it
is the normative "your corpus must speak this" contract a corpus
implementer (MEHO.Knowledge today, an ECP-side corpus tomorrow) builds
against. Every shape below is derived verbatim from the federation client
in [`backend/src/meho_backplane/auth/corpus.py`](../../backend/src/meho_backplane/auth/corpus.py)
— that file is the source of truth; if this section and the code ever
disagree, the code wins and this section is the bug.

> **Why this matters (the #1732 footgun).** The first true in-cluster
> `search_docs` round-trip returned **zero hits from a populated corpus**
> because the corpus answered `{"results": [{"text": …, "source_uri": …}]}`
> at a `/readyz` readiness path, while meho reads top-level **`chunks`**
> with per-chunk **`content`** / **`source_url`** and derives its
> readiness URL as **`/status`**. The shapes parsed cleanly into an empty
> hit list rather than failing loud (see the fail-closed note at the end).
> A corpus that satisfies the contract below cannot hit that class of
> silent mismatch.

### Search — request

meho issues `POST <backend.ref.endpoint>` (the per-collection corpus
search URL; the legacy global `corpus_url` for an unmigrated deploy) with
a JSON body and a forwarded operator JWT:

```http
POST /v1/search HTTP/1.1
Host: corpus.example
Authorization: Bearer <operator JWT>
Content-Type: application/json

{
  "query": "config maximums",
  "limit": 10,
  "metadata_filters": {"product": "nsx", "version": "9.0"},
  "audience": "https://corpus.example"
}
```

| Key | Type | Sent when | Notes |
|---|---|---|---|
| `query` | `str` | always | The free-text search query. |
| `limit` | `int` | always | Maximum chunks to return. A corpus that reads only `top_k` (or `k`/`size`) and ignores `limit` silently caps at *its* default — the **non-fatal** #1732 mismatch. Read `limit`. |
| `metadata_filters` | `{key: scalar}` | only when non-empty | Binary `{key: value}` narrowing (e.g. `{"product": "vmware"}`). Omitted entirely when meho has no filters — do not require the key. |
| `audience` | `str` | only when configured | RFC 8707 resource indicator, forwarded **in the request body** here (contrast readiness below). Omitted when no audience is configured. |

- **Auth.** `Authorization: Bearer <operator JWT>` — the **operator's** raw
  JWT is forwarded (not a meho service token), so the corpus authenticates
  **and audits** the call as the operator, the same forward-the-JWT
  contract meho uses for Vault. The corpus must accept and audit it as the
  forwarded operator. (`corpus.py:184`.)
- **Source:** request body `corpus.py:178-182`; JWT header `:184`;
  per-collection endpoint/audience resolution
  [`docs_search/backends/corpus_http.py`](../../backend/src/meho_backplane/docs_search/backends/corpus_http.py)`:105-128`.

### Search — response

meho expects a `2xx` JSON body with a **top-level `chunks`** array, ranked
**best-first** (meho preserves the corpus's order; it does not re-sort):

```json
{
  "chunks": [
    {
      "chunk_id": "c-001",
      "document_id": "d-042",
      "content": "The supported maximum is …",
      "source_url": "https://docs.example/vmware/9.0/maximums#c-001",
      "score": 0.87,
      "metadata": {"product": "vmware", "version": "9.0"}
    }
  ]
}
```

| Field | Type | Required | meho reads |
|---|---|---|---|
| `chunks` | `[chunk]` | **yes** (top-level key) | The ordered hit list. **Not** `results` / `hits` / `data`. |
| `chunk_id` | `str` | **yes** | Per-chunk id. |
| `document_id` | `str` | optional (#2004) | Owning document id; read only as a citation-label fallback. A blank `""` or an omitted key normalises to `None` — it is **not** a grounding key, so absence does not fail parse. |
| `content` | `str` | **yes** | The chunk text. **Not** `text` / `body` / `snippet`. |
| `source_url` | `str` | optional | Citation URL. **Not** `source_uri` / `url`. |
| `score` | `float` | optional | Rank score (meho keeps corpus order regardless). |
| `metadata` | `object` | optional | Per-chunk attributes (e.g. `product` / `version`); passed through. |

meho reads top-level **`chunks`** and per-chunk **`content`** /
**`source_url`** — a corpus that returns `results` / `text` / `source_uri`
is **not** speaking this contract (and triggers the empty-parse footgun
below, not a loud failure). Extra fields meho does not name are ignored
(`extra="ignore"`), so a corpus may add fields freely; **dropping** a
required field (`chunk_id` / `content`) fails parse and is surfaced as
`CorpusUnavailable` → 503. `document_id` is the lone exception (#2004): it
is a citation-label fallback only, so a blank or omitted value normalises
to `None` rather than failing parse. Source: `CorpusChunk` /
`CorpusSearchResponse`, `corpus.py:88-122`.

### Readiness — request + response

meho reads corpus readiness with a `GET` at a **derived** URL — there is
**no fixed `/status` literal**. `derive_status_url(<search endpoint>)`
(`corpus.py:248-265`) computes it:

- If the search URL's final path segment is **`search`**, that segment is
  rewritten to **`status`**:
  `https://corpus.example/v1/search` → `https://corpus.example/v1/status`.
- Otherwise **`/status`** is **appended** as a child segment:
  `https://corpus.example/v1/lookup` → `https://corpus.example/v1/lookup/status`.
- Any query string / fragment on the search URL is **dropped** from the
  readiness URL.

So a corpus that exposes readiness at `/readyz` (or any path the rule does
not produce) is **not reachable** by meho's probe — expose readiness at
the derived `/status` URL. On readiness, **`audience` is forwarded as a
query param**, not a body key (contrast search above):

```http
GET /v1/status?audience=https://corpus.example HTTP/1.1
Host: corpus.example
Authorization: Bearer <operator JWT>
```

The expected `2xx` JSON body:

```json
{
  "index_built": true,
  "doc_count": 17234,
  "last_ingested_at": "2026-06-05T09:12:00Z"
}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `index_built` | `bool` | **yes** | `false` ⇒ corpus is **reachable but not yet answerable** (ANN index rebuilding, or registered-but-never-ingested). meho surfaces this as the collection's `rebuilding` / `provisioning` lifecycle `status`, **not** as a silent empty search. |
| `doc_count` | `int` | optional | Indexed document count (operator liveness). |
| `last_ingested_at` | `datetime` | optional | ISO-8601 timestamp of the last ingest. |

`audience` is a **query param** here (`corpus.py:311`) versus a **body
key** on `/search` (`corpus.py:181-182`) — a corpus that only reads
`audience` from one of the two will mis-bind the token on the other path.
Source: `derive_status_url` `corpus.py:248-265`; `CorpusStatusResponse`
`:224-245`; audience-as-query-param `:311`.

### Fail-closed semantics meho enforces

Every error path collapses to **one** typed `CorpusUnavailable`, which the
`search_docs` route renders as **HTTP 503** (and `-32603` at the MCP face)
— never a silent empty result. meho fails closed when the corpus is:

- **unconfigured** — no `backend.ref` endpoint and an empty legacy
  `corpus_url` (`corpus.py:172-175`, `:303-307`);
- **unreachable / timed out** — any transport error or a request exceeding
  `corpus_timeout_seconds` (`:190-195`, `:318-320`);
- **non-2xx** — the upstream status is logged; the response **body is
  never** echoed into the 503 (`:197-205`, `:322-327`);
- **non-JSON** — a `2xx` whose body is not JSON (`:207-213`, `:329-333`);
- **schema-drift** — a `2xx` JSON body missing a required field or with a
  wrong type (`:215-221`, `:335-339`).

> **The one non-loud branch (#1732 footgun).** A `2xx` whose JSON body has
> **neither `chunks` nor any field meho consumes** parses to an **empty
> hit list**, not a `CorpusUnavailable`. Because `chunks` defaults to `[]`
> (`CorpusSearchResponse`, `corpus.py:120-122`) and unknown keys are
> ignored, a corpus returning `{"results": […]}` yields **zero hits**
> rather than failing loud — exactly the #1732 symptom. The corpus side
> must therefore return the `chunks` envelope above; making this branch
> fail loud instead is a **code** change tracked on #1732 and is out of
> scope for this doc.

## Verify

Prove the add-on end-to-end. The contract is: an entitled collection is
**discoverable and returns cited chunks on a provisioned, entitled tenant;
absent (not greyed-out) on an unprovisioned or unentitled one; and every
query is audited under `meho.docs.*` with its collection scope**.

### Present + cited chunks (provisioned, entitled tenant)

CLI (operator logged in as a tenant entitled to `vmware`):

```bash
# meho docs appears in --help only when the tenant has the add-on:
meho --help | grep -A1 '  docs'

# The entitled collection is in the catalogue:
meho docs collections list                 # vmware appears, status=ready

# A scoped query returns ranked cited chunks:
meho docs search "config maximums" --collection vmware
```

MCP face (an agent session against the same tenant):

- `list_doc_collections` returns `vmware`, and the
  `<<DOC_COLLECTIONS_AVAILABLE>>` band in `initialize.instructions` lists
  it at session start.
- `search_docs` appears in `tools/list`; a `tools/call` with
  `collection="vmware"` returns ranked chunks carrying `source_url` /
  `chunk_id` / `document_id`.
- The companion resource
  `meho://docs/vmware/{product}/{version}/{chunk_id}` returns the full
  text of a hit.

### Absent (unprovisioned or unentitled tenant)

For a tenant **without** the `meho-docs` add-on, the surface tells the
truth — it is absent, not greyed-out:

```bash
# meho docs is hidden from --help...
meho --help | grep -c '  docs'        # -> 0

# ...and every verb refuses with a typed error before any network call:
meho docs search "anything" --collection vmware
# -> addon_not_provisioned (exit 5): the meho-docs add-on is not
#    provisioned for your tenant; ask a tenant_admin to enable the
#    `meho-docs` capability
```

For a tenant **with** the add-on but **without** `meho-docs:vmware`, the
`vmware` collection is a **true absence** in the catalogue and the band
(`list_doc_collections` does not list it), and a direct
`search_docs(collection="vmware")` is rejected **403** / `-32602` — the
collection exists, but the tenant is not entitled. On the MCP face, the
add-on tools are still in `tools/list` (the base `meho-docs` gate passes);
only the unentitled *collection* is absent.

### Audit row visible (who-touched)

Every query — from any face — writes one `audit_log` row
(`op_class=read`) under a uniform `meho.docs.*` op id, with the chosen
collection bound as `audit_collection`:

| Face | op id |
|---|---|
| `search_docs` (REST / CLI / MCP) | `meho.docs.search` |
| `ask_docs` (REST / CLI / MCP) | `meho.docs.ask` |
| `list_doc_collections` (REST / CLI / MCP) | `meho.docs.collections.list` |

Because the op id is uniform, one filter catches every face — including
the MCP agent surface, the primary place agents reach a collection:

```bash
# All search_docs queries — REST, CLI, and MCP — in one pass:
meho audit query --op-id 'meho.docs.search' --since 24h
# Both search and ask (and the catalogue read) under the family glob:
meho audit query --op-id 'meho.docs.*' --since 24h
```

The raw query is never stored — only its SHA-256 hash, plus the
`audit_collection` scope and hit count. A fan-out binds `audit_collection`
to the sorted, comma-joined queried set, so who-touched attributes the
query to every collection it touched.

## Routing convention

Agents (and operators) carry one line to decide which retrieval surface to
reach for:

> **Ask the team first — `search_knowledge` / `search_memory` — escalate
> to `search_docs(collection=…)` only on a miss or an explicit vendor-fact
> need; pick the collection explicitly (it's a binary filter, not a
> guess).**

This matches the boundary the shipped tool descriptions steer agents
toward: use `search_docs` / `ask_docs` for **vendor reference** (what the
documentation says), `search_knowledge` for **how THIS team does
something** (lab conventions, known-good runbooks, post-incident
learnings), and `search_memory` for **cross-session state** (what you or
the operator established earlier in this or a prior session). When you do
escalate to the docs, pick the `collection` explicitly from
`list_doc_collections` or the session band — `collection` is a binary
routing + entitlement filter, never a classifier guess. Only fan out
(`--collection all`) when you genuinely do not know which collection holds
the answer.

## References

- Codebase walkthrough (how the surface is wired):
  [`docs/codebase/docs-search.md`](../codebase/docs-search.md).
- Corpus wire-contract source of truth (the federation client this
  contract is derived from):
  [`backend/src/meho_backplane/auth/corpus.py`](../../backend/src/meho_backplane/auth/corpus.py)
  (search request/response + `derive_status_url` + readiness + fail-closed
  branches) and the calling adapter
  [`backend/src/meho_backplane/docs_search/backends/corpus_http.py`](../../backend/src/meho_backplane/docs_search/backends/corpus_http.py)
  (per-collection `backend.ref` endpoint/audience resolution). Sibling code
  arm: [#1732](https://github.com/evoila/meho/issues/1732)
  (corpus-http ↔ MEHO.Knowledge `/search` contract mismatch).
- Realm-side claim recipe (the capabilities claim follows this pattern):
  [`keycloak-tenant-claims.md`](keycloak-tenant-claims.md).
- Catalogue Initiative: [#1548](https://github.com/evoila/meho/issues/1548)
  (G4.6 doc-collection catalogue). Tasks: registry
  [#1550](https://github.com/evoila/meho/issues/1550), backend router
  [#1551](https://github.com/evoila/meho/issues/1551), collection-scoped
  search + entitlement + audit
  [#1552](https://github.com/evoila/meho/issues/1552),
  `list_doc_collections` + catalogue band
  [#1553](https://github.com/evoila/meho/issues/1553), cross-collection
  fan-out [#1554](https://github.com/evoila/meho/issues/1554), readiness
  probe + lifecycle [#1555](https://github.com/evoila/meho/issues/1555).
- Predecessor Initiative: [#1518](https://github.com/evoila/meho/issues/1518)
  (G4.5 meho-docs add-on — the single-corpus surface this catalogue
  generalises).
- Parent Goal: [#215](https://github.com/evoila/meho/issues/215).
- Consumer side (external collection ingest + the MEHO.Knowledge →
  meho-docs rename + per-collection provisioning):
  [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc);
  consumer-side corpus reference
  [#1178](https://github.com/evoila/meho/issues/1178).
