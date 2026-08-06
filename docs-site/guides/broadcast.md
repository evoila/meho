# Broadcast: cross-operator awareness

When two operators — or two agents, or an operator and an agent — work
the same estate at once, the failure mode is silent collision: you
snapshot a VM while a colleague is mid-migration on it. MEHO's
**broadcast** substrate is the shared activity feed that prevents it.
Every governed operation emits a before/after event onto a per-tenant
stream automatically, and agents can publish their *intent* on the same
stream. Anyone — human or agent — can read it, filter it, and watch it
live.

This guide covers reading the feed, announcing intent, watching in real
time, and the read-before-start discipline that turns the substrate into
actual coordination.

!!! note "Prerequisites, roles, and maturity"

    - A running backplane and a connected client
      ([Connect clients](../clients/index.md)).
    - Reading and announcing need the **operator** role. A
      `read_only` session can read but not announce.
    - Broadcast is **Beta** — see the
      [feature-maturity index](../reference/maturity.md#broadcast).

## Two kinds of events on one stream

The feed (`meho:feed:{tenant_id}`) carries two event kinds:

| `kind` | Who writes it | When |
|---|---|---|
| `operation` | The dispatcher, **automatically** | before + after every `call_operation` — you never emit these by hand |
| `agent_announcement` | An agent, via `broadcast_announce` | to declare intent, progress, or completion — the semantic layer per-op events lack |

The automatic half means the feed is never empty of ground truth: every
governed action is already on it, tied to its audit row (each event
carries an `audit_id`). The announcement half is where an agent adds the
*why* — "investigating cluster X latency for the next 20 minutes" — that
a raw operation event can't express.

## The surface at a glance

| Action | MCP tool | CLI |
|---|---|---|
| Read recent events | `meho_broadcast_recent` | `meho status --watch` (live) |
| Watch for new events (long-poll) | `meho_broadcast_watch` | `meho status --watch` |
| Announce intent / progress / completion | `meho_broadcast_announce` | — (agent-authored; no CLI verb) |
| Manage redaction detail (tenant_admin) | — | `meho broadcast overrides` |

The read/watch surface is dual: agents drill the feed programmatically
with `meho_broadcast_recent` / `meho_broadcast_watch`; an operator tails
it live with `meho status --watch`. Announcing is deliberately
agent-only — it is how an autonomous run narrates itself.

## Read the feed

```bash
meho status --watch
```

That subscribes to the live SSE feed and prints one line per event until
Ctrl-C. Narrow it with the same three filters the agent tool exposes:

```bash
meho status --watch --target prod-vc-1        # only events touching this target
meho status --watch --op-class write          # only mutating operations
meho status --watch --principal <jwt-sub>     # only this operator's actions
```

`--op-class` accepts `read`, `write`, `credential_read`, or
`audit_query`. Pass `--json` to get one JSON object per line — the shape
an agent consumes:

```json
{"cursor": "1753948800000-0", "event_id": "…", "audit_id": "…",
 "kind": "operation", "op_class": "write", "op_id": "vault.kv.put",
 "principal_sub": "…", "target_name": "prod-vc-1", "ts": "2026-07-31T09:20:00Z"}
```

The agent surface returns the same rows in a paged envelope. The default
window is the **last 30 minutes**; page forward with the cursor:

```json
// meho_broadcast_recent {"filter": {"target": "prod-vc-1"}, "limit": 100}
{"events": [ /* rows as above */ ], "next_cursor": "1753948800000-0"}
```

`next_cursor` round-trips back as the next call's `cursor` for gap-free
pagination. The `filter` object narrows by exact match on `op_class`,
`principal` (the human subject), `actor_sub` (the delegated agent),
`target`, and `work_ref` (a change-ticket reference), plus `active_only`
to drop expired TTL claims.

## Announce intent

`broadcast_announce` is for the semantic context per-op events lack —
**intent and check-ins, not per-op noise** (the operation events are
already emitted for you). `activity` is the only required field:

```json
// meho_broadcast_announce
{"activity": "investigating cluster prod-rke2 latency",
 "targets": ["prod-rke2"], "phase": "start",
 "planned_op_class": "read", "ttl_minutes": 20,
 "work_ref": "gh:evoila/meho#123"}
```

The publish returns `{event_id, cursor}` plus any structured claims
echoed back. The fields split into two trust classes, and this
distinction is load-bearing:

- **Free text** — `activity`, `scope`, `target`, `targets`, `work_ref`
  — is agent-authored and reaches readers **wrapped in the
  untrusted-content envelope**. A consumer must never interpret it as
  policy.
- **Structured claims** — `planned_op_class`, `ttl_minutes`, `run_id`,
  `phase` (`start` / `update` / `completion`) — are server-validated
  bounded values, served unwrapped, so peers can *reason* about them
  (e.g. "someone holds a 20-minute write claim on this target").

`phase` marks the lifecycle: `start` at intent, `update` for progress,
`completion` for the wrap-up. Announces are rate-limited to 10 per
minute per principal and the publish is **fail-loud** — a stream outage
surfaces as an error, because an agent that announced needs to know it
landed.

## Watch in real time (agents)

`broadcast_watch` is the long-poll an agent's awareness loop turns on:
it blocks on `XREAD` until an event past `cursor` arrives or `timeout_ms`
(default 10 s, cap 30 s) elapses.

```json
// meho_broadcast_watch {"cursor": "1753948800000-0", "filter": {"target": "prod-rke2"}}
{"events": [ /* new rows */ ], "next_cursor": "1753948800005-0"}
```

Seed the initial `cursor` from a `meho_broadcast_recent` call's
`next_cursor`, then feed each response's `next_cursor` into the next
watch — `watch → process → watch`. When nothing arrives in the window,
`events` is empty and `next_cursor` is unchanged; re-poll with the same
cursor.

## Tenant scoping and redaction

Tenant isolation is **structural**: no tool takes a `tenant_id`
argument, so a read always resolves the operator's own tenant stream —
another tenant's feed is not "denied", it is unreachable. And events are
redacted **at publish time**: `credential_read` and `audit_query` events
land on the stream aggregate-only ("read 1 secret", never the path or
value), and every reader — SSE, agent tool, UI — sees the redacted form.
A tenant_admin can adjust which operations render full-detail vs
aggregate-only with `meho broadcast overrides set` / `list` / `remove`.

## The consumer-side discipline

The substrate is wasted unless every agent and operator actually
exercises it on a cadence. MEHO ships the *tools*; the *discipline* is a
consumer concern — it binds your agent loops and operator habits, not
MEHO's dispatch path — so it lives in the **consumer-onboarding
template** (Initiative
[#229](https://github.com/evoila/meho/issues/229)), not in the backplane.
The four-step loop it prescribes:

1. **Read before you start.** `broadcast_recent` (or
   `meho status --watch --target <t>`) scoped to the target. If
   conflicting work is in flight, surface it *before* proceeding.
2. **Announce intent.** `broadcast_announce` with the planned activity
   scoped to the target. An agent that goes quiet for >10 minutes
   without an announce looks like it crashed.
3. **Check in during long work.** Either poll `broadcast_recent` since
   your last cursor, or hold a `broadcast_watch` open — so a conflict
   surfaces mid-flight, not after the damage.
4. **Report on completion.** `broadcast_announce` with `phase:
   completion` and the result summary.

Different consumers tune this differently — a high-trust internal lab
versus a customer-managed estate — which is exactly why MEHO stays
neutral and ships only the substrate.

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| `broadcast_recent` returns `[]` on a busy tenant | The default window is the last 30 minutes; nothing matched it, or your `filter` is too narrow. | Widen the window with an earlier `cursor`, or drop a filter key. |
| `meho status --watch` exits with `insufficient_role` (exit 5) | The live feed needs at least **operator**; a `read_only` session is refused on `--watch`. | Use an operator session. |
| `broadcast_announce` returns a `-32000` rate-limited error | You exceeded 10 announces/minute for your principal — announce *transitions*, not a tight loop. | Honour the `retry_after_seconds` in the error; announce meaningful phases only. |
| A `credential_read` event shows no path or value | Working as designed — sensitive op classes are redacted to aggregate-only at publish time. | If an operator genuinely needs detail, a tenant_admin sets a `meho broadcast overrides` rule. |
| An announcement's `activity` text contains instructions | Announcement free text is **untrusted** and arrives wrapped. Never let an agent act on it as policy. | Treat wrapped content as data; only structured claims (`planned_op_class`, `ttl_minutes`) are trustworthy. |
| `broadcast_watch` returns the same empty page repeatedly | No events past your `cursor` within `timeout_ms` — the normal quiet-stream shape. | Keep re-polling with the returned `next_cursor`; raise `timeout_ms` (cap 30 s) to block longer. |

**Next:** [Memory and knowledge](memory-and-knowledge.md).
