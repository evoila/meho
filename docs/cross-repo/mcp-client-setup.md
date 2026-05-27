<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# MCP client setup — operator-side requirements

> Operator-facing recipe for wiring an MCP client (Claude.ai Custom Connector, MCP Inspector, Cline, Continue) to a running MEHO backplane. The MCP server itself is part of MEHO's backplane (architecture in [`docs/architecture/mcp.md`](../architecture/mcp.md)); this doc is the realm-side + client-side configuration the operator runs to connect to it.

## Why this doc exists

MEHO speaks MCP 2025-06-18 over Streamable HTTP at the `/mcp` route. The wire protocol is fixed by the spec; what isn't fixed is the Keycloak realm configuration that issues tokens carrying the right `aud` claim, and the per-client configuration step that points a given MCP client at your backplane. This doc walks both — the realm-side change is one-time, the client-side change is per-installation.

> **Pre-flight (load-bearing).** Before any MCP client can authenticate against MEHO, the deployer MUST pre-create a **public** OAuth client in the Keycloak realm — MEHO doesn't implement RFC 7591 Dynamic Client Registration, and Keycloak's default Trusted Hosts policy returns 403 for anonymous DCR on any prod realm. The full deployer recipe (5-step realm walk + 4-wall symptom→cause→fix matrix covering both the CLI and the MCP onramp) lives in [`deploy/values-examples/README.md` § Auth onramp recipe (CLI + MCP)](../../deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp). The steps below are the **MCP-client-side wire-up** that runs *after* the public client + mappers + scopes exist in the realm; Step 2 here is the minimum-shape recap, with the full recipe as the authoritative source. If `tools/list` returns an empty list or every call 401s with `invalid_token`, the recipe's 4-wall matrix is the right place to start.

If you're operating MEHO via the dogfood consumer ([`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)), the realm-side change ships in [Goal #11's cross-repo deps](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/issues/261); follow the client-side steps below.

## Step 1 — Register the MCP resource URI as an audience in Keycloak

MEHO validates every `/mcp` request's Bearer token with `aud == MCP_RESOURCE_URI`, distinct from the chassis HTTP-API audience (`KEYCLOAK_AUDIENCE`). Keycloak must know to issue tokens with this audience.

`MCP_RESOURCE_URI` defaults to `${BACKPLANE_URL}/mcp` if unset. For the standard deployment, `BACKPLANE_URL=https://meho.example.com` produces `MCP_RESOURCE_URI=https://meho.example.com/mcp`.

> **Chart-derived default (G0.8-T4 #633).** When you deploy via the Helm chart with an Ingress configured (the default), you do **not** set `BACKPLANE_URL` or `MCP_RESOURCE_URI` yourself — the chart derives `BACKPLANE_URL=https://<ingress.host>` (scheme follows `ingress.tls.enabled`) and `MCP_RESOURCE_URI=${BACKPLANE_URL}/mcp` into the backplane ConfigMap. Override `config.backplaneUrl` only if the public URL differs from the Ingress host, or `config.mcpResourceUri` only for a non-default MCP mount. If the chart can resolve neither (no Ingress and nothing set), the backplane **fails loudly at startup** (CrashLoopBackOff) with a log line naming `MCP_RESOURCE_URI` / `BACKPLANE_URL` and this step — it does not serve a dark, silent `/mcp`. The same remediation appears in the `/mcp` 401 response `detail` if a token reaches an unconfigured surface. You still must complete the Keycloak audience-mapper step below regardless of how the URI is set.

```bash
# Add an audience-protocol-mapper to the MCP client (see Step 2 for the
# client itself). The "included client audience" is the value the server
# will compare against the token's `aud` claim.
kcadm.sh create clients/<MCP_CLIENT_INTERNAL_ID>/protocol-mappers/models \
  -r <realm> \
  -b '{
    "name": "meho-mcp-audience",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-audience-mapper",
    "config": {
      "included.custom.audience": "https://meho.example.com/mcp",
      "access.token.claim": "true",
      "id.token.claim": "false"
    }
  }'
```

A different operator (different MEHO hostname) substitutes the URI accordingly. The trailing-slash discipline matters: MEHO normalises `MCP_RESOURCE_URI` server-side (strips trailing `/` per MCP 2025-06-18 §"Canonical Server URI"), so the audience claim in the token must match the no-trailing-slash form. Keycloak's UI for "Included Client Audience" stores the value verbatim — paste without the slash.

## Step 2 — Register an MCP client in Keycloak

MEHO doesn't implement RFC 7591 Dynamic Client Registration; operators register the MCP client statically. The recommended shape is one client per MCP-client-implementation per operator, but a single shared client also works.

> **Use the consolidated recipe.** The full client shape (5 protocol mappers, 4 default client scopes, the `basic`/`sub` gotcha) is documented in [`deploy/values-examples/README.md` § Auth onramp recipe (CLI + MCP)](../../deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp) — that recipe is the source of truth and stays in sync with the CLI's needs. The `kcadm.sh` snippet below is the minimum shape and **does not by itself produce a working MCP onramp** — without the 5 mappers and the 4 default scopes, tokens minted by this client are rejected with `invalid_token` (Wall #2 / Wall #3 in the matrix). Read the consolidated recipe and then return here for the per-MCP-client setup at Step 3.

```bash
# Public client (PKCE-only, no client secret) — the MCP spec mandates
# PKCE for the OAuth 2.1 authorization-code flow. PKCE on a public
# client is what the spec calls out as the v0.2 default.
kcadm.sh create clients -r <realm> -b '{
  "clientId": "meho-mcp-client",
  "name": "MEHO MCP client (static)",
  "protocol": "openid-connect",
  "publicClient": true,
  "standardFlowEnabled": true,
  "directAccessGrantsEnabled": false,
  "redirectUris": [
    "https://claude.ai/api/mcp/auth_callback",
    "http://localhost:*"
  ],
  "webOrigins": ["+"],
  "attributes": {
    "pkce.code.challenge.method": "S256"
  }
}'

# Attach the audience mapper from Step 1 to this client.
```

`redirectUris` covers two patterns:

- `https://claude.ai/api/mcp/auth_callback` for the Claude.ai Custom Connector flow.
- `http://localhost:*` for MCP Inspector and other CLI / desktop clients that listen on an ephemeral local port.

A future-proof alternative is to make this client a *Dynamic Client Registration* template once Keycloak's DCR support and the v0.2.next MEHO RFC 7591 work land; for v0.2, the static recipe above is the path.

## Step 3 — Configure the client to connect

MEHO speaks Streamable HTTP at `/mcp`, not stdio. This matters because Claude Desktop's local `claude_desktop_config.json` shape (the `mcpServers` + `command: "npx"` config) is for *stdio* MCP servers spawned as subprocesses. Remote HTTPS MCP servers like MEHO use a different setup path per client:

### Claude.ai Custom Connector (recommended)

[Claude.ai → Settings → Connectors → Add custom connector](https://modelcontextprotocol.io/docs/develop/connect-remote-servers). Paste the server URL when prompted:

```text
https://meho.example.com/mcp
```

Claude.ai resolves the URL, fetches the RFC 9728 protected-resource metadata document at `https://meho.example.com/.well-known/oauth-protected-resource`, discovers the Keycloak authorization server URL, and launches the OAuth 2.1 + PKCE flow against it. Complete the device-code prompt; the connector lands in the active state and MEHO's tools appear in the conversation toolbar.

### MCP Inspector CLI (debug / smoke)

`@modelcontextprotocol/inspector` ships a non-interactive CLI mode useful for scripting and post-deploy smoke tests. Authentication is via the `Authorization` header — the operator provides the token out of band.

```bash
# Obtain a token via the realm's preferred flow — for the dogfood
# consumer, `meho login` is the simplest path; for direct Keycloak
# device-code, `kcadm.sh` works too.
TOKEN=$(meho login --print-token)

# List tools (smoke test).
npx @modelcontextprotocol/inspector --cli \
  https://meho.example.com/mcp \
  --transport http \
  --method tools/list \
  --header "Authorization: Bearer $TOKEN"

# Invoke meho.status — exercises the full chain.
npx @modelcontextprotocol/inspector --cli \
  https://meho.example.com/mcp \
  --transport http \
  --method tools/call \
  --tool-name meho.status \
  --header "Authorization: Bearer $TOKEN"
```

### Cline / Continue (VS Code)

Cline and Continue both consume an `mcp.json` in the workspace. The shape mirrors Claude Desktop's `mcpServers` but with an `url` rather than a `command`:

```json
{
  "mcpServers": {
    "meho": {
      "url": "https://meho.example.com/mcp",
      "transport": "http"
    }
  }
}
```

OAuth handling is client-specific; refer to each client's docs for the token-acquisition path. Both expect the same Bearer-token flow MEHO advertises via the protected-resource metadata document.

### Claude Code (HTTP MCP) and Cursor — `.mcp.json` `client_id` limitation

Claude Code's native HTTP-MCP support and Cursor's MCP wire-up, as of 2026-05, follow the RFC 9728 metadata trail correctly: they fetch `/.well-known/oauth-protected-resource`, read the `authorization_servers` field, then attempt OAuth 2.1 + PKCE against the Keycloak realm. The problem is the next step: **neither `.mcp.json` shape exposes a `client_id` field**, so both clients fall back to dynamic client registration (RFC 7591). Keycloak's default Trusted Hosts policy ships with an empty whitelist — anonymous DCR is de-facto disabled — so the registration POST returns `HTTP 403 {"error":"insufficient_scope","error_description":"Policy 'Trusted Hosts' rejected request to client-registration service. Details: Host not trusted."}` and the wire-up never completes.

Pre-registering `meho-mcp-client` on the realm side (Step 2 above + the [auth onramp recipe](../../deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp)) is necessary but **not sufficient** for these clients: they have no place to put the resulting `client_id`.

Three workarounds today:

1. **Use a wire-format-compatible MCP client.** Claude.ai Custom Connector, MCP Inspector, Cline, and Continue all expose a place for the operator to provide the OAuth client_id (Custom Connector picks it up from the realm's discovery once the operator approves the consent screen; the rest take it from `mcp.json` / per-client config). For everyday operator workflows, this is the first-class path.

2. **Shim Claude Code / Cursor through `mcp-remote` (or an equivalent stdio→HTTP proxy).** The proxy holds a Bearer token (acquired out-of-band via `meho login --print-token` or any other path), translates the stdio MCP transport these clients spawn against an `npx mcp-remote ...` command-line into Streamable-HTTP calls to MEHO, and injects the `Authorization` header. The trade-off is operational: the proxy needs the token rotated when it expires; the Claude Code / Cursor process never sees the OAuth flow at all. This is the path for realms on Keycloak < 26.6.0 and for clients that don't yet support CIMD.

3. **Enable Client ID Metadata Documents (CIMD) on the Keycloak realm** so the CIMD-capable client (Claude Code on MCP protocol `2025-11-25+`) reaches an authenticated MCP state with **no pre-registered client and no DCR**. The `client_id` becomes the HTTPS URL of the client's own metadata document; Keycloak fetches it on the fly. This requires Keycloak ≥ 26.6.0 (CIMD landed **experimental** there), the `cimd` server feature flag, and a realm-level client-policy profile + policy pair documented in [`deploy/values-examples/README.md` § CIMD onramp](../../deploy/values-examples/README.md#cimd-onramp--no-pre-registered-client-keycloak--2660-experimental). CIMD is the cleanest long-term answer — but accept the experimental label until [keycloak#45284](https://github.com/keycloak/keycloak/issues/45284) closes and the feature graduates to GA.

Opening RFC 7591 DCR on the realm side is **not** the right fix: a public DCR endpoint on a prod realm is a long-term posture decision (which clients are allowed to self-register; how the operator audits unknown clients) and shouldn't be flipped on to work around a per-client config gap. The right long-term fix is one of the three workarounds above — CIMD when the realm is on a CIMD-capable Keycloak and the client supports it, the shim or a wire-format-compatible client otherwise.

## Step 4 — Verify connectivity

After the client is wired:

- Run `meho.status` from the connected client. The response should carry the operator's identity (sub, tenant_id, role) plus the Vault federation status and DB migration state.
- Read the operator's tenant info: ask the client to read the resource at `meho://tenant/<your-tenant-id>/info`. The response should be the operator's tenant identity bundle (id, slug, name, role).
- On the backplane, `SELECT method, path, operator_sub, status_code, occurred_at FROM audit_log ORDER BY occurred_at DESC LIMIT 10` should show the two operations with `method='MCP'` and `path='/mcp/tools/call/meho.status'` / `path='/mcp/resources/read/meho://tenant/<id>/info'`.

If any of these don't appear, walk the troubleshooting section.

## Step 5 — Troubleshooting

### 401 + `WWW-Authenticate` but the client doesn't reach OAuth

The response carries `WWW-Authenticate: Bearer resource_metadata="https://meho.example.com/.well-known/oauth-protected-resource"`. The client is supposed to fetch that URL to discover the authorization server. If it doesn't reach OAuth:

- Check the `resource_metadata` URL is publicly reachable from where the client runs. Claude.ai's connector backend runs in Anthropic's cloud; a backplane behind a private network or self-signed TLS won't be reachable. Expose the backplane via a public ingress or a tunnel (Cloudflare, ngrok) for the Custom Connector flow.
- Confirm `BACKPLANE_URL` in the backplane's ConfigMap resolves to the public hostname. If `BACKPLANE_URL` is wrong, `resource_metadata` points at the wrong host.

### Token rejected at the MCP server (401)

From v0.3.2 the 401 body carries a *specific* `detail` code naming the failed check — read the body first, then walk to the matching remediation. Earlier v0.3.x deploys collapsed every decode-stage failure into one opaque `invalid_token`; that opacity is what consumer Addendum II walls #2 + #3 burned ~40 minutes on (#797).

| `detail` code | What it means | Where to look |
|---|---|---|
| `invalid_audience` | The token's `aud` claim doesn't match `MCP_RESOURCE_URI`. | Confirm Step 1 wired the audience mapper on the *correct* client (the one that issues the operator's token). Confirm the OAuth `resource` parameter the client sends matches `MCP_RESOURCE_URI` — Claude.ai's Custom Connector flow derives `resource` automatically from the URL the operator pasted, but a forwarder / reverse proxy that rewrites the path can leave the audience claim pointing at the wrong URI. Capture the issued token via the realm's token-introspection endpoint and read `aud` to confirm. |
| `invalid_issuer` | The token's `iss` doesn't match the configured Keycloak realm. | Confirm the client is authenticating against the same Keycloak realm `KEYCLOAK_ISSUER_URL` names; a stale OAuth state from a previous realm survives in some clients across config changes. |
| `missing_sub` | The token lacks the `sub` claim — the silent killer (Wall #3 in the [consolidated matrix](../../deploy/values-examples/README.md#four-wall-symptom--cause--fix-matrix)). Keycloak 25+ moved `sub` into the `basic` client scope, and clients created via the admin REST API don't auto-inherit realm default-default scopes. | Add the `basic` default scope to the client (see [`deploy/values-examples/README.md` § Auth onramp recipe (CLI + MCP)](../../deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp) Step 2 / Wall #3). |
| `token_expired` | `exp` is in the past beyond the configured leeway. | Refresh the access token; check the client's refresh-token plumbing. |
| `token_not_yet_valid` | `nbf` is in the future beyond leeway — clock skew. | Sync the system clock on the issuing side (Keycloak host) or on the calling host. |
| `signature_verification_failed` | The JWS signature doesn't verify against the issuer's published key. | Confirm the client is presenting the token from the *same* realm the backplane validates against. A token from a different realm whose `kid` happens to collide with one in the cached JWKS will surface here. |
| `missing_tenant_claim` / `malformed_tenant_claim` / `missing_tenant_role_claim` / `unknown_tenant_role` | Post-decode tenant-claim extraction failed — the realm omits or mis-shapes the MEHO-required tenant mappers. | See [`keycloak-tenant-claims.md`](./keycloak-tenant-claims.md) for the realm-side recipe. |
| `invalid_token` | A structural failure with no more specific code — truncated JWS, `alg: none` rejection, or a `kid` not in the JWKS even after a forced refresh. | Capture the raw `Authorization` header value and inspect the JWS structure (three dot-separated base64url segments, RS256 alg). |

The diagnostic value behind each code (expected audience, expected issuer, claim name, exception class) is in the backplane's **server log**, not the response body — that's the deliberate body-vs-log info-leak boundary an unauthenticated 401 honours. Operators with log access can `grep` for the `detail` code on the backplane logger to see the full picture.

For the full cross-wall diagnostic chain (mapper misconfig vs. missing scope vs. wrong realm vs. proxy strip), walk [`deploy/values-examples/README.md` § Four-wall symptom → cause → fix matrix](../../deploy/values-examples/README.md#four-wall-symptom--cause--fix-matrix).

### MCP client → Keycloak DCR returns 403 `"Host not trusted"`

The client attempted dynamic client registration (RFC 7591) and Keycloak rejected with `HTTP 403 {"error":"insufficient_scope","error_description":"Policy 'Trusted Hosts' rejected request to client-registration service. Details: Host not trusted."}`:

- This is the **correct** response from Keycloak — its default Trusted Hosts policy ships with an empty whitelist, so anonymous DCR is de-facto disabled. MEHO doesn't implement RFC 7591 either; the metadata's "follow the trail" UX assumes the client already holds a `client_id`, which on a fresh realm it doesn't.
- The fix is on the deployer side, not the realm-policy side: pre-register a public client per [`deploy/values-examples/README.md` § Auth onramp recipe (CLI + MCP)](../../deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp) Step 2. Opening DCR on a prod realm is a long-term posture decision, not the right onramp workaround.
- If the MCP client can't carry `client_id` in its config (Claude Code's HTTP MCP, Cursor — see § Claude Code (HTTP MCP) and Cursor above), the deployer-side fix doesn't help until the upstream client exposes the field; the workaround is to shim through an stdio→HTTP proxy.

### `tools/list` returns an empty list

The operator's token carries a `tenant_role` claim below the role rank any registered tool requires (`read_only < operator < tenant_admin`). v0.2 ships only `meho.status` (`read_only` minimum), so an empty list means the JWT lacks the `tenant_role` claim entirely or carries a role below `read_only`. Walk back through the realm's `tenant_role` mapper.

### Audit row missing for a successful call

The MCP audit writer fails closed: an unauditable call returns JSON-RPC `INTERNAL_ERROR` (-32603). A *successful* call with no audit row means the row was rolled back — most commonly a DB connectivity issue mid-request. The chassis structlog stream will carry a `mcp_audit_write_failed` event with the exception class; check there before suspecting the writer.

## Step 6 — Alternative clients

| Client | Surface | Setup |
|---|---|---|
| [Claude.ai Custom Connector](https://modelcontextprotocol.io/docs/develop/connect-remote-servers) | Web (claude.ai) + Desktop (synced via account) | UI: paste `/mcp` URL, complete OAuth |
| [MCP Inspector](https://github.com/modelcontextprotocol/inspector) | CLI + browser-based debug UI | `npx @modelcontextprotocol/inspector --cli <url> --transport http --header "Authorization: Bearer $TOKEN"` |
| [Cline](https://github.com/cline/cline) | VS Code extension | `mcp.json` with `{ "url": "...", "transport": "http" }`; OAuth handled per-extension docs |
| [Continue](https://github.com/continuedev/continue) | VS Code + JetBrains extensions | Similar `mcp.json` shape; see the extension's docs for the auth flow |

MEHO is spec-conformant; any MCP-2025-06-18 Streamable-HTTP client with OAuth 2.1 + PKCE support should work. If a specific client breaks against MEHO, file an issue at [`evoila/meho`](https://github.com/evoila/meho/issues) with the client name + version, the MCP exchange (request + response), and the realm's token introspection output.

## References

- MEHO MCP architecture: [`docs/architecture/mcp.md`](../architecture/mcp.md).
- Consolidated deployer auth-onramp recipe (CLI + MCP) + 4-wall matrix: [`deploy/values-examples/README.md` § Auth onramp recipe (CLI + MCP)](../../deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp).
- MCP 2025-06-18 spec: <https://modelcontextprotocol.io/specification/2025-06-18>.
- MCP authorization: <https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization>.
- RFC 9728 (Protected Resource Metadata): <https://datatracker.ietf.org/doc/html/rfc9728>.
- RFC 8707 (Resource Indicators): <https://www.rfc-editor.org/rfc/rfc8707.html>.
- RFC 7591 (Dynamic Client Registration): <https://datatracker.ietf.org/doc/html/rfc7591>.
- RFC 8628 (Device Authorization Grant): <https://www.rfc-editor.org/rfc/rfc8628>.
- Claude.ai Custom Connectors: <https://modelcontextprotocol.io/docs/develop/connect-remote-servers>.
- MCP Inspector: <https://github.com/modelcontextprotocol/inspector>.
- Keycloak audience mappers: <https://www.keycloak.org/docs/latest/server_admin/#_audience>.
- Keycloak client-registration policies (Trusted Hosts): <https://www.keycloak.org/securing-apps/client-registration>.
