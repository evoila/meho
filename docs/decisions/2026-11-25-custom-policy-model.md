# The custom policy model: risk levels both ways, custom roles, policies and one switch (decision)

**Status:** Proposed, waits for the owner's word. The owner made these decisions on 25 November 2026. He answered the open points the same day. This record becomes the decision when the owner merges its pull request.
**Date:** 2026-11-25
**Initiatives:** none yet. It changes the plans of [#6033](https://github.com/evoila/meho/issues/6033) (configurable policy, part 1), [#6049](https://github.com/evoila/meho/issues/6049) (part 2: roles that the customer defines) and [#6188](https://github.com/evoila/meho/issues/6188) (a new agent can do nothing until it has a grant).
**Changes:** [2026-11-09-configurable-policy.md](2026-11-09-configurable-policy.md) and the token-call rule in [approval-groups-and-self-approval.md](approval-groups-and-self-approval.md). See "What this changes in earlier decisions" below.

Nothing in this record is built yet. Where it says "MEHO will", the code does not do it today.

## The decision in short

- MEHO has **two sides**. On one side are identities: persons, agents and roles. On the other side are operations. Each operation has a risk level.
- **Three things decide whether a call waits:** the risk level of the operation, who calls, and the fixed rules. The plain "always ask" mark goes away.
- **An admin can change the risk level of an operation, up or down.** Lowering it needs a reason and a fresh sign-in.
- **An admin can define custom roles.** Each person has exactly one of the four default roles. On top of it, a person can have any number of custom roles.
- **An admin sets policies.** A policy says what an agent may run without approval, and who must approve.
- **Fixed rules that no policy can lower.** Credentials, changes of rights and changes of the policy wait for a person. A delete, a `destructive` operation and a call to an address the caller gives always wait. An agent never approves.
- **Two layers.** The platform admin sets the start policy and the outer limit. Each tenant admin sets the tenant's own policy inside that limit.
- **Three kinds of caller:** person, personal agent and service agent. Personal agents and service agents get separate policy rules.
- **One switch for the new things,** working name `CUSTOM_POLICY`, off by default. The two edits that work today keep working without it.

## Why

- Customers want to decide which operations are risky in their place. They also want to decide who approves what.
- Today the risk levels and most approval rules are fixed in the code. A customer can only make them stricter.
- The owner wants one clear model. A customer sets it up in the console. A few fixed rules always hold.
- The owner on the "always ask" mark: it "adds another dimension to a problem that is already hard enough".

## Words used

- **Risk level:** how risky an operation is: `safe` (reads), `caution`, `dangerous` or `destructive`. The developers set a default for each operation.
- **Tenant:** one customer's own area in MEHO, with its own people, systems and rules.
- **Token:** the key that a tool sends with each call, instead of a sign-in in the web console.
- **MCP:** the protocol that AI tools use to talk to MEHO.
- **REST:** MEHO's web API for programs.
- **Audit row:** the record that MEHO writes for each call: who called, what ran, and why.
- **Person:** a human who signed in to the web console.
- **Personal agent:** a tool that uses a person's own login. Examples: an AI assistant, a script, the `meho` command line. It needs no setup. It can never do more than the person may do.
- **Service agent:** an identity that was created for a machine. It has its own grants (rights given to it in advance). It runs without a person watching.
- **Standing grant:** a grant that a person gives to a service account in advance.
- **Run approval:** one approval for the whole list of operations of one automation run.
- **Policy:** the rules a tenant sets. They say what an agent may run without approval, and who must approve.
- **Fixed rule:** a rule in the code that no policy can lower.
- **Baseline:** the policy that every new tenant starts with. The platform admin sets it.
- **Outer limit:** how far a tenant may loosen its policy. The platform admin sets it.
- **Change of rights:** a change to who may do what. Examples: roles, login groups and grants.
- **Fresh sign-in:** the person signed in again a short time before the change.
- **Delete sign:** how the code sees today that an operation deletes something. See "How MEHO knows a delete" below.

## Who is calling

**The simple rule:** if a person's own login is behind the call, it is a personal agent. If the identity was created for a machine, it is a service agent. MEHO cannot check whether an AI or a script is behind a machine identity. So both have one name. A grant says what each one may do.

The two middle columns say what is true in the code today.

| Who calls | What it is today | What runs at once today | New name |
|---|---|---|---|
| A person in the web console | A human who signed in to the console. Only the console's own sign-in marks a call as "web console". No token can do that. | `safe` and `caution`. `dangerous`, `destructive` and operations with the "always ask" mark wait. | person |
| A person's token (MCP, the command line, REST) | The same human's login, used by a tool such as an AI assistant or a script. MEHO has no separate personal API token. The token comes from the person's own sign-in. | Only `safe`. Everything else waits. | personal agent |
| Agent login | An identity that an admin registered for an AI agent. It has its own login. With the switch `AGENT_GRANT_REQUIRED` on, it can run nothing until it has a grant. With that switch off (the default), it runs `safe` operations without a grant. | Only `safe`. A grant never lets a higher level run at once. | service agent |
| Service account | An identity for a program that is not an AI, for example a script or a pipeline. It has its own login. | `safe` operations, and `caution` operations that only read. With a standing grant, `caution` writes and `dangerous` changes also run at once. `destructive` always waits. The only exception is a test install in [sandbox mode](sandbox-mode.md). | service agent |
| Hosted agent run (experimental) | MEHO itself runs the agent on the server and calls the model. The run has no login of its own. It acts for the person who started it. MEHO checks the agent's grants and the rights of that person. The stricter answer wins. | Only `safe`, and only if the person who started it may run it too. | service agent |

There is a fifth kind in the code: a **runner**. A runner is a program that runs checks in a remote network. It has its own login. It may only read, and it reaches only a few routes of its own. This record does not give it a name.

## The decisions

1. **The model.** One side holds identities: persons, agents and roles. The other side holds operations. Each operation has a risk level: `safe`, `caution`, `dangerous` or `destructive`. The default level is set when the operation is developed.
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
   - A change of rights always waits for a person, except an Admin's own change of rights in the web console, after a fresh sign-in.
   - A change of the policy itself always waits for a person.
   - An agent never approves.
   - A `destructive` operation always waits for approval.
   - A delete always waits for approval, except an Admin's own change of rights in the web console, after a fresh sign-in. MEHO knows a delete by its delete sign (see below). This holds on every install that is not in sandbox mode. An install in sandbox mode keeps its exception: there a delete grant lets a service account delete with no person ([sandbox-mode.md](sandbox-mode.md)).
   - A call to an address that the caller gives always waits for approval.

   Added by the owner on 26 November 2026:

   - A change of rights in a target system waits for a person by default. A tenant admin can allow it for one connector on purpose. The audit row names the rule.
   - With the switch on, three more rules hold. A standing grant does not cover an operation that deletes, stops or overwrites something for good. An install's own list of delete names only adds to the built-in list. A `dangerous` operation of a connector package that nobody has checked yet still waits when a personal agent calls it.

6. **Two layers.**
   - The platform admin sets the baseline and the outer limit.
   - Each tenant admin sets the tenant's own policy, inside the outer limit.
   - A tenant may always be stricter.
   - Custom roles belong to the tenant.
7. **Person, personal agent and service agent.**
   - Every call through MCP, the command line or a token is an agent call.
   - Only the web console counts as "a person decided". Later the passkey touch counts too.
   - A personal agent is automatic and needs no setup. Its audit row shows the person and the tool. The person's own rights are its upper limit.
   - A service agent was created for a machine. It has its own grants. It runs without a person watching.
   - Personal agents and service agents get separate policy rules.
   - A person may approve the requests of their own personal agent. They do it in the web console, up to what their role may approve. A tenant policy can ask for another role instead.
8. **A change of the policy.**
   - An Admin's own change of the policy in the web console runs at once. The person is there.
   - A change of the policy that an agent asks for waits for a person.
   - This is the same rule as for changes of rights in [the four default roles](2026-11-18-four-default-roles.md).
9. **Approving from the command line stays.** `meho approvals approve` opens a browser sign-in. That sign-in proves that a person is there.
10. **No "always ask" mark.** Three things decide whether a call waits:
    - the risk level of the operation;
    - who calls: person, personal agent or service agent, and their roles;
    - the fixed rules.

    What this means:
    - The plain "always ask" mark on an operation goes away.
    - The operations that are `caution` today and have the plain mark become `dangerous` by default. An admin can lower single ones.
    - **The consequence, accepted by the owner:** what waits stays the same. But an Operator can no longer approve another person's request for these operations, because an Operator approves only low-risk changes.
    - "Who approves" is a policy (decision 4b). So a tenant can give that right to a role.
    - Some marks have a reason: credential, change of rights, caller-given address and delete. An admin does not see them as marks. The fixed rules cover them.
11. **"Always wait" becomes "raise to `destructive`".** Today an admin can mark a category or one operation to always wait. This goes away as a mark of its own. Instead, the admin raises the operation to `destructive`, because `destructive` always waits.
    - Existing "always wait" settings are carried over to `destructive`. The carry-over is its own work step.
    - Nothing saved may get looser in the carry-over.
12. **One switch for the new things.** The switch has the working name `CUSTOM_POLICY`. It is off by default.
    - It turns on only the new things: lowering a risk level, custom roles, policies, the rules for personal agents and service agents, and the end of the "always ask" mark.
    - It folds in the three policy switches of today. They turn on categories on operations (`POLICY_CLASSIFICATION`), a test run of the new policy check (`POLICY_ENGINE_SHADOW`), and the routing of approvals to teams (`POLICY_APPROVER_ROUTING`).
    - Every other switch stays its own.
    - **Two edits that work today keep working with the switch off:**
      - a tenant admin raises the risk level of an operation;
      - a platform admin turns approval off for an operation.
    - Why they stay:
      - An upgrade must not change how an install behaves.
      - If MEHO ignored a raised level, an operation would run with less protection than the admin set.
      - "Approval off" is the way out for an install with only one admin.

## How MEHO knows a delete

Today the code sees that an operation deletes something by its **delete sign**. There are four kinds:

- the risk level `destructive`;
- the web method `DELETE` of an imported API operation;
- the tag `destructive` on an operation written by hand;
- a delete name. Examples: an operation id that ends in `.delete`, `.destroy`, `.remove` or `.purge`, and vendor action names such as `Destroy_Task`.

- Today the delete sign stops standing grants and run approvals from covering a delete.
- So a delete already waits today. The one exception is a test install in sandbox mode.
- Decision 5 makes this a fixed rule. It holds also when an admin lowers the risk level of a delete.

## What stays fixed

- **Approval is a person's decision.** An agent never approves. This is the line of the first MEHO design (v0.1-spec, section 7).
- **The fixed rules** of decision 5. No policy, no tenant admin and no platform admin can lower them.
- **The four default roles** of [2026-11-18-four-default-roles.md](2026-11-18-four-default-roles.md). They are the same in every tenant. Each person has exactly one of them.
- **Only the web console counts as a person.** A call with a token is an agent call. MEHO does not try to guess who or what is behind a token.
- **Approving from the command line** with a browser sign-in.
- **Every call writes an audit row.** The row names the rule that let the call run.
- **With the switch off, MEHO behaves as it does today,** with the two edits of today.

## What this changes in earlier decisions

The older records keep their text. Each one gets one line at the top that points here.

### [2026-11-09-configurable-policy.md](2026-11-09-configurable-policy.md)

Replaced, when the switch is on:

- "It can make them looser **only back to MEHO's shipped defaults**." (in "The decision in short"), and decision 3, "Tighten freely; loosen only back to the shipped defaults."
  - Now: an admin may lower a risk level below the developers' default (decision 2). A tenant may loosen up to the outer limit (decision 6). The fixed rules still hold.
- Floor 3: "Token calls and agents run only `safe` operations at once. This holds for MCP, the command line and REST, also for a person's own token. Everything else waits for approval."
  - Now: a policy decides what an agent may run without approval (decision 4a). Personal agents and service agents have separate rules (decision 7).
- Decision 12: "The file may not turn 'always wait' off below the code."
  - Now: there is no "always ask" mark (decision 10). "Always wait" becomes `destructive` (decision 11).
- "Everything ships in phases, **behind switches that are off by default**." and the consequence "Each phase ships behind its own switch."
  - Now: one switch, `CUSTOM_POLICY` (decision 12).

Stays:

- Decision 1: one base level per login, plus any number of custom roles. This record calls the base level "default role".
- Floor 1: agents, services and runners never approve.
- Floor 2: a person approves with a click in the web console, or on the command line with a browser sign-in.
- Floor 4: a `destructive` operation always waits.
- Floor 5, for a credential read and for an address that the caller gives.
- Floor 7: every decision writes an audit row that names the rule.
- Decision 2: groups of people live in Keycloak (the login server).
- Decision 5: a custom role may approve when its rules say so.
- The parts already changed by [2026-11-18-four-default-roles.md](2026-11-18-four-default-roles.md) stay changed.

### The token-call rule in [approval-groups-and-self-approval.md](approval-groups-and-self-approval.md)

Replaced, when the switch is on:

- "Every call made with a token follows the agent rule: only safe operations run at once." Also, in "Every token call, and every agent": "Only `safe` operations run at once. `caution`, `dangerous` and `destructive` operations wait for an approval click."
  - Now: the tenant's policy for personal agents and service agents decides (decisions 4a and 7).
- "A person may confirm their own token `caution` change."
  - Now: a person may approve the requests of their own personal agent in the web console, up to what their role may approve. A tenant policy can ask for another role (decision 7).
- "Changes marked `requires_approval` wait for an approval click." (`requires_approval` is the "always ask" mark.)
  - Now: there is no plain mark (decision 10). The risk level decides.

Stays:

- "Only the web interface counts as a person."
- "AI tools and agents never approve."
- "A plain API token never approves."
- How a person approves on the command line: with a browser sign-in.
- Approve and reject have no path through MCP (the protocol AI tools use).

### [2026-11-18-four-default-roles.md](2026-11-18-four-default-roles.md)

This record does not change it.

- The four default roles stay. A person has exactly one of them.
- The rules for who changes rights stay. Decision 8 uses the same rule for changes of the policy.
- This record adds custom roles on top.

## Not decided here

- The exact form of a policy rule and of the outer limit.
- The console pages.
- How existing installs move from the three small switches to the one switch.
- Whether a tenant marked as a lab gets a wider outer limit.
- **The list of `caution` operations with the plain mark** (58 operations written by hand today, plus imported ones). Each one is checked, one by one, before the change. So no operation changes behaviour by accident.
- **The 24 operations marked "approve each call".** Each one is looked at and gets a proposed risk level in its own work step. Until then they behave as today. Today such an operation waits on every call. No standing grant and no run approval covers it. A platform admin's "approval off" ends that wait.
- **The carry-over of "always wait" to `destructive`** (decision 11). Two facts for that work step:
  - A `destructive` call needs a preview first: MEHO shows what the call will do, and the approval is bound to that preview.
  - Today a step of a multi-step operation at the `destructive` level is refused, not held for approval.
  - So each saved setting is checked before it is carried over.
- **The two edits of today in the policy pages.** When the model is complete, the risk-level raise and "approval off" move into the policy pages, with a carry-over. That is its own step.
- **The list of delete names is a setting** (`SERVICE_GRANT_DELETE_SHAPED_PATTERNS`). Whether an install may change what counts as a delete is not decided here.
- Whether runners get one of the three names.
- Whether agent grants move into the policy.

## References

- v0.1-spec, section 7: approval is a human decision. This record keeps that line.
- [2026-11-09-configurable-policy.md](2026-11-09-configurable-policy.md): categories, routing and the policy file.
- [2026-11-18-four-default-roles.md](2026-11-18-four-default-roles.md): the four default roles and who changes rights.
- [approval-groups-and-self-approval.md](approval-groups-and-self-approval.md): what waits, and how a person approves.
- [sandbox-mode.md](sandbox-mode.md): standing grants that cover deletes on a test install.
- [governed-delete-operations.md](governed-delete-operations.md): the `destructive` level, the preview and what else a delete affects.
- `docs/codebase/approvals.md`: how approvals work today.
- `docs/codebase/agent-permission-model.md`: what each kind of caller may do today.
