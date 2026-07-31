# Keycloak realm setup

MEHO does not ship an identity provider — it validates tokens issued
by **your** Keycloak. This page configures a realm so that both
`meho login` (the CLI's device-code flow) and MCP clients (browser
OAuth) can authenticate. It is the promoted, outsider-readable version
of the recipe MEHO's own labs deploy from.

**Where this fits:** you arrived from
[the install trail, Step 4](index.md#step-4-set-up-the-keycloak-realm).
Work through this page top to bottom, run the verification block at
the end, then return to the trail.

## What you are creating

| Object | Kind | Used by |
|---|---|---|
| `meho-backplane` | Confidential client | The backplane itself — the audience it validates tokens for. If your realm does not have it yet, create it first: a confidential client with ID `meho-backplane` and no login flows enabled — it exists as the resource-server identity whose client ID is the token audience (`keycloak.audience` in the chart values). |
| `meho-cli` | **Public** client, device-code grant | `meho login` |
| `meho-mcp-client` | **Public** client, authorization-code + PKCE | Browser-capable MCP clients (Claude Desktop, MCP Inspector, …) |
| 5 protocol mappers | On both public clients | Make issued tokens carry the claims the backplane validates |
| 4 default client scopes | On both public clients | Including the `basic` scope that carries the mandatory `sub` claim |
| A user in `meho-admins` | Realm user | The human who approves device-code logins and operates MEHO |

!!! tip "There is an automation for this"

    The CLI ships an idempotent helper — `meho admin keycloak
    bootstrap-clients` — that creates the two public clients, the five
    mappers, and the four scopes in one invocation. This page is the
    manual reference path (and what the helper encodes); use the helper
    once you have an admin credential wired up, use this page to
    understand or verify what it did.

## Step 1 — Trust the deployment's CA on your workstation

Skip this step if your backplane and Keycloak present certificates
from a public CA.

If they are signed by an internal CA, install that CA into your
workstation's **operating-system trust store** before anything else —
`meho login` and most MCP clients verify TLS against the OS store, not
against environment variables. Platform commands and the reasons are
on [TLS and ingress](tls-ingress.md#your-workstation-os-trust-store).
Verify with `curl -sf https://<backplane-host>/healthz` from a fresh
shell before continuing — an `x509: certificate signed by unknown
authority` from `meho login` later means this step was skipped.

## Step 2 — Create the public `meho-cli` client

In the realm that hosts `meho-backplane`, create a client with:

| Setting | Value | Why |
|---|---|---|
| Client ID | `meho-cli` (suggested — any identifier works) | Must match `config.keycloakCliClientId` in your chart values. |
| Client authentication | **Off** (public client) | The CLI has nowhere to store a secret; the device grant against a confidential client fails with `unauthorized_client`. |
| Standard flow | Off | The CLI does not run the browser redirect flow. |
| Direct access grants | Off | Password grant is out of scope by design. |
| Implicit flow | Off | Deprecated by OAuth 2.1. |
| Service accounts roles | Off | Public clients cannot hold credentials. |
| **OAuth 2.0 Device Authorization Grant** | **On** | This *is* `meho login` ([RFC 8628](https://www.rfc-editor.org/rfc/rfc8628)). |
| Valid redirect URIs | (none) | Device flow does not redirect. |

## Step 3 — Add the five protocol mappers

Tokens minted by `meho-cli` must carry the same claim shape the
backplane validates — otherwise tokens are issued cleanly and then
rejected on every call. Add these five mappers to the client (copy the
concrete values from the `meho-backplane` client in the same realm):

| Mapper name | Type | Output claim | Without it |
|---|---|---|---|
| `audience-meho-backplane` | Audience mapper | adds `meho-backplane` to `aud` | Every call rejected with `invalid_audience`. |
| `meho-mcp-audience` | Audience mapper (**Included Custom Audience** = `https://<backplane-host>/mcp`, **no trailing slash**) | adds the MCP resource URI to `aud` | Tokens work for the REST API but not for `/mcp`. |
| `tenant-id` | Hardcoded claim | `tenant_id` = the operator's tenant UUID | Rejected with `missing_tenant_claim`. |
| `tenant-role` | Hardcoded claim | `tenant_role` = one of `read_only` / `operator` / `tenant_admin` | Rejected with `missing_tenant_role_claim`. |
| `groups-claim` | Group-membership mapper | `groups` | Group-gated surfaces silently return empty results. |

Hardcoded-claim mappers are the simplest path for a realm that does
not already model tenants and roles on its users. If yours does (a
group attribute, a user attribute, an identity-provider mapping), use
the corresponding user-model mappers instead — but keep the **claim
names** exactly `tenant_id` and `tenant_role`; those names are what
the backplane validates.

## Step 4 — Assign the four default client scopes

Explicitly assign these as **default** client scopes on the client:

| Scope | Why |
|---|---|
| `basic` | Carries the **`sub`** claim. Since Keycloak 25, `sub` is emitted by a protocol mapper inside this scope rather than unconditionally — and a JWT access token without `sub` is (correctly) rejected. This is the single most opaque failure in the whole setup. |
| `roles` | Realm and client roles in the token. |
| `web-origins` | CORS origins for browser flows. |
| `acr` | Authentication context class; cheap to ship, needed for step-up flows. |

!!! warning "Creating clients via the admin API? Read this"

    Clients created through Keycloak's **admin REST API** (or
    `kcadm.sh`) do **not** auto-inherit the realm's default client
    scopes the way the admin-console "Create" button does. If you
    scripted Step 2, you must set
    `defaultClientScopes: ["basic","roles","web-origins","acr"]`
    explicitly in the request body — or add the scopes in the console
    afterwards. The symptom of missing `basic` is a token that *looks*
    complete but has no `sub`, and a wall of `invalid_token` /
    `missing_sub` responses.

## Step 5 — Create an operator user

Device-code login requires a real user to approve the request — a
fresh realm prepared only for service-to-service traffic may not have
one. Create at least one user with:

- a password (whatever your realm policy requires), and
- membership in the **`meho-admins`** group, so the `groups-claim`
  mapper emits it and group-gated surfaces are reachable.

The user's email does not need to be verified for device-code login to
complete.

## The MCP client (`meho-mcp-client`)

Browser-capable MCP clients authenticate with authorization-code +
PKCE, which needs a second public client. Repeat Steps 2–4 with these
differences:

| Setting | `meho-mcp-client` | Why it differs |
|---|---|---|
| Standard flow (authorization-code + PKCE) | **On** | The MCP specification mandates OAuth 2.1 authorization-code + PKCE. |
| Device grant | Off | Not used by MCP clients. |
| Valid redirect URIs | `https://claude.ai/api/mcp/auth_callback`, `http://localhost:*` | Covers the Claude / claude.ai callback and localhost tools. |
| PKCE challenge method | `S256` | Spec-required for public clients. |
| `offline_access` client scope | Assign as **Optional** | Some MCP clients always request a refresh token; without the scope the authorization request fails with `invalid_scope`. Deliberately *not* given to `meho-cli` — the CLI re-runs the device dance instead of holding a long-lived refresh token. |

The five mappers and four default scopes apply identically. Which MCP
client uses which flow — and everything else client-side — is the
[Connect clients](../clients/index.md) section's territory.

## Wire it into Helm

Tell the chart which client the CLI should use (already shown in
[the install trail, Step 6](index.md#step-6-write-your-values-file)):

```yaml
config:
  keycloakCliClientId: meho-cli
```

The backplane serves this at `/api/v1/auth-config`, which is how
`meho login` discovers it. `meho login --client-id <id>` overrides it
per invocation.

## Verify

```bash
# 1. The realm's OIDC discovery document is reachable and names your issuer.
curl -fsS "https://keycloak.example.com/realms/<realm>/.well-known/openid-configuration" | jq .issuer

# 2. The backplane advertises the CLI client.
curl -fsS https://meho.example.com/api/v1/auth-config | jq .
# → {"keycloak_issuer": "…", "audience": "meho-backplane", "cli_client_id": "meho-cli"}

# 3. Device-code login completes end to end.
meho login https://meho.example.com

# 4. The stored token is accepted end to end.
meho status
```

To check the **claim shape** — `aud` contains both `meho-backplane` and
`https://<backplane-host>/mcp`, and `sub`, `tenant_id`, `tenant_role`,
`groups` are all present — you need the raw token, and the CLI has no
verb that prints it. `meho login` stores it in the OS keyring
(Keychain, Secret Service, Wincred), falling back to a 0600-mode file
at `$XDG_CONFIG_HOME/meho/credentials.json` on headless hosts. Force
that file backend with `MEHO_KEYRING_DISABLE=1` when you need to read
the token yourself:

```bash
MEHO_KEYRING_DISABLE=1 meho login https://meho.example.com

# The file holds one entry per backplane; a fresh host has exactly one.
jq -r '.entries[].access_token' \
  "${XDG_CONFIG_HOME:-$HOME/.config}/meho/credentials.json" \
  | cut -d. -f2 | base64 -d 2>/dev/null | jq .
```

When all four pass, return to
[the install trail, Step 5](index.md#step-5-decide-ingress-tls-and-trust).

## If login fails

The first-login failures are finite and well-mapped. In the order they
usually appear:

| Symptom | Cause | Fix |
|---|---|---|
| `unauthorized_client` at login start | The client is confidential, or the device grant is off | Step 2 — public client, device grant **On**. |
| Token issued, every call 401s with `invalid_audience` / `missing_tenant_claim` / `missing_tenant_role_claim` | Missing protocol mappers | Step 3 — add all five, then log out and back in. |
| Token issued, every call 401s with `invalid_token` or `missing_sub`; the decoded token has no `sub` | The `basic` scope is not a default scope on the client | Step 4 — assign it, re-login (existing tokens stay broken). |
| `meho login` times out before you can approve | An ambient wrapper timeout (CI step, IDE task) shorter than the approval wait | Run `meho login` in a real terminal, or raise the wrapper timeout. |
| MCP client fails with `invalid_scope` naming `offline_access` | The scope is not assigned on `meho-mcp-client` | Assign `offline_access` as an **optional** scope (see the MCP client table above). |
| `x509: certificate signed by unknown authority` | Workstation OS trust store missing your CA | Step 1 / [TLS and ingress](tls-ingress.md#your-workstation-os-trust-store). |

A fuller, client-by-client troubleshooting page ships with the
[Connect clients](../clients/index.md) section. Until then, the
in-repo
[auth onramp recipe](https://github.com/evoila/meho/blob/main/deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp)
carries the deepest version of this matrix, including advanced
realm shapes (per-audience capability claims, metadata-document
clients).
