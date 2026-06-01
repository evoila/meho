# MEHO

> Governance backplane for AI agents acting on infrastructure —
> policy-gated, audit-grade, MCP-native. Apache 2.0.

[![Release](https://img.shields.io/github/v/release/evoila/meho)](https://github.com/evoila/meho/releases)
[![CI](https://github.com/evoila/meho/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/evoila/meho/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)
[![OSS](https://img.shields.io/badge/OSS-public%20from%20day%201-success.svg)](./CONTRIBUTING.md#public-from-day-1-deliberately)

## The problem

AI agents are getting good enough to *do* infrastructure work — roll a
credential, drain a node, restart a service — not just describe it. But
handing an agent a long-lived admin token and a shell is how you get an
un-auditable, over-privileged actor loose in production. The moment an
agent can act, you need the same controls you'd demand of any operator:
who is allowed to do what, with credentials that expire, against which
targets, with every action recorded and reviewable.

Today that control plane doesn't exist. Each team bolts ad-hoc wrappers
around an MCP server, or trusts the agent runtime to behave. MEHO is the
missing layer.

## What MEHO is

MEHO is the layer that turns **"any MCP client"** into **"any MCP client
*operating against real infrastructure under audit*."**

It sits between AI agents (Claude Code, Cursor, Cline, Continue, custom
MCP clients) and the infrastructure they operate against (Kubernetes,
vCenter / VCF, NSX, public cloud, network appliances, secrets stores).
Every operation passes through a single governed seam before it touches
anything real.

The agent runtime is *not* part of MEHO. **Bring your own agent** — MEHO
governs what it's allowed to do.

### What it guarantees

Every interaction through MEHO is:

- **Policy-gated** — operations are authorised against the caller's role
  and per-target grants before they execute.
- **Credential-federated** — the agent never holds a backend credential;
  a short-lived Keycloak OIDC token is exchanged with Vault for a
  just-in-time backend credential per operation.
- **Server-reduced** — results are reduced server-side, so the agent
  sees a compact, relevant view instead of raw firehose output.
- **Broadcast** — every action is published to a real-time activity feed
  other agents and humans can watch.
- **Audited** — every interaction lands as an immutable audit row in
  PostgreSQL, attributed to the calling principal.
- **Tenant-scoped & version-aware** — every context lookup is scoped to
  the caller's tenant and aware of resource versions.

## See it work

The [`examples/r4-local-claude/`](./examples/r4-local-claude/) reference
pattern wires your local Claude Code to a MEHO backplane under your own
Keycloak identity. The full walkthrough is in
[`GUIDE.md`](./examples/r4-local-claude/GUIDE.md); the shape of one
audited round-trip is below.

**1. Point your MCP client at MEHO.** Drop this into your repo as
`.mcp.json` (full example, including the `mcp-remote` stdio variant, in
[`mcp.json.example`](./examples/r4-local-claude/mcp.json.example)):

```json
{
  "mcpServers": {
    "meho": {
      "type": "http",
      "url": "https://meho.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${MEHO_MCP_TOKEN}"
      }
    }
  }
}
```

The token comes from `meho login https://meho.example.com` (Keycloak
device-code flow) — the agent never sees a backend credential.

**2. Ask the agent to do something.** In your Claude Code session:

> what's interesting on the MEHO backplane right now?

The session calls the `search_memory` tool over MCP. MEHO authorises the
call against your role, executes it under a just-in-time credential, and
reduces the result before the agent ever sees it.

**3. The operation leaves an audit trail.** Every `tools/call` lands one
immutable audit row, attributed to *your* Keycloak `sub` — and emits a
broadcast event other watchers can see:

```bash
meho audit query --op-id search_memory --limit 1 --json \
  | jq '.rows[0] | {principal_sub, op_id, occurred_at}'
# -> {"principal_sub": "<your-keycloak-sub>", "op_id": "search_memory", ...}
```

The same identity model, RBAC, and audit trail apply whether the caller
is your local Claude session or a 24/7 hosted triage agent — there are
no parallel auth boundaries to learn.

## Architecture

```mermaid
flowchart LR
    A["MCP clients<br/>(Claude Code · Cursor · Cline · custom)"]
    subgraph MEHO["MEHO backplane"]
        direction TB
        P["policy gate"]
        T["token exchange<br/>(Keycloak → Vault)"]
        R["server-side reduce"]
        AU["audit (Postgres)"]
        B["broadcast (Valkey)"]
    end
    I["Infrastructure<br/>(Kubernetes · vCenter/VCF · NSX · cloud · appliances · secrets)"]

    A -->|"MCP over OIDC"| MEHO
    P --> T --> R
    R --> AU
    R --> B
    MEHO -->|"governed operation"| I
```

MEHO ships as three operator-facing artefacts:

- **Backplane** — Python / FastAPI service that brokers MCP operations
  against infrastructure. Receives short-lived Keycloak-issued OIDC
  tokens, exchanges them with Vault for backend credentials, executes
  policy-gated operations, writes audit rows to PostgreSQL, and
  broadcasts activity to Valkey. Container image at
  `ghcr.io/evoila/meho`. Codebase walkthrough:
  [`docs/codebase/backend.md`](./docs/codebase/backend.md).
- **Helm chart** — `meho-chart` published as an OCI artefact at
  `ghcr.io/evoila/meho-chart`. Renders the Deployment, Service, Ingress,
  NetworkPolicy, pre-install migration Job, the bundled Valkey broadcast
  subchart, and the typed `values.schema.json` contract that rejects
  misconfigured installs at `helm install` / `helm upgrade` /
  `helm template`. Chart walkthrough:
  [`docs/codebase/devops.md`](./docs/codebase/devops.md).
- **Operator CLI** — `meho` Go binary (cobra) with ~40 command groups
  spanning auth (`login`, `status`, `version`), the operation surface
  (`operation`, `connector`, `targets`, `audit`, `broadcast`,
  `retrieval`, `kb`, `runbook`, `memory`), agents + scheduling (`agent`,
  `agent-principal`, `approvals`, `scheduler`), per-vendor connector
  aliases (`vmware`, `nsx`, `k8s`, `vault`, `harbor`, `keycloak`,
  `argocd`, `gcloud`, `bind9`, `pfsense`, and more), and admin/migration
  tooling (`admin`, `migrate`). Released as multi-platform tarballs
  (`linux/macOS × amd64/arm64`) on every `v*` tag, each individually
  cosign-signed. Full command reference:
  [`docs/codebase/cli.md`](./docs/codebase/cli.md).

The agent runtime (Claude Code, Cursor, …) lives outside the deploy
contract — operators bring their own MCP client.

## Who it's for / not for

**MEHO is for you if:**

- You run AI agents (hosted or local MCP clients) that need to *act* on
  real infrastructure, not just read about it.
- You need every agent action policy-gated, credential-federated, and
  audited — for compliance, blast-radius control, or plain peace of mind.
- You operate VMware/VCF, Kubernetes, network appliances, or cloud and
  want one governed seam in front of all of them.
- You want to bring your own agent runtime and keep your audit trail in
  systems you control (Postgres, your own Keycloak + Vault).

**MEHO is probably not for you if:**

- You want an agent *runtime* — MEHO governs agents, it doesn't run the
  model. Bring your own MCP client.
- You only need read-only chat over infrastructure with no write path,
  no per-action authorisation, and no audit requirement.
- You're looking for a hosted SaaS — MEHO is self-hosted (you run the
  backplane, Postgres, Keycloak, and Vault).

## Deploy

### Local (kind, ~5 min)

A fully local dev loop. Useful for iterating on chart plumbing, the
backplane's startup contract, and the CLI without touching real Vault /
Keycloak / Postgres.

```bash
# 1. Spin up a single-node kind cluster.
kind create cluster --name meho-dev

# 2. Apply the prerequisites documented at the top of values-kind.yaml.
#    Only Postgres ships a real mock manifest (Namespace + Secret +
#    Deployment + Service for `postgres:16-alpine` — copy-paste it). Vault
#    and Keycloak are *placeholder URIs* in the overlay so the chart's
#    URI-validated fields resolve at install time; no in-cluster Vault or
#    Keycloak is deployed and no real auth flow runs. Federation probes
#    register but `meho login` will not work end-to-end.
#    For real federation (working Vault token-exchange + Keycloak OIDC
#    flow), use the existing-k8s path below instead — you provision Vault
#    and Keycloak yourself.
#    See deploy/values-examples/values-kind.yaml for the Postgres manifest.

# 3. Install the chart from its OCI artefact.
#    Pin to an immutable image tag — sha-<git-sha> from a green CI run
#    on main, or a v<x.y.z> release tag. Deploy discipline rejects
#    :latest entirely and treats :main as a dev-only moving alias
#    (not a deploy target).
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
out of the box), points Postgres at the in-cluster mock provisioned in
step 2, and supplies *placeholder* Vault + Keycloak URIs (no in-cluster
mock for those — the chart's URI-validated fields just need to resolve
at install time). Operator identity is **faked**; the federation probes
register but `meho login` will not complete. For real federation, use
the existing-k8s path below.

### Existing k8s (~5 min, requires Vault + Keycloak + Postgres)

The supported deploy shape: a Kubernetes cluster running an
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

Every operator-facing artefact is cosign keyless-signed under the
workflow that produced it. There is no public key to distribute —
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

## Container image

The backplane is published to GitHub Container Registry as a multi-arch
manifest (`linux/amd64` + `linux/arm64`):

```bash
# Pin to an immutable commit-sha tag (recommended for deploys):
docker pull ghcr.io/evoila/meho:sha-<40-char-git-sha>

# Latest tip of main (moving target — use for development only):
docker pull ghcr.io/evoila/meho:main

# Tagged release:
docker pull ghcr.io/evoila/meho:v0.9.0
```

**No `:latest` tag is ever published.** `:main` is a moving alias for
the latest main-branch build and is intended for **dev only** (the
`docker pull` recipe above). Operators deploying MEHO must pin to an
immutable `:sha-<...>` or `:v<x.y.z>` reference — `:main` is not a
deploy target. Every image is cosign-signed using the same keyless flow
described above.

## Verifying CLI release artefacts

Every CLI tarball published at <https://github.com/evoila/meho/releases>
is signed via cosign keyless. The signature, the Fulcio-issued
certificate, and the Rekor transparency-log inclusion proof are bundled
into a single `.cosign.bundle` JSON file attached to the release
alongside each `.tar.gz` and the combined `SHA256SUMS` file.

```bash
TAG=<version>   # e.g. v0.9.0 — the release tag you are verifying
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
`image.yml` (container image signing). A maliciously re-tagged workflow
on a fork cannot produce a bundle that satisfies it.

`SHA256SUMS` is signed the same way; verify it once, then
`sha256sum -c SHA256SUMS` against any subset of tarballs the
operator actually downloaded (two-step trust chain). The full
recipe and signing rationale live in
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
| `replicaCount` | `1` | Single-replica baseline. |
| `image.repository` | `ghcr.io/evoila/meho` | OCI repo from the image pipeline. |
| `image.pullPolicy` | `IfNotPresent` | `Always` \| `IfNotPresent` \| `Never`. |
| `service.type` / `service.port` | `ClusterIP` / `8000` | Service shape. |
| `ingress.className` | `""` | Cluster default IngressClass when empty. |
| `probes.liveness.*` / `probes.readiness.*` | `/healthz` / `/ready` httpGet + tuned timings | Operator-tunable; never disabled. |
| `resources.requests` / `resources.limits` | `100m`/`256Mi` / `1000m`/`1Gi` | Conservative chassis baselines. |
| `networkPolicy.ingressControllerNamespace` | `ingress-nginx` | RKE2 default; override per cluster. |
| `audit.postgresOnly` | `true` | Postgres-only audit sink baseline. |
| `broadcast.enabled` | `true` | Deploys the bundled Valkey broadcast subchart. |
| `connectors.enabled` | `[]` | Opt-in list; pick from the shipped connector catalog (see [`docs/architecture/connectors.md`](./docs/architecture/connectors.md) — VMware/VCF, NSX, Kubernetes, Vault, Harbor, Keycloak, ArgoCD, GCloud, BIND9, pfSense, and more). |

See [`docs/codebase/devops.md`](./docs/codebase/devops.md) for the full
chart contract, probe semantics, NetworkPolicy posture, install/upgrade
flow, and verification commands.

## v0.2 upgrade prerequisites

The v0.2 backplane reads two new claims (`tenant_id` + `tenant_role`)
from every authenticated access token. v0.1 chassis-era tokens do not
carry those claims; operators upgrading from v0.1 to v0.2 must apply the
realm-side configuration that mints them, or every authenticated request
returns `401 missing_tenant_claim`.

The realm-side recipe (group attribute + realm role protocol mappers,
verification + troubleshooting) lives at
[`docs/cross-repo/keycloak-tenant-claims.md`](./docs/cross-repo/keycloak-tenant-claims.md).
Apply it against the realm whose issuer is configured as
`config.keycloakIssuerUrl` in the chart values (rendered into the
`KEYCLOAK_ISSUER_URL` env var on the backplane Deployment by
[`templates/configmap.yaml`](./deploy/charts/meho/templates/configmap.yaml))
**before** rolling the backplane image to a v0.2 tag.

## Documentation

Codebase walkthroughs:

- **Backend** — [`docs/codebase/backend.md`](./docs/codebase/backend.md)
- **CLI** — [`docs/codebase/cli.md`](./docs/codebase/cli.md)
- **Chart + deploy** — [`docs/codebase/devops.md`](./docs/codebase/devops.md)

Architecture references live under
[`docs/architecture/`](./docs/architecture/) — topology, the operations
substrate, audit, MCP surface, and the connector catalog.

## Contributing

See [`CONTRIBUTING.md`](./CONTRIBUTING.md). Contributions require a
Developer Certificate of Origin sign-off (`git commit -s`) — there is
no CLA. Public-from-day-1: every line of MEHO ships on `evoila/meho`
from commit #1; operator-sensitive coordination lives in
`evoila-bosnia/meho-internal` (issues + ADRs only, no code).

## Security

Vulnerability reports: see [`SECURITY.md`](./SECURITY.md).

## Releasing

Maintainers: the release runbook (including one-time GHCR package
visibility setup) lives in [`docs/RELEASING.md`](./docs/RELEASING.md).

## Changelog

See [`CHANGELOG.md`](./CHANGELOG.md). Project-wide (image + chart + CLI
under one document) in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format. The top-level CHANGELOG is the authoritative source for the
narrative attached to every GitHub Release — the CLI release pipeline
extracts the matching section via `--release-notes` rather than
auto-generating from git log.

## License

[Apache License 2.0](./LICENSE). Inbound = outbound: every contribution
flows in under the same Apache 2.0 terms the project ships under, via
the Developer Certificate of Origin (DCO) sign-off on every commit
(`git commit -s`) — there is no separate CLA. See
[`CONTRIBUTING.md`](./CONTRIBUTING.md#developer-certificate-of-origin)
for the sign-off discipline.

## History

This repository was bootstrapped on 2026-05-09 as a strategic reset.
The prior MEHO codebase lives at `evoila-bosnia/MEHO.X`, deprecated
and retained for reference.
</content>
</invoke>
