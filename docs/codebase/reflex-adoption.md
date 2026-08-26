# Reflex-adoption KPIs

## Overview

The reflex work (tool-group descriptions, plugin hooks, dispatch
advisory) is behavioural: its point is to change what an agent does
*first* in a session — read the broadcast feed before acting, announce
intent before a write, write learnings back to knowledge/memory. This
subsystem measures whether that change is happening, directly from the
synchronous, append-only, session-tagged `audit_log` (plus the #2544
`AgentAnnouncement` store) MEHO already keeps. No separate metrics
pipeline: the numbers are re-derivable at any time from the same
canonical record the G8 audit-trail surface queries.

It reuses the #444 usage-telemetry seam exactly — a `compute_*` service
behind a REST route + CLI verb, aggregating `audit_log` in Python for
SQLite/PostgreSQL portability — and adds a new metric family on that
seam rather than a parallel mechanism.

Surface: `GET /api/v1/audit/reflex` (operator role min; `tenant_filter`
gated behind `platform_admin`) and `meho audit reflex`.

## Key types

### `compute_reflex_report(...)` (`meho_backplane.reflex.adoption`)

The service. Three tenant-scoped fetches, then per-surface Python
correlation:

- `_fetch_meta_tool_rows` — successful (`status_code == 200`) MCP
  meta-tool **envelope** rows in the window: `method="MCP"`,
  `path = f"/mcp/tools/call/{tool}"` for `call_operation`,
  `broadcast_recent`, `add_to_knowledge`, `add_to_memory` (the same
  path-prefix filter #444 uses).
- `_fetch_dispatch_rows` — successful **dispatch** rows
  (`method="DISPATCH"`, `path = op_id`); the write-class filter is
  applied in Python via `classify_op` because the classifier is
  match-order-significant Python, not portable SQL.
- `_fetch_announce_first_seen` — `run_id -> earliest created_at` over
  `AgentAnnouncement` (`created_at <= until`, no lower bound so an
  announce made before the window can still cover an in-window op).

### `ReflexReport` / `SurfaceMetrics`

Frozen Pydantic models. `ReflexReport.surfaces` is always ordered
`[agent, cli_rest]`. Each `SurfaceMetrics` carries the three ratios
plus their raw numerator + denominator; a ratio is `None` (N/A) when
its denominator is 0, so a zero-activity surface never reads as a
misleading `0.0`.

## Metric definitions (v1)

Precise enough that a second implementation reproduces the numbers.
Only successful rows (`status_code == 200`) count. Let a *session* be a
distinct non-null `agent_session_id`.

**Surface split.** Every metric is reported for two surfaces,
partitioned by the presence of `agent_session_id` on the row:

- `agent` — rows **with** `agent_session_id` (MCP/agent-run traffic,
  where client-side reflex levers such as plugin hooks apply).
- `cli_rest` — rows **without** `agent_session_id` (CLI/REST traffic,
  which can only ever get server-side levers).

The MCP-meta-tool metrics (read-before-act, write-back) are structurally
computable only on the `agent` surface (envelope rows always carry a
session), so `cli_rest` reports them as `None` — that absence *is* the
signal that a surface has no client-side reflex lever.

1. **read-before-act** — of the sessions with ≥1 `call_operation`, the
   fraction whose **first** `call_operation` is *strictly preceded*, in
   the same session, by a `broadcast_recent`. Denominator: sessions
   with ≥1 `call_operation`. `None` when 0 (the whole `cli_rest`
   surface).

2. **announce-coverage** — of the write-class dispatch rows
   (`classify_op(op_id) ∈ {write, credential_write, credential_mint}`),
   the fraction executed with an announce claim earlier in the same
   session: an `AgentAnnouncement` whose `run_id == agent_session_id`
   and whose `created_at <= occurred_at`. A `cli_rest` write op has no
   session to correlate a claim, so it is never covered. Denominator:
   write-class dispatch rows. `None` when 0.

3. **write-back rate** — the count of `add_to_knowledge` +
   `add_to_memory` calls per 100 `call_operation` calls on the surface.
   `None` when there are 0 `call_operation` calls.

## Control flow

### API surface (`GET /api/v1/audit/reflex`)

`meho_backplane.api.v1.audit_reflex`. Resolves the effective tenant via
`authorize_tenant_scope` (own tenant, or a `platform_admin` filter →
403 otherwise), parses `since` / `until` (relative `<N>d`/`<N>h` or
ISO-8601, reusing `retrieval.usage.parse_since`; `until` defaults to
now; malformed → 400), binds the audit overrides
(`audit_op_id="meho.audit.reflex"`, `audit_op_class="audit_query"`)
so the row broadcasts aggregate-only per decision #3, then calls
`compute_reflex_report`. `audit_row_count` is bound to the total
write-class ops scored.

### CLI (`meho audit reflex`)

`cli/internal/cmd/audit/reflex.go`. Wraps the route via the generated
typed client (`ReflexEndpointApiV1AuditReflexGetWithResponse`), renders
a per-surface table (or `--json` for the raw envelope). Nil ratios read
`n/a`.

## Dependencies

- `audit_log` (`meho_backplane.db.models.AuditLog`) — the
  `agent_session_id` column (migration `0014`) is the session key.
- `AgentAnnouncement` (#2544) — the structured announce-claim store;
  `run_id` is the session correlation key (the agent run id doubles as
  the session id).
- `classify_op` (`meho_backplane.broadcast.events`) — the op-class
  classifier that decides write-vs-read on dispatch rows.
- `retrieval.usage.parse_since` — the shared `--since` grammar (#444).

## Known issues

- **Direct-MCP operator writes are attributed to `cli_rest`.** A
  write op dispatched over a bare MCP session *without* an agent loop
  gets no `agent_session_id` on its dispatch row (the dispatcher reads
  `agent_session_id_var`, bound only by the agent-run invoker), so it
  lands in `cli_rest` and reads as uncovered. Agents running in the
  bounded loop — the reflex work's target population — are unaffected.
- **TTL is not yet enforced on announce coverage.** v1 correlates a
  claim by session identity + `created_at <= occurred_at` ("an announce
  earlier in the same session"). The stricter "active claim" reading
  (bounded by `AgentAnnouncement.ttl_minutes`) is a future refinement;
  it is a subset of the current boolean, so v1 is the looser, simpler
  definition.
- **Emission wiring may lag the metric.** As with #444's usage surface,
  the metric is defined against audit-row shapes that some meta-tools
  may not yet emit; the report degrades to all-`None`/zero gracefully.

## Delineation from meho-internal#200 (taint metrics)

These reflex KPIs measure *in-session discipline* (did the agent read /
announce / write back). meho-internal#200's taint metrics measure
meho-vs-local-fallback *adoption* (is work going through MEHO at all).
Related, not merged — the reflex numbers may feed that monthly roll-up
later, but the two are computed from different signals and answer
different questions.

## References

- Task #3134; parent Initiative #3128.
- #444 — the `audit_log` aggregation + REST + CLI precedent reused here
  (`docs/codebase/retrieval.md`, `meho_backplane.retrieval.usage`).
- #2544 — the `AgentAnnouncement` structured announce-claim store.
- `docs/codebase/audit_query.md` — the sibling G8 audit-trail surface
  whose tenant-scoping posture this route mirrors.
- Decision #3 (`docs/decisions/locked-decisions.md`) — the
  audit-on-audit-query aggregate-only broadcast posture.
