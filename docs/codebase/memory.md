# `meho_backplane.memory` — scoped tenant memory service

> Durable map of the memory subsystem. Update in lock-step with code
> changes; stale entries are bugs.

## Overview

The memory service is the persistent, RBAC-gated key-value layer that
backs both the REST surface (`/api/v1/memory/...`) and the MCP meta-
tools (`add_to_memory`, `search_memory`, `meho_memory_promote`, plus
the resource templates `meho://memory/id/{document_uuid}` and
`meho://memory/{scope}/{slug}`). All memory
rows are stored in the shared `documents` table under
`source = 'memory'` — the same table that backs `kb` and any future
indexed corpus — which lets hybrid BM25 + cosine retrieval span the
read path uniformly. Per-scope encoding of the natural key isolates
operators, tenants, and per-target slots inside that single physical
table.

The package is intentionally narrow in mutation surface: there are
five scopes (`user`, `user-tenant`, `user-target`, `tenant`,
`target`), one verb per CRUD shape (`remember`, `recall`, `forget`,
`list_memories`, `search_memories`, `promote`), and one RBAC matrix
(`MemoryRbacResolver`). Every transition is sized to fit inside one
session boundary.

Three transports — the REST router under `/api/v1/memory`, the MCP
`add_to_memory` / `search_memory` meta-tools, and the CLI `meho
memory` verb — all funnel through the same tenant-scoped
`MemoryService` that owns the RBAC matrix and the substrate I/O.

## Key types

### `MemoryScope` (`memory/schemas.py`)

Closed enum of the five scopes. Each value drives the
`source_id` encoding scheme and the RBAC matrix.

| scope            | natural-key encoding                          | who can read           | who can write              |
| ---------------- | --------------------------------------------- | ---------------------- | -------------------------- |
| `user`           | `user:<sub>:<slug>`                           | owning operator only   | owning operator only       |
| `user-tenant`    | `user-tenant:<sub>:<slug>`                    | owning operator only   | owning operator only       |
| `user-target`    | `user-target:<sub>:<target_name>:<slug>`      | owning operator only   | owning operator only       |
| `tenant`         | `tenant:<slug>`                               | every tenant operator  | `tenant_admin` only        |
| `target`         | `target:<target_name>:<slug>`                 | every tenant operator  | `tenant_admin` only        |

The colon-joined encoding is the reason `MemoryEntryCreate.slug`
pydantic-rejects colon-bearing slugs — without the guard the rsplit
in `slug_from_source_id` would silently truncate a slug like
`foo:bar` to `bar`.

### `MemoryEntry` (`memory/schemas.py`)

Frozen read shape. Carries the typed fields the API and MCP surfaces
need: `id`, `tenant_id`, `scope`, `slug`, `body`, `metadata`,
`expires_at`, `user_sub`, `target_name`, `created_at`, `updated_at`.
`created_at` and `updated_at` are **required, non-optional**; the
service never substitutes a sentinel.

### `MemoryEntrySearchHit` (`memory/schemas.py`)

One bounded search hit (#3751). It is evidence for picking a result,
not the entry. Fields:

| Field | Meaning |
|---|---|
| `rank` | 1-based position in the result list. An order only. |
| `id`, `scope`, `slug` | Which entry this is. |
| `user_sub` | Owner of a user-scoped entry; `null` for tenant and target scopes. |
| `target_name` | Target of a target-scoped entry, else `null`. |
| `created_at`, `updated_at` | Real column values. |
| `expires_at`, `expiry_state` | `expiry_state` is `never` (no expiry) or `scheduled`. Search leaves out expired rows, so there is no expired state. |
| `snippet` | A query-relevant excerpt of the body, at most `SNIPPET_MAX_CHARS` (240) characters plus a `…` at each cut end. |
| `snippet_truncated`, `body_chars` | Whether the body is longer than the snippet, and the body length in characters. |
| `locator` | Computed: `meho://memory/id/{document_uuid}`, built by `memory_locator(id)`. Holds only the row id: no target name, no owner. |

A hit never holds the full `body` or the free-form `metadata` (tags and
any caller keys). Read the locator for those. The fused (RRF) score and
the per-signal BM25 and cosine scores are not exposed: they are not
calibrated, so showing them would look like a confidence they are not.

### `query_snippet` (`memory/snippet.py`)

`query_snippet(body, query)` returns `(snippet, truncated)`. A body of
at most 240 characters comes back whole. A longer body is cut to a
240-character window that starts a little before the first query word
(two or more characters, case-insensitive) found in the body, or at the
start when none is found. It cuts by characters, not words. The work per
hit is bounded: it uses at most 8 query words (`SNIPPET_MAX_TERMS`,
longest first) and looks only at the first 32,768 characters of the body
(`SNIPPET_SCAN_MAX_CHARS`). A word that first appears after that point
does not move the window.

### `MemoryService` (`memory/service.py`)

Singleton-style service: holds a `MemoryRbacResolver` and a
`structlog` logger; opens a fresh `AsyncSession` per method. No
shared cursor / connection state across calls. Public methods:
`remember`, `recall`, `read_by_id`, `forget`, `list_memories`,
`search_memories`, `promote`.

### `resolve_default_expires_at` (`memory/ttl.py`)

Shared default-TTL resolver. Called by both REST and MCP write paths
with the surface-native "field absent from payload" signal so neither
surface duplicates the policy.

### `RememberBody` (`api/v1/memory.py`)

Pydantic v2 frozen model, `extra="forbid"`. REST request shape.

### `_add_to_memory_handler` (`mcp/tools/memory.py`)

MCP write handler. Receives `dict[str, Any]` arguments from the
dispatcher.

## Control flow

### Write path — default-TTL contract

The default-TTL policy (G5.2-T2 #624: omitted `expires_at` on a
`user`-scope write injects `now + memory_user_default_ttl_days`) lives
in **one** place — `memory/ttl.py:resolve_default_expires_at`. All
three write surfaces consume it (REST + MCP since #775; the UI create
form since #1697).

```
REST POST /api/v1/memory
    body: RememberBody (pydantic)
    │
    └─> _resolve_default_ttl(body)
        │   expires_at_was_set = "expires_at" in body.model_fields_set
        │   explicit_expires_at = body.expires_at
        └─> resolve_default_expires_at(scope, ...)
                │
                └─> MemoryService.remember(expires_at=...)

MCP tools/call add_to_memory
    arguments: dict[str, Any]
    │
    │   ttl_was_set = "ttl" in arguments
    │   explicit_expires_at = _parse_iso_duration(arguments["ttl"])
    │       (when ttl_was_set and value is not None)
    │
    └─> resolve_default_expires_at(scope, ...)
            │
            └─> MemoryService.remember(expires_at=...)

UI POST /ui/memory/create (#1697)
    form field: expires_at (datetime-local; FastAPI parses to
    datetime | None — blank submits "" which coerces to None)
    │
    │   expires_at_was_set = expires_at is not None
    │   explicit_expires_at = expires_at
    │
    └─> resolve_default_expires_at(scope, ...)
            │
            └─> create_entry(expires_at=...) ─> MemoryService.remember(...)
```

The discrimination on each side picks a surface-native "field absent
from the payload" signal:

* REST uses **pydantic v2's `model_fields_set`** — the set carries
  every field the constructor saw, even when the value was `null`.
  Verified against pydantic 2.13.4: explicit `b=None` is in
  `model_fields_set`; absent `b` is not.
* MCP uses **dict membership (`"ttl" in arguments`)** — the JSON-RPC
  dispatcher only populates `arguments` with keys the inbound payload
  carried, and `additionalProperties: false` on the tool's
  `inputSchema` already rejected unknown keys upstream.
* The UI create form uses **`expires_at is not None`** on the parsed
  form value — FastAPI (verified on 0.136.3) coerces both an absent
  field and the empty string a blank `<input type="datetime-local">`
  submits to `None`, and a browser form cannot express an explicit
  `null`, so the table's "field present, value `null`" row is
  unreachable from this surface; blank ≡ field-omitted.

The three semantic branches are:

| Caller shape | `expires_at_was_set` | `explicit_expires_at` | Resolver returns |
|---|---|---|---|
| Field omitted, `scope=user` | `False` | (n/a) | `now + memory_user_default_ttl_days` |
| Field omitted, non-`user` scope | `False` | (n/a) | `None` |
| Field present, value `null` (CLI `--persist`) | `True` | `None` | `None` |
| Field present, value `<ISO-8601>` | `True` | the parsed datetime | the parsed datetime |

Why this matters: before G0.9.1-T3 (#775) the MCP path always passed
`expires_at` explicitly to `MemoryService.remember` — including
`None` when the caller omitted `ttl` — so the surface-layer "set vs
unset" split was defeated and user-scope memories written via MCP
never expired (silent data-retention regression in v0.3.1). The fix
lifts the resolver into a shared helper both layers call with their
own set-vs-unset signal. The UI create form repeated the same
divergence class until #1697 (v0.14.0 cycle-10 dogfood finding FE-6):
the submit handler passed the parsed form value — always `None` on a
blank picker — straight through, so user-scope rows created in the
operator console persisted `expires_at = null` and the expiry sweeper
(#623, null rows skipped) never reaped them.

### Write path — `remember`

1. Validate `slug` (auto-generated 12-char hex if absent) and reject
   target-scoped writes without a `target_name`.
2. Check RBAC: `tenant_admin` for `tenant` / `target` writes;
   any operator for the three user-flavoured scopes.
3. Compute `source_id` via `encode_source_id`.
4. Build merged `doc_metadata` (caller keys + service-owned
   `scope` / `user_sub` / `target_name` / `expires_at`).
5. Call `meho_backplane.retrieval.indexer.index_document`, which
   upserts the row (insert-or-update on the
   `(tenant_id, source, source_id)` unique index) and computes the
   384-dim embedding inline.
6. SELECT the row back and project to `MemoryEntry` via
   `document_to_entry`, which pulls `created_at` / `updated_at`
   straight from the ORM row.

### Read path — direct (`recall` / `list_memories`)

`recall` is a natural-key SELECT; `list_memories` pulls a wider
candidate window (`limit * 4`, min 200) ordered by `updated_at
DESC`, then post-filters through the RBAC matrix on `user_sub`. Both
paths project via `document_to_entry`, so the returned timestamps
are the real DB column values.

### Read path — selected read by id (`read_by_id`)

A search hit carries a short snippet and the `locator`, never the full
body (#3751). The selected read is the only MCP path that serves the
full body. Access is checked at search time by the eligibility rules
pushed into the retriever (see "Read path — search" below), so a hit
already means the operator may read it. `read_by_id` checks access
again on the selected read.

Contract for `meho://memory/id/{document_uuid}`:

* The `{document_uuid}` is `documents.id`. No migration; the id
  already exists on every row.
* The id is not authority. `MemoryService.read_by_id` loads the row
  by `(id, tenant_id, source='memory')`, then checks, in order: the
  `kind` maps to a scope; a target-scoped row has a `target_name`;
  `MemoryRbacResolver.can_read` allows it; the row's `source_id`
  equals the natural key this operator would build for it (so for
  user scopes the owner is checked through the key, not only through
  metadata); the row has not expired.
* Every miss returns `None`: unknown id, deleted row, other tenant,
  other owner, non-memory row, broken row, expired row. The MCP
  handler also maps a malformed id to the same answer. The wire error
  is always `-32602` "memory entry not found: id=<input>".
* The read response is the `MemoryEntry` shape plus `locator`, with
  `body` inside the untrusted-text envelope. The legacy
  `meho://memory/{scope}/{slug}` read returns the same shape.
* The read is never cut. When the encoded `resources/read` text block
  is over the shared read limit `RESULT_QUERY_RESPONSE_WIRE_MAX_BYTES`
  (default 2 MiB, measured with `measure_delivered_wire_bytes`), the
  read fails with `-32602` and `error.data =
  {"reason": "output_too_large", "delivery": "unavailable"}`, the same
  data a too-large `result_query` read returns. The message names the
  CLI `meho recall <scope>/<slug>` path. An MCP write caps the body at
  64 KiB characters, so only very large REST writes or old rows reach
  this.
* The legacy scope/slug template still resolves `user`,
  `user-tenant`, and `tenant` entries. It does not resolve `target`
  or `user-target` entries (no target name in the URI); use the
  locator. A `meho://memory/id/<x>` URI also matches the legacy
  template's shape, so its handler forwards `scope == "id"` to the
  canonical handler. `id` is not a scope value, so this cannot
  clash.

Consumers: the MCP `search_memory` tool and the REST
`POST /api/v1/memory/search` route (used by `meho recall --query`)
return hits with `locator`.

### Read path — search (`search_memories`)

Rule: **eligibility first, ranking second** (#3741). A memory the
caller may not see, or one that has expired, must never take a
candidate slot from one they may see.

1. `MemoryRbacResolver.eligibility(operator, scope)` builds one frozen
   `MemoryEligibility` (defined in `retrieval/retriever.py`): the
   operator's `tenant_id`, their `sub`, and the visible kinds. With
   `scope=None` that is all five kinds (the documented default); a
   scope narrows it to that one kind.
2. `search_memories` makes **one** `retrieve(source="memory",
   memory_eligibility=..., limit=limit)` call. The query is embedded
   once, and all scopes rank in one pool. (The old per-kind fan-out
   and the `metadata_filters` work-around are gone.)
3. Both candidate SQL statements (BM25 and cosine) apply, before
   their `LIMIT`:
   * the tenant predicate;
   * `kind = ANY(:memory_kinds)`;
   * the per-principal predicate: a user-scoped kind needs
     `metadata->>'user_sub' = principal`; tenant and target kinds are
     shared across the tenant;
   * the expiry predicate (below).
4. The service maps each hit to a bounded `MemoryEntrySearchHit`
   (`_hit_to_search_result`): it cuts the body to a query snippet with
   `query_snippet` and numbers the hits with `rank`. It does **not**
   filter again in Python, so a page is never under-filled. Only a hit
   with an unknown kind is dropped (defensive).

"No match" and "only rows you may not see" give the same answer: an
empty `hits` list, with no count.

**Expiry predicate (PostgreSQL 16).** It uses the database's `now()`,
never a client clock, and runs for every `source='memory'` row in
*every* `retrieve` call, even without an eligibility value:

| Stored `expires_at` | Result |
|---|---|
| key missing, or JSON `null` | never expires (legacy rows, persistent memories) |
| ISO 8601 with offset (`+00:00`, `Z`, `+05:30`) | compared as the instant it names |
| ISO 8601 without offset | read as UTC (same as `metadata_datetime`), cast to `timestamp` so the session time zone cannot change it |
| anything else (`now`, `today`, `epoch`, `infinity`, numbers, bad dates) | treated as expired, excluded |

A guarded `CASE` checks the ISO shape with a regex and then
`pg_input_is_valid(value, 'timestamptz')` before any cast, so a bad
value can never abort the query. When a Memory search excludes
unreadable values, the retriever logs
`memory_expiry_unreadable_excluded` with the tenant id and a count
capped at 100 — never a raw value. The count is limited to rows the
caller could otherwise see.

There is no SQLite fallback for search: the rules exist only in the
PostgreSQL SQL. `tests/integration/test_memory_eligibility_pg.py`
proves them on `pgvector/pgvector:pg16`.

**Same front, same answer.** Three callers share this path:

* MCP `search_memory` → `MemoryService.search_memories`.
* REST `POST /api/v1/memory/search` (body `{query, scope?, limit?}`,
  response `{hits: [MemoryEntrySearchHit]}` — the same hits as MCP,
  see "Search output" below) →
  the same service call. Open to `read_only`, like other memory reads.
  Audit op id `memory.search`, class `read`; the query text is not
  stored.
* CLI `meho recall --query` → the REST route above. `--scope`
  narrows; no `--scope` searches all visible scopes. `--json` prints
  the `MemorySearchResponse`.

Generic `POST /api/v1/retrieve` and the `meho://retrieve/{query}`
resource pass the same `MemoryEligibility` whenever Memory rows can
be candidates (`source="memory"` or no source), so an all-source
query cannot go around these rules. Other sources are unchanged.

**G0.9.1-T4 (#776) fix.** Before v0.3.2 the search path substituted
`EPOCH = datetime(1970, 1, 1, tzinfo=UTC)` for both timestamps and
relied on a stale comment claiming "the API layer renders this as
null". Pydantic v2's `model_dump(mode='json')` does no such thing —
it serializes the datetime verbatim, so every search response
carried `"1970-01-01T00:00:00Z"` on persisted rows. The fix carries
the real column values on `RetrievalHit` (substrate-level) and
passes them through unchanged; the placeholder constant is gone.

### Search output — bounded, untrusted evidence (#3751)

What each front sends for the same search:

* **Service and REST** (`POST /api/v1/memory/search`, `meho recall
  --query`): `{"hits": [MemoryEntrySearchHit]}` with the plain snippet.
  The CLI table shows `RANK`, `SCOPE`, `SLUG`, and the snippet cut to
  80 characters; `--json` prints every field.
* **MCP `search_memory`** (`mcp/tools/_memory_evidence.py`): the same
  hits, each `snippet` wrapped in the `<<UNTRUSTED_AGENT_TEXT`
  envelope, plus:
  * `delivery`: `complete`, or `partial` when hits were left out to
    stay inside the size limit;
  * `omitted_hits`: how many were left out (only eligible hits, so it
    is not an existence signal);
  * `snippet_max_chars`: the snippet bound (240);
  * `remediation`: present only when `partial`.

  The tool declares an `outputSchema`, so the response goes on the wire
  twice: as `content[0].text` and as `structuredContent`. Both are the
  same object.

The two fronts agree on every hit field once the envelope is removed;
they are not byte-identical. `test_rest_and_mcp_return_the_same_eligible_hits`
pins this.

**Size limit.** The MCP response is measured with
`measure_delivered_wire_bytes` (text block plus `structuredContent`)
against the shared completed-call limit
`RESULT_COMPLETE_RESPONSE_WIRE_MAX_BYTES` (default 256 KiB, at least
2048). While it is over, the lowest-ranked hit is dropped. Fifty hits
with full snippets stay far below the default, so `partial` shows up
only with a lowered limit or very long slugs or target names. There is
no Memory-specific store, handle, or query grammar: the next step after
a hit is always the selected read on its `locator`.

### Promote path

Tenant-admin-only widening of a row's scope (`user → user-tenant →
tenant` etc.). Implemented as an insert-then-delete inside one
transaction, idempotent against re-runs via a `promoted_from`
marker.

## Dependencies

* `meho_backplane.db.models.Document` — the only persistent
  shape. Memory rows are `source='memory'`,
  `kind='memory-<scope>'`.
* `meho_backplane.retrieval.indexer.index_document` — write
  channel. Upserts on the natural-key composite index and computes
  the embedding inline (no async-indexer fan-out in v0.3).
* `meho_backplane.retrieval.retriever.retrieve` — read channel for
  `search_memories`. Returns a `RetrievalHit` list with the full
  ORM row's fields, including the timestamps. Owns the
  `MemoryEligibility` type and the SQL that enforces it.
* `meho_backplane.memory.rbac.MemoryRbacResolver` — the scope ×
  role matrix. Applied at write boundaries, on `recall` and
  `read_by_id`, at the `list_memories` in-process filter, and —
  through `eligibility()` — as the input the search SQL enforces
  before ranking. `search_memories` does not consult the resolver
  after retrieval; the SQL layer drops rows the caller may not see
  before the candidate budget is allocated.
* `meho_backplane.operations.delivery_budget.measure_delivered_wire_bytes`
  and the `result_*_wire_max_bytes` settings — the shared wire limits
  for the `search_memory` response and the selected read.
* `meho_backplane.untrusted_text.wrap_untrusted_text` — the envelope
  around search snippets and the selected-read body.
* `meho_backplane.memory.expiry` — background reaper for rows past
  `doc_metadata.expires_at`. Between expiry and reap, `recall` /
  `list_memories` hide expired rows in Python and search hides them
  in SQL.
* `meho_backplane.settings.Settings.memory_user_default_ttl_days` —
  env-var-controlled (`MEMORY_USER_DEFAULT_TTL_DAYS`), default 7. The
  shared `resolve_default_expires_at` resolver is the only reader;
  widening the default-TTL gate to other scopes is a one-line change
  in `memory/ttl.py`.
* `meho_backplane.mcp.tools.memory._parse_iso_duration` — parses
  the wire-string ISO 8601 duration ("P7D", "PT1H") into an absolute
  `datetime`. Months/years rejected (variable-length).

## Known issues

* **`onupdate` on `Document.updated_at` fires only on ORM UPDATEs.**
  Raw-SQL updates against PG bypass the SQLAlchemy hook. The memory
  write paths go through the ORM-backed `index_document`, so the
  hook fires; ad-hoc admin SQL would not.
* **No async-indexer fan-out.** Every `remember` blocks on the
  inline embedding computation. Acceptable at v0.3 throughputs
  (single-digit RPS per tenant); a future re-architecture would
  hand off to a background indexer queue.
* **Hybrid retrieval has no SQLite analogue.** `search_memories`
  exercises PG-only operators (`@@`, `<=>`, `pg_input_is_valid`); the
  unit tests mock the retrieve helper and the PG-real contract lives
  in `tests/integration/test_memory_eligibility_pg.py` and
  `tests/acceptance/test_g51_memory_canary.py`.
* **The expiry sweeper still compares strings.** `memory/expiry.py`
  deletes rows by a text `<` against `now()` in ISO form. That is only
  right for UTC values; search no longer depends on it, because the
  search SQL parses the timestamp itself.
* The MCP `add_to_memory` body field underwent a rename
  `content` -> `body` to align with `add_to_knowledge` and the REST
  `POST /api/v1/memory` body schema. G0.9.1-T7 (#779) shipped the
  rename; G0.13-T4 (#1134) retro-fitted the missing CHANGELOG and
  release-body callout against the actual release window (v0.6.0, not
  v0.3.2 as the original task's AC targeted) and added a one-cycle
  compatibility shim. v0.6.x accepts both fields: `body` is canonical
  and wins when both are supplied; `content` is a deprecated alias
  that fires a structured `add_to_memory_field_deprecated` warning
  log line with `replacement="body"`. The shim is dropped in v0.7.
* Non-`user` scopes have no default-TTL gate by design (per #624's
  narrow scope). A future Task widening it should change the
  `scope is not MemoryScope.USER` branch in `memory/ttl.py` and add
  matching tests on both surfaces.

## References

* Parent Initiative: G5.1 #421 (memory service initial build) and
  G5.2 (memory feature surface).
* Hybrid retrieval substrate: G0.4 #225 / #261 (`retriever.py`).
* T2 #422 — REST memory router.
* T3 #423 — MCP meta-tools.
* Goal #221 — Foundational substrate; parents the v0.3.x stream.
* Initiative #772 — G0.9.1 v0.3.2 dogfood hardening.
* Task #775 (G0.9.1-T3) — apply default-TTL injection on the MCP
  `add_to_memory` path; lifts the resolver into `memory/ttl.py`.
* Task #624 (G5.2-T2) — original default-TTL contract for the REST
  surface.
* #3741 — Memory eligibility before ranking, `POST /api/v1/memory/search`,
  CLI recall on the Memory service.
* G0.9.1-T4 #776 — `search_memory` timestamp passthrough fix
  (the path documented under "Read path — search" above).
* Best-practices: `python_best_practices.md` (don't duplicate
  validation logic across entry points — one canonical resolver;
  pydantic set-vs-unset is the idiomatic discriminator).

* Task #3743 — canonical selected read `meho://memory/id/{document_uuid}`
  and `MemoryService.read_by_id` (parent Initiative #3729).
* Task #3751 — bounded, untrusted search evidence: query snippets,
  provenance, `rank` instead of scores, the MCP `delivery` fields and
  `outputSchema`, and the selected-read wire limit.

See also: [`untrusted-text-envelope.md`](./untrusted-text-envelope.md)
— both memory resources serve `body`, and `search_memory` serves each
`snippet`, inside the `<<UNTRUSTED_AGENT_TEXT` guard envelope (stored
prompt-injection hardening at the LLM-facing read boundary).
[`result-delivery.md`](./result-delivery.md) — the shared wire limits
and `delivery` states the search response and the selected read use.
