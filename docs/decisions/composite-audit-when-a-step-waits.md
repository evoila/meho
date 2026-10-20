# A multi-step operation that stops part-way gets its own audit row (decision)

**Status:** proposed — a person decides by merging this file ([#4275](https://github.com/evoila/meho/issues/4275) asks a person to answer)
**Date:** 2026-10-20
**Issue:** [#4275](https://github.com/evoila/meho/issues/4275)
**Builds on:** [#4274](https://github.com/evoila/meho/issues/4274) (error rows), [#3733](https://github.com/evoila/meho/issues/3733) (kinds of audit rows), [#4217](https://github.com/evoila/meho/issues/4217) (continue after approval), [#2086](https://github.com/evoila/meho/issues/2086) (approval links)

Code paths are under `backend/src/meho_backplane/` unless they start with `backend/`, `cli/` or `docs/`. Line numbers are on `main` at `79caeff3`.

## The decision

A multi-step operation (in the code: a composite) runs several steps. It
can stop part-way: a step waits for approval, or a step is denied. From
now on MEHO writes one audit row for the operation when that happens.

- The row uses the operation's own audit id. The records that already
  point to that id then point to a row that exists.
- The row is an execution row (`operation_execution`): the operation ran.
- Its `result_status` says how it ended: `awaiting_approval` or `denied`.
- The decision about the step stays on the step's own decision row.

When the code is done, every operation that starts ends with exactly one saved
audit row, however it ends. #4274 did this for errors; this record does the two
other ways to stop.

**The rule:** a row's kind (`event_class`) says what happened to the operation
itself: `operation_execution` when its code ran, even partly, and `policy_decision`
when a check stopped it before its code ran. The `result_status` says how it ended.

## The five answers

**1. Does the operation get its own row? Yes, for both a wait and a deny.**

- MEHO makes the audit id and starts the trace (the flight-recorder record
  of vendor calls) before any step runs (`operations/dispatcher.py:764`, `:771`).
- That id is the parent of what the steps write (`operations/_branches.py:634`): rows of
  steps run through `dispatch_child`, the step's approval request row (`approval.request`,
  `operations/approval_queue.py:1001-1006`), and the deny row of a step refused before it
  could wait (`operations/composite.py:820`, #3542).
- Today no row has that id. The dispatcher passes a waiting or denied
  result straight through (`operations/dispatcher.py:1730`). So the trace
  route answers 404 (`api/v1/audit.py:432`), and the replay tree shows the
  steps as loose rows (`audit_query/replay.py:166`).
- Steps that call the vendor directly write no row of their own
  (`connectors/vmware_rest/composites/_write.py:1301-1317`, `docs/architecture/operations-substrate.md:322`).
  The operation's row is the only lasting record that these writes ran.
- MEHO's audit rule: every record that points to an audit id must point to a row that exists (postulate 7).

**2. What kind of row? An execution row. #3733 holds.**

- The operation was allowed and ran. Only a step was stopped. So its row is `operation_execution`.
- The step's decision has its own `policy_decision` row: the
  `approval.request` row for a wait (`operations/approval_queue.py:1274-1283`),
  and a `denied` row for a deny.
- One gap to close: when the policy check simply says no to a step, no step row
  is written today (`operations/composite.py:873`). Only a step refused before it
  could wait gets one (`:820`). The code writes this plain "no" through the same
  writer, `_audit_denied_and_return` (`operations/dispatcher.py:2761`).
- Only an execution row shows its trace (`api/v1/audit.py:436`,
  `ui/routes/audit/routes.py:432`). A decision row would hide the trace of
  the steps that ran.

**3. Which `result_status`? `awaiting_approval` for a wait, `denied` for a deny.**

- The row carries the result's own status, the one the caller got.
- `awaiting_approval` is new only as a written value. MEHO already maps it
  to the status code 202 (`operations/_errors.py:2556`), the code the
  `approval.request` row uses too (`operations/approval_queue.py:176`).
  `denied` maps to 403 (`operations/_errors.py:2558`).
- `payload.error` holds the result's `extras`, as on error rows (#2680):
  `error_code` and `approval_request_id` for a wait, `error_code` and
  `reason` for a deny. Nested there, it does not clash with
  `payload.approval_request_id`, which names the request a resumed run ran for (#3864).
- The row records the real time the run took, as #4274 does.

**4. How does the second run link to the first? Through the approval request. No new column.**

- After the approval, the second run gets a new audit id (`operations/dispatcher.py:764`).
- Its `parent_audit_id` (the column that points to the record that caused
  it) is the `approval.request` row. Both ways to continue set it: a
  person's approval (`operations/approval_queue.py:2045`) and an agent
  that waited (`agent/approval_wait.py:854`). A person's approval also adds
  `payload.approval_request_id` (`operations/approval_queue.py:1990`).
- The `approval.request` row's parent is the first run (`operations/approval_queue.py:1006`).
- So the chain is: second run → approval request → first run. Today the
  last link points to a missing row. #4275 makes that row exist.
- If the second run waits again (#4217), the rule repeats: it gets its own
  `awaiting_approval` row, and the new approval request points to it.

**5. What changes for the code that reads `result_status`? Two help texts.**

- Audit query filters (REST, console, MCP `query_audit`, CLI
  `meho audit query`): no new value. The query reads the status code, not
  `payload.result_status` (`audit_query/query.py:314-340`, `:405-425`). It
  shows 202 as `pending` and 403 as `denied`. Add
  `event_class=operation_execution` to leave out the decision rows.
- A new filter value would not fit anyway: the filter fields take at most
  16 characters (`api/v1/audit_models.py:84`, `ui/routes/audit/routes.py:707`,
  `mcp/tools/audit.py:163`), and `awaiting_approval` has 17.
- The CLI flag help (`cli/internal/cmd/audit/query.go:110`) and the MCP
  parameter text (`mcp/tools/audit.py:160`) list only `ok|error|denied`.
  They must name `pending` too. The console already offers it
  (`ui/routes/audit/routes.py:148`).
- In the audit log, `pending` means "this run stopped to wait", not "it
  still waits". The approval request shows the current state. The
  `approval.request` row of every wait already works this way.
- Trace read: no change. It never reads `result_status`. It serves the
  trace of any execution row (`api/v1/audit.py:436`). Each run has its own
  audit id and its own trace, so a trace has no "not finished" state.
- The row's live broadcast event carries the same value. The console feed
  shows an unknown value as text (`ui/templates/broadcast/_event_row.html:110`).

## What stays the same

- **#3733:** approval and deny records are decision rows, never execution
  rows. `approval.request` stays `policy_decision`; approve, reject and
  expiry stay `approval_decision`.
- **An operation stopped before its code runs** (it waits or is denied at
  its own check) keeps only its decision row. It has no trace and no run id.
- **A handler that handles the stopped step itself** and returns its own
  result keeps today's row for that result (`ok`, or `error` under #3809).
- **The caller's result:** the same as before, plus the `audit_id` of the
  new row, as #4274 does. The approval and resume flow (#3351, #4217).

## Other ways we did not choose

- **A decision row for the operation.** It would fill the missing id. But
  the operation was not refused; it ran. Its trace would be hidden, and
  the writes that ran would be missing from the execution rows.
- **A new column from the second run to the first.** The link already
  exists through the approval request. A second path for the same fact
  can disagree with the first.
- **`error` for the deny case**, as #4274 does for errors. The row would
  disagree with the caller's result (`denied`), and the `error` filter
  would mix refusals with real failures.

## Acceptance criteria for the code of #4275

- [ ] A multi-step operation whose step waits for approval leaves exactly one row under its own audit id: `event_class="operation_execution"`, `payload.result_status="awaiting_approval"`, `status_code=202`, and `payload.error.approval_request_id` equal to the id in the result.
- [ ] That step's `approval.request` row, and the rows of steps that ran before it, have `parent_audit_id` equal to that row's id.
- [ ] A step the policy check says no to leaves two rows: a `policy_decision` row for the step (`result_status="denied"`, parent = the operation's audit id), and an `operation_execution` row for the operation (`result_status="denied"`, `status_code=403`). A step refused before it could wait (#3542) keeps its step row and gets the same operation row.
- [ ] The caller gets the same result as before, plus `audit_id` equal to the new row's id. The row records the real elapsed time.
- [ ] With trace capture on, `GET /api/v1/audit/{id}/trace` for that id answers 200 with `trace_present=true`, not 404.
- [ ] After approval, the second run's row has a new audit id and `parent_audit_id` equal to the `approval.request` row's id, on the person's path and on the agent's path. If the second run waits again, it gets its own `awaiting_approval` row.
- [ ] `POST /api/v1/audit/query` with `result_status="pending"` and `event_class="operation_execution"` returns the waiting run's row and not the `approval.request` row.
- [ ] `meho audit query --help` and the `query_audit` parameter text name `pending`.
- [ ] The #4274 test `test_handler_returned_non_error_result_is_unchanged` (`backend/tests/test_operations_audit_error_envelope.py`) is replaced by tests for the new rows. They go through the real dispatcher and fail without the change.
- [ ] `docs/codebase/audit_query.md` (the class table and the #4274 section) and `docs/architecture/operations-substrate.md:348` (which says the result passes straight through) describe the new rows.
