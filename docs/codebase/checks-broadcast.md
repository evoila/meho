# Check events and Broadcast (retired publisher)

## Overview

Checks no longer publish anything to Broadcast (#3758). Broadcast now
carries deliberate agent announcements only (see
[`broadcast.md`](broadcast.md)).

Before #3758, `checks/broadcast.py` published a `checks.transition` event
for every claimed Dashboard rollup edge (#2720), and the evaluation-loop
watchdog published `checks.scheduler_stalled` /
`checks.scheduler_recovered` per tenant (#2763). That module is deleted.

## Where check signals go now

A claimed Dashboard transition (the compare-and-swap on
`check_dashboards.last_rollup_state`) has two consumers:

- the diagnose-only investigator, for worsening edges
  ([`checks-investigator.md`](checks-investigator.md));
- the notifier, for both directions above the configured floor
  ([`checks-notifications.md`](checks-notifications.md)).

The Dashboard state itself (`/ui/checks`, the checks API) is the record
of the current state.

The watchdog still detects a stalled evaluation loop. It writes the
structured logs `checks_scheduler_stalled` and
`checks_scheduler_recovered`, and the health facet reports the
staleness ([`checks-runner.md`](checks-runner.md)). It no longer looks up
tenants with active Sensors, because there is no per-tenant event to
send.

## What stayed

- `classify_op` still maps the three op-ids above to the `checks` class
  (`_CHECK_EVENT_OPS` in `broadcast/events.py`), so older stream entries
  and stored op-ids classify the same way. The `/api/v1/checks/*` gateway
  op-ids are still kept out of that class by exact membership.
- `checks` stays in `OP_CLASS_ENUM`. The broadcast filter vocabulary and
  its UI affordances are retired separately (#3762).

## References

- `backend/src/meho_backplane/checks/investigate.py` — `_process_transition`
- `backend/src/meho_backplane/checks/watchdog.py`
- `backend/src/meho_backplane/broadcast/events.py` — `_CHECK_EVENT_OPS`
- `backend/tests/test_checks_watchdog.py`,
  `backend/tests/test_broadcast_agent_intent_cutover.py`
