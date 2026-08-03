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

## Authorization request fails with `invalid_scope` naming `offline_access` (W7)

**Symptom.** A browser-flow MCP client (Claude Code) 400s with
`{"error":"invalid_scope","error_description":"Invalid scopes: … offline_access"}`;
the Keycloak login page never appears.

**Wall.** The client requested `offline_access` to obtain a refresh
token, but the `meho-mcp` client does not have that scope assigned.

**Fix.** Assign the realm's built-in `offline_access` scope to
`meho-mcp` as an **optional** scope (not default — only flows that ask
for a refresh token should mint one). `meho-cli` is deliberately not
given it: the device-code CLI re-runs the device dance instead of
holding a long-lived refresh token.

## Token issues but every call 401s

The token is minted, but the backplane rejects it on every call. From
v0.3.2 the 401 body carries a **specific `detail` code** — read it
first, then match the row:

| `detail` code | What it means | Fix |
|---|---|---|
| `invalid_audience` | The token's `aud` does not include the MCP resource URI (`MCP_RESOURCE_URI`, i.e. `https://<host>/mcp`). | Confirm the `meho-mcp-audience` mapper is on the client that issues the operator's token (**W2**). A reverse proxy that rewrites the request path can also leave the audience pointing at the wrong URI — capture the issued token via the realm's introspection endpoint and read `aud`. |
| `missing_sub` | The token has no `sub` claim. Keycloak 25+ moved `sub` into a mapper inside the `basic` client scope, and clients created via the admin REST API do not auto-inherit realm default scopes. RFC 9068 makes `sub` required, so rejection is correct — the opacity is the problem. | Assign `basic` (and `roles`, `web-origins`, `acr`) as **default** client scopes, then log out and back in (**W3** — the deepest wall). |
| `missing_tenant_claim` / `missing_tenant_role_claim` | The `tenant-id` / `tenant-role` mapper is absent, so the backplane cannot place the caller in a tenant. | Add the tenant mappers (**W2**). Realm recipe: [Keycloak realm setup § mappers](../install/keycloak-realm.md#step-3-add-the-five-protocol-mappers). |
| `invalid_issuer` | The token's `iss` does not match the configured realm. | Point the client at the same realm `KEYCLOAK_ISSUER_URL` names; a stale OAuth state from a previous realm can survive across config changes. |
| `token_expired` / `token_not_yet_valid` | `exp` is in the past, or `nbf` in the future beyond leeway (clock skew). | Refresh the token; for `nbf`, sync the clock on the issuing or calling host. |
| `signature_verification_failed` | The JWS does not verify against the issuer's published keys. | Confirm the token comes from the realm the backplane validates against. |
| `invalid_token` | A structural failure with no more specific code — a truncated JWS, `alg: none`, or a `kid` absent from the JWKS. | Capture the raw `Authorization` value and confirm it is a three-segment RS256 JWS. |

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

**Fix.** Confirm the `tenant-role` mapper emits a role of at least
`read_only`; walk back through the realm's tenant mappers
([Keycloak realm setup](../install/keycloak-realm.md)).

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
