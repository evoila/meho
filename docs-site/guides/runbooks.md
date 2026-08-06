# Author and run runbooks

A single operation does one thing. A **runbook** is the third kind of
operation MEHO ships (alongside generic and typed connectors): a
**versioned, parameterised, multi-step procedure** that composes
operation calls and manual steps into one reviewable, repeatable whole —
"rotate the ingress certificate", "drain and patch a host", "bring up a
new tenant". A tenant_admin *authors* the template once; operators *run*
it, advancing one gated step at a time.

This guide covers both halves: authoring a template through its
draft → publish lifecycle, and executing a run start-to-finish.

!!! note "Prerequisites, roles, and maturity"

    - A running backplane, a connected client, and at least one
      registered target
      ([Register targets and secrets](targets-and-secrets.md)).
    - **Authoring** templates (draft / edit / publish / deprecate)
      needs **tenant_admin**. **Running** a runbook needs **operator**.
    - Runbooks are part of MEHO's write surfaces, which are **Beta** —
      see the
      [feature-maturity index](../reference/maturity.md#write_surfaces).

## What a runbook is (and is not)

A runbook template has a `title`, an optional `target_kind`, and an
**ordered list of steps**. Each step is one of two kinds, and each
carries a **`verify`** gate that must pass before the run advances:

- **`operation_call`** — dispatches an `op_id` with `params`, exactly
  like `call_operation`. This is the automated half.
- **`manual`** — instructions for a human to carry out something MEHO
  can't (rack a disk, get a sign-off). The operator does it, then
  reports the step done.

The runbook itself carries **no `safety_level`**. Its safety is the
safety of the operations it dispatches: when an `operation_call` step
runs a `requires_approval` op, that step **parks in the approval queue**
exactly as a direct call would — the gate lives in the dispatch layer,
not the runbook. A runbook is therefore not a way to bypass approvals;
it is a governed sequence of the same governed calls.

## The surface at a glance

| Action | MCP tool | CLI |
|---|---|---|
| **Author** (tenant_admin) | | |
| Create the first draft | `meho_runbook_draft_template` | `meho runbook draft-template <slug> --from <file>` |
| Edit a draft | `meho_runbook_edit_template` | `meho runbook edit-template <slug> --from <file>` |
| Publish a draft | `meho_runbook_publish_template` | `meho runbook publish-template <slug>` |
| Deprecate a version | `meho_runbook_deprecate_template` | `meho runbook deprecate-template <slug>` |
| **Discover** (operator) | | |
| List templates | `meho_runbook_list_templates` | `meho runbook list-templates` |
| Show one template | `meho_runbook_show_template` | `meho runbook show-template <slug>` |
| **Run** (operator) | | |
| Start a run | `meho_runbook_start` | `meho runbook start <slug> --target <name>` |
| Advance one step | `meho_runbook_next` | `meho runbook next <run_id>` |
| Abort a run | `meho_runbook_abort` | `meho runbook abort <run_id> --reason "…"` |
| List runs | `meho_runbook_list_runs` | `meho runbook runs` |

## Authoring a template

Write the body as a YAML (or JSON) file and draft it. A minimal,
two-step template:

```yaml
title: Rotate the ingress certificate
target_kind: k8s
steps:
  - type: operation_call
    title: Snapshot the current secret
    body: Capture the live cert so the run has a rollback point.
    op_id: k8s.secret.get
    params: {namespace: ingress, name: tls-cert}
    verify:
      type: confirm
      prompt: Confirm the snapshot was captured before proceeding.
  - type: manual
    title: Stage the renewed certificate
    body: Place the renewed PEM at the target's secret path.
    verify:
      type: operation_call
      op_id: vault.kv.versions
      params: {path: tenants/acme/ingress-cert}
      expect: {status: ok}
```

A `verify` is `type: confirm` (MEHO shows the `prompt` and the operator
confirms) or `type: operation_call` (MEHO dispatches an op and matches
the result against `expect`). Draft, then publish:

```bash
meho runbook draft-template ingress-cert-rotate --from ./ingress-cert-rotate.yaml
meho runbook publish-template ingress-cert-rotate
```

Templates are **versioned and immutable once published**: editing a
published template forks a new draft version; a run pins the exact
version it started against, so republishing never changes a run already
in flight. Retire an old version with
`meho runbook deprecate-template <slug>` — existing runs finish, new
runs pick the current published version.

## Running a template

Start a run against a concrete target; pass any parameters the steps
reference:

```bash
meho runbook start ingress-cert-rotate --target lab-rke2 \
  --param namespace=ingress --work-ref gh:evoila/meho#412
# → run_id: 7c2a…   step 1/2: Snapshot the current secret
```

The run reveals **only the current step** — never the full template,
never the steps ahead. This opacity is structural: `start`, `next`, and
`runs` all refuse to leak future step bodies. Work the step, then
advance:

```bash
meho runbook next 7c2a…
# → step 1 verified; step 2/2: Stage the renewed certificate
```

`next` runs the step's `verify` — the **substrate is the oracle**. A
`confirm` verify asks you to confirm; an `operation_call` verify
dispatches its op and checks the result against `expect`. The run only
advances when verify passes; a failing verify holds the run on the
current step. Over MCP the same call is
`meho_runbook_next {"run_id": "…", "last_verified": true}` — but
`last_verified` is informational only; the server re-verifies regardless.

When every step verifies, the run completes. To stop early:

```bash
meho runbook abort 7c2a… --reason "upstream CA delayed the renewal"
```

The `reason` is persisted to the audit ledger. List your runs and their
state (run-level only — never step contents) with `meho runbook runs`.

## What can go wrong here

| Symptom | What it means | Fix |
|---|---|---|
| `next` won't advance — the run stays on the current step | The step's `verify` did not pass (a `confirm` you declined, or an `operation_call` whose result didn't match `expect`). | Fix the underlying condition and call `next` again; the substrate re-verifies. |
| A step returns `awaiting_approval` instead of running | That `operation_call` step dispatched a `requires_approval` op — the runbook does not bypass the gate. | Clear it on the approvals surface ([Approvals and break-glass](approvals-and-break-glass.md)), then continue. |
| `show-template` / `list-runs` returns `-32603` / 500 with `template_body_validation_failed` | A stored template predates a schema tightening (e.g. an empty step body from before the non-empty-body rule) and fails read-time re-validation. | A tenant_admin re-edits the offending step; on-prem deploys apply the backfill migration named in the error. |
| `draft-template` rejected — "disallowed substitution in op_id" | A step or verify tried to parameterise the `op_id` itself. Params may be substituted; the operation identity may not. | Hard-code `op_id`; use `params` for the variable parts. |
| `403 insufficient_role` on `publish-template` / `draft-template` | Authoring needs **tenant_admin**; your session is operator. | Author under a tenant_admin session; operators run, they don't author. |
| A republished template didn't change a run already in progress | By design — a run pins its template version at `start`. | Abort and re-`start` to pick up the new version, or let the in-flight run finish. |

**Next:** [Scheduler](scheduler.md).
