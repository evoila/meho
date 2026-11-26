# Who is calling: person, personal agent, service agent

Every call to MEHO comes from someone or something. Who calls decides
whether a call runs at once or waits until a person approves it.

This page says:

- which kinds of caller MEHO knows;
- how MEHO tells them apart;
- what runs at once for each kind today.

## The simple rule

- **Person:** a human who signed in to the web console. Only the web
  console runs a call at once as a person's own decision.
- **Personal agent:** a tool that uses a person's own login. Examples:
  an AI assistant, a script, the `meho` command line. It needs no setup.
  It can never do more than the person may do.
- **Service agent:** an identity that was created for a machine. It has
  its own grants (rights given to it in advance). An agent login and a
  service account also have their own login. A hosted agent uses its own
  login in the runs that MEHO starts on its own (by the scheduler). A
  service agent runs without a person watching.

MEHO cannot see whether an AI or a script uses a machine identity. So
both get one name: service agent. The grants of the identity say what
it may do.

!!! note "The names change nothing today"

    The three names come from the decision record
    [The custom policy model](https://github.com/evoila/meho/blob/main/docs/decisions/2026-11-25-custom-policy-model.md).
    Today they are names only. The rules in the table below are the
    rules MEHO uses now.

## The kinds of caller

MEHO refuses some calls at once, with no approval request. Examples:
the person's role is lower than the role the operation needs
(registering a target needs `tenant_admin`), or the operation's own
check of who may ask says no (for example an agent or an Operator who
asks to sign a tool out). The table is about calls the caller may make.

An operation marked `requires_approval` waits for approval, unless a
row of the table names a case where it runs (for example a service
account with a standing grant or a run approval for it), or a platform
admin has turned approval off for it.

| Who calls | How MEHO knows | What runs at once today | Name |
|---|---|---|---|
| A person in the web console | The person signed in to the web console. Only the console's own sign-in marks a call as "web console". No token can do that. | `safe` and `caution`. `dangerous` and `destructive` operations wait. So do operations marked `requires_approval`. A change of rights by an admin can run at once only when the admin starts it with a click in the web console after a fresh sign-in. In MEHO's own login realm only a platform admin's call can run at once. Every other change of rights waits for approval, for example a call with a token (also the admin's own), a call by an Operator in the web console, a call by an agent or in a hosted agent run, a call by a tenant admin in MEHO's own login realm, and a call whose approval card cannot be built. A `destructive` change of rights never runs at once. See [Changes of rights](approvals-and-break-glass.md#changes-of-rights). | person |
| A person's tool: MCP (for example an AI assistant), the `meho` command line, or REST | The person's own login, used by a tool. The token comes from the person's own sign-in. | Only `safe` operations that need no approval run at once. A `safe` operation marked `requires_approval` waits too. Everything else waits. | personal agent |
| An agent login | An identity that an admin registered for an AI agent (`meho agent-principal register`). It has its own login and its own grants. | Only `safe` operations that need no approval run at once. A `safe` operation marked `requires_approval` waits too. No grant lets a higher level run at once. A grant with the verdict `deny` refuses the call. A grant with the verdict `needs-approval` makes a `safe` call wait. An agent with the role `read_only` waits on a `safe` call, also with an `auto-execute` grant. The setting `AGENT_GRANT_REQUIRED` has three values: `off` (the default), `observe` (MEHO only logs the call that it would refuse) and `on` (a call that no grant covers is refused, also a `safe` one). | service agent |
| A service account | An identity for a program that is not an AI, for example a build pipeline. It has its own login. | `safe` operations and `caution` operations that only read run at once, unless the operation is marked `requires_approval`. A call that writes or makes a credential, and an operation that runs a command as root on the host, wait also at the level `safe`. A person can limit the targets a service account may read. Then MEHO refuses each call of that account that would run at once (a `safe` call or a `caution` read) on a target outside the limit. A call with no target is refused the same way when the limit has no "no target" entry. A call that waits for approval (for example an operation marked `requires_approval`, or a change) still waits outside the limit, and runs after approval. A `caution` change or a `dangerous` change runs at once when a standing grant covers it, or when a person approved the operation list of this automation run (a run approval). A standing grant and a run approval also let an operation marked `requires_approval` run at once. No grant and no run approval covers a call that shows a secret value, writes a credential or makes one; such a call waits. A read that hides every value or returns only names runs like any read of its risk level. A standing grant and a run approval also never cover: an operation that needs a person for each call; an operation that connects to an address the caller gives; an operation with the always-wait mark; an operation with a delete sign (for example the HTTP verb `DELETE`, also at level `dangerous`). Each of these waits for a person. A standing grant does cover an operation that runs a command as root on the host; a run approval does not. `destructive` always waits. | service agent |
| A hosted agent run (experimental) | MEHO runs the agent on its own server. A run started by hand (`meho agent run`) acts for the person who started it. MEHO checks the agent's grants and that person's rights. The stricter answer wins. A run that MEHO starts on its own (by the scheduler) uses the agent's own login and its grants, as in the agent login row. See [Who the grants apply to](hosted-agents.md#who-the-grants-apply-to). | In both kinds of run, only `safe` operations that need no approval run at once. With `AGENT_GRANT_REQUIRED` on, a call that no grant of the agent covers is refused, also in a run started by hand. | service agent |

### When approval is turned off for an operation

Only a platform admin can turn approval off for an operation marked
`requires_approval`, for the whole install or for one tenant's own copy
of the operation.

- In the web console the platform admin gives a reason.
- On the command line and over REST the platform admin gives a reason
  and must have signed in again a short time before (5 minutes by
  default, the setting `PER_ACTION_SIGN_IN_MAX_AGE_SECONDS`).
- An AI tool over MCP cannot turn approval off.

With approval off, each row of the table applies as for an operation
with no mark. For example:

- a `safe` operation then runs at once;
- a `caution` operation runs at once for a person in the web console,
  and a `caution` read also for a service account;
- at `caution`, a call by token, by an agent or in a hosted agent run
  still waits;
- a `dangerous` operation still waits, except for a service account that
  a standing grant or a run approval covers.

Approval cannot be turned off for a `destructive` operation, for a
change of rights, or for an operation that connects to an address the
caller gives.

With approval off, nothing of this changes:

- A call that writes or makes a credential still waits for a person,
  from every caller.
- A credential read that shows the secret value (for example
  `vault.kv.read` with `reveal_secret`) waits for a person when a person
  or a service account sends it.
- A service account's call that asks to show hidden values waits for a
  person on any read.
- MEHO refuses every call from an agent that asks to show hidden values,
  also in a hosted agent run.

## Rules for every kind of caller

- A person approves in the web console, or on the command line after a
  fresh sign-in in the browser. See
  [Approve on the command line](approvals-and-break-glass.md#approve-on-the-command-line).
- The person needs the role Operator or higher, or the approver flag. A
  person with the role `read_only` and no approver flag cannot approve.
- Only an Admin approves another person's `dangerous` change.
- A change of rights is approved only by an admin, and only with a click
  in the web console. This also holds for a change of rights that waits.
  See [Changes of rights](approvals-and-break-glass.md#changes-of-rights).
- An agent, a service account and a runner never approve, on no path.
  Approve and reject have no MCP tool.
- A `destructive` operation always waits for a person. The one exception
  is an install in sandbox mode: there, a delete grant can let a service
  account delete with no person. See
  [Sandbox mode](approvals-and-break-glass.md#sandbox-mode-one-exception-for-disposable-test-systems-only).
- A credential read that shows the secret value (for example
  `vault.kv.read` with `reveal_secret`) waits for a person when a person
  or a service account sends it. A service account's call that asks to
  show hidden values waits for a person on any read. MEHO refuses every
  call from an agent that asks to show hidden values, also in a hosted
  agent run.
- A call that writes or makes a credential waits when an agent or a
  service account sends it, with any grant.
- A read that hides every value follows the normal rules.
- Who may approve which request is in
  [Who may approve](approvals-and-break-glass.md#who-may-approve).

## A runner is none of these

A runner is a program that runs checks in a network that MEHO cannot
reach ([Satellite gateway](satellite-gateway.md)). It has its own login
(`meho runner-principal register`). It reaches only a few routes of its
own. It has none of the three names.

## See who called

Every operation call writes an audit row
([Audit forensics](audit-forensics.md)). By default each MCP tool call
in a chosen tenant also writes a row of its own, so one MCP
`call_operation` writes two: a row for the tool call and a row for the
operation it runs. A `meho_status` call before the caller chooses a
tenant writes no row. With the setting `AUDIT_WRAPPER_ROWS_RETIRED` on,
a tool call that succeeds writes no row of its own: one MCP
`call_operation` writes only the operation row, and a call such as
`list_operation_groups` writes no audit row. A tool call that MEHO
refuses for the caller's role still writes one decision row.

- `principal_sub` names the login that called.
- In a hosted agent run started by hand, `principal_sub` names the
  person who started the run and `actor_sub` names the agent.
- In a hosted agent run that MEHO starts on its own (by the scheduler),
  `principal_sub` names the agent and `actor_sub` is empty.
- With the setting `AUDIT_CLIENT_IDENTITY` on, the `client` block is on
  the row of each REST request, each MCP tool call, each operation call
  and each approval decision. It holds the login client (`azp`), the
  channel (`web` or `token`) and the kind of login.
- With the same setting on, MEHO writes one row when a new MCP session
  starts and the caller has chosen a tenant. The row holds the name and
  version that the tool sends about itself. They are empty when the tool
  sends none.
