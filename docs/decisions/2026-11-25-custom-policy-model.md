# The custom policy model: risk levels both ways, custom roles, policies and one switch (decision)

**Status:** Proposed, waits for the owner's word. The owner made these decisions on 25 November 2026. This record becomes the decision when the owner merges its pull request.
**Date:** 2026-11-25
**Initiatives:** none yet. It changes the plans of [#6033](https://github.com/evoila/meho/issues/6033) (configurable policy, part 1), [#6049](https://github.com/evoila/meho/issues/6049) (part 2: roles that the customer defines) and [#6188](https://github.com/evoila/meho/issues/6188) (a new agent can do nothing until it has a grant).
**Changes:** [2026-11-09-configurable-policy.md](2026-11-09-configurable-policy.md) and the token-call rule in [approval-groups-and-self-approval.md](approval-groups-and-self-approval.md). See "What this changes in earlier decisions" below.

Nothing in this record is built yet. Where it says "MEHO will", the code does not do it today.

## The decision in short

- MEHO has **two sides**. On one side are identities: persons, agents and roles. On the other side are operations, each with a risk level.
- **An admin can change the risk level of an operation, up or down.** Lowering it needs a reason and a fresh sign-in.
- **An admin can define custom roles.** A person can hold several roles. The four default roles stay the same in every tenant.
- **An admin sets policies:** what an agent may run without approval, and who must approve.
- **Some rules no policy can lower** (the floor): credentials, changes of rights and changes of the policy always wait for a person, and an agent never approves.
- **Two layers:** the platform admin sets the start policy and the outer limit. Each tenant admin sets the tenant's own policy inside that limit.
- **Personal agents and service agents** get separate policy rules.
- **All of this sits behind one switch,** working name `CUSTOM_POLICY`, off by default. Off: MEHO behaves as developed.

## Why

- Customers want to decide for themselves which operations are risky in their place, and who approves what.
- Today the risk levels and most approval rules are fixed in the code. A customer can only make them stricter.
- The owner wants one clear model that a customer sets up in the console, with a few fixed rules that always hold.

## Words used

- **Risk level:** how risky an operation is: `safe` (reads), `caution`, `dangerous` or `destructive`. The developers set a default for each operation.
- **Personal agent:** a person's own tool, signed in with that person's login. Examples: an AI assistant, a script, the `meho` command line. It needs no setup. It can never do more than the person may do.
- **Service agent:** an identity that an admin registers in MEHO. It has its own login and its own grants (rights given to it in advance). It runs without a person watching.
- **Policy:** the rules a tenant sets: what an agent may run without approval, and who must approve.
- **Floor:** a rule in the code that no policy can lower.
- **Baseline:** the policy that every new tenant starts with. The platform admin sets it.
- **Outer limit:** how far a tenant may loosen its policy. The platform admin sets it.
- **Change of rights:** a change to who may do what, for example roles, login groups and grants.
- **Fresh sign-in:** the person signed in again a short time before the change.

## The eight decisions

1. **The model.** One side holds identities: persons, agents and roles. The other side holds operations. Each operation has a risk level: `safe`, `caution`, `dangerous` or `destructive`. Its default is set when the operation is developed.
2. **Risk levels in both directions.** An admin can change the risk level of an operation up or down. Examples: `dangerous` to `caution`, or `safe` to `destructive`.
   - Lowering a level needs a reason and a fresh sign-in.
   - The audit row of every call names the rule that let the call run.
3. **Custom roles.** An admin can define custom roles. A person can hold several roles. The four default roles (Reader, Operator, Admin, Platform Admin) stay the same in every tenant.
4. **Policies.** An admin sets policies. There are two kinds:
   - **(a) What an agent may run without approval.** Example: an agent runs every operation below `destructive` at once.
   - **(b) Who must approve.** Example: every operation above `safe` on the NSX connector needs approval from the role `network_admin`.
5. **The floor.** No policy can lower these rules:
   - A credential read or write always waits for a person.
   - A change of rights always waits for a person.
   - A change of the policy itself always waits for a person.
   - An agent never approves.
6. **Two layers.**
   - The platform admin sets the baseline and the outer limit.
   - Each tenant admin sets the tenant's own policy, inside the outer limit.
   - A tenant may always be stricter.
   - Custom roles belong to the tenant.
7. **Personal agent and service agent.**
   - Every call through MCP, the command line or an API token is an agent call.
   - Only the web console counts as "a person decided". Later the passkey touch counts too.
   - A personal agent is automatic and needs no setup. Its audit row shows the person and the tool. The person's own rights are its upper limit.
   - A service agent is registered, with its own login and its own grants. It runs unattended.
   - Personal agents and service agents get separate policy rules.
   - A person may approve the requests of their own personal agent, in the web console, up to what their role may approve. A tenant policy can demand that another role approves.
8. **One switch.** All of this is behind one switch, working name `CUSTOM_POLICY`. It is off by default.
   - Off: MEHO behaves as developed.
   - On: risk-level changes, custom roles and policies work.
   - The smaller policy switches that exist today are folded into this one switch. Today these are `POLICY_CLASSIFICATION`, `POLICY_ENGINE_SHADOW` and `POLICY_APPROVER_ROUTING`.

## What stays fixed

- **Approval is a person's decision.** An agent never approves (v0.1-spec §7).
- **The floor** of decision 5. No policy, no tenant admin and no platform admin can lower it.
- **The four default roles** of [2026-11-18-four-default-roles.md](2026-11-18-four-default-roles.md). They are the same in every tenant.
- **Only the web console counts as a person.** A call with a token is an agent call. MEHO does not try to guess who or what is behind a token.
- **Every call writes an audit row.** The row names the rule that let the call run.
- **With the switch off, nothing changes.**

## What this changes in earlier decisions

The older records keep their text. Each gets one line at the top that points here.

### [2026-11-09-configurable-policy.md](2026-11-09-configurable-policy.md)

Replaced, when the switch is on:

- "It can make them looser **only back to MEHO's shipped defaults**." (decision in short) and decision 3, "Tighten freely; loosen only back to the shipped defaults."
  - Now: an admin may lower a risk level below the developers' default (decision 2), and a tenant may loosen up to the outer limit (decision 6). The floor still holds.
- Floor 3, "Token calls and agents run only `safe` operations at once. This holds for MCP, the command line and REST, also for a person's own token. Everything else waits for approval."
  - Now: a policy decides what an agent may run without approval (decision 4a), with separate rules for personal agents and service agents (decision 7).
- "Everything ships in phases, **behind switches that are off by default**." and the consequence "Each phase ships behind its own switch."
  - Now: one switch, `CUSTOM_POLICY` (decision 8).

Stays:

- Floor 1: agents, services and runners never approve.
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

Stays:

- "Only the web interface counts as a person."
- "AI tools and agents never approve."
- "A plain API token never approves."
- Approve and reject have no MCP path.

### [2026-11-18-four-default-roles.md](2026-11-18-four-default-roles.md)

This record does not change it. The four default roles stay. The rules for who changes rights stay. This record adds custom roles next to them.

## Not decided here

- The exact form of a policy rule, and of the outer limit.
- The console pages.
- How existing installs move from the small switches to the one switch.
- Whether a tenant marked as a lab gets a wider outer limit.
- Whether other rules in today's code stay fixed rules that no policy can lower. Examples: a `destructive` operation always waits; an operation that connects to an address the caller gives always waits; an operation marked for approval on every call.
- Whether "always waits for a person" in the floor also covers a change that an admin makes himself in the web console. The four default roles record says such a change of rights runs at once.
- Whether the "always wait" mark of an operation may also be turned off by a tenant admin, or only the risk level changes.
- Whether a person still approves on the command line with a fresh sign-in or a one-time code. Decision 7 names only the web console, and later the passkey touch.
- What "a person can hold several roles" means for the four default roles: one default role plus custom roles, or more than one default role.
- Which of today's machine logins become service agents: agent logins, service accounts, or both. And where MEHO's own hosted agent runs belong.
- Whether agent grants move into the policy.
- What happens to today's risk-level edits when the switch is off. Today a tenant admin can make an operation stricter, and a platform admin can turn approval off for one operation, without any switch.

## References

- v0.1-spec §7: approval is a human decision. This record keeps that line.
- [2026-11-09-configurable-policy.md](2026-11-09-configurable-policy.md): categories, routing and the policy file.
- [2026-11-18-four-default-roles.md](2026-11-18-four-default-roles.md): the four default roles and who changes rights.
- [approval-groups-and-self-approval.md](approval-groups-and-self-approval.md): what waits, and how a person approves.
- [sandbox-mode.md](sandbox-mode.md): standing grants that cover deletes on a test install.
- `docs/codebase/approvals.md`: the approvals plane today.
