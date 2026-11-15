# Broadcast: people read, agents publish (decision)

**Status:** decided in Initiative #3726; this record needs the project
owner's approval of its pull request
**Date:** 2026-11-08
**Task:** [#3762](https://github.com/evoila/meho/issues/3762)
**Initiative:** [#3726](https://github.com/evoila/meho/issues/3726) (intentional agent announcements and dependable fan-out)
**Supersedes:** the mixed feed of operations and announcements, and the
rule of the opt-in announce gate of #3133 that also stopped people

## The decision in short

- **People read Broadcast.** They read it under their normal tenant role:
  `meho_broadcast_recent`, `meho_broadcast_watch`, the REST routes, the
  CLI and the web console.
- **Only trusted agents publish.** A hosted agent run or a registered
  agent principal (#3736). Nothing in MEHO announces for anyone on its
  own; operations, approvals and checks publish no Broadcast event
  (#3758). The audit log is the record of operations.
- **Only a person skips the announce gate.** The old gate did not look
  at who called: in a tenant that turned it on, it stopped every caller.
  Now a person is never asked. A person cannot publish, so the gate
  could refuse a person's write for a claim the person can never make.
  Every other caller is asked: agents, hosted agent runs, service logins
  and runner logins. In a tenant with the gate on, their
  `caution`-or-higher writes need an active claim first. A service login
  and a runner login cannot make a claim, so in such a tenant those
  writes are refused. An AI tool that works with a person's own login
  counts as that person and is not asked. `announce_gate_enabled` turns
  the gate on or off for a tenant.
- **A claim is advice to readers.** Readers that ask "who is working on
  this target now" use only trusted claims. A claim is active while its
  newest step is not the `completion` and its finite TTL has not run
  out. A claim never locks a target.

## Why

Broadcast used to mix automatic operation events with what agents said
on purpose. The automatic events hid the messages people need, and some
features read the feed as if it were a record. #3758 moved the automatic
events off Broadcast. This record finishes that change for the parts
that still assumed the mixed feed:

- The announce gate (#3133) asked every caller for a claim before a
  caution-or-higher write, people included. Since agents alone publish,
  only agents can make a claim, so for a person the gate could only refuse, never pass.
  For an agent it does its job: announce first, then write. Service and
  runner logins stay asked, so the change frees only the callers it was
  meant for.
- The target advisory read the Valkey stream, which had operation events,
  and treated a claim without a TTL as active forever.
- The web console offered operation filters, an "Operations" switch and
  an operation drawer on a page that has no operations.

## What changed

- The announce gate skips only a person. A person is a caller that
  `is_human_principal` (`auth/operator.py`) names a person (a `user`
  login) and that is not inside a hosted agent run (`is_agent_caller`,
  `auth/agent_admission.py`). Every other caller is asked. The
  `announce_required` refusal, the `Tenant.announce_gate_enabled`
  switch and its key in the settings file stay as they were.
- The target advisory and the reflex claim lookup read trusted claims
  from the archive and judge each claim by its newest step
  (`broadcast/claim_state.py`). The newest step is the one committed
  last. `active_only` keeps only steps of claims that are still active.
- `/ui/broadcast` shows agent announcements only, says who can post, and
  links the audit log for operations.

## Not decided here

- The `op_class` and `actor_sub` read filters of the recent and watch
  reads stay in the contract for now; they match no announcement.
  Removing them, or matching them against announcement fields, is a
  separate contract change.
- The Broadcast detail overrides plane only serves audit payload fields
  now. Its clean-up is a separate task.
- Resource leases, conflict answers and new approval tools are out of
  scope (Initiative #3726).
