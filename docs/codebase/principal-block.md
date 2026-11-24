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
- A platform admin also unblocks with any role in the tenant.

Who may remove a block, and the way back for a platform admin:

- A platform admin whom a tenant admin blocked in a tenant removes that
  block on their own login, from that tenant. This is their way back. It
  holds on an install with one tenant too. Every other route of that
  tenant still refuses the person until the block is gone.
- A blocked platform admin removes only their own block, never the block
  of another person.
- A block that a platform admin made (scope `*`) stays until a platform
  admin who is not blocked removes it. The person under it gets 403 on
  unblock.
- A tenant admin who is blocked in their tenant gets 403 on unblock. A
  tenant admin or a platform admin who is not blocked removes the block.
- The platform admin flag comes from the person's own token (the claim or
  the realm role). A token without it, or with a malformed one, has no way
  back through this rule.
- When the block list cannot be read, the unblock answers 503, like every
  other route.

A blocked person reads: "This person is blocked in MEHO. Ask an admin."
The text names no admin and no reason.

## Key types

- Table `principal_block` (`PrincipalBlock`, migration `0186`): one row
  for each `(sub, scope)`. `scope` is `*` or the tenant id. It holds the
  reason, the admin's `sub` and the time.
- `meho_backplane/auth/principal_block.py`: the reader with its cache,
  and `block_principal`, `unblock_principal`, `list_blocked`.
- `meho_backplane/api/v1/principal_blocks.py`: the REST routes.
- `meho_backplane/operations/_principal_block_gate.py`: the check for
  operation calls.
- `meho_backplane/auth/principal_block_run.py`: what one running operation
  sent, and the step the block list refused in it.
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
| `dispatch()` (`operations/dispatcher.py`) | Every top-level operation call, also the calls of hosted agent runs and scheduled runs. Each step of a multi-step operation that runs through `dispatch()` checks again before it is sent. A `denied` result, and the vendor gets no call. |
| `enforce_subop_policy`, `run_governed_subop` (`operations/composite.py`) | Each governed direct step of a multi-step operation, also a step that is sent again on a resume. The step is refused before it is sent, and the operation returns the refusal. |
| `HttpConnector._request_json`, `HttpConnector._post_json` (`connectors/adapters/http.py`) | Each request that a multi-step operation sends straight to the target through the connector's own session (for example the GitHub commit steps). The request is not sent; `dispatch()` returns the same refusal. Outside a multi-step operation and outside an approved run, this check does nothing: the top-level call checked the list already. |
| `PrincipalBlockGate` (`agent/principal_block_gate.py`) | Every tool call and every model request of a hosted agent run and of its child runs (`invoke_agent`). The tool does not run, the model gets no further request, and the run ends failed with the refusal text. The audit row of the refused tool call has status 403 (503 when the list cannot be read). |
| `invoke_agent` (`agent/invoke.py`) | A child run that is refused because its person is blocked ends failed with the refusal text, and its heartbeat stops. The refusal goes on to the parent run: the parent stops with no retry and ends failed with the same text. |
| `refuse_resume_if_asker_blocked` (`operations/approval_queue.py`) | An approved call does not run while the person who asked is blocked. The request is refused like a release change: the claim is taken, one audit row is written, and the refusal is the stored result. The in-process agent waiter calls it too. |

A block made while a multi-step operation runs stops its next step, and
the whole operation with it. The operation answers with the refusal, also
when its handler catches the error of the step. An inventory read
(`linux.migration.inventory` and the others) stores no inventory then. No
further step starts. A list that cannot be read stops the operation the
same way.

The answer says what was sent (`execution_outcome`):

- `not_started`: nothing was sent. When the list cannot be read, the text
  says "Try again."
- `uncertain`: an earlier step or request of the same operation was sent,
  also in the run before an approval. The text says that a part of the
  operation may have run, and not to run it again before the target is
  checked. A request counts as sent when the HTTP connector base sends it.
  A step on another kind of connector (SSH, a vendor library) counts from
  its start.

When a target session ends and the dispatcher sends a step again, a refusal
before the second try is the same answer as on the first try.

The audit row of the stopped operation has the status `denied` (403) with
the code `principal_blocked`. When the list cannot be read it has the status
`error`.

Each step checks its own operator and every login bound with
`checking_logins_for_steps` (`auth/principal_block.py`): the operator of
the multi-step operation, and on an approved run also the person who asked.
So when an approver runs an approved multi-step operation and the person
who asked is blocked between two steps, the next step is refused. This
holds for a step through `dispatch()`, a governed direct step, and a
request sent straight to the target.

A new request of a blocked person is refused within 5 seconds. An open
live stream is not cut at once:

- The console's live stream (`/ui/broadcast/stream`) ends at its next
  session check, at most `ui_session_read_revalidation_seconds` later
  (300 seconds by default).
- `GET /api/v1/feed` ends when the person's token ends (or after
  `feed_stream_max_seconds`, when that comes first).

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
  is none. This route uses its own sign-in check
  (`verify_jwt_platform_admin_way_back` in `auth/jwt.py`,
  `verify_jwt_and_bind_platform_admin_way_back` in `middleware.py`): a
  platform admin passes a block of the current tenant and reaches the
  route, which lets a caller who is blocked there remove only their own
  block. A block in every tenant always refuses.

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
