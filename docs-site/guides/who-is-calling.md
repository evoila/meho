# Who is calling: person, personal agent, service agent

Every call to MEHO comes from someone or something. Who calls decides
whether a call runs at once or waits until a person approves it.

This page says:

- which kinds of caller MEHO knows;
- how MEHO tells them apart;
- what runs at once for each kind today.

## The simple rule

- **Person:** a human who signed in to the web console. Only the web
  console counts as "a person decided".
- **Personal agent:** a tool that uses a person's own login. Examples:
  an AI assistant, a script, the `meho` command line. It needs no setup.
  It can never do more than the person may do.
- **Service agent:** an identity that was created for a machine. It has
  its own login and its own grants (rights given to it in advance). It
  runs without a person watching.

MEHO cannot see whether an AI or a script uses a machine identity. So
both get one name: service agent. The grants of the identity say what
it may do.

!!! note "The names change nothing today"

    The three names come from the decision record
    [The custom policy model](https://github.com/evoila/meho/blob/main/docs/decisions/2026-11-25-custom-policy-model.md).
    Today they are names only. The rules in the table below are the
    rules MEHO uses now. A later version adds a switch. With that switch
    on, an admin can set separate rules for personal agents and service
    agents. This page will say so when it ships.

## The kinds of caller

| Who calls | How MEHO knows | What runs at once today | Name |
|---|---|---|---|
| A person in the web console | The person signed in to the web console. Only the console's own sign-in marks a call as "web console". No token can do that. | `safe` and `caution`. `dangerous`, `destructive` and `requires_approval` operations wait. An admin's [change of rights](approvals-and-break-glass.md#changes-of-rights) runs at once when started with a click after a fresh sign-in. | person |
| A person's tool: MCP (for example an AI assistant), the `meho` command line, or REST | The person's own login, used by a tool. MEHO has no separate personal API token. The token comes from the person's own sign-in. | Only `safe`. Everything else waits. | personal agent |
| An agent login | An identity that an admin registered for an AI agent (`meho agent-principal register`). It has its own login and its own grants. | Only `safe`. No grant lets a higher level run at once. With the setting `AGENT_GRANT_REQUIRED` on, a call that no grant covers is refused, also a `safe` one. | service agent |
| A service account | An identity for a program that is not an AI, for example a build pipeline. It has its own login. | `safe` operations, and `caution` operations that only read. A `safe` read is refused when the target is outside the set the account may read. With a standing grant (a person's approval, given in advance), `caution` changes and `dangerous` changes run too. A change can also run when a person approved that operation for this automation run. `destructive` always waits. | service agent |
| A hosted agent run started by a person (experimental) | MEHO runs the agent on its own server for a person who started it. MEHO checks the agent's grants and the rights of that person. The stricter answer wins. | Only `safe`, and only if the person who started it may run it too. Scheduled and event-triggered runs are different: they use the agent's own login and grants, and follow the agent login row above. | service agent |

## Rules for every kind of caller

- An agent never approves. Approve and reject have no MCP tool.
- A person approves in the web console. On the command line, a person
  approves after a fresh sign-in in the browser. See
  [Approve on the command line](approvals-and-break-glass.md#approve-on-the-command-line).
  A change of rights is different: only an admin approves a waiting one,
  and only with a click in the web console. A fresh sign-in on the
  command line is not enough. See
  [Changes of rights](approvals-and-break-glass.md#changes-of-rights).
- A `destructive` operation always waits for a person. The one exception
  is an install in sandbox mode: there, a delete grant can let a service
  account delete with no person. See
  [Sandbox mode](approvals-and-break-glass.md#sandbox-mode-one-exception-for-disposable-test-systems-only).
- A call that writes or makes a credential waits for a person when an
  agent or a service account sends it. No grant covers it.
- A call that reads a credential can run, wait or be refused. For example,
  `vault.kv.read` runs with the values hidden. If a service account asks
  to see them, the call waits. If an agent login asks to see them, MEHO
  refuses the call.
- Who may approve which request is in
  [Who may approve](approvals-and-break-glass.md#who-may-approve).

## A runner is none of these

A runner is a program that runs checks in a network that MEHO cannot
reach ([Satellite gateway](satellite-gateway.md)). It has its own login
(`meho runner-principal register`). It may only read, and it reaches
only a few routes of its own. It has none of the three names.

## See who called

Authenticated operations are audited
([Audit forensics](audit-forensics.md)). One call can write more than one
row: an MCP `call_operation` writes a row for the tool call and a row for
the operation it runs. Do not count both as two user actions.

- `principal_sub` names the login that called.
- `actor_sub` names the agent in a hosted agent run started by a person.
  The person who started the run is in `principal_sub`. For a scheduled
  or event-triggered run, `principal_sub` names the agent itself and
  `actor_sub` is empty.
- With the setting `AUDIT_CLIENT_IDENTITY` on, each row also has a
  `client` block: the login client (`azp`), the channel (`web` or
  `token`) and the kind of login. On an MCP session start, MEHO also
  writes the name the tool gives for itself.
