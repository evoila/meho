# Dashboard email notifications (`checks/notify.py`)

## Overview

When a check Dashboard's five-state rollup crosses an edge,
`meho_backplane.checks.notify` mails the Dashboard's configured
recipient — **in both directions**. An operator paged for `critical`
gets the all-clear when the Dashboard recovers, which is what Prometheus
Alertmanager's `send_resolved` provides and what #2716 made a binding
decision for this layer.

The notifier is the second, independent consumer of #2507's transition
claim. The first is the diagnose-only investigator
(`docs/codebase/checks-investigator.md`), which fires on a **worsening**
edge only. Both read the same compare-and-swap on
`check_dashboards.last_rollup_state`; neither knows about the other.

Delivery goes through the `mail.*` connector's shared
`transport.send_email()` (`docs/codebase/connectors-mail.md`), so the
deployment-level `MAIL_RECIPIENT_ALLOWLIST` floor applies here exactly as
it does to an agent-dispatched `mail.send`. An empty allowlist means the
notifier is inert — there is no path around the floor.

## Key types

- `notify_dashboard_transition(notice)` — the awaitable that applies the
  threshold rule, builds the message, and calls the transport. **Never
  raises** an ordinary exception; `asyncio.CancelledError` is a
  `BaseException`, so it still propagates and a tracked task can be
  cancelled at shutdown.
- `schedule_dashboard_notification(notice)` — spawns the above as a
  tracked fire-and-forget `asyncio.Task`. What `investigate.py` calls.
- `DashboardNotice` — the detached input: dashboard id, name, the
  `previous -> current` edge, the two config columns, and the non-green
  members. Built inside the claim transaction while the ORM row is live.
- `NotifyMember` — one non-green member as the mail renders it (name,
  effective state, last value, last evidence). A notifier-owned
  projection, not a reuse of the investigator's briefing snapshot: this
  module must not import `investigate`, which imports *it*.
- `_NOTIFY_RANK` — `ok`/`skip` = 0, `unknown`/`degraded` = 1,
  `critical` = 2.

## Configuration (per Dashboard, set at create only)

Two columns on `check_dashboards`, added by migration `0068`:

| Column | Type | Meaning |
|---|---|---|
| `notify_email` | `text` NULL | The single recipient. **NULL = notifications off**, the state every pre-#2719 row backfills to. |
| `notify_min_state` | `text` NOT NULL, default `'critical'`, CHECK `IN ('degraded','critical')` | The floor an edge must reach. |

`notify_min_state`'s vocabulary is deliberately narrower than
`CheckState`: `ok` as a floor would mail on every edge, and
`skip` / `unknown` are not severities a threshold is meaningful at. The
model, the wire `NotifyMinState` Literal, and the migration's frozen
tuple are pinned against each other by drift guards in
`tests/test_db_dashboard.py`.

Both are set at Dashboard-create only — the same immutability posture as
membership. There is no PATCH route; "edit" is delete + recreate.
Surfaces: `POST /api/v1/checks/dashboards` (body fields, `EmailStr`-
validated → 422 on a malformed address) and
`meho dashboard create --notify-email --notify-min-state`.

## The notify rule

```
notify  <=>  max(rank(previous), rank(current)) >= rank(notify_min_state)
```

Symmetric in the two states, which is what makes recovery notify. At the
default floor of `critical`:

| Edge | Mails? | Why |
|---|---|---|
| `ok -> critical` | yes | `critical` side clears the bar |
| `critical -> ok` | yes | all-clear; the `critical` side still clears the bar |
| `ok -> degraded` | no | neither side reaches `critical` |
| `degraded -> ok` | no | same |
| `ok -> unknown` | no | `unknown` ranks with `degraded` |
| `critical -> skip` | yes | the `critical` side, not the `skip` side |

At `notify_min_state='degraded'` every `no` row above flips to yes —
including `ok -> unknown`, since `unknown` shares rank 1 with
`degraded`. `skip` never clears the bar on its own at any floor.

## Control flow

1. **The claim** (`investigate._claim_dashboard_transition`). Unchanged
   mechanism: per-`(tenant, dashboard)` advisory lock, member fold under
   the lock, compare-and-swap of the memo. Since #2719 it returns a
   `_ClaimedTransition` for **both** directions instead of discarding the
   improving edge, carrying a prebuilt `DashboardNotice`.
2. **Routing** (`investigate._process_transition`). The worsening filter
   that used to live inside the claim now sits at this single site,
   unchanged in meaning: `worsening and non_green` → investigator. Every
   claimed edge → `schedule_dashboard_notification`.
3. **Threshold + build + send** (`notify_dashboard_transition`). Unset
   recipient short-circuits; then the rank rule; then one `send_email`.
4. **Outcome logging.** `checks_notify_sent` (info) on delivery;
   `checks_notify_failed` (warning, with the transport's stable reason
   code) on a `sent=False` result or an unexpected exception;
   `checks_notify_skipped_unconfigured` /
   `checks_notify_skipped_below_threshold` (info) on the two
   short-circuits. All four are at info or above because a claimed edge
   is a rare event and "no mail arrived" is a question the log has to be
   able to answer.

## Message shape

Subject: `[MEHO] <name>: <previous> -> <current>`, with an
`all clear - ` prefix when no member is non-green.

Body: the Dashboard name, the edge, then one block per non-green member
(name, effective state, last value, last evidence). Capped at
`_MAX_MEMBER_LINES` (20) members with an "N further member(s) not shown"
footer, and each untrusted field clipped at `_MAX_FIELD_CHARS` (200).

**Header safety.** A Dashboard name is operator-authored free text with
no newline constraint at the database, and the transport's in-process
entry point raises `ValueError` on a subject carrying a line break (only
the dispatched `mail.send` path gets the single-line parameter screen).
The subject therefore folds every control character out of the name, so
a crafted name cannot inject an SMTP header. The body is a MIME payload,
not headers, so its fragments need bounding rather than folding.

## Failure posture

Fire-and-forget in two senses:

- **Off the persist path.** The send runs as a tracked background task,
  because the transport bounds one SMTP session at 30 seconds and the
  check runner awaits `investigate_on_transition` inline in
  `_persist_outcome`. Strong references live in `_NOTIFICATIONS` so a
  task cannot be garbage-collected mid-flight;
  `_await_pending_notifications()` is the deterministic test drain.
- **Never raises.** A refusal, an unconfigured SMTP block, a delivery
  failure, or an unexpected exception is logged and swallowed.
  `asyncio.CancelledError` is the deliberate exemption — it propagates,
  so lifespan shutdown tears a tracked task down cleanly. A broken
  MTA cannot convert a committed transition claim into a persist-path
  failure — contract parity with `investigate_on_transition`.

Delivery and the claim are independent: the memo advances whether or not
the mail lands. That is deliberate — re-deriving "did anyone get told"
from the memo would mean re-mailing every unchanged evaluation.

## Dependencies

- `meho_backplane.connectors.mail.transport.send_email` — the only
  delivery path.
- `meho_backplane.checks.investigate` — imports this module (never the
  reverse) and owns the claim.
- `check_dashboards.notify_email` / `notify_min_state` — migration
  `0068`.

## Known issues / boundaries

- **No flap suppression.** One mail per *claimed* edge. A member Sensor
  going stale and back re-crosses `critical <-> unknown`, and each
  crossing is a real edge, so each mails. There is deliberately no rate
  limit, digest, or repeat-suppression window (#2716 scopes those out);
  the floor knob is the only volume control today. A Sensor that flaps
  at the runner cadence will mail at the runner cadence.
- **One recipient per Dashboard.** No lists, no routing rules, no
  per-Sensor recipients. More when a consumer asks.
- **Deployment-level SMTP.** One MTA and one allowlist for the whole
  backplane; there is no per-tenant mail config. A tenant admin can
  therefore name a recipient the allowlist refuses, and learns about it
  only from `checks_notify_failed` in the pod log.
- **No delivery record.** Nothing durable says a mail was sent; the
  structlog event is the whole trace. The `checks.transition` broadcast
  event (#2720) is the queryable record of the edge itself.

## References

- `backend/src/meho_backplane/checks/notify.py`
- `backend/src/meho_backplane/checks/investigate.py` —
  `_process_transition`, `_claim_dashboard_transition`,
  `_ClaimedTransition`, `_dashboard_notice`
- `backend/alembic/versions/0068_add_check_dashboard_notify.py`
- `backend/tests/test_checks_notify.py`,
  `backend/tests/migrations/test_migration_0068_add_check_dashboard_notify.py`
- `docs/codebase/connectors-mail.md`,
  `docs/codebase/checks-investigator.md`,
  `docs/codebase/checks-advisory.md`
- Alertmanager `send_resolved` (the empirical anchor for notifying on
  recovery):
  <https://prometheus.io/docs/alerting/latest/configuration/#email_config>
