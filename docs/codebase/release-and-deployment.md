<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2026 evoila Group
-->

# Release and deployment

## Overview

MEHO ships as two container images (`meho-backend` full + slim variants, `meho-frontend`)
and an open-core licensing system. This document describes how source becomes a release,
how a release becomes a deployable artifact, and how a deployed instance is unlocked into
enterprise mode via a signed license token. It complements
[public-mirror.md](public-mirror.md), which covers the orthogonal private→public source
projection.

The release/deployment surface is split across **four loosely coupled systems**:

1. **Quality gates** — `.github/workflows/ci.yml` and siblings. Run on every push to
   `main` and every PR. Produce pass/fail status checks; publish nothing.
2. **Public mirror** — see [public-mirror.md](public-mirror.md). Continuously projects
   `main` into `evoila/meho`. Runs on `workflow_run` after CI succeeds.
3. **Release pipeline** — `.github/workflows/release.yml`. Triggered by tag push (`v*`).
   Builds multi-arch images, pushes to GHCR, creates GitHub Release.
4. **Licensing** — `meho_app/core/licensing.py` (verifier embedded in the running app)
   plus the keypair-generation helper. Verifies an Ed25519-signed token at startup and
   gates enterprise features.

The four systems share **no automatic version contract**. Tag pushes are invisible to
the mirror; the mirror runs against `main` whenever CI completes. The release
pipeline's `validate-tag` pre-flight job (see
[Tag-validation pre-flight](#tag-validation-pre-flight)) is the only place the three
version sources — git tag, `pyproject.toml`, and `CHANGELOG.md` — are reconciled, and
only at release time. The licensing system has no version concept at all. Coordinating
a release across all four is a manual maintainer responsibility today (see
[RELEASING.md](../../RELEASING.md) once it exists, otherwise this document).

## Key files

### Quality gates

- [.github/workflows/ci.yml](../../.github/workflows/ci.yml) — six parallel jobs:
  Python lint (ruff), TypeScript lint (ESLint), type-check (mypy + tsc), Python unit
  tests, frontend unit tests, bats tests for `assemble-public-tree.sh`. Designed to be
  runnable on the public repo by external contributors — no integration tests, no
  external services, dummy env vars throughout.
- [.github/workflows/security-scan.yml](../../.github/workflows/security-scan.yml) —
  Semgrep SAST against `p/python`, `p/typescript`, `p/security-audit`,
  `p/owasp-top-ten`; pip-audit with documented per-CVE suppressions; npm audit at
  `--audit-level=high`. SARIF uploaded to GitHub Code Scanning.
- [.github/workflows/license-check.yml](../../.github/workflows/license-check.yml) —
  validates that every Python and npm dependency has a license compatible with
  AGPL-3.0-only. **Note**: this workflow checks *dependency* licenses (SPDX), not
  customer license tokens. Currently in WARN mode (`continue-on-error: true`).
- [.github/workflows/secret-scan.yml](../../.github/workflows/secret-scan.yml) —
  gitleaks against the diff.
- [.github/workflows/dead-code-check.yml](../../.github/workflows/dead-code-check.yml) —
  vulture against `meho_app/`.
- [.github/workflows/quality-gate.yml](../../.github/workflows/quality-gate.yml) —
  aggregate gate.
- [.github/workflows/cla.yml](../../.github/workflows/cla.yml) — CLA enforcement on PRs.
- [.github/workflows/planning-guard.yml](../../.github/workflows/planning-guard.yml) —
  blocks `.planning/` paths from PRs intended for the public repo.
- [.github/workflows/pat-expiration-probe.yml](../../.github/workflows/pat-expiration-probe.yml) —
  weekly cron; calls `gh api repos/evoila/meho` with `PUBLIC_REPO_PAT`; fails if the
  token is invalid or has lost its `public_repo` scope. Catches silent token expiry up
  to six days before the next mirror push would have failed.

### Release pipeline

- [.github/workflows/release.yml](../../.github/workflows/release.yml) — tag-driven
  workflow. Four jobs in three phases: `validate-tag` (pre-flight gate; see
  [Tag-validation pre-flight](#tag-validation-pre-flight)), then `build-backend`
  (matrix: `full` / `slim`) and `build-frontend` in parallel, then
  `publish-to-public-repo`. Multi-arch builds (`linux/amd64`, `linux/arm64`)
  via QEMU + Buildx. Cache backed by GitHub Actions cache (`type=gha,mode=max`).
  The publish job pushes the tag and creates the GitHub Release on
  `evoila/meho` (the public OSS surface) — see
  [How public-repo tagging works](#how-public-repo-tagging-works) below.
- [docker/Dockerfile.meho](../../docker/Dockerfile.meho) — backend image. Multi-stage:
  `base-cpu` (default) or `base-gpu` (NVIDIA CUDA 12.4) → `base` → `prod` (default)
  or `debug`. Build args: `INCLUDE_DOCLING=true|false` (heavy ML deps),
  `CUDA_ENABLED=true|false` (PyTorch alone), `TARGETBASE=base-cpu|base-gpu`.
- [docker/Dockerfile.meho-frontend](../../docker/Dockerfile.meho-frontend) — Vite SPA
  built by `node:20-alpine`, served by `nginx:alpine`. Two envsubst calls at startup
  process `nginx.conf.template` (CORS / Keycloak origin) and `config.js.template`
  (frontend runtime config — `API_URL`, `KEYCLOAK_*`). The `config.js` runtime-config
  cache contract is documented in [first-run-experience.md](first-run-experience.md).
- [docker/docker-entrypoint.sh](../../docker/docker-entrypoint.sh) — backend entrypoint.
  Runs `scripts/run-migrations-monolith.sh` then `exec`s the CMD.
- [pyproject.toml](../../pyproject.toml) — `version = "0.1.0"`. Hatchling backend.
  Heavy optional groups (`docling-group`, `torch-group`) are opt-in via Docker
  build args.
- [CHANGELOG.md](../../CHANGELOG.md) — Keep a Changelog 1.1.0 format. The
  `[Unreleased]` heading collects entries that graduate to a versioned section at
  release time.

### Licensing

- [meho_app/core/licensing.py](../../meho_app/core/licensing.py) — verifier. Reads
  `MEHO_LICENSE_KEY` env var via `config.py`, validates the Ed25519 signature against
  an embedded public key, decodes the JWT-shaped payload, computes
  `Edition.COMMUNITY` or `Edition.ENTERPRISE` with a 30-day post-expiry grace period.
  Singleton via `@lru_cache`.
- [scripts/generate-license-keypair.py](../../scripts/generate-license-keypair.py) —
  one-shot Ed25519 keypair generator. Safe-by-default: refuses to emit the
  private key without an explicit output flag. Exactly one of three flags must
  be given:
  - `--vault-write projects/<PROJECT>/secrets/<NAME>` — writes the private key
    directly to GCP Secret Manager (preferred). Strict path validation
    (exactly four `/`-separated segments — versioned paths like
    `…/versions/latest` are rejected). Bare `<NAME>` is accepted if
    `GOOGLE_CLOUD_PROJECT` is set. Pre-flights via `get_secret(name=parent)`
    *before* generating the keypair so a missing secret resource, IAM gap,
    or transient error never silently discards a freshly-minted private key.
    Lazy-imports `google-cloud-secret-manager`, which is not a project
    dependency — install on the maintainer's machine only
    (`uv pip install 'google-cloud-secret-manager>=2.0.0'`).
  - `--output-private FILE` — writes the private key to `FILE` with mode
    `0600`, atomically via `O_EXCL`; refuses to overwrite an existing file.
  - `--unsafe-stdout` — prints the private key to stdout with a warning.
    Legacy escape hatch only.
  The public key is always printed to stdout (it is not a secret) along with
  the line to paste into `_PUBLIC_KEY_B64`.
- **Maintainer-only custody runbook** — operational details for the production
  private key (vault location, active-key fingerprint, rotation, compromise,
  recovery procedures) live in a runbook outside this repo's public mirror.
  Maintainers find it in the private repo at `.claude/operations/license-key-custody.md`;
  it is intentionally excluded from the mirror because it documents the vault
  layout. OSS forks running their own deployment generate their own keypair
  per [scripts/generate-license-keypair.py](../../scripts/generate-license-keypair.py)
  and write their own runbook.

### Absent surfaces (gaps)

- **No `RELEASING.md`** — maintainer release procedure is undocumented.
- ~~**No production license issuance** — there is no script to mint a signed license
  token for a customer. Keypair generation exists; token issuance does not.~~
  Resolved: `scripts/issue-license.py` mints signed enterprise tokens against
  the production Ed25519 key, with fail-closed audit logging via
  `meho_app/modules/licensing/audit.py` (see [Issuance CLI](#issuance-cli)
  below).
- **Helm chart partial** — chart skeleton (`deploy/helm/meho/Chart.yaml`,
  `values.yaml`, `values-{dev,prod}.yaml`, README), the backend Deployment +
  Service, the frontend Deployment + Service + (optional) Ingress, and the
  Postgres/Redis subchart wiring all exist. The frontend Ingress is gated
  by `ingress.enabled` and uses the current `networking.k8s.io/v1` API
  (`spec.ingressClassName` rather than the deprecated annotation). The
  frontend's `readinessProbe` targets `/config.js` to confirm both that
  nginx is up and that the entrypoint's envsubst step actually finished
  writing the runtime-config asset (a `/` probe would only prove nginx
  itself started). The probe does *not* detect missing or empty env vars
  — `envsubst` substitutes unset variables as empty strings and exits 0 —
  so the chart additionally uses Helm's `required` on `frontend.apiUrl`,
  `frontend.keycloakUrl`, and (when `ingress.enabled=true`) `ingress.host`;
  `helm install` fails before any pod starts when those keys are empty.
  Detection of partially-rendered placeholders inside the served
  `config.js` is a Docker `HEALTHCHECK` concern owned by Task #535. The
  Postgres/Redis path follows the production-OSS convention used by Argo
  CD, Grafana, and Sentry: production deployments default to
  operator-supplied external services (`postgres.external.dsn`,
  `redis.external.url` — required at template time via Helm's `required`);
  evaluators set `embedded.enabled=true` to pull in Bitnami's `postgresql`
  (chart 13.4.x → PG 16) and `redis` (chart 20.x → Redis 7.4) subcharts
  under lower-case aliases (`embeddedpostgres`, `embeddedredis` —
  upper-case aliases produce RFC-1123-invalid resource names). Two helper
  templates (`templates/_postgres-dsn.tpl`, `templates/_redis-url.tpl`)
  resolve the right `DATABASE_URL` / `REDIS_URL` per mode and inject them
  as explicit `env:` entries on the backend Deployment, which take
  precedence over the same keys from the operator's backend Secret.
  Embedded mode uses `$(VAR)` env-var expansion to pull the Bitnami-emitted
  password from a sibling `valueFrom: secretKeyRef` entry — ordering
  matters in the env list (password entry must precede the URL entry that
  references it). `.helmignore` does **not** carry the `helm
  create`-skeleton `*.tgz` pattern: in Helm 4 that pattern filters out the
  subchart archives `helm dependency update` writes into `charts/`,
  breaking `helm template` with "found in Chart.yaml, but missing in
  charts/ directory". The backend Secret is chart-managed by default
  (`templates/secret.yaml` renders when `secrets.create=true` and
  `secrets.existingSecret` is empty), populated from `secrets.jwtSecretKey`
  / `secrets.credentialEncryptionKey` (both `required`-validated) and the
  optional `secrets.licenseKey`. Production deployments project the Secret
  from a cluster secret store via External Secrets Operator (or any
  equivalent — sealed-secrets, sops, raw `kubectl`) and point
  `secrets.existingSecret` at the operator-created name; the chart-managed
  template is skipped to keep credentials out of `helm history`. The Secret
  name resolves through `meho.backend.secretName` (default:
  `meho.backend.fullname` — `<release>-meho-backend` *unless* the release
  name already contains `meho`, in which case it collapses to
  `<release>-backend`; the helper avoids double-naming for the canonical
  `helm install meho` case). The Secret carries `JWT_SECRET_KEY`,
  `CREDENTIAL_ENCRYPTION_KEY`, optional `MEHO_LICENSE_KEY`, plus any
  `KEYCLOAK_*` keys the operator's external Secret supplies; `DATABASE_URL`
  and `REDIS_URL` are NOT consumed from it (the chart templates them
  explicitly into the backend Deployment, see above). Helm-test CI workflow
  (#529) and the operator runbook (#530) are still pending under Initiative
  #506.
## Control flow

### Quality gates (per push to main / per PR)

1. Push to a branch or open a PR targeting `main`.
2. `ci.yml`, `security-scan.yml`, `license-check.yml`, `secret-scan.yml`,
   `dead-code-check.yml`, `frontend-tests.yml`, and `planning-guard.yml` run in
   parallel. Each is independent — no `needs:` chain.
3. Required checks gate the merge. Optional checks (license-check today) run with
   `continue-on-error: true` and report-only.
4. On merge to `main`, CI re-runs against the merged commit. If it passes,
   `mirror-to-public.yml` triggers via `workflow_run` (see
   [public-mirror.md](public-mirror.md)).

### Release pipeline (per tag push)

1. Maintainer creates a release tag on the **private** repo:
   `git tag v<version> && git push origin v<version>`.
2. `release.yml` triggers via `on: push: tags: ['v*']`.
3. `validate-tag` runs first (see
   [Tag-validation pre-flight](#tag-validation-pre-flight)). If the tag is
   malformed, drifts from `pyproject.toml`, or has no `CHANGELOG.md` entry,
   the workflow fails before any image is built.
4. After `validate-tag` passes, three matrix jobs run in parallel:
   - `build-backend` variant `full` — builds with `INCLUDE_DOCLING=true`,
     pushes to `ghcr.io/evoila/meho-backend:<tag>` (plus `<major>.<minor>` and
     `latest`).
   - `build-backend` variant `slim` — builds with `INCLUDE_DOCLING=false`,
     pushes to `ghcr.io/evoila/meho-backend-slim:<tag>` (plus `<major>.<minor>`,
     no `latest`).
   - `build-frontend` — builds the SPA + nginx image, pushes to
     `ghcr.io/evoila/meho-frontend:<tag>` (plus `<major>.<minor>` and `latest`).
5. Each build is multi-arch (`linux/amd64,linux/arm64`) via QEMU emulation.
6. Once each image is pushed, the build job runs two supply-chain steps
   in sequence: cosign signs the just-published image(s) with keyless OIDC
   (`sigstore/cosign-installer` + `cosign sign --yes <tag>@<digest>`,
   scoped to the immutable digest the registry just accepted), then
   `anchore/sbom-action` (Syft engine) pulls the same manifest from GHCR
   and produces a CycloneDX JSON SBOM, uploaded as a workflow artifact
   (`sbom-backend-full`, `sbom-backend-slim`, `sbom-frontend`). See
   [Image signing with cosign](#image-signing-with-cosign) and
   [SBOM generation and Release-asset attachment](#sbom-generation-and-release-asset-attachment).
7. After all three image jobs succeed, `publish-to-public-repo` runs. It
   locates the public commit on `evoila/meho/main` whose body references the
   tagged private SHA (the mirror writes `mirror: sync from private <short>`
   into every projection commit), pushes the tag to `evoila/meho`,
   composes the release body from the matching `## [<version>]` section
   of `CHANGELOG.md` (extracted via
   [`scripts/extract-changelog-section.sh`](../../scripts/extract-changelog-section.sh))
   plus a copy-pastable `cosign verify` block for every image variant,
   downloads the three SBOM artifacts produced by the build jobs, runs
   `gh release create <tag> --repo evoila/meho --notes-file <path>
   --draft` *with the SBOMs as positional file args* (atomic
   create-with-assets in draft state), and finally
   `gh release edit --draft=false` to publish. The PR-summary half
   generated by `POST /repos/evoila/meho/releases/generate-notes` is no
   longer used; the curated CHANGELOG entry replaces it. The private
   workflow repo no longer receives a Release. See
   [How public-repo tagging works](#how-public-repo-tagging-works),
   [CHANGELOG.md graduation pattern](#changelogmd-graduation-pattern),
   [Image signing with cosign](#image-signing-with-cosign), and
   [SBOM generation and Release-asset attachment](#sbom-generation-and-release-asset-attachment).

### Tag-validation pre-flight

The `validate-tag` job in `release.yml` runs before any build job and gates them
via `needs: [validate-tag]`. It enforces three independent checks against the
pushed tag (`github.ref_name`):

1. **Tag shape** — must match the canonical SemVer 2.0 regex from
   [semver.org/spec/v2.0.0.html](https://semver.org/spec/v2.0.0.html), with the
   leading `v` prefix and build metadata via `+` deliberately excluded:

   ```regex
   ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-((0|[1-9][0-9]*|[a-zA-Z-][a-zA-Z0-9-]*)(\.(0|[1-9][0-9]*|[a-zA-Z-][a-zA-Z0-9-]*))*))?$
   ```

   This rejects leading zeros (`v01.2.3` per SemVer §2), empty pre-release
   identifiers (`v1.2.3-..` per SemVer §9), numeric pre-release identifiers
   with leading zeros (`v1.2.3-01`), and build metadata (`v1.2.3+build.1`).
   The `+` exclusion exists because Docker image tags cannot contain `+` and
   the downstream `docker/metadata-action` would silently mangle it.
2. **`pyproject.toml` version match** — the tag with the leading `v` stripped
   must equal the `[project] version` value in `pyproject.toml`. Catches the
   "tagged a release without bumping the manifest" mistake.
3. **`CHANGELOG.md` entry exists and has content** — there must be a
   `## [<version>]` heading in `CHANGELOG.md` for the tag's version, *and* the
   section under it must contain non-whitespace content. `[Unreleased]` does
   not satisfy the heading check; an empty body (heading present but no
   entries graduated underneath it) does not satisfy the content check. The
   maintainer must graduate `[Unreleased]` properly per the
   [CHANGELOG.md graduation pattern](#changelogmd-graduation-pattern) above.
   The content check invokes
   [`scripts/extract-changelog-section.sh`](../../scripts/extract-changelog-section.sh) —
   the same helper `publish-to-public-repo` uses to feed `--notes-file` —
   so the two jobs always extract identical text. Failing pre-flight is
   preferable to discovering an empty section after 30 minutes of image
   builds.

Each check uses `::error::` workflow commands so failures surface as red
errors in the run UI, not buried in step output. The job runs with explicit
`permissions: contents: read` (least privilege) and a 5-minute timeout.

### How public-repo tagging works

The release tag and GitHub Release land on `evoila/meho` — the public OSS
surface — even though the workflow itself runs on the private repo. There is
no SHA correspondence between the two repos: every commit on `evoila/meho`
is produced by `mirror-to-public.yml` as a *new* commit, not a copy of a
private commit. To tag a public commit for the release, the
`publish-to-public-repo` job has to find which public commit corresponds to
the tagged private SHA.

The lookup uses the mirror commit message as the bridge. The mirror runs
`git commit -m "mirror: sync from private <short-sha>"` — a single-line
message, so the marker is the commit subject — where `<short-sha>` is the
private `HEAD` at mirror time. `git log --grep` matches against the whole
commit message, so a subject-only marker is sufficient. The publish job:

1. Computes the 7-character prefix of the tagged private SHA
   (`github.sha`). 7 chars is git's default `--short` length, which is what
   the mirror writes into commit messages. As the repo grows git may extend
   the abbreviation, but the 7-char prefix is still a substring of the
   longer form so `git log --grep` matches either way.
2. Adds `evoila/meho` as a remote authenticated by `secrets.PUBLIC_REPO_PAT`.
3. Polls `git fetch public main --depth=50 --no-tags` + `git log
   public/main --grep="mirror: sync from private <short>"` for up to 5
   minutes (30 attempts × 10s). `--depth=50` keeps the bandwidth bill
   bounded as public history grows; `--no-tags` avoids fetching public's
   tag refs into the local repo, which would conflict with the local tag
   `actions/checkout` populated for the triggering tag.
4. On match: pushes the located public SHA directly to a remote tag
   refspec (`git push public <sha>:refs/tags/<tag>` — no local tag
   mutation, since `actions/checkout` already created the local tag at
   the *private* commit), then runs `gh release create --repo evoila/meho`.
5. On timeout: fails the job with an `::error::` annotation. Fail-closed,
   never silently tag the wrong commit.

The 5-minute window matters operationally: the mirror normally lands within
~1 minute of CI green on `main`, and the preceding image-build jobs in
`release.yml` take ~30 minutes, so the matching public commit is virtually
always already on `public/main` by the time this job runs. The poll exists
purely to absorb backed-up mirror queues.

This implementation chose post-mirror lookup over self-computing the public
commit via `scripts/assemble-public-tree.sh` because the mirror is the
single producer of public commits — reusing its output keeps the contract
one-way and avoids duplicating tree-assembly logic that could drift.

### Image signing with cosign

> **Operator-facing companion**: end users verifying a pulled image should
> read [Security & Data Handling § Supply chain & image provenance](../security.md#supply-chain--image-provenance)
> instead of this section — the operator doc covers cosign install, verify
> commands, and what verification proves. This section covers the
> implementation details a maintainer needs to extend or audit the
> signing pipeline.

Every image published by `release.yml` is signed with cosign keyless OIDC
([sigstore.dev](https://docs.sigstore.dev/)). No private key is generated,
stored, or rotated by the publisher — the GitHub-issued OIDC token (granted
to `build-backend` and `build-frontend` via per-job `id-token: write`) is
exchanged for a short-lived Fulcio certificate, used to sign the image
digest, and the signature plus certificate are recorded in the Sigstore
Rekor public transparency log.

The signing step runs immediately after `docker/build-push-action`, scoped
to the digest the registry just accepted (`steps.build.outputs.digest`).
Iterating over the metadata-action's tag list and signing `<tag>@<digest>`
ensures every published tag (`<version>`, `<major>.<minor>`, `latest`)
points at a verifiable signature for the same digest. A subsequent
tag-overwrite attack does not silently revalidate.

#### Certificate identity (the gotcha)

The Fulcio certificate's `Subject Alternative Name` carries a URL of the
form `https://github.com/<owner>/<repo>/.github/workflows/<workflow>@<ref>`,
reflecting the workflow run that requested the OIDC token. Because
`release.yml` runs on the **private** CI repository
(`evoila-bosnia/MEHO.X`) — even though the source self-hosters audit lives
on the public mirror (`evoila/meho`) — the cert identity reads:

```
https://github.com/evoila-bosnia/MEHO.X/.github/workflows/release.yml@refs/tags/<tag>
```

Self-hosters verify against that identity. The signature is publicly
verifiable on Sigstore Rekor without needing read access to the private
repo; the URL is a cryptographic anchor, not a source pointer. The verify
block in the public Release body uses `--certificate-identity` (exact
match), not `--certificate-identity-regexp`: the full URL is fully known
at release time, and exact-match avoids the trap that any unescaped `.`
in a SemVer tag (every tag, but especially prereleases like
`v1.2.3-rc.1`) would silently broaden a regex anchor to a wildcard.

#### Verify command

```bash
cosign verify \
  --certificate-identity "https://github.com/evoila-bosnia/MEHO.X/.github/workflows/release.yml@refs/tags/<tag>" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/evoila/meho-backend:<version>
```

A successful verify prints `Verified OK` plus the matched cert identity
and OIDC issuer. A tampered or unsigned image fails non-zero. The full
templated block lives in the Release body on `evoila/meho` and covers
all three published images (`meho-backend`, `meho-backend-slim`,
`meho-frontend`).

#### Dual-trigger guard

`release.yml` is mirrored to `evoila/meho` via the public-allowlist
`.github/` entry. The tag that `publish-to-public-repo` pushes to public
via `PUBLIC_REPO_PAT` would re-trigger the entire workflow on the public
mirror with a competing cosign cert identity (`evoila/meho/...`),
producing two signatures whose identities disagree and only one of which
the verify block in the Release body would accept. `validate-tag` carries
an `if: github.repository == 'evoila-bosnia/MEHO.X'` guard; every other
job chains via `needs:` so guarding the head job is sufficient. The
public-mirrored workflow short-circuits with all jobs skipped.

#### Action pinning

`sigstore/cosign-installer` is pinned by SHA, not tag, per the project
convention: `@cad07c2e89fa2edd6e2d7bab4c1aa38e53f76003` (= v4.1.1, defaults
to cosign v3.0.5). v4 changed `cosign sign-blob` to require `--bundle`, but
container-image `cosign sign` is unchanged from v2.

### SBOM generation and Release-asset attachment

Each `release.yml` build job (`build-backend` matrix variants `full` /
`slim`, plus `build-frontend`) generates a CycloneDX JSON SBOM after pushing
and signing its image, and uploads it as a workflow artifact. The signing
step runs first and the SBOM step second; both are independent of the
Release flow that follows in `publish-to-public-repo`:

1. **Generate** — `anchore/sbom-action` (Syft engine) pulls the just-pushed
   manifest from GHCR by reference (`ghcr.io/evoila/meho-<image>:<version>`)
   and writes a CycloneDX JSON SBOM to a deterministic filename
   (`meho-backend-<version>.cdx.json`,
   `meho-backend-slim-<version>.cdx.json`,
   `meho-frontend-<version>.cdx.json`).
2. **Upload as workflow artifact** — `actions/upload-artifact` stores it
   under `sbom-backend-full`, `sbom-backend-slim`, or `sbom-frontend` with
   90-day retention. The action's built-in upload is disabled
   (`upload-artifact: false`) to keep artifact naming deterministic across
   the matrix.
3. **Download before Release creation** — once both build jobs finish,
   `publish-to-public-repo` runs the [Compose release notes](#image-signing-with-cosign)
   step (CHANGELOG `## [<version>]` section extracted via
   [`scripts/extract-changelog-section.sh`](../../scripts/extract-changelog-section.sh)
   + cosign verify block, written to `/tmp/release-notes.md`), then
   pulls all three SBOM artifacts via `actions/download-artifact` with
   `pattern: sbom-*` and `merge-multiple: true` *before* the Release is
   created. Flattening the per-artifact subdirectories puts the three
   CycloneDX files in the working directory so the next step's positional
   file args can pick them up by glob. Same-run downloads use
   `ACTIONS_RUNTIME_TOKEN`, so this works under the workflow's strict
   `permissions: contents: read, packages: write` block without an extra
   `actions: read` grant.
4. **Atomic draft Release with notes and SBOMs attached** — a single
   `gh release create "$TAG" --repo evoila/meho --title "MEHO $TAG"
   --notes-file /tmp/release-notes.md --draft meho-backend-*.cdx.json
   meho-frontend-*.cdx.json` creates the Release as a *draft* (invisible
   to non-collaborators, no `release.published` webhook fires), uses the
   notes file built in step 3, and attaches the three SBOMs as Release
   assets in the same gh-CLI invocation. Globs are bash-expanded with
   `nullglob` off, so an unmatched pattern reaches `gh` literally and
   the command exits non-zero ("file not found") — fail-closed against
   the download step silently producing fewer than three SBOMs.
   `--notes-file` and `--generate-notes` are mutually exclusive in `gh`,
   so the curated CHANGELOG section is extracted in the Compose step and
   concatenated with the verify block before this step runs.
5. **Publish (un-draft)** — `gh release edit "$TAG" --repo evoila/meho
   --draft=false` flips the Release's visibility and fires the
   `release.published` webhook. If any prior step fails (compose,
   download, draft creation, asset upload), the workflow exits non-zero
   before this step runs and the draft persists for manual maintainer
   cleanup. No public Release is announced with missing or partial
   SBOMs — the strongest fail-closed contract `gh` permits.

#### Multi-arch caveat (single-platform SBOM)

Images are built for `linux/amd64` and `linux/arm64`, but Syft scans only
one platform per invocation and defaults to the runner's architecture
(`linux/amd64`). The published SBOMs therefore reflect amd64 dependencies
only. For the dependency-audit use case this is acceptable: Python and
Node component names and versions are identical across architectures —
only the resolved binary wheel differs — so a self-hoster running arm64
sees the same package list. Per-platform SBOMs are deferred until
enterprise demand surfaces; the workflow comments mark the entry point.

### License verification (per app startup)

1. The container starts; `meho_app/main.py` initialises the application.
2. `get_license_service()` is called via `@lru_cache(maxsize=1)`; first call
   reads `config.license_key` (sourced from `MEHO_LICENSE_KEY` env var).
3. If the env var is unset → `Edition.COMMUNITY`, no enterprise routes
   registered. The application starts.
4. If the env var is set → `_validate_license_key()` parses the
   `header.payload.signature` triple, decodes base64url, verifies the signature
   against the embedded `_PUBLIC_KEY_B64`, parses the payload as
   `LicensePayload`. Any expected validation error (bad signature, base64 or
   JSON decode failure, non-mapping payload, or `LicensePayload` schema
   mismatch) is debug-logged and returns `None`; `LicenseService.__init__`
   then sees `payload is None`, logs a warning, and falls back to community
   edition. Unexpected errors (e.g. a programmer error inside the verifier)
   propagate uncaught so they fail loud during development rather than
   silently masking a broken release.
5. If valid and not expired → `Edition.ENTERPRISE`, all features enabled.
6. If valid but expired within 30 days → `Edition.ENTERPRISE` with
   `in_grace_period=True`, warning logged with day count remaining.
7. If valid but more than 30 days expired → `Edition.COMMUNITY`, warning logged.

## Dependencies

### What this area depends on

- **GitHub Actions runners** — `ubuntu-latest` (currently `ubuntu-22.04`) for all jobs.
- **GHCR** (`ghcr.io`) — container registry for published images. Authentication via
  `GITHUB_TOKEN` (workflow-issued) for the release workflow.
- **`PUBLIC_REPO_PAT`** — repository secret. Classic PAT with `public_repo` scope. Used
  by the mirror workflow to push commits to `evoila/meho`, by `release.yml`'s
  `publish-to-public-repo` job to push tags and create Releases on
  `evoila/meho`, and by `pat-expiration-probe.yml` to monitor token validity.
  Cross-repo authentication is independent of the workflow's `permissions:`
  block — the PAT carries the user's permissions, not the workflow's.
- **`MEHO_LICENSE_KEY`** — runtime env var, sourced from operator deployment. Optional;
  unset means community mode.
- **GitHub Container Registry storage** — image layers, manifests.
- **GitHub Actions cache** (`type=gha`) — BuildKit cache backend; reduces multi-arch
  build times across runs of the release workflow.
- **`uv`** as the Python package manager — invoked via `astral-sh/setup-uv` and inside
  Docker builds via `COPY --from=ghcr.io/astral-sh/uv:latest`.
- **`docker/buildx`** — multi-arch image builds. Initialised per job by
  `docker/setup-buildx-action`.
- **QEMU** — ARM64 emulation on AMD64 runners. Initialised per job by
  `docker/setup-qemu-action`.
- **`cryptography`** Python library — Ed25519 verification in the licensing system.

### What depends on this area

- **Self-hosters** consuming `ghcr.io/evoila/meho-*` images. The image tag
  contract (`<version>`, `<major>.<minor>`, `latest`) is part of the public API.
- **`meho_app/core/config.py`** — reads `MEHO_LICENSE_KEY` via pydantic-settings,
  passes it to the `LicenseService`.
- **`meho_app/api/routes_*`** — enterprise routers gated by
  `Depends(require_enterprise)` patterns. Their inclusion in the FastAPI router tree
  is decided at startup by the licensing edition.
- **The frontend** — reads its edition state from `/api/v1/license` and conditionally
  shows enterprise UI. The endpoint serializes `LicenseInfo.to_api_response()`.

## Versioning conventions

MEHO follows [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html). The
project is currently in the `0.x` series, which per SemVer §4 means:

> Major version zero (0.y.z) is for initial development. Anything MAY change at any
> time. The public API SHOULD NOT be considered stable.

Implications for this stage:

- API endpoints, configuration keys, license-token payload schema, and image-tag
  conventions may change between any two minor versions in the 0.x stream.
- Self-hosters should pin to a specific `<major>.<minor>.<patch>` tag in production
  (`ghcr.io/evoila/meho-backend:0.1.0`), not to `latest` or `0.1`.
- A `0.MINOR` bump signals breaking changes; `0.MINOR.PATCH` is bugfix-only.
- The 1.0.0 cut is the inflection point at which API stability is committed.

The public artifact version is **decoupled from internal project milestone labels**.
Internal phases and milestones (`v2.3 Pre-Launch Hardening`, etc.) are work-cycle
labels for planning and have no relationship to the version stamped on
`pyproject.toml`, the git tag, or the `CHANGELOG.md` heading.

### Image tag conventions

Per `release.yml` and the `docker/metadata-action` configuration:

| Source | `meho-backend` | `meho-backend-slim` | `meho-frontend` |
|---|---|---|---|
| Tag `v0.1.0` | `0.1.0`, `0.1`, `latest` | `0.1.0`, `0.1` | `0.1.0`, `0.1`, `latest` |
| Tag `v0.1.0-rc.1` | `0.1.0-rc.1`, `0.1` | `0.1.0-rc.1`, `0.1` | `0.1.0-rc.1`, `0.1` |

The slim variant intentionally does not get `latest` — slim users must pin a version.

### `latest` policy for the 0.x series

**Decision**: keep `:latest` on `meho-backend` (full) and `meho-frontend` for the
entire 0.x series; warn loudly in `README.md` that production must pin.

The DevOps-pack axiom is *"no `:latest` tags in production compose files"* — the
risk of pulling a breaking change unintentionally outweighs the usability win.
SemVer 0.x makes this risk explicit: per
[SemVer 2.0.0 §4](https://semver.org/spec/v2.0.0.html#spec-item-4), any 0.MINOR
bump may break consumers. `:latest` would silently surface that.

We chose to keep `:latest` anyway because:

1. **Users will pull `:latest` regardless.** Removing the tag from `release.yml`
   doesn't stop a `docker pull ghcr.io/evoila/meho-backend` (which resolves to
   `:latest` by default) — it just means the resolved tag is whichever historic
   tag the registry still has marked, leading to *worse* surprises.
2. **The smoke-test path benefits.** Anyone evaluating MEHO via
   `docker compose up` against a curl'd compose snippet wants the latest stable
   working build without needing to know the current version number.
3. **The slim variant guards production users.** `meho-backend-slim` —
   intended for lean production deployments — never gets `:latest`. Users on
   the slim path must pin a `<major>.<minor>.<patch>` tag, the operational
   pattern we want production consumers on.

The user-facing warning lives in `README.md`'s top-of-file Pre-1.0 stability
callout (added in Initiative #504 alongside the version reset to 0.1.0):

> For production deployments, pin to a specific `<major>.<minor>.<patch>` image
> tag (e.g. `ghcr.io/evoila/meho-backend:0.1.0`) rather than `latest` or a
> floating `0.1` tag.

This decision is revisited at the 1.0 cut. Once API stability is committed,
`:latest` becomes the right default and the warning rotates out.

### CHANGELOG.md graduation pattern

The `[Unreleased]` heading at the top accumulates entries under Keep a Changelog
subsections (Added, Changed, Deprecated, Removed, Fixed, Security). At release time
the maintainer:

1. Replaces `[Unreleased]` with `[<version>] - <date>` (ISO 8601 date).
2. Adds a fresh empty `[Unreleased]` heading above.
3. Updates the comparison links at the bottom of the file:
   - `[Unreleased]: https://github.com/evoila/meho/compare/v<version>...HEAD`
   - `[<version>]: https://github.com/evoila/meho/releases/tag/v<version>`

The CHANGELOG.md entry must exist before the tag is pushed. The release pipeline
enforces this via the `validate-tag` pre-flight job (see
[Tag-validation pre-flight](#tag-validation-pre-flight) above): a tag whose
version has no `## [<version>]` heading in `CHANGELOG.md` fails the workflow
before any image is built.

## Licensing model

MEHO is open-core under AGPLv3. The community edition is fully functional for
single-tenant single-user use. Enterprise features (multi-tenancy beyond
`max_tenants=1`, advanced approval workflows, scheduled investigations, etc.) require
a signed license token.

### Token format

The license token is a JWT-shaped triple of base64url-encoded segments separated by
dots: `<header>.<payload>.<signature>`. Specifically:

- **Header**: a JSON object identifying the algorithm. Example: `{"alg":"EdDSA","typ":"MEHO-LICENSE"}`.
- **Payload**: a JSON object matching the `LicensePayload` model — `org`, `tier`,
  `features`, `issued_at`, `expires_at`, `max_tenants`, `license_id`.
- **Signature**: an Ed25519 signature over `<header_b64>.<payload_b64>` (the bytes
  of the joined string), base64url-encoded.

The signing key is **Ed25519**. The verifying public key is embedded in
`licensing.py:_PUBLIC_KEY_B64` as a base64url-encoded 32-byte string.

### Test vs production keys

`licensing.py` carries two embedded public keys. The active key is selected via
`MEHO_LICENSE_ENV`:

- Default → `_PUBLIC_KEY_B64`. Intended for production.
- `MEHO_LICENSE_ENV=test` → `_TEST_PUBLIC_KEY_B64`. Intended for unit and contract
  tests that mint short-lived test tokens with the matching test private key.

The test private key may live in the test fixtures; the production private key must
live only in a secrets manager.

### Grace period

A token whose `expires_at` is in the past but within 30 days continues to grant
`Edition.ENTERPRISE` with `in_grace_period=True`. Past 30 days, the edition drops
to `COMMUNITY` and a warning is logged. The grace period exists to avoid sudden
loss of enterprise functionality for honest customers who are mid-renewal.

The grace period trusts the system clock; an attacker setting the clock back can
extend it indefinitely. This is acceptable for the threat model — the goal is to
remind honest customers, not to stop a determined adversary.

### Issuance CLI

Production license tokens are minted by `scripts/issue-license.py` — a
maintainer-operated Typer CLI that reads the private signing key from
1Password, signs with Ed25519, records an audit-log row, and returns the token.
Three subcommands:

| Subcommand | Purpose |
|---|---|
| `issue` | Mint a signed token. Records the audit-log row before returning. Fail-closed. |
| `verify` | Validate a token against the embedded public key. Dev-sanity tool. |
| `decode` | Inspect a token's payload without verifying. For support cases. |

**Vault retrieval.** `issue` shells out to
`op read --no-newline <secret-ref>` for the private key, where `<secret-ref>`
is read from the `MEHO_LICENSE_SIGNING_KEY_REF` environment variable.
The production secret reference is documented in the maintainer custody
runbook (`.claude/operations/license-key-custody.md`); the runbook is
intentionally excluded from the public mirror so the URI does not appear in
this document, in the script source, or in CI logs. Pre-flight checks `op` on
PATH and an active session via `op whoami` so a missing CLI or expired session
fails before any keypair work. The retrieved value is held in process memory,
used to sign once, and dropped — never written to disk, never logged, never
echoed.

**Audit-log fail-closed.** `issue` calls
`LicenseAuditRepository.record_issuance(payload, issuer=<--issuer>, issuer_type="user")`
*before* returning the token. Any exception (DB unreachable, duplicate
`license_id`, validation failure) propagates out and aborts the issuance:
nothing is written to stdout, nothing to the `--output` file. `issuer_type`
is hardcoded to `"user"` because the issuance flow is maintainer-operated by
design — the custody runbook is the source of truth for who is allowed to run
the script, gated by 1Password vault membership.

**Output handling.** Without `--output`, the token is written to stdout (one
line, trailing newline) so it can be piped or captured. With `--output FILE`,
the token is written to FILE atomically at mode `0600` via `os.open` with
`O_WRONLY | O_CREAT | O_EXCL` plus an explicit mode argument; the call refuses
to overwrite an existing file. In both modes the `license_id` is surfaced to
stderr for the operator to record.

**Invocation.** Requires `op` on PATH, an active 1Password session for the
maintainer vault, `MEHO_LICENSE_SIGNING_KEY_REF` exported, and `DATABASE_URL`
pointing at a database with the `license_issuance` migration applied:

```bash
export MEHO_LICENSE_SIGNING_KEY_REF="<see-custody-runbook>"
uv run python scripts/issue-license.py issue \
  --org "Acme Corp" \
  --tier enterprise \
  --features multi_tenant,sso \
  --expires-at 2027-05-01 \
  --max-tenants 50 \
  --issuer "$(op whoami --format=json | jq -r .user.email)" \
  --output /secure/path/acme.token
```

**Out of scope at this layer:**

- Customer-facing self-service portal (deferred to v0.2).
- Bulk issuance from CSV (file follow-up if demand surfaces).
- Revocation tooling (audit-log columns reserved; CLI not built — v0.2).
- Authentication for who can run the CLI (handled by 1Password vault
  membership, not by the script).

### License-issuance audit log

Every signed enterprise token minted by `scripts/issue-license.py` is
recorded in an append-only `license_issuance` Postgres table. The table is the authoritative compliance
record: it answers *"which licenses have been issued, when, by whom, to whom,
and for how long"* without trusting any state that lives outside the database.

**Schema** (`meho_app/alembic/versions/0010_license_issuance_audit.py`):

| Column | Type | Notes |
|---|---|---|
| `license_id` | TEXT PRIMARY KEY | Mirrors `LicensePayload.license_id`; idempotency contract |
| `org` | TEXT NOT NULL | Customer organization |
| `tier` | TEXT NOT NULL | License tier (e.g. `enterprise`) |
| `features` | JSONB NOT NULL | Enabled feature list |
| `issued_at` | TIMESTAMPTZ NOT NULL | License claim time |
| `expires_at` | TIMESTAMPTZ | NULL = perpetual |
| `max_tenants` | INTEGER | Tenant cap |
| `issuer` | TEXT NOT NULL | Identity of the principal minting the token |
| `issuer_type` | TEXT NOT NULL | `user` or `service_account` (validated at the repository boundary) |
| `revoked_at` | TIMESTAMPTZ | Reserved for future revocation tooling |
| `revocation_reason` | TEXT | Reserved for future revocation tooling |
| `created_at` | TIMESTAMPTZ NOT NULL | Server clock at row insert (forensic) |

**Indexes**:

- `license_issuance_pkey` (`license_id`) — automatic from PK; satisfies `find_by_license_id`.
- `ix_license_issuance_org_issued_at` (`org`, `issued_at`) — composite, satisfies `list_by_org` (filter + order) without a sort step. Postgres uses a backwards btree scan for `ORDER BY issued_at DESC`.
- `ix_license_issuance_issued_at` (`issued_at`) — supports cross-org date-range reporting that v0.2 compliance work will likely add.

`license_id` is the primary key, so a duplicate write raises a SQLSTATE
`23505` violation that the repository surfaces as `DuplicateLicenseIDError`.
The split between `issued_at` (license claim) and `created_at` (server
clock at row insert) is forensic signal — divergence indicates clock skew
on the issuance host. Required string fields (`license_id`, `org`, `tier`,
`issuer`) are non-empty-validated at the repository boundary, and
`issuer_type` is validated against `{"user", "service_account"}` so a
typo never reaches the permanent compliance record.

**Repository** (`meho_app/modules/licensing/audit.py`):

```python
class LicenseAuditRepository:
    async def record_issuance(
        self,
        payload: LicensePayload,
        *,
        issuer: str,
        issuer_type: str,
    ) -> LicenseIssuance: ...

    async def find_by_license_id(self, license_id: str) -> LicenseIssuance | None: ...
    async def list_by_org(
        self, org: str, *, limit: int = 50, offset: int = 0
    ) -> list[LicenseIssuance]: ...
```

`record_issuance` owns its transaction: `commit()` on success, `rollback()`
on **any** commit failure (not just `IntegrityError`). A connection-level
failure at commit time — e.g. the backend gets terminated, the network
drops, or a statement times out — leaves the underlying SQLAlchemy
`AsyncSession` in a `PendingRollbackError` state that any subsequent write
on the same session would surface; rolling back universally guarantees the
session is reusable when the method returns. The caller is the issuance
CLI process, not a request handler sharing a session, so transaction
ownership lives here. The fail-closed contract is enforced at the
boundary: any exception raised by `record_issuance` aborts the issuance
flow before the signed token is returned to the customer.

**Caller integration** is implemented in `scripts/issue-license.py` (see the
[Issuance CLI](#issuance-cli) subsection above). The contract is: call
`record_issuance` *before* the function returns the signed token; treat any
exception as fatal; never hand a customer a token whose issuance row is not
durable.

**Out of scope at this layer**:

- Revocation tooling — columns are reserved; the CLI to set them is not
  built. Revocation is deferred until v0.2 (Goal #503 §"Out of scope").
- Customer-facing license-status endpoint.
- Retention pruning — the table is treated as append-only at v0.1.0
  scale.

## Known issues

### Linked to GitHub issues

The following gaps and deviations are tracked. References will be added once the
issues are filed.

- The production public key embedded in `licensing.py` is a one-shot placeholder; no
  vault-backed private key exists to mint matching tokens.
- ~~The release pipeline does not sign published images. Self-hosters cannot
  cryptographically verify image provenance.~~ Resolved: `release.yml` signs
  every published image with cosign keyless OIDC (see
  [Image signing with cosign](#image-signing-with-cosign) above).
- The release pipeline does not produce SBOM artifacts.
- ~~The release pipeline creates the GitHub Release on the private repo, not the
  public mirror — OSS users see no releases.~~ Resolved by the
  `publish-to-public-repo` job (see
  [How public-repo tagging works](#how-public-repo-tagging-works) below).
- ~~The release pipeline does not validate that the pushed tag matches `pyproject.toml`
  or the `CHANGELOG.md`.~~ Resolved by the `validate-tag` pre-flight job (see
  [Tag-validation pre-flight](#tag-validation-pre-flight) above).
- ~~The mirror workflow runs in `orphan` mode, which discards public history every
  run. Tags cannot accumulate on the public repo until this flips to `incremental`.~~
  Resolved: `PUBLIC_MIRROR_MODE` defaults to `incremental` and orphan mode is
  guarded against tag loss in `mirror-to-public.yml`.
- ~~No production license issuance pipeline exists. Customer onboarding is
  fully manual.~~ Resolved: `scripts/issue-license.py` mints signed
  enterprise tokens against the production Ed25519 key with fail-closed
  audit logging (see [Issuance CLI](#issuance-cli) above).
- No `RELEASING.md` runbook exists for maintainers cutting a release.
- Helm chart at `deploy/helm/meho/` is partial — backend Deployment +
  Service + chart-managed Secret, frontend Deployment + Service +
  (optional) Ingress, and Postgres/Redis subchart wiring all exist;
  helm-test CI workflow (#529) and the operator runbook (#530) remain
  pending under Initiative #506.
- The `Dockerfile.meho` and `Dockerfile.meho-frontend` images run as root.
- Several base images and GitHub Actions are pinned by tag, not by digest.
- `license-check.yml` is in WARN mode (`continue-on-error: true`); the gate does
  not actually block license-incompatible dependencies.
- The workflow filename `license-check.yml` is ambiguous against the customer-license
  concept; should be renamed for clarity.

## Python-specific notes

### Why `uv sync --frozen --no-install-project` runs twice

The Dockerfile runs `uv sync` once with `--no-install-project` (deps only), then
again without it (deps + project). The first invocation is the *cacheable* layer:
unless `pyproject.toml` or `uv.lock` change, this layer is pulled from the layer
cache instead of rebuilt. The second invocation runs only after source files have
been copied; it cache-busts on every code change but reuses the dependency
installation from the first layer.

This ordering — manifest before source — is a Docker build-cache pattern, not a
Python-specific one, but the `uv sync` semantics make it cleaner than `pip install
-r requirements.txt && pip install -e .` would.

### `--inexact` on the debug stage

The `debug` Dockerfile target runs `uv sync --frozen --group dev --inexact`. The
`--inexact` flag preserves any heavy groups (`docling-group`, `torch-group`)
installed in the `base` layer. Without it, `uv sync --group dev` reconciles the
venv to *exactly* the dev group's deps and uninstalls the heavy groups.

This is a Python-specific gotcha rooted in how venvs work — a venv is a flat
directory of installed packages, and `uv sync` makes it match the requested groups
unless told to leave non-requested deps alone.

### Why `python_keycloak` triggers a transitive `jwcrypto` security suppression

Documented inline in `security-scan.yml:134-143`. Importing the `keycloak` package
triggers `keycloak.__init__.py` → `keycloak_openid` → `from jwcrypto import jwk, jwt`,
which makes `jwcrypto` import-reachable but not call-reachable from MEHO code (MEHO
only references `KeycloakAdmin` and `keycloak.exceptions`, never any
`KeycloakOpenID` method that would exercise jwcrypto). The pip-audit `--ignore-vuln`
suppression is paired with revisit triggers: when jwcrypto ships a fix, when
`python-keycloak` switches its JOSE backend off jwcrypto, or when MEHO begins
calling `KeycloakOpenID` methods.

This pattern — reachability evidence + revisit triggers — is the project standard
for any pip-audit suppression and should be matched in any future addition.

## References

- [public-mirror.md](public-mirror.md) — sister document; covers the orthogonal
  private→public source projection.
- [first-run-experience.md](first-run-experience.md) — covers the `nginx.conf` and
  `config.js` runtime-config propagation model that the frontend image relies on.
- [bootstrap-and-migrations.md](bootstrap-and-migrations.md) — the
  `docker-entrypoint.sh` migration step is documented here.
- [docs/development/dual-repo-workflow.md](../development/dual-repo-workflow.md) —
  developer-facing operational guide for the private/public repo split.
- [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html)
- [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/)
- [SLSA — Supply-chain Levels for Software Artifacts](https://slsa.dev/) — the
  framework cosign + SBOM + provenance attestations target.
