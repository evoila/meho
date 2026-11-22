# The admin block list

## Overview

An admin can block a person in MEHO. MEHO then refuses every request of
that login within 5 seconds, on every MEHO process.

MEHO checks sign-in tokens itself, with no question to the login server
for each request. A person who is disabled in the login server keeps
working until the token ends. The block list closes that time.

Who may block:

- A platform admin blocks a person in every tenant (scope `*`).
- A tenant admin blocks a person in their own tenant. The block never
  reaches another tenant.
- Nobody blocks their own login (422 `cannot_block_self`).
- Only a person with `tenant_admin` can block, unblock or list. An agent,
  service or runner login gets 403. There is no MCP tool.

A blocked person reads: "This person is blocked in MEHO. Ask an admin."
The text names no admin and no reason.

## Key types

- Table `principal_block` (`PrincipalBlock`, migration `0182`): one row
  for each `(sub, scope)`. `scope` is `*` or the tenant id. It holds the
  reason, the admin's `sub` and the time.
- `meho_backplane/auth/principal_block.py`: the reader with its cache,
  and `block_principal`, `unblock_principal`, `list_blocked`.
- `meho_backplane/api/v1/principal_blocks.py`: the REST routes.
- `meho_backplane/operations/_principal_block_gate.py`: the check for
  operation calls.
- `meho_backplane/agent/principal_block_gate.py`: the check for hosted
  agent runs (`PrincipalBlockGate`).
- CLI: `cli/internal/cmd/principal/`.

## Control flow

The reader keeps the whole list in memory for 5 seconds per process
(`BLOCK_CACHE_SECONDS`). A block or unblock clears the cache of the
process that made it. Every other process sees the change when its cache
is 5 seconds old. A process that cannot read the table refuses the
request (503 `principal_block_check_unavailable`); it never answers "not
blocked" for an unknown state.

Where the list is checked:

| Place | What happens |
|---|---|
| `verify_jwt_for_audience` (`auth/jwt.py`) | Every REST and MCP request. 403 `principal_blocked`. It runs after the current tenant is chosen. |
| `UISessionMiddleware` (`ui/auth/middleware.py`) | Every console request, not only the 300-second token check. 403 with the plain text above. |
| `dispatch()` (`operations/dispatcher.py`) | Every top-level operation call, also the calls of hosted agent runs and scheduled runs. A `denied` result, and the vendor gets no call. |
| `PrincipalBlockGate` (`agent/principal_block_gate.py`) | Every tool call and every model request of a hosted agent run and of its child runs (`invoke_agent`). The tool does not run, the model gets no further request, and the run ends failed with the refusal text. The audit row of the refused tool call has status 403 (503 when the list cannot be read). |
| `refuse_resume_if_asker_blocked` (`operations/approval_queue.py`) | An approved call does not run while the person who asked is blocked. The request is refused like a release change: the claim is taken, one audit row is written, and the refusal is the stored result. The in-process agent waiter calls it too. |

A hosted agent run acts as the person who started it, so it stops when
that person is blocked. A scheduled run acts as the agent's own login, so
it is refused only when that login is blocked.

A block in one tenant holds only where the person works in that tenant.
A person with several tenants who has not picked one yet is checked
against blocks in every tenant only. So the person can sign in and pick
a tenant where no block holds. On the console's tenant picker and switch
(`/ui/tenant`, `/ui/tenant/switch`) a block in the session's tenant does
not hold either; the switch checks the tenant it moves to and refuses a
tenant where the person is blocked. A block in every tenant holds
everywhere, also at sign-in and on the picker.

Block and unblock each write one audit row in the same transaction as
the change (`op_id` `principal.block` or `principal.unblock`). The block
row holds the admin, the blocked `sub`, the scope and the reason the
admin typed. The unblock row holds the admin, the `sub` and each removed
block with its scope and reason (`removed_blocks`).

REST routes (`/api/v1/principals/blocked`):

- `GET`: the list. A platform admin sees every block, a tenant admin sees
  the blocks of their tenant.
- `POST` with `{"sub", "reason"}`: block. A second block in the same
  scope updates the reason.
- `DELETE /{sub}`: unblock. A platform admin removes every block of the
  login. A tenant admin removes the block of their tenant. 404 when there
  is none.

CLI: `meho principal block <sub> --reason <text>`,
`meho principal unblock <sub>`, `meho principal blocked`.

## Dependencies

- `session_store/audit.py` (`add_audit_row`) for the audit row.
- `auth/rbac.py` (`require_role`, `ensure_human_principal`) for who may
  call the routes.

## References

- `docs-site/install/keycloak-realm.md`: how to disable a person in the
  login server, and why to block the person in MEHO too.
- `docs/codebase/approvals.md`: the approved call and its resume checks.
