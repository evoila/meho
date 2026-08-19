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
  stalled. A lock-miss tick logs `sensor_tick_lock_busy`, counts on
  `advisory_lock_busy_total{subsystem="sensor_runner"}`, and passes
  `lock_acquired=False` so it never stamps the claim facet (#3010).
- `note_tick_completed(now=None, *, lock_acquired=True)`
  (`checks/watchdog.py`) — stamps the per-process "last tick completed
  at" unconditionally and "last claim at" only when the tick held the
  lock; when a stall was flagged, emits the recovery log + events and
  re-arms the detector. Never raises.
- `evaluate_stall_watchdog(now=None)` — one watchdog check: quiet time
  past the threshold trips the stall edge (log + events, once per
  continuous stall) and returns `True` while stalled. Clock-injectable.
- `sensor_runner_liveness(now=None)` → `SensorRunnerLiveness` — the
  read-only view the health surface renders: `seconds_since_last_tick`,
  `seconds_since_last_claim`, `stalled`, `stall_threshold_seconds`.
  `stalled` is derived live from the stamp, **not** from the watchdog's
  emission latch, so a dead watchdog task cannot blind the facet.
- `start_checks_watchdog()` / `stop_checks_watchdog()` — lifespan pair
  (`main.py`, gated with the runner). Start sets the staleness baseline
  so a runner that never completes a single tick still trips.
- `reset_sensor_runner_state()` (`checks/runner.py`) also resets the
  watchdog module state; a conftest autouse fixture clears it per test.

## Control flow

```text
runner loop:  sleep(interval) → run_one_sensor_tick()
                                  ├─ claim + advance + spawn evaluations
                                  │    evaluation → _persist_outcome:
                                  │      record_sensor_result (commit gate)
                                  │      pending window open? →
                                  │        accelerate_sensor_next_fire
                                  │        (next_fire_at := min(next_fire_at,
                                  │         evaluated_at + retry_backoff_seconds))
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

### The advisory-lock invariant (#3010)

**Advisory lock and unlock must run on the same connection.**
`pg_try_advisory_lock` is session-level — owned by the DBAPI connection
it executes on, released only by `pg_advisory_unlock` *on that
connection* or by the connection closing; a transaction commit does not
release it. The tick body commits per claimed row, and an
`AsyncSession` returns its pooled connection on every commit — so a
lock taken on the work session strands on an idle pooled connection the
moment the first advance commits, the `finally` unlock lands on a
different connection (PG warns `you don't own a lock`, returns `false`,
nothing raises), and every later tick that draws any other connection
silently claims nothing. That was #3010: 75–118 evaluations/day from a
300 s sensor (expected 288) with the watchdog reading healthy.

The tick therefore hosts the lock on a dedicated pinned connection via
`meho_backplane/db/advisory.py::advisory_lock(key, subsystem=...)`
(one `engine.connect()` checked out for the whole locked region, work
session commits freely, unlock provably on the acquiring connection).
The same helper backs `scheduler/loop.py`, `events/drain.py`,
`agent/reaper.py`, and `gateway/deadman.py`; the topology scheduler
already had the correct dedicated-lock-session shape. Regression tests:
`backend/tests/integration/test_advisory_lock_pg.py` (real PG,
`pg_locks` probes, cross-connection-commit shape).

### Confirmation retries (#2799)

A sensor with `retry_times > 0` does not commit a differing evaluation
outcome on first sight: `record_sensor_result` holds it as a pending
soft state (`pending_state` / `pending_count` on the row) and commits
`last_state` / `state_since` only after `retry_times` consecutive
confirming re-evaluations (Nagios soft/hard states; symmetric — recovery
to `ok` is confirmed too, and `unknown` participates like any state).
The runner's contribution is the **accelerated re-check**: when a
persist leaves the window open, `_persist_outcome` pulls the row's
`next_fire_at` to `min(next_fire_at, evaluated_at +
retry_backoff_seconds)` (`accelerate_sensor_next_fire`, a conditional
`UPDATE ... WHERE next_fire_at > :accel` — atomic min, never a delay).
The re-check then rides the ordinary claim/advance/overlap machinery:
no in-process sleep loop, so `stop_sensor_runner` and the at-most-once
advance discipline are untouched, and effective spacing quantizes up to
the tick grid like any sub-tick cadence.

**Worst-case detection latency** for a genuine transition becomes
`first differing reading + retry_times × (retry_backoff_seconds + one
tick interval)` — each confirming re-check waits its backoff plus up to
one tick of grid quantization (the same fire-time contract shape #2245
states for the scheduler's 30 s grid).

**Satellite asymmetry:** the commit gate lives in
`record_sensor_result`, so satellite-gateway batch-posted results are
confirmation-counted identically — but the accelerated re-fire is
runner-local (`next_fire_at` is the central runner's clock; a satellite
re-checks at its own posting cadence, so remote confirmation is paced
by that cadence instead of `retry_backoff_seconds`).

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
| structlog `info` | `sensor_tick_lock_busy` | tick skipped: advisory lock held elsewhere (#3010; also counts on `advisory_lock_busy_total{subsystem="sensor_runner"}`) |
| structlog `warning` | `advisory_unlock_failed` | unlock returned `false` — the #3010 same-connection invariant regressed (tripwire, should never fire) |
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
`seconds_since_last_claim`, `stalled`, `stall_threshold_seconds`; the
whole field is `null` exactly when `SENSOR_RUNNER_ENABLED=false` (a
deliberately disabled runner must not read as stalled). The liveness
route carries it because the external prober ("how we monitor the
monitoring") is a `read_only` monitoring principal, and polling the
deep check instead would federate a Vault credential and write an audit
row per poll; the facet is an in-memory clock read, honouring that
route's no-connector constraint.

`seconds_since_last_claim` (#3010) measures from the last tick that
actually held the advisory lock (falling back to the watchdog-start
baseline). A fresh tick stamp with stale claim staleness is the
stranded-lock / permanent-contention signature the tick facet alone
cannot see. It is per-process: on a multi-replica deploy only the
lock-winning replica's claim advances, so `stalled` deliberately stays
keyed on the tick stamp and the prober alerts on the *minimum* claim
staleness across replicas.

The division of labour is the Prometheus `Watchdog` /
dead-man's-switch pattern: MEHO surfaces the liveness signal; the
external prober alerts on it (`stalled == true`,
`seconds_since_last_tick > stall_threshold_seconds`, or fleet-min
`seconds_since_last_claim` growing while ticks stay fresh).

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
- `backend/src/meho_backplane/db/advisory.py` (#3010 pinned-connection
  advisory-lock helper)
- `backend/src/meho_backplane/api/v1/health.py` (`SensorRunnerStatus`)
- `backend/src/meho_backplane/main.py` (lifespan wiring)
- `backend/tests/test_checks_watchdog.py`, `test_sensor_runner.py`,
  `test_api_v1_health.py`, `test_db_advisory.py`,
  `tests/integration/test_advisory_lock_pg.py`
- #2763 (watchdog), #2505 (runner), #2799 (confirmation retries),
  #3010 (advisory-lock leak root cause), Initiative #2780, parent
  goal #221
- Moulds: `gateway/deadman.py` (#2501 — the satellite-runner dead-man
  switch, same lapse-detection shape at a different altitude),
  `memory/expiry.py` (loop lifecycle)
- Prometheus Watchdog / dead-man's-switch:
  https://runbooks.prometheus-operator.dev/runbooks/general/watchdog/
- `docs/codebase/sensor.md`, `checks-broadcast.md`, `checks-advisory.md`
