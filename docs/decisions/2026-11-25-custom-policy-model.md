# The custom policy model: risk levels both ways, custom roles, policies and one switch (decision)

**Status:** Proposed, waits for the owner's word. The owner made these decisions on 25 November 2026 and answered the open points the same day. This record becomes the decision when the owner merges its pull request.
**Date:** 2026-11-25
**Initiatives:** none yet. It changes the plans of [#6033](https://github.com/evoila/meho/issues/6033) (configurable policy, part 1), [#6049](https://github.com/evoila/meho/issues/6049) (part 2: roles that the customer defines) and [#6188](https://github.com/evoila/meho/issues/6188) (a new agent can do nothing until it has a grant).
**Changes:** [2026-11-09-configurable-policy.md](2026-11-09-configurable-policy.md) and the token-call rule in [approval-groups-and-self-approval.md](approval-groups-and-self-approval.md). See "What this changes in earlier decisions" below.

Nothing in this record is built yet. Where it says "MEHO will", the code does not do it today.

## The decision in short

- MEHO has **two sides**. On one side are identities: persons, agents and roles. On the other side are operations, each with a risk level.
- **Whether a call waits comes from three things only:** the risk level of the operation, who calls, and the fixed rules. The plain "always ask" mark goes away.
- **An admin can change the risk level of an operation, up or down.** Lowering it needs a reason and a fresh sign-in.
- **An admin can define custom roles.** Each person has exactly one of the four default roles, and any number of custom roles on top.
- **An admin sets policies:** what an agent may run without approval, and who must approve.
- **Some rules no policy can lower** (the fixed rules): credentials, changes of rights and changes of the policy wait for a person; a `destructive` operation and a call to an address the caller gives always wait; an agent never approves.
- **Two layers:** the platform admin sets the start policy and the outer limit. Each tenant admin sets the tenant's own policy inside that limit.
- **Three kinds of caller:** person, personal agent and service agent. Personal agents and service agents get separate policy rules.
- **All of this sits behind one switch,** working name `CUSTOM_POLICY`, off by default. Off: MEHO runs exactly as developed, with no edits at all.

## Why

- Customers want to decide for themselves which operations are risky in their place, and who approves what.
- Today the risk levels and most approval rules are fixed in the code. A customer can only make them stricter.
- The owner wants one clear model that a customer sets up in the console, with a few fixed rules that always hold.
- The owner on the "always ask" mark: it "adds another dimension to a problem that is already hard enough".

## Words used

- **Risk level:** how risky an operation is: `safe` (reads), `caution`, `dangerous` or `destructive`. The developers set a default for each operation.
- **Person:** a human who signed in to the web console.
- **Personal agent:** a tool that uses a person's own login, for example an AI assistant, a script or the `meho` command line. It needs no setup. It can never do more than the person may do.
- **Service agent:** an identity that was created for a machine. It has its own grants (rights given to it in advance). It runs without a person watching.
- **Policy:** the rules a tenant sets: what an agent may run without approval, and who must approve.
- **Fixed rule:** a rule in the code that no policy can lower. The floor is part of the fixed rules.
- **Baseline:** the policy that every new tenant starts with. The platform admin sets it.
- **Outer limit:** how far a tenant may loosen its policy. The platform admin sets it.
- **Change of rights:** a change to who may do what, for example roles, login groups and grants.
- **Fresh sign-in:** the person signed in again a short time before the change.

## Who is calling

**The simple rule:** if a person's own login is behind the call, it is a personal agent. If the identity was created for a machine, it is a service agent. MEHO cannot check whether an AI or a script is behind a machine identity. So both have one name, and a grant says what each may do.

The middle columns say what is true in the code today.

| Who calls | What it is today | What runs at once today | New name |
|---|---|---|---|
| A person in the web console | A human who signed in to the console. Only the console's own sign-in marks a call as "web console". No token can. | `safe` and `caution`. `dangerous`, `destructive` and operations with the "always ask" mark wait. | person |
| A person's token (MCP, the command line, REST) | The same human's login, used by a tool such as an AI assistant or a script. MEHO has no separate personal API token: the token comes from the person's own sign-in. | Only `safe`. Everything else waits. | personal agent |
| Agent login | An identity an admin registered for an AI agent, with its own login. With the switch `AGENT_GRANT_REQUIRED` on, it can run nothing until it has a grant. With that switch off (the default), it runs `safe` operations without a grant. | Only `safe`. A grant can never let a higher level run at once. | service agent |
| Service account | An identity for a program that is not an AI, for example a script or a pipeline. It has its own login. | `safe`, and `caution` reads. With a standing grant, `caution` writes and `dangerous` changes run at once too. `destructive` always waits (except on a test install in [sandbox mode](sandbox-mode.md)). | service agent |
| Hosted agent run (experimental) | MEHO itself runs the agent on the server and calls the model. The run has no login of its own: it acts for the person who started it. MEHO checks the agent's grants and the starter's own rights, and the stricter answer wins. | Only `safe`, and only if the starter may run it too. | service agent |

A fifth kind exists in the code: a **runner**. It is a program that runs checks in a remote network. It has its own login, may only read, and reaches only its own few routes. This record does not name it.

## The decisions

1. **The model.** One side holds identities: persons, agents and roles. The other side holds operations. Each operation has a risk level: `safe`, `caution`, `dangerous` or `destructive`. Its default is set when the operation is developed.
2. **Risk levels in both directions.** An admin can change the risk level of an operation up or down. Examples: `dangerous` to `caution`, or `safe` to `destructive`.
   - Lowering a level needs a reason and a fresh sign-in.
   - The audit row of every call names the rule that let the call run.
3. **Roles.** An admin can define custom roles.
   - Each person has exactly one of the four default roles: Reader, Operator, Admin or Platform Admin.
   - On top of it, a person can have any number of custom roles.
   - The four default roles stay the same in every tenant.
4. **Policies.** An admin sets policies. There are two kinds:
   - **(a) What an agent may run without approval.** Example: an agent runs every operation below `destructive` at once.
   - **(b) Who must approve.** Example: every operation above `safe` on the NSX connector needs approval from the role `network_admin`.
5. **The fixed rules.** No policy can lower these rules:
   - A credential read or write always waits for a person.
   - A change of rights always waits for a person.
   - A change of the policy itself always waits for a person.
   - An agent never approves.
   - A `destructive` operation always waits for approval.
   - A call to an address that the caller gives always waits for approval.
6. **Two layers.**
   - The platform admin sets the baseline and the outer limit.
   - Each tenant admin sets the tenant's own policy, inside the outer limit.
   - A tenant may always be stricter.
   - Custom roles belong to the tenant.
7. **Person, personal agent and service agent.**
   - Every call through MCP, the command line or a token is an agent call.
   - Only the web console counts as "a person decided". Later the passkey touch counts too.
   - A personal agent is automatic and needs no setup. Its audit row shows the person and the tool. The person's own rights are its upper limit.
   - A service agent was created for a machine. It has its own grants. It runs unattended.
   - Personal agents and service agents get separate policy rules.
   - A person may approve the requests of their own personal agent, in the web console, up to what their role may approve. A tenant policy can demand that another role approves.
8. **A change of the policy.**
   - An Admin's own change of the policy in the web console runs at once. The person is there.
   - A change of the policy that an agent asks for waits for a person.
   - This is the same rule as for changes of rights in [the four default roles](2026-11-18-four-default-roles.md).
9. **Approving from the command line stays.** `meho approvals approve` opens a browser sign-in. That sign-in is the proof that a person is there.
10. **No "always ask" mark.** Whether a call waits comes from three things only:
    - the risk level of the operation;
    - who calls: person, personal agent or service agent, and their roles;
    - the fixed rules.
    - The plain "always ask" mark on an operation goes away.
    - The operations that are `caution` today and carry the plain mark become `dangerous` by default. So their behaviour stays the same. An admin can lower single ones.
    - Marks that have a reason (credential, change of rights, approve each call, caller-given address, delete) are not flags that an admin sees. The fixed rules cover them.
11. **One switch.** All of this is behind one switch, working name `CUSTOM_POLICY`. It is off by default.
    - **Off:** MEHO runs exactly as developed. There are no edits at all.
    - **On:** risk-level edits, custom roles and policies work.
    - It folds in only the three policy switches of today: `POLICY_CLASSIFICATION`, `POLICY_ENGINE_SHADOW` and `POLICY_APPROVER_ROUTING`. Every other switch stays its own.

## What this means for today's edits

Today two edits work without any switch:

- a tenant admin raises the risk level of an operation, or marks it to always wait;
- a platform admin turns approval off for an operation.

With this decision, **both work only with the switch on.** With the switch off, MEHO uses the developers' values and nothing else.

- An install that uses these edits must turn the switch on first.
- The install guide must say "turn the switch on first" where it describes these edits.

## What stays fixed

- **Approval is a person's decision.** An agent never approves (v0.1-spec §7).
- **The fixed rules** of decision 5. No policy, no tenant admin and no platform admin can lower them.
- **The four default roles** of [2026-11-18-four-default-roles.md](2026-11-18-four-default-roles.md). They are the same in every tenant. Each person has exactly one of them.
- **Only the web console counts as a person.** A call with a token is an agent call. MEHO does not try to guess who or what is behind a token.
- **Approving from the command line** with a browser sign-in.
- **Every call writes an audit row.** The row names the rule that let the call run.
- **With the switch off, nothing changes** from the developers' values.

## What this changes in earlier decisions

The older records keep their text. Each gets one line at the top that points here.

### [2026-11-09-configurable-policy.md](2026-11-09-configurable-policy.md)

Replaced, when the switch is on:

- "It can make them looser **only back to MEHO's shipped defaults**." (decision in short) and decision 3, "Tighten freely; loosen only back to the shipped defaults."
  - Now: an admin may lower a risk level below the developers' default (decision 2), and a tenant may loosen up to the outer limit (decision 6). The fixed rules still hold.
- Floor 3, "Token calls and agents run only `safe` operations at once. This holds for MCP, the command line and REST, also for a person's own token. Everything else waits for approval."
  - Now: a policy decides what an agent may run without approval (decision 4a), with separate rules for personal agents and service agents (decision 7).
- Decision 12, "The file may not turn 'always wait' off below the code."
  - Now: there is no plain "always ask" mark (decision 10). An admin changes the risk level instead.
- "Everything ships in phases, **behind switches that are off by default**." and the consequence "Each phase ships behind its own switch."
  - Now: one switch, `CUSTOM_POLICY` (decision 11).

Stays:

- Decision 1: one base level per login, plus any number of custom roles. This record calls the base level "default role".
- Floor 1: agents, services and runners never approve.
- Floor 2: a person approves with a click in the web console, or on the command line with a browser sign-in.
- Floor 4: a `destructive` operation always waits.
- Floor 5, for an address that the caller gives, and for a credential read.
- Floor 7: every decision writes an audit row that names the rule.
- Decision 2: groups of people live in Keycloak (the login server).
- Decision 5: a custom role may approve when its rules say so.
- The parts already changed by [2026-11-18-four-default-roles.md](2026-11-18-four-default-roles.md) stay changed.

### The token-call rule in [approval-groups-and-self-approval.md](approval-groups-and-self-approval.md)

Replaced, when the switch is on:

- "Every call made with a token follows the agent rule: only safe operations run at once." and, in "Every token call, and every agent", "Only `safe` operations run at once. `caution`, `dangerous` and `destructive` operations wait for an approval click."
  - Now: the tenant's policy for personal agents and service agents decides (decisions 4a and 7).
- "A person may confirm their own token `caution` change."
  - Now: a person may approve the requests of their own personal agent, in the web console, up to what their role may approve, unless the tenant policy demands another role (decision 7).
- "Changes marked `requires_approval` wait for an approval click."
  - Now: there is no plain mark (decision 10). The risk level decides.

Stays:

- "Only the web interface counts as a person."
- "AI tools and agents never approve."
- "A plain API token never approves."
- How a person approves on the command line: with a browser sign-in.
- Approve and reject have no MCP path.

### [2026-11-18-four-default-roles.md](2026-11-18-four-default-roles.md)

This record does not change it. The four default roles stay. A person has exactly one of them. The rules for who changes rights stay, and decision 8 uses the same rule for changes of the policy. This record adds custom roles on top.

## Not decided here

- The exact form of a policy rule, and of the outer limit.
- The console pages.
- How existing installs move from the three small switches to the one switch.
- Whether a tenant marked as a lab gets a wider outer limit.
- **The list of `caution` operations with the plain mark.** Each one is checked, one by one, before the change, so that no operation changes behaviour by accident. The check also looks at who may approve: today an Operator may approve another person's `caution` change, but not a `dangerous` one.
- **"Approve each call" and delete-shaped operations.** These two are not in the list of fixed rules. Today's code:
  - An operation marked "approve each call" waits on every call. No standing grant and no run approval covers it. A platform admin's "approval off" edit ends that wait.
  - A `dangerous` operation with a delete sign (a `DELETE` call, a delete name or a `destructive` tag) waits like any `dangerous` operation. No standing grant and no run approval covers it, except a delete grant on a test install in sandbox mode.
  - This record does not change either rule.
- The "always wait" mark that an admin can set today, on a category or on one operation: whether it becomes a raised risk level, and how existing settings move.
- Whether runners get one of the three names.
- Whether agent grants move into the policy.

## References

- v0.1-spec §7: approval is a human decision. This record keeps that line.
- [2026-11-09-configurable-policy.md](2026-11-09-configurable-policy.md): categories, routing and the policy file.
- [2026-11-18-four-default-roles.md](2026-11-18-four-default-roles.md): the four default roles and who changes rights.
- [approval-groups-and-self-approval.md](approval-groups-and-self-approval.md): what waits, and how a person approves.
- [sandbox-mode.md](sandbox-mode.md): standing grants that cover deletes on a test install.
- `docs/codebase/approvals.md`: the approvals plane today.
- `docs/codebase/agent-permission-model.md`: what each kind of caller may do today.
