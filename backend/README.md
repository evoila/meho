# meho-backplane

The MEHO governance-layer backplane (Python / FastAPI). v0.1 ships a
minimum-viable chassis only — health, version, readiness, metrics, and
authn/authz federation are layered on by Tasks #19, #20 and the G2.2 /
G2.3 Initiatives.

Stack choices are locked in [ADR
0004](https://github.com/evoila-bosnia/meho-internal/issues/13)
(Python / FastAPI / Pydantic v2 / SQLAlchemy 2.x async / Alembic).

## Layout

```text
backend/
├── pyproject.toml              # uv-managed; locks deps + tool configs
├── Dockerfile                  # multi-stage uv build, non-root uid 1001
├── .dockerignore
├── src/
│   └── meho_backplane/
│       ├── __init__.py         # __version__ marker
│       └── main.py             # FastAPI `app`, root identity route
└── tests/
    └── test_app_starts.py
```

The `src/`-layout convention prevents tests from accidentally
importing the in-tree source — they only resolve the installed
package.

## Run locally

Requires [uv](https://docs.astral.sh/uv/) ≥ 0.4 and Python 3.12.

```bash
cd backend/
uv sync                                              # resolves deps from uv.lock
uv run ruff check src/ tests/                        # lint
uv run ruff format --check src/ tests/               # format check
uv run mypy src/                                     # type-check
uv run pytest -x                                     # unit tests

uv run uvicorn meho_backplane.main:app --port 8000   # boot the app
curl -s localhost:8000/ | jq .                       # → {"name":"meho-backplane","version":"0.1.0-dev"}
```

## Build and run the container

```bash
cd backend/
docker build -t meho-backplane:dev .
docker run --rm -d -p 8000:8000 --name meho-backplane meho-backplane:dev
curl -s localhost:8000/ | jq .
docker exec meho-backplane id -u                     # → 1001
docker rm -f meho-backplane
```

The image runs as **uid 1001** in the root group (gid 0) so the
runtime filesystem can stay read-only when the orchestrator sets
`readOnlyRootFilesystem: true` (configured in the Helm chart in
G2.5). The base image is `python:3.12-slim` pinned by manifest-list
digest (see `Dockerfile` — `ARG PYTHON_BASE_DIGEST`); the runtime
stage contains only the locked virtualenv, no build tools.

## Multi-arch build (linux/amd64 + linux/arm64)

The image is published for both `linux/amd64` (Hetzner deploy target)
and `linux/arm64` (Apple Silicon developer machines). Building the
manifest list locally requires `docker buildx` with QEMU registered
for the non-native architecture.

```bash
cd backend/

# One-time: register QEMU for cross-arch emulation on an amd64 host
# (and create a dedicated builder so the default builder stays
# untouched).
docker run --privileged --rm tonistiigi/binfmt --install all
docker buildx create --use --name meho-builder

# Build both architectures into a manifest list. `--load` only works
# with a single platform — to inspect both archs locally, push to a
# local registry or omit `--load` and use `buildx imagetools` after
# pushing to a registry.
docker buildx build \
  --platform=linux/amd64,linux/arm64 \
  --build-arg GIT_SHA=$(git rev-parse HEAD) \
  --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
  -t meho-backplane:multiarch-test \
  --output=type=image,push=false \
  .

# Inspect the manifest list (after pushing to a registry — buildx
# does not write multi-arch manifests to the local docker daemon).
docker buildx imagetools inspect meho-backplane:multiarch-test

# Per-arch single-platform builds (loadable into the local daemon):
docker buildx build --platform=linux/amd64 --load -t meho-backplane:amd64 .
docker buildx build --platform=linux/arm64 --load -t meho-backplane:arm64 .

# Smoke-test the resulting image — `platform.machine()` is the arch
# Python sees, which matches the build platform.
docker run --rm --platform=linux/amd64 meho-backplane:amd64 \
  python -c "import platform; print(platform.machine())"     # → x86_64
docker run --rm --platform=linux/arm64 meho-backplane:arm64 \
  python -c "import platform; print(platform.machine())"     # → aarch64
```

**Expect arm64 builds on an amd64 host to take 3–5× longer than the
native amd64 build** — QEMU user-mode emulation translates every
guest instruction, and `uv`'s wheel-install step is heavy on
compiled extensions (`asyncpg`, `cryptography`, `pydantic-core`).
The CI pipeline (G2.4-T2) runs both architectures in parallel jobs
so wall-clock time is bounded by the slower job, not the sum.
`docs/codebase/backend.md` records the cost detail and the refresh
policy for `PYTHON_BASE_DIGEST`.

## Verifying image signatures

Every image published to `ghcr.io/evoila/meho` from `.github/workflows/image.yml`
is signed with [cosign](https://github.com/sigstore/cosign) keyless OIDC. There
are **no private keys** to distribute — verification works against the public
Sigstore trust root (Fulcio CA + Rekor transparency log) plus the expected
**certificate identity** (which workflow file + ref produced the signature).

The signature is bound to the manifest-list digest, so the same signature
verifies every tag alias (`:sha-<long>`, `:main`, `:v<x.y.z>`) and every
per-architecture child manifest under that digest.

Verify the `:main` rolling tag:

```bash
cosign verify ghcr.io/evoila/meho:main \
  --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/image\.yml@refs/heads/main$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  | jq .
```

Verify a `v*` release tag:

```bash
cosign verify ghcr.io/evoila/meho:v0.1.0 \
  --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/image\.yml@refs/tags/v.*$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  | jq .
```

Verify by immutable digest (most defensible — content-addressed, never moves):

```bash
DIGEST=$(docker buildx imagetools inspect ghcr.io/evoila/meho:main \
  --format '{{json .Manifest}}' | jq -r '.digest')

cosign verify ghcr.io/evoila/meho@${DIGEST} \
  --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/image\.yml@.*$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  | jq .
```

A successful verification prints a JSON array of signature payload objects;
exit status is `0`. Verification failure (wrong identity, unsigned image,
tampered registry) exits non-zero with a structured error.

These are the same identity-regex + issuer values
[`claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)'s
`install.sh` uses as a **gating** check before pulling the image (per Goal #11
cross-repo coordination).

## Verifying the SBOM attestation

Every pushed image carries an [SPDX 2.x JSON](https://spdx.dev/specifications/)
Software Bill of Materials, generated by [syft](https://github.com/anchore/syft)
and attached as a [cosign attestation](https://docs.sigstore.dev/cosign/verifying/attestation/).
The attestation is signed under the **same keyless identity as the image
itself** (`image.yml@<ref>`), so SBOM provenance and image provenance verify
through a single chain.

Like the image signature, the attestation is bound to the manifest-list digest
— it covers every tag alias and every per-architecture child manifest without
separate invocations.

Verify the attestation and print the SBOM payload:

```bash
cosign verify-attestation \
  --type spdxjson \
  --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/image\.yml@refs/heads/main$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  ghcr.io/evoila/meho:main \
  | jq -r '.payload' | base64 -d | jq '.predicate'
```

The `--certificate-identity-regexp` field accepts the same shapes used for
plain `cosign verify` above — swap `refs/heads/main` for `refs/tags/v.*` to
verify a release tag attestation, or use `@.*` to accept any ref.

Verification failure exits non-zero. On success, `jq '.predicate'` prints the
SPDX document; `jq '.predicate.packages | length'` returns the package count
(expect ~120 — Python interpreter + FastAPI + every locked dep in
`uv.lock`).

The downstream `install.sh` from
[`claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)
can run this exact command to confirm the SBOM is intact and the bill of
materials matches the image being pulled.

## Verifying chart signatures

The Helm chart at [`deploy/charts/meho/`](../deploy/charts/meho/) is published
as a public OCI artefact at `oci://ghcr.io/evoila/meho-chart` and signed with
the same cosign keyless OIDC flow as the image, from
`.github/workflows/chart.yml`. The chart's GHCR package is intentionally a
separate package from the image package (`ghcr.io/evoila/meho`) so visibility,
retention, and signing identities can be managed independently.

Like the image signature, the chart signature is bound to the OCI manifest
digest, so the same signature verifies every tag alias of that digest.

Verify a calver release (main push):

```bash
cosign verify ghcr.io/evoila/meho-chart:0.1.20260510-abc1234 \
  --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/chart\.yml@refs/heads/main$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  | jq .
```

Verify a `v*` release tag:

```bash
cosign verify ghcr.io/evoila/meho-chart:0.1.0 \
  --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/chart\.yml@refs/tags/v.*$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  | jq .
```

Accept any ref (calver or tag) by widening the identity regex:

```bash
cosign verify ghcr.io/evoila/meho-chart:<version> \
  --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/chart\.yml@.*$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  | jq .
```

A successful verification prints a JSON array of signature payload objects;
exit status is `0`. Verification failure (wrong identity, unsigned chart,
tampered registry) exits non-zero with a structured error.

## Pulling the chart anonymously

The chart's GHCR package is **public** — no GitHub login is required to
pull it. The CI pipeline asserts this on every main / `v*` tag push by
running a dedicated `verify-anonymous-pull` job in a clean environment
that never calls `helm registry login` (Goal #11 DoD).

```bash
docker logout ghcr.io 2>/dev/null
helm registry logout ghcr.io 2>/dev/null
helm pull oci://ghcr.io/evoila/meho-chart --version <version>
ls meho-chart-*.tgz
```

This works from any network with outbound internet — no GitHub login
required. If the pull fails with `unauthorized` and you have not logged
in to GHCR, the chart package is private and a maintainer needs to flip
visibility to public (one-time, per package — same pattern as the image
package on its first push):

```bash
gh api --method PATCH /orgs/evoila/packages/container/meho-chart \
  -f visibility=public
```

After the chart is pulled, inspect it with `helm show chart`:

```bash
helm show chart oci://ghcr.io/evoila/meho-chart --version <version>
```

The published chart's `name` field is `meho-chart` (the OCI artefact
basename) while `app.kubernetes.io/name` labels on rendered resources
remain `meho` — the chart's `values.yaml` defaults `nameOverride: meho`
to preserve the label invariant. Operators selecting against the rendered
resources continue to use `app.kubernetes.io/name=meho`.

## Vulnerability scan

Every pushed image is scanned by [trivy](https://github.com/aquasecurity/trivy)
for known CVEs in OS packages and language dependencies. Findings are
surfaced two ways:

1. **GitHub Security tab.** The workflow uploads the SARIF report to repo →
   *Security* → *Code scanning alerts* (filter category `trivy-image-scan`).
   Severity counts (CRITICAL / HIGH only — LOW + MEDIUM are filtered out at
   scan time) appear inline on the alert list.
2. **Workflow artefact.** `trivy-results.sarif` attaches to each Actions run
   for **30 days**. Download via:

   ```bash
   gh run download <run-id> --repo evoila/meho --name trivy-results
   jq '.runs[0].results | length' trivy-results.sarif
   ```

**v0.1 is report-only.** The workflow stays green even on critical CVEs — the
deliberate split per Goal #11 is "scan now, gate later" so the team can
establish a baseline before committing to a remediation policy. v0.2 will
flip on a severity-based gate (likely `exit-code: 1` on `CRITICAL`) once the
noise level is known. Until then, treat the Security tab as the working
backlog: triage findings, file follow-ups, but do not expect builds to break
on them.

## What this skeleton intentionally omits

| Surface          | Lands in       |
| ---------------- | -------------- |
| `/healthz` / `/version` / `/ready` | Task #19   |
| Prometheus `/metrics`              | Task #20   |
| structlog JSON logs + middleware   | Task #20   |
| Keycloak JWT validation            | Initiative G2.2 |
| Vault OIDC, static-cred read       | Initiative G2.2 |
| SQLAlchemy + Alembic               | Initiative G2.3 |
| Multi-arch image, GHCR, cosign, SBOM, trivy | Initiative G2.4 |
| Helm chart deploying this image    | Initiative G2.5 |
| CI workflow exercising this tree   | Initiative G2.7 |

## References

- Goal #11 — Deployable v0.1
- Initiative #17 — G2.1 Backplane chassis
- Task #18 (this) — backplane Python source-tree bootstrap
- ADR 0003 — SPDX header convention (every authored Python file)
- [FastAPI tutorial](https://fastapi.tiangolo.com/tutorial/)
- [uv project guide](https://docs.astral.sh/uv/concepts/projects/)
- [uv Docker pattern](https://docs.astral.sh/uv/guides/integration/docker/)
- [Sigstore cosign keyless signing overview](https://docs.sigstore.dev/cosign/signing/overview/)
- [Sigstore CI quickstart (GitHub Actions OIDC)](https://docs.sigstore.dev/quickstart/quickstart-ci/)
- [`sigstore/cosign-installer`](https://github.com/sigstore/cosign-installer) action
- [syft (SBOM generator)](https://github.com/anchore/syft) and [`anchore/sbom-action`](https://github.com/anchore/sbom-action)
- [Sigstore cosign attestation verification](https://docs.sigstore.dev/cosign/verifying/attestation/)
- [SPDX 2.x specification](https://spdx.dev/specifications/)
- [trivy (vulnerability scanner)](https://github.com/aquasecurity/trivy) and [`aquasecurity/trivy-action`](https://github.com/aquasecurity/trivy-action)
