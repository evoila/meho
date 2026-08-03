# Claude Code

Claude Code speaks MCP over **native HTTP** — no shim. You point a
project-scoped `.mcp.json` at the backplane's `/mcp` route and pin the
pre-registered `meho-mcp` public client; Claude Code runs the OAuth 2.1
authorization-code + PKCE flow itself, listening on a loopback port for
the callback. This is the pattern both MEHO dogfood repos run daily.

Like every client in this section, Claude Code must run on a machine
**on the internal network / VPN** — MEHO is internal-only.

## Configure `.mcp.json`

Add a `meho` server to the workspace `.mcp.json`. Claude Code reads it
at session start; restart the session after edits:

```json
{
  "mcpServers": {
    "meho": {
      "type": "http",
      "url": "https://meho.example.com/mcp",
      "oauth": {
        "clientId": "meho-mcp",
        "callbackPort": 8456,
        "scopes": "mcp:read mcp:execute"
      }
    }
  }
}
```

- **`clientId: meho-mcp`** pins the pre-registered public client, so
  Claude Code skips Dynamic Client Registration — which Keycloak's
  Trusted Hosts policy blocks on any real realm (Wall W1 on the
  [troubleshooting page](troubleshooting.md)).
- **`callbackPort`** fixes the loopback port the authorization-code
  callback returns to. It must match a registered redirect URI on the
  `meho-mcp` client (next section).

## One-time realm redirect URI

Claude Code's callback path is **`/callback`**. With the `callbackPort`
above, register this loopback redirect URI on the `meho-mcp` client:

```
http://localhost:8456/callback
```

(This is distinct from the `/oauth/callback` path the
[Claude Desktop shim](claude-desktop.md#one-time-realm-redirect-uris)
uses — a client used for both needs both.) Claude Code's flow also
requests `offline_access` to obtain a refresh token, so the `meho-mcp`
client must carry `offline_access` as an **optional** scope, or the
authorization request fails with `invalid_scope` (Wall W7). Both are
covered in [Keycloak realm setup](../install/keycloak-realm.md).

## Internal-CA trust

Claude Code runs on Node, which trusts only public CAs by default and
**does not read the OS trust store**. On an internal-CA deploy, point
Node at the CA bundle before starting Claude Code:

```bash
export NODE_EXTRA_CA_CERTS=/path/to/internal-ca.pem
```

Without it the OAuth discovery and token requests fail with a
TLS/`unable to verify` error even though `curl` and `meho login`
(which use the OS store) succeed from the same machine. Skip this on a
publicly-trusted deploy.

## Verify

After restarting the session, Claude Code lists the `meho` server's
tools. Confirm the connection with two calls:

- **`meho_status`** — returns operator identity, Vault, and DB state.
- **`list_targets`** — returns your registered targets.

If the server shows as failed, or the browser never opens for the OAuth
step, the [troubleshooting page](troubleshooting.md) maps the symptom —
a DCR `403 Host not trusted` means the `clientId` was dropped from
`.mcp.json`; an `invalid_scope` means the `offline_access` optional
scope is missing.
