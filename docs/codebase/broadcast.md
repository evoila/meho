# Activity broadcast

Operator-facing real-time view of every audited operation, plus
agent-authored announcements, scoped per tenant. The substrate is a
Valkey 9.x Stream (`meho:feed:{tenant_id}`); the SSE surface
(`/api/v1/feed` and `/ui/broadcast/stream`) and the MCP tools
(`meho.broadcast.recent`, `meho.broadcast.watch`,
`meho.broadcast.announce`) all read or write to that single substrate.
MEHO-hosted agent runs reach the same substrate through the agent
meta-tool bridge (`broadcast_announce` / `broadcast_recent` /
`broadcast_watch`, #2548), which reuses these MCP handlers — see
`docs/codebase/agent-runtime.md` § Broadcast coordination tools.

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
   | Agent bridge | `broadcast_recent` / `broadcast_watch` meta-tools (#2548) | reuse the MCP recent/watch handlers | as MCP recent/watch | as MCP recent/watch |

   The SSE backlog prelude is the v0.8.0 fix for #1305 — see *Known
   issues* below.

   **Row identifier semantics (MCP recent/watch, #2479).** Every event
   row the two MCP read tools return carries the entry's Valkey stream
   id twice: `cursor` (self-labelled; round-trips as the tools'
   `cursor` input arg) and `id` (legacy alias of the same value —
   unlike every other MCP surface, a broadcast row's `id` is NOT the
   row's domain UUID). The durable identifiers live on the event
   fields: `event_id` / `audit_id` on operation rows; announcement
   rows have no UUID (until #2547 mints one).

   **Read-side `filter` object (MCP recent/watch).** Both tools accept a
   `filter` object narrowing by exact-match `op_class` / `principal` /
   `target` / `work_ref`, plus the boolean `active_only`. `target`
   matches a `BroadcastEvent`'s `target_name` or an announcement's single
   `target` **or any entry in its `targets` list**. `work_ref` matches
   an announcement's `work_ref` only (an audit-derived event never has
   one). `active_only` (default `false`) drops TTL'd announcement claims
   whose `expires_at` has elapsed; non-TTL events (announcements without
   a TTL, and all audit-derived rows) always pass. All matching runs on
   the raw model before the untrusted-text wrap (#2544).

## Key types

- `BroadcastEvent` (`broadcast/events.py`) — every audited operation
  publishes one. Fields: `event_id` (UUID), `ts`, `tenant_id`,
  `principal_sub`, `principal_name`, `target_name`, `op_id`,
  `op_class`, `result_status`, `audit_id`, `payload`, plus the lineage
  trio `actor_sub`, `agent_session_id` (UUID), `work_ref` (all
  `| None`). `event_kind = "audit_derived"` discriminator.
  - **Lineage projection (T3 #2545).** The three lineage fields mirror
    the `audit_log` columns of the same name: `actor_sub` is the
    RFC 8693 actor (the delegated agent that acted, distinct from
    `principal_sub` = the human/subject it acted for);
    `agent_session_id` groups every operation one agent run produced;
    `work_ref` ties the operation to an external change ticket. Every
    `BroadcastEvent` construction site reads them off the publish-site
    contextvars via `resolve_broadcast_lineage()`
    (`operations/_audit.py`) — the same `resolve_actor_sub()` /
    `agent_session_id_var` / `work_ref_var` the sibling audit-row writer
    reads — so a delegated agent's work is attributable to the agent on
    the feed instead of broadcasting under the human's `principal_sub`.
    Server-derived and trusted: no untrusted-prose envelope applies (the
    envelope guards agent free text on `AgentAnnouncementEvent` only).
    Optional with `None` defaults, so pre-T3 stream entries that predate
    the fields still validate on read. `event_matches`
    (`broadcast/history.py`) and the `meho.broadcast.recent` /
    `meho.broadcast.watch` `filter` object gained matching `actor_sub`
    and `work_ref` exact-match filters ("what has this agent been
    doing"); an announcement never qualifies for a lineage filter.
- `AgentAnnouncementEvent` (`broadcast/agent_events.py`) — agent-
  authored announcements published via `meho.broadcast.announce`.
  Fields: `tenant_id`, `principal_sub`, `activity`, `target`, `scope`,
  `phase`, plus the optional **structured intent claims** (Broadcast
  v2, #2544): `targets` (list, ≤10 names, each ≤256 chars — supersedes
  the single `target` for multi-target work), `planned_op_class` (the
  declared op class, spanning the full `classify_op` taxonomy),
  `ttl_minutes` (1..1440), `work_ref` (opaque change-ticket ref, ≤256,
  same convention as `AgentRun.work_ref`), `run_id` (UUID). A derived
  `expires_at` computed field (`ts + ttl_minutes`, or `None`) drives
  the `active_only` read filter. `kind` / `event_kind =
  "agent_announcement"` discriminator so readers can dispatch on the
  kind. All claim fields are optional with back-compatible defaults
  (`targets=[]`, the rest `None`), so pre-v2 stream entries parse
  unchanged.

  **Structure is trusted; prose is quarantined.** The typed fields
  (`planned_op_class`, `ttl_minutes`, `run_id`, `phase`, `ts`,
  `expires_at`) are server-validated bounded enums / ints / UUIDs /
  timestamps — they cannot carry a prompt injection, so `dump_event_wire`
  serves them **unwrapped** as trustworthy coordination data. The
  free-text fields (`activity`, `scope`, `target`, `targets[]`,
  `work_ref`) stay agent-authored prose and keep the untrusted-content
  envelope (`_ANNOUNCEMENT_UNTRUSTED_FIELDS` +
  `_ANNOUNCEMENT_UNTRUSTED_LIST_FIELDS`, wrapped per-element for the
  list). All filtering (`event_matches`) runs on the raw model **before**
  the wrap, so narrowing is unaffected by the envelope — the same split
  the pre-existing `target` filter already relied on. Invalid claims
  (11 targets, `ttl_minutes=0`, a 257-char `work_ref`, a non-UUID
  `run_id`, an out-of-enum `planned_op_class`) reject at the MCP
  boundary with JSON-RPC `-32602`, belt-and-suspenders with the
  pydantic `Field` bounds on the model.
- `op_class` sensitivity taxonomy (`classify_op` / `redact_payload` in
  `broadcast/events.py`) — derived from the op-id (no per-descriptor
  column), drives how `payload` is redacted before publish:
  - `read` / `write` — full detail (params pass through).
  - `credential_read` (`vault.kv.read` / `.list`) — aggregate-only;
    even the path string is withheld.
  - `audit_query` (`audit.*` / `meho.audit.*`) — aggregate-only +
    `row_count`; the query filter never broadcasts.
  - `credential_mint` (`harbor.robot.create`, `vault.token.create`,
    `vault.auth.approle.generate_secret_id`) — **response** carries a
    freshly-minted secret; aggregate-only.
  - `credential_write` (`vault.kv.put`, `vault.auth.userpass.write` /
    `.update_password`, `k8s.secret.create`, G11.7-T1 #1401) —
    **request params** carry the secret; aggregate-only.
  - `other` — full detail.

  `credential_read` and `audit_query` are *upgradeable*: a per-call or
  per-tenant override may surface full detail to an operator who already
  has the right to see the path/filter (G6.3 #379). `credential_mint`
  and `credential_write` are *non-upgradeable* — they carry secret
  material, so no override path (`compute_effective_broadcast_detail` in
  `broadcast/overrides.py`) may upgrade them to full; a `full` tenant
  rule on these classes is clamped back to aggregate.

  **Tier-1 floor on the dispatch path.** Classification is
  allowlist-driven, so a secret-bearing op missing from the
  `credential_*` allowlists (newly ingested, mis-registered, added
  without a pin) would fall through to a full-detail class. The
  dispatch publisher (`publish_broadcast` in `operations/_audit.py`)
  therefore runs every params dict through `scrub_broadcast_params`
  (`broadcast/events.py`): a key-name scrub (`password` /
  `client_secret` / `sessionToken`-shaped keys — the Tier-1 engine
  alone cannot catch these because a dict leaf carries no label) plus
  a Tier-1 deterministic-redactor pass with the packaged default
  policy (`Bearer ...` / `api_key=...` / `Authorization:` shapes
  embedded in string values). Any detection collapses the broadcast
  to aggregate-only regardless of `op_class`; no detection keeps
  decision #3's full detail. Fail-closed: a scrub error yields
  aggregate-only. Config scalars under secret-ish names
  (`bind_secret_id: true`, `secret_id_ttl: 3600`) are exempt so
  vetted Vault AppRole config writes keep their full mutation signal.
  The static companion is `tests/test_broadcast_classifier_coverage.py`,
  which enumerates every registered typed/composite op and fails CI
  when an op whose parameter schema declares a secret-shaped property
  still classifies to `write` / `other` — allowlist drift now breaks
  the build instead of broadcasting raw params. The MCP publish path
  (`mcp/handlers.py`) still relies on classification + overrides only
  (out of scope for the dispatch-path hardening).
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
    → enforce_announce_rate_limit(tenant_id, principal_sub)  ← fail-loud
      → (skipped when broadcast_announce_rate_per_minute == 0)
      → MULTI: INCR meho:ratelimit:announce:{tenant}:{sub}:{minute}
               EXPIRE …  60
      → count > limit → AnnounceRateLimitExceeded
           → McpRateLimitedError → JSON-RPC -32000 (retry-after in data)
    → publish_agent_announcement(AgentAnnouncementEvent)  ← fail-loud
      → XADD meho:feed:{operator.tenant_id} {event: <json>} MAXLEN ~ 10000
      → returns Valkey entry id verbatim
    → returns {event_id, cursor} to the agent
      (both the stream entry id — `cursor` is the canonical
       self-labelled name, #2479; `event_id` is the legacy alias
       and NOT a durable UUID: announcements carry no UUID)
```

The rate limit (G6.5-T6 #2546) is a per-`(tenant, principal)`
fixed-window counter (the canonical Redis `INCR` rate-limiter pattern)
enforced **before** the publish. It protects the count-trimmed stream
(`MAXLEN ~ 10000`): without it, one looping principal could evict the
whole tenant's coordination window in a burst. Default 10 announces per
60 s window (`broadcast_announce_rate_per_minute`, `0` disables). The
counter lives on the same fast broadcast client as the publish and is
fail-loud for the same reason — a Valkey wobble must not silently let a
principal bypass the cap. See `broadcast/rate_limit.py`.

The four-step broadcast discipline (check `meho.broadcast.recent` →
announce `start` → `update` → `completion`) is seeded into every MCP
session preamble as a static band (`BROADCAST_DISCIPLINE_BAND` in
`conventions/preamble.py`, #2546) so agents receive it without relying
on the optional consumer onboarding template.

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

### Read path (dispatch-time target-activity advisory)

On the **success path of a write-class dispatch**, the operation response
carries a compact advisory of recent *peer* activity on the same target so
the caller learns another principal is already active there — post-op
awareness, not a lock or a block (pre-op checking stays the discipline's
`meho.broadcast.recent` read step). The advisory rides
`OperationResult.extras["target_activity_advisory"]`
(`connectors/schemas.py`), the established envelope-extension slot.

```text
dispatcher._reduce_and_audit_success(...)   # after audit + broadcast
  → build_target_activity_advisory(operator, op_id, target_name)
      [gate — returns {} without any stream read when:]
        · settings.dispatch_activity_advisory_window_minutes == 0
        · classify_op(op_id) not in {write, credential_write, credential_mint}
        · target_name is None
      [otherwise — one bounded, newest-first read:]
        → XREVRANGE meho:feed:{tenant} + <since-ms> COUNT 100
          (newest-first, so the COUNT cap keeps the newest window
           entries — not the oldest, which an XRANGE + COUNT would)
        → parse + event_matches(target=target_name, active_only=True)
        → drop entries where (principal_sub, actor_sub) == the caller's
        → keep the newest 5, restore chronological order
  → wrap_ok_result(..., extras=advisory)
```

Design contract:

- **Write-class only.** Read-class dispatches (`read` / `credential_read`
  / `audit_query` / `other` / `approval`) short-circuit on the frozenset
  check before any Valkey call — the hot read path pays nothing.
- **Structure only, no prose.** Each entry is
  `{principal_sub, actor_sub?, kind (operation|announcement), op_id?/phase?, ts}`.
  The untrusted announcement free-text fields (`activity` / `scope` /
  `target` / `targets`) are never projected — the untrusted-prose envelope
  does not enter an op response (Initiative #2543, review finding 27).
- **Self-excluded.** An entry whose `principal_sub` *and* `actor_sub` both
  match the caller is the caller's own activity and is dropped; a sibling
  agent under the same human (distinct `actor_sub`) is a peer and surfaces.
- **Newest-first, so the cap keeps what matters.** The read is a single
  `XREVRANGE` (newest-first) time-bounded by the window and capped at 100
  entries; the newest five surviving peer entries are then re-ordered
  chronologically. An oldest-first `XRANGE` + `COUNT` would clip the tail
  and silently invert the "most-recent" contract on a busy target.
- **Fail-open and bounded.** Any error in the lookup (a Valkey teardown
  included) is swallowed and warn-logged (`target_activity_advisory_failed`),
  yielding no advisory rather than failing the op. The lookup *is* awaited
  on the success path, so it adds one small, bounded stream read to a
  write dispatch's latency — it never fails the dispatch, and the cost is
  capped by the `COUNT`.
- **Disable knob.** `DISPATCH_ACTIVITY_ADVISORY_WINDOW_MINUTES` (default
  `30`, `0` = off) gates the whole feature.

## Dependencies

- `redis.asyncio` (redis-py 7.4) — Valkey 9.x is wire-compatible with
  Redis 7.2.4; the same redis-py driver speaks both. **Two connection
  pools per process**, partitioned by read-timeout expectations
  (`broadcast/client.py`):
  - `get_broadcast_client()` — fast client, `socket_timeout=5 s`.
    Used by the readiness probe (`PING`), the publish hot path
    (`XADD`), and the SSE backlog prelude (`XREVRANGE`). A hung
    Valkey on this client raises `redis.TimeoutError` at 5 s so the
    `/ready` poll surfaces it.
  - `get_broadcast_blocking_client()` — long-poll client,
    `socket_timeout=35 s` (the 30 s `XREAD BLOCK` window + 5 s
    buffer). Used by every blocking `XREAD` caller: the SSE feed
    (`api/v1/feed.py`), the UI SSE bridge
    (`ui/routes/broadcast/stream.py`), the
    `meho.broadcast.watch` MCP tool, and the agent approval-wait
    loop (`agent/approval_wait.py`). The longer timeout lets a quiet
    BLOCK expire naturally (`xread` returns `None`) instead of
    raising `redis.TimeoutError` from the socket layer at 5 s, which
    would otherwise produce a spurious `feed_error` frame on every
    fresh SSE connection (RDC #789 N1 / Initiative #1353).
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

### `claude-rdc-hetzner-dc#789` N1 — fresh SSE connections die at ~5 s with a spurious error frame (FIXED v0.9.0, #1354)

**Symptom.** Every fresh `GET /api/v1/feed` (Bearer JWT) or
`/ui/broadcast/stream` (session cookie) SSE connection on a quiet
tenant died at ~5 s with a `feed_error` frame
(`code="broadcast_subsystem_unavailable"`) even when the substrate
was healthy. After #1305 the **first-byte** problem was fixed (the
backlog prelude surfaced ~50 entries on connect) but the **live tail
never survived one BLOCK cycle**: the post-prelude `XREAD BLOCK 30000`
raised `redis.TimeoutError` at ~5 s and the generator yielded the T11
error frame.

**Root cause.** The single process-wide broadcast client had
`socket_timeout=5.0` pinned for the fail-fast readiness probe. redis-py
7.4 resolves `xread`'s read-timeout from the connection's
`socket_timeout` when the caller passes no per-call timeout (see
`redis/asyncio/connection.py` `AbstractConnection.read_response`).
With `BLOCK=30000` but `socket_timeout=5.0`, every quiet BLOCK hit
the socket-layer `asyncio.TimeoutError` at 5 s and redis-py raised it
as `redis.TimeoutError` — well before the 30 s BLOCK window
expired. The generator's `except RedisError` arm caught it and
yielded `broadcast_subsystem_unavailable` (the unit test at
`test_api_v1_feed.py:866-909` even *asserted the bug as correct*).

A bare global removal of `socket_timeout` would have been wrong: the
same client serves the readiness `PING` (`broadcast/probe.py`), which
intentionally caps at 5 s so a hung Valkey can't block `/ready`
indefinitely.

**Fix.** Split the single client into two clients
(`broadcast/client.py`), each with its own connection pool:

- `get_broadcast_client()` — `socket_timeout=5 s`. Readiness probe,
  publish hot path, SSE backlog prelude (`XREVRANGE`).
- `get_broadcast_blocking_client()` — `socket_timeout=35 s` (30 s
  BLOCK + 5 s buffer). Every blocking `XREAD` caller: the SSE feed,
  the UI SSE bridge, the `meho.broadcast.watch` MCP tool, and the
  agent approval-wait loop.

Now on a quiet tenant, `XREAD BLOCK 30000` returns `None` after the
BLOCK window expires (the natural keepalive path), the generator's
`_consume_xread_batch` falls through to the heartbeat path, and the
SSE consumer sees a `: heartbeat\n\n` line instead of a `feed_error`
frame. A genuine transport failure — socket dead past the 35 s
window — still raises `redis.TimeoutError` and still produces the
T11 error frame (the operator-side remediation is the same).

**Acceptance tests** live in `backend/tests/test_api_v1_feed.py`
under `TestFeedGenerator`:

- `test_quiet_stream_block_timeout_yields_no_error_frame` — quiet
  BLOCK returns `None` → heartbeat, NOT `feed_error`.
- `test_fresh_dollar_quiet_stream_survives_past_fast_socket_timeout`
  — direct repro of the consumer signal: fresh `$` connection
  survives a window longer than the fast client's 5 s timeout and
  emits a heartbeat.
- `test_transport_timeout_mid_stream_emits_feed_error_after_prior_events`
  — `redis.TimeoutError` (now a genuine transport failure
  post-fix) still produces the T11 error frame.
- `test_broadcast_client.py:TestBlockingClientLifecycle` —
  asserts the fast / blocking split, distinct pools, and the
  ≥30 s socket_timeout invariant on the blocking client.

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

### SSE feed delivers zero bytes on deploy — `AuditMiddleware` buffered the stream (FIXED, #1389)

**Symptom.** On a real deploy `GET /api/v1/feed` (Bearer JWT) and
`/ui/broadcast/stream` (session cookie) delivered **zero bytes** for
the life of the connection even though the generator was yielding
`event: broadcast` frames — the live-activity UI and
`meho status --watch` stayed dark.

**Root cause (middleware side, distinct from #1305/#771).** The
chassis `AuditMiddleware` (`backend/src/meho_backplane/audit.py`) ran
the inner app through `_run_inner_app_buffered`, which appended every
`http.response.start` + `http.response.body` message to a list and
forwarded them only **after** the audit insert completed — i.e. after
`await app(...)` returned. An SSE generator's `await app(...)` never
returns (the BLOCK loop runs for the life of the connection), so the
buffer was never flushed. The handlers were correct (#1305 fixed the
backlog prelude / `$`-cursor blackout; #1354 fixed the 5 s
`socket_timeout` teardown); the bytes simply never left the
middleware. #1354 *unmasked* this defect by removing the periodic
spurious teardown that had been flushing the buffer with an error
frame.

**Fix.** `_run_inner_app_buffered` now recognises a
`text/event-stream` response (`_is_event_stream_start`, matched on the
`content-type` header of `http.response.start`) and forwards the start
message + every body chunk **immediately** through the real `send`
instead of buffering. The audit row is written when the stream ends —
normal completion or a client-disconnect `CancelledError` — so the
fail-closed "every authenticated action gets a row" contract holds.
The fail-closed-500 swap cannot apply once a stream's
`http.response.start` is on the wire; an audit-write failure is logged
loudly, and the feed handler surfaces transport faults to the
subscriber as an `event: feed_error` frame. Non-streaming JSON routes
keep the buffered fail-closed-500 contract verbatim.

**Acceptance test** lives in `backend/tests/test_api_v1_feed.py` under
`TestSseStreamsThroughMiddleware`. One test drives
`RequestContextMiddleware(AuditMiddleware(app))` as a raw ASGI
callable with an instrumented `send` and asserts a body chunk reaches
the transport *while the generator is still suspended* — it deadlocks
(and times out) against the old buffering behaviour. A second test
asserts the streaming request still writes one `audit_log` row.
(`httpx.ASGITransport` cannot prove incremental delivery — it runs the
inner app to completion before exposing the body — so the raw-ASGI
seam is the one that observes live forwarding.)

### Earlier issue: empty broadcast feed in v0.7.0 (#755)

The v0.7.0 cycle (`claude-rdc-hetzner-dc#753`) surfaced "I see empty
broadcast" without resolving the root cause; G0.15-T9 (tenant chip
wiring, #1217) confirmed the tenant context was correctly propagated
to the SSE endpoint, narrowing the suspect surface for v0.8.0
investigation. #1305 is the closing fix.

## References

- Sources:
  - `claude-rdc-hetzner-dc#789` N1 — fresh SSE connections die at
    ~5 s (v0.8.1 cycle, fixed in #1354).
  - `claude-rdc-hetzner-dc#771` Finding 14 — SSE feed delivers zero
    bytes (v0.8.0 cycle, fixed in #1305).
- Parent Initiatives: #1353 (G0.18 — v0.8.1 closed-loop dogfood
  hardening); #1302 (G0.16 — v0.8.0). Parent Goal: #221.
- Related closed: #1216 (G0.15-T7 BFF audit-thread — UI session is
  correctly tenant-scoped).
- ADR 0005 — Valkey 9.x as the broadcast substrate.
- Code:
  - Publishers: `backend/src/meho_backplane/broadcast/publisher.py`.
  - SSE API edge: `backend/src/meho_backplane/api/v1/feed.py`.
  - SSE UI bridge: `backend/src/meho_backplane/ui/routes/broadcast/stream.py`.
  - MCP tools: `backend/src/meho_backplane/mcp/tools/broadcast.py`.
  - Override CRUD (REST): `backend/src/meho_backplane/api/v1/broadcast_overrides.py`.
  - Override UI tab (BFF, tenant_admin): `backend/src/meho_backplane/ui/routes/broadcast/overrides.py` — the Overrides tab on `/ui/broadcast` (#1891). Lists/creates/deletes `BroadcastOverride` rows through the REST plane's `list_overrides_impl` / `create_override_impl` / `delete_override_impl` in-process (the same impl the admin MCP tools call), gated to `tenant_admin` via `resolve_operator_or_403`; delete is a two-step `<dialog>` confirm that spells out the re-exposure consequence; the create form echoes the 422 glob-not-regex / 409 already-exists as inline errors. The sensitive-op cross-link in the event drawer (`_event_drawer.html`) pre-fills the create form's `op_id_pattern`.
  - History (XRANGE) helper: `backend/src/meho_backplane/broadcast/history.py`.
  - Client / lifespan: `backend/src/meho_backplane/broadcast/client.py`.
- Valkey commands:
  - `XADD` — https://valkey.io/commands/xadd/
  - `XREAD` — https://valkey.io/commands/xread/
  - `XREVRANGE` — https://valkey.io/commands/xrevrange/
- SSE / EventSource — https://html.spec.whatwg.org/multipage/server-sent-events.html.
- Untrusted-text envelope on announcement re-serve (`dump_event_wire`
  wrapping `activity`/`scope`/`target` on the `recent`/`watch`/
  `tenant_feed` LLM-facing paths) —
  [`untrusted-text-envelope.md`](./untrusted-text-envelope.md).
