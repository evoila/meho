# `memory/` — server-side memory layer

> Durable map of the memory write surface — REST `/api/v1/memory*`,
> MCP `add_to_memory` / `search_memory`, CLI `meho memory remember`.
> Update in lock-step with code changes; stale entries are bugs.

## Overview

`backend/src/meho_backplane/memory/` exposes the five-scope memory
layer consumer-needs.md §G5 defines (`user` / `user-tenant` /
`user-target` / `tenant` / `target`) on top of the G0.4 documents
table. Three transports — the REST router under `/api/v1/memory`, the
MCP `add_to_memory` / `search_memory` meta-tools, and the CLI `meho
memory` verb — all funnel through a single tenant-scoped
`MemoryService` that owns the RBAC matrix and the substrate I/O.

## Key types

| Symbol | Module | What it is |
|---|---|---|
| `MemoryService.remember(...)` | `memory/service.py` | Tenant-scoped write entry. Persists one row via `index_document`; RBAC matrix and slug validation run here. |
| `MemoryScope` | `memory/schemas.py` | Five-value `StrEnum`: `user`, `user-tenant`, `user-target`, `tenant`, `target`. |
| `MemoryRbacResolver` | `memory/rbac.py` | Per-scope write/read matrix. `tenant` writes require `tenant_admin`. |
| `RememberBody` | `api/v1/memory.py` | Pydantic v2 frozen model, `extra="forbid"`. REST request shape. |
| `_add_to_memory_handler` | `mcp/tools/memory.py` | MCP write handler. Receives `dict[str, Any]` arguments from the dispatcher. |
| `resolve_default_expires_at` | `memory/ttl.py` | Shared default-TTL resolver. Called by both REST and MCP write paths. |

## Control flow — write path (default-TTL contract)

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

## Dependencies

* **`meho_backplane.retrieval.indexer.index_document`** — the
  substrate write the service wraps. Owns the actual SQL.
* **`meho_backplane.settings.Settings.memory_user_default_ttl_days`** —
  env-var-controlled (`MEMORY_USER_DEFAULT_TTL_DAYS`), default 7. The
  shared resolver is the only reader; widening the default-TTL gate
  to other scopes is a one-line change in `memory/ttl.py`.
* **`meho_backplane.mcp.tools.memory._parse_iso_duration`** — parses
  the wire-string ISO 8601 duration ("P7D", "PT1H") into an absolute
  `datetime`. Months/years rejected (variable-length).

## Known issues

* The MCP `add_to_memory` schema names the body field `content` while
  the REST + KB schemas use `body` — sibling task #779 (G0.9.1-T7)
  closes that asymmetry separately. Out of scope here.
* Non-`user` scopes have no default-TTL gate by design (per #624's
  narrow scope). A future Task widening it should change the
  `scope is not MemoryScope.USER` branch in `memory/ttl.py` and add
  matching tests on both surfaces.

## References

* Goal #221 — Foundational substrate; parents the v0.3.x stream.
* Initiative #772 — G0.9.1 v0.3.2 dogfood hardening; this Task ships
  as part of it.
* Task #775 — apply default-TTL injection on the MCP `add_to_memory`
  path (the work this doc captures).
* Task #624 (G5.2-T2) — original default-TTL contract for the REST
  surface.
* Best-practices: `python_best_practices.md` (don't duplicate
  validation logic across entry points — one canonical resolver;
  pydantic set-vs-unset is the idiomatic discriminator).
