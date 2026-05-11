# MEHO

> An MCP-native governance layer that lets any AI agent operate safely
> against shared infrastructure. Policy-gated. Audit-grade. Multi-tenant.

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)
[![OSS](https://img.shields.io/badge/OSS-public%20from%20day%201-success.svg)](./CONTRIBUTING.md#public-from-day-1-deliberately)

**Status:** v0.1 in development. The backplane image, the Helm chart, and
the operator CLI are all building toward the first tagged release; nothing
is GA yet.

## What this is

MEHO sits between AI agents (Claude Code, Cursor, Cline, Continue,
custom MCP clients) and the infrastructure they operate against
(Kubernetes, vCenter / VCF, NSX, public cloud, network appliances,
secrets stores). Every operation is policy-gated, every credential
short-lived and federated, every result reduced server-side, every
action broadcast to a real-time feed, every interaction audited,
every context lookup tenant-scoped and version-aware.

The agent runtime is *not* part of MEHO. Bring your own.

## Deploy

### Local (kind, ~5 min)

A fully local dev loop. Useful for iterating on chart plumbing, the
backplane's startup contract, and the CLI without touching real Vault /
Keycloak / Postgres.

```bash
# 1. Spin up a single-node kind cluster.
kind create cluster --name meho-dev

# 2. Apply the mock Postgres / Vault / Keycloak prerequisites documented
#    at the top of values-kind.yaml. (Mock Vault + Keycloak come up
#    cluster-internal so the chart's URI-validated fields resolve; real
#    federation requires the existing-k8s flow below.)
#    See deploy/values-examples/values-kind.yaml for the manifests.

# 3. Install the chart from its OCI artefact (published by Task #41).
#    Pin to an immutable image tag — sha-<git-sha> from a green CI run
#    on main, or a v<x.y.z> release tag. Goal #11 deploy discipline
#    forbids :latest and :main.
helm install meho-dev oci://ghcr.io/evoila/meho-chart \
  --version <chart-version> \
  -n meho --create-namespace \
  -f https://raw.githubusercontent.com/evoila/meho/main/deploy/values-examples/values-kind.yaml \
  --set image.tag=sha-<git-sha>

# 4. Verify the pod is up and the readiness probe is responding.
kubectl wait --for=condition=Ready pod \
  -l app.kubernetes.io/name=meho -n meho --timeout=2m
kubectl port-forward -n meho svc/meho-dev-meho 8000:8000 &
curl localhost:8000/healthz
```

`values-kind.yaml` disables ingress + NetworkPolicy (kind ships neither
out of the box) and points at in-cluster mock Vault + Keycloak. Operator
identity is **faked** — for real federation, use the existing-k8s path
below.

### Existing k8s (~5 min, requires Vault + Keycloak + Postgres)

The supported v0.1 deploy shape: a Kubernetes cluster running an
ingress-nginx controller, a HashiCorp Vault with the `meho-mcp` OIDC
role bound to your Keycloak issuer, a Keycloak realm + client fronting
the backplane, and a PostgreSQL database reachable from the cluster.
This is the shape the RDC Hetzner dogfooding lab runs.

```bash
helm upgrade --install meho oci://ghcr.io/evoila/meho-chart \
  --version <chart-version> \
  -n meho --create-namespace \
  -f your-values.yaml
```

See [`deploy/values-examples/values-rdc-example.yaml`](./deploy/values-examples/values-rdc-example.yaml)
for the shape `your-values.yaml` should follow and
[`deploy/values-examples/README.md`](./deploy/values-examples/README.md)
for the **External Secrets Operator (ESO) sync patterns** the chart
expects. Required: Vault address + role, Keycloak issuer + audience,
Postgres credentials Secret (synced from Vault by ESO).

### Verify image + chart + CLI signatures

Every operator-facing artefact is cosign keyless-signed (ADR 0006) under
the workflow that produced it. There is no public key to distribute —
verification compares the Fulcio-issued certificate's `subject` against
a regex anchored on the source workflow's path and tag ref. A maliciously
re-tagged fork cannot produce a bundle that satisfies it.

```bash
# Container image (signed by .github/workflows/image.yml on push)
cosign verify ghcr.io/evoila/meho:<tag> \
  --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/image\.yml@refs/(heads/main|tags/v.+)$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'

# Helm chart (signed by .github/workflows/chart.yml on every chart push)
cosign verify ghcr.io/evoila/meho-chart:<version> \
  --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/chart\.yml@refs/(heads/main|tags/v.+)$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'

# CLI release tarball (signed by .github/workflows/cli-release.yml on v* tags)
cosign verify-blob \
  --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/cli-release\.yml@refs/tags/v.+$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  --bundle meho_<version>_linux_amd64.tar.gz.cosign.bundle \
  meho_<version>_linux_amd64.tar.gz
```

Full CLI install + verification recipe lives at
[Verifying CLI release artefacts](#verifying-cli-release-artefacts);
chart and image specifics live in
[`cli/README.md`](./cli/README.md#verify-signatures) and
[`docs/codebase/devops.md`](./docs/codebase/devops.md).

## Architecture

MEHO ships as three operator-facing artefacts:

- **Backplane** — Python / FastAPI service that brokers MCP operations
  against infrastructure. Receives short-lived Keycloak-issued OIDC
  tokens, exchanges them with Vault for backend credentials, executes
  policy-gated operations, writes audit rows to PostgreSQL, and
  broadcasts activity to Valkey. Container image at
  `ghcr.io/evoila/meho`. Codebase walkthrough:
  [`docs/codebase/backend.md`](./docs/codebase/backend.md).
- **Helm chart** — `meho-chart` published as an OCI artefact at
  `ghcr.io/evoila/meho-chart` (Task #41). Renders the Deployment,
  Service, Ingress, NetworkPolicy, pre-install migration Job, the
  bundled Valkey broadcast subchart (ADR 0005), and the typed
  `values.schema.json` contract (Task #38) that rejects misconfigured
  installs at `helm install` / `helm upgrade` / `helm template`. Chart
  walkthrough: [`docs/codebase/devops.md`](./docs/codebase/devops.md).
- **Operator CLI** — `meho` Go binary (cobra). Wires `version`,
  `login` (Keycloak device-code flow, Task #44), and `status`
  (server-driven discovery, Task #45) for v0.1. Released as
  multi-platform tarballs (`linux/macOS × amd64/arm64`) on every
  `v*` tag, each individually cosign-signed (Task #47). CLI
  walkthrough: [`docs/codebase/cli.md`](./docs/codebase/cli.md).

The agent runtime (Claude Code, Cursor, …) lives outside the deploy
contract — operators bring their own MCP client. MEHO is the layer
that turns "any MCP client" into "any MCP client *operating against
real infrastructure under audit*."

## Container image

The backplane is published to GitHub Container Registry as a multi-arch
manifest (`linux/amd64` + `linux/arm64`):

```bash
# Pin to an immutable commit-sha tag (recommended for deploys):
docker pull ghcr.io/evoila/meho:sha-<40-char-git-sha>

# Latest tip of main (moving target — use for development only):
docker pull ghcr.io/evoila/meho:main

# Tagged release:
docker pull ghcr.io/evoila/meho:v0.1.0
```

**No `:latest` tag is ever published** — operators must pin to an
immutable `:sha-<...>` or `:v<x.y.z>` reference (Goal #11 deploy
discipline). Every image is cosign-signed (Task #34) using the same
keyless flow described above.

### Maintainer one-time setup

The first time `image.yml` pushes to `ghcr.io/evoila/meho`, GHCR creates
the package as **private**. A maintainer must flip visibility to
**public** once so anonymous `docker pull` works:

```bash
gh api --method PATCH /orgs/evoila/packages/container/meho \
  -f visibility=public
```

Or via the UI: GitHub org `evoila` → Packages → `meho` → Package settings →
Change visibility → **Public**.

Verify:

```bash
gh api /orgs/evoila/packages/container/meho --jq '.visibility'   # -> "public"
docker logout ghcr.io && docker pull ghcr.io/evoila/meho:main    # -> succeeds
```

## Verifying CLI release artefacts

Every CLI tarball published at <https://github.com/evoila/meho/releases>
is signed via cosign keyless (ADR 0006). The signature, the
Fulcio-issued certificate, and the Rekor transparency-log inclusion
proof are bundled into a single `.cosign.bundle` JSON file attached
to the release alongside each `.tar.gz` and the combined `SHA256SUMS`
file.

```bash
TAG=v0.1.0
TARBALL=meho_${TAG#v}_linux_amd64.tar.gz
BASE=https://github.com/evoila/meho/releases/download/${TAG}

curl -LO ${BASE}/${TARBALL}
curl -LO ${BASE}/${TARBALL}.cosign.bundle

cosign verify-blob \
  --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/cli-release\.yml@refs/tags/v.+$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  --bundle ${TARBALL}.cosign.bundle \
  ${TARBALL}
# Verified OK

tar xzf ${TARBALL}
sudo install meho /usr/local/bin/
meho version
```

The identity-claim regex is anchored on `cli-release.yml` and
`refs/tags/v` — same shape as `chart.yml` (chart signing) and
`image.yml` (container image signing) per ADR 0006. A maliciously
re-tagged workflow on a fork cannot produce a bundle that satisfies
it.

`SHA256SUMS` is signed the same way; verify it once, then
`sha256sum -c SHA256SUMS` against any subset of tarballs the
operator actually downloaded (two-step trust chain). The full
recipe and ADR-0006 rationale live in
[`cli/README.md#verify-signatures`](./cli/README.md#verify-signatures).

## Helm chart values reference

The deploy contract lives at [`deploy/charts/meho/`](./deploy/charts/meho/).
`values.yaml` ships safe-by-default — every field the backplane cannot
start without is **blank** and the bundled
[`values.schema.json`](./deploy/charts/meho/values.schema.json) (JSON
Schema draft-07, Task #38) rejects empty required values at
`helm install` / `helm upgrade` / `helm template` time with a clear
path. Unknown keys at any level fail with
`additional properties '<name>' not allowed`.

Operator-required (MUST be set; the schema rejects empty defaults):

| Path | Type | Notes |
| --- | --- | --- |
| `image.tag` | string | Immutable tag (`sha-<git-sha>` or `v<x.y.z>`); never `:latest`. |
| `ingress.host` | string (`hostname`) | External hostname the chart publishes. Required only when `ingress.enabled: true` (default); skipped when ingress is disabled. |
| `ingress.tls.secretName` | string | TLS Secret (cert-manager-managed or pre-provisioned). Required only when both `ingress.enabled` and `ingress.tls.enabled` are true. |
| `postgres.credentialsSecret` | string | Kubernetes Secret holding `DATABASE_URL` at key `url`. |
| `vault.address` | string (`uri`) | Vault endpoint, e.g. `https://vault.example.org`. |
| `keycloak.issuer` | string (`uri`) | Keycloak issuer URL (used for `iss` validation + JWKS discovery). |
| `config.keycloakIssuerUrl` | string | ConfigMap mirror of the above; consumed by the backplane env. |
| `config.keycloakAudience` | string | Keycloak client ID fronting the backplane. |
| `config.vaultAddr` | string (`uri`) | ConfigMap mirror of `vault.address`. |
| `networkPolicy.postgresCIDR` | CIDR (IPv4) | Egress CIDR; pattern-validated. Required only when `networkPolicy.enabled: true` (default). |
| `networkPolicy.vaultCIDR` | CIDR (IPv4) | Same. |
| `networkPolicy.keycloakCIDR` | CIDR (IPv4) | Same. |

Common operator overrides (safe defaults provided; tune as needed):

| Path | Default | Notes |
| --- | --- | --- |
| `replicaCount` | `1` | v0.1 ships single-replica; HA lands in v0.2. |
| `image.repository` | `ghcr.io/evoila/meho` | OCI repo from the G2.4 image pipeline. |
| `image.pullPolicy` | `IfNotPresent` | `Always` \| `IfNotPresent` \| `Never`. |
| `service.type` / `service.port` | `ClusterIP` / `8000` | Service shape. |
| `ingress.className` | `""` | Cluster default IngressClass when empty. |
| `probes.liveness.*` / `probes.readiness.*` | `/healthz` / `/ready` httpGet + tuned timings | Operator-tunable; never disabled. |
| `resources.requests` / `resources.limits` | `100m`/`256Mi` / `1000m`/`1Gi` | Conservative v0.1 chassis baselines. |
| `networkPolicy.ingressControllerNamespace` | `ingress-nginx` | RKE2 default; override per cluster. |
| `audit.postgresOnly` | `true` | v0.1; S3 mirror is v0.2. |
| `broadcast.enabled` | `true` | v0.1 deploys its own Valkey subchart (G2.5-T3). |
| `connectors.enabled` | `[]` | v0.1 chassis ships no connectors. |

See [`docs/codebase/devops.md`](./docs/codebase/devops.md) for the full
chart contract, probe semantics, NetworkPolicy posture, install/upgrade
flow, and verification commands.

## Documentation

Codebase walkthroughs:

- **Backend** — [`docs/codebase/backend.md`](./docs/codebase/backend.md)
- **CLI** — [`docs/codebase/cli.md`](./docs/codebase/cli.md)
- **Chart + deploy** — [`docs/codebase/devops.md`](./docs/codebase/devops.md)

`docs.meho.ai` (rendered reference docs, runbooks, connector authoring
guide) lands in v0.2 once the first connector + wrapper-replacement
work in Goal #59 closes.

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md). Contributions require a
Developer Certificate of Origin sign-off (`git commit -s`) — there is
no CLA. Public-from-day-1: every line of MEHO ships on `evoila/meho`
from commit #1; operator-sensitive coordination lives in
`evoila-bosnia/meho-internal` (issues + ADRs only, no code).

## Security

Vulnerability reports: see [`SECURITY.md`](./SECURITY.md).

## Changelog

See [`CHANGELOG.md`](./CHANGELOG.md). Project-wide (image + chart + CLI
under one document) in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format. The top-level CHANGELOG is the authoritative source for the
narrative attached to every GitHub Release — the CLI release pipeline
extracts the matching section via `--release-notes` rather than
auto-generating from git log.

## License

[Apache License 2.0](./LICENSE). Per ADR 0001 (license selection) and
the project's inbound = outbound discipline: every contribution flows
in under the same Apache 2.0 terms via the DCO sign-off — there is no
separate CLA.

## History

This repository was bootstrapped on 2026-05-09 as a strategic reset.
The prior MEHO codebase lives at `evoila-bosnia/MEHO.X`, deprecated
and retained for reference.
