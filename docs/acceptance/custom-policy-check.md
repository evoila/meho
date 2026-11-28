<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# The custom policy check on a sandbox install

`scripts/acceptance/custom-policy-check.sh` runs the checks of the custom
policy sandbox test (the switch `CUSTOM_POLICY`). For each check it prints
`[ok]`, `[FAIL]` or `[skip]`, the caller, the path (web console, command
line, MCP or REST) and the audit row ids. The run is quick and the same
each time.

## Rules

- It runs only on an install in sandbox mode
  ([sandbox-mode.md](../decisions/sandbox-mode.md)).
- Every step goes to the one install it checked at its start: REST and MCP
  calls go to `--backplane` (or `MEHO_BACKPLANE`), and every `meho` call
  gets `--backplane` with the same address.
- It uses only MEHO's own paths: the web console (a person does the step),
  the `meho` command line, MCP, and REST with a login's token.
- It holds no token, password, host name or address. It reads them from
  the environment (`bash scripts/acceptance/custom-policy-check.sh --help`
  lists every setting). A token reaches `curl` on a file descriptor, and a
  call's parameters reach `curl`, `jq` and `meho` in a private file or on
  their input, never on a command line.
- It never approves or decides a request. Its one approve call is the
  agent's attempt in check 6, which MEHO must refuse. Every approval is a
  person's click in the web console.
- MEHO gives back a request that already waits
  (`extras.approval_request_reused: true`) only for a call with the same
  work reference while `APPROVAL_SAME_CALL_REUSE` is on, and for a retry
  with the same `idempotency_key`. The script sends no idempotency_key, on
  REST, MCP or the meho command line. Each request of a
  run is sent with a work reference of its own, so MEHO never gives the
  first request of check 10 back for its second call. A request given back
  that is on this run's list is this run's: the check goes on as usual. Any
  other request given back waited before the run. Then the check fails,
  nobody is asked to approve it, and the script never withdraws it.
- Check 10 makes its second call only when it read the state of its first
  request (HTTP 200) and that state is not `pending`. While the first
  request still waits, both parts of the second call print `[skip]`. When
  the state cannot be read, both print `[skip] ... not run: the state of
  the first request ... could not be read`. In both cases no second call
  is sent.

## When it stops before any call

It stops with exit code 2, and sends no call, when:

- tracing (`bash -x`) is on, because the trace would show the tokens;
- a setting is missing, or a `CPC_PARAMS_*` value is not a JSON object;
- the install that the `meho` command line stored at its last `meho login`
  is not the install of `--backplane`.

Then it reads `/ready` and `/version` and prints the install's address and
version. It stops with exit code 2 when:

- `/ready` does not answer HTTP 200 with `"status": "ready"` and
  `"sandbox_mode": "on"`. A 503 answer (`not_ready` or `draining`) stops it
  too, also with `sandbox_mode: on`;
- the switch `CUSTOM_POLICY` (`features.policy.custom_policy` of `/ready`)
  is not on, or cannot be read. `--record-baseline` needs it off. When the
  install sets `METRICS_AUTH_TOKEN`, `/ready` shows the switch only with
  that token: set `CPC_METRICS_TOKEN`.

## What one run does to the install

- Check 4 runs an operation at once (the personal agent rule), and the
  approvals of checks 2, 5 and 10 run their operations.
- Check 6 sends a destructive call (`CPC_OP_DESTRUCTIVE`), a root command
  (`CPC_OP_ROOT_SHELL`) and a delete: the service login with the delete
  grant deletes the target of `CPC_OP_DELETE`. A person step first asks to
  make sure these targets are disposable systems. With no terminal, or a
  "no", none of these calls is sent.
- Check 11 starts a run of the sensor `CPC_SENSOR_ID`.
- At the start it reads the login of each token (one GET with its own
  request id, then the audit row of that GET) and the overrides
  (`GET /api/v1/connectors/operation-overrides`).
- Each run makes a new work reference (`custom-policy-check-<random>`),
  and prints it at the start. Each call sends a work reference of its own:
  the run's reference and the step, for example
  `custom-policy-check-<random>-c6-delete-person` (the `Meho-Work-Ref`
  header on REST and MCP, and `--work-ref` for `meho`). Each request of a
  run has its own work reference. An approve of all the requests of one
  work reference, in the web console or on the command line, covers one
  request of the run at most. A read the script repeats (for example the
  audit read of check 11) may send the same reference again; it makes no
  request.
- Many calls wait for approval. The script keeps a list of each request it
  made and the login that made it. At the end, and when it stops early (an
  error, Ctrl-C or a stop signal), it withdraws each request that still
  waits, with that login (`POST /api/v1/approvals/{id}/withdraw`). Then it
  reads the waiting requests (`GET /api/v1/approvals`) and withdraws each
  one whose work reference starts with this run's reference, so a request
  of a call that a stop cut off is taken back too. A waiting request of one
  of its logins without this run's reference is never withdrawn: it was
  made in another session, and the script prints one `[note]` line for it.
- It reads the queue also when it could not read the login of a token at
  the start. Each waiting request whose work reference starts with this
  run's reference and whose sender is none of the logins it knows prints
  `[left] request <id> (work reference of this run): not withdrawn, its sender is not known`.
- A request leaves the list only when it is handled. A request found in
  the queue goes on the list before the script reads its state or
  withdraws it. A stop signal during the clean-up prints one
  `[left] request <id> (sent with <login>): not withdrawn, the run was stopped`
  line for each request of the list that was not handled yet, also for one
  found only in the queue.
- The script reads the waiting requests only from an answer HTTP 200 whose
  body is a list. Any other answer prints
  `[left] the waiting requests of this run: could not read the queue (...)`,
  and the run ends with exit code 3, or 1 when a check also failed.
- A request stays on the list until its state is read. When the state
  cannot be read, or the withdraw does not answer 200, the script prints
  `[left] request <id> (sent with <login>): ...`, and the run ends with
  exit code 3, or 1 when a check also failed.
- The take-back asks only for what the person steps of this run added,
  each by name: the lowering of check 1, the agent rule of check 4, the
  custom role of check 5, the lowering of check 6, and an override of
  `CPC_OP_WRITE` left by check 8 or 9. A step that did not run is not
  named; with no person step done there is no question. An override that
  was there before the run is set back to how it was, not removed: the
  question names its risk level, category, labels and "always wait" mark.
  Category, labels and the "always wait" mark are the values that apply to
  the operation, which may come from the policy file. The question never
  asks to set an approval back.
  Then the script reads back each one against the state at the start:
  - each override of the operation, by scope (install or tenant), with the
    settings the override list shows: risk level, category, labels and the
    "always wait" mark. Labels are compared as key and value pairs, so
    `{"a": "b c=d"}` and `{"a": "b", "c": "d"}` are two different label
    sets. Each difference prints a `[left]` line that names the scope and
    the setting;
  - the approval setting of an override that was there before the run: the
    override list shows only the approval that applies, not what the
    override row stores (set to yes, set to no, or not set). So the script
    prints `[left] <name>: approval setting not confirmed`;
  - the agent rule and the custom role: MEHO has no route yet that reads a
    personal agent rule or a custom role from where it keeps them. So the
    script prints `[left] <name>: removal not confirmed`. A tenant policy
    file that is the same before and after the run is no proof, and nor is
    an explain answer.
- When the overrides cannot be read at the start, the person steps that
  edit an override (checks 1, 6, 8 and 9) print `not run: the overrides
  could not be read at the start`, and nobody is asked to edit or remove
  an override.
- Check 9 removes an override. When `CPC_OP_WRITE` has an override from
  before the run, check 9 prints `[skip] check 9 (an Admin, a person, web
  console): not run: an override from before the run`, and nobody is asked
  to remove it.
- Last, a person turns the switch off. The install ends with the switch off.

## Person steps and `[skip]` lines

A step that needs a person in the web console prints what to do and asks
"Is it done?". A step where a person approves a request, or tries to,
names the one request id that the person decides (checks 2, 5 and 10).
Decide only that request. With no terminal, or when the person answers no:

- it prints `[skip] check N (...): needs a person`;
- every later step that needs that person step prints
  `[skip] check N (...): not run: waits for the person step N`;
- it never runs a step that a person step guards. The destructive call,
  the root command and the deletes of check 6 run only after the person
  said their targets are disposable.

These lines are always `[skip]`, also in a run where every check passes:

- `check 7 (the owner's own approval of the request of check 10, web
  console and command line): not run: MEHO's explain answer says it only
  while the switch is on`. Check 7 runs with the switch off. With the
  switch off, MEHO's explain answer does not say whether the owner may
  approve their own request. This is on purpose: with the switch off, the
  explain answer stays the same as without the switch. So check 7 cannot
  compare it. Check 10 tests it while the switch is on. With the switch
  off, the approve step is MEHO's code without the switch, and MEHO's
  backend tests cover it.
- `check 8 (an Admin, a person, web console, the reason of the write rule):
  not run: waits for #6966`. For a `caution` write, the console refuses
  the lowering because the level is lower than the classification. The
  write rule has no reason of its own yet. This line prints only when the
  person step of check 8 was done.
- `check 8 (an Admin's token, REST, a policy file preview): not run: waits
  for #6966`, while MEHO's policy check has no mistake text of its own for
  "a write stays at least caution". It still fails when the refusal is the
  one for an operation that shows a secret or calls an address the caller
  gives.
- `check ... (..., the caller kind): not run: waits for #6962, audit row
  <id>`, as long as the audit row holds no `caller_kind` field. The id is
  the audit row the line read. Each line that expects a
  caller kind (checks 1 to 6, 10 and 11) still checks the rest: the
  answer, the rule, the audit row and the fresh result. Such a line never
  prints `[ok]`: it prints `[FAIL]` when another part failed, and else
  only this `[skip]` line. The summary line counts these lines as "caller
  kind not proven: N". A `caller_kind` of null, a wrong caller kind and a
  row that is not found still fail.
- Check 11 always prints `[skip]` for the event intake, the findings
  investigator and the runner executor, because the script cannot start
  their work on a sandbox install.

A `[skip]` line never counts as passed.

## How to run it

Set `CPC_OP_POLICY_CHANGE` to `policy-registry-1.x policy.apply`, and
`CPC_PARAMS_POLICY_CHANGE` to the params of the policy import: `scope`
(`tenant`), `file` (the whole policy file, as text), `file_sha256` and
`based_on`. Take the last two from a preview of the same file
(`POST /api/v1/policy/import?preview=true`). Record the baseline with the
same params.

1. With the switch `CUSTOM_POLICY` still off, save the answers that check 7
   compares with:

   ```bash
   bash scripts/acceptance/custom-policy-check.sh --record-baseline baseline.tsv
   ```

   A baseline file recorded with an earlier version of the script must be
   recorded again, because check 7 has probes that an older file does not
   hold.

   It saves the file only when every probe got HTTP 200, a valid answer,
   and its audit row. A preview that answers `status: "error"` is no valid
   answer. Else it fails and keeps an earlier file as it was.
   With the switch off the policy import operation is not there, so the
   three probes of `CPC_OP_POLICY_CHANGE` save the answer
   `unknown_connector`; any other error fails.

2. Turn the switch on and restart MEHO. Sign in with `meho login` to the
   same install. Set `CPC_BASELINE=baseline.tsv` and the other settings,
   then run:

   ```bash
   bash scripts/acceptance/custom-policy-check.sh
   ```

3. When the script prints `[person]`, do that step in the web console and
   answer `y`. Last, the script asks you to turn the switch off again, and
   runs check 7.

Exit codes:

- 0: every check passed.
- 1: at least one check failed.
- 2: wrong use, a missing setting, or an install the script may not use.
- 3: not complete. No check failed, but a step did not run (`[skip]`), or
  something the run made could not be taken back (`[left]`).

When a check failed and something was not taken back, the run ends with
exit code 1, and the `[left]` lines are printed.

Exit code 0 needs the three open parts of check 11: the event intake (the
script cannot send an event to the install), the findings investigator (it
needs a sensor change and an AI model on the install) and the runner
executor (it needs a satellite runner). It also needs the two parts of
check 8 that need the write rule's own reason (#6966), and the caller kind
on the audit row (#6962). Until then, a run where every check passes ends
with exit code 3. So does a run
where the person did checks 4 and 5, while MEHO has no route that reads
the agent rule and the custom role. The `[skip]` line of check 7 for the
owner's own approval prints in every run, so today every run where all
checks pass ends with exit code 3.

## The checks

| Check | What must be true |
|---|---|
| 1 | An Admin lowers a `dangerous` operation to `caution` with a reason and a fresh sign-in. A person in the console runs it at once. The audit row names the override made in this run. |
| 2 | A former `caution` operation with the plain mark is now `dangerous` (read first). An Operator cannot approve another person's request for it; an Admin can. |
| 3 | A saved "always wait" from before the switch is now `destructive`: without a preview the call is refused; with a preview it waits. |
| 4 | A personal agent rule lets a person's AI tool run one `caution` operation at once. A service login without a grant still waits for it, by the rule `default:service.caution_write`. |
| 5 | A custom role approves requests on one connector. A person without the role is refused at the click. |
| 6 | A credential read, a credential write, a change of rights, a change of the policy (with the Admin's token), a `destructive` operation, a delete (also after a lowering) and a call to an address the caller gives all wait. A service login's root command on a host waits by the rule `default:service.root_shell`. A service login with a delete grant runs the delete (sandbox mode). An agent's policy import (`policy.apply`) is refused before anything waits, and so is an Operator's (it needs the tenant_admin role). An agent's approve attempt is refused. |
| 8 | A write is never lowered below `caution`: in the console and with a token (a policy file preview). |
| 9 | Every change of the policy in the console needs a fresh sign-in: a raise and the removal of an override too. |
| 10 | A person approves their own personal agent's request in the console. The same approve from the command line is refused: the person runs the printed `meho approvals approve` command. |
| 11 | Each login of MEHO's own code writes its caller kind: `system` for the check runner, `person` for the web console reader. |
| 7 | With the switch off again, every answer, and who may approve, is the same as before the switch was turned on. |

What makes a check pass:

- A call passes on its answer (runs, waits or refused), its audit row, and
  the expected rule id on that row. A rule marked `(shadow)` did not
  decide the call, and an empty rule never matches. The expected rule ids
  sit next to each check in the script, each with its row in the rule
  table of [policy-evaluator.md](../codebase/policy-evaluator.md).
- Each `destructive` call and each delete first asks for a preview with
  the same login, target and parameters, and sends its hash. When the
  preview fails or gives no hash, the step prints `[FAIL]` and sends no
  call. After the lowering in check 6, the risk level must be exactly one
  level lower than before; a raise, the same level or no level fails.
- Check 1 starts its search at the person step itself: the server's time
  of the step start (the audit row of one GET the script sends just
  before). It finds the override of this run by its own fields: connector,
  operation, level `caution`, `set_at` after the step began, and
  `set_by_sub` equal to the login of the Admin's edit row
  (`meho_connector_edit_op`) of that operation. `rule_ref` of the override
  list names a classification entry, so the script does not use it. The
  edit row is the Admin's edit on the connector of `CPC_OP_LOWERED` that
  lowered the risk level to `caution`: its new level is `caution`, and its
  old level is higher (in the order `safe`, `caution`, `dangerous`,
  `destructive`). An edit that leaves the level at `caution`, a raise to
  `caution`, an edit of an operation with the same id on another
  connector, an edit of another setting, or the removal of an override is
  not that row. It takes only the run row of
  the Operator of `CPC_PERSON_TOKEN`, with the connector and target of
  `CPC_OP_LOWERED`, after that edit row. The run's
  audit row must carry a rule of the source `override`.
- A click in the web console passes on the request's status and the audit
  row of the click (`POST /ui/approvals/{id}/approve`). Every click row
  must have HTTP 200: a 401, a sign-in redirect or an error is a failure
  for another reason. An approval needs the click row of the person who
  approved it (`reviewed_by`).
- A refused click (checks 2 and 5): the person who may not approve the
  request first opens it on the Approvals page. When the Approve button is
  off, a correct install writes no click row: the person says whether the
  button was off and which reason the page named, and the request must
  still wait. When the button is active, the person clicks it: the check
  needs the row of a click by another person than the sender, and the
  person confirms that MEHO showed the refusal of the rule (the audit row
  does not hold its text). MEHO answers a refused click and a click with a
  too-old approval sign-in both with HTTP 200 and a banner, so each
  question names the "Sign in again" banner as a "no".
- Check 10 on the command line passes on the Operator's audit row of
  `POST /api/v1/approvals/{id}/decide` (the route `meho approvals approve`
  calls) with HTTP 403, a request that still waits, and the person's
  confirmation that `meho` printed "you may approve it yourself with a
  click in the web interface".
- The agent's approve attempt in check 6 goes only to a request the script
  made for `CPC_OP_HARMLESS`. It passes only on HTTP 403 with a `detail`
  that starts with `human_principal_required`, the audit row of the
  attempt, and a request that still waits. Any other answer fails, also a
  401.
- An agent's policy import (`policy.apply`, check 6) passes only when MEHO
  refuses it with "Only a person can ask for a policy import". An
  Operator's token is refused too, with "needs the tenant_admin role". The
  same call with the Admin's token (`CPC_ADMIN_TOKEN`) must wait. Check 7
  probes the policy import with the agent login, the Operator's token and
  the Admin's token.
- Check 8 with a token passes only on HTTP 200 with `ok: false` and the
  mistake of the write rule itself. Checks 8 and 9 in the console count
  only the audit rows of the Admin of `CPC_ADMIN_TOKEN` for the edit of
  `CPC_OP_WRITE`. Check 8 needs the refused console edit
  (`/ui/connectors/registry/<connector>/operations/<op>`) with HTTP 400,
  the answer of the "Edit rejected" panel, no edit row of the operation, a
  risk level read back of at least `caution`, and the person's word that
  the "Edit rejected" panel said the risk level is lower than the
  classification (MEHO's reason `risk_level_below_classification`). Every
  other "Edit rejected" text is a no, for example an empty edit, a value
  that is not valid or a missing reason. "Not found", a sign-in page and
  an error are a no too. A 404, or a 403 or 401 for a login, does not
  count, and nor does a saved edit.
  Check 9 needs an edit row (`meho_connector_edit_op`) of the connector and
  operation that sets the level `dangerous`, followed by the row that
  removes that override.
- Check 11 reads the sensor from the list `GET /api/v1/sensors` (MEHO has
  no GET of one sensor), page by page with `offset`, and fails at once
  when it is not there. It passes only on the audit row of a run after its
  run call (at or after the `next_fire_at` of the run answer, compared in
  full, with the fractions of a second): the sensor's operation, target
  and login. It counts only with a fresh result of the named sensor: one
  that `GET /api/v1/sensors/{id}/results` shows evaluated at or after
  `next_fire_at`. An install that keeps no result history
  (`CHECKS_EVIDENCE_RETENTION_DAYS=0`) answers that route with no row.
  For such an install, set `CPC_NO_RESULT_HISTORY=1`. Only then does an
  empty list lead the script to read the sensor list again, and the
  sensor's `last_evaluated_at` counts when it is at or after
  `next_fire_at`. Without the setting, an empty list is no fresh result,
  and the check runner line fails. Any other value than `1` stops the
  script with exit code 2 before any call. A fresh result row counts in
  both cases. The audit row of the run is still needed.

Check 7 runs last, because it turns the switch off. The script reads the
switch again first and fails when it is still on or cannot be read. Then
it asks `POST /api/v1/policy/explain` once for each probe, so it runs
nothing. Each probe asks as the caller of the check it repeats, by the
same way in (web console or token), and every operation of checks 1 to 6,
8 and 10 has a probe. A probe keeps the answer, who may approve (the
`approvers` of the answer), and, for an operation with `CPC_PARAMS_*`, the
`policy_verdict` of a REST preview with those parameters, because explain
takes no parameters. A preview that answers `status: "error"` (for example
`invalid_params`) is no valid answer. It passes only with HTTP 200, a valid
answer, its audit rows, and the same three values as the baseline. It
does not compare whether the owner may approve their own request: that
line is always `[skip]`, because MEHO's explain answer says it only while
the switch is on. Check 10 tests it while the switch is on.

## The start of a run

The script reads the install as it is; it does not upgrade it. Which start
was checked by a run is in the pull request of the script.

## The tests of the script

- `scripts/tests/test_custom_policy_check.py` runs the script with
  `--dry-run <folder>`: every answer comes from a recorded file
  (`<step>.json`, `{"code": ..., "body": ...}`). It checks the pass, fail
  and skip lines and their audit row ids, the order of the checks, the
  stop rules, the switch, and one broken answer for each check.
- `scripts/tests/test_custom_policy_check_live.py` runs the script without
  `--dry-run`, with a stand-in `curl` and `meho` on `PATH` that write down
  their own command lines. It checks that every call goes to the checked
  install, that no token or parameter value is on a command line, the
  preview hash of each delete, the clean-up, a call without a target, a run
  with no terminal (no destructive call, root command or delete is sent,
  and no take-back question), a stopped run (a request found in the queue
  is withdrawn), a request from before the run that is never withdrawn,
  a work reference of its own on every call, a request of the same
  Operator from another session that still waits after the run, and a stop
  during the clean-up.

Run both with Python 3.10 or newer and `jq` on the `PATH`:

```bash
python3 -m unittest discover -s scripts/tests -p 'test_custom_policy_check*.py'
```

CI runs both, and `shellcheck` on the script, in the workflow
`linux-server-test.yml`, on each pull request that changes the script, its
two test files, its fixture or this page.
