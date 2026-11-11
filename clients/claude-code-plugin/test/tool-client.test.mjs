// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Tests for lib/tool-client.mjs: the login client of each AI tool (#6210).
//
// The last two tests load the real pinned mcp-remote from
// clients/claude-code-plugin/node_modules (`npm ci --omit=dev`). They prove,
// with mcp-remote's own code, that the Meho-Tool header gives Claude Code and
// Claude Desktop two saved login files, and that a separate client id alone
// does not. Without the tree they are skipped, unless MEHO_PLUGIN_E2E=1 (set
// in CI), which turns a missing tree into a failure.

import { once } from "node:events";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { after, test } from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_CLIENT_ID,
  TOOL_HEADER,
  authConfigUrl,
  resolveToolClient,
} from "../lib/tool-client.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = join(HERE, "..");
const MCP_URL = "https://meho.internal.example/mcp";
const LIST = { "claude-code": "meho-mcp-code", "claude-desktop": "meho-mcp-desktop" };

// A fetch that answers `body` with `status`, and records the URLs it got.
function fakeFetch(body, status = 200) {
  const calls = [];
  const impl = async (url) => {
    calls.push(String(url));
    return new Response(typeof body === "string" ? body : JSON.stringify(body), { status });
  };
  return { impl, calls };
}

test("the auth-config URL replaces the /mcp at the end of the path", () => {
  for (const [mcp, want] of [
    ["https://meho.internal.example/mcp", "https://meho.internal.example/api/v1/auth-config"],
    ["https://meho.internal.example/mcp/", "https://meho.internal.example/api/v1/auth-config"],
    ["https://meho.internal.example:8443/meho/mcp", "https://meho.internal.example:8443/meho/api/v1/auth-config"],
    ["https://meho.internal.example/mcp?x=1#y", "https://meho.internal.example/api/v1/auth-config"],
    ["https://meho.internal.example/other", "https://meho.internal.example/api/v1/auth-config"],
  ]) {
    assert.equal(authConfigUrl(mcp), want, mcp);
  }
});

test("each tool kind gets its own listed client and the Meho-Tool header", async () => {
  for (const [kind, client] of Object.entries(LIST)) {
    const { impl, calls } = fakeFetch({ mcp_clients: LIST });
    const got = await resolveToolClient({ kind, mcpUrl: MCP_URL, fetchImpl: impl });
    assert.deepEqual(got, { clientId: client, header: `${TOOL_HEADER}: ${kind}` });
    assert.deepEqual(calls, ["https://meho.internal.example/api/v1/auth-config"]);
  }
});

test("no usable entry for the kind keeps meho-mcp and sends no header", async () => {
  for (const body of [
    {},
    { mcp_clients: {} },
    { mcp_clients: null },
    { mcp_clients: ["meho-mcp-code"] },
    { mcp_clients: "meho-mcp-code" },
    { mcp_clients: { "claude-desktop": "meho-mcp-desktop" } },
    { mcp_clients: { "claude-code": "" } },
    { mcp_clients: { "claude-code": 42 } },
    { mcp_clients: { "claude-code": 'a"b' } },
    { mcp_clients: { "claude-code": "x".repeat(256) } },
    null,
  ]) {
    const { impl } = fakeFetch(body);
    const said = [];
    const got = await resolveToolClient({
      kind: "claude-code",
      mcpUrl: MCP_URL,
      fetchImpl: impl,
      say: (t) => said.push(t),
    });
    assert.deepEqual(got, { clientId: DEFAULT_CLIENT_ID, header: null }, JSON.stringify(body));
    assert.deepEqual(said, [], JSON.stringify(body));
  }
});

test("a failed read keeps meho-mcp, sends no header, and says why", async () => {
  const failures = [
    fakeFetch({ mcp_clients: LIST }, 404).impl,
    fakeFetch({ mcp_clients: LIST }, 503).impl,
    fakeFetch("not json").impl,
    async () => {
      throw new TypeError("fetch failed");
    },
  ];
  for (const impl of failures) {
    const said = [];
    const got = await resolveToolClient({
      kind: "claude-code",
      mcpUrl: MCP_URL,
      fetchImpl: impl,
      say: (t) => said.push(t),
    });
    assert.deepEqual(got, { clientId: DEFAULT_CLIENT_ID, header: null });
    assert.equal(said.length, 1);
    assert.match(said[0], /could not read the login client list from MEHO/);
  }
});

test("a bad MCP URL keeps meho-mcp", async () => {
  const got = await resolveToolClient({ kind: "claude-code", mcpUrl: "not a url", say: () => {} });
  assert.deepEqual(got, { clientId: DEFAULT_CLIENT_ID, header: null });
});

test("MEHO that does not answer in time keeps meho-mcp", async () => {
  // The timer of AbortSignal.timeout does not keep node running; this one
  // does, until the abort.
  const hang = (_url, { signal }) =>
    new Promise((_resolve, reject) => {
      const keepAlive = setTimeout(() => {}, 10_000);
      signal.addEventListener("abort", () => {
        clearTimeout(keepAlive);
        reject(signal.reason);
      });
    });
  const started = Date.now();
  const got = await resolveToolClient({
    kind: "claude-code",
    mcpUrl: MCP_URL,
    fetchImpl: hang,
    timeoutMs: 50,
    say: () => {},
  });
  assert.deepEqual(got, { clientId: DEFAULT_CLIENT_ID, header: null });
  assert.ok(Date.now() - started < 5000);
});

test("a set client id wins, without a header and without asking MEHO", async () => {
  const { impl, calls } = fakeFetch({ mcp_clients: LIST });
  const got = await resolveToolClient({
    kind: "claude-code",
    mcpUrl: MCP_URL,
    override: "  my-mcp  ",
    fetchImpl: impl,
  });
  assert.deepEqual(got, { clientId: "my-mcp", header: null });
  assert.deepEqual(calls, []);
});

test("the real fetch reads the list from a MEHO over HTTP", async () => {
  const server = createServer((req, res) => {
    if (req.url === "/api/v1/auth-config") {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ mcp_clients: LIST }));
    } else {
      res.writeHead(404).end();
    }
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  try {
    const mcpUrl = `http://127.0.0.1:${server.address().port}/mcp`;
    const got = await resolveToolClient({ kind: "claude-desktop", mcpUrl });
    assert.deepEqual(got, { clientId: "meho-mcp-desktop", header: "Meho-Tool: claude-desktop" });
  } finally {
    server.close();
  }
});

// --- Saved login files, with the real mcp-remote ---------------------------

// mcp-remote's argument parser, from the pinned tree. It returns the hash
// that names the saved login file (<hash>_tokens.json).
async function loadParser() {
  const dist = join(PLUGIN_ROOT, "node_modules", "mcp-remote", "dist");
  if (!existsSync(dist)) {
    return null;
  }
  const chunk = readdirSync(dist).find(
    (name) => name.endsWith(".js") && /\bparseCommandLineArgs,/.test(readFileSync(join(dist, name), "utf8")),
  );
  assert.ok(chunk, `no mcp-remote file exports parseCommandLineArgs in ${dist}`);
  return (await import(pathToFileURL(join(dist, chunk)).href)).parseCommandLineArgs;
}

const parseArgs = await loadParser();
if (!parseArgs && process.env.MEHO_PLUGIN_E2E === "1") {
  throw new Error(
    "MEHO_PLUGIN_E2E=1 but clients/claude-code-plugin/node_modules/mcp-remote " +
      "is missing; run `npm ci --omit=dev` there first",
  );
}
const realTest = parseArgs
  ? test
  : (name) => test(name, { skip: "run `npm ci --omit=dev` in clients/claude-code-plugin" }, () => {});

const AUTH_DIR = mkdtempSync(join(tmpdir(), "meho-tool-client-"));
after(() => rmSync(AUTH_DIR, { recursive: true, force: true }));

// The saved-login hash mcp-remote uses for these launcher arguments.
async function loginFileHash(clientId, header) {
  process.env.MCP_REMOTE_CONFIG_DIR = AUTH_DIR;
  const args = [
    MCP_URL,
    "--static-oauth-client-info",
    JSON.stringify({ client_id: clientId }),
    ...(header ? ["--header", header] : []),
  ];
  return (await parseArgs(args, "usage")).serverUrlHash;
}

async function toolArgs(kind) {
  const { impl } = fakeFetch({ mcp_clients: LIST });
  return resolveToolClient({ kind, mcpUrl: MCP_URL, fetchImpl: impl });
}

realTest("Claude Code and Claude Desktop write two different saved login files", async () => {
  const code = await toolArgs("claude-code");
  const desktop = await toolArgs("claude-desktop");
  const codeHash = await loginFileHash(code.clientId, code.header);
  const desktopHash = await loginFileHash(desktop.clientId, desktop.header);
  assert.notEqual(codeHash, desktopHash);
  // Both differ from the shared login of the meho-mcp client too.
  assert.notEqual(codeHash, await loginFileHash(DEFAULT_CLIENT_ID, null));
});

// Keep the header: a separate client id alone does not split the saved
// login, because mcp-remote's file name ignores the client id.
realTest("a client id of its own without the header still shares one saved login", async () => {
  assert.equal(
    await loginFileHash("meho-mcp-code", null),
    await loginFileHash("meho-mcp-desktop", null),
  );
});
