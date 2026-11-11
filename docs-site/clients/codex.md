# Codex

New here? Start with the [laptop guide](../install/laptop-tools.md).

Codex can talk to MEHO's `/mcp` address directly. You do not need a local
helper program. The Codex desktop app, the command line and the IDE
extension read the same config file, so you set it up once.

Codex must run on a machine **on the internal network / VPN**. MEHO is
internal-only, and nothing is exposed to the internet.

!!! note "Status: not yet tested end to end"

    This page is written from the Codex documentation and from how the
    `meho-mcp` login client is set up. A live test with a Codex login is
    still open. Steps marked **Check** below are things to confirm on your
    first connection. If one of them fails, please report it.

## Before you start

- Trust your deployment's CA (certificate authority) first. If your
  deployment uses an internal CA, follow
  [Before you start: trust your deployment's CA](claude-code.md#before-you-start-trust-your-deployments-ca).
  Codex reads the root CA file from its own setting, see
  [Customer certificates](#customer-certificates).
- The realm needs the public `meho-mcp` login client. See
  [Keycloak realm setup](../install/keycloak-realm.md).
- Install the `meho` CLI and log in once, as described in
  [the CLI page](cli.md). It is the fastest way to prove that the realm,
  the certificate and the login work.

## Configure the server

Codex reads MCP servers from `~/.codex/config.toml`, in a
`[mcp_servers.<name>]` table. Add a `meho` server. Use your own host in
place of `meho.example.com`:

```toml
[mcp_servers.meho]
url = "https://meho.example.com/mcp"

[mcp_servers.meho.oauth]
client_id = "meho-mcp"
callback_port = 8457
```

- **`client_id = "meho-mcp"`** names the pre-registered public client.
  Codex then skips automatic client registration, which the login server
  (Keycloak) blocks. See wall W1 on the
  [troubleshooting page](troubleshooting.md).
- **`callback_port`** fixes the port that Codex listens on for the login
  answer. Without it, Codex picks a random port each time. Pick any free
  port; `8457` is only an example.

## One-time realm redirect URI

Codex waits for the login answer on `127.0.0.1`, at the path `/callback`
plus an id that it builds from the server address. The `meho-mcp` client
already allows any loopback port (`http://127.0.0.1:*` and
`http://localhost:*`). So you do not need to change the realm for Codex.

If your realm admin has removed those wildcard addresses, add the loopback
address of your fixed port to the `meho-mcp` client instead:

```
http://127.0.0.1:8457/callback*
```

## Log in

```bash
codex mcp login meho
```

Codex opens a browser. Sign in with your normal MEHO account. When the
browser says the login is done, go back to Codex.

**Check:** the login finishes without an error such as `invalid_redirect_uri`
or `client not found`. See
[troubleshooting](troubleshooting.md) if it does not.

## How long the login lasts

MEHO advertises two scopes to clients: `mcp:read` and `mcp:execute`. Codex
asks for the scopes that MEHO advertises. The `meho-mcp` client can also
give a long-lasting login, but only when the client asks for the
`offline_access` scope. The Claude clients ask for it. See
[How long an MCP login lasts](../install/keycloak-realm.md#how-long-an-mcp-login-lasts).

**Check:** whether Codex asks for `offline_access`. Log in, wait 30
minutes without using MEHO, then call a MEHO tool again.

- The call works: the login renews itself.
- Codex asks you to log in again: Codex did not get a long-lasting login.
  Run `codex mcp login meho` again, and tell your MEHO team, so that the
  gap can be fixed.

## Customer certificates

If your deployment uses an internal CA, point Codex at the root CA file.
Codex reads a PEM file from `CODEX_CA_CERTIFICATE`. It uses `SSL_CERT_FILE`
as a fallback:

```bash
export CODEX_CA_CERTIFICATE="$HOME/internal-ca.pem"
codex mcp login meho
```

Set the variable in the shell that starts Codex. Get the root CA file from
the person who deployed MEHO, and check its fingerprint before you trust
it, as described in the Claude Code page linked above.

## Check the connection

In Codex, ask it to call two tools:

1. `meho_status` shows that MEHO is up and which tenant you work in.
2. `list_targets` shows the targets that an operator has registered.

If both answer, the connection works. Then try `search_operations` and one
read with `call_operation`. Which tools an agent session can see is
explained in [MCP surface and scopes](mcp-surface-and-scopes.md).

## Approvals still apply

Every call from Codex uses your login token, so MEHO's approval rules
apply. Safe operations run at once. An operation with a higher risk level
waits until a second person approves it in the operator console or the
CLI. An agent cannot approve its own work. The tool-approval settings
inside Codex do not replace this.

**Check:** run one change of risk level `caution` or higher on a test
target. It should wait. After a person approves it, it should run.

## If the direct login does not work

Use the [`mcp-remote` shim](claude-desktop.md) as a local stdio server in
Codex. The shim runs the login for the client. The setup is the same as
for Claude Desktop. The static-token variant on the
[other clients page](mcp-remote-shim.md) is a last resort, because its
token expires quickly.
