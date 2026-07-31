# Local kind quickstart

Bring the backplane up on your workstation in a
[kind](https://kind.sigs.k8s.io/) cluster in a few minutes — to see
the install plumbing work, poke `/healthz`, and iterate on values.

!!! warning "Placeholder authentication — `meho login` will not work here"

    This quickstart runs **no real Keycloak and no real credential
    backend**. The chart is pointed at placeholder URIs so its
    URI-validated fields resolve at install time; a throwaway
    in-cluster PostgreSQL is the only real dependency. The backplane
    boots, migrations run, health endpoints answer — but operator
    identity is faked: **`meho login` will not complete end-to-end**,
    and nothing credential-federated works. For a functional deploy,
    follow [the install trail](index.md).

## What this exercises (and what it cannot)

| Works here | Does not work here |
|---|---|
| Chart install / upgrade plumbing, values schema validation | `meho login`, any authenticated CLI or API call |
| Pre-install migration Job against a real PostgreSQL | Just-in-time credentials (no Vault / Google Secret Manager) |
| Startup contract: catalog registration, embedding model preload | Agent (MCP) sessions — no tokens can be issued |
| `/healthz`, Pod lifecycle, probe behaviour | `/ready` fully green — the identity checks have nothing real to check |

## Run it

```bash
# 1. A single-node kind cluster.
kind create cluster --name meho-dev

# 2. The mock prerequisites. values-kind.yaml documents them at the top
#    of the file: a copy-paste Namespace + Secret + Deployment + Service
#    manifest for a mock in-cluster PostgreSQL. Vault and Keycloak are
#    placeholder URIs only — nothing to deploy for them.
#    → https://github.com/evoila/meho/blob/main/deploy/values-examples/values-kind.yaml
#    One substitution when you paste it: use image pgvector/pgvector:pg16
#    (not postgres:16-alpine) — MEHO's migrations enable the pgvector
#    extension, which the stock alpine image does not ship.

# 3. Install the chart from its OCI artefact, pinning an immutable
#    image tag (a release tag, or sha-<git-sha> from a green CI run).
#    The overlay turns ingress off, so the chart has no hostname to
#    derive the /mcp audience from and refuses to render without one.
#    The placeholder below satisfies that guard; /mcp stays fail-closed.
helm install meho-dev oci://ghcr.io/evoila/meho-chart \
  --version <chart-version> \
  -n meho --create-namespace \
  -f https://raw.githubusercontent.com/evoila/meho/main/deploy/values-examples/values-kind.yaml \
  --set image.tag=<immutable-tag> \
  --set config.backplaneUrl=http://localhost:8000

# 4. Watch it come up and poke it.
kubectl wait --for=condition=Ready pod \
  -l app.kubernetes.io/name=meho -n meho --timeout=6m
kubectl port-forward -n meho svc/meho-dev 8000:8000 &
curl localhost:8000/healthz
```

The overlay disables ingress and NetworkPolicy (kind ships neither by
default) and points the chart at the mock endpoints. The pgvector
prerequisite from the install trail is satisfied differently here: the
`pgvector/pgvector:pg16` image ships the extension, and the mock's
database user is a superuser, so the migration Job's
`CREATE EXTENSION` succeeds on its own. If the migration Job instead
fails with `extension "vector" is not available`, the mock is running
a stock PostgreSQL image — swap it as noted in step 2.

## When you outgrow it

The moment you want a real login, real credentials, or an agent
session, you have outgrown the quickstart — go to
[the install trail](index.md). Nothing from the quickstart carries
over; treat it as disposable (`kind delete cluster --name meho-dev`).
