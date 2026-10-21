# A multi-step operation that stops part-way gets its own audit row (decision)

**Status:** proposed — a person decides by merging this file ([#4275](https://github.com/evoila/meho/issues/4275) asks a person to answer)
**Date:** 2026-10-20
**Issue:** [#4275](https://github.com/evoila/meho/issues/4275)
**Builds on:** [#4274](https://github.com/evoila/meho/issues/4274) (error rows), [#3733](https://github.com/evoila/meho/issues/3733) (kinds of audit rows), [#4217](https://github.com/evoila/meho/issues/4217) (continue after approval), [#2086](https://github.com/evoila/meho/issues/2086) (approval links)

Code paths are under `backend/src/meho_backplane/` unless they start with `backend/`, `cli/` or `docs/`. Line numbers are on `main` at `79caeff3`.

## The decision

A multi-step operation (in the code: a composite) runs several steps. It can stop part-way: a step
waits for approval, or a step is denied. From now on MEHO writes one audit row for the operation
when that happens.

- The row uses the operation's own audit id. The records that already point to that id then point
  to a row that exists.
- The row is an execution row (`operation_execution`): the operation ran.
- Its `result_status` says how it ended: `awaiting_approval` or `denied`.
- The decision about the step stays on the step's own decision row.
- The row is saved last, once, and never changed. If it cannot be saved, the caller gets
  `audit_commit_failed`.

When the code is done, a multi-step operation that answers its caller with `ok`, `error` (#4274), a
wait or a deny has exactly one saved row of its own. Only two cases leave no row: MEHO stops before
the row is saved, or the database does not save it (see "Saving order and recovery").

**The rule:** a row's kind (`event_class`) says what happened to the operation itself:
`operation_execution` when its code ran, even partly, and `policy_decision` when a check stopped it
before its code ran. The `result_status` says how it ended.

## The five answers

**1. Does the operation get its own row? Yes, for both a wait and a deny.**

- MEHO makes the audit id and starts the trace (the flight-recorder record of vendor calls) before
  any step runs (`operations/dispatcher.py:764`, `:771`).
- That id is the parent of what the steps write (`operations/_branches.py:634`): rows of steps run
  through `dispatch_child`, the step's approval request row (`approval.request`, `operations/approval_queue.py:1001-1006`),
  and the deny row of a step refused before it could wait (`operations/composite.py:820`, #3542).
- Today no row has that id. The dispatcher passes a waiting or denied result straight through
  (`operations/dispatcher.py:1730`). So the trace route answers 404 (`api/v1/audit.py:432`), and the
  replay tree shows the steps as loose rows (`audit_query/replay.py:166`).
- Steps that call the vendor directly write no row of their own
  (`connectors/vmware_rest/composites/_write.py:1301-1317`, `docs/architecture/operations-substrate.md:322`).
  The operation's row is the only lasting record that these writes ran.
- Postulate 7: an operation returns success only when its audit row is saved. Records that point
  to an audit id should find that row once the call returns; for a wait or a deny they never can today.

**2. What kind of row? An execution row. #3733 holds.**

- The operation was allowed and ran. Only a step was stopped. So its row is `operation_execution`.
- The step's decision has its own `policy_decision` row: the `approval.request` row for a wait
  (`operations/approval_queue.py:1274-1283`), and a `denied` row for a deny.
- One gap to close: a step the policy check simply says no to gets no step row today
  (`operations/composite.py:873`); only a step refused before it could wait does (`:820`). The code
  writes this plain "no" through the same writer, `_audit_denied_and_return` (`operations/dispatcher.py:2761`).
- A decision row for the operation would fill the missing id, but it would hide the trace: only an
  execution row shows one (`api/v1/audit.py:436`, `ui/routes/audit/routes.py:432`).

**3. Which `result_status`? `awaiting_approval` for a wait, `denied` for a deny.**

- The row carries the result's own status, the one the caller got. Not `error` for a deny: the
  row would disagree with the caller, and the `error` filter would mix refusals with failures.
- `awaiting_approval` is new only as a written value. MEHO already maps it to the status code 202
  (`operations/_errors.py:2556`), the code the `approval.request` row uses too
  (`operations/approval_queue.py:176`). `denied` maps to 403 (`operations/_errors.py:2558`).
- `payload.error` holds the result's `extras`, as on error rows (#2680): `error_code` and
  `approval_request_id` for a wait, `error_code` and `reason` for a deny. Nested there, it does not
  clash with `payload.approval_request_id`, which names the request a resumed run ran for (#3864).
- Each run keeps its status forever. An approval never changes the first run's row to `ok`. The
  approved work gets the second run's row.

**4. How does the second run link to the first? Through the approval request. No new column.**

- After the approval, the second run gets a new audit id (`operations/dispatcher.py:764`).
- Its `parent_audit_id` (the column that points to the record that caused it) is the
  `approval.request` row. Both ways to continue set it: a person's approval
  (`operations/approval_queue.py:2045`) and an agent that waited (`agent/approval_wait.py:854`). A
  person's approval also adds `payload.approval_request_id` (`operations/approval_queue.py:1990`).
- The `approval.request` row's parent is the first run (`operations/approval_queue.py:1006`).
- So the chain is: second run → approval request → first run. Today the last link points to a
  missing row; #4275 makes it exist. A second stored link could disagree with this one.
- If the second run waits again (#4217), the rule repeats: it gets its own `awaiting_approval` row,
  and the new approval request points to it.
- The `parent_audit_id` filter finds direct children only, one link at a time. The replay view
  (console, REST, `meho audit replay`) follows the whole chain (`audit_query/replay.py:91`).

**5. What changes for the code that reads `result_status`? Two help texts and one doc table.**

- Audit query filters (REST, console, MCP `query_audit`, CLI `meho audit query`): no new value. The
  query reads the status code, not `payload.result_status` (`audit_query/query.py:314-340`, `:405-423`):
  202 shows as `pending`, 403 as `denied`. Add `event_class=operation_execution` to leave out decision rows.
- `pending` is history: "this run stopped to wait". The row stays `pending` after the request is
  approved, rejected or expired, like every `approval.request` row today. So the filter cannot show
  what waits now. The approval queue can (`meho approvals list`, or the console's approvals page).
- `denied` on an operation's row does not mean that nothing changed. Steps before the refused one
  may have written. The rows under it, and the trace when one was recorded, show what ran.
- The CLI flag help (`cli/internal/cmd/audit/query.go:110`), the MCP parameter text
  (`mcp/tools/audit.py:160`) and two tables in `docs/codebase/audit_query.md` (`:80`, `:110`) list
  only `ok|error|denied`. They must name `pending` and say that it is history. The console already
  offers `pending` (`ui/routes/audit/routes.py:148`).
- No change to the trace read: it never reads `result_status`, and each run has its own audit id
  and trace (`api/v1/audit.py:436`). The row's live broadcast event carries the same value; the
  console feed shows an unknown value as text (`ui/templates/broadcast/_event_row.html:110`).

## Saving order and recovery

The records are saved in separate database transactions, in this order:

1. A step's records, when the step ends: its step row, its deny row, or its approval request with
   its `approval.request` row (one transaction, so both or neither: `operations/approval_queue.py:1273`,
   saved at `operations/composite.py:858`).
2. The operation's own row, last, after the handler returns. Only then is the outcome known: a
   handler can go on after a step waits and return its own result (the VM deploy turns a waiting
   rename into a warning, `connectors/vmware_rest/composites/_write.py:3283`). So the row cannot
   share the approval request's transaction. Saving it first would need a placeholder row that is
   changed later, and audit rows are never changed (the log is append-only).

While the operation runs, its step records point to an id with no row yet, as on today's `ok` and
`error` paths. That is normal. After the call returns, the row exists, except in two cases:

- **The database does not save the row.** The new rows use `require_audit=True`, as the `ok` row
  does (`operations/dispatcher.py:1218-1227`). Without it, the helper only logs the failure
  (`operations/_audit.py:840-848`). The caller gets `audit_commit_failed` (`operations/_errors.py:603`):
  outcome `uncertain`, `retry: false`, no `audit_id`. For a wait it also keeps `approval_request_id`,
  because that request is saved and still waits. The trace is saved, but the trace route shows
  nothing without the row (404). An `error` row (#4274) does not change: if it is not saved, the
  caller still gets the error, only without an `audit_id`.
- **MEHO stops before the row is saved** (a crash, a restart, or a shutdown that cancels the call).
  The caller gets no answer. The step records saved before stay as they are, and their parent id
  never gets a row. The trace is lost too: it is saved after the row (`operations/dispatcher.py:795`).
  A saved approval request stays valid. Approving it runs the second run as usual.

**Recovery.** MEHO writes no repair row later: it would have to guess how the run ended. No start-up
or scheduled repair job is needed. "No missing parent, ever" is not possible: a step must be
recorded when it ends, and the operation's row can only be saved when the operation ends. Such rows
stay visible: the replay view shows them as loose rows, and a query for their parent id finds no row.

**Retry.** A caller that got no answer or `audit_commit_failed` must not simply run the operation
again. Earlier steps may write again, and a step may ask for a second approval. A new audit id does
not make a repeat safe. Check the approval queue and the target first.

## What stays the same

- **#3733:** approval and deny records are decision rows, never execution rows. `approval.request`
  stays `policy_decision`; approve, reject and expiry stay `approval_decision`.
- **An operation stopped before its code runs** (it waits or is denied at its own check) keeps only
  its decision row. It has no trace and no run id.
- **A handler that handles the stopped step itself** and returns its own result keeps today's row
  for that result (`ok`, or `error` under #3809).
- **The caller's result** (the same as before, plus the new row's `audit_id`, as #4274 does) and the
  approval and resume flow (#3351, #4217).

## Acceptance criteria for the code of #4275

- [ ] A multi-step operation whose step waits for approval leaves exactly one row under its own audit id: `event_class="operation_execution"`, `payload.result_status="awaiting_approval"`, `status_code=202`, and `payload.error.approval_request_id` equal to the id in the result.
- [ ] That step's `approval.request` row, and the rows of steps that ran before it, have `parent_audit_id` equal to that row's id.
- [ ] A step the policy check says no to leaves two rows: a `policy_decision` row for the step (`result_status="denied"`, parent = the operation's audit id), and an `operation_execution` row for the operation (`result_status="denied"`, `status_code=403`). A step refused before it could wait (#3542) keeps its step row and gets the same operation row.
- [ ] The caller gets the same result as before, plus `audit_id` equal to the new row's id. The row records the real elapsed time.
- [ ] With trace capture on, `GET /api/v1/audit/{id}/trace` for that id answers 200 with `trace_present=true`, not 404.
- [ ] After approval, the second run's row has a new audit id and `parent_audit_id` equal to the `approval.request` row's id, on the person's path and on the agent's path. If the second run waits again, it gets its own `awaiting_approval` row.
- [ ] Two waits in a row (run 1 waits, approve, run 2 waits, approve, run 3 ends `ok`): the session's replay view shows one tree, run 1 → request 1 → run 2 → request 2 → run 3, with no loose rows. Runs 1 and 2 keep `awaiting_approval`.
- [ ] Failed save: when the operation's row for a wait or a deny is not saved, the caller gets `audit_commit_failed` (`execution_outcome="uncertain"`, `retry=false`, no `audit_id`), never `awaiting_approval` or `denied`. For a wait the result keeps `approval_request_id`, and that request still waits.
- [ ] Crash: a test stops the call after the step's approval request is saved and before the operation's row (the row write raises `asyncio.CancelledError`). The request and the earlier step rows are unchanged, no row has the operation's audit id, the replay view shows those rows as loose rows, and approving the request still runs the second run.
- [ ] `POST /api/v1/audit/query` with `result_status="pending"` and `event_class="operation_execution"` returns the waiting run's row and not the `approval.request` row.
- [ ] The #4274 test `test_handler_returned_non_error_result_is_unchanged` (`backend/tests/test_operations_audit_error_envelope.py`) is replaced by tests for the new rows. They go through the real dispatcher and fail without the change.
- [ ] Docs and help: `meho audit query --help`, the `query_audit` parameter text and both tables in `docs/codebase/audit_query.md` name `pending`, say that it is history, and point to the approval queue. `docs/codebase/audit_query.md` (the class table and the #4274 section) and `docs/architecture/operations-substrate.md:348` (which says the result passes straight through) describe the new rows, the saving order, and that `denied` on an operation's row does not mean that nothing changed.
