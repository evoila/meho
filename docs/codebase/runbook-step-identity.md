# Runbook step identity

## Overview

A runbook step with an `operation_call` check runs an operation. MEHO runs
that operation with the identity of the caller who advances the run. There
is no stand-in identity.

## Which identity a step runs as

- Only the run's assignee (`runbook_runs.assigned_to`) may advance a run.
  The advance comes in one request: `meho_runbook_next` (MCP),
  `POST /api/v1/runbooks/runs/{run_id}/next` (REST and CLI), or the
  Advance button in the web console.
- The checked login of that request goes to
  `RunbookRunService.next_step` as `caller`. The check call
  (`call_operation`) runs as this caller: its principal kind (person,
  agent or service login), client id, role and token.
- So the same rules apply as for a direct call of the operation:
  - an agent is checked against its grants (`AgentPermission`), also the
    `AGENT_GRANT_REQUIRED` switch;
  - a service login is checked against its read scope and its standing
    grants;
  - a person is checked against the role floor of the operation;
  - a target whose login comes from Vault per call uses the caller's
    token.
- The audit row of the check, and any approval request the check makes,
  name the real caller: `operator_sub`, `principal_name` (for an agent:
  its registered name) and the `client` block (`principal_kind`, `azp`).
- `caller` must be the assignee (`caller.sub == operator_sub`, same
  tenant). Else MEHO refuses with `NotRunAssigneeError` before anything
  runs.
- Without `caller`, a step with an `operation_call` check is refused with
  `StepCallerRequiredError`. The step state does not change, and the
  caller can try again. The web console passes the person's login only for
  a step whose check runs an operation. When it cannot check the login at
  that moment, it shows the refusal as an inline alert.
- A `confirm` check runs no operation, so it needs no `caller`.

## When the check call does not return `ok`

When the call is refused (`denied`), fails (`error`), or waits for
approval (`awaiting_approval`), the step fails, whatever its `expect`
says. The `PreviousStepFailedError` text names the reason, for example
`step 'check' check call did not run: denied: ...`. REST and MCP return
this text as the error. The console shows it as an inline alert above the
failed-step banner. The step's
`verify_response.actual` holds `status` and `error` of the call. The
assignee can fix the cause and retry the step
([runbook-step-retry.md](runbook-step-retry.md)).

## References

- `backend/src/meho_backplane/runbooks/run_service.py`
- `backend/src/meho_backplane/ui/routes/runbooks/driver.py`
- `backend/tests/test_runbook_step_caller_identity.py`
