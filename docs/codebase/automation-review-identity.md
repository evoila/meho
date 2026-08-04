<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Automation review identity — the `meho-review` GitHub App

## Overview

The autonomous review skills (`/auto-review-pr`, driven by the
`/auto-implement-*` family in `evoila-bosnia/meho-internal`) post a
binary APPROVE / REQUEST_CHANGES verdict on `evoila/meho` PRs. Those
PRs are authored by a maintainer's identity, and GitHub refuses a
formal review on your own PR — so without a second identity the
verdict degrades to a comment and `reviewDecision` never leaves
`REVIEW_REQUIRED` (#2733).

The fix is a dedicated **GitHub App** the review skills authenticate as
when submitting the formal review. The App is a real second party:
its `APPROVE` flips `reviewDecision` to `APPROVED`, satisfying branch
protection's `required_approving_review_count: 1` honestly.

Decision record (why an App and not a PAT machine user, and the DCO
choice): [`docs/decisions/automation-review-identity.md`](../decisions/automation-review-identity.md).

Status: the repo-side contract below is landed; the App itself is
provisioned by an org admin via the runbook at the end of this doc.
Until that happens the skills run permanently in degraded mode (see
"Degraded mode" below).

## The identity

| Property | Value |
|---|---|
| Kind | GitHub App (org-owned, `evoila`) |
| Recommended name / slug | "MEHO Review" / `meho-review` |
| Attribution on PRs | `meho-review[bot]` |
| Webhook | none (the App is a credential, not a service) |
| Installed on | `evoila/meho` only |
| Token kind | installation access token, expires after 1 hour |

Permissions (least privilege):

| Permission | Level | Why |
|---|---|---|
| Pull requests | Read and write | submit `APPROVE` / `REQUEST_CHANGES` reviews and inline review comments |
| Issues | Read and write | `auto-parked` label + PR-level comments (both go through the Issues API) |
| Contents | Read-only | the App never pushes; commits stay under the maintainer identity |
| Metadata | Read-only | mandatory baseline for every App |

The App authors **no commits**. DCO therefore never applies to it;
commit sign-off discipline (`git commit -s`) is unchanged, and the
repair path for a missed trailer is a remediation commit per
[`.github/dco.yml`](../../.github/dco.yml) — see
[CONTRIBUTING.md](../../CONTRIBUTING.md), "Developer Certificate of
Origin".

## Secret custody

Two secrets exist, both operator-held (never in the repo, never in CI
secrets — the skills run on operator machines):

| Secret | Where it lives | Notes |
|---|---|---|
| App private key (PEM) | 1Password item `meho-review-app`, field `private-key` | generated in the App's settings page; GitHub keeps no copy |
| Client ID | same item, field `client-id` | not secret in itself, stored alongside for one-stop reads |

Optional convenience fields on the same item: `app-id`,
`installation-id` (the mint script auto-discovers the installation id,
so this is cache, not contract).

Canonical read pattern (see the `op-cli` conventions — no secret values
on command lines):

```bash
op read "op://<vault>/meho-review-app/private-key" | \
  scripts/setup/mint-review-app-token.sh \
    --client-id "$(op read --no-newline "op://<vault>/meho-review-app/client-id")" \
    --key-file -
```

## Token flow

[`scripts/setup/mint-review-app-token.sh`](../../scripts/setup/mint-review-app-token.sh)
turns the App credentials into a short-lived installation token:

1. Builds an RS256-signed JWT (`iss` = client ID, `iat` = now − 60 s,
   `exp` = now + 9 min — inside GitHub's 10-minute cap).
2. Discovers the installation with
   `GET /repos/<owner>/<repo>/installation` (default repo
   `evoila/meho`, override with `--repo`).
3. Mints the token with
   `POST /app/installations/{installation_id}/access_tokens`.
4. Prints **only** the token to stdout; all diagnostics go to stderr;
   any failure exits non-zero with a specific reason (missing
   dependency, unreadable key, JWT rejected, App not installed).

The token expires after 1 hour — mint per review session, never store.

Usage: scope the token to the single mutation, keep every read on the
normal identity (installation tokens can hit "Resource not accessible
by integration" on GraphQL reads, and `gh pr view` and friends use
GraphQL):

```bash
MEHO_REVIEW_APP_TOKEN=$(op read "op://<vault>/meho-review-app/private-key" | \
  scripts/setup/mint-review-app-token.sh \
    --client-id "$(op read --no-newline "op://<vault>/meho-review-app/client-id")" \
    --key-file -)

# Formal review — the ONLY calls that use the App token:
GH_TOKEN="$MEHO_REVIEW_APP_TOKEN" gh pr review <n> --repo evoila/meho \
  --approve --body-file /tmp/review-summary.md
# (or --request-changes; inline comments via
#  GH_TOKEN=... gh api repos/evoila/meho/pulls/<n>/comments ...)
```

## How the skills select the identity

Contract implemented by
`evoila-bosnia/meho-internal/.claude/skills/auto-review-pr/SKILL.md`
(review posting) and consumed by the orchestrators' merge gates:

1. **Machine token present** (`MEHO_REVIEW_APP_TOKEN` set, or mintable
   from the 1Password item via the script above) → post the review
   formally under the App identity. `reviewDecision` flips; this is
   the normal path.
2. **Machine credential absent or invalid** → the mint step fails
   loudly (non-zero exit, stderr reason). The skill then **degrades
   explicitly**: it posts the verdict as a plain comment that opens
   with a degraded-mode banner, records
   `review_mode: "degraded-comment"` in its result file, and the
   orchestrator's merge gate hands the merge to a human instead of
   expecting `reviewDecision=APPROVED`.
3. **Never** post a formal review attempt under the author identity
   (GitHub 422s it), and **never** post a prose verdict styled as an
   approval without the degraded banner.

## Degraded mode

Degraded mode is today's pre-#2733 behaviour, made explicit instead of
implicit. The comment the skill posts opens with:

> **DEGRADED MODE — machine review identity unavailable.** This
> verdict is advisory prose, **not** a GitHub review;
> `reviewDecision` is unchanged and branch protection is not
> satisfied by this comment.

followed by the normal review body. The result file's
`review_mode: "degraded-comment"` field is the machine-readable twin.

### Manual check for the fail-loud path

Covers #2733's "with the credential absent or invalid, the skills fail
loudly" criterion; run it whenever the token plumbing changes:

```bash
# 1. Absent credential: the mint script must exit non-zero with a
#    usage error, printing nothing to stdout.
scripts/setup/mint-review-app-token.sh; echo "exit=$?"   # expect exit=2, empty stdout

# 2. Invalid credential: a syntactically valid but unregistered key
#    must be rejected by GitHub, exit non-zero, empty stdout.
openssl genrsa 2048 2>/dev/null | \
  scripts/setup/mint-review-app-token.sh --client-id Iv1.deadbeef --key-file -
echo "exit=$?"                                            # expect non-zero, empty stdout

# 3. Degraded posting: run /auto-review-pr with no
#    MEHO_REVIEW_APP_TOKEN and no 1Password item reachable; verify the
#    posted comment opens with the degraded banner and
#    gh pr view <n> --json reviewDecision is unchanged.
```

## Rotation

- **Routine rotation** (or key compromise): App settings →
  "Private keys" → *Generate a private key*; update the
  `meho-review-app` 1Password item; **delete the old key** in the App
  settings. Deleting the key invalidates JWT minting immediately;
  already-minted installation tokens die within 1 hour on their own.
- **Immediate revocation** of a live token:
  `DELETE /installation/token` authenticated with that token.
- **Losing the key entirely** is recoverable: generate a new key in the
  App settings; nothing else changes (client ID and installation are
  stable).

## Provisioning runbook (org admin, one-time)

Every step below needs `evoila` org-admin rights; none can be executed
by automation. Record completion in the decision record's
"Provisioning record" section.

1. **Create the App** — <https://github.com/organizations/evoila/settings/apps/new>:
   - GitHub App name: `MEHO Review`
   - Homepage URL: `https://github.com/evoila/meho`
   - **Uncheck** "Active" under Webhook.
   - Repository permissions: Pull requests → *Read and write*;
     Issues → *Read and write*; Contents → *Read-only*.
     (Metadata: Read-only is added automatically.)
   - "Where can this GitHub App be installed?" → *Only on this account*.
   - Create.
2. **Note the Client ID** from the App's *General* page.
3. **Generate a private key** — *General* → "Private keys" →
   *Generate a private key*; a `.pem` downloads.
4. **Install the App** — *Install App* (left sidebar) → `evoila` →
   *Only select repositories* → `evoila/meho` → Install.
5. **Store the credentials** (then delete the downloaded `.pem`):

   ```bash
   # Per the op-cli template flow — do not put the key on a command line.
   # Item: meho-review-app, fields: client-id, private-key.
   # See .claude/skills/op-cli/SKILL.md for the create-via-template shape.
   op item get meho-review-app --vault <vault> >/dev/null 2>&1 || \
     echo "create item meho-review-app in <vault> with fields client-id, private-key"
   # Plain rm — secure-wipe flags are platform-specific (and moot on
   # modern filesystems); if the deletion worries you, rotate the key.
   rm ~/Downloads/meho-review.*.private-key.pem
   ```

6. **Smoke-test the mint path:**

   ```bash
   op read "op://<vault>/meho-review-app/private-key" | \
     scripts/setup/mint-review-app-token.sh \
       --client-id "$(op read --no-newline "op://<vault>/meho-review-app/client-id")" \
       --key-file - | wc -c    # expect a non-zero length, no errors
   ```

7. **Verify the two-party property on a real PR** (#2733 acceptance
   criterion 1): on any open PR authored by a maintainer,

   ```bash
   GH_TOKEN=$(op read "op://<vault>/meho-review-app/private-key" | \
     scripts/setup/mint-review-app-token.sh \
       --client-id "$(op read --no-newline "op://<vault>/meho-review-app/client-id")" \
       --key-file -) \
   gh pr review <n> --repo evoila/meho --approve --body "Provisioning smoke test (#2733)."
   gh pr view <n> --repo evoila/meho --json reviewDecision   # expect "APPROVED"
   ```

   (Dismiss the smoke-test review afterwards if the PR is not actually
   ready: PR → Reviews → *Dismiss review*.)

8. **Complete the "Provisioning record"** in
   [`docs/decisions/automation-review-identity.md`](../decisions/automation-review-identity.md)
   and close out #2733's remaining acceptance criteria.

## Known gaps

- Until the runbook above is executed, every automated review runs in
  degraded mode and merges keep needing a human click — unchanged from
  the pre-#2733 status quo, but now labelled honestly.
- `#2733` acceptance criteria 1–2 (live `reviewDecision` flip;
  unattended `--auto-merge` run) are provable only post-provisioning.

## References

- Task [#2733](https://github.com/evoila/meho/issues/2733); decision
  record [`docs/decisions/automation-review-identity.md`](../decisions/automation-review-identity.md).
- [Authenticating as a GitHub App installation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)
  (installation tokens, 1-hour expiry, installation-id discovery).
- [Generating a JWT for a GitHub App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)
  (RS256, `iss` = client ID, 10-minute `exp` cap, 60-second `iat`
  backdate).
- [dcoapp/app](https://github.com/dcoapp/app) — `.github/dco.yml`
  remediation-commit configuration.
- Skill-side wiring: `evoila-bosnia/meho-internal`,
  `.claude/skills/auto-review-pr/SKILL.md` (identity selection,
  degraded banner) and
  `.claude/skills/auto-implement-initiative/SKILL.md` (merge-gate
  handling of `review_mode`).
