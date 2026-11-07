# The install trail

This page is one continuous path from a bare Kubernetes cluster to a
running MEHO backplane and a successful `meho login`. Follow it top to
bottom; every side quest (choosing a credential backend, configuring
the Keycloak realm, TLS trust) is linked at the exact step where you
need it, and each of those pages sends you back here.

The Helm chart brings its own PostgreSQL (the database, with pgvector),
Keycloak (the login server) and OpenBao (the password safe, an
open-source fork of Vault). This is the default since v0.45.0. The
chart connects MEHO to these parts by itself, and MEHO manages them.
This is the normal way to run MEHO. If you already run your own
PostgreSQL or Vault, you can connect them instead.
[Two ways to install](#two-ways-to-install) below says which steps each
way needs.

!!! tip "Just exploring?"

    If you only want to see the backplane come up on your workstation —
    without a real Keycloak, Vault, or login — take the
    [local kind quickstart](kind-quickstart.md) instead. It is faster,
    and it is honest about what it fakes.

## What you need

| You bring | Requirements |
|---|---|
| **Kubernetes cluster** | Kubernetes 1.28 or newer, with an ingress controller (the examples use ingress-nginx) and a default storage class (the built-in parts keep their data on disks the cluster makes). [Helm](https://helm.sh/) 3.8 or newer on your workstation, so it can install from an OCI registry. The chart is tested with Helm 3.16.4. Helm 4 recommended — the [upgrades](upgrades.md) page's server-side-apply guidance assumes it. |
| **A DNS name + TLS story** | A hostname for the backplane (e.g. `meho.example.com`) and a certificate for it — public CA or internal CA both work; [TLS and ingress](tls-ingress.md) covers the differences. With the built-in parts, the chart can also make a self-signed certificate for you. |
| *Only with your own parts:* **PostgreSQL** | A reachable PostgreSQL server with the [pgvector](https://github.com/pgvector/pgvector) extension *available* (installed as a server package). Steps 1 and 3. |
| *Only on the expert path:* **Keycloak** | A [Keycloak](https://www.keycloak.org/) where you have realm admin rights, to make MEHO's realm, clients and groups. Step 4. |
| *Only with your own parts:* **Credential backend** | HashiCorp Vault **or** access to Google Secret Manager. Step 2. |

Everything else — the activity-feed store (Valkey), database
migrations, the operations catalog — ships inside the Helm release.

## Two ways to install

### 1. The normal way: MEHO brings its parts (recommended)

One `helm install` installs MEHO, PostgreSQL, Keycloak and OpenBao, and
connects them. MEHO manages these parts. The chart makes every password
and key itself and keeps them in Kubernetes Secrets. The values file
needs only the hostname and an id for your first tenant. Skip Steps 1
to 4 and go to [Step 5](#step-5-decide-ingress-tls-and-trust).

**Company login.** People can log in with their company account:
Active Directory or LDAP, Microsoft Entra ID, authentik, or your own
Keycloak. All four connect *in front of* MEHO's Keycloak. MEHO still
trusts only its own Keycloak. Keycloak works as a broker: it sends
people on to the company login, then writes the MEHO token. See
[Company login with authentik](company-login-authentik.md) for how the
broker works, and [Company login](login/index.md) for each of the four
systems.

### 2. Connect your own PostgreSQL or secret store

You can connect a PostgreSQL or a secret store (HashiCorp Vault or
Google Secret Manager) that you already run. Follow every step below
that applies to you.

- **Your own secret store.** Set `coreVault.enabled: false`. It works
  with the built-in PostgreSQL and Keycloak.
- **Your own PostgreSQL.** Set `corePostgres.enabled: false`. You can
  keep MEHO's Keycloak: it then needs its own database and login role
  on your PostgreSQL. See
  [Your own PostgreSQL with the built-in Keycloak](#your-own-postgresql-with-the-built-in-keycloak).
  The built-in OpenBao needs the built-in Keycloak. If you bring your
  own Keycloak (expert path, below), also bring your own secret store:
  set `coreKeycloak.enabled` and `coreVault.enabled` to `false`.

!!! warning "Your own Keycloak (expert path)"

    You can replace MEHO's Keycloak with your own
    (`coreKeycloak.enabled: false`, which also needs
    `coreVault.enabled: false`). This is not the normal way. It needs:

    - realm admin rights on your Keycloak, to make MEHO's realm,
      clients and groups by hand —
      [Keycloak realm setup](keycloak-realm.md) is the recipe;
    - a service account client for MEHO itself in that realm
      (values key `keycloakAdmin`), so MEHO can manage its agents and
      service accounts there.

    If you only want people to log in with their company account, you
    do not need this. Keep MEHO's Keycloak and connect your login in
    front of it: see [Company login with authentik](company-login-authentik.md).

## Step 1 — Pre-create the pgvector extension

*Only with your own PostgreSQL.* The built-in PostgreSQL creates the
extension itself.

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
      # postInitApplicationSQL, not postInitSQL: the latter runs against
      # the `postgres` database, so the extension would land in the wrong
      # one. This runs inside the application database MEHO uses.
      postInitApplicationSQL:
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

*Only with your own Vault or Google Secret Manager.* With the built-in
OpenBao, the chart sets up the password safe and connects MEHO to it.

MEHO fetches target credentials (the vCenter password, the appliance
API key) from a secret store at operation time — that store is the
**credential backend**, and the chart supports two: HashiCorp
**Vault** (the default) and **Google Secret Manager**.

**Read the [credential backends](credential-backends.md) decision page
now** — it states what each backend requires and an honest support
matrix of what works on each today. Come back with a decision; Step 6
shows the values for both.

## Step 3 — Provision the database credentials Secret

*Only with your own PostgreSQL.* The built-in PostgreSQL makes this
Secret itself.

The chart never embeds credentials in values. It references a
Kubernetes Secret **by name**; the Secret must hold the full database
DSN at the key `url`, using the **async** driver prefix:

```bash
kubectl create namespace meho
kubectl create secret generic meho-postgres -n meho \
  --from-literal=url='postgresql+asyncpg://<user>:<password>@<host>:5432/meho'
```

Three things bite here if missed:

- The prefix must be `postgresql+asyncpg://`. A bare `postgresql://`
  DSN selects the synchronous driver and fails at connect time.
- The DSN is a URI, so reserved characters in the user or password
  (`@ : / ? # [ ] %` and friends) must be **percent-encoded**. Shell
  quoting protects them from the shell, not from the URI parser; an
  unencoded `@` in a password silently truncates the host.
- The key must be `url` — the Deployment reads `DATABASE_URL` from
  exactly that key.

In production you will usually not `kubectl create` this by hand:
the recommended pattern is syncing it from your secret store with
[External Secrets Operator](https://external-secrets.io/). The
manifests and the chart's optional ESO integration are documented in
the [values-examples deep-dive](https://github.com/evoila/meho/blob/main/deploy/values-examples/README.md)
— the trail continues the same either way, as long as the Secret
exists before you install.

**Optional: a second, narrower login for the running backplane.** The
login above owns the tables and runs the migrations. The running
backplane can use its own role instead. That role may read and write
every table, but on the audit log it may only add rows, read them and
empty the raw-payload column; it cannot change or delete audit rows.
Create the role as a superuser, give it `CONNECT`, and put its DSN in a
second Secret:

```sql
CREATE ROLE meho_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '<password>';
GRANT CONNECT ON DATABASE meho TO meho_app;
```

```bash
kubectl create secret generic meho-postgres-app -n meho \
  --from-literal=url='postgresql+asyncpg://meho_app:<password>@<host>:5432/meho'
```

Then name both in the values file (Step 6):

```yaml
postgres:
  runtime:
    role: meho_app
    credentialsSecret: meho-postgres-app
```

Name both keys, or neither: the install stops when one is missing. The
migration Job grants the role its rights on every install and upgrade,
so create the role before the upgrade that first names it. The built-in
PostgreSQL of the chart does all of this on its own; leave both keys
empty there.

## Step 4 — Set up the Keycloak realm

*Only on the expert path, with your own Keycloak.* MEHO authenticates every caller against
Keycloak. The built-in Keycloak (`coreKeycloak.enabled: true`, the
default) makes the realm `meho` at the first start, with everything
listed below, and two first users. Skip to the end of this step.

With your own Keycloak, before the backplane can be logged into, the
realm needs:

1. the **confidential `meho-backplane` client** — the resource-server
   identity MEHO validates tokens against;
2. a **public `meho-cli` client** with the device-code grant — what
   `meho login` uses;
3. a **public `meho-mcp`** client with authorization-code + PKCE —
   what browser-capable MCP clients use;
4. a **confidential `meho-web`** client for the web UI;
5. a **public `meho-cli-approve`** client with authorization-code +
   PKCE — what `meho approvals approve` uses for its fresh sign-in;
6. eight **protocol mappers** and four **default client scopes** on the
   login clients, so issued tokens carry the claims MEHO validates;
7. five **groups** (`MEHO-Readers`, `MEHO-Operators`,
   `MEHO-SA-Operators`, `MEHO-Admins`, `MEHO-Approvers`) that give each
   person their tenant and role. The groups are the only source: no
   login client carries a fixed tenant or role;
8. a first **user** in `MEHO-Admins` and `MEHO-Platform-Admins`, and a
   second one in `MEHO-SA-Operators` who can approve the first one's
   changes.

**Follow [Keycloak realm setup](keycloak-realm.md) now** — it is the
step-by-step recipe, including the non-obvious Keycloak 25+ `basic`
scope gotcha that otherwise costs hours. Come back when the realm
verification block at the end of that page passes.

!!! tip "People log in with their company account"

    To let people log in with their authentik account, and let their
    authentik group decide their MEHO role, follow
    [Company login with authentik](company-login-authentik.md) after
    the realm works. It also covers the built-in Keycloak of the chart.

Then let people log in with their company account (local users, Active
Directory / LDAP, Entra ID, authentik): see
[Company login](login/index.md).

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

### With the built-in parts (default)

```yaml
image:
  # Pin an immutable tag: v<x.y.z> for a release (see
  # https://github.com/evoila/meho/releases), or sha-<git-sha> from CI.
  # :latest is never published; :main is a dev-only moving alias.
  tag: "v0.45.0"

ingress:
  enabled: true
  className: nginx
  host: meho.example.com          # drives the MCP audience — see Step 5
  tls:
    enabled: true
    # Empty: the install makes a self-signed certificate in the Secret
    # meho-core-tls. Or name your own TLS Secret here.
    secretName: ""

coreKeycloak:
  hostname: meho.example.com      # the same value as ingress.host
  # A UUID you pick once for your first tenant, and keep.
  # For example, make one with: uuidgen | tr 'A-Z' 'a-z'
  firstTenantId: "<REPLACE: a new UUID>"
```

That is all. Leave `postgres.credentialsSecret`, `vault.address`,
`config.vaultAddr`, `config.keycloakIssuerUrl` and `keycloak.issuer`
empty: the chart sets them. If you set them, Helm stops with an error
that names the key. The NetworkPolicy is on by default, and it reaches
the built-in parts without any address ranges from you.

### Your own PostgreSQL with the built-in Keycloak

Set `corePostgres.enabled: false` and keep `coreKeycloak.enabled: true`.
Fill `postgres.credentialsSecret` (and `networkPolicy.postgresCIDR`) as
for any own PostgreSQL. MEHO's Keycloak needs its own database and its
own login role on your server. Name the server in
`coreKeycloak.database.host`, then pick one of two ways.

**The chart creates them.** Give the chart a login that may create a
role and a database. A one-time Job uses it before Keycloak starts. It is
safe to run twice: it makes only what is missing. The Job sets the
password of the Keycloak role to the one in the Secret
`<release>-core-postgres-keycloak`, which the install makes by itself.

```bash
kubectl create secret generic keycloak-db-admin \
  --from-literal=username=<admin-user> \
  --from-literal=password=<admin-password>
```

```yaml
corePostgres:
  enabled: false
coreKeycloak:
  database:
    host: db.example.com
    create:
      enabled: true
      adminSecret:
        name: keycloak-db-admin
```

**You name them in values.** Your database team makes the database and
the role. Put the role's password in a Secret with the key `password`
(or name another key in `passwordSecret.key`).

```yaml
corePostgres:
  enabled: false
coreKeycloak:
  database:
    host: db.example.com
    name: keycloak            # default
    user: keycloak            # default
    passwordSecret:
      name: keycloak-db-password
```

If the Keycloak role already exists, the Job sets its password again. This
can fail when the admin login is not allowed to manage that role. This is
the case on PostgreSQL 16 and later when another login made the role. The
Job then stops with an error. Fix it in one of two ways: give the admin
login the rights to manage the role, or make the role yourself and use the
"You name them in values" way.

Both ways accept `coreKeycloak.database.port` (default `5432`) and
`coreKeycloak.database.sslMode` (`require`, `verify-full` and so on) for
a TLS connection. Without a host, or without a password Secret or an
admin Secret, Helm stops with a message that names the key. These keys
work only with your own PostgreSQL. Moving the data of an existing
install from the built-in PostgreSQL to yours is not covered here.

### With your own PostgreSQL, Keycloak and Vault (expert path)

!!! warning "This example also replaces MEHO's Keycloak"

    This example turns off all three built-in parts, so it uses your own
    Keycloak. That is the expert path: read
    [Your own Keycloak](#2-connect-your-own-postgresql-or-secret-store)
    first. With only your own secret store, start from the built-in
    example above and set `coreVault.enabled: false` (see the Google
    Secret Manager note below for the values that change).

The first three keys turn the built-in parts off. The rest points MEHO
at your own services. Without the first three keys, Helm stops with an
error.

```yaml
corePostgres:
  enabled: false
coreKeycloak:
  enabled: false
coreVault:
  enabled: false

image:
  # Pin an immutable tag: v<x.y.z> for a release (see
  # https://github.com/evoila/meho/releases), or sha-<git-sha> from CI.
  # :latest is never published; :main is a dev-only moving alias.
  tag: "v0.45.0"

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
  # Staged deliberately: off for the first install, on once the backplane
  # is green. The CIDRs below are placeholders, and if they do not match
  # where your dependencies actually resolve, the Pod cannot reach them
  # and /ready stays red — a failure that looks exactly like a credential
  # or TLS problem. To turn it on afterwards: replace all three CIDRs
  # with the real addresses, set enabled: true, and re-run the same
  # `helm upgrade` from Step 7.
  enabled: false
  ingressControllerNamespace: ingress-nginx
  # Egress allow-list: the CIDRs your Postgres / Vault / Keycloak
  # actually resolve to. For in-cluster Services, recover them with:
  #   kubectl get endpoints <svc> -n <ns> -o jsonpath='{.subsets[].addresses[].ip}'
  # For anything outside the cluster, use the address your DNS name
  # resolves to.
  postgresCIDR: "10.0.1.0/24"   # REPLACE
  vaultCIDR: "10.0.2.0/24"      # REPLACE
  keycloakCIDR: "10.0.3.0/24"   # REPLACE
```

!!! warning "Baseline: `networkPolicy.enabled: true` for any multi-tenant or production install"

    Staging `networkPolicy.enabled: false` above is a first-install
    convenience — it keeps a bring-up from failing on a CIDR typo before the
    backplane is even green. The **secure baseline is default-deny network
    isolation on** (`enabled: true`), and leaving it off past bring-up is a
    documented downgrade, not a default. Turning it on makes the schema
    *require* the three egress CIDRs, so it refuses to ship a wide subnet by
    accident — replace the placeholder CIDRs above with the real addresses
    your Postgres / Vault / Keycloak resolve to, set `enabled: true`, and
    re-run the same `helm upgrade` from Step 7. The broadcast subchart
    renders its own ingress NetworkPolicy and Valkey auth alongside.
    The CIDRs are needed only for the parts you bring yourself. The
    policy reaches the built-in parts by their pods.

### Let MEHO reach the systems it manages

With `networkPolicy.enabled: true`, MEHO can only connect to PostgreSQL,
Vault and Keycloak (the built-in parts, or the three CIDRs above), its
own stores, and DNS.
Everything else is blocked: vCenter, NSX, ESXi, SSH and WinRM hosts, and
the AI service. MEHO still reports ready, because its readiness check only
tests its own parts. So list those networks in `networkPolicy.extraEgress`.
Each item is a normal Kubernetes egress rule, and the chart adds it as
written.

Open only the networks and ports MEHO needs. Each rule you add lets MEHO
send traffic there.

```yaml
networkPolicy:
  extraEgress:
    # The systems MEHO manages. Replace the range with yours, and list
    # only the ports your connectors use.
    - to:
        - ipBlock:
            cidr: 192.0.2.0/24
      ports:
        - port: 443     # vCenter, NSX
          protocol: TCP
        - port: 22      # SSH
          protocol: TCP
        - port: 5985    # WinRM (HTTP)
          protocol: TCP
        - port: 5986    # WinRM (HTTPS)
          protocol: TCP
    # The AI service and other public APIs, on 443 only.
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
            except: [10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16]
      ports:
        - port: 443
          protocol: TCP
```

!!! warning "The second rule is wide"

    This rule lets MEHO reach every public address on port 443. When you
    can, use your AI provider's address range or your outgoing proxy
    instead.

Every rule needs a `to` list. The chart refuses a rule without one,
because Kubernetes reads a rule with no `to` as "every destination". The
chart also refuses keys it does not know, so a typo such as `port:` for
`ports:` cannot remove the port limit.

**Vault or Keycloak behind your ingress.** Kubernetes does not define
whether a network rule is checked before or after the cluster redirects a
connection. On Calico and Canal with kube-proxy (RKE2's default), it is
checked *after*. So when `vault.example.com` points at your ingress load
balancer, the connection is redirected to an ingress Pod on that Pod's
own port (8443 for Traefik), and the Vault rule on port 8200 never
matches. Add a rule for the ingress Pods and their port:

```yaml
networkPolicy:
  extraEgress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: traefik
          podSelector:
            matchLabels:
              app.kubernetes.io/name: traefik
      ports:
        - port: 8443
          protocol: TCP
```

Check the `targetPort` of your ingress Service. For ingress-nginx it is
usually 443.

The same applies to any rule: name the real Pod or address and its port,
not the load balancer address.

For a **Google Secret Manager** deploy, set `coreVault.enabled: false`
(the chart refuses Google Secret Manager while the built-in OpenBao is
on), then replace the `vault` block and `config.vaultAddr` with the GSM
fields — the exact delta is on the
[credential backends](credential-backends.md#helm-values-per-backend)
page.

Notes for your own parts:

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

!!! tip "Baseline: deploy by digest and verify the signature"

    Beyond pinning an immutable `image.tag`, the production baseline is to
    **deploy by digest and verify the signature before rollout**. Set
    `image.digest` (`sha256:…`) so the Deployment (and the byte-identical
    migration Job) render `repository@digest` — the exact, content-addressed
    digest the image pipeline scanned, promoted, and cosign-signed; a digest
    can never be re-pointed the way a tag can. `cosign verify` that digest
    against the keyless workflow identity before `helm upgrade`:

    ```bash
    DIGEST=sha256:<the digest you are pinning>
    cosign verify "ghcr.io/evoila/meho@${DIGEST}" \
      --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/image\.yml@.*$' \
      --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
    ```

    Ideally the same check is enforced at admission (a Sigstore
    policy-controller / Kyverno image-verification rule). The full
    verification recipes — image, chart, and CLI — are in the
    [repository README](https://github.com/evoila/meho#verify-image--chart--cli-signatures).

What happens, in order, **with the built-in parts**:

1. A setup Job makes every password and key once and keeps them in
   Secrets named `meho-core-*`. Every upgrade keeps them.
2. PostgreSQL, Keycloak and OpenBao start. Keycloak loads the realm
   `meho`. A second Job sets up OpenBao, and a third one makes the two
   first users in Keycloak.
3. MEHO runs its database migrations, then starts.

What happens, in order, **with your own parts**:

1. A **pre-install migration Job** runs all database migrations before
   any application Pod exists. If a migration fails, the release fails
   — an unmigrated Pod never takes traffic. Failed Jobs stick around
   for ten minutes, so `kubectl logs job/<name> -n meho` shows why.
   This is where Step 1's pgvector prerequisite is enforced.
2. The Deployment rolls out.

The **first start takes up to 15 minutes** either way. On an empty
database, MEHO first registers the full operations catalog and builds
the search data for every operation, **before** the app starts
listening. The chart's startup probe allows 15 minutes for this, so a
Pod that is `Running` but not `Ready` for several minutes on the first
start is normal. Later starts take about 20 seconds.

With the built-in parts, the install notes (`helm status meho -n meho`)
also print OpenBao's unseal key Secret, `meho-core-vault-unseal`. Copy
the key to your own key store now; see
[OpenBao unseal key](openbao-unseal-key.md).

## Step 8 — Verify the backplane

```bash
# Rollout completes inside the startup budget.
kubectl -n meho rollout status deploy/meho --timeout=960s

# The runtime image is python:3.14-slim — it ships neither curl nor
# wget — so reach the Pod with a port-forward instead of `kubectl exec`.
kubectl -n meho port-forward deploy/meho 8000:8000 >/dev/null 2>&1 &
PF=$!

# kubectl binds the local port only after it has dialled the API server,
# so wait for the forward rather than racing it. Any HTTP answer counts,
# including the 503 that /ready returns while dependencies are still red.
for _ in $(seq 30); do
  curl -s -o /dev/null http://localhost:8000/healthz && break
  sleep 1
done

# Liveness endpoint answers.
curl -fsS http://localhost:8000/healthz

# Readiness includes live checks against Postgres, Keycloak, and the
# credential backend — all must be green. It answers 503 until they are,
# so read the body rather than failing on the status code.
curl -sS http://localhost:8000/ready | jq .

kill "$PF"

# From outside the cluster, through the Ingress:
curl -fsS https://meho.example.com/healthz
curl -fsS https://meho.example.com/api/v1/auth-config
# → {"keycloak_issuer": "...", "audience": "meho-backplane", "cli_client_id": "meho-cli",
#    "cli_approve_client_id": "meho-cli-approve"}
```

If `/healthz` is green but `/ready` returns 503 with its `keycloak`
check reading `jwks_fetch_failed: ConnectError` (or the credential
backend's check reading `unreachable: SSLError`), the backplane
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
If the install made the self-signed certificate (built-in parts with an
empty `ingress.tls.secretName`), its CA is the `ca.crt` key of the
Secret `meho-core-tls`:

```bash
kubectl -n meho get secret meho-core-tls -o jsonpath='{.data.ca\.crt}' | base64 -d > meho-ca.crt
```

Then log in:

```bash
meho login https://meho.example.com
```

This runs the OAuth device-code flow: the CLI prints a verification
URL and code, you approve it in a browser, and the CLI stores the
resulting token in your OS keyring.

**Who logs in with the built-in Keycloak.** The install made two users
with temporary passwords: the first admin (`admin`) and a second person
who can approve the admin's changes (`approver`). Both are in the
Secret `meho-core-first-login`. Keycloak asks for a new password, a
name and an email at the first login. Read the admin's login with:

```bash
kubectl -n meho get secret meho-core-first-login \
  -o jsonpath='{.data.admin_username}' | base64 -d; echo
kubectl -n meho get secret meho-core-first-login \
  -o jsonpath='{.data.admin_password}' | base64 -d; echo
```

Keep these passwords in your own key store.

**Who logs in with your own Keycloak.** The user you created in Step 4.
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
- **[Company login with authentik](company-login-authentik.md)** — let
  people log in with their company account and MFA.
- **[Upgrades](upgrades.md)** — how `helm upgrade` behaves, the Helm 4
  field-ownership caveat, and per-version upgrade notes. Backup/restore
  and observability guides land with MEHO's disaster-recovery work.
