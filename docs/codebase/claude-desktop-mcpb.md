# Claude Desktop `.mcpb` bundle (`clients/claude-desktop-mcpb/`)

## Overview

The "MEHO for Claude Desktop" bundle packages the proven Desktop onramp —
a local `mcp-remote` stdio→Streamable-HTTP shim running on the operator's
VPN-connected machine — as a one-click MCPB extension. It is built by
`build.sh` (manifest validation + pack via the pinned `@anthropic-ai/mcpb`
CLI) and attached to each GitHub Release by `cli-release.yml`; PRs touching
the directory get a dry-run pack and the launcher test suite from
`mcpb-bundle.yml`. Operator-facing behaviour (install dialog, OAuth first
connect, elevated scopes, trust routes) is documented in the bundle's
[`README.md`](../../clients/claude-desktop-mcpb/README.md); this page
records the launcher's contract and invariants.

## Key files

| File | Role |
|---|---|
| `manifest.json` | MCPB 0.3 manifest. `mcp_config` runs `node server/index.mjs <backplane_url> <ca_cert>`; env carries `MEHO_MCP_CLIENT_ID`, `MEHO_MCP_SCOPES`, and `NODE_EXTRA_CA_CERTS` (older-Node CA fallback). |
| `server/index.mjs` | The launcher. Validates argv, injects the internal CA, rewrites `process.argv`, imports the vendored `mcp-remote` entry in-process. |
| `package.json` / `package-lock.json` | Pin `mcp-remote@0.1.38` and its tree; `build.sh` vendors it with `npm ci --omit=dev`. |
| `test/index.test.mjs` | `node --test` suite: runs the launcher against a stub `mcp-remote` that reports the argv, env, pid/ppid, and default-CA fingerprints it sees at import time. |

## Control flow

`server/index.mjs`, top to bottom, all before any network I/O:

1. **argv[2] — endpoint.** Must parse as a URL with `https:`; otherwise
   exit 1 before importing anything.
2. **argv[3] — internal CA (optional).** Empty, whitespace, or an
   unsubstituted `${user_config.ca_cert}` placeholder means "no CA" (the
   MCPB reference host keeps an args item it has no value for as the
   literal placeholder). With a path, on a Node that has
   `tls.setDefaultCACertificates` (22.19+ / 24.5+), the launcher extracts
   the file's PEM certificate blocks and calls
   `tls.setDefaultCACertificates([...tls.getCACertificates("default"), ...certs])`.
   An unreadable path, no PEM block, or an unparsable block exits 1 with the
   path in the message. On an older Node it cannot inject; it relies on
   `NODE_EXTRA_CA_CERTS` having been read at boot and warns when that
   variable is absent.
3. **Env seams.** `MEHO_MCP_CLIENT_ID` (default `meho-mcp`) and
   `MEHO_MCP_SCOPES` (default `mcp:read mcp:execute`) are read, defaulted
   when empty, and deleted from `process.env`.
4. **mcp-remote entry.** Resolved from the bundle's `node_modules` via the
   package's `bin` mapping.
5. **Hand-off.** `process.argv` becomes
   `[node, entry, url, --static-oauth-client-info, {"client_id"}, --static-oauth-client-metadata, {"scope"}]`
   and the entry is dynamically imported; `mcp-remote` parses argv at
   module top level and owns stdio from then on.

## Invariants

- **In-process, never spawned (#3341, F6).** The launcher imports no
  `child_process`; `process.execPath` under Desktop is an Electron helper
  that cannot run as Node. The suite checks both the source and the stub's
  ppid.
- **CA injected before `mcp-remote` loads (#3855, F7).** Connections opened
  before the call keep the old list, so the injection precedes the import.
  It appends rather than replaces: the bundled roots and, when the host
  enables Node's system store (`NODE_USE_SYSTEM_CA=1`, as current Desktop
  does), the OS-store roots stay trusted.
- **CA as an argument, not only `NODE_EXTRA_CA_CERTS`.** Claude Desktop
  1.3109.0 strips `NODE_EXTRA_CA_CERTS` from the extension environment, and
  Node reads that variable only at startup. The manifest env entry remains
  solely as the older-Node fallback.
- **argv[3] is load-bearing.** The manifest's third `args` item must stay
  `${user_config.ca_cert}`; the suite pins the manifest `args` array.

## Dependencies

- Node `tls.setDefaultCACertificates()` (added v24.5.0 / v22.19.0) and
  `tls.getCACertificates()` (added v23.10.0 / v22.15.0). `mcp-remote`
  0.1.38 fetches through the npm `undici` package, whose connector calls
  `tls.connect()` without a `ca` option, so it uses the default CA list the
  launcher set.
- `mcp-remote@0.1.38` (vendored).
- The MCPB host's `user_config` substitution (`${user_config.KEY}` in
  `args` / `env`).

## Known issues

- Desktop's version probe starts the launcher twice, and both instances run
  OAuth (#3857).
- The field tests so far (#3143) ran on macOS; how Desktop on Windows
  handles either CA route is unverified.

## References

- Bundle README: [`clients/claude-desktop-mcpb/README.md`](../../clients/claude-desktop-mcpb/README.md)
- Client setup recipe: [`docs/cross-repo/mcp-client-setup.md`](../cross-repo/mcp-client-setup.md) § Claude Desktop
- Published page: `docs-site/clients/claude-desktop.md`
- Node TLS API: <https://nodejs.org/api/tls.html#tlssetdefaultcacertificatescerts>
- MCPB manifest spec: <https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md>
- Field test and findings: #3143 (F6 → #3341 / #3385, F7 → #3855)
