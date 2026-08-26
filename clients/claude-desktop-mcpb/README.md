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
| `server/index.mjs` | The bundle's entry point. A transparent stdio launcher for `npx -y mcp-remote <url>` that guards the internal-CA path. |
| `build.sh` | Validates + packs the bundle via the pinned `@anthropic-ai/mcpb` CLI. |

## Building locally

```bash
./build.sh 0.31.0            # writes ./dist/meho-claude-desktop-0.31.0.mcpb
./build.sh 0.31.0 /tmp/out   # or choose the output directory
```

Requires Node.js (for `npx`) and `jq`. The pinned `@anthropic-ai/mcpb`
CLI validates `manifest.json` against the MANIFEST 0.3 schema before
zipping, so a malformed manifest fails the build.

In CI the same script runs two ways (see
[`.github/workflows/mcpb-bundle.yml`](../../.github/workflows/mcpb-bundle.yml)
and [`.github/workflows/cli-release.yml`](../../.github/workflows/cli-release.yml)):

- **Pull requests touching this directory** — a dry-run pack that
  validates the manifest and uploads the bundle as a CI artifact.
- **`v*` tag push** — builds with the tag version and attaches the
  bundle to the GitHub Release alongside the CLI tarballs.

## How the launcher works

The manifest's `mcp_config.command` is `node`, running `server/index.mjs`
with the operator's backplane URL as its argument and the optional CA
path delivered as the `MEHO_CA_CERT` environment variable. The launcher:

1. Validates that the URL is a well-formed `https` URL.
2. Exports `NODE_EXTRA_CA_CERTS` to the child **only** when a non-empty
   CA path was supplied. An unset optional file must not become the
   child's TLS trust path, and the MCPB host's substitution of an unset
   optional value is undocumented — so the guard lives in code rather
   than depending on that behavior.
3. Spawns `npx -y mcp-remote <url>` with inherited stdio, so `mcp-remote`
   runs its OAuth 2.1 + PKCE flow and forwards Streamable-HTTP calls to
   `/mcp` exactly as it does when spawned directly.

The operator's machine therefore needs system Node.js / `npx` on `PATH`
(the same prerequisite the raw shim recipe has).

## Verifying a built bundle

```bash
./build.sh 0.0.0-dev /tmp/out
unzip -l /tmp/out/meho-claude-desktop-0.0.0-dev.mcpb   # manifest.json + server/index.mjs
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
