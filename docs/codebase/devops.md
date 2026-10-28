# `deploy/` — chart, manifests, and deployment glue

> Durable map of the deployment surface. Update in lock-step with chart
> changes; stale entries are bugs.

## Overview

Everything a consumer needs to install MEHO onto a Kubernetes cluster lives
under `deploy/`. The Helm chart at `deploy/charts/meho/` is the **single
contract** between MEHO and the deployment environment — `helm install` /
`helm upgrade --install` consumes it and produces the core Kubernetes
resources that make up a running backplane:

- Deployment — the backplane Pod (FastAPI app, `uvicorn` on port 8000
  with `--proxy-headers` so the cluster's TLS-terminating Ingress'
  `X-Forwarded-Proto: https` survives FastAPI's trailing-slash 307
  redirects; trusted-proxy CIDR is operator-configurable via
  `config.forwardedAllowIps`, see
  [`docs/cross-repo/reverse-proxy-contract.md`](../cross-repo/reverse-proxy-contract.md)).
- Service — ClusterIP front-door for the Deployment, target port `http`.
- Ingress — TLS-enabled external entry with cert-manager annotations.
- ConfigMap — non-secret env (Keycloak URLs, Vault address, pool sizes,
  `FORWARDED_ALLOW_IPS` for the uvicorn proxy-header trust list, and
  `MEHO_TARGET_SSRF_ALLOWLIST` — the operator-scoped opt-out for the
  target-destination SSRF guard, set via `config.targetSsrfAllowlist`
  (v0.20.0; see `docs/codebase/target-ssrf-guard.md`), and
  `INGEST_JOB_TIMEOUT_SECONDS` — the async ingest-job watchdog budget
  override, set via `config.ingestJobTimeoutSeconds` (rendered only when
  non-empty so the default deploy inherits the backend's 30-min ceiling;
  #2318, see `docs/codebase/spec-ingestion.md`)).
- ServiceAccount — Pod identity, `automountServiceAccountToken: false`.
- NetworkPolicy — default-deny ingress + explicit egress allow-list to
  Postgres, Vault, Keycloak, the broadcast subchart, and CoreDNS only.
- Migration Job — `pre-install,pre-upgrade` Helm hook running
  `python -m meho_backplane.db.migrate` before the Deployment rolls forward.
- Broadcast subchart — in-tree Valkey 9.x Deployment + Service + ConfigMap
  per ADR 0005, plus its **own** ingress NetworkPolicy and a chart-managed
  `requirepass` Secret (#276) so the store is ingress-isolated and
  authenticated regardless of the umbrella policy.
- PostgreSQL subchart — the built-in PostgreSQL 17 with pgvector (#4039),
  off by default (`corePostgres.enabled`); see
  [PostgreSQL subchart](#postgresql-subchart-chartspostgres-4039).

## Log shipping (stdout → collector)

The backplane emits **structured JSON logs to stdout, one object per
line**. Application events (structlog), uvicorn runtime logs, and any
third-party library that logs through Python's standard `logging` module
are all routed through the same JSON pipeline
(`meho_backplane.logging.configure_logging`, #2887). Every record carries
an ISO 8601 UTC `timestamp`, a `level`, the event name, and — for
anything logged inside a request — the correlating `request_id`.

There is no log file and no log endpoint, and the chart ships nothing for
log delivery: the Kubernetes node's container-stdout pathway is the
contract. Wire the cluster's log collector (Fluent Bit / Vector /
Promtail → Loki, or the platform equivalent) to the pod's stdout — each
line is already collector-ready JSON, so no parser or multiline regex is
needed. uvicorn's per-request access lines are intentionally disabled:
the request middleware's `request_completed` / `request_failed` JSON line
already covers them (with `request_id`), so enabling uvicorn access
logging would only duplicate every request line. uvicorn's pre-startup
banner (a handful of lines before the app begins serving) is the one
exception that keeps uvicorn's own plain format.

## Chart layout

```
deploy/charts/meho/
├── Chart.yaml              # apiVersion v2, kubeVersion >=1.28, dependencies: broadcast, result-cache, postgres (alias corePostgres), keycloak (alias coreKeycloak)
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
│   ├── networkpolicy.yaml  # default-deny + explicit egress (broadcast / result-cache egress conditional)
│   ├── migration-job.yaml  # pre-install/pre-upgrade Helm hook (alembic upgrade head)
│   ├── _core-secrets.tpl   # built-in core: hook account name, core Secret names, the secrets Job's plan
│   ├── core-secrets-rbac.yaml # built-in core: hook ServiceAccount + Role + RoleBinding (weight -40)
│   ├── core-secrets-job.yaml  # built-in core: makes missing <release>-core-* Secrets (weight -30)
│   ├── _core-bootstrap.tpl    # built-in core: the bootstrap Job's realm-stage plan
│   ├── core-bootstrap-job.yaml # built-in core: finishes the Keycloak realm after install/upgrade (weight 10)
│   ├── _core-postgres-helpers.tpl # shared core helpers: image triple, OpenShift-aware pod security context
│   ├── tests/test-pgvector.yaml   # helm test pod on the chart's PostgreSQL image
│   ├── _core-keycloak.tpl  # /auth paths for the Ingress + the hostname check (coreKeycloak.enabled only)
│   ├── core-keycloak-checks.yaml # renders nothing; runs those checks
│   └── NOTES.txt           # post-install hints
└── charts/
    ├── broadcast/          # in-tree Valkey 9.x subchart (ADR 0005)
    │   ├── Chart.yaml
    │   ├── values.yaml
    │   └── templates/
    │       ├── _helpers.tpl
    │       ├── deployment.yaml    # single-replica Recreate; readonly rootfs + emptyDir /data
    │       ├── service.yaml       # ClusterIP :6379 (port name "redis")
    │       ├── configmap.yaml     # minimal valkey.conf (requirepass Secret-sourced, no persistence)
    │       ├── secret.yaml        # chart-managed requirepass Secret (#276)
    │       └── networkpolicy.yaml # own ingress policy: 6379 from backplane only (#276)
    ├── result-cache/       # dedicated Valkey for result handles (#3639), same file set
    ├── postgres/           # built-in PostgreSQL 17 + pgvector (#4039), off by default
    │   └── templates/      # statefulset (server + reconcile sidecar), service, configmap (SQL + scripts), networkpolicy
    └── keycloak/           # built-in Keycloak, off by default (#4040)
        ├── Chart.yaml
        ├── values.yaml
        ├── values.schema.json     # checked only when coreKeycloak.enabled is true
        ├── files/meho-realm.json  # the MEHO realm, imported at the first start
        └── templates/             # statefulset, service, configmap-realm, networkpolicy
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

**Digest pinning (`image.digest`, #284 / F15b).** An optional
`image.digest` (`sha256:<64 hex>`, schema-validated) makes the Deployment —
and the migration Job, which the chart keeps byte-identical to the
Deployment — render `repository@digest` instead of `repository:tag`. This
pins the exact, content-addressed digest the image pipeline
(`.github/workflows/image.yml`) scanned, promoted and cosign-signed: unlike
a tag, a digest can never be re-pointed, and it is the reference a
`cosign verify` gate checks the signature against. `tag` stays required as
the human-readable release label; the digest wins in the rendered reference,
and an empty digest (the default) keeps the tag path unchanged. The
`image.yml` quarantine → scan → promote ordering guarantees any digest an
operator can pin from a release alias was scanned before it was advertised.

**Verifying provenance before deploy.** The install path SHOULD gate on a
`cosign verify` of the pinned digest against the trusted keyless identity
before `helm upgrade`, e.g.

```bash
cosign verify "ghcr.io/evoila/meho@${DIGEST}" \
  --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/image\.yml@.*$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

(full recipes, including `cosign verify-attestation` for the SBOM, live in
[`backend/README.md`](../../backend/README.md)). As of #284 **no live
admission policy enforces this** — the cluster has no Sigstore
policy-controller / Kyverno image-verification rule, and the lab installer
(`rdc-hetzner-dc/manifests/meho/install.sh`, a separate repo) runs
`helm upgrade` without a `cosign verify` step. Adding that gate to the
install path is the cross-repo follow-up; this chart supplies the digest pin
it verifies against.

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

For an install with no internet, two global settings reach every pod of
the chart and its subcharts, the `helm test` pods too (#4100):
`global.imageRegistry` replaces the registry host of every image, and
`global.imagePullSecrets` is added to every pod (with the parent's own
`imagePullSecrets`). Every image line uses the helper `meho.core.image`
and every pod uses `meho.core.imagePullSecrets` (both in
`templates/_core-postgres-helpers.tpl`); a new pod must use both, or
`backend/tests/test_chart_image_registry.py` fails. The recipe is in
[`../deploying.md`](../deploying.md#install-with-no-internet).

### Probes

The Deployment renders `startupProbe`, `livenessProbe`, and
`readinessProbe` against the backplane chassis endpoints from G2.1-T2
(`backend/src/meho_backplane/health.py`):

| Probe | Endpoint | Failure semantics | Default timings (operator-tunable) |
| --- | --- | --- | --- |
| `startupProbe` | `/healthz` | Pod **restarts** once the budget is exhausted; disables liveness/readiness until it first passes | `periodSeconds: 10`, `timeoutSeconds: 1`, `failureThreshold: 30` (30 × 10s = 300s / 5-min first-boot budget) |
| `livenessProbe` | `/healthz` (always 200 if the process is up) | Pod **restarts** on failure | `initialDelaySeconds: 30`, `periodSeconds: 10`, `timeoutSeconds: 1`, `failureThreshold: 3` |
| `readinessProbe` | `/ready` (200 only when every registered probe in the readiness registry passes; 503 with an empty registry at the chassis stage) | Pod **removed from Service endpoints**, no restart | `initialDelaySeconds: 5`, `periodSeconds: 5`, `timeoutSeconds: 2`, `failureThreshold: 3` |

The `startupProbe` (Issue #2393) exists because first boot registers the
full typed-op catalog and preloads the fastembed embedding model inside the
FastAPI lifespan **before** the app binds `:8000` — ~2-3 minutes, and longer
on a cold install where the fastembed cache PVC is empty and the model
weights are downloaded. The kubelet disables the liveness and readiness
probes until the startup probe first succeeds, so a slow-but-healthy first
boot no longer trips the short-delay liveness probe into a
CrashLoopBackOff. The budget is `failureThreshold × periodSeconds` (300s by
default); once it passes, the liveness probe's fast 30s detection window
takes over for genuine hang detection. Inflating
`probes.liveness.initialDelaySeconds` (the old-only lever) is strictly
worse — it also blinds liveness to a real hang for the whole life of the
Pod.

The 30-second liveness `initialDelaySeconds` gives the FastAPI app time to
import, build the JWKS cache, and bind structlog context before the first
check — under-provisioning it would restart-loop the Pod during slow image
pulls or cold-start library imports. The shorter readiness window (15s
total detection) makes the Pod fall out of rotation promptly when a
downstream dependency goes flaky, without triggering an unnecessary
restart of the backplane process itself.

Liveness and readiness are **always on** — there is no `enabled: false`
escape valve. Disabling them would mask startup deadlocks and let an
unready Pod accept traffic; that tradeoff is never the right call for a
governance backplane. Every field under `probes.liveness.*`,
`probes.readiness.*`, and `probes.startup.*` in `values.yaml` is
operator-tunable for environments that need different timings. The
`startupProbe` alone is rendered under a `{{- with .Values.probes.startup }}`
guard so an operator on a fast cluster can opt out by clearing
`probes.startup` (e.g. `--set probes.startup=null`); it ships defaulted-on
because slow first boots are the common case.

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
- Keycloak — `tcp/443` to `networkPolicy.keycloakCIDR`; with the built-in
  Keycloak (`coreKeycloak.enabled`) instead `tcp/8080` to a `podSelector`
  matching its pod (`app.kubernetes.io/name: keycloak` and the release),
  and `keycloakCIDR` is not used (#4591)
- Broadcast subchart — `tcp/<broadcast.service.port>` (default 6379) to a
  `podSelector` matching the in-cluster broadcast subchart's selector
  labels (`app.kubernetes.io/name: broadcast`); the rule is conditional
  on `broadcast.enabled: true` and is omitted when the broadcast subchart
  is disabled
- DNS — `udp/53` to the `k8s-app: kube-dns` selector (matches CoreDNS)
- Operator rules — every item of `networkPolicy.extraEgress` (default
  `[]`), added after the rules above, exactly as written (#3516)

Without `extraEgress` the backplane cannot reach the systems it manages
or the AI service, yet `/ready` stays green, because readiness only checks
MEHO's own dependencies. Each item must have a non-empty `to` list:
Kubernetes reads a missing or empty `to` as every destination, so a stray
`- {}` would silently undo the default-deny egress. The schema checks this,
and the template checks it again with `fail`, because Helm, Flux and Argo
CD can all skip the schema (`--skip-schema-validation`). The schema also
accepts only the keys Kubernetes knows in a rule, a `to` item and a `ports`
item, so a typo cannot quietly drop a port limit. Kubernetes leaves
it undefined whether a rule is checked before or after a Service redirect;
Calico / Canal with kube-proxy check it after. So when Vault or Keycloak
sit behind an in-cluster ingress, traffic arrives at the ingress Pod's own
port (Traefik `websecure` 8443), the fixed `tcp/8200` / `tcp/443` rules do
not match, and the operator adds an `extraEgress` rule selecting the
ingress Pods on that port.

The broadcast clause above is an **egress** allowance on the backplane
side. A Pod is ingress-isolated only when *some* NetworkPolicy selects it,
and this policy selects the backplane Pods — not the broadcast store. The
broadcast subchart therefore ships its **own** ingress NetworkPolicy
(`charts/broadcast/templates/networkpolicy.yaml`, #276) selecting the
broadcast Pod and admitting `tcp/6379` only from the backplane Pods (and
any `broadcast.networkPolicy.extraAllowedSelectors`), so the store's
isolation does not depend on the umbrella policy alone.

Ingress is permitted only from the namespace whose
`kubernetes.io/metadata.name` label matches
`networkPolicy.ingressControllerNamespace` (default `ingress-nginx`,
RKE2's bundled controller).

`networkPolicy.ingestAllowedNamespaces` (default `[]`) renders a
**second** ingress rule for **in-cluster webhook senders** delivering to
the inbound event-ingest endpoint (`POST
/api/v1/events/ingest/{source_slug}`, #2881): each listed namespace
becomes an OR-ed `kubernetes.io/metadata.name` peer admitted to
`tcp/8000`, matched on the label kube-apiserver stamps on every namespace
(Kubernetes ≥ 1.22). Empty by default, so the rule is omitted and the
backplane stays default-deny — a sender in another namespace otherwise
has to hair-pin through the ingress hostname. The full operator flow
(enable the knob, register the `event_source`, custody its per-source
secret, point the sender's webhook at the in-cluster Service URL) is the
[In-cluster webhook senders](../deploying.md#in-cluster-webhook-senders)
section of the deploy guide. SaaS-origin senders and the satellite-runner
inbound listener are out of scope — they reach the endpoint through the
ingress hostname, not this rule.

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
- **No `serviceAccountName`** — unlike the backplane Deployment, the Job
  deliberately omits it (#2391). As a `pre-install,pre-upgrade` hook the
  Job is scheduled *before* Helm creates the chart's normal (non-hook)
  resources, so referencing the chart-managed `meho` ServiceAccount here
  deadlocks a fresh `helm install` (`serviceaccount "meho" not found` on
  every admission attempt until the release times out). The runner needs
  no Kubernetes API access, so the pod falls back to the namespace
  `default` SA and — with `automountServiceAccountToken: false` — mounts
  no token. Annotating the shared `meho` SA as a hook is *not* the fix:
  it backs the running Deployment and a hook-delete-policy would delete
  it out from under that Deployment.
- Same `imagePullSecrets` and pod/container `securityContext` as the
  backplane Deployment (`runAsNonRoot`, `readOnlyRootFilesystem`,
  `drop: [ALL]`), with `/tmp` mounted as an `emptyDir` to keep the
  read-only root invariant.
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

**pgvector superuser prerequisite (cold install).** Revision `0003`
(`backend/alembic/versions/0003_create_documents_with_pgvector.py`) runs
`CREATE EXTENSION IF NOT EXISTS vector`, which PostgreSQL only allows a
**superuser** to execute (the `vector` extension is not marked trusted).
The migration Job runs under the app-role `DATABASE_URL`, so a **cold**
install against a least-privilege role fails at this step with
`permission denied to create extension "vector"`. The extension must be
pre-created once by a superuser (or bootstrapped via CNPG
`postInitSQL`) — see the `deploy/values-examples/README.md` § *pgvector
extension prerequisite* and the recorded decision at
`docs/decisions/pgvector-superuser-prerequisite.md` (which rejects a
dedicated `migrationSuperuserDsn` chart value in favour of the documented
prerequisite). The chart does **not** ship a superuser migration DSN. With
the built-in PostgreSQL (`corePostgres.enabled: true`, below) the chart
pre-creates the extension itself, so this step goes away; the migrations
still run as a role that is not a superuser.

### Core secrets Job (`templates/core-secrets-*.yaml`)

Rendered only when a built-in part is on (`corePostgres.enabled` or
`coreKeycloak.enabled`); with every part off the chart renders none of these
objects. A pre-install and pre-upgrade hook Job (weight -30) runs
`python -m meho_backplane.core.secrets` from the backplane image and makes
every missing `<release>-core-*` Secret: database passwords, the Keycloak
first-start service account, the three MEHO client secrets, the approval
hand-off key, the Valkey password and a first TLS certificate. It never
changes an existing Secret and prints no value. It runs under its own hook
ServiceAccount, Role and RoleBinding (weight -40, all four phases), which
may create Secrets, read and patch only the `<release>-core-*` names, and
read the Secrets the operator names in values.
Secret table, rules and rights: [core-secrets.md](core-secrets.md).

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
| Auth | `requirepass` (default on), Secret-sourced via `secretKeyRef` (#276) | Defense in depth alongside the ingress policy; `protected-mode` stays on |
| Network isolation | Own ingress NetworkPolicy — `tcp/6379` from backplane Pods only (#276) | The parent policy selects the backplane, not the store; the store needs its own selector to be isolated |
| Update strategy | `Recreate` | Single-replica + port-bind constraint makes RollingUpdate worse |
| Probes | TCP `connect` on 6379 | Minimal — avoids coupling to `redis-cli` / `valkey-cli` binary naming variance |
| Service | ClusterIP `<release>-broadcast:6379` (port name `redis`) | In-cluster only; backplane consumes via the operator-facing `BROADCAST_REDIS_URL` env |

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
backplane Deployment renders a `BROADCAST_REDIS_URL` env var pointing at
`redis://{{ .Release.Name }}-broadcast:{{ .Values.broadcast.service.port }}/0`.
The env-var name is load-bearing: `Settings.broadcast_redis_url`
(`backend/src/meho_backplane/settings.py`) resolves from
`BROADCAST_REDIS_URL` and falls back to `redis://localhost:6379` when it
is unset, so a chart that injects any other name (the v0.2 `REDIS_URL`
mismatch fixed in #583) leaves the readiness probe's broadcast leg
dialing localhost while the healthy Service is never contacted. ADR 0005
locked `redis-py` as the driver — it parses `redis://` schemes against a
Valkey endpoint unchanged (wire-protocol compatibility carries from
Redis 7.2.4).

**Valkey auth (#276).** `broadcast.auth.enabled: true` (the default)
renders a chart-managed Secret (`<release>-broadcast-auth`, key
`requirepass`) — the password is `broadcast.auth.password` when set, an
operator's `broadcast.auth.existingSecret` when supplied, or a generated
32-char value preserved across upgrades via a `lookup` of the existing
Secret. The store reads it into a runtime config file at Pod start-up (kept
out of the ConfigMap and off the process command line); the backplane reads
the *same* Secret into `BROADCAST_REDIS_PASSWORD`, and
`Settings.broadcast_redis_password` is applied as the `password` kwarg on
both `redis.asyncio.from_url` clients (`broadcast/client.py`). The
`BROADCAST_REDIS_URL` above stays password-free so no secret rides a
plaintext env `value:`. Setting `broadcast.auth.enabled: false` drops the
Secret, the client password, and flips `protected-mode` off so the
NetworkPolicy-gated port still accepts connections.

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

### Result-cache subchart (`charts/result-cache/`)

A second in-tree Valkey 9.x subchart, off by default
(`resultCache.enabled: false`). It holds the full rows behind result
handles, so a burst of large handles can only fill this store, not the
broadcast store that also holds rate limits and approval waits.

- **Shape.** Same as the broadcast subchart: `valkey/valkey:9.0-alpine`,
  one-replica `Recreate` Deployment, no PVC (`save ""`, `appendonly no`),
  read-only root filesystem, `requirepass` from a Secret
  (`<release>-result-cache-auth` or `resultCache.auth.existingSecret`),
  ClusterIP Service `<release>-result-cache:6379`, and its own ingress
  NetworkPolicy (tcp/6379 from the backplane Pods only).
- **Memory ceiling.** `valkey.conf` sets `maxmemory`
  (`resultCache.maxmemory`, default `768mb`) and `maxmemory-policy`
  (`resultCache.maxmemoryPolicy`, default `noeviction`). At the ceiling
  Valkey refuses new result writes; the backplane treats that as "rows not
  stored" and still returns the inline sample. The pod memory limit
  (`1Gi`) sits above the ceiling. The broadcast store is not changed.
- **Values key.** The parent `Chart.yaml` maps `resultCache:` to the
  subchart with `alias: resultCache`. An alias also renames
  `.Chart.Name`, so the subchart helpers pin the resource name to
  `result-cache`. There is no `nameOverride`: the backplane URL, the
  Secret reference, and the egress selector all use the fixed name, and
  the schema rejects the key. The parent chart builds the URL host and
  the Secret name with the subchart's `resultCache.fullname` helper, so
  a long release name that gets cut to 63 characters still matches.
- **Ports.** `resultCache.service.port` only changes the Service's front
  port. Valkey always listens on 6379, and both NetworkPolicies admit
  that Pod port, so a custom Service port does not block the traffic.
- **Config changes roll the pod.** The pod template carries a
  `checksum/config` annotation over the ConfigMap. A new `maxmemory` or
  `maxmemoryPolicy` therefore restarts the pod on upgrade; Valkey reads
  its config at start-up only.
- **Backplane wiring.** When enabled, the backplane Deployment gets
  `RESULT_CACHE_REDIS_URL`, `RESULT_CACHE_REDIS_PASSWORD` (secretKeyRef),
  and `RESULT_CACHE_WRITER_ENABLED` (from `resultCache.writerEnabled`),
  and the parent NetworkPolicy gets an egress rule to the result-cache Pods.
  How reads and writes move between the two stores:
  [result-spill.md](result-spill.md#dedicated-result-cache-store-3639).

**Roll order (result-cache).** Every deploy that turns this on (the
central deploy and any GitOps-managed install) gains one result-cache pod.

1. Set `resultCache.enabled: true` with `resultCache.writerEnabled: false`
   and roll out. Backplane Pods now read the result-cache store first and
   fall back to the broadcast store; they still write to the broadcast store.
2. Check that the backplane can reach the new store. A wrong NetworkPolicy
   drops the traffic without an error, and every result read on the new
   store then misses. Check that `result_cache_reads_total` rises and that
   backplane logs show no `result_handle_fetch_failed` with
   `namespace=v2`.
3. Only after every backplane Pod runs this code, set
   `resultCache.writerEnabled: true` and roll out again. New handles are
   then written to the result-cache store. Old handles stay readable from
   the broadcast store until their TTL ends.

### PostgreSQL subchart (`charts/postgres/`, #4039)

The built-in PostgreSQL of the core. Off by default
(`corePostgres.enabled: false`); it turns on by default once the existing
bring-your-own installs set `corePostgres.enabled: false` in their values.
Design: the internal core design note, §2 (one database server) and §1
(images, OpenShift).

- **Shape.** One StatefulSet `<release>-postgres` with one replica, one PVC
  (`data`, 10 Gi, `corePostgres.persistence.*`) and a ClusterIP Service
  `<release>-postgres:5432`. Image `pgvector/pgvector:0.8.7-pg17-bookworm`,
  pinned by digest (`corePostgres.image.{repository,tag,digest}`, digest
  wins). The image is built FROM the official `postgres:17-bookworm` image,
  so the official entrypoint runs. PostgreSQL 18 is not used: it moves the
  data directory layout.
- **Two databases, two roles.** One SQL file (`core.sql` in the ConfigMap)
  makes the roles `meho` and `keycloak` (`LOGIN NOSUPERUSER NOCREATEDB
  NOCREATEROLE`, connection limits 60 and 25), the databases `meho` and
  `keycloak`, each owned by its role, takes CONNECT and TEMPORARY away from
  PUBLIC, and creates `vector` in `meho` as the image superuser. Every
  statement is safe to run twice (`CREATE` is guarded with `\gexec`).
  Passwords reach SQL only as psql variables (`\getenv`).
- **Two paths, one file.** On an empty disk the entrypoint runs `apply.sh`
  from `/docker-entrypoint-initdb.d`. On every pod start, and whenever a
  mounted password changes, the `reconcile` sidecar runs the same
  `apply.sh`. So a regenerated or restored Secret never drifts from the
  live role (the failure shape of a chart regenerating a database password
  while the database keeps the old one). The sidecar waits for the server
  on TCP (the initdb temporary server listens on the socket only), checks
  every 30 s, and connects as `postgres` over the shared socket directory
  (`/var/run/postgresql`, an emptyDir) through initdb's `local all all
  trust` line. It never needs the superuser password.
- **Secrets.** The chart makes no password. It reads
  `<release>-core-postgres-superuser`, `-meho` and `-keycloak` (key
  `password`), which the install's secrets Job makes (#4041). The
  superuser Secret is mounted by the `postgres` container alone, as
  `POSTGRES_PASSWORD_FILE`; initdb reads it once. The sidecar mounts only
  the two role Secrets. `corePostgres.existingSecrets.{superuser,keycloak}`
  names operator-made Secrets instead.
- **Logins.** `POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256`: TCP logins
  from inside the pod (127.0.0.1, also `kubectl port-forward`) need a
  password; only the local socket is `trust`. Other pods use
  `scram-sha-256`.
- **User ids.** `runAsUser`/`runAsGroup`/`fsGroup` 999 (the image's
  `postgres` user). `global.openshift: true` leaves the three out through
  the shared helper `meho.core.podSecurityContext`
  (`templates/_core-postgres-helpers.tpl`), so OpenShift picks them;
  `runAsNonRoot`, `seccompProfile: RuntimeDefault`, `drop: [ALL]` and no
  privilege escalation stay. PGDATA is a sub-directory of the mount
  (`/var/lib/postgresql/data/pgdata`), so initdb gets an empty directory it
  owns under any user id; the image's entrypoint fakes a passwd entry for a
  user id it does not know. The root filesystem is read-only, with
  emptyDirs at `/tmp`, `/var/run/postgresql` and a memory emptyDir at
  `/dev/shm`.
- **Probes.** `pg_isready` on `127.0.0.1:5432` for startup (up to 5 min
  for a first setup), readiness and liveness.
- **NetworkPolicy.** Its own ingress-only policy admits tcp/5432 from the
  backplane pods (`networkPolicy.backplanePodLabels`; the migration Job and
  the helm test pod carry the same labels) and the Keycloak pods
  (`networkPolicy.keycloakPodLabels`) of the release. The parent policy's
  egress rule to the built-in database (by pod, not CIDR) comes with #4050.
- **Disk lifetime.** The PVC outlives `helm uninstall` (StatefulSet default).
  Delete the PVC and the generated Secrets together, or neither.
- **After a restore.** A restored dump brings the role passwords of the
  dump. Restart the pod after a restore: the sidecar then sets the
  passwords from the Secrets again (it also does so on the next Secret
  change, but not on its own clock).
- **What wires it to the backplane.** Not this subchart: the
  `DATABASE_URL` default, the schema conditionals and the migration as an
  init container come with #4041 and #4047.

**Helm test.** `templates/tests/test-pgvector.yaml` runs on the same
image (`corePostgres.image`), in both modes, as user 999. It needs no
Bitnami image.

**Bring-your-own render.** With the built-in parts off, a new chart version
must render the same objects as the previous one.
`backend/tests/test_chart_bring_your_own_render.py` renders
`deploy/values-examples/values-bring-your-own-fixture.yaml` and compares it
with `backend/tests/fixtures/chart/bring-your-own-render.golden` (blank
lines and the random broadcast password evened out). A wanted change is
committed with a new copy (`MEHO_UPDATE_CHART_RENDER=1`), so the review
shows it.

**Database tests.** `backend/tests/_pgvector_image.py` gives the
testcontainers database tests the chart's image (`repository@digest` from
`values.yaml`); CI's `MEHO_TEST_PGVECTOR_IMAGE` pins the same digest
through the registry mirror, and a test keeps the two equal.

**CI.** `chart.yml` validates a `corePostgres.enabled=true` render (also
with `global.openshift=true`) with kubeconform. The kind job starts the
subchart (`--show-only` its templates, with the three Secrets made the way
the secrets Job makes them), installs the release in bring-your-own mode
against it so the migration Job runs every migration as `meho`, runs
`helm test`, and then checks the roles, the fencing, `vector` and a
reconcile after a changed Secret plus a pod restart.

### Built-in Keycloak subchart (`charts/keycloak/`)

The install can bring its own Keycloak, the login server, so nobody sets
up a realm by hand. It is **off by default** (`coreKeycloak.enabled:
false`). With it off, the chart renders exactly what it rendered before.

**What it needs.** The built-in PostgreSQL (`corePostgres.enabled:
true`; the schema refuses `coreKeycloak` without it) and five Secrets
that the install's secrets Job creates. Keycloak only reads them:

| Secret (default name) | Keys | Used for |
| --- | --- | --- |
| `<release>-core-postgres-keycloak` | `password` | `KC_DB_PASSWORD`, the `keycloak` database role |
| `<release>-core-keycloak-bootstrap` | `client_id`, `client_secret` | the temporary admin service account in the master realm |
| `<release>-core-keycloak-admin` | `client_secret` | the `meho-admin` client |
| `<release>-core-ui-auth` | `client_secret` | the `meho-web` client (web console login) |
| `<release>-core-check-runner` | `client_secret` | the `meho-check-runner` client |

Keycloak reads exactly these names. To use your own value, create the
Secret with that name before the install; the secrets Job keeps a Secret
that exists. Only the bootstrap Secret may have another name, through
`coreKeycloak.existingSecrets.bootstrap`, which the secrets Job reads too.
While the built-in Keycloak is on, leave
`corePostgres.existingSecrets.keycloak`,
`keycloakAdmin.clientSecret.secretName`, `uiConsole.secretName` and
`checkRunner.clientSecret.secretName` empty (or set them to the core name):
the subchart cannot read these parent keys, so with another name the
secrets Job would check that Secret, not make the core one, and the
Keycloak pod would wait for it forever. The render stops with one sentence
that names the key and the expected Secret (#4591). The pod does not start
until all five exist.

**What it renders.**

| Object | Shape |
| --- | --- |
| StatefulSet `<release>-keycloak` | one pod, `quay.io/keycloak/keycloak:26.8.1` pinned by digest, `start --import-realm`, `KC_CACHE=local` |
| init container `wait-for-database` | the PostgreSQL image; loops `pg_isready -U keycloak -d keycloak` until the database answers, because Keycloak does not retry at start |
| ConfigMap `<release>-keycloak-realm` | `meho-realm.json`, mounted at `/opt/keycloak/data/import` |
| Service `<release>-keycloak` | port 8080, plain HTTP inside the namespace |
| two more paths on the backplane's Ingress (parent chart, `templates/_core-keycloak.tpl`) | on `ingress.host`: `/auth/realms/meho` and `/auth/resources` only (`Prefix`), to the Keycloak Service; `coreKeycloak.ingress.exposeAdmin: true` adds `/auth/admin` and `/auth/realms/master`. One Ingress keeps one set of annotations and one TLS certificate for the host |
| NetworkPolicy `<release>-keycloak` | off by default; admits port 8080 from the backplane pods and `networkPolicy.extraFrom` |

**Addresses.** Browsers, the CLI and the `iss` claim use the public URL
`https://<coreKeycloak.hostname>/auth` (`KC_HOSTNAME`,
`KC_HTTP_RELATIVE_PATH=/auth`). `coreKeycloak.hostname` must equal
`ingress.host`; the render fails otherwise. Pods use the Service,
`http://<release>-keycloak:8080/auth`. With
`KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true`, the discovery document fetched
at the Service URL names the public `issuer` and Service-URL
`jwks_uri` and `token_endpoint`, so tokens keep the public `iss`.
`KC_PROXY_HEADERS=xforwarded`, because TLS ends at the ingress.

**Settings that matter.** `KC_DB_POOL_MAX_SIZE=20` (initial and minimum
2), so Keycloak and the backplane fit PostgreSQL's 100 connections. Probes
use the management port 9000: `startupProbe` on `/auth/health/started`
(60 × 5 s, for the build, the schema and the import at the first start),
then `/auth/health/ready` and `/auth/health/live`. Resources: request
250m / 1Gi, limit 1 CPU / 2Gi (the heap is 70 % of the limit). Measured on
kind: about 45 seconds from container start to ready; 168 MiB written to
the container's own disk by the build at start.

**Security.** No `runAsUser`: the image runs as user 1000 in group 0 and
`runAsNonRoot` checks that number. `global.openshift: true` removes
`runAsUser`, `runAsGroup` and `fsGroup` from every security context of the
subchart (OpenShift assigns them), through the shared helper
`meho.core.podSecurityContext`; the image reference uses `meho.core.image`
(both in `templates/_core-postgres-helpers.tpl`). The init container uses user 999 (the
PostgreSQL image has no `USER` line), also removed under
`global.openshift`. `readOnlyRootFilesystem: false` for the Keycloak
container only: the stock image rebuilds under `/opt/keycloak` at every
start. Checked on kind: the pod starts under the `restricted` Pod Security
Admission label, and again with user id 1000650000 in group 0.

#### The realm (`charts/keycloak/files/meho-realm.json`)

Keycloak imports the file at its **first start only**. When the realm
exists, the import is skipped, so a changed file does not change a running
realm. Changes after the first start go through the realm stage of the
bootstrap Job.

The file holds **no secret and no host name**. Keycloak fills every
`${...}` from the pod's environment during the import:

| Placeholder | Value |
| --- | --- |
| `MEHO_REALM_PUBLIC_URL` | `https://<coreKeycloak.hostname>` |
| `MEHO_REALM_TENANT_ID` | `coreKeycloak.firstTenantId` (a UUID; pick one per install and keep it for a move) |
| `MEHO_REALM_TENANT_NAME` | `coreKeycloak.tenantName` (the first tenant's short name, default `main`; goes into `meho_tenant`) |
| `MEHO_REALM_ADMIN_CLIENT_SECRET`, `MEHO_REALM_WEB_CLIENT_SECRET`, `MEHO_REALM_CHECK_RUNNER_CLIENT_SECRET` | the three client Secrets above |

**Groups.** Each group carries the first tenant id and a role:

| Group | `tenant_role` | Other attribute |
| --- | --- | --- |
| MEHO-Readers | `read_only` | |
| MEHO-Operators | `operator` | |
| MEHO-SA-Operators | `operator` | |
| MEHO-Admins | `tenant_admin` | |
| MEHO-Approvers | `read_only` | `approver` = `true` |

**A person is in exactly one of these groups.** The mappers take the first
group that has the attribute; with two groups the result is not
predictable. The realm keeps Keycloak's default user profile, which does
not accept attributes it does not declare, so nobody can put `tenant_role`
on a person directly: the claim always comes from the group.

**Clients.**

| Client | Kind | Notes |
| --- | --- | --- |
| `meho-backplane` | confidential, no login flow | the token audience only |
| `meho-cli` | public, device flow | `meho login`; no `offline_access` (as `bootstrap-clients` without `--cli-offline-access`) |
| `meho-mcp` | public, authorization code + PKCE (S256) | redirect URIs `http://localhost:*`, `http://127.0.0.1:*`; optional scopes `offline_access`, `mcp:read`, `mcp:execute`, `mcp:admin`; offline login idle limit 48 hours |
| `meho-web` | confidential, authorization code + PKCE (S256) | redirect `https://<host>/ui/auth/callback`, after logout `https://<host>/ui/auth/login` |
| `meho-admin` | confidential, service account | realm-management roles `manage-clients` and `manage-users` |
| `meho-check-runner` | confidential, service account | its tokens carry the audience `meho-check-runner`, not `meho-backplane` |

The three clients people log in with (`meho-cli`, `meho-mcp`, `meho-web`)
have the default scopes `basic`, `roles`, `web-origins`, `acr`,
`profile`, `email` and the eight mappers `meho admin keycloak
bootstrap-clients` sets: `tenant_id`, `tenant_role`, `approver` and
`platform_admin` with the stock User Attribute mapper (`aggregate.attrs`
and `multivalued` false; into the access token and userinfo; `approver`
and `platform_admin` as booleans), `meho-tenants` (the `meho_tenant`
values of all the person's groups as the list `meho_tenants`;
`aggregate.attrs` and `multivalued` true), the audience mappers for
`meho-backplane` and `https://<host>/mcp`, and the `groups` claim. No client has a fixed-value tenant or role mapper. The group model is
the decision in `docs/decisions/login-role-groups.md`.

**Realm settings.** Offline sessions end after 7 days at most
(`offlineSessionMaxLifespanEnabled`). Brute-force detection is on.
Registration is off. `identityProviders` is empty: the company login comes
later. `attributes.CreateDefaultClientScopes: "true"` keeps Keycloak's
built-in client scopes (`basic`, `profile`, `roles`, …) even though the
file lists its own `mcp:*` scopes; this attribute needs Keycloak 26.6 or
newer.

#### How the backplane uses it (#4591)

With `coreKeycloak.enabled` on, the chart sets every Keycloak setting of the
backplane itself (helper `meho.keycloakSettings` in
`templates/_core-keycloak.tpl`). The values come from the subchart and the
realm file, not from `config.*` or `keycloakAdmin.*`:

| Setting | Value |
| --- | --- |
| `KEYCLOAK_ISSUER_URL` | `https://<coreKeycloak.hostname>/auth/realms/meho`, the `iss` of every token |
| `KEYCLOAK_DISCOVERY_URL` | `http://<release>-keycloak:8080/auth/realms/meho`, the Service |
| `KEYCLOAK_AUDIENCE` | `meho-backplane` |
| `KEYCLOAK_CLI_CLIENT_ID` | `meho-cli` |
| `UI_KEYCLOAK_CLIENT_ID` | `meho-web` |
| `KEYCLOAK_ADMIN_URL` | `http://<release>-keycloak:8080/auth/admin/realms/meho` |
| `KEYCLOAK_ADMIN_CLIENT_ID` | `meho-admin` |
| `KEYCLOAK_ADMIN_CLIENT_SECRET` | Secret `<release>-core-keycloak-admin`, key `client_secret` |

The `KEYCLOAK_ADMIN_*` settings render whether or not
`keycloakAdmin.enabled` is set. `KEYCLOAK_DISCOVERY_URL` is the realm base
URL for the backplane's own calls: the discovery document (keys, readiness
probe, web console endpoints) and the token endpoint (admin client, check
runner, agent runs). The discovery document fetched there names the public
issuer and authorization endpoint and Service-URL keys and token endpoints,
so tokens keep the public `iss`, browsers keep the public login pages, and
no pod calls the public address. The issuer check, `/api/v1/auth-config`
and `/.well-known/oauth-protected-resource` keep the public issuer. With
the built-in Keycloak off, none of this renders: `KEYCLOAK_DISCOVERY_URL`
is absent and every other setting comes from values as before, so a
bring-your-own render is byte for byte the same
(`backend/tests/test_chart_bring_your_own_render.py`).

The render fails with one sentence when the values contradict the built-in
Keycloak:

- `config.keycloakIssuerUrl` or `keycloak.issuer` is set (the chart sets
  the issuer);
- `corePostgres.existingSecrets.keycloak`,
  `keycloakAdmin.clientSecret.secretName`, `uiConsole.secretName` or
  `checkRunner.clientSecret.secretName` names a Secret other than the
  matching `<release>-core-…` one, for example
  `uiConsole.secretName must be empty or "meho-core-ui-auth" while
  coreKeycloak.enabled is on: the built-in Keycloak reads only that
  Secret.`

The schema needs `keycloak.issuer`, `config.keycloakIssuerUrl` and
`config.keycloakAudience` only while the built-in Keycloak is off.

**HTTPS inside the cluster.** The realm keeps `sslRequired: external`, and
pods still call the Service over plain HTTP. That works for every pod
address: Keycloak's check (`TokenEndpoint.checkSsl`, Keycloak 26.8.1) looks
at the scheme of the frontend address, which `KC_HOSTNAME=https://<host>/auth`
fixes to https. Without `KC_HOSTNAME` the request's own scheme and source
address decide, and a plain HTTP call from a non-private address would get
"HTTPS required". Both cases are tested against a real Keycloak in
`backend/tests/integration/test_core_keycloak_realm_import.py`
(`sslRequired: all` still passes with the fixed address and fails without
it). Browsers and the CLI come through the ingress, which ends TLS.

**Trust inside the cluster.** With the built-in Keycloak, every call from a
pod to Keycloak is plain HTTP. Nothing on this path is encrypted:

- the signing keys the backplane uses to check every token;
- the client secrets of `meho-admin`, the agents and the check runner, and
  the tokens Keycloak sends back;
- the web console's login code exchange and the user tokens it returns;
- the realm setup Job's bootstrap client secret.

This matters for two reasons:

- `meho-admin` can manage the realm's clients and users. Anyone who reads
  its secret can make any person a MEHO admin.
- Anyone who can serve fake signing keys to the backplane can sign tokens
  that the backplane accepts, for any tenant and any role.

`sslRequired` does not protect this path (see above).

What protects it today:

- The backplane's own NetworkPolicy (`networkPolicy.enabled`, on by
  default) lets the backplane connect only to this release's Keycloak pods,
  on 8080. So a pod elsewhere in the cluster cannot pose as Keycloak. This
  needs a network plugin that enforces NetworkPolicy. Someone who can
  create pods in this namespace can still pose as Keycloak, but that person
  can already read the namespace's Secrets.
- Nothing else yet. The Keycloak subchart's own NetworkPolicy
  (`coreKeycloak.networkPolicy.enabled`) is off by default. While it is
  off, any pod in the cluster can reach Keycloak on 8080, including its
  Admin REST API. The ingress hides the admin paths only from callers
  outside the cluster.

What an operator can do now:

- Turn on the Keycloak NetworkPolicy, and allow the ingress controller in
  `extraFrom`, or browser and CLI logins stop. For example, with Traefik in
  `kube-system`:

  ```yaml
  coreKeycloak:
    networkPolicy:
      enabled: true
      extraFrom:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
  ```

- Run a service mesh that encrypts traffic between pods (mutual TLS).

A NetworkPolicy controls who can connect. It does not encrypt. Only
encryption helps against someone who can read traffic on the cluster
network, for example from a taken-over node. #4050 turns the Keycloak rule
on by default, and it decides whether Keycloak's port 8080 gets TLS, along
with OpenBao's port 8200.

**Known limits.**

- The admin console does not work through `kubectl port-forward`: its
  login page uses the public address, and `/auth/realms/master` is not
  routed unless `exposeAdmin` is true. The Admin REST API works through
  the Service and through `port-forward`.

### Built-in OpenBao subchart (`charts/openbao/`, #4044)

The install can bring its own password safe, OpenBao (ADR 0008: OpenBao,
not HashiCorp Vault). It is **off by default** (`coreVault.enabled:
false`). With it off, the chart renders exactly what it rendered before.
The `vault:` block stays for a Vault you bring yourself. MEHO's names stay
`vault` (`coreVault`, `<release>-vault`, `VAULT_ADDR`): they name the job,
not the product.

**Not yet:** the setup Job `vault-init-<hash>` (the first start, the key
and login setup, the policies). Until it lands, the server runs empty and
locked, and nothing makes the Secret `<release>-core-vault-unseal`.

**What it renders.**

| Object | Shape |
| --- | --- |
| StatefulSet `<release>-vault` | one pod, `quay.io/openbao/openbao-ubi:2.7.1` pinned by digest, the image's start script with `server` and `SKIP_CHOWN=true`; a 10Gi disk (`persistence.size`) at `/openbao/data`; a second container `unseal` (below) |
| ConfigMap `<release>-vault-config` | `server.hcl` at `/openbao/config`: raft storage, a plain-HTTP listener on 8200 (`tls_disable`), `ui = false`, the `file` audit device with `file_path = "stdout"`. A changed config restarts the pod (checksum annotation) |
| Service `<release>-vault` | port 8200; `publishNotReadyAddresses: true` |
| Service `<release>-vault-internal` | headless, ports 8200 and 8201; `publishNotReadyAddresses: true`. It gives the pod its DNS name, which sets `BAO_API_ADDR` and `BAO_CLUSTER_ADDR` |
| NetworkPolicy `<release>-vault` | off by default; admits port 8200 from the backplane pods and `networkPolicy.extraFrom` |
| ServiceAccount `<release>-vault` and ClusterRoleBinding `<namespace>-<release>-vault-auth-delegator` | only with `coreVault.authDelegator: true` (default false); the chart's only cluster-wide object |

**A locked pod is not ready.** The readiness check is `bao status`, which
exits 2 while OpenBao is locked ("sealed") or not set up yet. There is no
liveness check: a restart would not unlock it. Both Services still reach a
pod that is not ready, so the setup Job can start OpenBao and the backplane
reports `sealed` instead of "unreachable". The StatefulSet uses
`podManagementPolicy: Parallel`: with the default `OrderedReady`, Kubernetes
waits until the pod is ready before it rolls out a change, so a locked pod
would keep the old version and be replaced, locked again, right after
someone unlocks it. Keep the line: the field cannot be changed after the
first install.

**Unseal after a restart (#4046).** The `unseal` container (same image,
same security settings, `coreVault.unseal.resources`) checks `bao status`
every 5 seconds. When the server is sealed and `/unseal/key` exists, it runs
`tr -d '[:space:]' </unseal/key | bao write sys/unseal key=-` against
`127.0.0.1:8200`. The key file is the Secret `<release>-core-vault-unseal`,
mounted only in this container, `optional: true` and without `subPath`.
The Secret's `key` value is the key alone. White space around it (such as
the line break `echo` adds) is dropped, so the setup Job (#4044 part 2),
the installers and an operator restoring by hand can write it either way;
without the `tr`, a key with a line break never unseals.

- The mode is whether the Secret exists; there is no mode value. Secret
  present: self-unseal. Secret absent: "keyholder at boot", the pod stays
  not ready and `/ready` says `vault: sealed`.
- The key goes from the file through a pipe into `bao` on every pass, and
  the call's output is thrown away. The key is never on a command line, in a shell variable or
  in a log, and nothing keeps it: a deleted Secret takes effect on the next
  pass once the kubelet removes the file (about one to two minutes on a
  running pod).
- The log has fixed lines only, printed when the state changes
  (`unseal: unsealed`, `unseal: sealed, no unseal key in /unseal ...`).
- No RBAC, no CronJob. NOTES print the `kubectl get secret` command for the
  key, never the key.
- Switching modes, the trade-off and recovery mode: the operator page
  `docs-site/install/openbao-unseal-key.md`.
- Cluster check: `scripts/ci/check-openbao-unseal.sh <namespace> <release>`
  (pod restart, keyholder mode, server process restart without the Secret,
  and a search for the key in pod logs and `helm get`). Checked on RKE2
  v1.37.1 with the CIS profile, after install and after an upgrade.

**Audit.** OpenBao refuses audit devices made through the API, so the
device is in the config file. The lines go to the container log, which the
kubelet rotates; they count against the container's `ephemeral-storage`.

**A Vault image (tests only).** `coreVault.binary: vault` with a matching
`image` switches the program name, the variables (`VAULT_*`), the paths
(`/vault/...`) and the config: `disable_mlock = true` is written, and the
audit device is left out (the setup Job turns it on through the API).

**`authDelegator`.** A Kubernetes login method checks tokens through the
cluster's TokenReview API, which needs the cluster-wide binding to
`system:auth-delegator`. MEHO does not need it: a test on RKE2 with the CIS
profile proved a login for the setup Job that checks tokens with the
cluster's public keys, with namespace rights only.
The option gives OpenBao its own service account, so the namespace's
`default` account does not get the right.

**Security.** User 100 and group 1000 (the image's `openbao` user), with
`fsGroup` 1000 for the disk; `global.openshift: true` removes all three
through `meho.core.podSecurityContext`. Read-only root filesystem, no
added capabilities, no service-account token (unless `authDelegator`),
`enableServiceLinks: false`. Resources: request 100m / 256Mi, limit
500m / 512Mi. Checked on RKE2 v1.37.1 with the CIS profile and namespace
rights only: the pod starts under the strict pod rules, reaches ready after
a setup and unlock by hand, keeps its data across a pod restart, and the
network rule blocks other pods.

### Metrics scrape wiring (`templates/servicemonitor.yaml`, `templates/prometheusrule.yaml`)

The backplane serves Prometheus exposition at an unauthenticated
`/metrics` route (`backend/src/meho_backplane/metrics.py`, legacy
`0.0.4` content type for universal scraper support), but nothing scrapes
it out of the box. Two **optional, disabled-by-default** Prometheus
Operator resources close that gap (Initiative #2884, #2885):

- `serviceMonitor` (`serviceMonitor.enabled`, default `false`) renders a
  `ServiceMonitor` (`monitoring.coreos.com/v1`) that selects **this
  release's** backplane `Service` by its `app.kubernetes.io/name` +
  `app.kubernetes.io/instance` labels and scrapes the named
  `service.portName` port at `serviceMonitor.path` (default `/metrics`).
  The broadcast subchart's Service carries a different
  `app.kubernetes.io/name`, so it is not matched.
- `prometheusRule` (`prometheusRule.enabled`, default `false`) renders a
  starter `PrometheusRule` with four conservative alerts:
  - `MehoMetricsScrapeAbsent` — `absent(up{job=<fullname>, namespace=<ns>})`;
    fires when Prometheus has **no** target series for this release at
    all (the ServiceMonitor was never picked up, the selector drifted, or
    the Service was removed). A known-but-failing target surfaces as
    `up == 0`, a separate signal the starter set does not cover.
  - `MehoBroadcastPublishErrors` — `rate(broadcast_publish_errors_total[5m]) > 0`;
    the fail-open broadcast publisher swallows dropped events by design
    (`broadcast/publisher.py`), so a sustained nonzero rate is the only
    signal that the activity feed is silently losing events.
  - `MehoBackgroundLoopStalled` (#2888) —
    `(time() - background_loop_last_tick_timestamp_seconds) > (N * background_loop_interval_seconds)`;
    every one of the 13 lifespan background loops stamps
    `background_loop_last_tick_timestamp_seconds{loop}` on each completed
    tick (via `metrics.note_loop_tick`, called at the end of every loop
    body incl. no-op/heartbeat ticks) and re-publishes its cadence as
    `background_loop_interval_seconds{loop}`. That companion gauge lets one
    rule threshold each loop at `N x its own interval`
    (`N = prometheusRule.loopLiveness.missedTicks`) — matched element-wise
    on the `loop` label — without the chart enumerating loop names or baking
    in intervals that would drift from settings. A wedged or dead loop stops
    advancing its stamp while healthy loops move on; the stamp is
    per-process (scraped per-pod), so a single stalled replica fires without
    healthy siblings masking it. Complements — does not replace — the
    sensor-runner watchdog's faster in-process broadcast stall detection
    (`checks/watchdog.py`), which is unchanged.
  - `MehoDocsScopedZeroHits` (#3915) —
    `sum(increase(docs_search_scoped_zero_hits_total{job=<fullname>, namespace=<ns>}[W])) >= T`;
    the docs search service counts every search that requested a `product`
    or `version` and returned no chunks (`docs_search/call_log.py`),
    whatever the collection's scope gates (#3912) did with them. Where they
    went out as hard filters (`scope_forwarded="filters"`), a run of them
    means the filter vocabulary callers send does not match the corpus's;
    with `"soft"` or `"none"` the backend did not filter on them. The
    counter has no labels (`/metrics` can be unauthenticated); the matching
    `docs_search_scoped_zero_hits` warning log line names the collection,
    product, version and `scope_forwarded`
    (`docs/codebase/docs-search.md` § Per-call logs). `T` and `W` are
    `prometheusRule.docsScopedZeroHits.threshold` (default 5) and `.window`
    (default `1h`). The counter counts backend searches, not calls: a local
    `ask_docs` runs one search per expansion variant (up to 4), so one
    scoped ask that finds nothing counts up to 4, and two such asks in the
    window reach the default threshold. A `search_docs` call counts once;
    an upstream ask (#3911) runs no search and never counts.

  The rules are split into groups **by concern** (`meho.scrape`,
  `meho.broadcast`, `meho.loops`, `meho.docs`) so follow-up tasks extend
  them cleanly — a new alert lands as a new group, not an edit to an
  existing group.

Both resources require the Prometheus Operator CRDs
(`monitoring.coreos.com/v1`) to be installed in the cluster; a default
install renders neither and is unaffected. **The `labels` knob on each is
the discovery contract, not cosmetic**: a Prometheus custom resource only
picks up ServiceMonitors / PrometheusRules whose labels match its
`serviceMonitorSelector` / `ruleSelector`. On a kube-prometheus-stack
install that selector defaults to `release: <prometheus-release>`, so
`serviceMonitor.labels` / `prometheusRule.labels` must carry
`release: kube-prometheus-stack` (or the site's equivalent) or the scrape
config / rules are silently never generated. The alert `for` durations
and `severity` labels are operator-tunable under
`prometheusRule.scrapeAbsent` / `prometheusRule.broadcastPublishErrors` /
`prometheusRule.loopLiveness` / `prometheusRule.docsScopedZeroHits` —
`loopLiveness` also exposes `missedTicks`, the per-loop staleness
multiplier `N`, and `docsScopedZeroHits` its `threshold` and `window`.

Render assertions live in `backend/tests/test_chart_observability_scrape.py`
(unit layer, skips where `helm` is absent) and in the `chart.yml`
`validate` job (the authoritative CI gate: renders with both opt-ins on
and greps the load-bearing fields, and re-checks the default render
carries neither resource).

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
| `vault.address` | Per-environment Vault endpoint. Required only when `config.credentialBackend: vault` (the default) — a `gsm` install leaves it blank (#2231) |
| `keycloak.issuer` | Per-environment Keycloak issuer URL. Not set with the built-in Keycloak (`coreKeycloak.enabled`): the chart derives the issuer and the render fails if one is set |
| `config.keycloakIssuerUrl` / `config.keycloakAudience` / `config.vaultAddr` | ConfigMap env-var mirrors of the above (`backend/src/meho_backplane/settings.py` contract). `config.vaultAddr` is required-when-`credentialBackend: vault`, like `vault.address` |
| `config.backplaneUrl` / `config.mcpResourceUri` | G0.8-T4 (#633). Blank by design: for the common ingress-fronted deploy the chart derives `BACKPLANE_URL=https://<ingress.host>` (scheme follows `ingress.tls.enabled`) and `MCP_RESOURCE_URI=${BACKPLANE_URL}/mcp` via the `meho.backplaneUrl` / `meho.mcpResourceUri` helpers, so the `/mcp` audience resolves without operator action. Set explicitly only when the public URL differs from the Ingress host, or for a non-default MCP mount. When neither resolves (no ingress / empty host, nothing set) the chart `fail`s at `helm template` / `helm install` time with an actionable message naming `config.backplaneUrl` / `config.mcpResourceUri` / `ingress.host` (#2394, in `templates/configmap.yaml`) instead of rendering an empty audience and letting the pod crash-loop at startup on `audience_not_configured` (`_assert_mcp_resource_uri_configured` in `main.py`, still the runtime backstop). There is no `allowNoMcpResourceUri` escape hatch — for a deliberate MCP-less bring-up before ingress/DNS exists, set a placeholder `config.backplaneUrl`; `/mcp` stays per-request fail-closed regardless. The operator must still add a matching Keycloak `oidc-audience-mapper` — see `docs/cross-repo/mcp-client-setup.md` Step 1 |
| `networkPolicy.{postgres,vault,keycloak}CIDR` | Per-environment subnet for each upstream. Required only when `networkPolicy.enabled: true` (the default) — relaxed when networkPolicy is disabled. `keycloakCIDR` is not used with the built-in Keycloak |

A blank field falls into the typed-schema contract immediately — `helm
install` fails before a single Kubernetes resource is created. The
operator sees the exact missing path (e.g. `at '/vault/address':
minLength: got 0, want 1`) and a single targeted override fixes it.

Conservative resource defaults (`requests: {cpu: 100m, memory: 256Mi}`,
`limits: {cpu: 1000m, memory: 1Gi}`) reflect observed steady-state usage
of the v0.1 chassis (authn/authz traffic + synchronous audit-write fanout);
tune limits up for higher-throughput deployments.

### Full values reference

The complete operator-facing values surface. These two tables are the
authoritative reference (the README links here rather than duplicating
them).

**Operator-required** (MUST be set; the schema rejects empty defaults):

| Path | Type | Notes |
| --- | --- | --- |
| `image.tag` | string | Immutable tag (`sha-<git-sha>` or `v<x.y.z>`); never `:latest`. |
| `image.digest` | string | Optional `sha256:<64 hex>` digest pin (#284). When set, the Deployment + migration Job render `repository@digest` instead of `repository:tag`; empty (default) uses `image.tag`. |
| `ingress.host` | string (`hostname`) | External hostname the chart publishes. Required only when `ingress.enabled: true` (default); skipped when ingress is disabled. |
| `ingress.tls.secretName` | string | TLS Secret (cert-manager-managed or pre-provisioned). Required only when both `ingress.enabled` and `ingress.tls.enabled` are true. |
| `postgres.credentialsSecret` | string | Kubernetes Secret holding `DATABASE_URL` at key `url`. |
| `vault.address` | string (`uri`) | Vault endpoint, e.g. `https://vault.example.org`. Required only when `config.credentialBackend: vault` (the default); a `gsm` install leaves it blank (#2231). |
| `keycloak.issuer` | string (`uri`) | Keycloak issuer URL (used for `iss` validation + JWKS discovery). Must stay empty with the built-in Keycloak, which sets it. |
| `config.keycloakIssuerUrl` | string | ConfigMap mirror of the above; consumed by the backplane env. Must stay empty with the built-in Keycloak. |
| `config.keycloakAudience` | string | Keycloak client ID fronting the backplane. Not needed with the built-in Keycloak (`meho-backplane`). |
| `config.vaultAddr` | string (`uri`) | ConfigMap mirror of `vault.address`. Required-when-`credentialBackend: vault`, like `vault.address`. |
| `config.credentialBackend` | enum `vault` \| `gsm` | Credential backend a schemeless target `secret_ref` and the `/api/v1/health` federation proof resolve through (#2227). Default `vault` (rendered `CREDENTIAL_BACKEND`). `vault` requires `vault.address` + `config.vaultAddr`; `gsm` requires `gsm.enabled: true` + `gsm.project` (root-level `allOf` conditional). |
| `gsm.enabled` / `gsm.project` | boolean / string | GSM credential backend (#2227). Required (`enabled: true` + non-empty `project`) only when `config.credentialBackend: gsm`; inert on a Vault install. Runtime values the backplane reads are `config.gsmProject` (`GSM_PROJECT`) + optional `config.gsmImpersonateSa` (`GSM_IMPERSONATE_SA`). The `gsm.workloadIdentityFederation.*` keys render as `GSM_WIF_*` (#2642 — inert stubs before that); a non-empty `audience` switches credential reads onto the calling operator's identity, which makes *background* dispatch (Sensors, the topology-refresh scheduler, runbook verify dispatch — the callers that dispatch with no operator JWT at all) need an identity of its own — an ambient pod identity or `checkRunner.*`. Connector `probe()` / `fingerprint()` are **not** covered by either: they carry a non-empty placeholder JWT, so they take the WIF branch and fail the exchange regardless. See `docs/deploying.md` § GSM / Vault-free, `deploy/values-examples/values-gsm-example.yaml` (SA-direct), and `deploy/values-examples/values-gsm-wif-example.yaml` (per-operator WIF). |
| `checkRunner.enabled` / `.clientId` / `.clientSecret.*` | boolean / string / object | In-process check-runner service principal (#2642). `enabled: true` requires a non-empty `clientId` + `clientSecret.secretName`, and wires `CHECK_RUNNER_CLIENT_ID` (plain env) + `CHECK_RUNNER_CLIENT_SECRET` (`secretKeyRef` only). Default `false` renders nothing: background dispatch carries no bearer token and a Sensor whose target needs an operator-context credential read evaluates `unknown`. |
| `networkPolicy.postgresCIDR` | CIDR (IPv4) | Egress CIDR; pattern-validated. Required only when `networkPolicy.enabled: true` (default). |
| `networkPolicy.vaultCIDR` | CIDR (IPv4) | Same. |
| `networkPolicy.keycloakCIDR` | CIDR (IPv4) | Same, but not used (and not required) with the built-in Keycloak, which the backplane reaches by podSelector. |

**Common operator overrides** (safe defaults provided; tune as needed):

| Path | Default | Notes |
| --- | --- | --- |
| `replicaCount` | `1` | Single-replica baseline. |
| `image.repository` | `ghcr.io/evoila/meho` | OCI repo from the image pipeline. |
| `image.pullPolicy` | `IfNotPresent` | `Always` \| `IfNotPresent` \| `Never`. |
| `service.type` / `service.port` | `ClusterIP` / `8000` | Service shape. |
| `ingress.className` | `""` | Cluster default IngressClass when empty. |
| `probes.liveness.*` / `probes.readiness.*` | `/healthz` / `/ready` httpGet + tuned timings | Operator-tunable; never disabled. |
| `probes.startup.*` | `/healthz` httpGet + 5-min first-boot budget (#2393) | Operator-tunable; defaulted-on, opt-out by clearing `probes.startup`. Gates liveness/readiness through catalog registration + fastembed preload. |
| `resources.requests` / `resources.limits` | `100m`/`256Mi` / `1000m`/`1Gi` | Conservative chassis baselines. |
| `networkPolicy.ingressControllerNamespace` | `ingress-nginx` | RKE2 default; override per cluster. |
| `audit.postgresOnly` | `true` | Postgres-only audit sink baseline. |
| `broadcast.enabled` | `true` | Deploys the bundled Valkey broadcast subchart. |
| `corePostgres.enabled` / `coreKeycloak.enabled` | `false` | Built-in core parts (#4041). When on, the core secrets Job makes the missing `<release>-core-*` Secrets; see [core-secrets.md](core-secrets.md). |
| `corePostgres.existingSecrets.{superuser,keycloak}` / `coreKeycloak.existingSecrets.bootstrap` | `""` | Your own Secret instead of a made one; the Job only checks its keys. |
| `coreKeycloak.enabled` | `false` | Also deploys the built-in Keycloak subchart (#4040). When true it needs `corePostgres.enabled: true`, `coreKeycloak.hostname` (equal to `ingress.host`) and `coreKeycloak.firstTenantId` (a UUID). See [Built-in Keycloak subchart](#built-in-keycloak-subchart-chartskeycloak). |
| `connectors.enabled` | `[]` | Opt-in list; pick from the shipped connector catalog (see [`docs/architecture/connectors.md`](../architecture/connectors.md) — VMware/VCF, NSX, Kubernetes, Vault, Harbor, Keycloak, ArgoCD, GCloud, BIND9, pfSense, and more). |
| `config.ingestJobTimeoutSeconds` | `""` | Async ingest-job watchdog budget override, in seconds (#2318, hardens #2275). Empty (default) omits `INGEST_JOB_TIMEOUT_SECONDS` so the backend's built-in 30-min ceiling applies; set a positive number to raise it for a slow shared executor / large spec fleet. Set via a values file or `--set-string` (bare `--set` coerces to a number and fails the `type: string` schema — as with every `*Seconds` knob here). Non-finite (`inf`/`nan`), non-positive, or malformed values are rejected at the backend (warn + fall back to 30 min), so the watchdog can never be disabled. |

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
`evoila-bosnia/claude-rdc-hetzner-dc`'s
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

> For the operator-facing deployment & upgrade guide (cold-install
> prerequisites, Helm-4 SSA upgrade caveats, credential-backend variants),
> see [`../deploying.md`](../deploying.md). This section covers the chart
> mechanics.

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
`.github/workflows/chart.yml`. The workflow targets `meho-runners-ci` (the
project's self-hosted runner pool on the dedicated rke2-ci cluster,
introduced by #160 + #167 on rke2-meho and migrated to rke2-ci via
claude-rdc-hetzner-dc#610 / #715) and shares the
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

### Namespace-only install check (#4099)

The chart must install with the rights of a namespace admin (install
form 3). Two checks keep it that way:

- `validate` renders the chart with the default values and with every file
  in `deploy/values-examples/`, and pipes each render into
  `scripts/ci/check_chart_namespace_scope.py`. It fails on any object whose
  kind is not in the script's `NAMESPACED_KINDS` allow-list, and on any
  object in another namespace. A new namespaced kind is added to that list.
- `helm-test` binds the user `meho-installer` to the built-in `admin` role
  in `meho-test` only, then runs `helm install` and `helm test` with
  `--kube-as-user meho-installer`.

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

## PR-level CI (`.github/workflows/ci.yml`)

`ci.yml` is the central per-PR test harness. Every PR targeting `main`
runs the toolchain jobs in parallel and every push to `main` re-runs the
same matrix as a regression catch. Branch protection consumes the
required jobs' status per
`branches/main/protection.required_status_checks.contexts` —
re-verified after the 2026-05-20 #698 promotion of the integration
lane, the structural corrective to the v0.2 / G3.4 green-but-hollow
incidents #634 / #697.

Since #3251 the required unit lane is **sharded**: the required context
`Python (ruff + mypy + pytest)` no longer maps to one monolithic job but
to a **fan-in** (`python-unit-lane`) over a lint job (`python-lint`) and a
4-way pytest matrix (`python-unit-shard`). The required-context
_string_ is unchanged, so branch protection / the ruleset are untouched —
see [Sharded unit lane (#3251)](#sharded-unit-lane-3251) below.

### Merge queue (#769, #3253)

Every workflow that owns a required status check triggers on
`merge_group` in addition to `pull_request` and `push`: `ci.yml`,
`security-scan.yml`, `secret-scan.yml`, `dependency-license-check.yml`
(all landed with #769), `chart.yml` (closed by #3253) and
`migration-heads.yml` (#4204, see `docs/codebase/migrations.md`).
The `merge_group` event fires when a PR is admitted to the GitHub merge
queue and runs the full check matrix against the **synthesised merge
commit** — PR head + current `main` tip + any PRs ahead in the queue. A
merge that would break `main` fails in the queue and never reaches
`main`, ending the inherited-red episodes from 2026-05-20/21 where
cancelled post-merge CI allowed broken combinations to land silently.

**Why the queue is worth enabling — two distinct wins:**

- *Inherited-red race.* Without the queue, two independently-green PRs
  can merge in sequence and produce a broken `main` (each was tested
  against an older tip). The queue tests the **actual** merge result of
  each admission, so a semantically-broken combination is caught before
  it lands.
- *Changelog-churn / full-relap cost.* Today every force-push to a PR —
  including the mechanical rebases that resolve `[Unreleased]`
  CHANGELOG-union conflicts when a sibling PR merges first — reruns the
  **entire** check matrix on that PR. On a busy day the same PR pays for
  many full laps it did not semantically change. The queue replaces that
  with a single batched rebase-test-merge against the queued tip: one
  authoritative lap per batch instead of N redundant laps per PR. This
  is the code-side lever the lab-capacity Initiative
  (claude-rdc-hetzner-dc#2777) leans on.

**Required-check → workflow map** (branch protection on `main`,
`branches/main/protection.required_status_checks.contexts`, 11
contexts). Enabling the queue means every one of these must report on
`merge_group` refs — a required check that never reports on a queue ref
hangs the queue forever:

| Required context | Owning workflow | Reports on `merge_group`? |
| --- | --- | --- |
| `Python (ruff + mypy + pytest)` | `ci.yml` | yes (#769) |
| `Python (integration testcontainers)` | `ci.yml` | yes (#769) |
| `Python (database migrations)` | `ci.yml` | yes (#769) — `changes` job emits `true` on non-PR events |
| `Go (golangci-lint + go test)` | `ci.yml` | yes (#769) |
| `Helm (lint + template + kubeconform)` | `ci.yml` | yes (#769) |
| `Semgrep SAST` | `security-scan.yml` | yes (#769) |
| `TruffleHog Secret Scan` | `secret-scan.yml` | yes (#769 trigger; #3307 scan-range fix) — see caveat below |
| `Python License Check` | `dependency-license-check.yml` | yes — `changes` + job-level `if:` |
| `NPM License Check` | `dependency-license-check.yml` | yes — same shape |
| `helm install + helm test (pgvector preflight)` | `chart.yml` | **yes (#3253)** — the `helm-test-gate` aggregator (`if: always()`) is the required context and fails closed |
| `Migration chain (single head)` | `migration-heads.yml` | yes (#4204) — no path filter; when a migration lands on `main`, the push run also posts a fresh check run under this name on every open PR that adds a migration (`scripts/ci/recheck_migration_prs.py`). **Operator registers the context after #4204 lands.** |
| `DCO` | **external GitHub App** (`app_id 1861`), not a workflow | **operator must verify** — see caveat below |

Two shapes are load-bearing for queue compatibility and appear across
these workflows:

- **No `on.*.paths` filter on a required-check workflow.** A workflow
  skipped by a path filter leaves its required context stuck at
  "Expected — waiting for status" and blocks the merge (the #2140
  skipped-vs-pending trap). Instead each conditional workflow always
  triggers, a lightweight `changes` job detects relevance, and the
  expensive jobs are gated by a job-level `if:` — a job skipped via `if:`
  reports **Success** and satisfies the required check. `chart.yml` adds
  one more layer: an `always()` `helm-test-gate` aggregator carries the
  required-context *name* and explicitly re-checks the upstream job
  results so a skipped-because-a-dependency-failed run cannot masquerade
  as green.
- **Concurrency scoped so a queue run is never cancelled.** Each
  workflow's `concurrency` group is suffixed with
  `${{ github.event_name }}` and sets
  `cancel-in-progress: ${{ github.event_name != 'merge_group' }}`. A
  cancelled queue check fails the merge attempt and drops the PR out of
  the queue, so `merge_group` runs are never cancelled; `pull_request` /
  `push` runs still cancel their own predecessors as before.
- **Third-party actions that derive their own scan range must be told
  the `merge_group` range explicitly.** A `merge_group` trigger makes the
  workflow *run*, but an action that computes its diff from the event
  payload can still misfire: the TruffleHog action's dispatch handles
  only `push` / `pull_request` / `workflow_dispatch` / `schedule`, so on a
  `merge_group` event its `BASE`/`HEAD` stay empty and it falls back to a
  full-filesystem scan of the whole tree — which, with
  `--results=verified,unknown`, trips `--fail` on pre-existing unverified
  fixture findings and ejected the first real queue entry `failed_checks`
  (#3306 → #3307). The fix passes `base: ${{ github.event.merge_group.base_sha }}`
  / `head: ${{ github.event.merge_group.head_sha }}` so the queue ref is
  scanned as a `base..head` diff; both expressions are empty off the
  queue, preserving PR/push behaviour. When adding any range-deriving
  action to a required-check workflow, check its `merge_group` handling —
  the trigger alone is not enough.
- **The TruffleHog scanner container image is digest-pinned (#284 /
  F15b).** The action shells out to `docker run "${IMAGE}:${VERSION}"`,
  defaulting `VERSION` to the mutable `:latest` tag — so the required
  secret-scan gate would otherwise run whatever `:latest` resolves to at
  pull time on the self-hosted `meho-runners-ci` pool. `secret-scan.yml`
  now sets a job-level `TRUFFLEHOG_VERSION: <ver>@sha256:<digest>` and
  feeds it to both the warm-cache pre-pull and the action's `version`
  input, so both reference the same digest-pinned image. The action's
  `uses:` SHA is bumped weekly by Dependabot (github-actions ecosystem);
  Dependabot cannot resolve a digest held in a `with:`/`env:` value, so
  the pinned scanner digest is bumped in lockstep in the same PR (the job
  `env:` comment carries the `imagetools inspect` command that resolves
  the new digest).

`chart.yml` additionally guards its `publish` and `verify-anonymous-pull`
jobs to `github.event_name == 'push'` (was `!= 'pull_request'`), so a
queue admission validates the merge result at full depth **without**
packaging or pushing a chart to GHCR — the merge has not happened yet.

**Enabling the queue is an operator action — a session never flips it.**
Repo-settings / ruleset mutations are outside a session's remit (the
park note on #769; restated in #3253). The code side is now complete;
the operator performs:

1. **Add a `merge_queue` rule to the `protect main` ruleset**
   (id `14556458`). UI path: *Settings → Rules → Rulesets → protect
   main → Add rule → Require merge queue*. API path — fetch the ruleset,
   append the rule, and PUT it back (the endpoint replaces the whole
   `rules` array, so a blind PUT would drop the existing
   `pull_request` / `deletion` / `non_fast_forward` rules):

   ```bash
   gh api repos/evoila/meho/rulesets/14556458 > ruleset.json
   # add {"type":"merge_queue","parameters":{ ...grouping/merge_method... }}
   # to .rules, then:
   gh api -X PUT repos/evoila/meho/rulesets/14556458 --input ruleset.json
   ```

2. **Verify the queue enforces the same 11 required contexts.** The
   required checks currently live in *classic* branch protection
   (`branches/main/protection`), not in the ruleset. Confirm the merge
   queue waits on the same set against the merge ref — mirror the
   contexts into a `required_status_checks` rule on the ruleset if the
   queue does not pick up the classic set.

3. **Verify the `DCO` context reports on `merge_group` refs before
   enabling.** DCO is provided by an external GitHub App, not a workflow
   in this repo — nothing in #3253 can make it report on queue refs. If
   the DCO App does not post a status on the synthesised merge commit,
   the required `DCO` context will hang the queue. Confirm on a canary
   queue entry, or remove `DCO` from the queue's required set and keep it
   as a PR-only gate, before turning the queue on for everyone.

4. **DCO / `require_last_push_approval` /
   `require_extra_approval_for_unattributed_changes` interplay.** The
   `protect main` ruleset's `pull_request` rule has both approval
   ratchets on. The merge queue rebases the PR onto the queued tip, which
   can re-touch attribution; verify a queued PR still satisfies these on
   the merge ref, or document the admin-merge fallback. (This is the
   #769 AC item that was never closed out.)

No repo setting is changed by the session that lands #3253 — only
workflow files, this doc, and the changelog.

### Matrix

| Job | Surface | Steps |
| --- | --- | --- |
| `python-lint` (`Python lint + typecheck (ruff + mypy)`) | `backend/src/` + `backend/tests/` | `uv sync --locked --all-groups` -> `ruff check` -> `ruff format --check` -> `mypy --strict`. Non-required; whole-tree, unshardable-by-file, runs once on a GitHub-hosted runner (#3251, #4626). |
| `python-unit-shard` (`Python unit shard N/4`) | `backend/` unit + acceptance subtree, split 4 ways | `pytest -n 3 --dist loadscope --maxfail=1 --timeout=180 --timeout-method=signal` over a deterministic file set split by measured time (excludes `tests/integration/` and `tests/migrations/`; `-n 3` per shard for CPU/memory headroom — see [runner-CPU rule](#runner-cpu-rule)). Non-required matrix (#3251). |
| `python-unit-lane` (`Python (ruff + mypy + pytest)`) | fan-in | `needs: [changes, python-lint, python-unit-shard]`; **required check** — fails iff lint or any shard failed (or the detector is not green). Carries the exact branch-protection required context so the ruleset is untouched (#3251); reports success-by-skip on a docs-only diff (#3252). |
| `python-integration` (`Python (integration testcontainers)`) | `backend/tests/integration/` | `uv sync --locked --all-groups` -> `pytest tests/integration/` against pgvector / valkey / k3d / vcsim / vault testcontainers via DinD. **Required merge gate (#698)** so the lane that exercises real connector dispatch can no longer ship red. |
| `go-lint-test` (`Go (golangci-lint + go test)`) | `cli/` | `golangci-lint` (v6 action) -> `go build ./...` -> `go test -race -cover ./...` |
| `helm-lint-template` (`Helm (lint + template + kubeconform)`) | `deploy/charts/meho/` | `helm lint` -> `helm template` -> `kubeconform --strict --kubernetes-version 1.28.0` |

The four `python-unit-shard` legs run on `meho-runners-ci-heavy`
(dedicated ARC scale set, 6000m requests=limits, max 5 pods — #761 /
rdc-gitops#55; four shards fit the 5-pod ceiling). `python-integration`,
`python-migration-tests` and the nightly `python-coverage-combine` run
on the dense `meho-runners-ci` pool (4-core): they pull their test images
from the internal image mirror or read the private vendor spec shelf.

Since #4626 (step 1), the six small jobs that need nothing from the
internal network run on GitHub-hosted `ubuntu-latest` runners:

| Job | Runs on | Why it can leave the internal pool |
| --- | --- | --- |
| `changes` | `ubuntu-latest` | one `git diff`, 12 s of work |
| `python-lint` | `ubuntu-latest` | `uv sync` from PyPI + ruff + mypy |
| `python-unit-lane` | `ubuntu-latest` | one bash step over `needs.*.result` |
| `go-lint-test` | `ubuntu-latest` | Go toolchain + public egress; gcc and make are preinstalled, so the "install if missing" steps are no-ops |
| `cli-api-snapshot-freshness` | `ubuntu-latest` | Go + uv + `make`; regenerates committed artifacts offline |
| `helm-lint-template` | `ubuntu-latest` | helm + kubeconform from public releases |

Before the move, every one of these jobs waited a median of about 14
minutes for a free slot on the 8-slot internal pool to run for seconds to
two minutes, and the detect job at the start and the fan-in at the end of
every run put that wait on the critical path twice. Each moved job keeps
its name, its `if:` guard and its triggers, so the required checks are
unchanged. GitHub-hosted runners are free for public repositories; the
organisation shares a 20-concurrent-job limit across all its repositories
on the current plan, so a job on a hosted runner can still wait when many
hosted jobs run at once — measure the hosted wait before moving a long
job such as the image build (#4626, step 3).

Each `python-unit-shard` carries a 20-minute
`timeout-minutes` (hang backstop only — the expected shard job wall is
~8 min: ~2 min setup + ~6 min pytest, down from the pre-#3251
monolithic lane's ~15-18 min serial pytest wall); a per-shard
perf-budget-guard step (`budget_s=900`; 660 s until 2026-10-24, scaled
from the old 1320 s whole-suite budget) is the PR-level early-warning
gate that fires well below the cap. `python-lint` and the fan-in carry
10 and 5 minutes.
`go-lint-test` and `helm-lint-template` carry 10 minutes;
`python-integration` carries 60 minutes for the container-pull + DinD
spin-up + testcontainers sweep (xdist loadgroup parallelisation tracked
in #564). Wall-clock for a green PR is the slowest job's elapsed time
because the jobs never block each other — since #3251 sharded the unit
lane, `python-integration` is typically the dominant lane and the
dispatch surface for the Goal #11 budget conversation.

### Sharded unit lane (#3251)

The required unit lane was a single `python-lint-test` job running ruff +
ruff format + mypy + the whole ~13k-test unit sweep serially (~19-min
wall) — the biggest single contributor to PR wall-time. #3251 split it
into three cooperating jobs, mirroring the #3172/#3177 coverage-sweep
shape (deterministic file-level matrix + fan-in):

- **`python-lint`** — ruff check + ruff format --check + mypy. Whole-tree
  checks that cannot be split by test file and cost ~1-2 min, so they run
  **once** on the light pool rather than N times per shard. Only pytest
  fans out.
- **`python-unit-shard`** — the pytest sweep as a **4-way matrix split by
  measured file time**. Each shard lists the test files with `find tests
  -name 'test_*.py' -not -path 'tests/integration/*' -not -path
  'tests/migrations/*' | sort` and pipes the list into
  `tests/_unit_shard_split.py --shard N --total 4`. The splitter reads the
  measured seconds per test file from `tests/_unit_shard_durations.json`,
  sorts the files slowest first, and gives each file to the shard with
  the least time so far (a file without a measurement counts as the
  median; every file also carries a small fixed cost for import and
  collection, so files whose tests all skip still spread out). The same
  list always gives the same split, and the four shards partition the
  file set **exactly** (union == full, pairwise disjoint;
  `tests/test_unit_shard_split.py` pins both). The splitter prints the
  file count and the predicted seconds per shard on stderr, so the shard
  log shows when the measurements drift from the real wall. Each shard
  writes its per-test times with `-o junit_family=xunit1 --junitxml` and
  uploads them as the artifact `python-unit-shard-junit-<N>`. To refresh
  the measurements, download the four artifacts of one green run and
  pass them to `tests/_unit_shard_split.py --measure`; the module
  docstring has the exact commands. Take the numbers from CI, not from a
  laptop: tests that need Docker or the spec shelf skip locally but run
  in CI, and a local measurement put all of them into one shard (584 s
  against 443 s for the lightest). #3251 started with 3 shards and gave
  every N-th file of
  the sorted list to one shard. #4655 raised the count to 4 when the
  slowest shard reached the 660 s per-shard budget (579 s as a middle
  value and up to 605 s on `main`; 663 s on a pull request). #4809
  replaced the every-N-th-file rule with the measured split when shard 4
  ran 613 s as a middle value against 465 s for shard 2, and the merge
  queue dropped pull requests whose tests had all passed. The budget
  stayed at 660 s: the rule in `ci.yml` is to measure, then rebalance or
  add shards, not to raise the budget. On 2026-10-24 the budget went to
  900 s. From the measured split until that day, the slowest good shard
  took 625 s, even when every heavy runner was busy (2 jobs per node).
  That afternoon every heavy runner was busy again. Four shards that
  passed every test took 674-710 s, and the merge queue dropped good pull
  requests. With few runners busy, the four shards of one run took
  448-515 s. So the extra time came from the busy runners, not from
  slower tests. To change the count, edit the
  matrix and `TOTAL` in the pytest step together. File-level sharding is
  strictly coarser than what xdist `--dist loadscope` already does across
  workers (whole modules stay together, conftest semantics identical to a
  full run), so an xdist-safe suite is shard-safe — proven live by the
  `python-coverage` sweep, which runs a file-level split (every N-th
  file) over a superset of these files. `fail-fast:
  false` so one red shard does not cancel the others; each shard keeps
  `--maxfail=1` for fast per-shard feedback. Each shard also arms a
  per-test hang guard, `--timeout=180 --timeout-method=signal` (#4270):
  a test that stops making progress fails on its own after 180 s with
  its name and a stack dump of every thread, instead of sitting silent
  until the 20-minute job cap (the shards print one dot per test, so a
  hang left no test name in the log). `signal`, not `thread`, because
  the thread method calls `os._exit()`, which xdist reads as a worker
  crash. The `tests/integration` /
  `tests/migrations` exclusions and the `MEHO_SKIP_SPEC_INGEST_TESTS` G0.7
  canary opt-out (#2980) are preserved on **every** shard. Every shard also
  installs Helm v3.16.4 (the same pinned `azure/setup-helm` step as
  `chart.yml`) and sets `MEHO_REQUIRE_HELM=1`, so the `tests/test_chart_*.py`
  render tests really run there and fail, not skip, if helm is missing.
- **`python-unit-lane`** — the **fan-in** carrying the exact
  branch-protection required context `Python (ruff + mypy + pytest)`. It
  `needs: [changes, python-lint, python-unit-shard]` and, via `if: always()
  && <fork guard>`, runs regardless of upstream results, then inspects
  `needs.*.result`. `always()` is load-bearing: a plain `needs:` gate would
  be *skipped* when a shard fails, and a skipped required check counts as
  Success (#2140) — i.e. a red shard would let the PR merge. A hung shard
  hits its own `timeout-minutes` → `cancelled` leg → aggregate
  `cancelled`/`failure` → the gate FAILS (not open). On a fork PR the fork
  guard skips this job (and the shards), reporting the required context as
  Success exactly as the old single job did. Since #3252 the step first
  fails CLOSED if the `changes` detector did not itself succeed, then
  branches on `needs.changes.outputs.docs_only`: on the **docs-only fast
  path** it accepts the detect-driven `skipped` lint/shards as pass; on a
  **full run** it requires strict `success` (a `skipped` lint/shard there is
  anomalous and fails the gate). See [Docs-only fast path
  (#3252)](#docs-only-fast-path-3252).

Because the required context string is unchanged and stays on a job, the
**branch-protection ruleset and merge-queue required-check list need no
edit** — the required name simply moved from the monolithic job onto the
fan-in (the `python-coverage-combine` technique that kept quality-gate.yml
byte-identical). No escape-hatch label (the coverage lane's
`ci-coverage-validate`) is needed: the coverage lane runs once a night and
must be *forced* onto a PR, whereas the unit lane runs on every PR by default,
so a sharding PR self-validates through its own `pull_request` run.

### Docs-only fast path (#3252)

A PR whose diff is **purely documentation** cannot affect the Python
toolchain, yet it used to pay the full ~25-min tax of both heavy Python
lanes. The `changes` detector now classifies the diff and the heavy jobs
skip via their own `if:`, so a changelog roll / guide edit / README fix
completes CI in minutes.

- **Classification (allow-list, fail-closed).** In the `changes` job,
  `docs_only` starts `false` and flips to `true` **only** when *every*
  changed path matches the allow-list `^docs/` (any file under `docs/`) **or**
  `\.md$` (any Markdown file anywhere). Anything else — a `.py`, a workflow, a
  config, a `cli/` or `deploy/` file — sets `docs_only=false` and forces the
  full matrix. The diff is taken with `git diff --name-only --no-renames
  HEAD^1 HEAD`. On a PR, HEAD is the test merge (the PR on top of the newest
  `main`) and `HEAD^1` is that `main` commit, so the list holds exactly what
  the PR changes. The detector does not use `github.event.pull_request.base.sha`:
  that is `main` as of when the PR was opened, and once `main` moves a diff
  against it also lists other PRs' files, which cost docs-only PRs the fast
  path (#4657). `--no-renames` makes a rename appear as delete(old) +
  add(new) so a code→`*.md` (or `*.md`→code) rename can never smuggle a
  non-docs path past the allow-list. The step is written to always exit `0`
  and emit both outputs — an unresolvable diff or an empty diff keeps the
  fail-safe defaults (`docs_only=false`). `merge_group` and `push` NEVER
  fast-path (a queue batch may mix docs + code; a `main` push must always run
  the full matrix).
- **What skips.** `python-lint`, the four `python-unit-shard` legs, and
  `python-integration` gain `needs: changes` and an `if:` that skips them when
  `docs_only == 'true'` (same #2140 shape as the `python-migration-tests`
  gate). A job skipped by its own `if:` reports **Success**, so the required
  contexts `Python (ruff + mypy + pytest)` (via the fan-in) and
  `Python (integration testcontainers)` stay green without running.
- **What still runs.** `go-lint-test`, `helm-lint-template`, and the sibling
  workflows' required contexts (`Semgrep SAST`, `TruffleHog Secret Scan`,
  `Python License Check`, `NPM License Check`, `helm install + helm test`,
  `DCO`) are untouched — already fast, and docs can still leak a secret.
- **Why it cannot skip for code.** A required check that skipped on a *code*
  diff would let an untested change land green. Two independent guards prevent
  it: (1) the classifier is fail-safe, so a code path is never proven
  docs-only; (2) if the detector itself does not succeed, the
  `python-unit-lane` fan-in fails **CLOSED** (it asserts
  `needs.changes.result == 'success'` before trusting `docs_only`), which
  blocks a code PR even though `python-integration` would skip-cascade to
  green off the failed `needs`. The integration lane deliberately stays on the
  plain `needs:`+`if:` shape (no `always()`) to preserve cancel-in-progress on
  the heavy 60-min lane; its detect-crash exposure is identical to the
  pre-existing migration gate and is neutralised for code PRs by the unit
  lane's fail-closed guard above.

### Runner-CPU rule

**`pytest-xdist -n` must stay below the runner container's usable
cores**, leaving at least one core for the GHA agent + xdist
coordinator.

The `meho-runners-ci-heavy` pod is currently `requests=limits=6000m`
(QoS Guaranteed — no burst slack). With a hard 6-core cgroup quota the
kernel enforces the cap via CPU throttle: once `-n` workers + the xdist
coordinator + the GHA agent saturate the quota, the agent is
CPU-throttled, misses its heartbeat, and GitHub kills the job with
"runner lost communication" — even though the node is at ~0% CPU.
That is the root cause of the `~15-21 min` failures that `-n 6 → 4 → 3`
failed to fix: reducing `-n` only shifts the throttle timeline by a few
minutes against a hard cap.

**The correct fix is burst headroom on the pod's CPU _limit_**, not a
smaller `-n`. Target: request 4000m / limit 7000m (pending
`evoila-bosnia/rdc-gitops#70`; tracked here as #1983). Until that lands,
do not raise `-n` above the current value to chase wall-clock gains — the
heartbeat failures will return.

### Vendor vCenter spec-shelf in CI (#2949)

The five real-spec vCenter lanes (`test_g07_vsphere_canary.py`, the two
`tests/integration/test_operations_ingest_*` lanes,
`test_portgroup_audit_op_id_reconcile.py`,
`test_connectors_vmware_rest_composites_l2_ingest_reconcile.py`) validate
ingest + op_id reconciliation against the **real** Broadcom/VMware-licensed
`vcenter.yaml` / `vi-json.yaml`, which are deliberately not in this public
repo. `ci.yml` provisions them at runtime, gated on a secret:

- Both Python test jobs (`python-unit-shard` — every shard — and
  `python-integration`) carry a
  secret-gated "Checkout vendor vCenter spec-shelf" step: a cone-mode sparse
  checkout of `docs/vcenter-9.0/` from the private
  `evoila-bosnia/claude-rdc-hetzner-dc` shelf repo into
  `$GITHUB_WORKSPACE/spec-shelf`, authenticated by the **`SPEC_SHELF_TOKEN`**
  repo secret (fine-grained PAT, Contents read-only, that single repo).
- `MEHO_CONSUMER_DOCS_ROOT` is set **unconditionally** to
  `$GITHUB_WORKSPACE/spec-shelf/docs` on both jobs. When the secret is absent
  (fork PRs, Dependabot runs, not-yet-provisioned repos) the checkout step
  skips, the directory doesn't exist, and the resolvers
  (`backend/tests/_spec_shelf.py`, the product-agnostic #2980 harness the
  vcenter-specific `backend/tests/acceptance/_vcenter_spec.py` delegates its
  shelf-root branch to) return `None` → the lanes
  `pytest.skip()` exactly as before the wiring. When the secret exists, all
  five lanes run for real, and a fail-loud verify step turns any shelf-layout
  drift into a job failure instead of a silent regression to lane-skip.
- Lane placement (#2980): the seconds-cheap reconcile lanes run armed in
  the unit sweep; the canary's full-ingest tests run armed only in
  `python-integration` — its pytest step selects
  `tests/acceptance/test_g07_vsphere_canary.py` explicitly, and the unit
  sweep opts out via `MEHO_SKIP_SPEC_INGEST_TESTS=1` (a collection-time
  `skipif` marker on the canary's full-ingest tests, backstopped in its
  shared `_canary_corpus` fixture and its `vcenter_spec_path` fixture).
  The armed cost is amortised: the ~164 s two-spec ingest runs ONCE per
  module via the module-scoped `_canary_corpus` fixture (PR #2995 — the
  original per-test-ingest shape was 25 ingests and timeout-killed the
  60-min integration cap on its first armed run). Rationale + rule +
  the shared-corpus pattern for future heavy lanes:
  [`spec-reconcile-guards-standard.md`](../decisions/spec-reconcile-guards-standard.md)
  §Lane placement.
- Per-vendor reconcile lanes beyond vCenter (#2981-#2993) extend the same
  wiring — one `sparse-checkout` line + one verify line per product dir,
  same `SPEC_SHELF_TOKEN`, never a new secret. Standard + extension
  mechanics + red-lane protocol:
  [`docs/decisions/spec-reconcile-guards-standard.md`](../decisions/spec-reconcile-guards-standard.md).
- The `secrets` context is unavailable in step-level `if:`, so the gate is
  the job-env boolean `SPEC_SHELF_TOKEN_PRESENT` — presence only, never the
  token value.

Licensing record, signoff gate, and the full provisioning/rotation runbook:
[`docs/decisions/vendor-spec-ci-provisioning.md`](../decisions/vendor-spec-ci-provisioning.md).
**Do not provision `SPEC_SHELF_TOKEN` before that ADR's signoff-of-record
section is completed AND its "Known findings" prerequisite is fixed on
`main`** — the two reconcile lanes are known-red against the real shelf
(real op_id findings, the guard working), and both live inside required
merge-gate jobs, so premature provisioning turns CI red for every PR. The
wiring is deliberately inert until then.

### Fail-loud posture

No step in `ci.yml` carries `continue-on-error: true` except the Python
coverage artefact upload. Linters, formatters, type checkers, tests,
and the kubeconform schema validation are all allowed to fail the job.
The artefact upload is the only soft-fail: losing it degrades the
SonarCloud signal (no coverage for the run) but never invalidates the
test outcome, and `quality-gate.yml` already guards its own
`actions/download-artifact` with `continue-on-error` for the same
reason.

### Why no image build job

`ci.yml` deliberately does **not** build the backplane container image.
[`image.yml`](../../.github/workflows/image.yml) runs on PRs that touch
`backend/**`, `.github/workflows/image.yml` or `.trivyignore`
(path-filtered, see `image.yml`'s `on.pull_request.paths`) with
`push: false` — the Dockerfile + dep-resolution gate — plus a trivy scan
of `backend/uv.lock` on a hosted runner (the `lock-scan` job; see
`docs/codebase/backend.md`). PRs that don't touch the backend
(chart-only, CLI-only, docs-only) skip the image build by design,
because the gate's inputs haven't changed and rebuilding would add zero
signal. Repeating the build in `ci.yml` would double the cost for
backend PRs and pointlessly run the gate for the non-backend PRs that
`image.yml` already filters out. The same reasoning applies to the
chart publish (`chart.yml` runs `validate` + the required `helm-test`
job on PRs, gated by a `changes` job rather than an `on:` path filter —
a path-filtered workflow whose job is a *required* check would block
non-chart PRs on a never-reported status, so the jobs skip via a
job-level `if:` (→ reported Success) when the chart is untouched) —
`ci.yml` exercises a parallel `helm lint`/`helm template`/
`kubeconform` pass unconditionally so a chart-touching regression also
fails the central CI check, but it does not duplicate the publish
path. Migration backward-compat (`migration-compat.yml`), dependency
license scan (`dependency-license-check.yml`), secret scan
(`secret-scan.yml`), and the SAST stack (`security-scan.yml`) all stay
in their dedicated workflows. There, the `Semgrep SAST` job sends every
finding of its four rule packs (error, warning and info) to the GitHub
Security tab and shows new ones as annotations on pull requests, but it
fails only on a finding from an error-level rule.

### Coverage handoff to SonarCloud

Coverage runs **once a night**, not after every merge (#4910). `ci.yml`
has a `schedule` trigger (`cron: '17 6 * * *'`, UTC). That run is a full
CI run on `main`. The 8 `python-coverage` shards and the
`python-coverage-combine` job run only in that run, or on a PR that
carries the `ci-coverage-validate` label; their `if:` conditions allow
`schedule` and the label, never `push` or `merge_group`. The combine job
uploads `backend/coverage.xml` as the `python-coverage` artefact.
[`quality-gate.yml`](../../.github/workflows/quality-gate.yml) listens
on `workflow_run: workflows: ["CI"]`, runs only when the CI run's event
is `schedule` and its conclusion is `success`, downloads that exact
artefact name, and feeds the XML into the SonarCloud scan. The workflow
name (`CI`), the artefact name (`python-coverage`) and the event filter
are the load-bearing contract between the two workflows — changing one
side without the other would silently lose coverage reporting in
SonarCloud, or make SonarCloud import 0% backend coverage from a run
that never produced the file.

Why nightly: a push run on `main` used to start 8 coverage shards next
to the 4 unit shards, so every merge filled the 12-runner heavy pool.
Coverage took about 3,000 heavy-runner minutes a day, and the next merge
cancelled most of it. Meanwhile the merge queue's unit shards waited for
a runner or ran over their time budget. Coverage is not a required
check, so one analysis a day is enough. The nightly run has its own
concurrency group, so a merge does not cancel it.

History: #726 gated `--cov` to push-only on the belief that pytest-cov
was the unit job's dominant cost; the #771 diagnostic (#793) disproved
that — the real cost was per-test descriptor re-embedding (fixed in
#799) — and `--cov` ran on every PR for a while. #1982 then took
coverage out of the required unit lane for good (the OOM story in
[`sonarcloud.md`](sonarcloud.md)), #1987 gave it its own non-required,
push-only job, #3172 sharded that job, and #4910 moved it to the nightly
run. The `quality-gate.yml` whole-job
`continue-on-error` means a missing or late artefact never blocks a
merge. `COVERAGE_CORE=sysmon` (#739) swaps coverage.py's default C
tracer for the PEP 669 `sys.monitoring` backend (Python 3.12+,
supported by coverage.py 7.4+; the lockfile pins 7.14). Sysmon's
event-driven model removes most of the per-line tracing tax; line
counts matched the C tracer exactly (2913/11832 in both), so the
SonarCloud signal is unaffected.

### Fork-PR guard

Every job carries the same `if:` guard the publish workflows use:

```yaml
if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
```

This is defence in depth on top of branch protection — the
`meho-runners-ci` self-hosted runner pool is internal infrastructure, so
arbitrary code from a forked PR (`go test`, `pytest`, `helm template`
with custom values) is never allowed to execute on it. The OR
short-circuits on `push` events so main-branch CI is unaffected.

The six jobs that moved to GitHub-hosted runners (#4626, step 1) keep the
same guard. On a hosted runner the guard no longer protects internal
infrastructure. It stays because the `python-unit-lane` fan-in expects
`python-lint`, the shards and itself to skip together on a fork PR, and
because one identical guard on every `ci.yml` job keeps the
`test_ci_fork_runner_isolation.py` invariant simple to reason about. A
fork PR still gets every `ci.yml` required context reported as
success-by-skip, exactly as before.

This guard is `ci.yml`'s arm of the repo-wide untrusted-PR isolation
model — the full inventory, the disposable-runner routing pattern, and
the GitHub-settings enforcement live under "Untrusted pull-request
isolation" below.

### Local reproduction

Every gate the workflow runs can be reproduced locally with the same
commands. From the repo root:

Each toolchain command runs in its own subshell so `cd` never leaks
between sections — copy-paste the whole block and every command lands
in the correct subdir on its own.

```bash
# Python
(cd backend && uv sync --locked --all-groups)
(cd backend && uv run ruff check src/ tests/)
(cd backend && uv run ruff format --check src/ tests/)
(cd backend && uv run mypy src/)
(cd backend && uv run pytest -x --cov=meho_backplane --cov-report=term tests/)
# To mirror the CI coverage run (both push and PR, Python 3.12+):
# (cd backend && COVERAGE_CORE=sysmon uv run pytest -n 6 --dist loadscope --maxfail=1 \
#     --ignore=tests/integration --cov=meho_backplane --cov-report=xml tests/)

# Go
# CGO_ENABLED=1 is required for `go test -race` — same reason ci.yml
# sets it on the race step. The build/lint steps don't need cgo.
(cd cli && golangci-lint run)
(cd cli && go build ./...)
(cd cli && CGO_ENABLED=1 go test -race -cover ./...)

# Helm + kubeconform (run from repo root)
helm lint deploy/charts/meho/ <same --set overrides as ci.yml>
helm template test deploy/charts/meho/ <same --set overrides> > /tmp/rendered.yaml
kubeconform -strict -kubernetes-version 1.28.0 -ignore-missing-schemas -summary /tmp/rendered.yaml
```

The `--set` override block is the same one this doc's `## Verification`
section above documents — `ci.yml`, `chart.yml`, and the operator
copy-paste in [`backend/README.md`](../../backend/README.md) all keep
it in sync intentionally. Any drift means one of the three is wrong.

### Action pinning audit

All third-party actions in `ci.yml` are pinned to immutable SHAs with
the human-readable tag in a trailing comment. The SHAs match the ones
the publish workflows use where the action overlaps
(`actions/checkout`, `actions/setup-go`, `azure/setup-helm`,
`actions/upload-artifact`), so a single supply-chain audit covers all
of CI. Two actions are unique to `ci.yml`:

- `astral-sh/setup-uv@v10.1.0` — the uv installer, with
  `enable-cache: true` keyed by `uv.lock`. Every setup-uv step (also in
  `eval-gate.yml` and `docs-site.yml`) pins uv itself with `version:`,
  so a new uv release cannot change CI on its own. The Python version
  comes from `backend/.python-version` (3.14, the same as the image's
  `python:3.14-slim`). Raise the uv pin by hand, in all steps at once.
- `golangci/golangci-lint-action@v6.5.2` — pinned to the **v6** major,
  not v7+/v8+, because the in-tree
  [`cli/.golangci.yml`](../../cli/.golangci.yml) is written in the
  golangci-lint v1 config schema (`disable-all` + explicit `enable:`
  list, v1 linter names like `gosimple` / `errcheck`).
  golangci-lint-action v7.x+ defaults to the v2 binary which rejects
  v1 configs; pinning the binary to `v1.64.8` keeps the existing
  config valid. A future migration to the v2 schema flips both the
  action major and the binary version together.

## Untrusted pull-request isolation

Self-hosted runners are internal infrastructure. Any workflow that runs
on the `meho-runners-ci` pool in response to a **fork** pull request runs
attacker-controlled code (the fork's checked-out content, and for a fork
`pull_request` event the fork's own copy of the workflow body) on that
pool. GitHub documents this as the primary self-hosted-runner risk; see
the [secure-use reference](https://docs.github.com/en/actions/reference/security/secure-use).
`ci.yml`, `image.yml`, `chart.yml`, `pr-smoke.yml`, `plugin-test.yml`,
`eval-gate.yml`, `mcpb-bundle.yml` and `consumer-tool-name-check.yml`
already carried the fork guard; the audit below (F06, meho-internal#268)
closed the workflows that did not.

### Inventory — every PR-triggered job

Jobs that run in response to `pull_request` / `pull_request_target` and
their isolation posture:

| Workflow | Runs untrusted PR code? | Isolation mechanism |
| --- | --- | --- |
| `ci.yml` | yes (build/test/lint) | fork guard on every job (skip on fork PR) |
| `image.yml` | yes (Docker build) | fork guard (skip on fork PR) on the `build` job; the `lock-scan` job runs on GitHub-hosted `ubuntu-latest` and only reads `backend/uv.lock` |
| `chart.yml` | yes (helm template) | fork guard on the internal-pool PR job; publish/verify jobs are `push`-only |
| `pr-smoke.yml` | yes (`pull_request_target`) | fork guard + `environment: rke2-ci` + PR-head-SHA checkout + isolated `pr-smoke` cache scope |
| `migration-compat.yml` | yes (runs `check_migration_compat.py`) | **route**: fork PR → disposable GitHub-hosted runner |
| `migration-heads.yml` | yes (runs `check_migration_heads.py`) | **route**: fork PR → disposable GitHub-hosted runner; the `recheck-open-prs` job is `push`-only, runs on `ubuntu-latest` from a checkout of `main`, and only reads PR migration files (never runs PR code) |
| `dependency-license-check.yml` | yes (`pip install -e ./backend` runs the build backend) | **route**: fork PR → disposable GitHub-hosted runner (all 3 jobs) |
| `readme-version-check.yml` | yes (inline shell over PR content) | **route**: fork PR → disposable GitHub-hosted runner |
| `plugin-version-check.yml` | yes (runs `check_plugin_version.py`) | **route**: fork PR → disposable GitHub-hosted runner |
| `linux-server-test.yml` | yes (shellcheck, and the unit tests run `deploy/linux-server/install.sh` against stub commands) | **route**: fork PR → disposable GitHub-hosted runner |
| `secret-scan.yml` | yes (`docker run` TruffleHog over PR content) | **route**: fork PR → disposable GitHub-hosted runner |
| `security-scan.yml` | yes (Semgrep over PR content) | **fork guard** (skip on fork PR — the job runs inside the internal Harbor-proxied container, which a GitHub-hosted runner cannot reach; the required context still runs on `merge_group` + `push`) |
| `docs-site.yml`, `quality-gate.yml` | n/a | already on GitHub-hosted `ubuntu-latest` / `workflow_run` (trusted) |

### Two in-repo isolation patterns

- **Fork guard (skip).** The job-level `if:` the publish workflows use:

  ```yaml
  if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
  ```

  A fork PR (`head.repo.full_name != github.repository`) skips the job;
  a skipped required job reports **Success**, so branch protection is
  satisfied while nothing executes on the internal pool. Used where the
  job genuinely needs an internal resource — `security-scan.yml` runs in
  a `harbor.evba.lab`-proxied container, so relocating it to a disposable
  runner is impossible; the real scan runs on the trusted `merge_group`
  (queue admission) and `push` refs instead.

- **Route to a disposable runner.** Where the job needs no internal
  route, deployment credential or trusted build cache — only a language
  toolchain and public-registry egress — the fork PR runs on an
  ephemeral GitHub-hosted runner rather than being skipped, so the check
  still gives external contributors real feedback:

  ```yaml
  runs-on: ${{ (github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name != github.repository) && 'ubuntu-latest' || 'meho-runners-ci' }}
  ```

  Trusted refs (same-repo PR, `push`, `merge_group`) stay on the internal
  pool unchanged — this deliberately leaves the tuned `merge_group`
  required-gate paths (e.g. `secret-scan.yml`'s ghcr warm-cache retry,
  #3307/#3310) untouched and only relocates the untrusted fork-PR path.

### Enforcement outside PR-editable YAML

For a fork `pull_request`, GitHub runs the workflow body **from the PR
head** — so the in-YAML arms above hold only while the fork does not also
rewrite the workflow. They cover the exact exploit in the F06 finding (a
fork PR touching *only* a checker script inherits `main`'s workflow and
so routes off the internal pool), but the durable enforcement lives in
GitHub settings, which a PR cannot edit. Effective settings recorded
2026-09-07 (`gh api repos/evoila/meho/...`):

- **Fork-PR approval policy** = `all_external_contributors`
  (`actions/permissions/fork-pr-contributor-approval`) — every external
  fork PR needs a maintainer's "Approve and run" before any workflow
  runs. This is the primary containment (flipped 2026-09-06); keep it.
- **Default workflow token** = `read`, `can_approve_pull_request_reviews`
  = false (`actions/permissions/workflow`) — least privilege by default;
  jobs opt into `packages: write` / `id-token: write` explicitly.
- **Runner-group access** is an org-level setting (not readable from the
  repo API) and must restrict which repositories/workflows may schedule
  the `meho-runners-ci*` groups — the belt-and-suspenders against a fork
  that rewrites `runs-on` back to the internal pool.
- **`pull_request_target` environment approval**: `pr-smoke.yml`
  references `environment: rke2-ci`, but that environment does not yet
  exist (`copilot` and `github-pages` are the only ones). Before
  `vars.RKE2_SMOKE_ENABLED` is flipped on, `rke2-ci` must be created with
  required reviewers so the secret-bearing same-repo `pull_request_target`
  run gates on a human.

### Signing, deployment and cache separation

Untrusted PR jobs carry no signing or deployment identity: cosign
signing lives in `image.yml` (fork-guarded), the `chart.yml` publish
job (`push`-only), `cli-release.yml` (tag-only) and the `publish` job of
`linux-download.yml` (tag-only, called by `cli-release.yml`; it signs the
form-2 download's `SHA256SUMS-linux`, see
[linux-server-download.md](linux-server-download.md)), and `pr-smoke.yml`
exports its buildx cache to an isolated `pr-smoke` scope so an unmerged
PR-head build can never seed the default cache the cosign-signed
`image.yml` release build restores from (#3475). The CI cluster is recorded as separate from the automation
cluster; verifying the firewall/routing that a compromised CI runner
could traverse is an infra check, tracked outside this repo.


## Per-PR ephemeral cluster smoke (`.github/workflows/pr-smoke.yml`)

`pr-smoke.yml` is the per-PR ephemeral-cluster discipline Goal #11 DoD
bullet 4 hangs off: every PR against `main` builds a PR-tagged
backplane image, deploys the chart into a fresh `meho-ci-<pr-number>`
namespace on the consumer-operated `rke2-infra` Kubernetes cluster
(claude-rdc-hetzner-dc), runs `scripts/ci/pr-smoke.sh`, and tears the
namespace down regardless of smoke outcome. It is the inversion of
MEHO.X's failure mode — every code path that ships through G2.0–G2.6
closes the real-target feedback loop on a real Kubernetes API before
merge, not against mocks.

### Trigger and concurrency

| Property | Value |
| --- | --- |
| Event | `pull_request_target` against `main` (`opened`, `synchronize`, `reopened`) |
| Runner | `meho-runners-ci` (self-hosted) |
| Concurrency group | `pr-smoke-${{ github.event.pull_request.number }}` with `cancel-in-progress: true` |
| Permissions | `contents: read`, `packages: write`, `id-token: write`, `pull-requests: write` |
| Job timeout | 12 min (8 min smoke budget + cold-cache headroom — Task #50 AC #5) |

`pull_request_target` (not `pull_request`) is the load-bearing choice:
GitHub Actions executes the workflow file from `main`, not from the PR
head ref. That trigger is documented for "needs org secrets / OIDC on
PRs" precisely because the workflow body the runner executes is the
trusted base-branch version, not whatever the PR author pushed. The
job-level fork-PR guard (same shape as `ci.yml` / `image.yml` /
`chart.yml`) then skips fork PRs entirely so no untrusted Dockerfile
or shell ever executes on the self-hosted runner pool with secret
access. Same-repo PRs run with full secret + OIDC access; the PR head
SHA is checked out by SHA (not by ref) so a force-push during an
in-flight run cannot inject newer code after the secret-access gate
fired. The `pull_request_target` hardening pattern lifted from
[`securitylab.github.com/research/github-actions-preventing-pwn-requests/`](https://securitylab.github.com/research/github-actions-preventing-pwn-requests/).

`cancel-in-progress: true` on the per-PR concurrency group means an
author push during an in-flight run cancels the prior run; the
always-teardown step on the cancelled run still fires (GitHub Actions
runs `if: always()` steps even on cancellation) so the namespace is
reclaimed. Two PRs share no concurrency group, so the runner pool's
smoke capacity scales linearly with PR throughput.

### Consumer-side auth (gated)

Cluster auth + RBAC for `meho-ci-*` namespaces is provisioned on
`evoila-bosnia/claude-rdc-hetzner-dc`,
not in this repo — see
[`docs/cross-repo/rke2-infra-coordination.md`](../cross-repo/rke2-infra-coordination.md)
for the full contract (Section 1: auth options; Section 2: RBAC verb
set; Verification: end-to-end check). The consumer-side tracker is
`evoila-bosnia/meho-internal#53`
(G2.7-T5).

The workflow ships now and **fails-skip** (skipped at job level, not
red) while the consumer side is still being provisioned. Once auth
lands, the gate flips on automatically — no workflow edit required.
The gate is:

```yaml
if: |
  (github.event.pull_request.head.repo.full_name == github.repository) &&
  (vars.RKE2_SMOKE_ENABLED == 'true')
```

GitHub Actions does **not** expose the `secrets` context to job-level
`if:` expressions (only `github`, `needs`, `vars`, and `inputs` are
available — see the
[GitHub Actions contexts reference](https://docs.github.com/en/actions/reference/contexts-reference)).
A clause like `secrets.RDC_KUBECONFIG != ''` therefore collapses to
undefined at evaluation time and the gate silently breaks. The single
repository-scoped `vars.RKE2_SMOKE_ENABLED` is the documented gate;
maintainers flip it to `'true'` after the consumer-side auth (Task
\#53) lands. The "Build kubeconfig" step still inspects
`env.RKE2_CA_CERT` / `env.RDC_KUBECONFIG` to pick Option A vs Option B
at step level (where the env indirection makes `secrets.*` legal).

Two auth modes are supported, matching the cross-repo doc's Sections 1A
and 1B:

| Mode | Required from consumer | Selected when |
| --- | --- | --- |
| Option A (OIDC trust) | `RKE2_CA_CERT` secret + `RKE2_API_SERVER` var + apiserver `--oidc-issuer-url=https://token.actions.githubusercontent.com` (or `AuthenticationConfiguration`) | `RKE2_CA_CERT` is set AND `RKE2_API_SERVER` is set |
| Option B (kubeconfig) | `RDC_KUBECONFIG` secret (base64-encoded SA kubeconfig) | Only `RDC_KUBECONFIG` is set |

When both are set, Option A wins (the cross-repo doc's preference
order: short-lived OIDC tokens vs. a long-lived stored kubeconfig).
When neither is set, the "Build kubeconfig" step errors out — but
the job-level `vars.RKE2_SMOKE_ENABLED` gate is the upstream guard
that prevents that path from ever running while the consumer side
is still unrolled-out.

The repository-scoped `vars.RKE2_SMOKE_ENABLED == 'true'` clause is the
**single enable knob** for maintainers: once the consumer side rolls
out and the secrets are provisioned, set the variable to `true` and
the gate flips on for every subsequent PR. Useful for the `5
consecutive green smokes` Goal #11 window — flip the var, queue 5 PRs,
count. While the variable is unset (or `'false'`), the workflow is
`skipped` at job level (not `failure`), so PRs are never blocked on
the unrolled-out consumer side.

### Job structure

A single job (`smoke`) with sequential steps. Splitting into multiple
jobs (build / deploy / smoke / teardown) would force per-job runner
startup overhead onto every PR's 8-minute budget, and `if: always()`
would have to traverse `needs:` edges with explicit
`if: ${{ always() && needs.deploy.result != 'skipped' }}` plumbing —
a single-job layout keeps the teardown invariant trivially correct
("always() runs even on cancel").

Steps, in order:

1. **Checkout PR head SHA** — `actions/checkout@v6.0.2` against
   `${{ github.event.pull_request.head.sha }}` (not `head.ref`).
2. **QEMU + buildx + GHCR login** — same action SHAs as `image.yml`.
3. **Build + push PR-tagged image** — `docker/build-push-action@v7.1.0`
   pushing `ghcr.io/evoila/meho:pr-<n>-<sha>`. The `<sha>` suffix
   makes the tag immutable per push: a force-push to the PR branch
   produces a NEW tag (new sha), so the deploy step always pulls the
   build the smoke is about to assert against. amd64-only — the
   per-PR feedback loop optimises for time, not for proving multi-arch
   (that invariant belongs to `image.yml` on main-push).
4. **Install kubectl + Helm** — `azure/setup-kubectl@v5.1.0` (v1.28.15
   tracking the chart's `kubeVersion: ">=1.28.0-0"` floor) +
   `azure/setup-helm@v4.3.1` (same SHA as `chart.yml`).
5. **Configure kubectl (OIDC mode)** — `actions/github-script@v9.0.0`
   mints an OIDC ID token via `core.getIDToken(audience)` against the
   consumer-chosen audience (default `rke2-infra.evba.lab`, override
   via `vars.RKE2_OIDC_AUDIENCE`). Skipped when Option A's inputs
   aren't set, so Option B's kubeconfig path takes over below.
6. **Build kubeconfig** — assembles `$HOME/.kube/config` from either
   the OIDC token + CA cert (Option A) or the base64-decoded
   kubeconfig secret (Option B). Pins the default namespace on the
   active context. Surfaces `kubectl auth whoami` in logs so any
   later RBAC denial is debuggable.
7. **Create ephemeral namespace** — idempotent
   `apply --dry-run=client -o yaml | apply -f -` so a leftover
   namespace from a cancelled run doesn't trip `AlreadyExists`.
   Labels the namespace with `meho.io/managed-by=pr-smoke` and
   `meho.io/pr-number=<n>` for consumer-side audit-log filtering.
8. **Helm install** — `helm upgrade --install meho deploy/charts/meho/
   -f deploy/values-examples/values-rdc-example.yaml
   --set image.tag=pr-<n>-<sha> --wait --timeout 5m`. Uses the
   in-tree example overlay as the base (real-target fixture layout)
   with the PR-tagged image as the only override.
9. **Run smoke** — `bash scripts/ci/pr-smoke.sh "$NS"`. See "Smoke
   contract" below.
10. **Teardown** — `if: always()`. `helm uninstall` + `kubectl delete
    namespace --wait=false --ignore-not-found`. `|| true` on each so
    a partial-cleanup error doesn't block the namespace delete that
    follows. Final `kubectl get namespace "$NS"` echo for observability.
11. **PR comment** — `if: always() && github.event.pull_request.number
    != ''`. `actions/github-script@v9.0.0` posts a one-paragraph
    pass/fail summary with the workflow-run link.

### Smoke contract (`scripts/ci/pr-smoke.sh`)

The smoke script is deliberately scoped to the **unauthenticated
operator surface** — four assertions, no Keycloak access token:

| Endpoint | Assertion | Why |
| --- | --- | --- |
| `/healthz` | HTTP 200 | Liveness probe contract; the in-cluster kubelet uses this exact path |
| `/version` | `git_sha` present and not `"unknown"` | Confirms the deployed image carries build metadata (`image.yml` / `pr-smoke.yml` pass `GIT_SHA` as a Docker `build-arg`, #631) and isn't a fallback build |
| `/version` | `chart_version` present, not `"unknown"`, non-empty | Confirms the helm-installed chart injected `CHART_VERSION` from `.Chart.Version` (#631) — the deployed-chart provenance the governance backplane exists to answer |
| `/api/v1/health` | HTTP 401 unauthenticated | Negative auth test — a 200 here would mean auth middleware regressed open OR Keycloak realm is wired wrong; both are PR-blocking regressions Goal #11 considers non-negotiable |

The full authenticated federation-chain smoke
(claude-rdc-hetzner-dc/manifests/meho/smoke.sh — operator-facing,
real Keycloak + Vault credentials, against the persistent install) is
**out of scope** for the per-PR ephemeral lane: every PR provisioning
a Keycloak realm + Vault role would be both slow and a security
liability. Goal #11 G2.8 covers the authenticated smoke against the
production-style instance.

Script invariants:

- `set -euo pipefail` aborts on first failure (a half-ready backplane
  shouldn't be probed for more endpoints than the first one that
  failed).
- Inline literal compare (`[ "$X" = "200" ]`), not `-eq` family —
  `-eq 200` also matches an empty string from a failed curl on some
  bash builds. Literal-string comparison fails-loud as intended.
- Background `kubectl port-forward` PID captured into `PF_PID` with
  an `EXIT` trap that kills it even on bash abort, so a CI runner
  doesn't leak the port-forward process for the next job on the same
  runner.
- `curl --retry 5 --retry-delay 1 --retry-connrefused` covers the
  port-forward warm-up gap — the socket is bound before the
  kubectl-proxy handshake fully settles. The retry budget (5s total)
  is shorter than helm's `--wait` budget (5m) so a failing rollout
  surfaces in helm, not in the smoke retry.

### Acceptance-criterion verification

| Criterion (Task #50 AC) | Verification path |
| --- | --- |
| Workflow exists; runs after PR open/update | `gh workflow list --repo evoila/meho` shows `pr-smoke` |
| Pushes image to `ghcr.io/evoila/meho:pr-<n>` | Verifiable on a live PR run once consumer-side auth is provisioned |
| Deploys chart to `meho-ci-<n>` on rke2-infra | Same — deferred-AC pending consumer side |
| Tears down namespace regardless of smoke result | `if: always()` on teardown step; `cancel-in-progress: true` invariant covered in concurrency block above |
| Smoke result posted as PR comment | `actions/github-script` step with `if: always()` gate |
| Concurrency: PR update cancels prior smoke | `concurrency.cancel-in-progress: true` |
| Wall-clock < 8min for green smoke | 12-min job timeout with 8-min headline budget; cold buildx cache is the typical worst case; main-push image.yml's cache fills shared layers |

Deferred-AC: every criterion that requires a live run against
rke2-infra (image-push verification, namespace-create-then-teardown
verifiability) is gated on the consumer-side OIDC trust OR
`RDC_KUBECONFIG` secret landing. Until then the workflow ships in
"skipped at job level" mode and the AC bullets stay open in the
Task body's tracking checklist. The cross-repo doc's "Status" table
is the source of truth for when these bullets close.

### Workflow-coexistence map

`pr-smoke.yml` is the per-PR ephemeral-cluster layer **above** the
toolchain matrix; the layers below it are unchanged and run in
parallel on every PR:

| Layer | Workflow | Surface | Cost |
| --- | --- | --- | --- |
| Toolchain matrix | `ci.yml` | Python + Go + Helm lint/template/test | ~5 min |
| Image gate | `image.yml` | Dockerfile + deps build (path-filtered) | ~3 min on backend PRs, skipped otherwise |
| Chart validation | `chart.yml` | helm lint + template + kubeconform | ~1 min |
| **Per-PR ephemeral** | **`pr-smoke.yml`** | **Build → deploy → smoke → teardown** | **~6-8 min on green** |
| Migration backward-compat | `migration-compat.yml` | Path-scoped to `backend/alembic/versions/**` | ~30s when triggered |

`pr-smoke.yml` does **not** duplicate the image build with
`image.yml` — it pushes a transient PR-tag, while `image.yml` on PRs
builds without pushing (gate only). The smoke build **reads** the shared
GHA buildx cache (`cache-from: type=gha`, the default `buildkit` scope),
so it typically hits a warm cache when application code is the only
delta — but it **exports** only to an isolated `cache-to:
type=gha,scope=pr-smoke,mode=max` scope. Because `pr-smoke.yml` runs
under `pull_request_target` with `GITHUB_REF` pinned to `main`, that
isolation keeps an unmerged PR-head build from writing the default cache
scope the cosign-signed `image.yml` release build restores from; the
release image's trust chain never depends on cache produced by unmerged
code, while the per-PR build keeps its warm-cache read.

## Dependabot waits 7 days before it takes a new version

Every entry in `.github/dependabot.yml` has a `cooldown` block with
`default-days: 7`. Dependabot opens a version-update pull request for a
new release (a GitHub Action, a Python package in `backend/uv.lock`, or
a base image in a Dockerfile) only when that release is at least 7 days
old.

- **Why.** A new release can carry harmful code, for example when an
  attacker takes over a maintainer's account. Such releases are usually
  found and removed within a few days. The wait skips them.
- **Security updates are not delayed.** Dependabot's `cooldown` applies
  to version updates only. A security update still comes at once.
- **Without the block, Dependabot already waits 3 days** on github.com.
  The block makes the wait visible in the file and sets it to 7 days.
- **A manual upgrade does not wait.** `uv lock --upgrade` or a hand
  edit of a Dockerfile digest can still take a release that is younger
  than 7 days. The matching uv setting (`exclude-newer` in
  `backend/pyproject.toml`) is not set yet; it needs a lock file in
  which every locked version is already 7 days old, otherwise
  `uv lock` moves packages back.

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

- HPA / PDB / topologySpreadConstraints — deferred to v0.2. v0.1 is
  single-replica per Goal #11 scope. (ServiceMonitor + PrometheusRule
  shipped in #2885 — see "Metrics scrape wiring" under Chart contract.)
- Broadcast subchart HA (Sentinel/Cluster) and persistence — deferred
  to v0.2 per ADR 0005. (Valkey `requirepass` auth + the subchart's own
  ingress NetworkPolicy shipped in #276 — see "Broadcast subchart" above.)
- `broadcast.externalEndpoint` opt-out for operators with a managed
  Redis/Valkey already running — deferred to v0.2 (the
  `broadcast.enabled: false` knob lands in v0.1 as the disable path).

## Cross-repo handshake

The v0.1 deploy contract crosses one repo boundary: `evoila/meho`
produces the chart + image; the dogfooding consumer
`evoila-bosnia/claude-rdc-hetzner-dc`
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

## Acceptance contracts (Goal #11 DoD)

Goal #11's Definition of Done is a deploy contract — it is satisfied
when a `claude-rdc-hetzner-dc` operator can install, smoke, roll back,
and observe a MEHO instance against the real lab in bounded wall-clock
time. The producer-side acceptance contracts that codify what
"passing" looks like for each DoD bullet live in
[`docs/acceptance/`](../acceptance/README.md), each pointing at the
companion verifier shell under
[`scripts/acceptance/`](../../scripts/acceptance/).

| DoD bullet | Contract | Verifier |
| --- | --- | --- |
| 1 — `install.sh` cold-deploy → working MEHO in <5 min | [`docs/acceptance/install.md`](../acceptance/install.md) | [`scripts/acceptance/install-verify.sh`](../../scripts/acceptance/install-verify.sh) |
| 2 — `smoke.sh` passes (login + status + audit-row + Vault + DB-migration state) — federation chain end-to-end | [`docs/acceptance/smoke.md`](../acceptance/smoke.md) | [`scripts/acceptance/smoke.sh`](../../scripts/acceptance/smoke.sh) |
| 3 — `helm rollback meho` end-to-end with a non-trivial schema diff (cluster-level forward-compat proof) | [`docs/acceptance/rollback.md`](../acceptance/rollback.md) | [`scripts/acceptance/rollback-verify.sh`](../../scripts/acceptance/rollback-verify.sh) (sample N+1 migration at [`scripts/acceptance/synthetic-n-plus-1.sql`](../../scripts/acceptance/synthetic-n-plus-1.sql)) |
| 4 + 5 — 5-consecutive-merged-PR green-smoke counter + `targets.yaml` `rdc-meho` entry (deploy-stability proof + chassis registration) | [`docs/acceptance/green-counter.md`](../acceptance/green-counter.md) | producer-side contract only — counter implementation and the `targets.yaml` entry land on the consumer side per the cross-repo split. Schema for the `targets.yaml` entry lives at [`docs/cross-repo/targets-yaml.md`](../cross-repo/targets-yaml.md); the draft consumer-side issue body the maintainer files is at [`docs/cross-repo/issue-58-consumer-ticket-body.md`](../cross-repo/issue-58-consumer-ticket-body.md); the README ships a badge placeholder the maintainer swaps for the live Shields endpoint URL once the consumer-side counter is up |

The split between producer-owned contracts + verifiers and
consumer-owned wrappers (`install.sh`, `smoke.sh`,
`rollback-drill.sh`, …) is the same shape as the cross-repo
handshake above: the chart producer owns "what passing means"; the
consumer owns "how to drive the install on this environment". The
verifier is invoked as the last step of the consumer's wrapper, and
the verifier's exit code becomes the wrapper's exit code.

The rollback contract is the **cluster-level** half of the
forward-compat assurance Goal #11 DoD bullet 3 promises; the
**unit-level** half lives at
[`backend/tests/migrations/test_migration_rollback.py`](../../backend/tests/migrations/test_migration_rollback.py)
(Task #30, Initiative #26) and runs on migration-touching PRs via the
`python-migration-tests` job in
[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) (relocated
out of the every-PR unit lane by #2140; a broken chain still fails
every PR via the unit lane's `alembic upgrade head` fixture, so only
the deep per-migration assertions defer to migration PRs).
Together they assert "the N image runs cleanly against the N+1
schema" at two layers: testcontainers in CI (fast, on migration-touching
PRs) and real `helm rollback` against the lab (slow, Goal-closing milestone).

## References

- Parent Goal: #11 — Deployable v0.1
- Parent Initiative: #36 — G2.5 Helm chart
- Parent Initiative: #48 — G2.7 CI/CD + per-PR ephemeral smoke
- Task #50 (G2.7-T2) — Per-PR ephemeral cluster deploy + smoke + teardown
- Task #53 (G2.7-T5) — Cross-repo coordination tracker (consumer-side kubeconfig + RBAC)
- Task #55 (G2.8-T1) — `install.sh` cold-deploy acceptance contract + verifier (`docs/acceptance/install.md`, `scripts/acceptance/install-verify.sh`)
- Task #56 (G2.8-T2) — `smoke.sh` federation-chain acceptance contract + verifier (`docs/acceptance/smoke.md`, `scripts/acceptance/smoke.sh`)
- Task #57 (G2.8-T3) — `helm rollback` end-to-end acceptance contract + verifier (`docs/acceptance/rollback.md`, `scripts/acceptance/rollback-verify.sh`, `scripts/acceptance/synthetic-n-plus-1.sql`)
- Task #58 (G2.8-T4) — 5-consecutive-merged-PR green-smoke counter contract + `targets.yaml` `rdc-meho` schema (`docs/acceptance/green-counter.md`, `docs/cross-repo/targets-yaml.md`, `docs/cross-repo/issue-58-consumer-ticket-body.md`, README badge placeholder); consumer-side counter implementation + `targets.yaml` entry tracked on `claude-rdc-hetzner-dc`
- Task #30 (G2.3-T4) — unit-level forward-compat regression test (`backend/tests/migrations/test_migration_rollback.py`)
- Helm `helm rollback` reference: https://helm.sh/docs/helm/helm_rollback/
- Helm chart hooks reference: https://helm.sh/docs/topics/charts_hooks/
- GitHub Actions OIDC: https://docs.github.com/en/actions/concepts/security/openid-connect
- `pull_request_target` hardening guide: https://securitylab.github.com/research/github-actions-preventing-pwn-requests/
- Helm chart structure: https://helm.sh/docs/topics/charts/
- Kubernetes NetworkPolicy: https://kubernetes.io/docs/concepts/services-networking/network-policies/
- cert-manager Ingress annotations: https://cert-manager.io/docs/usage/ingress/
- External Secrets Operator: https://external-secrets.io/
- ESO Vault provider: https://external-secrets.io/latest/provider/hashicorp-vault/
- ESO ExternalSecret API: https://external-secrets.io/latest/api/externalsecret/
- Cross-repo handshake spec: [`docs/cross-repo/rke2-infra-coordination.md`](../cross-repo/rke2-infra-coordination.md)
