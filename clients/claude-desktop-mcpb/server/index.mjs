#!/usr/bin/env node
// Launcher for the "MEHO for Claude Desktop" bundle.
//
// Claude Desktop runs this file with its bundled Node runtime (the
// manifest's server.mcp_config.command is "node"). It hands the proven
// onramp `mcp-remote@0.1.39 <url> …` (recipe:
// docs/cross-repo/mcp-client-setup.md, proven by #2666) full ownership of
// this process's stdio.
//
// It runs mcp-remote **in-process**: the vendored CLI entry is imported
// into this process after process.argv is set to the mcp-remote
// invocation. Nothing is spawned — there is no child process, no execPath
// launch, no `npx`, no PATH lookup, and no per-platform shell.
//
// Why in-process rather than a spawn (#3341, field-test F6). #3144 spawned
// mcp-remote by launching the bundle's own runtime (execPath) with
// ELECTRON_RUN_AS_NODE=1, on the assumption that flag makes the child run
// as plain Node whether execPath is a standalone `node` or Claude Desktop's
// Electron helper. On Claude Desktop 1.40609.0 that assumption is false:
// execPath is the
// `Claude Helper (Plugin)` Electron binary, which FATALs when spawned as
// Node even with ELECTRON_RUN_AS_NODE=1 ("Unable to find helper app"), so
// the child never started and Desktop cancelled `initialize` after 60 s.
// Importing the entry sidesteps execPath entirely — mcp-remote runs in the
// launcher process Claude Desktop already started as Node, so F6 cannot
// recur. mcp-remote's CLI entry has no `import.meta.url === argv[1]` main
// guard and reads `process.argv.slice(2)` at module top level, so importing
// it runs its `main` against the argv set below.
//
// It adds five things over invoking mcp-remote directly:
//
//  1. Internal-CA trust. The optional `ca_cert` user_config (a PEM file
//     path) arrives as argv[3], and the launcher appends its certificates to
//     Node's default CA list with tls.setDefaultCACertificates() before it
//     imports mcp-remote, so every TLS connection mcp-remote opens (OAuth
//     discovery, token exchange, /mcp) trusts the internal CA. It is an
//     argument rather than NODE_EXTRA_CA_CERTS because current Claude
//     Desktop strips NODE_EXTRA_CA_CERTS from the extension's environment
//     (#3143 F7). The existing default list is kept, so the bundled roots
//     and, when the host enables Node's system store (Desktop sets
//     NODE_USE_SYSTEM_CA=1), roots trusted in the OS store still apply.
//     On a Node without the API (before 22.19 / 24.5) the manifest's
//     NODE_EXTRA_CA_CERTS delivery, read by Node at boot, is the fallback.
//  2. The static OAuth client id: MEHO's Keycloak realm blocks anonymous
//     RFC 7591 Dynamic Client Registration (default Trusted Hosts policy →
//     403 "Host not trusted"), so a bare `mcp-remote` cannot complete
//     OAuth on a fresh install. The launcher presents the pre-registered
//     public `meho-mcp` client via --static-oauth-client-info, overridable
//     through the optional `client_id` user_config (delivered as
//     MEHO_MCP_CLIENT_ID) without editing the installed bundle.
//  3. One sign-in at a time (#3857). Claude Desktop can start the extension
//     twice at once; signin-lock.mjs makes the second copy wait until the
//     first one is connected, so only one browser tab opens.
//  4. The login guard (#4003). mcp-remote runs through the Claude Code
//     plugin's login guard (clients/claude-code-plugin/lib/login-guard.mjs;
//     build.sh copies it into server/). It stops a connect that is not done
//     within MEHO_MCP_LOGIN_TIMEOUT seconds (default 300), which frees the
//     login port, and it answers a call whose login has expired with a
//     clear message instead of no answer.
//  5. The current tenant (#5242). The optional `tenant` user_config
//     (delivered as MEHO_TENANT) is sent as the Meho-Tenant header on every
//     request, so the session works in that tenant.

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import tls from "node:tls";
import { fileURLToPath, pathToFileURL } from "node:url";

import { acquireSignInLock } from "./signin-lock.mjs";

const rawUrl = process.argv[2];

let url;
try {
  url = new URL(rawUrl ?? "");
} catch {
  process.stderr.write(
    "meho-claude-desktop: the MEHO MCP endpoint must be a valid URL " +
      "(e.g. https://meho.internal.example/mcp)\n",
  );
  process.exit(1);
}
if (url.protocol !== "https:") {
  process.stderr.write(
    "meho-claude-desktop: the MEHO MCP endpoint must use https\n",
  );
  process.exit(1);
}

// The optional internal-CA bundle path. For an untouched optional field the
// MCPB reference host code keeps an arg it has no value for as the literal
// `${user_config.ca_cert}` placeholder, so an unsubstituted placeholder
// means "no CA", the same as an empty or missing argument.
const caArg = (process.argv[3] ?? "").trim();
const caCertPath = /^\$\{[^}]*\}$/.test(caArg) ? "" : expandHome(caArg);

// Desktop's file field also takes a typed path and stores it as typed.
// Node does not expand `~`, so a typed `~/internal-ca.pem` would fail to
// load. Expand a bare `~` or a leading `~/` (or `~\` on Windows) to the
// home folder; leave every other path unchanged.
function expandHome(path) {
  return path === "~" || /^~[/\\]/.test(path) ? homedir() + path.slice(1) : path;
}

if (caCertPath && typeof tls.setDefaultCACertificates === "function") {
  // Append, never replace: the bundled roots and any OS-store roots already
  // in the default list must stay trusted. Must run before mcp-remote is
  // imported; connections opened earlier would not see the new list.
  try {
    const certs =
      readFileSync(caCertPath, "utf8").match(
        /-----BEGIN CERTIFICATE-----[\s\S]+?-----END CERTIFICATE-----/g,
      ) ?? [];
    if (certs.length === 0) {
      throw new Error("no PEM certificate found");
    }
    tls.setDefaultCACertificates([...tls.getCACertificates("default"), ...certs]);
  } catch (err) {
    process.stderr.write(
      `meho-claude-desktop: cannot load the internal CA bundle ${caCertPath}: ${err.message}\n`,
    );
    process.exit(1);
  }
} else if (caCertPath && !process.env.NODE_EXTRA_CA_CERTS) {
  process.stderr.write(
    `meho-claude-desktop: Node ${process.version} cannot add the internal CA ` +
      "bundle in-process (needs Node 22.19+ or 24.5+) and NODE_EXTRA_CA_CERTS " +
      "was not delivered; run the extension on a newer Node, or trust the " +
      "root CA in the OS trust store\n",
  );
}

// The pre-registered public OAuth client the shim presents to Keycloak.
// Fall back to the default when the host substitutes an empty value for an
// untouched optional field, so an operator who never opens the advanced
// `client_id` field still authenticates. Scrubbed from process.env (which
// mcp-remote inherits in-process) to keep the config seam out of its
// environment.
const clientId = (process.env.MEHO_MCP_CLIENT_ID ?? "").trim() || "meho-mcp";
delete process.env.MEHO_MCP_CLIENT_ID;

// OAuth scopes requested at login. The default is the working surface plus
// offline_access. offline_access makes the login last: Keycloak then issues
// an offline refresh token, which ends at the offline idle limit (48 hours
// recommended on meho-mcp) instead of the realm's SSO idle limit (30 minutes
// by default). The optional `scopes` user_config (delivered as
// MEHO_MCP_SCOPES) replaces the whole list — for example to add mcp:admin
// for the operator planes. Fall back to the default when the host
// substitutes an empty/whitespace value for the untouched optional field.
// Keycloak refuses a login that asks for a scope the meho-mcp client does
// not offer (invalid_scope, no token); it does not drop the scope silently.
// Scrubbed from process.env like the client id.
const scopes =
  (process.env.MEHO_MCP_SCOPES ?? "").trim() ||
  "mcp:read mcp:execute offline_access";
delete process.env.MEHO_MCP_SCOPES;

// The current tenant (#5242). A login can list several tenants, and MEHO
// works in one of them per request: the one the Meho-Tenant header names (a
// tenant name or ID). The optional `tenant` user_config (delivered as
// MEHO_TENANT) sets it for every request of this session. Empty, whitespace,
// or an unsubstituted `${user_config.tenant}` placeholder sends no header, so
// a person with one tenant needs no setting. mcp-remote keeps a separate saved
// login per header value, so the first session in a new tenant asks for a
// browser login. Scrubbed from process.env like the client id.
const tenantValue = (process.env.MEHO_TENANT ?? "").trim();
const tenant = /^\$\{[^}]*\}$/.test(tenantValue) ? "" : tenantValue;
delete process.env.MEHO_TENANT;
const tenantArgs = tenant ? ["--header", `Meho-Tenant: ${tenant}`] : [];

// Resolve the vendored mcp-remote CLI entry from the bundle's node_modules
// (populated by `npm ci --omit=dev` at pack time). Read the package's own
// `bin` mapping rather than hardcoding a path, so a future pinned version
// that relocates its entry script keeps working.
const require = createRequire(import.meta.url);
const mcpRemotePkgPath = require.resolve("mcp-remote/package.json");
const mcpRemotePkg = JSON.parse(readFileSync(mcpRemotePkgPath, "utf8"));
const mcpRemoteBin =
  typeof mcpRemotePkg.bin === "string"
    ? mcpRemotePkg.bin
    : mcpRemotePkg.bin["mcp-remote"];
const mcpRemoteEntry = join(dirname(mcpRemotePkgPath), mcpRemoteBin);

// The login guard, staged next to this file by build.sh.
const loginGuard = fileURLToPath(new URL("./login-guard.mjs", import.meta.url));

// Hand the guard its invocation via process.argv, then import it. The guard
// reads process.argv.slice(2): the mcp-remote entry first, then the
// arguments for mcp-remote. It sets process.argv to [node, entry, ...args]
// before it imports mcp-remote, which reads process.argv.slice(2), so
// mcp-remote gets the URL, the static-OAuth contract, and the Meho-Tenant
// header when a tenant is set. Present the static
// `meho-mcp` client via --static-oauth-client-info so the shim skips the
// DCR the realm blocks; the scope metadata matches what the backplane's
// token audience mappers expect.
process.argv = [
  process.argv[0],
  loginGuard,
  mcpRemoteEntry,
  url.href,
  "--static-oauth-client-info",
  JSON.stringify({ client_id: clientId }),
  "--static-oauth-client-metadata",
  JSON.stringify({ scope: scopes }),
  ...tenantArgs,
];

// One sign-in at a time (#3857): Claude Desktop can start this extension
// twice at once, and both copies would open a browser sign-in. Wait until no
// other copy is signing in, and release the lock once mcp-remote is
// connected. mcp-remote writes nothing to stdout before its connection to
// MEHO works, so its first stdout message is the "connected" signal. Details
// in signin-lock.mjs.
// The wrapper stays in place for every write: the login guard wraps
// process.stdout.write on top of it, so this wrapper must not swap itself
// out. Calling the release again is cheap; it does nothing once released.
const releaseSignInLock = await acquireSignInLock(url.href);
const stdoutWrite = process.stdout.write;
process.stdout.write = function writeAndRelease(...args) {
  releaseSignInLock();
  return stdoutWrite.apply(this, args);
};

// The guard's messages name this bundle and say how to reconnect in Claude
// Desktop. The guard removes both variables before mcp-remote loads.
process.env.MEHO_LOGIN_GUARD_PREFIX = "meho-claude-desktop";
process.env.MEHO_LOGIN_GUARD_RECONNECT =
  "turn the MEHO extension off and on again in Claude Desktop (Settings > Extensions)";

// Import (not spawn) the guard, which imports the vendored entry in this
// same process. pathToFileURL keeps the dynamic import valid on Windows,
// where a bare path is not a valid import specifier. mcp-remote takes over
// this process's stdio and installs its own SIGINT / stdin-EOF handlers;
// Desktop closing the stdio pipe drives its graceful shutdown. A failure to
// import surfaces as a clear error.
try {
  await import(pathToFileURL(loginGuard).href);
} catch (err) {
  process.stderr.write(
    `meho-claude-desktop: failed to launch mcp-remote: ${err.message}\n`,
  );
  process.exit(1);
}
