<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Keycloak agent-client recipe — RFC 8693 token-exchange delegation and `client_credentials`

> Cross-repo handshake between `evoila/meho` (this repo, producer of
> the G11.2 agent identity primitives) and the operator's Keycloak
> realm (consumer side; not a single repo — every MEHO deployment has
> its own realm).
>
> G11.2-T2 (#816). Two agent-authentication flows are documented here:
> delegation (user-triggered run via RFC 8693 token exchange) and
> autonomous (`client_credentials`). Both flows use the same Keycloak
> client.

## Why this doc exists

MEHO agents are Keycloak principals. When a human triggers an agent
run, the audit must record *both* who asked (the user) and what acted
(the agent) — the RFC 8693 "delegation" model. When no human is
involved (scheduled / cron runs), the agent authenticates as itself
via `client_credentials`.

This doc specifies the Keycloak realm-side setup for both flows,
mirroring [`keycloak-tenant-claims.md`](./keycloak-tenant-claims.md)
(tenant + role claim mappers) and [`keycloak-web-client.md`](./keycloak-web-client.md)
(BFF confidential client) as precedents.

## Keycloak prerequisites

- Keycloak **26.2 or later** — RFC 8693 Standard Token Exchange is GA
  from 26.2. Earlier Keycloak versions only supported the legacy (non-
  standard) token-exchange, which has different request parameters.
- The existing `meho-backplane` resource-server client (or whichever
  client emits the `KEYCLOAK_AUDIENCE` the backplane validates)
  already exists in the realm.

## Step 1 — Create the agent Keycloak client

In the Keycloak Admin Console for your realm:

1. **Clients → Create client**
   - Client type: `OpenID Connect`
   - Client ID: `meho-agent` (suggested; set `AGENT_TOKEN_EXCHANGE_CLIENT_ID`
     to match)
   - Name: `MEHO Agent`

2. **Capability config** (next screen):
   - Client authentication: `ON` (confidential client — it will use a
     client secret for both `client_credentials` and as the actor in
     token exchanges)
   - Authorization: `OFF`
   - Standard flow: `OFF`
   - Direct access grants: `OFF`
   - **Service accounts enabled: `ON`** — required for `client_credentials`
     autonomous runs
   - **Standard Token Exchange: `ON`** — required for RFC 8693 delegation;
     this is the Keycloak 26.2 toggle that enables standard exchange

3. **Save** → go to the **Credentials** tab → copy the `Client secret`.
   Store it in Vault and render it into `AGENT_TOKEN_EXCHANGE_CLIENT_SECRET`.

## Step 2 — Add claim mappers on the agent client

The agent's service-account token must carry the same tenant claims
the backplane expects on every JWT (`tenant_id`, `tenant_role`). Add
the same protocol mappers documented in
[`keycloak-tenant-claims.md`](./keycloak-tenant-claims.md) to the
agent client:

| Mapper type | Token claim name | Claim value | Token type |
|---|---|---|---|
| Hardcoded claim | `tenant_id` | The tenant UUID the agent belongs to | Access token |
| Hardcoded claim | `tenant_role` | `operator` (or `read_only` for read-only agents) | Access token |

Without these mappers the backplane's `verify_jwt` rejects the token
with `missing_tenant_claim` or `missing_tenant_role_claim`.

## Step 3 — Add an audience mapper for the backplane

The agent's token must carry `meho-backplane` (or your
`KEYCLOAK_AUDIENCE` value) in the `aud` claim so `verify_jwt` accepts
it:

In the agent client → **Client scopes** tab → open the client-specific
scope (`meho-agent-dedicated`) → **Add mapper → By configuration →
Audience**:

| Field | Value |
|---|---|
| Name | `backplane-audience` |
| Included Client Audience | `meho-backplane` (the `KEYCLOAK_AUDIENCE` value) |
| Add to access token | `ON` |

## Step 4 — Grant `may_act` permission on the backplane resource server

For the delegation flow to succeed, Keycloak must permit the agent
client to act on behalf of users at the `meho-backplane` resource
server. In Keycloak 26.2 Standard Token Exchange this is configured
as a **token-exchange permission** on the target client:

1. Open the **`meho-backplane`** client (the resource-server client
   whose tokens the backplane validates).
2. Go to **Permissions** tab → enable **Permissions** if not already
   enabled.
3. Click **token-exchange** → **Create policy → Client policy**:
   - Name: `meho-agent-may-act`
   - Clients: select `meho-agent`
   - Logic: Positive
4. Back in the `token-exchange` permission → assign the
   `meho-agent-may-act` policy.

Without this step Keycloak returns `invalid_target` (mapped to
`TokenExchangeExchangeRefusedError` by MEHO), and the agent run fails
with a 403 before it executes any tool.

## Step 5 — Configure MEHO backplane env vars

| Env var | Value | Notes |
|---|---|---|
| `AGENT_TOKEN_EXCHANGE_CLIENT_ID` | `meho-agent` | The client_id from Step 1 |
| `AGENT_TOKEN_EXCHANGE_CLIENT_SECRET` | `<from Vault>` | The client secret from Step 1 |
| `KEYCLOAK_ISSUER_URL` | `https://<host>/realms/<realm>` | Already required for `verify_jwt` |
| `KEYCLOAK_AUDIENCE` | `meho-backplane` | Already required for `verify_jwt` |

## How the two flows work

### User-triggered (delegation) flow

```
User browser/CLI              MEHO backplane               Keycloak
     │                              │                           │
     │──POST /agents/{n}/run ──────>│                           │
     │   Authorization: Bearer      │                           │
     │   <user_token>               │                           │
     │                              │──POST /token ────────────>│
     │                              │  grant_type=client_creds  │
     │                              │  (agent authenticates)    │
     │                              │<─ actor_token ────────────│
     │                              │                           │
     │                              │──POST /token ────────────>│
     │                              │  grant_type=token-exchange│
     │                              │  subject_token=user_token │
     │                              │  actor_token=agent_token  │
     │                              │<─ delegated_token ────────│
     │                              │  (sub=user, act=agent)    │
     │                              │                           │
     │                              │  run under delegated_token│
     │                              │  audit: operator_sub=user │
     │                              │         actor_sub=agent   │
```

The resulting `audit_log` row records:

| Column | Value |
|---|---|
| `operator_sub` | The user's Keycloak `sub` (who initiated) |
| `actor_sub` | The agent's Keycloak `sub` (who acted) |

### Autonomous (client_credentials) flow

```
Scheduler / cron              MEHO backplane               Keycloak
     │                              │                           │
     │──trigger agent run ─────────>│                           │
     │                              │──POST /token ────────────>│
     │                              │  grant_type=client_creds  │
     │                              │<─ agent_token ────────────│
     │                              │  (sub=agent, no act)      │
     │                              │                           │
     │                              │  run under agent_token    │
     │                              │  audit: operator_sub=agent│
     │                              │         actor_sub=NULL    │
```

## Verification

After setup, verify the delegation exchange works:

```bash
# 1. Get a user token
USER_TOKEN=$(curl -s -X POST \
  "${KEYCLOAK_ISSUER_URL}/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=meho-cli&username=${USER}&password=${PASS}" \
  | jq -r .access_token)

# 2. Perform the exchange (MEHO does this internally; this is for verification)
DELEGATED=$(curl -s -X POST \
  "${KEYCLOAK_ISSUER_URL}/protocol/openid-connect/token" \
  -u "meho-agent:${AGENT_SECRET}" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:token-exchange" \
  -d "subject_token=${USER_TOKEN}" \
  -d "subject_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "actor_token=$(curl -s -X POST \
      "${KEYCLOAK_ISSUER_URL}/protocol/openid-connect/token" \
      -u "meho-agent:${AGENT_SECRET}" \
      -d "grant_type=client_credentials" | jq -r .access_token)" \
  -d "actor_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "requested_token_type=urn:ietf:params:oauth:token-type:access_token" \
  -d "audience=meho-backplane" \
  | jq -r .access_token)

# 3. Decode the delegated token — check sub and act claims
echo "${DELEGATED}" | cut -d. -f2 | base64 -d 2>/dev/null | jq '{sub, act}'
# Expected: {"sub": "<user-sub>", "act": {"sub": "<agent-sub>"}}
```

## Failure modes and diagnostics

| Keycloak error | MEHO exception | Cause | Fix |
|---|---|---|---|
| `invalid_target` | `TokenExchangeExchangeRefusedError` | `may_act` permission not granted | Step 4 above |
| `access_denied` | `TokenExchangeExchangeRefusedError` | Policy denied (client policy not matching) | Check Step 4 policy |
| `unauthorized_client` | `TokenExchangeExchangeRefusedError` | Standard Token Exchange not enabled on `meho-agent` | Step 1: enable Standard Token Exchange toggle |
| `invalid_client` | `TokenExchangeError` | Wrong `client_secret` | Step 3: re-copy secret from Keycloak Credentials tab |
| `missing_access_token` | `TokenExchangeError` | Unexpected Keycloak 200 response (rare) | Check Keycloak logs for misconfiguration |
| `network_error` | `TokenExchangeError` | `KEYCLOAK_ISSUER_URL` unreachable | Check network / TLS / URL |

All errors are logged by MEHO at `warning` level with `client_id` and
the Keycloak `error` code. Client secrets are **never** logged.

## References

- RFC 8693 OAuth 2.0 Token Exchange: https://datatracker.ietf.org/doc/html/rfc8693
- Keycloak 26.2 Standard Token Exchange announcement:
  https://www.keycloak.org/2025/05/standard-token-exchange-kc-26-2
- MEHO tenant + role claim mappers: [`keycloak-tenant-claims.md`](./keycloak-tenant-claims.md)
- G11.2-T2 implementation: `backend/src/meho_backplane/auth/token_exchange.py`
