<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2026 evoila Group
-->

# Releasing MEHO

Maintainer runbook for cutting a versioned MEHO release. Every step is a
copy-pastable command. The full flow is end-to-end manual; the human-gated
nature is intentional (a tag push publishes globally to GHCR and `evoila/meho`).

For the architectural model behind the pipeline this runbook drives, see
[`docs/codebase/release-and-deployment.md`](docs/codebase/release-and-deployment.md).
For the operator-facing image-verification doc, see
[`docs/security.md` § Supply chain & image provenance](docs/security.md#supply-chain--image-provenance).

> **First-time release on the new pipeline.** v0.1.0 is the first release
> through the cosign-signed, public-mirrored, CHANGELOG-driven pipeline.
> Most sibling-initiative dependencies are now in place (Helm chart shipping
> per Initiative #506; cosign signing + SBOMs per the release pipeline);
> any remaining conditional carries the issue number that, when closed,
> lifts the condition.

## At a glance

| Step | What | Where it runs |
|---|---|---|
| 1 | Pre-release checklist | Local + GitHub UI |
| 2 | Graduate `[Unreleased]` in CHANGELOG.md | Local |
| 3 | Tag and push | Local → private repo |
| 4 | Watch the release pipeline | GitHub Actions UI |
| 5 | Verify the public Release | `evoila/meho` |
| 6 | Verify cosign signatures | Local (cosign CLI) |
| 7 | Post-release smoke | Local (Docker, optionally Helm) |
| 8 | Rollback procedure (if needed) | Documented; not executed unless triggered |
| 9 | License key rotation triggers | Reference; not part of every release |
| 10 | Common failure modes and fixes | Reference |

Total wall-clock: ~75 minutes (60 min pipeline + ~15 min verification).

---

## 1. Pre-release checklist

Run all five before tagging. Any "no" stops the release.

```bash
# 1.1 — CI green on main
gh run list --repo evoila-bosnia/MEHO.X --branch main --workflow ci.yml --limit 1 --json conclusion,status,headSha
# Expect: conclusion=success, status=completed
```

```bash
# 1.2 — pyproject.toml version matches the tag you plan to push
grep '^version' pyproject.toml
# Expect: version = "<the version you're about to tag>"
```

```bash
# 1.3 — CHANGELOG.md has [Unreleased] content for the planned version
awk '/^## \[Unreleased\]/,/^## \[/{ if (NR>1 && /^## \[[0-9]/) exit; print }' CHANGELOG.md
# Expect: non-empty list of changes (Added/Changed/Deprecated/Removed/Fixed/Security)
# Empty -> there is nothing to release. Stop and write the changelog.
```

```bash
# 1.4 — PUBLIC_REPO_PAT not silently expired
gh run list --repo evoila-bosnia/MEHO.X --workflow pat-expiration-probe.yml --limit 1 --json conclusion,createdAt
# Expect: most recent run conclusion=success, createdAt within the last 7 days
# Not green or stale -> see step 10.B (rotate the PAT) before tagging.
```

```bash
# 1.5 — local clone is current and clean
git fetch origin main
git status                   # expect: nothing to commit, working tree clean
git log -1 --format='%H %s'  # confirm you are at the commit you intend to release
```

If any of the five fails, fix the underlying cause. Do not work around.

---

## 2. CHANGELOG graduation

Convert `[Unreleased]` to a versioned section dated today (UTC), then add a
fresh empty `[Unreleased]` above it. The release pipeline's `validate-tag`
job rejects any tag whose version has no matching `## [<version>]` heading.

Edit [`CHANGELOG.md`](CHANGELOG.md):

```diff
-## [Unreleased]
+## [Unreleased]
+
+## [0.1.1] - 2026-05-15
```

Update the comparison links at the bottom of the file:

```diff
-[Unreleased]: https://github.com/evoila/meho/compare/v0.1.0...HEAD
+[Unreleased]: https://github.com/evoila/meho/compare/v0.1.1...HEAD
+[0.1.1]: https://github.com/evoila/meho/releases/tag/v0.1.1
 [0.1.0]: https://github.com/evoila/meho/releases/tag/v0.1.0
```

Commit on `main` (or via PR — whichever the team's branch protection requires):

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): graduate [Unreleased] to [<version>]"
git push origin main
```

Wait for CI to land green on the merged commit before step 3 — the tag must
point at a commit that has passed CI.

---

## 3. Tag and push

Always **annotated** tags (`-a`), never lightweight. Annotated tags carry
author identity, timestamp, and message — required for `validate-tag` to
do its work and for the public Release notes to render correctly.

```bash
# Adjust to the version you are cutting
TAG="v<MAJOR>.<MINOR>.<PATCH>"

git tag -a "$TAG" -m "MEHO $TAG"
git push origin "$TAG"
```

The tag push triggers [`.github/workflows/release.yml`](.github/workflows/release.yml).
Do not push the tag from any branch other than `main` — the workflow assumes
the triggering ref is on the canonical mainline.

> **Pre-release tags (RCs, betas).** Out of scope for this runbook for now.
> The pipeline accepts SemVer 2.0 prerelease tags (`v1.2.3-rc.1`), but the
> ceremony for cutting one — separate CHANGELOG handling, dual-tag GHCR
> behaviour — is not documented yet. Use this runbook only for GA tags
> until prerelease handling is added explicitly.

---

## 4. Watch the release pipeline

Find the run:

```bash
gh run list --repo evoila-bosnia/MEHO.X --workflow release.yml --limit 1
gh run view <run-id> --repo evoila-bosnia/MEHO.X --web   # opens in browser
```

Or watch from CLI:

```bash
gh run watch <run-id> --repo evoila-bosnia/MEHO.X --exit-status
```

Pipeline stages and expected wall-clock:

| Job | Time | What it does |
|---|---|---|
| `validate-tag` | ~30s | SemVer regex, `pyproject.toml` match, `CHANGELOG.md [<version>]` exists, dual-trigger guard |
| `build-backend` (full + slim, parallel) | ~30 min | Multi-arch buildx + push to GHCR + cosign sign + CycloneDX SBOM |
| `build-frontend` | ~10 min | Multi-arch buildx + push to GHCR + cosign sign + CycloneDX SBOM |
| `publish-to-public-repo` | ~3 min | Locate public commit, push tag, compose notes (CHANGELOG + cosign block), download SBOMs, atomic draft Release publish |

If a job fails, see step 10 for common failure modes. Do not retry blindly —
the validate-tag and build jobs are deliberately fail-closed; a failure is
information, not noise.

---

## 5. Verify the public Release

Open `https://github.com/evoila/meho/releases/tag/<TAG>`. Confirm:

- **Title:** `MEHO <TAG>`
- **Body:** the CHANGELOG section for `<version>` (Added/Changed/Fixed
  subsections), followed by a `## Verify image signatures (cosign)` block
  with three copy-pastable verify commands — one per image variant.
- **Assets:** three CycloneDX JSON files —
  `meho-backend-<version>.cdx.json`,
  `meho-backend-slim-<version>.cdx.json`,
  `meho-frontend-<version>.cdx.json`.

GHCR side:

```bash
docker pull ghcr.io/evoila/meho-backend:<version>
docker pull ghcr.io/evoila/meho-backend-slim:<version>
docker pull ghcr.io/evoila/meho-frontend:<version>
# All three should pull successfully.
```

Any of the three pulls fails or any asset is missing → re-read the run logs
in step 4 and treat as a failed release. Do not announce. See step 10.

---

## 6. Verify cosign signatures

This is the load-bearing smoke test that proves the full provenance chain
works. Run it before announcing the release publicly.

Install cosign once (if not already present):

```bash
# macOS
brew install cosign

# Linux
gh release download -R sigstore/cosign --pattern '*-linux-amd64*' \
  && chmod +x cosign-linux-amd64 \
  && sudo mv cosign-linux-amd64 /usr/local/bin/cosign
```

Run the verify commands published in the Release body — copy them
directly from `https://github.com/evoila/meho/releases/tag/<TAG>` rather
than reconstructing the URL. Each one looks like:

```bash
cosign verify \
  --certificate-identity "https://github.com/evoila-bosnia/MEHO.X/.github/workflows/release.yml@refs/tags/<TAG>" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/evoila/meho-backend:<version>
```

Expected output (excerpt):

```
Verification for ghcr.io/evoila/meho-backend:<version> --
The following checks were performed on each of these signatures:
  - The cosign claims were validated
  - Existence of the claims in the transparency log was verified offline
  - The code-signing certificate was verified using trusted certificate authority certificates
[{"critical":{...,"image":{"docker-manifest-digest":"sha256:..."}, ...}, "optional":{..., "Issuer":"https://token.actions.githubusercontent.com", "Subject":"https://github.com/evoila-bosnia/MEHO.X/.github/workflows/release.yml@refs/tags/<TAG>"}}]
```

The `Subject` and `Issuer` lines at the end are what you actually care
about — they are the cryptographic proof of which workflow built the image.

Run all three (`meho-backend`, `meho-backend-slim`, `meho-frontend`). If
any returns non-zero, treat as a failed release — see
[`docs/security.md` § Supply chain & image provenance](docs/security.md#what-verification-proves-and-what-it-doesnt)
for the failure-mode interpretation table, then jump to step 10.

---

## 7. Post-release smoke

### 7.1 Docker compose smoke (always run)

Pull the published image, exercise the basic boot path:

```bash
# In a scratch directory away from the repo
mkdir /tmp/meho-smoke && cd /tmp/meho-smoke
cp /path/to/MEHO.X/env.example .env

# Set the bare minimum required env (adjust for your test setup)
# - ANTHROPIC_API_KEY
# - CREDENTIAL_ENCRYPTION_KEY (run scripts/generate-encryption-key.sh)

docker run --rm --env-file .env -p 8000:8000 \
  ghcr.io/evoila/meho-backend:<version> \
  /bin/sh -c 'python -c "import meho_app; print(meho_app.__name__)"'
# Expect: "meho_app" printed; clean exit 0
```

Backend HTTP smoke (separate terminal, after the above):

```bash
docker run --rm --env-file .env -p 8000:8000 \
  ghcr.io/evoila/meho-backend:<version> &
sleep 30  # wait for migrations + startup
curl -fsS http://localhost:8000/health
# Expect: 200 OK with body {"status":"ok"}
```

### 7.2 Helm install smoke

The Helm chart at `deploy/helm/meho/` is shipping as of Initiative
#506 (chart skeleton, backend + frontend templates, Postgres/Redis
subcharts, Secret template, helm-test CI workflow, and operator
runbook all landed). Run the smoke against a kind / minikube
cluster:

```bash
helm dependency update deploy/helm/meho
helm install meho-test deploy/helm/meho \
  --values deploy/helm/meho/values-dev.yaml \
  --set image.tag=<version>

kubectl rollout status deploy/meho-test-backend --timeout=5m
kubectl rollout status deploy/meho-test-frontend --timeout=5m
helm uninstall meho-test
```

A rollout stuck at "waiting for replicas" is a smoke failure — see
step 8 (rollback) before announcing. For full operator-facing
install / upgrade / troubleshoot procedures see
[docs/deployment/kubernetes.md](docs/deployment/kubernetes.md).

---

## 8. Rollback procedure

The cardinal rule: **never delete published tags or images.** Other
maintainers, evaluators, and CI systems may already have pulled them.
Mutating the historical record is worse than the bug you are rolling back.

The procedure for any post-release-discovered defect:

1. **Cut a patch tag.** Increment the patch level (`v0.1.0` → `v0.1.1`)
   or use a `.postN` suffix for documentation-only fixes
   (`v0.1.0` → `v0.1.0.post1`).
2. **Land the fix on `main`.** Branch from the affected tag, fix, PR
   into `main`, run CI, merge. Do not modify the tagged commit.
3. **Run this runbook again** for the patch tag.
4. **Communicate prominently.** The patch Release's notes lead with a
   "## Why this patch" section that names the regression, the affected
   versions, and a workaround if applicable. Pin the announcement on
   the project's communication channels.
5. **Mark the bad version on the Release page.** Edit the bad tag's
   GitHub Release notes (do NOT delete the Release) to add a top-line
   warning:
   > **⚠️ Superseded by v0.1.1 due to <one-sentence-why>. See
   > [Release notes](https://github.com/evoila/meho/releases/tag/v0.1.1).**

Tag deletion is reserved for tags pushed in error before any user could
have pulled them (e.g., a fat-fingered tag name within a few minutes
of the push). Even then, prefer cutting a corrected tag and treating
the bad one as superseded.

---

## 9. License key rotation triggers

The Ed25519 keypair that signs enterprise license tokens has its public
half embedded in [`meho_app/core/licensing.py`](meho_app/core/licensing.py)
and its private half in vault. **Rotation is a release-adjacent operation**
because it requires shipping a new image with the updated public key.

Rotate the keypair when:

- **Annual schedule.** Default cadence; mark a calendar reminder for
  twelve months after the previous rotation.
- **Suspected compromise.** Any indication the private key may have been
  exposed (vault audit log anomaly, personnel with key access leaving
  on bad terms, vault provider security incident).
- **Personnel change.** Key custodian leaves the team or changes role;
  even without compromise, rotate so the previous custodian's access
  becomes irrelevant.

The rotation procedure (vault commands, key-generation step, license
re-issuance for existing customers) lives in
[`.claude/operations/license-key-custody.md`](.claude/operations/license-key-custody.md).
The custody doc is intentionally private — `.claude/` is gitignored and
not mirrored to `evoila/meho` — because it carries vault-path
conventions and incident-response details that should not ship to the
public OSS surface. Maintainers with access to the private repo can
read it directly; external contributors do not need it because they do
not hold the signing key.

The placeholder/real-key swap is tracked separately under
[#518](https://github.com/evoila-bosnia/MEHO.X/issues/518) (replace
`_PUBLIC_KEY_B64` placeholder + add CI guard against re-introduction).

The `_PUBLIC_KEY_B64` swap in `licensing.py` is a normal code change
that ships in the next release through this runbook — there is no
special "rotation release" ceremony. Customers verifying license tokens
will see the new public key the moment they pull the new image.

---

## 10. Common failure modes and fixes

### A. `validate-tag` failed: "Tag '...' does not match SemVer 2.0"

The tag isn't a valid SemVer 2.0 string. Common mistakes: leading zeros
(`v01.2.3`), build metadata (`v1.2.3+build.1` — Docker can't tag those),
empty pre-release identifiers (`v1.2.3-`).

```bash
# Delete the bad tag locally and remotely (it never reached the public
# repo because the workflow failed before publish-to-public-repo)
git tag -d "$TAG"
git push origin --delete "$TAG"
# Then push the corrected tag.
```

### B. Mirror PAT expired

`pat-expiration-probe.yml` has gone red OR `publish-to-public-repo` fails
with HTTP 403 from `git push public`. The classic PAT
(`PUBLIC_REPO_PAT`) silently expires; rotation procedure is in
[`docs/development/dual-repo-workflow.md` § PAT rotation](docs/development/dual-repo-workflow.md#pat-rotation).

After rotation, re-run the failed job:

```bash
gh run rerun <run-id> --repo evoila-bosnia/MEHO.X --failed
```

The build jobs from the same run are reused (cached); only the failed
publish job re-executes.

### C. CHANGELOG mismatch: "no '## [<version>]' heading"

`validate-tag` rejected the tag because `CHANGELOG.md` does not contain
a `## [<version>]` heading. You probably tagged before completing
step 2.

```bash
# Delete the bad tag (workflow failed at validate-tag, nothing reached public)
git tag -d "$TAG"
git push origin --delete "$TAG"
# Complete step 2 (graduate [Unreleased] -> [<version>]), commit, push.
# Then re-tag.
```

### D. Cosign signing failed mid-build

The build step succeeded (image pushed to GHCR) but `Sign published image`
failed. Common causes: Sigstore Rekor transient outage; OIDC token
denied (extremely rare).

The image is published but unsigned — that is not a valid release.
Options:

1. **Retry the failed job.** `gh run rerun <run-id> --failed`. The build
   step is cached; sign-step rerun usually succeeds.
2. **If the retry fails too,** check Sigstore status
   (`https://status.sigstore.dev/`). If Rekor is down, wait it out.
3. **If Sigstore is healthy and signing still fails,** treat the
   pushed image as bad. Cut a `v<version>.post1` patch tag (which
   re-pushes the same image bytes under a new tag with a fresh
   signing attempt). Add a Release-notes warning on the bad tag
   per step 8.

### E. Image build OOM or timeout

`build-backend` (full variant) is the heaviest job — `INCLUDE_DOCLING=true`
pulls heavy ML deps. GitHub-hosted ubuntu-latest runners occasionally
fail with OOM during multi-arch buildx. Retry once
(`gh run rerun <run-id> --failed`); if it fails twice consecutively,
escalate (consider self-hosted runners under #533 / #532 follow-up
tickets).

### F. Public repo's Release exists but body is missing the cosign block

The CHANGELOG-section extractor (`scripts/extract-changelog-section.sh`)
returned content but the cosign block was somehow absent. Check the
`Compose release notes` step's logs in the failed run. If the bug is
a code defect, file an issue, fix on `main`, then **edit the existing
Release body manually** (don't recut the tag — the images are signed
and pulled, only the notes are wrong).

```bash
gh release edit "$TAG" --repo evoila/meho --notes-file /path/to/corrected-notes.md
```

### G. Public-mirror double-trigger fired

`release.yml` ran twice — once on `evoila-bosnia/MEHO.X`, once on
`evoila/meho` after the tag push to public. The dual-trigger guard
(`if: github.repository == 'evoila-bosnia/MEHO.X'` on `validate-tag`)
should prevent this.

Check the Release Actions tab on `evoila/meho`: the public-side run
should show `validate-tag` as **skipped**. If it didn't skip, the guard
is broken — open a critical issue and treat the release as
indeterminate (the public-side run may have re-pushed images and
re-signed with a competing cert identity). Recovery: cut
`v<version>.post1` against the corrected workflow.

---

## References

- [`docs/codebase/release-and-deployment.md`](docs/codebase/release-and-deployment.md) — release pipeline architecture (the *why* behind every step here)
- [`docs/security.md` § Supply chain & image provenance](docs/security.md#supply-chain--image-provenance) — operator-facing image-verification doc
- [`.github/workflows/release.yml`](.github/workflows/release.yml) — the canonical pipeline this runbook drives
- [`.github/workflows/mirror-to-public.yml`](.github/workflows/mirror-to-public.yml) — private → public source projection
- [`.github/workflows/pat-expiration-probe.yml`](.github/workflows/pat-expiration-probe.yml) — weekly PAT health check
- [`docs/development/release-notes-template.md`](docs/development/release-notes-template.md) — the announce template (separate concern; this runbook covers the cut, not the announcement)
- [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html)
- [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/)
- [Sigstore documentation](https://docs.sigstore.dev/)
