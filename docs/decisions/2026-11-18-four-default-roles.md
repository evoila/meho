# The four default roles, and who changes rights (decision)

**Status:** Proposed, waits for the owner's word. The owner made these decisions on 18 November 2026. This record becomes the decision when the owner merges its pull request.
**Date:** 2026-11-18
**Initiatives:** [#6049](https://github.com/evoila/meho/issues/6049) (roles that the customer defines, planned again with the console pages first). Part 1 of the configurable policy, [#6033](https://github.com/evoila/meho/issues/6033), stays as built.
**Changes:** [login-role-groups.md](login-role-groups.md), [approval-groups-and-self-approval.md](approval-groups-and-self-approval.md), [2026-11-09-configurable-policy.md](2026-11-09-configurable-policy.md), [2026-10-28-several-tenants-per-person.md](2026-10-28-several-tenants-per-person.md). See "What this record changes" below.

Most of this record is not built yet. Where it says "MEHO will", the code does not do it today.

## The decision in short

- MEHO ships **four default roles**: Reader, Operator, Admin and Platform Admin.
- The groups MEHO-SA-Operators ("self-approval operators") and MEHO-Approvers are **no longer defaults**. MEHO will read them as Operators and Readers, so nobody is locked out.
- **Only an Admin or a Platform Admin approves their own waiting change**, at every risk level. MEHO records it as self-approved.
- **An Admin changes rights alone.** A change of rights that an Admin makes with a click in the web console runs at once. There is no waiting request and no second click. The audit log records it.
- **A change of rights asked with a token or by an AI agent waits.** An Admin approves it with a click in the web console. The Admin who asked may give that click.
- **Customers define other roles themselves.** The console page comes first. The policy file is exported from it, loaded into it, or loaded at install.
- **The built-in install creates one Admin.** That first Admin is also the Platform Admin.
- **An Operator keeps exactly today's rights.**

## Why

The owner's words:

- "We have overcomplicated the default roles. Let's keep only Reader, Operator, Admin and Platform Admin and let customers customize for themselves."
- "Only Admin can change rights, that has nothing to do with approval in my view."
- "One Admin should be enough and it should be able to change everything. The first Admin is at the same time the platform admin."
- "Console page is always first, file can be exported from it or loaded into it (or on install)."

## Words used

- **Role:** what a person may do in MEHO. A person gets it from their login group in Keycloak (the login server).
- **Risk level:** how risky an operation is: `safe` (reads), `caution`, `dangerous` or `destructive` (deletes). Low risk means `safe` and `caution`. High risk means `dangerous` and `destructive`.
- **Waiting change:** a change that is stored and runs only after a person approves it.
- **Own change:** a change that I asked for, and that now waits.
- **Change of rights:** a change to who may do what: login groups, roles, sign-in settings, operation categories and the policy file.
- **Token call:** a call from the command line, an MCP client (for example an AI tool) or the REST API. It is not a click in the web console.
- **Self-approved:** the person who asked for the change also approved it. The audit log says so.
- **A second person:** a different login. MEHO compares logins, not people. The docs and the approval screen will say so.

## The four roles

| | Reader | Operator | Admin | Platform Admin |
|---|---|---|---|---|
| Read | yes | yes | yes | yes |
| Run a low-risk change | no | yes | yes | yes |
| Run a high-risk change | no | yes, it waits for an Admin | yes, it waits | yes, it waits |
| Approve another person's low-risk change | no | yes | yes | yes |
| Approve another person's high-risk change | no | no | yes | yes |
| Approve my own change | no | only a `caution` change that waited because it came with a token, with a click in the web console | yes, every risk level | yes, every risk level |
| Change rights | no | may ask; it waits for an Admin | yes, alone | yes, alone; also the platform admin group |
| Act in every tenant | no | no | no | yes |

- What waits does not change. In the web console, `safe` and `caution` changes run at once. With a token, only `safe` changes run at once. Everything else waits. A change of rights is the one new case: see the next section.
- An Operator keeps exactly today's rights. An Operator gains nothing and loses nothing.
- Who approves another person's change that always waits (a credential read, write or make) stays as today. An Operator approves another person's low-risk one. A new target and high-risk changes need an Admin.
- AI tools, agents, service logins and runners never approve.
- One Admin login is enough for a site. A site with one person makes that person an Admin. There is no new setting for this.

## Who changes rights

- Only an Admin or a Platform Admin changes rights. Changing rights is the Admin's job. It is not a question of a second approver.
- **An Admin's own change in the web console runs at once.** No waiting request, no second click. The audit log records who made it.
- **A change of rights asked with a token or by an AI agent waits.** This is also true for the Admin's own token or own agent.
- **A change of rights asked by an Operator waits** for an Admin.
- **A waiting change of rights is approved only with a click in the web console**, by an Admin or a Platform Admin. The command line and the REST API do not approve it. Only the web console's own sign-in can make such a click. No token can.
- The Admin who asked (with his token or his AI agent) may give that click himself. The audit log records it as self-approved.
- These rules cover every operation that changes rights: the rights writes in Keycloak, the policy file import and the category and label edits.
- An Admin may also change the rights of his own login, for example his own passkey or group.
- MEHO will refuse Keycloak writes that would lock MEHO's own sign-in user out. This guard is planned, and it stays after this change.
- A change to the platform admin group still needs a Platform Admin.

Today every change of rights waits, also in the web console, and another admin must approve it. MEHO will change this after this record is built.

## Customer roles

- A customer who wants more than the four roles defines them in MEHO.
- The console page comes first. The policy file is the export and import format of what the console holds. It can also be loaded at install.
- MEHO ships no example files for extra roles. The groups "Approvers" and "Self-approval operators" do not come back as example files.
- The documentation will explain how a customer builds such a role, for example a role that may approve but not run changes.

## The built-in install

- The built-in install will create one Admin login. That login is also the Platform Admin.
- It will not create a separate approver login.
- New sites and new tenants will get three role groups: Readers, Operators and Admins. The platform admin group stays.

## The upgrade

- MEHO will read the old groups as the new roles. A person in MEHO-SA-Operators counts as an Operator. A person in MEHO-Approvers counts as a Reader. Nothing changes in Keycloak.
- Waiting changes stay. MEHO checks who may approve at the moment of the click, so the new rules apply to them at once.

What a person in MEHO-SA-Operators no longer does:

- approve their own change (except the `caution` token case that every Operator has);
- approve another person's high-risk change.

Their own high-risk change now waits for an Admin.

What a person in MEHO-Approvers no longer does:

- approve any change;
- see the approval pages. They read, like a Reader.

People in the two old groups also stop getting the message "a change waits for you".

**Before you upgrade:** if a person must keep approving high-risk changes or their own changes, put them in MEHO-Admins (or the tenant's Admins group) and take them out of the old group. A person may be in only one role group per tenant. The upgrade notes will say this.

## Rules that stay

- A call that reads, writes or makes a credential always waits for a person.
- Making a new target always waits for a person.
- Some operations always wait for a person, whatever the role.
- Approval is a person's decision. AI never approves.
- The emergency self-approval switch `APPROVAL_ALLOW_SELF_APPROVAL` stays exactly as it is. It never lets a person who is not an Admin approve a change of rights.

## What gets weaker, and the guards

- **One stolen Admin login can change rights alone.** Today a second admin must approve. The guards: the web console sign-in (a token cannot act as a console click), the approval sign-in level where it applies, passkeys, and the audit log.
- **An Admin may approve an AI agent's change of rights without reading it.** The guards: the approval card shows what the change does, the request names the agent, and the audit log.
- **An Admin may lock himself out.** The guard: the Keycloak administrator of the `master` realm stays. A planned guard will also refuse Keycloak writes that would lock out MEHO's own sign-in user.
- **A policy import or a category edit has no second pair of eyes.** The guards: the dry run and the diff before an import, the history of the last 20 policy files, and the audit log.
- **People in the two old groups lose rights on the upgrade day.** The guards: the upgrade notes name each loss, and the steps "before you upgrade" above.

## What this record changes

Each older record keeps its text and gets a note at the top that points here.

- **[login-role-groups.md](login-role-groups.md):** the five groups. New installs will get three role groups (Readers, Operators, Admins) and the platform admin group. MEHO will read MEHO-SA-Operators as Operators and MEHO-Approvers as Readers.
- **[approval-groups-and-self-approval.md](approval-groups-and-self-approval.md):** only Admins and Platform Admins approve their own changes. MEHO-SA-Operators and MEHO-Approvers are no longer default groups. An Admin's own change of rights in the console runs at once. A waiting change of rights is approved only with a click in the web console.
- **[2026-11-09-configurable-policy.md](2026-11-09-configurable-policy.md):** an Admin changes rights alone. In the console, it runs at once. A change of rights asked with a token or by an AI waits for an Admin's click in the console. Decision 8 and floor 6 (a second admin, and the emergency switch for a site with one admin) are withdrawn. Decision 7 and decision 10 no longer need a second admin for an import. Decision 4 and floor 4 no longer name SA-Operators. The console comes first, and the file is its export and import. No more shipped example files.
- **[2026-10-28-several-tenants-per-person.md](2026-10-28-several-tenants-per-person.md):** an extra tenant gets three role groups. A Platform Admin who acts as an Admin approves their own change. An Admin changes tenant groups alone.

## Not decided here

- The order of the work and its size.
- When MEHO stops reading the two old group names. That comes in a later release, and its notes will say "move people first".
- The details of the console pages for customer roles.

## References

- v0.1-spec §7: approval is a human decision. This record keeps that line.
- [login-role-groups.md](login-role-groups.md): the login groups.
- [approval-groups-and-self-approval.md](approval-groups-and-self-approval.md): what waits, and how a person approves.
- [2026-11-09-configurable-policy.md](2026-11-09-configurable-policy.md): categories, routing and the policy file.
- [2026-10-28-several-tenants-per-person.md](2026-10-28-several-tenants-per-person.md): one person in several tenants, and the platform admin.
- `docs-site/guides/approvals-and-break-glass.md`: the approvals guide.
