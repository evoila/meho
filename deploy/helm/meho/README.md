# MEHO Helm chart

[MEHO](https://meho.ai) is an AI-powered diagnostic and operations
platform. This chart deploys MEHO on a Kubernetes cluster — the
backend (full or slim variant), frontend SPA, and the supporting
services (Postgres, Redis) MEHO depends on.

> **Status — `0.1.0` skeleton.** This release ships chart metadata
> and the values contract only. Backend, frontend, and Secret
> templates land in subsequent chart releases. `helm install`
> against this version produces no resources yet. Use the SemVer
> 0.x channel — anything may change between minor versions.
>
> **Requires Helm 3 or later.** The chart declares
> `apiVersion: v2`.

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

For laptops, kind / minikube clusters, and quick demos. The chart
pulls in Bitnami's `postgresql` and `redis` subcharts when
`embedded.enabled=true`.

```bash
helm install meho ./deploy/helm/meho \
  --values ./deploy/helm/meho/values-dev.yaml \
  --set image.tag=0.1.0
```

The bundled subcharts have no production-grade backup, monitoring,
or upgrade story. Treat the evaluator install as ephemeral.

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

## Operator runbook

A full install / upgrade / troubleshoot runbook lands in a future
release at `docs/deployment/kubernetes.md`. Until then, inspect
`values.yaml`, the install commands above, and
[`docs/codebase/release-and-deployment.md`](../../../docs/codebase/release-and-deployment.md)
for the supporting context.

## License

AGPL-3.0-only. See the repo-level `LICENSE` file.
