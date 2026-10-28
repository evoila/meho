# Activity broadcast

Broadcast is the tenant's feed of **deliberate agent announcements**:
"starting X", "update", "done". Since #3758 nothing else is on it.
Automatic activity (REST and MCP calls, dispatches, approvals, standing
grants, checks, topology writes, event ingest) publishes no Broadcast
event; the audit log (`query_audit`, `/ui/audit`) is the record of that
activity.

Announcements are stored in the `agent_announcement` table, and that
table is the record. Every reader reads it in commit order (#3748). The
Valkey 9.x stream `meho:feed:{tenant_id}` only wakes live readers early.
MEHO-hosted agent runs reach the same substrate through the agent
meta-tool bridge (`broadcast_announce` / `broadcast_recent` /
`broadcast_watch`, #2548) — see `docs/codebase/agent-runtime.md`
§ Broadcast coordination tools.

There is no Slack or other chat mirror of Broadcast (public issue #333
was closed as not planned).

## Overview

1. **Writer side.** Only `publish_agent_announcement`
   (`broadcast/publisher.py`), reached through `meho_broadcast_announce`
   / `POST /api/v1/broadcast/announce`, and only for a trusted agent
   identity (#3736). It stores the row first (**fail-loud**), then writes
   one `XADD meho:feed:{tenant_id}` entry, `MAXLEN ~ 10_000`
   (**fail-soft**: a failed write returns `delivery_pending=true` and
   counts on `broadcast_publish_errors_total`).

2. **Fanout side.** There is no fanout worker. Readers scan the archive
   and use `XREAD BLOCK` on the stream only as a wake-up; when Valkey
   fails they poll the archive every 5 seconds
   (`archive_reader.WATCH_POLL_SECONDS`).

3. **Reader side.** All readers read the archive:

   | Reader | Surface | Read | Start |
   | --- | --- | --- | --- |
   | MCP / REST recent | `meho_broadcast_recent`, `GET /api/v1/broadcast/recent` | `read_announcement_history` | 30-min window, paged |
   | MCP watch | `meho_broadcast_watch` | `watch_announcements` | caller's watch checkpoint |
   | SSE feed | `GET /api/v1/feed` (Bearer JWT, operator role) | `announcement_feed_frames` | newest 50, then live; `Last-Event-Id` / `since` resume |
   | SSE bridge | `GET /ui/broadcast/stream` (session cookie) | same generator | same |
   | CLI watch | `meho broadcast watch` / `meho status --watch` → `GET /api/v1/feed` | same generator | same |
   | MCP resource | `meho://tenant/{id}/feed` | `latest_announcements` | newest 50 |
   | UI history | `GET /ui/broadcast/history` | `latest_announcements` | retention window, newest `IN_DOM_ROW_CAP` |
   | Agent bridge | `broadcast_recent` / `broadcast_watch` meta-tools | the MCP handlers | as MCP |

   **SSE frames (#3758).** Each announcement is
   `event: broadcast` / `data: <AgentAnnouncementEvent JSON>` /
   `id: <watch checkpoint just after it>`. A reconnect with that id in
   `Last-Event-Id` resumes right after it. A `Last-Event-Id` that is not
   a checkpoint of this tenant (for example a Valkey stream id a browser
   kept from before #3758) starts fresh instead of a 400, so an
   `EventSource` does not get stuck. `since` takes a checkpoint or an
   ISO-8601 time; anything else is a 400. A retention gap or a
   checkpoint ahead of the archive is one `event: gap` frame. A database
   error after streaming began is one `event: feed_error` frame. A
   heartbeat comment follows 30 seconds without output.

   **Row identifier semantics.** Every reader returns `event_id` (the
   announcement's UUID, equal to the `agent_announcement` row's PK).
   Readers de-duplicate by `event_id`.

   **Read-side `filter`.** `op_class` / `principal` / `target` /
   `actor_sub` / `work_ref`, plus `active_only` on recent/watch.
   `op_class` and `actor_sub` are operation filters, so no announcement
   matches them; the filter vocabulary is retired in #3762.

**What moved off Broadcast (#3758).**

- *Approval wait.* The agent waiter reads the stored approval row every
  2 seconds (`agent/approval_wait.py`); it no longer touches Valkey.
- *Approvals bell.* One timer in `approvals-bell.js` dispatches
  `meho:approval-badge-poll` every 5 seconds while the page is visible;
  both badges re-fetch `GET /ui/approvals/badge`. A decision dispatches
  `meho:approval-bump` (badges plus the open history list).
- *Recent operations.* The dashboard tray (`GET /ui/dashboard/recent-operations`,
  newest 50 via the audit reader) and the connector card
  (`GET /ui/connectors/{name}/recent-operations`, newest 10) render on
  the server from `operation_execution` audit rows. `recent-ops-refresh.js`
  swaps in a fresh copy every 5 seconds while the page is visible; the
  card shows when it was updated. Both need the `operator` role, the
  same minimum as the audit API: a `read_only` session (or a role lift
  that cannot complete) sees a notice instead of rows, and the fragment
  answers 403 (`ui/roles.py::is_ui_operator`, `connectors/operator.py`
  `is_operator`).
- *Checks.* See [`checks-broadcast.md`](checks-broadcast.md).

Plain JS timers are used instead of `hx-trigger="every 5s [filter]"`
because htmx evaluates trigger filters with `eval`, which the console's
CSP does not allow.

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
    (`broadcast/history.py`) and the `meho_broadcast_recent` /
    `meho_broadcast_watch` `filter` object gained matching `actor_sub`
    and `work_ref` exact-match filters ("what has this agent been
    doing"); an announcement never qualifies for a lineage filter.
- `AgentAnnouncementEvent` (`broadcast/agent_events.py`) — agent-
  authored announcements published via `meho_broadcast_announce`.
  Fields: `event_id` (UUID, minted at publish via `default_factory=uuid4`
  — the real per-announcement identity, #2547; also the durable
  `agent_announcement` row's PK, served unwrapped), `tenant_id`,
  `principal_sub`, `activity`, `target`, `scope`,
  `phase`, plus the optional **structured intent claims** (Broadcast
  v2, #2544): `targets` (list, ≤10 names, each ≤256 chars — supersedes
  the single `target` for multi-target work), `planned_op_class` (the
  declared op class, spanning the full `classify_op` taxonomy),
  `ttl_minutes` (1..1440), `work_ref` (opaque change-ticket ref, ≤256,
  same convention as `AgentRun.work_ref`), `run_id` (UUID), and the
  server-stamped trusted claim fields (#3736): `claim_id`, `owner_sub`,
  `actor_sub`, `admission` (see *Who can publish*). A derived
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
  list) **only on LLM-facing re-serves**. The wrap is applied by the
  read-side serialiser: the archive reader `broadcast/archive_reader.py`
  (MCP `meho_broadcast_recent` / `meho_broadcast_watch` and
  `GET /api/v1/broadcast/recent`, #3748) uses `dump_event_wire` (wraps prose);
  the UI history pane and the SSE feeds send the plain `model_dump` (no
  wrap) because the browser sink escapes every field separately (Alpine `x-text` sets `textContent`) and the
  multi-line guard block would be human-visible noise on a feed row
  (#2549). All filtering (`event_matches`) runs on the raw model **before**
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
  - `approval` (`approval.*` lifecycle events, G10.7-T3 #1778) — full
    detail; exists so the approvals console bell can filter the SSE
    bridge server-side.
  - `checks` (`checks.transition`, #2720) — full detail; the checks
    subsystem's own state-change events, published outside the audit
    path. Matched by an **exact-membership allowlist**
    (`_CHECK_EVENT_OPS`), not by a `checks.` prefix: the
    `/api/v1/checks/*` gateway routes already own three op-ids under
    that prefix (`checks.assignment.put` / `.get`, `checks.results.post`),
    and because `classify_op` is re-run at render time in the drawers, a
    prefix branch would relabel their rows — historical ones included.
    See [`docs/codebase/checks-broadcast.md`](checks-broadcast.md).
  - `other` — full detail.

  **One class per call.** The audit row and the broadcast event of one
  call carry the same `op_id` and `op_class`. Every writer gets the class
  from `resolve_op_class(op_id, declared)` in `broadcast/events.py`:
  - The declared class wins when there is one: the MCP tool's
    `ToolDefinition.op_class`, or a route's `audit_op_class`. Tool names
    like `list_targets` or `meho_status` have no verb suffix, so
    `classify_op` alone would call them `other`.
  - The exception: when `classify_op` says `credential_*` or
    `audit_query`, that class wins. A declared class can never turn an
    aggregate-only broadcast into a full one.
  - With no declared class, the class is `classify_op(op_id)`.

  Where it is used:
  - **MCP `tools/call`** (`mcp/handlers.py`): the row and the event carry
    the resolved class, and the event carries the row's `op_id` (the tool
    name, or the `audit_op_id` the handler bound). `call_operation` keeps
    `tool_call` on both; only the redaction detail comes from the inner op.
    `resources/read` rows carry `op_id = mcp.resource.read` and `read`, like
    their events.
  - **Dispatch rows** (`operations/_audit.py`): the row's `op_id` is
    always the op that ran, and the row stores its class. A route label
    that is still bound while the op runs (for example `agent.run` /
    `write` from an agent run) no longer replaces them.
  - **HTTP rows** (`audit.py`): a route-declared class is stored as
    resolved. Rows with no declared class keep none, and audit query
    derives it the same way.

  MEHO's own registry writes that run through the dispatcher
  (`targets.register`, `topology.annotate` / `.unannotate` /
  `.bulk_import` / `.create_node` / `.delete_node`) are pinned `write` in
  `_WRITE_OPS`.

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

### Write path (agent-authored)

```
MCP tools/call meho_broadcast_announce   (REST: POST /api/v1/broadcast/announce)
  → _handler_announce (mcp/tools/broadcast.py)
    → resolve_agent_admission(operator)  (auth/agent_admission.py, #3736)
      → not a trusted agent → AgentIdentityRequiredError
           → MCP -32602, error.data.code = "agent_identity_required" (REST 403)
    → enforce_announce_rate_limit(tenant_id, principal_sub)  ← fail-loud
      → (skipped when broadcast_announce_rate_per_minute == 0)
      → MULTI: INCR meho:ratelimit:announce:{tenant}:{sub}:{minute}
               EXPIRE …  60
      → count > limit → AnnounceRateLimitExceeded
           → McpRateLimitedError → JSON-RPC -32000 (retry-after in data)
    → build_trusted_announcement(admission, …)  (broadcast/announcement_claims.py)
      → stamps principal, owner, actor, run, work_ref; mints claim_id on
        'start'; missing ttl_minutes → 30
    → publish_agent_announcement(event, idempotency_key)
      → claim rules + INSERT agent_announcement in one transaction  ← the record, fail-loud
        (update/completion lock the claim's start row until commit and
         look the retry key up under that lock;
         known retry key → replay the stored step, no INSERT)
        → UPDATE agent_announcement_sequence SET last_seq = last_seq + 1
            RETURNING last_seq  ← row's seq; locks the tenant counter until commit (#3748)
      → SELECT agent_announcement ... FOR UPDATE  ← one delivery at a time per row
        (stream_entry_id already set → nothing to deliver)
      → one Lua script (deliver once):  ← fail-soft (#3748)
          GET meho:announce:delivered:{tenant}:{event_id}  ← known → return it, no write
          XADD meho:feed:{operator.tenant_id} {event: <json>} MAXLEN ~ 10000
          SET meho:announce:delivered:{tenant}:{event_id} <entry id> EX <stream window>
        (any error → rollback, log broadcast_announcement_delivery_pending,
         delivery_pending=true)
      → UPDATE agent_announcement SET stream_entry_id  ← delivery mark, fail-soft
      → returns AnnouncementPublishResult(event, seq, stream_entry_id,
         replayed, delivery_pending)
    → returns {event_id, watch_checkpoint, delivery_pending, claim_id, phase,
       owner_sub, admission, ttl_minutes, replayed, …} to the agent
      (`event_id` is the per-announcement UUID minted at publish, equal to
       the durable row's id and to `event_id` on recent/watch reads;
       `watch_checkpoint` is a watch cursor just after this announcement.)
```

See *Who can publish* below for the admission and claim rules.

The durable INSERT runs **before** the `XADD` so the archive is never
lost to a stream-side failure. A persistence failure surfaces to the
agent as JSON-RPC `-32603` (nothing was stored). A stream failure after
the commit is not an error: the agent gets `delivery_pending=true`,
readers already see the announcement, and a retry with the same
`idempotency_key` finishes the stream write without a second row. See
*Durable announcements* and *Archive order and cursors* below.

The rate limit (G6.5-T6 #2546) is a per-`(tenant, principal)`
fixed-window counter (the canonical Redis `INCR` rate-limiter pattern)
enforced **before** the publish. It protects the count-trimmed stream
(`MAXLEN ~ 10000`): without it, one looping principal could evict the
whole tenant's coordination window in a burst. Default 10 announces per
60 s window (`broadcast_announce_rate_per_minute`, `0` disables). The
counter lives on the same fast broadcast client as the publish and is
fail-loud for the same reason — a Valkey wobble must not silently let a
principal bypass the cap. See `broadcast/rate_limit.py`.

The four-step broadcast discipline (check `meho_broadcast_recent` →
announce `start` → `update` → `completion`) is seeded into the MCP
session preamble as a static band (`BROADCAST_DISCIPLINE_BAND` in
`conventions/preamble.py`, #2546) so agents receive it without relying
on the optional consumer onboarding template.

The band goes only to a session that can use it (#4113).
`_session_instructions` in `mcp/server.py` checks the session's tool
list with `all_tools_for` (the same filter `tools/list` uses). If
`meho_broadcast_announce` is not on it, the assembler is called with
`include_broadcast=False` and the band is left out. The broadcast tools
need the `operator` role, so a `read_only` session gets no broadcast
band. The band also tells the agent to skip a step when its client does
not offer the tool, because MEHO cannot see tools the client hides. The
conventions write path keeps the default (`include_broadcast=True`), so
its result does not change.

### Read path (SSE and UI history)

See *Reader side* in the overview. `/ui/broadcast/stream` and
`/api/v1/feed` share `api/v1/feed.announcement_feed_frames`; the UI
history pane and the MCP resource share
`archive_reader.latest_announcements`. The shared row partial
(`broadcast/_event_row.html`) renders an announcement with its
agent-authored text bound via `x-text`.

### Read path (dispatch-time target-activity advisory)

On the **success path of a write-class dispatch**, the operation response
carries a compact advisory of recent *peer* activity on the same target so
the caller learns another principal is already active there — post-op
awareness, not a lock or a block (pre-op checking stays the discipline's
`meho_broadcast_recent` read step). The advisory rides
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

Three independent extras fragments now ride the same dispatcher success
path, merged as a clobber-free union of distinct keys
(`extras={**activity, **checks, **reflex}`):

1. `target_activity_advisory` — this fragment (#2550), write-class only.
2. `checks_alert_advisory` — #2718, built in `checks/advisory.py`,
   deduped per caller via Valkey `SET NX EX`, applied to **all** op
   classes; see [`docs/codebase/checks-advisory.md`](checks-advisory.md).
3. `reflex_advisory` — #3133, built in `broadcast/reflex.py`, a
   session-scoped coordination nudge (read-before-act /
   announce-before-mutate) deduped per `(session, heuristic)`; see
   [`docs/codebase/reflex-advisory.md`](reflex-advisory.md). #3133 also
   ships the opt-in **announce gate** (`broadcast/announce_gate.py`),
   which *rejects* — rather than nudges — a caution-or-higher write-class
   op dispatched without an active claim in a tenant that opted in.

## Who can publish (#3736)

Only a trusted agent can publish an announcement. People keep read
access (`meho_broadcast_recent`, `watch`, the UI) under their normal
tenant role. `auth/agent_admission.py::resolve_agent_admission` decides
from server-side facts only. There are two ways in:

1. **A MEHO-hosted agent run.** The call runs inside a live hosted run.
   The run id comes from `current_agent_run_id_var`, which only the agent
   runtime sets. The `agent_run` row must be in the caller's tenant and
   not finished (`pending`, `running` or `awaiting_approval`). The acting
   agent comes from the delegation binding (`resolve_actor_sub`), and the
   work reference comes from the run row.
2. **A registered agent principal.** The verified token says
   `principal_kind=agent`, and its client id names a registered,
   not-revoked `agent_principal` row in the caller's tenant.

Everything else gets the stable machine error `agent_identity_required`
(MCP `-32602` with `error.data = {code, reason, remediation}`, REST
`403` with the same `detail`). MEHO never guesses an agent from the
transport, a person's login client, a CLI login, a flag, or a `run_id`
the caller sent.

**How to get a trusted identity.** Pick one:

- Run the work as a MEHO-hosted agent run (`meho agent run …`). The
  run's announcements are stamped automatically.
- Register an agent principal: `meho agent-principal register <name>`
  (a tenant admin does this). Then the agent calls MEHO with that
  principal's client-credentials token.

**What the server stamps.** The caller cannot set these fields:
`principal_sub` (token `sub`), `owner_sub` (the acting agent of a hosted
run, or the agent principal's `agent:<name>` client id), `actor_sub`,
`run_id`, `admission` (`hosted_run` / `agent_principal`), and, inside a
hosted run, `work_ref`. A `run_id` the caller sends is refused unless it
equals the checked run (`server_stamped_field`); inside a hosted run a
different `work_ref` is refused the same way. A registered agent
principal may still declare its own `work_ref`.

**Claims.** Each new announcement is one step of a claim:

| Step | Needs | What happens |
|---|---|---|
| `start` (default) | no `claim_id`; `idempotency_key` optional | The server mints a new `claim_id`. |
| `update` | `claim_id` + `idempotency_key` | Continues an open claim. |
| `completion` | `claim_id` + `idempotency_key` | Closes the claim. No more steps after this. |

- A missing `ttl_minutes` becomes 30 (`TTL_DEFAULT_MINUTES`), inside the
  existing 1..1440 bound.
- Only the claim's owner can update or complete it. For a hosted run the
  claim also stays with that run: another run of the same agent gets
  `claim_not_owned`.
- The `idempotency_key` is scoped to (tenant, owner). Sending the same
  step again returns the first result with `replayed=true`; nothing new
  is stored. The same key for a different step gets
  `idempotency_key_reused`.
- A retry also finishes a delivery that failed half-way. The row is
  written first, then the stream. If the stream write fails, the caller
  gets a success with `delivery_pending=true` (#3748) and the row keeps
  `stream_entry_id = NULL`. The retry with the same key finds that row,
  writes the stream entry with the stored ids, marks the row, and
  returns `replayed=true` with `delivery_pending=false` and the same
  `event_id` and `watch_checkpoint`. A `start` sent without a key cannot
  be retried this way; its row stays in the archive (readers see it) and
  only the stream wake-up is missing.
- Overlapping requests never deliver a step twice. The stored row is
  locked (`SELECT ... FOR UPDATE`) from the delivery check through the
  stream write and the delivery mark, so a retry that overlaps the first
  try waits, then sees the entry id and writes nothing. For `update` /
  `completion` the retry key is looked up after the claim's start row is
  locked, so a retry that waits behind the first copy of its step
  replays it instead of getting `claim_completed`.
- A retry after a lost delivery mark never writes a second entry either.
  The stream write is one Lua script: it checks a per-event marker key
  (`meho:announce:delivered:{tenant}:{event_id}`), runs the `XADD`, and
  stores the new entry id in the marker, all in one step. If the first
  try wrote the entry but the row mark failed, or the reply to the write
  never came back, the row still says "not delivered"; the retry's script
  then finds the marker, returns the known entry id without writing, and
  the row is marked with it. The retry gets `replayed=true` and writes
  nothing. The marker lives as long as the stream read window
  (`broadcast_retention_hours`).
- "Same step" means every field matches: `run_id`, `phase`, `activity`,
  `target`, `targets`, `scope`, `planned_op_class`, `ttl_minutes`,
  `work_ref`, and `claim_id` for `update` / `completion`. So a second
  hosted run of the same agent that reuses a key gets
  `idempotency_key_reused`, not the first run's claim.
- Error codes (MCP `error.data.code`, REST `422` `detail.code`):
  `claim_id_not_allowed`, `claim_id_required`, `idempotency_key_required`,
  `server_stamped_field`, `claim_not_found`, `claim_not_owned`,
  `claim_completed`, `idempotency_key_reused`.
- The checks and the INSERT share one transaction. For `update` and
  `completion` the claim's `start` row is locked (`SELECT ... FOR
  UPDATE`) until commit, so two steps on one claim run one after the
  other: an `update` that waits behind a `completion` sees the completion
  row and gets `claim_completed`. Three partial unique indexes
  (migration `0112`) back this up and also catch two requests that race
  on the same retry key: one retry key per owner, one `start` per claim,
  one `completion` per claim. The lock is a PostgreSQL feature; SQLite
  (dev/test) drops it and relies on the indexes. The PostgreSQL test is
  `tests/integration/test_broadcast_claim_steps_pg.py`.

A claim is **advisory**. It tells others what an agent intends. It is
never a lock, never blocks another claim, and never blocks a write.

**Legacy rows.** Rows written before #3736 have no `claim_id` /
`owner_sub` (`AgentAnnouncementEvent.is_trusted_claim` is `False`). They
stay readable as history, but no step can continue them: an `update` or
`completion` naming such a row gets `claim_not_found`.

**Not changed yet.** The read side still shows legacy entries the same
way, `active_only` still treats a TTL-less legacy entry as active, and
the opt-in announce gate still asks the caller for a claim. Retiring the
human announce gate and making reads use only trusted claims is #3762.

## Durable announcements (#2547)

The Valkey stream is the hot real-time path but not durable: it is
count-trimmed at `BROADCAST_MAXLEN = 10_000` and the broadcast subchart
runs with persistence disabled (`save ""` / `appendonly no` — "streams
are ephemeral by design"), so a restart wipes the whole coordination
window. Operations persist forever in `audit_log`; coordination *intent*
had no durable home until T2.

**Archive table.** `publish_agent_announcement` writes an append-only
`agent_announcement` row (migration `0067`, model
`db/models.py::AgentAnnouncement`) keyed on the event's minted UUID
(`event_id`), persisting the typed claim fields (T1 #2544) and lineage.
The row is written **before** the `XADD` so a stream-side failure never
costs the durable record; the archive leg is fail-loud, the stream leg
fail-soft (`delivery_pending`, #3748). The `XADD` runs with
the row locked (`SELECT ... FOR UPDATE`), so only one request delivers a
stored step, and inside one Lua script with a per-event marker key, so a
retry after a lost delivery mark or a lost reply finds the entry instead
of writing it again. After a successful `XADD` one UPDATE in the same
transaction sets `stream_entry_id` (the delivery mark, fail-soft: a
failure there only logs a warning, because the announcement is already
delivered; the next retry marks the row).
Columns: `id` (UUID
PK), `tenant_id` (FK), `principal_sub`, `activity`, `target`, `scope`,
`targets` (JSONB), `phase`, `planned_op_class`, `ttl_minutes`,
`work_ref`, `run_id`, `created_at` (the event's `ts`), plus the trusted
claim columns from migration `0112` (#3736): `claim_id`, `owner_sub`,
`actor_sub`, `admission`, `idempotency_key`, `stream_entry_id`, plus
`seq` from migration `0114` (#3748). Indexes:
`(tenant_id, created_at DESC)` (recent backfill + prune) and
`(tenant_id, work_ref)` (filter). This is the split Kubernetes draws
between its bounded Events TTL and the durable records it expects
elsewhere.

**Stream backfill (removed in #3758).** Every reader now reads the archive
directly, so no reader backfills a stream window from the archive.

**Retention prune.** `broadcast/announcement_retention.py` is a
lifespan-owned weekly `asyncio` loop (a deliberate copy of the
topology-history prune mold, `topology/history_retention.py`) that
deletes rows older than `broadcast_announcement_retention_days` (default
90) in one bounded audited batch per tick. `days=0` is the keep-forever
opt-out (a no-op heartbeat, no DELETE, no audit row);
`broadcast_announcement_prune_enabled=false` skips starting the loop
entirely. Each non-no-op tick writes one `INTERNAL` audit row
(`operator_sub='system:broadcast-announcement-retention'`,
`path='broadcast.announcement.prune'`, `payload={dropped_rows,
retention_days, cutoff}`) attributed to the system-wide sentinel tenant.
Helm knobs: `broadcastAnnouncement.{retentionDays,pruneIntervalSeconds,pruneEnabled}`
(env `BROADCAST_ANNOUNCEMENT_*`). This is distinct from
`broadcast_retention_hours` (the *stream* read-window heuristic, a
different substrate).

**When it runs.** The interval (`BROADCAST_ANNOUNCEMENT_PRUNE_INTERVAL_SECONDS`, default 7 days) counts
from the **last recorded run**, not from pod start. So a pod restart (an
update, a crash, a node drain) does not start the interval again:

- After a pod starts, the prune waits one minute, so it does not run in
  the middle of startup.
- Then it reads the time of its newest `broadcast.announcement.prune` audit row within the
  last interval. A row counts only if it has the system tenant id,
  `method='INTERNAL'`, `operator_sub='system:broadcast-announcement-retention'` and that path. Every run
  writes this row, also when it deleted 0 rows.
- If there is no such row, it runs at once. Otherwise it waits only for
  the rest of the interval. After that it runs once per interval.
- A last-run time in the future (a pod with a fast clock wrote it) counts
  as "due now". If the read fails, it logs `broadcast_announcement_retention_last_run_read_failed`
  and runs at once. The wait is never longer than one interval.
- Each start logs `broadcast_announcement_retention_scheduled` with `last_run_at` and
  `next_run_in_seconds`, so you can check the schedule after a restart.
- With retention days set to `0` no row is written, so the no-op
  heartbeat runs about one minute after each start.

The shared steps live in `backend/src/meho_backplane/loop_schedule.py`.

## Archive order and cursors (#3748)

The archive is the record; the stream is only a wake-up for watchers.
`meho_broadcast_recent`, `meho_broadcast_watch` and
`GET /api/v1/broadcast/recent` read announcements from
`agent_announcement` by a per-tenant sequence number, `seq`
(`broadcast/archive_reader.py`).

**Commit-order sequence.** `broadcast/archive_sequence.py`. Each tenant
has one `agent_announcement_sequence` row (`tenant_id`, `last_seq`,
migration `0114`). The publisher runs `UPDATE ... SET last_seq =
last_seq + 1 RETURNING last_seq` in the same transaction as the
announcement INSERT, right before it. The UPDATE locks the counter row
until commit or rollback, so:

- a second writer in the same tenant waits and gets the next number
  only after the first one is done: numbers follow commit order;
- a rollback (including a lost retry-key race) gives the number back:
  committed numbers have no holes;
- a reader that has seen number N has seen every smaller number,
  because those committed earlier. "The highest number I have seen" is
  a safe resume point.

The lock only orders writes. It is held for the INSERT and the commit,
never across a Valkey call, and it is not a lease, a claim, or a way to
stop competing work. A unique index `(tenant_id, seq)` backs it up. The
first announcement of a tenant creates the counter row (`INSERT ... ON
CONFLICT DO NOTHING`). Existing rows were numbered 1..n per tenant by
the migration, oldest first. `seq` is nullable only so an older pod
can still write during a rolling upgrade. Before every scan, the reader
gives such rows the next numbers, oldest first, under the same counter
lock (`number_unnumbered_rows`), so they show up as new announcements
instead of staying invisible. The lock order is the same everywhere:
announcement rows first, the counter second. A claim step locks the
claim's start row and then takes the counter; the numbering reader
locks the rows it will number (`SELECT ... FOR UPDATE`, oldest first)
and then takes the counter. Nobody holds the counter while waiting for
a row, so a reader and a claim step on the same start row take turns
instead of waiting for each other. The PostgreSQL lock tests are in
`tests/integration/test_broadcast_archive_cursor_pg.py`.

**Two cursor kinds.** `broadcast/archive_cursor.py`. Both are opaque,
versioned by prefix, and bound to the tenant (a cursor from another
tenant is refused with `-32602`):

| Cursor | Prefix | Holds | Used by |
|---|---|---|---|
| History page cursor | `h1.` | the fixed upper end of one read, the last scanned `seq`, the window start | recent `next_cursor` → recent `cursor` |
| Watch checkpoint | `w1.` | "seen everything up to `seq`" | recent `watch_checkpoint`, announce ack `watch_checkpoint`, watch `cursor` / `next_cursor` |

An old Valkey stream id (`<ms>-<seq>`) as `cursor` is refused with a
hint.

**recent.** Takes the tenant's current `last_seq` first (the read's
upper end), then pages rows `after < seq <= upper` in `seq` order,
inside the time window (default 30 minutes; an ISO-8601 `cursor` starts
it earlier). It scans at most `limit` rows, then filters. It returns
`{events, next_cursor, watch_checkpoint, gap}`: `next_cursor` continues
the same fixed view (`null` at the end), and `watch_checkpoint` is the
upper end, so a watcher that starts there misses nothing after the
read. The checkpoint is set even when the archive is empty or the
filter hides every row.

**watch.** Scans rows after the checkpoint (up to `WATCH_SCAN_LIMIT` =
100). It returns at once when a row matches, the scan page is full, or
there is a gap. Otherwise it waits and scans again after each wake-up
and at the timeout. Before each scan it notes the stream's newest id
(`XREVRANGE + - COUNT 1`), then waits with `XREAD BLOCK` from that id, so
an announcement that commits during the scan still wakes it. Operation
events on the same stream cause harmless extra scans. If Valkey fails,
the call scans every `WATCH_POLL_SECONDS` (5 s) instead, until the
timeout. `next_cursor` always moves past every scanned row, also when
the filter hides them; event ids are de-duplicated within a call, and a
reconnect with the returned checkpoint never repeats an event.

**Gaps.** The retention prune deletes rows by `created_at` but never
the counter, and numbers have no holes. So every scan knows how many
rows it should have found between its start and its end (the last row
of a full page, or the upper end), and the difference to the rows it did
find is the number of announcements that are gone. The count comes from
the scanned rows themselves, not from a separate "oldest kept row" query.
That way a prune that lands between the high-water mark read and the row
scan is still counted, and so is a row pruned from the middle of the
range: a row's `created_at` is set before it takes its number, so the
time-based prune can remove number 2 and keep 1 and 3. The read says so
instead of claiming to be complete:

- `{"reason": "retention_trimmed", "missed_events": N, ...}` — a watch
  checkpoint, a history page cursor, or a fresh window whose scanned
  range is missing N rows.
- `{"reason": "retention_trimmed", "missed_events": null, ...}` — a
  fresh recent window, or an SSE feed `since=<time>`, that starts before
  `broadcast_announcement_retention_days` while rows were pruned. The
  feed sends this frame before the first replayed announcement.
- `{"reason": "checkpoint_ahead", ...}` — a watch checkpoint above the
  tenant's current `last_seq` (for example after a database restore);
  watching restarts at the current end.

`gap` is `null` otherwise.

**Not changed yet.** The SSE feed (`/api/v1/feed`, `meho broadcast
watch`), the UI pane, and the `meho://tenant/{id}/feed` resource still
read the stream, and audit-driven operation events are still published
there. Moving those consumers is #3758.

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
    `meho_broadcast_watch` MCP tool, and the agent approval-wait
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

The fixed issues below describe the stream-based SSE reader that #3758
replaced with the archive reader; they stay as history.

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
  the UI SSE bridge, the `meho_broadcast_watch` MCP tool, and the
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
   The MCP `meho_broadcast_recent` tool (XRANGE-based) surfaced
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
  wrapping `activity`/`scope`/`target`/`work_ref` on the `recent`/`watch`/
  `tenant_feed` LLM-facing paths; the human UI history + SSE surfaces
  serve unwrapped and escape at the sink) —
  [`untrusted-text-envelope.md`](./untrusted-text-envelope.md).
