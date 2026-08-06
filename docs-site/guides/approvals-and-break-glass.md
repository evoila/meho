# Approvals and break-glass

Some operations change infrastructure. MEHO does not let those run
silently: an operation the connector marks `requires_approval` — every
`dangerous` write, and any `caution` write a policy elevates — does not
execute when you call it. It **parks** in an approval queue and returns
`awaiting_approval`, and a second person has to approve it before it
runs. That is the four-eyes rule, and it is on by default.

This guide is about the case the rule does not obviously cover: **you
are the only operator.** A solo deploy still has to be able to make
gated writes, so MEHO ships two ways for one person to clear the queue
— one you should reach for every day, and one reserved for emergencies.
This page covers both, and how to tell an auditor which one your deploy
is running.

!!! note "Prerequisites"

    - A running backplane and a connected client —
      [the install trail](../install/index.md) and
      [Connect clients](../clients/index.md).
    - A registered target you can dispatch a write against —
      [Register targets and secrets](targets-and-secrets.md).
    - An **operator**-role (or `tenant_admin`) session. Approving is an
      operator action; `read_only` sessions cannot decide.

## What you see when a write parks

Call a gated operation — from the CLI, an agent, or the operator
console — and instead of a result you get a parked request:

```bash
meho operation call vault-1.x vault.kv.put --target rdc-vault --params '{...}'
# → status: awaiting_approval
#   approval_request_id: 4b1c…
```

The parked request is side-effect-free: nothing was written to the
target. It is now waiting in the queue, which you can list and inspect:

```bash
meho approvals list                 # pending requests
meho approvals show <id>            # the parked operation + its proposed effect
```

To act on it you **approve** or **reject** it — on any surface, and they
all share one backplane path (same policy, same audit row):

```bash
meho approvals approve <id>         # runs the parked write now
meho approvals reject <id>          # terminal; the requester must re-file
```

The operator console exposes the same queue at **`/ui/approvals`**: a
bell with the live pending count, a request-detail modal with the
proposed effect, and Approve / Deny buttons.

## The rule: requester and approver must differ

Approval keys on your **subject** — the stable `sub` claim on your
token, not your display name or your role. The guard is simple: the
identity that *requested* a parked write may not be the identity that
*approves* it. Try to approve your own request and every surface refuses
with the same error:

```text
self_approval_forbidden: operator '<your-sub>' may not approve approval_request
<id>: requester and approver must differ
(set APPROVAL_ALLOW_SELF_APPROVAL=true for audited single-operator break-glass)
```

That is a REST `403`, an MCP invalid-params error, and a disabled
**Approve** button in the console — all naming the same flag. Rejecting
your *own* request is always allowed: withdrawing a write you asked for
is not a privilege escalation.

On a team this is invisible: your colleague approves your write. On a
**one-operator** deploy it looks like a dead end — you are both the
requester and the only identity that could approve. It is not a dead
end. Pick one of the two paths below.

## Option 1 — the agent-requester pattern (recommended)

The everyday answer is to make sure the write is **not requested under
your own subject** in the first place. Park it under an **agent
principal** instead — a first-class non-human identity with its own
stable `sub`. Then, when you approve as yourself, requester and approver
are already different subjects, the gate clears, and you never touch the
break-glass flag. The full audit lineage stays intact: the request row
records the agent as requester, the decision row records you as
approver.

An agent principal is a Keycloak `client_credentials` client that
carries `principal_kind=agent`. You create one, attach it to an agent
definition, and drive the gated write through a **scheduled** run of
that definition. Four steps:

```bash
# 1. Register the agent principal (mints its client_credentials client;
#    the output includes the client id you pass to --identity-ref below).
meho agent-principal register nightly-writer

# 2. Author an agent definition bound to that principal. Its toolset and
#    prompt are what perform the gated write when the definition runs.
meho agent create nightly-writer --identity-ref <client-id-from-step-1>

# 3. Schedule the definition. The autonomous run parks the write under the
#    agent's sub (principal_sub=<agent-sub>). Use --kind one_off with
#    --fire-at <ISO8601> instead for a single run rather than a cron.
meho scheduler create --kind cron --agent-definition nightly-writer \
  --cron-expr "0 2 * * *"

# 4. The write is now parked under the agent. Approve as yourself — your
#    sub differs from the agent's, so the gate clears and it dispatches.
meho approvals list
meho approvals approve <id>
```

!!! warning "It must be an *autonomous* run, not a *delegated* one"

    Only an **autonomous / scheduled** agent run parks the request under
    the agent's own subject. A run you launch *interactively* binds the
    agent as your delegate (RFC 8693 token exchange): you stay the
    *subject*, the agent is only the *actor*, and the parked request
    still carries **your** `sub` — so you still cannot approve it.
    Routing a solo write through an interactive agent run does **not**
    break the deadlock. The scheduled trigger is what makes the agent
    the requester.

Which identity ends up on the request — and therefore whether you can
approve it — comes down to how the write was dispatched:

| How the write was dispatched | Requester `sub` | You can approve it? |
|---|---|---|
| You call it directly (human operator) | your `sub` | **No** — requester == approver |
| You launch an agent interactively; it makes the write | your `sub` (agent is only the actor) | **No** — you are still the subject |
| A **scheduled / autonomous** agent run makes the write | the **agent's** `sub` | **Yes** — distinct subject, no flag needed |

This is the endorsed posture for a single-operator tenant: it keeps the
four-eyes invariant genuinely intact (two distinct subjects really do
touch every write) instead of switching it off.

## Option 2 — the audited break-glass flag (emergencies)

When you genuinely cannot stage an agent-requester — an incident, a
first bring-up, a write you must make in the next five minutes — a
deployment admin can enable **self-approval** posture-wide:

```yaml
# Helm values overlay
config:
  approvalAllowSelfApproval: "true"   # renders APPROVAL_ALLOW_SELF_APPROVAL=true
```

With the flag on, the requester == approver guard is lifted and you can
approve your own parked writes. Understand exactly what you traded away:

- **It is posture-wide, not per-write.** The flag re-opens the
  single-account *request-and-grant* hole for **every** operation, not
  just the one you are trying to clear. There is no scoping.
- **It is emergency-grade, not the solo default.** Reach for Option 1
  first. Leaving this on turns four-eyes into no-eyes for the whole
  tenant.
- **It is still audited.** Self-approval is not silent (see below). A
  reject never needs the flag.

!!! danger "Break-glass is a lever an admin holds, not a per-write toggle"

    Prefer the agent-requester pattern for anything you do more than
    once. Set `APPROVAL_ALLOW_SELF_APPROVAL=true` for a real emergency,
    make the write, and turn it back off.

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

**Did a specific write self-approve?** Two signals:

- A structured **`approval_self_approval_break_glass`** log event
  (WARNING) is emitted every time a self-approval goes through, carrying
  the `approval_request_id`, `op_id`, `operator_sub`, and `tenant_id`.
- On the audit ledger, a self-approval is the `approval.decision` row
  where the **requester equals the approver** — `principal_sub ==
  reviewed_by`. There is no separate `self_approved` field to filter on;
  the equality *is* the marker.

So a break-glass self-approval is visible three ways — the live posture
on `/ready`, a WARNING log at the moment it happens, and the
requester-equals-approver decision row on the permanent ledger.

## Which one should I use?

| Situation | Use |
|---|---|
| Solo deploy, recurring or planned gated writes | **Agent-requester** (Option 1) — no flag, four-eyes stays real |
| Solo deploy, one-off write you can schedule | **Agent-requester** (Option 1) |
| Genuine emergency, no time to stage an agent | **Break-glass** (Option 2), then turn it back off |
| More than one operator | Neither — a second operator approves the first's request |

## Troubleshooting

**`approve` returns `self_approval_forbidden` and I am the only
operator.** The write was parked under *your* subject — a direct human
call, or an *interactive* agent run (where you stay the subject).
Re-park it under an agent-requester: wire it as a **scheduled** trigger
under an agent definition (Option 1) so the autonomous run parks it with
the agent's `sub`, then approve as yourself. Reserve
`APPROVAL_ALLOW_SELF_APPROVAL` for genuine emergencies — it re-opens the
single-account hole for every operation.

**The Approve button is greyed out in the console.** Same cause: you are
the requester. The modal names both options inline and links back here.
Deny stays enabled — you can always withdraw your own request.

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
