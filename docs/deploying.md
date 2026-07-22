<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright (c) 2026 evoila Group -->

# Deploying and upgrading MEHO

Operator-facing guide for installing MEHO from cold and upgrading an
existing release. It consolidates deploy/upgrade knowledge that was
previously split across the chart values, the acceptance contracts, and
several deep-dive docs. It **links into** those deep-dives rather than
restating them — when a section says "see X", X is the source of truth.

Everything here is grounded in the shipped chart at
[`../deploy/charts/meho/`](../deploy/charts/meho/) and the example values
under [`../deploy/values-examples/`](../deploy/values-examples/). The chart
ships a typed [`values.schema.json`](../deploy/charts/meho/values.schema.json)
contract: every operator-supplied field the backplane cannot start without
is blank by default and rejected at `helm install` / `helm upgrade` /
`helm template` time with the exact failing field path, so a misconfigured
release fails up-front instead of CrashLoopBackOff-ing at first request.

## Scope and audience

- **Local / dev deploy** (kind, ~5 min) is covered in the repository
  [`README.md`](../README.md#deploy) and
  [`../deploy/values-examples/values-kind.yaml`](../deploy/values-examples/values-kind.yaml).
  This guide covers **production-shaped** installs against an
  operator-managed cluster.
- MEHO is on-prem-first: v0.1 expects an operator-managed PostgreSQL
  cluster (no bundled PG subchart), an operator-run Vault **or** GCP
  Secret Manager, and a Keycloak realm. See
  [`codebase/devops.md`](codebase/devops.md) for the chart's internals and
  [`acceptance/install.md`](acceptance/install.md) for the cold-deploy
  acceptance contract.

## Deploy from cold — prerequisites checklist

Satisfy every row before the first `helm install`. Two of the original
cold-install foot-guns (migrate-hook ServiceAccount ordering, #2391; the
unresolvable-MCP-audience dark boot, #2394) are now handled **inside the
chart** and are noted below as "chart-handled" — no operator step remains,
but they explain failure modes you may still hit if you mis-set values.

| Prerequisite | Why | Verify |
|---|---|---|
| PostgreSQL reachable with the `vector` (pgvector) extension **already created** | The pre-install migration Job runs Alembic revision `0003`, which executes `CREATE EXTENSION IF NOT EXISTS vector`. Creating an extension needs **superuser**, so a cold install against a least-privilege app role fails with `permission denied to create extension "vector"`. The chart deliberately does **not** automate this (rejected — see [`decisions/pgvector-superuser-prerequisite.md`](decisions/pgvector-superuser-prerequisite.md)). Pre-create it once as a superuser, or via CNPG `spec.bootstrap.initdb.postInitSQL`. | `psql -d meho -c "SELECT extname FROM pg_extension WHERE extname='vector';"` (CNPG: `kubectl exec <cluster>-1 -n <ns> -c postgres -- psql -d meho -c "CREATE EXTENSION IF NOT EXISTS vector;"`) |
| DB-credentials Secret (`postgres.credentialsSecret`) holding an **asyncpg** DSN at key `url` | The Deployment env reads `DATABASE_URL` from this Secret's `url` key. It must be an async driver DSN: `postgresql+asyncpg://<user>:<pass>@<host>:<port>/<db>` (a bare `postgresql://` DSN drives the sync driver and fails at connect). Provisioned directly (GSM/no-Vault) or synced by ESO (Vault). See [`../deploy/values-examples/README.md`](../deploy/values-examples/README.md). | `kubectl get secret <name> -o jsonpath='{.data.url}' \| base64 -d` (expect a `postgresql+asyncpg://` prefix) |
| Keycloak realm with the backplane confidential client + the public `meho-cli` device-code client and audience mappers | The JWT validator expects `iss == keycloak.issuer` and `aud` containing `keycloak.audience` (default `meho-backplane`). `meho login` drives the RFC 8628 device-code grant against `config.keycloakCliClientId`. The realm-side recipe (clients, protocol mappers, client scopes) is the auth-onramp section of the values-examples README. | `curl -fsS "$KEYCLOAK_ISSUER/.well-known/openid-configuration" \| jq .issuer` (see [Auth onramp recipe](../deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp)) |
| MCP audience resolvable — set `ingress.host`, or `config.backplaneUrl`, or `config.mcpResourceUri` | Every `/mcp` Bearer token must carry `aud == MCP_RESOURCE_URI`. The chart derives `BACKPLANE_URL`/`MCP_RESOURCE_URI` from the Ingress host by default. If none of the three resolves, the chart **fails at render time** (#2394) with a remediation naming all three knobs — it will not deploy a dark, silent `/mcp`. **Chart-handled**: a mis-set install fails `helm template`, not at runtime. | `helm template meho ./deploy/charts/meho -f values.yaml \| grep -A1 MCP_RESOURCE_URI` |
| Internal-CA trust bundle (when Vault/Keycloak/Postgres use internal-CA certs) | Python's default TLS context only trusts public CAs. Mount the CA bundle via `extraVolumes` + `extraVolumeMounts` and point `SSL_CERT_FILE` at it via `extraEnv` (httpx, hvac, asyncpg, SQLAlchemy all honour it). See the [Internal-CA trust bundle](../deploy/values-examples/README.md#internal-ca-trust-bundle-extravolumes--extraenv) section. Clusters using only public CAs need nothing here. | `kubectl exec deploy/meho -- python -c "import ssl,os;print(os.environ.get('SSL_CERT_FILE'))"` |
| Pinned, immutable `image.tag` | Deploy discipline (Goal #11) forbids `:latest` and treats `:main` as a dev-only moving alias, never a deploy target. Use `sha-<git-sha>` from a green `main` run or a `vX.Y.Z` release tag. The schema rejects an empty tag. | `helm get values meho -o json \| jq -r '.image.tag'` (must be non-empty, not `latest`/`main`) |
| First-boot timing budget | First boot registers the full typed-op catalog and preloads the fastembed embedding model **before** the app binds `:8000`. The default model (`BAAI/bge-small-en-v1.5`) is baked into the image (offline, version-locked), so the default deploy needs no HuggingFace egress or PVC; a **custom** `config.retrievalEmbeddingModel` downloads at runtime into the opt-in `retrieval.modelCache` PVC. The `startupProbe` budget is `failureThreshold × periodSeconds = 30 × 10s = 300s`. | `kubectl rollout status deploy/meho --timeout=360s` |

**Chart-handled cold-install fixes (no operator step, listed for failure-mode context):**

- **Migrate-hook ordering (#2391)** — the pre-install migration Job and its
  ServiceAccount are ordered so the Job never deadlocks waiting for an SA
  that Helm has not created yet.
- **Unresolvable MCP audience (#2394)** — see the MCP-audience row above;
  the chart fails at render time rather than serving a dark `/mcp`.

## Install paths

Two credential-backend shapes are supported. Both use the same chart; they
differ only in `config.credentialBackend` and the credential-store block
the schema then requires. The recommended flow substitutes an example
values file rather than long `--set` strings (the `--set` form is in
[`codebase/devops.md`](codebase/devops.md#install--upgrade)).

### Vault-backed (default)

`config.credentialBackend: vault` (the default) keeps every install on
today's Vault KV-v2 read: a schemeless `targets/<id>` ref and an explicit
`vault:targets/<id>` ref both dispatch to Vault, and the
`/api/v1/health` federation proof reads `secret/meho/test/federation`. The
schema requires `vault.address` (and the Keycloak/Postgres/NetworkPolicy
fields) for this backend. Full worked example:
[`../deploy/values-examples/values-rdc-example.yaml`](../deploy/values-examples/values-rdc-example.yaml).

```bash
helm upgrade --install meho ./deploy/charts/meho/ \
  --namespace meho --create-namespace \
  --set image.tag=sha-<git-sha> \
  -f values-rdc.yaml
```

### GSM / Vault-free

`config.credentialBackend: gsm` selects the GCP Secret Manager backend:
schemeless refs resolve through Secret Manager and the health proof reads
`gsm:<gsmProject>/meho-test-federation`. `vault.address` / `config.vaultAddr`
are left **blank** — the schema requires them only when the backend is
`vault`, and instead requires `gsm.enabled: true` + `gsm.project` here
(`vault.address` became optional on the gsm backend in #2277). MEHO runs
under a GKE Workload Identity SA (no SA JSON keys);
`config.gsmImpersonateSa` optionally impersonates a dedicated reader SA.
Because the Vault tenant-scope guard does not apply to the GSM backend, a
gsm `secret_ref` registers with **zero** `extraEnv` workaround (contrast
the Vault path in the [version-specific notes](#version-specific-upgrade-notes)
below — #2585). Full worked example:
[`../deploy/values-examples/values-gsm-example.yaml`](../deploy/values-examples/values-gsm-example.yaml).

```bash
helm upgrade --install meho ./deploy/charts/meho/ \
  --namespace meho --create-namespace \
  --set image.tag=sha-<git-sha> \
  -f values-gsm.yaml
```

#### Per-operator WIF and background dispatch (#2642)

`gsm.workloadIdentityFederation.audience` switches credential reads onto
the **calling operator's** identity: MEHO exchanges their Keycloak JWT at
`sts.googleapis.com`, so GCP's own audit log names the operator rather than
MEHO's platform SA. Leaving it empty keeps the SA-direct read above. These
keys render into the ConfigMap as `GSM_WIF_*` — no `extraEnv` needed.

Turning that on raises a question the interactive path doesn't have:
**background dispatch has no calling operator.** Sensor evaluations
(Initiative #2416) run on a timer with no bearer token, so there is nothing
to federate. Decide which identity serves them:

| Deployment | Ambient GCP identity | Background reads run as | Extra config |
|---|---|---|---|
| WIF unconfigured (Phase 1) | required | MEHO's Workload Identity SA | none |
| GKE + per-operator WIF | yes (Workload Identity) | MEHO's Workload Identity SA (`auth_path=sa_direct_fallback`) | none |
| On-prem / no pod identity + per-operator WIF | no | the check-runner principal, federated through WIF | `checkRunner.*` |
| On-prem / no pod identity + per-operator WIF, no `checkRunner` | no | **nothing — credentialed Sensors read `unknown` forever** | — |

**"Background reads" here means the empty-`raw_jwt` callers only** — the
sensor check-runner, the topology-refresh scheduler, runbook verify
dispatch, the legacy connector `execute()` shims. It does **not** cover
connector `probe()` / `fingerprint()`. Those build their operator with
`synthesise_system_operator()`, which by deliberate design (G3.10) carries a
**non-empty placeholder** `raw_jwt`; `_select_auth_path` only tests
truthiness, so on a per-operator-WIF install a probe takes the `wif` path
and federates that placeholder at `sts.googleapis.com`, which rejects it.
Row 2 does **not** rescue credentialed-target probes on a per-operator-WIF
install — they fail with `GcpSecretManagerReadError` and `auth_path=wif`,
on GKE as much as on-prem. The failed exchange is otherwise harmless: the
placeholder is a fixed non-secret sentinel, not a credential, and the
connector degrades to `reachable=false` / `auth_failed` rather than raising.
Making probes take the fallback would mean teaching `_select_auth_path` to
treat the placeholder as absent, which is a behaviour change with its own
blast radius and is tracked separately — this release documents the
behaviour rather than changing it.

The last row is the failure the third one exists to prevent. Configure it:

```yaml
checkRunner:
  enabled: true
  clientId: meho-check-runner # confidential Keycloak client
  clientSecret:
    secretName: meho-check-runner # Secret you provision; wired via secretKeyRef
    secretKey: client_secret
```

Realm side, create a confidential client with the `client_credentials`
grant enabled and an audience mapper matching the WIF provider's allowed
audience (same shape as the agent/runner principal clients —
[`cross-repo/keycloak-agent-client.md`](cross-repo/keycloak-agent-client.md)),
and grant the federated principal `roles/secretmanager.secretAccessor` on the
target secrets. Leaving `checkRunner.enabled: false` is safe on any
deployment that does not use per-operator WIF, and changes nothing for
targets whose credentials MEHO does not have to fetch per read.

#### `checkRunner.*` on a Vault deploy widens what background dispatch can read

`checkRunner.*` is not a GSM-only knob, and on `credentialBackend: vault` it
is **not** inert-until-you-provision-more. The `meho-mcp` JWT role this
project documents
([`cross-repo/vault-provisioning.md`](cross-repo/vault-provisioning.md#2-role-meho-mcp))
is `role_type=jwt user_claim=sub bound_audiences=<keycloak-audience>` with
**no** `bound_subject` and **no** `bound_claims`, and the `meho-mcp` policy
grants read on all of `secret/data/meho/*`. The runner mints its token with
`audience=KEYCLOAK_AUDIENCE`, and the realm recipe just above tells you to
add the matching audience mapper. Those two facts compose: Vault accepts
the runner principal against the **existing** role with no further
provisioning, and background dispatch inherits the role's entire policy.

Concretely, enabling `checkRunner.*` on a Vault install removes the
"system-initiated calls cannot perform an operator-context Vault read"
carve-out that the rest of this credential layer is built on — every
scheduled evaluation can then read any target credential under
`secret/meho/*`. That may be exactly what you want (it is what makes
credentialed Sensors work on Vault), but it is a deliberate privilege
decision, not a no-op.

Bound the role first if it isn't:

- **Preferred** — give the runner client a **distinct** audience *instead
  of* the backplane audience mapper (Keycloak audience mappers add to `aud`
  rather than replace it, so a client carrying both still passes
  `meho-mcp`'s `bound_audiences`) and provision it a **separate**, narrower
  Vault JWT role whose policy covers only the secrets your Sensors
  evaluate.
- **Otherwise** — add an **exact-match** `bound_claims` to `meho-mcp` keyed
  on a claim value only operator tokens carry, e.g. a dedicated
  `meho-operator` realm role. A `bound_claims_type=glob` `"*"` is not a
  restriction: it matches any present value, and the runner's
  `client_credentials` token carries `preferred_username =
  service-account-<clientId>` like any other principal.

Both recipes, plus the `vault write auth/jwt/login` command that proves
which one is in force, are in
[`cross-repo/vault-provisioning.md` § "Bounding the check-runner principal"](cross-repo/vault-provisioning.md#7-bounding-the-check-runner-principal-2642).
The chart prints the same warning in its `helm install` notes whenever
`checkRunner.enabled: true` meets `config.credentialBackend: vault`.

Diagnostics: the `gsm_secret_accessed` structlog event carries an
`auth_path` of `wif`, `sa_direct`, or `sa_direct_fallback`, so you can see
which identity served a given read. A read that could not resolve any
identity fails with `connector_error: GcpSecretManagerReadError` — never a
Vault-named error on a Vault-free deploy (#2642).

### Operator console (`/ui/*`) — optional, either backend

The browser console is off by default and all-or-nothing. To light it up,
set `config.uiKeycloakClientId` (the public `client_id` of a confidential
`meho-web` Keycloak client, rendered as `UI_KEYCLOAK_CLIENT_ID`) **and**
`uiConsole.enabled: true` with `uiConsole.secretName` pointing at a Secret
that holds both the web client secret and a Fernet session-encryption key
(#2594). The schema then requires all three, so a half-configured console
fails at `helm install` rather than returning `503 ui_oauth_not_configured`
at first login. Realm-side recipe:
[Operator-console OAuth wiring](../deploy/values-examples/README.md#operator-console-browser-bff-oauth-wiring-2594).

## Upgrading

### What a `helm upgrade` does mechanically

1. The **pre-install/pre-upgrade migration Job** (Helm hook,
   `helm.sh/hook: pre-install,pre-upgrade`, `hook-weight: "-10"`) runs
   `python -m meho_backplane.db.migrate` → `alembic upgrade head`
   **before** the new Deployment is created or rolled forward. The runner
   exits non-zero on any Alembic failure, which fails the release and
   prevents an unmigrated Pod from taking traffic. Failed Jobs are not
   garbage-collected immediately (`ttlSecondsAfterFinished: 600`) so you
   can `kubectl logs` them. See
   [`codebase/devops.md`](codebase/devops.md#migration-job-templatesmigration-jobyaml).
2. Only after the migration succeeds does the new Deployment roll.

### Helm 4 / server-side-apply field-conflict caveat

MEHO is deployed with **Helm 4**, which performs upgrades via Kubernetes
**server-side apply** (`--server-side auto`). SSA tracks per-field
ownership by *field manager*. When a release first ships a field that
operators commonly hand-patched onto the running Deployment during an
earlier version — the live example is v0.22.0 introducing the backplane
`startupProbe` for the first time (#2393; absent in `v0.21.0`, present in
`v0.22.0`) — the chart's apply **conflicts** with the field manager that
owns that field, e.g. `Apply failed with 1 conflict: conflict with
"kubectl-patch"` (or `"kubectl-edit"`).

**Pre-flight check** — before upgrading, look for foreign field managers on
the Deployment:

```bash
kubectl get deploy meho -n meho -o yaml --show-managed-fields \
  | grep -E 'manager:|f:startupProbe'
```

Any `manager:` other than `helm` / `helm-controller` owning a field the
new chart sets will conflict.

**Remedy** — force the chart to take those fields:

```bash
helm upgrade meho ./deploy/charts/meho/ --force-conflicts -f values.yaml
```

`--force-conflicts` makes server-side apply overwrite conflicting fields
and hand ownership to Helm. Note (observed in the field): `--take-ownership`
is **not** the right flag here — it ignores Helm's ownership *annotation*
check and adopts whole un-owned *resources*, not individual SSA *fields*,
so it does not clear a per-field conflict.

### Rollback

`helm rollback meho` is supported, but **migrations are forward-only**: the
migration hook has no pre-rollback `alembic downgrade`, so the schema stays
at `N+1` after a rollback to an app version that expects `N`. The chart
relies on backend forward-compat (an `N` app tolerating an `N+1` schema).
See the [`helm rollback meho` acceptance contract](acceptance/rollback.md)
for what "verified" means and the schema-check fallback.

## Version-specific upgrade notes

Append a row here in the same PR that carries an upgrade-relevant change
(this discipline is enforced by a checklist line in
[`RELEASING.md`](RELEASING.md)).

| Version | Upgrade note | Action |
|---|---|---|
| **v0.15.0** | The Vault KV tenant-scope guard became **default-on** (#1725). The mount-pinned default namespace is `secret/tenants/{tenant_id}/`. | Run the `secret_ref` migration runbook, **or** hold with `VAULT_KV_TENANT_SCOPE_PREFIX=""` (as an `extraEnv` entry) until migrated. See [`codebase/connectors-vault-tenant-scope.md`](codebase/connectors-vault-tenant-scope.md). GSM-backend installs are unaffected (the guard is Vault-only, #2585). |
| **v0.22.0** | First release to ship the backplane `startupProbe` (#2393). | On a cluster where you previously hand-patched a `startupProbe`, the Helm 4 SSA upgrade conflicts with `kubectl-patch`/`kubectl-edit`. Pre-flight `--show-managed-fields`, remedy `helm upgrade --force-conflicts` (see [Upgrading](#upgrading)). |

## Operational chart knobs

Non-obvious values you may need to set post-install. Full documentation for
each lives inline in
[`../deploy/charts/meho/values.yaml`](../deploy/charts/meho/values.yaml).

- **`config.targetSsrfAllowlist`** → `MEHO_TARGET_SSRF_ALLOWLIST`. The
  backplane refuses to register or dial a target whose host resolves to a
  non-public address (loopback, RFC 1918, link-local, cloud metadata). MEHO
  is on-prem, so a deploy managing LAN appliances **must** allowlist those
  ranges, e.g. `"10.0.0.0/8,192.168.0.0/16"`. Empty (`""`) keeps the guard
  fully on. A scoped opt-in is the intended posture, never a global
  off-switch.
- **`netdiag.pingGroupRange`** → the `net.ipv4.ping_group_range` pod sysctl
  for unprivileged ICMP-echo ping. Default-off (empty) ships no sysctl:
  `net.trace` / `net.path_mtu` still work, but `net.ping` degrades
  gracefully to `{available: false, reason:
  icmp_echo_unprivileged_unavailable}`. Set (e.g. `"1001 1001"` to match
  the pod GID) **only after a security review**; on pre-1.29 clusters the
  sysctl is *unsafe* and needs a kubelet allowlist.
- **`netdiag.probeAllowlist`** → `MEHO_NETDIAG_PROBE_ALLOWLIST`. An
  *allow-only-what-is-listed* floor for the `net.*` diagnostics connector:
  the parsed set is the whole permitted probe space, so an empty value
  means **deny every probe** — the connector is inert until an operator
  opts a range in.
- **`probes.startup`** — the first-boot startup probe (300s budget). Clear
  it to opt out on a fast cluster where op-catalog registration + fastembed
  preload finish inside the liveness `initialDelaySeconds`.
- **`extraVolumes` / `extraVolumeMounts` / `extraEnv`** — the internal-CA
  trust-bundle mount point (`SSL_CERT_FILE`) and any other operator env.
  Applied to **both** the backplane container and the migration Job
  container.

## See also

- [`codebase/devops.md`](codebase/devops.md) — chart internals, migration
  Job, ESO wiring, `--set` install form.
- [`../deploy/values-examples/README.md`](../deploy/values-examples/README.md)
  — ESO patterns, auth onramp, trust bundle, `verify_tls`/CA-pin.
- [`acceptance/install.md`](acceptance/install.md) /
  [`acceptance/rollback.md`](acceptance/rollback.md) — the cold-deploy and
  rollback acceptance contracts.
- [`decisions/pgvector-superuser-prerequisite.md`](decisions/pgvector-superuser-prerequisite.md)
  — why the pgvector `CREATE EXTENSION` step is an operator prerequisite,
  not chart automation.
