# Approvals

MEHO does not let a change that matters run silently. It waits until a
person approves.

## What it does

When an operation needs approval, it does not run at once. MEHO stores
the request, answers `awaiting_approval`, and changes nothing on the
system. The request runs only after a person approves it. The approver
sees what will run, on which system and at which risk level. The audit
log records who approved, how, and when.

What waits depends on how the call arrives:

- **Web interface:** `safe` and `caution` operations run at once.
  `dangerous` and `destructive` operations wait, and so does any
  operation marked `requires_approval`.
- **A token, and every agent** (MCP, the `meho` command line and REST):
  only `safe` operations run at once. Every other change waits.
- **Service principals** (fixed automation, for example a build
  pipeline) keep a rule of their own. A standing grant, which a person
  makes in advance, lets their `caution` and `dangerous` changes run. A
  `destructive` change waits for a person. The one exception is an
  instance in sandbox mode, which holds only disposable test systems.
  There a tenant admin may give a service principal a standing grant
  that also covers deletes.

More rules:

- An agent never approves. Approve and reject have no MCP tool.
- An approval needs a recent sign-in with a password. On the command
  line, one sign-in covers 15 minutes. A `destructive` operation needs a
  sign-in of its own, at most 5 minutes old.
- An approved request runs only on the target and the preview that the
  person approved. If either changed, MEHO sends nothing and the
  requester asks again.
- As a rule, a second person must approve. For another person's
  `dangerous` or `destructive` change, the approver must be a member of
  the `MEHO-SA-Operators` group or an admin. A person may approve their
  own change in three cases only:
    - A member of the `MEHO-SA-Operators` group may approve their own
      change at every risk level.
    - An operator or an admin may approve their own `caution` change
      when it waited only because they sent it with a token. This works
      only with a click in the web interface.
    - When an admin turns on the audited emergency switch for the whole
      deploy, a person may approve their own `safe` or `caution` change.
      The switch never covers a `dangerous` or `destructive` change.
- The approver does not see the stored parameters of a request. The
  approver sees the preview, and the preview hides secret values.
- A request that waits stores the parameters of the call, because MEHO
  runs the approved call with them. A secret value in those parameters,
  for example a password in a deployment spec, stays stored until the
  run starts, or until the request is rejected, withdrawn or expires.
  Then MEHO replaces it with a marker.
- The web interface and the command line show for each request whether
  you may approve it, and why not.

## Product versions

Approvals work the same for every connector and every product version.
The rules for what waits are set per operation, by risk level. There is
no exception per system.

## State

- **Shipped.** Approvals are a GA feature in the
  [maturity index](../reference/maturity.md).
- Tokens and agents run only `safe` operations at once from v0.45.0.
- A fresh sign-in for every approval, also in the web interface, comes
  from v0.42.0.
- An approved request that runs only as approved comes from v0.40.0.

## Operator guide

- [Approvals and break-glass](../guides/approvals-and-break-glass.md):
  who may approve, how to approve in the web interface or on the
  command line, and how one operator approves their own change.
- [Audit forensics](../guides/audit-forensics.md): how to show who
  approved what.
- [Secret values stay hidden](hidden-secrets.md): what an approver sees
  in a preview.
