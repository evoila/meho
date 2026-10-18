# Claude Desktop

Claude Desktop reaches an internal MEHO backplane through a **local
[`mcp-remote`](https://github.com/geelen/mcp-remote) stdio→HTTP shim**
that runs on your own VPN-connected machine. The shim runs the OAuth
2.1 + PKCE flow against your internal Keycloak and forwards
Streamable-HTTP calls to `/mcp`. This shim is the **only** Desktop
transport for an internal-only backplane — but you have two ways to set
it up:

- **The one-click `.mcpb` bundle (recommended)** — install by opening a
  file; a dialog asks for your backplane URL and an optional internal-CA
  bundle, and no JSON editing is involved. Start here.
- **A manual `claude_desktop_config.json`** — wire the same shim
  yourself. Use it if you cannot install the bundle, or want to see
  exactly what it runs.

Either way, everything runs on your own VPN-connected machine and
nothing is exposed to the internet.

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
  to the [next section](#the-one-command-path-the-mcpb-bundle).
- It fails with `SSL certificate problem` (for example `unable to get
  local issuer certificate`): the deployment uses an internal CA. Go on.

### 2. Get the root CA file

The MEHO servers send only their own certificate, not the root CA. So
you cannot copy the root CA from the server. The person or team that
deployed MEHO (your deployer) publishes it in a place you can open
before you trust it, for example:

- an internal docs page,
- an item in your team's password manager,
- a plain-HTTP link, such as `http://ca.example.internal/root.pem`.

Ask your deployer where it is. You do not need access to the secrets
store or to the cluster. Save the file as `internal-ca.pem`. For a
plain-HTTP link:

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

Run the one command for your system, then open a new terminal.

- **macOS** (asks for your password):

    ```bash
    sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain internal-ca.pem
    ```

- **Windows** (in a terminal you started as administrator):

    ```
    certutil -addstore -f Root internal-ca.pem
    ```

The `meho` CLI reads this trust store, and so does current Claude
Desktop (details in [Trusting an internal CA](#trusting-an-internal-ca)).
No admin rights on macOS? The login keychain command there is enough
for Claude Desktop.

Run the check from step 1 again. It now prints `trusted`. Keep
`internal-ca.pem`: you can also pick it in the bundle's CA field.

Deployers: the
[deployer guide](https://github.com/evoila/meho/blob/main/deploy/values-examples/README.md#publish-the-root-ca-for-operators)
says how to publish the root CA so that operators can do this step.

## The one-command path: the `.mcpb` bundle

MEHO ships a Claude Desktop bundle in the MCPB format, distributed as an
asset on each [GitHub Release](https://github.com/evoila/meho/releases).

!!! tip "Check the download before you install it"

    Each Release also has a checksum file for the bundle,
    `SHA256SUMS-mcpb`. Download it into the same folder as the `.mcpb`
    file and run (macOS or Linux):

    ```bash
    shasum -a 256 -c SHA256SUMS-mcpb
    ```

    It must print `meho-claude-desktop-<version>.mcpb: OK`. On Linux,
    `sha256sum -c SHA256SUMS-mcpb` works too.

    The checksum file is signed with cosign, like the CLI checksums. To
    check the signature as well, also download
    `SHA256SUMS-mcpb.cosign.bundle` and run:

    ```bash
    cosign verify-blob \
      --certificate-identity-regexp '^https://github\.com/evoila/meho/\.github/workflows/cli-release\.yml@refs/tags/v.+$' \
      --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
      --bundle SHA256SUMS-mcpb.cosign.bundle \
      SHA256SUMS-mcpb
    ```

Install it by **opening the `.mcpb` file** in Claude Desktop: it wires
the same `mcp-remote` shim and asks, in a dialog, for the two things
that differ per deployment —

- **MEHO MCP endpoint** — your backplane's `/mcp` URL (reachable over
  your VPN).
- **Internal CA bundle** *(optional)* — the PEM for the CA that signs
  the backplane's TLS certificate; the extension adds it to its own trust
  store. Leave it empty on a public-CA deploy, or when you trust the root
  CA in your OS trust store instead — see
  [Trusting an internal CA](#trusting-an-internal-ca).

Two advanced fields default to the working setup and rarely need
changing: the OAuth **client id** (default `meho-mcp`) and the OAuth
**scopes** (default `mcp:read mcp:execute offline_access`). To opt the
session into the operator planes, add `mcp:admin` to the scopes field — see
[MCP surface and scopes](mcp-surface-and-scopes.md#how-a-client-requests-elevation).

`offline_access` keeps the login alive between uses. The login lasts until
you have not used MEHO for the `meho-mcp` client's offline idle limit: 48
hours with the
[recommended realm setting](../install/keycloak-realm.md#how-long-an-mcp-login-lasts).
Without it, the login ends at the realm's SSO idle limit (30 minutes by
default). If you change the scopes field, keep `offline_access` in it.

### When the login expires or is not finished

The bundle makes sure a MEHO call never hangs because of a login:

- **The login expired.** The next MEHO call fails at once with "Your MEHO
  login has expired. Turn the MEHO extension off and on again in Claude
  Desktop (Settings > Extensions) to log in again." Then the extension
  stops. Turn it off and on in **Settings > Extensions**, and finish the
  browser login that opens.
- **Nobody finished the browser login.** The extension stops after 5
  minutes, so the local login port is free again. Finish any open MEHO
  login in your browser, then turn the extension off and on.

After you update to a bundle with this fix, the first use opens **one new
browser login**. The bundle's newer `mcp-remote` keeps saved logins in a
new folder, `~/.mcp-auth/mcp-remote-0.1.38/`.

The bundle still expects the realm's public `meho-mcp` client with the
shim's loopback redirect URIs registered (below), and — on an
internal-CA deploy — one of the two CA trust routes below. If you cannot
install the bundle, wire the shim by hand with the rest of this page.

## Trusting an internal CA

On an internal-CA deploy the shim must trust the CA that signs the
backplane's and Keycloak's certificates. Either route is enough; a
public-CA deploy needs neither.

- **The bundle's CA field.** Pick the CA's PEM file in the install
  dialog with the file picker. If you type the path instead, it must be
  absolute or start with `~/`. The bundle's launcher adds its
  certificates to Node's trust store in-process before the shim starts,
  keeping the trust it already has. A path it cannot read, or a file
  without a PEM certificate, stops the extension with an error naming
  the file.
- **Your OS trust store.** Leave the CA field empty and trust the root CA
  in the OS trust store. On macOS that is the login keychain:

    ```bash
    security add-trusted-cert -k ~/Library/Keychains/login.keychain-db internal-ca.pem
    ```

    Then toggle the extension off and on. Current Claude Desktop runs
    its built-in Node with `NODE_USE_SYSTEM_CA=1`, so Node reads the OS
    store with nothing to configure in the bundle (verified on macOS;
    Windows is not yet field-tested).

!!! note "Why the CA is not passed as `NODE_EXTRA_CA_CERTS`"

    Claude Desktop 1.3109.0 strips `NODE_EXTRA_CA_CERTS` from an
    extension's environment, so a CA delivered that way never reaches
    Node: the shim fails with `UNABLE_TO_VERIFY_LEAF_SIGNATURE` and
    Desktop reports an `initialize` timeout
    ([#3143](https://github.com/evoila/meho/issues/3143)). The bundle
    therefore passes the CA path as an argument and injects it
    in-process.

## Prerequisites

- Claude Desktop on a machine **on the internal network / VPN**.
- [Node.js](https://nodejs.org/) (for `npx`), which spawns the shim.
- The `meho` CLI installed and `meho login` working from the same
  machine ([The meho CLI](cli.md)) — it proves TLS trust and the realm
  before you add the shim's moving parts.
- On an internal-CA deploy, the deployment's CA in your OS trust store
  (the `meho` CLI reads it; see
  [Before you start](#before-you-start-trust-your-deployments-ca)), plus a way for the shim's Node to trust it —
  Node ignores the OS store unless told otherwise: a PEM file for
  `NODE_EXTRA_CA_CERTS`, or `NODE_USE_SYSTEM_CA=1` (below).
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
four default scopes, and `offline_access`, `mcp:read`, `mcp:execute` and
`mcp:admin` optional) is in
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
        "--static-oauth-client-metadata", "{\"scope\": \"mcp:read mcp:execute offline_access\"}"
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
- `offline_access` in the scope makes the login last between uses (see
  above). The `meho-mcp` client must offer it as an optional scope.
- `NODE_EXTRA_CA_CERTS` is only needed on an internal-CA deploy; drop it
  if your backplane and Keycloak present publicly-trusted certificates.
  The alternative is the OS route: with the root CA trusted in the OS
  store, use `"env": { "NODE_USE_SYSTEM_CA": "1" }` instead (Node 22.19+
  / 24.6+). Switch to it if the shim fails with
  `UNABLE_TO_VERIFY_LEAF_SIGNATURE` although `NODE_EXTRA_CA_CERTS` is set
  — current Desktop strips that variable from the bundle's environment,
  and this manual shape has not been re-tested on current Desktop.

Restart Claude Desktop. On first use the shim opens a browser, you
complete OAuth 2.1 + PKCE against the internal Keycloak (a static
client, so no DCR wall), and tokens land in `~/.mcp-auth`, which the
shim auto-refreshes.

## What actually works — and the sharp edges

The smoke test proved the transport end-to-end and surfaced three
findings. **Two are build-gated** — fixed on a current backplane, but
they dictate which build you must pin (below). **The third — resource
discovery — is a standing limitation** with no fix yet.

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
