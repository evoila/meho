# `deploy/` — chart, manifests, and deployment glue

> Durable map of the deployment surface. Update in lock-step with chart
> changes; stale entries are bugs.

## Overview

Everything a consumer needs to install MEHO onto a Kubernetes cluster lives
under `deploy/`. The Helm chart at `deploy/charts/meho/` is the **single
contract** between MEHO and the deployment environment — `helm install` /
`helm upgrade --install` consumes it and produces the core Kubernetes
resources that make up a running backplane:

- Deployment — the backplane Pod (FastAPI app, `uvicorn` on port 8000).
- Service — ClusterIP front-door for the Deployment, target port `http`.
- Ingress — TLS-enabled external entry with cert-manager annotations.
- ConfigMap — non-secret env (Keycloak URLs, Vault address, pool sizes).
- ServiceAccount — Pod identity, `automountServiceAccountToken: false`.
- NetworkPolicy — default-deny ingress + explicit egress allow-list to
  Postgres, Vault, Keycloak, the broadcast subchart, and CoreDNS only.
- Migration Job — `pre-install,pre-upgrade` Helm hook running
  `python -m meho_backplane.db.migrate` before the Deployment rolls forward.
- Broadcast subchart — in-tree Valkey 9.x Deployment + Service + ConfigMap
  per ADR 0005.

## Chart layout

```
deploy/charts/meho/
├── Chart.yaml              # apiVersion v2, kubeVersion >=1.28, dependencies: broadcast
├── .helmignore             # standard exclusions
├── values.yaml             # safe-by-default; required fields are blank
├── values.schema.json      # draft-07 typed contract; rejects typos and empty required fields
├── templates/
│   ├── _helpers.tpl        # name / fullname / labels / SA helpers
│   ├── deployment.yaml     # backplane Pod + probes (/healthz, /ready)
│   ├── service.yaml        # ClusterIP :8000
│   ├── ingress.yaml        # TLS + cert-manager
│   ├── configmap.yaml      # non-secret env
│   ├── serviceaccount.yaml # Pod identity
│   ├── networkpolicy.yaml  # default-deny + explicit egress (broadcast egress conditional)
│   ├── migration-job.yaml  # pre-install/pre-upgrade Helm hook (alembic upgrade head)
│   └── NOTES.txt           # post-install hints
└── charts/
    └── broadcast/          # in-tree Valkey 9.x subchart (ADR 0005)
        ├── Chart.yaml
        ├── values.yaml
        └── templates/
            ├── _helpers.tpl
            ├── deployment.yaml   # single-replica Recreate; readonly rootfs + emptyDir /data
            ├── service.yaml      # ClusterIP :6379 (port name "redis")
            └── configmap.yaml    # minimal valkey.conf (no auth, no persistence)
```

## Chart contract

### `Chart.yaml`

- `apiVersion: v2` — required for Helm 3 / 4.
- `name: meho-chart` is the **OCI artefact basename**. `helm push` derives
  the published package path (`ghcr.io/evoila/meho-chart`) from this field.
  The chart is named `meho-chart` rather than `meho` so the GHCR package
  stays distinct from the backplane image package at `ghcr.io/evoila/meho`
  — visibility, retention, and signing identities are managed
  independently on each package. To preserve the existing resource-label
  invariant (`app.kubernetes.io/name: meho`) the chart sets
  `nameOverride: meho` in `values.yaml`; the rename is purely a publish-
  coordinate concern.
- `version` is the **chart** version (calver-bumped by
  `.github/workflows/chart.yml` to `0.1.YYYYMMDD-<short-sha>` on main
  pushes, plain semver on `v*` tag pushes); `appVersion` is the
  **application** version, overridden by the same workflow to the git sha
  being deployed. The values shipped in `Chart.yaml` are placeholders —
  they exist only so `helm lint` / `helm template` succeed on a fresh
  checkout.
- `kubeVersion: ">=1.28.0-0"` — matches Goal #11's RKE2 target. The
  manifests use only API versions that have been stable since 1.19; the
  floor is set higher than strictly required to align with the test bed.
- `sources` + `maintainers` + `keywords` follow Artifact Hub norms for
  discoverability now that the OCI publish workflow has landed.

### Image reference

The Deployment renders `{{ .Values.image.repository }}:{{ .Values.image.tag }}`
with **no `.Chart.AppVersion` fallback**. `values.schema.json` rejects an
empty `image.tag` (`minLength: 1`), so every install pins the tag
operator-supplied at `helm install` / `helm upgrade` time. Goal #11's
deploy discipline forbids moving references (including a chart-`appVersion`
shadow), and the chart enforces that contract at the schema layer rather
than relying on consumers to remember to `--set image.tag`.

`image.repository` accepts the lowercase OCI grammar including an optional
`:<port>` segment after the host, so private registries like
`registry.example.com:5000/team/meho` are valid. The pattern enforces the
shape; the operator picks the value.

`imagePullSecrets` is a values-configurable list, empty by default. The
backplane image is pushed to **public GHCR** (Goal #11's locked artefact-
distribution principle), so no pull secret is required in the default
deployment path. Consumers mirroring through a private registry override
`image.repository` to point at their mirror and (if needed) populate
`imagePullSecrets`.

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
- Broadcast subchart — `tcp/<broadcast.service.port>` (default 6379) to a
  `podSelector` matching the in-cluster broadcast subchart's selector
  labels (`app.kubernetes.io/name: broadcast`); the rule is conditional
  on `broadcast.enabled: true` and is omitted when the broadcast subchart
  is disabled
- DNS — `udp/53` to the `k8s-app: kube-dns` selector (matches CoreDNS)

Ingress is permitted only from the namespace whose
`kubernetes.io/metadata.name` label matches
`networkPolicy.ingressControllerNamespace` (default `ingress-nginx`,
RKE2's bundled controller).

The three egress CIDR fields ship **empty** in `values.yaml` and are
required-with-shape-validation in the schema **when
`networkPolicy.enabled: true`**. The chart will not render with the
default `enabled: true` without explicit per-environment CIDR overrides
— defense-in-depth against accidentally allowing a wide subnet because
a typo silently fell through.

Operators on clusters running an equivalent mesh-level policy (Istio
`AuthorizationPolicy`, Cilium `CiliumNetworkPolicy`, etc.) can set
`networkPolicy.enabled: false` to skip the chart's NetworkPolicy
entirely; the schema's conditional `if/then` relaxes the CIDR
requirements in that mode so the values overlay does not need to
populate them. Disabling without a replacement policy in place removes
the chart's least-privilege egress story — only do it when an
equivalent control is enforced upstream.

### Migration Job (`templates/migration-job.yaml`)

A Kubernetes `Job` runs as a Helm hook before the Deployment is created
(install) or rolled forward (upgrade). The container executes
`python -m meho_backplane.db.migrate` — the entrypoint shipped by Task
#29 — which invokes `alembic upgrade head` against the same
`DATABASE_URL` Secret the backplane Deployment consumes. The Job uses
the same image as the backplane (`{{ .Values.image.repository }}:{{ .Values.image.tag }}`)
so the migrations applied match exactly the revision the rolling-out
Deployment expects — a separate migration image would drift.

Hook semantics:

| Annotation | Value | Meaning |
| --- | --- | --- |
| `helm.sh/hook` | `pre-install,pre-upgrade` | Runs the Job both on a fresh `helm install` and every `helm upgrade` |
| `helm.sh/hook-weight` | `"-10"` | Runs ahead of any other hook resources (only documentary at the chassis stage — no other hooks ship yet) |
| `helm.sh/hook-delete-policy` | `before-hook-creation,hook-succeeded` | Overwrites the previous Job on retry; GCs the Job once it exits 0. `hook-failed` is **intentionally absent** — failed Jobs stay in the namespace for `kubectl logs` forensics |

Pod spec:

- `restartPolicy: OnFailure` — retry in-place on transient asyncpg
  errors without re-scheduling the whole Pod.
- `backoffLimit: 3` (operator-tunable via `.Values.migrationJob.backoffLimit`) —
  catches transient network blips between the Job pod and PostgreSQL.
  Alembic migrations are idempotent so re-running a partially-applied
  step is safe.
- `ttlSecondsAfterFinished: 600` (operator-tunable via
  `.Values.migrationJob.ttlSecondsAfterFinished`) — Kubernetes-side
  garbage-collection backstop: even if `helm uninstall` is delayed, the
  Job + Pod logs are reaped after the configured window (10 minutes by
  default).
- Same `serviceAccountName`, `imagePullSecrets`, and pod/container
  `securityContext` as the backplane Deployment (`runAsNonRoot`,
  `readOnlyRootFilesystem`, `drop: [ALL]`), with `/tmp` mounted as an
  `emptyDir` to keep the read-only root invariant.
- `envFrom` reuses the backplane's ConfigMap so any Alembic-relevant env
  vars (pool sizes, timeouts) stay in lock-step; `DATABASE_URL` is
  pulled from `Values.postgres.credentialsSecret` at the `url` key
  exactly like the Deployment does.

**Failure semantics.** When the Job exhausts `backoffLimit`, Helm fails
the release at the pre-install/pre-upgrade hook step. The Deployment is
never created against an unmigrated schema. The failed Job is left in
the namespace; `kubectl logs -n <ns> job/<release>-meho-migrate` shows
the Alembic error (rendered to stderr by the runner as
`migration_failed: <ExcClass>: <msg>`).

### Broadcast subchart (`charts/broadcast/`)

A custom in-tree Helm subchart deploys Valkey 9.x as the
Redis-protocol-compatible activity-broadcast store. ADR 0005 locks the
upstream choice and the workload shape; the subchart is the
implementation of that decision.

| Aspect | Value | Rationale |
| --- | --- | --- |
| Upstream image | `valkey/valkey:9.0-alpine` (Docker Hub) | BSD-3-Clause; Linux Foundation governance; carries Redis 7.2.4's last permissive license forward |
| Slug | `broadcast` (not `redis`) | The protocol contract matters more than the brand |
| Workload | `Deployment` (not `StatefulSet`), single replica | Streams are ephemeral in v0.1; HA via Sentinel/Cluster is v0.2+ |
| Persistence | None (no PVC, `save ""`, `appendonly no`) | Restart-loss of stream history is acceptable in v0.1 |
| Auth | None (no `requirepass`) | v0.1 single-tenant; gated at the network layer by the umbrella chart's NetworkPolicy |
| Update strategy | `Recreate` | Single-replica + port-bind constraint makes RollingUpdate worse |
| Probes | TCP `connect` on 6379 | Minimal — avoids coupling to `redis-cli` / `valkey-cli` binary naming variance |
| Service | ClusterIP `<release>-broadcast:6379` (port name `redis`) | In-cluster only; backplane consumes via the operator-facing `REDIS_URL` env |

The subchart lives unpacked at `deploy/charts/meho/charts/broadcast/`.
The parent `Chart.yaml` declares it as a dependency with
`repository: ""` (the documented Helm shape for an unpacked local
subchart — `helm dependency update` is not required and would fail
trying to fetch from a remote registry). `condition: broadcast.enabled`
lets operators flip the entire subchart off with a single boolean — for
example, on clusters where an external managed Valkey/Redis (Azure
Cache, AWS ElastiCache, GCP Memorystore) is already available. The
v0.2+ `broadcast.externalEndpoint` opt-out lands when the broadcast
feature actually carries cross-deployment streams.

**Schema interaction with the parent.** The umbrella chart's
`values.schema.json` declares a `broadcast` property with
`additionalProperties: false` plus an explicit list of permitted keys,
**plus a permissive `global` property** because Helm injects
`.Values.global` into every subchart's values namespace. Without the
`global` allowance, `helm lint` reports
`at '/broadcast': additional properties 'global' not allowed`. The
subchart's own values.yaml shape is also enforced by Helm independently —
this parent block is the surface visible to the umbrella's `--set` flags.

**Backplane wiring.** When `broadcast.enabled: true` (the default), the
backplane Deployment renders a `REDIS_URL` env var pointing at
`redis://{{ .Release.Name }}-broadcast:{{ .Values.broadcast.service.port }}/0`.
The full broadcast feature is v0.2 work; the env var is forward-prepared
in v0.1 so the chassis discovers the endpoint as soon as the broadcast
code uses it. ADR 0005 locked `redis-py` as the driver — it parses
`redis://` schemes against a Valkey endpoint unchanged (wire-protocol
compatibility carries from Redis 7.2.4).

**Operator-supplied secrets.** The Job + the backplane both consume
`DATABASE_URL` from a Kubernetes Secret named by
`postgres.credentialsSecret` at key `url`. The chart references this
Secret by name only — provisioning it is the operator's job. Production
deployments use **External Secrets Operator (ESO)** to sync the value
from HashiCorp Vault (G2.5-T4 ships the example overlay; G2.6 wires the
ESO ExternalSecret resources). Dev installs may pre-provision the Secret
manually:

```bash
kubectl create secret generic meho-postgres \
  --from-literal=url='postgresql+asyncpg://meho:<password>@<host>:5432/meho' \
  --namespace meho
```

### Safe-by-default values

`values.yaml` deliberately ships **blank** for every field the backplane
cannot start without. Operators MUST override these via `--set` or a
values overlay:

| Field | Why blank |
| --- | --- |
| `image.tag` | Goal #11 deploy discipline: every install pins an immutable tag, never a moving reference |
| `ingress.host` | Per-environment; no generic placeholder is correct. Required only when `ingress.enabled: true` (the default) — relaxed when ingress is disabled |
| `ingress.tls.secretName` | Per-environment Secret name (cert-manager-managed or pre-provisioned). Required only when both `ingress.enabled` and `ingress.tls.enabled` are true |
| `postgres.credentialsSecret` | Per-environment Secret holding `DATABASE_URL` (ESO-synced from Vault in production) |
| `vault.address` | Per-environment Vault endpoint |
| `keycloak.issuer` | Per-environment Keycloak issuer URL |
| `config.keycloakIssuerUrl` / `config.keycloakAudience` / `config.vaultAddr` | ConfigMap env-var mirrors of the above (`backend/src/meho_backplane/settings.py` contract) |
| `networkPolicy.{postgres,vault,keycloak}CIDR` | Per-environment subnet for each upstream. Required only when `networkPolicy.enabled: true` (the default) — relaxed when networkPolicy is disabled |

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
3. **Subchart compatibility.** The umbrella's `properties` map declares
   a `broadcast` key for the in-tree subchart at `charts/broadcast/`, and
   the subchart's own `values.schema.json` (if shipped) is also enforced
   by Helm independently — the parent chart cannot circumvent subchart
   restrictions. A permissive `broadcast.global` allowance is required
   because Helm injects `.Values.global` into every subchart's
   values namespace; omitting it causes
   `at '/broadcast': additional properties 'global' not allowed`.

`helm lint` against the unmodified `values.yaml` **deliberately fails**
with the safe-by-default empty fields. The chart's `validate` job in
[`.github/workflows/chart.yml`](../../.github/workflows/chart.yml) and
[`deploy/values-examples/values-rdc-example.yaml`](../../deploy/values-examples/values-rdc-example.yaml)
both supply the required overrides; ad-hoc lint invocations pass them via
`--set` or `-f`.

### Example values + ESO patterns

A sanitized example values file lives at
[`deploy/values-examples/values-rdc-example.yaml`](../../deploy/values-examples/values-rdc-example.yaml).
It targets the RDC Hetzner dogfooding lab shape (cluster-internal
Postgres + Vault + Keycloak on `*.evba.lab`, rke2-infra ingress-nginx) and
is structured so that other Vault-+-Keycloak-+-Postgres-shaped labs can
copy it, substitute the `<REPLACE: ...>` placeholders, and apply it.

The placeholders are deliberate: every site-specific field
(`image.tag`, the Keycloak realm in `config.keycloakIssuerUrl` /
`keycloak.issuer`, the three NetworkPolicy CIDRs) is left as a
`<REPLACE: ...>` literal that fails the schema's `format: uri` /
`format: hostname` / IPv4-CIDR pattern at `helm install` time. A
forgotten substitution surfaces as `at '/networkPolicy/postgresCIDR':
'<REPLACE: ...>' does not match pattern …` instead of silently rendering
a NetworkPolicy that allows everything (or nothing).

The actual `values-rdc.yaml` for the dogfooding consumer is environment-
private and lives in
[`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)'s
`manifests/meho/values-rdc.yaml` per Goal #11 cross-repo deps; the
example here is the public template.

**External Secrets Operator (ESO) sync patterns.** The chart references
operator-provisioned Kubernetes Secrets *by name*
(`postgres.credentialsSecret` and, in v0.2, a Keycloak client-secret
Secret). It does not ship a `Secret` template or accept secret values
via `--set`. The recommended sync mechanism is
[ESO](https://external-secrets.io/) with the upstream store of the
operator's choice (the RDC lab uses
[HashiCorp Vault](https://external-secrets.io/latest/provider/hashicorp-vault/)).
Two resources combine to materialise a chart-consumable Secret:

1. **`ClusterSecretStore`** (cluster-scoped pointer at Vault, carrying
   the auth credentials). Created once per cluster by the platform team.
   **Owned by the consumer's GitOps repo**, not this chart — it outlives
   any release and embeds cluster-level Vault credentials.
2. **`ExternalSecret`** (namespaced resource that pulls keys out of the
   upstream store into a target k8s Secret). Two ownership options:
    - **Default (consumer-managed):** the consumer's GitOps repo applies
      the ExternalSecret alongside or before the chart. The chart
      references the resulting Secret by name. This is the RDC convention.
    - **Opt-in (chart-managed):** flip `eso.enabled: true` in values and
      the chart renders the ExternalSecret(s) itself via
      `templates/externalsecrets.yaml`. The schema requires
      `eso.secretStore.{name,kind}` when `eso.enabled: true`, so a
      misconfigured opt-in fails at install. With the default
      `eso.enabled: false`, `helm template ... | grep -c ExternalSecret`
      returns `0`.

The full ExternalSecret + ClusterSecretStore manifests, the Vault KV
path mapping (`secret/meho/postgres` → `DATABASE_URL`, etc.), and the
end-to-end install ordering (ESO → ClusterSecretStore → ExternalSecret →
wait-for-Secret → `helm install`) are in
[`deploy/values-examples/README.md`](../../deploy/values-examples/README.md).

## Install / upgrade

The recommended flow uses the example values file rather than long
`--set` strings:

```bash
# Copy + substitute the example into your private deploy repo first
# (see deploy/values-examples/README.md).
helm upgrade --install meho ./deploy/charts/meho/ \
  --namespace meho \
  --create-namespace \
  --set image.tag=sha-<git-sha> \
  -f values-rdc.yaml
```

The bare-`--set` equivalent (no values file) — useful for CI smoke
tests:

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

## Publish workflow (`.github/workflows/chart.yml`)

The chart is packaged, pushed to OCI, and cosign-signed by
`.github/workflows/chart.yml`. The workflow targets `meho-runners` (the
project's self-hosted runner pool, per #160 + #167) and shares the
hardening conventions of `image.yml` (Task #33): job-level fork-PR guard,
SHA-pinned actions with `# vX.Y.Z` comments, minimum `permissions:` block
(`contents: read`, `packages: write`, `id-token: write`), per-job
`timeout-minutes`, and a `concurrency:` group that cancels stale runs per
ref.

### Triggers and locked publish behaviour

| Trigger | Jobs run | Side effects |
| --- | --- | --- |
| `pull_request` against `main` (same-repo PRs only) | `validate` | Lint + render + kubeconform; no push, no sign |
| `push` to `main` (chart paths) | `validate` -> `publish` -> `verify-anonymous-pull` | Push at calver `0.1.YYYYMMDD-<short-sha>`, cosign-sign, anonymous-pull check |
| `push` of a `v*` tag | `validate` -> `publish` -> `verify-anonymous-pull` | Push at plain semver `<x.y.z>` (leading `v` stripped), cosign-sign, anonymous-pull check |
| Fork PR | (skipped at job level) | None — `head.repo.full_name != github.repository` short-circuits |

The `push:` block intentionally has no `paths:` filter — path filtering
applies to both branch and tag pushes when set, which would silently skip
a `v*` tag annotating a non-chart commit. Releases always publish; the
cost of an occasional chart re-publish on a non-chart main push is
negligible.

### Version stamping

Inline Python (with the standard-library + PyYAML on the runner) reads
`Chart.yaml`, rewrites `version` and `appVersion` in place, and re-dumps
the file before `helm package` runs. The chart's `name` field stays
`meho-chart` (set permanently in-tree); only `version` and `appVersion`
are workflow-stamped. The post-stamp `cat` of `Chart.yaml` lands in the
workflow log so operators can confirm the published metadata.

### OCI push and signing

`helm push <tgz> oci://ghcr.io/evoila` lands the artefact at
`ghcr.io/evoila/meho-chart:<version>` because Helm derives the basename
from `Chart.yaml`'s `name` field. The push step parses the `Digest:
sha256:...` line from Helm's stdout and exposes it as a step output;
`cosign sign --yes "ghcr.io/evoila/meho-chart@<digest>"` then signs the
chart by digest under the same keyless OIDC identity as the image — the
operator-facing verification command shape is identical between the two
packages (one workflow file path differs from the other, matched by the
`--certificate-identity-regexp`).

### Anonymous-pull verification (Goal #11 DoD)

A dedicated `verify-anonymous-pull` job runs after `publish` on main / tag
pushes. It installs Helm in a fresh job context and intentionally does
**not** call `helm registry login`. The job also scrubs any stale
`HELM_REGISTRY_CONFIG` left over from a prior run on the same self-hosted
runner. `helm pull oci://ghcr.io/evoila/meho-chart --version <ver>` from
that scrubbed environment can only succeed if the GHCR package is public
— a successful pull is the DoD signal.

### First-time public-package step

GHCR creates a new package PRIVATE by default. The first time
`chart.yml` pushes to `ghcr.io/evoila/meho-chart`, the
`verify-anonymous-pull` job will fail with `unauthorized` until a
maintainer flips visibility to public **once**:

```bash
gh api --method PATCH /orgs/evoila/packages/container/meho-chart \
  -f visibility=public
```

(Or via the GHCR UI: org -> Packages -> meho-chart -> Package settings ->
Change visibility -> Public.) The workflow itself cannot do this safely
from CI — visibility is org-scoped and changing it from a workflow would
require a PAT with org-admin scope, which the `GITHUB_TOKEN` lacks. The
image package at `ghcr.io/evoila/meho` had the same one-time gate
documented in `image.yml`'s header comment.

### Verification commands (operator copy-paste)

The published-chart's verification commands live in
[`backend/README.md`](../../backend/README.md) (sections "Verifying chart
signatures" and "Pulling the chart anonymously"), alongside the image's
equivalent commands so an operator learns one verification pattern for
both artefacts. The workflow itself also emits the verification block
into `GITHUB_STEP_SUMMARY` on every successful publish.

## Dependencies

- Image — `ghcr.io/evoila/meho:<tag>` from G2.4 (#31). Multi-arch
  (amd64 + arm64), cosign-signed, SBOM-attested.
- Migration runner — invoked by the `pre-install,pre-upgrade` Job hook
  defined in `templates/migration-job.yaml`; shells out to the
  entrypoint added in G2.3-T3 (#29) — `python -m meho_backplane.db.migrate`.
- Broadcast subchart — in-tree at `charts/broadcast/`, Valkey 9.x per
  ADR 0005. Declared in `Chart.yaml`'s `dependencies:` block with
  `repository: ""` (local unpacked subchart; no
  `helm dependency update` needed).
- External Secrets Operator (ESO) — owns the Kubernetes Secrets the chart
  references (`postgres.credentialsSecret`, future Keycloak client secret,
  future Vault role bindings). ESO is consumer-owned by default; the chart
  references the synced Secrets by name only. The chart can optionally
  render `ExternalSecret` resources itself when `eso.enabled: true` — see
  the ESO patterns section above.

## Known gaps (filled by sibling tasks)

- HPA / PDB / topologySpreadConstraints / ServiceMonitor / PrometheusRule
  — deferred to v0.2. v0.1 is single-replica per Goal #11 scope.
- Broadcast subchart HA (Sentinel/Cluster), persistence, auth —
  deferred to v0.2 per ADR 0005.
- `broadcast.externalEndpoint` opt-out for operators with a managed
  Redis/Valkey already running — deferred to v0.2 (the
  `broadcast.enabled: false` knob lands in v0.1 as the disable path).

## Cross-repo handshake

The v0.1 deploy contract crosses one repo boundary: `evoila/meho`
produces the chart + image; the dogfooding consumer
[`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)
operates the rke2-infra cluster the per-PR ephemeral smoke and
post-merge deploy run against. The handshake spec — cluster auth
options (OIDC trust preferred over a long-lived kubeconfig secret),
namespace-scoped RBAC for `meho-ci-*`, the `meho-image-pushed`
`repository_dispatch` event shape, and the verification commands
either side can run to prove the contract holds — lives in
[`docs/cross-repo/rke2-infra-coordination.md`](../cross-repo/rke2-infra-coordination.md).
The companion script
[`scripts/cross-repo/verify-rke2-access.sh`](../../scripts/cross-repo/verify-rke2-access.sh)
automates the kubectl portion of the check.

## References

- Parent Goal: #11 — Deployable v0.1
- Parent Initiative: #36 — G2.5 Helm chart
- Helm chart structure: https://helm.sh/docs/topics/charts/
- Kubernetes NetworkPolicy: https://kubernetes.io/docs/concepts/services-networking/network-policies/
- cert-manager Ingress annotations: https://cert-manager.io/docs/usage/ingress/
- External Secrets Operator: https://external-secrets.io/
- ESO Vault provider: https://external-secrets.io/latest/provider/hashicorp-vault/
- ESO ExternalSecret API: https://external-secrets.io/latest/api/externalsecret/
- Cross-repo handshake spec: [`docs/cross-repo/rke2-infra-coordination.md`](../cross-repo/rke2-infra-coordination.md)
