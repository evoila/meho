// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Behavioral suite for server/index.mjs — the .mcpb launcher.
//
// The launcher spawns the vendored mcp-remote through this process's own
// runtime (process.execPath), so these tests run index.mjs under a
// deliberately minimal PATH (/usr/bin:/bin — no /opt/homebrew/bin, no nvm
// shims) to reproduce Claude Desktop's UtilityProcess GUI PATH. If the
// launcher still reached for `npx`, the spawn would die with
// `spawn npx ENOENT` (field-test #3143) and the stub below would never run.
//
// mcp-remote is stubbed: a fake node_modules/mcp-remote whose bin prints a
// JSON diagnostic of the argv + env it was invoked with, then exits 0. That
// lets the suite assert the exact child contract without a live backplane.

import { spawnSync } from "node:child_process";
import {
  cpSync,
  mkdirSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { after, test } from "node:test";
import assert from "node:assert/strict";

const HERE = dirname(fileURLToPath(import.meta.url));
const REAL_INDEX = join(HERE, "..", "server", "index.mjs");

// A minimal PATH with no Homebrew/nvm entries — the GUI PATH shape.
const MINIMAL_PATH = "/usr/bin:/bin";

// Build one isolated bundle copy: a standalone index.mjs plus a stub
// mcp-remote whose bin echoes how it was launched.
const BUNDLE = mkdtempSync(join(tmpdir(), "meho-mcpb-test-"));
mkdirSync(join(BUNDLE, "server"), { recursive: true });
cpSync(REAL_INDEX, join(BUNDLE, "server", "index.mjs"));

const STUB_DIR = join(BUNDLE, "node_modules", "mcp-remote");
mkdirSync(join(STUB_DIR, "dist"), { recursive: true });
writeFileSync(
  join(STUB_DIR, "package.json"),
  JSON.stringify({
    name: "mcp-remote",
    version: "0.1.38",
    type: "module",
    bin: { "mcp-remote": "dist/proxy.js", "mcp-remote-client": "dist/client.js" },
  }),
);
writeFileSync(
  join(STUB_DIR, "dist", "proxy.js"),
  [
    "process.stdout.write(",
    "  JSON.stringify({",
    "    argv: process.argv,",
    "    execPath: process.execPath,",
    "    runAsNode: process.env.ELECTRON_RUN_AS_NODE ?? null,",
    "    nodeExtraCaCerts: process.env.NODE_EXTRA_CA_CERTS ?? null,",
    "    mehoCaCert: process.env.MEHO_CA_CERT ?? null,",
    "    mehoClientId: process.env.MEHO_MCP_CLIENT_ID ?? null,",
    "    mehoScopes: process.env.MEHO_MCP_SCOPES ?? null,",
    "  }) + '\\n',",
    ");",
    "",
  ].join("\n"),
);

const INDEX = join(BUNDLE, "server", "index.mjs");
// require.resolve inside the launcher returns the realpath, so compare
// against the realpath here (macOS maps /var → /private/var via a symlink).
const STUB_ENTRY = realpathSync(join(STUB_DIR, "dist", "proxy.js"));

after(() => rmSync(BUNDLE, { recursive: true, force: true }));

// Run the launcher with a controlled env; PATH defaults to the minimal set.
function runLauncher(args, extraEnv = {}) {
  return spawnSync(process.execPath, [INDEX, ...args], {
    encoding: "utf8",
    env: { PATH: MINIMAL_PATH, ...extraEnv },
  });
}

test("minimal PATH: no ENOENT, child argv is [execPath, bundled mcp-remote, url, --static-oauth…]", () => {
  const url = "https://meho.internal.example/mcp";
  const res = runLauncher([url]);

  assert.equal(res.status, 0, `launcher exited non-zero: ${res.stderr}`);
  const diag = JSON.parse(res.stdout);

  // The child ran through process.execPath (argv[0] is that same runtime),
  // proving a direct binary spawn rather than a PATH-resolved npx.
  assert.equal(diag.argv[0], diag.execPath);
  assert.equal(diag.argv[1], STUB_ENTRY);
  assert.equal(diag.argv[2], url);
  assert.equal(diag.argv[3], "--static-oauth-client-info");
  assert.deepEqual(JSON.parse(diag.argv[4]), { client_id: "meho-mcp" });
  assert.equal(diag.argv[5], "--static-oauth-client-metadata");
  assert.deepEqual(JSON.parse(diag.argv[6]), { scope: "mcp:read mcp:execute" });
  assert.equal(diag.argv.length, 7);
});

test("ELECTRON_RUN_AS_NODE=1 is set in the child env", () => {
  const res = runLauncher(["https://meho.internal.example/mcp"]);
  assert.equal(res.status, 0, res.stderr);
  assert.equal(JSON.parse(res.stdout).runAsNode, "1");
});

test("guard: NODE_EXTRA_CA_CERTS exported only on a non-empty CA path, MEHO_CA_CERT scrubbed", () => {
  const withCa = runLauncher(["https://meho.internal.example/mcp"], {
    MEHO_CA_CERT: "/etc/ssl/internal-ca.pem",
  });
  assert.equal(withCa.status, 0, withCa.stderr);
  const a = JSON.parse(withCa.stdout);
  assert.equal(a.nodeExtraCaCerts, "/etc/ssl/internal-ca.pem");
  assert.equal(a.mehoCaCert, null); // scrubbed from the child env

  for (const caValue of ["", "   "]) {
    const res = runLauncher(["https://meho.internal.example/mcp"], {
      MEHO_CA_CERT: caValue,
    });
    assert.equal(res.status, 0, res.stderr);
    assert.equal(JSON.parse(res.stdout).nodeExtraCaCerts, null);
  }

  // Unset entirely — NODE_EXTRA_CA_CERTS must not leak in.
  const unset = runLauncher(["https://meho.internal.example/mcp"]);
  assert.equal(JSON.parse(unset.stdout).nodeExtraCaCerts, null);
});

test("guard: client_id override applied and MEHO_MCP_CLIENT_ID scrubbed", () => {
  const res = runLauncher(["https://meho.internal.example/mcp"], {
    MEHO_MCP_CLIENT_ID: "meho-mcp-lab",
  });
  assert.equal(res.status, 0, res.stderr);
  const diag = JSON.parse(res.stdout);
  assert.deepEqual(JSON.parse(diag.argv[4]), { client_id: "meho-mcp-lab" });
  assert.equal(diag.mehoClientId, null); // scrubbed from the child env
});

test("guard: an empty client_id falls back to meho-mcp", () => {
  const res = runLauncher(["https://meho.internal.example/mcp"], {
    MEHO_MCP_CLIENT_ID: "   ",
  });
  assert.equal(res.status, 0, res.stderr);
  const diag = JSON.parse(res.stdout);
  assert.deepEqual(JSON.parse(diag.argv[4]), { client_id: "meho-mcp" });
});

test("guard: scopes override applied to metadata and MEHO_MCP_SCOPES scrubbed", () => {
  const elevated = "mcp:read mcp:execute mcp:admin";
  const res = runLauncher(["https://meho.internal.example/mcp"], {
    MEHO_MCP_SCOPES: elevated,
  });
  assert.equal(res.status, 0, res.stderr);
  const diag = JSON.parse(res.stdout);
  assert.deepEqual(JSON.parse(diag.argv[6]), { scope: elevated });
  assert.equal(diag.mehoScopes, null); // scrubbed from the child env
});

test("guard: an empty or whitespace-only scopes falls back to the default surface", () => {
  for (const value of ["", "   "]) {
    const res = runLauncher(["https://meho.internal.example/mcp"], {
      MEHO_MCP_SCOPES: value,
    });
    assert.equal(res.status, 0, res.stderr);
    assert.deepEqual(JSON.parse(JSON.parse(res.stdout).argv[6]), {
      scope: "mcp:read mcp:execute",
    });
  }
});

test("guard: a non-https endpoint is rejected before spawning", () => {
  const res = runLauncher(["http://meho.internal.example/mcp"]);
  assert.equal(res.status, 1);
  assert.equal(res.stdout, ""); // never reached the child
  assert.match(res.stderr, /https/);
});

test("guard: a malformed URL is rejected before spawning", () => {
  const res = runLauncher(["not a url"]);
  assert.equal(res.status, 1);
  assert.match(res.stderr, /valid URL/);
});

test("guard: a missing URL argument is rejected", () => {
  const res = runLauncher([]);
  assert.equal(res.status, 1);
  assert.match(res.stderr, /valid URL/);
});
