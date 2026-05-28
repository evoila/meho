# Activity broadcast

Operator-facing real-time view of every audited operation, plus
agent-authored announcements, scoped per tenant. The substrate is a
Valkey 9.x Stream (`meho:feed:{tenant_id}`); the SSE surface
(`/api/v1/feed` and `/ui/broadcast/stream`) and the MCP tools
(`meho.broadcast.recent`, `meho.broadcast.watch`,
`meho.broadcast.announce`) all read or write to that single substrate.

## Overview

Three layers, separated for traceability:

1. **Writer side.** `AuditMiddleware` (`backend/src/meho_backplane/audit.py`)
   and the MCP dispatcher (`backend/src/meho_backplane/mcp/handlers.py`)
   both call `publish_event(BroadcastEvent)` after the audit row
   commits. The publisher is **fail-open** — a Valkey wobble never
   converts an OK request into a 5xx. The agent-authored counterpart
   `publish_agent_announcement(AgentAnnouncementEvent)` is
   **fail-loud** — the calling agent needs to know whether its
   announcement landed. Both go through one `XADD meho:feed:{tenant_id}`
   per call, single `event` field carrying the JSON-serialised model,
   `MAXLEN ~` trim to `BROADCAST_MAXLEN = 10_000` entries.

2. **Fanout side.** **There is no separate fanout worker.** The
   publisher writes directly to the per-tenant stream; readers
   subscribe directly. This is a deliberate v0.1+ shape (ADR 0005):
   Valkey Streams' XREAD BLOCK semantics give us the live-tail
   subscription primitive without an intermediate broker process. A
   future Slack mirror / cross-cluster bridge (G6.2 #333) would be
   the first surface that takes a separate consumer-group reader; v0.8
   does not have one.

3. **Reader side.** Three reader shapes for three call patterns:

   | Reader | Surface | Read primitive | Cursor default | Backlog |
   | --- | --- | --- | --- | --- |
   | SSE feed | `GET /api/v1/feed` (Bearer JWT) | `XREAD BLOCK` | `$` (live tail) | **Last 50 on `$` connections** |
   | SSE bridge | `GET /ui/broadcast/stream` (session cookie) | `XREAD BLOCK` | `$` (live tail) | **Last 50 on `$` connections** |
   | MCP recent | `meho.broadcast.recent` | `XRANGE` | 30-min window | All entries in window |
   | MCP watch | `meho.broadcast.watch` | `XREAD BLOCK` | caller-supplied `since_cursor` (required) | None — caller pins |
   | MCP resource | `tenant_feed` snapshot | `XREVRANGE + COUNT 50` | n/a | Latest 50 |
   | UI history | `GET /ui/broadcast/history` | `XRANGE` | 30-min window | All entries in window |

   The SSE backlog prelude is the v0.8.0 fix for #1305 — see *Known
   issues* below.

## Key types

- `BroadcastEvent` (`broadcast/events.py`) — every audited operation
  publishes one. Fields: `event_id` (UUID), `ts`, `tenant_id`,
  `principal_sub`, `principal_name`, `target_name`, `op_id`,
  `op_class`, `result_status`, `audit_id`, `payload`.
  `event_kind = "audit_derived"` discriminator.
- `AgentAnnouncementEvent` (`broadcast/agent_events.py`) — agent-
  authored announcements published via `meho.broadcast.announce`.
  Fields: `tenant_id`, `principal_sub`, `activity`, `target`,
  `scope`, `phase`. `event_kind = "agent_announcement"` discriminator
  so readers can dispatch on the kind.
- `Operator` (`auth/operator.py`) — the JWT-bound principal the SSE
  feed reads its `tenant_id` from. UUID.
- `UISessionContext` (`ui/auth/middleware.py`) — the session-cookie
  equivalent, same `tenant_id: uuid.UUID` shape. Sourced from the
  encrypted session row.

## Control flow

### Write path (audit-derived)

```
HTTP request
  → AuditMiddleware (audit.py)
    → handler runs
    → audit row INSERT commits
    → publish_event(BroadcastEvent)        ← fail-open
      → XADD meho:feed:{operator.tenant_id} {event: <json>} MAXLEN ~ 10000
```

### Write path (agent-authored)

```
MCP tools/call meho.broadcast.announce
  → _handler_announce (mcp/tools/broadcast.py)
    → publish_agent_announcement(AgentAnnouncementEvent)  ← fail-loud
      → XADD meho:feed:{operator.tenant_id} {event: <json>} MAXLEN ~ 10000
      → returns Valkey entry id verbatim
    → returns {event_id} to the agent
```

### Read path (SSE)

```
GET /api/v1/feed (Bearer JWT)
  → require_role(OPERATOR) gate
  → _validate_cursor_or_400(_resolve_cursor(Last-Event-Id, since))
  → _feed_generator(operator, cursor, op_class, principal, target)
    [prelude — only when cursor == "$"]
      → XREVRANGE meho:feed:{tenant_id} + - COUNT 50
      → yield each entry as `event: broadcast` frame (chronological)
      → advance cursor to last entry id
    [live-tail loop]
      → XREAD BLOCK 30000 COUNT 20 meho:feed:{tenant_id} {cursor}
      → yield each surviving entry as `event: broadcast` frame
      → heartbeat on outbound silence ≥ 30s
```

### Read path (UI SSE bridge)

`GET /ui/broadcast/stream` is the session-cookie-gated mirror of
`/api/v1/feed`. The browser's `EventSource` can't set
`Authorization: Bearer ...` headers, so the live broadcast page
(`/ui/broadcast`) wires its `sse-connect` to this bridge, which
authenticates via `UISessionMiddleware` and the BFF session cookie.
Frame shape is byte-compatible with the API edge — the `_process_entries`
helper, the cursor resolver, and the backlog prelude are imported
verbatim from `api/v1/feed.py`.

## Dependencies

- `redis.asyncio` (redis-py 7.4) — Valkey 9.x is wire-compatible with
  Redis 7.2.4; the same redis-py driver speaks both. Connection pool
  per-process, cached via `broadcast/client.py`'s
  `get_broadcast_client()`.
- `BROADCAST_REDIS_URL` env var — production points at the Helm
  chart's `redis://{{ .Release.Name }}-broadcast:6379/0` Service.
  Dev default is `redis://localhost:6379`. Schemes other than
  `redis://` / `rediss://` / `unix://` are rejected at startup.
- `prometheus_client.Counter` — three counters surface the publish
  path on `/metrics`:
  `broadcast_events_published_total{op_class,result_status}`,
  `broadcast_publish_errors_total`,
  `broadcast_agent_announcements_total{phase}`.

## Known issues

### `claude-rdc-hetzner-dc#771` Finding 14 — SSE feed delivers zero bytes (FIXED v0.9.0, #1305)

**Symptom.** A fresh `GET /api/v1/feed` (Bearer JWT) or
`/ui/broadcast/stream` (session cookie) returned zero bytes within a
6–8 s curl test window even when the tenant stream contained 76+
entries and fresh writes were observed via `mcp.broadcast.announce`
during the window. The operator-facing `/ui/broadcast` page rendered
its "Live activity across tenant X" header but an empty event list,
permanently.

**Root cause (consumer side).** The SSE generator's initial cursor
was unconditionally `$` (`_LIVE_TAIL_CURSOR`). Valkey's `$` means
"deliver only entries XADD'd after the XREAD call landed", which
combined with the 30 s heartbeat cadence produced two visible
failure modes:

1. **No new writes during the window → 0 bytes.** A curl test with
   no concurrent writer would see zero bytes for 30 s
   (`_HEARTBEAT_INTERVAL_SECONDS`) — well past the 8 s curl default
   timeout — even on a tenant with thousands of entries on the
   stream. The HTTP intermediary observed a dropped connection
   without a single byte transferred and assumed the SSE endpoint
   was broken.
2. **76+ existing events never surfaced.** The `/ui/broadcast`
   page's first render had no signal of life, no backlog, no
   indication that the operator was even on the right tenant.
   The MCP `meho.broadcast.recent` tool (XRANGE-based) surfaced
   the same events fine to agents, deepening the consumer's
   "the SSE layer is broken" diagnosis.

**Not** a consumer-group mismatch (the publisher and readers use
plain XADD / XREAD against the same key with no consumer-group
layer), **not** a fanout worker outage (no separate fanout worker
exists; writers XADD directly), **not** a tenant-scoping divergence
(every reader and the publisher derive the stream key from the same
`tenant_id: UUID` field; the `f"meho:feed:{tenant_id}"` string is
byte-identical across surfaces).

**Fix.** `_feed_generator` and `_ui_feed_generator` now run a
**backlog prelude** before entering the BLOCK loop, but only when
the resolved cursor is `$` (the live-tail default for a fresh
connection without `Last-Event-Id` / `since`). The prelude:

- Issues `XREVRANGE meho:feed:{tenant_id} + - COUNT 50`.
- Reverses the result into chronological order.
- Filters via `_process_entries` (same filter helper the live loop
  uses, so `op_class` / `principal` / `target` apply identically).
- Yields each surviving entry as a regular `event: broadcast`
  SSE frame — wire-shape identical to the live loop, so clients
  cannot distinguish "this is replay" at the frame level.
- Advances the live-loop cursor to the most recent prelude entry
  id (NOT the last *matched* one — mirrors the live loop's
  `_consume_xread_batch` "advance past every consumed entry"
  invariant so a busy-but-filtered tenant doesn't re-read the same
  prelude batch).

Explicit-replay cursors (`Last-Event-Id`, `since`) skip the prelude
— the caller pinned an anchor; replaying from `+` would re-deliver
entries the caller already saw.

The cap (`_BACKLOG_PRELUDE_COUNT = 50`) matches the MCP
`tenant_feed` snapshot ceiling so the SSE prelude and the MCP
resource surface the same window of context on first connection.

**Acceptance test** lives in `backend/tests/test_api_v1_feed.py`
under `TestFeedBacklogPrelude`. The test mirrors the RDC repro:
publish via `_publish_event_into_mock` → open the SSE generator →
assert the published events show up in the first batch of frames
within bounded latency. A second test asserts that an explicit
`since=` cursor skips the prelude.

### Earlier issue: empty broadcast feed in v0.7.0 (#755)

The v0.7.0 cycle (`claude-rdc-hetzner-dc#753`) surfaced "I see empty
broadcast" without resolving the root cause; G0.15-T9 (tenant chip
wiring, #1217) confirmed the tenant context was correctly propagated
to the SSE endpoint, narrowing the suspect surface for v0.8.0
investigation. #1305 is the closing fix.

## References

- Source: `claude-rdc-hetzner-dc#771` Finding 14, signal draft
  `sse-feed-delivers-zero-events-despite-stream-writes`.
- Parent Initiative: #1302 (G0.16 — v0.8.0 closed-loop dogfood
  hardening). Parent Goal: #221.
- Related closed: #1216 (G0.15-T7 BFF audit-thread — UI session is
  correctly tenant-scoped).
- ADR 0005 — Valkey 9.x as the broadcast substrate.
- Code:
  - Publishers: `backend/src/meho_backplane/broadcast/publisher.py`.
  - SSE API edge: `backend/src/meho_backplane/api/v1/feed.py`.
  - SSE UI bridge: `backend/src/meho_backplane/ui/routes/broadcast/stream.py`.
  - MCP tools: `backend/src/meho_backplane/mcp/tools/broadcast.py`.
  - History (XRANGE) helper: `backend/src/meho_backplane/broadcast/history.py`.
  - Client / lifespan: `backend/src/meho_backplane/broadcast/client.py`.
- Valkey commands:
  - `XADD` — https://valkey.io/commands/xadd/
  - `XREAD` — https://valkey.io/commands/xread/
  - `XREVRANGE` — https://valkey.io/commands/xrevrange/
- SSE / EventSource — https://html.spec.whatwg.org/multipage/server-sent-events.html.
