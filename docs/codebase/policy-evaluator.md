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
| `Decision` | Verdict (`run`, `wait`, `deny`), the rule, the groups that may approve (only for `wait`), `waits_because` (`token_call` for a person's `caution` call that waits only because it came with a token), every step in order (`steps`, each a `Step` with its rule and verdict), and the same steps as text lines (`trace`). |

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
group change is for; own changes (MEHO-Admins, a web
click on a token-only `caution` wait, the break-glass switch for a lower
risk level); another person's `dangerous` or `destructive` change only a
MEHO-Admin; then the role floor.

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

## `meho policy explain` (#6045)

`POST /api/v1/policy/explain` and `meho policy explain` say why one call
runs, waits or is denied, and who may approve it. Nothing runs and nothing
is written: no audit row for the call, no approval request, no grant use,
no call to an outside system. The REST request itself is logged as
`policy.explain` (a read).

Code: `policy/explain.py` (the work), `policy/explain_asker.py` (who
asks), `policy/explain_models.py` (the answer),
`policy/who_approves.py` (who may approve; shared with the preview),
`api/v1/policy.py` (the route), `cli/internal/cmd/policy/explain.go`.

**Who asks.**

- `me`: the caller. Needs the `operator` role, like the preview and the
  call. `via` can ask for a person in the web interface or with a token;
  without it, as the caller calls.
- `groups` (+ `principal`: `person`, `agent` or `service`): a login made
  up from login group kinds, in the caller's tenant, with a `sub` that no
  grant names. So it has no agent grant, no standing grant, no run
  approval and no read scope. An agent gets at most the `operator` role,
  as a real agent token. Groups that give only `read_only` are refused:
  such a login cannot call operations. Needs a tenant admin or a platform
  admin. `via` is `token` unless asked. The made-up `sub` is
  `WHAT_IF_SUB` (`policy-explain-what-if`): the agent grant service and
  the standing grant service refuse a grant that names it. The question
  also unbinds the hosted agent of the caller's run (`ACTOR_SUB_KEY`), so
  it never reads the grants of the real caller or of a hosted agent that
  acts for the caller.
- `request_id`: the rule stored on an approval request (`policy_rule`,
  `policy_version`) and the groups that may approve it, as the approve
  check sees them: the stored approver groups only while
  `POLICY_APPROVER_ROUTING` is on, else today's rule. Needs the right to
  read approvals.

**Who sees the policy.** Only a tenant admin or a platform admin gets the
whole answer. Everyone else gets it through `without_policy`: the answer,
today's reason, the risk level, the approval flag and the approver
groups. Left out: the matching classification entries, the category and
labels, the place of each evaluator rule, and the approver rule.

**No parameters.** Explain asks about a call with no parameters
(`params={}`). For an operation whose answer depends on its parameters,
the answer is the one of a call without parameters.

**What it answers.** The same steps as the preview, in the same order:
target by name, descriptor and catalog choice (`_resolve_descriptor`),
then `governance_and_gate`, which calls `policy_gate(decide_only=True)` and
the step checks of a multi-step operation. It also gives the gate's own
answer, from before the step check of a multi-step operation. Then, only with
`POLICY_ENGINE_SHADOW` on, `evaluate_call(decide_only=True)`. The gate's
shadow keeps a rule; explain drops it (`forget_rule`).

**Checks after the gate** (`policy/explain_checks.py`,
`refusal_after_gate`). The policy gate is not the last check of a call.
Which checks after the gate a call is asked comes from one function,
`checks_after_gate` (`operations/_after_gate.py`). The dispatcher reads
it before it asks the announce rule and before it asks the ingested check
of a call that waits. Explain reads it too and asks each check with the
same code the call uses. Both ask for the gate's own answer, from before
the step check of a multi-step operation: the call checks the steps only
when the operation runs. So a multi-step write that the gate lets run but
whose step would wait is asked the announce rule.

| Check | Code | Asked when the gate says |
|---|---|---|
| The announce rule of the tenant (`announce_required`): an agent or a service login, not a person | `announce_gate_blocks` (`broadcast/announce_gate.py`) | `run` only. A call that waits is not asked again after the approval. |
| An ingested operation on a target whose connector does not take ingested operations (`ingested_ops_not_accepted`) | `_ingested_ops_refusal` (`operations/dispatcher.py`) | `run` (in the connector lookup) or `wait` |

When one refuses, `answer` is `deny`, and `today` holds `deny` with the
call's reason (`announce_required: ...` or
`ingested_ops_not_accepted: ...`). The evaluator does not know these
checks, so its `same_as_today` is then `false`. A call the gate denies is
not asked again.

**What explain does not check.** `not_checked` is a list of fixed names
(`not_checked()` in `policy/explain_checks.py`). It names each check after
the gate that explain does not ask and that can still refuse the call. A
`run` or `wait` answer with an empty list would mean nothing after the
gate can refuse the call.

| Name | The call's refusal | Listed when the answer is |
|---|---|---|
| `connector_resolution` | `no_connector`, `ambiguous_connector`, `target_required` | `run`, `wait` (asked when the call runs, also after the approval) |
| `operation_param_check` | `refused_before_approval` (`operations/_park_check.py`) | `wait`, when the operation has such a check |
| `destructive_preview_binding` | `preview_binding_required` | `run` (a delete grant in sandbox mode) or `wait`, for a `destructive` operation |
| `permission_preflight` | `write_capability_denied` (asks the vendor) | `wait`, when the operation has a preflight |
| `idempotency_key` | `idempotency_key_conflict` | `wait` |

`deny` gets an empty list. Not listed: a MEHO fault, such as a missing
approval encryption key, and the checks done again after an approval with
the same rules (the steps of a multi-step operation, the role of the
person who runs it). The answer for a stored approval request has no
list. The CLI prints the list last (`Not checked: a, b`).

`tests/test_policy_explain_parity.py` runs a matrix of callers and
operations through explain and through the real call (with the step that
would send the call replaced). It fails on any difference that
`not_checked` does not name.

- `answer`: today's answer (`run`, `wait`, `deny`). While the evaluator is
  off or in shadow mode, today's checks decide (`decided_by: today`).
- `today`: the gate's verdict and reason. When a check after the gate
  refuses the call, `today` holds `deny` with that check's reason.
- `evaluator` (null while the switch is off): its verdict, the rule that
  decided, the floor that decided (when the rule is a `floor` or
  `role_floor`), every step in order (`Decision.steps`), and
  `same_as_today`. Only the `always_wait` step carries `path`: the most
  specific classification entry that makes the operation always wait.
  The other classification entries that match are listed under
  `classification`.
- `classification`: the risk level and the approval flag the call is
  checked with, the category and labels, and every classification entry
  that matches, least specific first, each with its place
  (`install:classifications[0]`, `tenant:classifications[2]`,
  `tenant:override`).
- `approvers` (only when the answer is `wait`): the groups that may
  approve another person's call, and the rule. The groups are MEHO group
  kinds (`MEHO-<kind>`, from `policy/who_approves.py`), not the names of
  the login groups of the install. Each login group of the install is
  bound to one of these kinds. A change of rights: only
  `MEHO-Admins` (`rights_change`). With `POLICY_APPROVER_ROUTING` on: the
  routing entries that fit, the install's first
  (`install:routing[0], tenant:routing[1]`), and `floor`, the install's
  entry: a tenant's routing can only narrow it
  ([policy.md](policy.md), "The install's routing is a floor"). For a
  stored request, the install's routing as it is now, as at the approve
  click; `category_not_stored` with no group when the click refuses the
  request because its category is not known. Else today's rule
  (`default`). For a caller who is not a tenant admin or a
  platform admin, `rule` and `floor` are null.

**A file that is not imported (`file`).** Needs a tenant admin (or a
platform admin for `scope: install`) and `POLICY_CLASSIFICATION` (with it
off, 409: a file changes no answer). The file is checked like
`meho policy validate` (`build_plan`); a file with mistakes is 422 with
the list. Its classification and routing take the place of the stored
policy of its scope; its entries are named `file:classifications[N]` and
`file:routing[N]`. The operation's values are worked out again in the
dispatch order (`policy_in_force`: the code's value, the overrides, the
classification, the floors), so today's gate and the evaluator both see
the file. The steps of a multi-step operation are still checked with the
stored policy.

**A stored policy that is not valid.** A stored classification or a
stored routing that is not valid gives 409 with one sentence that names
it (`InvalidStoredPolicyError`; for the routing `invalid_routing` in
`policy/who_approves.py`). A preview in that state still answers: with
`POLICY_ENGINE_SHADOW` on, its `approvers` is `null` when the routing is
not valid.

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

## The parity test

`backend/tests/test_policy_evaluate_parity.py` runs every case through
today's code and through the evaluator. It fails on any difference. It
runs in the normal unit test job, in about 90 seconds.

- **Calls** (41,472 cases, one test per `AGENT_GRANT_REQUIRED` setting):
  7 caller kinds (a person in the web interface, a person with a token,
  an agent, a `read_only` agent, a service login, a runner, a hosted
  agent run) x 6 logins (the five login groups, and a platform admin in
  another tenant) x 4 risk levels x 32 combinations of five marks
  (requires approval, write, shows a secret, connects to an address the
  caller gives, needs the `tenant_admin` role) x the grants of the
  caller kind. Today's side is `policy_gate`; the evaluator's side is
  `evaluate_call`, with the same lookups. Both must give the same
  verdict, the same reason class, the same approver groups and the same
  `waits_because`.
  - Grants of the agent rows (agent, `read_only` agent, hosted run): none,
    an agent grant that runs, one that denies, one that needs approval,
    and a wide grant that denies next to a narrower one that runs. These
    rows run with `AGENT_GRANT_REQUIRED` set to `off`, `observe` and `on`.
  - Grants of a service login: none, a standing grant, and a run approval
    of the operations that do not show a secret.
  - Grants of a person and a runner: none, and a standing grant.
- **Grants that never clear**: a table that calls `evaluate` directly
  with a standing grant and a run approval in `Lookups`, for an operation
  that shows a secret, connects to an address the caller gives, needs
  approval for every call, or always waits. The call must still wait.
  The lookups of a real call drop such a grant first, so only this table
  sees a change of `grant_may_clear`, `run_approval_may_clear` or
  `NO_GRANT_MARKS`.
- **Approvals** (9,600 cases): 5 approver kinds x 6 logins x own change
  or not x 5 risk levels (also none) x token wait x role floor x pending
  or decided x change of rights x the break-glass switch. Today's side is
  `approve_right`; the evaluator's side is `may_approve`. Both must
  allow or refuse, for the same reason.
- **Routes and tools**: every REST route with a `require_role` check and
  every MCP tool, for each of the six logins. The evaluator has no route
  table, so the test checks that the evaluator reads the login the same
  way: its role floor for an operation that needs the route's role gives
  the same answer as the door.

The "small team" file is checked in `test_policy_small_team_parity.py`:
its export on a fresh install equals the file, and importing it changes
no answer of any registered operation. That test compares today's answers
before and after the import; it does not compare them with the evaluator.

### How to read a failing case

The test prints up to 20 differing cases, each like this:

```
caller=service login=Operators grant=standing agent_grant_required=off level=dangerous marks=[write]
  today={'verdict': 'run', 'class': 'standing_grant', ...} reason='auto-granted by ...'
  evaluator={'verdict': 'wait', 'class': 'wait', ...} trace=['default service.dangerous: wait', ...]
```

- The first line is the case: who asks, the login, the grant, the
  `AGENT_GRANT_REQUIRED` setting, the risk level and the marks of the
  operation.
- `today` is what MEHO does now, with today's reason text. `evaluator`
  is the evaluator's answer, with every step it took (`trace`).
- The reason class says what let a call run (`default`,
  `standing_grant`, `run_approval`, `agent_grant`) or what refused it
  (`role_floor`, `read_scope`, `agent_no_grant`, `agent_rule`). Every
  call that waits has the class `wait`.
- `approvers` are the login groups that may approve another person's
  call. On today's side they come from `approve_right`, one person of
  each group.
- Find the step in the trace where the two answers part. Then fix the
  evaluator, or add today's rule to it. Never change today's code in the
  same change: report a difference in today's code on its own issue.

An approval case prints the approver, the request and both answers:
today's `may_approve_reason` and rule, and the evaluator's rule.

### What the table does not cover

These are the places where the two answers can differ today. Each has
its own issue; when it is fixed, its line goes away.

- Sandbox mode with a preview hash (#6594). A service login's
  `destructive` call in sandbox mode with a preview hash can differ:
  today's gate checks the hash and what else the call affects before a
  delete grant counts, and the evaluator's lookup finds the grant without
  these checks. The sandbox cases without a hash are in
  `test_policy_shadow.py`.
- A change of rights on the call side (#6595). `Decision.approvers` does
  not know it yet (only another admin approves it), so the call table
  uses no such operation. The approval table covers it.
- Approver routing (#6596). The evaluator does not know approver routing
  (`POLICY_APPROVER_ROUTING`): `Decision.approvers` and `may_approve`
  always use today's group rule. The test runs with routing off.

Also not in this file:

- Read scopes. The evaluator's read scope step is in
  `test_policy_evaluate.py`.

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
  and `backend/tests/migrations/test_migration_0168_add_policy_rule_columns.py`:
  the rule on audit rows, approval requests and the console, and the
  migration on PostgreSQL with the append-only trigger (#6044).
- `backend/tests/test_policy_evaluate_parity.py`: the parity test (see
  "The parity test").
- `backend/tests/test_policy_shadow_load.py`: the load check.
- `backend/tests/test_policy_explain.py`: `meho policy explain`: one
  answer per verdict, the floor, the switch off, a file that is not
  imported, a stored request, who may ask, and no audit row or request.
  `backend/tests/test_policy_explain_access.py`: who sees the policy, the
  made-up login and its grants, the approver groups of a stored request in
  both routing states, and a stored routing that is not valid.
  `backend/tests/test_operations_request_preview.py`: the preview's
  `policy_rule` and `approvers`. `cli/internal/cmd/policy/explain_test.go`.

## References

- Settings: `Settings.policy_engine_shadow` (`settings.py`).
- Related: [policy-classification.md](policy-classification.md),
  [agent-permission-model.md](agent-permission-model.md),
  [approvals.md](approvals.md).
