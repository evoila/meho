<!--
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 evoila Group
-->

# MEHO for Claude Desktop — MCPB bundle

A one-click [MCPB](https://github.com/modelcontextprotocol/mcpb) bundle
(formerly "DXT") that connects Claude Desktop to an internal MEHO
backplane. Opening the `.mcpb` file in Claude Desktop shows an install
dialog and prompts for the two per-operator values — no hand-editing of
`claude_desktop_config.json`.

## What it packages

Claude Desktop cannot reach an internal MEHO through its cloud-brokered
remote connector: MEHO is internal-only and never publicly exposed, so
Anthropic's cloud can't fetch its RFC 9728 metadata. Desktop instead
reaches the backplane through a **local `mcp-remote` stdio→Streamable-HTTP
shim** that runs on the operator's own VPN-connected machine (the recipe
in [`docs/cross-repo/mcp-client-setup.md`](../../docs/cross-repo/mcp-client-setup.md),
proven by #2666). This bundle packages exactly that shim invocation.

Distribution is the **GitHub Release asset channel only** — there is no
public marketplace or registry. The bundle is generic (the backplane URL
is operator-supplied config), and the internal-only posture is unchanged
because the shim runs on a machine already on the VPN.

## Layout

| Path | Role |
|---|---|
| `manifest.json` | MCPB manifest (`manifest_version` 0.3). Committed with a `0.0.0` placeholder version; `build.sh` injects the real version at pack time. |
| `server/index.mjs` | The bundle's entry point. A thin stdio launcher that spawns the vendored `mcp-remote` through the bundle's own runtime, guards the internal-CA path, and presents the pre-registered `meho-mcp` OAuth client. |
| `package.json` + `package-lock.json` | Pin `mcp-remote@0.1.38` and its full, integrity-checked dependency tree. `build.sh` runs `npm ci --omit=dev` from the lock to vendor `node_modules` into the bundle. Never committed: `node_modules/`. |
| `test/index.test.mjs` | Behavioral suite (`node --test`) for the launcher's spawn contract and guards. Runs on every PR (`mcpb-bundle.yml`); not shipped in the bundle. |
| `build.sh` | Vendors dependencies, then validates + packs the bundle via the pinned `@anthropic-ai/mcpb` CLI. |

## Building locally

```bash
./build.sh 0.31.0            # writes ./dist/meho-claude-desktop-0.31.0.mcpb
./build.sh 0.31.0 /tmp/out   # or choose the output directory
```

Requires Node.js / `npm` (`build.sh` runs `npm ci --omit=dev` to vendor
`mcp-remote`, and `npx` for the pinned `@anthropic-ai/mcpb` CLI) and `jq`.
The `@anthropic-ai/mcpb` CLI validates `manifest.json` against the
MANIFEST 0.3 schema before zipping, so a malformed manifest fails the
build.

In CI the same script runs two ways (see
[`.github/workflows/mcpb-bundle.yml`](../../.github/workflows/mcpb-bundle.yml)
and [`.github/workflows/cli-release.yml`](../../.github/workflows/cli-release.yml)):

- **Pull requests touching this directory** — a dry-run pack that
  validates the manifest and uploads the bundle as a CI artifact.
- **`v*` tag push** — builds with the tag version and attaches the
  bundle to the GitHub Release alongside the CLI tarballs.

## How the launcher works

The manifest's `mcp_config.command` is `node`, running `server/index.mjs`
with the operator's backplane URL as its argument, the optional CA path
delivered as the `MEHO_CA_CERT` environment variable, the OAuth client id
delivered as `MEHO_MCP_CLIENT_ID`, and the requested OAuth scopes delivered
as `MEHO_MCP_SCOPES`. The launcher:

1. Validates that the URL is a well-formed `https` URL.
2. Exports `NODE_EXTRA_CA_CERTS` to the child **only** when a non-empty
   CA path was supplied. An unset optional file must not become the
   child's TLS trust path, and the MCPB host's substitution of an unset
   optional value is undocumented — so the guard lives in code rather
   than depending on that behavior.
3. Resolves the OAuth client id from `MEHO_MCP_CLIENT_ID`, falling back to
   `meho-mcp` when the host substitutes an empty value for the untouched
   optional `client_id` config — so an operator who never opens the
   advanced field still authenticates.
4. Resolves the requested OAuth scopes from `MEHO_MCP_SCOPES`, falling back
   to the default working surface `mcp:read mcp:execute` when the host
   substitutes an empty/whitespace value for the untouched optional
   `scopes` config. Setting it to an elevated value (e.g.
   `mcp:read mcp:execute mcp:admin`) is the deliberate opt-in to
   [operator mode](#elevated-operator-mode-opt-in).
5. Spawns the vendored `mcp-remote@0.1.38` with inherited stdio, passing
   `--static-oauth-client-info '{"client_id": "…"}'` and
   `--static-oauth-client-metadata '{"scope": "<resolved scopes>"}'`.
   The static client info is load-bearing: MEHO's Keycloak realm blocks
   anonymous RFC 7591 Dynamic Client Registration (default Trusted Hosts
   policy → `403 "Host not trusted"`), so a bare `mcp-remote` could never
   complete OAuth on a fresh install. Presenting the pre-registered public
   `meho-mcp` client skips DCR entirely. `mcp-remote` then runs its OAuth
   2.1 + PKCE flow and forwards Streamable-HTTP calls to `/mcp`.

The spawn is a direct `process.execPath` + args-array call — the same
runtime already executing `index.mjs`, with `ELECTRON_RUN_AS_NODE=1` in
the child env so it runs as plain Node whether `execPath` is a standalone
`node` or Claude Desktop's Electron helper. There is **no `npx`, no PATH
lookup, and no per-platform shell**: `mcp-remote` is vendored in the
bundle's `node_modules` (MCPB Node servers ship their dependencies), and
the launcher resolves its entry from there. This is what fixes the
field-test failure where Claude Desktop's UtilityProcess GUI PATH (no
Homebrew / nvm entries) made the old `npx` spawn die with
`spawn npx ENOENT` before OAuth (#3143).

The `meho-mcp` client (or whatever name `client_id` overrides it to) must
already exist on the realm — see
[`docs/cross-repo/mcp-client-setup.md`](../../docs/cross-repo/mcp-client-setup.md).
No system Node or `npx` on `PATH` is required at run time — the bundle
carries its dependencies and Claude Desktop supplies the runtime.

## Elevated operator mode (opt-in)

By default a session lists only the **working surface** — status,
connector/operation discovery, `call_operation` / `preview_operation` /
`result_query`, knowledge, memory, the broadcast trio, target/topology
reads, and the runbook run family. That is everything an operator needs to
get work done, and it is what every session gets with the `scopes` field
left at its default `mcp:read mcp:execute`.

The advanced `scopes` user_config is the deliberate opt-in to **operator
mode**. Set it to add the elevated scope:

```
mcp:read mcp:execute mcp:admin
```

An elevated session additionally lists the **operator planes**: the agents
/ principals registry and grants, connector lifecycle, broadcast overrides,
the scheduler, sensors, runbook template authoring, and the approvals
**read** views (`meho_approvals_list` / `meho_approvals_get`).

Elevation **never** exposes the human-only verbs. `meho_approvals_approve`,
`meho_approvals_reject`, and `meho_agents_grant_elevate` have no MCP path
under any scope — approving a parked operation and granting elevation are
human decisions made through the console or CLI only (#3155). A model
holding the approve button would collapse the four-eyes gate, so it is
never on the wire.

**Realm prerequisite (and the fallback if it is missing).** `mcp:admin`
must be offered as an **optional** (requestable, non-default) client scope
on the realm's `meho-mcp` client before elevation takes effect — tracked as
[`evoila-bosnia/claude-rdc-hetzner-dc#2734`](https://github.com/evoila-bosnia/claude-rdc-hetzner-dc/issues/2734).
Until it lands, requesting `mcp:admin` **degrades to the default working
surface**: per OAuth 2.1 (RFC 6749 §3.3) the authorization server may ignore
a scope it does not grant, and Keycloak drops any requested scope that is
not an assigned default/optional client scope — so the token comes back
without `mcp:admin`, no error is raised, and the operator planes simply do
not appear. Setting the `scopes` field on a realm that does not yet offer
the scope is therefore safe: the session behaves exactly as the default.

## Verifying a built bundle

```bash
./build.sh 0.0.0-dev /tmp/out
unzip -l /tmp/out/meho-claude-desktop-0.0.0-dev.mcpb | grep -i mcp-remote  # vendored
```

Then open the `.mcpb` in Claude Desktop on a VPN-connected machine, enter
an internal `/mcp` URL, and confirm `meho_status` is callable (the #2666
bar).

## References

- MCPB spec + CLI: <https://github.com/modelcontextprotocol/mcpb>;
  MANIFEST 0.3: <https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md>
- Background (DXT → MCPB rename): <https://www.anthropic.com/engineering/desktop-extensions>
- Shim recipe this packages: [`docs/cross-repo/mcp-client-setup.md`](../../docs/cross-repo/mcp-client-setup.md) Step 3
- Release wiring: [`docs/RELEASING.md`](../../docs/RELEASING.md)
