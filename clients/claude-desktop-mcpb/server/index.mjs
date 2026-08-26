#!/usr/bin/env node
// Launcher for the "MEHO for Claude Desktop" bundle.
//
// Claude Desktop runs this file with its bundled Node runtime (the
// manifest's server.mcp_config.command is "node"). It is a transparent
// stdio pass-through to the proven onramp `npx -y mcp-remote <url>`
// (recipe: docs/cross-repo/mcp-client-setup.md, proven by #2666).
//
// The one thing it adds over invoking npx directly is a guard on the
// internal-CA path: the optional `ca_cert` user_config is delivered as
// MEHO_CA_CERT, and NODE_EXTRA_CA_CERTS is exported to the child ONLY
// when a non-empty path was supplied. Wiring the optional file straight
// into NODE_EXTRA_CA_CERTS would leave the host's substitution of an
// unset optional value (empty string vs. omitted vs. literal) to decide
// the child's TLS trust store — undocumented and not worth depending on.

import { spawn } from "node:child_process";

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

// On Windows `npx` resolves to `npx.cmd`, which Node refuses to spawn
// without a shell (CVE-2024-27980 mitigation). The URL is validated
// above, so the only interpolated argument is a well-formed https URL.
const isWindows = process.platform === "win32";
const child = spawn(
  isWindows ? "npx.cmd" : "npx",
  ["-y", "mcp-remote", url.href],
  { stdio: "inherit", env, shell: isWindows },
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
