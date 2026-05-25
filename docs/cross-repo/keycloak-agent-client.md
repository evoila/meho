<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Keycloak agent-client recipe — registering agent principals (G11.2-T1 #815)

> Cross-repo handshake between `evoila/meho` (producer of the
> agent-principal lifecycle surface) and the operator's Keycloak realm
> (consumer side; not a single repo — every MEHO deployment has its own
> realm).
>
> This page documents the `meho agent-principal register / list / revoke`
> commands, the REST + MCP equivalents, the Keycloak Admin API
> credentials needed to enable them, and the `principal_kind=agent`
> claim an agent carries in its access token. It is the upstream-side
> specification that every MEHO consumer deploying agents must satisfy.

## Why this doc exists

G11.2-T1 (#815, Initiative #803) adds an agent-identity layer to MEHO.
Before an agent can call the backplane it needs a Keycloak confidential
client (type `kind=agent`) whose service account can obtain
`client_credentials` access tokens. The `meho agent-principal register`
command (and its REST + MCP equivalents) automate this by calling the
Keycloak Admin REST API. Enabling that path requires three env vars and
a Keycloak service account on the backplane side. This doc is the recipe
the operator follows.

Without these settings every `register` and `revoke` call returns
**`503 keycloak_admin_not_configured`**.

## Concepts

### Agent principal

An agent principal is a Keycloak confidential client whose `clientId`
follows the convention `agent:<name>` (e.g. `agent:deploy-bot`). The
client has `serviceAccountsEnabled=true` so it can authenticate via
`client_credentials`; it has `standardFlowEnabled=false` so human-facing
browser flows are disabled. Three custom attributes are set:

| Attribute | Value |
|-----------|-------|
| `kind` | `"agent"` |
| `tenant_id` | the tenant UUID from the MEHO DB |
| `owner_sub` | OIDC sub of the human who registered the agent |

The MEHO DB records the Keycloak-assigned `id` (the internal UUID in the
client representation) so `meho agent-principal revoke` can call
`PUT /clients/{id}` to flip `enabled=false` instantly.

### Principal-kind claim

When an agent authenticates it obtains an access token. The token should
carry the custom claim:

```json
{ "principal_kind": "agent" }
```

The MEHO backplane reads this claim (default claim name:
`principal_kind`; override: `JWT_PRINCIPAL_KIND_CLAIM_NAME`) and sets
`Operator.principal_kind = PrincipalKind.AGENT`. Tokens without the
claim default to `PrincipalKind.USER` — all pre-G11.2 human-operator
tokens continue to work unchanged.

To add `principal_kind=agent` to every token issued for an agent client,
add a **Hardcoded claim** mapper to the client (or to a client scope):

| Field | Value |
|-------|-------|
| Mapper type | `Hardcoded Claim` |
| Token Claim Name | `principal_kind` |
| Claim value | `agent` |
| Claim JSON type | `String` |
| Add to access token | On |
| Add to ID token | Off |
| Add to userinfo | Off |

## Prerequisites

- Keycloak admin access to the realm where MEHO agents authenticate.
- A Keycloak **confidential client** (e.g. `meho-admin`) in that realm
  with `serviceAccountsEnabled=true`. Its service account must hold the
  Keycloak built-in role `manage-clients` in the target realm (see
  Step 2 below).
- The backplane already runs with `KEYCLOAK_ISSUER_URL` set and
  validated (`meho status` returns green).
- Keycloak version **22+** (Admin REST API v2 path shape).

## Step 1 — Create the `meho-admin` service-account client

In the Keycloak Admin Console for your realm:

1. **Clients → Create client**
2. **Client type**: OpenID Connect
3. **Client ID**: `meho-admin` (or any name; you will set
   `KEYCLOAK_ADMIN_CLIENT_ID` to match)
4. **Next** → enable **Client authentication** (confidential) and
   **Service accounts roles** → **Save**
5. **Credentials** tab → note the **Client secret** → copy it to
   `KEYCLOAK_ADMIN_CLIENT_SECRET`

## Step 2 — Grant `manage-clients` to the service account

The service account that backs `meho-admin` needs permission to
create and update clients. In Keycloak 22+:

1. **Clients → `meho-admin` → Service account roles** tab
2. **Assign role** → filter by **Client** → select the realm's
   `realm-management` client → assign `manage-clients`

> **Security note**: `manage-clients` allows creating any client in the
> realm. Restrict the `meho-admin` service account to only those
> operations if your realm's threat model requires it (Keycloak fine-
> grained admin permissions, available since Keycloak 24, can scope this
> to clients whose `clientId` starts with `agent:`).

## Step 3 — Set the backplane env vars

```shell
KEYCLOAK_ADMIN_URL=https://<keycloak-host>/admin/realms/<realm>
KEYCLOAK_ADMIN_CLIENT_ID=meho-admin
KEYCLOAK_ADMIN_CLIENT_SECRET=<secret from Step 1>
```

All three must be non-empty; the service raises
`keycloak_admin_not_configured` when any is blank.

`KEYCLOAK_ADMIN_URL` is the Admin REST API base for your realm —
typically `https://keycloak.example.com/admin/realms/meho`. The
backplane derives the token URL automatically from `KEYCLOAK_ISSUER_URL`
(`{issuer}/protocol/openid-connect/token`), so the issuer URL must
already be set.

## Step 4 — Register an agent principal

```shell
# CLI
meho agent-principal register deploy-bot

# REST
curl -X POST https://meho.example.com/api/v1/agent-principals \
  -H "Authorization: Bearer <operator-token>" \
  -H "Content-Type: application/json" \
  -d '{"name": "deploy-bot"}'
```

On success you get back the record including:

```json
{
  "id": "<uuid>",
  "name": "deploy-bot",
  "keycloak_client_id": "agent:deploy-bot",
  "keycloak_internal_id": "<keycloak-uuid>",
  "owner_sub": "<caller-sub>",
  "revoked": false,
  "created_by_sub": "<caller-sub>",
  "created_at": "...",
  "updated_at": "..."
}
```

Share the `keycloak_client_id` and Keycloak admin console URL with the
team responsible for the agent. They can obtain a `client_secret` from
the **Credentials** tab in the Keycloak Console for client
`agent:deploy-bot`.

## Step 5 — Add the principal_kind mapper (optional but recommended)

To have the agent's access token carry `principal_kind=agent`:

1. **Clients → `agent:deploy-bot` → Client scopes → `agent:deploy-bot`-dedicated**
2. **Add mapper → By configuration → Hardcoded claim**
3. Fill in the table from [Principal-kind claim](#principal-kind-claim)
   above.

Tokens issued for `agent:deploy-bot` will now carry
`"principal_kind": "agent"` and `Operator.principal_kind` will resolve
to `PrincipalKind.AGENT` in the backplane.

## Step 6 — Revoke (kill switch)

```shell
meho agent-principal revoke deploy-bot
```

This immediately sets `enabled=false` on the Keycloak client (blocking
new token grants) and marks the MEHO DB row `revoked=true`. In-flight
tokens remain valid until their `exp`. There is no un-revoke.

## Verification

```shell
# List active principals for your tenant
meho agent-principal list

# Show one
meho agent-principal list --json | jq '.principals[] | select(.name=="deploy-bot")'

# Confirm the Keycloak client exists
curl -H "Authorization: Bearer <admin-token>" \
  https://<keycloak-host>/admin/realms/<realm>/clients?search=agent:deploy-bot
```

## MCP surface

The same lifecycle is also available via MCP tools for agents that
manage their own sub-principals:

| Tool | Role | Description |
|------|------|-------------|
| `meho.agent_principals.list` | operator | List active principals |
| `meho.agent_principals.register` | tenant_admin | Register a new principal |
| `meho.agent_principals.revoke` | tenant_admin | Revoke (kill switch) |

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `503 keycloak_admin_not_configured` | `KEYCLOAK_ADMIN_URL` / credentials not set | Set the three env vars (Step 3) |
| `502 keycloak_admin_error` | Keycloak API returned an unexpected error | Check Keycloak logs; verify `meho-admin` has `manage-clients` |
| `409 agent_principal_already_exists` | A principal with that name already exists | Use `meho agent-principal list` to inspect; pick a different name |
| `404 agent_principal_not_found` | Revoke on an already-revoked or unknown name | Check `meho agent-principal list --include-revoked` |
| `401 missing_tenant_claim` | The operator token does not carry `tenant_id` | See [`keycloak-tenant-claims.md`](keycloak-tenant-claims.md) |

## Related

- [`keycloak-tenant-claims.md`](keycloak-tenant-claims.md) — realm-side
  protocol mapper recipe for `tenant_id` and `tenant_role`
- [`mcp-client-setup.md`](mcp-client-setup.md) — MCP browser-flow client
  provisioning (for human-facing MCP sessions, not agent clients)
- Issue [#815](https://github.com/evoila/meho/issues/815) — G11.2-T1
  implementation
- Initiative [#803](https://github.com/evoila/meho/issues/803) — G11.2
  Agent identity + RBAC + approval
