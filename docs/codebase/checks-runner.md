# Sensor check-runner + evaluation-loop watchdog

## Overview

Two lifespan-owned background tasks drive the deterministic check layer's
evaluation plane:

- **The runner** (`checks/runner.py`, #2505) — a sleep-then-tick loop
  (`SENSOR_RUNNER_TICK_INTERVAL_SECONDS`, default 10 s) that claims due
  `Sensor` rows under a process-wide advisory lock, advances each row's
  `next_fire_at` before dispatch, and spawns bounded background
  evaluations (dispatch → assertion → latest-state projection). No LLM
  anywhere on this path.
- **The watchdog** (`checks/watchdog.py`, #2763) — a sibling task that
  detects the runner loop going quiet and makes that outage observable.
  It exists because a stalled evaluation loop is the one outage the
  checks plane cannot alert on: no evaluations → no transitions → no
  notifications (the v0.26.0 37-minute fleet-wide silent stall).

Both start and stop together behind `SENSOR_RUNNER_ENABLED`. The
watchdog deliberately has no enable flag of its own.

## Key types

- `run_one_sensor_tick()` (`checks/runner.py`) — one deterministic tick;
  public test seam. Every *completed* tick (including the
  advisory-lock-not-acquired no-op) calls `note_tick_completed()`; a
  tick that raises does not — a loop failing every tick must read as
  stalled.
- `note_tick_completed(now=None)` (`checks/watchdog.py`) — stamps the
  per-process "last tick completed at"; when a stall was flagged, emits
  the recovery log + events and re-arms the detector. Never raises.
- `evaluate_stall_watchdog(now=None)` — one watchdog check: quiet time
  past the threshold trips the stall edge (log + events, once per
  continuous stall) and returns `True` while stalled. Clock-injectable.
- `sensor_runner_liveness(now=None)` → `SensorRunnerLiveness` — the
  read-only view the health surface renders: `seconds_since_last_tick`,
  `stalled`, `stall_threshold_seconds`. `stalled` is derived live from
  the stamp, **not** from the watchdog's emission latch, so a dead
  watchdog task cannot blind the facet.
- `start_checks_watchdog()` / `stop_checks_watchdog()` — lifespan pair
  (`main.py`, gated with the runner). Start sets the staleness baseline
  so a runner that never completes a single tick still trips.
- `reset_sensor_runner_state()` (`checks/runner.py`) also resets the
  watchdog module state; a conftest autouse fixture clears it per test.

## Control flow

```text
runner loop:  sleep(interval) → run_one_sensor_tick()
                                  ├─ claim + advance + spawn evaluations
                                  └─ note_tick_completed()
                                       └─ stall flagged? → emit
                                          checks_scheduler_recovered
                                          (log + event, stall duration)

watchdog loop: sleep(interval) → evaluate_stall_watchdog()
                 quiet ≤ N × interval → healthy, return False
                 quiet > N × interval, first time →
                     error-log checks_scheduler_stalled
                     fan checks.scheduler_stalled event out to every
                       tenant with ≥1 active Sensor (set captured for
                       the recovery pair)
                 already flagged → True, no re-emission

health read:   GET /api/v1/health · GET /api/v1/health/live
                 sensor_runner_enabled=false → "sensor_runner": null
                 else → live-derived SensorRunnerStatus
```

Detection latency is bounded by `threshold + one tick interval` (the
watchdog rides the runner's own cadence; a finer grid would not tighten
that bound).

## Settings

- `SENSOR_RUNNER_ENABLED` (default `true`) — gates runner **and**
  watchdog.
- `SENSOR_RUNNER_TICK_INTERVAL_SECONDS` (default 10) — both loops'
  cadence.
- `SENSOR_RUNNER_STALL_AFTER_TICKS` (default 6, min 2) — the stall
  threshold is this × the tick interval (60 s by default). Scales with
  the tick grid; the minimum of 2 keeps the threshold above the ordinary
  sleep-then-tick gap.

## Event kinds and log lines

| Surface | Name | When |
|---|---|---|
| structlog `error` | `checks_scheduler_stalled` | stall detection edge |
| structlog `warning` | `checks_scheduler_recovered` | first completed tick after a stall (carries `stalled_for_seconds`) |
| structlog `warning` | `checks_watchdog_emit_failed` | fan-out query/publish block failed (fail-open) |
| broadcast event | `checks.scheduler_stalled` | detection edge, per affected tenant |
| broadcast event | `checks.scheduler_recovered` | recovery, to the same tenants |

Both event op-ids are pinned in `broadcast/events.py::_CHECK_EVENT_OPS`
(exact membership, never a `checks.` prefix match) and classify as
op-class `checks`, so `broadcast_recent` / `broadcast_watch` /
`/ui/broadcast/stream?op_class=checks` all see them. They ride the
`publish_check_transition_event` mould: principal `__checks__`,
nil-UUID `audit_id` (a quiet loop is a non-operation — there is no
audit row to point at), fail-open publish
(`checks_scheduler_broadcast_failed`). See
`docs/codebase/checks-broadcast.md`.

## The health facet

`GET /api/v1/health` and `GET /api/v1/health/live` both carry
`sensor_runner` (`SensorRunnerStatus`): `seconds_since_last_tick`,
`stalled`, `stall_threshold_seconds`; the whole field is `null` exactly
when `SENSOR_RUNNER_ENABLED=false` (a deliberately disabled runner must
not read as stalled). The liveness route carries it because the
external prober ("how we monitor the monitoring") is a `read_only`
monitoring principal, and polling the deep check instead would federate
a Vault credential and write an audit row per poll; the facet is an
in-memory clock read, honouring that route's no-connector constraint.

The division of labour is the Prometheus `Watchdog` /
dead-man's-switch pattern: MEHO surfaces the liveness signal; the
external prober alerts on it (`stalled == true`, or
`seconds_since_last_tick > stall_threshold_seconds`).

## Known issues / boundaries

- **The stamp is per-process, deliberately.** No fleet-level persisted
  stamp: it would cost a DB write per tick and mask a single wedged
  replica behind healthy siblings. Each replica's health facet answers
  for its own loop — which is the observed failure mode (one process's
  loop coroutine going quiet). Fleet-level aggregation belongs to the
  external prober hitting each replica.
- **Whole-event-loop starvation takes the watchdog with it.** The
  watchdog catches the runner coroutine going quiet while the process
  stays otherwise alive (the reported shape — pod healthy, zero
  restarts). If the entire event loop wedges, the health facet goes
  stale/unreachable and the external prober is the detector.
- **No `/ready` probe registration, deliberately.** Flipping readiness
  on a monitoring-plane stall would pull the API pod out of the load
  balancer — wrong blast radius. The facet is informational; reaction
  policy belongs to the prober.
- **Recovery events go to the tenant set captured at detection.** A
  tenant whose first sensor was created mid-stall receives neither the
  stalled nor the recovered event of that episode.
- **A stall costs notification latency, not notifications.** On resume,
  the first evaluations claim their CAS transition edges normally and
  the notifier fires; the recovery event carries the stall window for
  forensics (#2763 scoped retro-notification out).

## References

- `backend/src/meho_backplane/checks/runner.py`,
  `backend/src/meho_backplane/checks/watchdog.py`,
  `backend/src/meho_backplane/checks/broadcast.py`
- `backend/src/meho_backplane/api/v1/health.py` (`SensorRunnerStatus`)
- `backend/src/meho_backplane/main.py` (lifespan wiring)
- `backend/tests/test_checks_watchdog.py`, `test_sensor_runner.py`,
  `test_api_v1_health.py`
- #2763 (watchdog), #2505 (runner), Initiative #2780, parent goal #221
- Moulds: `gateway/deadman.py` (#2501 — the satellite-runner dead-man
  switch, same lapse-detection shape at a different altitude),
  `memory/expiry.py` (loop lifecycle)
- Prometheus Watchdog / dead-man's-switch:
  https://runbooks.prometheus-operator.dev/runbooks/general/watchdog/
- `docs/codebase/sensor.md`, `checks-broadcast.md`, `checks-advisory.md`
