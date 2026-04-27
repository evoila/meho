<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2026 evoila Group
-->

# CLA verification workflow

## Overview

Every pull request to `evoila-bosnia/MEHO.X` must be authored by a contributor who
has signed the [CLA](../../CLA.md). Enforcement is automated via
[.github/workflows/cla.yml](../../.github/workflows/cla.yml), which invokes the
pinned [`contributor-assistant/github-action@v2.6.1`](https://github.com/contributor-assistant/github-action/tree/ca4a40a7d1004f18d9960b404b97e5f30a505a08)
on every PR event and on every new issue comment. The action compares the PR
author against a persisted signatures file; if the author is not present, it
leaves a comment asking them to sign; after they reply with the sign-off text,
it records their signature and flips the `CLA Verification` status check to
green.

The signatures are the load-bearing piece of state this workflow manages, and
where they live is the subject of the architectural decision documented below.

## Key files

- [.github/workflows/cla.yml](../../.github/workflows/cla.yml) — the workflow
  that drives CLA enforcement. Triggers on `pull_request_target` (opened, closed,
  synchronize) and on `issue_comment` (created), calls the pinned action, and
  relies on the repo's default `GITHUB_TOKEN` for all writes.
- [CLA.md](../../CLA.md) — the legal text contributors agree to. Referenced by
  the action via `path-to-document`. Lives on `main` and ships to `evoila/meho`
  via the public mirror allowlist.
- `cla-signatures` branch — dedicated, non-protected branch holding
  `signatures/cla.json`. Created once (out of band) and written to by the
  action on every new signer. Not mirrored to `evoila/meho`.
- [`signatures/cla.json`](../../../../blob/cla-signatures/signatures/cla.json) —
  the signatures file itself. JSON array, one entry per signer, appended by
  the action. Lives only on `cla-signatures`; never on `main`.

## Control flow

1. A contributor opens a PR on `main`.
2. `cla.yml` fires on the `pull_request_target` event. Because the trigger is
   `pull_request_target` and not `pull_request`, the workflow runs in the
   context of `main`, not the PR's head commit — untrusted PR code cannot read
   repo secrets or elevate workflow permissions.
3. The action loads `signatures/cla.json` from the `cla-signatures` branch
   (specified by the `branch` input).
4. If the PR author's GitHub login is in that file, the `CLA Verification`
   check goes green and the workflow exits.
5. If not, the action posts a comment on the PR asking the contributor to reply
   with the sign-off text `I have read the CLA Document and I hereby sign the CLA`,
   and the check stays red.
6. When the contributor posts the sign-off reply, `cla.yml` fires again on
   `issue_comment`, this time with `github.event.comment.body` matching the
   sign-off literal. The action appends a new entry to `signatures/cla.json` on
   `cla-signatures`, pushes the update using `GITHUB_TOKEN`, and flips the
   check green.
7. On every subsequent PR from the same contributor, step 4 matches and the
   check passes without further interaction.

Accounts in the `allowlist` input (`dependabot[bot]`, `renovate[bot]`,
`meho-mirror-bot`) skip this flow entirely — they are treated as pre-signed and
never prompted. The allowlist is a short-circuit at the top of the action, not
a modification of `signatures/cla.json`.

## Why signatures live on a separate branch

The action writes `signatures/cla.json` via GitHub's Contents API
(`PUT /repos/.../contents/{path}`), which is treated by the platform as a
direct push to the target branch. `main` is branch-protected with
`required_pull_request_reviews`, so every Contents API write to `main` fails
with:

```
Changes must be made through a pull request.
```

The pinned action version (`ca4a40a7…`, v2.6.1) has no mode for opening a PR
on the contributor's behalf — verified against the `src/persistence/persistence.ts`
implementation, which assumes the target branch is writable and fails hard
otherwise. Attempts to bypass protection with a fine-grained or classic PAT
(even one owned by a repository admin listed in `bypass_pull_request_allowances`)
fail at the platform level: Contents API writes from PATs are subject to branch
protection regardless of bypass-list membership. The record of failed attempts
lives in bug [#328](../../../../issues/328) and PR [#365](../../../../pull/365).

The implementable fix is to move the write target to a branch that is not
protected. `cla-signatures` is a dedicated, non-protected branch that holds
only `signatures/cla.json`; `main` is untouched by the action's writes, and
branch protection on `main` remains fully intact.

The action's own docs name this pattern directly: *"make sure the branch where
signatures are stored is NOT protected."*

## Authentication

The workflow relies on the default `GITHUB_TOKEN` that GitHub Actions mints for
each workflow run. No PAT is involved.

The required permissions are declared at the top of `cla.yml`:

```yaml
permissions:
  actions: write
  contents: write
  pull-requests: write
  statuses: write
```

`contents: write` is what allows `GITHUB_TOKEN` to push to `cla-signatures`.
Because `cla-signatures` is not protected, no bypass entry is needed —
`required_pull_request_reviews` does not apply to branches without a protection
rule.

The action prefers a `PERSONAL_ACCESS_TOKEN` env var over `GITHUB_TOKEN` when
both are set. The workflow deliberately does **not** pass a
`PERSONAL_ACCESS_TOKEN`, which forces the action onto the default token. This
eliminates the PAT rotation surface the pre-Option-B design carried.

## Public-side enforcement

`.github/workflows/cla.yml` is mirrored to `evoila/meho` via the
[public allowlist](../../.github/workflows/public-allowlist.txt). On a
public-repo PR, the same workflow runs in the public repo's context.
`contributor-assistant/github-action` resolves the `branch` input against
`context.repo` when the `remote-organization-name` and `remote-repository-name`
inputs are unset (verified in
[`src/persistence/persistence.ts`](https://github.com/contributor-assistant/github-action/blob/ca4a40a7d1004f18d9960b404b97e5f30a505a08/src/persistence/persistence.ts):
`owner: input.getRemoteOrgName() || context.repo.owner`, with the same fallback
for `repo`). The public-side run therefore reads and writes
`signatures/cla.json` on **public's own** `cla-signatures` branch, not on the
private one.

Public signatures live in a dedicated, non-protected branch on `evoila/meho`,
same shape as the private side. No cross-repo PAT, no shared signature store,
no mirror-mode change, no allowlist edit. The default `GITHUB_TOKEN` minted
for each public-side workflow run carries `contents: write` and pushes
straight to `cla-signatures` because that branch is unprotected.

### Why mirror does not clobber this branch

[`.github/workflows/mirror-to-public.yml`](../../.github/workflows/mirror-to-public.yml)
force-pushes to public `main` only — orphan mode runs
`git push public mirror-staging:main --force`, incremental mode runs
`git push public mirror-incremental:main`. Neither path touches any other
ref on the public remote. `cla-signatures` on public is mirror-exempt by
construction, with no configuration to drift.

The `public-allowlist.txt` allowlist is a *file-tree* gate that controls
which repo-relative paths are copied into the staging tree before the push.
`signatures/cla.json` is never on `main` (private or public), so it is never
a candidate for mirroring regardless of allowlist contents.

### Bootstrap (one-time, per repo)

The public `cla-signatures` branch must exist before the first external PR,
or the first `CLA Verification` run 404s on the action's Contents-API read
of `signatures/cla.json`. Bootstrap as an orphan branch containing only
`signatures/cla.json = []`:

```bash
git clone https://github.com/evoila/meho.git /tmp/meho-cla-bootstrap
cd /tmp/meho-cla-bootstrap
git checkout --orphan cla-signatures
git rm -rf --cached .
rm -rf -- * .[!.]*
mkdir signatures
printf '[]\n' > signatures/cla.json
git add signatures/cla.json
git commit -m 'chore(cla): bootstrap public signatures branch'
git push -u origin cla-signatures
```

Confirm the new branch is unprotected:

```bash
gh api /repos/evoila/meho/branches/cla-signatures --jq '.protected'
# -> false
```

This mirrors the private-side `cla-signatures` bootstrap procedure by intent,
not by force-push: the private and public `cla-signatures` branches are
independent histories from day one.

### Verification

- Open a throwaway PR on `evoila/meho` from a non-allowlisted GitHub
  account, post the sign-off reply
  (`I have read the CLA Document and I hereby sign the CLA`), and confirm
  `CLA Verification` flips green and `signatures/cla.json` on
  `cla-signatures` gains the signer entry.
- Wait for the next private-main CI → mirror cycle, then open a second PR
  from the same account. It clears immediately without a second sign-off,
  confirming the mirror push to public `main` did not affect
  `cla-signatures`.

### Two signature stores

Private and public each maintain their own `cla-signatures` branch. A
contributor who signs on one repo is not automatically signed on the other.
This is an accepted operational cost of the chosen approach (Option (a)
from [Initiative #366](../../../../issues/366)) — the alternative options
each carried a worse tradeoff: a cross-repo PAT (Option (b) from
Initiative #366) lives in public secrets but writes to private; a hosted
GitHub App (Option (c) from Initiative #366) replaces the pinned action
with a service dependency. The cherry-pick flow in
[`docs/development/dual-repo-workflow.md`](../development/dual-repo-workflow.md#handling-external-prs)
carries code from public back to private; signatures are not migrated.

## Known issues

- **Don't protect `cla-signatures` on either repo**: if a future admin
  accidentally applies a branch protection rule to `cla-signatures`, every
  new signature write will fail with the same
  `Changes must be made through a pull request.` error that originally
  motivated the split. The branch must stay unprotected on both
  `evoila-bosnia/MEHO.X` and `evoila/meho`.
- **Don't rename or delete the branch on either repo**: the `branch:` input
  in `cla.yml` and the contents of `cla-signatures` are coupled. Renaming
  requires a coordinated workflow update; deleting (even accidentally via
  a `git push --delete origin cla-signatures` miskey) wipes every recorded
  signature on that side and can only be recovered from a clone that has
  the branch fetched. Recovery does not cross repos — a private clone with
  the branch fetched cannot recover public signatures and vice versa.
- **Don't hand-edit `signatures/cla.json`**: the action is the only writer. A
  hand edit that drifts from the action's expected schema will break future
  appends.
- **`path-to-document`** still points at `evoila/meho`'s public `main` — CLA.md
  renders for contributors on the public repo, even though the action itself
  runs on the private repo. Don't repoint this at a branch that isn't served
  via a stable public URL.

## References

- Parent bug: [#328](../../../../issues/328) — diagnosis and option analysis.
- Failed Option A implementation: [PR #365](../../../../pull/365) — empirical
  evidence that PAT-based bypasses don't work at the current platform behavior.
- Execution task for this doc: [#368](../../../../issues/368) — Option B
  implementation.
- Action source (pinned): [contributor-assistant/github-action @ ca4a40a7](https://github.com/contributor-assistant/github-action/tree/ca4a40a7d1004f18d9960b404b97e5f30a505a08).
- Action target-repo resolution:
  [`src/persistence/persistence.ts` @ pinned SHA](https://github.com/contributor-assistant/github-action/blob/ca4a40a7d1004f18d9960b404b97e5f30a505a08/src/persistence/persistence.ts) —
  shows the `context.repo` fallback used by the public-side enforcement.
- Public-side CLA enforcement: [Initiative #366](../../../../issues/366),
  [Task #367](../../../../issues/367).
- Related public-mirror mechanics: [public-mirror.md](public-mirror.md).
