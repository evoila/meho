<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# GitHub connector first-day on-ramp — operator runbook for `gh-rest`

> Cross-repo handshake between `evoila/meho` (this repo, producer of
> the `gh-rest` typed connector under
> [Initiative #1220](https://github.com/evoila/meho/issues/1220)) and
> the operator's GitHub organization (consumer side; not a single
> repo — every MEHO deployment talks to its own GitHub org / set of
> repos).
>
> This page is the **end-to-end usage runbook**: a single concrete
> sequence an operator follows to go from "I have a GitHub repo and
> a backplane" to "an agent dispatched `gh.pr.get` against a real
> PR, with approval-gated writes and a chain-of-custody audit
> trail." Credential setup is delegated to the sibling
> [`github-app-credential.md`](./github-app-credential.md); this
> doc starts the moment that recipe completes.

## Why this doc exists

The G3.11 Initiative ([#1220](https://github.com/evoila/meho/issues/1220))
ships five surfaces that together make the `gh-rest` connector usable
end to end:

- **T1** ([#1221](https://github.com/evoila/meho/issues/1221)) —
  `GitHubRestConnector` substrate (registered, fingerprintable).
- **T2** ([#1222](https://github.com/evoila/meho/issues/1222)) —
  [`github-app-credential.md`](./github-app-credential.md), the
  credential recipe.
- **T3** ([#1223](https://github.com/evoila/meho/issues/1223)) —
  `gh/3` catalog entry (post-T8 #1242 canonical form; the upstream
  API label "v3" lives in the catalog row's ``notes`` and in the
  fingerprint payload); ~700 ops grouped into ~40 tags ingest
  through the G0.7 generic pipeline.
- **T4** ([#1224](https://github.com/evoila/meho/issues/1224)) —
  first L1 composite `gh.composite.pr_status_summary`.
- **T5** ([#1225](https://github.com/evoila/meho/issues/1225)) —
  `requires_approval=true` annotations on four high-blast-radius
  write ops (`gh.issue.create`, `gh.pr.merge`,
  `gh.workflow_run.dispatch`, `gh.release.create`).

Without this runbook an operator lands on those five surfaces
piecemeal and has to reconstruct the integration story from scratch.
The consumer feedback on
[`docs/cross-repo/keycloak-web-client.md`](./keycloak-web-client.md)
is the quality bar: *"unusually complete + readable... high enough
that we wrote a Bash script from it on the first pass without
iteration."* This page targets the same bar for the GitHub surface.

The backplane cannot enforce GitHub-side configuration; it can only
fail closed when the target is misconfigured, the catalog isn't
ingested, the groups aren't enabled, the write op isn't annotated,
or the operator hasn't approved a queued call. This doc is the
contract that specifies the operator-side steps that turn the
substrate into a working agent path.

## What this connector covers

**In scope (v0.x):**

- `github.com` REST API surface (the **v3** API). The connector is
  registered against the registry triple
  `(product="gh", version="3", impl_id="gh-rest")`; the operator-
  visible connector class is `gh-rest-3` (the digit-prefix slot
  the dispatcher's connector-id parser requires). GitHub's own
  "v3" upstream label lives in the connector's `FingerprintResult.
  version` and in the catalog row's `notes`; the catalog
  `version` field itself stores the digit-prefix `"3"` (G3.11-T8
  #1242 reconciled the catalog with the registry so the
  `(product, version, impl_id)` tuple-lookup resolves cleanly).
  Operators ingest with `--catalog gh/3`. See
  [§ Connector identifier conventions](#connector-identifier-conventions)
  below for the full distinction.
- Read ops — repo metadata, PR / issue / workflow-run reads,
  commit / check-run reads. ~700 ops ingest under T3's catalog
  entry once the parser-limitation follow-up below lands; see
  [§ Step 4 — Ingest the catalog](#step-4--ingest-the-catalog).
- Write ops — `gh.issue.create`, `gh.pr.merge`,
  `gh.workflow_run.dispatch`, `gh.release.create`. Each carries
  `requires_approval=true` (T5) so a queued approval gates every
  dispatch.
- Composite operations — `gh.composite.pr_status_summary` (T4) is
  the first L1 composite; the operator-facing on-ramp uses it as
  the smoke / verify call.
- Audit chain — every dispatch through the connector lands an
  `audit_log` row keyed by the operator's Keycloak `sub`, the
  target, the op_id, and the result status (G8 audit-replay tree).
  See [§ Audit log story](#audit-log-story).
- Two credential paths — GitHub **App installation tokens**
  (preferred, machine-identity) and **fine-grained PAT** (fallback).
  See [§ App-vs-PAT credential picker](#app-vs-pat-credential-picker)
  for the decision rubric; the credential setup itself lives in
  [`github-app-credential.md`](./github-app-credential.md).

**Out of scope:**

- **GitHub Enterprise Server (GHES).** `github.com` only for v0.x.
  GHES adds a per-target `api_base_url` override + a different OAuth
  shape; deferred per Initiative #1220 scope.
- **OAuth user-flow auth.** The connector authenticates as the App
  identity (or the PAT owner), not as the operator's interactive
  GitHub session. Operators who want personal-account access use
  the `gh` CLI on their workstation.
- **Webhooks / push-to-meho.** The connector is pull-based.
  Real-time push (webhook fan-out into meho events) is separate
  G2.x infrastructure.
- **Per-org permission policy templates.** Operator-specific; the
  [permission scope picker](./github-app-credential.md#permission-scope-picker)
  in T2 gives a starting set, but the per-deploy tightening is
  outside this runbook's scope.
- **Classic PATs.** Only **fine-grained** PATs are supported on the
  fallback path — classic PATs lack per-repo scoping and are
  refused by [`github-app-credential.md`](./github-app-credential.md)
  § PAT fallback.

## Prerequisites

This runbook assumes:

- **Credential side complete.** A GitHub App (or fine-grained PAT)
  exists, the credential is in Vault at
  `secret/<tenant>/<target>/github-app`, and Step 3 of
  [`github-app-credential.md`](./github-app-credential.md) installed
  the App on the target repo(s). Run T2's [Check 2 — Probe the
  target](./github-app-credential.md#check-2--probe-the-target)
  before continuing; this runbook picks up at the very next step.
- **Backplane reachable.** `meho login <backplane-url>` has written
  a session token the CLI reuses. The verbs below need
  `tenant_admin` (`targets import`, `connector ingest`,
  `connector edit-op`, `connector enable`) or `operator`
  (`operation call`, `audit query`, `approvals approve`).
- **`gh-rest` connector class registered.** Initiative #1220's T1
  (#1221) has merged into the backplane image you're deploying.
  Verify with `meho connector list | grep gh-rest` — a row keyed
  on `gh-rest-3` is what you're looking for (or no row at all if
  the catalog hasn't been ingested yet, which is the common
  first-day state; the class is registered in memory regardless).

## First-day on-ramp recipe

Single sequence from a fresh backplane to a green dispatch of
`gh.composite.pr_status_summary` against a real PR. The target name
`github-meho` is the canonical example throughout this doc; substitute
your own deployment's target name as needed.

### Step 1 — Confirm credential setup is done

Re-run T2's verification gates from
[`github-app-credential.md`](./github-app-credential.md#verification):

```bash
# Check 1: connector + (eventual) target registered.
meho connector list | grep gh-rest
# Expected: a row with `connector_id=gh-rest-3` once any ingest has
# landed in this tenant. Pre-ingest the connector class is registered
# in memory but no DB row exists -- skip this expectation until
# Step 4 completes.

# Check 2: probe the target (proves credential chain).
meho targets probe github-meho
# Expected: `reachable=true` and an `extras.app_slug` showing the
# GitHub App name (App path) or `extras.user.login` (PAT path).
```

If Check 2 fails with `github_app_not_installed`,
`github_jwt_mint_failed`, or
`github_installation_token_mint_failed`, return to
[`github-app-credential.md` § Failure modes](./github-app-credential.md#failure-modes)
and re-walk the corresponding step before continuing.

### Step 2 — Register the target row

The target row that
[`github-app-credential.md` § Step 5](./github-app-credential.md#step-5--register-the-gh-v3-target-with-meho)
prepared in `targets.yaml` lands via `meho targets import`. There is
no `meho targets create` verb — the canonical (and only) target-
provisioning path is the YAML file + `import`:

```bash
meho targets import targets.yaml
```

Re-running `import` is idempotent: rows whose `name` already exists
update in place rather than re-creating. The output names every row
that was created vs updated.

Verify the row landed:

```bash
meho targets describe github-meho
```

Expected output (abbreviated):

```
name:        github-meho
product:     gh
host:        api.github.com
secret_ref:  secret/<tenant>/github-meho/github-app
auth_model:  shared_service_account
```

The `auth_model: shared_service_account` value is the
`AuthModel` enum slot from
[`backend/src/meho_backplane/connectors/schemas.py`](../../backend/src/meho_backplane/connectors/schemas.py)
— the App is the single machine identity shared across every
operator that dispatches against this target. The **App-vs-PAT
discriminator** is **not** on the target row itself; it lives
**inside the Vault secret payload** (presence of `app_id +
private_key` selects the App path; presence of `pat` selects the
PAT path). See
[§ App-vs-PAT credential picker](#app-vs-pat-credential-picker)
for the picker rubric and
[`github-app-credential.md` § Step 5](./github-app-credential.md#step-5--register-the-gh-v3-target-with-meho)
for the YAML notes.

### Step 3 — Probe the target

The probe round-trips the credential chain end to end and returns
the installation metadata you'll cite when filing a forensic ticket
against the App:

```bash
meho targets probe github-meho
```

Expected (abbreviated):

```json
{
  "vendor": "GitHub",
  "product": "gh",
  "version": "v3",
  "reachable": true,
  "probed_at": "2026-05-27T14:30:00Z",
  "probe_method": "GET /app/installations (App) or GET /user (PAT)",
  "extras": {
    "app_slug": "meho-prod",
    "installation_count": 1,
    "installation_account": "evoila",
    "target_type": "Organization",
    "permissions": {
      "contents": "read",
      "issues": "read",
      "metadata": "read",
      "pull_requests": "read"
    }
  }
}
```

Note the `"version": "v3"` here is the **upstream** API label
emitted by the connector's `fingerprint()` (it matches github.com's
own documentation). The **registry** stores this connector under
`version="3"` (digit-prefix) and the operator-visible connector_id
is `gh-rest-3`. The two labels intentionally differ; see
[§ Connector identifier conventions](#connector-identifier-conventions).

### Step 4 — Ingest the catalog

Ingest the `gh/3` catalog entry to populate the `endpoint_descriptor`
table with the ~700 GitHub REST ops and run the LLM-summarised
grouping pass:

```bash
meho connector ingest --catalog gh/3 --json
```

The `--catalog gh/3` shape (G0.14-T9 #1150) resolves the curated
entry from
[`backend/src/meho_backplane/operations/ingest/catalog.yaml`](../../backend/src/meho_backplane/operations/ingest/catalog.yaml)
server-side and dispatches the multi-spec ingest pipeline against
the GitHub REST API description repository.

Expected output on first ingest (abbreviated):

```
ingest gh/3/gh-rest — connector_id=gh-rest-3
  operations: 784 total (784 inserted / 0 updated / 0 skipped)
  connector_registered: true
  operations_grouped: true
  grouping: 49 groups, 760 ops assigned, 24 unassigned (16 LLM call(s), 38000ms)

Connector is in review_status=staged. Next:
  meho connector review gh-rest-3
  meho connector enable gh-rest-3 --confirm
```

> **Known limitation — live ingest blocked pre-parser-extension.**
> As of T3's merge, the G0.7 OpenAPI parser only inlines
> `#/components/schemas/*` and `#/components/parameters/*` refs.
> The GitHub REST spec uses `#/components/responses/*` refs on
> every operation, so the live ingest returns
> `UnsupportedSpecError` (rendered as a structured error
> envelope). The fix is a sibling parser-scope follow-up that
> extends
> [`backend/src/meho_backplane/operations/ingest/refs.py`](../../backend/src/meho_backplane/operations/ingest/refs.py)
> to inline the `responses` bucket; the catalog test
> [`backend/tests/integration/test_operations_ingest_github.py`](../../backend/tests/integration/test_operations_ingest_github.py)
> is currently `xfail-strict` on the limitation and flips to
> `xpass` the moment the parser learns the extra ref shape.
> **Until that follow-up lands, Step 4 returns the parser error
> and Steps 5–7 are gated on it.** The `gh.composite.*` paths
> registered in T4 (#1224) come via the typed-op registrar (the
> `register_operations()` classmethod) and **are** dispatchable
> independently of the ingest path; if you only need the composite
> use case, jump to [§ Step 7 — Smoke-test with the composite](#step-7--smoke-test-with-the-composite).

The catalog-resolved connector_id is `gh-rest-3` (from
`<impl_id>-<version>` with the catalog's `version: "3"` — G3.11-T8
#1242 canonicalised the catalog ``version`` field to match the
registry's digit-prefix slot, so the dispatcher's
``(product, version, impl_id)`` tuple-lookup against ingested rows
resolves the registered :class:`GitHubRestConnector` cleanly).
Operators see `gh-rest-3` in connector-list rows post-ingest; the
typed-op composite paths reference `gh.composite.*` directly
without a version slug.

### Step 5 — Review the LLM-summarised groups

Once Step 4 lands rows in the staged state, audit the grouping
output before enabling anything:

```bash
meho connector review gh-rest-3
```

The review payload renders ~49 groups with `name`, `when_to_use`,
per-group operation count, and per-op flags (`safety_level`,
`requires_approval`, `is_enabled`). Inspect each group's
`when_to_use` carefully — this is the verbatim text the agent
reads before deciding which group to search within. A
`when_to_use` like "Use these operations for any PR workflow:
list, inspect, review, merge, comment, request changes" is
actionable; "Operations related to pull requests" is not.

Polish weak group hints with `meho connector edit-group`:

```bash
meho connector edit-group gh-rest-3 pulls \
  --when-to-use "Use these operations for any pull-request workflow: list open / closed PRs, inspect status / checks / reviews, request review, request changes, merge, or otherwise drive PR state."
```

Most operators polish 2–4 groups per ingest; the LLM's output is
usable for the rest.

### Step 6 — Annotate write ops for approval

T5 ([#1225](https://github.com/evoila/meho/issues/1225)) ships
`requires_approval=true` annotations on four write ops in the
shipped catalog data, but if the operator's tenant ingested a
fork that carries different write-op coverage (or an older
snapshot pre-T5), the annotations need re-asserting. Use
`meho connector edit-op` — there is **no** `meho operation
annotate` verb; the canonical write-op flag toggle is
`connector edit-op`:

```bash
meho connector edit-op gh-rest-3 'POST:/repos/{owner}/{repo}/issues' \
  --safety dangerous --requires-approval
meho connector edit-op gh-rest-3 'PUT:/repos/{owner}/{repo}/pulls/{pull_number}/merge' \
  --safety dangerous --requires-approval
meho connector edit-op gh-rest-3 'POST:/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches' \
  --safety dangerous --requires-approval
meho connector edit-op gh-rest-3 'POST:/repos/{owner}/{repo}/releases' \
  --safety dangerous --requires-approval
```

The `op_id` form is `<METHOD>:<path>` exactly as it appears in the
spec — the colon and slashes are part of the natural key. Quote
the `op_id` in shells that would otherwise interpret the braces.
See
[`connector-ingestion.md` § "`op_id` carries `:` and `/`"](./connector-ingestion.md#op_id-carries--and-)
for the convention.

Each edit writes a `meho.connector.edit_op` audit row in the same
transaction as the column update, so audit replay can reconstruct
exactly which operator annotated which op at which time.

Enable the connector now that the per-op annotations are pinned:

```bash
meho connector enable gh-rest-3 --confirm
```

`enable` cascades every group to `review_status='enabled'` and every
op to `is_enabled=true`. After this step the agent meta-tools start
surfacing the connector and operations become dispatchable through
`call_operation`. The `--confirm` flag skips the stdin prompt;
without it the CLI asks for `y/yes`.

### Step 7 — Smoke-test with the composite

The canonical first dispatch is `gh.composite.pr_status_summary`
against a known PR. The composite is registered as a typed op
(T4 #1224) so it works independently of the Step 4 ingest, which
makes it the right smoke-test target while the parser limitation
keeps the broader catalog gated.

```bash
meho operation call gh-rest 'gh.composite.pr_status_summary' \
  --target github-meho \
  --params '{"owner":"evoila","repo":"meho","pull_number":1193}' \
  --json
```

Expected (abbreviated):

```json
{
  "pull_request": {
    "number": 1193,
    "state": "closed",
    "title": "docs(changelog): roll [Unreleased] → [0.7.0] - 2026-05-27",
    "mergeable_state": "clean",
    "merged_at": "2026-05-27T..."
  },
  "checks": {
    "total": 12,
    "passed": 12,
    "failed": 0
  },
  "reviews": [
    {"user": {"login": "..."}, "state": "APPROVED"}
  ]
}
```

A green Step 7 confirms: the credential chain works end to end,
the typed-op classpath is wired, dispatch lands an audit row,
and the agent path will resolve `gh.composite.*` ops once the
ingest path lands the read-op companions.

To verify the **write-op approval flow**, attempt a write op
without an approval first; you should see the call enter the
queue:

```bash
meho operation call gh-rest 'POST:/repos/{owner}/{repo}/issues' \
  --target github-meho \
  --params '{"owner":"evoila","repo":"meho","title":"smoke-test issue","body":"approval flow smoke"}' \
  --json
```

Expected response:

```json
{
  "status": "approval_required",
  "approval_id": "01J...",
  "approval_url": "/ui/approvals/01J..."
}
```

Then approve the request from a different operator session (the
agent / operator separation pattern — see
[§ Approval-queue flow](#approval-queue-flow) below):

```bash
meho approvals show 01J...
meho approvals approve 01J... --reason "smoke test"
```

The original call resumes from the queue once approved; the audit
log carries both rows.

## App-vs-PAT credential picker

Two credential paths reach github.com from the connector. The
deep credential-setup recipe lives in
[`github-app-credential.md`](./github-app-credential.md) (both
paths, side by side); this section summarises the operator-facing
**decision** of which path to pick.

| Dimension | GitHub App (preferred) | Fine-grained PAT (fallback) |
| --- | --- | --- |
| Audit shape on GitHub | App identity (`meho-prod`) — agent-vs-operator boundary preserved | Operator's personal account — agent calls indistinguishable from manual UI clicks |
| Token lifetime | Installation tokens auto-refresh every ~50 minutes; key rotates without dispatch breaks | Expires on wall-clock date (≤90 days by GitHub default); breaks every dispatch until rotated |
| Permission scope | Per-installation, narrowable to a specific repo set | Account-scoped (every repo the operator can reach is in scope) |
| Rate limit ceiling | 5000/hour per installation; scales with installation count | 5000/hour total across the operator's tools |
| Org governance | App installation requires org-owner action (one-time) | Operator-account scope; no org coordination needed |
| Personal-account tie | None — App outlives operator turnover | Operator-leaving invalidates the PAT; org cannot centrally rotate |
| Vault payload | `app_id` + `private_key` (PEM) | `pat` |

**Pick the App path** for any production deployment, any deployment
where an audit-readable agent-vs-operator boundary matters, and any
deployment where operator turnover is expected to outpace credential
rotation. This is the default.

**Pick the PAT fallback** only when:

- The target org's owners will not approve an App install on the
  timeline you need (the org-owner action is a hard prerequisite
  for the App path).
- The target is a small set of personal repositories where an App
  install is overkill.
- A short-lived integration test or canary run needs a credential
  with a known expiry date and a hand-curated permission set.

The PAT path is supported but **not first-class**. Every dispatch
through a PAT-credentialed target attributes to the PAT owner's
personal GitHub account in GitHub's own audit log; that crosses
the agent-vs-operator boundary every other meho connector
preserves.

The Vault payload shape — `app_id + private_key` for the App,
`pat` for the PAT — is the connector-side discriminator. The
connector's credential loader inspects the secret's fields and
picks the App code path or the PAT code path accordingly. The
`auth_model` enum value on the target row stays
`shared_service_account` in both cases (the App **is** the shared
service account; the PAT is one too, just owned by a personal
GitHub account rather than a deployment-owned bot).

## Approval-queue flow

The four write ops carry `requires_approval=true` (T5
[#1225](https://github.com/evoila/meho/issues/1225)) so every
attempt at one of them is gated by the G11.2 approval queue. An
operator (or an agent acting under operator identity) hits one of
the four endpoints; the dispatcher writes an `approvals` row,
returns an `approval_required` envelope, and the call **does not
reach GitHub** until another operator approves the queue entry.

### What the agent / operator sees on the call site

```json
{
  "status": "approval_required",
  "approval_id": "01J5K8A...",
  "approval_url": "/ui/approvals/01J5K8A...",
  "details": {
    "op_id": "PUT:/repos/{owner}/{repo}/pulls/{pull_number}/merge",
    "target": "github-meho",
    "principal": "agent.assistant@pmsoft.at",
    "requested_at": "2026-05-27T14:31:00Z"
  }
}
```

The CLI exit code is non-zero (the call did not succeed), but the
shape is **not** an error envelope — the operator can branch on
`status == "approval_required"` to thread the approval through.

### Operator path — CLI

```bash
# List pending approvals on a target.
meho approvals list --target github-meho

# Inspect a specific request.
meho approvals show 01J5K8A...

# Approve.
meho approvals approve 01J5K8A... --reason "merge after CI green"

# Or reject.
meho approvals reject 01J5K8A... --reason "needs second review"
```

The approval / rejection writes a row to the `approvals` table
and (on approval) wakes the original call's continuation — the
queued dispatch retries against GitHub with the approver's
identity threaded into the audit row alongside the original
caller.

### Operator path — `/ui/approvals`

The G11.2 operator-console UI exposes the same queue at
`/ui/approvals` (rendered by the BFF at
`backend/src/meho_backplane/api/ui/approvals.py`). Pending rows
show the op_id, target, principal, requested-at, and any context
the principal attached (e.g. PR number, issue title). Buttons
approve / reject with a free-form reason.

### Operator path — MCP

The MCP tool surface exposes
`meho.approvals.list`, `meho.approvals.show`,
`meho.approvals.approve`, `meho.approvals.reject` for
agent-driven approval workflows (a senior operator's agent
approving a junior operator's agent's request). Same shape as
the CLI verbs; the agent runs them under operator identity.

### Approval bypass for read-only first-day operators

Operators provisioning the target for the first time can run the
entire on-ramp (Steps 1–7 above) without ever triggering an
approval queue — none of `targets import`, `targets probe`,
`connector ingest`, `connector edit-op`, `connector enable`, or
`operation call gh.composite.pr_status_summary` is a
`requires_approval` op. The queue activates the moment an agent
(or operator) reaches for one of the four write ops.

## Audit log story

Every dispatch through the connector lands an `audit_log` row keyed
by the operator's Keycloak `sub`, the target name, the op_id, and
the result status. The combined chain-of-custody coverage (agent →
operator → approver → connector → GitHub App identity → GitHub
audit log) is what Initiative #1220 calls out as the load-bearing
property of the typed connector over a raw `gh` CLI shell-out.

### Worked example — "who merged PR #754 and when?"

The end-to-end reconstruction story takes a single
`meho audit query` call:

```bash
meho audit query \
  --target github-meho \
  --op-id 'PUT:/repos/evoila/meho/pulls/754/merge' \
  --since 7d \
  --json
```

Expected (abbreviated):

```json
{
  "rows": [
    {
      "audit_id": "8c3f...d2a1",
      "principal": "agent.assistant@pmsoft.at",
      "target": "github-meho",
      "op_id": "PUT:/repos/{owner}/{repo}/pulls/{pull_number}/merge",
      "result_status": "ok",
      "occurred_at": "2026-05-27T14:35:12Z",
      "approval_id": "01J5K8A...",
      "approval_approved_by": "damir.topic@pmsoft.at",
      "agent_session_id": "01J5K8B...",
      "request_params": {"owner": "evoila", "repo": "meho", "pull_number": 754}
    }
  ]
}
```

Reading the row top to bottom:

- **`principal`** — the agent (under operator delegation) that
  asked for the merge.
- **`approval_approved_by`** — the operator who clicked approve in
  `/ui/approvals` (or hit `meho approvals approve` on the CLI).
- **`agent_session_id`** — the MCP session that originated the
  call (G0.14-T6 #1147 capture; G0.15-T4 closure unblocks the
  audit-replay tree shape — the v0.7.0 G8.2 audit-replay surface
  navigates this column).
- **`occurred_at`** — when the dispatch landed (post-approval).
- **`request_params`** — what was actually asked for.

### Cross-referencing against GitHub's own audit log

The meho audit row anchors the meho side. To close the loop on the
**GitHub-side** view of the same event:

1. Open `https://github.com/organizations/evoila/settings/audit-log`
   (or the org's audit-log API endpoint).
2. Filter `actor:meho-prod action:pull_request.merge`. Every meho-
   driven merge attributes to the App identity (the App's slug),
   which makes the GitHub-side row immediately recognisable.
3. Match the GitHub-side `created_at` timestamp against the meho
   row's `occurred_at` (the times should agree within ~100 ms —
   the connector emits the API call to GitHub immediately after
   writing the local audit row).

The pair of rows establishes: who in meho asked, who in meho
approved, when meho dispatched, what GitHub saw, and which App
identity GitHub attributed it to. That is the full chain.

### Composite ops fan out into multiple audit rows

`gh.composite.pr_status_summary` is the canonical example: one
dispatch from the agent fans out to (currently) 3 sub-calls
against GitHub (`pulls.get`, `pulls.list_commits`, `checks.list_for_ref`).
Each sub-call writes its own audit row, all carrying the same
`agent_session_id` and a `parent_audit_id` (UUID) pointing back to the
composite parent row. `meho audit query --op-id 'gh.composite.*' --json`
returns the composite rows; `meho audit replay <session-id>` then
renders the parent + sub-call fan-out as a nested tree. The G8.2
audit-replay surface is the supported way to inspect the linkage
today; flat per-parent filtering on the CLI lands in a later release
(the `--parent-audit-id` flag exists on `meho audit query` but the v0.2
backend rejects it with HTTP 400 pending the recursive-CTE endpoint).

## Common failure modes

The four failure modes operators hit most often during first-day
onboarding, in order of how often they show up:

### `503 github_app_not_installed`

**What you see:**

```json
{
  "code": "github_app_not_installed",
  "message": "GitHub App 'meho-prod' (App ID 123456) is not installed on owner='evoila' repo='meho'. Install via https://github.com/settings/apps/meho-prod/installations.",
  "details": {"app_id": 123456, "app_slug": "meho-prod", "owner": "evoila", "repo": "meho"}
}
```

**What happened:** the App exists and the private key is valid, but
the App is not installed on the target repo / org. Most common
shape: the operator created the App in their personal account,
forgot Step 3 of
[`github-app-credential.md`](./github-app-credential.md#step-3--install-the-app-on-the-target-repo-or-org),
and tried to dispatch against a repo in the org.

**Fix:** open the App settings page (URL is in the `message`),
click **Install App**, pick the target account, grant access to
the target repos. Re-run `meho targets probe <target>` to confirm.

### `503 github_installation_token_mint_failed` — missing permission

**What you see:**

```json
{
  "code": "github_installation_token_mint_failed",
  "message": "GitHub App 'meho-prod' is installed but lacks permission 'pull_requests:write' on owner='evoila' repo='meho'. Add the permission at https://github.com/settings/apps/meho-prod/permissions.",
  "details": {"missing_permission": "pull_requests:write"}
}
```

**What happened:** the App is installed, JWT mint succeeded, but
the installation-token exchange returned a token missing the
requested permission. This is the most common **write-op** failure
— the operator installed the App with the Tier-1 read-only
permission set and is now trying to dispatch a Tier-2 write op
(merge, issue-create, workflow-dispatch, release-create).

**Fix:** open the App's permissions page in GitHub, add the missing
permission, **and then accept the permission upgrade in the
installation** (GitHub requires explicit consent on the install,
separate from the App-level permission change — the App owner
sees a banner reading "Approve new permissions"; clicking through
is mandatory before the new scope takes effect). See
[`github-app-credential.md` § Tier 2 — Write catalog](./github-app-credential.md#tier-2--write-catalog-additional-permissions-for-requires_approvaltrue-ops)
for the full permission map.

### `429 github_rate_limited` — composite fan-out

**What you see:**

```json
{
  "code": "github_rate_limited",
  "message": "GitHub API rate limit exceeded; reset at 2026-05-27T15:00:00Z (in 12m 34s).",
  "details": {"limit": 5000, "remaining": 0, "reset_at": "2026-05-27T15:00:00Z", "resource": "core"}
}
```

**What happened:** the per-installation rate limit (5000
requests/hour for App installation tokens; same ceiling for
fine-grained PATs) is exhausted. The `X-RateLimit-Remaining: 0`
header on the most recent response carried the reset wall-clock
time, which the connector surfaces in `details`.

**Composite-op multiplier.** `gh.composite.pr_status_summary`
makes ~3 sub-calls per invocation (pulls.get + pulls.list_commits +
checks.list_for_ref). At the 5000/hour ceiling, that limits a
single installation token to ~1666 composite calls/hour — which
sounds like a lot but a polling agent on a busy repo with many open
PRs can chew through it. Read the `X-RateLimit-Remaining` value off
the structured error's `details.remaining` to gauge headroom on
every dispatch.

**Fix:** wait until `reset_at`, then retry. If the limit hits
during normal operation rather than a runaway loop, audit the
agent's dispatch pattern — most read paths should run well under
the ceiling. If the agent legitimately needs more headroom,
install the App on additional installations (each installation
token has its own 5000/hour bucket) and partition target rows by
installation.

### `404 github_app_not_installed` — PR not visible

**What you see:**

```json
{
  "code": "github_app_not_installed",
  "message": "GitHub returned 404 for owner='evoila' repo='private-research' pull_number=42. Verify the App is installed on this repo (or the PAT has read access).",
  "details": {"owner": "evoila", "repo": "private-research", "pull_number": 42}
}
```

**What happened:** the dispatch reached GitHub but GitHub returned
404. Two distinct shapes share this code: the App / PAT genuinely
can't see the resource (private repo + App lacks installation
scope, or PAT lacks the per-repo read permission), or the resource
genuinely doesn't exist. Both render identically — GitHub does not
distinguish "not allowed to see this private resource" from "no
such resource" by design (otherwise the existence of a private
resource leaks through the 403/404 split). See
`github_app_not_installed` above for the most common cause; the
fix is the same.

**Fix:** verify the repo / PR exists on GitHub. If it does,
re-walk Step 3 of
[`github-app-credential.md`](./github-app-credential.md#step-3--install-the-app-on-the-target-repo-or-org)
and add the missing repo to the App's installation. If using PAT,
re-mint the PAT with the new repo in the **Repository access**
list — fine-grained PATs are scope-locked at mint time and cannot
be widened in place.

## Connector identifier conventions

The `gh-rest` connector exposes the same `(product, version,
impl_id)` triple in three identifier surfaces; pinning them all
in one place:

| Identifier | Source | Example value |
| --- | --- | --- |
| Registry triple | `register_connector_v2(product=, version=, impl_id=, ...)` in [`backend/src/meho_backplane/connectors/github/__init__.py`](../../backend/src/meho_backplane/connectors/github/__init__.py) | `("gh", "3", "gh-rest")` |
| Registered connector_id | `<impl_id>-<version>` of the registry triple; the digit-prefix the dispatcher's `parse_connector_id` regex accepts | `gh-rest-3` |
| Catalog connector_id | `<impl_id>-<version>` of the **catalog** entry from `catalog.yaml`. As of 2026-05-27 (G3.11-T8 #1242, Resolution A) the catalog ``version`` field is the canonical digit-prefix form ``"3"`` -- matches the registry triple so the dispatcher's tuple lookup resolves cleanly | `gh-rest-3` |
| Upstream API label | GitHub's own documentation; mirrored in the connector's `FingerprintResult.version` and in the catalog row's ``notes`` for operator recognition | `v3` |
| Target row `product` | The `product` column on `targets.yaml`; matches the registry's `product` slot | `gh` |

The **registry uses `version="3"`** because the dispatcher's
`parse_connector_id` (in
[`backend/src/meho_backplane/operations/ingest/parser.py`](../../backend/src/meho_backplane/operations/ingest/parser.py))
splits `connector_id` on the first dash-followed-by-digit and
requires the version segment to start with a digit; `"v3"` fails
that regex. **As of 2026-05-27 this is the canonical form for the
catalog ``version`` field too** -- G3.11-T8 (#1242, Resolution A)
reconciled the catalog with the registry after the initial T3
shipped with ``version: v3`` (a drift caught by T5's worker
running a triple lookup that bypassed ``parse_connector_id``).
The upstream "v3" label remains visible in the
`FingerprintResult.version` payload and in the catalog row's
``notes`` so operators reading github.com's docs still recognise
the API generation.

The G0.15-T6 wildcard tie-break ladder is still load-bearing for
**unfingerprinted** targets: a target with
`(product="gh", version=None)` resolves to the registered class
via the ``("gh", "", "")`` wildcard entry; a target with
`(product="gh", version="3")` resolves via the versioned entry
``("gh", "3", "gh-rest")``. There is no longer a separate "v3"
form that needs reconciling at dispatch time -- both the
catalog-driven and registry-driven paths converge on ``"3"``.

**As an operator, you'll see:**

- `gh-rest-3` in `meho connector list` rows (registered class /
  in-memory) and in `audit_query` `connector_id` cells.
- `gh-rest-3` in `meho connector ingest --catalog gh/3` outputs
  and in connector-list rows **after** the catalog ingest lands
  the DB row (same form as the in-memory registered class).
- `gh/3` in `meho connector catalog list` and the `--catalog`
  CLI flag.
- `gh` in the `product` column of `targets.yaml`.
- `v3` in the `version` field of the probe / fingerprint response
  and in the ``notes`` field of the catalog row -- the upstream
  API generation label, preserved for operator recognition.

## Sample agent definition

A worked example: a "weekly PR-review summary" agent that walks
open PRs on `evoila/meho`, builds a status digest, and posts the
digest to a tracking issue. The post-issue step trips
`requires_approval=true` so a human operator gates every weekly
publication.

```yaml
# agents/weekly-pr-summary.yaml
agent_definition:
  name: weekly-pr-summary
  description: |
    Walks open pull requests on evoila/meho once a week, builds a
    status summary (mergeable state, check status, review state per
    PR), and posts the summary as a comment on the tracking issue
    evoila/meho#900. The summary-post step requires operator
    approval (gh.issue.create is requires_approval=true per
    Initiative #1220 T5).

  schedule:
    cron: "0 9 * * MON"   # Monday 09:00 UTC
    timezone: UTC

  identity:
    operator_delegation: damir.topic@pmsoft.at

  permitted_targets:
    - github-meho

  permitted_op_groups:
    # Read groups -- no approval queue, no rate friction.
    - gh-rest-3/pulls
    - gh-rest-3/issues
    - gh-rest-3/checks
    - gh-rest-3/repos

  permitted_op_ids:
    # The one write op the agent is allowed to reach for. Carries
    # requires_approval=true; every weekly run queues an approval
    # the on-call operator clears.
    - gh-rest-3/POST:/repos/{owner}/{repo}/issues/{issue_number}/comments

  resources:
    target_owner: evoila
    target_repo: meho
    summary_issue_number: 900

  steps:
    - name: list_open_prs
      op: gh-rest/gh.composite.pr_status_summary
      params_template: |
        For each open PR on {target_owner}/{target_repo}, call
        gh.composite.pr_status_summary and aggregate the results.

    - name: post_summary
      op: gh-rest/POST:/repos/{owner}/{repo}/issues/{issue_number}/comments
      params:
        owner: "{target_owner}"
        repo: "{target_repo}"
        issue_number: "{summary_issue_number}"
        body: "{rendered_summary}"
      # This step enters the approval queue; the on-call operator
      # clears it via `meho approvals approve <id>` or via
      # /ui/approvals. Without an approver the step blocks until
      # the agent's timeout; the audit row records the queue exit.
```

The agent above is intentionally short — production agent definitions
add prompt context, output schemas, and step-level retry budgets.
The `permitted_op_groups` + `permitted_op_ids` shape is the agent-
runtime contract that scopes which surfaces the agent can reach;
the `requires_approval=true` annotation on `POST:/.../comments`
(if you choose to annotate comment-creation as well — Initiative
#1220 ships the four highest-blast-radius writes by default; comment
posting is opt-in) gates the post step.

The agent runs under operator delegation
(`identity.operator_delegation`) so every dispatch attributes to
both the agent identity and the delegating operator in the audit
log — the same pattern every meho agent runtime uses.

## Status

| Item | Side | State |
| --- | --- | --- |
| Recipe (this doc) | producer | landed in this PR ([`./github-connector.md`](./github-connector.md)) |
| `GitHubRestConnector` substrate (`gh-rest-3`) | producer | landed at T1 [#1221](https://github.com/evoila/meho/issues/1221) |
| `github-app-credential.md` operator recipe | producer | landed at T2 [#1222](https://github.com/evoila/meho/issues/1222) |
| `gh/3` catalog entry | producer | landed at T3 [#1223](https://github.com/evoila/meho/issues/1223); reconciled to digit-prefix form at T8 [#1242](https://github.com/evoila/meho/issues/1242) (live ingest gated on parser-scope follow-up T7 [#1241](https://github.com/evoila/meho/issues/1241)) |
| `gh.composite.pr_status_summary` | producer | tracked at T4 [#1224](https://github.com/evoila/meho/issues/1224) |
| `requires_approval=true` on 4 write ops | producer | tracked at T5 [#1225](https://github.com/evoila/meho/issues/1225) |
| Parser extension to inline `#/components/responses/*` | producer | sibling follow-up — gates Step 4's live ingest |
| `meho-prod` App provisioned on the `evoila` org | consumer | pending — applied by the dogfooding lab operator before first-day on-ramp |
| Operator follows this runbook end to end against `evoila/meho` PR #754 in ≤45 min | consumer | pending — the closing-comment artefact on Initiative #1220 |

## References

- Parent Initiative: [#1220 — G3.11 github-rest typed connector](https://github.com/evoila/meho/issues/1220) — first GitHub REST surface under Goal #214
- Parent Goal: [#214 — Connector parity with ClaudeVCF wrapper set](https://github.com/evoila/meho/issues/214)
- Sibling Task — substrate: [T1 #1221](https://github.com/evoila/meho/issues/1221) — `GitHubRestConnector` skeleton + credential loader
- Sibling Task — credential recipe: [T2 #1222](https://github.com/evoila/meho/issues/1222) — [`github-app-credential.md`](./github-app-credential.md)
- Sibling Task — catalog entry: [T3 #1223](https://github.com/evoila/meho/issues/1223) — `gh/3` Layer-2 ingest acceptance (canonical digit-prefix form per T8 #1242)
- Sibling Task — first composite: [T4 #1224](https://github.com/evoila/meho/issues/1224) — `gh.composite.pr_status_summary`
- Sibling Task — write-op annotations: [T5 #1225](https://github.com/evoila/meho/issues/1225) — `requires_approval=true` on 4 write ops
- Companion shape: [`./keycloak-web-client.md`](./keycloak-web-client.md) — the v0.7.0 G10.0 client recipe this doc mirrors in shape
- Companion runbook: [`./connector-ingestion.md`](./connector-ingestion.md) — the broader operator workflow this runbook plugs into; covers the generic-ingestion pipeline that powers Step 4 in detail
- Connector inventory (codebase map): [`../codebase/connectors-github.md`](../codebase/connectors-github.md) — symbol-level walk-through of `backend/src/meho_backplane/connectors/github/`
- Target row layout: [`./targets-yaml.md`](./targets-yaml.md) — `name`, `aliases`, `product`, `host`, `secret_ref`, `auth_model` column conventions
- Per-target Vault read policy: [`./connector-vault-policy.md`](./connector-vault-policy.md) — the ACL templating contract every per-target secret read flows through
- Audit query CLI: [`../codebase/audit_query.md`](../codebase/audit_query.md) — `meho audit query` flags, output shape, RBAC notes
- Operator-console approvals surface: [`../codebase/approvals.md`](../codebase/approvals.md) — `/ui/approvals` rendering, `meho.approvals.*` MCP tools
- Post-deploy enablement: [`docs/RELEASING.md` § 6a](../RELEASING.md) — the release checklist's gated-features section cross-references this runbook
- Error-message-shape convention: [`docs/codebase/error-message-shape.md`](../codebase/error-message-shape.md) — T11 stable-code error envelope
- GitHub REST API documentation: <https://docs.github.com/en/rest>
- GitHub REST API rate limits: <https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api>
- GitHub audit log API: <https://docs.github.com/en/enterprise-cloud@latest/admin/monitoring-activity-in-your-enterprise/reviewing-audit-logs-for-your-enterprise/using-the-audit-log-api-for-your-enterprise>
- Consumer feature request: `claude-rdc-hetzner-dc#753` § "GitHub typed connector — operator runbook is a named acceptance criterion."
