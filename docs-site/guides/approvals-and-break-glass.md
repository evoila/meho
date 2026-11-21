# Approvals and break-glass

Some operations change infrastructure. MEHO does not let those run
silently. A change that needs approval does **not** run when you call
it. It **waits** in an approval queue and returns `awaiting_approval`.
It runs only after a person approves it. What waits depends on how the
call arrives:

- **In the web interface:** `safe` and `caution` operations run at once.
  `dangerous` and `destructive` operations, and operations marked
  `requires_approval`, wait for an approval click.
- **With a token, and for every agent:** MCP (for example an AI tool),
  the `meho` command line and REST calls. This also holds for your own
  token. Only `safe` operations run at once. `caution`, `dangerous` and
  `destructive` operations wait for an approval click. MEHO does not try
  to tell who is behind a token, because an AI tool can use a person's
  login.
- **Service principals** (fixed automation, for example a build
  pipeline) keep their own rule. A standing grant (a person's approval,
  given in advance) lets their `caution` and `dangerous` changes run.
  Without one, they wait. Their `destructive` changes always wait for a
  person. Only a person makes a standing grant. For an operation that
  needs the **tenant_admin** role (registering a target, changing the
  topology), that person needs the **tenant_admin** role too.

Who may approve depends on the person's login group (see
[Who may approve](#who-may-approve)). The web interface is the normal
place to approve. On the command line you can approve too, after a fresh
sign-in (see [Approve on the command line](#approve-on-the-command-line)).
AI tools and agents never approve. Approve and reject have no MCP tool.

!!! note "Baseline: the emergency switch is off by default"

    `config.approvalAllowSelfApproval` defaults to **`"false"`**
    (`APPROVAL_ALLOW_SELF_APPROVAL=false`), the secure baseline. With it
    off, only the self-approval rules of the groups apply, and
    `GET /ready` reports `approval_queue.effective_posture:
    "four_eyes_enforced"`. Leave it off. Setting it `"true"` is an
    audited emergency switch for the whole deploy (Option 2 below). It
    covers only `safe` and `caution` changes and never reaches a
    `dangerous` or `destructive` operation. The endorsed way for one
    person to approve their own changes is the **MEHO-Admins** group, not
    the switch.

This guide also covers the case where **you are the only operator.** A
solo deploy still has to be able to make changes that wait. This page
shows how one person approves their own change on purpose, the other
ways to clear the queue, and how to show an auditor which way your
deploy uses.

!!! note "Prerequisites"

    - A running backplane and a connected client —
      [the install trail](../install/index.md) and
      [Connect clients](../clients/index.md).
    - A registered target you can dispatch a write against —
      [Register targets and secrets](targets-and-secrets.md).
    - A session in one of the groups that may approve (see
      [Who may approve](#who-may-approve)). `read_only` sessions without
      the `approver` flag cannot decide.

## What you see when a write waits

Call an operation that needs approval — from the CLI, an agent, or the
operator console — and instead of a result you get a waiting request:

```bash
meho operation call vault-1.x vault.kv.put --target <vault-target> --params '{...}'
# → status: awaiting_approval
#   approval_request_id: 4b1c…
```

The waiting request has no side effect: nothing was written to the
target. It is now in the queue, which you can list and inspect:

```bash
meho approvals list                 # pending requests
meho approvals show <id>            # the waiting operation + its proposed effect
```

To act on it you **approve** or **reject** it, in the web interface or
on the command line. Both use one backplane path (same policy, same
audit row):

```bash
meho approvals approve <id>         # runs the waiting write now
meho approvals reject <id>          # terminal; the requester must ask again
```

After approval, the person who sent the request reads what the operation
printed with one command. Only that person can read it:

```bash
meho approvals result <id>          # the output of the approved run
```

The operator console shows the same queue at **`/ui/approvals`**: a
bell with the live pending count, a request-detail window with the
proposed effect, and Approve / Deny buttons. The window also says
whether you may approve the request, and why not.

A `linux.script.run` request shows the script and each `env` name with
its value. Read the `env` values as carefully as the script: some names
change what a tool runs (for example `TAR_OPTIONS` or `GIT_SSH_COMMAND`).
The run uses exactly these values; a call with other values needs a new
approval. Secrets do not belong in `env`. Send them in `secret_env`, as a
Vault path and field: the request shows only that reference, never the
secret. MEHO refuses a call with a secret-looking `env` name (for example
`DB_PASSWORD`) or value (for example a token or a password in a URL)
before it waits for approval. The `env` values are for the person who
approves: reads by an agent or a token show the `env` names only (the
MCP approval tools, `preview_operation`, and the REST approval reads of a
`linux.script.run` request with an agent, service or runner login).

When several requests belong to one change ticket and depend on each
other, approve them as a group, oldest first:

```bash
meho approvals approve --filter-work-ref <ticket> --in-order
```

Each step is an ordinary approval and runs at once. MEHO goes on to the
next step only when the run was OK. It stops at the first step that fails
or is refused, and names that step and the steps it did not run. At each
step it shows what the step does and asks you to type `yes` first. In
the console, filter the Pending tab to the change ticket and use
**Approve group in order**. The sign-in rules hold for every step: when
a step needs a new sign-in (your sign-in is too old, or the step is
`destructive` and your sign-in already approved something), the run
stops there and shows **Sign in again**. Sign in, then start the run
again; the steps that ran OK are skipped.

You can also approve several requests by id in one run:
`meho approvals approve <id> <id> ...`. The command shows each request
(what it does, the operation, the connector and its version, the target,
the risk level and the parameters) and asks you to type `yes` for each
one. Anything else skips that request.

## Approve on the command line

**Approving asks you to sign in again.** An approval must come from a
person who is present, so MEHO checks the sign-in behind every approval.
The web interface is the normal place to approve. There, your sign-in
must be at most 15 minutes old and must have asked for your password
or a passkey. When it is older, the request shows **Sign in to approve**
instead of **Approve**: click it, sign in with your password (and your
authenticator code) or with a passkey, then open the request again and
approve. A passkey is one tap with Face ID, Touch ID, a fingerprint or
Windows Hello ([how to add one](phone-and-laptop.md#sign-in-with-a-passkey)).

On the command line and REST, MEHO needs a fresh sign-in made only for
approvals. So `meho approvals approve` opens your browser and asks for
your password, and for the one-time code from your authenticator app
when your realm asks for one.

- The browser always asks for your password, even when you are signed in
  to the console in the same browser.
- One sign-in covers approvals for 15 minutes. In that time one run of
  the command can approve several requests.
- An operation marked `destructive` needs a sign-in of its own, at most
  5 minutes old. The command opens the browser again for it.
- The command keeps the sign-in in memory for this one run. It never
  stores it.
- Your normal `meho login` stays as it is, but it never approves. An MCP
  token, an API token or an agent token never approves either.
- Reject needs no new sign-in, in the console or on the command line.
- The same limits hold in the console: 15 minutes for one sign-in, and a
  sign-in of its own for a `destructive` operation.
- An admin can change both limits (`FRESH_SIGN_IN_MAX_AGE_SECONDS` and
  `PER_ACTION_SIGN_IN_MAX_AGE_SECONDS`).

Without a browser (for example over SSH), the command prints the sign-in
link and stops. Approve in the console instead.

## A second person is a second login

MEHO tells two people apart by their login: the login ID in the token.
It does not know when two logins belong to one person. So "a second
person" always means a second login. If one person has two logins, MEHO
treats them as two people. The approval card in the web console says the
same in one line.

## Who may approve

Your login group decides. MEHO reads it only from your signed login
token, never from a request field or a command-line flag. The groups
come from the [Keycloak realm setup](../install/keycloak-realm.md#step-5-create-the-three-groups).

| Your group | Your own change | Another person's `safe` or `caution` change (also one marked `requires_approval`) | Another person's `dangerous` or `destructive` change | Another person's [change of rights](#changes-of-rights) |
|---|---|---|---|---|
| `MEHO-Admins` | yes, at every risk level; a change of rights only with a click in the web interface | yes | yes | yes, with a click in the web interface |
| `MEHO-Operators` (also the old group `MEHO-SA-Operators`) | only a `caution` change that waited because you sent it with a token, with a click in the web interface | yes, but not a change of rights | no | no |
| `MEHO-Readers` (also the old group `MEHO-Approvers`) | (you cannot run changes) | no | no | no |

A platform admin (`MEHO-Platform-Admins`) who has no role group in a
tenant acts as its admin there, so they may approve another person's
change of rights in that tenant. A platform admin with a lower role
group in a tenant keeps that role there.

MEHO reads the old group `MEHO-SA-Operators` as Operators and the old
group `MEHO-Approvers` as Readers. MEHO-Admins may approve their own
changes. A change of rights has its own rules (see
[Changes of rights](#changes-of-rights)). A MEHO-Admin's change still waits
like any other. The same person then approves it, on purpose: with a click in
the web interface, or on the command line after a fresh sign-in. The
wait keeps three things: the approver sees exactly what will run, sees
what else is affected, and the audit log records who approved, how and
when. There are no exceptions per system or per connector. A few
operations, for example registering a target, also need an admin as the
approver, whatever the group.

Approval keys on your **subject**: the stable `sub` claim on your
token, not your display name or your role. When the rules above do not
let you approve your own request, every surface refuses with the same
text:

```text
self_approval_forbidden: operator '<your-sub>' may not approve approval_request
<id>: You cannot approve your own change. Only members of the
MEHO-Admins group may approve their own changes. Ask
another person in MEHO-Operators or MEHO-Admins to approve it. They find it
on the Approvals page of the web interface.
```

That is a REST `403` (the command line prints it), and a disabled
**Approve** button in the console. The text says only who may approve
the change and where to ask. The groups come from the rules in the table
above: for a `dangerous` or `destructive` change the text names
MEHO-Admins. With approver routing on, it names
the groups the policy routes the change to. For your
own `caution` change that waits only because you sent it with a token,
the text says instead that you may approve it yourself with a click in
the web interface. Another
person's `dangerous` or `destructive` change, approved by someone who is
neither, gets `403 approval_group_required`.

### A request of an agent counts as a request of the people behind it

An agent, an add-on (a service login) or a satellite runner does not ask
on its own. People set it up. So MEHO treats its request as a request of
the people behind it:

- who registered or owns the agent, add-on or runner;
- who gave one of its grants that are in force;
- who wrote or last changed its agent definition;
- who made one of its schedules, or an event source when a schedule waits
  for events;
- who started or handed over the runbook run it works on.

Only people count. An agent, add-on or runner login that set up, changed
or handed over something is never one of the people behind another login.

Such a person may approve the request only where they may approve their
own change (the table above). A change of rights always needs another
admin, also when an admin is behind the agent. Everyone else approves it
as another person's change.

When MEHO knows nobody behind the login (for example a login made only in
Keycloak, or one that only other machine logins set up), only an Admin may
approve its requests.

The request shows these people: the request card and window in the web
interface ("Set up by ... Grant by ..."), `meho approvals show`
("Behind it:"), and the REST answer (`people_behind`) when a person asks.
For an agent, add-on or runner login, the REST answer leaves the two
fields out. When MEHO knows
nobody, they say "Set up by: not known".

Use an agent to run changes on a schedule. An agent is never a way to
approve your own change.

To take back your *own* pending request, withdraw it:
`meho approvals withdraw <id> [--reason TEXT]` (or
`POST /api/v1/approvals/{id}/withdraw`). The request then shows
"withdrawn by the sender", nobody can approve it, and the operation does
not run. A person, an agent or a service account can each withdraw its
own request.

<a id="changes-of-rights-need-another-admin"></a>

## Changes of rights

A change of rights is a change that can give someone more rights. For
such a change, these rules hold in every realm a Keycloak target
manages, not only in MEHO's own login realm:

- **An admin makes it alone in the web console.** An admin is a tenant
  admin (`MEHO-Admins`) or a platform admin (`MEHO-Platform-Admins`).
  When an admin starts the change with a click in the web console after
  a fresh sign-in, it runs at once. No request waits. MEHO asks for the
  same sign-in as for an approval in the web console. Without it, and
  when MEHO cannot show on the approval card who the change gives rights
  to, the call waits for an approval. In MEHO's own login realm only a
  platform admin makes a change of rights alone (see below). The audit
  row of the call says that it was a change of rights that an admin ran
  at once in the web console, also in its main columns
  (`policy_rule: default:rights_change.admin_in_web_console` and
  `rights_change_run: admin_in_web_console`), so no second person
  approved it.
- **Every other call waits.** A call with a token waits: the command
  line, REST and MCP, also the admin's own token. A call of an agent, a
  service account or a hosted agent run waits. An Operator's call waits
  too, also in the web console.
- **Only an admin approves a waiting change of rights, and only with a
  click in the web console.** The admin may be the person who asked.
  MEHO then records it as a self-approval. In MEHO's own login realm
  stricter rules hold (see below). The command line and REST do
  not approve a change of rights.
- **A change of rights asked by an agent follows the rule of the person
  behind it.** An admin who stands behind the agent, add-on or runner
  that asked (who set it up, owns it, or gave it a grant) may approve its
  change of rights in the web console, after a fresh sign-in, the same
  as their own change. An Operator behind it never approves a change of
  rights. In MEHO's own login realm only a platform admin behind it may.
- An Operator cannot approve it.
- A standing grant or a run approval never covers it. Each call that
  waits needs its own approval, also the call of an agent or a service
  account.
- The break-glass switch never covers it.

Some changes of rights always wait, also for an admin in the web console:
a change that reads, writes or makes a credential (for example a new
password, a new user, or the realm settings with the mail password), a
change whose values carry a credential (for example a client update
with a new client secret), a `destructive` change, a change with the "always wait" mark (from an
override or a classification rule), a `secret.move` with a
`keycloak:` side, and, outside MEHO's own login realm, a Keycloak write
that does not change one login or group (for example a new client scope
or protocol mapper, a change of a realm or a client, or turning a login
on).

When you may not approve such a change, MEHO answers "This is a change
of rights. Only an admin may approve it." On the command line and over
REST it answers "This is a change of rights. Approve this change of
rights in the web console."

These Keycloak writes are changes of rights:

- **Who has which role or group:** give a user a realm role or a role of
  a client, add a user to a group, or change the attributes of a group.
- **Who may sign in, and how:** create a user, change a user's user name
  or email, turn a login on, set a new password, remove a passkey or a
  one-time code, or set the steps a user must do at the next sign-in.
- **What a login token says:** create or change a realm or a client,
  create a client scope, or create or change a protocol mapper.
- **How strong a sign-in is:** set up MEHO's approval sign-in, the touch
  sign-in, or the step that skips the one-time code after a passkey.

The exact list, with the reason for each write, is in
[Writes that are changes of rights](https://github.com/evoila/meho/blob/main/docs/codebase/connectors-keycloak.md#writes-that-are-changes-of-rights).
A Keycloak write that only takes rights away (remove a user from a
group, turn a login off, delete) is not a change of rights. A few other
operations are changes of rights too, for example a policy import and a
change of an operation's category (see [the policy file guide](policy-file.md)).

### More rules for a change of one login or one group

A change that adds a login to a group, gives a login a role, changes a
user's details, removes a credential, or changes the attributes of a
group has more rules. They hold for an admin's call in the web console
and for the approval of a waiting request:

- An admin may change their own login, for example their own passkey or
  their own group. MEHO still refuses a Keycloak write that would lock
  out the user a target signs in with.
- MEHO must know which login and which group the change is for. When a
  user name or a group name could not be looked up, the change cannot
  run and the request cannot be approved. Send a new one.
- In MEHO's own login realm, the change needs a platform admin. The
  approval card says why when the change can give platform admin rights,
  or a role of Keycloak's `realm-management` client. For a group, MEHO
  checks what the group gives, not its name: the attribute
  `platform_admin=true` on the group or a parent group, a place under
  `/MEHO-Platform-Admins/`, or the realm role in
  `JWT_PLATFORM_ADMIN_ROLE_NAME`. A group MEHO cannot read counts too.
- In MEHO's own login realm, in `master` and in the target's admin realm,
  a platform admin approves every Keycloak write, and every `secret.move`
  that reads from or writes to one of these realms. A Keycloak admin in
  `master` can change every realm. The target's admin realm holds the
  login MEHO uses for that target. So the same rule holds there.
  This holds also for a write that sets only keys from a short list that
  cannot touch sign-in or rights: the display name of the realm
  (`keycloak.realm.update`), the name and description of a client
  (`keycloak.client.update`), and the first and last name of a user
  (`keycloak.user.update`). When a param names the realm,
  MEHO reads that realm from Keycloak and judges the name Keycloak answers
  with. When MEHO cannot read which realm a write changes, it counts as the
  login realm. The name of a new realm (`keycloak.realm.create`) may hold
  only plain ASCII characters: letters `a` to `z` and `A` to `Z`, digits,
  the space and the other ASCII signs except `/`, `\`, `?`, `#` and `%`.
  No letters with accents. When the name of one of these protected realms
  has a letter with an accent, every `secret.move` with a Keycloak source
  or sink on that target needs a platform admin. The approval card
  says why, in the row "Platform admin must approve". No standing grant
  and no run approval clears such a write: each call waits for a person.
  A person never approves a `secret.move` that sets the password of their
  own login.
- In MEHO's own login realm, a platform admin makes every change of
  rights alone in the web console, after a fresh sign-in (the approval
  sign-in level, inside the approval window). This holds for every
  Keycloak write that is a change of rights there, also one that does not
  change one login or group, such as a protocol mapper, a client or realm
  change, turning a login on, or setting up the approval sign-in. Without
  a fresh sign-in the change waits. A tenant admin who is not a platform
  admin never makes such a change alone and never approves it: the change
  waits, and only a platform admin approves it.
- In MEHO's own login realm, a write that can change the sign-in MEHO asks
  for before an approval never runs alone, also not for a platform admin
  with a fresh sign-in. It waits, and a second platform admin approves it:
  the login that asked never does. Such a write is a change of the clients
  of the approval sign-in or of the console login, a protocol mapper of the
  `acr`, `amr`, `auth_time` or `azp` claim or of the sign-in level, and a
  realm change of other settings than names, mail, events and languages.
- In MEHO's own login realm, a platform admin who asked for a change in
  the web console may approve it there, after a fresh sign-in, when it
  waits (for example because it reads, writes or makes a credential). A
  change asked with a token (the command line, REST, MCP) or by an agent
  is never approved by the login that asked, also not in the web
  console: a second person who is a platform admin approves it. A change
  asked by an agent, an add-on or a runner may be approved by a platform
  admin who stands behind that login, in the web console, after a fresh
  sign-in.
- A Keycloak write approved before this rule does not run: MEHO refuses it
  when the run starts, in every realm. Send the request again.

The approval is for the login and the group as they were when the request
was made. If a user name or a group name points at another account or group
when the approved change runs, the change does not run. The same happens
when the change now needs a stricter approval than the one it got. Send a
new request.

The decision record in the audit log names the login the change is for and
says whether it is the approver.

## When you are the only operator

On a team, a colleague approves your change. On a **one-operator**
deploy you are both the requester and the only person who could
approve. Use Option 1. Option 2 is for deployment admins in an
emergency.

### Option 1 — a MEHO-Admin approves their own change (recommended)

Put yourself in the **`MEHO-Admins`** group (see
[Keycloak realm setup](../install/keycloak-realm.md#who-may-approve-their-own-changes)).
The change applies at your next login. Then:

1. Call the operation. The change waits, as every change does.
2. Open the request in the web interface (`/ui/approvals`). Check the
   proposed effect and, for a delete, the "what this destroys" card.
3. Click **Approve**, then **Confirm**. The console then says that you
   approved your own change.

This works at every risk level, also for `dangerous` and `destructive`
changes. For a change of rights it works only with the click in the web
interface (see [Changes of rights](#changes-of-rights)).
On the command line, `meho approvals approve <id>` does the
same after a fresh sign-in, and prints a line that says it was a
self-approval. The decision record shows `self_approved: true` and the
rule `sa_operator_self_approval`.

A person is in exactly one role group per tenant. MEHO reads the old
group `MEHO-SA-Operators` as Operators, so a person there does not
approve their own change; make that person an Admin. The decision record
shows the rule `sa_operator_self_approval` (the stored value keeps its
name). If you want
a second person to check every risky change of an admin, keep the admins
out of risky work: give the people who run changes the `MEHO-Operators`
group.

### Option 2 — the audited break-glass flag (deployment admins, emergencies)

This part is for deployment admins. When Option 1 is not possible — an
incident, a first bring-up, a write you must make in the next five
minutes — a deployment admin can enable **self-approval** for the whole
deploy:

```yaml
# Helm values overlay
config:
  approvalAllowSelfApproval: "true"   # renders APPROVAL_ALLOW_SELF_APPROVAL=true
```

With the flag on, anyone may approve their own `safe` or `caution`
change. Understand exactly what you traded away:

- **It covers the whole deploy, not one write.** The flag re-opens the
  single-account *request-and-grant* hole for every **lower-risk**
  operation in every tenant of this deploy, not just the one you are
  trying to clear. There is no scoping to a single write.
- **It never reaches a `dangerous` or `destructive` operation.**
  Break-glass self-approval applies only to the `safe` and `caution`
  risk levels. For `dangerous` and `destructive` changes only the group
  rules count: a MEHO-Admin may approve their own, everyone else needs a
  MEHO-Admin.
- **It never covers a change of rights.** Only an admin approves a
  change of rights, in the web console, also with the flag on (see
  [Changes of rights](#changes-of-rights)).
- **It is for emergencies, not the solo default.** Reach for Option 1
  first. Leaving this on lets every person in every tenant approve their
  own lower-risk changes.
- **It is still audited.** Self-approval is not silent (see below). A
  reject never needs the flag.

!!! danger "Break-glass is a lever an admin holds, not a per-write toggle"

    Prefer a MEHO-Admin for anything you do more
    than once. Set
    `APPROVAL_ALLOW_SELF_APPROVAL=true` for a real emergency, make the
    write, and turn it back off.

## A segregated approver

A different need is **separation of duties** — someone who may approve
other people's changes but may never run an operation. MEHO expresses
that in the code: a principal can carry an **`approver`** capability,
independent of its tenant role. An approver may list, inspect, approve,
and reject waiting requests, yet dispatch stays gated on the operator
role — so a `read_only` login with the `approver` flag would approve
another person's lower-risk change but is refused `call_operation`
outright. No role gives the flag today, and MEHO does not read it from
the `approver` claim of the token. Roles that customers define come
later. Today, put a person in `MEHO-Operators` or `MEHO-Admins` to let
them approve.

An approver cannot approve a request it filed itself, and it cannot
approve a `dangerous` or `destructive` change. The capability grants
only the queue — an approver does not gain the right to read the
*result* of a dispatch it did not request. See
[Keycloak realm setup](../install/keycloak-realm.md#approve-only-principal).

## Permanent deletes are gated hardest

A permanent delete — a VM, a DNS record, a firewall rule, a secret —
runs on the most restrictive tier, `destructive` (the safety order is
`safe < caution < dangerous < destructive`). A destructive operation
runs in a fixed order: it is previewed, a person approves it, the
approval is bound to that exact preview, the system states exactly what
will be removed, and only then does it run — with the whole sequence
written to the audit log. Concretely:

- **The approval is bound to the preview.** The preview returns a hash
  of the exact resolved request. That hash is presented on the call and
  re-checked at approve time, so the approval is tied to the preview a
  human actually saw. If the parameters changed between preview and
  approval, the approve is refused.
- **The run checks the object again.** Right before an approved delete
  runs, MEHO reads the object once more and compares it with the approval
  card: the same id or key, or, for an object that has none, the same
  fields. If someone deleted the object and created a new one under the
  same name while the request waited, the run is refused and nothing is
  deleted. The same happens when the card said there was nothing to delete
  and an object with that name is there now, and when MEHO cannot read the
  object right before the run. The requester sends a new request, and the
  approver sees the new object. If the object is simply gone, the run
  reports it as already absent.
- **A blast-radius statement is mandatory.** A destructive operation
  cannot park without a block naming the object, its enumerated child
  objects, and how irreversible the change is. The console renders it as
  a "what this destroys" card above the request detail. An operation
  that cannot produce that statement cannot be parked at all — it fails
  closed.
- **Only a MEHO-Admin approves their own delete.**
  The break-glass switch never applies to a `dangerous` or `destructive`
  operation. A MEHO-Admin may approve their own
  delete, on purpose, after
  checking the preview; there are no per-system exceptions. Everyone
  else's delete needs a MEHO-Admin to approve it.
- **The record shows who decided.** The decision names the approver, so
  the audit answers "who signed off on this delete?" without inference.
- **An agent or an add-on can propose a delete.** An agent's delete
  waits for a person, who checks the preview and approves it. An add-on
  that signs in as a service principal previews the delete and calls it
  with the preview hash. The call waits for a person to
  approve it; no standing grant can approve it in advance. After approval
  the delete runs under the approver, and the add-on reads the result with
  `GET /api/v1/approvals/{id}/result`. Only the caller that started the
  delete can read that result.

## Sandbox mode: one exception, for disposable test systems only

A MEHO instance can run in **sandbox mode**. It is only for an instance
that holds disposable test systems: systems built for one test and
deleted after it. On such an instance a tenant admin may give a service
principal a standing grant that also covers deletes, a **delete grant**.
So an automated test run can build, change and delete its own test
systems with no wait for a person. Every other instance keeps the rules
above; it shows `"sandbox_mode": "off"` on `/ready` and in
`meho status --json`. The normal `meho status` output shows a line only
when sandbox mode is on.

What stays the same on a sandbox:

- A person's delete still waits for approval under the group rules. An
  agent's delete still waits for a person. A satellite runner never gets
  a delete.
- A permanent delete under a delete grant still needs the preview hash
  of its own preview and a blast-radius statement. MEHO checks both
  before it records the grant as used, so a refused call leaves no
  "auto-approved" record. The record of a used grant holds the hash and
  the blast radius.

How delete grants work:

- Only a tenant admin creates one, in the web console. The REST API and
  the CLI refuse it, also with a person's token, unless the instance has
  the chart value `sandboxMode.deleteGrantsFromApi: true`. Then a tenant
  admin who signed in with `meho login` may create one through REST or
  `meho service-principals grants create`, with the same checks.
- Every delete grant expires within 30 days. MEHO never renews one by
  itself; the banner on every console page counts the delete grants that
  expire within 7 days. A tenant admin renews the delete grants of one
  service principal as a set, on the grant page: one new expiry for all
  of them, and each one is checked like a new grant.
- Delete grants are kept in their own table. An older MEHO build never
  reads it, so after a rollback every delete waits for a person again.

How the mode is set:

- At install time, with the chart values `sandboxMode.enabled: true` and
  `sandboxMode.confirmHost` equal to the instance's host name. The host
  name must have a part that is exactly `sandbox` (for example
  `meho.sandbox.example`). MEHO refuses to start otherwise.
- MEHO stores the mode in the database at the first start and refuses to
  start when the stored mode differs from the setting. To change the
  mode, install a new instance with a new database.
- Keep a sandbox on its own site: its own MEHO, Keycloak and Vault, only
  disposable systems as targets, and no network route to any other
  system. Those walls take the place of a person's approval.

## Proving which posture a deploy is running

Break-glass leaves a trail, so an auditor never has to trust a claim
about the values file.

**Is self-approval even enabled?** Read it off the health surface — no
cluster access to the chart needed:

```bash
curl -s https://<your-backplane>/ready | jq .features.approval_queue.effective_posture
# "four_eyes_enforced"            → the default; the emergency switch is off
# "single_operator_break_glass"   → APPROVAL_ALLOW_SELF_APPROVAL=true is set
```

The posture reports only the emergency switch. A MEHO-Admin may approve their own
change under either posture; that shows on the
decision row, not here.

`/ready` returns the `features` block only when you send the metrics
token. Add `-H "Authorization: Bearer <metrics-token>"` to the `curl`
call. A chart install makes the token on the first install; read it with
`kubectl get secret <release>-metrics-auth -o jsonpath='{.data.token}' | base64 -d`.

**Did a specific write self-approve?** Two signals:

- On the audit ledger, every approval is an `approval.decision` row with
  the field **`self_approved`** (`true` when the requester is also the
  approver) and the field **`approval_rule`**, which names the rule that
  allowed it: `sa_operator_self_approval` (a MEHO-Admin
  approved their own change), `break_glass_self_approval` (the emergency switch),
  `token_call_self_approval` (a person's own token `caution` change,
  approved with a web click), `sa_operator_or_admin` or `reviewer`. The
  block `approval_auth` says how the person proved they were present:
  `channel` (`web` or `rest`) and `method` (`fresh_sign_in` or
  `one_time_code`).
- A structured **`approval_self_approval_break_glass`** log event
  (WARNING) is emitted every time a break-glass self-approval goes
  through, carrying the `approval_request_id`, `op_id`, `operator_sub`,
  and `tenant_id`.

So a break-glass self-approval is visible three ways — the live posture
on `/ready`, a WARNING log at the moment it happens, and the
`break_glass_self_approval` decision row on the permanent ledger. A
self-approval by a MEHO-Admin is visible on its
decision row.

## Deciding on someone else's written instruction

Sometimes you approve or reject a request because another person told
you to, in writing. You still decide as yourself. MEHO records who told
you, their exact words, and where the work is tracked.

End your reason with three lines, after a blank line:

```bash
meho approvals approve 4b1c… --reason "Ran the cleanup in the change window.

Instructed-by: lead@example.com
Instruction: \"please run the cleanup tonight\"
Refs: #12, CHG-9"
```

- **Instructed-by:** the person who told you (their login or email).
- **Instruction:** their words, in quotes, exactly as they wrote them.
- **Refs:** the issue or change ticket, comma-separated.

!!! warning "Only on lab-class deployments"
    On production, MEHO refuses a decision made on someone else's
    instruction. You must decide on your own judgement. A lab-class
    deployment turns this on at install time with
    `config.approvalAllowDelegatedDecision: "true"` (env var
    `APPROVAL_ALLOW_DELEGATED_DECISION`). The default is `"false"`.
    No request can change it.

    When it is off, MEHO answers with a 403 error whose message starts
    with `delegated_decision_forbidden:`. The request stays pending.
    Remove the three lines and decide yourself, or ask the person who
    instructed you to decide. MEHO also writes a WARNING log event named
    `approval_delegated_decision_refused`.

    Check the stance of a deploy on `/ready`:

    ```bash
    curl -s https://<your-backplane>/ready | jq .features.approval_queue.delegated_decision_posture
    # "production_own_hand_only"      → the default; delegated decisions are refused
    # "lab_class_delegation_allowed"  → APPROVAL_ALLOW_DELEGATED_DECISION=true is set
    ```

The same lines work in the console reason box and in the REST
`reason` field.

If you write `Instructed-by:` but leave out `Instruction:` or `Refs:`,
MEHO refuses the decision with a 422 error and the request stays
pending. Add the missing line and try again. A reason can be at most
2000 characters.

**Where it shows up afterwards:**

- `meho approvals show <id>` prints `Instructed by`, `Instruction` and
  `Refs` under `Reviewed by`.
- The console shows the same three lines on the decided request.
- The audit log keeps your full reason. The decision row also has a
  `delegation` block with the three values, and the backplane writes a
  WARNING log event named `approval_delegated_decision`.

## Which one should I use?

| Situation | Use |
|---|---|
| Solo deploy, any change that waits | **MEHO-Admin** (Option 1) — you approve your own change on purpose, in the web interface |
| Solo deploy, a change of rights | **MEHO-Admin**: make it in the web console, where it runs at once, or approve your own waiting request there. See [Changes of rights](#changes-of-rights) |
| Genuine emergency, `safe` or `caution` change, and nobody in MEHO-Admins | **Break-glass** (Option 2, a deployment admin turns it on), then turn it back off |
| More than one operator | Another person approves; for a `dangerous` or `destructive` change, a MEHO-Admin; for a change of rights, an admin in the web console |

## Troubleshooting

**`approve` returns `self_approval_forbidden`.** It is your own change,
and you are not in `MEHO-Admins`. Ask a person in
one of the groups the text names to approve it on the Approvals page of
the web interface. If you are the only operator, see
[When you are the only operator](#when-you-are-the-only-operator).

**`approve` refuses with "You set up, changed or started the agent,
add-on or runner that asked for it".** You are one of the people behind
the login that asked (see
[A request of an agent counts as a request of the people behind it](#a-request-of-an-agent-counts-as-a-request-of-the-people-behind-it)),
so the change counts as your own change. Ask another person the text
names to approve it.

**`approve` refuses with "MEHO does not know who set up the login".** An
agent, add-on or runner asked, and MEHO knows nobody behind it. Ask an
Admin to approve it.

**`approve` returns `approval_group_required`.** The change is another
person's `dangerous` or `destructive` change, and you are not a
MEHO-Admin. Ask one to approve it.

**`approve` returns `rights_change_required`.** The request is a change
of rights. When the answer says "Approve this change of rights in the web
console.", you approved on the command line or over REST: open the request
on the Approvals page of the web console and approve it there. When the
answer says "Only an admin may approve it.", you are not an admin in this
tenant: ask an admin. No group, risk level or break-glass flag changes this
(see [Changes of rights](#changes-of-rights)).

**The Approve button is greyed out in the console.** The request window
says why: for example, it is your own change and the group rules do not
let you approve it. For your own change, the window also says who may
approve it and where to ask. Deny stays enabled. To take back your own
request, use `meho approvals withdraw <id>` instead.

**`approve` fails with `fresh_sign_in_required` or
`fresh_sign_in_already_used`.** The command line or REST call did not show
a fresh, unused sign-in. Run `meho approvals approve <id>` in a terminal
where a browser can open, and sign in when asked. If it still fails right
after you signed in, the realm is not set up for it yet: see
[Keycloak realm setup, Step 2](../install/keycloak-realm.md#step-2-create-the-public-meho-cli-client).
Or approve in the console.

**I set the flag but `/ready` still says `four_eyes_enforced`.** The
backplane reads `APPROVAL_ALLOW_SELF_APPROVAL` at startup. Confirm the
value reached the pod (`config.approvalAllowSelfApproval: "true"` in
your overlay renders the `APPROVAL_ALLOW_SELF_APPROVAL` env var) and
that the pod restarted onto the new ConfigMap.

!!! tip "When a guide and your deploy disagree"

    These guides are written against the product version this site
    version documents (see the version selector). If a command or error
    message differs on your deploy, check that your CLI and backplane
    versions match the docs version you are reading — then
    [file a docs issue](https://github.com/evoila/meho/issues): a guide
    a fresh user cannot execute verbatim is a bug.
