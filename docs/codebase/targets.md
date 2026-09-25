# Targets — listing and the result-handle threshold

## Overview

A **target** is a per-tenant registered endpoint an agent or operator
can act on: a vCenter, a Kubernetes API server, a Vault, an SSH host.
Rows live in the `targets` table (`backend/src/meho_backplane/db/models.py`,
class `Target`); `name` is unique among a tenant's live rows and is the
identifier every `target` argument resolves by.

Targets are reached through three fronts that share the table, not
each other's code:

| Front | Where | Purpose |
|---|---|---|
| REST `/api/v1/targets` | `backend/src/meho_backplane/api/v1/targets.py` | list, discover, describe, probe, create, patch, soft-delete |
| CLI `meho targets …` | `cli/` (Go) over the REST routes | operator verbs; `meho targets list` calls `GET /api/v1/targets` |
| MCP `list_targets` | `backend/src/meho_backplane/mcp/tools/topology.py` | working-surface enumeration for agents |

`meho_targets_register` (`mcp/tools/targets_register.py`) is the
operator-plane MCP write; every `target` argument elsewhere resolves
through `resolve_target` (`targets/resolver.py`). Destination checks
are covered in [`target-ssrf-guard.md`](target-ssrf-guard.md).

This document covers the two **list** paths and how `list_targets`
bounds its response (#3858).

## Key types

| Symbol | Where | Role |
|---|---|---|
| `Target` | `db/models.py` | ORM row, including the cached probe `fingerprint`, `extras`, and the per-target `tls_ca_pin` |
| `TargetSummary` / `TargetListResponse` | `targets/schemas.py` | REST list row and `{items, next_cursor}` envelope |
| `project_target_to_summary` | `targets/schemas.py` | the one ORM → `TargetSummary` projection (REST list + resolver diagnostics) |
| `_list_targets_handler` | `mcp/tools/topology.py` | MCP handler: tenant scope, query, page |
| `_reduce_target_page` | `mcp/tools/topology.py` | runs the page through JSONFlux |
| `JsonFluxReducer` | `operations/jsonflux_reducer.py` | the reducer `call_operation` results go through; thresholds and handle shape |
| `ResultHandleStore` | `connectors/result_handle_store.py` | Valkey-backed spill that `result_query` reads back |

## Control flow — listing targets

### REST and CLI: `GET /api/v1/targets`

1. Tenant comes from the JWT; soft-deleted rows (`deleted_at IS NOT
   NULL`) are excluded.
2. Keyset pagination on `name`: `limit` default 100, max 500; the
   route over-fetches `limit + 1` so `next_cursor` is non-null only when
   another page exists (#1312).
3. Each row is projected to the full `TargetSummary` (including
   `fingerprint`, `secret_ref`, TLS fields) and returned as
   `{items, next_cursor}`.

This path is **not** reduced. It is the operator surface: the CLI
renders the table and pages with `--cursor`, and the rows carry fields
the MCP projection omits. The JSONFlux bound protects an agent's
context window, which the REST front does not feed.

### MCP: `list_targets`

1. **Tenant scope.** No `tenant_id` → the caller's tenant. A slug or
   UUID naming another tenant requires `platform_admin` (#1641).
2. **One query.** A single SELECT of the five projected columns —
   `id`, `name`, `aliases`, `product`, `host` — keyset on `name`,
   `limit` default 100 / max 500, optionally narrowed by the product
   component of `connector_id`. No joins, no per-row follow-up query,
   and the `fingerprint` / `extras` JSON and CA pin are never loaded.
3. **JSONFlux.** The page `{targets, next_cursor}` goes through
   `JsonFluxReducer` with the same default thresholds as every
   `call_operation` result (CLAUDE.md postulate 6):
   - **At or under** (≤ 50 rows and ≤ 4 KB serialized): returned
     unchanged.
   - **Over**: `targets` is absent. The response is the reducer summary
     — `row_count`, `total`, `sample_rows_returned`, `sample_bytes`,
     `source_key: "targets"`, and `next_cursor` (kept as a preserved
     scalar) — plus `handle`, the same `ResultHandle` shape
     `call_operation` returns: `sample_rows` (at most 5 rows within the
     4 KB `jsonflux_sample_byte_budget`), `schema_`, `summary_md`, and
     `fetch_more`. `fetch_more.drill_in` names `result_query`;
     `fetch_more.native_pagination` names `connector_id` / `limit` /
     `cursor` for re-calling `list_targets`.
   - `row_count` and `total` count **this page**, not the tenant.
     `next_cursor` still continues the listing.
4. **Spill key.** The handle is spilled under the **caller's** tenant
   and subject, even when a `platform_admin` listed another tenant:
   `result_query` reads back under the JWT's tenant and subject, so that
   is the only key the caller can drill into.
5. The MCP dispatcher (`mcp/handlers.py`) packs the result as a text
   block **and** `structuredContent` (#2774), then writes the audit row
   and publishes the broadcast event.

An agent reads a reduced page with `result_query(handle_id=…)`: page
with `offset` / `limit`, or pass a `query` such as
`{"select": ["name", "product"]}` or a `product` filter. Narrowing with
`connector_id`, or a smaller `limit`, returns inline rows instead.

Response sizes measured locally for #3858 (in-process test app, SQLite,
rows about 165 bytes each):

| Page | Before (HTTP body / text block) | After |
|---|---|---|
| 89 rows, default `limit` | 30.5 KB / 14.7 KB | 5.1 KB / 2.4 KB |
| 10 rows (`limit=10`) | 3.6 KB / 1.7 KB | unchanged (inline) |
| 500 rows (`limit=500`) | 173 KB / 84 KB | 5.1 KB / 2.4 KB |

Handler time was 10–25 ms warm at every size in both versions.

## Dependencies

- SQLAlchemy 2.0 async sessions. A column select returns `Row` objects
  with attribute access by column name, not ORM instances.
- `JsonFluxReducer` and `ResultHandleStore`, which shares the broadcast
  Valkey client (`BROADCAST_REDIS_URL`). If the store cannot persist the
  spill, the handle still ships with `drill_in.available=false` and
  `reason="result_store_unavailable"`; the agent falls back to
  `native_pagination` (narrow with `connector_id`, or a smaller
  `limit`). See [`result-spill.md`](result-spill.md).
- Settings: `jsonflux_sample_byte_budget` (preview size),
  `result_handle_max_spill_rows` (spill cap, default 10000, above the
  500-row page maximum).

## Known issues

### #3858 — default `list_targets` timed out and returned inline rows above the threshold

**Symptom (field test #3143 F10).** On a tenant with 89 targets, the
default-params `list_targets` call from Claude Desktop timed out twice,
while 10-row pages returned instantly. The call also returned all 89
rows inline, above the size at which postulate 6 says a set-shaped
result becomes a handle.

**Root cause.**

- *Server-side latency was not the cause.* The suspected per-row
  fingerprint or probe joins do not exist. The MCP handler (and the
  REST route) ran one keyset SELECT; there was no N+1. Reproduced
  locally, the 89-row default call completes in about 25 ms.
  The handler did hydrate whole ORM rows, decoding each target's
  `fingerprint` / `extras` JSON and CA pin only to project five fields;
  that per-row decode is small but was the only per-row work on the
  path.
- *What scaled with the row count was the response size.* The handler
  bypassed JSONFlux. Only `call_operation` went through the reducer, so
  `list_targets` inlined every row, and the dispatcher emits the payload
  twice (text block and `structuredContent`). The 89-row default call
  put about 30 KB on the wire, against about 3.5 KB for a 10-row page;
  response size was the one input that differed between the calls that
  timed out and the calls that returned instantly. The size-dependent
  cost is on the client path (Desktop, the `mcp-remote` stdio bridge,
  the model's context). The field session's audit rows, which would
  confirm the server-side duration of the two timed-out calls, were not
  readable from the implementing session.

**Fix.** `list_targets` now reduces an over-threshold page through the
same `JsonFluxReducer` `call_operation` uses and returns a summary plus
`handle`, as described under [MCP: `list_targets`](#mcp-list_targets).
The query now selects the five projected columns only. The
`outputSchema` declares the reduced fields and no longer requires
`targets`. Tool name, arguments, the default `limit` of 100, and the
inline shape for small pages are unchanged. REST and the CLI are
untouched.

**Regression tests.** `backend/tests/test_mcp_list_targets_result_handle.py`
covers: an 89-row default page returns a handle and no inline rows,
within 4 KB; all rows read back through `result_query` (paging and
query); small pages stay inline; a `platform_admin`'s cross-tenant
handle is readable by the caller; and the listing is one SELECT with no
`fingerprint` or CA-pin column.
`test_list_targets_reduced_page_conforms` in
`backend/tests/test_mcp_output_schema_conformance.py` validates the
reduced payload against the declared `outputSchema`, which Claude
Desktop enforces client-side.

**Pending human verification.** The live check (default `list_targets`
under 5 s from Claude Desktop on a tenant with 89 or more targets) needs
a Desktop session against a deployed backplane running this release.

### MCP `next_cursor` on an exactly full last page

`list_targets` sets `next_cursor` whenever the page fills `limit`, so a
tenant whose target count is an exact multiple of `limit` gets one extra
empty follow-up page. The REST route avoids this by over-fetching
`limit + 1` (#1312). Not changed by #3858.

## References

- #3858, field test #3143 (F10).
- CLAUDE.md postulate 6 (JSONFlux and result handles); v0.1-spec §4.
- [`result-spill.md`](result-spill.md) (spill and `result_query` read-back);
  [`../architecture/jsonflux.md`](../architecture/jsonflux.md) (reducer contract).
- [`mcp.md`](mcp.md) (tool inventory, `structuredContent` emission, #2774).
- [`api-shape-conventions.md`](api-shape-conventions.md) §14 (MCP list-tool
  `limit` / `cursor` parity).
- #134 (preview serialized once, on `handle.sample_rows`); #1312 (REST
  `limit + 1` over-fetch); #1641 (cross-tenant listing gated to
  `platform_admin`).
