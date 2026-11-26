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
- Check 6 runs a delete: the service login with the delete grant deletes
  the target of `CPC_OP_DELETE`. A person step first asks to make sure that
  target is a disposable system.
- Check 11 starts a run of the sensor `CPC_SENSOR_ID`.
- Many calls wait for approval. The script keeps a list of each request it
  made and the login that made it. At the end, and when it stops early (an
  error, Ctrl-C or a stop signal), it withdraws each request that still
  waits, with that login (`POST /api/v1/approvals/{id}/withdraw`).
- For the checks, a person adds a personal agent rule (check 4), a custom
  role (check 5) and two lowerings (checks 1 and 6). At the end the script
  asks the person to remove them, and reads back that they are gone: the
  override list has no lowering of this run, the Operator's token no longer
  runs the operation of check 4 at once, and the approvers of check 5 do
  not name the custom role.
- It prints one `[left]` line for each thing it could not take back.
- Last, a person turns the switch off. The install ends with the switch off.

## Person steps and `[skip]` lines

A step that needs a person in the web console prints what to do and asks
"Is it done?". With no terminal, or when the person answers no:

- it prints `[skip] check N (...): needs a person`;
- every later step that needs that person step prints
  `[skip] check N (...): not run: waits for the person step N`;
- it never runs a step that a person step guards. The deletes of check 6
  run only after the person said the target is disposable.

These lines are always `[skip]`, also in a run where every check passes:

- `check 10 (command line approve): needs a person`. The script never runs
  `meho approvals approve`; it prints the command for the person.
- `check 6 root command (...): not run: waits for #7051`. The fixed rule id
  of a root command comes with that issue.
- `check 11: not run: waits for #7073`, as long as the audit row has no
  caller kind. With it, check 11 still prints `[skip]` for the event
  intake, the findings investigator and the runner executor, because the
  script cannot start their work on a sandbox install.

A `[skip]` line never counts as passed.

## How to run it

1. With the switch `CUSTOM_POLICY` still off, save the answers that check 7
   compares with:

   ```bash
   bash scripts/acceptance/custom-policy-check.sh --record-baseline baseline.tsv
   ```

   It saves the file only when every probe got HTTP 200, an answer `run`,
   `wait` or `deny`, and its audit row. Else it fails and keeps an earlier
   file as it was.

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

## The checks

| Check | What must be true |
|---|---|
| 1 | An Admin lowers a `dangerous` operation to `caution` with a reason and a fresh sign-in. A person in the console runs it at once. The audit row names the override made in this run. |
| 2 | A former `caution` operation with the plain mark is now `dangerous` (read first). An Operator cannot approve another person's request for it; an Admin can. |
| 3 | A saved "always wait" from before the switch is now `destructive`: without a preview the call is refused; with a preview it waits. |
| 4 | A personal agent rule lets a person's AI tool run one `caution` operation at once. A service login without a grant still waits for it. |
| 5 | A custom role approves requests on one connector. A person without the role is refused at the click. |
| 6 | A credential read, a credential write, a change of rights, a change of the policy, a `destructive` operation, a delete (also after a lowering) and a call to an address the caller gives all wait. A service login with a delete grant runs the delete (sandbox mode). An agent's change of the policy is refused before anything waits. An agent's approve attempt is refused. |
| 8 | A write is never lowered below `caution`: in the console and with a token (a policy file preview). |
| 9 | Every change of the policy in the console needs a fresh sign-in: a raise and the removal of an override too. |
| 10 | A person approves their own personal agent's request in the console. The same approve from the command line is refused (a person step). |
| 11 | Each login of MEHO's own code writes its caller kind: `system` for the check runner, `person` for the web console reader. |
| 7 | With the switch off again, every answer is the same as before the switch was turned on. |

What makes a check pass:

- A call passes on its answer (runs, waits or refused), its audit row, and
  the expected rule id on that row. A rule marked `(shadow)` did not
  decide the call, and an empty rule never matches. The expected rule ids
  sit next to each check in the script, each with its row in the rule
  table of [policy-evaluator.md](../codebase/policy-evaluator.md).
- Each `destructive` call and each delete first asks for a preview with
  the same login, target and parameters, and sends its hash. After the
  lowering in check 6, the script reads the new risk level and fails when
  it did not change.
- A click in the web console passes on the request's status and the audit
  row of the click (`POST /ui/approvals/{id}/approve`). A refused click
  needs the row of a click by another person than the sender.
- The agent's approve attempt in check 6 goes only to a request the script
  made for `CPC_OP_HARMLESS`. It passes only on HTTP 403 with a `detail`
  that starts with `human_principal_required`, the audit row of the
  attempt, and a request that still waits. Any other answer fails, also a
  401.
- An agent's change of the policy (check 6) passes only when MEHO refuses
  it with "Only a person can ask for a policy import". The same call with
  the Operator's token must wait.
- Check 8 with a token passes only on HTTP 200 with `ok: false` and the
  mistake that the write stays at least `caution`. Checks 8 and 9 in the
  console need the audit rows of the person's changes.
- Check 11 passes only on the audit row of the sensor's run: its
  operation, its target and its login.

Check 7 runs last, because it turns the switch off. The script reads the
switch again first and fails when it is still on or cannot be read. Then
it asks `POST /api/v1/policy/explain` once for each probe, so it runs
nothing. Each probe asks as the caller of the check it repeats, by the
same way in (web console or token), and every operation of checks 1 to 6,
8 and 10 has a probe. A probe passes only with HTTP 200, an answer `run`,
`wait` or `deny`, its audit row, and the answer of the baseline.

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
  with no terminal, and a stopped run.

The CI workflow `linux-server-test.yml` runs both.
