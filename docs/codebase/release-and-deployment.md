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
the mirror; the mirror runs against `main` whenever CI completes. The release pipeline
reads the version from the git tag, never from `pyproject.toml` or `CHANGELOG.md`. The
licensing system has no version concept at all. Coordinating a release across all four
is a manual maintainer responsibility today (see [RELEASING.md](../../RELEASING.md)
once it exists, otherwise this document).

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
  workflow. Three jobs in two phases: `build-backend` (matrix: `full` / `slim`),
  `build-frontend`, then `create-release`. Multi-arch builds (`linux/amd64`,
  `linux/arm64`) via QEMU + Buildx. Cache backed by GitHub Actions cache
  (`type=gha,mode=max`).
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
  one-shot Ed25519 keypair generator. Prints both halves to stdout; the maintainer
  is expected to copy the public half into `licensing.py` and store the private half
  in a vault.

### Absent surfaces (gaps)

- **No `RELEASING.md`** — maintainer release procedure is undocumented.
- **No production license issuance** — there is no script to mint a signed license
  token for a customer. Keypair generation exists; token issuance does not.
- **No K8s manifests / Helm chart** — self-hosters running on Kubernetes must hand-roll
  Deployments, Services, and Ingress from `docker-compose.yml`.
- **No image signing** — GHCR images are not signed by cosign or any provenance tooling.
- **No SBOM artifact** — release artifacts include no CycloneDX or SPDX SBOM.

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
3. Three matrix jobs run in parallel:
   - `build-backend` variant `full` — builds with `INCLUDE_DOCLING=true`,
     pushes to `ghcr.io/evoila/meho-backend:<tag>` (plus `<major>.<minor>` and
     `latest`).
   - `build-backend` variant `slim` — builds with `INCLUDE_DOCLING=false`,
     pushes to `ghcr.io/evoila/meho-backend-slim:<tag>` (plus `<major>.<minor>`,
     no `latest`).
   - `build-frontend` — builds the SPA + nginx image, pushes to
     `ghcr.io/evoila/meho-frontend:<tag>` (plus `<major>.<minor>` and `latest`).
4. Each build is multi-arch (`linux/amd64,linux/arm64`) via QEMU emulation.
5. After all three image jobs succeed, `create-release` runs `gh release create
   <tag> --generate-notes` on the workflow's repository — which today is the
   private repo. The public repo receives no Release object.

### License verification (per app startup)

1. The container starts; `meho_app/main.py` initialises the application.
2. `get_license_service()` is called via `@lru_cache(maxsize=1)`; first call
   reads `config.license_key` (sourced from `MEHO_LICENSE_KEY` env var).
3. If the env var is unset → `Edition.COMMUNITY`, no enterprise routes
   registered. The application starts.
4. If the env var is set → `_validate_license_key()` parses the
   `header.payload.signature` triple, decodes base64url, verifies the signature
   against the embedded `_PUBLIC_KEY_B64`, parses the payload as
   `LicensePayload`. On any error → community fallback with a warning log.
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
  by the mirror workflow to push to `evoila/meho`. Not used directly by the release
  pipeline today (will be needed once the release pipeline coupling to public repo
  is implemented).
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

### CHANGELOG.md graduation pattern

The `[Unreleased]` heading at the top accumulates entries under Keep a Changelog
subsections (Added, Changed, Deprecated, Removed, Fixed, Security). At release time
the maintainer:

1. Replaces `[Unreleased]` with `[<version>] - <date>` (ISO 8601 date).
2. Adds a fresh empty `[Unreleased]` heading above.
3. Updates the comparison links at the bottom of the file:
   - `[Unreleased]: https://github.com/evoila/meho/compare/v<version>...HEAD`
   - `[<version>]: https://github.com/evoila/meho/releases/tag/v<version>`

The CHANGELOG.md entry must exist before the tag is pushed. The release pipeline does
not currently enforce this, but a future tag-validation job will.

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

## Known issues

### Linked to GitHub issues

The following gaps and deviations are tracked. References will be added once the
issues are filed.

- The production public key embedded in `licensing.py` is a one-shot placeholder; no
  vault-backed private key exists to mint matching tokens.
- The release pipeline does not sign published images. Self-hosters cannot
  cryptographically verify image provenance.
- The release pipeline does not produce SBOM artifacts.
- The release pipeline creates the GitHub Release on the private repo, not the
  public mirror — OSS users see no releases.
- The release pipeline does not validate that the pushed tag matches `pyproject.toml`
  or the `CHANGELOG.md`.
- The mirror workflow runs in `orphan` mode, which discards public history every
  run. Tags cannot accumulate on the public repo until this flips to `incremental`.
- No production license issuance pipeline exists. Customer onboarding is
  fully manual.
- No `RELEASING.md` runbook exists for maintainers cutting a release.
- No K8s manifests or Helm chart for self-hosters running on Kubernetes.
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
