# Connect clients

You have a running backplane. This section wires operators and agents
to it — **the CLI first**, then the MCP client matrix, then a
symptom-first [troubleshooting page](troubleshooting.md) for the auth
walls every first connection tends to hit.

!!! danger "MEHO is internal-only — never publicly exposed"

    MEHO is a governance backplane for internal infrastructure. It is
    deployed VPN-internal / behind an internal CA **by design** and
    must **never** be placed on public DNS or a public ingress — not
    even a short-lived copy. Every client below connects **from a
    machine already on the internal network / VPN**. This rules out the
    cloud-brokered **remote Custom Connector** (see
    [below](#remote-custom-connector-not-applicable)); do not expose the
    backplane to make it work.

## Start with the CLI

Even if your end goal is an agent in Claude Desktop or Claude Code,
install and log in with the [`meho` CLI](cli.md) first. Two reasons:

- **Registering targets and secrets has no MCP tools — deliberately.**
  That is an operator-trust decision, so it lives on the CLI, the
  operator console, and REST; agents get the read-only `list_targets`
  tool and consume whatever you registered
  ([Register targets and secrets](../guides/targets-and-secrets.md)).
  Until at least one target exists, an agent session can discover
  operations but cannot act — so the CLI is a prerequisite for a useful
  agent session anyway.
- The CLI is the fastest way to prove the realm, TLS trust, and token
  chain are correct before you add an MCP client's moving parts on top.

## The MCP client matrix

All three MCP paths are **internal-only** — the client, or the local
shim it spawns, runs on a VPN-connected machine and speaks Streamable
HTTP to the backplane's `/mcp` route directly. Pick the row that
matches your client:

| Client | How it connects | Page |
|---|---|---|
| **Claude Desktop** | A local [`mcp-remote`](https://github.com/geelen/mcp-remote) stdio→HTTP shim runs the OAuth 2.1 + PKCE flow and forwards to `/mcp`. The only Desktop path for an internal-only backplane. | [Claude Desktop](claude-desktop.md) |
| **Claude Code** | Native HTTP MCP with a loopback PKCE flow, pinned to the pre-registered `meho-mcp` public client. The pattern both dogfood repos run daily. | [Claude Code](claude-code.md) |
| **Cursor and other clients that can't carry a `client_id`** | A generic `mcp-remote` stdio shim carrying a CLI-minted bearer token. | [Other MCP clients](mcp-remote-shim.md) |

All three assume the realm already has the public `meho-mcp` OAuth
client and the operator's workstation trusts the deployment's CA. The
one-time realm work is in
[Keycloak realm setup](../install/keycloak-realm.md); the CA-trust step
is in [TLS and ingress](../install/tls-ingress.md#your-workstation-os-trust-store).

## Remote Custom Connector — not applicable

The **remote** claude.ai / Claude Desktop *Custom Connector* (pasting a
`/mcp` URL into Settings → Connectors) is **not a supported path for
MEHO**. Its connector backend runs in Anthropic's cloud, so it requires
the backplane to be **publicly reachable** to fetch the RFC 9728
metadata and run the OAuth handshake — a requirement MEHO, being
internal-only, deliberately never meets. Reach Claude Desktop through
the [`mcp-remote` shim](claude-desktop.md) instead, which runs on your
own VPN-connected machine and exposes nothing.

## When something breaks

First connections fail in a small, well-mapped set of ways — a token
that decodes but is rejected, an empty tool list, a client that never
reaches the OAuth screen. The [troubleshooting page](troubleshooting.md)
is symptom-first: find your error message, read the wall behind it, fix
it.
