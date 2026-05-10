# `deploy/` — chart, manifests, and deployment glue

> Durable map of the deployment surface. Update in lock-step with chart
> changes; stale entries are bugs.

## Overview

Everything a consumer needs to install MEHO onto a Kubernetes cluster lives
under `deploy/`. The Helm chart at `deploy/charts/meho/` is the **single
contract** between MEHO and the deployment environment — `helm install` /
`helm upgrade --install` consumes it and produces all six core Kubernetes
resources that make up a running backplane:

- Deployment — the backplane Pod (FastAPI app, `uvicorn` on port 8000).
- Service — ClusterIP front-door for the Deployment, target port `http`.
- Ingress — TLS-enabled external entry with cert-manager annotations.
- ConfigMap — non-secret env (Keycloak URLs, Vault address, pool sizes).
- ServiceAccount — Pod identity, `automountServiceAccountToken: false`.
- NetworkPolicy — default-deny ingress + explicit egress allow-list to
  Postgres, Vault, Keycloak, in-cluster Redis, and CoreDNS only.

## Chart layout

```
deploy/charts/meho/
├── Chart.yaml              # apiVersion v2, kubeVersion >=1.28
├── .helmignore             # standard exclusions
├── values.yaml             # scaffold defaults; G2.5-T2 hardens them
├── values.schema.json      # placeholder; G2.5-T2 fills the typed contract
└── templates/
    ├── _helpers.tpl        # name / fullname / labels / SA helpers
    ├── deployment.yaml     # backplane Pod
    ├── service.yaml        # ClusterIP :8000
    ├── ingress.yaml        # TLS + cert-manager
    ├── configmap.yaml      # non-secret env
    ├── serviceaccount.yaml # Pod identity
    ├── networkpolicy.yaml  # default-deny + explicit egress
    └── NOTES.txt           # post-install hints
```

## Chart contract

### `Chart.yaml`

- `apiVersion: v2` — required for Helm 3 / 4.
- `version` is the **chart** version (calver-bumped by the publish workflow
  in G2.5-T5 to `0.1.YYYYMMDD-<sha>`); `appVersion` is the **application**
  version, overridden by the same workflow to the git sha being deployed.
  The values shipped in `Chart.yaml` are placeholders — they exist only so
  `helm lint` / `helm template` succeed on a fresh checkout.
- `kubeVersion: ">=1.28.0-0"` — matches Goal #11's RKE2 target. The
  manifests use only API versions that have been stable since 1.19; the
  floor is set higher than strictly required to align with the test bed.
- `sources` + `maintainers` + `keywords` follow Artifact Hub norms for
  discoverability after the OCI publish workflow lands.

### Image reference

The Deployment renders `{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}`.
That gives consumers three idiomatic ways to pin the image:

1. **Pin via `--set image.tag=<sha>`** — typical CI flow; `image.tag` wins.
2. **Pin via `appVersion` rewrite** — the publish workflow rewrites
   `Chart.yaml.appVersion` to the git sha at OCI push time; consumers
   who omit `image.tag` get that pin automatically.
3. **Override `image.repository`** — consumers mirroring through a private
   registry point the chart at their mirror.

`imagePullSecrets` is a values-configurable list, empty by default. The
backplane image is pushed to **public GHCR** (Goal #11's locked artefact-
distribution principle), so no pull secret is required in the default
deployment path.

### Probes (scaffolded, off by default in T1)

The Deployment template emits `livenessProbe` / `readinessProbe` /
`startupProbe` blocks gated on `probes.<kind>.enabled`. All three default
to `enabled: false` in this T1 scaffold; G2.5-T2 (issue #38) wires the
real probe targets (`/healthz` / `/ready` / `/version`) against the
chassis from G2.1. Keeping probes off until T2 lands avoids a half-
configured probe marking the Pod NotReady before its health endpoints
are reachable.

### Security context

The Pod runs with `runAsNonRoot: true`, `runAsUser: 1001`,
`seccompProfile.type: RuntimeDefault`; the container drops every
capability, disallows privilege escalation, and mounts the root filesystem
read-only. `/tmp` is mounted as an `emptyDir` for libraries that insist on
a writable tempdir. These defaults match cert-manager, ArgoCD, and Flux,
and they're the minimum surface that admits a Pod under the cluster's
**restricted** PodSecurity profile.

### NetworkPolicy

`networkPolicy.enabled: true` ships a default-deny policy with explicit
egress rules to:

- Postgres — `tcp/5432` to `networkPolicy.postgresCIDR`
- Vault — `tcp/8200` to `networkPolicy.vaultCIDR`
- Keycloak — `tcp/443` to `networkPolicy.keycloakCIDR`
- Redis — `tcp/6379` to a `podSelector` matching the in-cluster Redis
  subchart (G2.5-T3)
- DNS — `udp/53` to the `k8s-app: kube-dns` selector (matches CoreDNS)

Ingress is permitted only from the namespace whose
`kubernetes.io/metadata.name` label matches
`networkPolicy.ingressControllerNamespace` (default `ingress-nginx`,
RKE2's bundled controller).

The CIDR defaults are intentional **/32 placeholders** — they make the
chart fail-closed if a consumer forgets to override them rather than
silently allow a /8 subnet.

## Install / upgrade

```bash
helm install meho ./deploy/charts/meho/ \
  --namespace meho \
  --create-namespace \
  --set image.tag=<sha> \
  --set ingress.host=meho.example.org \
  --set config.keycloakIssuerUrl=https://keycloak.example.org/realms/meho \
  --set config.keycloakAudience=meho-backplane \
  --set config.vaultAddr=https://vault.example.org \
  --set networkPolicy.postgresCIDR=10.0.1.0/24 \
  --set networkPolicy.vaultCIDR=10.0.2.0/24 \
  --set networkPolicy.keycloakCIDR=10.0.3.0/24

helm upgrade --install meho ./deploy/charts/meho/ -f values-rdc.yaml
```

Until G2.5-T2 lands the typed `values.schema.json`, the chart accepts any
shape — the safety net comes from the backplane itself, which fails-closed
at startup on missing `KEYCLOAK_*` / `VAULT_*` / `DATABASE_URL` env vars.

## Verification

```bash
helm lint deploy/charts/meho/

helm template test-release deploy/charts/meho/ \
  --set image.tag=test \
  --set ingress.host=meho.test \
  --set postgres.credentialsSecret=test \
  --set networkPolicy.postgresCIDR=10.0.0.0/24 \
  --set networkPolicy.vaultCIDR=10.0.0.0/24 \
  --set networkPolicy.keycloakCIDR=10.0.0.0/24 \
  --set networkPolicy.ingressControllerNamespace=ingress-nginx \
  > /tmp/rendered.yaml

grep -c '^kind:' /tmp/rendered.yaml  # expect >= 6
```

## Dependencies

- Image — `ghcr.io/evoila/meho:<tag>` from G2.4 (#31). Multi-arch
  (amd64 + arm64), cosign-signed, SBOM-attested.
- Migration runner — invoked by a pre-install/pre-upgrade Job hook landed
  in G2.5-T3 (#39); shells out to the entrypoint added in G2.3-T3 (#29).
- Redis subchart — added by G2.5-T3 (#39) per ADR 0005.
- External Secrets Operator (ESO) — owns the Kubernetes Secrets the chart
  references (`postgres.credentialsSecret`, future Keycloak client secret,
  future Vault role bindings). ESO is consumer-owned; the chart references
  the synced Secrets by name only.

## Known gaps (filled by sibling tasks)

- `values.schema.json` is a placeholder — G2.5-T2 (#38) fills it.
- Probes are scaffolded but disabled — G2.5-T2 (#38) wires real targets.
- Pre-install migration Job + Redis subchart — G2.5-T3 (#39).
- `deploy/values-examples/values-rdc-example.yaml` — G2.5-T4 (#40).
- OCI publish to `ghcr.io/evoila/meho-chart` + cosign signing — G2.5-T5
  (#41).
- HPA / PDB / ServiceMonitor / PrometheusRule — deferred to v0.2.

## References

- Parent Goal: #11 — Deployable v0.1
- Parent Initiative: #36 — G2.5 Helm chart
- Helm chart structure: https://helm.sh/docs/topics/charts/
- Kubernetes NetworkPolicy: https://kubernetes.io/docs/concepts/services-networking/network-policies/
- cert-manager Ingress annotations: https://cert-manager.io/docs/usage/ingress/
