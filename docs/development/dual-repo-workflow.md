# Dual-Repo Development Workflow

MEHO uses a dual-repository architecture: a private repo for development and a public repo for the open-source community.

## Repository Structure

| Repository | Visibility | Purpose |
|-----------|-----------|---------|
| `evoila-bosnia/MEHO.X` | Private | Development, planning, CI |
| `evoila/meho` | Public | Open-source distribution |

## How the Mirror Works

1. Developers push to `main` on the **private** repo (`evoila-bosnia/MEHO.X`)
2. CI runs on the private repo
3. If CI passes, the `mirror-to-public` GitHub Action triggers
4. The action assembles a staging tree containing **only** the paths listed in [.github/workflows/public-allowlist.txt](../../.github/workflows/public-allowlist.txt)
5. The staged tree is synced to the **public** repo (`evoila/meho`) using the configured mirror mode

## Whitelist, Not Blacklist

The mirror uses an explicit allowlist of paths to ship, not a list of paths to strip. **New top-level files and directories are private by default** — they will not appear in the public repo until you add them to `.github/workflows/public-allowlist.txt`.

This is intentional: a blacklist drifts every time someone adds a new private directory and forgets to update the strip list. A whitelist fails closed — a missing entry means the file is withheld, which is the safe direction.

### Adding a new public path

1. Edit `.github/workflows/public-allowlist.txt` and add the file or directory (one path per line, relative to repo root).
2. Open a PR on the private repo. Reviewers check the allowlist diff specifically for anything that shouldn't leak.
3. After merge, the next mirror run will include the new path.

Directories are mirrored recursively. For nested paths (e.g. a single file inside a private directory), list the nested path; the workflow preserves parent structure.

### Sanity checks

The workflow fails if any of these required paths are missing from the staged tree: `meho_app`, `pyproject.toml`, `README.md`, `LICENSE`. It also fails if any top-level path appears in the staging tree that is not covered by `.github/workflows/public-allowlist.txt` — the allowed set is derived from the allowlist itself, so there is no second list to maintain.

## Mirror Modes

The mirror workflow supports two modes controlled by GitHub variable `PUBLIC_MIRROR_MODE`:

- `orphan` (default): force-pushes a single snapshot commit to public `main`
- `incremental`: appends one normal commit per successful private `main` CI run

If `PUBLIC_MIRROR_MODE` is unset, behavior defaults to `orphan`.

## Switching to Incremental History

When ready to expose incremental public history:

1. Set private-repo Actions variable `PUBLIC_MIRROR_MODE=incremental`
2. Push/merge to private `main` and wait for CI + mirror workflow
3. Verify public `main` receives a normal commit on top of prior public `main` (no force rewrite)

## Rollback to Orphan Mode

If incremental mirroring needs to be reverted quickly:

1. Set `PUBLIC_MIRROR_MODE=orphan` (or remove the variable)
2. Next successful mirror run returns to snapshot force-push behavior

## Developer Rules

1. **Always push to the private repo** (`origin`)
2. **Never push directly to the public repo** -- the pre-commit pre-push hook installed by `scripts/dev-setup.sh` blocks this locally. The hook is per-clone, not server-side, so it only protects maintainers who have run the setup script. Treat the rule as policy backed by local tooling, not as an unconditional server-enforced block.
3. **Never commit `.planning/` files** to branches that will merge to main
4. **Run `scripts/dev-setup.sh`** after cloning to configure git hooks

## First-Time Setup

After cloning the private repo:
```bash
scripts/dev-setup.sh
```
This runs `pre-commit install --install-hooks`, wiring the pre-commit, pre-push, and commit-msg hooks -- including the public-remote push guard at `scripts/hooks/block-public-remote-push.sh`.

## Handling External PRs

When the community submits a PR to the public repo:
1. Review and approve on the public repo as normal
2. Merge on the public repo
3. Cherry-pick the merge commit back to the private repo:
   ```bash
   git remote add public https://github.com/evoila/meho.git
   git fetch public
   git cherry-pick <merge-commit-sha>
   ```
4. Push to private main -- the next mirror sync will be a no-op (changes already exist)

The cherry-pick carries code, **not** CLA signatures. Public and private
each maintain an independent `cla-signatures` branch — see
[Public-side CLA](#public-side-cla) below.

## Public-side CLA

External contributors on `evoila/meho` clear `CLA Verification` the same way private-side contributors do: they post the sign-off reply, the mirrored `.github/workflows/cla.yml` records their signature, and the check flips green. The public-side signatures live on `evoila/meho`'s own `cla-signatures` branch — separate from private's.

**No new secrets** are required on `evoila/meho`. The workflow uses the default `GITHUB_TOKEN` minted for each Actions run, which has `contents: write` on the non-protected `cla-signatures` branch. The mirror pipeline pushes to public `main` only, so `cla-signatures` on public is never overwritten.

The branch must be bootstrapped once before the first external PR — see [docs/codebase/cla-verification.md#bootstrap-one-time-per-repo](../codebase/cla-verification.md#bootstrap-one-time-per-repo) for the procedure and [docs/codebase/cla-verification.md#public-side-enforcement](../codebase/cla-verification.md#public-side-enforcement) for the full enforcement model.

## Emergency: Direct Push to Public

If the mirror is broken and you must push directly:
```bash
git push --no-verify public main
```
The `--no-verify` flag bypasses the pre-push hook. Use sparingly.

## Secret Management

| Secret | Location | Purpose |
|--------|----------|---------|
| `PUBLIC_REPO_PAT` | Private repo secrets | Mirror workflow pushes to public repo |
| `SONAR_TOKEN` | Both repos | SonarCloud quality gate |
| `CODECOV_TOKEN` | Both repos | Coverage reporting |

CLA signatures do not require a dedicated secret on either repo. The CLA workflow uses the default `GITHUB_TOKEN`, and writes target each repo's unprotected `cla-signatures` branch. See [Public-side CLA](#public-side-cla) and [docs/codebase/cla-verification.md](../codebase/cla-verification.md).

`PUBLIC_REPO_PAT` is a GitHub **classic** personal access token with `public_repo` scope. Classic tokens cannot be scoped to a single repository — migration to a fine-grained PAT or a GitHub App installation token is tracked as a v2.3+ consideration in #257. Maximum lifetime is 1 year; see the [PAT Rotation](#pat-rotation) section below for the rotation procedure and calendar cadence.

## What Gets Shipped

See [.github/workflows/public-allowlist.txt](../../.github/workflows/public-allowlist.txt) for the authoritative list. It is the single source of truth — the workflow does not contain any hardcoded "strip these" logic anymore.

Anything not on the allowlist stays private. Notable private-only top-level entries include `.planning/`, `.claude/`, `.cursor/`, `_archive/`, `meho-claude/`, `meho-website/`, `launch-content/`, `ui-design/`, and `test-scenarios-vcf/`.

## Release Owner Responsibilities

Every public release has a single named **release owner**. The release owner is accountable for:

1. **Executing the pre-release checklist** (below) before tagging.
2. **Authorizing rollbacks** — no one else runs `PUBLIC_MIRROR_MODE=orphan` or force-pushes without their explicit go-ahead.
3. **First-response for mirror incidents** — if something leaks, the release owner owns the incident playbook until the situation is contained.
4. **Owning the PAT rotation calendar** — `PUBLIC_REPO_PAT` rotation is tracked on the release owner's calendar, not in an anonymous shared reminder.

The release owner is typically the person cutting the release, but the role can be explicitly handed off in writing (Slack, email, PR comment) as long as the handoff is unambiguous.

## Pre-release Checklist

Run this checklist before tagging any public release. Each item must be a hard yes.

- [ ] **Install smoke passed.** On the release commit, with a local stack running (`docker compose up`), run `./scripts/validate-install.sh`. All 5 steps green. This catches the failure modes that only fire under a real LLM call — stale model IDs, missing embeddings backend, auth misconfiguration — before they land in evaluator hands. The script is deliberately separate from CI; the CI bootstrap smoke job is Goal #294's scope.
- [ ] **Allowlist diff reviewed.** Run `git diff main -- .github/workflows/public-allowlist.txt` since the last release. Every added line is explicitly approved by the release owner. Removed lines are checked against whether the file was truly withdrawn or accidentally dropped.
- [ ] **Local dry-run completed.** Run `scripts/assemble-public-tree.sh --dry-run` from a clean checkout of the release commit. The output is reviewed for surprises — unexpected files, missing files, or paths that should not ship.
- [ ] **CHANGELOG updated.** The release entry exists in `CHANGELOG.md` (see [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format) and references the version being shipped.
- [ ] **Release notes drafted.** The GitHub Release body is populated per [`docs/development/release-notes-template.md`](release-notes-template.md), with known limitations and migration notes filled in.
- [ ] **PAT probe green.** The `PUBLIC_REPO_PAT` expiration probe workflow (tracked in #320) is on its latest successful run. If the probe hasn't shipped yet, the release owner manually verifies the PAT is at least 60 days from expiry via GitHub → Settings → Developer settings → Personal access tokens.
- [ ] **No open `priority:p0` issues** on the current OSS-readiness milestone that would block the release.
- [ ] **CI green on the release commit.** Not "green on the merge commit that triggered the release" — green on the exact commit being shipped, verified in Actions.

If any item is a no, the release does not happen. Document the reason in the release notes draft and return to the checklist.

## Rollback Procedures

Three rollback cases, in order of severity. Always start with the least destructive option that actually fixes the problem.

### Orphan mode — bad content pushed

Default `PUBLIC_MIRROR_MODE=orphan` force-pushes every sync, so any bad public content is one private fix away from being overwritten.

1. Identify the bad content on public `main`.
2. Fix the underlying issue on **private** `main` — revert the offending commit, fix the allowlist, whatever is correct.
3. Wait for private CI to go green on the fix commit.
4. The `mirror-to-public` workflow triggers automatically on `workflow_run: CI` success and force-pushes the corrected tree.
5. Verify public `main` now reflects the fix. Commit SHA and file tree should match the local `scripts/assemble-public-tree.sh` output.

No manual force-push is needed. The orphan mode's force-push behavior **is** the rollback mechanism.

### Incremental mode — bad content pushed

If `PUBLIC_MIRROR_MODE=incremental` is set, public history is preserved and a force-push would rewrite that history. Prefer a revert.

1. On public `main`, create a revert commit: `git revert <bad-commit-sha>`.
2. Push the revert to public `main` **via a PR on the public repo**, not directly. The local pre-push hook from `scripts/dev-setup.sh` blocks direct pushes for any maintainer who has run that setup, but the hook is client-side only — the "no direct pushes" rule is enforced by policy and local tooling, not by the server. Do not work around it even if your own clone has no hook installed.
3. Merge the revert PR on public.
4. Cherry-pick the revert back to private `main` (same flow as the "Handling External PRs" section above) so the next mirror sync is a no-op instead of re-applying the bad content.
5. If the bad commit is truly unfixable and history rewrite is the only option, drop back to `PUBLIC_MIRROR_MODE=orphan`, push one snapshot, then decide whether to re-enable incremental afterwards. This is the nuclear option — document it in the incident postmortem.

### Leaked private content

**This is not a rollback. This is an incident.** Go to [Incident Playbook: Leaked Content to Public](#incident-playbook-leaked-content-to-public) immediately and come back here only if the playbook tells you to.

## PAT Rotation

`PUBLIC_REPO_PAT` is a GitHub **classic** personal access token. Classic tokens cannot be scoped to a single repo — the `public_repo` scope grants write to every public repo the token owner can reach. This is a known limitation; migration to a fine-grained PAT or a GitHub App installation token is tracked as a v2.3+ consideration in initiative #257.

**Maximum lifetime**: classic PATs can be set to any expiration up to 1 year. The production PAT is created with 1-year expiration.

**Rotation procedure** (classic PAT). Two paths — pick one before you start:

**Path A — Regenerate in place** (default; fastest; has a downtime window):

The **Regenerate** button on a classic PAT is an in-place replacement. Same token record (same ID, name, scopes), new secret string. **The old secret is invalidated the instant you click Regenerate — there is no grace window where both values authenticate.** That means steps 2–4 below must run as a single contiguous session: between the click and the secret update, the mirror workflow is dead.

1. **Regenerate the token.** On the owning account: `github.com → profile photo → Settings → Developer settings → Personal access tokens → Tokens (classic) → select the existing PUBLIC_REPO_PAT token → Regenerate token`. Choose **1 year** expiration. Scopes are preserved automatically — do not change them.
2. **Copy the new value immediately.** GitHub shows the token exactly once.
3. **Update the secret.** In the private repo: `Settings → Secrets and variables → Actions → PUBLIC_REPO_PAT → Update`. Paste the new value. Click `Update secret`.
4. **Verify via probe.** Once the PAT expiration probe workflow from #320 lands, run it manually by file path (file-path invocation is case-insensitive and survives renames, unlike matching the workflow's `name:` field): `gh workflow run .github/workflows/pat-expiration-probe.yml --repo evoila-bosnia/MEHO.X`. Until that workflow exists, `mirror-to-public.yml` cannot be triggered directly with `gh workflow run` — its only trigger is `workflow_run` on `CI`, so it has no `workflow_dispatch` entry point. Instead, re-run the latest successful `CI` workflow run on `main` from the Actions UI (or push a no-op commit to `main`); that fires the `workflow_run` event and `Mirror to Public` starts automatically. The resulting mirror run must push successfully.
5. **Update the calendar.** The release owner's calendar gets a reminder for **11 months** from today — one month before the new PAT expires, so there's slack if rotation needs to be rescheduled.

Old token deletion is **not** a step on this path. Regenerate already killed the old secret; there is nothing left to delete.

**Path B — Generate alongside** (no downtime; use for pre-emptive rotation when you want a rollback escape hatch):

This path creates a distinct second token that coexists with the old one during cutover, so the mirror keeps working on the old token until you're confident the new one is good.

1. **Generate a new token.** Same screen: `Generate new token (classic)`. Give it a distinct name (e.g. `PUBLIC_REPO_PAT_v2`), **1 year** expiration, the same scopes the old token had (`public_repo`).
2. **Copy the new value immediately.** GitHub shows the token exactly once.
3. **Update the secret.** In the private repo: `Settings → Secrets and variables → Actions → PUBLIC_REPO_PAT → Update`. Paste the new value. Click `Update secret`.
4. **Verify via probe.** Same as Path A step 4 — once #320 lands, `gh workflow run .github/workflows/pat-expiration-probe.yml --repo evoila-bosnia/MEHO.X`. Until then, re-run the latest successful `CI` run on `main` from the Actions UI (or push a no-op commit to `main`) to fire the `workflow_run` trigger on `Mirror to Public`; `mirror-to-public.yml` has no `workflow_dispatch` entry point so `gh workflow run` on it directly will fail. The resulting mirror run must push successfully.
5. **Delete the old token.** Same Developer settings screen → `Delete` on the old token record. Do this only after step 4 is green; until then the old token is your rollback.
6. **Update the calendar.** Reminder for **11 months** from today, same as Path A.

**Which path to use:**

- **Under incident response** (suspected leak, forced rotation) — **Path A**. The old token may already be compromised; speed matters more than downtime, and you want the old secret dead immediately.
- **Pre-emptive rotation on a scheduled cadence** — **Path B**. No reason to take a downtime window when you're not under pressure.

If the PAT is rotated in response to a suspected leak, follow the [Incident Playbook](#incident-playbook-leaked-content-to-public) instead and treat rotation as step 1 of containment.

## Incident Playbook: Leaked Content to Public

Triggered when any content has been pushed to `evoila/meho` that should not have been — secrets, credentials, private docs, draft code, personally identifying information, or anything the release owner would not have authorized.

**First thing to know**: a git force-push does **not** reliably remove the leaked commit from GitHub's systems. See [Reality check](#reality-check-what-force-push-does-not-remove) at the bottom of this section before assuming the leak is "cleaned up" by pushing over it.

### Immediate (first 15 minutes)

1. **Rotate the leaked secret.** If the leak contains a credential, API key, PAT, password, or any other secret, **rotate it before doing anything else**. Assume every value in the leaked commit is compromised the moment it hit GitHub.
2. **Stop the mirror.** Pause the `mirror-to-public` workflow in the private repo's Actions settings. This prevents a second bad sync while the first one is being investigated.
3. **Force-push a clean state** (orphan mode). Push a known-good commit from private `main` that does *not* contain the leaked content. This shrinks the visible-on-public-`main` window but does **not** erase the leaked commit from GitHub — see the Reality check below.
4. **Confirm public `main` is clean.** Inspect `evoila/meho` in a browser. The leaked content must no longer be visible on the default branch. If it still is, escalate immediately.

### Within 1 hour

5. **Audit what leaked.** List every file in the leaked commit. Classify each: is it a secret, a credential, a private doc, a draft, PII?
6. **Rotate every exposed credential.** Not just the obvious one from step 1 — every secret that appeared in the leaked files. Assume they are all compromised.
7. **Notify stakeholders** whose data or credentials were exposed. Customer data, partner API keys, or personal credentials each have their own notification requirements.
8. **Open a private incident issue** on the private repo with the timeline so far. Use it as the working log for the rest of the response.

### Within 24 hours

9. **Contact GitHub Support.** This is the only mechanism for purging cached views of the leaked commit from GitHub's systems. Request: (a) cache invalidation for the leaked commit SHA, (b) purging of the fork network if any forks picked up the bad commit, (c) removal of the commit from `refs/pull/*/head` references if it ended up in any PRs. Have the commit SHA, repo name, and leak timeline ready.
10. **Post-mortem.** Root cause: how did the leaked content get past the allowlist? Was it a new top-level file not covered by the derived denylist check? A file explicitly added to the allowlist that shouldn't have been? A bug in the assembly script?
11. **Update the guardrails.** If the root cause is a gap in the allowlist, the derived denylist check, or the assembly script, file a task to close that gap **before** the next release is cut. The incident does not close until the guardrail is in place.
12. **Resume the mirror.** Re-enable the workflow once the guardrail change is merged and green on private CI.

### Reality check: what force-push does NOT remove

A force-push rewrites `refs/heads/main` on the public remote. It does not erase the leaked commit. The leaked commit remains reachable through every one of these channels, and each is a real leak vector:

- **GitHub's REST commit API.** `GET /repos/evoila/meho/commits/<sha>` returns the commit body and diff for as long as GitHub has the object in storage. Force-push does not trigger immediate garbage collection.
- **The fork network.** GitHub repos and their forks share an object store. If anyone has forked the repo, the leaked commit persists in that shared store **indefinitely** as long as any fork references it. Forks are outside our control.
- **Pull request refs.** If the leaked commit was ever referenced by a PR (even a closed one), `refs/pull/<N>/head` keeps it reachable until the PR is deleted.
- **Local clones.** Anyone who cloned the repo between the leak and the force-push has a local copy. Local copies cannot be recalled.
- **Search indexes and CDN caches.** GitHub's code search index and CDN caches may surface leaked content for some time after the commit is unreachable.
- **Archive.org and third-party crawlers.** If the repo is popular enough, the leaked commit may have been archived externally within minutes.

**Treat every leak as compromised.** Rotate secrets immediately, contact GitHub Support for purging, and do not rely on force-push alone for remediation. GitHub's own documentation says: *"Once you have pushed a commit to GitHub, you should consider any data it contains to be compromised."*
