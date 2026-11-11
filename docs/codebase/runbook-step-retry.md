# Runbook step retry

## Overview

A runbook run keeps one state per step: pending, in progress, verified or
failed. A failed step used to be a dead end: the only way on was to abort
the run. Now the assignee can fix the cause and try the step again.

## How it works

- `NextStepRequest` has an optional `retry` field (default `false`). It is
  used by `meho_runbook_next`, `POST /api/v1/runbooks/runs/{run_id}/next`,
  `meho runbook next --retry` and the "Try this step again" button in the
  web interface. No new MCP tool was added.
- Without `retry`, a failed step refuses to advance, as before
  (`PreviousStepFailedError`, HTTP 400).
- With `retry` on a failed step, `RunbookRunService.next_step` runs the
  check again: the verify operation call, or the new answer for a
  `confirm` step. If it passes, the step becomes `verified` and the run
  goes on. If it fails again, the step stays `failed`.
- On a step that is not failed, `retry` has no effect.
- The same rules apply as for any advance: only the assignee may retry,
  and a finished or aborted run cannot be retried. An operation call
  writes its usual audit row for every attempt. A `confirm` step writes an
  audit row (`runbook.confirm`) for every attempt, pass or fail, in the
  same transaction as the step change. A failed confirm plus a retry
  leaves two rows. Each row carries the session it ran in
  (`agent_session_id`: the agent run, or the MCP session), so session
  replay shows every attempt.
- The write locks the run row (`SELECT ... FOR UPDATE`). Two calls on the
  same run at the same time write one after the other. The second one reads
  what the first one wrote: it adds its attempt to the list, or refuses
  when the first one already passed the step.

## History

`runbook_run_step_states.attempts` (JSON, migration 0158) lists every check
attempt, oldest first: `at`, `passed` and `verify_response`. A retry adds
an entry. No entry is replaced. `verify_response` still holds the latest
result.

A step that failed before the upgrade has no list yet (NULL). Its failed
`verify_response` becomes the first entry on the first retry, with
`at: null` because the time of that attempt is not known.

## References

- `backend/src/meho_backplane/runbooks/run_service.py`
- `backend/src/meho_backplane/ui/templates/runbooks/_run_step.html`
- `docs/codebase/mcp.md` (tool inventory)
