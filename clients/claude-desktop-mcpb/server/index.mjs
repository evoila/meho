#!/usr/bin/env node
// Launcher for the "MEHO for Claude Desktop" bundle.
//
// Claude Desktop runs this file with its bundled Node runtime (the
// manifest's server.mcp_config.command is "node"). It hands the proven
// onramp `mcp-remote@0.1.38 <url> …` (recipe:
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
// It adds two things over invoking mcp-remote directly:
//
//  1. Internal-CA trust. The optional `ca_cert` user_config is delivered as
//     NODE_EXTRA_CA_CERTS by the manifest `env` block, which Claude Desktop
//     applies to this process's environment *before Node boots*. That is
//     the only workable delivery route: NODE_EXTRA_CA_CERTS is read once at
//     Node startup and cannot be set from launcher code afterwards (proven
//     in #3341's spike), and in-process there is no child env to export it
//     to. When the field is left empty (the default fresh install) Node
//     receives an empty NODE_EXTRA_CA_CERTS and treats it as "no extra
//     certs" — the default trust store is untouched and public-CA deploys
//     keep working.
//  2. The static OAuth client id: MEHO's Keycloak realm blocks anonymous
//     RFC 7591 Dynamic Client Registration (default Trusted Hosts policy →
//     403 "Host not trusted"), so a bare `mcp-remote` cannot complete
//     OAuth on a fresh install. The launcher presents the pre-registered
//     public `meho-mcp` client via --static-oauth-client-info, overridable
//     through the optional `client_id` user_config (delivered as
//     MEHO_MCP_CLIENT_ID) without editing the installed bundle.

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

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

// The pre-registered public OAuth client the shim presents to Keycloak.
// Fall back to the default when the host substitutes an empty value for an
// untouched optional field, so an operator who never opens the advanced
// `client_id` field still authenticates. Scrubbed from process.env (which
// mcp-remote inherits in-process) to keep the config seam out of its
// environment.
const clientId = (process.env.MEHO_MCP_CLIENT_ID ?? "").trim() || "meho-mcp";
delete process.env.MEHO_MCP_CLIENT_ID;

// OAuth scope metadata presented during the handshake. The default is the
// working surface; the optional `scopes` user_config (delivered as
// MEHO_MCP_SCOPES) lets an operator deliberately request an elevated
// surface — append mcp:admin to reach the operator planes. Fall back to the
// default when the host substitutes an empty/whitespace value for the
// untouched optional field. Requesting a scope the realm does not grant
// degrades to the default surface: per OAuth 2.1 (RFC 6749 §3.3) the
// authorization server may ignore an ungranted scope, and Keycloak drops a
// scope that is not an assigned default/optional client scope, so the token
// never carries it and the elevated tools simply do not list. Scrubbed from
// process.env like the client id.
const scopes =
  (process.env.MEHO_MCP_SCOPES ?? "").trim() || "mcp:read mcp:execute";
delete process.env.MEHO_MCP_SCOPES;

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

// Hand mcp-remote its invocation via process.argv, then import it: it reads
// process.argv.slice(2), so argv[2..] carry the URL and the static-OAuth
// contract. argv[0]/argv[1] are ignored by its parser (kept only so any
// usage string it prints names the real entry). Present the static
// `meho-mcp` client via --static-oauth-client-info so the shim skips the
// DCR the realm blocks; the scope metadata matches what the backplane's
// token audience mappers expect.
process.argv = [
  process.argv[0],
  mcpRemoteEntry,
  url.href,
  "--static-oauth-client-info",
  JSON.stringify({ client_id: clientId }),
  "--static-oauth-client-metadata",
  JSON.stringify({ scope: scopes }),
];

// Import (not spawn) the vendored entry. pathToFileURL keeps the dynamic
// import valid on Windows, where a bare path is not a valid import
// specifier. mcp-remote takes over this process's stdio and installs its
// own SIGINT / stdin-EOF handlers; Desktop closing the stdio pipe drives
// its graceful shutdown. A failure to import surfaces as a clear error.
try {
  await import(pathToFileURL(mcpRemoteEntry).href);
} catch (err) {
  process.stderr.write(
    `meho-claude-desktop: failed to launch mcp-remote: ${err.message}\n`,
  );
  process.exit(1);
}
