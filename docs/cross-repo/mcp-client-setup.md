<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# MCP client setup — operator-side requirements

> Operator-facing recipe for wiring an MCP client (Claude.ai Custom Connector, MCP Inspector, Cline, Continue) to a running MEHO backplane. The MCP server itself is part of MEHO's backplane (architecture in [`docs/architecture/mcp.md`](../architecture/mcp.md)); this doc is the realm-side + client-side configuration the operator runs to connect to it.

## Why this doc exists

MEHO speaks MCP 2025-06-18 over Streamable HTTP at the `/mcp` route. The wire protocol is fixed by the spec; what isn't fixed is the Keycloak realm configuration that issues tokens carrying the right `aud` claim, and the per-client configuration step that points a given MCP client at your backplane. This doc walks both — the realm-side change is one-time, the client-side change is per-installation.

If you're operating MEHO via the dogfood consumer ([`evoila-bosnia/claude-rdc-hetzner-dc`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc)), the realm-side change ships in [Goal #11's cross-repo deps](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/issues/261); follow the client-side steps below.

## Step 1 — Register the MCP resource URI as an audience in Keycloak

MEHO validates every `/mcp` request's Bearer token with `aud == MCP_RESOURCE_URI`, distinct from the chassis HTTP-API audience (`KEYCLOAK_AUDIENCE`). Keycloak must know to issue tokens with this audience.

`MCP_RESOURCE_URI` defaults to `${BACKPLANE_URL}/mcp` if unset. For the standard deployment, `BACKPLANE_URL=https://meho.example.com` produces `MCP_RESOURCE_URI=https://meho.example.com/mcp`.

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

MEHO v0.2 doesn't implement RFC 7591 Dynamic Client Registration; operators register the MCP client statically. The recommended shape is one client per MCP-client-implementation per operator, but a single shared client also works.

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

### Token rejected at the MCP server (401, `invalid_token`)

The token's `aud` doesn't match `MCP_RESOURCE_URI`:

- Confirm Step 1 wired the audience mapper on the *correct* client (the same client that's issuing the operator's token).
- Confirm the OAuth `resource` parameter the client sends matches `MCP_RESOURCE_URI`. Claude.ai's Custom Connector flow derives `resource` automatically from the URL the operator pasted — but a forwarder / reverse proxy that rewrites the path can leave the audience claim pointing at the wrong URI. Capture the issued token via the realm's token-introspection endpoint and read `aud` to confirm.

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
- MCP 2025-06-18 spec: <https://modelcontextprotocol.io/specification/2025-06-18>.
- MCP authorization: <https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization>.
- RFC 9728 (Protected Resource Metadata): <https://datatracker.ietf.org/doc/html/rfc9728>.
- RFC 8707 (Resource Indicators): <https://www.rfc-editor.org/rfc/rfc8707.html>.
- Claude.ai Custom Connectors: <https://modelcontextprotocol.io/docs/develop/connect-remote-servers>.
- MCP Inspector: <https://github.com/modelcontextprotocol/inspector>.
- Keycloak audience mappers: <https://www.keycloak.org/docs/latest/server_admin/#_audience>.
