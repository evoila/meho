# Other MCP clients (the `mcp-remote` shim)

Some MCP clients — Cursor as of this writing, and others — follow the
RFC 9728 metadata trail correctly but **expose no `client_id` field**
in their config. They then fall back to Dynamic Client Registration,
which Keycloak's Trusted Hosts policy blocks, and the wire-up never
completes. Pre-registering `meho-mcp` does not help: the client has
nowhere to put the resulting id.

opencode has its own login for remote MCP servers, with a client ID
setting (`oauth.clientId`). That login has not been tested against MEHO.
The setup tested with opencode is the local shim on this page (see
[Example: opencode](#example-opencode)).

The general-purpose way around this is a
[`mcp-remote`](https://github.com/geelen/mcp-remote) stdio shim. The
client starts the shim as a local command. The shim signs in as the
registered client `meho-mcp`, keeps the login, and forwards the calls to
`/mcp`. This works for any client that can start a local command.

There are two ways to run the shim:

1. **Login mode (use this one).** The shim signs in once and then renews
   its login by itself. See [Connect with a login that renews
   itself](#connect-with-a-login-that-renews-itself) and, for a machine
   with no browser, [A machine without a browser](#a-machine-without-a-browser-for-example-an-agent-on-a-jump-host).
2. **Static token (last resort).** You make a token by hand and give it to
   the shim. The token ends after 5 minutes. See
   [The static token](#the-static-token-last-resort).

Runs here, as everywhere in this section, from a machine **on the
internal network / VPN**.

## Connect with a login that renews itself

Set the client's MCP server to a local command that runs `mcp-remote` in
login mode. The exact config key differs per client (`command` + `args`,
an `mcp.json`, a settings pane). The command is the same:

```bash
npx -y mcp-remote@0.1.38 https://meho.example.com/mcp 8456 \
  --static-oauth-client-info '{"client_id": "meho-mcp"}' \
  --static-oauth-client-metadata '{"scope": "mcp:read mcp:execute offline_access"}'
```

- `8456` is the local port where the shim waits for the sign-in to come
  back. The realm's `meho-mcp` client accepts any local port.
- `--static-oauth-client-info` names the registered client `meho-mcp`
  (a public client, so there is no client secret).
- `offline_access` in the scope makes the login last between uses.
- The shim keeps the login in `~/.mcp-auth` and renews the 5-minute access
  token by itself. You do not mint a token and you put no token in any
  config.

If your login belongs to several tenants, add the tenant header to the
command, in the same way for the sign-in and for the client:

```bash
npx -y mcp-remote@0.1.38 https://meho.example.com/mcp 8456 \
  --static-oauth-client-info '{"client_id": "meho-mcp"}' \
  --static-oauth-client-metadata '{"scope": "mcp:read mcp:execute offline_access"}' \
  --header 'Meho-Tenant:<tenant name or ID>'
```

Without it, only `meho_status` works. Why, and how a tenant is chosen, is
in [Which tenant a session works in](mcp-surface-and-scopes.md#which-tenant-a-session-works-in).

The shim needs Node.js with `npx` on the machine where it runs. For an
internal CA, set `NODE_EXTRA_CA_CERTS=/path/to/internal-ca.pem` in the
environment of the shim.

The redirect addresses the realm must accept, the CA trust setting for
Node (`NODE_EXTRA_CA_CERTS`) and the sharp edges of the shim are the same
as for Claude Desktop. They are on the
[Claude Desktop page](claude-desktop.md#one-time-realm-redirect-uris)
and are not copied here.

The first time, the shim opens a browser for the sign-in. If the machine
has no browser, follow the next section.

## A machine without a browser (for example an agent on a jump host)

Use this when the client runs where no browser can open, such as a
headless agent on a jump host. You sign in once from your laptop, through
an SSH tunnel. After that the shim renews the login by itself.

You need a laptop with a browser and SSH access to the jump host. The
jump host needs Node.js with `npx`. For an internal CA, set
`NODE_EXTRA_CA_CERTS=/path/to/internal-ca.pem` in the environment of
every shim run on it, by hand (step 2) and from the client (step 6).

1. **Make the login folder private** (on the jump host). The shim stores
   its login in `~/.mcp-auth`. It writes each file readable only by its
   owner, but it makes the folders with your default permissions, which
   usually let other users look inside. Close that first:

    ```bash
    umask 077
    mkdir -p ~/.mcp-auth && chmod 700 ~/.mcp-auth
    ```

    Run the later steps as the same user, and run the client as that
    user too.

2. **Start the shim once, by hand** (on the jump host), with the command
   from the section above. Leave it running. It prints an address after
   the line `Please authorize this client by visiting:`. Copy that
   address. It also prints `Browser opened automatically.` Ignore that
   line on a machine with no browser: no browser opened. With several
   tenants, the command must hold the `--header 'Meho-Tenant:...'` part.

3. **Open an SSH tunnel** (on your laptop) for the shim's local port.
   Leave it open:

    ```bash
    ssh -L 127.0.0.1:8456:127.0.0.1:8456 <jump-host>
    ```

    The port must be the one in the shim command. `127.0.0.1:` at the
    start keeps the laptop end of the tunnel on your laptop only. The
    shim listens only on `127.0.0.1` of the jump host. So the port is
    closed to other machines, but other users on the jump host can still
    reach it. Keep the shim running only for the sign-in. The sign-in
    comes back to `http://localhost:8456/oauth/callback`, and the tunnel
    carries it to the shim.

4. **Sign in** (on your laptop). Open the address from step 2 in your
   laptop's browser and sign in. The browser then shows `Authorization
   successful!`. The shim is not done yet: it still has to finish the
   sign-in and save the login. Wait until the shim prints `Press Ctrl+C
   to exit` on the jump host.

5. **Stop the shim** with Ctrl+C, but only after it printed `Press Ctrl+C
   to exit`, and close the tunnel. The login stays in `~/.mcp-auth` on
   the jump host. If you stop it earlier, no login may be saved, and the
   client then cannot connect.

6. **Start the client.** Set its MCP server to the same command, as the
   same user. The shim finds the stored login and needs no sign-in.

    The command must be exactly the same as in step 2: the same address,
    the same `--header` parts and the same `mcp-remote` version. The shim
    files the login under the address and the headers (and keeps it in a
    folder named for the version). With any difference, it finds no
    login and starts a new sign-in, which a headless client cannot finish.
    With several tenants, the tenant header must be in both places.

7. **Check it** with `meho_status` (see [Verify](#verify)).

### Example: opencode

opencode takes the same command in its config file, `opencode.json` in
the project folder. Each server is an entry under `mcp`, and the command
is a list of words. This was run with opencode 1.19.6:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "meho": {
      "type": "local",
      "command": [
        "npx", "-y", "mcp-remote@0.1.38", "https://meho.example.com/mcp", "8456",
        "--static-oauth-client-info", "{\"client_id\": \"meho-mcp\"}",
        "--static-oauth-client-metadata", "{\"scope\": \"mcp:read mcp:execute offline_access\"}"
      ],
      "enabled": true
    }
  }
}
```

Start opencode as the same user who did the sign-in in step 2, so the shim
finds `~/.mcp-auth`. `opencode mcp list` shows `meho` as `connected` when
the stored login works. For the `NODE_EXTRA_CA_CERTS` setting, add an
`"environment": {"NODE_EXTRA_CA_CERTS": "/path/to/internal-ca.pem"}`
entry to the server.

How long the login lasts:

- The access token ends after 5 minutes. The shim renews it by itself
  from the stored login. This needs no person and no tunnel.
- The login ends after **48 hours without use**, or after **7 days at
  most**, whichever comes first. These are the values of MEHO's built-in
  login setup. On your own Keycloak see
  [How long an MCP login lasts](../install/keycloak-realm.md#how-long-an-mcp-login-lasts).
- When it ends, a call through the client can hang, and the client
  shows no sign-in address. To sign in again:
    1. Stop the client, so its shim stops too. Two shims cannot share the
       port.
    2. Do steps 2 to 5 again, with the shim run by hand.
    3. Start the client again.

Treat `~/.mcp-auth` like a password. Anyone who can read it can use your
login until it ends. A call through the shim runs under your login, so
MEHO's approval rules apply to it as to every other call: a call that
needs approval waits for a person.

## The static token (last resort)

!!! warning "The token ends after 5 minutes"

    The token ends after 5 minutes. Every call then fails until you make
    a new token and restart the client. Use this only when the client
    cannot run the login mode above. Prefer a client that can carry a
    `client_id` — [Claude Code](claude-code.md), or the
    [Claude Desktop shim](claude-desktop.md) — or the login mode on this
    page.

In this mode the shim runs no login. It sends the token you give it.

### Mint a token

Log in with `--print-token`. The CLI then writes only the access token to
standard output. The login prompt and all other lines go to standard
error:

```bash
export MEHO_TOKEN="Bearer $(meho login --print-token https://meho.example.com)"
```

To use the token of an existing login without a new login, read it from
the CLI's credentials file instead; see
[Reading the raw token](cli.md#reading-the-raw-token).

Keep the `Bearer ` prefix (and its trailing space) inside the variable:
`mcp-remote` splits `--header` on the first colon into name and value,
and its README documents stashing a space-containing value in the
environment as the robust form for a bearer header.

### Point the client's stdio command at the shim

Configure the client's MCP server as a local stdio command running
`mcp-remote` with `--header`. The exact config key differs per client
(`command` + `args`, an `mcp.json`, a settings pane); the command is the
same:

```bash
npx -y mcp-remote@0.1.38 https://meho.example.com/mcp \
  --header 'Authorization:${MEHO_TOKEN}'
```

The single quotes are load-bearing: they stop the shell from expanding
`${MEHO_TOKEN}`, so the literal `${MEHO_TOKEN}` reaches `mcp-remote`,
which substitutes the value from its own environment — the token itself
never lands on the command line, where it would otherwise sit in the
process table for the whole life of this long-running shim. Because the
token still lives in the shim's environment as a bearer credential, treat
this static-token stopgap as unsuitable for shared or multi-user
workstations.

The backplane is HTTPS, so no `--allow-http` is needed. Set
`NODE_EXTRA_CA_CERTS=/path/to/internal-ca.pem` in the client's
environment for an internal-CA deploy — by default Node does not read the
OS trust store. If the root CA is already trusted there, setting
`NODE_USE_SYSTEM_CA=1` instead makes Node read it (Node 22.19+ / 24.6+).
Because the header carries the token, `mcp-remote` performs no OAuth flow
and needs no redirect URI on the realm.

## Verify

Once the client attaches the toolset, run **`meho_status`** and
**`list_targets`** — the same two-call check as every other client.
Then wait more than 10 minutes and run **`meho_status`** again. In login
mode it must still work, with no new sign-in. With a static token it
fails after 5 minutes.

What a `401` answer means:

- `token_expired` with a static token: the token has ended. Re-run the
  mint step and restart the client.
- `token_expired` in login mode: the access token has ended. The shim
  renews it by itself, so this alone does not mean the login has ended.
  Sign in again only when the renewal is refused, or calls hang (see
  [How long the login lasts](#a-machine-without-a-browser-for-example-an-agent-on-a-jump-host)
  above).
- `invalid_token`: this is not about time. For example, MEHO could not
  check the key that signed the token. A new sign-in does not fix it.
  Follow [Token issues but every call 401s](troubleshooting.md#token-issues-but-every-call-401s).

## The cleaner long-term answer

For clients on newer protocol versions, **Client ID Metadata Documents
(CIMD)** dissolve this wall entirely — the `client_id` becomes an HTTPS
URL the authorization server fetches, so neither DCR nor a pre-registered
client is needed. CIMD shipped **experimental** in Keycloak 26.6.0 and
is off by default; the realm-side recipe is in the
[values-examples deep-dive § CIMD onramp](https://github.com/evoila/meho/blob/main/deploy/values-examples/README.md#cimd-onramp--no-pre-registered-client-keycloak--2660-experimental).
It is out of scope for this page and pinned to a moving Keycloak
feature — treat it as a preview.
