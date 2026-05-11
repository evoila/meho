# MEHO

> Governance backplane for AI agents acting on infrastructure —
> policy-gated, audit-grade, MCP-native. Apache 2.0.

**Status:** v0.1 in development. No released artifact yet.

## What this is

MEHO sits between AI agents (Claude Code, Cursor, Cline, Continue,
custom MCP clients) and the infrastructure they operate against
(Kubernetes, vCenter / VCF, NSX, public cloud, network appliances,
secrets stores). Every operation is policy-gated, every credential
short-lived and federated, every result reduced server-side, every
action broadcast to a real-time feed, every interaction audited,
every context lookup tenant-scoped and version-aware.

The agent runtime is *not* part of MEHO. Bring your own.

## Status

This repository is in active development toward v0.1. There is
nothing to install yet. Watch the repo for the v0.1 announcement.

## Quickstart

(Placeholder — full v0.1 install / smoke / upgrade path lands with
the release.)

For the backplane (Python / FastAPI) skeleton — `uv` and Docker
recipes for running it locally — see [`backend/README.md`](./backend/README.md).

For the `meho` operator CLI (Go / cobra) — build, install, and
`meho version` recipes — see [`cli/README.md`](./cli/README.md). The
CLI ships as a single static binary; v0.1 wires `version`, `login`,
and `status`, plus a cosign-signed multi-platform release pipeline
(linux/macOS × amd64/arm64). Operator-side signature-verification
recipe is below in [Verifying CLI release artefacts](#verifying-cli-release-artefacts).

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
discipline).

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
Schema draft-07) rejects empty required values at `helm install` /
`helm upgrade` / `helm template` time with a clear path. Unknown keys at
any level fail with `additional properties '<name>' not allowed`.

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
| `broadcast.redis.bundled` | `true` | v0.1 deploys its own Redis subchart (G2.5-T3). |
| `connectors.enabled` | `[]` | v0.1 chassis ships no connectors. |

See [`docs/codebase/devops.md`](./docs/codebase/devops.md) for the full
chart contract, probe semantics, NetworkPolicy posture, install/upgrade
flow, and verification commands.

## Deploy

A sanitized example values file for a Vault + Keycloak + Postgres +
ingress-nginx-shaped cluster (the same shape as the RDC Hetzner
dogfooding lab) lives at
[`deploy/values-examples/values-rdc-example.yaml`](./deploy/values-examples/values-rdc-example.yaml);
the substitution recipe and the **External Secrets Operator (ESO) sync
patterns** the chart expects are documented in
[`deploy/values-examples/README.md`](./deploy/values-examples/README.md).

Once v0.1 is released the chart will also be published as an OCI artifact
at `oci://ghcr.io/evoila/meho-chart` (Task #41); until then, install
directly from the repository tree:

```bash
helm upgrade --install meho ./deploy/charts/meho/ \
  --namespace meho --create-namespace \
  -f deploy/values-examples/values-rdc-example.yaml \
  --set image.tag=sha-<git-sha>
  # ...plus the substitutions documented in deploy/values-examples/README.md
```

## Documentation

(Placeholder — `docs.meho.ai` will land before v0.1.)

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md). Contributions require a
Developer Certificate of Origin sign-off (`git commit -s`).

## Security

Vulnerability reports: see [`SECURITY.md`](./SECURITY.md).

## License

[Apache License 2.0](./LICENSE).

## History

This repository was bootstrapped on 2026-05-09 as a strategic reset.
The prior MEHO codebase lives at `evoila-bosnia/MEHO.X`, deprecated
and retained for reference.
