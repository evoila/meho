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
| `meho-mcp` | **Public** client, authorization-code + PKCE | Browser-capable MCP clients (Claude Desktop, MCP Inspector, …) |
| 6 protocol mappers | On both public clients | Make issued tokens carry the claims the backplane validates |
| 4 default client scopes | On both public clients | Including the `basic` scope that carries the mandatory `sub` claim |
| `mcp:read`, `mcp:execute`, `mcp:admin` | Client scopes, optional on `meho-mcp` | The scopes MEHO's MCP clients ask for at login |
| `MEHO-Readers`, `MEHO-Operators`, `MEHO-SA-Operators`, `MEHO-Admins`, `MEHO-Approvers` | Top-level groups with attributes | Give each person their tenant and their role |
| A user in `MEHO-Admins` | Realm user | The first admin: approves device-code logins and operates MEHO |

!!! tip "There is an automation for this"

    The CLI ships an idempotent helper — `meho admin keycloak
    bootstrap-clients` — that creates the two public clients, the six
    mappers, the four default scopes, the three `mcp:*` scopes, and the
    five groups, and puts the first admin user in `MEHO-Admins`, in
    one invocation. This page is the
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
`meho login` contacts **both** hosts, so verify both from a fresh shell
before continuing:

```bash
curl -sf https://<backplane-host>/healthz
curl -sf https://<keycloak-host>/realms/<realm>/.well-known/openid-configuration
```

An `x509: certificate signed by unknown authority` from `meho login`
later means one of these two was skipped.

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

## Step 3 — Add the six protocol mappers

Tokens minted by `meho-cli` must carry the same claim shape the
backplane validates — otherwise tokens are issued cleanly and then
rejected on every call. Add these six mappers to the client:

| Mapper name | Type | Output claim | Without it |
|---|---|---|---|
| `audience-meho-backplane` | Audience mapper | adds `meho-backplane` to `aud` | Every call rejected with `invalid_audience`. |
| `meho-mcp-audience` | Audience mapper (**Included Custom Audience** = `https://<backplane-host>/mcp`, **no trailing slash**) | adds the MCP resource URI to `aud` | Tokens work for the REST API but not for `/mcp`. |
| `tenant-id` | User Attribute (**User Attribute** = `tenant_id`, **Claim JSON Type** `String`) | `tenant_id`, from the person's group | Rejected with `missing_tenant_claim`. |
| `tenant-role` | User Attribute (**User Attribute** = `tenant_role`, **Claim JSON Type** `String`) | `tenant_role`, from the person's group | Rejected with `missing_tenant_role_claim`. |
| `approver` | User Attribute (**User Attribute** = `approver`, **Claim JSON Type** `boolean`) | `approver`, only for `MEHO-Approvers` | Nobody with the `read_only` role can approve. |
| `groups-claim` | Group-membership mapper | `groups` | Group-gated surfaces silently return empty results. |

On the three User Attribute mappers, set **Token Claim Name** to the
same name as the attribute, **Add to access token** on, **Add to ID
token** off, **Add to userinfo** on, and keep **Multivalued** and
**Aggregate attribute values** off.

These mappers hold **no fixed value**. They read the attributes of the
person's group ([Step 5](#step-5-create-the-five-groups)): when a person
has no value of their own, the stock User Attribute mapper takes it
from the person's group. Never put a fixed `tenant_id` or `tenant_role`
(a Hardcoded claim mapper) on these clients: everyone who logs in
through them would get the same tenant and role. The full recipe and
the reasons are in the
[tenant claims recipe](https://github.com/evoila/meho/blob/main/docs/cross-repo/keycloak-tenant-claims.md)
and the
[decision record](https://github.com/evoila/meho/blob/main/docs/decisions/login-role-groups.md).

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

## Step 5 — Create the five groups

A person gets their tenant and their role from their group. Create
these five groups at the **top level** (not inside another group), and
give each one these **attributes** (open the group → **Attributes**):

| Group | `tenant_id` | `tenant_role` | `approver` |
|---|---|---|---|
| `MEHO-Readers` | your tenant UUID | `read_only` | – |
| `MEHO-Operators` | your tenant UUID | `operator` | – |
| `MEHO-SA-Operators` | your tenant UUID | `operator` | – |
| `MEHO-Admins` | your tenant UUID | `tenant_admin` | – |
| `MEHO-Approvers` | your tenant UUID | `read_only` | `true` |

All five groups carry the same tenant UUID (mint one with `uuidgen`).
`tenant_role` uses an underscore (`read_only`), never a hyphen.

Then put each person in **exactly one** of the five groups:

- A person in **no** group gets no tenant. MEHO refuses the login with
  `401 missing_tenant_claim`.
- A person in **two** groups gets one of the two roles, and you cannot
  tell which one.
- To change a person's role, move them to another group. The change
  applies at their next login.

`meho admin keycloak bootstrap-clients --tenant-id <uuid>` creates the
five groups for you.

## Step 6 — Create an operator user

Device-code login requires a real user to approve the request — a
fresh realm prepared only for service-to-service traffic may not have
one. Create at least one user with:

- a password (whatever your realm policy requires), and
- membership in the **`MEHO-Admins`** group, so the user gets the
  `tenant_admin` role.

The user's email does not need to be verified for device-code login to
complete.

## The MCP client (`meho-mcp`)

Browser-capable MCP clients authenticate with authorization-code +
PKCE, which needs a second public client. Repeat Steps 2–4 with these
differences:

| Setting | `meho-mcp` | Why it differs |
|---|---|---|
| Standard flow (authorization-code + PKCE) | **On** | The MCP specification mandates OAuth 2.1 authorization-code + PKCE. |
| Device grant | Off | Not used by MCP clients. |
| Valid redirect URIs | **Loopback only** — `http://localhost:<port>/callback` (Claude Code) plus `http://localhost:<port>/oauth/callback` and its `http://127.0.0.1:<port>/oauth/callback` twin (the `mcp-remote` shim) | MEHO is internal-only, so there is **no** public `claude.ai` Custom-Connector redirect. `<port>` matches the client's configured callback port; the exact per-client values are in [Connect clients](../clients/index.md). |
| PKCE challenge method | `S256` | Spec-required for public clients. |
| `offline_access` client scope | Assign as **Optional** | The MEHO Claude Code plugin, the Claude Desktop bundle, and Claude Code's own HTTP client ask for it, so the login lasts past the realm's SSO idle limit. Keycloak refuses a login that asks for a scope the client does not offer: it answers `invalid_scope` and issues no token. `meho-cli` does not get this scope by default. |
| `mcp:read`, `mcp:execute`, `mcp:admin` client scopes | Create each, then assign as **Optional** | The Claude Code plugin and the Claude Desktop bundle ask for `mcp:read mcp:execute` at every login, and for `mcp:admin` when you turn on the operator tools. Keycloak does not ship these scopes. Create each one under **Client scopes → Create client scope**: protocol **OpenID Connect**, **Include in token scope** On. Then add all three to `meho-mcp` as **Optional**. If one is missing, Keycloak refuses the login with `invalid_scope`. `meho-cli` does not get them. |
| Client Offline Session Idle (attribute `client.offline.session.idle.timeout`) | **`172800`** seconds (48 hours) | Sets how long the MCP login lasts with no use. Without it, the realm's Offline Session Idle applies (30 days by default). If you set up the client by hand, set it on the **Advanced** tab of the client, or with `kcadm.sh update clients/<client-uuid> -r <realm> -s 'attributes."client.offline.session.idle.timeout"=172800'`. |

The six mappers and four default scopes apply identically. Which MCP
client uses which flow — and everything else client-side — is the
[Connect clients](../clients/index.md) section's territory.

### How long an MCP login lasts

With `offline_access`, Keycloak issues an **offline** refresh token. The
login then lasts until it has not been used for the offline idle limit
above: 48 hours with the recommended setting. Using MEHO renews the
token, which starts the 48 hours again. If your realm turns on **Offline
Session Max Limited**, that maximum also applies.

Without `offline_access`, the refresh token ends at the realm's **SSO
Session Idle** limit, which is 30 minutes by default. After that, the next
MEHO call needs a new browser login.

Two more things must be true:

- The user has the realm role `offline_access`. Keycloak gives it to every
  user through the realm's default roles (`default-roles-<realm>`). Without
  it, the token request fails with "Offline tokens not allowed for the user
  or client".
- `meho admin keycloak bootstrap-clients` assigns `offline_access` and the
  three `mcp:*` scopes as optional scopes on `meho-mcp`. It also sets the 48-hour offline idle
  limit, on new and existing clients. To use a different limit, pass
  `--mcp-offline-session-idle=<seconds>`. Each run sets the value again,
  so a value changed by hand goes back to 48 hours unless you pass the
  flag.

## Approve-only principal

By default, clearing the approval queue needs the **operator** role. For
separation of duties you can instead grant a dedicated approver the
**`approver`** capability — an optional claim, orthogonal to the tenant
role. A principal provisioned as `read_only` role **plus**
`approver=true` may approve and reject parked requests but is refused
`call_operation`: "approve, but never operate." (See
[Approvals and break-glass](../guides/approvals-and-break-glass.md#a-third-path-a-segregated-approver).)

The claim is fail-closed — absent or malformed means *not* an approver.
To make a person an approver, put them in the **`MEHO-Approvers`**
group ([Step 5](#step-5-create-the-five-groups)), and in no other of the
five groups. That group carries `tenant_role` = `read_only` and
`approver` = `true`, and the `approver` mapper from
[Step 3](#step-3-add-the-six-protocol-mappers) copies it into the
token. Its **Token Claim Name** must match the backplane's
`JWT_APPROVER_CLAIM_NAME` (default `approver`). The person keeps a low
tenant role (`read_only`), so the capability adds approval power
without adding dispatch power.

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
# JWT payloads are unpadded base64url, so translate the alphabet and
# re-pad before decoding.
jq -r '.entries[].access_token' \
  "${XDG_CONFIG_HOME:-$HOME/.config}/meho/credentials.json" \
  | cut -d. -f2 | tr '_-' '/+' \
  | awk '{ n = length % 4; if (n) $0 = $0 substr("===", 1, 4 - n); print }' \
  | base64 -d | jq .
```

When all four pass, return to
[the install trail, Step 5](index.md#step-5-decide-ingress-tls-and-trust).

## If login fails

The first-login failures are finite and well-mapped. In the order they
usually appear:

| Symptom | Cause | Fix |
|---|---|---|
| `unauthorized_client` at login start | The client is confidential, or the device grant is off | Step 2 — public client, device grant **On**. |
| Token issued, every call 401s with `invalid_audience` / `missing_tenant_claim` / `missing_tenant_role_claim` | Missing protocol mappers, or the person is in none of the five groups | Step 3 — add all six; Step 5 — put the person in one group. Then log out and back in. |
| Everyone gets the same role, whatever their group | A fixed-value (Hardcoded claim) `tenant_role` mapper is still on the client | Delete it, or re-run `meho admin keycloak bootstrap-clients`, which replaces it. |
| Token issued, every call 401s with `invalid_token` or `missing_sub`; the decoded token has no `sub` | The `basic` scope is not a default scope on the client | Step 4 — assign it, re-login (existing tokens stay broken). |
| `meho login` times out before you can approve | An ambient wrapper timeout (CI step, IDE task) shorter than the approval wait | Run `meho login` in a real terminal, or raise the wrapper timeout. |
| MCP login fails with `invalid_scope` ("Invalid scopes: …") and the Keycloak login page never appears | The client asked for a scope that `meho-mcp` does not offer: `offline_access`, `mcp:read`, `mcp:execute` or `mcp:admin` | Create the scope if it is missing, then assign it as an **optional** scope on `meho-mcp` (see the MCP client table above). Or re-run `meho admin keycloak bootstrap-clients`. |
| `x509: certificate signed by unknown authority` | Workstation OS trust store missing your CA | Step 1 / [TLS and ingress](tls-ingress.md#your-workstation-os-trust-store). |

A fuller, client-by-client troubleshooting page ships with the
[Connect clients](../clients/index.md) section. Until then, the
in-repo
[auth onramp recipe](https://github.com/evoila/meho/blob/main/deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp)
carries the deepest version of this matrix, including advanced
realm shapes (per-audience capability claims, metadata-document
clients).
