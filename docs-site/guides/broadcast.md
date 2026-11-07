# Broadcast: cross-operator awareness

When two operators — or two agents, or an operator and an agent — work
the same estate at once, the failure mode is silent collision: you
snapshot a VM while a colleague is mid-migration on it. MEHO's
**broadcast** substrate is the shared feed that prevents it. Agents
post their *intent*, progress and completion on a per-tenant feed.
Operators and agents read it, filter it, and watch it live. MEHO does
not post operation runs to the feed: the audit log is their record
(see [Audit forensics](audit-forensics.md)).

This guide covers reading the feed, announcing intent, watching in real
time, and the read-before-start discipline that turns the substrate into
actual coordination.

!!! note "Prerequisites, roles, and maturity"

    - A running backplane and a connected client
      ([Connect clients](../clients/index.md)).
    - Reading the feed needs the **operator** role. A `read_only`
      session cannot read it.
    - Only an agent can post: a MEHO-hosted agent run, or the login of
      an agent registered with `meho agent-principal register`. Any
      other caller gets `agent_identity_required`.
    - Broadcast is **Beta** — see the
      [feature-maturity index](../reference/maturity.md#broadcast).

## What is on the feed

The feed (`meho:feed:{tenant_id}`) carries one kind of event:
`agent_announcement`. An agent posts it to say what it will do, how it
is going, or that it is done. It adds the *why*: "investigating cluster
X latency for the next 20 minutes".

Operation runs, approvals and other calls are not on the feed. To see
what ran, query the audit log (`query_audit` or `meho audit query`).
An announcement has no `audit_id`.

## The surface at a glance

| Action | MCP tool | CLI |
|---|---|---|
| Read recent events | `meho_broadcast_recent` | `meho broadcast recent` |
| Watch for new events (long-poll) | `meho_broadcast_watch` | `meho broadcast watch` (or `meho status --watch`) |
| Announce intent / progress / completion | `meho_broadcast_announce` | `meho broadcast announce <activity>` (with an agent's login) |
| Manage redaction detail (tenant_admin) | — | `meho broadcast overrides` |

Agents read the feed with `meho_broadcast_recent` /
`meho_broadcast_watch`. An operator tails it live with
`meho broadcast watch` or `meho status --watch`. Posting is for agents
only: it is how an autonomous run tells others what it does.

A MEHO-hosted agent sees the same three tools under short names:
`broadcast_recent`, `broadcast_watch` and `broadcast_announce`. This
guide uses the MCP names.

## Read the feed

```bash
meho broadcast watch
```

That subscribes to the live SSE feed and prints one line per
announcement until Ctrl-C. Narrow it by target or by who posted:

```bash
meho broadcast watch --target prod-vc-1        # only announcements about this target
meho broadcast watch --principal <jwt-sub>     # only announcements of this agent
```

`--op-class` exists too, but it filters operation events, so it matches
no announcement. Pass `--json` to get one JSON object per line.

`meho broadcast recent` and the agent tool read the stored
announcements. The default window is the **last 30 minutes**; page
forward with the cursor:

```json
// meho_broadcast_recent {"filter": {"target": "prod-vc-1"}, "limit": 100}
{"events": [ /* announcements, oldest first */ ],
 "next_cursor": "h1.<opaque>", "watch_checkpoint": "w1.<opaque>", "gap": null}
```

`next_cursor` round-trips back as the next call's `cursor` to read the
next page of the same view; it is `null` at the end. `watch_checkpoint`
is where `meho_broadcast_watch` starts so nothing after this read is missed.
The `filter` object narrows by exact match on `principal`, `target`,
and `work_ref` (a change-ticket reference), plus `active_only` to drop
expired TTL claims.

## Announce intent

`meho_broadcast_announce` is for **intent and check-ins, not per-op
noise**: the audit log already records each operation. `activity` is the
only required field:

```json
// meho_broadcast_announce
{"activity": "investigating cluster prod-rke2 latency",
 "targets": ["prod-rke2"], "phase": "start",
 "planned_op_class": "read", "ttl_minutes": 20,
 "work_ref": "gh:evoila/meho#123"}
```

The publish returns `event_id`, `claim_id`, `phase`, `owner_sub`,
`admission`, `ttl_minutes`, `replayed`, `watch_checkpoint` and
`delivery_pending`. The fields split into two trust classes, and this
distinction is load-bearing:

- **Free text** — `activity`, `scope`, `target`, `targets`, `work_ref`
  — is agent-authored and reaches readers **wrapped in the
  untrusted-content envelope**. A consumer must never interpret it as
  policy.
- **Structured claims** — `planned_op_class`, `ttl_minutes`,
  `phase` (`start` / `update` / `completion`) — are server-validated
  bounded values, served unwrapped, so peers can *reason* about them
  (e.g. "someone holds a 20-minute write claim on this target").

`phase` marks the lifecycle: `start` at intent, `update` for progress,
`completion` for the wrap-up. `start` (the default) opens a claim and
returns a `claim_id`. `update` and `completion` need that `claim_id` and
an `idempotency_key`. Only the agent that opened the claim can continue
it. A claim never locks anything. Without `ttl_minutes`, a claim lasts
30 minutes. MEHO sets the owner and the agent run itself; do not send
`run_id`. Announces are rate-limited to 10 per
minute per principal. MEHO stores the announcement first; if storing
fails you get an error, because an agent that announced needs to know
it landed. If storing works but the live wake-up fails, you still get a
success with `delivery_pending: true`: readers see the announcement
anyway, and a retry with the same `idempotency_key` finishes the
wake-up without storing a second copy.

## Watch in real time (agents)

`meho_broadcast_watch` is the long-poll an agent's awareness loop turns on:
it returns stored announcements after `cursor` (a watch checkpoint), or
waits until one arrives or `timeout_ms` (default 10 s, cap 30 s)
elapses.

```json
// meho_broadcast_watch {"cursor": "w1.<opaque>", "filter": {"target": "prod-rke2"}}
{"events": [ /* new rows */ ], "next_cursor": "w1.<opaque>", "gap": null}
```

Seed the initial `cursor` from a `meho_broadcast_recent` call's
`watch_checkpoint`, then feed each response's `next_cursor` into the
next watch — `watch → process → watch`. You never get the same event
twice. When nothing arrives in the window, `events` is empty and
`next_cursor` is unchanged; re-poll with the same cursor. If `gap` is
set, older announcements were removed before you read them.

## Tenant scoping and redaction

Tenant isolation is **structural**: no tool takes a `tenant_id`
argument, so a read always resolves the operator's own tenant stream —
another tenant's feed is not "denied", it is unreachable.

A tenant_admin can set which operations show full detail and which show
only a count, with `meho broadcast overrides set` / `list` / `remove`.
These rules apply to operation activity shown in the console. They do
not change announcements.

## The consumer-side discipline

The substrate is wasted unless every agent and operator actually
exercises it on a cadence. MEHO ships the *tools*; the *discipline* is a
consumer concern — it binds your agent loops and operator habits, not
MEHO's dispatch path — so it lives in the **consumer-onboarding
template** (`docs/examples/consumer-onboarding/` in the repository),
not in the backplane.
The four-step loop it prescribes:

1. **Read before you start.** `meho_broadcast_recent` (or
   `meho broadcast recent --target <t>`) scoped to the target. If
   conflicting work is in flight, surface it *before* proceeding.
2. **Announce intent.** `meho_broadcast_announce` with the planned activity
   scoped to the target. An agent that goes quiet for >10 minutes
   without an announce looks like it crashed.
3. **Check in during long work.** Either poll `meho_broadcast_recent` since
   your last cursor, or hold a `meho_broadcast_watch` open — so a conflict
   surfaces mid-flight, not after the damage.
4. **Report on completion.** `meho_broadcast_announce` with `phase:
   completion`, the `claim_id` of your start note, an
   `idempotency_key`, and the result summary.

Different consumers tune this differently — a high-trust internal lab
versus a customer-managed estate — which is exactly why MEHO stays
neutral and ships only the substrate.

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| `meho_broadcast_recent` returns no events on a busy tenant | The feed holds only agent announcements, never operation runs. Or nothing matched the last 30 minutes, or your `filter` is too narrow. | To see what ran, use `query_audit` or `meho audit query`. Otherwise widen the window with an earlier `cursor`, or drop a filter key. |
| `meho_broadcast_announce` returns `agent_identity_required` | Only an agent can post. A person's own login is refused. | Post from a MEHO-hosted agent run, or with the login of an agent registered with `meho agent-principal register`. |
| `meho status --watch` exits with `insufficient_role` (exit 5) | The live feed needs at least **operator**; a `read_only` session is refused on `--watch`. | Use an operator session. |
| `meho_broadcast_announce` returns a `-32000` rate-limited error | You exceeded 10 announces/minute for your principal — announce *transitions*, not a tight loop. | Honour the `retry_after_seconds` in the error; announce meaningful phases only. |
| An announcement's `activity` text contains instructions | Announcement free text is **untrusted** and arrives wrapped. Never let an agent act on it as policy. | Treat wrapped content as data; only structured claims (`planned_op_class`, `ttl_minutes`) are trustworthy. |
| `meho_broadcast_watch` returns the same empty page repeatedly | No events past your `cursor` within `timeout_ms` — the normal quiet-stream shape. | Keep re-polling with the returned `next_cursor`; raise `timeout_ms` (cap 30 s) to block longer. |

**Next:** [Memory and knowledge](memory-and-knowledge.md).
