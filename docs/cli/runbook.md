<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# `meho runbook` — operator-facing CLI reference

The `meho runbook` verb tree wraps the runbook substrate ([Goal
#1195](https://github.com/evoila/meho/issues/1195)) on the operator
terminal. Eleven verbs across two surfaces: **template authoring**
(TENANT_ADMIN: draft / edit / publish / deprecate / list-templates /
show-template) and **run execution** (OPERATOR or TENANT_ADMIN:
start / next / abort / reassign / runs). Every verb is a thin HTTP
wrapper around `/api/v1/runbooks/templates*` (G12.2) or
`/api/v1/runbooks/runs*` (G12.3), with one or two CLI-specific UX
seams (interactive verify prompt on `next`, abort-reason prompt on
`abort`).

This doc covers **CLI usage**. It does not cover:

- The substrate architecture (entities, opacity contract, state
  machine, audit correlation): see
  [`docs/architecture/runbooks.md`](../architecture/runbooks.md).
- The agent-side authoring patterns (multi-session drafting,
  capture-as-you-go, fork-on-edit): see
  [`docs/runbooks/authoring.md`](../runbooks/authoring.md).
- The MCP session-priming surface and its role-floor: see
  [`docs/architecture/mcp.md`](../architecture/mcp.md#runbook-session-priming).
- The broader CLI map (auth, login, server-driven discovery, sibling
  verb trees): see [`docs/codebase/cli.md`](../codebase/cli.md).

---

## The verb taxonomy

Eleven verbs grouped by audience and lifecycle phase:

| Surface     | Verb                  | Role                   | Wraps                                                   |
|-------------|-----------------------|------------------------|---------------------------------------------------------|
| Authoring   | `draft-template`      | TENANT_ADMIN           | `POST /api/v1/runbooks/templates`                       |
| Authoring   | `edit-template`       | TENANT_ADMIN           | `PATCH /api/v1/runbooks/templates/{slug}`               |
| Authoring   | `publish-template`    | TENANT_ADMIN           | `POST /api/v1/runbooks/templates/{slug}/publish`        |
| Authoring   | `deprecate-template`  | TENANT_ADMIN           | `POST /api/v1/runbooks/templates/{slug}/deprecate`      |
| Discovery   | `list-templates`      | OPERATOR               | `GET /api/v1/runbooks/templates`                        |
| Discovery   | `show-template`       | TENANT_ADMIN (carve-out: OPERATOR post-completion) | `GET /api/v1/runbooks/templates/{slug}`                 |
| Discovery   | `runs`                | OPERATOR (own) / TENANT_ADMIN (all) | `GET /api/v1/runbooks/runs`                |
| Execution   | `start`               | OPERATOR               | `POST /api/v1/runbooks/runs`                            |
| Execution   | `next`                | OPERATOR (assignee)    | `POST /api/v1/runbooks/runs/{run_id}/next`              |
| Execution   | `abort`               | OPERATOR (assignee) or any TENANT_ADMIN | `POST /api/v1/runbooks/runs/{run_id}/abort` |
| Escalation  | `reassign`            | TENANT_ADMIN           | `POST /api/v1/runbooks/runs/{run_id}/reassign`          |

Tenant scoping is enforced server-side via the JWT — there is no
`--tenant` flag. The role on your token decides what each verb does
(operators see their own runs; admins see all tenant runs). Run
`meho login <backplane-url>` once per workstation; subsequent verbs
recover the bearer from the OS keyring (see
[`cli/README.md`](../../cli/README.md#login)).

Every verb accepts `--json` for machine-parseable output and
`--backplane <url>` to override the cached URL. Every verb returns
the structured exit codes documented per verb below — see
[`docs/codebase/cli.md`](../codebase/cli.md) for the cross-tree
conventions.

---

## Template verbs (TENANT_ADMIN)

The authoring surface — draft a procedure, edit it across sessions,
publish when the senior signs off, deprecate when it goes stale.
TENANT_ADMIN role required on every write; OPERATOR JWT lands as
HTTP 403.

### `draft-template`

```
meho runbook draft-template <slug> --from <file.yaml> [--json] [--backplane URL]
```

Create the first draft of a new runbook slug. The YAML at `--from`
is parsed, pre-flighted locally (slug grammar, step id grammar,
step / verify type allowlists, substitution allowlist — see [YAML
template body schema](#yaml-template-body-schema)), then POSTed.
The backend re-validates authoritatively, so any drift between the
CLI pre-flight and the backend's `schemas.py` surfaces as a 422.

If a draft already exists for the slug, the backend rejects with
HTTP 409 (`draft_already_exists`) — use `edit-template` to mutate
the existing draft.

**Example.**

```bash
meho runbook draft-template cert-rotation-vcenter --from cert-rotation.yaml
# Created draft cert-rotation-vcenter@1
# Status: draft
```

**Exit codes:** `0` draft created · `1` YAML parse / pre-flight
failure · `2` auth_expired · `3` unreachable · `4`
unexpected_response (incl. 409, 422) · `5` insufficient_role.

**REST route:** [`backend/src/meho_backplane/api/v1/runbook_templates.py`](../../backend/src/meho_backplane/api/v1/runbook_templates.py).

### `edit-template`

```
meho runbook edit-template <slug> --from <file.yaml> [--json] [--backplane URL]
```

Edit a template. Behaviour depends on what currently exists for the
slug — the backend decides, the CLI just surfaces the response:

- **Draft exists** → edit in place. No version bump.
- **Only published / deprecated versions exist** → fork-on-edit.
  Creates a new draft at `version = max + 1`. The response carries
  `forked_from` so you see how many in-flight runs are still pinned
  to the version you forked from. They stay pinned; new runs use
  the latest published version.

Drafts are mutable across sessions — call `edit-template` as often
as you like during the multi-session drafting pattern (see
[`docs/runbooks/authoring.md`](../runbooks/authoring.md)).

**Example (in-place draft edit).**

```bash
meho runbook edit-template cert-rotation-vcenter --from cert-rotation.yaml
# Edited cert-rotation-vcenter@1 (status=draft)
```

**Example (fork-on-edit).**

```bash
meho runbook edit-template cert-rotation-vcenter --from cert-rotation.yaml
# Edited cert-rotation-vcenter@2 (forked from cert-rotation-vcenter@1, 3 in-flight run(s) on previous version, status=draft)
```

**Exit codes:** `0` edit landed · `1` YAML parse / pre-flight
failure · `2` auth_expired · `3` unreachable · `4`
unexpected_response (incl. 404 `slug_not_found`, 422) · `5`
insufficient_role.

**REST route:** [`backend/src/meho_backplane/api/v1/runbook_templates.py`](../../backend/src/meho_backplane/api/v1/runbook_templates.py).

### `publish-template`

```
meho runbook publish-template <slug> --version N [--json] [--backplane URL]
```

Flip a draft to `status=published`. Pass `--version` with the value
the prior `draft-template` / `edit-template` call returned.
Idempotent: a second publish against an already-published
`(slug, version)` returns HTTP 200 with no state change.

After publish, the template becomes the latest start target for
`meho runbook start`. Previous published versions stay addressable
for in-flight runs (pinned at start time) and for `show-template`.

**Example.**

```bash
meho runbook publish-template cert-rotation-vcenter --version 1
# Published cert-rotation-vcenter@1 (status=published)
```

**Exit codes:** `0` published · `2` auth_expired · `3` unreachable
· `4` unexpected_response (incl. 404 `slug_not_found`, 400 on
attempting to publish a deprecated version) · `5`
insufficient_role.

**REST route:** [`backend/src/meho_backplane/api/v1/runbook_templates.py`](../../backend/src/meho_backplane/api/v1/runbook_templates.py).

### `deprecate-template`

```
meho runbook deprecate-template <slug> --version N [--json] [--backplane URL]
```

Mark a published version as deprecated. **In-flight runs keep
advancing** — they're pinned at start time and finish on the
version they started. New `meho runbook start` calls fall back to
the latest non-deprecated published version of the slug; if none
exists, `start` refuses.

**Example.**

```bash
meho runbook deprecate-template cert-rotation-vcenter --version 1
# Deprecated cert-rotation-vcenter@1 (status=deprecated)
```

**Exit codes:** `0` deprecated · `2` auth_expired · `3` unreachable
· `4` unexpected_response (incl. 404 `slug_not_found`, 400 on
attempting to deprecate a draft / already-deprecated version) · `5`
insufficient_role.

**REST route:** [`backend/src/meho_backplane/api/v1/runbook_templates.py`](../../backend/src/meho_backplane/api/v1/runbook_templates.py).

### `list-templates`

```
meho runbook list-templates [--status published|draft|deprecated] [--target-kind KIND] [--limit N] [--json] [--backplane URL]
```

OPERATOR-level discovery — what procedures exist in this tenant.
Returns a compact 5-column table (`SLUG`, `VERSION`, `STATUS`,
`TARGET_KIND`, `EDITED_AT`); does **not** carry step bodies. The
backend enforces the projection — operators cannot read step
content through `list-templates`.

`--limit` is server-capped at 500 (the default page size is 100).

**Example (default — latest of each slug).**

```bash
meho runbook list-templates
# SLUG                                     VERSION STATUS     TARGET_KIND          EDITED_AT
# cert-rotation-vcenter                    1       published  vmware-rest          2026-05-22T09:11:03Z
# vault-unseal                             3       published  vault-rest           2026-05-18T14:42:55Z
# host-onboarding-baremetal                2       draft      ssh                  2026-05-29T07:55:11Z
```

**Example (filter on lifecycle).**

```bash
meho runbook list-templates --status published --target-kind vmware-rest
```

**Exit codes:** `0` (incl. zero rows) · `2` auth_expired · `3`
unreachable · `4` unexpected_response · `5` insufficient_role.

**REST route:** [`backend/src/meho_backplane/api/v1/runbook_templates.py`](../../backend/src/meho_backplane/api/v1/runbook_templates.py).

### `show-template`

```
meho runbook show-template <slug> [--version N] [--json] [--backplane URL]
```

Read the full body of a template — title, description, ordered
steps with verify summary. `--version` pins; omitted means the
latest non-deprecated version.

**Role gate.** TENANT_ADMIN unconditionally. OPERATOR only with the
**post-completion carve-out** (G12.3-T4 #1309): once the operator
has a `completed` or `abandoned` run against `(slug, version)`,
they can read the template for post-mortem review. While a run is
in flight against the slug — even one the operator started — the
403 still holds (the `opacity_floor` error category). The right
path at run time is `meho runbook next <run_id>` step-by-step.

**Example.**

```bash
meho runbook show-template cert-rotation-vcenter --version 1
# Template: cert-rotation-vcenter@1
# Title:       vCenter 9.0 certificate rotation
# Status:      published
# Target kind: vmware-rest
# Created by:  alice@example.com (2026-05-20T11:02:14Z)
# Edited by:   alice@example.com (2026-05-22T09:11:03Z)
#
# Description:
#   Rotate the SSL certificate on a vCenter Server appliance. Runs the
#   Subject CN through the example CA and stages the rollout with a
#   single drain step before swapping the cert.
#
# Steps (5):
#   1. [manual] Pre-flight: confirm CA reachability (id: preflight-ca)
#       Open a browser to ${run.params.ca_url} and verify the CA console
#       responds.
#       verify: confirm — Did the CA console respond cleanly?
#   2. [operation_call] Request a new cert from the CA (id: request-cert, op_id: vmware-cert-request)
#       Issue a new cert with CN=${run.params.cn} via the CA.
#       verify: operation_call op_id=vmware-cert-status
#   3. ...
```

**Exit codes:** `0` rendered · `2` auth_expired · `3` unreachable
· `4` unexpected_response (incl. 404 `slug_not_found`) · `5`
insufficient_role (incl. `opacity_floor` while a run is in flight).

**REST route:** [`backend/src/meho_backplane/api/v1/runbook_templates.py`](../../backend/src/meho_backplane/api/v1/runbook_templates.py).

---

## Run verbs (OPERATOR / TENANT_ADMIN)

The execution surface — start a run, advance step-by-step, abort if
the procedure is broken, reassign if the wrong person is driving.

### `start`

```
meho runbook start <slug> --target <name> [--param k=v ...] [--json] [--backplane URL]
```

Begin a new run on the latest non-deprecated published version of
`<slug>`. The caller is auto-assigned as the run's `assigned_to`
server-side; only the assignee (or a senior who calls `reassign`)
can advance the run.

`--target` is required: the run subject (the host, cluster, cert
thumbprint…) substituted into the template body as `${run.target}`.
`--param k=v` sets a value for `${run.params.k}`; repeat for
multiple params. Every `${run.params.X}` the template references
must be satisfied at start — a missing key surfaces as HTTP 422.

The CLI renders **only step 1**: the run_id, the template
coordinates, the step body with substitutions applied, and the
verify gate. This is the opacity contract at the human surface —
see [The opacity contract from the CLI perspective](#the-opacity-contract-from-the-cli-perspective).

**Example.**

```bash
meho runbook start cert-rotation-vcenter \
  --target prod-vc01 \
  --param cn=vcenter.example.com \
  --param ca_url=https://ca.example.com/console

# Run ID:      6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118
# Template:    cert-rotation-vcenter@1
#
# Step 1/5: Pre-flight: confirm CA reachability  (id: preflight-ca)
# ─────────────────────────────────────────────
# Open a browser to https://ca.example.com/console and verify the CA
# console responds.
# ─────────────────────────────────────────────
# Step kind:   manual
# Verify type: confirm
#   Prompt: Did the CA console respond cleanly?
#   Next: `meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118 --verify-response yes|no|escalate`
```

**Exit codes:** `0` started + step 1 rendered · `2` auth_expired ·
`3` unreachable · `4` unexpected_response (incl. 400
`deprecated_template`, 404 `slug_not_found`, 422 `missing_params`) ·
`5` insufficient_role.

**REST route:** [`backend/src/meho_backplane/api/v1/runbook_runs.py`](../../backend/src/meho_backplane/api/v1/runbook_runs.py).

### `next`

```
meho runbook next <run_id> [--verify-response yes|no|escalate] [--json] [--backplane URL]
```

Advance an in-progress run by one step. The substrate is the verify
oracle: a step transitions to `verified` (and the run advances)
when the verify predicate matches. If not, the step transitions to
`failed` and the only forward path is `meho runbook abort`.

**Two verify shapes.**

- **`verify.type=confirm`** — the operator answers yes/no/escalate.
  Only `yes` advances.
  - Without `--verify-response`, the CLI calls the backend, sees
    HTTP 422 `VerifyResponseRequiredError`, prompts on stdin
    (`Answer [yes/no/escalate]:`), and re-issues with the answer.
    This is the interactive path operators take by default.
  - With `--verify-response yes|no|escalate`, the CLI sends the
    answer non-interactively. Scripted use only — operators
    running runbooks by hand should rely on the prompt.
- **`verify.type=operation_call`** — the substrate dispatches the
  verify call itself. The CLI does not prompt; pass / fail is
  decided by the dispatched call's result. No client action
  needed.

**Single-assignee.** Only the run's `assigned_to` can advance.
A TENANT_ADMIN who is not the assignee gets HTTP 403
`not_run_assignee` — the right path for a senior to take over is
`meho runbook reassign`, then `next`.

**Example (interactive confirm).**

```bash
meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118
# Verify required: this step has a confirm-typed verify.
# Answer [yes/no/escalate]: yes
#
# Step 2/5: Request a new cert from the CA  (id: request-cert)
# ─────────────────────────────────────────────
# Issue a new cert with CN=vcenter.example.com via the CA.
# ─────────────────────────────────────────────
# Step kind:   operation_call (op_id: vmware-cert-request)
# Verify type: operation_call
#   Will dispatch op_id: vmware-cert-status
#   Next: `meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118` (substrate dispatches the verify call)
```

**Example (scripted confirm).**

```bash
meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118 --verify-response yes
```

**Example (run completed).**

```bash
meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118
# Run complete. (run_id=6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118, state=completed)
# Completed at: 2026-05-29T11:48:33Z
```

**Exit codes:** `0` step advanced or run completed · `2`
auth_expired · `3` unreachable · `4` unexpected_response (incl. 400
`previous_step_failed` / `run_already_terminal`, 404
`run_not_found`, 422 `verify_response_mismatch`) · `5`
insufficient_role (403 `not_run_assignee`).

**REST route:** [`backend/src/meho_backplane/api/v1/runbook_runs.py`](../../backend/src/meho_backplane/api/v1/runbook_runs.py).

### `abort`

```
meho runbook abort <run_id> --reason "<text>" [--json] [--backplane URL]
```

Terminate an in-progress run. The reason is persisted to
`audit_log` for senior review (per Initiative #1198's
abort-with-audit guarantee).

Permitted callers: the run's `assigned_to`, **or** any TENANT_ADMIN
(the admin path is the senior cleaning up someone else's stuck
run). Operators who aren't the assignee and aren't admins get HTTP
403.

`--reason` is required. When omitted **and** stdin is a TTY, the
CLI prompts (`Aborting run <id>. Reason (recorded to audit_log):`).
When omitted **and** stdin is not a TTY, the CLI exits 1 — scripted
callers must supply `--reason` explicitly rather than block on
stdin.

**Example (TTY prompt).**

```bash
meho runbook abort 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118
# Aborting run 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118. Reason (recorded to audit_log): VPN to vCenter dropped, retrying tomorrow
# Aborted run 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118 (state=abandoned, abandoned_at=2026-05-29T11:51:02Z)
```

**Example (scripted).**

```bash
meho runbook abort 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118 \
  --reason "VPN dropped, will retry tomorrow"
```

**Exit codes:** `0` aborted · `1` `--reason` missing and stdin not
a TTY · `2` auth_expired · `3` unreachable · `4`
unexpected_response (incl. 400 `run_already_terminal`, 404
`run_not_found`) · `5` insufficient_role (403 `not_run_assignee`).

**REST route:** [`backend/src/meho_backplane/api/v1/runbook_runs.py`](../../backend/src/meho_backplane/api/v1/runbook_runs.py).

### `reassign`

```
meho runbook reassign <run_id> --to <operator-sub> [--json] [--backplane URL]
```

Transfer ownership of an in-progress run. TENANT_ADMIN only — the
route gate refuses operator callers.

After reassign, only the new assignee can call `meho runbook next`
on the run. The previous assignee gets 403 `not_run_assignee` on
their next call.

`--to` takes the subject identifier (`sub` claim) of the new
owner — the same subject the operator's JWT carries. Match it
against the value in `runs`' `ASSIGNED_TO` column.

**Use when.** A junior is stuck on a step the senior needs to
drive — VPN issue, an SSH key the junior doesn't have, an
escalation that needs the senior's eyes on the actual host. The
junior should `meho runbook abort` if the *procedure itself* is
broken; `reassign` is for "this operator needs to step in", not
"this procedure is broken".

**Example.**

```bash
meho runbook reassign 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118 --to senior-alice
# Reassigned run 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118 to senior-alice (reassigned_at=2026-05-29T11:53:18Z)
```

**Exit codes:** `0` reassigned · `2` auth_expired · `3`
unreachable · `4` unexpected_response (incl. 400
`run_already_terminal`, 404 `run_not_found`, 422
`empty_new_assignee`) · `5` insufficient_role.

**REST route:** [`backend/src/meho_backplane/api/v1/runbook_runs.py`](../../backend/src/meho_backplane/api/v1/runbook_runs.py).

### `runs`

```
meho runbook runs [--assignee <sub>] [--status in_progress|completed|abandoned] [--template-slug <slug>] [--limit N] [--json] [--backplane URL]
```

List runs in the operator's tenant. Role-based scoping is enforced
server-side:

- **OPERATOR** sees only their own runs. `--assignee` is ignored by
  the backend (role floor — the backend forces `assignee=self`).
- **TENANT_ADMIN** sees all tenant runs unless `--assignee`
  narrows.

The CLI sends what you pass and renders what the backend returns —
no client-side double-checking.

`RUN_ID` is truncated to 8 chars in the human table (usually enough
to disambiguate). Pipe `--json` through `jq` for the full UUIDs.
`STEP` shows `n/total` for in-progress runs, `-` for terminal runs
(opacity: terminal runs have no current step). `--limit` is
server-capped at 500.

**Example (operator: own runs).**

```bash
meho runbook runs
# RUN_ID    TEMPLATE_SLUG                  VERSION ASSIGNED_TO          STATE        STEP     STARTED_AT
# 6f8c8b27  cert-rotation-vcenter          1       junior-bob           in_progress  2/5      2026-05-29T11:42:18Z
# 1a2b3c4d  vault-unseal                   3       junior-bob           completed    -        2026-05-28T08:14:55Z
```

**Example (admin: in-flight runs across the tenant).**

```bash
meho runbook runs --status in_progress
# RUN_ID    TEMPLATE_SLUG                  VERSION ASSIGNED_TO          STATE        STEP     STARTED_AT
# 6f8c8b27  cert-rotation-vcenter          1       junior-bob           in_progress  2/5      2026-05-29T11:42:18Z
# 9e8d7c6b  host-onboarding-baremetal      2       junior-carol         in_progress  4/9      2026-05-29T10:05:01Z
```

**Exit codes:** `0` (incl. zero rows) · `2` auth_expired · `3`
unreachable · `4` unexpected_response · `5` insufficient_role.

**REST route:** [`backend/src/meho_backplane/api/v1/runbook_runs.py`](../../backend/src/meho_backplane/api/v1/runbook_runs.py).

---

## Worked session — execution

A junior operator (`junior-bob`) running a published cert-rotation
runbook against `prod-vc01`. The senior already published version 1
of `cert-rotation-vcenter` in the session below.

```bash
# 0. Find the runbook.
$ meho runbook list-templates --status published --target-kind vmware-rest
SLUG                                     VERSION STATUS     TARGET_KIND          EDITED_AT
cert-rotation-vcenter                    1       published  vmware-rest          2026-05-22T09:11:03Z

# 1. Start the run.
$ meho runbook start cert-rotation-vcenter \
    --target prod-vc01 \
    --param cn=vcenter.example.com \
    --param ca_url=https://ca.example.com/console
Run ID:      6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118
Template:    cert-rotation-vcenter@1

Step 1/5: Pre-flight: confirm CA reachability  (id: preflight-ca)
─────────────────────────────────────────────
Open a browser to https://ca.example.com/console and verify the CA
console responds.
─────────────────────────────────────────────
Step kind:   manual
Verify type: confirm
  Prompt: Did the CA console respond cleanly?
  Next: `meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118 --verify-response yes|no|escalate`

# 2. Step 1 done; advance with the confirm answer (interactive prompt).
$ meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118
Verify required: this step has a confirm-typed verify.
Answer [yes/no/escalate]: yes

Step 2/5: Request a new cert from the CA  (id: request-cert)
─────────────────────────────────────────────
Issue a new cert with CN=vcenter.example.com via the CA.
─────────────────────────────────────────────
Step kind:   operation_call (op_id: vmware-cert-request)
Verify type: operation_call
  Will dispatch op_id: vmware-cert-status
  Next: `meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118` (substrate dispatches the verify call)

# 3. Substrate dispatches the verify call; no client prompt.
$ meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118
Step 3/5: Stage the new cert on prod-vc01  (id: stage-cert)
─────────────────────────────────────────────
SSH to prod-vc01 and place the issued cert at
/etc/vmware-vpx/ssl/staging.crt. Do not swap yet.
─────────────────────────────────────────────
Step kind:   manual
Verify type: confirm
  Prompt: Cert staged at /etc/vmware-vpx/ssl/staging.crt?
  Next: `meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118 --verify-response yes|no|escalate`

# 4. Stage it, confirm.
$ meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118
Verify required: this step has a confirm-typed verify.
Answer [yes/no/escalate]: yes

Step 4/5: Drain prod-vc01 sessions  (id: drain-sessions)
─────────────────────────────────────────────
Run `vmware-cert-drain` against prod-vc01. The substrate will
verify the active-session count is zero before advancing.
─────────────────────────────────────────────
Step kind:   operation_call (op_id: vmware-cert-drain)
Verify type: operation_call
  Will dispatch op_id: vmware-cert-session-count

# 5. Substrate dispatches drain + verify session-count == 0.
$ meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118
Step 5/5: Swap and restart vCenter  (id: swap-restart)
─────────────────────────────────────────────
Atomic mv staging.crt -> active.crt, then restart vmware-vpxd.
─────────────────────────────────────────────
Step kind:   manual
Verify type: confirm
  Prompt: vCenter UI reachable on the new cert?

# 6. Confirm the final step.
$ meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118
Verify required: this step has a confirm-typed verify.
Answer [yes/no/escalate]: yes

Run complete. (run_id=6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118, state=completed)
Completed at: 2026-05-29T12:14:55Z

# 7. Post-mortem: read the template now that the run is done.
$ meho runbook show-template cert-rotation-vcenter --version 1
Template: cert-rotation-vcenter@1
...
```

What this transcript shows:

- One step at a time. Step 3's body was not visible while the
  junior was on step 1.
- The confirm prompt is interactive by default.
- `operation_call` verifies are silent at the CLI — the backend
  dispatches and the next call surfaces the next step.
- The post-completion `show-template` carve-out lets the junior
  read the full template *after* the run completes, for review.

---

## Worked session — authoring

A senior + Claude pair authoring a new cert-rotation runbook across
two sessions. The senior runs `meho runbook` themselves; the agent
drives the YAML edits.

```bash
# Session 1: rough out the procedure.
# Senior writes cert-rotation.yaml (see "YAML template body schema"
# for the shape) and Claude helps fill in op_ids / verify shapes.
$ meho runbook draft-template cert-rotation-vcenter --from cert-rotation.yaml
Created draft cert-rotation-vcenter@1
Status: draft

# ... senior + Claude refine the YAML body across the session ...
$ meho runbook edit-template cert-rotation-vcenter --from cert-rotation.yaml
Edited cert-rotation-vcenter@1 (status=draft)

# End of session. The draft is safe server-side; nothing to "save".

# Session 2 (next day). Re-read the current draft body.
$ meho runbook show-template cert-rotation-vcenter --version 1
Template: cert-rotation-vcenter@1
Title:       vCenter 9.0 certificate rotation
Status:      draft
... full body ...

# ... senior + Claude polish the draft further ...
$ meho runbook edit-template cert-rotation-vcenter --from cert-rotation.yaml
Edited cert-rotation-vcenter@1 (status=draft)

# Senior is happy. Publish.
$ meho runbook publish-template cert-rotation-vcenter --version 1
Published cert-rotation-vcenter@1 (status=published)
```

What this transcript shows:

- Editing a draft does not bump the version. `cert-rotation-vcenter@1`
  is the same row across the two sessions, mutated in place.
- The draft is mutable; published versions are not. Once
  `publish-template` ran, any subsequent `edit-template` against
  `cert-rotation-vcenter` forks a `v2` draft (see the
  fork-on-edit example under [`edit-template`](#edit-template)).
- The senior can do the publish step from a different workstation
  than the drafting session — server-side state.

Authoring patterns (multi-session capture, fork-on-edit, the senior
+ Claude + junior split, the YAML shape rationale) live in
[`docs/runbooks/authoring.md`](../runbooks/authoring.md). This doc
just shows how to drive them from the CLI.

---

## Worked session — escalation

A junior is stuck on step 4 of the cert-rotation runbook — the
substrate's `vmware-cert-drain` step failed because the operator's
VPN dropped mid-step. The junior asks the senior in chat; the
senior takes over.

```bash
# Junior's terminal. The substrate already marked step 4 as failed
# on the previous `next` call; junior can no longer advance.
$ meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118
Error: previous_step_failed: step 4 (drain-sessions) failed; abort and restart
exit code 4

# Two paths forward:
#   (a) The procedure itself is broken (rare) — junior aborts.
#   (b) The junior is blocked, the senior should drive (common) —
#       senior reassigns, takes over.

# Path (b): senior reassigns to themselves and continues.
# Senior's terminal (TENANT_ADMIN JWT):
$ meho runbook reassign 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118 --to senior-alice
Reassigned run 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118 to senior-alice (reassigned_at=2026-05-29T11:53:18Z)

# Senior tries to advance. The step is still in `failed` state — the
# substrate refuses; the only forward path from a failed step is abort.
$ meho runbook next 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118
Error: previous_step_failed: step 4 (drain-sessions) failed; abort and restart
exit code 4

# Senior aborts with a useful reason.
$ meho runbook abort 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118 \
    --reason "VPN dropped mid-drain; senior taking over with a fresh run"
Aborted run 6f8c8b27-2dd9-4d0e-8d6f-44e8a7d2a118 (state=abandoned, abandoned_at=2026-05-29T11:55:02Z)

# Start a fresh run, same target.
$ meho runbook start cert-rotation-vcenter --target prod-vc01 \
    --param cn=vcenter.example.com \
    --param ca_url=https://ca.example.com/console
Run ID:      ab12cd34-5678-90ef-1234-56789abcdef0
Template:    cert-rotation-vcenter@1

Step 1/5: Pre-flight: confirm CA reachability  (id: preflight-ca)
...
```

What this transcript shows:

- `reassign` is the only way to take over a stuck run — there is
  no admin bypass on `next`. Even after reassign, a `failed` step
  is still failed; the new assignee can't force it past.
- The exit from a `failed` step is always `abort` + start a new
  run. There is no skip, no force-advance, no set-state. This is
  by design (see [No skip, no force-advance](#no-skip-no-force-advance)).
- The abort reason is recorded to `audit_log` — the senior's
  reason text is the audit trail for "why did this run end
  abandoned" investigations later.

---

## The opacity contract from the CLI perspective

`meho runbook start` and `meho runbook next` render **only the
current step's body**. There is no flag — and no future flag — to
preview the next step, dump the full step list, or fetch step
bodies in bulk. The substrate guarantees this structurally (the
opacity contract, see
[`docs/architecture/runbooks.md`](../architecture/runbooks.md)
§"The opacity contract"); the CLI replicates the discipline at the
human surface.

What this means in practice:

- **No `--show-all-steps` flag.** None exists; none is planned.
  Adding one would defeat the substrate guarantee — even a
  read-only "for context" surface would let an operator (or an
  agent on their behalf) skip ahead.
- **`runs` carries no step bodies.** The `STEP` column is `n/total`
  position only — never a step title, never a body. To see the
  current step body of an in-progress run you own, call `next`.
- **`show-template` is the only verb that ever shows multiple step
  bodies in one response.** And its role gate is the floor — until
  the operator's run on that template terminates (or unless they
  hold TENANT_ADMIN), the substrate refuses.

The substrate enforces this redundantly at four layers — schema,
function signature, service, transport — so even a backend bug
that tried to leak future-step content into the response envelope
would be caught at the next layer. The CLI is the fifth layer: the
rendering helper reads only fields under the `current_step` key.
If you're tempted to script around this, you're working against the
adherence floor and should reconsider whether the runbook
abstraction is the right tool for what you're doing.

---

## No skip, no force-advance

There is no `meho runbook skip`. There is no `meho runbook
force-advance`. There is no `meho runbook set-state`. None will be
added.

The only forward path from a `failed` step is **`abort` + start a
new run**. The only forward path from an `in_progress` step is the
substrate's verify gate passing (the operator answers the confirm
prompt with `yes`, or the dispatched `operation_call` verify
matches `expect`).

Operators who feel they need a skip surface should:

1. Check that the template is right — if step 2's verify is too
   strict (a misspelled `expect` field, an `op_id` that doesn't
   match the target), the template needs an edit + republish, not
   a skip.
2. Use `abort` + restart if the procedure is genuinely broken.
3. Use `reassign` if a senior should drive — the senior can `abort`
   from a position of authority that the junior can't.

The "no skip" call is the same one #1177 makes for retrieval
weights — substrate trust comes from a small, predictable surface
that the operator can reason about. Adding a skip flag would
quietly invert that promise.

---

## YAML template body schema

The on-disk shape the `--from` flag of `draft-template` and
`edit-template` parses. The CLI runs a pre-flight pass on the
YAML before any HTTP call (slug grammar, step-id grammar,
discriminator allowlists, substitution allowlist); the backend
re-validates authoritatively at the wire.

```yaml
# Required: human-readable display name.
title: vCenter 9.0 certificate rotation

# Required: one-paragraph summary shown in list views.
description: |
  Rotate the SSL certificate on a vCenter Server appliance. Runs the
  Subject CN through the example CA and stages the rollout with a
  single drain step before swapping the cert.

# Optional: free-form classifier for what `target` refers to.
# Null when the procedure is target-agnostic.
target_kind: vmware-rest

# Required: ordered list of steps. At least one.
steps:
  # ---- A manual step. -----------------------------------------------
  - id: preflight-ca                          # required; [a-z][a-z0-9-]{0,63}
    title: Pre-flight - confirm CA reachability   # required
    type: manual                              # required: manual | operation_call
    body: |                                   # required; supports ${run.target} / ${run.params.X}
      Open a browser to ${run.params.ca_url} and verify the CA
      console responds.
    verify:                                   # required
      type: confirm                           # confirm | operation_call
      prompt: Did the CA console respond cleanly?

  # ---- An operation_call step with operation_call verify. -----------
  - id: request-cert
    title: Request a new cert from the CA
    type: operation_call                      # carries op_id + optional params
    op_id: vmware-cert-request                # required for type=operation_call
    params:                                   # optional; substitutions allowed
      cn: ${run.params.cn}
      ca_url: ${run.params.ca_url}
    body: |
      Issue a new cert with CN=${run.params.cn} via the CA.
    verify:
      type: operation_call
      op_id: vmware-cert-status               # required for verify type=operation_call
      params:
        request_id: ${run.params.request_id}
      expect:                                 # structural equality + presence
        status: issued
```

**Substitution allowlist.** Only two patterns are accepted anywhere
in a step body, op-call params, verify params, or verify expect:

- `${run.target}` — the run's subject (the `--target` flag of `start`).
- `${run.params.X}` — where `X` matches `[a-z_][a-z0-9_]*`. Nested
  paths like `${run.params.X.Y}` are rejected.

Any other `${...}` pattern lands as a pre-flight error before the
HTTP call. The backend re-walks at publish time and at every
`next` to defend against drift.

**Discriminator rules.**

- `type: manual` steps must **not** carry `op_id` or `params`.
- `type: operation_call` steps **must** carry `op_id`; `params` is
  optional.
- `verify.type: confirm` must carry `prompt`, must **not** carry
  `op_id` / `params` / `expect`.
- `verify.type: operation_call` must carry `op_id`; may carry
  `params` / `expect`; must **not** carry `prompt`.

For the substrate-level rationale (why two step shapes, why two
verify shapes, why no Jinja, why no JSONPath) see
[`docs/architecture/runbooks.md`](../architecture/runbooks.md).

---

## JSON output mode

Every verb accepts `--json`. With the flag, the CLI emits the raw
response envelope from the underlying REST route — pipe-able to
`jq` for scripted use.

```bash
meho runbook list-templates --json | jq '.templates[] | {slug, version, status}'
meho runbook runs --status in_progress --json | jq '.runs[].run_id'
meho runbook start cert-rotation-vcenter --target prod-vc01 --json | jq -r '.run_id'
```

Caveat from Initiative #1200's scope: **scripting runbook execution
somewhat defeats the purpose**. Runbooks exist because a human
operator with a verify gate at each step is more reliable than a
shell script that runs all the steps in sequence. The `--json` mode
is not blocked from scripting `start` / `next` — but if your
procedure is mechanical enough to wrap in a script, it's probably
mechanical enough to be one `operation_call` step instead of N
manual ones. Reach for `--json` to inspect, to feed dashboards, to
correlate with `audit_log` — not as a way to bypass the operator.

---

## Cross-references

- **Substrate architecture:**
  [`docs/architecture/runbooks.md`](../architecture/runbooks.md) —
  the three entities, the opacity contract's four layers, the
  verify state machine, the audit-log correlation columns. Goal
  [#1195](https://github.com/evoila/meho/issues/1195).
- **Authoring patterns:**
  [`docs/runbooks/authoring.md`](../runbooks/authoring.md) — the
  senior + Claude + junior split, multi-session drafting,
  fork-on-edit, agent-side capture-as-you-go. Initiative
  [#1197](https://github.com/evoila/meho/issues/1197).
- **MCP session priming:**
  [`docs/architecture/mcp.md`](../architecture/mcp.md#runbook-session-priming) —
  the `initialize.instructions` band that injects per-run guidance
  when an operator's MCP session opens. Initiative
  [#1199](https://github.com/evoila/meho/issues/1199).
- **CLI map:**
  [`docs/codebase/cli.md`](../codebase/cli.md) — the broader Go
  module map (auth, login, server-driven discovery, sibling verb
  trees, structured exit codes). The `meho runbook` tree is at
  `cli/internal/cmd/runbook/` per
  [`docs/codebase/cli.md`](../codebase/cli.md).
- **Backend REST routes:**
  [`backend/src/meho_backplane/api/v1/runbook_templates.py`](../../backend/src/meho_backplane/api/v1/runbook_templates.py)
  (template surface, G12.2-T3
  [#1297](https://github.com/evoila/meho/issues/1297)) and
  [`backend/src/meho_backplane/api/v1/runbook_runs.py`](../../backend/src/meho_backplane/api/v1/runbook_runs.py)
  (run surface, G12.3-T5
  [#1311](https://github.com/evoila/meho/issues/1311)) — the wire
  shapes every verb wraps.
- **CLI verb implementations:**
  [`cli/internal/cmd/runbook/`](../../cli/internal/cmd/runbook/) —
  one Go file per verb plus the chassis (`runbook.go`) and the
  YAML pre-flight (`yaml.go`). G12.5-T1
  [#1318](https://github.com/evoila/meho/issues/1318) (template
  verbs) and G12.5-T2
  [#1319](https://github.com/evoila/meho/issues/1319) (run verbs).
- **`meho runbook --help`** — every verb carries a `Long`
  description that surfaces its role gate, exit codes, and the
  load-bearing UX seams (interactive prompt on `next`, TTY check on
  `abort`, single-assignee on `next`, opacity on `start` / `next`).
