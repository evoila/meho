# Claude Desktop `.mcpb` bundle (`clients/claude-desktop-mcpb/`)

## Overview

The "MEHO for Claude Desktop" bundle packages the proven Desktop onramp —
a local `mcp-remote` stdio→Streamable-HTTP shim running on the operator's
VPN-connected machine — as a one-click MCPB extension. It is built by
`build.sh` (manifest validation + pack via the pinned `@anthropic-ai/mcpb`
CLI) and attached to each GitHub Release by `cli-release.yml`, together
with its own checksum file `SHA256SUMS-mcpb` (two-space `sha256sum` format,
cosign-signed like the CLI `SHA256SUMS`); PRs touching
the directory get a dry-run pack and the launcher test suite from
`mcpb-bundle.yml`. Operator-facing behaviour (install dialog, OAuth first
connect, elevated scopes, trust routes) is documented in the bundle's
[`README.md`](../../clients/claude-desktop-mcpb/README.md); this page
records the launcher's contract and invariants.

## Key files

| File | Role |
|---|---|
| `manifest.json` | MCPB 0.3 manifest. `mcp_config` runs `node server/index.mjs <backplane_url> <ca_cert>`; env carries `MEHO_MCP_CLIENT_ID`, `MEHO_MCP_SCOPES`, `MEHO_TENANT` (the optional `tenant` setting, #5242), and `NODE_EXTRA_CA_CERTS` (older-Node CA fallback). |
| `server/index.mjs` | The launcher. Validates argv, injects the internal CA, rewrites `process.argv`, imports the login guard, which takes the sign-in lock and imports the vendored `mcp-remote` entry in-process. |
| `../claude-code-plugin/lib/login-guard.mjs` | The login guard (#4003), shared with the Claude Code plugin. Not in this folder: `build.sh` copies every `lib/*.mjs` file into `server/` in the staged bundle. |
| `../claude-code-plugin/lib/signin-lock.mjs` | One sign-in at a time (#3857, #5514). A per-endpoint lock file, so two copies (of the bundle or of the plugin) do not both run the browser login. The guard uses the same code, with another file name, for the renewal lock. |
| `../claude-code-plugin/lib/token-renewal.mjs` | The renewal rules (#5514): the shared login file is never deleted, one renewal at a time, a network or server error is tried again, a refused renewal first looks for a newer saved login. |
| `package.json` / `package-lock.json` | Pin `mcp-remote@0.1.39` and its tree; `build.sh` vendors it with `npm ci --omit=dev`. |
| `test/index.test.mjs` | `node --test` suite: runs the launcher against a stub `mcp-remote` that reports the argv, env, pid/ppid, and default-CA fingerprints it sees at import time. |
| `test/login-guard.test.mjs` | The guard inside the bundle: stub tests, and end-to-end tests with the real pinned `mcp-remote` against a fake MEHO + Keycloak on `127.0.0.1` over https (throwaway `openssl` certificate passed as the CA argument). CI sets `MEHO_MCPB_E2E=1`, so a missing `node_modules` fails instead of skipping. |

## Control flow

`server/index.mjs`, top to bottom, all before any network I/O:

1. **argv[2] — endpoint.** Must parse as a URL with `https:`; otherwise
   exit 1 before importing anything.
2. **argv[3] — internal CA (optional).** Empty, whitespace, or an
   unsubstituted `${user_config.ca_cert}` placeholder means "no CA" (the
   MCPB reference host keeps an args item it has no value for as the
   literal placeholder). A bare `~` or a leading `~/` (`~\` on Windows)
   is expanded to `os.homedir()`, because Desktop's file field also takes
   a typed path and stores it as typed; other paths are used as given.
   With a path, on a Node that has
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
   `MEHO_TENANT` (#5242) is read, trimmed, and deleted from `process.env`.
   Empty, whitespace, or an unsubstituted `${user_config.tenant}`
   placeholder means no tenant. A tenant adds
   `--header "Meho-Tenant: <tenant>"` to the `mcp-remote` argv, the header
   the backplane reads to pick the current tenant
   (`auth/current_tenant.py`, `TENANT_HEADER`). `mcp-remote` puts custom
   headers into the key of its saved login, so each tenant value has its
   own saved login.
4. **mcp-remote entry.** Resolved from the bundle's `node_modules` via the
   package's `bin` mapping.
5. **Hand-off.** `process.argv` becomes
   `[node, server/login-guard.mjs, entry, url, --static-oauth-client-info, {"client_id"}, --static-oauth-client-metadata, {"scope"}]`,
   plus `--header, "Meho-Tenant: <tenant>"` when a tenant is set,
   the launcher sets `MEHO_LOGIN_GUARD_PREFIX` / `MEHO_LOGIN_GUARD_RECONNECT`
   (the bundle's log prefix and the "turn the extension off and on" hint),
   and imports the guard. The guard reads and deletes those two variables,
   sets `process.argv` to `[node, entry, url, ...]`, and imports the entry;
   `mcp-remote` parses argv at module top level and owns stdio from then
   on.
6. **Login guard (#4003, #5514).** The guard starts its connect timer,
   then takes the sign-in lock (below), then sets up the renewal rules,
   then imports `mcp-remote`. The timer covers the wait for the lock: a
   copy that waits for another copy's login stops at the same limit. If
   `mcp-remote` writes nothing to stdout within `MEHO_MCP_LOGIN_TIMEOUT`
   seconds (unset in the bundle, so 300), the guard exits 1: the exit frees
   the OAuth callback port, and the `exit` listeners remove `mcp-remote`'s
   lockfile and the sign-in lock. The first stdout write (`mcp-remote`
   answering the host, which only happens after it is connected) releases
   the sign-in lock. When the login has ended during a session, the call
   fails with the "login has expired" message and the guard exits 1.
7. **Sign-in lock (#3857).** `acquireSignInLock(url)` waits until it can
   create `<config dir>/meho-signin-<sha256(url)[:16]>.lock` with the
   exclusive `wx` flag. The config dir is `MCP_REMOTE_CONFIG_DIR` or
   `~/.mcp-auth`, the same base folder `mcp-remote` uses. The URL is
   normalised with `new URL(...).href`, so the Claude Code plugin and the
   bundle use the same lock for the same MEHO server. The file holds the
   owner's pid. A lock is stale when its pid is not running, when it is
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
   so the removal never hits a name a running copy is using. The release
   uses the same second-name step as the takeover: it gives the file a
   second name, goes on only when the file has exactly two names, and
   deletes the lock path only when the file still holds its own pid. While
   another copy's second name exists, that copy may be about to write its
   pid; the release then leaves the lock and tries again on the next call.
   So a release never deletes a lock another copy has just taken over.
   After waiting five minutes, or when the lock file cannot be used, the
   guard starts without the lock.
8. **Renewal rules (#5514).** All copies on one computer share one saved
   login file (`<config dir>/mcp-remote-<v>/<hash>_tokens.json`).
   `token-renewal.mjs` changes `mcp-remote` from the inside, without
   editing its files:
   - It wraps `fs.promises.unlink`, so `mcp-remote` never deletes the
     login file (it did that on every `invalid_grant`), and
     `fs.promises.writeFile`, so the file is written all at once (a new
     file, then a rename).
   - It wraps the global `fetch` for the refresh-token request only. One
     renewal runs at a time per MEHO server (`meho-renew-<hash>.lock`,
     same lock code). Under the lock it reads the login file again,
     renews with the newest refresh token in it, and saves the result.
     `mcp-remote` saves the same result once more afterwards, outside the
     lock. The guard drops that save (it knows the access token it handed
     out), so a late save never writes over a newer login that another
     copy saved in between.
   - A network error or HTTP 5xx is tried again after 1, 2, 4 and 8
     seconds (about 30 seconds in total). A connection that drops while
     the answer is read counts as a network error. If it still fails,
     `mcp-remote` gets a `temporarily_unavailable` answer. That is an
     OAuth error that is not a server error, so `mcp-remote` fails the
     call and opens no browser. The login is kept.
   - On `invalid_grant` it reads the file again. A newer refresh token
     (another copy renewed, or finished a browser login) is tried next.
     The same token is tried once more after a short wait. Only when the
     token read again is refused has the login ended.
   - An ended login before connect: the guard wraps
     `fs.promises.readFile` so `mcp-remote` sees no login file, and runs
     its browser login (under the sign-in lock). An ended login after
     connect: the guard waits while another copy holds the sign-in lock,
     looks again, and otherwise answers `invalid_grant` with its own
     "login has ended" text. `mcp-remote` then fails the call without a
     browser (the login file still has a refresh token), and the guard
     rewrites the error and exits, as for `Unauthorized`.

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
- **One copy signs in at a time (#3857).** `mcp-remote` 0.1.39 has its own
  lockfile, but it checks for it and creates it in two steps, so copies
  that start together both miss it; on Windows it skips the check. The
  guard's sign-in lock closes that gap, for the bundle and the plugin. It relies on `mcp-remote` writing nothing
  to stdout before it is connected (its `log` goes to stderr, and the stdio
  transport starts only after the remote connection). Newer upstream
  `mcp-remote` (0.15.x checked) coordinates concurrent starts itself; if
  the pin moves there, re-check whether this lock is still needed.
- **One guard.** The bundle does not keep its own copy of the guard or its
  helper files; `build.sh` copies the plugin's `lib/*.mjs`, and
  `mcpb-bundle.yml` also runs on changes there. The guard's own
  contract (argv shape, rules) is documented in its header.
- **One tenant per session (#5242).** The tenant is fixed when the launcher
  starts; there is no MCP tool to change it. No setting sends no header, so
  a login with one tenant behaves as before. The Claude Code plugin shim
  (`bin/meho-mcp-remote`, `MEHO_TENANT`) sends the same header.
- **argv[3] is load-bearing.** The manifest's third `args` item must stay
  `${user_config.ca_cert}`; the suite pins the manifest `args` array.

## Dependencies

- Node `tls.setDefaultCACertificates()` (added v24.5.0 / v22.19.0) and
  `tls.getCACertificates()` (added v23.10.0 / v22.15.0). `mcp-remote`
  0.1.39 makes its requests through two HTTP clients. Its own OAuth
  discovery probe and its SSE-fallback transport use the vendored npm
  `undici`, whose connector calls `tls.connect()` without a `ca` option.
  The bundled MCP SDK's Streamable HTTP transport and OAuth helpers use the
  global `fetch` (Node's built-in undici). Both open TLS through `node:tls`
  with no explicit `ca`, so both use the default CA list the launcher set.
- `mcp-remote@0.1.39` (vendored). It keeps saved logins in
  `mcp-remote-0.1.38/` under the config dir (its built-in version string
  lags the npm version); 0.1.38 used `mcp-remote-0.1.37/`. So the update
  costs each machine one new browser login.
- The MCPB host's `user_config` substitution (`${user_config.KEY}` in
  `args` / `env`).

## Known issues

- If Desktop stops the copy that holds the sign-in lock before the login
  finishes, the waiting copy takes over and opens a second browser tab.
- The takeover of a stale lock needs hard links. On a file system without
  them the guard logs that it cannot use the lock and starts without it.
- The renewal rules depend on `mcp-remote` 0.1.39 internals: the token
  request goes through the global `fetch` with a `URLSearchParams` body,
  and the login file is read, written and deleted through `fs.promises`.
  The end-to-end tests in `clients/claude-code-plugin/test/` pin this; a
  pin bump that changes it turns them red.
- A login file without a refresh token (not issued by Keycloak for this
  client) cannot be renewed; a connected session then still reaches
  `mcp-remote`'s own browser login, which cannot finish without its
  callback server.
- The field tests so far (#3143) ran on macOS; how Desktop on Windows
  handles either CA route is unverified.

## References

- Bundle README: [`clients/claude-desktop-mcpb/README.md`](../../clients/claude-desktop-mcpb/README.md)
- Client setup recipe: [`docs/cross-repo/mcp-client-setup.md`](../cross-repo/mcp-client-setup.md) § Claude Desktop
- Published page: `docs-site/clients/claude-desktop.md`
- Node TLS API: <https://nodejs.org/api/tls.html#tlssetdefaultcacertificatescerts>
- MCPB manifest spec: <https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md>
- Field test and findings: #3143 (F6 → #3341 / #3385, F7 → #3855)
