# MEHO Helm chart

[MEHO](https://meho.ai) is an AI-powered diagnostic and operations
platform. This chart deploys MEHO on a Kubernetes cluster — the
backend (full or slim variant), frontend SPA, and the supporting
services (Postgres, Redis) MEHO depends on.

> **Status — `0.1.0` partial.** This release ships the backend
> Deployment + Service, the frontend Deployment + Service + optional
> Ingress, and optional embedded Postgres + Redis subcharts. The
> chart's own backend Secret template (#528), the helm-test CI
> workflow (#529), and the operator runbook (#530) are still pending
> under Initiative #506. Until #528 lands, every install needs a
> pre-existing backend Secret resolved from `meho.backend.secretName`
> (see `values.yaml: backend.existingSecret`) carrying the runtime
> credentials, otherwise backend pods stay in
> `CreateContainerConfigError`. Use the SemVer 0.x channel — anything
> may change between minor versions.
>
> **Requires Helm 3 or later.** The chart declares
> `apiVersion: v2`.
>
> **Required values.** `frontend.apiUrl`, `frontend.keycloakUrl`,
> and (when `ingress.enabled=true`) `ingress.host` use Helm's
> `required` function — bare `helm template` / `helm lint` / `helm
> install` against `values.yaml` alone fails with a clear error
> until those are set. Use one of the starter overlays
> (`values-dev.yaml` or `values-prod.yaml`) or pass `--set` flags
> for those keys.

## Install paths

This chart supports two install paths: **production** (operator
provides Postgres and Redis) and **evaluator** (the chart bundles
Postgres + Redis subcharts). The production path is the default.
Argo CD, Grafana, Sentry, and Keycloak ship with the same split.

### Production — external managed services

Recommended for any non-laptop deployment. The operator points the
chart at managed Postgres (RDS, Cloud SQL, Aiven, ...) and managed
Redis (ElastiCache, Memorystore, Upstash, ...). The chart never
manages production data.

```bash
helm install meho ./deploy/helm/meho \
  --values ./deploy/helm/meho/values-prod.yaml \
  --set image.tag=0.1.0 \
  --set-string postgres.external.dsn='postgresql+asyncpg://user:pass@host:5432/meho' \
  --set-string redis.external.url='redis://host:6379/0'
```

`--set-string` (rather than `--set`) avoids Helm's value-parser
splitting on commas / equals inside connection strings. For real
production, do not pass credentials on the command line at all —
wire them through a `Secret` or an External Secrets Operator
integration.

### Evaluator — embedded Postgres + Redis

> **Embedded mode is for evaluators only.** Production deployments
> must always use external managed Postgres and Redis. The bundled
> Bitnami subcharts have no production-grade backups, monitoring, or
> version-aware upgrade story. To switch from embedded to external
> later, run `helm upgrade --set embedded.enabled=false --set-string
> postgres.external.dsn=... --set-string redis.external.url=...`
> (both DSN/URL are required when `embedded.enabled=false` — `helm`
> will refuse to upgrade with a missing-value error otherwise) and
> provide your own data-migration story.

For laptops, kind / minikube clusters, and quick demos. The chart
pulls in Bitnami's `postgresql` and `redis` subcharts when
`embedded.enabled=true`. The evaluator overlay (`values-dev.yaml`)
ships deterministic dev passwords so the install works with one
command:

```bash
helm dependency update ./deploy/helm/meho
helm install meho ./deploy/helm/meho \
  --values ./deploy/helm/meho/values-dev.yaml \
  --set image.tag=0.1.0
```

`helm dependency update` populates `deploy/helm/meho/charts/` with
the pinned `postgresql` and `redis` `.tgz` archives. Those archives
are not committed — run the command on a fresh checkout.

Treat the evaluator install as ephemeral. There is no upgrade path
across chart-version-pinned Postgres major bumps; switch to external
managed Postgres before any data you care about.

## Versioning

| Field | Source | Bumped when |
|---|---|---|
| `Chart.yaml: version` | this chart | the chart itself changes (template fix, value rename, dependency bump) |
| `Chart.yaml: appVersion` | the MEHO release | a new MEHO image is published |
| `values.yaml: image.tag` | operator override | leave empty to default to `appVersion` |

The two versions move independently. Pin `image.tag` to a specific
MEHO release (`0.1.0`, not `latest`) in production.

## Values

`values.yaml` is the public contract of the chart. The starter
overlays — `values-dev.yaml` and `values-prod.yaml` — show typical
overrides. Read those files for the documented keys; this README
deliberately does not duplicate the value docs.

## Frontend runtime configuration

The frontend image fetches `/config.js` at boot — a runtime-config
asset rendered by the container's entrypoint via envsubst from a
template baked into the image. The chart drives that rendering by
passing the SPA's public configuration as container env vars:

| Values key | Container env var | Purpose |
|---|---|---|
| `frontend.apiUrl` | `API_URL` | backend HTTP base URL |
| `frontend.keycloakUrl` | `KEYCLOAK_URL` | Keycloak server URL |
| `frontend.keycloakRealm` | `KEYCLOAK_REALM` | Keycloak realm |
| `frontend.keycloakClientId` | `KEYCLOAK_CLIENT_ID` | Keycloak SPA client ID (public) |
| `frontend.allowedOrigins` | `ALLOWED_ORIGINS` | CORS allow-list for nginx |
| `frontend.keycloakOrigin` | `KEYCLOAK_ORIGIN` | Keycloak origin appended to nginx CORS |

These values ship to every browser — never put secrets here.

The frontend `readinessProbe` targets `/config.js` rather than `/`:
it confirms nginx is up *and* that the runtime-config asset is
actually being served (proves the entrypoint's envsubst step
finished writing the file). It does **not** detect missing or
empty env vars — `envsubst` substitutes unset variables with
empty strings and exits 0, so an unconfigured pod still serves
`/config.js` (just with empty values, breaking the SPA at
runtime). The chart's defence against that footgun is Helm's
`required` function on `frontend.apiUrl` and `frontend.keycloakUrl`
— `helm install` fails before any pod starts when those keys are
empty. Detection of partially-rendered placeholders inside the
served file is a Docker `HEALTHCHECK` concern owned by Task #535.

## Ingress

The chart renders an `Ingress` only when `ingress.enabled=true`.
Operators bringing their own LoadBalancer or service-mesh gateway
should set `ingress.enabled=false` and route traffic directly to
the frontend Service. The chart does not issue TLS certificates —
integrate cert-manager (or similar) out-of-band and reference the
resulting Secret via `ingress.tls[].secretName`.

## Operator runbook

A full install / upgrade / troubleshoot runbook lands in a future
release at `docs/deployment/kubernetes.md`. Until then, inspect
`values.yaml`, the install commands above, and
[`docs/codebase/release-and-deployment.md`](../../../docs/codebase/release-and-deployment.md)
for the supporting context.

## License

AGPL-3.0-only. See the repo-level `LICENSE` file.
