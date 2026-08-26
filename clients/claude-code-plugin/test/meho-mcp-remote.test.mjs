// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Stubbed-argv suite for bin/meho-mcp-remote — the Claude Code plugin's
// stdio shim wrapper. The wrapper ends in `exec npx -y mcp-remote@… "$url"
// --static-oauth-client-info … --static-oauth-client-metadata …`, so the
// contract worth pinning is the exact argv (and, in particular, the scope
// metadata) it hands to npx.
//
// npx is stubbed: a fake `npx` on a controlled PATH prints each argument on
// its own line, then exits 0. The wrapper `exec`s it, so the stub replaces
// the process and its stdout is the wrapper's stdout — letting the suite
// assert the child contract without a live backplane or a real mcp-remote.

import { spawnSync } from "node:child_process";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { after, test } from "node:test";
import assert from "node:assert/strict";

const HERE = dirname(fileURLToPath(import.meta.url));
const WRAPPER = join(HERE, "..", "bin", "meho-mcp-remote");
const URL = "https://meho.internal.example/mcp";
const DEFAULT_SCOPE = "mcp:read mcp:execute";

// A temp bin dir holding a stub `npx` that echoes its argv, one arg per
// line. /usr/bin:/bin keeps `env`/`bash` resolvable under the controlled
// PATH (the wrapper is `#!/usr/bin/env bash`).
const STUB_BIN = mkdtempSync(join(tmpdir(), "meho-plugin-test-"));
writeFileSync(
  join(STUB_BIN, "npx"),
  '#!/usr/bin/env bash\nfor a in "$@"; do printf "%s\\n" "$a"; done\n',
);
chmodSync(join(STUB_BIN, "npx"), 0o755);
const PATH_WITH_STUB = `${STUB_BIN}:/usr/bin:/bin`;

after(() => rmSync(STUB_BIN, { recursive: true, force: true }));

// Run the wrapper with the stub npx on PATH and no operator config file
// (MEHO_PLUGIN_CONFIG points at a path that does not exist, so the
// wrapper's optional `. "$config_file"` sourcing is skipped).
function runWrapper(extraEnv = {}) {
  return spawnSync(WRAPPER, [], {
    encoding: "utf8",
    env: {
      PATH: PATH_WITH_STUB,
      MEHO_MCP_URL: URL,
      MEHO_PLUGIN_CONFIG: join(STUB_BIN, "does-not-exist.env"),
      ...extraEnv,
    },
  });
}

// Parse the stub's line-per-arg output into the metadata JSON that follows
// the --static-oauth-client-metadata flag.
function scopeMetadataFrom(stdout) {
  const args = stdout.split("\n").filter((l) => l.length > 0);
  const i = args.indexOf("--static-oauth-client-metadata");
  assert.notEqual(i, -1, `metadata flag absent in argv: ${stdout}`);
  return JSON.parse(args[i + 1]);
}

test("default: scope metadata is the working surface, url passed through", () => {
  const res = runWrapper();
  assert.equal(res.status, 0, `wrapper exited non-zero: ${res.stderr}`);
  const args = res.stdout.split("\n").filter((l) => l.length > 0);
  // npx invocation shape: -y mcp-remote@<v> <url> --static-oauth-client-info
  // {…} --static-oauth-client-metadata {…}
  assert.equal(args[0], "-y");
  assert.match(args[1], /^mcp-remote@/);
  assert.equal(args[2], URL);
  assert.deepEqual(scopeMetadataFrom(res.stdout), { scope: DEFAULT_SCOPE });
});

test("override: MEHO_MCP_SCOPES sets the requested scope verbatim", () => {
  const elevated = "mcp:read mcp:execute mcp:admin";
  const res = runWrapper({ MEHO_MCP_SCOPES: elevated });
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(scopeMetadataFrom(res.stdout), { scope: elevated });
});

test("empty MEHO_MCP_SCOPES falls back to the default surface", () => {
  const res = runWrapper({ MEHO_MCP_SCOPES: "" });
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(scopeMetadataFrom(res.stdout), { scope: DEFAULT_SCOPE });
});

test("whitespace-only MEHO_MCP_SCOPES falls back to the default surface", () => {
  const res = runWrapper({ MEHO_MCP_SCOPES: "   " });
  assert.equal(res.status, 0, res.stderr);
  assert.deepEqual(scopeMetadataFrom(res.stdout), { scope: DEFAULT_SCOPE });
});
