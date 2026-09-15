// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Behavioural suite for bin/meho-mcp-remote — the Claude Code plugin's
// stdio shim wrapper. The wrapper no longer floats mcp-remote's transitive
// tree via `npx -y`; it vendors the exact tree from the committed
// package-lock.json with `npm ci --omit=dev` into a per-operator cache, then
// runs the lock-installed local mcp-remote entry in place (#309). Two
// contracts are pinned here:
//   1. The install path — the shim runs `npm ci` from the committed lock and
//      never falls back to `npx -y`; a warm cache is not re-installed.
//   2. The child argv — the URL and the static-OAuth scope metadata handed to
//      the vendored entry are exactly what the previous npx invocation passed.
//
// No network / no real npm: `npm` and `npx` are stubbed on a controlled PATH.
// The `npm` stub fabricates a minimal mcp-remote tree (package.json `bin` +
// an entry that echoes its argv one-per-line) so the shim can resolve and
// `exec` a real local entry — the same resolution the real vendored tree
// uses — without touching the registry. The `npx` stub fails loudly, so any
// regression back to `npx -y` turns the suite red.

import { spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { after, test } from "node:test";
import assert from "node:assert/strict";

const HERE = dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = join(HERE, "..");
const WRAPPER = join(PLUGIN_ROOT, "bin", "meho-mcp-remote");
const URL = "https://meho.internal.example/mcp";
const DEFAULT_SCOPE = "mcp:read mcp:execute";

// Real `node` must stay resolvable under the controlled PATH: the shim shells
// out to `node` to resolve the vendored entry, then `exec node <entry> …`.
const NODE_DIR = dirname(process.execPath);

// A temp bin dir holding stub `npm` and `npx`. The npm stub fabricates a
// mcp-remote tree on `npm ci` and appends its argv+cwd to $NPM_LOG. The npx
// stub records any invocation to $NPX_LOG and exits non-zero — the shim must
// never reach it.
const STUB_BIN = mkdtempSync(join(tmpdir(), "meho-plugin-stub-"));
writeFileSync(
  join(STUB_BIN, "npm"),
  [
    "#!/usr/bin/env bash",
    'printf "%s (cwd=%s)\\n" "$*" "$PWD" >> "$NPM_LOG"',
    'if [ "$1" = "ci" ]; then',
    "  mkdir -p node_modules/mcp-remote/dist",
    "  cat > node_modules/mcp-remote/package.json <<'PKG'",
    '{ "name": "mcp-remote", "version": "0.1.38", "bin": { "mcp-remote": "dist/proxy.js" } }',
    "PKG",
    "  cat > node_modules/mcp-remote/dist/proxy.js <<'PROXY'",
    "#!/usr/bin/env node",
    "for (const a of process.argv.slice(2)) console.log(a);",
    "PROXY",
    "  chmod +x node_modules/mcp-remote/dist/proxy.js",
    "fi",
    "exit 0",
    "",
  ].join("\n"),
);
chmodSync(join(STUB_BIN, "npm"), 0o755);
writeFileSync(
  join(STUB_BIN, "npx"),
  [
    "#!/usr/bin/env bash",
    'printf "npx %s\\n" "$*" >> "$NPX_LOG"',
    'echo "meho-plugin-test: npx must not be invoked by the shim" >&2',
    "exit 1",
    "",
  ].join("\n"),
);
chmodSync(join(STUB_BIN, "npx"), 0o755);
const PATH_WITH_STUB = `${STUB_BIN}:${NODE_DIR}:/usr/bin:/bin`;

const TMP_DIRS = [STUB_BIN];
after(() => {
  for (const d of TMP_DIRS) rmSync(d, { recursive: true, force: true });
});

function freshCache() {
  const dir = mkdtempSync(join(tmpdir(), "meho-plugin-cache-"));
  TMP_DIRS.push(dir);
  return dir;
}

// Run the wrapper with the stubs on PATH, a private vendor cache, and no
// operator config file (MEHO_PLUGIN_CONFIG points at a nonexistent path, so
// the wrapper's optional `. "$config_file"` sourcing is skipped). Each run
// gets its own npm/npx log files.
function runWrapper({ cache = freshCache(), extraEnv = {} } = {}) {
  const npmLog = join(cache, "npm-invocations.log");
  const npxLog = join(cache, "npx-invocations.log");
  const res = spawnSync(WRAPPER, [], {
    encoding: "utf8",
    env: {
      PATH: PATH_WITH_STUB,
      MEHO_MCP_URL: URL,
      MEHO_PLUGIN_CACHE: cache,
      MEHO_PLUGIN_CONFIG: join(cache, "does-not-exist.env"),
      NPM_LOG: npmLog,
      NPX_LOG: npxLog,
      ...extraEnv,
    },
  });
  return { res, cache, npmLog, npxLog };
}

// Parse the fabricated entry's line-per-arg stdout. The vendored entry is
// exec'd as `node <entry> <url> --static-oauth-client-info {…}
// --static-oauth-client-metadata {…}`, so it echoes argv.slice(2): the URL
// followed by the two static-OAuth flag/value pairs.
function argvFrom(stdout) {
  return stdout.split("\n").filter((l) => l.length > 0);
}
function scopeMetadataFrom(stdout) {
  const args = argvFrom(stdout);
  const i = args.indexOf("--static-oauth-client-metadata");
  assert.notEqual(i, -1, `metadata flag absent in argv: ${stdout}`);
  return JSON.parse(args[i + 1]);
}

test("vendors from the committed lock and runs the local entry, not npx", () => {
  const { res, npmLog, npxLog } = runWrapper();
  assert.equal(res.status, 0, `wrapper exited non-zero: ${res.stderr}`);
  const args = argvFrom(res.stdout);
  // New shape: the local entry receives the URL first (no `-y mcp-remote@…`
  // npx preamble), then the static-OAuth flags.
  assert.equal(args[0], URL);
  assert.equal(args[1], "--static-oauth-client-info");
  assert.deepEqual(scopeMetadataFrom(res.stdout), { scope: DEFAULT_SCOPE });
  // npm ci ran from the committed lock; npx was never reached.
  assert.match(readFileSync(npmLog, "utf8"), /^ci --omit=dev/m);
  assert.equal(existsSync(npxLog), false, "npx stub must not have run");
});

test("client id + url are handed to the vendored entry verbatim", () => {
  const { res } = runWrapper();
  const args = argvFrom(res.stdout);
  const i = args.indexOf("--static-oauth-client-info");
  assert.notEqual(i, -1, `client-info flag absent: ${res.stdout}`);
  assert.deepEqual(JSON.parse(args[i + 1]), { client_id: "meho-mcp" });
});

test("a warm cache is not re-installed on the next launch", () => {
  const cache = freshCache();
  const first = runWrapper({ cache });
  assert.equal(first.res.status, 0, first.res.stderr);
  const second = runWrapper({ cache });
  assert.equal(second.res.status, 0, second.res.stderr);
  // Both launches share one npm log (keyed on the cache dir). Only the first
  // should have triggered `npm ci`; the second sees the matching lock + tree.
  const ciRuns = readFileSync(first.npmLog, "utf8")
    .split("\n")
    .filter((l) => l.startsWith("ci "));
  assert.equal(ciRuns.length, 1, `expected exactly one npm ci, got:\n${ciRuns.join("\n")}`);
});

test("override: MEHO_MCP_SCOPES sets the requested scope verbatim", () => {
  const elevated = "mcp:read mcp:execute mcp:admin";
  const { res } = runWrapper({ extraEnv: { MEHO_MCP_SCOPES: elevated } });
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(scopeMetadataFrom(res.stdout), { scope: elevated });
});

test("empty MEHO_MCP_SCOPES falls back to the default surface", () => {
  const { res } = runWrapper({ extraEnv: { MEHO_MCP_SCOPES: "" } });
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(scopeMetadataFrom(res.stdout), { scope: DEFAULT_SCOPE });
});

test("whitespace-only MEHO_MCP_SCOPES falls back to the default surface", () => {
  const { res } = runWrapper({ extraEnv: { MEHO_MCP_SCOPES: "   " } });
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(scopeMetadataFrom(res.stdout), { scope: DEFAULT_SCOPE });
});

// The committed lock is the security artifact: it must pin mcp-remote to the
// smoke-tested build and integrity-lock the transitive tree — including the
// hardened `qs` the desktop bundle already pins (#3444) — so a floating
// transitive version cannot slip in without a reviewed lockfile change.
test("committed package-lock pins mcp-remote + integrity-locks the tree", () => {
  assert.ok(
    existsSync(join(PLUGIN_ROOT, "package.json")),
    "package.json must be committed",
  );
  const lock = JSON.parse(
    readFileSync(join(PLUGIN_ROOT, "package-lock.json"), "utf8"),
  );
  assert.equal(lock.packages[""].dependencies["mcp-remote"], "0.1.38");
  assert.equal(lock.packages["node_modules/mcp-remote"].version, "0.1.38");
  assert.equal(lock.packages["node_modules/qs"].version, "6.16.0");
  const integrities = Object.values(lock.packages).filter((p) => p.integrity);
  assert.ok(integrities.length >= 1, "transitive tree must carry integrity hashes");
});
