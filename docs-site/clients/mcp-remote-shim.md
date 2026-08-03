# Other MCP clients (the `mcp-remote` static-token shim)

Some MCP clients — Cursor as of this writing, and others — follow the
RFC 9728 metadata trail correctly but **expose no `client_id` field**
in their config. They then fall back to Dynamic Client Registration,
which Keycloak's Trusted Hosts policy blocks, and the wire-up never
completes. Pre-registering `meho-mcp` does not help: the client has
nowhere to put the resulting id.

The general-purpose workaround is a
[`mcp-remote`](https://github.com/geelen/mcp-remote) stdio shim that
carries a **pre-minted bearer token** instead of running OAuth. The
client spawns the shim as a local stdio server; the shim injects the
`Authorization` header and forwards Streamable-HTTP calls to `/mcp`.

!!! warning "This is a stopgap, not the first-class path"

    A pre-minted access token is **short-lived**. In this mode the shim
    is *not* running the OAuth refresh flow, so when the token expires
    every call fails until you re-mint it and restart the client. Prefer
    a client that can carry a `client_id` — [Claude Code](claude-code.md),
    or the OAuth-mode [Claude Desktop shim](claude-desktop.md), both of
    which refresh automatically. Use the static-token shim only when the
    client supports neither.

Runs here, as everywhere in this section, from a machine **on the
internal network / VPN**.

## Mint a token

There is no `--print-token` verb. Force the CLI's file token backend and
read the access token out of `credentials.json`:

```bash
MEHO_KEYRING_DISABLE=1 meho login https://meho.example.com

export MEHO_TOKEN="Bearer $(jq -r '.entries[].access_token' \
  "${XDG_CONFIG_HOME:-$HOME/.config}/meho/credentials.json")"
```

Keep the `Bearer ` prefix (and its trailing space) inside the variable:
`mcp-remote` splits `--header` on the first colon into name and value,
and its README documents stashing a space-containing value in the
environment as the robust form for a bearer header.

## Point the client's stdio command at the shim

Configure the client's MCP server as a local stdio command running
`mcp-remote` with `--header`. The exact config key differs per client
(`command` + `args`, an `mcp.json`, a settings pane); the command is the
same:

```bash
npx -y mcp-remote@0.1.38 https://meho.example.com/mcp \
  --header "Authorization:${MEHO_TOKEN}"
```

The backplane is HTTPS, so no `--allow-http` is needed. Set
`NODE_EXTRA_CA_CERTS=/path/to/internal-ca.pem` in the client's
environment for an internal-CA deploy — Node does not read the OS trust
store. Because the header carries the token, `mcp-remote` performs no
OAuth flow and needs no redirect URI on the realm.

## Verify

Once the client attaches the toolset, run **`meho_status`** and
**`list_targets`** — the same two-call check as every other client.
A `401` with an `invalid_token` / `token_expired` detail almost always
means the pre-minted token has expired: re-run the mint step and restart
the client. The full set of token-rejection causes is on the
[troubleshooting page](troubleshooting.md).

## The cleaner long-term answer

For clients on newer protocol versions, **Client ID Metadata Documents
(CIMD)** dissolve this wall entirely — the `client_id` becomes an HTTPS
URL the authorization server fetches, so neither DCR nor a pre-registered
client is needed. CIMD shipped **experimental** in Keycloak 26.6.0 and
is off by default; the realm-side recipe is in the
[values-examples deep-dive § CIMD onramp](https://github.com/evoila/meho/blob/main/deploy/values-examples/README.md#cimd-onramp--no-pre-registered-client-keycloak--2660-experimental).
It is out of scope for this page and pinned to a moving Keycloak
feature — treat it as a preview.
