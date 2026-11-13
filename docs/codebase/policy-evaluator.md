# The policy evaluator and shadow mode

## Overview

The policy evaluator is one function that says, for every call: **run**,
**wait** for a person, or **deny**, and which rule decided. It also says,
for every approval request, whether a person **may approve** it.

Today these decisions are spread over many places (the policy gate in
`operations/_validate.py`, the agent grants in `auth/permissions.py`, the
approval checks in `operations/approval_queue.py`). The evaluator puts
them in one place, with a fixed schema and no rule language. Later work
uses it for approver routing, for the rule on audit rows and for
`meho policy explain`.

Before the evaluator decides anything, it must prove that it gives
today's answers. That is **shadow mode**:

- Switch: `POLICY_ENGINE_SHADOW` (Helm `config.policy.engineShadow`).
  **Off by default.** `/ready` shows it under
  `features.policy.engine_shadow`.
- With the switch **off**, the evaluator does not run.
- With the switch **on**, the evaluator works out its own answer after
  today's checks. When the two answers differ, MEHO writes one
  `policy_shadow_mismatch` log line with both answers and adds one to the
  metric `policy_shadow_mismatch_total{kind="call"|"approval"}`.
  **Today's checks still decide.** A failure inside the shadow is logged
  as `policy_shadow_failed` and never reaches the caller.
- The switch becomes the default only after the parity test and a
  sandbox run show no difference.

## Key types (`backend/src/meho_backplane/policy/evaluate.py`)

All are frozen dataclasses.

| Type | Holds |
|---|---|
| `Asker` | Who asks: kind (`person`, `agent`, `service`, `runner`), sub, tenant, roles (the tenant role plus the flags `approver`, `sa_operator`, `platform_admin`), via (`web` or `token`), the acting agent of a hosted run. |
| `Asked` | What is asked: connector id, op id, group key, category, risk level, "always wait" (the effective approval flag), labels, mutating, op class marks, caller address, target. |
| `Lookups` | What the caller found in the database for this call: the matching agent grant (also of the hosted agent), a standing grant, a run approval, the read-scope refusal, sandbox mode, `AGENT_GRANT_REQUIRED`. The evaluator never reads the database itself. |
| `RuleRef` | The rule that decided: source (`floor`, `tenant_rule`, `default`, `agent_grant`, `standing_grant`, `run_approval`, `read_scope`, `role_floor`), rule id, policy version (empty in part 1), text. |
| `Decision` | Verdict (`run`, `wait`, `deny`), the rule, the groups that may approve (only for `wait`), `waits_because` (`token_call` for a person's `caution` call that waits only because it came with a token), and a trace of every step. |

The op class marks of `Asked` come from the code: `root_shell`,
`credential_read`, `per_call_approval`, `delete_shaped` and
`classified_always_wait`.

## Control flow

The order is fixed.

1. **Floors** (`policy/floors.py`). Rules in the code, never in a file. A
   floor sets the least strict answer; no later step loosens it.
   - A person below the operation's role floor is denied.
   - A `destructive` call waits for a person. Only a sandbox delete grant
     can run it, for a service login, in sandbox mode.
   - A call made with a token (a person's token, an agent, a runner) runs
     at once only when it is `safe`.
   - No standing grant and no run approval clears a call that shows a
     secret, connects to an address the caller gives, is marked for
     per-call approval, or that a classification makes always wait.
2. **Customer rules.** None in part 1.
3. **Shipped defaults**, today's table:
   - a person in the web interface runs `safe` and `caution`;
   - a person with a token, and a runner, run `safe`;
   - a service login: `dangerous` waits, a `caution` write waits, an
     operation that runs a command as root waits, the rest runs;
   - an agent: `safe` runs, `caution` / `dangerous` / `destructive` wait,
     an unknown level is denied;
   - a `read_only` login's agent never runs a call at once;
   - "always wait" makes every caller wait.
4. **Per-principal steps**, in today's order:
   - an agent grant replaces the agent default (the floors and ceilings
     still apply); with `AGENT_GRANT_REQUIRED=on` an agent without a
     grant is denied;
   - a hosted agent run gets the stricter of the caller and the agent,
     with today's shortcut: when the agent waits and the caller is not an
     agent, the agent's answer stands;
   - a service login's read scope can deny a call that would run;
   - a standing grant, then a run approval, can clear a service login's
     wait, never a floor.

A floor names itself when it gives the same verdict as a later step, so
the rule of a floor-held answer is always the floor.

Steps that stay in the dispatcher and are not in the evaluator: the
children of an approved run, the wait of a secret reveal, the announce
gate, the role floor check when an approved call resumes, and the
satellite ladder.

### The approval side (`policy/approve.py`)

`may_approve(asker, ask) -> (bool, RuleRef)` answers with today's rules,
in the order of `approve_right`: only a person approves (floor); a
`read_only` person needs the approver flag; only a pending request; a
change of rights only another admin (floor); the checks of who a login or
group change is for; own changes (MEHO-SA-Operators and MEHO-Admins, a web
click on a token-only `caution` wait, the break-glass switch for a lower
risk level); another person's `dangerous` or `destructive` change only a
MEHO-SA-Operator or a MEHO-Admin; then the role floor.

It does not check the sign-in age, the deadline, the params or the
preview binding. The approve still checks those at the click.

### Where the shadow runs (`policy/shadow.py`)

- `policy_gate` (`operations/_validate.py`): after today's verdict, also
  for the preview (`decide_only`) and the step gate of a multi-step
  operation.
- `approve_right` (`operations/approval_rights.py`): every "may I approve
  this?" answer that the CLI and the web interface show.
- The approve itself (`_load_pending_for_approval` in
  `operations/approval_queue.py`), on both the allow and the refuse path.

The shadow only reads. It looks up a standing grant and a run approval
with `record=False`, so it never uses a grant and never writes an audit
row. For a real `destructive` call without a preview hash it does not look
up a delete grant, because today's gate never uses one there. Run
approvals (the approval of a whole run's operation list) are not
compared.

### The rule on the audit row and the approval request (#6044)

With the switch on, the gate also keeps the rule the evaluator matched
(`policy/stamp.py`: `RuleStamp`, `keep_rule`, `take_rule`). The
dispatcher takes it right after its gate and binds it to
`policy_rule_var` (`operations/_audit.py`). Each `dispatch` clears the
rule when it starts and puts the caller's rule back when it returns. So
a step of a multi-step operation that is refused before its own gate
(for example a target of the wrong kind) names no rule. The step gate of a multi-step operation binds the
step's rule only for its own rows, so the parent's row keeps the
parent's rule. Then:

- `audit_log.policy_rule` holds `<source>:<rule id> (shadow)`, and
  `audit_log.policy_version` the policy file version (empty today).
- A new `approval_request` gets `policy_rule` as
  `{"source", "rule_id", "text", "decided": false}` and
  `policy_version`. The approvals console shows it in the request
  details.

`(shadow)` and `decided: false` mean: the evaluator matched this rule,
but today's checks decided. The rule can differ from today's answer;
the mismatch line says when it does. With the switch off, or when the
shadow fails, nothing is kept and the columns stay empty. Each gate
first drops a rule an earlier gate kept (a preview, for example), so a
row never gets the rule of another call.

## How to read a mismatch line

```
policy_shadow_mismatch kind=call op_id=<op-id> principal_kind=<kind>
  today_verdict=needs-approval today_reason=<today's reason>
  shadow_verdict=run shadow_rule_source=standing_grant shadow_rule_id=standing_grant
  shadow_trace=[...]
```

- `today_verdict` is what MEHO did. `shadow_verdict` is what the evaluator
  would have done.
- `shadow_rule_*` names the rule that decided in the evaluator.
  `shadow_trace` lists every step in order, as `<source> <rule id>:
  <verdict>`.
- Compare the trace with `today_reason`. The difference is either a bug in
  the evaluator (fix the evaluator) or a rule of today's code that the
  evaluator does not know yet (add it to the evaluator, never change
  today's code in the same change).

For an approval line, `today_may_approve` and `shadow_may_approve` are
the two answers.

## Cost

The evaluator itself is a few dictionary and set checks. The added time
comes from the lookups it does next to today's: one or two reads for an
agent, up to three for a service login, none for a person. The load check
`tests/test_policy_shadow_load.py` (opt-in:
`MEHO_RUN_LOAD_TESTS=1 uv run pytest -m load tests/test_policy_shadow_load.py -s`)
prints the time per call with 20,000 registered operations, with the
switch off and on.

## Dependencies

- `auth/permissions.py`: `matched_grant` (the matching agent grant,
  before ceilings).
- `operations/service_grants.py`: `consult_and_record_grant(record=False)`.
- `operations/run_approval_gate.py`: `consult_and_record_run_approval(record=False)`.
- `operations/service_read_scopes.py`: `read_scope_denial`.
- `operations/role_floors.py`, `op_classes.py`, `operations/caller_address.py`.

## Tests

- `backend/tests/test_policy_evaluate.py`: the order, every default, the
  floors, the per-principal steps and `may_approve`, without a database.
- `backend/tests/test_policy_shadow.py`: switch off, a forced difference,
  a failure, no grant use, and a table of caller kind x risk level x
  approval flag x delete sign x grant x gate in and out of sandbox mode
  with no difference; the kept rule (#6044).
- `backend/tests/test_operations_dispatcher.py`,
  `backend/tests/test_approval_queue.py`, `backend/tests/test_ui_approvals.py`
  and `backend/tests/migrations/test_migration_0165_add_policy_rule_columns.py`:
  the rule on audit rows, approval requests and the console, and the
  migration on PostgreSQL with the append-only trigger (#6044).
- `backend/tests/test_policy_shadow_load.py`: the load check.

## References

- Settings: `Settings.policy_engine_shadow` (`settings.py`).
- Related: [policy-classification.md](policy-classification.md),
  [agent-permission-model.md](agent-permission-model.md),
  [approvals.md](approvals.md).
