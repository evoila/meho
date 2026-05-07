<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2026 evoila Group
-->

# Kubernetes deployment runbook

This guide covers installing, licensing, upgrading, and
troubleshooting MEHO on Kubernetes via the Helm chart at
`deploy/helm/meho/`. It is the operator-facing companion to the
chart README and is intended to be the single document you read end
to end before running `helm install` against a non-laptop cluster.

For Docker Compose (development / single-host evaluation) see
[Deployment](../deployment.md). The Compose stack and the Helm chart
are independent install paths — use one or the other, not both
against the same data.

> **Status — `0.1.0` early.** The chart and this runbook are pre-1.0;
> any value key, default, or template contract may change between
> minor versions. Pin `image.tag` to a specific patch release in
> production.

## 1. Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| Kubernetes | 1.27+ tested | Older versions may work (the chart uses GA-1.19 `networking.k8s.io/v1` Ingress and GA-1.9 `apps/v1` Deployments) but are unverified; the embedded Bitnami subcharts may carry their own `kubeVersion` floors when `embedded.enabled=true`. |
| Helm | 3.x | The chart declares `apiVersion: v2`. Helm 2 is unsupported. |
| Ingress controller | one installed | `nginx`, `traefik`, `aws-load-balancer-controller`, `gke-ingress`, anything that honours `ingressClassName`. Required only when `ingress.enabled=true`. |
| External Postgres 16 | reachable from the cluster | Required for production. RDS, Cloud SQL, Aiven, or self-managed all work. The DSN must be accepted by `asyncpg`. |
| External Redis 7 | reachable from the cluster | Required for production. ElastiCache, Memorystore, Upstash, or self-managed. |
| Keycloak (or compatible OIDC IdP) | browser-reachable | The chart does **not** deploy Keycloak. Operators must bring their own — the Compose stack at [Deployment](../deployment.md) ships one for development, but production deployments typically point `frontend.keycloakUrl` at a managed Keycloak / Auth0 / similar OIDC server. |
| TLS certificate | for Ingress host | The chart does not issue certificates. Integrate `cert-manager` (or your cloud's managed certificate) and reference the resulting Secret via `ingress.tls[].secretName`. |
| Container registry access | `ghcr.io/evoila/*` reachable | Public images, but air-gapped clusters need a registry mirror. |

Evaluators on a laptop (kind / minikube / Docker Desktop) can skip
the external Postgres/Redis requirement by setting
`embedded.enabled=true`; see the [evaluator quick start](#2-quick-start-evaluator-path)
below.

## 2. Quick start (evaluator path)

For evaluating MEHO on a single-namespace kind / minikube cluster.
**Not for production** — embedded Postgres and Redis are bundled by
Bitnami subcharts and have no production-grade backup, monitoring,
or upgrade story. The first thing the [production install](#3-production-install)
does is replace them.

```bash
git clone https://github.com/evoila/meho.git
cd meho

# Pull the Bitnami postgresql + redis subcharts into charts/
helm dependency update deploy/helm/meho

# Install with the dev overlay — bundles Postgres + Redis,
# uses the slim backend image, ships deterministic dev secrets.
helm install meho deploy/helm/meho \
  --values deploy/helm/meho/values-dev.yaml \
  --set image.tag=0.1.0
```

Wait for all pods to be ready (15–60 seconds against a warm cache,
longer on the first pull):

```bash
kubectl rollout status deployment/meho-backend
kubectl rollout status deployment/meho-frontend
kubectl get pods -l app.kubernetes.io/instance=meho
```

The dev overlay disables Ingress (kind / minikube don't ship one),
so reach the SPA via port-forward:

```bash
kubectl port-forward svc/meho-frontend  8080:80   &
kubectl port-forward svc/meho-backend   8000:8000 &
open http://localhost:8080
```

The `frontend.apiUrl` default in `values-dev.yaml`
(`http://localhost:8000`) matches the port-forward binding above —
cluster-internal Service DNS would not work because the browser,
not a pod, performs the fetches.

> **Keycloak is not bundled.** The chart deploys backend, frontend,
> and (in embedded mode) Postgres + Redis, but **no Keycloak
> Service**. `values-dev.yaml` ships with `frontend.keycloakUrl:
> http://localhost:8081`, which assumes you have a Keycloak running
> separately on `localhost:8081` (e.g. via `docker run` or a
> `kubectl port-forward` against an OIDC server you deploy
> separately). Without one, the SPA loads but the login flow fails
> at the OIDC redirect. For evaluation, the simplest fix is to run
> Keycloak via Docker on the host and let the SPA reach it via
> `localhost:8081`.

To uninstall:

```bash
helm uninstall meho
kubectl delete pvc -l app.kubernetes.io/instance=meho   # only if you want the data gone too
```

## 3. Production install

The production path assumes you have already provisioned managed
Postgres 16 and Redis 7 (RDS / Cloud SQL / Aiven, ElastiCache /
Memorystore / Upstash, etc.) and have an Ingress controller plus a
TLS certificate strategy in place.

### 3.1 Prepare the backend Secret via External Secrets Operator

The recommended pattern is [External Secrets Operator](https://external-secrets.io)
projecting the backend Secret from your cluster's secret store
(GCP Secret Manager, AWS Secrets Manager, Vault, …). The chart
references the resulting Kubernetes Secret by name; values never
appear in `helm history` or chart values.

Save the following as `external-secret.yaml`, replacing the
provider stanza and `remoteRef.key` values for your store:

```yaml
apiVersion: external-secrets.io/v1
kind: SecretStore
metadata:
  name: meho-secret-store
spec:
  provider:
    gcpsm:
      projectID: "your-gcp-project"
---
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: meho-backend
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: meho-secret-store
    kind: SecretStore
  target:
    name: meho-backend
    creationPolicy: Owner
  data:
    - secretKey: JWT_SECRET_KEY
      remoteRef:
        key: meho-jwt-secret
    - secretKey: CREDENTIAL_ENCRYPTION_KEY
      remoteRef:
        key: meho-credential-encryption-key
    - secretKey: MEHO_LICENSE_KEY
      remoteRef:
        key: meho-license-key
```

Apply it and wait for the Secret to materialise:

```bash
kubectl apply -f external-secret.yaml
kubectl wait --for=condition=Ready externalsecret/meho-backend --timeout=60s
kubectl get secret meho-backend                    # should exist
```

`CREDENTIAL_ENCRYPTION_KEY` must be at least 32 characters — the
backend rejects shorter values at startup ([meho_app/core/config.py](https://github.com/evoila/meho/blob/main/meho_app/core/config.py)).
The chart-managed Secret template enforces the same minimum at
template time when `secrets.create=true`, but External-Secrets-
projected values bypass that template; if your store carries a
short value you'll only discover it via `CrashLoopBackOff` on the
backend pod. Validate the source-of-truth length when you write the
value into the secret store.

`MEHO_LICENSE_KEY` is optional; omit the entry to run in community
edition.

`JWT_SECRET_KEY`, `CREDENTIAL_ENCRYPTION_KEY`, and `MEHO_LICENSE_KEY`
are the keys the backend reads via `envFrom: secretRef`. `KEYCLOAK_*`
keys are not consumed from this Secret today — operators needing
custom Keycloak credentials must bake them into the same Secret and
the backend Pod's environment will pick them up via `envFrom`.

`DATABASE_URL` and `REDIS_URL` are *not* read from this Secret. The
chart owns those keys: it templates them onto the backend Deployment
as explicit `env:` entries from `postgres.external.dsn` /
`redis.external.url`, and explicit `env:` takes precedence over
`envFrom` — any same-named keys your store happens to carry are
silently overridden. This is deliberate: it prevents an operator
mistake in the secret store from rerouting the backend at a
connection level the chart did not authorise.

### 3.2 Compose your overrides file

The shipped [`values-prod.yaml`](https://github.com/evoila/meho/blob/main/deploy/helm/meho/values-prod.yaml)
ships the production-side defaults: two backend replicas, production
resource requests, `embedded.enabled: false`, and
`secrets.create: false`. Don't replace it — layer on top with a
short overrides file (call it `overrides.yaml`) that carries only
the values your environment dictates:

```yaml
# overrides.yaml — operator-supplied values layered on top of the
# shipped values-prod.yaml. Helm merges later --values files over
# earlier ones, so anything you set here wins.

image:
  tag: "0.1.0"      # pin a specific MEHO patch release in production
  variant: full     # or "slim" (no PyTorch / no Docling, ~500 MB image)

frontend:
  apiUrl: https://meho.example.com
  keycloakUrl: https://auth.meho.example.com
  allowedOrigins: https://meho.example.com
  keycloakOrigin: https://auth.meho.example.com

ingress:
  enabled: true
  className: nginx
  host: meho.example.com
  tls:
    - hosts:
        - meho.example.com
      secretName: meho-tls    # cert-manager-issued, out-of-band

secrets:
  existingSecret: meho-backend   # the ExternalSecret target name
```

Pass the database connection strings via `--set-string` rather than
committing them to a values file. `--set-string` (not `--set`)
avoids Helm's value-parser splitting on commas / equals inside a
DSN.

### 3.3 Install

```bash
helm install meho deploy/helm/meho \
  --namespace meho --create-namespace \
  --values deploy/helm/meho/values-prod.yaml \
  --values overrides.yaml \
  --set-string postgres.external.dsn='postgresql+asyncpg://USER:PASS@HOST:5432/meho' \
  --set-string redis.external.url='redis://HOST:6379/0' \
  --wait --timeout=10m
```

Helm merges `--values` files left-to-right, so your `overrides.yaml`
wins over the shipped `values-prod.yaml` for any key both set. The
shipped file's keys (replica counts, resources, `embedded.enabled:
false`, etc.) carry through unchanged unless you override them.

`--wait --timeout=10m` blocks until every workload reports ready (or
fails). On the first install with a cold image cache the backend
container can take 2-5 minutes to pull (the full image is ~4 GB; the
slim image ~500 MB).

Verify:

```bash
kubectl -n meho get pods,svc,ingress
kubectl -n meho rollout status deployment/meho-backend
kubectl -n meho rollout status deployment/meho-frontend
kubectl -n meho logs deployment/meho-backend --tail=50
```

The backend's first start runs Alembic migrations against the
external Postgres; the migration step is loud about success or
failure (`scripts/run-migrations-monolith.sh` exits non-zero on a
schema problem and the lifespan handler refuses to serve traffic).

## 4. License activation

MEHO ships open-core under AGPLv3. The community edition is fully
functional for single-tenant single-user deployments. Enterprise
features (multi-tenancy, advanced approval workflows, scheduled
investigations, …) require a signed Ed25519 license token — a
JWT-shaped triple verified at backend startup. Past the token's
`expires_at`, MEHO grants a 30-day grace period before falling back
to community mode.

### 4.1 Activate via External Secrets Operator (production)

Add or update the `MEHO_LICENSE_KEY` data entry in your
`ExternalSecret` resource (Section 3.1):

```yaml
data:
  - secretKey: MEHO_LICENSE_KEY
    remoteRef:
      key: meho-license-key   # whatever your store calls the secret
```

ESO refreshes the projected Secret per the `refreshInterval` you
set (1h in the §3.1 example), but Kubernetes does not signal pods
on Secret change — they read environment variables once at
startup. Trigger a rolling restart so the backend picks up the
new value:

```bash
kubectl -n meho rollout restart deployment/meho-backend
kubectl -n meho rollout status  deployment/meho-backend
```

Confirm:

```bash
kubectl -n meho logs deployment/meho-backend --tail=20 | grep -iE 'license|edition'
```

The verifier (in [`meho_app/core/licensing.py`](https://github.com/evoila/meho/blob/main/meho_app/core/licensing.py))
emits one of these messages at startup:

| Status | Log message |
|---|---|
| Valid enterprise token | `Enterprise edition active (org=<org>)` (info) |
| No license key set | `Community edition -- enterprise routers excluded` (info) |
| License malformed / signature fails | `Invalid license key -- falling back to community edition` (warning) |
| Token expired, within 30-day grace period | `License expired on YYYY-MM-DD -- grace period active (N days remaining)` (warning) |
| Token expired more than 30 days ago | `License expired on YYYY-MM-DD -- grace period ended, community edition active` (warning) |

The verifier emits status only — never the token itself.

### 4.2 Activate via chart-managed Secret (evaluator only)

When `secrets.create=true` (the evaluator default), set the license
key alongside the other chart-managed values:

```bash
helm upgrade meho deploy/helm/meho \
  --reuse-values \
  --set-string secrets.licenseKey='eyJhbGciOiJFZERTQSIsInR5cCI6Ik1FSE...'
```

`--reuse-values` carries forward every other value from the previous
release, so only `secrets.licenseKey` changes. The chart re-renders
the Secret, Kubernetes triggers a rolling update, and the backend
boots with `MEHO_LICENSE_KEY` set.

Tokens passed through `--set` flow into Helm's release history; this
is acceptable for evaluation but never for production. Use the ESO
path (Section 4.1) for any non-laptop deployment.

### 4.3 Where license tokens come from

Production tokens are minted by maintainers via
[`scripts/issue-license.py`](https://github.com/evoila/meho/blob/main/scripts/issue-license.py)
against the production Ed25519 signing key (held in a maintainer
vault, never on the cluster). Self-hosters running in fully
community mode skip this step; self-hosters paying for enterprise
features receive the token out-of-band from the maintainer who ran
the issuance CLI.

## 5. Upgrade procedure

Upgrades follow the standard `helm upgrade` flow. The backend pod's
entrypoint runs `alembic upgrade head` against the configured
Postgres on every start; migrations are forward-only and additive,
so a rolling upgrade does not pause traffic.

### 5.1 Standard upgrade

```bash
# 1. Pull the new chart version (or bump the chart-museum reference)
cd path/to/meho && git pull

# 2. Refresh subchart archives if dependencies changed
helm dependency update deploy/helm/meho

# 3. Preview the rendered diff for the new release
helm diff upgrade meho deploy/helm/meho \
  --values values-prod.yaml \
  --set-string postgres.external.dsn="$DSN" \
  --set-string redis.external.url="$REDIS"

# 4. Apply
helm upgrade meho deploy/helm/meho \
  --values values-prod.yaml \
  --set image.tag=0.1.1 \
  --set-string postgres.external.dsn="$DSN" \
  --set-string redis.external.url="$REDIS" \
  --wait --timeout=10m
```

`helm diff` is a community plugin
(<https://github.com/databus23/helm-diff>); install with `helm plugin
install https://github.com/databus23/helm-diff`. It is optional but
recommended — it surfaces unintended drift before `helm upgrade`
commits the change.

Helm applies the upgrade to both Deployments; K8s controllers roll
each one independently and concurrently. Both Deployments use the
default `RollingUpdate` strategy with `maxSurge=25%` /
`maxUnavailable=25%`, so a pod that fails to become Ready is
removed from its Service via the readiness probe before it
accepts traffic. Backend readiness hits `/health` (zero-I/O
liveness; see [§6.1 below](#61-readiness-probe-design)) so a
backend that fails to boot is taken out of rotation cleanly.

### 5.2 Reset values vs reuse values

| Flag | Behaviour | When to use |
|---|---|---|
| _none_ (default) | Use only the values you pass on this command. | Fresh install or full re-spec. |
| `--reuse-values` | Carry forward last release's values, merge command-line overrides on top. | Small targeted changes (e.g. license key, single image bump). |
| `--reset-values` | Discard last release's values; back to chart defaults plus this command's overrides. | Recovering from a values-file mistake. |
| `--reset-then-reuse-values` | Reset to chart defaults, replay last release's values, then merge overrides. | Picking up new chart-default keys in a new chart version while keeping operator overrides. |

`--reset-then-reuse-values` is the closest to a "values-aware
upgrade": it lets new chart defaults take effect for keys you've
never explicitly set, while keeping every key you have overridden.
Prefer it when bumping across chart versions that introduce new
defaults.

### 5.3 Rollback

`helm history` lists every revision the release has been through
(default 10 revisions retained):

```bash
helm history meho
```

Roll back to a previous revision by number, or omit the argument to
go back exactly one:

```bash
helm rollback meho                 # previous revision
helm rollback meho 7 --wait        # specific revision
```

Rollback re-renders the manifest set from the earlier revision's
stored values and applies it. It restores Deployment, Service,
Ingress, and Secret state; it does **not** roll back database
schemas (Alembic migrations are forward-only) or external state
(operator-managed Secret values, cert-manager certificates, PVC
contents). If a release introduced a backwards-incompatible
migration you cannot roll back via Helm alone — restore the
database from a backup taken before the upgrade, then `helm
rollback`.

### 5.4 Image-tag-only changes

Bumping just the backend image tag (no chart change) does not need
a values-file edit:

```bash
helm upgrade meho deploy/helm/meho \
  --reuse-values \
  --set image.tag=0.1.1 \
  --wait
```

This re-renders the Deployment with the new image, K8s rolls the
pods, and every other value is preserved.

## 6. Troubleshooting

Most problems surface as one of: pods that won't reach `Ready`,
Ingress that returns 502, the SPA loading but failing all backend
calls, or `helm install` refusing to render. Each section below
opens with the symptom you'll see, names the diagnostic command,
and ends with the fix.

### 6.1 Readiness probe design

Both backend probes (`liveness` and `readiness`) target `/health`
on purpose. `/health` is a zero-I/O endpoint that returns 200 as
soon as the FastAPI app is accepting requests; it does **not**
verify Postgres, Redis, or Keycloak connectivity. This is
deliberate: routing readiness to an endpoint that calls into
shared dependencies (a `/ready` style probe) would drop every
backend pod from the Service simultaneously when one of those
dependencies hiccupped, causing a cascading outage. With `/health`,
a Postgres blip fails inside individual requests but does not pull
the entire backend out of rotation.

If you want dependency-aware readiness for an internal SLO, fork
the chart and switch the readiness probe path; do not switch the
liveness probe path or you will reduce a transient connectivity
issue into a `CrashLoopBackOff` storm.

### 6.2 Pods stuck in `Pending`

Symptom: `kubectl get pods` shows `meho-backend-...` pinned at
`Pending` for >2 minutes.

```bash
kubectl -n meho describe pod -l app.kubernetes.io/component=backend
```

Common causes:

- **Insufficient resources.** The `Events:` block at the bottom
  shows `0/N nodes are available: ... insufficient cpu` or
  `insufficient memory`. The chart's production defaults
  (1 CPU / 2 Gi requested, 2 replicas) need at least one node with
  that capacity free. Either scale the cluster or shrink
  `resources.backend.requests` (see [§8 Sizing](#8-sizing-recommendations)).
- **No matching node selectors / taints.** If you've added
  `nodeSelector` / `tolerations` overrides, the message will say
  `node(s) didn't match Pod's node affinity/selector`. Verify the
  selector keys match your node labels.
- **PVC pending.** Embedded Postgres pulls a PVC; on a cluster
  without a default StorageClass it sits at `Pending` waiting for
  manual binding. `kubectl get pvc -n meho` shows the unbound
  claim. Set `embeddedpostgres.primary.persistence.storageClass` to
  an existing class, or disable persistence in evaluator mode.

### 6.3 Backend pod in `CrashLoopBackOff`, readiness fails

Symptom: backend pod restarts repeatedly; `kubectl get pods` shows
`Ready 0/1` and a climbing `RESTARTS` count.

```bash
kubectl -n meho logs deployment/meho-backend --tail=100
kubectl -n meho logs deployment/meho-backend --previous   # the pod that just crashed
```

Common causes (the log line tells you which):

- **Postgres unreachable.** `asyncpg` raises `ConnectionRefusedError`
  or `OSError: [Errno -2] Name or service not known`. Verify
  `postgres.external.dsn` value and that the cluster can reach the
  Postgres host: `kubectl run -n meho --rm -it pg-debug --image=postgres:16 -- psql "$DSN" -c '\l'`.
- **Migrations failed.** The log shows
  `alembic.util.exc.CommandError` or a SQL error. Check Postgres
  permissions — the user in the DSN needs `CREATE` on the database.
  If you upgraded across a migration boundary that introduced a
  destructive change, restore from backup before retrying.
- **Encryption key too short.** The backend startup logs
  `CREDENTIAL_ENCRYPTION_KEY must be at least 32 characters` and
  exits non-zero. Update the source-of-truth value in your secret
  store, wait for ESO to refresh, then `kubectl rollout restart
  deployment/meho-backend`.
- **License key invalid.** This is *not* a crash — the backend logs
  a warning and falls back to community edition, but the pod
  becomes Ready. If you expected enterprise mode, grep the logs for
  `license` (Section 4.1).

### 6.4 Ingress returns 502 / 503

Symptom: visiting the configured `ingress.host` returns 502 Bad
Gateway or 503 Service Unavailable from the Ingress controller.

```bash
kubectl -n meho get pods,svc,ingress
kubectl -n meho describe ingress meho-frontend
```

Common causes:

- **Frontend pod not ready.** The Ingress controller routes only to
  Endpoints whose backing pods pass the readiness probe. If
  `kubectl get pods` shows `meho-frontend-... Ready 0/1`, the
  502 is upstream-truthful — fix the frontend pod first
  ([§6.5](#65-frontend-pod-ready-but-spa-shows-no-data)).
- **Wrong `ingressClassName`.** `kubectl describe ingress
  meho-frontend` shows the configured class. If your cluster's
  Ingress controller serves `traefik` but the chart was installed
  with `ingress.className=nginx`, no controller picks the resource
  up. `helm upgrade --reuse-values --set
  ingress.className=traefik` to fix.
- **TLS Secret missing.** `kubectl describe ingress` shows
  `secretName: meho-tls` but `kubectl get secret meho-tls` returns
  `NotFound`. Ingress controllers behave differently here — nginx
  serves a self-signed cert with a warning; some controllers refuse
  to admit the resource. Issue the certificate (cert-manager) or
  remove `ingress.tls` for an HTTP-only test.

### 6.5 Frontend pod Ready but SPA shows no data

Symptom: SPA loads in the browser; every API call fails (network
errors in DevTools, "Failed to fetch" toasts). DevTools shows
requests going to the wrong host or being blocked by CORS.

```bash
kubectl -n meho exec deployment/meho-frontend -- cat /usr/share/nginx/html/config.js
```

Common causes:

- **`API_URL` empty or wrong.** The rendered `config.js` shows the
  effective values. If it reads
  `window.MEHO_CONFIG = { API_URL: "", ... }` the chart's `required`
  check did not catch the missing value (this should be impossible
  for `frontend.apiUrl` — but it is for any field that is *not*
  `required`-validated). Update `frontend.apiUrl` and roll the
  frontend.
- **`envsubst` placeholders visible.** The file shows literal
  `${API_URL}` strings. The image's entrypoint failed to run, or
  the template variable name in the image differs from the chart's.
  This is an image bug, not a chart bug — file an issue with the
  rendered `config.js` attached. (Detection of this footgun is
  tracked under [Task #535](https://github.com/evoila/meho/issues/535).)
- **CORS rejection.** Browser console shows
  `Access-Control-Allow-Origin` errors. The backend's CORS allow-list
  comes from the `CORS_ORIGINS` env var on the backend pod, not from
  `frontend.allowedOrigins` (which is the *nginx* allow-list). The
  chart does not template `CORS_ORIGINS` onto the backend today;
  set it via your operator-managed Secret or as a same-named entry,
  noting that the chart's explicit `env:` entries override
  `envFrom` for `DATABASE_URL` / `REDIS_URL` only.

### 6.6 `helm install` fails before any pod starts

Symptom: `helm install` exits non-zero with one of these messages:

| Error from `helm install` | Cause | Fix |
|---|---|---|
| `frontend.apiUrl is required` | `frontend.apiUrl` empty in values | Set the browser-reachable backend URL. |
| `frontend.keycloakUrl is required` | `frontend.keycloakUrl` empty | Set the browser-reachable Keycloak URL. |
| `ingress.host is required when ingress.enabled=true` | Placeholder hostname not replaced | Set to your real hostname or pass `--set ingress.enabled=false`. |
| `secrets.jwtSecretKey is required when secrets.create=true` | Chart-managed Secret path with empty key | Provide the key, or switch to `secrets.existingSecret=<name>` and `secrets.create=false`. |
| `secrets.credentialEncryptionKey must be at least 32 characters` | Chart-managed key shorter than 32 chars | Generate a longer key. The backend enforces the same minimum at startup. |
| `postgres.external.dsn must be set when embedded.enabled is false` | No DSN given and embedded mode off | Either set the DSN via `--set-string postgres.external.dsn=...` or switch to `embedded.enabled=true`. |
| `redis.external.url must be set when embedded.enabled is false` | No Redis URL given and embedded mode off | Set the URL or switch to embedded. |
| `found in Chart.yaml, but missing in charts/ directory` | Embedded subcharts not pulled | Run `helm dependency update deploy/helm/meho` once before installing. |

These errors fail at template time (before any K8s object is
created), so they are safe to iterate on.

### 6.7 `helm install` succeeds in embedded mode, backend can't reach Postgres

Symptom: chart installs cleanly with `embedded.enabled=true`, but
the backend pod logs `password authentication failed` or `database
"meho" does not exist`.

```bash
kubectl -n meho logs deployment/meho-backend | grep -i postgres
kubectl -n meho get secret meho-embeddedpostgres -o jsonpath='{.data.password}' | base64 -d
```

Common cause: the env-var ordering invariant in the backend
Deployment was disturbed (only an issue if you forked the chart).
The chart relies on `POSTGRES_PASSWORD` being declared in the env
list **before** `DATABASE_URL`, because `DATABASE_URL` interpolates
`$(POSTGRES_PASSWORD)` and Kubernetes substitutes only from earlier
entries. If the order is reversed, `$(POSTGRES_PASSWORD)` is
substituted with an empty string. Restore the chart's order
(`POSTGRES_PASSWORD` first) or pull the password value into a
separate Secret and reference it directly.

### 6.8 Diagnostic command quick reference

```bash
# Cluster-wide chart inventory
helm list -A

# Effective values for a release (post-merge)
helm get values meho                  # operator overrides only
helm get values meho --all            # full effective set including chart defaults

# Manifest as Helm rendered it
helm get manifest meho

# Current revision history
helm history meho

# Realtime backend logs across all replicas
kubectl -n meho logs -l app.kubernetes.io/component=backend -f --tail=100

# Shell into a backend pod (requires the slim image to have sh/bash)
kubectl -n meho exec -it deployment/meho-backend -- /bin/sh

# Verify the in-pod env (sanitised — secrets show as literal values)
kubectl -n meho exec deployment/meho-backend -- env | grep -E '^(DATABASE_URL|REDIS_URL|ENV)='
```

`helm get values --all` is the fastest way to see what the chart
believes is true; pair it with `helm get manifest` to confirm the
rendered output matches.

## 7. Observability hooks

The backend emits OpenTelemetry traces via OTLP and structured logs
to stdout. Both are off-by-default in the chart — the chart does
not bundle a collector and expects operators to plug in their own.

### 7.1 OTLP trace export

Set `OTEL_EXPORTER_OTLP_ENDPOINT` on the backend Deployment via the
External Secrets Operator path (Section 3.1) by adding the entry to
your `ExternalSecret`'s `data` block, or as a same-named entry in a
hand-rolled Secret:

```yaml
data:
  - secretKey: OTEL_EXPORTER_OTLP_ENDPOINT
    remoteRef:
      key: meho-otlp-endpoint     # base URL only — e.g. http://otel-collector.observability:4318
```

The value is a **base URL**: the backend's exporter
([`meho_app/core/otel/exporters.py`](https://github.com/evoila/meho/blob/main/meho_app/core/otel/exporters.py))
appends `/v1/traces` and `/v1/logs` itself. Setting the value to a
URL that already ends in `/v1/traces` produces a duplicated path
(`…/v1/traces/v1/traces`) and the exporter fails silently — only the
path-suffix-bearing variant breaks; bare `host:port` works.

Roll the backend after the projection refreshes:

```bash
kubectl -n meho rollout restart deployment/meho-backend
```

Sample OTLP collectors that interoperate: Jaeger, Grafana Tempo,
Datadog Agent, New Relic, OpenTelemetry Collector. MEHO ships only
the **HTTP-OTLP** exporter today (port 4318 by convention); the
gRPC-OTLP exporter (port 4317) would require a code change to swap
in `OTLPSpanExporter` from
`opentelemetry.exporter.otlp.proto.grpc.trace_exporter`. The
[OTel spec](https://opentelemetry.io/docs/specs/otel/protocol/exporter/)
selects transport via `OTEL_EXPORTER_OTLP_PROTOCOL` (`grpc` /
`http/protobuf` / `http/json`), not via a URL scheme — there is no
`grpc://` prefix convention.

For high-volume installs, set `OTEL_TRACE_LEVEL=summary` (default
`full`) to reduce per-trace payload size. The backend honours this
via [meho_app/core/config.py](https://github.com/evoila/meho/blob/main/meho_app/core/config.py).

### 7.2 Logs

The backend logs to stdout in JSON format (one event per line);
nginx in the frontend pod logs to stdout in combined format. Both
are picked up by any log shipper that reads container stdout —
Fluent Bit, Vector, Loki's Promtail, Datadog Agent, etc.

```bash
# Tail backend logs across all replicas
kubectl -n meho logs -l app.kubernetes.io/component=backend -f --tail=100

# Tail frontend (nginx access + error)
kubectl -n meho logs -l app.kubernetes.io/component=frontend -f --tail=100

# Search for license-related events on a specific pod
kubectl -n meho logs <pod> --since=10m | grep -i license
```

Set `MEHO_LOG_LEVEL=WARNING` (default `INFO`) on the backend Secret
to reduce log volume in production.

### 7.3 Metrics

The chart does not expose Prometheus scrape annotations today.
Operators wanting metrics surface them via the standard
`prometheus.io/scrape` annotations on a forked Service template, or
via a `ServiceMonitor` resource (Prometheus Operator) targeting the
backend Service's `http` port. This is intentionally out of scope
for v0.1.0; the backend exposes no `/metrics` endpoint yet.

## 8. Sizing recommendations

These values are intentionally conservative — they cover the
"single-tenant single-user" and "small team" footprints MEHO targets
at v0.1.0. Workloads heavier than ~10 active investigations per
hour, or connector pools larger than ~30 typed connectors, should
benchmark before scaling these numbers up.

### 8.1 Single-tenant single-user (evaluation, dev cluster)

Suitable for evaluators, internal demo clusters, kind / minikube.

| Component | Replicas | CPU request | CPU limit | Memory request | Memory limit |
|---|---|---|---|---|---|
| Backend (slim) | 1 | 500m | 2000m | 1 Gi | 4 Gi |
| Frontend | 1 | 50m | 200m | 64 Mi | 256 Mi |

These match the chart's defaults in [`values.yaml`](https://github.com/evoila/meho/blob/main/deploy/helm/meho/values.yaml).
Memory limits at 4 Gi accommodate the slim backend's typical
working set; the full image (with PyTorch / Docling) needs 6-8 Gi
for memory-heavy PDF ingestion.

### 8.2 Small team (~10 users, ~30 connectors)

Suitable for one-team production deployments with active connectors
and ongoing investigations.

| Component | Replicas | CPU request | CPU limit | Memory request | Memory limit |
|---|---|---|---|---|---|
| Backend (full) | 2 | 1000m | 4000m | 2 Gi | 8 Gi |
| Frontend | 2 | 100m | 500m | 128 Mi | 512 Mi |

These match the shipped [`values-prod.yaml`](https://github.com/evoila/meho/blob/main/deploy/helm/meho/values-prod.yaml)
defaults and assume the full backend image. The two-replica pattern
gives the rolling-update strategy room to surge without making the
deployment unavailable; single-replica installs see brief
unavailability during every upgrade.

### 8.3 External services

The chart is silent on managed Postgres / Redis sizing — those are
operator-owned. Reasonable starting points:

| Service | Purpose | Starting size |
|---|---|---|
| Postgres 16 | primary application DB + pgvector | 2 vCPU / 4 Gi RAM / 50 Gi storage |
| Redis 7 | cache + BM25 indices + approval signalling | 1 vCPU / 2 Gi RAM, no persistence required |

Bump Postgres ahead of Redis as connector counts grow (the topology
graph and knowledge embeddings live in Postgres). The connector
credential cache, session memory, and approval state live in Redis;
its working set scales with concurrent users, not connector count.

### 8.4 What to watch when scaling up

- **Backend memory.** PDF ingestion via the full image's Docling
  path peaks at 6-22 Gi during processing. Set `memory: limit` on
  the backend with headroom for at least one in-flight ingestion
  per replica, or split ingestion to the
  [ephemeral worker](../deployment.md#ephemeral-ingestion-worker)
  path.
- **Postgres connections.** Each backend replica opens a small
  asyncpg pool (`pool_min=5, pool_max=20`). At 10 replicas that's
  up to 200 connections — well under managed-Postgres defaults but
  worth checking against your tier's connection limit.
- **Frontend replicas.** Static SPA, near-zero CPU. Two replicas is
  for HA, not throughput; do not scale unless you have a specific
  reason (e.g. cluster-wide Pod Disruption Budget compliance).

## 9. Going further

- [`deploy/helm/meho/README.md`](https://github.com/evoila/meho/blob/main/deploy/helm/meho/README.md)
  — chart-level value reference and the canonical ExternalSecret
  example.
- [Deployment (Docker Compose)](../deployment.md) — sister document
  for the development / single-host install path.
- [Security & Data Handling](../security.md) — image provenance,
  cosign verification, supply-chain context.
- [External Secrets Operator docs](https://external-secrets.io) —
  full SecretStore / ExternalSecret reference.
- [Helm 3 docs](https://helm.sh/docs/) — install, upgrade,
  rollback semantics in detail.
