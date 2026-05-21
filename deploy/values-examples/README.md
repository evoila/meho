<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# values-examples — sanitized chart-values templates

This directory ships **sanitized example values files** for the MEHO Helm
chart at [`deploy/charts/meho/`](../charts/meho/). Each file targets a
specific deployment shape (today: the RDC Hetzner dogfooding lab) and is
designed to be copied into a private deploy repo, the placeholders
substituted, and applied via `helm install` / `helm upgrade`.

The files here ship **no real secrets, no real CIDRs**. Every
site-specific field uses a `<REPLACE: ...>` placeholder that fails
`values.schema.json` validation at install time, so an operator who
forgets to substitute one fails-loud at `helm install` rather than
silently connecting to the wrong system or CrashLoopBackOff'ing at first
request.

## Files

| File | Targets | Backed by |
| --- | --- | --- |
| [`values-rdc-example.yaml`](./values-rdc-example.yaml) | The RDC Hetzner lab (`*.evba.lab` hosts, rke2-infra ingress-nginx, cluster-internal Postgres + Vault + Keycloak). | The actual, private file lives in [`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)'s `manifests/meho/values-rdc.yaml`; this file is the sanitized template for other Vault-+-Keycloak-+-Postgres-shaped labs. |

## Using `values-rdc-example.yaml`

1. **Copy** the file into your private deploy repo. Do not put real
   CIDRs / hostnames / image tags into a public repo.

2. **Substitute** the `<REPLACE: ...>` placeholders. The minimum required
   set:

    | Placeholder | What to put |
    | --- | --- |
    | `image.tag` | An immutable tag from the G2.4 image pipeline. Production: `sha-<40-char-git-sha>` from a green CI run. Pre-prod / lab: `v0.1.0` once the release tag exists. **`:latest` and `:main` are forbidden by Goal #11 deploy discipline.** |
    | `config.keycloakIssuerUrl` / `keycloak.issuer` realm | Your Keycloak realm name. Both fields must agree — the ConfigMap-sourced env mirrors the values block (`config.keycloakIssuerUrl` is what the backplane process reads at startup). |
    | `config.keycloakCliClientId` | The client_id of the **public** Keycloak client `meho login` uses for device-code flow. Pre-create the client in the realm above per the [auth onramp recipe](#auth-onramp-recipe-cli--mcp) (`meho-cli` is the suggested default). Leaving this empty keeps v0.3.1 behaviour: the backplane endpoint serves an empty value and `meho login` surfaces an actionable public-client error. |
    | `networkPolicy.postgresCIDR` | The IPv4 CIDR of your Postgres Service. Recover via `kubectl get endpoints <pg-svc> -n <ns> -o jsonpath='{.subsets[].addresses[].ip}'` and widen to the controlling subnet. |
    | `networkPolicy.vaultCIDR` | Same, for Vault. |
    | `networkPolicy.keycloakCIDR` | Same, for Keycloak. |

3. **Provision** the Kubernetes Secrets named in the values file. The
   chart references `postgres.credentialsSecret` by name; the Pod will
   not start until that Secret exists with a `url` key holding the
   `DATABASE_URL`. The recommended sync mechanism is **External Secrets
   Operator (ESO)** — see [§ ESO sync patterns](#eso-sync-patterns) below.

4. **Install** (after ESO has populated the target Secret — see install
   flow below):

    ```bash
    helm upgrade --install meho ./deploy/charts/meho/ \
      --namespace meho --create-namespace \
      -f values-rdc.yaml
    ```

5. **Verify** the release came up clean:

    ```bash
    kubectl -n meho rollout status deploy/meho
    kubectl -n meho get pods -l app.kubernetes.io/name=meho
    kubectl -n meho logs deploy/meho --tail=50
    ```

## ESO sync patterns

The MEHO chart references operator-provisioned Kubernetes Secrets *by
name* — it does not embed credentials in `values.yaml`, and it does not
ship a `Secret` template that consumers `--set` values into. Instead, the
chart expects the consumer to sync secrets from a backing store (Vault,
1Password Connect, AWS Secrets Manager, …) into Kubernetes via
[**External Secrets Operator (ESO)**](https://external-secrets.io/).

The RDC lab uses ESO with **HashiCorp Vault** as the backend
([provider docs](https://external-secrets.io/latest/provider/hashicorp-vault/)).
Two resources combine to materialise a Secret the chart can consume:

1. **`ClusterSecretStore`** — cluster-scoped pointer at the upstream
   store. Carries the Vault address, auth method, and (for JWT/Kubernetes
   auth) the SA token. Created once, by the platform team. **Lives
   outside this chart by design**: it outlives any given release and
   carries the cluster's Vault credentials, so it belongs in the
   consumer's GitOps repo, not the application chart.

    ```yaml
    apiVersion: external-secrets.io/v1beta1
    kind: ClusterSecretStore
    metadata:
      name: vault-store
    spec:
      provider:
        vault:
          server: https://vault.evba.lab
          path: secret          # KV v2 mount
          version: v2
          auth:
            kubernetes:
              mountPath: kubernetes
              role: external-secrets
              serviceAccountRef:
                name: external-secrets
                namespace: external-secrets
    ```

2. **`ExternalSecret`** — namespaced resource that pulls one or more
   keys out of the upstream store and projects them into a Kubernetes
   Secret in the same namespace. **Two options for who owns this**:

    - **Default (consumer-managed):** the consumer's GitOps repo applies
      ExternalSecret manifests alongside (or before) the chart. The
      chart references the resulting Secret by name. This is the RDC
      lab's convention and stays out of the chart entirely.

      ```yaml
      apiVersion: external-secrets.io/v1beta1
      kind: ExternalSecret
      metadata:
        name: meho-postgres
        namespace: meho
      spec:
        refreshInterval: 1h
        secretStoreRef:
          name: vault-store
          kind: ClusterSecretStore
        target:
          # MUST match `.Values.postgres.credentialsSecret` in the chart.
          name: meho-postgres
          creationPolicy: Owner
        data:
          - secretKey: url       # the Secret's data key — MUST be `url`
            remoteRef:
              key: secret/meho/postgres   # Vault KV path
              property: url               # the JSON property holding DATABASE_URL
      ```

      The chart's Deployment env reads `DATABASE_URL` from this Secret's
      `url` key — see [`deploy/charts/meho/templates/deployment.yaml`](../charts/meho/templates/deployment.yaml).

    - **Opt-in (chart-managed):** set `eso.enabled: true` in your
      values file and the chart renders the ExternalSecret itself. Use
      this when you'd rather keep one source of truth (`helm template`
      shows everything) and you're not running a sibling GitOps
      operator that already owns the ExternalSecrets.

      ```yaml
      eso:
        enabled: true
        secretStore:
          name: vault-store
          kind: ClusterSecretStore
        refreshInterval: 1h
        postgres:
          remoteKey: secret/meho/postgres
          remoteProperty: url
        keycloak:
          enabled: false        # v0.1 does not yet consume this in env
      ```

      The schema enforces `secretStore.name` + `secretStore.kind` when
      `eso.enabled: true`, so a misconfigured opt-in fails at install
      time. With `eso.enabled: false` (the default) the chart never
      renders ExternalSecret resources — verify with
      `helm template ... | grep -c ExternalSecret` → `0`.

### Vault paths the chart expects

| Vault KV path | What's stored | Consumed by |
| --- | --- | --- |
| `secret/meho/postgres` (property `url`) | The full `DATABASE_URL`: `postgresql+asyncpg://<user>:<pass>@<host>:<port>/<db>` | The Deployment env `DATABASE_URL` via `postgres.credentialsSecret` |
| `secret/meho/keycloak/client_secret` (property `client_secret`) | The Keycloak OAuth client secret backing `keycloak.audience` | v0.2 federation wiring (rendered optionally today for end-to-end sync verification) |

The `secret/meho` base is configurable via `vault.paths.kv` — adjust the
KV paths above accordingly if you remount Vault elsewhere.

## Internal-CA trust bundle (`extraVolumes` / `extraEnv`)

The backplane connects to **Vault**, **Keycloak**, and **PostgreSQL**
over TLS. Any lab whose Vault / Keycloak / PG ingress is signed by an
**internal CA** (the realistic posture for every regulated lab) needs
to inject that CA into the backplane Pod, otherwise the readiness probes
fail with `SSLError` / `ConnectError` and `/ready` returns 503 even
though `/healthz` is green. The `--atomic --wait` install path rolls
back the release. This is Issue [#209](https://github.com/evoila/meho/issues/209).

The chart exposes three top-level knobs — `extraVolumes`,
`extraVolumeMounts`, `extraEnv` — that flow into both the backplane
Deployment AND the migration Job (Postgres' internal-CA-signed TLS is
the typical reason the migration Job needs the bundle too).

### Recommended pattern: trust-manager + `SSL_CERT_FILE`

In v0.1 the recommended path is [trust-manager](https://cert-manager.io/docs/trust/trust-manager/)
(jetstack/trust-manager). The lab admin creates one `Bundle` resource
cluster-wide; trust-manager distributes a `ConfigMap` containing
`ca.crt` into every namespace flagged via
`trust.cert-manager.io/include` (or a NamespaceSelector). The chart
mounts that ConfigMap and points Python's ssl module at it via
`SSL_CERT_FILE`:

```yaml
extraVolumes:
  - name: trust-bundle
    configMap:
      name: internal-ca-bundle       # the ConfigMap trust-manager renders
      optional: false                # fail-loud if it's missing

extraVolumeMounts:
  - name: trust-bundle
    mountPath: /etc/ssl/extra-certs
    readOnly: true

extraEnv:
  - name: SSL_CERT_FILE
    value: /etc/ssl/extra-certs/ca.crt
```

`SSL_CERT_FILE` is CPython's standard env var
([`ssl.get_default_verify_paths`](https://docs.python.org/3/library/ssl.html#ssl.get_default_verify_paths))
— httpx, hvac, asyncpg, and SQLAlchemy all honour it without code
changes. Mounting read-only keeps the discipline (the bundle is owned
by trust-manager, not by anything inside the Pod).

### Alternatives if trust-manager isn't deployed

- **Direct ConfigMap.** Skip trust-manager; create the ConfigMap by hand
  in the `meho` namespace. `extraVolumes[0].configMap.name` points at
  it. Rotation is now the operator's job.
- **Secret instead of ConfigMap.** Same `extraVolumes` shape with
  `secret:` instead of `configMap:`. Useful if the bundle itself is
  sensitive (uncommon — CA certificates are public-by-design).
- **Pre-baked image.** Add the CA to the runtime image's
  `/etc/ssl/certs/`. Rejected for v0.1 because it puts environment-
  specific state into a public OSS artefact; the chart-side approach
  keeps the image generic.

### Verification

After `helm install`:

```bash
# The mount is present
kubectl exec -n meho deployment/meho -- ls -l /etc/ssl/extra-certs/ca.crt
# SSL_CERT_FILE resolves to it
kubectl exec -n meho deployment/meho -- printenv SSL_CERT_FILE
# Python sees it
kubectl exec -n meho deployment/meho -- python -c "import ssl; print(ssl.get_default_verify_paths().cafile)"
# /ready turns green for vault + keycloak
kubectl exec -n meho deployment/meho -- wget -qO- http://localhost:8000/ready | jq '.checks'
```

If `/ready` still reports `ssl_error` after the mount lands, check the
**migration Job's** Pod logs — that Job uses the same bundle. A
common drift cause: a typo'd ConfigMap name (the mount succeeds but
the file is empty / wrong).

## Auth onramp recipe (CLI + MCP)

> First-login auth-onramp for both `meho login` (CLI device-code) and
> the MCP-client surface (`/mcp` over RFC 9728 + OAuth 2.1 + PKCE).
> The 2026-05-21 RDC dogfood walked the realm from scratch and paid
> ~2.5 hours hitting four sequential walls — none of them documented
> in one place at the time. This section is the consolidated 5-step
> recipe + 4-wall symptom→cause→fix matrix that closes that gap.
> Companion to [`docs/cross-repo/mcp-client-setup.md`](../../docs/cross-repo/mcp-client-setup.md)
> (MCP-client-side configuration) and
> [`docs/acceptance/install.md`](../../docs/acceptance/install.md)
> (cold-deploy acceptance contract).
>
> **Scope.** Both `meho login` and most MCP clients (Claude Desktop /
> Claude.ai, MCP Inspector, Claude Code's HTTP MCP, Cursor) need
> **public** OAuth clients pre-provisioned in the Keycloak realm
> before they can authenticate. MEHO is RFC-correct (the chassis
> emits the RFC 9728 metadata document, and the `/mcp` 401 carries
> `WWW-Authenticate: Bearer resource_metadata=…`) — but the implicit
> "follow the metadata trail and run dynamic client registration"
> path is closed by Keycloak's default Trusted Hosts policy on any
> realistic prod realm, and a public client can't appear by magic.
> The deployer creates two public clients (one for the CLI, one for
> MCP) with the right mappers + scopes; the consolidated automation
> verb that does this in one idempotent step ships under
> [#791](https://github.com/evoila/meho/issues/791) (G0.9.1-T11).
> This recipe is the manual path the verb encodes.

### 5-step realm recipe

The recipe assumes the confidential resource-server client
`meho-backplane` already exists (it's how the backplane validates
inbound tokens; created at install time alongside the realm). Steps
1–5 add the **public** clients + the user that approve and consume
those tokens.

#### Step 1 — Install the deployment's TLS CA on the operator workstation

`meho login` (Go) and most MCP clients verify TLS against the
operator's **OS trust store**, not the `SSL_CERT_FILE` env var.
On Linux: `update-ca-certificates` after dropping the CA in
`/usr/local/share/ca-certificates/`. On macOS: import to the system
keychain via `Keychain Access` or `security add-trusted-cert -d -r
trustRoot -k /Library/Keychains/System.keychain <ca>.pem`; Go on
macOS reads the system keychain via the Security framework and
**ignores `SSL_CERT_FILE`**, so the env-var trick that works for
Python on the backplane side doesn't carry over to the workstation
CLI. On Windows: import to `Trusted Root Certification Authorities`
via `certutil -addstore -f Root <ca>.pem`. Verify with
`curl -sf https://<backplane-host>/healthz` from a fresh shell.

If this step is skipped, `meho login` fails at the discovery probe
with an `x509: certificate signed by unknown authority` error and
the breadcrumb points at `--client-id`/`--issuer` overrides — which
doesn't fix TLS. The override surfaces a separate failure mode but
isn't the right recovery for an untrusted CA.

#### Step 2 — Create the public `meho-cli` device-code client

In the Keycloak realm that hosts `meho-backplane`, create a new
**public** client with these settings:

| Setting | Value | Why |
| --- | --- | --- |
| Client ID | `meho-cli` (suggested) | Matches the default in `values-rdc-example.yaml`. Any short identifier works — set `config.keycloakCliClientId` (`KEYCLOAK_CLI_CLIENT_ID`) to whatever you choose. |
| Client authentication | **Off** (public client) | The device grant cannot be completed by a confidential client; the CLI has nowhere to store a secret. Confidential client + device-grant → `401 unauthorized_client` from Keycloak's device endpoint (Wall #1). |
| Authentication flow → Standard flow | Off | The CLI doesn't run the authorization-code grant. |
| Authentication flow → Direct access grants | Off | Resource-owner password is explicitly out of scope. |
| Authentication flow → Implicit flow | Off | Deprecated by OAuth 2.1. |
| Authentication flow → Service accounts roles | Off | Public clients can't hold credentials. |
| Authentication flow → **OAuth 2.0 Device Authorization Grant** | **On** | Required for `meho login` ([RFC 8628](https://www.rfc-editor.org/rfc/rfc8628)). |
| Valid redirect URIs | (none) | Device flow doesn't redirect. |

#### Step 3 — Clone all 5 protocol mappers from `meho-backplane` onto `meho-cli`

Tokens minted by `meho-cli` must carry the same claim shape the
backplane validates — otherwise the token decodes cleanly but is
rejected with `invalid_token` (Wall #2). The five mappers (verbatim
names from the dogfood reference, copy hardcoded values from the
`meho-backplane` client in the same realm):

| Mapper name | Type | Output claim | Notes |
| --- | --- | --- | --- |
| `audience-meho-backplane` | `oidc-audience-mapper` | `aud` adds `meho-backplane` | Without this, tokens carry `aud: meho-cli` and the backplane rejects them with `audience_not_configured` / `invalid_audience`. |
| `meho-mcp-audience` | `oidc-audience-mapper` | `aud` adds `<backplane-url>/mcp` | Required so a token minted via the CLI can also drive `/mcp` calls. Use **Included Custom Audience** (no client-mapper UI option exists for an arbitrary URI). Paste the URI **without** a trailing slash — MEHO normalises `MCP_RESOURCE_URI` server-side and the audience claim must match the no-trailing-slash form. |
| `tenant-id` | `oidc-hardcoded-claim` | `tenant_id` | Hardcoded to the tenant UUID the operator belongs to. Without this the backplane rejects with `missing_tenant_claim` (Wall #2). |
| `tenant-role` | `oidc-hardcoded-claim` | `tenant_role` | Hardcoded to one of `read_only` / `operator` / `tenant_admin`. Without this the backplane rejects with `missing_tenant_role_claim`. |
| `groups-claim` | `oidc-group-membership-mapper` | `groups` | Drives group-based RBAC (`meho-admins`, etc.). Without this group-gated tools report empty results rather than failing — a softer failure than the above, but still misleading. |

Hardcoded-claim mappers are intentional: in the dogfood lab the
realm doesn't model tenants/roles as group attributes, so hardcoded
values are the simplest path. A realm that already encodes
tenant/role on the user (group attribute, custom user-attribute,
identity-provider mapping) can swap these for `oidc-usermodel-*`
mappers — keep the **claim names** identical (`tenant_id`,
`tenant_role`) because that's what the backplane validates against.

Validator-side errors at the decode stage are made specific by
[#797](https://github.com/evoila/meho/issues/797) (G0.9.1-T12):
once that lands, `invalid_audience` / `missing_sub` / `token_expired`
/ `invalid_signature` / `invalid_issuer` are distinguished in the
401 `detail` body instead of all collapsing to `invalid_token`.

#### Step 4 — Explicitly assign the 4 default client scopes

| Scope | Why | What breaks if it's missing |
| --- | --- | --- |
| `basic` | Carries the **Subject (sub) protocol mapper**. Keycloak 25 moved the `sub` claim out of the hardcoded token-generation path and into a protocol mapper inside this scope ([Keycloak 25 release notes](https://www.keycloak.org/2024/06/keycloak-2500-released) — the change persists in Keycloak 26+). RFC 9068 §2.2.1 makes `sub` REQUIRED on JWT access tokens, so a token without it is correctly rejected — the diagnostic is the opaque part. | Wall #3: every backplane call returns 401 `invalid_token` with no hint that `sub` is missing. This is the deepest wall — every other claim is present, the token *looks* well-formed, and the breakage moves with the realm regardless of how the client was created. |
| `roles` | Carries realm-roles + client-roles into the token. | Group-gated tools may report empty / unauthorised results. |
| `web-origins` | Allowed CORS origins for browser-driven flows. | Browser-driven MCP clients (Claude.ai Custom Connector) fail CORS pre-flight. |
| `acr` | Authentication Context Class Reference — drives step-up auth. | Step-up flows misbehave; not load-bearing for first login but cheap to ship. |

> **The `basic`/`sub` gotcha (load-bearing).** Clients **created via
> the Keycloak admin REST API do not auto-inherit the realm's
> default-default client scopes** the way the admin-console UI's
> "Create" button does. The console populates `defaultClientScopes`
> on the new client to the realm's "Default" set; `kcadm.sh` /
> direct admin-API `POST /clients` does not, unless the request body
> explicitly includes a `defaultClientScopes` array. Re-using a
> realm-default scope **name** in `optionalClientScopes` doesn't
> back-fill it as default either. The recovery is to set
> `defaultClientScopes: ["basic","roles","web-origins","acr"]`
> explicitly in the admin-API request body, or to re-add each scope
> via the admin console afterwards. The consumer's reference script
> at [`scripts/keycloak-bootstrap-meho-cli.sh`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/issues/670)
> sets all four explicitly to be safe.

#### Step 5 — Provision a user in `meho-admins` with a password

The realm ships with zero human users — prior lab work rode
`client_credentials` and never needed one. Device-code, by RFC 8628
§3.4, requires a real user to approve the verification URL.

Create at least one user (any username; the dogfood lab uses the
operator's email) with:

- A password (any policy-compliant value; the user is prompted to
  change it on first login if the realm's password policy says so).
- Group membership: `meho-admins` (so the `groups-claim` mapper at
  Step 3 emits `meho-admins` and the backplane's group-gated tools
  are reachable).

The user's `email_verified` flag does not need to be true for
device-code to complete; if your realm enforces verified email
elsewhere, set it true on this user.

### MCP onramp — public `meho-mcp-client` (Claude.ai / Claude Desktop / MCP Inspector)

Repeat Steps 2–4 for a second public client used by MCP clients
that **can** carry `client_id` in their config (Claude.ai Custom
Connector, MCP Inspector). Suggested client ID `meho-mcp-client`;
client-shape contrast with `meho-cli`:

| Setting | `meho-mcp-client` | Why differs from `meho-cli` |
| --- | --- | --- |
| Standard flow (authorization-code + PKCE) | **On** | MCP 2025-06-18 mandates OAuth 2.1 authorization-code + PKCE for the `/mcp` path; the device grant is a CLI-only convenience. |
| Device grant | Off | Not needed; MCP clients run the browser-redirect flow. |
| Valid redirect URIs | `https://claude.ai/api/mcp/auth_callback`, `http://localhost:*` | Covers the Claude.ai Custom Connector and any localhost MCP Inspector. |
| Web origins | `+` (or a tight allow-list) | CORS for the browser flow. |
| PKCE challenge method | `S256` | Spec-required for public-client PKCE. |

The 5 protocol mappers + 4 default client scopes from Steps 3–4
apply identically. The recipe at
[`docs/cross-repo/mcp-client-setup.md`](../../docs/cross-repo/mcp-client-setup.md)
documents the per-client configuration step for each MCP client.

> **`.mcp.json` `client_id` limitation (Claude Code + Cursor).**
> Claude Code's HTTP-MCP support and Cursor's MCP wire-up
> as of 2026-05 follow the RFC 9728 metadata trail to the
> Keycloak authorization server but **do not expose a
> `client_id` field in their `.mcp.json` shape**. They
> attempt dynamic client registration (RFC 7591) against the
> Keycloak `clients-registrations/openid-connect` endpoint
> and hit Keycloak's Trusted Hosts policy — which ships with
> an empty whitelist, so anonymous DCR is **de-facto
> disabled** ([Keycloak docs](https://www.keycloak.org/securing-apps/client-registration)).
> The deployer-side fix (registering a public client by hand)
> doesn't help these clients until they expose `client_id` in
> `.mcp.json`. Two workarounds today: (a) use Claude.ai
> Custom Connector or MCP Inspector for first-class wire-up
> against `meho-mcp-client`; (b) shim Claude Code / Cursor
> through `mcp-remote` (or an equivalent stdio→HTTP proxy)
> and bake the Bearer token into the wrapper. The right
> long-term fix is upstream MCP-client `client_id` support,
> not opening DCR on a prod realm.

### Wire it into Helm

Set the chart value to the CLI client_id you created at Step 2:

```yaml
config:
  keycloakCliClientId: meho-cli   # or whatever you chose
```

The backplane's `/api/v1/auth-config` endpoint surfaces this value
as the `cli_client_id` JSON field, which `meho login`'s discovery
parser maps to the OAuth `client_id`. The CLI also accepts
`--client-id <id>` as a per-invocation override, useful when a
deployer publishes multiple public clients (e.g. `meho-cli-prod`,
`meho-cli-staging`) and the chart value pins one default.

The MCP public client (`meho-mcp-client`) is not chart-wired today
— Claude.ai's Custom Connector and MCP Inspector both ask the
operator to paste a client_id at config time. Auto-discovery via
RFC 9728 advertises the authorization server but not the
`client_id` (the metadata document defines that as an out-of-band
parameter, intentionally).

### Verify

After `helm install` / `helm upgrade` and the realm steps above:

```bash
# 1. Auth-config endpoint carries cli_client_id.
curl -sf https://meho.evba.lab/api/v1/auth-config | jq .
# {
#   "keycloak_issuer": "https://keycloak.evba.lab/realms/evba",
#   "audience": "meho-backplane",
#   "cli_client_id": "meho-cli"
# }

# 2. CLI device-code flow completes.
meho login https://meho.evba.lab
# Logged in to https://meho.evba.lab; token stored in keyring.

# 3. Authenticated REST call succeeds.
curl -sf -H "Authorization: Bearer $(meho status --print-token)" \
  https://meho.evba.lab/api/v1/health
# {"status": "ok"}

# 4. MCP RFC 9728 metadata document is reachable.
curl -sf https://meho.evba.lab/.well-known/oauth-protected-resource | jq .
# {
#   "resource": "https://meho.evba.lab/mcp",
#   "authorization_servers": ["https://keycloak.evba.lab/realms/evba"],
#   ...
# }
```

### Four-wall symptom → cause → fix matrix

The deployer's first-login walk hits these four walls in sequence;
each surfaces with a different symptom but the cause-and-fix chain
is bounded. Walls are numbered for the dogfood-report cross-reference.

| Wall | Symptom | Cause | Fix |
| --- | --- | --- | --- |
| **W1** | Device-code initiation 401s with `{"error":"unauthorized_client","error_description":"Invalid client or Invalid client credentials"}`. Or DCR 403s with `{"error":"insufficient_scope","error_description":"Policy 'Trusted Hosts' rejected request to client-registration service. Details: Host not trusted."}`. | The CLI / MCP client tried to drive the device or authorization-code grant against a **confidential** client (typically `meho-backplane`), or anonymous DCR against a realm whose Trusted Hosts policy ships with an empty whitelist. Confidential clients require a `client_secret` the CLI can't carry; DCR on a prod realm is closed by design ([Keycloak docs](https://www.keycloak.org/securing-apps/client-registration)). | Pre-create a **public** client per Step 2 (and Step 2-MCP for the MCP path). Set `config.keycloakCliClientId` to the CLI client's ID so `/api/v1/auth-config` surfaces it. Operators on older backplanes can pass `--client-id <id>` per-invocation. Don't open DCR — the right fix is a deployer-side public client. |
| **W2** | Token issuance succeeds; every backend call 401s with `{"detail":"invalid_token"}` (specifics post-[#797](https://github.com/evoila/meho/issues/797): `invalid_audience` / `missing_tenant_claim` / `missing_tenant_role_claim`). | The public client mints tokens with a different claim shape than `meho-backplane` validates against — missing the `audience-meho-backplane` mapper (wrong `aud`), the `meho-mcp-audience` mapper (no MCP audience), the `tenant-id` mapper, or the `tenant-role` mapper. | Clone all 5 mappers from the `meho-backplane` client onto the public client per Step 3. After the fix, decode the issued token (`jwt.io` or `kcadm.sh evaluate-protocol-mappers`) and confirm `aud` is an array containing both `meho-backplane` and `<backplane-url>/mcp`, plus `tenant_id`, `tenant_role`, `groups` are present. |
| **W3** | Token issuance succeeds; every backend call 401s with `{"detail":"invalid_token"}` even after Wall #2 is closed; decoded token has `aud`, `tenant_id`, `tenant_role`, `groups` but **no `sub` claim**. | The `basic` client scope wasn't assigned. Keycloak 25 moved `sub` into the Subject (sub) protocol mapper inside the `basic` scope; clients created via the admin REST API don't auto-inherit realm default-default scopes the way the admin-console UI populates them. RFC 9068 §2.2.1 makes `sub` REQUIRED on JWT access tokens, so rejection is spec-correct — the diagnostic is the opaque part. | Add `basic`, `roles`, `web-origins`, `acr` to the public client's **default** client scopes per Step 4. Re-issue (logout and re-login; existing tokens are stale) and confirm `sub` is now present. After [#797](https://github.com/evoila/meho/issues/797) lands the symptom is `{"detail":"missing_sub"}` instead of the opaque form. |
| **W4** | `meho login` fails with `meho: token exchange failed: context deadline exceeded`, often before the human has a chance to approve the verification URL. | Not the device-code TTL (which is already 10 minutes per `cli/internal/auth/devicecode.go:355`'s `PollTimeout = 10 * time.Minute` — longer than Keycloak's default 600 s `expires_in`). The real cause is an **ambient parent deadline** on `cmd.Context()` (CI step timeout, `claude` bash-tool timeout, IDE-task wrapper) that truncates the approval wait far below the device-code lifetime. | (Until [#798](https://github.com/evoila/meho/issues/798) lands) run `meho login` in a real interactive terminal without a short wrapper deadline; or raise the wrapper timeout to ≥ `expires_in` + headroom. After #798 lands the message distinguishes "parent deadline fired" from "device code expired" and the device-flow approval wait detaches from a too-short parent context. |

### Out of scope for this recipe

- **The broader Deploying-MEHO guide.** This recipe is one entry in
  the consolidated deployment guide tracked under
  [#559](https://github.com/evoila/meho/issues/559) (deployment-
  friction umbrella). When #559 lands, this recipe moves there
  verbatim; until then `deploy/values-examples/README.md` is the
  authoritative deployer doc for the chart values + the auth-onramp
  recipe.
- **Automation of the recipe.** [#791](https://github.com/evoila/meho/issues/791)
  (G0.9.1-T11) ships `meho admin keycloak bootstrap-cli-client` — an
  idempotent verb that creates the two public clients + 5 mappers +
  4 default scopes from one invocation. The recipe above is the
  manual path until that automation lands.
- **Confidential `meho-backplane` client.** Out of scope — it's
  created at realm-install time alongside the Keycloak realm itself
  and isn't touched by this recipe. Rotating its client secret is
  also out of scope.
- **RFC 7591 Dynamic Client Registration support in MEHO.** A real
  feature with its own design tradeoffs (which clients are allowed
  to self-register; how operators audit unknown registrations);
  tracked separately, not a v0.3.2 onramp fix. `docs/cross-repo/mcp-client-setup.md`
  notes it as future work.
- **The CLI device-code client provisioning + auth-config endpoint
  completion.** [#789](https://github.com/evoila/meho/issues/789)
  (G0.9.1-T9) — already landed in v0.3.2.
- **Token-validator error specificity at the decode stage.**
  [#797](https://github.com/evoila/meho/issues/797) (G0.9.1-T12)
  makes Wall #2 / Wall #3 symptoms specific (`invalid_audience` /
  `missing_sub` / …) instead of opaque `invalid_token`.
- **The `meho login` context-deadline fix.**
  [#798](https://github.com/evoila/meho/issues/798) (G0.9.1-T13)
  closes Wall #4 in code.

## End-to-end install flow

The order matters because the chart's migration Job runs as a
`pre-install,pre-upgrade` hook and refuses to start without
`DATABASE_URL`.

```bash
# 0. Once per cluster: install External Secrets Operator.
helm repo add external-secrets https://charts.external-secrets.io
helm install external-secrets external-secrets/external-secrets \
  -n external-secrets --create-namespace

# 1. Once per cluster: create the ClusterSecretStore pointing at Vault.
#    (Owned by your GitOps repo, not this chart — see above.)
kubectl apply -f cluster-secret-store-vault.yaml

# 2. Per release: provision the ExternalSecret(s) the chart references.
#    Skip this step if you set `eso.enabled: true` in values — the chart
#    renders them itself.
kubectl apply -n meho -f externalsecret-meho-postgres.yaml

# 3. Wait for the target Secret to materialise. The chart's pre-install
#    migration Job mounts it; if it isn't ready, the Job fails fast.
kubectl -n meho wait --for=create secret/meho-postgres --timeout=60s
kubectl -n meho get externalsecret meho-postgres -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'
# → "True"

# 4. Apply the chart.
helm upgrade --install meho ./deploy/charts/meho/ \
  --namespace meho --create-namespace \
  -f values-rdc.yaml
```

## Out of scope for v0.1

- The actual `values-rdc.yaml` (private, lives in the consumer's repo).
- The `ClusterSecretStore` / `SecretStore` manifest itself — consumer-owned.
- Multi-environment overlays (staging, prod) — v0.1 ships only the lab shape.
- Helm `--values=secret://` plugin integration — v0.2 if the dual-source
  ExternalSecret pattern proves friction-heavy.
- ApplicationSet (ArgoCD) — v0.2.

## References

- Parent Goal: [#11 — Deployable v0.1](https://github.com/evoila-bosnia/meho-internal/issues/11)
- Parent Initiative: [#36 — G2.5 Helm chart](https://github.com/evoila-bosnia/meho-internal/issues/36)
- This task: [#40 — values-rdc-example.yaml + ESO sync patterns documented](https://github.com/evoila-bosnia/meho-internal/issues/40)
- External Secrets Operator: <https://external-secrets.io/>
- ESO Vault provider: <https://external-secrets.io/latest/provider/hashicorp-vault/>
- ESO ExternalSecret API: <https://external-secrets.io/latest/api/externalsecret/>
- Chart documentation: [`docs/codebase/devops.md`](../../docs/codebase/devops.md)
