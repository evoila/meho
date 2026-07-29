<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# Keycloak `meho-web` client recipe — confidential client for the operator-console BFF

> Cross-repo handshake between `evoila/meho` (this repo, producer of
> the v0.2 backplane that **runs** the BFF login flow at
> `/ui/auth/*`) and the operator's Keycloak realm (consumer side;
> not a single repo — every MEHO deployment has its own realm).
>
> This page is the upstream-side **tracker** for the realm-side
> client + secret each consumer must provision before deploying the
> v0.2 operator-console UI. The configuration itself is
> operator-applied (Keycloak Admin Console + Vault secret-write); what
> lives here is the recipe the operator follows and the verification
> commands either side can run to prove the contract holds.

## Why this doc exists

The v0.2 operator-console (Initiative
[#337](https://github.com/evoila/meho/issues/337)) ships a
Backend-for-Frontend auth flow served by the backplane FastAPI
process at `/ui/auth/*`. The flow runs OAuth 2.1 Authorization Code +
PKCE against the operator's existing Keycloak realm, using a **new
confidential client** named `meho-web`. The client is distinct from:

- **`meho-cli`** (public client, device-code flow) — used by the
  `meho login` CLI, no secret. Tracked in
  [`mcp-client-setup.md`](./mcp-client-setup.md).
- **`meho-mcp`** or whichever client emits the `KEYCLOAK_AUDIENCE`
  the chassis JWT chain validates (`meho-backplane`). The
  resource-server identifier the backplane validates `aud` against,
  unchanged from v0.1.

The BFF flow needs its own client because:

1. The authorization-code grant runs server-side and carries a
   `client_secret` on the token-endpoint POST. Confidential client.
2. The redirect URI is exact-match
   (`https://meho.evba.lab/ui/auth/callback`) and Keycloak enforces
   this; the CLI client cannot share the redirect URI.
3. The PKCE flow is enforced (OAuth 2.1 mandates PKCE on every
   authorization-code grant). The CLI client runs device-code, a
   different flow.

The backplane cannot enforce realm configuration; it can only fail
closed when the client / secret / redirect URI is missing or wrong.
This doc is the contract that specifies what the realm must produce.

## Prerequisites

- Keycloak admin access to the realm where MEHO operators
  authenticate (the realm whose issuer URL is configured as
  `KEYCLOAK_ISSUER_URL` in the backplane's
  [`Settings`](../codebase/backend.md)).
- The realm already issues access tokens carrying the `tenant_id` +
  `tenant_role` claims per
  [`keycloak-tenant-claims.md`](./keycloak-tenant-claims.md). The BFF
  validates the access token through the same chassis JWT chain
  `/api/*` uses, so the chassis recipe must already be in place.
- A populated Vault server reachable by the backplane (same Vault
  the existing CLI / MCP-client secrets live under). The
  `meho-web` client secret lands under a Vault path the deploy
  renders into the pod environment as `UI_KEYCLOAK_CLIENT_SECRET`.
- Keycloak version **22+** (matches the `meho-cli` / `meho-mcp`
  recipe baseline).

## Recipe

### Step 1 — Create the confidential client

In the Admin Console (logged in as a realm admin):

1. Navigate to **Clients** → **Create client**.
2. **Client type:** `OpenID Connect`.
3. **Client ID:** `meho-web` (this is the value rendered into the
   backplane as `UI_KEYCLOAK_CLIENT_ID`).
4. **Name:** `MEHO Operator Console` (cosmetic).
5. **Next** → **Capability config**:
   - **Client authentication:** **on** (this is what makes the
     client *confidential* — Keycloak will generate a secret on
     save).
   - **Authorization:** off (no Keycloak-Authorization-Services
     usage; the backplane does RBAC).
   - **Authentication flow:** check **Standard flow**
     (authorization-code grant). Leave the other grants off:
     **Direct access grants**, **Service accounts roles**, and
     **OAuth 2.0 Device Authorization Grant** all stay off — the
     BFF uses authorization-code only, and enabling the other
     grants on the same client widens the attack surface for no
     gain.
6. **Next** → **Login settings**:
   - **Root URL:** `https://meho.evba.lab` (the deploy's public
     URL).
   - **Home URL:** `https://meho.evba.lab/ui/`.
   - **Valid redirect URIs:**
     `https://meho.evba.lab/ui/auth/callback` — **exact match
     only**, no wildcards. Keycloak enforces exact match on the
     token-endpoint exchange; a mismatch surfaces as
     `invalid_grant` on the callback.
   - **Valid post-logout redirect URIs:**
     `https://meho.evba.lab/ui/auth/login` (the BFF logout flow
     bounces the operator back to this URL after the IdP-side
     end-session hop).
   - **Web origins:** leave blank — CORS is not needed (the BFF
     is same-origin with the operator-console UI).
7. **Save.**

### Step 2 — Pin PKCE and confirm the client-auth method

PKCE is mandatory on every OAuth 2.1 authorization-code flow,
including confidential clients (RFC 9700 §2.1.1; OAuth 2.0 for
Browser-Based Apps BCP §6.1). The backplane's BFF always sends
`code_challenge` + `code_challenge_method=S256` on the
authorization URL, so the realm must accept this shape:

1. On the `meho-web` client → **Advanced** tab.
2. **Proof Key for Code Exchange Code Challenge Method:** set to
   `S256`. (Leaving this unset lets the client send any method
   including `plain`, which OAuth 2.1 forbids; pinning `S256`
   here makes Keycloak reject a malformed BFF that drifted to
   `plain` for any reason.)
3. Confirm under the **Credentials** tab → **Client Authenticator**:
   `Client Id and Secret` (the default).
   - The token-endpoint authentication method `client_secret_post`
     is the conventional default for confidential clients;
     authlib picks this method on the BFF side. Keycloak accepts
     both `client_secret_post` and `client_secret_basic` — either
     works.
4. **Save.**

### Step 3 — Copy the client secret into a Secret the pod reads

> **Chart-native wiring (#2594).** The Helm chart now renders all three
> console env vars from first-class values — no hand-rolled `extraEnv`
> `valueFrom` needed. Set `config.uiKeycloakClientId` (plain, →
> `UI_KEYCLOAK_CLIENT_ID`) and `uiConsole.enabled: true` +
> `uiConsole.secretName` pointing at a Kubernetes Secret holding the
> client secret and the session key (→ `UI_KEYCLOAK_CLIENT_SECRET` /
> `UI_SESSION_ENCRYPTION_KEY` via `secretKeyRef`). The Vault path below
> is the recipe for a **Vault-backed** deploy; a **GSM / no-Vault**
> deploy skips Vault entirely and provisions a plain Kubernetes Secret
> (or a GSM-synced ExternalSecret) instead — the end-to-end recipe,
> including generating the session encryption key, is in
> [`deploy/values-examples/README.md`](../../deploy/values-examples/README.md)
> § operator-console (browser BFF).

1. On the `meho-web` client → **Credentials** tab.
2. Copy the **Client secret** value. Treat this value as
   sensitive; do not paste it into chat logs or commit it to a
   repo.
3. Write the secret to Vault under the path the deploy renders
   into the pod environment. Suggested layout (matches the
   existing CLI / MCP-client conventions; adjust to the
   deploy's path namespace):

   ```bash
   # Path layout: secret/meho/<environment>/ui/keycloak/web-client
   # The deploy renders the secret value into the pod's
   # UI_KEYCLOAK_CLIENT_SECRET env var via the same chain that
   # lands DATABASE_URL / UI_SESSION_ENCRYPTION_KEY.
   vault kv put secret/meho/prod/ui/keycloak/web-client \
     client_secret='<paste-value-here>'
   ```

   For the dogfooding lab the path is
   `secret/meho/evba-lab/ui/keycloak/web-client`; update the
   Helm values `extraEnv` block (or the `ExternalSecret` /
   `VaultSecret` CRD the deploy already uses for
   `DATABASE_URL`) so the pod environment carries:

   ```yaml
   - name: UI_KEYCLOAK_CLIENT_ID
     value: "meho-web"
   - name: UI_KEYCLOAK_CLIENT_SECRET
     valueFrom:
       secretKeyRef:
         name: meho-ui-keycloak-web
         key: client_secret
   ```

   The exact secret-management chain depends on the deploy's
   secret-rendering stack (External Secrets Operator, Vault
   Agent Injector, Helm hook with `vault kv get`, etc.); the
   contract is that `UI_KEYCLOAK_CLIENT_SECRET` is present in
   the pod environment at startup. The backplane reads it once
   via [`get_settings`](../codebase/backend.md) and never logs
   or re-emits it.

4. Verify by querying Vault for the key (the value MUST NOT be
   pasted into logs or chat):

   ```bash
   vault kv get -field=client_secret secret/meho/prod/ui/keycloak/web-client \
     | wc -c
   # Expected: 30+ -- Keycloak issues 36-character UUID-style
   # secrets by default. The check confirms the value lands
   # without leaking it.
   ```

### Step 4 — Rotate by regenerating the secret on Keycloak

The secret can be rotated at any time:

1. **Credentials** tab → **Regenerate**. Copy the new value
   immediately — the old value stops working on regenerate.
2. `vault kv put secret/meho/<env>/ui/keycloak/web-client
   client_secret='<new-value>'`.
3. Restart the backplane pods (or trigger the deploy's
   secret-refresh path) so the new value is read at startup.

During the gap between "secret regenerated on Keycloak" and "pod
restarted with the new secret", any in-flight BFF login attempts
fail with `invalid_client` at the token endpoint — the operator
sees a generic 400 and can retry after the deploy completes.
Plan rotations during a low-traffic window or use the deploy's
rolling-restart strategy to keep the failure surface small.

## Verification

Three checks. Run them after applying the recipe and before
considering the realm "BFF-ready". Checks 1 and 2 prove the realm
half (client + secret exist and PKCE is pinned); Check 3 proves
the end-to-end contract.

### Check 1 — `meho-web` client is configured correctly

```bash
# Replace REALM / ADMIN_TOKEN with values from your environment.
ISSUER="https://keycloak.example.org/realms/<realm-name>"
ADMIN_TOKEN="<a token from kcadm.sh config credentials>"

curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" \
  "$ISSUER/clients?clientId=meho-web" \
  | jq '.[0] | {
      clientId,
      publicClient,
      standardFlowEnabled,
      directAccessGrantsEnabled,
      redirectUris,
      pkceMethod: .attributes."pkce.code.challenge.method"
    }'
```

Expected:

```json
{
  "clientId": "meho-web",
  "publicClient": false,
  "standardFlowEnabled": true,
  "directAccessGrantsEnabled": false,
  "redirectUris": ["https://meho.evba.lab/ui/auth/callback"],
  "pkceMethod": "S256"
}
```

Mismatch on `publicClient: true` → client authentication is off;
go back to Step 1. Mismatch on `pkceMethod` → re-apply Step 2.
Mismatch on `redirectUris` → add the exact callback URL on the
client's **Settings** tab.

### Check 2 — discovery doc exposes `end_session_endpoint`

The logout flow relies on Keycloak's OIDC Session Management
end-session endpoint. The discovery doc lists it:

```bash
curl -sS "$ISSUER/.well-known/openid-configuration" \
  | jq '{
      authorization_endpoint,
      token_endpoint,
      end_session_endpoint
    }'
```

All three URLs must be present. The BFF logout flow degrades
gracefully when `end_session_endpoint` is absent (clears the local
cookie + redirects to `/ui/auth/login`), but the IdP-side session
stays alive until its own TTL — undesirable.

### Check 3 — End-to-end login round-trip against the deployed BFF

After the backplane is deployed with `UI_KEYCLOAK_CLIENT_ID=meho-web`
+ `UI_KEYCLOAK_CLIENT_SECRET=<value-from-Vault>` +
`UI_SESSION_ENCRYPTION_KEY=<value-from-Vault>`:

```bash
# Visit the dashboard in a browser:
open https://meho.evba.lab/ui/

# Expected:
# 1. 302 → /ui/auth/login?return_to=/ui/
# 2. 302 → https://keycloak.example.org/realms/<realm>/protocol/openid-connect/auth
#    Query string carries code_challenge + code_challenge_method=S256 +
#    resource=https://meho.evba.lab/api + state=<random>.
# 3. Operator authenticates at Keycloak.
# 4. 302 → /ui/auth/callback?code=...&state=...
# 5. Backplane sets the meho_session cookie (HttpOnly + Secure +
#    SameSite=Strict + Path=/) and 302s to /ui/.
# 6. Dashboard renders with the operator's identity in the header.
```

Failure modes:

- **400 `authorization_failed` on callback** — usually a
  `state` mismatch (the verifier store dropped the entry, which
  can happen on a stale browser tab or after a backplane
  restart) or a `client_secret` typo. Re-check Step 3 (the
  rendered secret matches what Keycloak shows under
  **Credentials**) and try the flow with a fresh browser tab.
- **503 `ui_oauth_not_configured`** — `UI_KEYCLOAK_CLIENT_ID` or
  `UI_KEYCLOAK_CLIENT_SECRET` is unset in the pod environment.
  Re-check Step 3 (the Helm values rendered the env var) and
  the deploy's secret-refresh path.
- **502 `upstream_auth_provider_unreachable`** — the backplane
  cannot reach Keycloak's discovery / token endpoint. DNS, TLS,
  or network policy issue; outside the BFF's control.
- **401 from the chassis JWT chain on callback** — the access
  token Keycloak issued does not carry the `tenant_id` /
  `tenant_role` claims, or the `aud` does not match
  `KEYCLOAK_AUDIENCE`. The BFF inherits the same chassis JWT
  checks the API surface enforces; re-run the checks in
  [`keycloak-tenant-claims.md`](./keycloak-tenant-claims.md) §
  Verification.

## Why a separate `meho-web` client (and not reuse `meho-cli` or `meho-mcp`)

- **`meho-cli` is public.** Public clients have no secret. The
  authorization-code grant the BFF runs requires a secret on the
  token-endpoint POST (confidential client). Reusing the CLI
  client would force the realm to either weaken the CLI client to
  accept public-grant flows it doesn't run, or shoehorn the BFF
  into the device-code flow it isn't suited for.
- **`meho-mcp` is the resource server.** The audience the
  backplane validates against (`KEYCLOAK_AUDIENCE`). Tokens are
  issued **for** that audience, not by that client. A confidential
  client that issues a token with `aud=meho-mcp` is the right
  shape; that confidential client is `meho-web`.
- **Redirect URI exact match.** Each client has its own
  redirect URIs list. The BFF callback URL
  (`/ui/auth/callback`) is BFF-specific; mixing it with CLI
  device-code URIs on one client confuses Keycloak's
  redirect-validation logic.

The cost of a separate client is one extra Admin Console object
+ one extra Vault secret. The benefit is each operator surface
(CLI / MCP / Web) has its own client identity that the realm can
audit / disable / rate-limit independently.

## Status

| Item | Side | State |
| --- | --- | --- |
| Recipe (this doc) | producer | landed in this PR ([`./keycloak-web-client.md`](./keycloak-web-client.md)) |
| BFF reads `UI_KEYCLOAK_CLIENT_ID` / `UI_KEYCLOAK_CLIENT_SECRET` | producer | tracked at [#865](https://github.com/evoila/meho/issues/865) |
| `meho-web` client provisioned on `evba.lab` realm | consumer | pending — applied by the dogfooding lab operator before deploying v0.2 operator-console |
| Vault path `secret/meho/evba-lab/ui/keycloak/web-client` populated | consumer | pending — same operator step |
| End-to-end browser login against the v0.2 BFF returns 200 on `/ui/` | consumer | pending — the closing-comment artefact on Initiative #337 |

## References

- Parent Initiative: [#337 — G10.0 Frontend chassis](https://github.com/evoila/meho/issues/337) — HTMX + Jinja2 + Tailwind + BFF auth + FastAPI `/ui` mount
- Parent Goal: [#336 — G10 Operator web UI](https://github.com/evoila/meho/issues/336)
- Sibling handshake: [`./keycloak-tenant-claims.md`](./keycloak-tenant-claims.md) — `tenant_id` + `tenant_role` protocol mappers (the BFF inherits these via the chassis JWT chain)
- Sibling handshake: [`./mcp-client-setup.md`](./mcp-client-setup.md) — `meho-cli` / `meho-mcp` client recipes (this `meho-web` doc mirrors the same shape)
- Backend codebase walkthrough: [`../codebase/ui.md`](../codebase/ui.md) — BFF auth flow + middleware + session storage
- Settings: [`Settings.ui_keycloak_client_id`](../codebase/backend.md), [`Settings.ui_keycloak_client_secret`](../codebase/backend.md)
- OAuth 2.0 Authorization Code grant — [RFC 6749 §4.1](https://www.rfc-editor.org/rfc/rfc6749)
- PKCE — [RFC 7636](https://www.rfc-editor.org/rfc/rfc7636)
- Resource indicators — [RFC 8707](https://www.rfc-editor.org/rfc/rfc8707)
- OAuth Security BCP — [RFC 9700](https://datatracker.ietf.org/doc/rfc9700/)
- OAuth 2.0 for Browser-Based Apps BCP — [draft-ietf-oauth-browser-based-apps](https://datatracker.ietf.org/doc/draft-ietf-oauth-browser-based-apps/)
- Keycloak Server Admin Guide — [Client configuration](https://www.keycloak.org/docs/latest/server_admin/index.html#_client-configuration)
- Keycloak OIDC layers — [OIDC endpoints](https://www.keycloak.org/securing-apps/oidc-layers)
- Consumer's Keycloak realm: see `evoila-bosnia/claude-rdc-hetzner-dc/rdc-hetzner-dc/INVENTORY.md` Keycloak section
