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

## The token rule holds on every path

The check call always follows the token rule of
[approvals.md](approvals.md), also when the run is advanced from the web
console. `_resolve_verify_response` calls the operation with
`caller.model_copy(update={"web_session": False})`. It keeps the caller's
kind, client id, roles, session id and token. So:

- for a person, only a `safe` check runs at once. A `caution` or higher
  check waits for approval, and the step fails with `awaiting_approval`;
- an agent and a service login follow their own rules (grants, read
  scope, standing grants), as on a direct call;
- a `credential_read` check never runs at once, for any caller.

## A check that waits for approval

When the check call waits for approval, the step fails, and MEHO keeps the
id of the approval request in the step's `verify_response.actual`
(`approval_request_id`). An approver approves the request, and the
approved call runs. Then the assignee retries the step
(`meho runbook next --retry`, or **Try this step again** in the console).
On the retry, MEHO reads the linked request
(`runbooks/check_approval.py`) and does not run the call again:

- the request still waits, or the approved call has not finished: the
  step waits again with the same request. MEHO makes no new request;
- the request is approved and its call ran: the check uses the stored
  result of that call (`resume_result`) and compares it with `expect`;
- the request was rejected, expired or withdrawn: the check runs the call
  again, so it asks for approval again.

Only the caller who made the check wait reads the request, in the same
tenant (`read_approval_result`). The read writes an `approval.result`
audit row. An approved result is used once: the next retry of the same
step runs the call again.

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
- `backend/src/meho_backplane/runbooks/check_approval.py`
- `backend/src/meho_backplane/ui/routes/runbooks/driver.py`
- `backend/tests/test_runbook_step_caller_identity.py`
- `backend/tests/test_runbook_check_approval.py`
