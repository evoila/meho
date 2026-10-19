# Troubleshooting auth walls

First connections fail in a small, well-mapped set of ways. This page
is **symptom-first**: match the error you see, read the wall behind it,
apply the fix. The `W#` labels cross-reference the deployer recipe's
[auth-onramp matrix](https://github.com/evoila/meho/blob/main/deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp),
which carries the deepest realm-side detail.

!!! note "Every client here is internal-only"

    MEHO is never publicly exposed. If a client cannot reach the
    backplane's `/.well-known/oauth-protected-resource` metadata, the
    fix is to put the client (or its `mcp-remote` shim) **on the
    VPN / internal network** — never to expose the backplane through a
    public ingress or tunnel.

## Start with `meho doctor`

`meho doctor` checks your machine and names the layer that fails. It
only reports; it changes nothing.

```
meho doctor
```

- The **offline layer** checks the plugin shim, the vendored
  `mcp-remote` lock and cache, your CA bundle, and which credential
  store holds your login.
- The **authenticated layer** checks DNS, TLS, and your token, then runs
  the MCP handshake. It shows your scopes and any working-surface tool
  your session should see but does not.

Use the failure class it prints (`dns`, `tls`, `refresh_rejected`,
`surface_gap`, ...) to pick the section below. See also the
[CLI reference](../reference/cli.md) and
[MCP surface and scopes](mcp-surface-and-scopes.md).

## The client never reaches the OAuth screen

**Symptom.** The `/mcp` call returns `401` with
`WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource"`,
but the client never opens a browser or shows a login.

**Wall.** The client cannot fetch the `resource_metadata` URL, or the
URL points at a host it cannot resolve.

**Fix.** Confirm the client's machine reaches the backplane over the
VPN, and that `BACKPLANE_URL` in the backplane ConfigMap resolves to the
internal hostname clients actually use — if it is wrong, the metadata
document advertises the wrong host.

## DCR fails with `403 Host not trusted` (W1)

**Symptom.**

```json
{"error":"insufficient_scope",
 "error_description":"Policy 'Trusted Hosts' rejected request to
  client-registration service. Details: Host not trusted."}
```

**Wall.** The client has no `client_id` in its config, so it attempted
Dynamic Client Registration (RFC 7591). Keycloak's default Trusted Hosts
policy ships with an empty whitelist, so anonymous DCR is disabled — the
correct, secure response.

**Fix.** Give the client a pre-registered `client_id`. [Claude Code](claude-code.md)
takes it in `.mcp.json` (`oauth.clientId`); the
[Claude Desktop shim](claude-desktop.md) takes it via
`--static-oauth-client-info`. For a client that exposes neither, carry a
token through the [static-token shim](mcp-remote-shim.md). Do **not**
open DCR on a production realm to work around a per-client config gap.

## Login start fails with `unauthorized_client` (W1)

**Symptom.** The device-code or authorization request 401s with
`{"error":"unauthorized_client", …}` before any browser approval.

**Wall.** The flow ran against a **confidential** client (typically
`meho-backplane`) that requires a secret the client cannot hold.

**Fix.** Point the client at a **public** client — `meho-cli` for the
CLI device-code flow, `meho-mcp` for MCP. See
[Keycloak realm setup](../install/keycloak-realm.md).

## Authorization request fails with `invalid_scope` (W7)

**Symptom.** The Keycloak login page never appears. Keycloak sends the
browser back to the client's callback address with
`error=invalid_scope` and `error_description=Invalid scopes: …`. The
message names one or more of `offline_access`, `mcp:read`, `mcp:execute`
or `mcp:admin`. Claude Code shows this error. With the `mcp-remote` shim
(the MEHO plugin and the Claude Desktop bundle), the browser tab shows
`Error: No authorization code received`.

**Wall.** The client asked for a scope that the `meho-mcp` client does
not offer. Keycloak refuses a login that asks for a scope the client does
not offer as a default or optional client scope. It does not drop the
scope silently. Four scopes can cause this:

- `offline_access` — the MEHO plugin, the Claude Desktop bundle, and
  Claude Code's own HTTP client all ask for it.
- `mcp:read` and `mcp:execute` — the MEHO plugin and the Claude Desktop
  bundle ask for them at every login.
- `mcp:admin` — asked for only when you turn on the operator tools.

Keycloak ships `offline_access`, but not the three `mcp:*` scopes. A realm
set up by hand often misses them.

**Fix.** Re-run `meho admin keycloak bootstrap-clients`. It creates the
missing `mcp:*` scopes and adds all four scopes to `meho-mcp` as
**optional**. Or do it by hand: create each missing `mcp:*` scope under
**Client scopes → Create client scope**, then add each scope the error
names to `meho-mcp` as an **optional** scope (not default — only flows
that ask for a scope should get it). See
[Keycloak realm setup](../install/keycloak-realm.md#the-mcp-client-meho-mcp).
`meho-cli` does not get these scopes by default.

## The MEHO login ends after about 30 minutes with no use

**Symptom.** MEHO tools work, but after about half an hour without a MEHO
call, the next call opens a new browser login or does not answer.

**Wall.** The client did not ask for `offline_access`. Then Keycloak
issues a normal refresh token, and it ends at the realm's SSO idle limit
(30 minutes by default). Older versions of the MEHO plugin and the Claude
Desktop bundle did not ask for it.

**Fix.** Update the MEHO plugin (`claude plugin update meho@meho`, then
restart Claude Code) or install the current Claude Desktop bundle. If you
set the scopes yourself (`MEHO_MCP_SCOPES` or the bundle's `scopes`
field), keep `offline_access` in the list. The login then lasts until the
`meho-mcp` client's offline idle limit, 48 hours with the
[recommended realm setting](../install/keycloak-realm.md#how-long-an-mcp-login-lasts).

## A MEHO call says "Your MEHO login has expired" (Claude Desktop)

**Symptom.** A MEHO tool call fails at once with "Your MEHO login has
expired. Turn the MEHO extension off and on again in Claude Desktop
(Settings > Extensions) to log in again." After that, the extension shows
as stopped.

**Wall.** The saved login can no longer be renewed: it was not used for
the offline idle limit, or the session was ended in Keycloak. A new
browser login is needed. The bundle does not wait for that login in the
middle of a call, so the call fails fast instead of hanging. Older bundles
gave the call no answer at all.

**Fix.** In Claude Desktop, open **Settings > Extensions** and turn the
MEHO extension off and on. Finish the login in the browser tab that opens;
it may open behind the Claude Desktop window. If a MEHO login tab opened
when the call failed, you can close it.

## The extension stops with "MEHO did not connect" (Claude Desktop)

**Symptom.** The MEHO extension shows as stopped, and its log has a line
like `meho-claude-desktop: MEHO did not connect within 5 minutes, so this
attempt stopped.`

**Wall.** A browser login started, but nobody finished it within 5
minutes. Stopping frees the local login port, so the next attempt can log
in. Older bundles waited forever instead.

**Fix.** Finish any open MEHO login in your browser. Then turn the MEHO
extension off and on in **Settings > Extensions**. If several MEHO login
tabs are open, close them all first.

## A MEHO call says "Your MEHO login has expired" (Claude Code plugin)

**Symptom.** A MEHO tool call fails at once with "Your MEHO login has
expired. Run /mcp, pick meho, and reconnect to log in again." After that,
`/mcp` shows `meho` as failed.

**Wall.** The saved login can no longer be renewed: it was not used for
the offline idle limit, or the session was ended in Keycloak. A new
browser login is needed. The plugin does not wait for that login in the
middle of a call, so the call fails fast instead of hanging.

**Fix.** Run `/mcp`, pick `meho`, and reconnect. Finish the login in the
browser tab that opens. If Claude Code stops waiting before you finish,
finish the login anyway and reconnect once more. If a MEHO login tab
opened when the call failed, you can close it.

## The plugin stops with "MEHO did not connect" (Claude Code plugin)

**Symptom.** `meho` shows as failed, and the MCP log has a line like
`meho plugin: MEHO did not connect within 5 minutes, so this attempt
stopped.`

**Wall.** The connect did not finish within `MEHO_MCP_LOGIN_TIMEOUT`
seconds (default 300). Two common reasons:

- A browser login started, but nobody finished it in time.
- Another Claude Code session was running a login at the same time. This
  session then waits, and it does not pick up the other session's login
  even when that login is done.

Stopping frees the local callback port, so the next attempt can log in.
Older plugin versions waited forever instead, and every later session
failed with `listen EADDRINUSE`.

**Fix.** Finish any open MEHO login in your browser. Then run `/mcp`, pick
`meho`, and reconnect. If the login tab opens behind your terminal, look
for it there. If several MEHO login tabs are open, close them all and
reconnect once.

A session that is connected does not hold the callback port. It frees
the port and removes its lockfile as soon as it connects, so a new login
in another session can finish while it stays open.

## Token issues but every call 401s

The token is minted, but the backplane rejects it on every call. From
v0.3.2 the 401 body carries a **specific `detail` code** — read it
first, then match the row:

| `detail` code | What it means | Fix |
|---|---|---|
| `invalid_audience` | The token's `aud` does not include the MCP resource URI (`MCP_RESOURCE_URI`, i.e. `https://<host>/mcp`). | Confirm the `meho-mcp-audience` mapper is on the client that issues the operator's token (**W2**). A reverse proxy that rewrites the request path can also leave the audience pointing at the wrong URI — capture the issued token via the realm's introspection endpoint and read `aud`. |
| `missing_sub` | The token has no `sub` claim. Keycloak 25+ moved `sub` into a mapper inside the `basic` client scope, and clients created via the admin REST API do not auto-inherit realm default scopes. RFC 9068 makes `sub` required, so rejection is correct — the opacity is the problem. | Assign `basic` (and `roles`, `web-origins`, `acr`) as **default** client scopes, then log out and back in (**W3** — the deepest wall). |
| `missing_tenant_claim` / `missing_tenant_role_claim` | The person is in none of the five MEHO groups, or the `tenant-id` / `tenant-role` mapper is absent, so the backplane cannot place the caller in a tenant. | Put the person in one group ([Keycloak realm setup § groups](../install/keycloak-realm.md#step-5-create-the-five-groups)), or add the tenant mappers (**W2**, [§ mappers](../install/keycloak-realm.md#step-3-add-the-six-protocol-mappers)). |
| `invalid_issuer` | The token's `iss` does not match the configured realm. | Point the client at the same realm `KEYCLOAK_ISSUER_URL` names; a stale OAuth state from a previous realm can survive across config changes. |
| `token_expired` / `token_not_yet_valid` | `exp` is in the past, or `nbf` in the future beyond leeway (clock skew). | Refresh the token; for `nbf`, sync the clock on the issuing or calling host. |
| `signature_verification_failed` | The JWS does not verify against the issuer's published keys. | Confirm the token comes from the realm the backplane validates against. |
| `invalid_token` | A structural failure with no more specific code — a truncated JWS, `alg: none`, or a `kid` absent from the JWKS. | Inspect the token **locally** — confirm it is a three-segment RS256 JWS (segment count, `alg`, `kid`). Treat the bearer as a live credential: decode it on your own machine and never paste it into a shared issue, log, or chat — redact it from any diagnostic you share. |

The diagnostic detail (expected audience, claim name, exception class)
is in the backplane's **server log**, not the 401 body — that
body-vs-log split is the deliberate info-leak boundary an
unauthenticated 401 honours.

## `meho login` times out before you can approve (W4)

**Symptom.** `meho: token exchange failed: context deadline exceeded`,
often before you have finished approving in the browser.

**Wall.** Not the device-code TTL (10 minutes). An ambient **parent
deadline** — a CI step timeout, an IDE task wrapper, an agent
bash-tool timeout — truncates the approval wait.

**Fix.** Run `meho login` in a real interactive terminal without a short
wrapper deadline, or raise the wrapper timeout above the device-code
lifetime.

## `tools/list` returns an empty list

**Symptom.** The client connects and authenticates, but no MEHO tools
appear.

**Wall.** The token's `tenant_role` claim is below the rank a tool
requires (`read_only < operator < tenant_admin`), or the claim is
missing entirely.

**Fix.** Confirm the person is in one of the five MEHO groups and the
`tenant-role` mapper is on the client; walk back through the realm
setup ([Keycloak realm setup](../install/keycloak-realm.md)).

## The whole toolset is rejected (Claude Desktop)

**Symptom.** Settings → Connectors lists the MEHO tools, but a
conversation sees **none** of them; the UI names a tool-name pattern
error like
`String should match pattern '^[a-zA-Z0-9_-]{1,64}$'`.

**Wall.** Claude's frontend validates every remote-MCP tool name against
that pattern and rejects the **whole** toolset if any name fails. The
older dotted names (`meho.status`) failed.

**Fix.** Pin backplane **v0.27.0 or later**, where the tools were renamed
to underscores (`meho_status`, `list_targets`, …). See
[Claude Desktop § pinning a build](claude-desktop.md#pin-a-build-with-underscore-tool-names-and-structured-results).

## A tool call fails with "Tool execution failed" but the server logged 200

**Symptom.** A specific tool (e.g. `list_targets`, `query_topology`)
fails client-side with "Tool execution failed", while the backplane logs
a clean `200` and writes an audit row. `meho_status` works.

**Wall.** The tool declares an `outputSchema`, and MCP 2025-06-18
requires a conforming `structuredContent` result for such tools — which
Claude's frontend enforces. A build without the fix omits it, so every
schema-declaring tool hard-fails.

**Fix.** Pin a backplane build carrying the `structuredContent` fix
(merged after v0.27.0). Tools that declare no schema (`meho_status`) are
unaffected.

## Newly-shipped tools don't appear after a backplane upgrade

**Symptom.** After upgrading the backplane, a new tool stays invisible;
`tools/list` keeps returning the old catalog.

**Wall.** The backplane advertises `tools.listChanged: false` (the
registry is fixed at process start), so a client that cached the catalog
at `initialize` has no signal to refetch.

**Fix.** Re-initialize the client — restart it, or disconnect and
reconnect the server. Also confirm the rollout actually cycled the
process (`kubectl rollout status`, or check the `serverInfo.version`),
since a new tool only exists in a new backplane process.

## A successful call left no audit row

**Symptom.** A tool call succeeded, but no `audit_log` row exists for it.

**Wall.** The MCP audit writer fails closed — an unauditable call returns
JSON-RPC `INTERNAL_ERROR` (-32603), so a *successful* call with no row
means the row was rolled back mid-request, most often a transient DB
connectivity issue.

**Fix.** Check the backplane's structured logs for a
`mcp_audit_write_failed` event carrying the exception class before
suspecting the writer.

## Going deeper

- Realm-side recipe, end to end: [Keycloak realm setup](../install/keycloak-realm.md).
- The full deployer auth-onramp matrix (per-audience capability claims,
  advanced realm shapes): [values-examples deep-dive](https://github.com/evoila/meho/blob/main/deploy/values-examples/README.md#auth-onramp-recipe-cli--mcp).
- Workstation TLS trust: [TLS and ingress](../install/tls-ingress.md#your-workstation-os-trust-store).
