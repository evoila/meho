# Broadcast v2 — ticket drafts (approval bundle, 2026-07-16)

**FILED 2026-07-16** — Goal #2542 · Initiative #2543 · T1 #2544 · T3 #2545 · T6 #2546 · T2 #2547 · T4 #2548 · T5 #2549 · T7 #2550. Coordination comments posted on #2479 and #2537. This file is the pre-filing draft record; the issues on `evoila/meho` are canonical.

Filing target: `evoila/meho` (labels + Parent-lines hierarchy, no board).
Bundle: **1 new Goal + 1 new Initiative + 7 Tasks.**

Build order (DAG):

- **Wave 1:** T1 (keystone; Depends-on external #2479), T3, T6
- **Wave 2:** T2 (**the only migration**, Depends-on T1), T4 (Depends-on T1), T5 (Depends-on T1)
- **Wave 3:** T7 (Depends-on T1 + T3; serialize with topology #2537 on dispatcher.py)

Migration discipline: exactly one migration-carrying task (T2); number assigned at implementation time (head was 0062 at drafting — never pin).

External sequencing: #2479 and #2480 (open wire-shape bugs under #2494) touch the same row builders / announce handler — T1 carries `Depends-on: #2479`; #2480 is Related on T1.

---
---

## GOAL (new — needs approval to create)

**Title:** `Goal: Broadcast as the coordination channel — every agent's activity and intent legible to agents and humans, durable, and crossfire-avoidable`

**Labels:** `goal, enhancement, priority:high, effort:large, infrastructure`

**Body:**

## Goal

**Every principal connected to the backplane — human, MCP agent, or MEHO-hosted agent — can declare what it is doing and why, see what everyone else is doing and why, and the record survives long enough to coordinate across shifts. Crossfire ("I didn't know you were on that") is avoidable from data, not luck.**

Goal #217 stood up the activity feed and closed completed, but its own done-when — "at least two operators have used the feed to coordinate or avoid a collision in a real situation" — was never verified, and the 2026-07-16 deep review of the shipped feature (`broadcast-feature-review.md`, repo root at time of filing; 43-agent, adversarially verified) found the coordination thesis was recorded verbatim in #217/#228, partially built (`meho.broadcast.announce` with start/update/completion phases), then eroded:

1. **The intent stream is write-only for humans** — the SSE feed validates entries as `BroadcastEvent` and skips announcements (`api/v1/feed.py:584-592`); the UI history pane filters them out (`ui/routes/broadcast/history.py:132-152`); the Slack mirror was wontfixed (#333).
2. **The WHY is the least durable data in the system** — announcements have no DB row anywhere; the only copy lives on a count-trimmed Valkey stream (`BROADCAST_MAXLEN=10000`, `publisher.py:78`) whose chart runs `appendonly no` (`deploy/charts/meho/charts/broadcast/templates/configmap.yaml:30-31`) — a restart wipes the tenant's coordination window while every operation persists forever in audit_log.
3. **WHY and WHAT cannot be joined, and agents are misattributed** — announcements carry no run/ticket/audit linkage; `BroadcastEvent` projects none of audit_log's lineage columns (`actor_sub`/`agent_session_id`/`work_ref`, `db/models.py:475,488,546`), so delegated agent work broadcasts under the human's `principal_sub`.
4. **MEHO's own hosted agents are excluded** — the runtime meta-tool catalog is exactly three dispatch tools (`agent/toolset.py:162-255`); hosted agents can neither announce nor read peers.
5. **Nothing drives adoption or protects the channel** — the four-step broadcast discipline lives only in a stale consumer template that still claims the tools "are not yet wired" (`docs/examples/consumer-onboarding/CLAUDE.md:182-187`), the server-assembled preamble carries zero broadcast content, and announce has no rate limit (one looping agent can evict the whole tenant window).

The security posture that shaped v1 is respected, not reversed: agent free prose stays quarantined (untrusted envelope, meho-internal#154); credential/audit classes stay aggregate-only (decision #3). The v2 insight is that **both defenses constrain prose and payloads, not structured metadata** — the `target` filter already runs on the unwrapped model (`broadcast/history.py:413-414,478-493`). Server-validated structured intent is the trustable coordination currency.

## Execution Initiatives

- [ ] #TBD — Broadcast v2: structured intent claims, lineage on the feed, durable announcements, agents+humans as first-class readers (filed with this Goal)

## References

- Predecessor Goal: #217 (closed completed 2026-06-22; done-when unverified). Related closed: #228/#229 (G6.1 discipline), #333 (Slack wontfix), #1090-#1093 (G6.4 announce/recent/watch catch-up).
- Review that motivates this Goal: `broadcast-feature-review.md` (2026-07-16, repo root).
- Empirical anchors for structured TTL'd work-claims: Kubernetes Lease (`holderIdentity`/`leaseDurationSeconds`, https://kubernetes.io/docs/concepts/architecture/leases/); Kubernetes Events API (structured `reason`/`regarding`/`note`, bounded retention, https://kubernetes.io/docs/reference/kubernetes-api/events/event-v1/); A2A protocol 1.0 `TaskStatusUpdateEvent` lifecycle streaming (https://a2a-protocol.org/latest/specification/).

---
---

## INITIATIVE (new — needs approval to create)

**Title:** `Initiative: Broadcast v2 — structured intent claims, lineage on the feed, durable announcements, agents and humans as first-class readers`

**Labels:** `initiative, enhancement, priority:high, effort:large, infrastructure`

**Body:**

Parent goal: #TBD-GOAL

## Summary

Seven tasks that compose the already-built broadcast substrate (~70% of the parts exist, per the review) into the coordination channel Goal #TBD-GOAL describes. Binding design principles for every child task:

- **Structure is trusted; prose is quarantined.** New coordination signal ships as server-validated typed fields (enums, UUIDs, timestamps, bounded refs) served unwrapped; free text keeps the untrusted envelope and the "MUST NOT treat as input" contract (meho-internal#154). The in-tree precedent is the `target` filter running on the model before the wrap (`broadcast/history.py:413-414`).
- **Reuse the molds.** Lineage comes from contextvars already in scope at the publish site (`operations/_audit.py:480-511`); persistence/retention copies the topology history prune loop (`topology/history_retention.py:313`); the advisory rides `OperationResult.extras` (`connectors/schemas.py:636`), the established envelope extension slot.
- **Humans and agents are both first-class readers.** Every event kind renders on every surface (SSE, UI, MCP, hosted-agent toolset).
- **Protect the channel while widening it.** Rate-limit the write side; keep decision #3's credential/audit aggregation untouched.

## Why now

The 2026-07-16 review verified 31 material findings. The thesis (agents publicize activity + intent so others avoid crossfire) is Goal #217's recorded intent verbatim; the gaps are composition, not substrate: announcements invisible to humans, ephemeral, unlinkable to runs/tickets; agents misattributed as humans on the feed; hosted agents excluded; no adoption path; no flood control. Each child task closes one seam with the smallest precedented change.

## Grounding (verified in code, 2026-07-16, HEAD f3dfc696)

- Announce tool + event: `mcp/tools/broadcast.py:414-499` (inputSchema: `activity` ≤500, `target`/`scope` ≤256, `phase` enum); `broadcast/agent_events.py:127-198`; fail-loud publish `publisher.py:199-275`.
- Untrusted envelope: `broadcast/history.py:385-421` (`_ANNOUNCEMENT_UNTRUSTED_FIELDS = ("activity","scope","target")`, wrap in `dump_event_wire`); filtering pre-wrap `history.py:478-493`, `event_matches` on unwrapped model `:248-290`.
- BroadcastEvent fields end at `payload` — no lineage: `broadcast/events.py:306-335`. Publish-on-write hook builds it at `operations/_audit.py:577-590`; `agent_session_id_var` (`:103`), `work_ref_var` (`:165`), `resolve_actor_sub` (`:36`) are all read by the sibling audit-row writer in the same module (`:480,:499,:511`).
- Audit lineage columns + indexes: `db/models.py:475,488,546` / `:609-628`.
- SSE drops announcements: `api/v1/feed.py:584-592` (`ValidationError` → skip); UI filters: `ui/routes/broadcast/history.py:132-152,202` ("follow-up" docstring `:53-54`); `/ui/broadcast/stream` reuses `_process_entries` (`stream.py:75-84,212`).
- Hosted-agent toolset: `_META_TOOL_CATALOG` exactly 3 specs (`agent/toolset.py:162-255`); unknown names warn-and-ignore (`:454-459`); `MetaToolSpec` shape `:108-132`.
- Ephemerality: `BROADCAST_MAXLEN=10000` (`publisher.py:78`); chart `save ""` / `appendonly no` (`deploy/charts/meho/charts/broadcast/templates/configmap.yaml:30-31`, "streams are ephemeral by design"); `broadcast_retention_hours` is a read-window knob only (`settings.py:1017`).
- Prune mold: `topology/history_retention.py:255-345`; knobs `settings.py:1118-1119`.
- Preamble assembler has zero broadcast content: `conventions/preamble.py:252` (grep broadcast = 0 hits). Stale onboarding caveat: `docs/examples/consumer-onboarding/CLAUDE.md:182-187` (underscore names; "not yet wired" — false since #1092).
- Advisory seam: OK envelope built via `wrap_ok_result` (`operations/dispatcher.py:756`; builder `operations/_errors.py:1478`); `OperationResult.extras` (`connectors/schemas.py:616-636`); `target.name`/target id in scope (`operations/_audit.py:474-475,575`).
- Client lib: redis-py **8.0.1** (`backend/uv.lock:3229-3230`).

**Grounding corrections the child tasks must respect:**
- `event_id` on announce returns is today the Valkey cursor mislabeled (#2479); announcements have no UUID anywhere until T2 mints one.
- The advisory attaches to the dispatch *response* (post-op awareness); pre-op checking remains the discipline's read step (`meho.broadcast.recent`) — no blocking, no locks.

## Child tasks

Build order (DAG; Depends-on lines on each task):

- [ ] #TBD — T1 Structured intent claims on announce — typed targets/planned_op_class/TTL/work_ref/run_id, served trusted (keystone) *(Depends-on #2479)*
- [ ] #TBD — T3 Lineage projection onto BroadcastEvent — actor_sub, agent_session_id, work_ref from the publish-site contextvars — no deps
- [ ] #TBD — T6 Announce rate limit + broadcast discipline in the tenant preamble + fix the stale onboarding template — no deps
- [ ] #TBD — T2 Durable announcements — `agent_announcement` table, retention prune, recent/watch backfill *(Depends-on T1)* — **1st migration**
- [ ] #TBD — T4 Hosted-agent bridge — announce/recent/watch as runtime meta-tools *(Depends-on T1)*
- [ ] #TBD — T5 Humans see announcements — SSE union validation + UI rendering *(Depends-on T1)*
- [ ] #TBD — T7 Dispatch-time target-activity advisory on write-op responses *(Depends-on T1, T3)*

Migration discipline: only T2 carries a migration. T7 serializes with topology #2537 (shared `operations/dispatcher.py`).

## Definition of done

- [ ] A hosted MEHO agent announces "start: rotating tokens on cluster X" with a typed target claim and TTL; a second agent's `meho.broadcast.recent --target cluster-X` surfaces it (structured fields unwrapped); a human sees the same announcement on `/ui/broadcast` and the SSE feed.
- [ ] The announcement (and who made it — agent, not the delegating human) is still queryable after a Valkey restart and after 30 days.
- [ ] A write-op dispatch response on a target with recent peer activity carries the advisory in `extras`; delegated agent operations on the feed are attributable to the agent (`actor_sub`) and groupable by run (`agent_session_id`).
- [ ] A looping announce caller is rate-limited before it can trim the tenant's coordination window.
- [ ] Decision #3 aggregation and the untrusted-prose envelope are byte-identical for existing event shapes (existing tests green unmodified).

## Out of scope

- Any weakening of credential/audit aggregate-only classes (decision #3) or of the untrusted-prose envelope.
- Blocking/locking on claims (a claim is awareness, not mutual exclusion); a Lease-style hard claim primitive is a later decision.
- Slack or other external mirrors (#333 stands).
- Threads/replies on announcements (#1092 out-of-scope stands).
- Read access below OPERATOR role.

## Dependencies

External sequencing: #2479 (announce-return/cursor fix) lands before T1; #2480 (error-text cosmetics) Related. Sibling initiative #2533 (Topology v2): #2537 shares the dispatcher seam with T7 — serialize merges; no semantic overlap.

## References

- `broadcast-feature-review.md` (repo root, 2026-07-16) — the verified review this Initiative executes.
- Prior art being composed: #1090-#1093 (announce/recent/watch), #316/#318 (preamble + onboarding docs), #1305 (SSE fanout fix), #2086 (lineage-gap precedent), #2338 (`{items,next_cursor}` list convention).
- Comparables: Kubernetes Lease (https://kubernetes.io/docs/concepts/architecture/leases/), Kubernetes Events v1 (https://kubernetes.io/docs/reference/kubernetes-api/events/event-v1/; `--event-ttl` default 1h), A2A 1.0 TaskStatusUpdateEvent (https://a2a-protocol.org/latest/specification/), Valkey XADD/XREAD (https://valkey.io/commands/xadd/, https://valkey.io/commands/xread/), Redis rate-limiter pattern (https://redis.io/docs/latest/commands/incr/).

---
---

## T1 (keystone)

**Title:** `Task: Structured intent claims on meho.broadcast.announce — typed targets, planned_op_class, TTL, work_ref, run_id; structured fields trusted, prose stays enveloped`

**Labels:** `task, enhancement, priority:high, effort:medium, infrastructure`

**Body:**

Parent goal: #TBD-GOAL
Parent initiative: #TBD-INIT
Depends-on: #2479

## Summary

Keystone of Initiative #TBD-INIT: turn announcements from quarantined prose into coordination data. The cross-agent prompt-injection defense (meho-internal#154) rightly prevents agents from absorbing peer *prose* — so the coordination signal must ride **server-validated structured fields**, which are served unwrapped because they cannot carry instructions. The in-tree proof this split works: `target` equality filtering already runs on the unwrapped model before `dump_event_wire` wraps it (`broadcast/history.py:413-414,478-493`).

## Current state (verified in code, 2026-07-16, HEAD f3dfc696)

- `AgentAnnouncementEvent` fields: `activity` (≤500, `ACTIVITY_MAX_CHARS` `agent_events.py:124`), `target`/`scope` (free text ≤256), `phase` start/update/completion, `ts`, `tenant_id`, `principal_sub` (`agent_events.py:127-198`). No targets list, no op-class declaration, no TTL, no `work_ref`, no `run_id`.
- Announce inputSchema: `mcp/tools/broadcast.py:442-494` (`required: ["activity"]`, `additionalProperties: False`); registration `:414-499` (OPERATOR, `op_class="write"`).
- Untrusted wrap on display: `_ANNOUNCEMENT_UNTRUSTED_FIELDS = ("activity","scope","target")` (`history.py:385`), applied in `dump_event_wire` (`:417-421`); `event_matches` filters on the unwrapped model (`:248-290`).
- Announce return mislabels the stream cursor as `event_id` (#2479, open — this task lands after it).
- Linkage conventions to reuse: `AgentRun.work_ref` (`db/models.py:3363-3374`; #2507 uses `checks:<dashboard>:<group>`); `agent_session_id` contextvar.
- Comparables for TTL'd structured claims: Kubernetes Lease `holderIdentity`/`leaseDurationSeconds` (https://kubernetes.io/docs/concepts/architecture/leases/); Kubernetes Events v1 structured `reason`/`regarding`/`note` (https://kubernetes.io/docs/reference/kubernetes-api/events/event-v1/); A2A 1.0 `TaskStatusUpdateEvent` (https://a2a-protocol.org/latest/specification/).

## Desired state

- `AgentAnnouncementEvent` gains optional typed fields: `targets: list[str]` (each 1-256 chars, max 10 — supersedes-not-replaces the single `target`), `planned_op_class: Literal[...]` (the existing `classify_op` taxonomy values, `events.py:341`), `ttl_minutes: int` (1-1440; consumers derive `expires_at = ts + ttl`), `work_ref: str | None` (≤256, same conventions as `AgentRun.work_ref`), `run_id: UUID | None`.
- Announce inputSchema extended accordingly (pydantic 2.13.4 validation on the event; JSON-schema mirrors the bounds); `additionalProperties: False` retained.
- Trust rule implemented and documented in `agent_events.py`: enum/UUID/int/timestamp fields (`planned_op_class`, `ttl_minutes`, `run_id`, `phase`, `ts`) are serialized **unwrapped**; string fields (`activity`, `scope`, `target`, `targets[]`, `work_ref`) stay in `_ANNOUNCEMENT_UNTRUSTED_FIELDS`-style wrapping on display, with all filtering (`event_matches`) running pre-wrap on the model. `recent`/`watch` gain `work_ref` and `active_only` (claims whose TTL has not elapsed) filters.
- Announce return shape carries the #2479-corrected `cursor` plus the declared claim fields echoed back.

## Acceptance criteria

- [ ] Announce with `{targets: ["cluster-x"], planned_op_class: "write", ttl_minutes: 30, work_ref: "gh:evoila/meho#123"}` round-trips: `meho.broadcast.recent {target: "cluster-x"}` and `{work_ref: ...}` and `{active_only: true}` each surface it; after TTL elapses `active_only: true` excludes it (unit + integration test).
- [ ] Wire dump: `planned_op_class`/`ttl_minutes`/`run_id`/`phase` appear unwrapped; `activity`/`scope`/`targets[]`/`work_ref` are enveloped; `event_matches` still matches on unwrapped values (test asserts both).
- [ ] Invalid claims rejected at the boundary with typed -32602 (11 targets; ttl 0; 257-char work_ref).
- [ ] Existing single-`target` announcements and pre-v2 stream entries still parse and render (back-compat test on a mixed stream).
- [ ] `pytest backend/tests -k broadcast` green; ruff + mypy clean; `docs/architecture` broadcast section (or `docs/codebase` equivalent) documents the trust rule.

## Out of scope

- Persistence (T2). Rate limiting (T6). Hosted-agent access (T4). Human rendering (T5). Hard claim/lease semantics (awareness only — no locking).

## References

- Parent: #TBD-INIT. Mould: announce registration `mcp/tools/broadcast.py:414-499`; wrap/filter split `broadcast/history.py:385-421,478-493`.
- Depends-on #2479 (same handler: announce return shape). Related: #2480.
- Pydantic 2.13.4 field constraints: https://docs.pydantic.dev/latest/api/types/. Comparables: K8s Lease, K8s Events v1, A2A 1.0 (URLs above).

---
---

## T2 (1st migration)

**Title:** `Task: Durable announcements — agent_announcement table (append-only), retention prune loop, recent/watch DB backfill, real event UUIDs`

**Labels:** `task, enhancement, priority:high, effort:medium, infrastructure`

**Body:**

Parent goal: #TBD-GOAL
Parent initiative: #TBD-INIT
Depends-on: #TBD-T1

## Summary

Give the WHY a durable home. Announcement content today exists only on the per-tenant Valkey stream — count-trimmed at `BROADCAST_MAXLEN=10000` and wiped on restart because the chart deliberately disables persistence. Operations persist forever in audit_log; intent evaporates within ~a day. Cross-shift coordination ("does this conflict with what agent A said yesterday?") requires a DB row.

## Current state (verified in code, 2026-07-16)

- No DB table for announcements; the audit row for the announce call stores a params hash, not the text. Stream trim: `publisher.py:78`; chart `save ""` / `appendonly no` with "streams are ephemeral by design" comment (`deploy/charts/meho/charts/broadcast/templates/configmap.yaml:19-31`).
- Announcements have no UUID anywhere (#2479 documents this); `event_id` on returns is the mislabeled stream cursor.
- Retention mold to copy: `topology/history_retention.py:255-345` (lifespan `asyncio` prune loop, per-tick try/except, `0` = keep-forever sentinel, audit row per non-no-op tick); knobs mold `settings.py:1118-1119`. `broadcast_retention_hours` (`settings.py:1017`) remains the stream read-window knob — distinct concern.
- List-shape convention for any new query surface: `{items, next_cursor}` (#2338).
- Comparable: Kubernetes keeps Events on a bounded TTL (`--event-ttl`, default 1h) and expects durable records to live elsewhere — MEHO's split (hot stream + DB table) mirrors that division (https://kubernetes.io/docs/reference/kubernetes-api/events/event-v1/).

## Desired state

- One migration (next free head; append-only mold from `0012_create_topology_history.py`): `agent_announcement` — `id` UUID PK (minted at publish — becomes the real `event_id`), `tenant_id` FK NOT NULL, `principal_sub`, `activity`, `scope`, `targets` JSONB, `phase`, `planned_op_class`, `ttl_minutes`, `work_ref`, `run_id`, `created_at`; indexes `(tenant_id, created_at DESC)` and `(tenant_id, work_ref)`.
- `publish_agent_announcement` writes the DB row and the stream entry; both fail-loud (the existing contract — the agent must know if the team didn't hear it). The stream entry carries the row's UUID as `event_id`; the return shape's `cursor` (#2479) and `event_id` are now genuinely distinct values.
- `meho.broadcast.recent` backfills from the table when the requested window predates the stream's oldest entry (stream = hot path; DB = archive), same `{items, next_cursor}` shape.
- Retention: `broadcast_announcement_retention_days` (default 90, `0` = keep forever) + prune loop mirroring the topology mold, one audit row per non-no-op tick.

## Acceptance criteria

- [ ] Integration test: announce → `FLUSHALL` on the Valkey container (or client-level stream delete) → `meho.broadcast.recent` with a wide window still returns the announcement from the DB, wrapped per the T1 trust rule.
- [ ] `event_id` is a stable UUID equal to the DB row id and distinct from `cursor` (asserts the #2479 contract).
- [ ] Prune test: rows older than the retention cutoff deleted, audit row written; `retention_days=0` heartbeats without deleting (mold parity with `test_topology_history_retention.py`).
- [ ] Migration stamp-replay idempotency pinned to this migration's own revision; `tests/integration/` doubles gain the new model attr where fabricated.
- [ ] ruff + mypy clean; Helm values document the new knobs.

## Out of scope

- Persisting operation `BroadcastEvent`s (audit_log already is their durable record). Changing stream MAXLEN or chart persistence. UI history-pane pagination changes (T5 renders; deep-history UX later).

## References

- Parent: #TBD-INIT. Moulds: `topology/history_retention.py:255-345`, migration `0012`, settings `1118-1119`.
- Alembic 1.18.5 ops: https://alembic.sqlalchemy.org/en/latest/ops.html. K8s Events TTL: kube-apiserver `--event-ttl` (https://kubernetes.io/docs/reference/command-line-tools-reference/kube-apiserver/).
- Absorbs the announcement-UUID half of #2479 (coordinate in PR description).

---
---

## T3

**Title:** `Task: Lineage projection onto BroadcastEvent — actor_sub, agent_session_id, work_ref from the publish-site contextvars (agents stop broadcasting as humans)`

**Labels:** `task, enhancement, priority:high, effort:small, infrastructure`

**Body:**

Parent goal: #TBD-GOAL
Parent initiative: #TBD-INIT

## Summary

Delegated agent operations broadcast under the delegating human's `principal_sub` — a feed reader cannot tell agent from human, group a run's operations, or join work to a ticket. Every needed value is already computed, indexed on audit_log, and **in scope in the same module as the publish site**. This is a projection, not a plumbing job.

## Current state (verified in code, 2026-07-16)

- `BroadcastEvent` fields end at `payload` — no `actor_sub`/`agent_session_id`/`work_ref` (`broadcast/events.py:306-335`).
- Publish hook builds the event at `operations/_audit.py:577-590`; the sibling audit-row writer in the same module already reads all three sources: `agent_session_id_var.get()` (`:480`), `work_ref_var.get()` (`:499`), `resolve_actor_sub()` (`:511`).
- Audit columns + indexes exist: `db/models.py:475,488,546` / `:609-628`. Prior-art gap fix for lineage on approval-resume: #2086.
- Other `publish_event` call sites to sweep for the same projection: `operations/agent_run.py:484`, `operations/approval_queue.py:1265`, `audit.py:527`, `mcp/handlers.py:845`.
- Consumers tolerate additive fields: SSE re-validates the frozen model (`api/v1/feed.py:585`), MCP rows are dict dumps, UI templates ignore unknown keys.
- Sibling-collision note: #2479 edits row builders in `broadcast/history.py`/`mcp/tools/broadcast.py`; this task edits `events.py` + `_audit.py` — disjoint, but serialize merges if concurrent.

## Desired state

- `BroadcastEvent` gains optional `actor_sub: str | None`, `agent_session_id: UUID | None`, `work_ref: str | None`; every `publish_event` call site populates them from the same sources its adjacent audit write uses.
- `event_matches` (`broadcast/history.py:248`) gains `work_ref` and `actor_sub` filters so `meho.broadcast.recent {actor_sub: ...}` answers "what has this agent been doing".
- Wire/docs: fields are server-derived (trusted, unwrapped); `docs/codebase` broadcast section updated.

## Acceptance criteria

- [ ] Integration test: an operation dispatched under a delegated agent token broadcasts with `actor_sub` = the agent's sub and `principal_sub` = the human's — distinguishable on the feed (mirrors the audit-row assertion pattern of #2086's tests).
- [ ] Events from an agent run carry `agent_session_id`; `recent {actor_sub}` and `{work_ref}` filters return exactly the matching events (unit tests on `event_matches`).
- [ ] All five `publish_event` call sites project the fields (grep-pinned invariant test: no call site constructs `BroadcastEvent` without the lineage kwargs).
- [ ] Pre-v2 stream entries without the fields still validate (defaults `None`) — mixed-stream back-compat test; SSE/UI/MCP surfaces unchanged for old events.
- [ ] ruff + mypy clean.

## Out of scope

- Announcement linkage (T1 owns `run_id`/`work_ref` on announcements). UI rendering of the new fields (T5). Any change to `principal_sub` semantics.

## References

- Parent: #TBD-INIT. Ground: `operations/_audit.py:36,103,165,474-511,577-590`; `db/models.py:475-628`.
- Prior art: #2086 (lineage columns null on approval-gated dispatch — the audit-side twin of this fix). Adjacent: #2537 (actor_sub on topology audit rows), #2472 (agent-run projections).

---
---

## T4

**Title:** `Task: Hosted-agent bridge — broadcast announce/recent/watch as runtime meta-tools (the platform's own agents join the channel)`

**Labels:** `task, enhancement, priority:medium, effort:small, infrastructure`

**Body:**

Parent goal: #TBD-GOAL
Parent initiative: #TBD-INIT
Depends-on: #TBD-T1

## Summary

MEHO-hosted agent runs get exactly three meta-tools — `list_operation_groups`, `search_operations`, `call_operation` — so the platform's own agents can neither declare intent nor read peers; their only feed use is internal approval-resume plumbing. The announcement primitive already exists for MCP clients; this task is a toolset entry, not a new capability.

## Current state (verified in code, 2026-07-16)

- `_META_TOOL_CATALOG` = 3 `MetaToolSpec` entries (`agent/toolset.py:162-250`); `META_TOOL_NAMES` derives (`:255`); allow-list ∩ role filtering in `resolve_agent_tools` (`:406,465-469`); unknown names warn-and-ignore (`:454-459`).
- The MCP-side implementations to wrap: announce handler + `_list_recent_events_core` + watch long-poll (`mcp/tools/broadcast.py:198-925`; watch cap `_WATCH_MAX_TIMEOUT_MS=30_000` `:530`).
- Hosted runs already consume the stream internally for approval resume (`agent/approval_wait.py`) — the transport works in-runtime today.
- G11 goal #800 (closed) framed agent visibility as governance only; no issue ever proposed peer legibility for hosted agents (review finding 15, never-considered).

## Desired state

- Three new `MetaToolSpec` entries — `broadcast_announce`, `broadcast_recent`, `broadcast_watch` (runtime naming follows the catalog's underscore convention) — delegating to the same service helpers the MCP tools call, same OPERATOR `required_role`, same input bounds (T1 schema), same untrusted-envelope dump on reads.
- Announce calls from a hosted run auto-populate `run_id`/`work_ref` from the run context (the run knows itself — no self-reporting needed).
- Watch inside a run keeps the ≤30s single long-poll contract (no background subscriptions).

## Acceptance criteria

- [ ] A hosted agent run can call `broadcast_announce` and the event lands with `run_id` = the run's id and correct tenant/principal (runtime integration test through the toolset, mold: existing toolset dispatch tests).
- [ ] `broadcast_recent`/`broadcast_watch` return the same wire shape as the MCP tools (envelope wrapping included) — parity test.
- [ ] Tool allow-listing works: a run definition restricted to the three dispatch tools does NOT see the broadcast tools (`resolve_agent_tools` filter test).
- [ ] Prompt-safety: read results delivered to the run pass through `dump_event_wire` (untrusted envelope) — asserted, not assumed.
- [ ] ruff + mypy clean; agent-runtime docs updated.

## Out of scope

- Forcing announcements (adoption is T6's preamble discipline). Push/subscription delivery. Sub-OPERATOR read access.

## References

- Parent: #TBD-INIT. Moulds: `agent/toolset.py:108-255` (spec + catalog), `mcp/tools/broadcast.py` (handlers), `agent/approval_wait.py` (in-runtime stream consumption precedent).
- Review finding 15 (hosted agents excluded — never-considered), `broadcast-feature-review.md` 2026-07-16.

---
---

## T5

**Title:** `Task: Humans see announcements — SSE feed union validation + UI history/stream rendering`

**Labels:** `task, enhancement, priority:medium, effort:small, infrastructure`

**Body:**

Parent goal: #TBD-GOAL
Parent initiative: #TBD-INIT
Depends-on: #TBD-T1

## Summary

The intent stream is write-only for humans: the SSE pipeline validates every stream entry as `BroadcastEvent`, so announcement JSON fails validation and is skipped as malformed; the UI history pane filters announcements out with a docstring calling rendering "a follow-up" that was never filed. This task is that follow-up.

## Current state (verified in code, 2026-07-16)

- Skip site: `api/v1/feed.py:584-592` (`BroadcastEvent.model_validate_json` → `ValidationError` → `feed_skipped_malformed_event` → `continue`).
- UI filter: `ui/routes/broadcast/history.py:132-152` (`_is_audit_event` drops `agent_announcement`), applied `:202`; "follow-up" docstring `:53-54,148-149`.
- `/ui/broadcast/stream` imports `_process_entries` from the API feed module (`stream.py:75-84,212`) — fixing `_process_entries` fixes both.
- Prior art: #1305 (SSE fanout zero-bytes fix) — the transport is sound; this is a validation/rendering change.
- Announcement wire shape post-T1: structured fields unwrapped, prose enveloped (`broadcast/history.py:385-421`).

## Desired state

- `_process_entries` validates against a discriminated union on `kind` (`BroadcastEvent | AgentAnnouncementEvent`, pydantic 2.13.4 discriminated unions) — announcements flow to SSE consumers as first-class frames; genuinely malformed entries still skip with the existing log.
- UI history pane renders announcement rows (principal, phase chip, enveloped activity text displayed as plain quoted text — never interpreted; targets/work_ref/TTL shown from structured fields); live stream renders the same row partial. `_is_audit_event` filtering becomes a user-facing kind filter, not a hard drop.
- `meho status --watch` (CLI SSE consumer) tolerates the new frame kind (additive; verify the decode path).

## Acceptance criteria

- [ ] SSE integration test: an announcement published mid-stream arrives as a typed frame on `GET /api/v1/feed` and `/ui/broadcast/stream` (no `feed_skipped_malformed_event` log).
- [ ] UI history shows announcement rows with phase/target/work_ref; activity text is HTML-escaped and visually marked as agent-authored (screenshot-able template test; mold: existing `test_ui_broadcast_*` suites).
- [ ] Kind filter: `?kind=agent_announcement` and `?kind=operation` each return only their kind (route test).
- [ ] Mixed pre/post-T1 stream replays render without error (back-compat).
- [ ] ruff + mypy clean; CLI `status --watch` snapshot/decode unaffected or regenerated.

## Out of scope

- Slack or external mirrors (#333 stands). New standalone announcement pages. Deep DB-history UI (T2's backfill serves the MCP surface; UI deep-history is a later UX call).

## References

- Parent: #TBD-INIT. Ground: `api/v1/feed.py:545-592`, `ui/routes/broadcast/history.py:53-202`, `stream.py:75-212`.
- Pydantic discriminated unions: https://docs.pydantic.dev/latest/concepts/unions/. Prior art: #1305.
- Review finding 2 (announcements invisible to humans — CONFIRMED critical), `broadcast-feature-review.md` 2026-07-16.

---
---

## T6

**Title:** `Task: Protect and seed the channel — per-principal announce rate limit, broadcast discipline in the tenant preamble, fix the stale onboarding template`

**Labels:** `task, enhancement, priority:medium, effort:small, infrastructure`

**Body:**

Parent goal: #TBD-GOAL
Parent initiative: #TBD-INIT

## Summary

Three small changes that make the channel adoptable and un-floodable: (1) announce has no rate limit — one looping OPERATOR agent can trim the whole tenant's ~10k-entry coordination window in a burst; (2) the four-step broadcast discipline lives only in an optional consumer template — the server-assembled tenant preamble carries zero broadcast content; (3) that template still tells adopters the tools "are not yet wired" and names them with pre-rename underscore forms.

## Current state (verified in code, 2026-07-16)

- No rate limit on announce (`mcp/tools/broadcast.py:414-499`); stream trims at `BROADCAST_MAXLEN=10000` (`publisher.py:78`); review finding 28 (CONFIRMED bug).
- Preamble assembler: `conventions/preamble.py:252` (`assemble_preamble`) — grep for broadcast = 0 hits across the 525-line module.
- Stale template: `docs/examples/consumer-onboarding/CLAUDE.md:182-187` — quotes `broadcast_recent`/`broadcast_announce`/`broadcast_watch` and says registration is "**not yet wired**"; the tools have been registered since #1092 as `meho.broadcast.recent`/`.announce`/`.watch` (`mcp/tools/broadcast.py:200,416,812`).
- Rate-limit pattern: official Redis INCR fixed-window rate limiter (per-principal-per-window key, `INCR`+`EXPIRE`, reject over limit) — https://redis.io/docs/latest/commands/incr/ ("Pattern: rate limiter"); redis-py 8.0.1 (`uv.lock:3229`).

## Desired state

- Per-`(tenant, principal_sub)` fixed-window rate limit on announce (settings knob `broadcast_announce_rate_per_minute`, default 10, `0` = unlimited) enforced in the announce handler before publish; over-limit → typed JSON-RPC error naming the window, not a silent drop (fail-loud contract preserved).
- A broadcast-discipline band in `assemble_preamble` (static text, same band mechanics as the existing conventions band): check `meho.broadcast.recent` for conflicting activity before starting work on a target → announce start with targets/TTL → announce completion. Substrate-minimal: guidance, not enforcement.
- Onboarding template corrected: dotted tool names, "Tooling status" caveat replaced with the real contract (announce discipline + trust rule).

## Acceptance criteria

- [ ] Rate-limit test: 11th announce in one minute from one principal rejected with the typed error; another principal in the same tenant unaffected; `0` disables (unit + Valkey integration).
- [ ] Preamble test: assembled preamble contains the discipline band exactly once (`test_conventions_preamble` mold); band text names the dotted tool names.
- [ ] `grep -n "not yet wired\|broadcast_recent\`" docs/examples/consumer-onboarding/CLAUDE.md` returns nothing; template names match `tools/list` output.
- [ ] Existing announce happy path unaffected under the default limit (regression).
- [ ] ruff + mypy clean.

## Out of scope

- Rate limiting operation events (server-derived, already bounded by dispatch). Enforcing announcements (discipline stays advisory). Global/stream-level quotas.

## References

- Parent: #TBD-INIT. Ground: `mcp/tools/broadcast.py:414-499`, `publisher.py:78`, `conventions/preamble.py:252`, onboarding CLAUDE.md:182-187 (#316/#318 shipped these artifacts).
- Redis rate-limiter pattern: https://redis.io/docs/latest/commands/incr/; survey https://redis.io/docs/latest/develop/use-cases/rate-limiter/.
- Review findings 5 (adoption) + 28 (flood), `broadcast-feature-review.md` 2026-07-16.

---
---

## T7

**Title:** `Task: Dispatch-time target-activity advisory — write-op responses carry recent peer activity on the same target via OperationResult.extras`

**Labels:** `task, enhancement, priority:medium, effort:medium, infrastructure`

**Body:**

Parent goal: #TBD-GOAL
Parent initiative: #TBD-INIT
Depends-on: #TBD-T1
Depends-on: #TBD-T3

## Summary

Close the crossfire loop with awareness, not locks: when a write-class operation dispatches against a target that has recent peer activity (another principal's operations or an active announcement claim), the dispatch response carries a compact advisory so the caller — human or agent — learns about the overlap at the moment it matters. Pre-op checking remains the discipline's `recent` step; this is the safety net for the caller who didn't check.

## Current state (verified in code, 2026-07-16)

- No dispatch-time consult of the feed exists; crossfire avoidance is 100% voluntary reader-side polling (review finding 23).
- The response seam: OK envelope built by `wrap_ok_result` (`operations/dispatcher.py:756`; builder `operations/_errors.py:1478`); `OperationResult.extras: Mapping[str, Any]` is the established extension slot (`connectors/schemas.py:616-636`, frozen model).
- Target identity in scope at the seam: `target.name` / `_resolve_target_id(target)` (`operations/_audit.py:474-475,575`).
- Recent-activity query primitives: `_list_recent_events_core` + `event_matches` target filter (`broadcast/history.py:248-493`); post-T1 `active_only` claims; post-T3 `actor_sub` distinguishes the acting agent.
- Sibling-collision: topology #2537 edits the same dispatcher region (MCP handlers dispatching through `policy_gate`) — serialize merges.

## Desired state

- In the dispatch success path for write-class ops (`op_class` already resolved for audit/broadcast), a bounded read of the tenant stream (last N minutes, target-filtered, excluding the caller's own principal/actor) yields `extras["target_activity_advisory"]`: up to 5 entries of `{principal_sub, actor_sub?, kind (operation|announcement), op_id?/phase?, ts}` — structured fields only, **no announcement prose** (the untrusted envelope never enters an op response).
- Fail-open and bounded: advisory lookup failure or timeout (single capped stream read) never fails or delays the dispatch beyond a small budget; a settings knob (`dispatch_activity_advisory_window_minutes`, default 30, `0` = off) gates the feature.
- Read-class ops skip the lookup entirely (no cost on the hot read path).

## Acceptance criteria

- [ ] Integration test: principal A announces a claim on target X (T1) and runs a write op; principal B's write op on X returns `extras["target_activity_advisory"]` naming A's claim and op; B's op on target Y carries no advisory.
- [ ] The advisory contains zero prose fields (grep/schema test: no `activity`/`scope` keys) and excludes the caller's own activity.
- [ ] Advisory lookup failure (Valkey down) → op succeeds without the key, warn-logged (fail-open test, mold: `publish_event` fail-open tests).
- [ ] Read-class dispatch performs no advisory lookup (call-count assertion); knob `0` disables entirely.
- [ ] ruff + mypy clean; OpenAPI snapshot regenerated if the envelope schema surfaces `extras` typing changes.

## Out of scope

- Blocking, locking, or claim enforcement (a Lease-style hard claim is a separate future decision). Advisory on the error path. UI rendering of advisories.

## References

- Parent: #TBD-INIT. Ground: `operations/dispatcher.py:756`, `operations/_errors.py:1478`, `connectors/schemas.py:616-636`, `broadcast/history.py:248-493`.
- Comparable: Kubernetes Lease as the awareness/coordination primitive agents check before acting (https://kubernetes.io/docs/concepts/architecture/leases/) — this task ships the awareness half only.
- Review findings 23 (no dispatch-time consult) + 27 (structured-fields-are-trustable), `broadcast-feature-review.md` 2026-07-16. Serialize with #2537.
