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
├── values.yaml             # safe-by-default; required fields are blank
├── values.schema.json      # draft-07 typed contract; rejects typos and empty required fields
└── templates/
    ├── _helpers.tpl        # name / fullname / labels / SA helpers
    ├── deployment.yaml     # backplane Pod + probes (/healthz, /ready)
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

### Probes

The Deployment always renders `livenessProbe` and `readinessProbe` against
the backplane chassis endpoints from G2.1-T2
(`backend/src/meho_backplane/health.py`):

| Probe | Endpoint | Failure semantics | Default timings (operator-tunable) |
| --- | --- | --- | --- |
| `livenessProbe` | `/healthz` (always 200 if the process is up) | Pod **restarts** on failure | `initialDelaySeconds: 30`, `periodSeconds: 10`, `timeoutSeconds: 1`, `failureThreshold: 3` |
| `readinessProbe` | `/ready` (200 only when every registered probe in the readiness registry passes; 503 with an empty registry at the chassis stage) | Pod **removed from Service endpoints**, no restart | `initialDelaySeconds: 5`, `periodSeconds: 5`, `timeoutSeconds: 2`, `failureThreshold: 3` |

The 30-second liveness `initialDelaySeconds` gives the FastAPI app time to
import, build the JWKS cache, and bind structlog context before the first
check — under-provisioning it would restart-loop the Pod during slow image
pulls or cold-start library imports. The shorter readiness window (15s
total detection) makes the Pod fall out of rotation promptly when a
downstream dependency goes flaky, without triggering an unnecessary
restart of the backplane process itself.

Probes are **always on** — there is no `enabled: false` escape valve.
Disabling probes would mask startup deadlocks and let an unready Pod
accept traffic; that tradeoff is never the right call for a governance
backplane. Every field under `probes.liveness.*` and `probes.readiness.*`
in `values.yaml` is operator-tunable for environments that need different
timings.

The `/ready` endpoint **returns 503 by design** until G2.2 (Vault /
Keycloak probes) and G2.3 (Alembic migration probe) register concrete
probes — that's the fail-closed chassis state, not a bug. During chassis-
stage dev installs the readinessProbe will hold the Pod out of rotation
until those probes land; that's the intended signal.

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

The three egress CIDR fields ship **empty** in `values.yaml` and are
required-with-shape-validation in the schema. The chart will not render
without explicit per-environment CIDR overrides — defense-in-depth against
accidentally allowing a wide subnet because a typo silently fell through.

### Safe-by-default values

`values.yaml` deliberately ships **blank** for every field the backplane
cannot start without. Operators MUST override these via `--set` or a
values overlay:

| Field | Why blank |
| --- | --- |
| `image.tag` | Goal #11 deploy discipline: every install pins an immutable tag, never a moving reference |
| `ingress.host` | Per-environment; no generic placeholder is correct |
| `ingress.tls.secretName` | Per-environment Secret name (cert-manager-managed or pre-provisioned) |
| `postgres.credentialsSecret` | Per-environment Secret holding `DATABASE_URL` (ESO-synced from Vault in production) |
| `vault.address` | Per-environment Vault endpoint |
| `keycloak.issuer` | Per-environment Keycloak issuer URL |
| `config.keycloakIssuerUrl` / `config.keycloakAudience` / `config.vaultAddr` | ConfigMap env-var mirrors of the above (`backend/src/meho_backplane/settings.py` contract) |
| `networkPolicy.{postgres,vault,keycloak}CIDR` | Per-environment subnet for each upstream |

A blank field falls into the typed-schema contract immediately — `helm
install` fails before a single Kubernetes resource is created. The
operator sees the exact missing path (e.g. `at '/vault/address':
minLength: got 0, want 1`) and a single targeted override fixes it.

Conservative resource defaults (`requests: {cpu: 100m, memory: 256Mi}`,
`limits: {cpu: 1000m, memory: 1Gi}`) reflect observed steady-state usage
of the v0.1 chassis (authn/authz traffic + synchronous audit-write fanout);
tune limits up for higher-throughput deployments.

### `values.schema.json` typed contract

The chart ships a **JSON Schema draft-07** contract for `values.yaml`
(Helm's supported dialect). Helm validates the merged `.Values` object
against this schema on:

- `helm lint`
- `helm template`
- `helm install` / `helm install --dry-run`
- `helm upgrade`

Three properties make this the right contract:

1. **`additionalProperties: false` at every object level.** A typo
   (`postgress` for `postgres`) fails at `helm install` time with the
   exact path, not silently at first request when the backplane fails to
   resolve a Vault secret. Helm reports e.g. `at '': additional properties
   'postgress' not allowed`.
2. **`minLength: 1` on every required-but-blank field plus
   `format: uri` / `format: hostname` / `pattern: …` shape validation
   on URLs / hostnames / CIDRs.** The safe-by-default empty placeholders
   in `values.yaml` are intentionally rejected, surfacing the exact field
   the operator must override.
3. **Subchart compatibility.** When G2.5-T3 (#39) adds the Redis subchart
   dependency, the top-level `properties` map gains a `redis` key and the
   subchart's own `values.schema.json` (if present) is also enforced —
   the parent chart cannot circumvent subchart restrictions.

`helm lint` against the unmodified `values.yaml` **deliberately fails**
with the safe-by-default empty fields. The chart's CI / lint workflow
(G2.5-T5) and `deploy/values-examples/values-rdc-example.yaml` (G2.5-T4)
both supply the required overrides; ad-hoc lint invocations pass them via
`--set` or `-f`.

## Install / upgrade

```bash
helm install meho ./deploy/charts/meho/ \
  --namespace meho \
  --create-namespace \
  --set image.tag=sha-<git-sha> \
  --set ingress.host=meho.example.org \
  --set ingress.tls.secretName=meho-tls \
  --set postgres.credentialsSecret=meho-postgres \
  --set vault.address=https://vault.example.org \
  --set keycloak.issuer=https://keycloak.example.org/realms/meho \
  --set config.keycloakIssuerUrl=https://keycloak.example.org/realms/meho \
  --set config.keycloakAudience=meho-backplane \
  --set config.vaultAddr=https://vault.example.org \
  --set networkPolicy.postgresCIDR=10.0.1.0/24 \
  --set networkPolicy.vaultCIDR=10.0.2.0/24 \
  --set networkPolicy.keycloakCIDR=10.0.3.0/24

helm upgrade --install meho ./deploy/charts/meho/ -f values-rdc.yaml
```

Missing any required override fails the schema validation at install
time with the exact field path — e.g. omitting `--set vault.address=...`
produces `at '/vault/address': '' is not valid uri: relative url`. The
backplane never starts against a misconfigured set of values.

## Verification

```bash
# helm lint passes only with a values overlay or `--set` overrides for every
# required-but-blank field; the bare chart deliberately fails-loud:
helm lint deploy/charts/meho/ \
  --set image.tag=test \
  --set ingress.host=meho.test \
  --set ingress.tls.secretName=meho-tls \
  --set postgres.credentialsSecret=meho-postgres \
  --set vault.address=https://vault.test \
  --set keycloak.issuer=https://keycloak.test/realms/meho \
  --set config.keycloakIssuerUrl=https://keycloak.test/realms/meho \
  --set config.keycloakAudience=meho-backplane \
  --set config.vaultAddr=https://vault.test \
  --set networkPolicy.postgresCIDR=10.0.0.0/24 \
  --set networkPolicy.vaultCIDR=10.0.0.0/24 \
  --set networkPolicy.keycloakCIDR=10.0.0.0/24

# Same flags reproduce the render:
helm template test-release deploy/charts/meho/ \
  --set image.tag=test \
  --set ingress.host=meho.test \
  --set ingress.tls.secretName=meho-tls \
  --set postgres.credentialsSecret=meho-postgres \
  --set vault.address=https://vault.test \
  --set keycloak.issuer=https://keycloak.test/realms/meho \
  --set config.keycloakIssuerUrl=https://keycloak.test/realms/meho \
  --set config.keycloakAudience=meho-backplane \
  --set config.vaultAddr=https://vault.test \
  --set networkPolicy.postgresCIDR=10.0.1.0/24 \
  --set networkPolicy.vaultCIDR=10.0.2.0/24 \
  --set networkPolicy.keycloakCIDR=10.0.3.0/24 \
  > /tmp/rendered.yaml

grep -c '^kind:' /tmp/rendered.yaml  # expect >= 6

# Negative tests — the chart fails-loud on the misuse cases the schema covers:
helm template test deploy/charts/meho/ 2>&1 | grep -E "minLength|valid"
helm template test deploy/charts/meho/ --set bogus.field=x 2>&1 | grep "additional properties"
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

- Pre-install migration Job + Redis subchart — G2.5-T3 (#39). The subchart
  will extend `values.yaml` with a top-level `redis:` key and the matching
  schema branch.
- `deploy/values-examples/values-rdc-example.yaml` — G2.5-T4 (#40).
- OCI publish to `ghcr.io/evoila/meho-chart` + cosign signing — G2.5-T5
  (#41).
- HPA / PDB / topologySpreadConstraints / ServiceMonitor / PrometheusRule
  — deferred to v0.2. v0.1 is single-replica per Goal #11 scope.

## References

- Parent Goal: #11 — Deployable v0.1
- Parent Initiative: #36 — G2.5 Helm chart
- Helm chart structure: https://helm.sh/docs/topics/charts/
- Kubernetes NetworkPolicy: https://kubernetes.io/docs/concepts/services-networking/network-policies/
- cert-manager Ingress annotations: https://cert-manager.io/docs/usage/ingress/
