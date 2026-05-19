<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Memory layer (G5.1)

> Reads [CLAUDE.md](../../CLAUDE.md) postulate 5 (the agent surface is a narrow waist of meta-tools — `search_memory` / `add_to_memory` are 2 of the ~17) and postulate 7 (audit is synchronous, append-only). Sister to [docs/architecture/kb.md](kb.md): that doc owns the team-wide knowledge corpus that rides on the G0.4 retrieval substrate; this doc owns the per-operator × per-tenant memory layer that rides on the same substrate.
>
> Covers the implementation that landed under [Initiative #332 G5.1](https://github.com/evoila/meho/issues/332) (Tasks #421-#427). The operator runbook that uses this surface is [`docs/cross-repo/memory-migration.md`](../cross-repo/memory-migration.md). Sibling Initiatives (auto-expiry + tenant-promotion in [#374 G5.2](https://github.com/evoila/meho/issues/374), laptop-local migration UX in [#375 G5.3](https://github.com/evoila/meho/issues/375)) extend this surface; the read-side `expires_at` filter is wired here and the daily reap is G5.2's.

## What this surface does

One sentence: server-side per-operator × per-tenant memory across five scopes (user / user-tenant / user-target / tenant / target), replacing the laptop-local `~/.claude/.../memory/` files so the *team* becomes the unit of memory per consumer-needs.md §G5 L131.

The memory layer does not own a storage table of its own. It is a thin, memory-shaped vocabulary (slug, scope, `MemoryEntry`, expiry) over the G0.4 retrieval substrate's `documents` table, pinned to `source="memory"`. Every memory row is a `documents` row with `source="memory"` and `kind="memory-<scope>"`; the natural key is `(tenant_id, source, source_id)` where `source_id` is a colon-separated encoding of `(scope, user_sub, target_name, slug)` (see [`_internal.py`](../../backend/src/meho_backplane/memory/_internal.py)). Hybrid BM25 + cosine retrieval, tenant scoping, and the body-hash re-embed short-circuit are all inherited from G0.4 (#225) — G5.1 adds the vocabulary, the five-scope RBAC matrix, and the four consumer surfaces.

This realises [consumer-needs.md §G5](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/docs/meho-coordination/consumer-needs.md) (the team-as-unit-of-memory unlock; per-scope visibility; operator-initiated promotion only).

## Module shape

The substrate lives in [`backend/src/meho_backplane/memory/`](../../backend/src/meho_backplane/memory/):

| File | What it owns |
|---|---|
| [`schemas.py`](../../backend/src/meho_backplane/memory/schemas.py) | The string contract against the `documents` table — `MEMORY_SOURCE = "memory"` (in `_internal.py`), `kind_for_scope()` / `scope_for_kind()` for the `memory-<scope>` mapping (changing either is a data migration). `SLUG_PATTERN` (`^[A-Za-z0-9_\-\.]+$` — letters, digits, hyphen, underscore, **dot** — colon is forbidden because the `source_id` encoding uses it as a segment separator). `validate_slug()`. The `MemoryScope` `StrEnum` (USER / USER_TENANT / USER_TARGET / TENANT / TARGET). The `TARGET_SCOPED` and `USER_SCOPED` `frozenset`s the resolver dispatches on. Frozen Pydantic v2 models: `MemoryEntry`, `MemoryEntryCreate`, `MemoryEntrySearchHit`. |
| [`_internal.py`](../../backend/src/meho_backplane/memory/_internal.py) | Pure helpers — `MEMORY_SOURCE` constant, `auto_slug()` (12-char UUID hex prefix for unnamed remembers), `encode_source_id()` (colon-joined `scope:user_sub:target_name:slug` segments), `slug_from_source_id()` (`rsplit(':', 1)` inverse), `build_metadata()` (merges caller metadata with the bookkeeping fields `user_sub` / `target_name` / `expires_at`), `document_to_entry()`, `is_expired()`, `metadata_str()` / `metadata_datetime()` / `has_tag()` typed-extractors. |
| [`rbac.py`](../../backend/src/meho_backplane/memory/rbac.py) | The five-scope RBAC matrix — `MemoryRbacResolver` (`can_read`, `can_write`, `visible_kinds`) plus `PermissionDeniedError`. Stateless and pure; no database access. The matrix codifies consumer-needs.md §G5 L143-148. |
| [`service.py`](../../backend/src/meho_backplane/memory/service.py) | `MemoryService` — the single class every front-end calls. Stateless, method-scoped: each public method opens its own `AsyncSession` and commits before returning. The constructor takes an optional `MemoryRbacResolver` so tests can inject a fake; production callers leave it `None` and the service builds one internally. |
| [`__init__.py`](../../backend/src/meho_backplane/memory/__init__.py) | Re-exports `MemoryService`, `MemoryRbacResolver`, `PermissionDeniedError`, `MemoryEntry`, `MemoryEntryCreate`, `MemoryEntrySearchHit`, `MemoryScope`. |

## The five-scope shape

The scopes from consumer-needs.md §G5 L137-141 are encoded in two parallel ways: the typed `MemoryScope` enum (`MemoryScope.USER`, ...) and the `documents.kind` string (`memory-user`, ...). `kind_for_scope()` and `scope_for_kind()` are the canonical bijection — callers never derive `"memory-" + scope.value` inline.

| Scope (`MemoryScope`) | `documents.kind` | Visible to | Use for |
|---|---|---|---|
| `USER` (`"user"`) | `memory-user` | Just the writing operator, across **every** tenant they belong to | Personal behavioral preferences ("I prefer kubectl over k9s") that travel with the operator across tenants |
| `USER_TENANT` (`"user-tenant"`) | `memory-user-tenant` | The writing operator, within one tenant | Personal context scoped to one lab ("for this tenant I'm investigating the snapshot regression") |
| `USER_TARGET` (`"user-target"`) | `memory-user-target` | The writing operator, scoped to one target | Personal notes about one vCenter / k8s cluster |
| `TENANT` (`"tenant"`) | `memory-tenant` | Every operator in the tenant (read); `tenant_admin` only (write) | Team-wide conventions ("we use GitOps for everything") |
| `TARGET` (`"target"`) | `memory-target` | Every operator with access to the target (in v0.2: every operator in the target's tenant) | Target-specific gotchas ("rdc-vcenter requires VPN") |

Each scope's `documents` row carries:

- `source = "memory"`, `kind = memory-<scope>` (discriminator).
- `source_id` = `encode_source_id(scope, user_sub, target_name, slug)` — colon-joined segments. The encoding is asymmetric on slug-internal colons by design; the `SLUG_PATTERN` excludes them so the round-trip via `slug_from_source_id()` is uniquely decodable.
- `metadata` JSONB — carries `user_sub` (for user-flavoured rows), `target_name` (for target-flavoured rows), `expires_at` (optional ISO 8601), and any caller-supplied tags.

`TARGET_SCOPED = {USER_TARGET, TARGET}` and `USER_SCOPED = {USER, USER_TENANT, USER_TARGET}` are the `frozenset`s the resolver and service dispatch on; they make the per-scope branches symbolic rather than string-list-literal.

## The `MemoryService`

`MemoryService(rbac: MemoryRbacResolver | None = None)`. Every public method takes an [`Operator`](../../backend/src/meho_backplane/auth/operator.py) (bound by the route / CLI / MCP layer from the JWT) as its first parameter — no contextvar resolution; the tenant boundary and the operator's `sub` are auditable at the call site. RBAC dispatches via the injected resolver. The service does not hold a DB session — every method opens its own via `get_sessionmaker()`.

| Method | Wraps | Used by |
|---|---|---|
| `remember(operator, scope, body, slug=None, metadata=None, expires_at=None, target_name=None) -> MemoryEntry` | `validate_slug` (or `auto_slug` when `slug` is `None`) + `encode_source_id` + `build_metadata` + `index_document` (G0.4-T3). Body-hash short-circuit means a same-body re-remember costs only an `updated_at` bump. | `POST /api/v1/memory`, `add_to_memory`, `meho remember` |
| `forget(operator, scope, slug, target_name=None) -> bool` | Natural-key `select` + `delete`. Returns whether a row existed (idempotent — re-forgetting an already-absent slug is `False`, not an error). | `DELETE /api/v1/memory/{scope}/{slug}`, `meho forget` |
| `recall(operator, scope, slug, target_name=None) -> MemoryEntry \| None` | Natural-key `select`. Collapses "not found" and "RBAC denied" into the same `None` return — see the info-leak section below. Read-side expiry filter (rows with past `expires_at` surface as `None` until G5.2's reaper deletes them). | `GET /api/v1/memory/{scope}/{slug}`, `meho://memory/{scope}/{slug}`, `meho recall <scope>/<slug>` |
| `list_memories(operator, scope=None, slug_pattern=None, tag=None, include_expired=False, limit=100) -> list[MemoryEntry]` | `select(Document)` scoped to `source="memory"` and the operator's `visible_kinds`, ordered `updated_at desc`. SQL-side filter on `tenant_id` + `kind`; in-process filter on `user_sub` (user-scoped RBAC), `slug_pattern` (substring), `tag` (membership in `metadata.tags`), and `expires_at` (`include_expired=False` default). | `GET /api/v1/memory`, `meho list` |
| `search_memories(operator, query, scope=None, limit=10) -> list[MemoryEntrySearchHit]` | `retrieve()` (G0.4-T4) with `source="memory"` pinned and optional `kind=memory-<scope>`. Adapts `RetrievalHit` → `MemoryEntrySearchHit` (slug instead of source_id, per-signal `bm25_score` / `cosine_score` / ranks carried). Post-filters via `MemoryRbacResolver.can_read` so an operator's search never surfaces another operator's user-scoped row. | `search_memory` (MCP), `meho recall --query` |

The `recall` 404-vs-403 collapse is load-bearing: a caller cannot distinguish "no such memory" from "you don't have access" by the response shape. For user-flavoured scopes the operator's `sub` is in the `source_id` encoding, so another operator trying to recall someone else's slug gets `None` at the natural-key layer before RBAC is even consulted. The HTTP route (T2) renders both as 404, the MCP resource returns the same JSON-RPC `-32602` for both.

## The five-scope RBAC matrix

[`MemoryRbacResolver`](../../backend/src/meho_backplane/memory/rbac.py) — stateless, pure functions of `(operator, scope, ...)`. The matrix:

| Scope | Read | Write |
|---|---|---|
| `USER` | `operator.sub == stored.user_sub` | Any `operator` (writes their own row; the service binds `operator.sub` server-side) |
| `USER_TENANT` | `operator.sub == stored.user_sub` | Any `operator` |
| `USER_TARGET` | `operator.sub == stored.user_sub` + `target_name` present | Any `operator` + `target_name` present |
| `TENANT` | Any operator in the tenant (including `read_only`) | **`tenant_admin` only** |
| `TARGET` | Any operator in the tenant (including `read_only`) — G0.3 will tighten to per-target ACL | Any `operator` (not `read_only`) + `target_name` present — G0.3 will tighten |

Tenant scoping is enforced upstream by `documents.tenant_id`; the resolver only decides within-tenant visibility. `read_only` operators are denied every write across every scope.

The `TARGET` scope's "any operator in the tenant" RBAC is a v0.2 placeholder. The resolver's `can_read` / `can_write` branches for `USER_TARGET` and `TARGET` are the two call sites G0.3-T3's per-target policy will tighten when it lands; the resolver already accepts `target_name` precisely so the future per-target ACL plugs in without a signature change.

## Expiry

`expires_at` is stored in `documents.metadata` rather than as a dedicated column — the substrate is shared with G4 kb (no expiry concept), and adding a NULL-able expiry column for one consumer would pollute the schema. The read paths (`recall` / `list_memories` / `search_memories`) filter out rows whose stored `expires_at` lies in the past unless the caller opts in via `include_expired=True`. G5.1 ships only this read-side filter; G5.2 #374's daily background task is what *deletes* expired rows and writes the `INTERNAL/memory.expire` audit row.

The default-7-day-TTL injection on user-scoped writes (when the caller omits `expires_at`) is also G5.2 — G5.1 stores `None` in `metadata.expires_at` and the read-side filter treats it as "never expires". The `--persist` flag at the CLI and explicit `expires_at: null` at the API are the opt-outs G5.2 will respect.

## The four surfaces

All four converge on `MemoryService`. None is a wrapper for another — each is a transport front on the same backplane.

### REST (T2 #422) — four routes under `/api/v1/memory*`

[`backend/src/meho_backplane/api/v1/memory.py`](../../backend/src/meho_backplane/api/v1/memory.py):

| Route | Role | Notes |
|---|---|---|
| `POST /api/v1/memory` | `operator` | Create one memory (`remember`). Body: `RememberBody` (frozen Pydantic v2, `extra="forbid"`). 201 on success. `PermissionDeniedError` from the service → 403 (write denial is honest; the operator is identified and the matrix mismatch is the answer they need). 422 for invalid scope / slug / missing `target_name` on a target-scoped write. |
| `GET /api/v1/memory` | `operator` | List memories the operator can read (`list_memories`). Query params: `scope` / `slug_pattern` / `tag` / `include_expired` / `limit` (1-500, default 100). Returns `MemoryListResponse` envelope `{"entries": [...]}`. |
| `GET /api/v1/memory/{scope}/{slug}` | `operator` | Fetch one memory by natural key (`recall`). Optional `target_name` query param required for `user-target` / `target` scopes. 404 on both "not found" *and* "RBAC-denied" — the info-leak avoidance is the whole point of this surface shape. |
| `DELETE /api/v1/memory/{scope}/{slug}` | `operator` | Idempotent: 204 whether the row existed or not (a 404-on-missing would let an operator probe for a slug they can't read). |

There is **no memory search route** at `/api/v1/memory/search`. Memory-scoped search rides the G0.4-T5 `POST /api/v1/retrieve` route with `source="memory"` and optional `kind=memory-<scope>` — the same shape kb uses for its search.

Every route binds two contextvars **before** the service call so the chassis [`AuditMiddleware`](../../backend/src/meho_backplane/audit.py) and the publish-on-write broadcast hook classify the row correctly: `audit_op_id` (one of `memory.remember` / `memory.list` / `memory.recall` / `memory.forget`) + `audit_op_class` (`"read"` for `memory.list` / `memory.recall`, `"write"` for the other two). The op-class is bound explicitly because the broadcast classifier's suffix table would not match `memory.recall` (no `.list` / `.get` / `.info` suffix), `memory.remember` (no `.create` suffix), or `memory.forget` (no `.delete` suffix) and they would fall through to the `other` bucket. `remember` / `forget` additionally bind `audit_scope` + `audit_slug` so the audit payload carries the natural-key coordinates. The body itself is **never** bound — the audit row is for the operation, not the content.

### MCP meta-tools (T3 #423)

[`backend/src/meho_backplane/mcp/tools/memory.py`](../../backend/src/meho_backplane/mcp/tools/memory.py) — two of the ~17 agent-facing meta-tools (Memory family):

- **`search_memory(query, scope?, limit?)`** — `op_class="read"`, `operator` role. Hybrid retrieval over the tenant's memory corpus, post-filtered by the RBAC matrix. Default limit 10, cap 50. Returns `{"hits": [...]}`. Drives the agent's recall recipe (search → pick a slug → fetch the full body via the resource).
- **`add_to_memory(content, scope, ttl?, slug?, metadata?, target_name?)`** — `op_class="write"`, `operator` role. The `ttl` parameter accepts an ISO 8601 duration string (`"P7D"` for 7 days, `"PT1H"` for one hour); the handler parses it into a concrete `expires_at` before calling the service. `PermissionDeniedError` from the service → JSON-RPC `-32602` so the dispatcher's "invalid params" lane stays consistent.

`forget` and `list` are **deliberately absent** from the agent surface (CLAUDE.md postulate 5 carves the agent surface narrow). An agent reaches list-style queries via `search_memory(scope=...)`; `forget` is a deliberate-write op normally driven by operators, not by the agent without explicit confirmation.

The tool descriptions are load-bearing agent UX — `add_to_memory` is called by every agent session that learns something worth retaining. The descriptions name the lifecycle contract (default 7-day TTL on user scope when G5.2 ships, `--persist` opt-out) so an agent learns it from the tool definition itself rather than guessing.

### MCP resource (T3 #423)

[`backend/src/meho_backplane/mcp/resources/memory.py`](../../backend/src/meho_backplane/mcp/resources/memory.py) — `meho://memory/{scope}/{slug}` (`operator` role, `mimeType: text/markdown`). The fetch-by-slug companion to `search_memory`: the agent calls `resources/read` with a hit's `(scope, slug)` to get the full body. A malformed scope / slug or a slug absent in the operator's tenant collapses to `-32602` "not found" (same info-leak shape as the REST `GET` route).

The URI template is a v0.2 simple-expansion shape — `target_name` is **not** in the template. Tenant- and target-scoped reads still need it; the v0.2 resource is shaped for the user-flavoured scopes where the operator's own `sub` provides the disambiguation. A future polish would extend the template or add a query-parameter form; for now the resource serves the most-called shape (the agent reading back its own user-scoped memories).

### CLI (T4 #424)

[`cli/internal/cmd/memory/`](../../cli/internal/cmd/memory/) — four **top-level** Cobra verbs. Each wraps one REST route (or, for `recall --query`, the retrieve route) and renders human-readable output or `--json`. Auth piggybacks on the token `meho login` wrote.

| Verb | Backend call | Role |
|---|---|---|
| `meho remember "body" [--scope SCOPE] [--slug SLUG] [--target NAME] [--tag T] [--ttl 7d] [--json]` | `POST /api/v1/memory` | `operator` |
| `meho recall <scope>/<slug> [--target NAME] [--json]` | `GET /api/v1/memory/{scope}/{slug}` | `operator` |
| `meho recall --query "search terms" [--scope SCOPE] [--limit N] [--json]` | `POST /api/v1/retrieve` (`source="memory"` pinned) | `operator` |
| `meho forget <scope>/<slug> [--target NAME] [--confirm] [--json]` | `DELETE /api/v1/memory/{scope}/{slug}` | `operator` |
| `meho list [--scope SCOPE] [--tag T] [--include-expired] [--slug-pattern P] [--limit N] [--json]` | `GET /api/v1/memory` | `operator` |

The verbs are **top-level** (not nested under `meho memory ...`) per the consumer-needs.md §G5 ergonomic shape — "I prefer kubectl over k9s" is a user-scoped `meho remember`, not a `meho memory remember`. The `meho list` verb takes the bare-word slot the issue's acceptance criterion names verbatim (`meho list --scope user`).

`--ttl 7d` is parsed CLI-side into an RFC 3339 `expires_at` and POSTed as `expires_at` in the body; the shorthand accepts `s` / `m` / `h` / `d` suffixes (`time.ParseDuration` doesn't accept `d`, so days are handled explicitly). Empty `--ttl` defers to the backplane default (none in G5.1; default 7 days on `memory-user` writes when G5.2 ships).

`meho remember "body"` accepts inline text by default; the bare hyphen `-` reads stdin (`echo "body text" | meho remember -`) so piped migration works.

## How agents use it

Per CLAUDE.md postulate 5, the agent never sees vendor-specific tools. The memory flow is:

1. When the agent starts a session in a tenant, `search_memory(scope=user)` and `search_memory(scope=user-tenant)` for relevant context (the operator's preferences, the lab's conventions). The agent surface is two read tools; the implicit recipe is "search first, ask the operator second".
2. The agent picks a hit by slug, then `resources/read` on `meho://memory/{scope}/{slug}` for the full body if the snippet isn't enough.
3. When the agent (or the operator working with it) learns something durable — a preference, a tenant convention, a target gotcha — it `search_memory` first to avoid duplicates, then `add_to_memory` at the narrowest appropriate scope. Same-slug re-add merges in place via the body-hash short-circuit.
4. Promotion to a broader scope (e.g. "this user-tenant memory is actually useful tenant-wide") is **operator-initiated only** — the agent does not have a `promote_memory` tool. G5.2's `meho promote` CLI verb is the human-authorised path; AI-suggested promotion is explicitly disallowed per consumer-needs.md §G5 "Out of scope".

Ephemeral session notes belong in memory (this surface). Durable team-wide vendor knowledge belongs in the kb (G4.1, [`docs/architecture/kb.md`](kb.md)) — the distinction is who else benefits from the entry over time.

## Tenant boundary

Every route, MCP handler, and CLI verb derives `tenant_id` from the JWT-validated `Operator`. There is no surface that accepts a tenant id from the body / query string / argv — cross-tenant probes are impossible by construction. A cross-tenant `recall` surfaces as 404 (same shape kb uses). The G5.1 canary acceptance ([`backend/tests/acceptance/test_g51_memory_canary.py`](../../backend/tests/acceptance/test_g51_memory_canary.py), T5 #426) explicitly asserts the boundary holds across all five scopes.

## What's intentionally out of scope (for G5.1)

- **Auto-expiry executor** (the daily reap loop + the `INTERNAL/memory.expire` audit row + default-7-day-TTL injection on `memory-user` writes) — [#374 G5.2](https://github.com/evoila/meho/issues/374). G5.1 ships only the read-side filter and the `expires_at` metadata field.
- **Tenant-promotion verb** (`meho promote <scope>/<slug> --to <target-scope>`) and the per-scope promotion RBAC matrix — G5.2.
- **Laptop-local migration UX** (`meho migrate memory` interactive per-file picker + machine-local heuristic + post-login nudge) — [#375 G5.3](https://github.com/evoila/meho/issues/375). G5.1 ships only the server side + the `meho remember` CLI verb operators can pipe into manually.
- **AI-suggested promotion** — every promotion is operator-initiated per consumer-needs.md §G5 "Out of scope".
- **Cross-tenant memory federation** — explicitly disallowed; the tenant boundary is the substrate's strongest property.
- **Memory diff / history** — v0.2 stores latest-version only; `metadata.expires_at` plus `updated_at` are the only temporal fields.
- **Memory editing UI** (web admin) — [#341 G10.4](https://github.com/evoila/meho/issues/341).

## Sibling Initiatives

- **[#374 G5.2](https://github.com/evoila/meho/issues/374) — Auto-expiry background task + tenant-promotion verb + per-scope RBAC tightening.** Adds the daily reap (`MemoryExpirySweeper` running in the FastAPI lifespan, deleting rows whose `metadata.expires_at` is in the past and writing one `INTERNAL/memory.expire` audit row per affected tenant), the default-7-day-TTL injection on `memory-user` writes (with `--persist` opt-out), and the `meho promote <scope>/<slug> --to <target>` verb gated by a scope-aware RBAC helper. G5.1's read-side expiry filter is the contract G5.2 builds on; the `_PROMOTION_LADDER` lives there, not here.
- **[#375 G5.3](https://github.com/evoila/meho/issues/375) — Laptop-local memory migration UX.** Ships `meho migrate memory` as an interactive, re-runnable Cobra verb (not coupled to `meho login`). The verb walks `~/.claude/projects/<...>/memory/`, runs a machine-local heuristic to flag laptop-only entries, presents a per-file picker (huh-based form), and POSTs each accepted entry to `POST /api/v1/memory` at the operator-chosen scope. Idempotent on re-run via a stable `source_id` derived from the body SHA-256. Until G5.3 ships, the operator runbook ([`docs/cross-repo/memory-migration.md`](../cross-repo/memory-migration.md)) documents the manual migration via piped `meho remember`.

## References

- [Initiative #332 G5.1](https://github.com/evoila/meho/issues/332) — scope + DoD. Tasks: T1 [#421](https://github.com/evoila/meho/issues/421), T2 [#422](https://github.com/evoila/meho/issues/422), T3 [#423](https://github.com/evoila/meho/issues/423), T4 [#424](https://github.com/evoila/meho/issues/424), T5 [#426](https://github.com/evoila/meho/issues/426), T6 [#427](https://github.com/evoila/meho/issues/427).
- Substrate: [#225 G0.4 retrieval substrate](https://github.com/evoila/meho/issues/225) — `index_document` ([`retrieval/indexer.py`](../../backend/src/meho_backplane/retrieval/indexer.py)), `retrieve` ([`retrieval/retriever.py`](../../backend/src/meho_backplane/retrieval/retriever.py)).
- Tenancy: [#222 G0.1 Tenant model](https://github.com/evoila/meho/issues/222) — `Operator` + `TenantRole`.
- Sibling Goals: [#216 Goal G5 Memory layer](https://github.com/evoila/meho/issues/216) (parent), [#374 G5.2](https://github.com/evoila/meho/issues/374), [#375 G5.3](https://github.com/evoila/meho/issues/375).
- Consumer-needs.md §G5 (the canonical product spec): [evoila-bosnia/claude-rdc-hetzner-dc/docs/meho-coordination/consumer-needs.md](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/blob/main/docs/meho-coordination/consumer-needs.md) L131-160 — team-as-unit-of-memory, 5-scope shape, auto-expiry policy, operator-initiated promotion only.
- v0.1-spec §"Memory / context layer" L457-487.
- Canary acceptance: [`backend/tests/acceptance/test_g51_memory_canary.py`](../../backend/tests/acceptance/test_g51_memory_canary.py) (T5 #426) — 5-scope exercise + RBAC matrix + tenant boundary + 10-query eval corpus.
- Sister docs: [`docs/architecture/kb.md`](kb.md) (the team-wide knowledge corpus, same substrate), [`docs/architecture/mcp.md`](mcp.md), [`docs/architecture/audit.md`](audit.md).
- Operator runbook: [`docs/cross-repo/memory-migration.md`](../cross-repo/memory-migration.md) — the migration recipe from laptop-local files to MEHO.
