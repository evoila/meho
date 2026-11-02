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
| `server/index.mjs` | The bundle's entry point. A thin stdio launcher that runs the vendored `mcp-remote` in-process (imports its CLI entry — spawns nothing), and presents the pre-registered `meho-mcp` OAuth client. |
| `../claude-code-plugin/lib/*.mjs` | The login guard and its helper files (the sign-in lock, the renewal rules), shared with the Claude Code plugin. `build.sh` copies them into `server/` in the bundle. See [When the login expires or is not finished](#when-the-login-expires-or-is-not-finished). |
| `package.json` + `package-lock.json` | Pin `mcp-remote@0.1.39` and its full, integrity-checked dependency tree. `build.sh` runs `npm ci --omit=dev` from the lock to vendor `node_modules` into the bundle. Never committed: `node_modules/`. |
| `test/index.test.mjs` | Behavioral suite (`node --test`) for the launcher's in-process invocation contract, the in-process CA injection, and the guards. Runs on every PR (`mcpb-bundle.yml`); not shipped in the bundle. |
| `test/login-guard.test.mjs` | Tests for the login guard in the bundle: stub tests, plus end-to-end tests with the real pinned `mcp-remote` against a fake MEHO and Keycloak on `127.0.0.1`. Run on every PR (`mcpb-bundle.yml`); not shipped in the bundle. |
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
with the operator's backplane URL and the optional internal-CA path as its
arguments, the OAuth client id delivered as `MEHO_MCP_CLIENT_ID`, and the
requested OAuth scopes delivered as `MEHO_MCP_SCOPES`, and the optional
tenant delivered as `MEHO_TENANT`. The launcher:

1. Validates that the URL is a well-formed `https` URL.
2. When an internal-CA path was given, appends its certificates to Node's
   default CA list in-process — see [Internal-CA trust](#internal-ca-trust).
3. Resolves the OAuth client id from `MEHO_MCP_CLIENT_ID`, falling back to
   `meho-mcp` when the host substitutes an empty value for the untouched
   optional `client_id` config — so an operator who never opens the
   advanced field still authenticates. The seam variable is scrubbed from
   the environment `mcp-remote` inherits.
4. Resolves the requested OAuth scopes from `MEHO_MCP_SCOPES`, falling back
   to the default `mcp:read mcp:execute offline_access` (the working
   surface plus a login that lasts — see
   [How long the login lasts](#how-long-the-login-lasts)) when the host
   substitutes an empty/whitespace value for the untouched optional
   `scopes` config. Setting it to an elevated value (e.g.
   `mcp:read mcp:execute offline_access mcp:admin`) is the deliberate opt-in to
   [operator mode](#elevated-operator-mode-opt-in). The seam variable is
   scrubbed from the environment `mcp-remote` inherits.
5. Reads the tenant from `MEHO_TENANT` (the optional `tenant` config). Empty,
   whitespace, or an unsubstituted `${user_config.tenant}` placeholder means
   no tenant — see [Work in one tenant](#work-in-one-tenant). The seam
   variable is scrubbed from the environment `mcp-remote` inherits.
6. Sets `process.argv` to the `mcp-remote` invocation — the backplane URL,
   plus `--static-oauth-client-info '{"client_id": "…"}'`,
   `--static-oauth-client-metadata '{"scope": "<resolved scopes>"}'`, and
   `--header 'Meho-Tenant: <tenant>'` when a tenant is set — and
   **imports** the login guard. The guard waits until no other copy is
   signing in and takes a small lock file (see
   [One sign-in at a time](#one-sign-in-at-a-time)), then imports the vendored
   `mcp-remote@0.1.39` CLI entry into this process (see
   [When the login expires or is not finished](#when-the-login-expires-or-is-not-finished)).
   `mcp-remote` reads `process.argv` at import and takes over the
   launcher's stdio; nothing is spawned. The static client info is
   load-bearing: MEHO's Keycloak realm blocks anonymous RFC 7591 Dynamic
   Client Registration (default Trusted Hosts policy → `403 "Host not
   trusted"`), so a bare `mcp-remote` could never complete OAuth on a fresh
   install. Presenting the pre-registered public `meho-mcp` client skips DCR
   entirely. `mcp-remote` then runs its OAuth 2.1 + PKCE flow and forwards
   Streamable-HTTP calls to `/mcp`.

`mcp-remote` runs **in-process** — the launcher imports its CLI entry
rather than spawning it — so it runs under the exact Node runtime Claude
Desktop already started for the launcher. This is what fixes field-test
finding **F6** (#3341): #3144 spawned `mcp-remote` by launching the
bundle's own runtime (`process.execPath`) with `ELECTRON_RUN_AS_NODE=1`, on
the assumption that flag makes the child run as plain Node whether
`execPath` is a standalone `node` or Claude Desktop's Electron helper. On
Claude Desktop 1.40609.0 that is false: `execPath` is the `Claude Helper
(Plugin)` Electron binary, which FATALs when spawned as Node even with
`ELECTRON_RUN_AS_NODE=1` (`Unable to find helper app`), so the child never
started and Desktop cancelled `initialize` after 60 s. Importing the entry
sidesteps `execPath` entirely — there is no child, no `npx`, no PATH
lookup, and no per-platform shell. (The earlier `npx`-based launcher died
with `spawn npx ENOENT` under Claude Desktop's UtilityProcess GUI PATH,
fixed by #3144; F6 was the deeper failure that vendoring alone left.)

The `meho-mcp` client (or whatever name `client_id` overrides it to) must
already exist on the realm — see
[`docs/cross-repo/mcp-client-setup.md`](../../docs/cross-repo/mcp-client-setup.md).
No system Node or `npx` on `PATH` is required at run time — the bundle
carries its dependencies and Claude Desktop supplies the runtime.

## Internal-CA trust

On an internal-CA deploy the shim must trust the CA that signs the
backplane's and Keycloak's TLS certificates. There are two routes; either
one is enough, and a public-CA deploy needs neither.

1. **The "Internal CA bundle" field.** Pick the CA's PEM file in the
   install dialog with the file picker. A typed path must be absolute or
   start with `~/`; the launcher expands a leading `~` to the home folder.
   Claude Desktop passes the path to the launcher as an
   argument; the launcher reads the file and appends its certificates to
   Node's default CA list with
   [`tls.setDefaultCACertificates()`](https://nodejs.org/api/tls.html#tlssetdefaultcacertificatescerts)
   before it imports `mcp-remote`, so every TLS connection `mcp-remote`
   opens (OAuth discovery, token exchange, `/mcp`) trusts the CA. The
   existing list — Node's bundled roots, plus the OS-store roots when the
   OS route below is also in effect — is kept. A path that cannot be read,
   or a file without a PEM certificate, stops the launcher with an error
   naming the path instead of a TLS failure later.
2. **The OS trust store.** Leave the field empty and trust the root CA in
   the OS trust store. On macOS that is the login keychain:

   ```bash
   security add-trusted-cert -k ~/Library/Keychains/login.keychain-db internal-ca.pem
   ```

   then toggle the extension off and on. No bundle configuration is
   involved: current Claude Desktop runs its built-in Node with
   `NODE_USE_SYSTEM_CA=1`, which makes Node read the OS store. Verified on
   Claude Desktop 1.3109.0 on macOS (#3143); Windows is not yet
   field-tested.

**Why an argument, not `NODE_EXTRA_CA_CERTS`.** Claude Desktop 1.3109.0
strips `NODE_EXTRA_CA_CERTS` from the extension's environment while
delivering the rest of the manifest `env` block (#3143 F7), so a CA sent
that way never reached Node: the shim exited with
`UNABLE_TO_VERIFY_LEAF_SIGNATURE` and Desktop reported an `initialize`
timeout. The launcher cannot set the variable itself either — Node reads it
once at startup. The manifest still delivers it as the fallback for a Node
older than 22.19 / 24.5, which has no `tls.setDefaultCACertificates()`: a
host that passes the variable through gets the CA loaded at boot, and if it
does not, the launcher prints a warning naming the OS-trust-store route and
starts anyway.

An untouched optional field reaches the launcher as an empty value or as
the unsubstituted `${user_config.ca_cert}` placeholder; both mean "no CA".

## First connect — the OAuth login race

The **first** time a fresh machine enables this bundle, the vendored
`mcp-remote` runs the full Keycloak OAuth 2.1 + PKCE login before the server
reports ready. It opens a browser tab for that login **behind the Claude
Desktop window**, and Claude Desktop enforces its **own MCP startup timeout** on
the `.mcpb` server — if the browser login has not finished before that elapses,
Desktop marks the extension disconnected and tears the shim (and its OAuth
callback server) down mid-handshake.

Unlike the Claude Code CLI, Desktop's startup timeout is **not** operator-
settable — the CLI's `MCP_TIMEOUT` env var
([Claude Code MCP docs](https://code.claude.com/docs/en/mcp.md)) does not apply
to Desktop — so the recipe is *finish the login promptly, then retry if it
lapsed*:

1. Right after enabling the extension, switch to the browser tab that opened
   **behind** the Claude Desktop window and complete the Keycloak login without
   delay.
2. If the first attempt timed out (the extension shows disconnected), toggle it
   off and back on — or fully quit and relaunch Claude Desktop — to restart the
   handshake, then complete the login.

`mcp-remote` caches the tokens under `~/.mcp-auth`, so once the first login
succeeds **every later launch connects instantly** — the race only bites the
very first connect on a fresh machine (or again after you clear `~/.mcp-auth`).

## One sign-in at a time

Claude Desktop can start the extension **twice** at almost the same moment
(it checks which MCP protocol version the server speaks). Before this fix,
both copies ran the browser login: two browser tabs opened, and one copy
failed with `InvalidGrantError: Code not valid` (#3857).

Now only one copy signs in:

- The first copy creates a lock file next to the saved logins:
  `~/.mcp-auth/meho-signin-<id>.lock`. It removes the file as soon
  as it is connected to MEHO, and when it stops.
- The second copy waits while that file exists. Then it starts and uses the
  saved login. It opens no browser tab.
- A lock file whose copy has stopped, or that is older than five minutes, is
  taken over by the next copy. The copy does not delete the old file. It
  writes its own process id into it, after a check that only one copy can
  pass. So two copies that find the same old lock never both sign in.
- A copy never waits more than five minutes. After that, or when it cannot
  use the lock file, it starts anyway.

**Expected log lines.** In the extension's log, the waiting copy writes
these lines. They are normal, not an error:

```
meho-claude-desktop: another MEHO instance (pid 12345) is signing in; waiting for it to finish
meho-claude-desktop: the other MEHO instance finished; starting
```

This line is also normal. It means an earlier copy stopped without cleaning
up, and this copy took over:

```
meho-claude-desktop: found an old sign-in lock: its instance (pid 12345) stopped; taking it over
```

If Claude Desktop stops the first copy before you finish the login, the
waiting copy takes over and opens a new browser tab. Finish the login in the
new tab.

## How long the login lasts

The bundle asks Keycloak for `offline_access` by default. Keycloak then
issues an offline refresh token, and the login lasts until you have not
used MEHO for the `meho-mcp` client's offline idle limit. The realm docs
recommend 48 hours
([Keycloak realm setup](../../docs-site/install/keycloak-realm.md)).

Without `offline_access`, the login ends at the realm's SSO idle limit,
which is 30 minutes by default. The next MEHO call then needs a new browser
login. So if you change the `scopes` field, keep `offline_access` in it.

## When the login expires or is not finished

The launcher runs `mcp-remote` through a small login guard. It is the same
file the Claude Code plugin uses (`clients/claude-code-plugin/lib/login-guard.mjs`).
The guard makes sure a MEHO call never hangs because of a login:

- **The login expired during a session.** This happens after the offline
  idle limit (48 hours with no use, see
  [How long the login lasts](#how-long-the-login-lasts)) or when an admin
  ended the session in Keycloak. The next MEHO call fails at once with:
  "Your MEHO login has expired. Turn the MEHO extension off and on again in
  Claude Desktop (Settings > Extensions) to log in again." Then the
  extension stops. Do what the message says: turning it on again opens the
  browser login.
- **A renewal fails, but the login has not ended.** The extension keeps the
  login. A network error or a server error is tried again for about 30
  seconds; if the login server still does not answer, the call fails with
  "The MEHO login server did not answer" and the extension keeps running.
  When the login server refuses a renewal, the extension first uses a newer
  login that another copy (or a Claude Code session) saved, and otherwise
  asks the login server once more. A failed renewal never deletes the saved
  login.
- **Nobody finishes a browser login.** The extension stops after 5 minutes
  without a connection. This frees the local login port. The extension's
  log then has a line like this:

  ```
  meho-claude-desktop: MEHO did not connect within 5 minutes, so this attempt stopped. Finish any open MEHO login in your browser, then turn the MEHO extension off and on again in Claude Desktop (Settings > Extensions).
  ```

- **Claude Desktop starts the extension twice.** The second copy waits for
  the first one, as described in [One sign-in at a time](#one-sign-in-at-a-time).
  Its 5 minutes include the wait. It does not crash.

Older bundles used `mcp-remote@0.1.38`. A call whose login had expired got
no answer, and an unfinished login waited forever.

**One new browser login after the update.** `mcp-remote@0.1.39` keeps saved
logins in `~/.mcp-auth/mcp-remote-0.1.38/` (it names the folder after its
previous version). Older bundles used `~/.mcp-auth/mcp-remote-0.1.37/`. So
after you install this bundle, the first use opens one browser login. If no
other tool on your machine still uses the old folder, you can delete it.

## Work in one tenant

One login can belong to several tenants. MEHO works in one tenant per
request, the one the `Meho-Tenant` header names (a tenant name or ID). The
optional **Tenant** field (`tenant` user_config) is sent as that header on
every request, so the extension works in one tenant until you change it.

- One tenant: leave the field empty. Nothing changes.
- Several tenants: set it. Without it, MEHO allows only `meho_status`,
  which lists your tenants and your role in each.
- To switch: change the field, then turn the extension off and on again in
  Claude Desktop (Settings > Extensions). `mcp-remote` keeps one saved login
  per header value, so the first session in a tenant opens a browser login
  once more.

There is no tool to switch tenant inside a session. So an agent cannot move
to another tenant on its own.

## Elevated operator mode (opt-in)

By default a session lists only the **working surface** — status,
connector/operation discovery, `call_operation` / `preview_operation` /
`result_query`, knowledge, memory, the broadcast trio, target/topology
reads, and the runbook run family. That is everything an operator needs to
get work done, and it is what every session gets with the `scopes` field
left at its default `mcp:read mcp:execute offline_access`.

The advanced `scopes` user_config is the deliberate opt-in to **operator
mode**. Set it to add the elevated scope:

```
mcp:read mcp:execute offline_access mcp:admin
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

**Realm prerequisite.** `mcp:admin` must be offered as an **optional**
(requestable, non-default) client scope on the realm's `meho-mcp` client.
If it is not, Keycloak refuses the whole login: it answers `invalid_scope`
("Invalid scopes: …") and issues no token. Keycloak does not drop a scope
it does not offer. So add `mcp:admin` to the `scopes` field only after the
realm offers it; until then, leave the field at its default.

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
