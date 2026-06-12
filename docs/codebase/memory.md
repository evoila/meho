# `meho_backplane.memory` — scoped tenant memory service

> Durable map of the memory subsystem. Update in lock-step with code
> changes; stale entries are bugs.

## Overview

The memory service is the persistent, RBAC-gated key-value layer that
backs both the REST surface (`/api/v1/memory/...`) and the MCP meta-
tools (`add_to_memory`, `search_memory`, `meho.memory.promote`, plus
the resource template `meho://memory/{scope}/{slug}`). All memory
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

Frozen wrap around `MemoryEntry` plus the per-signal retrieval
scores (`fused_score`, `bm25_score`, `cosine_score`, `bm25_rank`,
`cosine_rank`) that `MemoryService.search_memories` projects from
the retrieval substrate's `RetrievalHit`. The fused-score is what
the result list is sorted by; the per-signal numbers are observability
glass for "why was this hit ranked here".

### `MemoryService` (`memory/service.py`)

Singleton-style service: holds a `MemoryRbacResolver` and a
`structlog` logger; opens a fresh `AsyncSession` per method. No
shared cursor / connection state across calls. Public methods:
`remember`, `recall`, `forget`, `list_memories`, `search_memories`,
`promote`.

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
in **one** place — `memory/ttl.py:resolve_default_expires_at`. Both
surface layers consume it.

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
own set-vs-unset signal.

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

### Read path — search (`search_memories`)

1. Translate the optional `MemoryScope` arg into a `kind` filter.
2. Build a `metadata_filters` dict from the RBAC predicate
   (`_metadata_filters_for_scope`). User-flavoured scopes
   (`USER` / `USER_TENANT` / `USER_TARGET`) push
   `{"user_sub": operator.sub}` into the substrate;
   tenant/target-flavoured scopes pass `None` (within-tenant
   RBAC is unconditional, and the substrate's `tenant_id`
   predicate already enforces the tenant boundary).
3. Call `meho_backplane.retrieval.retriever.retrieve` with
   `source='memory'`, `kind=...`, and `metadata_filters=...`. The
   retriever runs a hybrid BM25 + cosine query, fuses with
   Reciprocal Rank Fusion, and SELECTs the full `documents` row
   for the top-fused ids. The `metadata_filters` dict is
   translated to a PG `documents.doc_metadata @> :jsonb`
   containment predicate that fires *before* the
   50-candidate-per-signal budget is allocated — pre-migration
   the budget could be burned on RBAC-invisible rows belonging
   to other operators, returning an under-filled or empty result
   even when matching memories existed deeper in the corpus.
4. The `RetrievalHit` model carries every `documents` column the
   downstream caller needs — including `created_at` /
   `updated_at` — so memory does not re-query the table.
5. For each hit, `_hit_to_search_result` extracts the
   `MemoryEntry` fields, drops expired rows (range predicate;
   see "Expiry note" below), and projects to a
   `MemoryEntrySearchHit` that passes the substrate timestamps
   through to `entry.created_at` / `entry.updated_at` verbatim.
   RBAC is **not** rechecked here — the push-down at step 3
   guarantees the rows are already operator-visible.

**Cross-scope (`scope=None`) fan-out (G4.4-T2 / #1179).** The
substrate's `metadata_filters` is flat `@>` containment; it
cannot express the conditional "user_sub must match for
user-flavoured kinds, no predicate for tenant/target-flavoured
kinds" predicate in a single call. `search_memories` resolves
this by issuing one `retrieve` per visible `MemoryScope` and
merging the per-call ranked lists on `fused_score` descending.
RRF is rank-based and scale-invariant, so per-call scores live
in the same `[0, 2/(RRF_K+1)]` envelope and cross-kind
comparison is a total-order sort without renormalisation. The
cost is a fixed 5× retrieve-volume increase on the cross-scope
path; correctness (no candidate-budget burn on invisible rows)
wins over volume on a workload that's already off the request
hot path.

**Expiry note.** `expires_at > now()` is a *range* predicate
and the T1 substrate (#1177) only expresses scalar containment
via `@>`. `expires_at` therefore stays as a post-retrieval
filter in `_hit_to_search_result` until a future Initiative
broadens the substrate to support range predicates. The
substrate-minimalism postulate forbids broadening T1's shape
for one consumer; the `MEMORY_USER_DEFAULT_TTL_DAYS` setting
plus the daily expiry reaper (`memory/expiry.py`) keep the
expired-row pool small enough that the post-retrieval drop is
a tolerable cost in practice.

**G0.9.1-T4 (#776) fix.** Before v0.3.2 the search path substituted
`EPOCH = datetime(1970, 1, 1, tzinfo=UTC)` for both timestamps and
relied on a stale comment claiming "the API layer renders this as
null". Pydantic v2's `model_dump(mode='json')` does no such thing —
it serializes the datetime verbatim, so every search response
carried `"1970-01-01T00:00:00Z"` on persisted rows. The fix carries
the real column values on `RetrievalHit` (substrate-level) and
passes them through unchanged; the placeholder constant is gone.

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
  ORM row's fields, including the timestamps.
* `meho_backplane.memory.rbac.MemoryRbacResolver` — the scope ×
  role matrix. Applied at write boundaries and at the
  `list_memories` in-process filter. `search_memories` no longer
  consults the resolver post-retrieval; G4.4-T2 (#1179) pushed
  the `user_sub` predicate into the substrate via
  `metadata_filters` so the SQL layer eliminates RBAC-invisible
  rows before the candidate budget is allocated.
* `meho_backplane.memory.expiry` — background reaper for rows past
  `doc_metadata.expires_at`. The read-side filter in
  `list_memories` / `search_memories` masks expired rows in the
  window between expiry and reap.
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
  exercises PG-only operators (`@@`, `<=>`); the unit tests mock
  the retrieve helper and the PG-real contract lives in
  `tests/acceptance/test_g51_memory_canary.py`.
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
* G0.9.1-T4 #776 — `search_memory` timestamp passthrough fix
  (the path documented under "Read path — search" above).
* Best-practices: `python_best_practices.md` (don't duplicate
  validation logic across entry points — one canonical resolver;
  pydantic set-vs-unset is the idiomatic discriminator).
