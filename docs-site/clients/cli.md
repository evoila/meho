# The `meho` CLI

The `meho` CLI is a single static Go binary and the **starting point**
for connecting to a backplane. It runs on a VPN-connected workstation,
verifies TLS against your operating system's trust store, and
authenticates with the OAuth 2.0 device-code flow — so it works on
private networks with no browser redirect back to the client.

Install it and log in before wiring any MCP client: registering targets
and secrets happens only through the CLI (and the operator console /
REST), never through MCP tools, so a useful agent session depends on
this step having happened first.

## Install the signed binary

Releases ship as four platform tarballs plus a `SHA256SUMS` file at the
[releases page](https://github.com/evoila/meho/releases). Each artefact
carries a matching `.cosign.bundle` sigstore bundle — signed keyless
via [cosign](https://docs.sigstore.dev/) (the GitHub Actions OIDC token
is exchanged at Fulcio for a short-lived signing cert), so there is no
public key to distribute. The canonical operator recipe is in the
[CLI README § Verify signatures](https://github.com/evoila/meho/blob/main/cli/README.md#verify-signatures).

Download the tarball and its checksums file:

```bash
TAG=<the release tag matching your backplane, e.g. v0.27.0>
TARBALL=meho_${TAG#v}_linux_amd64.tar.gz   # or darwin_arm64, linux_arm64, darwin_amd64
BASE=https://github.com/evoila/meho/releases/download/${TAG}

curl -LO ${BASE}/${TARBALL}
curl -LO ${BASE}/${TARBALL}.cosign.bundle
curl -LO ${BASE}/SHA256SUMS
curl -LO ${BASE}/SHA256SUMS.cosign.bundle
```

Verify the signatures with [cosign](https://docs.sigstore.dev/), then
the checksums. The identity regex pins the signer to this repo's
release workflow on a tag ref, so a bundle produced anywhere else fails
the check:

```bash
IDENTITY='^https://github\.com/evoila/meho/\.github/workflows/cli-release\.yml@refs/tags/v.+$'
ISSUER='https://token.actions.githubusercontent.com'

# Verify the checksums file's signature once...
cosign verify-blob \
  --certificate-identity-regexp "$IDENTITY" \
  --certificate-oidc-issuer "$ISSUER" \
  --bundle SHA256SUMS.cosign.bundle \
  SHA256SUMS

# ...then trust every tarball it covers via the checksums.
sha256sum -c SHA256SUMS --ignore-missing
```

Unpack and install:

```bash
tar xzf ${TARBALL}
sudo install -m 0755 meho /usr/local/bin/meho
meho version
```

## Trust the deployment's CA

Skip this if your backplane and Keycloak present publicly-trusted
certificates.

If they are signed by an **internal CA**, install that CA into your
workstation's **operating-system trust store** first. The CLI is a Go
binary — on macOS it reads the system keychain and **ignores the
`SSL_CERT_FILE` environment variable entirely**, so the env-var trick
that works on the backplane does not carry over here. The per-platform
commands are on
[TLS and ingress § Your workstation](../install/tls-ingress.md#your-workstation-os-trust-store).

Verify from a fresh shell before logging in — `meho login` contacts
**both** the backplane and Keycloak:

```bash
curl -sf https://meho.example.com/healthz
curl -sf https://keycloak.example.com/realms/<realm>/.well-known/openid-configuration
```

## Log in

```bash
meho login https://meho.example.com
```

This runs the device-code flow: the CLI discovers the realm and the
public `meho-cli` client id from the backplane's
`/api/v1/auth-config` endpoint, prints a verification URL and code, you
approve it in a browser as a realm user, and the resulting token is
stored in your OS keyring (Keychain / Secret Service / Wincred).

The CLI also writes the backplane URL to `~/.config/meho/config.json`
(`$XDG_CONFIG_HOME/meho/config.json` when set) so later subcommands do
not need the URL re-typed. That file holds **only** the backplane URL —
no secret; the token lives in the keyring, or in a `0600`-mode
`credentials.json` sibling on headless hosts where no keyring is
available.

Two useful overrides:

- `meho login --client-id <id>` / `--issuer <url>` skip or override the
  auto-discovered values — handy when a realm publishes several CLI
  clients (`meho-cli-prod`, `meho-cli-staging`).
- `MEHO_KEYRING_DISABLE=1 meho login …` forces the file backend even
  where a keyring exists (see
  [reading the raw token](#reading-the-raw-token)).

Prove the whole chain end to end:

```bash
meho status
```

`meho status` calls `/api/v1/health` with the stored token, so a clean
result confirms CLI, token, ingress, and backplane all agree.

## Known walls

Two workstation-side issues account for most first-login failures:

- **Internal-CA trust.** `meho login` dying at its discovery probe with
  `x509: certificate signed by unknown authority` means the
  deployment's CA is not in your OS trust store — redo
  [Trust the deployment's CA](#trust-the-deployments-ca). This is the
  workstation twin of the backplane's own internal-CA trust problem.
- **Split DNS.** If the backplane's hostname resolves to different
  addresses inside and outside the VPN, a login started off-VPN (or
  during a VPN-idle DNS flap) can blackhole the first lookup. Confirm
  the host resolves to its internal address from the machine you are
  logging in from; a temporary `/etc/hosts` pin to the internal VIP is
  a reliable workaround while you sort DNS out.

For realm-side login failures (`unauthorized_client`,
`invalid_audience`, the `missing_sub` / `basic`-scope trap), the
symptom-to-fix table is
[Keycloak realm setup § If login fails](../install/keycloak-realm.md#if-login-fails),
and the deeper cross-wall walk is the
[troubleshooting page](troubleshooting.md).

## Reading the raw token

There is **no `meho ... --print-token` verb** — the CLI never prints
the bearer token to stdout. When you genuinely need the raw JWT (to
decode its claims, or to bake it into a shim — see
[Other MCP clients](mcp-remote-shim.md)), force the file backend and
read it from `credentials.json`:

```bash
MEHO_KEYRING_DISABLE=1 meho login https://meho.example.com

# The file holds one entry per backplane; extract the access token:
jq -r '.entries[].access_token' \
  "${XDG_CONFIG_HOME:-$HOME/.config}/meho/credentials.json"
```

## Where next

- **[Register targets and secrets](../guides/targets-and-secrets.md)** —
  the first real work, and why the CLI had to come first.
- **[Connect an MCP client](index.md#the-mcp-client-matrix)** — add
  Claude Desktop, Claude Code, or another client on top of the working
  CLI.
