<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# The custom policy check on a sandbox install

`scripts/acceptance/custom-policy-check.sh` runs the checks of the custom
policy sandbox test (the switch `CUSTOM_POLICY`). For each check it prints
`[ok]` or `[FAIL]`, the caller, the path (web console, command line, MCP
or REST) and the audit row ids. The run is quick and the same each time.

## Rules

- It runs only on an install in sandbox mode
  ([sandbox-mode.md](../decisions/sandbox-mode.md)). It reads `/ready` first
  and stops with exit code 2 when `sandbox_mode` is not `on`.
- It uses only MEHO's own paths: the web console (a person does the step),
  the `meho` command line, MCP, and REST with a login's token.
- It holds no token, password, host name or address. It reads them from
  the environment (`bash scripts/acceptance/custom-policy-check.sh --help`
  lists every setting).
- It changes no setting of the product by itself. A person makes the
  policy changes in the web console when the script asks. The install ends
  with the switch off.

## How to run it

1. With the switch `CUSTOM_POLICY` still off, save the answers that check 7
   compares with:

   ```bash
   bash scripts/acceptance/custom-policy-check.sh --record-baseline baseline.tsv
   ```

2. Turn the switch on and restart MEHO. Set `CPC_BASELINE=baseline.tsv`
   and the other settings, then run:

   ```bash
   bash scripts/acceptance/custom-policy-check.sh
   ```

3. When the script prints `[person]`, do that step in the web console and
   press Enter. Last, the script asks you to turn the switch off again, and
   runs check 7.

Exit code 0: every check passed. 1: at least one failed. 2: wrong use,
a missing setting, or an install that is not in sandbox mode.

## The checks

| Check | What must be true |
|---|---|
| 1 | An Admin lowers a `dangerous` operation to `caution` with a reason and a fresh sign-in. A person in the console runs it at once. The audit row names the edit. |
| 2 | A former `caution` operation with the plain mark is now `dangerous`. An Operator cannot approve another person's request for it; an Admin can. |
| 3 | A saved "always wait" from before the switch is now `destructive`: without a preview the call is refused; with a preview it waits. |
| 4 | A personal agent rule lets a person's AI tool run one `caution` operation at once. A service login without a grant still waits for it. |
| 5 | A custom role approves requests on one connector. A person without the role is refused at the click. |
| 6 | A credential read, a credential write, a change of rights, a change of the policy asked by an agent, a `destructive` operation, a delete (also after a lowering), a call to an address the caller gives and a root command on a host all wait. A service login with a delete grant runs the delete (sandbox mode). An agent's approve attempt is refused. |
| 8 | A write is never lowered below `caution`: in the console and with a token (a policy file preview). |
| 9 | Every change of the policy in the console needs a fresh sign-in: a raise and the removal of an override too. |
| 10 | A person approves their own personal agent's request in the console. The same approve from the command line is refused. |
| 11 | A check-runner call writes the caller kind `system` on its audit row. |
| 7 | With the switch off again, every answer is the same as before the switch was turned on. |

Check 7 runs last, because it turns the switch off. It asks
`POST /api/v1/policy/explain` for each caller and operation, so it runs
nothing. Its audit rows are the rows of those requests.

The expected rule ids sit next to each check in the script. Each one names
its row in the rule table of [policy-evaluator.md](../codebase/policy-evaluator.md).
A rule marked `(shadow)` did not decide the call, so it never counts.

## A start from an upgraded install

The script can start on an install that was upgraded from the released
version: check 3 needs one, because a saved "always wait" from before the
switch exists only there. The script does not upgrade the install. It does
not check a request that was waiting from before the upgrade.

## The test of the script

`scripts/tests/test_custom_policy_check.py` runs the script with
`--dry-run <folder>`: every answer comes from a recorded file
(`<step>.json`, `{"code": ..., "body": ...}`), and no person step waits.
It checks the pass and fail lines and their audit row ids, the order of the
checks, the refusal of an install that is not in sandbox mode, and that the
script holds no token or address. The CI workflow `linux-server-test.yml`
runs it.
