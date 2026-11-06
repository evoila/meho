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
  person.

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
    audited emergency switch for the whole deploy (Option 3 below). It
    covers only `safe` and `caution` changes and never reaches a
    `dangerous` or `destructive` operation. The endorsed way for one
    person to approve their own changes is the **MEHO-SA-Operators**
    group, not the switch.

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
must be at most 15 minutes old and must have asked for your password.
When it is older, the request shows **Sign in to approve** instead of
**Approve**: click it, sign in with your password (and your
authenticator code or passkey), then open the request again and approve.

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

## Who may approve

Your login group decides. MEHO reads it only from your signed login
token, never from a request field or a command-line flag. The groups
come from the [Keycloak realm setup](../install/keycloak-realm.md#step-5-create-the-five-groups).

| Your group | Your own change | Another person's `safe` or `caution` change (also one marked `requires_approval`) | Another person's `dangerous` or `destructive` change |
|---|---|---|---|
| `MEHO-SA-Operators` | yes, at every risk level | yes | yes |
| `MEHO-Admins` | only a `caution` change that waited because you sent it with a token, with a click in the web interface | yes | yes |
| `MEHO-Operators` | the same as `MEHO-Admins` | yes | no |
| `MEHO-Approvers` | (you cannot run changes) | yes | no |
| `MEHO-Readers` | (you cannot run changes) | no | no |

"SA" means self-approval. A MEHO-SA-Operator's change still waits like
any other. The same person then approves it, on purpose: with a click in
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
MEHO-SA-Operators group may approve their own changes. Ask another operator,
an admin or an approver to approve it.
(set APPROVAL_ALLOW_SELF_APPROVAL=true for audited single-operator break-glass)
```

That is a REST `403` (the command line prints it), and a disabled
**Approve** button in the console. For a `dangerous` or `destructive`
change the text asks for a MEHO-SA-Operator or a MEHO-Admin and does not
name the switch, because the switch never reaches these levels. Another
person's `dangerous` or `destructive` change, approved by someone who is
neither, gets `403 approval_group_required`.

To take back your *own* pending request, withdraw it:
`meho approvals withdraw <id> [--reason TEXT]` (or
`POST /api/v1/approvals/{id}/withdraw`). The request then shows
"withdrawn by the sender", nobody can approve it, and the operation does
not run. A person, an agent or a service account can each withdraw its
own request.

## When you are the only operator

On a team, a colleague approves your change. On a **one-operator**
deploy you are both the requester and the only person who could
approve. Pick one of the paths below.

### Option 1 — a MEHO-SA-Operator approves their own change (recommended)

Put yourself in the **`MEHO-SA-Operators`** group (see
[Keycloak realm setup](../install/keycloak-realm.md#who-may-approve-their-own-changes)).
The change applies at your next login. Then:

1. Call the operation. The change waits, as every change does.
2. Open the request in the web interface (`/ui/approvals`). Check the
   proposed effect and, for a delete, the "what this destroys" card.
3. Click **Approve**, then **Confirm**. The console then says that you
   approved your own change.

This works at every risk level, also for `dangerous` and `destructive`
changes. On the command line, `meho approvals approve <id>` does the
same after a fresh sign-in, and prints a line that says it was a
self-approval. The decision record shows `self_approved: true` and the
rule `sa_operator_self_approval`.

A person is in exactly one role group per tenant. So a person in
`MEHO-Admins` is not a MEHO-SA-Operator, and cannot approve their own
`dangerous` or `destructive` change. A deploy with a single admin needs
a MEHO-SA-Operator, or the admin's own `dangerous` and `destructive`
changes wait with nobody to approve them.

### Option 2 — the agent-requester pattern

The second way is to make sure the write is **not requested under
your own subject** in the first place. Park it under an **agent
principal** instead — a first-class non-human identity with its own
stable `sub`. Then, when you approve as yourself, requester and approver
are already different subjects, and you never touch the break-glass
flag. The full audit lineage stays intact: the request row records the
agent as requester, the decision row records you as approver. This fits
well for writes that run on a schedule.

An agent principal is a Keycloak `client_credentials` client that
carries `principal_kind=agent`. You create one, attach it to an agent
definition, and drive the write through a **scheduled** run of
that definition. Four steps:

```bash
# 1. Register the agent principal (mints its client_credentials client;
#    the output includes the client id you pass to --identity-ref below).
meho agent-principal register nightly-writer

# 2. Author an agent definition bound to that principal. Its toolset and
#    prompt are what perform the write when the definition runs.
meho agent create nightly-writer --identity-ref <client-id-from-step-1>

# 3. Schedule the definition. The autonomous run parks the write under the
#    agent's sub (principal_sub=<agent-sub>). Use --kind one_off with
#    --fire-at <ISO8601> instead for a single run rather than a cron.
meho scheduler create --kind cron --agent-definition nightly-writer \
  --cron-expr "0 2 * * *"

# 4. The write now waits under the agent. Approve as yourself — your
#    sub differs from the agent's, so it runs.
meho approvals list
meho approvals approve <id>
```

Every agent request above `safe` waits for a person. For a `dangerous`
or `destructive` write, the person who approves must be a
MEHO-SA-Operator or a MEHO-Admin.

!!! warning "It must be an *autonomous* run, not a *delegated* one"

    Only an **autonomous / scheduled** agent run parks the request under
    the agent's own subject. A run you launch *interactively* binds the
    agent as your delegate (RFC 8693 token exchange): you stay the
    *subject*, the agent is only the *actor*, and the waiting request
    still carries **your** `sub`. Then the rules for your own change
    apply. The scheduled trigger is what makes the agent the requester.

Which identity ends up on the request — and therefore whether you can
approve it — comes down to how the write was dispatched:

| How the write was dispatched | Requester `sub` | You can approve it? |
|---|---|---|
| You call it directly | your `sub` | Only as a MEHO-SA-Operator (or under the rules for your own change in [Who may approve](#who-may-approve)) |
| You launch an agent interactively; it makes the write | your `sub` (agent is only the actor) | The same as a direct call — you are still the subject |
| A **scheduled / autonomous** agent run makes the write | the **agent's** `sub` | **Yes**, under the rules for another person's change |

### Option 3 — the audited break-glass flag (emergencies)

When neither option above is possible — an incident, a first bring-up,
a write you must make in the next five minutes — a deployment admin can
enable **self-approval** for the whole deploy:

```yaml
# Helm values overlay
config:
  approvalAllowSelfApproval: "true"   # renders APPROVAL_ALLOW_SELF_APPROVAL=true
```

With the flag on, anyone may approve their own `safe` or `caution`
change. Understand exactly what you traded away:

- **It covers the whole deploy, not one write.** The flag re-opens the
  single-account *request-and-grant* hole for every **lower-risk**
  operation across the whole tenant, not just the one you are trying to
  clear. There is no scoping to a single write.
- **It never reaches a `dangerous` or `destructive` operation.**
  Break-glass self-approval applies only to the `safe` and `caution`
  risk levels. For `dangerous` and `destructive` changes only the group
  rules count: a MEHO-SA-Operator may approve their own, everyone else
  needs a MEHO-SA-Operator or a MEHO-Admin.
- **It is for emergencies, not the solo default.** Reach for Option 1
  first. Leaving this on lets every person approve their own lower-risk
  changes across the whole tenant.
- **It is still audited.** Self-approval is not silent (see below). A
  reject never needs the flag.

!!! danger "Break-glass is a lever an admin holds, not a per-write toggle"

    Prefer a MEHO-SA-Operator for anything you do more than once. Set
    `APPROVAL_ALLOW_SELF_APPROVAL=true` for a real emergency, make the
    write, and turn it back off.

## A segregated approver

A different need is **separation of duties** — someone who may approve
other people's changes but may never run an operation. MEHO expresses
that directly: a principal can carry an **`approver`** capability,
independent of its tenant role. An approver may list, inspect, approve,
and reject waiting requests, yet dispatch stays gated on the operator
role — so an approver provisioned with the `read_only` role plus
`approver=true` (the `MEHO-Approvers` group) may approve another
person's lower-risk change but is refused `call_operation` outright.
"Approve, but never operate" is a role, not a matter of discipline.

An approver cannot approve a request it filed itself, and it cannot
approve a `dangerous` or `destructive` change. The capability grants
only the queue — an approver does not gain the right to read the
*result* of a dispatch it did not request. Provision the `approver`
claim in your realm with the
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
- **Only a MEHO-SA-Operator approves their own delete.** The break-glass
  switch never applies to a `dangerous` or `destructive` operation. A
  MEHO-SA-Operator may approve their own delete, on purpose, after
  checking the preview; there are no per-system exceptions. Everyone
  else's delete needs a MEHO-SA-Operator or a MEHO-Admin to approve it.
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
above; it shows `"sandbox_mode": "off"` on `/ready` and in `meho status`.

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
# "four_eyes_enforced"            → the default; self-approval is OFF
# "single_operator_break_glass"   → APPROVAL_ALLOW_SELF_APPROVAL=true is set
```

`/ready` returns the `features` block only when you send the metrics
token. Add `-H "Authorization: Bearer <metrics-token>"` to the `curl`
call. A chart install makes the token on the first install; read it with
`kubectl get secret <release>-metrics-auth -o jsonpath='{.data.token}' | base64 -d`.

**Did a specific write self-approve?** Two signals:

- On the audit ledger, every approval is an `approval.decision` row with
  the field **`self_approved`** (`true` when the requester is also the
  approver) and the field **`approval_rule`**, which names the rule that
  allowed it: `sa_operator_self_approval` (a MEHO-SA-Operator approved
  their own change), `break_glass_self_approval` (the emergency switch),
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
MEHO-SA-Operator's self-approval is visible on its decision row.

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
| Solo deploy, any change that waits | **MEHO-SA-Operator** (Option 1) — you approve your own change on purpose, in the web interface |
| Solo deploy, writes that run on a schedule | **Agent-requester** (Option 2) |
| Genuine emergency, `safe` or `caution` change, and nobody in MEHO-SA-Operators | **Break-glass** (Option 3), then turn it back off |
| More than one operator | Another person approves; for a `dangerous` or `destructive` change, a MEHO-SA-Operator or a MEHO-Admin |

## Troubleshooting

**`approve` returns `self_approval_forbidden` and I am the only
operator.** The write waits under *your* subject — a direct call, or an
*interactive* agent run (where you stay the subject) — and you are not
in `MEHO-SA-Operators`. Ask your Keycloak admin to move you to
`MEHO-SA-Operators` (Option 1), sign in again, and approve in the web
interface. Or re-park the write under an agent-requester (Option 2).
Reserve `APPROVAL_ALLOW_SELF_APPROVAL` for genuine emergencies — it
re-opens the single-account hole for every `safe` and `caution`
operation.

**`approve` returns `approval_group_required`.** The change is another
person's `dangerous` or `destructive` change, and you are not a
MEHO-SA-Operator or a MEHO-Admin. Ask one of them to approve it.

**The Approve button is greyed out in the console.** The request window
says why: for example, it is your own change and the group rules do not
let you approve it. For your own change, the window also names ways
forward and links back here. Deny stays enabled. To take back your own
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
