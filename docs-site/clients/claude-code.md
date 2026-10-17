# Claude Code

There are two ways to connect Claude Code to a MEHO backplane:

- **The MEHO plugin (recommended)** — one command installs the MCP
  wiring and MEHO's operating discipline as skills. Start here.
- **A manual `.mcp.json`** — native HTTP MCP you configure yourself.
  Use it for non-plugin clients, CI bots, or when you want the wiring
  in the repo.

Either way, Claude Code must run on a machine **on the internal network
/ VPN** — MEHO is internal-only — and nothing is exposed to the
internet.

## Before you start: trust your deployment's CA

Do this step first. It takes a few minutes.

A CA (certificate authority) is the certificate that signs the server
certificates of your MEHO deployment. Many deployments use their own
internal CA. Your machine does not trust an internal CA yet, so every
login fails until you add it.

### 1. Check if you need this step

Run the command for your shell. Use your backplane host in place of
`meho.example.com`.

- **macOS and Linux** (bash or zsh):

    ```bash
    curl -sS -o /dev/null https://meho.example.com/healthz && echo trusted
    ```

- **Windows** (PowerShell; type `curl.exe`, because plain `curl` is a
  PowerShell alias for a different command):

    ```powershell
    curl.exe -sS -o NUL https://meho.example.com/healthz; if ($LASTEXITCODE -eq 0) { "trusted" }
    ```

- It prints `trusted`: your machine already trusts the deployment. Skip
  to the [next section](#the-one-command-path-the-meho-plugin).
- It fails with `SSL certificate problem` (for example `unable to get
  local issuer certificate`): the deployment uses an internal CA. Go on.

### 2. Get the root CA file

The MEHO servers send only their own certificate, not the root CA. So
you cannot copy the root CA from the server. The person or team that
deployed MEHO (your deployer) publishes it in a place you can open
before you trust it, for example an internal docs page, an item in your
team's password manager, or a plain-HTTP link such as
`http://ca.example.internal/root.pem`. Ask your deployer where it is.
You do not need access to the secrets store or to the cluster.

Save the file as `internal-ca.pem`. For a plain-HTTP link:

```bash
curl -fsS -o internal-ca.pem http://ca.example.internal/root.pem
```

Then check that it is the right file before you trust it. You need the
expected fingerprint from a place you already trust, such as your
team's password manager or an internal docs page you log in to. Do not
take it from the same plain-HTTP page as the file: someone who can swap
the file can also swap a fingerprint shown next to it. If you have no
trusted source for the fingerprint, stop and ask your deployer.

Print the file's SHA-256 fingerprint and compare it with the expected
one (on Windows, run this in Git Bash, which includes `openssl`):

```bash
openssl x509 -in internal-ca.pem -noout -subject -fingerprint -sha256
```

If the fingerprints do not match, stop and ask your deployer. Do not go
on to step 3 until they match.

### 3. Trust the root CA

Two parts need it:

- **Your OS trust store**, for the `meho` CLI. Run the one command for
  your system, then open a new terminal:
    - macOS: `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain internal-ca.pem`
    - Linux (Debian, Ubuntu): `sudo cp internal-ca.pem /usr/local/share/ca-certificates/internal-ca.crt && sudo update-ca-certificates`
    - Windows (terminal started as administrator): `certutil -addstore -f Root internal-ca.pem`
- **The plugin's MCP shim.** It runs on Node, which does not read the
  OS trust store. Give it the file in `~/.config/meho/plugin.env` (next
  section):

    ```bash
    MEHO_CA_CERT="$HOME/internal-ca.pem"   # where you saved the file
    ```

    On the manual `.mcp.json` path, use `NODE_EXTRA_CA_CERTS` instead —
    see [Internal-CA trust](#internal-ca-trust).

Run the check from step 1 again. It now prints `trusted`.

Deployers: the
[deployer guide](https://github.com/evoila/meho/blob/main/deploy/values-examples/README.md#publish-the-root-ca-for-operators)
says how to publish the root CA so that operators can do this step.

## The one-command path: the MEHO plugin

MEHO ships an in-repo Claude Code plugin marketplace. Add it and install
the plugin:

```bash
claude plugin marketplace add evoila/meho
/plugin install meho@meho
```

The plugin carries the MCP server (over the bundled `mcp-remote` stdio
shim, so no per-project `.mcp.json` editing) and loads the MEHO-first
operating discipline as skills. Point it at your backplane by setting
the endpoint once — either an environment variable or a small config
file the plugin reads:

```bash
# ~/.config/meho/plugin.env
MEHO_MCP_URL="https://meho.example.com/mcp"
# MEHO_CA_CERT="/path/to/internal-ca.pem"   # only on internal-CA deploys
```

`MEHO_CA_CERT` is only needed on an internal-CA deploy (the plugin's
shim runs on Node, which does not read the OS trust store). Get the file
first — see [Before you start](#before-you-start-trust-your-deployments-ca). To opt a
session into the operator planes, set `MEHO_MCP_SCOPES` to add
`mcp:admin` — see
[MCP surface and scopes](mcp-surface-and-scopes.md#how-a-client-requests-elevation).
The realm still needs the public `meho-mcp` client and its loopback
redirect URI, exactly as the manual path below (see
[Keycloak realm setup](../install/keycloak-realm.md)).

### How long the plugin login lasts

The plugin asks for `offline_access` by default (default scopes:
`mcp:read mcp:execute offline_access`). So the login lasts until you have
not used MEHO for the `meho-mcp` client's offline idle limit: 48 hours
with the [recommended realm setting](../install/keycloak-realm.md#how-long-an-mcp-login-lasts).
Using MEHO renews the login. `MEHO_MCP_SCOPES` replaces the whole list, so
keep `offline_access` in it when you set it.

When the login has expired, the next MEHO call fails at once with "Your
MEHO login has expired. Run /mcp, pick meho, and reconnect to log in
again." A login that nobody finishes stops after 5 minutes. No MEHO call
waits for a login. See
[troubleshooting](troubleshooting.md#a-meho-call-says-your-meho-login-has-expired-claude-code-plugin).

Claude Code does not update plugins from this marketplace on its own,
unless you turn on auto-update for the marketplace in `/plugin`. To get a
new plugin version, run `claude plugin update meho@meho`, then restart
Claude Code.

## The manual path: configure `.mcp.json`

If you are not using the plugin, Claude Code also speaks MCP over
**native HTTP** — no shim. You point a project-scoped `.mcp.json` at the
backplane's `/mcp` route and pin the pre-registered `meho-mcp` public
client; Claude Code runs the OAuth 2.1 authorization-code + PKCE flow
itself, listening on a loopback port for the callback. This is the
pattern both MEHO dogfood repos run daily.

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
