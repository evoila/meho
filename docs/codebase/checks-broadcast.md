# Check transition broadcast events (`checks/broadcast.py`)

## Overview

Every claimed Dashboard rollup edge is published to the tenant broadcast
feed (`meho:feed:{tenant_id}`) as one `BroadcastEvent` with op-id
`checks.transition` and op-class `checks`. Feed watchers —
`meho.broadcast.recent`, `meho.broadcast.watch`, `GET /api/v1/feed`, and
`/ui/broadcast/stream?op_class=checks` — see check state changes the way
they already see `approval.*` lifecycle events (#2720).

This is the **third** independent consumer of #2507's transition claim,
beside the diagnose-only investigator
(`docs/codebase/checks-investigator.md`, worsening edges only) and the
email notifier (`docs/codebase/checks-notifications.md`, both directions
above a configured floor). None of the three knows about the others;
all three read the same compare-and-swap on
`check_dashboards.last_rollup_state`.

Unlike the notifier, the publish has **no floor**. A feed event costs one
`XADD` against a `MAXLEN ~`-trimmed stream, so narrowing belongs to the
consumer (`op_class=checks`), not to the producer.

## Key types

- `publish_check_transition_event(*, tenant_id, dashboard_id,
  dashboard_name, previous_state, new_state)` — the awaitable that builds
  and publishes the event. **Never raises.**
- `CHECK_TRANSITION_OP_ID` — the `"checks.transition"` literal.
- `_CHECK_EVENT_OPS` (in `broadcast/events.py`) — the classifier's
  allowlist, the other half of the op-id contract.

## Control flow

```text
runner persists a Sensor result
  → investigate_on_transition(sensor_id, tenant_id)          [never raises]
    → _process_transition
      → _claim_dashboard_transition (advisory lock + CAS)     [per dashboard]
        → _ClaimedTransition | None
      → for each won claim:
          _schedule_investigation(...)        [worsening + non-green only]
          schedule_dashboard_notification(...)          [background task]
          await publish_check_transition_event(...)     [inline, bounded]
```

The claim is the exactly-once token. Because publish sits on the
claim-win branch, two replicas racing the same edge produce exactly one
event with no dedupe key here — the same property the notifier inherits.

The publish is **awaited inline** while the two expensive consumers are
backgrounded. A claim is a rare edge, `XADD` is bounded by the fast
broadcast client's pinned connect and read timeouts, and awaiting keeps
the event ordered with the claim that caused it.

## The `checks` op-class

`classify_op` maps `checks.transition` to `checks` by **exact
membership** in `_CHECK_EVENT_OPS`, not by a `checks.` prefix. The prefix
is already occupied: `/api/v1/checks/*` binds `checks.assignment.put`,
`checks.assignment.get`, and `checks.results.post` as audit op-ids.

What those three rows *persist* is not at risk from a prefix branch.
Each of the routes also binds an explicit `audit_op_class` contextvar
(`write` / `read` / `write`), and `resolve_broadcast_detail` in
`broadcast/overrides.py` takes that `op_class_override` in preference to
`classify_op`. The same value is what the audit row stores in
`payload["op_class"]`, and that stored value is what `meho.audit.query`
filters on in SQL (`audit_query/query.py`). `classify_op` therefore never
decides the gateway rows' class on the write path, and no saved
`op_class=write` query would change its results.

The hazard is on the **read** side. `classify_op` is re-run at render
time against the stored op-id by the audit drawer
(`ui/routes/audit/routes.py`) and the broadcast event drawer
(`ui/routes/broadcast/event.py`), supplying the row's displayed class and
badge and feeding `is_aggregate_only`. A prefix branch would therefore

1. relabel every `/api/v1/checks/*` row in those drawers as `checks` —
   retroactively, since the class is derived on read rather than read
   back off the row, so there is no deploy boundary to reason about; and
2. sweep in any future non-transition `checks.*` op-id by construction,
   diluting the class whose whole purpose is surfacing rare transition
   edges.

`checks` is **not** sensitive: it is absent from
`broadcast/overrides.py::_SENSITIVE_OP_CLASSES`, so events broadcast at
full detail. That is safe because the payload stays at Dashboard
altitude — id, name, and the two states. No Sensor values, evidence, or
member names ride the feed; a consumer that wants the failing members
reads the Dashboard.

`checks` is also listed in `broadcast/history.py::OP_CLASS_ENUM`. That
tuple is not cosmetic: the MCP dispatcher validates every `tools/call`
against the advertised `inputSchema` with `jsonschema`, and
`filter.op_class` carries the enum, so a class missing from it is a
JSON-RPC `-32602` before the handler runs. `meho.audit.query` shares the
tuple as one taxonomy, where `checks` is always empty — see "Known
issues" below.

## Not audit-derived

`BroadcastEvent.audit_id` is documented as an FK to `audit_log.id`, and
every other publisher sits downstream of an audit row. A rollup edge is
not an audited operation: it is derived state, folded from Sensor
evaluations that were each audited on their own dispatch. So the event
carries the **nil UUID** rather than a fabricated id a consumer could not
distinguish from a real one. The one surface that dereferences the field
— the UI event drawer at `/ui/broadcast/event/{audit_id}` — already
renders its not-found fragment for an id with no row, so the nil id
degrades rather than breaks.

`principal_sub` is `"__checks__"` for the same reason: the edge has no
operator behind it, and a Dashboard folds many Sensors that may each
dispatch under a different `identity_sub`, so attributing the edge to any
one of them would be a lie. `"__sensor__"` is deliberately not reused —
that value is a per-Sensor *configurable dispatch identity*, not a
subsystem identity.

## Failure posture

Fail-open twice over, and the two halves log in **different** places —
which is what an operator wiring an alert needs to know.

`publish_event` already swallows every Valkey error (at-most-once
delivery is the feed's documented contract). It never re-raises, so a
Valkey outage or a failing `XADD` does **not** reach this module's guard:
it surfaces on the existing feed-wide signals, the
`broadcast_publish_failed` warning and the `broadcast_publish_errors_total`
counter, exactly as it does for every other publisher. Those are what a
"transitions stopped reaching the feed" alert should watch.

`publish_check_transition_event` wraps the whole body in a second guard
purely so a failure *before* the publish — an unexpected lineage
resolution or a `BroadcastEvent` validation error on a malformed field —
cannot reach the caller either. That guard, and only that guard, logs
`checks_transition_broadcast_failed` at warning with the dashboard id and
both states. It is a construction-bug signal, not an outage signal.

The durable truth is the committed `last_rollup_state` memo. A broadcast
outage must never convert a committed transition into a persist-path
failure, and must not affect the email notifier.

## Dependencies

- `meho_backplane.broadcast.events` — `BroadcastEvent`, `classify_op`.
- `meho_backplane.broadcast.publisher` — `publish_event` (fail-open
  `XADD`, `MAXLEN ~ 10000`).
- `meho_backplane.operations._audit` — `resolve_broadcast_lineage`, so
  the event carries the same actor / agent-session / work_ref lineage an
  audit-driven event would.

The dependency runs one way: `checks/` imports `broadcast/`, never the
reverse. That is why the `checks.transition` literal is written in both
layers and pinned together by a test rather than shared as a constant.

## Known issues / boundaries

- **`meho.audit.query` advertises `op_class=checks` but never matches.**
  The filter vocabulary is single-sourced from `OP_CLASS_ENUM` across the
  broadcast and audit tools, and a transition has no audit row. An
  operator narrowing audit to `checks` gets an empty result; the checks
  *gateway* rows are stored under `write` / `read` / `write`, the classes
  their routes bind via `audit_op_class`. (The drawers *display*
  `classify_op`'s answer instead, which is `write` / `read` / `other` —
  a pre-existing read-vs-write divergence for any route binding an
  explicit `audit_op_class`, not something this change introduces.)
- **No rate limit, digest, or repeat suppression.** A flapping Dashboard
  publishes one event per real edge, matching the notifier's stance
  (#2716 scopes volume control out).
- ~~**The UI feed palette and filter dropdown do not list `checks`.**~~
  Resolved by #2731: `OP_CLASS_BADGE_CLASSES` covers every
  `OP_CLASS_ENUM` member (`checks` badges as `badge-primary`) and
  `OP_CLASS_FILTER_OPTIONS` is now single-sourced from `OP_CLASS_ENUM`;
  a test pins both tables to the enum.
- **No event-outbox routing and no durable event table.** The Valkey
  stream plus its retention window is the carrier, exactly as for
  `approval.*` (the outbox matcher is a no-op pending #826).

## References

- `backend/src/meho_backplane/checks/broadcast.py`
- `backend/src/meho_backplane/checks/investigate.py` —
  `_process_transition`, `_claim_dashboard_transition`
- `backend/src/meho_backplane/broadcast/events.py` — `_CHECK_EVENT_OPS`,
  `classify_op`
- `backend/src/meho_backplane/broadcast/history.py` — `OP_CLASS_ENUM`
- `backend/src/meho_backplane/api/v1/checks.py` — `_CHECKS_OP_IDS`, the
  three gateway op-ids the allowlist exists to protect
- `backend/tests/test_checks_broadcast.py`,
  `backend/tests/test_broadcast_events.py`,
  `backend/tests/test_mcp_tool_broadcast_recent.py`
- `docs/codebase/broadcast.md`, `docs/codebase/checks-notifications.md`,
  `docs/codebase/checks-investigator.md`
- Mould: `operations/approval_queue.py::publish_approval_event`
