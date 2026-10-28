# Who may approve a change, what waits for approval, and how a person approves (decision)

**Status:** decided — product owner. This record: 2026-10-28.
**Goal:** approvals stay a human decision (v0.1-spec §7) while a trusted person can approve their own change
**Issue:** [#3326](https://github.com/evoila/meho/issues/3326)
**Initiative:** [#3325](https://github.com/evoila/meho/issues/3325) — MEHO-SA-Operators approve their own changes on purpose; AI never approves
**Groups from:** [login-role-groups.md](login-role-groups.md) ([#3959](https://github.com/evoila/meho/issues/3959))
**Narrows:** requirement 1 of [governed-delete-operations.md](governed-delete-operations.md), only the "no self-approval" clause, and only for MEHO-SA-Operators
**Replaces:** the "DENY for agent principals" rule for `destructive` changes in [governed-delete-operations.md](governed-delete-operations.md)

## The decision in short

- **The purpose of an approval:** a person looks at a change and approves it on purpose.
- **AI tools and agents never approve.** Not their own changes, and not anyone else's.
- **Only the web interface counts as a person.** Every call made with a token follows the agent rule: only safe operations run at once.
- **People in the MEHO-SA-Operators group may approve their own changes**, on purpose.
- **Approval needs proof that a person is present:** a click in the web interface, or on the command line a fresh sign-in or a one-time code. A plain API token never approves.

The rest of this record says this in detail. The full table of who may approve what is in [#3325](https://github.com/evoila/meho/issues/3325) ("Who can do what"). This record does not copy every cell of it.

## Words used

- **Safety level:** how risky an operation is: `safe` (reads), `caution`, `dangerous` or `destructive` (deletes).
- **`requires_approval`:** a mark on an operation. It says the operation always waits for approval, whatever its safety level.
- **Web session:** a person signed in to the web interface. The browser holds a session cookie; MEHO holds the tokens (locked decision #11).
- **Token call:** any call that sends a token itself: MCP (for example an AI tool), the `meho` command line, and REST/API calls. A person's own token counts too.
- **Agent:** a login that MEHO marks as an AI agent (`principal_kind=agent`).
- **Service principal:** fixed automation, for example a build pipeline. It signs in with its own client secret.
- **Standing grant:** a person's approval, given in advance, for one service principal and one operation.
- **Self-approval:** the person who started a change also approves it.

## The groups and who may approve

The groups come from the login ([login-role-groups.md](login-role-groups.md)).

- **MEHO-Operators** run changes. Their `dangerous` and `destructive` changes wait for a MEHO-SA-Operator or an admin. They may not approve their own `dangerous` or `destructive` change.
- **MEHO-SA-Operators** run changes like Operators. They may also approve their own changes, on purpose. "SA" means self-approval.
- **MEHO-Admins** may approve other people's changes, also `dangerous` and `destructive` ones.
- **MEHO-Approvers** (optional) may approve other people's lower-risk changes that need approval. They may not approve `dangerous` or `destructive` changes.
- **No group membership means no self-approval.**

**Group membership comes from the login only.** Keycloak puts the group into the login token ([#3959](https://github.com/evoila/meho/issues/3959)). MEHO never reads it from a request field, a header or a command-line flag.

## What waits for approval

### A person in the web interface

- `safe` and `caution` changes run at once.
- `dangerous` and `destructive` changes wait for an approval click.
- Changes marked `requires_approval` wait for an approval click.
- A MEHO-SA-Operator may give that click for their own change.

### Every token call, and every agent

- Only `safe` operations run at once.
- `caution`, `dangerous` and `destructive` operations wait for an approval click.
- This holds for MCP, the command line and REST/API tokens, also for a person's own token.
- Agents follow the same rule. An agent's `destructive` request is no longer refused. It waits until a person checks the preview and approves it with a click (for example a MEHO-SA-Operator).

**Why a token call counts as an agent:** an AI tool can use a person's login. MEHO does not try to detect who or what is behind a token. So only the web session counts as a person, and everything with a token gets the stricter rule.

**A person may confirm their own token `caution` change.** A MEHO-Operator's `caution` change that waits only because it came with a token may be approved by the same person, with a click in the web interface. Their `dangerous` and `destructive` changes keep the group rules above.

### Service principals keep today's rule

- `safe` runs at once.
- `caution` and `dangerous` run when a standing grant covers them. Otherwise they wait for approval.
- `destructive` always waits for a person. A standing grant never covers it (except on a sandbox instance, [sandbox-mode.md](sandbox-mode.md)).
- The token rule above does not apply to them.

**Why:** a service principal is fixed automation. It does the same steps each time. A standing grant is a person's deliberate approval, given in advance.

## How a person approves

- **In the web interface:** a click.
- **On the command line or REST:** one of these two:
  - a fresh browser sign-in, at most 5 minutes old. The 5 minutes is a setting. One sign-in allows exactly one approval.
  - a one-time code from the person's authenticator app (a Keycloak one-time password).
- **A plain API token never approves.** MEHO refuses such an approval with a clear message.
- **Reject needs no fresh sign-in.** A reject never runs anything.
- **Approve and reject have no MCP path.** They stay human-only (v0.1-spec §7, `backend/src/meho_backplane/mcp/human_only.py`).
- **Every approval records who approved and how** (web click, command line with a fresh sign-in, or authenticator code), and whether it was a self-approval.

## No exceptions

- There are no exceptions per system, per connector or for a limited time. The rules are the groups only.
- The emergency self-approval switch `APPROVAL_ALLOW_SELF_APPROVAL` stays as it is. It covers only `safe` and `caution` changes. It never reaches `dangerous` or `destructive` changes.

## What this changes in older decisions

### Requirement 1 of governed-delete-operations.md: narrowed

Requirement 1 says: *"Mandatory human approval, always. No standing grant applies, no self-approval, no autonomous-session approval."*

- This record replaces the **"no self-approval"** clause, on one condition only: a member of MEHO-SA-Operators may approve their own change, on purpose.
- For everyone else, "no self-approval" stays.
- **"Mandatory human approval"**, **"no standing grant"** and **"no autonomous-session approval"** stay fully in force.
- A MEHO-SA-Operator's self-approval is still a human approval, given on purpose. It is not an automatic run.

### "DENY for agent principals" for destructive: replaced

[governed-delete-operations.md](governed-delete-operations.md) says the `destructive` level is *"DENY for agent principals, non-grantable, human-approval-always"*.

- Now an agent's `destructive` request waits for a person's approval, under the group rules. For example, a MEHO-SA-Operator checks the preview and approves it with a click.
- The preview hash and the blast-radius statement (requirements 2 and 3) stay required.
- "Non-grantable" and "human-approval-always" stay. Agents still never approve.

## The model we keep, and the one we reject

- **Kept:** the change still waits for approval. Only *who* may approve changes. A MEHO-SA-Operator's change waits like any other, and the same person then approves it.
- **Rejected:** a MEHO-SA-Operator's changes run at once, with no wait.
- **Why:** the wait keeps three things that matter:
  - the preview-hash binding: the approver sees exactly what will run;
  - the blast-radius review: the approver sees what else is affected;
  - the approval record: the audit log shows who approved, how, and when.

## Today's code, for the follow-up Tasks

This record changes no code. The code Tasks under #3325 implement it.

- **Who may approve:** `_check_self_approval` and `_NO_SELF_APPROVAL_TIERS` in `backend/src/meho_backplane/operations/approval_queue.py`. Today nobody may approve their own `dangerous` or `destructive` change ([#3328](https://github.com/evoila/meho/issues/3328) changes this).
- **The group flag:** `Operator.approver` in `backend/src/meho_backplane/auth/operator.py` is the model for a MEHO-SA-Operators flag ([#3327](https://github.com/evoila/meho/issues/3327)).
- **What waits for a person or a service principal:** `_non_agent_verdict` and `_non_agent_park_reason` in `backend/src/meho_backplane/operations/_validate.py`. Today only `destructive` and `requires_approval` changes wait for a person; `caution` and `dangerous` run at once on every path ([#3980](https://github.com/evoila/meho/issues/3980) changes this).
- **What agents get:** `_SAFETY_DEFAULT` and `_SAFETY_CEILING` in `backend/src/meho_backplane/auth/permissions.py`. Today `caution` waits, `dangerous` is refused unless a grant covers it, and `destructive` is refused ([#3980](https://github.com/evoila/meho/issues/3980) changes this).
- **Fresh sign-in or one-time code:** [#3979](https://github.com/evoila/meho/issues/3979).

## Order of work

1. Self-approval for MEHO-SA-Operators ships first ([#3328](https://github.com/evoila/meho/issues/3328)).
2. The new waiting rules ([#3980](https://github.com/evoila/meho/issues/3980)) ship after it. In the other order, every `dangerous` change would need a second person until #3328 ships.
3. Self-approval on the command line ships with the fresh sign-in check ([#3979](https://github.com/evoila/meho/issues/3979)), or after it.

**Upgrade note:** before a site upgrades to the version with these rules, it needs at least one member in MEHO-SA-Operators. Otherwise nobody can approve `dangerous` or `destructive` changes. The upgrade notes and the docs say this ([#3330](https://github.com/evoila/meho/issues/3330)).

## References

- v0.1-spec §7: approval is a human decision. This record keeps that line.
- [governed-delete-operations.md](governed-delete-operations.md): the `destructive` level and its four requirements.
- [login-role-groups.md](login-role-groups.md): the five login groups.
- [sandbox-mode.md](sandbox-mode.md): the one standing-grant exception for deletes.
- [locked-decisions.md](locked-decisions.md) decision #11: the web session model.
- `docs/codebase/approvals.md`: the approvals plane today.
