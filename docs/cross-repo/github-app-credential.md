<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# GitHub App credential recipe — machine-identity for the `gh-rest-3` connector

> Cross-repo handshake between `evoila/meho` (this repo, producer of
> the `gh-rest-3` typed connector under
> [Initiative #1220](https://github.com/evoila/meho/issues/1220)) and
> the operator's GitHub organization (consumer side; not a single
> repo — every MEHO deployment talks to its own GitHub org / set of
> repos).
>
> This page is the upstream-side **tracker** for the GitHub App +
> private-key pair each consumer must provision before standing up
> a `gh-v3` target. The configuration itself is operator-applied
> (`https://github.com/settings/apps/new` + Vault secret-write);
> what lives here is the recipe the operator follows and the
> verification commands either side can run to prove the contract
> holds.

## Why this doc exists

The G3.11 Initiative ([#1220](https://github.com/evoila/meho/issues/1220))
ships a `gh-rest-3` typed connector — the first GitHub REST surface
under the connector-parity Goal's "operators progressively retire local wrappers
in favour of `meho <connector> <op>`" charter. The connector reaches
GitHub through one of two credential paths, picked per target:

1. **GitHub App installation tokens** (the preferred, machine-identity
   path) — the connector loads an App ID + RSA private key from Vault,
   mints a short-lived (10-minute) JWT, exchanges it for a
   per-installation token (1-hour TTL), and refreshes on expiry.
2. **Fine-grained Personal Access Token** (the fallback path, when
   the operator can't / won't create an App in the target org) — the
   connector reads a single PAT from Vault and sends it as a bearer
   token verbatim.

The two paths produce **identifiably different audit shapes** on the
GitHub side, and the recipe below documents both so an operator can
make the App-vs-PAT call deliberately rather than by drift.

The backplane cannot enforce GitHub-side configuration; it can only
fail closed when the App is missing, the private key is malformed,
the installation lacks a requested scope, or the rate limit kicks in.
This doc is the contract that specifies what the GitHub-side
configuration must look like so the connector reaches steady state.

## Prerequisites

- **A GitHub organization** (or a personal account) you control where
  the App will be created and the target repo(s) live. `github.com`
  only — GitHub Enterprise Server (GHES) is out of scope for the
  v0.x `gh-rest-3` connector.
- **Owner / admin rights** in that org if you intend to install the
  App org-wide. Repo-admin rights suffice for single-repo
  installations.
- **A populated Vault server** reachable by the backplane — the
  per-target Vault read precedent from G3.9
  ([#939](https://github.com/evoila/meho/issues/939)) applies; the
  App's credential pair lands under a path the operator's Vault role
  is allowed to read per
  [`docs/cross-repo/connector-vault-policy.md`](./connector-vault-policy.md).
- **An operator session** against the backplane —
  `meho login <backplane-url>` writes a session token the CLI reuses
  across every verb. The `targets import / probe / describe` verbs
  need `tenant_admin`; the `operation call` verbs need `operator`.
- **`gh-rest-3` connector registered.** The backplane image ships
  the `GitHubRestConnector` class and its credential loader
  (`backend/src/meho_backplane/connectors/github/`).

## Recipe

### Step 1 — Create the GitHub App

In the GitHub UI (logged in as an org owner, or as the personal
account that will own the App):

1. Navigate to **Settings** → **Developer settings** → **GitHub Apps**
   → **New GitHub App**, or jump directly to
   `https://github.com/settings/apps/new` (personal-account App) or
   `https://github.com/organizations/<your-org>/settings/apps/new`
   (org-owned App).
2. **GitHub App name:** `meho-<environment>` (e.g. `meho-prod`,
   `meho-lab`). The name is visible in GitHub's audit log as
   the actor on every API call the connector makes, so pick a name
   the audit reader will recognize.
3. **Homepage URL:** any URL identifying the deployment (e.g. the
   backplane's public URL). GitHub requires this field but does not
   verify it.
4. **Webhook:** **uncheck "Active"**. The `gh-rest-3` connector is
   pull-based; it does not receive webhooks. Leaving webhooks enabled
   with no `Webhook URL` produces a GitHub warning banner on every
   App page but is otherwise harmless; unchecking removes the noise.
5. **Permissions:** apply the scope picker from
   [§ Permission scope picker](#permission-scope-picker) below. Pick
   the **read-only** set for a first install; widen to write later as
   the catalogue of dispatchable ops grows.
6. **Where can this GitHub App be installed?:** **Only on this account**
   for a personal-account App or **Any account** for an org-owned App
   you intend to share with sub-orgs. The narrower setting is the
   conservative default.
7. **Create GitHub App.** GitHub takes you to the App's settings page.

### Step 2 — Generate and download the private key

1. On the App's settings page, scroll to **Private keys**.
2. **Generate a private key.** The browser immediately downloads a
   file named like `meho-prod.2026-05-27.private-key.pem`. This is
   the **only time** GitHub will expose this value — treat it as
   sensitive; do not paste it into chat logs or commit it to a
   repo.
3. Note the **App ID** displayed near the top of the App's settings
   page (a small integer, e.g. `123456`). You will need both the
   App ID and the private key in Step 4.

### Step 3 — Install the App on the target repo or org

1. On the App's settings page, sidebar → **Install App**.
2. Pick the target account (your personal account or the target org).
3. **Repository access:** **All repositories** (org-wide install) or
   **Only select repositories** (single-repo / multi-repo install).
   The narrower setting is the conservative default — the connector
   will only see the explicitly listed repos.
4. **Install.** GitHub confirms with the App's installation page.
   The URL carries the **Installation ID** in the path
   (`/settings/installations/<installation_id>`) — **record this
   integer**; you will write it into Vault in Step 4 alongside the
   App ID and the private key. The connector's Vault-payload
   discriminator (G0.16-T2 #1304) requires all three fields
   (`app_id`, `private_key`, `installation_id`); a two-field payload
   surfaces as `github_ambiguous_vault_payload` at first call.

### Step 4 — Copy App ID, installation ID, and private key to Vault

The credential triple lands under a Vault path the per-target Vault
read policy grants the operator. The recommended layout (matches the
existing per-target conventions and works with the credential-loader
path the connector ships in T1):

```bash
# Path layout: secret/<tenant>/<target>/github-app
#
# The target row's `secret_ref` column holds the path inside the
# `secret` mount: <tenant>/<target>/github-app (no `secret/`, no `/data/`). The
# connector reads three fields from the secret: `app_id` (the small
# integer shown on the App settings page), `installation_id` (the
# integer from the `/settings/installations/<id>` URL recorded in
# Step 3), and `private_key` (the entire .pem file contents,
# including the BEGIN / END lines and every newline). All three are
# required — a two-field payload surfaces as
# `github_ambiguous_vault_payload` at first call (see
# [§ Failure modes](#failure-modes)).
vault kv put secret/<tenant>/<target>/github-app \
  app_id='123456' \
  installation_id='987654321' \
  private_key=@/path/to/meho-prod.2026-05-27.private-key.pem
```

Substitute the placeholders:

- `<tenant>` — the tenant slug the target lives under (e.g.
  `my-lab`, `prod`).
- `<target>` — the target name you will use in the `targets.yaml`
  row in Step 5 (e.g. `github-meho`, `github-evoila`).
- The `@` prefix on `private_key=@<path>` is mandatory when reading
  from a file — without it the shell passes the literal string
  `@/path/...` which lands as a malformed key, surfacing later as
  the `github_jwt_mint_failed` failure mode (see
  [§ Failure modes](#failure-modes)).

Verify the secret landed without leaking the value:

```bash
# Expected: app_id           <small-integer>
#           installation_id  <integer>
#           private_key      <hash>
vault kv get -format=json secret/<tenant>/<target>/github-app \
  | jq '.data.data | {app_id, installation_id, private_key_present: (.private_key | length > 0)}'
```

The `private_key | length > 0` check confirms the value is non-empty
without ever printing the key material. PEM files are 1700+
characters; an empty / one-line value at this step is the
operator-side smoking gun for the `github_jwt_mint_failed` failure
mode at first call.

### Step 5 — Register the `gh-v3` target with meho

Add the target row to your tenant's `targets.yaml` (see
[`targets-yaml.md`](./targets-yaml.md) for the file layout
conventions):

```yaml
targets:
  - name: github-meho
    aliases:
      - gh-meho
    product: gh
    host: api.github.com
    fqdn: api.github.com
    secret_ref: <tenant>/github-meho/github-app
    auth_model: shared_service_account
    notes: |
      GitHub App "meho-prod" (App ID 123456) installed on
      evoila/meho. Read-only catalog scope.
```

Notes on the column choices:

- **`product: gh`** — the product slug for the `gh-rest-3`
  connector (named per the no-AI-tool-name + no-customer-name
  convention; `gh` is the generic, no-ambiguity short form).
- **`host: api.github.com`** — the connector's HTTP base URL. GHES
  deployments would point this at the org-internal API host, but
  GHES is out of scope for the v0.x connector.
- **`secret_ref`** — the path written in Step 4, inside the `secret`
  mount: **without** the `secret/` mount name and **without** the
  `/data/` part (MEHO adds both when it reads the secret).
- **`auth_model: shared_service_account`** — the App is the single
  machine identity shared across every operator on this target. The
  `AuthModel` enum
  ([`backend/src/meho_backplane/connectors/schemas.py`](../../backend/src/meho_backplane/connectors/schemas.py))
  ships three values today (`impersonation`,
  `shared_service_account`, `per_user`); the App fits
  `shared_service_account` because every operator-driven call from
  this target is attributed in GitHub's own audit log to the App
  identity. The **credential format** discriminator (App-
  installation vs fine-grained PAT) lives **inside the Vault secret
  payload**, not on the enum — the connector picks the code path
  based on which of `app_id` + `private_key` + `installation_id`
  vs `token` is present in the secret (G0.16-T2 #1304 reconciled
  the code with this documented contract; pre-#1304 the connector
  demanded the operator-facing field carry `github-app` /
  `github-pat`, neither of which is a value the `AuthModel` enum
  accepts).

Register:

```bash
meho targets import targets.yaml
```

> **Next:** once the target row is registered and the credential
> verifies via Check 2 below, [`github-connector.md`](./github-connector.md)
> picks up from "credential side complete" and walks the end-to-end
> first-day on-ramp (target probe → catalog ingest → group enable →
> write-op annotation → composite smoke-test). This credential
> recipe and the connector runbook are designed to be read in
> sequence; combined first-day time is ≤45 minutes (T2 ≤30 min;
> T6 ≤15 min on top).

### Step 6 — Rotate the private key when needed

The GitHub App private key can be rotated at any time without
deleting the App:

1. App settings page → **Private keys** → **Generate a private key**.
   Download the new `.pem`.
2. (Optional but recommended) **Delete the old private key** from
   the same page — GitHub retains both until you explicitly remove
   the old one. Leaving the old key active widens the blast radius
   of a leak.
3. Rewrite the Vault secret with **all three** App fields — `app_id`
   and `installation_id` re-supplied unchanged, `private_key` pointed
   at the new `.pem`:

   ```bash
   vault kv put secret/<tenant>/<target>/github-app \
     app_id='<unchanged>' \
     installation_id='<unchanged>' \
     private_key=@/path/to/<new-key.pem>
   ```

   Vault KV-v2 `kv put` creates a new version with **exactly** the
   fields you supply — it is a replace, not a merge. Omitting
   `installation_id` (or `app_id`) at rotation drops the field from
   the new version; the next installation-token mint then surfaces
   as `github_ambiguous_vault_payload` because the credential loader
   only picks the App path when **all three** `_GH_APP_FIELDS` are
   present (see [§ Failure modes](#failure-modes) and the loader
   contract in
   [§ Step 4](#step-4--copy-app-id-installation-id-and-private-key-to-vault)).
   When you genuinely want merge semantics elsewhere (rare for this
   secret — the field set is intentionally closed) use `vault kv
   patch` instead; `kv put` always replaces.

   The App ID and installation ID do not change on key rotation — copy
   them out of the current version (`vault kv get -format=json …`)
   before writing the new one so you don't have to fish them out of
   the GitHub UI again.
4. The connector picks up the new key on its next installation-token
   mint (within at most 50 minutes, when MEHO's kept installation
   token expires). To switch at once, update the target row (any
   target change drops the kept token) or restart the backplane pods.

During the gap between "new key in Vault" and "old installation
token expired", the connector continues to use the old token until
it expires; the next mint uses the new key. No in-flight calls
fail.

## Permission scope picker

GitHub's App permissions are fine-grained; pick the narrowest set
that covers your dispatchable operations. The Initiative-#1220
catalogue ships two scope tiers:

### Tier 1 — Read-only catalog (most installs start here)

Covers the read operations the consumer named as daily-driver hits:
`gh.pr.get`, `gh.pr.get_checks`, `gh.pr.get_reviews`,
`gh.repo.list_prs`, `gh.issue.list`, `gh.composite.pr_status_summary`,
and similar.

| GitHub App permission | Access | Why |
| --- | --- | --- |
| **Actions** | Read-only | `gh.workflow_run.list`, CI checks read paths |
| **Contents** | Read-only | Repo metadata, file reads, commit SHAs |
| **Issues** | Read-only | `gh.issue.list`, `gh.issue.get` |
| **Metadata** | Read-only (mandatory; auto-granted) | Repo + org metadata |
| **Pull requests** | Read-only | `gh.pr.get`, `gh.pr.get_reviews`, `gh.pr.get_mergeable` |

### Tier 2 — Write catalog (additional permissions for `requires_approval=true` ops)

Add the following on top of Tier 1 when you intend to dispatch the
four high-blast-radius write ops Initiative #1220 ships under
T5 ([#1225](https://github.com/evoila/meho/issues/1225)):
`gh.issue.create`, `gh.pr.merge`, `gh.workflow_run.dispatch`,
`gh.release.create`. Every one of these carries
`requires_approval=true` once the annotation in
[`github-write-ops-approval.md`](./github-write-ops-approval.md) runs,
so the G11.2 approval queue gates the call before dispatch — the App permission is the second gate (GitHub's
side; meho's approval queue is the first).

| GitHub App permission | Access | Why |
| --- | --- | --- |
| **Issues** | Read **and write** | `gh.issue.create` |
| **Pull requests** | Read **and write** | `gh.pr.merge` |
| **Actions** | Read **and write** | `gh.workflow_run.dispatch` |
| **Contents** | Read **and write** | `gh.release.create` (creates a Git tag + release) |

GitHub's permission model is "write implies read" — picking
**Read and write** in Tier 2 supersedes the Tier-1 **Read-only**
entry on the same permission name.

### Tightening the scope further

Operators running a more conservative install pattern can drop any
permission whose corresponding op is not in their dispatch
catalogue. Keep the ops that need a dropped permission disabled
with `meho connector edit-op gh-rest-3 '<op-id>' --disable` after
the `gh/3` catalogue ingest.

## Vault custody

The Vault path the connector reads is
`secret/<tenant>/<target>/github-app`, KV-v2, three fields (all
required — the connector's Vault-payload discriminator picks the
App branch only when **all three** App fields are present; see
[`github-connector.md`](./github-connector.md) § "App-vs-PAT credential picker"):

| Field | Type | Notes |
| --- | --- | --- |
| `app_id` | small integer (as string) | GitHub-assigned ID shown on the App settings page |
| `installation_id` | integer (as string) | From the `/settings/installations/<id>` URL recorded in Step 3 — the specific install of this App on the target org / repos |
| `private_key` | multi-line string | Entire `.pem` file contents — BEGIN line, body, END line, every newline preserved |

The per-target Vault read precedent applies
([`connector-vault-policy.md`](./connector-vault-policy.md) § 2):
the path renders against the operator's Vault Identity entity via
ACL templating; **only operators whose policy grants
`secret/data/<tenant>/<target>/github-app` read** can dispatch
against the target. Operators outside the path see a
`VaultRoleDeniedError` at dispatch time (rendered as `403
{"code":"connector_error","message":"vault denied read for
<path>"}`).

### Production vs dev separation

Each environment should have its **own GitHub App** with its **own
installation** on the target repos:

- A `meho-prod` App installed on `evoila/meho` production
  repositories — Tier-2 scope, audited.
- A `meho-lab` App installed on the lab's mirror repositories
  — Tier-1 scope (read-only).

Vault paths follow the same separation:
`secret/prod/github-meho/github-app` vs
`secret/my-lab/github-meho/github-app`. The shared per-target
Vault convention applies; do not point production targets at the
dev App's secret.

## PAT fallback

Personal Access Tokens (the **fine-grained** variant — classic PATs
are out of scope) are the supported fallback when:

- The target org's owners will not approve a GitHub App install on
  the timeline you need (App installs require org-owner action;
  PATs are operator-account-scoped).
- The target is a small set of personal repositories where an App
  install is overkill.
- A short-lived integration test or canary run needs a credential
  with a known expiry date (fine-grained PATs expire by policy;
  App tokens are infinite-life until you delete the App).

PATs have a materially different audit shape: every call attributes
to the operator's **personal account**, not to an App identity.
That crosses the agent-vs-operator boundary every other meho
connector preserves; reach for the App path whenever the org's
governance allows it.

### Step P1 — Mint the fine-grained PAT

1. As the GitHub user whose account will own the token, navigate to
   **Settings** → **Developer settings** → **Personal access tokens**
   → **Fine-grained tokens** → **Generate new token**, or jump to
   `https://github.com/settings/personal-access-tokens/new`.
2. **Token name:** `meho-<environment>-<target>` (e.g.
   `meho-lab-github-meho`). Names are visible to org owners
   when they audit token-driven actions.
3. **Expiration:** the shortest window your rotation cadence allows
   (90 days is the GitHub default ceiling; longer requires a
   custom policy on the org).
4. **Resource owner:** the target user / org.
5. **Repository access:** **Only select repositories** with the
   exact set in scope. **All repositories** widens the blast radius
   unnecessarily.
6. **Repository permissions:** mirror the scope picker above —
   `contents:read, issues:read, pull_requests:read, actions:read,
   metadata:read` for read-only; add the write tier when needed.
7. **Generate token.** Copy the token immediately —
   GitHub will not show it again.

### Step P2 — Store the PAT in Vault

```bash
vault kv put secret/<tenant>/<target>/github-app \
  token='ghp_<token-value>'
```

Note the field name is `token`. The presence of `token` (instead
of `app_id` + `private_key` + `installation_id`) is the
connector's credential-format discriminator — the credential
loader picks the PAT code path when the secret contains a
`token` field.

Verify without leaking the value:

```bash
vault kv get -format=json secret/<tenant>/<target>/github-app \
  | jq '.data.data | {token_present: (.token | length > 0)}'
```

### Step P3 — Register the target

The `targets.yaml` row is identical to the App variant — the
operator does not need to declare the credential format on the
target row:

```yaml
targets:
  - name: github-meho-pat
    product: gh
    host: api.github.com
    fqdn: api.github.com
    secret_ref: <tenant>/github-meho-pat/github-app
    auth_model: shared_service_account
    notes: |
      PAT fallback (operator operator@example.com). Expires 2026-08-25.
      Tier-1 read-only scope on evoila/meho.
```

### Step P4 — Rotate the PAT before expiry

Fine-grained PATs expire on a wall-clock date. Rotate ≥1 week
before the expiry:

1. Mint a new PAT per Step P1 with the same scope set.
2. `vault kv put secret/<tenant>/<target>/github-app token='<new>'`.
3. MEHO keeps the PAT in memory. It reads Vault again when GitHub
   rejects the old PAT with `401`, or after any change to the
   target row. To switch at once, update the target or restart the
   backplane pods.

## Verification

Three checks. Run them after applying the recipe and before
considering the target "ready for first-day on-ramp". Check 1 proves
the meho side (target row + connector registered); Check 2 proves
the GitHub side (App or PAT can actually reach the API); Check 3
proves the end-to-end contract.

### Check 1 — `gh-rest-3` connector + target registered

Confirm both halves of the substrate exist:

```bash
# The connector itself.
meho connector list | grep gh-rest-3
# Expected: a row with `connector_id=gh-rest-3` and
# `review_status=enabled`.

# The target row.
meho targets describe github-meho
# Expected output (abbreviated):
#   name:        github-meho
#   product:     gh
#   host:        api.github.com
#   secret_ref:  <tenant>/github-meho/github-app
#   auth_model:  shared_service_account
```

`connector list` shows no `gh-rest-3` row, or it is not `enabled`
→ the `gh/3` catalogue has not been ingested and enabled in this
tenant yet. That is normal before the first-day on-ramp; see
[`github-connector.md` § Step 4](./github-connector.md#step-4--ingest-the-catalog).

`targets describe` returns `404 target not found` → re-run
`meho targets import targets.yaml` and confirm the YAML loaded
without errors.

### Check 2 — Probe the target

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
    "app_slug": "meho-prod"
  }
}
```

The connector's `fingerprint()` call exercises the full credential
chain: Vault read → (App path: JWT mint → installation-token mint)
→ `GET /user/installations`. When the target `host` is
`api.github.com/repos/<owner>/<repo>`, the probe calls
`GET /repos/<owner>/<repo>` instead. A green probe confirms the
App / PAT is correctly wired end to end.

A failed probe shows `reachable: false`, with the code in
`extras.error_code`. The codes map to the
[Failure modes](#failure-modes) section below; the common ones at
first install:

- `github_app_not_installed` → Step 3 was skipped, installed the
  App on the wrong org / repo set, or Vault holds a wrong
  `installation_id`.
- `github_jwt_mint_failed` → the `private_key` in Vault is
  malformed (missing newlines, missing BEGIN line, wrong algorithm).
- `github_installation_token_mint_failed` → GitHub refused to mint
  the installation token (for example a revoked key).
- `github_ambiguous_vault_payload` → the Vault secret has neither
  all three App fields nor a `token`.

### Check 3 — Round-trip a real read op against a known PR

Once Check 2 is green, dispatch the first real read op. The
canonical first call is `gh.pr.get` against an arbitrary PR the
operator already knows the state of (e.g. PR #1193 in `evoila/meho`,
the v0.7.0 changelog roll-up):

```bash
meho operation call gh-rest-3 'GET:/repos/{owner}/{repo}/pulls/{pull_number}' \
  --target github-meho \
  --params '{"owner":"evoila","repo":"meho","pull_number":1193}' \
  --json \
  | jq '{number, state, title, merged_at, mergeable_state}'
```

Expected:

```json
{
  "number": 1193,
  "state": "closed",
  "title": "docs(changelog): roll [Unreleased] → [0.7.0] - 2026-05-27",
  "merged_at": "2026-05-27T...",
  "mergeable_state": "clean"
}
```

A green Check 3 confirms: the App / PAT is installed on the right
repo set, the permission scope includes `pull_requests:read`, the
HTTP path resolves under the catalogue ingest, and the response
round-trips through the connector cleanly. From this point the
operator can dispatch any other enabled op from the catalogue.

> **Note on the ingest:** the `meho operation call` verb above needs
> the `gh/3` catalogue ingested and the op enabled (see
> [`github-connector.md`](./github-connector.md) Steps 4–6). Without
> the ingest the verb returns `unknown_op` for every ingested GitHub
> op_id. Checks 1 and 2 work without it, and so does
> `gh.composite.pr_status_summary`.

## Failure modes

The four credential failures below follow the T11 message-shape
convention
([`docs/codebase/error-message-shape.md`](../codebase/error-message-shape.md)):
each message starts with a stable `github_*` code so operator-side
runbooks can dispatch on it. The codes come from
`backend/src/meho_backplane/connectors/github/session.py`.

Where the code shows up:

- **`meho targets probe`** returns `reachable: false`, with the code
  in `extras.error_code` and the full message in `extras.error`.
- **`meho operation call`** returns `status: "error"` with
  `extras.error_code: connector_error`; `extras.exception_class`
  names the error class and `extras.exception_message` starts with
  the `github_*` code.

### `github_app_not_installed`

**What you see:**

```
github_app_not_installed: installation_id='987654321' is not reachable for this App — the App may not be installed on the target repo/org, or the installation_id in Vault is stale. ...
```

**What happened:** the App exists and the private key is valid (the
JWT was signed), but GitHub answered `404` on
`POST /app/installations/{installation_id}/access_tokens`. The App is
not installed on the target account, or the `installation_id` in
Vault is wrong.

**Fix:** revisit [Step 3](#step-3--install-the-app-on-the-target-repo-or-org).
Open the App's settings page in GitHub, click **Install App**, pick
the target org, grant access to the target repos, and write the
`installation_id` from the URL into Vault. Re-run Check 2.

### `github_jwt_mint_failed`

**What you see:**

```
github_jwt_mint_failed: cannot sign JWT for app_id='123456' — check the Vault-stored RSA private key (PEM) is well-formed and matches the App's configured public key. ...
```

**What happened:** the private key in Vault is malformed. Common
causes:

- The shell expansion in Step 4 dropped the `@` prefix, so the
  literal string `@/path/to/...` landed in the `private_key` field
  instead of the PEM body.
- The PEM was pasted with line endings stripped (CRLF → LF
  conversion, single-line paste from a chat client).
- The wrong algorithm — GitHub Apps use RSA SHA-256
  (RS256); ED25519 / EC private keys fail to sign.

**Fix:** re-run Step 4 with the `@` prefix and confirm the verify
command in Step 4 shows a `private_key_present: true` row. Rotate
the key via [Step 6](#step-6--rotate-the-private-key-when-needed)
if the original key file was lost.

### `github_installation_token_mint_failed`

**What you see:**

```
github_installation_token_mint_failed: POST https://api.github.com/app/installations/987654321/access_tokens returned HTTP 401. Body excerpt: '...'. ...
```

**What happened:** the JWT was signed, but GitHub refused to mint
the installation token with a status other than `404` (or the call
did not reach GitHub at all). Common causes: the private key was
revoked or belongs to another App, the App is suspended, or a
network / TLS failure to `api.github.com`.

**Fix:** check the HTTP status and body excerpt in the message.
Generate a new private key ([Step 6](#step-6--rotate-the-private-key-when-needed))
if the key was revoked; check network access to `api.github.com`
for a transport failure.

**Missing permission is a different error.** When the App lacks a
permission (for example the Tier-1 read-only set, and an agent
tries a Tier-2 write op), the token mint works and GitHub refuses
the call itself with `403`. MEHO returns `connector_http_403`; its
`extras.permission_headers` echoes the permission GitHub wants
(`X-Accepted-GitHub-Permissions`) when GitHub sends it. Fix: open the
App's permissions page in GitHub, add the missing permission, **and
then accept the permission upgrade in the installation** (GitHub
requires explicit consent on the installation, separate from the
App-level permission change — the App owner sees a banner saying
"Approve new permissions"; clicking through is mandatory before the
new scope takes effect).

### `github_rate_limited`

**What you see:**

```
github_rate_limited: api.github.com returned HTTP 403 with X-RateLimit-Remaining: 0 (resets at unix=1779894000). Wait for the reset window or reduce per-target call volume. ...
```

**What happened:** the rate limit was exhausted while MEHO minted an
installation token. The per-installation limit is 5000
requests/hour for App tokens and 5000/hour for fine-grained PATs.
When the limit hits during an operation call instead, GitHub's `403`
comes back as `connector_http_403` and a `429` as `connector_error`
with `extras.http_status: 429`.

**Fix:** wait until the reset time, then retry. If the limit hits
during normal operation rather than a runaway loop, the catalogue's
dispatch pattern needs auditing — most read paths should run well
under 5000/hour. Composites that fan out (e.g.
`gh.composite.pr_status_summary` makes 3 GitHub calls per dispatch)
multiply the per-call count; budget accordingly.

## Audit story

Every dispatch through the connector lands an `audit_log` row keyed
by the operator's Keycloak `sub` (the operator's identity), the
target name (`github-meho`), the op_id, and the result status. The
GitHub-side audit (the **App identity's** activity, or the **PAT
owner's** activity) is a separate forensics surface that combines
with the meho audit log to reconstruct the full chain.

Query the meho-side audit log after a dispatch:

```bash
meho audit query --target github-meho --since 1h
```

Expected (abbreviated):

```
AUDIT_ID      TIME                  TENANT  PRINCIPAL              TARGET       OP_ID                                           CLASS  STATUS
8c3f...d2a1   2026-05-27T14:31:02Z  prod    operator@example.com   github-meho  gh.composite.pr_status_summary                  read   ok
```

For a write op that went through the approval queue:

```bash
meho audit query --target github-meho --op-class write --since 24h
```

The run row of an approved call carries the approver as its
principal, and its `parent_audit_id` points to the approval request
row. List the request's decision and run rows with:

```bash
meho audit query --parent-audit-id <approval-request-audit-id>
```

This chain answers: who asked (the request row), who approved (the
decision and run rows), when MEHO ran it, and what GitHub's own
audit log will show (the App identity's `meho-prod` actor on the
issue-creation event). All three identities — agent, operator,
App — are pinned and queryable.


## Why a separate GitHub App (and not the operator's PAT)

- **Per-installation scoping.** An App is installed on a specific
  set of repos / an org; a PAT is account-scoped (every repo the
  operator can reach is in scope, including the operator's
  side-projects). The App is a narrower blast radius by construction.
- **Refreshable, short-lived tokens.** App installation tokens
  expire after 1 hour; the connector re-mints them automatically.
  A PAT lives for the operator-picked expiry (≤90 days by GitHub
  default policy), at which point every dispatch breaks until the
  rotation completes. The App path has no rotation cliff.
- **Machine-identity audit shape.** Every API call attributes to
  the App identity (`meho-prod`) in GitHub's audit log. A PAT
  attributes to the operator's personal account — every call looks
  like the operator personally clicking through the UI. That
  crosses the agent-vs-operator boundary every other meho connector
  preserves; the audit story becomes harder to reconstruct on the
  GitHub side when the actor is a human-looking identity that may
  also be doing actual UI work in parallel.
- **No tie to a personal account.** An operator leaving the
  organization invalidates their PATs immediately; the App keeps
  running. PATs are a personal-secrecy bond that the org cannot
  centrally rotate.
- **Higher rate limit ceiling**. GitHub's per-installation App
  token rate is 5000/hour by default; the org-wide aggregate ceiling
  scales with installation count. A PAT shares the user's personal
  account ceiling (5000/hour total across every tool that account
  has tokens for).

The cost of an App is one extra Settings-page object + Vault entry.
The benefit is each meho-driven action on GitHub is attributable to
the deployment, not to whichever operator happened to mint the
fallback PAT.

## Status

| Item | Side | State |
| --- | --- | --- |
| Recipe (this doc) | producer | landed (T2) |
| `GitHubRestConnector` reads App ID + private key + installation ID (or a PAT) from Vault | producer | landed (T1) — `backend/src/meho_backplane/connectors/github/session.py` |
| `gh/3` catalogue ingested + dispatchable | producer | landed (T3, with the parser fix in T7) |
| Operator-side runbook ties this credential doc into the full first-day on-ramp | producer | landed (T6) — [`./github-connector.md`](./github-connector.md) |
| `meho-prod` App provisioned on the `evoila` org | consumer | pending — applied by the dogfooding lab operator before standing up `github-meho` |
| Vault path `secret/<tenant>/github-meho/github-app` populated | consumer | pending — same operator step |
| Check 3 round-trips against a known PR | consumer | pending — the closing-comment artefact on Initiative #1220 |

## References

- Parent Initiative: [#1220 — G3.11 github-rest typed connector](https://github.com/evoila/meho/issues/1220) — first GitHub REST surface under Goal #214
- Parent Goal: G-connector-parity — operators progressively retire local wrappers
- Sibling Task — code: T1 — `GitHubRestConnector` + credential loader (`backend/src/meho_backplane/connectors/github/`)
- Sibling Task — first-day on-ramp recipe: T6 — [`./github-connector.md`](./github-connector.md)
- Sibling Task — catalogue ingest: [T3 #1223](https://github.com/evoila/meho/issues/1223) — `gh/3` Layer-2 ingest acceptance
- Sibling Task — write-op annotation: [T5 #1225](https://github.com/evoila/meho/issues/1225) — `requires_approval=true` on the 4 write ops
- Companion shape: [`./keycloak-web-client.md`](./keycloak-web-client.md) — the v0.7.0 G10.0 client recipe this doc mirrors in shape
- Companion shape: [`./keycloak-agent-client.md`](./keycloak-agent-client.md) — the agent-runtime confidential client recipe
- Per-target Vault read precedent: [`./connector-vault-policy.md`](./connector-vault-policy.md) — the ACL templating contract every per-target secret read flows through
- Target row layout: [`./targets-yaml.md`](./targets-yaml.md) — `name`, `aliases`, `product`, `host`, `secret_ref`, `auth_model` column conventions
- Connector-ingestion runbook: [`./connector-ingestion.md`](./connector-ingestion.md) — the broader operator workflow this credential doc plugs into
- GitHub Apps documentation: <https://docs.github.com/en/apps/creating-github-apps>
- GitHub fine-grained PAT documentation: <https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens>
- GitHub App authentication (JWT minting + installation tokens): <https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation>
- GitHub REST API rate limits: <https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api>
- Error-message-shape convention: [`docs/codebase/error-message-shape.md`](../codebase/error-message-shape.md) — T11 stable-code error envelope
- Consumer feature request: the consumer's request for a GitHub typed connector (`gh-rest`).
