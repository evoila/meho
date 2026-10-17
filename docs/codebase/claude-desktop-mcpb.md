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
| `server/index.mjs` | The launcher. Validates argv, injects the internal CA, takes the sign-in lock, rewrites `process.argv`, imports the vendored `mcp-remote` entry in-process. |
| `server/signin-lock.mjs` | One sign-in at a time (#3857). A per-endpoint lock file, so two copies started together do not both run the browser login. |
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
   `MEHO_MCP_SCOPES` (default `mcp:read mcp:execute offline_access`) are
   read, defaulted when empty, and deleted from `process.env`.
   `offline_access` gets an offline refresh token, so the login outlasts the
   realm's SSO idle limit (30 minutes by default).
4. **mcp-remote entry.** Resolved from the bundle's `node_modules` via the
   package's `bin` mapping.
5. **Sign-in lock (#3857).** `acquireSignInLock(url.href)` waits until it
   can create `<config dir>/meho-claude-desktop-<sha256(url)[:16]>.lock`
   with the exclusive `wx` flag. The config dir is `MCP_REMOTE_CONFIG_DIR`
   or `~/.mcp-auth`, the same base folder `mcp-remote` uses. The file holds
   the owner's pid. A lock is stale when its pid is not running, when it is
   older than five minutes, or when it has no readable pid after five
   seconds; the next copy takes it over in place. It never deletes the stale
   file, because two copies can see the same stale lock, and the second
   delete would hit the lock the first copy had just made. Instead it gives
   the file a second name of its own with a hard link
   (`<lock>.takeover.<pid>.<random>`), checks that the file has exactly two
   names (so no other copy is taking it over at the same time), checks that
   the file still holds the stale content it read, writes its own pid into
   it, and checks that the lock path shows that pid. Other copies then read
   a live lock and wait. The second name is removed afterwards. A copy
   removes a second name left behind by another copy only when the pid in
   the name is not running; no other process can have made that exact name,
   so the removal never hits a name a running copy is using. The launcher
   wraps
   `process.stdout.write` once: the first write (`mcp-remote` answering the
   host, which only happens after it is connected) releases the lock. An
   `exit` listener also releases it. The release uses the same second-name
   step as the takeover: it gives the file a second name, goes on only when
   the file has exactly two names, and deletes the lock path only when the
   file still holds its own pid. While another copy's second name exists,
   that copy may be about to write its pid; the release then leaves the
   lock and tries again on the next call. So a release never deletes a lock
   another copy has just taken over. After waiting five minutes, or when
   the lock file cannot be used, the launcher starts without the lock.
6. **Hand-off.** `process.argv` becomes
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
- **One copy signs in at a time (#3857).** `mcp-remote` 0.1.38 has its own
  lockfile, but it checks for it and creates it in two steps, so copies
  that start together both miss it; on Windows it skips the check. The
  launcher lock closes that gap. It relies on `mcp-remote` writing nothing
  to stdout before it is connected (its `log` goes to stderr, and the stdio
  transport starts only after the remote connection). Newer upstream
  `mcp-remote` (0.15.x checked) coordinates concurrent starts itself; if
  the pin moves there, re-check whether this lock is still needed.
- **argv[3] is load-bearing.** The manifest's third `args` item must stay
  `${user_config.ca_cert}`; the suite pins the manifest `args` array.

## Dependencies

- Node `tls.setDefaultCACertificates()` (added v24.5.0 / v22.19.0) and
  `tls.getCACertificates()` (added v23.10.0 / v22.15.0). `mcp-remote`
  0.1.38 makes its requests through two HTTP clients. Its own OAuth
  discovery probe and its SSE-fallback transport use the vendored npm
  `undici`, whose connector calls `tls.connect()` without a `ca` option.
  The bundled MCP SDK's Streamable HTTP transport and OAuth helpers use the
  global `fetch` (Node's built-in undici). Both open TLS through `node:tls`
  with no explicit `ca`, so both use the default CA list the launcher set.
- `mcp-remote@0.1.38` (vendored).
- The MCPB host's `user_config` substitution (`${user_config.KEY}` in
  `args` / `env`).

## Known issues

- If Desktop stops the copy that holds the sign-in lock before the login
  finishes, the waiting copy takes over and opens a second browser tab.
- The takeover of a stale lock needs hard links. On a file system without
  them the launcher logs that it cannot use the lock and starts without it.
- The field tests so far (#3143) ran on macOS; how Desktop on Windows
  handles either CA route is unverified.

## References

- Bundle README: [`clients/claude-desktop-mcpb/README.md`](../../clients/claude-desktop-mcpb/README.md)
- Client setup recipe: [`docs/cross-repo/mcp-client-setup.md`](../cross-repo/mcp-client-setup.md) § Claude Desktop
- Published page: `docs-site/clients/claude-desktop.md`
- Node TLS API: <https://nodejs.org/api/tls.html#tlssetdefaultcacertificatescerts>
- MCPB manifest spec: <https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md>
- Field test and findings: #3143 (F6 → #3341 / #3385, F7 → #3855)
