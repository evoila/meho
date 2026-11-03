# Pod stop and drain (#5024)

## Overview

When Kubernetes stops a MEHO pod (an update, a node drain, a scale-down),
the pod drains before it ends:

- it takes no new work,
- it lets running operations finish, up to a time limit,
- it records every operation that did not finish, so nothing is lost
  without a trace.

Together with the chart's `Recreate` update strategy (the old pod stops
before the new one starts, [devops.md](devops.md#update-strategy-and-pod-stop-5024))
this makes an update on a one-pod install safe: old and new code never
work on the database at the same time. The cost is about one to two
minutes without MEHO per update.

Code: `backend/src/meho_backplane/operations/shutdown_drain.py`.
Tests: `backend/tests/test_shutdown_drain.py`,
`backend/tests/test_chart_safe_updates.py`.

## Control flow

1. **Pre-stop wait.** Kubernetes runs the container's `preStop` hook
   (`sleep`, `shutdown.preStopSleepSeconds`, default 5). The pod still
   serves. In this time the Service and the ingress remove the pod, so no
   new request reaches it after it closes.
2. **Stop signal.** Kubernetes sends `SIGTERM`. uvicorn handles the signal.
   At start-up the lifespan put a small handler in front of uvicorn's
   (`install_stop_signal_hook`). It calls `begin_drain()` and then
   uvicorn's own handler. From now on:
   - `/ready` answers 503 with `{"status": "draining", "checks": []}`;
   - `dispatch()` refuses a new top-level call with the error code
     `meho_shutting_down` (`execution_outcome: not_started`,
     `retry: true`). Nothing was sent to a target;
   - an approved request is not started (`resume_dispatch_after_approval`
     returns the same error before it takes the run claim, so the claim
     stays free);
   - the sensor runner claims no sensor and the scheduler fires no trigger.

   Not refused, because it is part of work that already runs: a step of a
   running multi-step operation (`composite_depth_var > 0`), and an
   approved run whose claim was already taken (`_approved=True`).
3. **Open requests.** uvicorn closes its listener and waits for the open
   HTTP requests, up to `UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN`. At that limit
   it cancels them.
4. **Background operations.** The lifespan shutdown
   (`_drain_running_operations` in `main.py`) waits for the operations
   that run outside a request (async runs, sensor checks, agent runs)
   until `SHUTDOWN_DRAIN_SECONDS` after the stop signal.
5. **Record what did not finish.** Every top-level call is kept in a list
   while it runs (`track_operation`). A call that uvicorn cancelled during
   the drain, and every call still in the list at the deadline, gets:
   - a log line `operation_unfinished_at_shutdown` (op, connector, caller,
     tenant, request id, start time);
   - an audit row: `method=DISPATCH`, `path=<op_id>`, `status_code=503`,
     payload `result_status: unfinished`, `execution_outcome: uncertain`,
     `retry: false`, `started_at`, `stopped_at` and a plain reason.

   The audit write has a 5-second limit and never raises; the log lines
   come first.
6. **Stop the loops.** The background loops stop as before, which cancels
   what still runs. Then the pools close.

The chart sets both limits from `shutdown.drainSeconds` (default 60) and
the grace period to `preStopSleepSeconds + drainSeconds + 30`, so the
kubelet does not kill the pod before step 5.

## What the start-up recovery finds

MEHO never runs an unfinished operation again by itself: it may have
reached the target. The lasting records stay as they are:

- an approved request keeps its started claim (`resumed_at`) and has no
  result;
- an async run stays `running`; when its lease ends, the reaper of the
  next pod ends it ([async-governed-dispatch.md](async-governed-dispatch.md));
- the audit row above marks every cut-off call, also a plain call that has
  no other lasting record.

## Key types

- `RunningOperation`: one running top-level call (op, connector, caller,
  tenant, request id, start time, approved run or not).
- `SHUTTING_DOWN_ERROR_CODE` = `meho_shutting_down`.
- Setting `shutdown_drain_seconds` (`SHUTDOWN_DRAIN_SECONDS`, default 60).

## Known issues

- The drain state is per process. An install with several pods needs a
  lease for each running operation (a time-limited claim that shows the
  run is still alive) before the start-up recovery can tell a dead run
  from one that runs on another pod.
- An open streaming request (for example the console's live broadcast
  feed) keeps uvicorn waiting until its limit. The update then takes the
  full drain time.
- The signal handler is installed only in the main thread. Under a server
  that runs the app in another thread, the drain starts when the lifespan
  shutdown starts.

## References

- [Rollback contract](../acceptance/rollback.md): what an update and a
  rollback do, and the recovery steps after a failed start.
- uvicorn: `--timeout-graceful-shutdown`, read from the environment as
  `UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN` (uvicorn 0.52, `auto_envvar_prefix`).
- Kubernetes: [Pod termination](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination),
  [Deployment strategy](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#strategy).
