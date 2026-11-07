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
   `yes` first.
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

- `RunApproval` (`db/models.py`, table `run_approval`, migration `0141`):
  `principal_sub`, `work_ref`, `entries` (JSON), `reason`, `requested_at`,
  `expires_at`, `approved_at` / `approved_by_sub`, `revoked_at` /
  `revoked_by_sub` / `revoked_reason`, `ended_at`. Rows are never deleted.
- `Tenant.run_approval_target_kinds_enabled` (migration `0141`, default
  `false`): the tenant setting for target kinds. It is set with the
  settings file (`policies.run_approval_target_kinds`).
- `operations/run_approval_schemas.py`: `RunApprovalEntry`,
  `RunApprovalCreate`, `RunApprovalRevoke`, `RunApprovalRead`
  (`status`: `pending`, `approved`, `revoked`, `ended`, `expired`).
- `operations/run_approvals.py`: `RunApprovalService` (request, list, get,
  approve, revoke, end).
- `operations/run_approval_gate.py`: `consult_and_record_run_approval`,
  the call-time check.
- `api/v1/run_approvals.py`: the REST routes.
- `cli/internal/cmd/approvals/runs.go`: `meho approvals runs list|show|approve|revoke`.

## An entry

| Field | Meaning |
|---|---|
| `connector_id`, `op_id` | exact values; a `*` is refused |
| `target_id` | exactly that target |
| `target_product` | every target of that product (a target kind), so targets the run registers itself are covered. Only when the tenant setting is on; otherwise the request is refused (422), and an entry stored before the setting was turned off covers nothing |
| neither | only calls **without** a target. No target is not "any target" |

## Control flow at call time

The policy gate (`_validate.py::_service_grant_verdict`) runs for a
service principal whose call would wait for approval. It first looks for
a standing grant ([service-principal-grants.md](service-principal-grants.md)).
When none covers the call, it calls `consult_and_record_run_approval`:

- It reads the run reference from `work_ref_var` (bound from
  `Meho-Work-Ref`). No run reference: no cover.
- It never covers an operation with a delete sign (the same rule as the
  standing grants, `service_grants.delete_grant_reason`: the
  `destructive` level, the HTTP verb `DELETE`, the `destructive` tag, a
  delete name or pattern) or an operation that connects to an address the
  caller gives. Those wait for a person on each call.
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

## REST

| Verb | Path | Who |
|---|---|---|
| `POST` | `/api/v1/service-principals/run-approvals` | a service principal; others get 403 `service_principal_required`. 422 for a delete entry, a target kind the tenant does not allow, a bad expiry; 409 when the run already has a live request |
| `GET` | `/api/v1/service-principals/run-approvals` | approvals access (`operator` role or `approver`); filters `principal_sub`, `status` |
| `GET` | `.../{id}` | approvals access; 404 across tenants |
| `POST` | `.../{id}/approve` | a person with approvals access and a fresh approval sign-in, like approving one request: a machine token gets 403 `human_principal_required`, a stored login token gets 401 with the RFC 9470 challenge. 409 when not pending; 403 `role_floor_required` when an entry needs a higher role |
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
destructive, tenants, target kinds), `tests/test_api_v1_run_approvals.py`
(REST rules), `tests/migrations/test_migration_0141_add_run_approval.py`,
`cli/internal/cmd/approvals/runs_test.go`.
