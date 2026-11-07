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
  `destructive` change always waits for a person.

More rules:

- An agent never approves. Approve and reject have no MCP tool.
- An approval needs a recent sign-in with a password. On the command
  line, one sign-in covers 15 minutes. A `destructive` operation needs a
  sign-in of its own, at most 5 minutes old.
- An approved request runs only on the target and the preview that the
  person approved. If either changed, MEHO sends nothing and the
  requester asks again.
- Only a member of the `MEHO-SA-Operators` group approves their own
  change. For everyone else, a second person must approve. For another
  person's `dangerous` or `destructive` change, the approver must be an
  SA-Operator or an admin.
- A request that waits keeps secret values out of its stored
  parameters, and the approver sees them hidden.
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
