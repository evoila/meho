# Run approvals: approve the operation list of one automation run once (#4126)

## Overview

The automation add-on runs as a service principal (a machine login with
its own secret). Without help, each change it makes waits for approval,
one call at a time. A **run approval** lets a person read the list of
operations one run may use and approve it once.

1. **The add-on asks.** `POST /api/v1/service-principals/run-approvals`
   with the run reference (`work_ref`), the list of entries and a reason.
   The request is `pending`.
2. **A person approves it in MEHO**, on REST or with
   `meho approvals runs approve <id>`. The CLI shows the list and asks for
   `yes` first. The same group rule as for one request holds: a list with a
   `dangerous` operation needs a tenant admin.
3. **The add-on reads the result.** It polls
   `GET /api/v1/service-principals/run-approvals/{id}` until the status is
   `approved` (or `revoked`).
4. **The run calls MEHO** with the header `Meho-Work-Ref: <work_ref>` (or
   the `work_ref` of `call_operation`). A call that is on the list runs
   without a new approval.
5. **It ends** when the add-on calls `.../{id}/end`, when a person revokes
   it, or at `expires_at` (default 24 hours, at most 7 days).

**Design choice for the add-on gate (comment of 2026-10-21 on the issue).**
The add-on does not decide anything itself. It asks MEHO for a run
approval and waits; a person decides in MEHO (CLI or REST today; a page in
the web console can link to the same request later); the add-on reads the
status. MEHO records the decision; there is no second-person step.

## Key types

- `RunApproval` (`db/models.py`, table `run_approval`, migration `0143`):
  `principal_sub`, `work_ref`, `entries` (JSON), `reason`, `requested_at`,
  `expires_at`, `approved_at` / `approved_by_sub`, `revoked_at` /
  `revoked_by_sub` / `revoked_reason`, `ended_at`, `expired_at`. Rows are
  never deleted. The partial unique index `run_approval_open_run_idx`
  lets one run hold at most one open row (see below).
- `Tenant.run_approval_target_kinds_enabled` (migration `0143`, default
  `false`): the tenant setting for target kinds
  (`policies.run_approval_target_kinds`). Only the settings file sets it;
  turning it on applies only in the web console, on the settings file
  page (`/ui/settings`).
- `operations/run_approval_schemas.py`: `RunApprovalEntry`,
  `RunApprovalCreate`, `RunApprovalRevoke`, `RunApprovalRead`
  (`status`: `pending`, `approved`, `revoked`, `ended`, `expired`).
- `operations/run_approvals.py`: `RunApprovalService` (request, list, get,
  approve, revoke, end).
- `operations/run_approval_gate.py`: `consult_and_record_run_approval`,
  the call-time check.
- `operations/run_approval_notes.py`: `with_wait_notes`, the
  `entries_that_wait` notes of an answer.
- `api/v1/run_approvals.py`: the REST routes.
- `cli/internal/cmd/approvals/runs.go`: `meho approvals runs list|show|approve|revoke`.

## An entry

| Field | Meaning |
|---|---|
| `connector_id`, `op_id` | exact values; a `*` is refused |
| `target_id` | exactly that target |
| `target_product` | every target of that product (a target kind), so targets the run registers itself are covered. Only when the tenant setting is on; otherwise the request is refused (422), and an entry stored before the setting was turned off covers nothing |
| neither | only calls **without** a target. No target is not "any target" |

## One open request per run

One run (tenant, token subject, run reference) has at most one open
request at a time: a row that is not revoked, not ended and not recorded
as expired. `RunApprovalService.request` refuses a second one with 409
while the first is pending or approved and before its `expires_at`.

Two requests for the same run can arrive at the same time on two
workers, and both pass that check. The partial unique index
`run_approval_open_run_idx` (migration `0143`) lets only one of them
insert. The other insert fails inside a savepoint, and the caller gets
409 with the id of the row that was stored. The same shape as the
idempotency key of the approval queue (migration `0105`).

A row past `expires_at` covers nothing, but it still holds the run's
slot in the index. The next request of the same run writes `expired_at`
on it, in the same transaction as its own insert, and takes the slot.
The status of that row stays `expired`.

## Control flow at call time

The policy gate (`_validate.py::_service_grant_verdict`) runs for a
service principal whose call would wait for approval. First it asks
`policy/fixed_rules.py::fixed_rule_for` which fixed rule holds for the
call ([policy-evaluator.md](policy-evaluator.md)). Outside sandbox mode,
when it names a rule (a credential call, a change of rights or of the
policy, a destructive operation, a delete, a caller-address field), the
gate looks up no standing grant and no run approval. It logs
`policy_gate_fixed_rule_no_grant` with the rule id, and the call waits.
The wait reason the caller reads stays the same. In the other cases, and
in sandbox mode, it looks for a standing grant
([service-principal-grants.md](service-principal-grants.md)). When none
covers the call, it calls `consult_and_record_run_approval`:

- It reads the run reference from `work_ref_var` (bound from
  `Meho-Work-Ref`). No run reference: no cover.
- It never covers an operation that `service_grants.never_covered_reason`
  names: one that reads, writes or makes a credential (see below), one a connector marks
  for per-call approval, one that connects to an address the caller
  gives, one classified to always wait, or one with a delete sign (the
  `destructive` level, the HTTP verb `DELETE`, the `destructive` tag, a
  delete name or pattern). The standing grant check uses the same
  function. On top of it, a run approval never covers an operation that
  runs a command as root on the host. Those wait for a person on each call.
- It looks for a live row: same tenant, same token subject (`sub`), same
  run reference, approved, not revoked, not ended, before `expires_at`.
  The key is the `sub`, never a client name.
- An entry must match the connector id and op id exactly, and the target
  as in the table above. An exact target wins over a target kind.
- On a match it writes one `approval.decision` audit row (the same shape
  as a standing-grant use: `decision=auto-approved`,
  `reviewed_by=run_approval:<id>`, plus `run_approval_id`,
  `approved_by_sub`, `matched_by`) in its own transaction, before the
  call runs. It also binds `audit_run_approval_id`, so the call's own
  `DISPATCH` row carries `run_approval_id`. The dispatcher drops that
  binding when the call ends.
- The preview (`decide_only`) only reports the cover and writes nothing.

Every call still goes through the policy gate and the audit log. A call
that is not on the list waits for approval as before.

A multi-step operation is checked like with grants: the operation itself
and each write step must be on the list.

## Never covered: a command as root on the host

The automation design decided that a call that runs a command as root on a
host always needs its own approval, also inside a run. So a connector marks
such operations with `declare_root_shell(op_ids)` (`op_classes.py`; the
connector kit exports it). `is_root_shell(op_id)` reads the mark. It is
separate from the op class, because an op id has only one class.

- `RunApprovalService.request` refuses a list with a marked operation
  (422): "runs a command as root on the host; a person approves each
  call".
- At call time the gate never lets a run approval clear a marked
  operation. The call waits for its own approval.
- For a service principal a marked operation waits at every risk level,
  also `safe` (`_service_safety_gate_reason` in `_validate.py`). Without
  this, a `safe` one such as `rke2.etcd-snapshot.save` would run at once
  and never reach the run approval check.
- Standing grants and single approvals keep their rules: a standing grant
  can still cover a marked operation.

Marked today: the writes of `linux-ssh` that run through its sudo helper
(`linux.file.write`, `linux.service.control`, `linux.script.run`,
`linux.sysctl.write`, `linux.firewall.load`, `linux.firewall.confirm`),
the record and config writes of `bind9-ssh`, and the writes of
`rke2-ssh` (`rke2.token.rotate`, `rke2.node.service.restart`,
`rke2.node.config.update`, `rke2.etcd-snapshot.save`), and the writes of
`pfsense-ssh`. The pfSense SSH login `admin` is root, and each pfSense
write runs a `pfSsh.php playback` script: the gateway and static route
adds, the filter rule add and delete, the user enable and disable, and
the gateway, static route, alias, alias member and NAT deletes. A read with
a fixed command, for example `linux.firewall.show` or `pfsense.diag.ping`,
is not marked: it does not change the host.

## The steps of an approved multi-step operation, with `CUSTOM_POLICY` on

This is a different approval: a person approves one whole multi-step
operation, and its steps go ahead under that approval. With the switch
`CUSTOM_POLICY` off, a step keeps its own approval when it is
`destructive`, marked `requires_approval`, "always wait" or a credential
call. With the switch on, a step keeps its own approval also when one of
these holds (`operations/run_step_rules.py`):

- a fixed rule (`policy/fixed_rules.fixed_rule_for`), asked as a caller
  who is not in the web console: a change of rights or of the policy keeps
  its own approval whoever approved the run;
- a rule that no run approval covers (`never_covered_reason`, plus the
  root shell mark above, the same check as `_never_covered` here).

`fixed_rule_for` alone is not enough: it has no root-shell rule. A step
with only the plain "always ask" mark goes ahead under the run's approval.
These rules hold for a step of the step gate, which reads the stored facts
of the step's operation, and for a child call. In a run after an approval
they hold also when the step's policy answer lets it run at once.
Details: [approvals.md](approvals.md#steps-of-an-approved-multi-step-operation-with-custom_policy-on).

## Never covered: a call a person approves one by one

A connector marks an operation with `declare_per_call_approval(op_ids)`
(`op_classes.py`) when a person approves each of its calls.
`needs_per_call_approval(op_id)` reads the mark. Neither a run approval nor
a standing grant covers such an operation:

- `RunApprovalService.request` refuses a list with a marked operation (422).
- At call time the gate never lets a run approval or a standing grant clear
  a marked operation.

A platform admin can turn approval off for some of these operations, for
example the MSAD group membership changes, for the whole install or for one
tenant's own copy. Both the console and the REST API or CLI need a written
reason; the REST API and the CLI also need a fresh sign-in. The edit is written
to the audit log. After it, a person in the web console runs a `caution`
operation at once. A `dangerous` operation still waits because of its risk
level. A standing grant or a run approval still does not cover the operation.
MEHO refuses the edit for an operation registered as a change of rights
(`approval_off_rights_change`, see the last item of the list below), for a
destructive operation (`approval_off_destructive`), and for an operation with a
caller-address parameter (`approval_off_caller_address`). An operation that
writes or makes a credential keeps its approval: the edit does not end its
wait.

Marked today:

- `msad.group.add-member` and `msad.group.remove-member`. A group
  membership gives rights in the domain.
- `secret.move`. A move writes a secret into a store or into a file on a
  host, so a person checks each place and owner.
- Every operation registered as a change of rights
  (`register_grant_op`, `register_rights_change_op` in
  `operations/grant_approval.py`). The register functions set the mark
  themselves. Examples: `policy.classify` and the Keycloak writes that
  change what a token says or who has a role.

## Never covered: an operation that reads, writes or makes a credential

A call that reads, writes or makes a credential always waits for a
person. No standing grant, no agent grant and no run approval covers it.
The classes are `credential_read` (for example `k8s.secret.read_to_ref`
or `vault.kv.read`), `credential_write` (for example `vault.kv.put`) and
`credential_mint` (for example `vault.token.create`). The class comes from
the class stored on the row of an ingested operation, or from the op id (a
connector's `declare_op_classes`, or the core lists of
`broadcast/events.py`). An operation a connector marks with
`declare_credential_capable` counts too (today `k8s.exec` and
`proxmox.api.write`), and so does a `k8s.apply` whose manifest holds a
Secret or cannot be read. The rule is the one of the standing grants
(`service_grants.credential_grant_reason`,
[service-principal-grants.md](service-principal-grants.md#credential-calls)):

- For a service login outside sandbox mode, `fixed_rule_for` names the
  rule `credential_waits`. The gate then looks up no standing grant and no
  run approval, and logs `policy_gate_fixed_rule_no_grant`. It does not
  reach `_never_covered`.
- In sandbox mode, `_never_covered` in `run_approval_gate.py` refuses it,
  and logs `run_approval_refused_credential` with the reason "A standing
  grant, an agent grant or a run approval never covers 'OP': it reads,
  writes or makes a credential, so a person approves each call."
- Either way the call gets the same answer as with no run approval. The
  shadow evaluator (`policy/floors.py::run_approval_may_clear`) gives the
  same answer.
- When such a service call waits, its reason ends with "the operation
  reads, writes or makes a credential, so no standing grant, no agent
  grant and no run approval covers it; a person approves each call"
  (`_validate.CREDENTIAL_WAIT_NOTE`). The `approval.request` audit row of
  the wait keeps the reason of the policy gate in `payload.wait_reason`.
- This also holds for a row that a person approved before the upgrade
  that brought the rule: the gate reads the class at call time, not from
  the row.
- `RunApprovalService.request` refuses such an entry (`_entry_refusal`):
  by op id before the lookup, and by the stored class after it. The
  answer is "entries[N]: " plus the reason and "Leave it out."
- A request made before this rule can still list such an entry. The
  answer of show and approve has `entries_that_wait`: one note per such
  entry whose calls wait (`service_grants.credential_call_waits`), with
  its position (`entry`), `connector_id`, `op_id` and the reason "reads,
  writes or makes a credential. A run approval never covers it: each call
  waits for a person" (`operations/run_approval_notes.py`). For
  `vault.kv.read` the reason is "reads a credential. A run approval never
  covers it: each call with reveal_secret waits for a person". A read
  that returns no secret value gets no note. Request answers have an empty
  list. The list, revoke and end routes leave the field `null`, because
  they look up no operation.
  `meho approvals runs show` and `approve` print the reason under the
  entry.

## Approver routing

With the `POLICY_APPROVER_ROUTING` switch on, each entry of the list is
routed like a single request ([approvals.md](approvals.md), "Approver
routing"). When a person approves the list, `_check_routed_entry` works
out the login group kinds that may approve each entry, from the policy
file's `routing` section, the entry's risk level and connector in this
tenant, and its category when the lookup gave one. A person whose group
kind is not among them gets 403 `approval_group_required` with the entry
and one sentence that names who may approve. A change of rights is not
routed. A run approval stores no kinds: they are worked out at the
approve. With the switch off, nothing changes.

## The people behind the service login

`RunApprovalService.approve` looks up the people behind the service login
that asked (`_check_people_behind`): who paired or owns its add-on and who
gave one of its standing grants in force. MEHO finds the add-on pairing by
the login id, and by the client id it stored on the list when the add-on
asked (`run_approval.principal_client_id`). The lookup runs at approve time,
so a list that waited across an upgrade follows the rule too.

- A person behind the login may approve the list only when they may approve
  their own change (`Operator.may_self_approve`). Else `403`
  `own_change: ...`.
- MEHO knows nobody behind the login: only an admin may approve. Else `403`
  `requester_not_known: ...`.
- An entry that is a change of rights: never the requester and never a
  person behind it (`rights_change_required`).

See [The people behind a request](approvals.md#the-people-behind-a-request).

## REST

| Verb | Path | Who |
|---|---|---|
| `POST` | `/api/v1/service-principals/run-approvals` | a service principal; others get 403 `service_principal_required`. 422 for a delete entry, an entry that runs a command as root on the host, a target kind the tenant does not allow, a bad expiry; 409 when the run already has a live request. 422 also for an entry that reads, writes or makes a credential |
| `GET` | `/api/v1/service-principals/run-approvals` | approvals access (`operator` role or `approver`); filters `principal_sub`, `status` |
| `GET` | `.../{id}` | approvals access; 404 across tenants |
| `POST` | `.../{id}/approve` | a person with approvals access and a fresh approval sign-in, like approving one request: a machine token gets 403 `human_principal_required`, a stored login token gets 401 with the RFC 9470 challenge. 409 when not pending; 403 `role_floor_required` when an entry needs a higher role; 403 `approval_group_required` when an entry's operation is `dangerous` (or `destructive`) and the person is not a tenant admin (MEHO-Admins), the same group rule as approving one request ([decision](../decisions/approval-groups-and-self-approval.md)); 422 when MEHO cannot find an entry's operation in the tenant, because its risk level is not known |
| `POST` | `.../{id}/revoke` | a person with approvals access. Body `{"reason": ...}` (optional). Works on a pending request too (turns it down) |
| `POST` | `.../{id}/end` | the principal that asked; 404 for anyone else |

There is no MCP tool. Approving is a person's decision (v0.1-spec §7).
`test_run_approvals_rest_plane_has_cli_parity` checks the CLI verbs.

## Known limits

- No web console page yet. The add-on can show the request id and the
  CLI command.
- No push to the add-on when the decision is made; the add-on polls the
  show route.

## Tests

`tests/test_run_approvals.py` (call time, dispatch, revoke, end, expiry,
destructive, root commands, tenants, target kinds),
`tests/test_run_approval_credential_reads.py` (every registered credential
read, from the registry; the gate and the shadow evaluator agree; the
notes of show and approve), `tests/test_credential_calls_always_wait.py`
(every registered credential read, write and mint, from the registry;
refusals at create time), `tests/test_api_v1_run_approvals.py`
(REST rules), `tests/test_run_approvals_routing.py` (approver routing), `tests/migrations/test_migration_0143_add_run_approval.py`,
`cli/internal/cmd/approvals/runs_test.go`.
