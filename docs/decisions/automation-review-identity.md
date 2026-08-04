<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Automation review identity — GitHub App reviewer + DCO remediation commits (decision)

**Status:** decided (repo side landed); **org-admin provisioning routed for
human execution** (see "Provisioning record" below — deliberately not filled
by this record)
**Date:** 2026-08-04
**Goal:** [#221](https://github.com/evoila/meho/issues/221)
**Task:** [#2733](https://github.com/evoila/meho/issues/2733) (this decision +
the accompanying config and docs)

## The matter this records

The autonomous implement/review skills run under a maintainer's own GitHub
identity — the same identity that authors the PRs they review. GitHub
refuses `APPROVE` / `REQUEST_CHANGES` on your own pull request (HTTP 422),
so every automated review degrades to a `COMMENT`-state review carrying a
prose `**Verdict:**` line. Verified across the whole of Initiative #2716
(PRs #2724, #2726, #2728, #2729, #2730) and the earlier #2710–#2715
series: every PR ended `mergeable=MERGEABLE`, all checks green, and
`mergeStateStatus=BLOCKED` purely on `reviewDecision=REVIEW_REQUIRED`.

Consequences of the status quo:

1. **The two-party review property is not actually held.** Branch
   protection asks for one approving review; what exists is a comment
   written by automation running as the author. The manual merge click is
   the only real second-party step, and it is not a review.
2. `--auto-merge` on the orchestration skills can never complete a run
   unattended.
3. A reader skimming the PR sees "Verdict: APPROVE" and may reasonably
   believe the PR carries an approval it does not.

An adjacent symptom from the same root: commits created by automation
lack `Signed-off-by` unless every worker remembers `git commit -s`, and
with no `.github/dco.yml` the DCO app's remediation-commit path was
disabled — two PRs in #2716 needed a branch rewrite and force-push to
repair a missing trailer.

## Decision 1 — reviewer identity: GitHub App, not a PAT machine user

**Decided: a dedicated GitHub App** (recommended name "MEHO Review", slug
`meho-review`, so reviews attribute to `meho-review[bot]`) is the identity
the review skills authenticate as when posting a formal review verdict.

Considered and rejected — **PAT-backed machine user**:

- A machine user consumes an org seat and carries a long-lived personal
  access token; the token is a standing credential with no built-in
  expiry.
- App installation tokens are short-lived (1 hour), minted on demand
  from a private key, scoped to a single installation, and attribute
  cleanly as `<slug>[bot]` in the PR timeline.
- Source: [Authenticating as a GitHub App installation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)
  (`POST /app/installations/{installation_id}/access_tokens`, 1-hour
  expiry).

Properties the App identity restores:

- `APPROVE` / `REQUEST_CHANGES` submissions succeed (the reviewer is not
  the PR author), so `reviewDecision` reflects the automated review's
  actual verdict and branch protection's
  `required_approving_review_count: 1` is satisfied honestly.
- The PR author identity (maintainer) and the reviewer identity (App) are
  distinct accounts — GitHub's self-review guard is satisfied, not
  routed around.

Minimum permissions (least privilege): **Pull requests: Read and write**
(submit reviews), **Issues: Read and write** (labels such as
`auto-parked` and PR comments go through the Issues API),
**Contents: Read-only** (the App pushes nothing), Metadata: Read-only
(mandatory). No webhook.

## Decision 2 — DCO: sign-off stays mandatory; remediation commits enabled

The task offered a choice: have the machine identity sign its commits
off automatically, or land `.github/dco.yml` enabling remediation
commits. **Decided: both halves of the existing discipline stay, plus
the repair path lands:**

- `git commit -s` remains mandatory for every commit, human or
  automation (CONTRIBUTING.md, "Developer Certificate of Origin"). The
  review App authors no commits at all — it only posts reviews — so
  "the App signs its commits" is moot by design.
- [`.github/dco.yml`](../../.github/dco.yml) lands with
  `allowRemediationCommits.individual: true`: a commit that slipped
  through without its trailer is repaired by a follow-up remediation
  commit (per DCO 1.1), never again by rewriting a published branch.
- `thirdParty` remediation stays **disabled** (least surface), and
  `require.members: false` is deliberately **not** set — exempting org
  members would weaken the DCO property this repo relies on.
- Config schema source: [dcoapp/app](https://github.com/dcoapp/app)
  (reads `.github/dco.yml` from the default branch;
  `allowRemediationCommits.individual` / `.thirdParty`).

## Decision 3 — fail-loud degraded mode, never a fake approval

When the machine credential is absent or invalid, the review skills must
**fail loudly and degrade explicitly**:

- The formal review submission is skipped (it would 422 anyway under the
  author identity); the verdict is posted as a plain comment that opens
  with an explicit **degraded-mode banner** stating that it is *not* a
  GitHub approval and that `reviewDecision` is unchanged.
- The machine-readable result file records
  `review_mode: "degraded-comment"` so orchestrators know the merge gate
  cannot pass on `reviewDecision` and must hand off to a human instead
  of parking the PR as failed.
- Under no circumstances does automation post a prose verdict styled to
  read like a GitHub approval without the banner.

The full contract — identity selection order, custody, rotation, the
manual fail-loud check — lives in
[`docs/codebase/automation-review-identity.md`](../codebase/automation-review-identity.md).

## What this record does not decide

- **Org-admin provisioning** (creating the App under the `evoila` org,
  installing it on `evoila/meho`, generating and storing the private
  key) is a human action. The copy-pasteable runbook is in
  `docs/codebase/automation-review-identity.md`; the completion is
  attested below.
- Acceptance criteria #1 and #2 of #2733 (a real PR's `reviewDecision`
  flipping; an unattended `--auto-merge` run) are verifiable only after
  provisioning; they stay open on the task until then.

## Consequences

- Repo side (this decision's PR): `.github/dco.yml`,
  `docs/codebase/automation-review-identity.md` (identity, permissions,
  custody, rotation, runbook, degraded mode),
  [`scripts/setup/mint-review-app-token.sh`](../../scripts/setup/mint-review-app-token.sh)
  (installation-token minting, fail-loud), this record.
- Skill side (`evoila-bosnia/meho-internal`,
  `.claude/skills/auto-review-pr/SKILL.md` and
  `auto-implement-initiative/SKILL.md`): the COMMENT-state fallback is
  described as an explicitly degraded mode, and the formal-review path
  authenticates via the App token when present.
- Once provisioning completes, the "verdict-of-record overlay" the
  orchestration skills apply for the self-review 422 case deletes
  itself: formal `reviewDecision=APPROVED` is required again
  (`.claude/skills/auto-implement/SKILL.md`, "Merge-gate reality").

## Provisioning record

**This section is deliberately left for the org admin to complete** after
executing the runbook in
[`docs/codebase/automation-review-identity.md`](../codebase/automation-review-identity.md).

- **App created (name / slug):** _(to be completed)_
- **Installed on:** _(to be completed — expected `evoila/meho`)_
- **Secret custody location:** _(to be completed — expected 1Password
  item `meho-review-app`)_
- **Provisioned by / date:** _(to be completed)_
- **AC verification (reviewDecision flipped on a real PR):** _(link to
  the PR — to be completed)_

Until this section is completed, the identity decision stands as
**decided and routed**, not provisioned.

## References

- Task [#2733](https://github.com/evoila/meho/issues/2733); observed
  failure runs: Initiative #2716 (PRs #2724, #2726, #2728, #2729,
  #2730), earlier #2710–#2715.
- [Authenticating as a GitHub App installation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)
  — installation access tokens, 1-hour lifetime.
- [Generating a JWT for a GitHub App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)
  — RS256, `iss` = client ID, `exp` ≤ 10 minutes.
- [dcoapp/app](https://github.com/dcoapp/app) — `.github/dco.yml`
  schema, remediation commits.
- Precedent record with a human-completion section:
  [`shipped-spec-provenance.md`](shipped-spec-provenance.md).
