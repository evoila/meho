<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Knowledge base (G4.1)

> Reads [CLAUDE.md](../../CLAUDE.md) postulate 5 (the agent surface is a narrow waist of meta-tools — `search_knowledge` / `add_to_knowledge` are 2 of the ~17) and postulate 7 (audit is synchronous, append-only). Sister to [operations-substrate.md](operations-substrate.md): that doc owns the dispatcher and the `endpoint_descriptor` table; this doc owns the kb layer that rides on the G0.4 `documents` retrieval substrate.
>
> Covers the implementation that landed under [Initiative #331 G4.1](https://github.com/evoila/meho/issues/331) (Tasks #415-#420). The operator runbook that uses this surface is [`docs/cross-repo/kb-migration.md`](../cross-repo/kb-migration.md).

## What this surface does

One sentence: tenant-scoped retrieval of the team's distilled vendor knowledge (Markdown kb entries — vCenter / NSX / Vault / Keycloak / k8s / Argo / Harbor / general), so a new entry reaches every operator in the tenant on ingest instead of via clone + `grep`.

The kb layer does not own a storage table of its own. It is a thin, kb-shaped vocabulary (slug, snippet, `KbEntry`) over the G0.4 retrieval substrate's `documents` table, pinned to `source="kb"`. Every kb row is a `documents` row with `source="kb"` and `kind="kb-entry"`; the natural key is `(tenant_id, source, source_id)` where `source_id` is the slug. Hybrid BM25 + cosine retrieval, tenant scoping, and the body-hash re-embed short-circuit are all inherited from G0.4 (#225) — G4.1 adds the vocabulary, the four consumer surfaces, and the ingestion file-walker.

This realises [decision #2](../planning/v0.2-decisions.md) (one-shot import + ≥1-month overlap; operator-driven retire).

## Module shape

The substrate lives in [`backend/src/meho_backplane/kb/`](../../backend/src/meho_backplane/kb/):

| File | What it owns |
|---|---|
| [`schemas.py`](../../backend/src/meho_backplane/kb/schemas.py) | The string contract against the `documents` table — `KB_SOURCE = "kb"`, `KB_KIND_ENTRY = "kb-entry"` (changing either is a data migration). `SLUG_PATTERN` (`^[a-z](?:[a-z0-9.\-]*[a-z0-9])?$` — lowercase, starts with a letter, ends with letter/digit, **dots allowed** for version numbers like `vcenter-9.0-snapshot-revert`). `validate_slug()`, `InvalidKbSlugError`. Frozen Pydantic v2 models: `KbEntry`, `KbEntrySearchHit`, `KbIngestionResult` (four-bucket counter: inserted / updated / skipped / error). |
| [`file_walker.py`](../../backend/src/meho_backplane/kb/file_walker.py) | `walk_kb_directory(root, errors=None) -> Iterator[KbFileRecord]`. Recursively yields one record per ingestible `*.md` file: skips hidden paths and any path matched by an optional root-level `.kb-ignore` file (one glob pattern per line, `#` comments). Slug = front-matter `slug:` override when present, else `Path.stem`; validated. Front-matter parsed via `python-frontmatter` 1.x; malformed YAML → `KbFileParseError`. `errors=None` is strict mode (first bad file aborts); a supplied list is best-effort mode (per-file failures are collected, the walk continues). |
| [`service.py`](../../backend/src/meho_backplane/kb/service.py) | `KbService` — the single class every front-end calls. Stateless, method-scoped: each public method opens its own `AsyncSession` and commits before returning. Per-file commits during ingest are deliberate (one bad file in a 44-entry corpus must not roll back the good files). |
| [`__init__.py`](../../backend/src/meho_backplane/kb/__init__.py) | Re-exports `KbService`, `KbEntry`, `KbEntrySearchHit`, `KbIngestionResult`. |

There is no separate per-operation handler module — the issue's draft outline said "per-op handlers"; the shipped shape collapses all operations onto `KbService`'s methods, which is what every surface below calls.

## The `KbService`

`KbService` takes no constructor arguments. Every public method takes `tenant_id` as its first parameter — no contextvar resolution; the route / CLI / MCP layer binds the value from the operator's JWT and the tenant boundary is auditable at the call site. RBAC is **not** in the service: it assumes the caller already gated the role. Splitting RBAC out keeps the service callable from contexts with different role discipline (a future unattended reindex job).

| Method | Wraps | Used by |
|---|---|---|
| `ingest_directory(directory, tenant_id, *, dry_run=False) -> KbIngestionResult` | `walk_kb_directory` + `index_document` (G0.4-T3) per file, with an extra `SELECT` per file to classify inserted / updated / skipped. `dry_run` classifies without writing. **Confined to `KB_INGEST_ROOT`** (default `/opt/meho/kb-ingest`): the directory is resolved (symlinks followed, `..` collapsed) and a path landing outside the root raises `KbIngestRootError` before any file is read — the path-traversal / LFI guard from #101 (L8 + L14). The guard runs in `dry_run` mode too. | `POST /api/v1/kb/ingest`, `meho kb ingest` |
| `list_entries(tenant_id, *, filter_pattern=None, limit=100, offset=0) -> list[KbEntry]` | Direct `select(Document)` scoped to `source="kb"`, slug-sorted. `filter_pattern` is a forwarded SQL `LIKE`. Pure list, no retrieval. | `GET /api/v1/kb`, `meho kb list` |
| `get_entry(tenant_id, slug) -> KbEntry \| None` | Natural-key `select`. Slug is **not** re-validated here (the route/CLI does it); a malformed slug just yields `None`. | `GET /api/v1/kb/{slug}`, `meho kb show`, `meho://kb/{slug}` |
| `create_entry(tenant_id, slug, body, metadata=None) -> KbEntry` | `validate_slug` then `index_document`. Body-hash short-circuit means a same-body re-add costs only an `updated_at` bump. `metadata=None` preserves an existing row's metadata; `{}` clears it. | `POST /api/v1/kb`, `add_to_knowledge` |
| `delete_entry(tenant_id, slug) -> bool` | Natural-key `delete`. Returns whether a row existed. | `DELETE /api/v1/kb/{slug}`, `meho kb delete` |
| `search_entries(tenant_id, query, *, filters=None, limit=10) -> list[KbEntrySearchHit]` | `retrieve()` (G0.4-T4) with `source="kb"` pinned. Adapts `RetrievalHit` → `KbEntrySearchHit` (slug instead of source_id, a ~200-char snippet instead of full body). `filters` consumes only the `"kind"` key in v0.2; other keys are reserved. | `POST /api/v1/retrieve` (CLI search verb), `search_knowledge` |

The snippet/full-body split is the load-bearing agent recipe: `search → decide on a slug → fetch the full body` without round-tripping every full body on every search.

## The four surfaces

All four converge on `KbService`. None is a wrapper for another — each is a transport front on the same backplane.

### REST (T2 #416) — five routes under `/api/v1/kb*`

[`backend/src/meho_backplane/api/v1/kb.py`](../../backend/src/meho_backplane/api/v1/kb.py):

| Route | Role | Notes |
|---|---|---|
| `GET /api/v1/kb` | `operator` | Paginated list. Query params `filter` (SQL `LIKE`), `limit` (1-500, default 100), `offset`. Returns `{"entries": [KbEntryPreview]}` — body truncated to a 200-char preview. |
| `GET /api/v1/kb/{slug}` | `operator` | Full entry. Absent **or cross-tenant** slug → 404 `slug_not_found` (the conflation prevents enumerating other tenants via status differential). |
| `POST /api/v1/kb` | `tenant_admin` | Create / re-index. 201. Invalid slug → 422. |
| `DELETE /api/v1/kb/{slug}` | `tenant_admin` | Idempotent: 204 whether the row existed or not (a 404-on-missing would let an operator probe for a slug they can't read). |
| `POST /api/v1/kb/ingest` | `tenant_admin` | Server-side bulk ingest from a directory on the backplane host. The directory is **confined to `KB_INGEST_ROOT`** (default `/opt/meho/kb-ingest`) — a path resolving outside it (traversal or escaping symlink) returns **400** `kb_ingest_path_outside_root` before any file is read (path-traversal / LFI guard, #101). `tarball_url` is accepted by the request schema for forward-compat but returns **501** — only `directory` is implemented in v0.2. |

There is **no kb search route**. kb-scoped search rides the G0.4-T5 `POST /api/v1/retrieve` route with `source="kb"`. Every route binds `audit_op_id` (`kb.list` / `kb.show` / `kb.create` / `kb.delete` / `kb.ingest`) + `audit_op_class` (`read` / `write`) before the service call so the chassis audit middleware and the decision-#3 broadcast classifier shape the row correctly; the ingest route additionally binds the four `KbIngestionResult` counters into the audit payload — never the file contents.

### MCP meta-tools (T3 #417)

[`backend/src/meho_backplane/mcp/tools/knowledge.py`](../../backend/src/meho_backplane/mcp/tools/knowledge.py) — two of the ~17 agent-facing meta-tools:

- **`search_knowledge(query, filters?, limit?)`** — `op_class="read"`, `operator` role. Hybrid retrieval over the tenant's kb corpus. Default limit 10, cap 50. Returns `{"hits": [...]}` with a 200-char snippet per hit.
- **`add_to_knowledge(slug, body, metadata?)`** — `op_class="write"`, `operator` role (deliberately **not** `tenant_admin` like the REST `POST` route: the agent surface is intentionally narrow; the audit row + broadcast event provide traceability, and the stricter REST gate is for fleet-wide bulk imports the agent surface does not perform). `InvalidKbSlugError` → JSON-RPC `-32602`.

The tool descriptions are load-bearing agent UX (`search_knowledge` is one of the most-called tools across every G3-G9 flow) and teach the recipe "search → pick a slug → fetch the full body via the resource".

### MCP resource (T3 #417)

[`backend/src/meho_backplane/mcp/resources/kb.py`](../../backend/src/meho_backplane/mcp/resources/kb.py) — `meho://kb/{slug}` (`operator` role, `mimeType: text/markdown`). The fetch-by-slug companion to `search_knowledge`: the agent calls `resources/read` with a hit's slug to get the full body. A malformed slug or a slug absent in the operator's tenant collapses to `-32602` "not found" (cross-tenant reads do not reveal the foreign tenant's existence). `resources/subscribe` is advertised `false` in v0.2.

### CLI (T4 #418)

[`cli/internal/cmd/kb/`](../../cli/internal/cmd/kb/) — six operator verbs. Each wraps one REST route (or, for search, the retrieve route) and renders human-readable output or `--json`. Auth piggybacks on the token `meho login` wrote.

| Verb | Backend call | Role |
|---|---|---|
| `meho kb ingest <dir> [--dry-run] [--json]` | `POST /api/v1/kb/ingest` | `tenant_admin` |
| `meho kb search <query> [--limit N] [--json]` | `POST /api/v1/retrieve` (`source="kb"` pinned) | `operator` |
| `meho kb list [--filter P] [--limit N] [--offset N] [--json]` | `GET /api/v1/kb` | `operator` |
| `meho kb show <slug> [--json]` | `GET /api/v1/kb/{slug}` | `operator` |
| `meho kb add <slug> --body @file\|@-\|<text> [--metadata k=v,...] [--json]` | `POST /api/v1/kb` | `tenant_admin` |
| `meho kb delete <slug> [--confirm] [--json]` | `DELETE /api/v1/kb/{slug}` | `tenant_admin` |

`meho kb search` exists as an operator convenience; the agent reaches the same data via the `search_knowledge` meta-tool (CLI `list` / `show` are not MCP tools — the agent reaches `list` via a broad `search_knowledge`, and `show` is implicit once it picks a hit).

## How agents use it

Per CLAUDE.md postulate 5, the agent never sees vendor-specific tools. The kb flow is:

1. When the agent needs an answer it doesn't already have, `search_knowledge` is one of its first calls (before asking the operator a factual question — the answer may already be captured).
2. It picks a hit by slug, then `resources/read` on `meho://kb/{slug}` for the full body if the 200-char snippet is not enough.
3. When the agent (or the operator working with it) learns something generalisable, it `search_knowledge` first to avoid duplicates, then `add_to_knowledge` — same-slug re-add merges in place via the body-hash short-circuit. Ephemeral session notes belong in `add_to_memory` (G5), not the kb.

## Decision #2 migration shape

- One-shot import: the operator points `meho kb ingest` at their cloned consumer `kb/` directory; re-running after a consumer-side `git pull` is cheap (body-hash skip re-embeds only changed entries).
- ≥1-month overlap: the consumer repo's `kb/` stays live as the fallback; operators use `meho kb search` for daily lookups, `grep` is the override.
- Operator-driven retire — there is **no auto-retirement**. The retire decision is operationalised by G4.3's `meho retrieval retire-checklist` (a surface-scoped 5-criterion checklist; kb is one of three surfaces it scores), backed by the G4.3 eval corpus (#440, closed) and eval runner (#441, closed — `meho retrieval eval` / `POST /api/v1/retrieve/eval`, precision@5 / MRR / coverage@5 vs the `grep` baseline). The G4.3 Initiative [#373](https://github.com/evoila/meho/issues/373) tracks the remaining operationalisation. The operator runbook [`docs/cross-repo/kb-migration.md`](../cross-repo/kb-migration.md) is the end-to-end recipe.

## What's intentionally out of scope

- **Per-entry version history** — v0.2.next.
- **Tarball ingest** — the request schema accepts `tarball_url` but the route returns 501; v0.2.next.
- **kb editing UI (web admin)** — Goal G10.2 [#339](https://github.com/evoila/meho/issues/339).
- **Vendor docs ingestion** (the larger `docs/<product>-<version>/` shelf) — covered by the G0.6 + G0.7 operation-search surface, not the kb layer.
- **Multilingual retrieval** — English-only per the G0.4 substrate.

## References

- [Initiative #331 G4.1](https://github.com/evoila/meho/issues/331) — scope + DoD. Tasks: T1 [#415](https://github.com/evoila/meho/issues/415), T2 [#416](https://github.com/evoila/meho/issues/416), T3 [#417](https://github.com/evoila/meho/issues/417), T4 [#418](https://github.com/evoila/meho/issues/418), T5 [#419](https://github.com/evoila/meho/issues/419), T6 [#420](https://github.com/evoila/meho/issues/420).
- Substrate: [#225 G0.4 retrieval substrate](https://github.com/evoila/meho/issues/225) — `index_document` ([`retrieval/indexer.py`](../../backend/src/meho_backplane/retrieval/indexer.py)), `retrieve` ([`retrieval/retriever.py`](../../backend/src/meho_backplane/retrieval/retriever.py)).
- [decision #2](../planning/v0.2-decisions.md) — one-shot import + 1-month overlap.
- Downstream: [#373 G4.3](https://github.com/evoila/meho/issues/373) — retrieval migration tooling (eval + `meho retrieval retire-checklist`).
- Canary acceptance: [`backend/tests/acceptance/test_g41_kb_canary.py`](../../backend/tests/acceptance/test_g41_kb_canary.py) (T5 #419).
- [docs/architecture/connectors.md](connectors.md), [docs/architecture/mcp.md](mcp.md), [docs/architecture/audit.md](audit.md).
