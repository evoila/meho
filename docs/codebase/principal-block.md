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
- Only a person can block, unblock or list. An agent, service or runner
  login gets 403. There is no MCP tool.
- Block and list need `tenant_admin`. Unblock needs `tenant_admin`, or a
  platform admin who removes their own block.

Who removes a block:

- A tenant admin removes the blocks of their own tenant.
- A platform admin with `tenant_admin` in the current tenant removes every
  block of another person's login.
- Only a platform admin removes a block that a platform admin made (scope
  `*`). The person under it gets 403 on unblock.
- A tenant admin who is blocked in their tenant gets 403 on unblock.

A platform admin's way back:

- A platform admin whom a tenant admin blocked removes that block on their
  own login, in the tenant of the block:
  `meho principal unblock <sub> --reason "<why>"`.
- This call needs a reason of 1 to 500 characters and a fresh approval
  sign-in: the same check as an approve (`check_fresh_approval_sign_in` in
  `auth/approval_sign_in.py`). Without a reason MEHO answers 422
  `unblock_reason_required`; without a fresh sign-in, 401
  `fresh_sign_in_required`. The block stays in both cases.
- The audit row `principal.unblock` holds the reason and the sign-in
  (`sign_in`).
- The way back uses the approval sign-in client of the login server
  (`meho-cli-approve` in MEHO's own setup). An install without that client
  has no way back; another platform admin or a tenant admin of that tenant
  removes the block then.
- On their own login, MEHO removes only the block of the current tenant.
  It reads the table for this, not the list it keeps for 5 seconds.
- A platform admin who is blocked in the current tenant removes only their
  own block there. Every other route of that tenant refuses them until the
  block is gone.
- The platform admin flag comes from the person's own token (the claim or
  the realm role).
- When the block list or the table cannot be read, the unblock answers 503
  `principal_block_check_unavailable`, like every other route.
- The console never removes a block on the admin's own login. A platform
  admin removes a block on their own login with
  `meho principal unblock <sub> --reason <text>`, run in the tenant of that
  block, with a fresh sign-in. It removes only the block of that tenant. A
  block in every tenant stays until another platform admin removes it. A
  tenant admin cannot remove a block on their own login. Another tenant admin of that tenant removes a block of that tenant, and a platform admin removes any block, on this page or with `meho principal unblock`. Only a platform admin removes a block in every tenant.

To stop a platform admin: another platform admin blocks the person in
every tenant. In the login server, take the platform admin role away and
end the person's sessions and long logins.

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
- `meho_backplane/ui/routes/principal_blocks.py`: the console page
  `/ui/blocked-people`.
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
| `raise_if_send_refused` (`auth/principal_block.py`) | A check that MEHO runs at fixed points while a call runs. A call that reaches it while its login is blocked ends with the block refusal; `dispatch()` returns it. |
| `PrincipalBlockGate` (`agent/principal_block_gate.py`) | Every tool call and every model request of a hosted agent run and of its child runs (`invoke_agent`). The tool does not run, the model gets no further request, and the run ends failed with the refusal text. The audit row of the refused tool call has status 403 (503 when the list cannot be read). |
| `invoke_agent` (`agent/invoke.py`) | A child run that is refused because its person is blocked ends failed with the refusal text, and its heartbeat stops. The refusal goes on to the parent run: the parent stops with no retry and ends failed with the same text. |
| `refuse_resume_if_asker_blocked` (`operations/approval_queue.py`) | An approved call does not run while the person who asked is blocked. The request is refused like a release change: the claim is taken, one audit row is written, and the refusal is the stored result. The in-process agent waiter calls it too. |

MEHO checks the block again at fixed points while a call runs
(`raise_if_send_refused`). A call that reaches such a point ends with the
block refusal, and the answer says whether a part of it may have run. A
request that is already on its way to a system runs to its end. The check
reads the login that the dispatcher noted for the running call
(`tracking_run` in `auth/principal_block_run.py`) and every login bound with
`checking_logins_for_steps`. It reads a copy of the list that is at most 5
seconds old, so it reads the database at most once in 5 seconds per process.

Once a check of a run refused, every later check of the same run refuses
too, also when the list can be read again. The operation answers with the
refusal, also when its handler catches the error of the step. An inventory
read (`linux.migration.inventory` and the others) stores no inventory then.
A list that cannot be read stops the call the same way. The answer says
that the list could not be read.

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
live stream of the person ends within a few seconds too. The console's
live stream (`/ui/broadcast/stream`) and `GET /api/v1/feed` each read the
block list in their liveness check (`StreamLiveness.still_valid`), at every
check and not only at the session check. A list that cannot be read ends the
stream as well. A stream that waits for an announcement checks again after
2 seconds at the latest, and its check accepts a copy of the list that is
at most 2 seconds old. So a block ends the stream within 4 seconds.

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
- `DELETE /{sub}`: unblock. A platform admin with `tenant_admin` removes
  every block of the login, but on their own login only the block of the
  current tenant. A tenant admin removes the block of their tenant. 404
  when there is none. On the caller's own login the call needs the body
  `{"reason": "..."}` (1 to 500 characters, else 422
  `unblock_reason_required`) and a token of a fresh approval sign-in (else
  401 `fresh_sign_in_required` with the `WWW-Authenticate` challenge).
  Both are checked before any block is read. On another person's login
  the reason is optional and goes into the audit row. This route uses its own sign-in check
  (`verify_jwt_platform_admin_way_back` in `auth/jwt.py`,
  `verify_jwt_and_bind_platform_admin_way_back` in `middleware.py`): a
  platform admin passes a block of the current tenant and reaches the
  route, which lets a caller who is blocked there remove only their own
  block. A block in every tenant always refuses.

CLI: `meho principal block <sub> --reason <text>`,
`meho principal unblock <sub> [--reason <text>]`, `meho principal blocked`.
On a 401 with the challenge, `unblock` opens the browser approval sign-in
(the same way as `meho approvals approve`) and sends the call again with
that token; the token stays in memory for this one run.

Console: the page `/ui/blocked-people` lists the blocks the admin may see
(login id, tenant, reason, who blocked, when), blocks a login with a
reason, and unblocks a login after a confirm step. It calls the same
functions as the REST routes, so the rules are the same: only a person
with `tenant_admin` may open the page or send a form (403 for anyone
else), a tenant admin works in their own tenant, a platform admin in
every tenant, and blocking your own login shows a refusal and writes no
block. The admin check uses the signed-in session only. The unblock
button of a row opens a confirm page; only its form (`confirm=yes`)
removes blocks. The confirm page names the login, its blocks and the
scope: for a platform admin the button reads "Unblock in all tenants",
and the page says that every block of the login goes, also the blocks of
tenant admins. The page shows no button on the admin's own login row, and
the unblock form answers 403 `own_login` for it. The console never removes
a block on the admin's own login. A platform admin removes a block on their
own login with `meho principal unblock <sub> --reason <text>`, run in the
tenant of that block, with a fresh sign-in. It removes only the block of
that tenant. A block in every tenant stays until another platform admin
removes it. A tenant admin cannot remove a block on their own login.
Another tenant admin of that tenant removes a block of that tenant, and a platform admin removes any block, on this page or with `meho principal unblock`. Only a platform admin removes a block in every tenant. The own-login hint of the page
says the rule for the role of the viewer. When MEHO cannot read the blocks, the page and
both steps of an unblock answer 503 `principal_block_check_unavailable`. Every form is a plain
`POST` form with the CSRF token and the page's tenant (`meho_tenant`)
in hidden fields, so it works without JavaScript, and a form from a page
of another tenant answers 409. A done form answers 303 back to the page. A refused block
answers 422 and shows the page with the message next to its field. Each
block and unblock writes one audit row (`principal.block` or
`principal.unblock`). The row of a page action names the console route
(`POST /ui/blocked-people`, `POST /ui/blocked-people/unblock`); the rows of
REST calls name the REST method and path. The request row of the form keeps
its own `http.post:` name. The menu entry "Blocked people" shows only for a
person with `tenant_admin` (the session context holds it, set at login and
at each token check); the page and both forms keep their own 403.

## Dependencies

- `session_store/audit.py` (`add_audit_row`) for the audit row.
- `auth/rbac.py` (`require_role`, `ensure_human_principal`) for who may
  call the routes.

## References

- `docs-site/install/keycloak-realm.md`: how to disable a person in the
  login server, and why to block the person in MEHO too.
- `docs/codebase/approvals.md`: the approved call and its resume checks.
