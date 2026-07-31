# The install trail

This page is one continuous path from a bare Kubernetes cluster to a
running MEHO backplane and a successful `meho login`. Follow it top to
bottom; every side quest (choosing a credential backend, configuring
the Keycloak realm, TLS trust) is linked at the exact step where you
need it, and each of those pages sends you back here.

!!! tip "Just exploring?"

    If you only want to see the backplane come up on your workstation —
    without a real Keycloak, Vault, or login — take the
    [local kind quickstart](kind-quickstart.md) instead. It is faster,
    and it is honest about what it fakes.

## What you need

| You bring | Requirements |
|---|---|
| **Kubernetes cluster** | Any conformant cluster with an ingress controller (the examples assume ingress-nginx). [Helm](https://helm.sh/) 3.8+ on your workstation; Helm 4 recommended — the [upgrades](upgrades.md) page's server-side-apply guidance assumes it. |
| **PostgreSQL** | A reachable PostgreSQL server with the [pgvector](https://github.com/pgvector/pgvector) extension *available* (installed as a server package — Step 1 covers enabling it). MEHO does not bundle a database. |
| **Keycloak** | A [Keycloak](https://www.keycloak.org/) you administer, with a realm for MEHO (an existing realm works). Step 4 covers the realm configuration. |
| **Credential backend** | HashiCorp Vault **or** access to Google Secret Manager. Step 2 helps you choose. |
| **A DNS name + TLS story** | A hostname for the backplane (e.g. `meho.example.com`) and a certificate for it — public CA or internal CA both work; [TLS and ingress](tls-ingress.md) covers the differences. |

Everything else — the activity-feed store (Valkey), database
migrations, the operations catalog — ships inside the Helm release.

## Step 1 — Pre-create the pgvector extension

MEHO's semantic search runs on pgvector, and the chart's pre-install
migration Job executes `CREATE EXTENSION IF NOT EXISTS vector` on
first boot. In stock PostgreSQL, `CREATE EXTENSION` requires a
**superuser** — and the database role you give MEHO should *not* be
one. So a cold install against a least-privilege role fails with:

```text
permission denied to create extension "vector"
HINT:  Must be superuser to create this extension.
```

The fix is a one-time, idempotent command run **as a superuser**
against MEHO's database, before the first install:

```bash
psql -d meho -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

On [CloudNativePG](https://cloudnative-pg.io/), exec into the primary
Pod (its `postgres` container runs as the bootstrap superuser):

```bash
kubectl exec <cluster>-1 -n <namespace> -c postgres -- \
  psql -d meho -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

or declare it at cluster-bootstrap time so it survives re-provisioning:

```yaml
spec:
  bootstrap:
    initdb:
      postInitSQL:
        - "CREATE EXTENSION IF NOT EXISTS vector;"
```

If the extension is already present (many managed PostgreSQL services
pre-enable it, and some run migrations as a privileged role), this
step is a no-op — run it anyway; it is safe.

!!! note "Why doesn't the chart automate this?"

    Deliberately. Automating it would require shipping a superuser
    credential to the migration Job, which is a worse trade than a
    one-time operator command. The full rationale is recorded in
    [`docs/decisions/pgvector-superuser-prerequisite.md`](https://github.com/evoila/meho/blob/main/docs/decisions/pgvector-superuser-prerequisite.md).

Verify:

```bash
psql -d meho -c "SELECT extname FROM pg_extension WHERE extname='vector';"
```

## Step 2 — Choose your credential backend

MEHO fetches target credentials (the vCenter password, the appliance
API key) from a secret store at operation time — that store is the
**credential backend**, and the chart supports two: HashiCorp
**Vault** (the default) and **Google Secret Manager**.

**Read the [credential backends](credential-backends.md) decision page
now** — it states what each backend requires and an honest support
matrix of what works on each today. Come back with a decision; Step 6
shows the values for both.

## Step 3 — Provision the database credentials Secret

The chart never embeds credentials in values. It references a
Kubernetes Secret **by name**; the Secret must hold the full database
DSN at the key `url`, using the **async** driver prefix:

```bash
kubectl create namespace meho
kubectl create secret generic meho-postgres -n meho \
  --from-literal=url='postgresql+asyncpg://<user>:<password>@<host>:5432/meho'
```

Two things bite here if missed:

- The prefix must be `postgresql+asyncpg://`. A bare `postgresql://`
  DSN selects the synchronous driver and fails at connect time.
- The key must be `url` — the Deployment reads `DATABASE_URL` from
  exactly that key.

In production you will usually not `kubectl create` this by hand:
the recommended pattern is syncing it from your secret store with
[External Secrets Operator](https://external-secrets.io/). The
manifests and the chart's optional ESO integration are documented in
the [values-examples deep-dive](https://github.com/evoila/meho/blob/main/deploy/values-examples/README.md)
— the trail continues the same either way, as long as the Secret
exists before you install.

## Step 4 — Set up the Keycloak realm

MEHO authenticates every caller against your Keycloak. Before the
backplane can be logged into, the realm needs:

1. the **confidential `meho-backplane` client** — the resource-server
   identity MEHO validates tokens against;
2. a **public `meho-cli` client** with the device-code grant — what
   `meho login` uses;
3. a **public `meho-mcp-client`** with authorization-code + PKCE —
   what browser-capable MCP clients use;
4. five **protocol mappers** and four **default client scopes** on the
   public clients, so issued tokens carry the claims MEHO validates;
5. at least one **user** in the `meho-admins` group.

**Follow [Keycloak realm setup](keycloak-realm.md) now** — it is the
step-by-step recipe, including the non-obvious Keycloak 25+ `basic`
scope gotcha that otherwise costs hours. Come back when the realm
verification block at the end of that page passes.

## Step 5 — Decide ingress, TLS, and trust

The backplane serves one hostname (set as `ingress.host`), and that
hostname does double duty: the chart **derives the MCP audience** —
the identifier every agent token must be issued for — from it. Getting
TLS trust right matters in three distinct places (the browser, the
backplane's own outbound connections, and your workstation).

**Read [TLS and ingress](tls-ingress.md)** — especially if your
Keycloak, Vault, or PostgreSQL present certificates from an internal
CA. If everything in your environment uses publicly-trusted
certificates, the defaults in Step 6 are all you need.

## Step 6 — Write your values file

Create `values.yaml`. The chart ships a typed schema: every field the
backplane cannot start without is blank by default and **rejected at
install time** with the exact failing field path — so a misconfigured
release fails at `helm install`, not as a crash-looping Pod at 2 a.m.

A minimal Vault-backed file:

```yaml
image:
  # Pin an immutable tag: v<x.y.z> for a release (see
  # https://github.com/evoila/meho/releases), or sha-<git-sha> from CI.
  # :latest is never published; :main is a dev-only moving alias.
  tag: "<REPLACE: v0.25.0 or later>"

ingress:
  enabled: true
  className: nginx
  host: meho.example.com          # drives the MCP audience — see Step 5
  tls:
    enabled: true
    secretName: meho-tls

config:
  keycloakIssuerUrl: "https://keycloak.example.com/realms/<realm>"
  keycloakAudience: meho-backplane
  keycloakCliClientId: meho-cli   # the public client from Step 4
  vaultAddr: https://vault.example.com

postgres:
  host: postgres.example.com
  port: 5432
  database: meho
  credentialsSecret: meho-postgres   # the Secret from Step 3

vault:
  address: https://vault.example.com
  authMethod: oidc
  role: meho-mcp                  # Vault JWT role bound to the realm above
  paths:
    kv: secret/meho

keycloak:
  issuer: "https://keycloak.example.com/realms/<realm>"
  audience: meho-backplane

networkPolicy:
  enabled: true
  ingressControllerNamespace: ingress-nginx
  # Egress allow-list: the CIDRs your Postgres / Vault / Keycloak
  # actually resolve to. Recover with:
  #   kubectl get endpoints <svc> -n <ns> -o jsonpath='{.subsets[].addresses[].ip}'
  postgresCIDR: "10.0.1.0/24"
  vaultCIDR: "10.0.2.0/24"
  keycloakCIDR: "10.0.3.0/24"
```

For a **Google Secret Manager** deploy, replace the `vault` block and
`config.vaultAddr` with the GSM fields — the exact delta is on the
[credential backends](credential-backends.md#helm-values-per-backend)
page.

Notes:

- `config.keycloakIssuerUrl` / `config.vaultAddr` mirror
  `keycloak.issuer` / `vault.address` — the config block is what the
  backplane process reads; the two must agree.
- If your Vault, Keycloak, or PostgreSQL use internal-CA certificates,
  add the trust-bundle `extraVolumes` / `extraEnv` block from
  [TLS and ingress](tls-ingress.md#the-backplanes-own-trust-internal-ca-bundle).
- The full annotated values reference lives in the
  [example values files](https://github.com/evoila/meho/tree/main/deploy/values-examples)
  and the chart's own
  [`values.yaml`](https://github.com/evoila/meho/blob/main/deploy/charts/meho/values.yaml).

## Step 7 — Install the chart

The chart is published as an OCI artefact. Install:

```bash
helm upgrade --install meho oci://ghcr.io/evoila/meho-chart \
  --namespace meho --create-namespace \
  -f values.yaml
```

Pin `--version <chart-version>` in production (discover versions with
`helm show chart oci://ghcr.io/evoila/meho-chart`); without it Helm
uses the latest published chart.

What happens, in order:

1. A **pre-install migration Job** runs all database migrations before
   any application Pod exists. If a migration fails, the release fails
   — an unmigrated Pod never takes traffic. Failed Jobs stick around
   for ten minutes, so `kubectl logs job/<name> -n meho` shows why.
   This is where Step 1's pgvector prerequisite is enforced.
2. The Deployment rolls out. First boot registers the full operations
   catalog and loads the embedding model **before** the app starts
   listening — the chart's startup probe budgets **300 seconds** for
   this, so a Pod that is `Running` but not `Ready` for a couple of
   minutes on first boot is normal.

## Step 8 — Verify the backplane

```bash
# Rollout completes inside the startup budget.
kubectl -n meho rollout status deploy/meho --timeout=360s

# Liveness endpoint answers.
kubectl -n meho exec deploy/meho -- wget -qO- http://localhost:8000/healthz

# Readiness includes live checks against Postgres, Keycloak, and the
# credential backend — all must be green.
kubectl -n meho exec deploy/meho -- wget -qO- http://localhost:8000/ready

# From outside the cluster, through the Ingress:
curl -fsS https://meho.example.com/healthz
curl -fsS https://meho.example.com/api/v1/auth-config
# → {"keycloak_issuer": "...", "audience": "meho-backplane", "cli_client_id": "meho-cli"}
```

If `/healthz` is green but `/ready` returns 503 with its `keycloak`
check reading `jwks_fetch_failed: ConnectError` (or the credential
backend's check reading `unreachable: ConnectError`), the backplane
does not trust the certificate that dependency presented — go back to
[TLS and ingress](tls-ingress.md#the-backplanes-own-trust-internal-ca-bundle).

## Step 9 — Install the CLI and log in

Download the `meho` CLI for your platform from the
[releases page](https://github.com/evoila/meho/releases) — a static
binary in a tarball, with checksums and signatures:

```bash
TAG=<the release tag matching your backplane, e.g. v0.25.0>
TARBALL=meho_${TAG#v}_linux_amd64.tar.gz   # or darwin_arm64, etc.
curl -LO https://github.com/evoila/meho/releases/download/${TAG}/${TARBALL}
curl -LO https://github.com/evoila/meho/releases/download/${TAG}/SHA256SUMS
sha256sum -c SHA256SUMS --ignore-missing
tar xzf ${TARBALL}
sudo install -m 0755 meho /usr/local/bin/meho
```

(Signature verification with cosign, and the full client-side setup
story including MCP clients, live in
[Connect clients](../clients/index.md).)

If your deployment's certificate chain comes from an internal CA,
install that CA into your workstation's **OS trust store** first — the
CLI verifies TLS against it, and on macOS it ignores the
`SSL_CERT_FILE` environment variable entirely. Details:
[TLS and ingress](tls-ingress.md#your-workstation-os-trust-store).

Then log in:

```bash
meho login https://meho.example.com
```

This runs the OAuth device-code flow: the CLI prints a verification
URL and code, you approve it in a browser as the user created in
Step 4, and the CLI stores the resulting token in your OS keyring.
Prove the whole chain end to end:

```bash
meho status
```

`meho status` calls `/api/v1/health` with the stored bearer token, so a
clean result proves the whole chain — CLI, token, ingress, backplane.

If login fails, the symptom almost always maps to a known
misconfiguration in the realm —
[Keycloak realm setup § If login fails](keycloak-realm.md#if-login-fails)
has the symptom-to-fix table.

**You now have a running, governed backplane and an authenticated
operator.**

## Where next

- **[Connect clients](../clients/index.md)** — wire up MCP clients
  (Claude Desktop, Claude Code) and the rest of the CLI story.
- **[Do real work](../guides/index.md)** — register targets and
  secrets, run first operations.
- **[Upgrades](upgrades.md)** — how `helm upgrade` behaves, the Helm 4
  field-ownership caveat, and per-version upgrade notes. Backup/restore
  and observability guides land with MEHO's disaster-recovery work.
