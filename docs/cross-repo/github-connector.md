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
  fingerprint payload); ~700 ops grouped into ~49 tags ingest
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
  commit / check-run reads. ~700 ops ingest under the `gh/3`
  catalog entry; see
  [§ Step 4 — Ingest the catalog](#step-4--ingest-the-catalog).
- Write ops — `gh.issue.create`, `gh.pr.merge`,
  `gh.workflow_run.dispatch`, `gh.release.create`. Each carries
  `requires_approval=true` once you run the Step 6 annotation, so a
  queued approval gates every dispatch.
- Composite operations — `gh.composite.pr_status_summary` (T4) is
  the first L1 composite; the operator-facing on-ramp uses it as
  the smoke / verify call. MEHO also ships composites for repos,
  issues, sub-issues, Projects-v2 boards and git commits; see
  [`../codebase/connectors-github.md`](../codebase/connectors-github.md).
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
- **`gh-rest` connector class registered.** The backplane image
  ships the `GitHubRestConnector` class. Verify with `meho connector list | grep gh-rest` — a row keyed
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
# Expected: `reachable=true`, `extras.installations_count`, and the
# first installation's `extras.app_slug` / `extras.installation_account`.
```

If Check 2 shows `reachable: false` and `extras.error_code` is
`github_app_not_installed`,
`github_jwt_mint_failed`, or
`github_installation_token_mint_failed`, return to
[`github-app-credential.md` § Failure modes](./github-app-credential.md#failure-modes)
and re-walk the corresponding step before continuing.

### Step 2 — Register the target row

The target row that
[`github-app-credential.md` § Step 5](./github-app-credential.md#step-5--register-the-gh-v3-target-with-meho)
prepared in `targets.yaml` lands via `meho targets import`. (`meho targets add <name>`, alias
`create`, registers one target from flags instead of a file.)

```bash
meho targets import targets.yaml
```

By default `import` stops the whole import when a `name` already
exists in the tenant, and writes nothing. Pass `--update` to update
existing rows and create new ones; `--dry-run` prints the plan first.

Verify the row landed:

```bash
meho targets describe github-meho
```

Expected output (abbreviated):

```
name:        github-meho
product:     gh
host:        api.github.com
secret_ref:  <tenant>/github-meho/github-app
auth_model:  shared_service_account
```

The `auth_model: shared_service_account` value is the
`AuthModel` enum slot from
[`backend/src/meho_backplane/connectors/schemas.py`](../../backend/src/meho_backplane/connectors/schemas.py)
— the App is the single machine identity shared across every
operator that dispatches against this target. The **App-vs-PAT
discriminator** is **not** on the target row itself; it lives
**inside the Vault secret payload** (presence of all three of
`app_id` + `private_key` + `installation_id` selects the App
installation-token path; presence of `token` (and no App fields)
selects the fine-grained PAT path). G0.16-T2 (#1304) reconciled
the connector code with this documented contract — before that
fix the gh-rest connector demanded `auth_model="github-app"` /
`auth_model="github-pat"` on the target row, neither of which is a
value the `AuthModel` enum accepts; the post-#1304 connector
matches the runbook. See
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
  "vendor": "github",
  "product": "gh",
  "version": "v3",
  "reachable": true,
  "probed_at": "2026-05-27T14:30:00Z",
  "probe_method": "GET /user/installations",
  "extras": {
    "installations_count": 1,
    "installation_id": 987654321,
    "installation_account": "evoila",
    "target_type": "Organization",
    "app_slug": "meho-prod",
    "permissions": {
      "contents": "read",
      "issues": "read",
      "metadata": "read",
      "pull_requests": "read"
    }
  }
}
```

When the target `host` is `api.github.com/repos/<owner>/<repo>`, the
probe reads that one repo instead (`probe_method` is
`GET /repos/<owner>/<repo>`) and `extras` shows `repo_full_name`,
`owner_login`, `default_branch` and the workflow check.

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

> **Note.** The OpenAPI parser inlines `#/components/responses/*`
> and `#/components/requestBodies/*` refs, which the GitHub REST
> spec uses on most operations. The live ingest test
> [`backend/tests/integration/test_operations_ingest_github.py`](../../backend/tests/integration/test_operations_ingest_github.py)
> runs when `MEHO_GH_INGEST_LIVE=1` is set. The `gh.composite.*`
> operations read GitHub through the connector session and do not
> need this ingest; if you only need a composite, jump to
> [§ Step 7 — Smoke-test with the composite](#step-7--smoke-test-with-the-composite).

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

A fresh ingest stores the four write ops with
`requires_approval=false` (the parser gives `POST` / `PUT` the
`caution` level). Turn approval on after every ingest, either with
the one-shot script described in
[`github-write-ops-approval.md`](./github-write-ops-approval.md) or
with `meho connector edit-op` (there is **no** `meho operation
annotate` verb):

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

Each edit writes a `meho_connector_edit_op` audit row in the same
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
against a known PR. The composite makes its three reads through
the connector session, so it works even without the Step 4 ingest.

```bash
meho operation call gh-rest-3 'gh.composite.pr_status_summary' \
  --target github-meho \
  --params '{"owner":"evoila","repo":"meho","pull_number":1193}' \
  --json
```

Expected (abbreviated):

```json
{
  "pr": {"number": 1193, "state": "closed", "merged_at": "2026-05-27T..."},
  "checks": {"total_count": 12, "check_runs": ["..."]},
  "reviews": [{"user": {"login": "..."}, "state": "APPROVED"}],
  "mergeable": null,
  "mergeable_state": "unknown",
  "checks_status": "all_passed",
  "review_status": "approved"
}
```

A green Step 7 confirms: the credential chain works end to end,
the composite is registered, and the dispatch lands an audit row.

To verify the **write-op approval flow**, attempt a write op
without an approval first; you should see the call enter the
queue:

```bash
meho operation call gh-rest-3 'POST:/repos/{owner}/{repo}/issues' \
  --target github-meho \
  --params '{"owner":"evoila","repo":"meho","title":"smoke-test issue","body":"approval flow smoke"}' \
  --json
```

Expected response (abbreviated):

```json
{
  "status": "awaiting_approval",
  "op_id": "POST:/repos/{owner}/{repo}/issues",
  "error": "awaiting_approval: 'POST:/repos/{owner}/{repo}/issues' requires approval before execution",
  "extras": {
    "error_code": "awaiting_approval",
    "approval_request_id": "<approval_request_id>"
  }
}
```

Then approve the request from a different operator session (the
agent / operator separation pattern — see
[§ Approval-queue flow](#approval-queue-flow) below):

```bash
meho approvals show <approval_request_id>
meho approvals approve <approval_request_id> --reason "smoke test"
```

MEHO runs the call once it is approved; `meho approvals result
<approval_request_id>` shows the outcome, and the audit log carries
the request, decision and run rows.

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
| Vault payload | `app_id` + `private_key` (PEM) + `installation_id` | `token` |

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

The Vault payload shape — all three of `app_id` + `private_key` +
`installation_id` for the App, `token` for the PAT — is the
connector-side discriminator. The connector's credential loader
inspects the secret's fields and picks the App code path or the
PAT code path accordingly; a payload that carries neither shape
fails closed with a typed `github_ambiguous_vault_payload` error
naming both required field sets so the operator can repair the
Vault row without guessing. The `auth_model` enum value on the
target row stays `shared_service_account` in both cases (the App
**is** the shared service account; the PAT is one too, just owned
by a personal GitHub account rather than a deployment-owned bot).

## Approval-queue flow

The four write ops carry `requires_approval=true` (T5
[#1225](https://github.com/evoila/meho/issues/1225)) so every
attempt at one of them is gated by the G11.2 approval queue. An
operator (or an agent acting under operator identity) hits one of
the four endpoints; the dispatcher writes an `approval_request` row,
returns an `awaiting_approval` result, and the call **does not
reach GitHub** until another operator approves the request.

### What the agent / operator sees on the call site

```json
{
  "status": "awaiting_approval",
  "op_id": "PUT:/repos/{owner}/{repo}/pulls/{pull_number}/merge",
  "error": "awaiting_approval: 'PUT:/repos/{owner}/{repo}/pulls/{pull_number}/merge' requires approval before execution",
  "extras": {
    "error_code": "awaiting_approval",
    "approval_request_id": "<approval_request_id>"
  }
}
```

The status is `awaiting_approval`, not `ok` or `error` — the caller
can branch on it and pass `extras.approval_request_id` to the
approver.

### Operator path — CLI

```bash
# List pending approvals (the default status filter is pending).
meho approvals list

# Inspect a specific request.
meho approvals show <approval_request_id>

# Approve.
meho approvals approve <approval_request_id> --reason "merge after CI green"

# Or reject.
meho approvals reject <approval_request_id> --reason "needs second review"
```

The decision updates the `approval_request` row and writes a decision
audit row. On approval MEHO runs the call against GitHub; the run's
audit row carries the approver as its principal and points to the
request row through `parent_audit_id`.

### Operator path — `/ui/approvals`

The G11.2 operator-console UI exposes the same queue at
`/ui/approvals` (rendered by
`backend/src/meho_backplane/ui/routes/approvals/routes.py`). Pending rows
show the op_id, target, principal, requested-at, and any context
the principal attached (e.g. PR number, issue title). Buttons
approve / reject with a free-form reason.

### Operator path — MCP

The MCP tool surface exposes the **read** tools
`meho_approvals_list` and `meho_approvals_get` so an agent can
surface and inspect pending requests. The **decision** verbs
(`meho_approvals_approve` / `meho_approvals_reject`) have no MCP
path under any claim set (#3155): approving or rejecting a parked
operation is a human decision (v0.1-spec §7), made on the REST /
CLI / console surfaces — a model holding the approve button would
collapse the very gate the approval exists to enforce. An agent
reads the queue over MCP; a human decides it.

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

### Worked example — "who merged a PR and when?"

The `op_id` in the audit log is the template
(`PUT:/repos/{owner}/{repo}/pulls/{pull_number}/merge`), not the
filled-in path. Start with one `meho audit query` call:

```bash
meho audit query \
  --target github-meho \
  --op-id 'PUT:/repos/{owner}/{repo}/pulls/{pull_number}/merge' \
  --since 7d \
  --json
```

Expected (abbreviated):

```json
{
  "rows": [
    {
      "id": "8c3f...d2a1",
      "ts": "2026-05-27T14:35:12Z",
      "principal_sub": "...",
      "principal_name": "approver@example.com",
      "actor_name": null,
      "target_name": "github-meho",
      "op_id": "PUT:/repos/{owner}/{repo}/pulls/{pull_number}/merge",
      "op_class": "write",
      "result_status": "ok",
      "parent_audit_id": "e4c2...",
      "agent_session_id": "..."
    }
  ],
  "next_cursor": null
}
```

Reading the row top to bottom:

- **`principal_name`** — on an approved run this is the person who
  approved it in `/ui/approvals` or with `meho approvals approve`.
- **`parent_audit_id`** — the `approval.request` row. Look it up
  with `meho audit query --audit-id <parent_audit_id>` to see who
  asked for the merge, and list its decision and run rows with
  `meho audit query --parent-audit-id <parent_audit_id>`.
- **`actor_name`** — the agent that acted for the person, when an
  agent run made the call.
- **`agent_session_id`** — the MCP session that started the call;
  `meho audit replay <session-id>` shows the whole session.
- **`ts`** — when the run was recorded.
- `meho audit show <id>` prints the full row, including the payload
  with the call's parameters.

### Cross-referencing against GitHub's own audit log

The meho audit row anchors the meho side. To close the loop on the
**GitHub-side** view of the same event:

1. Open `https://github.com/organizations/evoila/settings/audit-log`
   (or the org's audit-log API endpoint).
2. Filter `actor:meho-prod action:pull_request.merge`. Every meho-
   driven merge attributes to the App identity (the App's slug),
   which makes the GitHub-side row immediately recognisable.
3. Match the GitHub-side `created_at` timestamp against the meho
   row's `ts` (the times should be close: MEHO writes the audit row
   right after the GitHub call returns).

The pair of rows establishes: who in meho asked, who in meho
approved, when meho dispatched, what GitHub saw, and which App
identity GitHub attributed it to. That is the full chain.

### Composite ops write one audit row

`gh.composite.pr_status_summary` makes 3 calls to GitHub (the PR,
the check runs of its head commit, and its reviews). It makes them
through the connector session, not through the dispatcher, so the
composite writes **one** audit row for the whole call.
`meho audit query --op-id 'gh.composite.*' --json` returns these rows.

## Common failure modes

The four failure modes operators hit most often during first-day
onboarding, in order of how often they show up.

Where the error shows up:

- **`meho targets probe`** returns `reachable: false`. `extras.error`
  holds the error class and message, and `extras.error_code` holds
  the stable `github_*` code.
- **`meho operation call`** returns `status: "error"`. For a
  credential or token error, `extras.error_code` is `connector_error`,
  `extras.exception_class` names the class (for example
  `GitHubAppNotInstalledError`), and `extras.exception_message` starts
  with the `github_*` code.

### `github_app_not_installed`

**What you see** (in `extras.exception_message`, or in
`extras.error` on a probe):

```
github_app_not_installed: installation_id='987654321' is not reachable for this App — the App may not be installed on the target repo/org, or the installation_id in Vault is stale. ...
```

**What happened:** the private key is valid (the JWT was signed), but
GitHub answered `404` when MEHO asked for an installation token. The
App is not installed on the target account, or the `installation_id`
in Vault is wrong. Most common shape: the operator created the App in
their personal account, forgot Step 3 of
[`github-app-credential.md`](./github-app-credential.md#step-3--install-the-app-on-the-target-repo-or-org),
and tried to dispatch against a repo in the org.

**Fix:** open the App settings page, click **Install App**, pick the
target account, grant access to the target repos, and write the new
`installation_id` into Vault. Re-run `meho targets probe <target>` to
confirm.

### `connector_http_403` — missing permission

**What you see:**

```json
{
  "status": "error",
  "error": "connector_http_403: the upstream returned HTTP 403 Forbidden. ...",
  "extras": {
    "error_code": "connector_http_403",
    "http_status": 403,
    "upstream_message": "Resource not accessible by integration",
    "permission_headers": {"X-Accepted-GitHub-Permissions": "issues=write"}
  }
}
```

**What happened:** the App is installed and the installation token
was minted, but GitHub refused the call because the installation
lacks the permission the operation needs. This is the most common
**write-op** failure — the operator installed the App with the
Tier-1 read-only permission set and is now trying to dispatch a
Tier-2 write op (merge, issue-create, workflow-dispatch,
release-create). `extras.permission_headers` echoes the permission
GitHub wants, when GitHub sends it.

**Fix:** open the App's permissions page in GitHub, add the missing
permission, **and then accept the permission upgrade in the
installation** (GitHub requires explicit consent on the install,
separate from the App-level permission change — the App owner
sees a banner reading "Approve new permissions"; clicking through
is mandatory before the new scope takes effect). See
[`github-app-credential.md` § Tier 2 — Write catalog](./github-app-credential.md#tier-2--write-catalog-additional-permissions-for-requires_approvaltrue-ops)
for the full permission map.

`github_installation_token_mint_failed` is a different error: GitHub
refused to mint the installation token at all (for example a revoked
private key, or a network failure to `api.github.com`).

### Rate limit — composite fan-out

**What you see:** when the limit hits during the installation-token
mint, the error is `github_rate_limited` (with the reset time as a
Unix timestamp). When it hits during an operation call, GitHub
answers `403` or `429`: a `403` comes back as `connector_http_403`,
a `429` as `connector_error` with `extras.http_status: 429`. In both
cases `extras.upstream_message` carries GitHub's text.

**What happened:** the per-installation rate limit (5000
requests/hour for App installation tokens; same ceiling for
fine-grained PATs) is exhausted.

**Composite-op multiplier.** `gh.composite.pr_status_summary`
makes 3 GitHub calls per invocation (the PR, its check runs, its
reviews). At the 5000/hour ceiling, that limits a single
installation token to ~1666 composite calls/hour — which sounds
like a lot but a polling agent on a busy repo with many open PRs
can chew through it.

**Fix:** wait for the reset window, then retry. If the limit hits
during normal operation rather than a runaway loop, audit the
agent's dispatch pattern — most read paths should run well under
the ceiling. If the agent legitimately needs more headroom,
install the App on additional installations (each installation
token has its own 5000/hour bucket) and partition target rows by
installation.

### HTTP `404` — PR not visible

**What you see:** `status: "error"` with `extras.error_code:
connector_error`, `extras.http_status: 404`, and GitHub's
`Not Found` text in `extras.upstream_message`.

**What happened:** the dispatch reached GitHub but GitHub returned
404. Two cases look the same: the App / PAT can't see the resource
(private repo + App lacks installation scope, or PAT lacks the
per-repo read permission), or the resource does not exist. GitHub
answers both with 404 by design, so it does not reveal that a
private resource exists.

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
  in-memory).
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
    operator_delegation: operator@example.com

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

This YAML is an illustration of the idea, not a file format MEHO
reads. The agent above is intentionally short — production agent definitions
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
| `gh/3` catalog entry | producer | landed (T3), in the digit-prefix form (T8) |
| `gh.composite.pr_status_summary` | producer | landed (T4) |
| `requires_approval=true` on 4 write ops | producer | landed (T5) as the one-shot script in `backend/scripts/annotate_github_write_ops.py` |
| Parser extension to inline `#/components/responses/*` | producer | landed (T7) — Step 4's live ingest works |
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
- Operator-console approvals surface: [`../codebase/approvals.md`](../codebase/approvals.md) — `/ui/approvals` rendering, `meho_approvals_*` MCP tools
- Post-deploy enablement: [`docs/RELEASING.md` § 6a](../RELEASING.md) — the release checklist's gated-features section cross-references this runbook
- Error-message-shape convention: [`docs/codebase/error-message-shape.md`](../codebase/error-message-shape.md) — T11 stable-code error envelope
- GitHub REST API documentation: <https://docs.github.com/en/rest>
- GitHub REST API rate limits: <https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api>
- GitHub audit log API: <https://docs.github.com/en/enterprise-cloud@latest/admin/monitoring-activity-in-your-enterprise/reviewing-audit-logs-for-your-enterprise/using-the-audit-log-api-for-your-enterprise>
- Consumer feature request: the consumer's request for a GitHub typed connector, which names this operator runbook as an acceptance item.
