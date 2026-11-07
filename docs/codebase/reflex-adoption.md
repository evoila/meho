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

The report also has a `learning` block with four numbers that show
whether stored lessons help: the repeat-mistake rate, the hit rate, the
precision of retrieved entries, and the time to the first correct
action on a known system. See "Learning numbers" below. The weekly
knowledge upkeep report carries the same four numbers per tenant
([kb.md](kb.md#weekly-upkeep-lessonsupkeeppy)).

Since #3734, `compute_reflex_report` also takes `source="request_usage"`.
Then the meta-tool calls come from privacy-bounded request usage events
(see [request-usage.md](request-usage.md)) instead of the MCP envelope
rows. Dispatch rows still come from `audit_log`. The route reads
`source="request_usage"` since #3747; when usage collection is off, the
meta-tool counts are zero.

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
misleading `0.0`. `ReflexReport.learning` is a `LearningMetrics`
(`meho_backplane.lessons.metrics`): the four learning numbers, each with
its count and base, `None` when the base is 0. They are not split by
surface: each one is about agent sessions.

### `LessonRetrieval` (`lesson_retrieval` table, migration `0142`)

One row for each knowledge or memory entry that `search_knowledge` (a
search, or a read by `slug`) or `search_memory` gave to a session
(`meho_backplane.lessons.retrievals`). It keeps the reading tenant, the
store (`kb` or `memory`), the entry id and its owner tenant, the
session id, the time, and the entry's header `status` and whether it had
expired at that moment. A call without a session id is not recorded.
Recording never fails the search. The entry id has no foreign key, so a
row stays when its entry is deleted. The upkeep job deletes rows older
than 180 days.

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

## Learning numbers

Defined in `meho_backplane.lessons.metrics`. Let an operation be a
`DISPATCH` audit row with a session id. It *failed* when its status is
400 or more, other than 429 (a rate limit is not a mistake), and it
*succeeded* when its status is 200. A retrieved entry was **used** when
the same session ran an operation that succeeded at or after the
retrieval. The audit row carries the checked result of the operation,
so this is the feedback MEHO already has.

4. **repeat-mistake rate** (`repeat_mistake_pct`) — of the failed
   operations in the window, the share where the same operation
   (`op_id`) on the same target (or both without a target) already
   failed in another, earlier session of the same tenant. The look-back
   reaches 90 days before the window. A failure in the same session
   again is not a repeat.
5. **hit rate** (`hit_rate_pct`) — of the retrievals in the window, the
   share that were used.
6. **precision** (`precision_pct`) — of the retrievals in the window
   whose entry still exists, the share that were right. An entry was
   wrong when its `status` was `disputed` at the search or is `disputed`
   now. It was stale when it had expired, or had `status: replaced` or
   `retired`, at the search. A deleted entry cannot be judged and is left
   out (`precision_judged` is the base).
7. **time to the first correct action on a known system**
   (`first_correct_action_median_seconds`) — for each session whose first
   successful operation in the window ran on a known target, the seconds
   from the session's start to that operation; the number is the median
   over those sessions (`first_correct_action_sessions`). A target is
   known when an operation on it succeeded in another, earlier session
   of the same tenant. The session starts at its first audit row, usage
   event or retrieval.

`meho audit reflex` prints them in a `[learning]` block after the
surfaces.

## Control flow

### API surface (`GET /api/v1/audit/reflex`)

`meho_backplane.api.v1.audit_reflex`. Resolves the effective tenant via
`authorize_tenant_scope` (own tenant, or a `platform_admin` filter →
403 otherwise), parses `since` / `until` (relative `<N>d`/`<N>h` or
ISO-8601, reusing `retrieval.usage.parse_since`; `until` defaults to
now; malformed → 400), binds the audit overrides
(`audit_op_id="meho.audit.reflex"`, `audit_op_class="audit_query"`)
so the audit row is tagged as an audit query (locked decision 3), then calls
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

- **"Used" is a broad signal.** Any operation that succeeds later in
  the same session uses every entry the session retrieved before it,
  even when the operation had nothing to do with the entry. So the hit
  rate is an upper bound. Reads through the `meho://kb/...` and
  `meho://memory/...` resources are not recorded; only the two search
  tools are. When recall before an operation ships, its entries can be
  recorded with the same `record_*_retrievals` helpers.
- **Precision needs a person or the upkeep job to mark entries.** It
  counts only what the header says: `disputed`, `replaced`, `retired`
  and the expiry. An entry nobody marked counts as right.

- **A direct MCP call without an `Mcp-Session-Id` header is attributed
  to `cli_rest`.** Since #4672 a write op dispatched over MCP gets the
  MCP session id on its dispatch row (the dispatcher reads
  `resolve_agent_session_id()`, which uses the agent run id inside an
  agent loop and the MCP session id otherwise), so it counts on the
  `agent` side. Only a call that sends no session header still has no
  `agent_session_id` and lands in `cli_rest`, where it reads as
  uncovered. Agents running in the bounded loop — the reflex work's
  target population — are unaffected.
- **TTL is not yet enforced on announce coverage.** v1 correlates a
  claim by session identity + `created_at <= occurred_at` ("an announce
  earlier in the same session"). The stricter "active claim" reading
  (bounded by `AgentAnnouncement.ttl_minutes`) is a future refinement;
  it is a subset of the current boolean, so v1 is the looser, simpler
  definition.
- **The read count uses the tool name `broadcast_recent`.** The report
  counts envelope rows with path `/mcp/tools/call/broadcast_recent`
  (`BROADCAST_RECENT_TOOL` in `reflex/adoption.py`). The MCP tool is
  registered as `meho_broadcast_recent`, so its rows carry
  `/mcp/tools/call/meho_broadcast_recent`. So read-before-act counts no
  reads today and reads `0.0` on the `agent` surface. The reflex advisory
  uses the right path (see [reflex-advisory.md](reflex-advisory.md)).
- **Emission wiring may lag the metric.** As with #444's usage surface,
  the metric is defined against audit-row shapes that some meta-tools
  may not yet emit; the report degrades to all-`None`/zero gracefully.

## How this differs from adoption metrics

These reflex KPIs measure *in-session discipline* (did the agent read /
announce / write back). Adoption metrics measure whether work goes
through MEHO at all, or falls back to local tools. They are computed
from different signals and answer different questions.

## References

- Task #3134; parent Initiative #3128.
- #444 — the `audit_log` aggregation + REST + CLI precedent reused here
  (`docs/codebase/retrieval.md`, `meho_backplane.retrieval.usage`).
- #2544 — the `AgentAnnouncement` structured announce-claim store.
- #5733 — the learning numbers and the weekly knowledge upkeep.
- `docs/codebase/audit_query.md` — the sibling G8 audit-trail surface
  whose tenant-scoping posture this route mirrors.
- Decision #3 (`docs/decisions/locked-decisions.md`) — the
  audit-on-audit-query aggregate-only broadcast posture.
