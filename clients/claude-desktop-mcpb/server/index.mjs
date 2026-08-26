#!/usr/bin/env node
// Launcher for the "MEHO for Claude Desktop" bundle.
//
// Claude Desktop runs this file with its bundled Node runtime (the
// manifest's server.mcp_config.command is "node"). It is a thin stdio
// pass-through to the proven onramp `mcp-remote@0.1.38 <url> …`
// (recipe: docs/cross-repo/mcp-client-setup.md, proven by #2666).
//
// mcp-remote is vendored into the bundle's node_modules at pack time
// (build.sh runs `npm ci --omit=dev`) and launched through the same
// runtime already executing this file — `process.execPath` — so there is
// no `npx`, no PATH lookup, and no per-platform shell. Under Claude
// Desktop the entry point runs inside a UtilityProcess whose PATH is the
// GUI PATH (no /opt/homebrew/bin, no nvm shims), so the old `npx` spawn
// died with `spawn npx ENOENT` before OAuth (field-test #3143); a direct
// `process.execPath` spawn cannot hit that failure.
//
// It adds two things over invoking mcp-remote directly:
//
//  1. A guard on the internal-CA path: the optional `ca_cert` user_config
//     is delivered as MEHO_CA_CERT, and NODE_EXTRA_CA_CERTS is exported to
//     the child ONLY when a non-empty path was supplied. Wiring the
//     optional file straight into NODE_EXTRA_CA_CERTS would leave the
//     host's substitution of an unset optional value (empty string vs.
//     omitted vs. literal) to decide the child's TLS trust store —
//     undocumented and not worth depending on.
//  2. The static OAuth client id: MEHO's Keycloak realm blocks anonymous
//     RFC 7591 Dynamic Client Registration (default Trusted Hosts policy →
//     403 "Host not trusted"), so a bare `mcp-remote` cannot complete
//     OAuth on a fresh install. The launcher presents the pre-registered
//     public `meho-mcp` client via --static-oauth-client-info, overridable
//     through the optional `client_id` user_config (delivered as
//     MEHO_MCP_CLIENT_ID) without editing the installed bundle.

import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

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

const env = { ...process.env };
const caCert = (process.env.MEHO_CA_CERT ?? "").trim();
if (caCert !== "") {
  env.NODE_EXTRA_CA_CERTS = caCert;
} else {
  delete env.NODE_EXTRA_CA_CERTS;
}
delete env.MEHO_CA_CERT;

// The pre-registered public OAuth client the shim presents to Keycloak.
// Fall back to the default when the host substitutes an empty value for an
// untouched optional field, so an operator who never opens the advanced
// `client_id` field still authenticates. Stripped from the child env (like
// MEHO_CA_CERT) to keep the config seam out of mcp-remote's environment.
const clientId = (process.env.MEHO_MCP_CLIENT_ID ?? "").trim() || "meho-mcp";
delete env.MEHO_MCP_CLIENT_ID;

// Run the child as a plain Node process even when process.execPath is
// Claude Desktop's Electron helper rather than a standalone `node`:
// ELECTRON_RUN_AS_NODE=1 "starts the process as a normal Node.js process"
// (Electron docs). Harmless no-op when execPath is already `node`.
env.ELECTRON_RUN_AS_NODE = "1";

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

// Spawn the pinned mcp-remote through this process's own runtime — a
// direct binary + args-array spawn on every platform (no external
// launcher, no PATH resolution, no shell). Present the static `meho-mcp`
// client via --static-oauth-client-info so the shim skips the DCR the
// realm blocks; the scope metadata matches what the backplane's token
// audience mappers expect.
const child = spawn(
  process.execPath,
  [
    mcpRemoteEntry,
    url.href,
    "--static-oauth-client-info",
    JSON.stringify({ client_id: clientId }),
    "--static-oauth-client-metadata",
    JSON.stringify({ scope: "mcp:read mcp:execute" }),
  ],
  { stdio: "inherit", env },
);

child.on("error", (err) => {
  process.stderr.write(
    `meho-claude-desktop: failed to launch mcp-remote: ${err.message}\n`,
  );
  process.exit(1);
});
child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
  } else {
    process.exit(code ?? 0);
  }
});
