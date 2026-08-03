# Claude Desktop

Claude Desktop reaches an internal MEHO backplane through a **local
[`mcp-remote`](https://github.com/geelen/mcp-remote) stdio→HTTP shim**
that runs on your own VPN-connected machine. The shim runs the OAuth
2.1 + PKCE flow against your internal Keycloak and forwards
Streamable-HTTP calls to `/mcp`. This is the **only** Desktop path for
an internal-only backplane.

!!! danger "The remote Custom Connector is not applicable"

    Claude Desktop's remote *Custom Connector* (Settings → Connectors →
    add a `/mcp` URL) is brokered through Anthropic's cloud and requires
    the backplane to be **publicly reachable**. MEHO is internal-only
    and must never be publicly exposed, so that path does not apply — it
    is not a TLS problem you can solve with a public certificate. Use
    the shim below. See the
    [Connect clients overview](index.md#remote-custom-connector-not-applicable)
    for the full rationale.

The configuration and findings on this page are the **empirically
observed** result of the smoke test run against a standing internal
deploy over VPN (issue
[#2666](https://github.com/evoila/meho/issues/2666)) — not a
design sketch.

## Prerequisites

- Claude Desktop on a machine **on the internal network / VPN**.
- [Node.js](https://nodejs.org/) (for `npx`), which spawns the shim.
- The `meho` CLI installed and `meho login` working from the same
  machine ([The meho CLI](cli.md)) — it proves TLS trust and the realm
  before you add the shim's moving parts.
- The deployment's CA in your OS trust store **and** as a PEM file you
  can point `NODE_EXTRA_CA_CERTS` at (Node does not read the OS store).
- The realm's public **`meho-mcp`** OAuth client, with the shim's
  loopback redirect URIs registered (next section).

## One-time realm redirect URIs

`mcp-remote`'s OAuth callback path is **`/oauth/callback`** — not the
`/callback` that Claude Code and the bootstrap script use. On a fixed
local port (below, `8456`), the `meho-mcp` client therefore needs
**both** loopback forms as valid redirect URIs:

```
http://localhost:8456/oauth/callback
http://127.0.0.1:8456/oauth/callback
```

`mcp-remote` tries `localhost` and its `127.0.0.1` twin, so register
both. The port must match the one in the config below. The rest of the
`meho-mcp` client shape (public PKCE client, five protocol mappers,
four default scopes, `offline_access` optional) is in
[Keycloak realm setup](../install/keycloak-realm.md).

## Configure `claude_desktop_config.json`

Add a **local stdio server** that runs the shim. This is the exact
shape proven in the smoke test (`mcp-remote@0.1.38`):

```json
{
  "mcpServers": {
    "meho": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote@0.1.38",
        "https://meho.example.com/mcp",
        "8456",
        "--static-oauth-client-info", "{\"client_id\": \"meho-mcp\"}",
        "--static-oauth-client-metadata", "{\"scope\": \"mcp:read mcp:execute\"}"
      ],
      "env": { "NODE_EXTRA_CA_CERTS": "/path/to/internal-ca.pem" }
    }
  }
}
```

- `8456` is the positional local callback port — it must match the
  registered redirect URIs above.
- `--static-oauth-client-info` pins the pre-registered public
  `meho-mcp` client so the shim skips Dynamic Client Registration
  (which Keycloak's Trusted Hosts policy blocks anyway).
- `NODE_EXTRA_CA_CERTS` is only needed on an internal-CA deploy; drop it
  if your backplane and Keycloak present publicly-trusted certificates.

Restart Claude Desktop. On first use the shim opens a browser, you
complete OAuth 2.1 + PKCE against the internal Keycloak (a static
client, so no DCR wall), and tokens land in `~/.mcp-auth`, which the
shim auto-refreshes.

## What actually works — and the sharp edges

The smoke test proved the transport end to end and surfaced three
version-sensitive findings. All three are resolved on a current
backplane; they are documented because they gate **which build you
must pin**.

### Pin a build with underscore tool names and structured results

- **Tool names must be underscores.** Claude's frontend validates every
  remote-MCP tool name against `^[a-zA-Z0-9_-]{1,64}$` and rejects the
  whole toolset if any name fails — the older dotted names (`meho.status`)
  failed *all-or-nothing*, so a conversation saw **zero** MEHO tools
  even though Settings → Connectors listed them. The rename to
  underscores (`meho_status`, `list_targets`, …) shipped in **v0.27.0**;
  pin that release or later.
- **`outputSchema`-declaring tools need conforming `structuredContent`.**
  MCP 2025-06-18 makes structured results mandatory for a tool that
  declares an `outputSchema`, and Claude's frontend enforces it. On a
  build without the fix, the ~17 declaring tools (including the
  narrow-waist `list_targets`, `query_topology`, `query_audit`) hard-fail
  client-side with "Tool execution failed" while the server logs a clean
  `200` and writes its audit row. `meho_status` — which declares no
  schema — is unaffected. Pin a build carrying the fix (merged after
  v0.27.0); on it, `list_targets` returns and renders in a conversation.

### `meho://` resources are not discoverable through the shim

`resources/list` returns **zero** concrete resources over the shim, so a
discovery-driven client (Claude Desktop asks at the handshake) sees
nothing to offer. The resource *templates* are published
(`resources/templates/list` carries the docs-chunk template) and the
read handler works — a raw `resources/read meho://tenant/<uuid>/info`
returns the tenant bundle — but **unlisted-but-readable is not usable by
a discovery-driven client**. Do not expect to browse `meho://` resources
from a Desktop conversation; drive everything through the tools.

## Verify

From a Claude Desktop conversation, once the toolset attaches by its
underscore names:

- Ask it to run **`meho_status`** — it returns your operator identity
  (sub / name / email), the Vault KV read result, DB migration state,
  and the negotiated MCP protocol version.
- Ask it to run **`list_targets`** — on a build with the
  `structuredContent` fix, it returns your registered targets, rendered
  in the conversation.

If the tools do not attach, or a call fails, walk the
[troubleshooting page](troubleshooting.md) — a rejected token, a name
that failed validation, and a schema-conformance failure each present
differently.
