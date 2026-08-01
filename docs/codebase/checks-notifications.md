# Checks operator email (`checks/notify.py`)

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

Since #2721 this module renders a **second notice kind**: the
investigator's finding, once the run is terminal and its
noise-suppression policy is persisted. Both kinds share one task set, one
never-raise posture, and one header-safety fold, so the delivery contract
is stated once. The distinction that matters is *who sends*: the finding
mail is sent by this deterministic wrapper, never by the agent — the
investigator has no mail tool and no agent toolset exposes one. An agent
that should send ad-hoc mail does so through `call_operation` on the
`mail.*` connector under normal policy, which is a different seam.

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
- `notify_finding(notice)` / `schedule_finding_notification(notice)`
  (#2721) — the finding-mail twins of the two functions above, with the
  same never-raise and fire-and-forget contracts.
- `FindingNotice` — the detached finding: dashboard id + name, `run_id`,
  verdict, summary, evidence, recommended action, and the resolved
  recipient. A notifier-owned projection of `ChecksFinding` for the same
  reason `NotifyMember` is one — importing `investigate` would be a
  cycle.

## Configuration (per Dashboard, set at create only)

Two columns on `check_dashboards`, added by migration `0068`
(`investigator_prompt`, migration `0069`, belongs to the briefing, not to
delivery — see `docs/codebase/checks-investigator.md`):

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
3. **Threshold + flap window + build + send**
   (`notify_dashboard_transition`). Unset recipient short-circuits; a
   recovery crossing clears the Dashboard's flap windows; then the rank
   rule; then the #2732 suppression claim (see below); then one
   `send_email`.
4. **Outcome logging.** `checks_notify_sent` (info) on delivery;
   `checks_notify_failed` (warning, with the transport's stable reason
   code) on a `sent=False` result or an unexpected exception;
   `checks_notify_skipped_unconfigured` /
   `checks_notify_skipped_below_threshold` /
   `checks_notify_suppressed` (info) on the three short-circuits;
   `checks_notify_suppression_failed` (warning, `phase` = `claim` or
   `clear`) when Valkey misbehaves and the fail-open path runs. All are
   at info or above because a claimed edge is a rare event and "no mail
   arrived" is a question the log has to be able to answer.

## Flap suppression (#2732)

A member Sensor whose evaluation stops updating derives `unknown`
(stale grace, `checks/rollup.py`) and returns to its real state when it
evaluates again, so a Dashboard sitting at `critical` re-crosses
`critical <-> unknown` — each crossing a genuine claimed edge. Since
#2732 delivery (not the claim) is bounded by a per-`(tenant, dashboard,
state)` window:

- **Mechanism.** The first crossing into a non-green state claims a
  Valkey key — `meho:checks:notify:<tenant>:<dashboard>:<state>`, one
  atomic `SET NX EX`, the #2718 advisory's idiom (`checks/advisory.py`)
  minus the caller segment (the audience is the Dashboard's one
  configured recipient, not whoever dispatches). Repeat crossings into
  the **same** state inside the window lose the claim and log
  `checks_notify_suppressed`. The Valkey TTL key *is* the state —
  nothing durable; a Valkey flush re-arms every window.
- **Knob.** `CHECKS_NOTIFY_SUPPRESSION_WINDOW_MINUTES`, default 30.
  `0` disables suppression entirely (one mail per claimed edge, the
  pre-#2732 behaviour) and short-circuits before any Valkey call.
  Deployment-level, like the SMTP block — not per Dashboard.
- **Escalation is never suppressed.** A different state is a different
  key: `degraded → critical` mails immediately after a recent
  `degraded` notice.
- **Recovery is never suppressed, and it resets the windows.** A
  crossing into a rank-0 state (`ok`/`skip`) is exempt from the claim,
  and it `DEL`s the Dashboard's three suppressible-state keys — even
  when the recovery edge itself is below the floor and sends nothing
  (`degraded → ok` at the default `critical` floor). The incident after
  an all-clear is a new incident; its first crossing always mails.
- **Fail-open.** Any Valkey error on claim or clear warn-logs
  `checks_notify_suppression_failed` and the notification is sent. A
  missed alert is worse than a duplicate.
- **Attempt-based.** The key is claimed *before* the send (atomic
  check-and-claim; claiming after would race a slow SMTP session
  against the next flap edge), so a send that fails inside a window is
  not retried by the next same-state crossing — the #2719
  one-attempt-per-claimed-transition contract, per window. The failure
  is warn-logged either way.
- **Delivery only.** The memo compare-and-swap, the `checks.transition`
  broadcast event (#2720), and the investigator's fire gate see every
  edge, suppressed or not. Finding mail is not flap-suppressed — its
  volume control is the investigator's fire gate.

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

### The finding mail (#2721)

Subject: `[MEHO] investigation <verdict>: <name>` — verdict first,
because that is the triage bit a filter rule routes on.

Body: the Dashboard, the verdict, the `run_id`, then `## Summary`,
`## Evidence`, and `## Recommended action`. Deliberately the same section
order as the memory entry `_render_finding_body` writes for the same
finding, so the mail and the memory entry read as one shape, and the
`run_id` is carried so the recipient can pull the full durable run.

The recommended action rides along **only for an `actionable` verdict** —
the same gate the memory entry applies — and carries the diagnose-only
disclaimer with it. Bounds differ from the transition mail because the
inputs differ: `ChecksFinding.summary` is model output with no schema
length, so summary and action are truncated at `_MAX_FINDING_CHARS`
(4000, large enough that the diagnosis survives) and evidence is capped
at `_MAX_EVIDENCE_LINES` (20) lines each clipped at `_MAX_FIELD_CHARS`.

Gating: the finding mail fires on `notify_email` alone. There is
deliberately **no** `notify_min_state` check — that floor is defined over
a transition *edge* (`max(rank(previous), rank(current))`) and a finding
is not an edge. The volume control is the investigator's own fire gate,
already far narrower than any state floor: a finding exists only when the
edge worsened into a non-green state with an actively-failing member, the
correlated cause was novel, no run for it was in flight, and the budget
gate admitted it.

#2721's review flagged the operator surprise in this and #2732 re-put
the question; the decision is to **keep the no-floor shape**, and this
paragraph is its record. The surprise, stated plainly: an operator on
the default `critical` floor can receive finding mail about a
`degraded` Dashboard whose transition mail was silent. That is
intended — the floor tunes *paging* volume on a per-edge signal that
can fire often, while a finding is a rare, budget-gated diagnosis that
exists precisely because something worsened; a recipient who configured
`notify_email` and an investigator wants the diagnosis. Gating it on
the floor would silently discard completed investigations, and there is
no separate per-Dashboard knob because no consumer has asked for one.
For the same fire-gate reason, findings are exempt from the #2732 flap
window (`test_finding_mail_has_no_state_floor_and_no_flap_window` pins
both exemptions).

## Failure posture

Fire-and-forget in two senses:

- **Off the caller's path.** The send runs as a tracked background task,
  because the transport bounds one SMTP session at 30 seconds. For a
  transition that keeps it off the check runner's persist path (which
  awaits `investigate_on_transition` inline in `_persist_outcome`); for a
  finding it keeps it out of the investigator's *serial* cause-group loop,
  where an inline session would delay the next group's diagnosis, not just
  its mail. Strong references live in `_NOTIFICATIONS` (one set, both
  kinds) so a task cannot be garbage-collected mid-flight;
  `_await_pending_notifications()` is the deterministic test drain for
  both.
- **Never raises.** A refusal, an unconfigured SMTP block, a delivery
  failure, or an unexpected exception is logged and swallowed.
  `asyncio.CancelledError` is the deliberate exemption — it propagates,
  so lifespan shutdown tears a tracked task down cleanly. A broken
  MTA cannot convert a committed transition claim into a persist-path
  failure — contract parity with `investigate_on_transition` — and a
  broken MTA cannot cost the tenant the noise-suppression policy write
  the finding mail is ordered after.

Delivery and the claim are independent: the memo advances whether or not
the mail lands. That is deliberate — re-deriving "did anyone get told"
from the memo would mean re-mailing every unchanged evaluation.

## Dependencies

- `meho_backplane.connectors.mail.transport.send_email` — the only
  delivery path.
- `meho_backplane.checks.investigate` — imports this module (never the
  reverse) and owns the claim.
- `check_dashboards.notify_email` / `notify_min_state` — migration
  `0068`. The finding mail reuses `notify_email` as its recipient (no
  separate column).

## Known issues / boundaries

- **Suppression bounds repeats, not distinct states.** A Dashboard
  cycling through *N* distinct non-green states mails once per state
  per window — the window is per-`(dashboard, state)`, not
  per-dashboard. Digests, batching, and grouping several Dashboards
  into one mail remain out of scope (#2716).
- **The window is attempt-based and ephemeral.** A send that fails
  inside a window is not retried by the next same-state crossing, and a
  Valkey flush (or failover to an empty replica) re-arms every window —
  worst case one extra mail per state, never a dropped one.
- **One recipient per Dashboard.** No lists, no routing rules, no
  per-Sensor recipients. More when a consumer asks.
- **Deployment-level SMTP.** One MTA and one allowlist for the whole
  backplane; there is no per-tenant mail config. A tenant admin can
  therefore name a recipient the allowlist refuses, and learns about it
  only from `checks_notify_failed` in the pod log.
- **No delivery record.** Nothing durable says a mail was sent; the
  structlog event is the whole trace (`checks_notify_sent` /
  `checks_finding_email_sent`). The `checks.transition` broadcast event
  (#2720) is the queryable record of the edge itself, and the `agent_run`
  row is the durable record of the finding.
- **Two mails per incident when both are configured.** A Dashboard with a
  `notify_email` and an enabled investigator mails the transition
  immediately and the finding minutes later, when the run terminates.
  That is the intended shape — the page and the diagnosis are different
  messages with different latencies — but there is no threading or
  correlation header tying them together today.

## References

- `backend/src/meho_backplane/checks/notify.py` —
  `_claim_suppression` / `_clear_suppression` / `_suppression_key` are
  the #2732 flap-window seam
- `backend/src/meho_backplane/checks/investigate.py` —
  `_process_transition`, `_claim_dashboard_transition`,
  `_ClaimedTransition`, `_dashboard_notice`, `_finding_notice`
- `backend/src/meho_backplane/checks/advisory.py` — the `SET NX EX`
  claim precedent the flap window follows
- `backend/alembic/versions/0068_add_check_dashboard_notify.py`
- `backend/tests/test_checks_notify.py`,
  `backend/tests/test_checks_investigate.py` (the finding mail through
  the investigation seam),
  `backend/tests/migrations/test_migration_0068_add_check_dashboard_notify.py`
- `docs/codebase/connectors-mail.md`,
  `docs/codebase/checks-investigator.md`,
  `docs/codebase/checks-advisory.md`
- Alertmanager `send_resolved` (the empirical anchor for notifying on
  recovery):
  <https://prometheus.io/docs/alerting/latest/configuration/#email_config>
