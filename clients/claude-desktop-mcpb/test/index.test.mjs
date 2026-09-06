// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Behavioral suite for server/index.mjs — the .mcpb launcher.
//
// The launcher runs the vendored mcp-remote **in-process**: it sets
// process.argv to the mcp-remote invocation and imports the entry, spawning
// nothing (#3341, field-test F6). These tests run index.mjs under a
// deliberately minimal PATH (/usr/bin:/bin — no /opt/homebrew/bin, no nvm
// shims) to reproduce Claude Desktop's UtilityProcess GUI PATH; the
// launcher must not depend on PATH at all.
//
// mcp-remote is stubbed: a fake node_modules/mcp-remote whose entry is an
// ESM module that, when imported, prints a JSON diagnostic of the argv +
// env + process identity it was invoked with, then exits 0. Because the
// launcher imports it, the stub runs in the launcher's own process — its
// reported ppid is the test runner, which is what proves the F6 failure
// mode (a spawned process.execPath child) cannot recur.

import { spawnSync } from "node:child_process";
import {
  cpSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
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
// mcp-remote whose entry echoes how it was launched.
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
// The stub entry is imported by the launcher, so it runs in the launcher's
// process. It reports process.pid/ppid so the suite can prove there was no
// spawned child (F6 regression), then exits 0 so spawnSync returns.
writeFileSync(
  join(STUB_DIR, "dist", "proxy.js"),
  [
    "process.stdout.write(",
    "  JSON.stringify({",
    "    argv: process.argv,",
    "    execPath: process.execPath,",
    "    pid: process.pid,",
    "    ppid: process.ppid,",
    "    runAsNode: process.env.ELECTRON_RUN_AS_NODE ?? null,",
    "    nodeExtraCaCerts: process.env.NODE_EXTRA_CA_CERTS ?? null,",
    "    mehoCaCert: process.env.MEHO_CA_CERT ?? null,",
    "    mehoClientId: process.env.MEHO_MCP_CLIENT_ID ?? null,",
    "    mehoScopes: process.env.MEHO_MCP_SCOPES ?? null,",
    "  }) + '\\n',",
    ");",
    "process.exit(0);",
    "",
  ].join("\n"),
);

const INDEX = join(BUNDLE, "server", "index.mjs");
// require.resolve inside the launcher returns the realpath, so compare
// against the realpath here (macOS maps /var → /private/var via a symlink).
const STUB_ENTRY = realpathSync(join(STUB_DIR, "dist", "proxy.js"));

after(() => rmSync(BUNDLE, { recursive: true, force: true }));

// Run the launcher with a controlled env; PATH defaults to the minimal set.
// The launcher is a direct child of this test process, so a diagnostic whose
// ppid equals our pid proves mcp-remote ran in the launcher (in-process),
// not in a grandchild the launcher spawned.
function runLauncher(args, extraEnv = {}) {
  return spawnSync(process.execPath, [INDEX, ...args], {
    encoding: "utf8",
    env: { PATH: MINIMAL_PATH, ...extraEnv },
  });
}

test("in-process: child argv is [node, bundled mcp-remote, url, --static-oauth…]", () => {
  const url = "https://meho.internal.example/mcp";
  const res = runLauncher([url]);

  assert.equal(res.status, 0, `launcher exited non-zero: ${res.stderr}`);
  const diag = JSON.parse(res.stdout);

  // mcp-remote reads process.argv.slice(2); argv[1] names the resolved entry.
  assert.equal(diag.argv[1], STUB_ENTRY);
  assert.equal(diag.argv[2], url);
  assert.equal(diag.argv[3], "--static-oauth-client-info");
  assert.deepEqual(JSON.parse(diag.argv[4]), { client_id: "meho-mcp" });
  assert.equal(diag.argv[5], "--static-oauth-client-metadata");
  assert.deepEqual(JSON.parse(diag.argv[6]), { scope: "mcp:read mcp:execute" });
  assert.equal(diag.argv.length, 7);
});

test("regression (F6): mcp-remote runs in the launcher process, not a spawned child", () => {
  const res = runLauncher(["https://meho.internal.example/mcp"]);
  assert.equal(res.status, 0, res.stderr);
  const diag = JSON.parse(res.stdout);

  // The launcher is a direct child of this test runner. If mcp-remote ran
  // in-process, its parent is this test runner; if the launcher had spawned
  // a process.execPath child (the F6 failure), the parent would be the
  // launcher instead. This is what makes execPath being an Electron helper
  // irrelevant — nothing is ever spawned through it.
  assert.equal(
    diag.ppid,
    process.pid,
    "mcp-remote must run in-process (ppid = test runner), never in a spawned child",
  );
});

test("regression (F6): launcher imports no process-spawning module", () => {
  // child_process is the only vector for a process.execPath child; without
  // importing it the launcher structurally cannot reintroduce the F6 spawn.
  // (Comments may still discuss the old spawn — strip them before matching
  // so documentation of the fix never trips the guard.)
  const code = readFileSync(REAL_INDEX, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
  assert.doesNotMatch(
    code,
    /child_process/,
    "launcher must not import child_process",
  );
  assert.doesNotMatch(
    code,
    /\bspawn\s*\(/,
    "launcher must not spawn a subprocess",
  );
});

test("ELECTRON_RUN_AS_NODE is no longer set (no child to coax into Node mode)", () => {
  const res = runLauncher(["https://meho.internal.example/mcp"]);
  assert.equal(res.status, 0, res.stderr);
  assert.equal(JSON.parse(res.stdout).runAsNode, null);
});

test("CA: NODE_EXTRA_CA_CERTS is boot-delivered by the manifest and passed through untouched", () => {
  // In-process the launcher cannot manage NODE_EXTRA_CA_CERTS: Node reads it
  // once at startup, before launcher code runs, and the CA is delivered by
  // the manifest env block. So the launcher must leave whatever value it was
  // started with intact — a non-empty path reaches mcp-remote, and an empty
  // value (the untouched-optional fresh install) stays empty, which Node
  // treats as "no extra certs" (proven in #3341's spike).
  const withCa = runLauncher(["https://meho.internal.example/mcp"], {
    NODE_EXTRA_CA_CERTS: "/etc/ssl/internal-ca.pem",
  });
  assert.equal(withCa.status, 0, withCa.stderr);
  assert.equal(
    JSON.parse(withCa.stdout).nodeExtraCaCerts,
    "/etc/ssl/internal-ca.pem",
  );

  const empty = runLauncher(["https://meho.internal.example/mcp"], {
    NODE_EXTRA_CA_CERTS: "",
  });
  assert.equal(empty.status, 0, empty.stderr);
  assert.equal(JSON.parse(empty.stdout).nodeExtraCaCerts, "");

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
  assert.equal(diag.mehoClientId, null); // scrubbed from mcp-remote's env
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
  assert.equal(diag.mehoScopes, null); // scrubbed from mcp-remote's env
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

test("guard: a non-https endpoint is rejected before importing mcp-remote", () => {
  const res = runLauncher(["http://meho.internal.example/mcp"]);
  assert.equal(res.status, 1);
  assert.equal(res.stdout, ""); // never reached mcp-remote
  assert.match(res.stderr, /https/);
});

test("guard: a malformed URL is rejected before importing mcp-remote", () => {
  const res = runLauncher(["not a url"]);
  assert.equal(res.status, 1);
  assert.match(res.stderr, /valid URL/);
});

test("guard: a missing URL argument is rejected", () => {
  const res = runLauncher([]);
  assert.equal(res.status, 1);
  assert.match(res.stderr, /valid URL/);
});
