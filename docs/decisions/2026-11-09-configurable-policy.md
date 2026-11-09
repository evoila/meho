# Configurable policy: categories, custom roles, bindings and approver routing (decision)

**Status:** Proposed, waits for the owner's confirmation. The owner answered the twelve questions below on 9 Nov 2026. This record becomes the decision only when the owner confirms it in his own words.
**Date:** 2026-11-09
**Initiatives:** [#6033](https://github.com/evoila/meho/issues/6033) (part 1: categories, approver routing by category, the policy file) and [#6049](https://github.com/evoila/meho/issues/6049) (part 2: roles that the customer defines)
**Replaces in part:** the "No exceptions" rule in [approval-groups-and-self-approval.md](approval-groups-and-self-approval.md), and rule 1 "One group per person" in [login-role-groups.md](login-role-groups.md). See "What this record replaces" below.

## The decision in short

- A customer can decide who may change what, and who approves it. Today these rules are fixed in the code.
- The rules live in **one policy file**, separate from the settings file, with its own command `meho policy`.
- Every login has **one base level** (read-only, operator or admin). On top of it, it can have **any number of custom roles**. Permissions add up. A "deny" always wins.
- **Groups of people stay in Keycloak** (the login server). MEHO stores no membership.
- A file can make rules **stricter** as much as it likes. It can make them looser **only back to MEHO's shipped defaults**.
- Some safety rules live in the code and **always hold**, whatever the file says. They are listed under "The floors".
- Everything ships in phases, **behind switches that are off by default**. With every switch off, MEHO behaves exactly as today.

## Words used

- **Policy:** the rules that decide three things: who may run an operation, which operations wait for a person's approval, and who may approve.
- **Category:** a name for an area of work, for example network, storage or identity. A category can be put on a connector, an operation group or a single operation.
- **Risk level:** how risky an operation is: `safe` (reads), `caution`, `dangerous` or `destructive` (deletes).
- **Base level:** today's three levels of access: read-only, operator and admin. They decide which pages, commands and tools a login can use.
- **Custom role:** a role the customer defines. It says what the role may call, what it may approve, and what else it may change or read.
- **Binding:** a rule that says which login group gets which roles.
- **Routing:** a rule that says who is asked to approve a waiting change, for example "network changes go to the network team".
- **Rights change:** a change to who may do what: roles, bindings, routing, categories, labels and the policy file itself.
- **Floor:** a safety rule in the code that no policy file can loosen.
- **Break-glass switch:** today's emergency switch (`APPROVAL_ALLOW_SELF_APPROVAL`) that lets a person approve their own change when nobody else can. Every use is written to the audit log.

## Context

- Customers must be able to say which team may change what, and who approves it. For example: only the network team approves network changes.
- Today these rules are Python code. A customer can make one operation stricter, but nothing more.
- Who may approve depends only on the login group and the risk level. Everyone who may approve is asked, for every system.
- There are no categories on connectors or operations.
- The current records forbid what customers ask for: [approval-groups-and-self-approval.md](approval-groups-and-self-approval.md) says there are no exceptions per system, per connector or for a limited time. [login-role-groups.md](login-role-groups.md) allows exactly one role group per person.

## The twelve decisions

1. **Roles per login.** One base level per login (read-only, operator or admin), plus any number of custom roles on top. Permissions add up; deny wins.
2. **Where groups of people live.** People groups live in Keycloak. Bindings match the group attribute or the full path, never the short name. MEHO stores no membership.
   - Why not the short name: any sub-group can have the same short name, in any case, also one copied in from a company directory.
3. **How far a file may loosen.** Tighten freely; loosen only back to the shipped defaults. The floors always hold.
4. **Your own click for destructive changes.** The requester's own click stays enough for admins and SA-Operators, also for destructive changes. Routing may narrow who may click.
   - This keeps today's rule from [approval-groups-and-self-approval.md](approval-groups-and-self-approval.md).
5. **Custom roles that approve.** A custom role may approve other people's dangerous and destructive changes when its approve rule and the routing say so. Floors stay: agents never approve, nobody approves their own rights change, fresh sign-in for destructive.
6. **Categories.** One category list per install; tenants may add their own categories.
7. **Who may import a policy file.** A tenant admin for their tenant, a platform admin install-wide. Every import waits for a second admin.
8. **A site with only one admin.** A site with one admin may use the break-glass switch for rights changes. Every such approval writes an audit row and shows a warning banner. The install guide says: add a second admin.
   - This is the only way around the floor "rights changes need a second admin". It needs the switch, and it always leaves a trail.
9. **Agent grants.** Agent grants stay out of the policy file for now. They keep their own table and commands.
10. **The file.** A separate policy file with its own command (`meho policy import|export|diff|validate|explain`), not a section of the settings file.
    - The settings import applies at once and never deletes. A policy import must replace the whole file, or a removed right would stay. And it must wait for a second admin. So the two files have different import rules.
11. **History.** MEHO keeps the last 20 policy files per tenant: who imported it, when, the file and the diff.
12. **"Always wait" below the code.** The file may not turn "always wait" off below the code. That stays the platform admin's single edit with a reason and a fresh sign-in, never for destructive.

## The floors: rules that always hold

These rules live in the code, not in the policy file. The import refuses a file that goes below one. At run time, MEHO checks the floors first.

1. **Agents, services and runners never approve.** A runner is the program that runs checks in a remote network. Only a person approves.
2. **How a person approves:** with a click in the web interface, or on the command line with a fresh sign-in or a one-time code. Never with a plain token.
3. **Token calls and agents run only `safe` operations at once.** This holds for MCP, the command line and REST, also for a person's own token. Everything else waits for approval.
4. **A `destructive` operation always waits for a click.** No grant clears this wait. For admins and SA-Operators the click may be their own (decision 4).
5. **Minimum levels stay.** An operation that reads a secret, or that connects to an address the caller gives, is at least `caution` and always waits.
6. **Rights changes need a second admin.** Nobody approves their own rights change. The only way around it is the break-glass switch on a site with one admin (decision 8).
7. **Every decision writes an audit row that names the rule** that matched.

## What this record replaces

This record replaces two rules on purpose. The text of the old records stays. Each old record gets a short note at the top that points here.

- **[approval-groups-and-self-approval.md](approval-groups-and-self-approval.md), section "No exceptions":** the sentence "There are no exceptions per system, per connector or for a limited time. The rules are the groups only."
  - Now: categories, custom roles and routing may give different rules per connector, per operation group and per operation.
  - The rest of that record stays: who approves today, what waits, how a person approves, and the limits of the break-glass switch for `dangerous` and `destructive` changes.
- **[login-role-groups.md](login-role-groups.md), rule 1 "One group per person":** a login is in exactly one role group.
  - Now: a login still has exactly one base level. On top of it, it may have any number of custom roles from other groups (decision 1).
  - The rest of that record stays: the five groups, the group attributes and the stock Keycloak mappers.

## Consequences

- **Phased delivery behind switches.** Each phase ships behind its own switch, off by default. A switch becomes the default only after a test shows no change to today's answers and a run on the sandbox (a test system) confirms it.
  - Part 1 ([#6033](https://github.com/evoila/meho/issues/6033)): fixes to the floors first; then categories and the policy file; then the policy check that runs next to today's code and only logs differences; then approver routing by category.
  - Part 2 ([#6049](https://github.com/evoila/meho/issues/6049)): custom roles, and then the new policy check makes the decisions.
- **The "small team" default equals today.** MEHO ships a "small team" policy file. MEHO without an imported file behaves exactly like it, and importing it changes nothing. A test proves this.
- **Two more shipped files** come later: "separated duties" (network, storage, compute and identity teams) and "read-only agents".
- **The policy file has its own command and its own REST endpoints.** The settings import does not read it.
- **The login check changes with custom roles.** Today MEHO refuses a login in two role groups. With custom roles, a login keeps exactly one base level, and other groups may add custom roles on top. The Tasks of part 2 change this check.
- **Sites with one admin** must turn on the break-glass switch to change rights. The install guide tells them to add a second admin instead.
- **Agent grants do not change.**

## References

- v0.1-spec §7: approval is a human decision. This record keeps that line.
- [approval-groups-and-self-approval.md](approval-groups-and-self-approval.md): who approves today, and how.
- [login-role-groups.md](login-role-groups.md): the five login groups.
- [2026-10-28-several-tenants-per-person.md](2026-10-28-several-tenants-per-person.md): one role group per tenant.
- [governed-delete-operations.md](governed-delete-operations.md): the `destructive` level.
- `docs/codebase/approvals.md`: the approvals plane today.
- `docs-site/guides/approvals-and-break-glass.md`: the break-glass switch.
