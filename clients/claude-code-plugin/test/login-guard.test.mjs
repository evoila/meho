// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Behavioural suite for lib/login-guard.mjs (#3961). The guard runs the
// vendored mcp-remote in its own process and adds two rules:
//   1. A connect that does not finish within MEHO_MCP_LOGIN_TIMEOUT seconds
//      stops, so an unfinished browser login frees the callback port.
//   2. When the login expires during a session, the failed call gets a clear
//      message, and the process stops.
//   3. Once connected, the process frees the OAuth callback port and removes
//      its own lockfile, so another session can run its own login (#4000).
//      It also drops mcp-remote's exit hooks that would later delete
//      another session's lockfile.
//
// Two layers:
//   - Stub tests: tiny fake mcp-remote entries. No network, always run.
//   - End-to-end tests: the real mcp-remote pinned by package-lock.json,
//     against a fake MEHO + Keycloak on 127.0.0.1. They need the tree
//     installed in clients/claude-code-plugin/node_modules
//     (`npm ci --omit=dev`). Without it they are skipped, unless
//     MEHO_PLUGIN_E2E=1 (set in CI), which turns a missing tree into a
//     failure. They pin the exact mcp-remote behaviour the guard relies on,
//     so a future mcp-remote bump that changes it turns this suite red.

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createServer as createHttpServer } from "node:http";
import { createServer as createTcpServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { after, test } from "node:test";
import assert from "node:assert/strict";

const HERE = dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = join(HERE, "..");
const GUARD = join(PLUGIN_ROOT, "lib", "login-guard.mjs");
const LOGIN_EXPIRED =
  "Your MEHO login has expired. Run /mcp, pick meho, and reconnect to log in again.";

const TMP_DIRS = [];
const CHILDREN = new Set();
after(() => {
  for (const c of CHILDREN) c.kill("SIGKILL");
  for (const d of TMP_DIRS) rmSync(d, { recursive: true, force: true });
});
function tempDir(prefix) {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  TMP_DIRS.push(dir);
  return dir;
}

// Stub `open` / `xdg-open` / $BROWSER so no test ever opens a real browser.
const STUB_BIN = tempDir("meho-guard-bin-");
for (const name of ["open", "xdg-open", "browser"]) {
  writeFileSync(join(STUB_BIN, name), "#!/bin/sh\nexit 0\n");
  chmodSync(join(STUB_BIN, name), 0o755);
}
const BASE_ENV = {
  PATH: `${STUB_BIN}:${dirname(process.execPath)}:/usr/bin:/bin`,
  BROWSER: join(STUB_BIN, "browser"),
};

// Start the guard. Collects stdout lines and stderr text; `exited` resolves
// with { code, ms } when the process ends.
function startGuard(entry, args = [], env = {}) {
  const started = Date.now();
  const child = spawn(process.execPath, [GUARD, entry, ...args], {
    env: { ...BASE_ENV, HOME: tempDir("meho-guard-home-"), ...env },
    stdio: ["pipe", "pipe", "pipe"],
  });
  CHILDREN.add(child);
  const run = { child, lines: [], stderr: "", listeners: [] };
  let buf = "";
  child.stdout.on("data", (d) => {
    buf += d;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 1);
      run.lines.push(line);
      for (const fn of run.listeners) fn(line);
    }
  });
  child.stderr.on("data", (d) => {
    run.stderr += d;
  });
  child.stdin.on("error", () => {});
  run.exited = new Promise((resolve) => {
    child.on("exit", (code) => {
      CHILDREN.delete(child);
      resolve({ code, ms: Date.now() - started });
    });
  });
  run.send = (msg) => child.stdin.write(`${JSON.stringify(msg)}\n`);
  return run;
}

async function waitFor(check, what, ms = 10000) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    const value = check();
    if (value) return value;
    await new Promise((r) => setTimeout(r, 50));
  }
  throw new Error(`timed out waiting for ${what}`);
}

function portIsFree(port) {
  return new Promise((resolve) => {
    const s = createTcpServer();
    s.once("error", () => resolve(false));
    s.listen(port, "127.0.0.1", () => s.close(() => resolve(true)));
  });
}

async function waitForFreePort(port, ms = 5000) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    if (await portIsFree(port)) return true;
    await new Promise((r) => setTimeout(r, 50));
  }
  return false;
}

function stubEntry(source) {
  const dir = tempDir("meho-guard-stub-");
  const file = join(dir, "entry.mjs");
  writeFileSync(file, source);
  return file;
}

// --- Stub tests ------------------------------------------------------------

test("an unfinished login stops after the time limit and frees the port", async () => {
  // Like mcp-remote waiting for a browser login: it holds a port and never
  // writes to stdout.
  const entry = stubEntry(`
    import { createServer } from "node:net";
    const s = createServer().listen(0, "127.0.0.1", () => {
      process.stderr.write("PORT " + s.address().port + "\\n");
    });
  `);
  const run = startGuard(entry, [], { MEHO_MCP_LOGIN_TIMEOUT: "1" });
  const port = Number(
    await waitFor(() => run.stderr.match(/PORT (\d+)/)?.[1], "stub port"),
  );
  const { code, ms } = await run.exited;
  assert.equal(code, 1);
  assert.ok(ms < 8000, `took ${ms} ms`);
  assert.match(run.stderr, /did not connect within 1 second/);
  assert.match(run.stderr, /Finish any open MEHO login.*run \/mcp, pick meho, and reconnect/);
  assert.equal(await portIsFree(port), true, "the port must be free again");
});

test("a connected session is not stopped by the time limit", async () => {
  const entry = stubEntry(`
    process.stdout.write('{"jsonrpc":"2.0","id":1,"result":{}}\\n');
    setTimeout(() => process.exit(0), 2000);
  `);
  const run = startGuard(entry, [], { MEHO_MCP_LOGIN_TIMEOUT: "1" });
  const { code } = await run.exited;
  assert.equal(code, 0, run.stderr);
  assert.deepEqual(run.lines, ['{"jsonrpc":"2.0","id":1,"result":{}}']);
});

test("a too-large time limit is capped, not turned into an instant stop", async () => {
  // Node fires a timer of 2^31 ms or more after 1 ms. The guard caps the
  // limit at one day instead.
  const entry = stubEntry(`
    setTimeout(() => process.stdout.write('{"jsonrpc":"2.0","id":1,"result":{}}\\n'), 500);
    setTimeout(() => process.exit(0), 1000);
  `);
  const run = startGuard(entry, [], { MEHO_MCP_LOGIN_TIMEOUT: "99999999" });
  const { code } = await run.exited;
  assert.equal(code, 0, run.stderr);
  assert.match(run.stderr, /more than one day\. Using 86400/);
  assert.doesNotMatch(run.stderr, /TimeoutOverflowWarning|did not connect/);
});

test("a bad time limit value is reported and the default is used", async () => {
  const entry = stubEntry(`process.exit(0);`);
  const run = startGuard(entry, [], { MEHO_MCP_LOGIN_TIMEOUT: "abc" });
  const { code } = await run.exited;
  assert.equal(code, 0, run.stderr);
  assert.match(run.stderr, /ignoring MEHO_MCP_LOGIN_TIMEOUT=abc.*Using 300/);
});

test("an expired login fails the call at once with a clear message", async () => {
  // What the pinned mcp-remote writes when a call needs a new login.
  const entry = stubEntry(`
    process.stdout.write('{"jsonrpc":"2.0","id":1,"result":{}}\\n');
    process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: 2, error: {
      code: -32603,
      message: "Failed to forward message to remote server: Unauthorized",
    } }) + "\\n");
    setInterval(() => {}, 1000);
  `);
  const run = startGuard(entry);
  const { code, ms } = await run.exited;
  assert.equal(code, 1);
  assert.ok(ms < 8000, `took ${ms} ms`);
  assert.equal(run.lines[0], '{"jsonrpc":"2.0","id":1,"result":{}}');
  assert.deepEqual(JSON.parse(run.lines[1]), {
    jsonrpc: "2.0",
    id: 2,
    error: { code: -32603, message: LOGIN_EXPIRED },
  });
  assert.match(run.stderr, /Your MEHO login has expired/);
});

test("other errors pass through unchanged", async () => {
  const line = JSON.stringify({
    jsonrpc: "2.0",
    id: 3,
    error: {
      code: -32603,
      message: "Failed to forward message to remote server: fetch failed",
    },
  });
  const entry = stubEntry(`
    process.stdout.write(${JSON.stringify(`${line}\n`)});
    setTimeout(() => process.exit(0), 300);
  `);
  const run = startGuard(entry);
  const { code } = await run.exited;
  assert.equal(code, 0, run.stderr);
  assert.deepEqual(run.lines, [line]);
});

test("the guard hands mcp-remote its argv unchanged", async () => {
  const entry = stubEntry(`
    process.stdout.write(JSON.stringify(process.argv.slice(1)) + "\\n");
  `);
  const args = ["https://meho.example.internal/mcp", "--flag", '{"a": 1}'];
  const run = startGuard(entry, args);
  const { code } = await run.exited;
  assert.equal(code, 0, run.stderr);
  assert.deepEqual(JSON.parse(run.lines[0]), [entry, ...args]);
});

test("a missing entry argument is rejected", async () => {
  const child = spawn(process.execPath, [GUARD], { env: BASE_ENV });
  let stderr = "";
  child.stderr.on("data", (d) => (stderr += d));
  const code = await new Promise((r) => child.on("exit", r));
  assert.equal(code, 1);
  assert.match(stderr, /needs the mcp-remote entry/);
});

test("a connected session frees its port and removes only its own lockfile", async () => {
  // Like mcp-remote after a login: it holds the callback port and a
  // lockfile with its pid, then connects (writes to stdout) and keeps
  // running. Another session's lockfile sits next to it.
  const configDir = tempDir("meho-guard-auth-");
  const dir = join(configDir, "mcp-remote-0.0.0");
  mkdirSync(dir, { recursive: true });
  const otherLock = join(dir, "other_lock.json");
  writeFileSync(otherLock, JSON.stringify({ pid: 1, port: 1, timestamp: Date.now() }));
  const entry = stubEntry(`
    import { createServer } from "node:http";
    import { writeFileSync } from "node:fs";
    const s = createServer().listen(0, "127.0.0.1", () => {
      writeFileSync(${JSON.stringify(join(dir, "own_lock.json"))},
        JSON.stringify({ pid: process.pid, port: s.address().port, timestamp: Date.now() }));
      process.stderr.write("PORT " + s.address().port + "\\n");
      process.stdout.write('{"jsonrpc":"2.0","id":1,"result":{}}\\n');
    });
    setInterval(() => {}, 1000);
  `);
  const run = startGuard(entry, [], { MCP_REMOTE_CONFIG_DIR: configDir });
  const port = Number(await waitFor(() => run.stderr.match(/PORT (\d+)/)?.[1], "stub port"));
  await waitFor(() => run.lines.length === 1, "the first message");
  assert.equal(await waitForFreePort(port), true, "the port must be free");
  assert.equal(existsSync(join(dir, "own_lock.json")), false, "own lockfile must be gone");
  assert.equal(existsSync(otherLock), true, "another session's lockfile must stay");
  assert.equal(run.child.exitCode, null, "the connected session must keep running");
  run.child.kill("SIGTERM");
  await run.exited;
});

// --- End-to-end tests with the real pinned mcp-remote ----------------------

function realEntry() {
  const pkgPath = join(PLUGIN_ROOT, "node_modules", "mcp-remote", "package.json");
  if (!existsSync(pkgPath)) return null;
  const pkg = JSON.parse(readFileSync(pkgPath, "utf8"));
  const bin = typeof pkg.bin === "string" ? pkg.bin : pkg.bin["mcp-remote"];
  return join(dirname(pkgPath), bin);
}
const REAL_ENTRY = realEntry();
if (!REAL_ENTRY && process.env.MEHO_PLUGIN_E2E === "1") {
  throw new Error(
    "MEHO_PLUGIN_E2E=1 but clients/claude-code-plugin/node_modules/mcp-remote " +
      "is missing; run `npm ci --omit=dev` there first",
  );
}
const e2e = REAL_ENTRY
  ? test
  : (name) =>
      test(name, { skip: "run `npm ci --omit=dev` in clients/claude-code-plugin" }, () => {});

// mcp-remote keeps its files in <MCP_REMOTE_CONFIG_DIR>/mcp-remote-<v>, where
// <v> is the version string built into its code (not always the npm version).
function mcpRemoteConfigVersion() {
  const dist = dirname(REAL_ENTRY);
  for (const f of readdirSync(dist).filter((n) => n.endsWith(".js"))) {
    const m = readFileSync(join(dist, f), "utf8").match(/var version\d* = "([^"]+)"/);
    if (m) return m[1];
  }
  throw new Error("cannot find mcp-remote's built-in version string");
}

// A fake MEHO /mcp plus a fake Keycloak.
//   "login": every /mcp call answers 401, so mcp-remote starts a browser login.
//   "expire": the token "good" works for initialize and tools/list; tools/call
//     answers 401, and every refresh answers invalid_grant. That is a login
//     that expired during the session.
//   "browser": every finished browser login (an authorization code sent to
//     the token endpoint) gets a new token, and only the newest token works.
//     expireLogin() makes the current token stop working, as when a login
//     expires; a refresh then answers invalid_grant.
async function startFakeMeho(mode) {
  let base;
  let issued = 0;
  let validToken = mode === "browser" ? null : "good";
  const server = createHttpServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const path = new URL(req.url, base).pathname;
      const json = (status, obj, headers = {}) => {
        res.writeHead(status, { "content-type": "application/json", ...headers });
        res.end(JSON.stringify(obj));
      };
      if (path.startsWith("/.well-known/oauth-protected-resource")) {
        return json(200, {
          resource: `${base}/mcp`,
          authorization_servers: [`${base}/realms/test`],
          scopes_supported: ["mcp:read", "mcp:execute"],
        });
      }
      if (path.includes("/.well-known/")) {
        return json(200, {
          issuer: `${base}/realms/test`,
          authorization_endpoint: `${base}/realms/test/auth`,
          token_endpoint: `${base}/realms/test/token`,
          response_types_supported: ["code"],
          code_challenge_methods_supported: ["S256"],
        });
      }
      if (path === "/realms/test/token") {
        if (mode === "browser" && new URLSearchParams(body).get("grant_type") === "authorization_code") {
          issued += 1;
          validToken = `token-${issued}`;
          return json(200, { access_token: validToken, token_type: "Bearer", expires_in: 3600 });
        }
        return json(400, { error: "invalid_grant", error_description: "Token is not active" });
      }
      if (path !== "/mcp") {
        res.writeHead(404);
        return res.end();
      }
      const challenge = {
        "www-authenticate": `Bearer resource_metadata="${base}/.well-known/oauth-protected-resource"`,
      };
      if (mode === "login" || !validToken || req.headers.authorization !== `Bearer ${validToken}`) {
        return json(401, { detail: "unauthorized" }, challenge);
      }
      if (req.method !== "POST") {
        res.writeHead(405);
        return res.end();
      }
      const msg = JSON.parse(body || "{}");
      if (mode === "expire" && msg.method === "tools/call") {
        return json(401, { detail: "token_expired" }, challenge);
      }
      if (msg.id === undefined) {
        res.writeHead(202);
        return res.end();
      }
      const result =
        msg.method === "initialize"
          ? {
              protocolVersion: msg.params.protocolVersion,
              capabilities: { tools: {} },
              serverInfo: { name: "fake-meho", version: "0" },
            }
          : { tools: [] };
      return json(200, { jsonrpc: "2.0", id: msg.id, result });
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${server.address().port}`;
  return {
    url: `${base}/mcp`,
    close: () => server.close(),
    expireLogin: () => {
      validToken = null;
    },
  };
}

function mcpRemoteArgs(url) {
  return [
    url,
    "--static-oauth-client-info",
    '{"client_id": "meho-mcp"}',
    "--static-oauth-client-metadata",
    '{"scope": "mcp:read mcp:execute offline_access"}',
  ];
}

const INITIALIZE = {
  jsonrpc: "2.0",
  id: 1,
  method: "initialize",
  params: {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "login-guard-test", version: "0" },
  },
};

e2e("e2e: a login that expires during a session fails the call fast", { timeout: 30000 }, async () => {
  const fake = await startFakeMeho("expire");
  try {
    const configDir = tempDir("meho-guard-auth-");
    const hash = createHash("md5").update(fake.url).digest("hex");
    const dir = join(configDir, `mcp-remote-${mcpRemoteConfigVersion()}`);
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      join(dir, `${hash}_tokens.json`),
      JSON.stringify({ access_token: "good", token_type: "Bearer", expires_in: 3600, refresh_token: "dead" }),
    );
    const run = startGuard(REAL_ENTRY, mcpRemoteArgs(fake.url), {
      MCP_REMOTE_CONFIG_DIR: configDir,
    });
    let callSent = 0;
    run.listeners.push((line) => {
      if (JSON.parse(line).id === 1) {
        run.send({ jsonrpc: "2.0", method: "notifications/initialized" });
        callSent = Date.now();
        run.send({ jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: "meho_status", arguments: {} } });
      }
    });
    run.send(INITIALIZE);
    const { code } = await run.exited;
    assert.equal(code, 1, run.stderr);
    const answer = run.lines.map((l) => JSON.parse(l)).find((m) => m.id === 2);
    assert.ok(answer, `no answer to the call; stderr:\n${run.stderr}`);
    assert.equal(answer.error.message, LOGIN_EXPIRED);
    assert.ok(Date.now() - callSent < 10000, "the call must fail fast");
  } finally {
    fake.close();
  }
});

e2e("e2e: an unfinished login stops, frees the callback port and the lockfile", { timeout: 30000 }, async () => {
  const fake = await startFakeMeho("login");
  try {
    const configDir = tempDir("meho-guard-auth-");
    const run = startGuard(REAL_ENTRY, mcpRemoteArgs(fake.url), {
      MCP_REMOTE_CONFIG_DIR: configDir,
      MEHO_MCP_LOGIN_TIMEOUT: "2",
    });
    run.send(INITIALIZE);
    const port = Number(
      await waitFor(
        () => run.stderr.match(/callback server running at http:\/\/127\.0\.0\.1:(\d+)/)?.[1],
        "the OAuth callback server",
      ),
    );
    const { code, ms } = await run.exited;
    assert.equal(code, 1);
    assert.ok(ms < 15000, `took ${ms} ms`);
    assert.match(run.stderr, /did not connect within 2 seconds/);
    assert.equal(await portIsFree(port), true, "the callback port must be free again");
    const hash = createHash("md5").update(fake.url).digest("hex");
    const lock = join(configDir, `mcp-remote-${mcpRemoteConfigVersion()}`, `${hash}_lock.json`);
    assert.equal(existsSync(lock), false, "the lockfile must be gone");
  } finally {
    fake.close();
  }
});

e2e("e2e: a second session does not crash while another one waits for a login", { timeout: 30000 }, async () => {
  const fake = await startFakeMeho("login");
  try {
    const configDir = tempDir("meho-guard-auth-");
    const first = startGuard(REAL_ENTRY, mcpRemoteArgs(fake.url), {
      MCP_REMOTE_CONFIG_DIR: configDir,
      MEHO_MCP_LOGIN_TIMEOUT: "60",
    });
    first.send(INITIALIZE);
    await waitFor(() => /Creating lockfile/.test(first.stderr), "the first session's lockfile");

    const second = startGuard(REAL_ENTRY, mcpRemoteArgs(fake.url), {
      MCP_REMOTE_CONFIG_DIR: configDir,
      MEHO_MCP_LOGIN_TIMEOUT: "2",
    });
    second.send(INITIALIZE);
    const { code, ms } = await second.exited;
    assert.equal(code, 1);
    assert.ok(ms < 15000, `took ${ms} ms`);
    assert.match(second.stderr, /did not connect within 2 seconds/);
    assert.doesNotMatch(second.stderr, /EADDRINUSE/);
    first.child.kill("SIGTERM");
    await first.exited;
  } finally {
    fake.close();
  }
});

// Play the browser: wait until mcp-remote prints the login URL and its
// callback server listens, then send the authorization code to the
// redirect_uri in that URL, as Keycloak would.
//
// mcp-remote prints the login URL before it starts the callback server. A
// code sent in between reaches a port where nothing listens yet, and fetch
// fails with ECONNREFUSED (#4896). So wait for the "running at" line too; it
// names the port the server listens on.
async function finishBrowserLogin(run, code) {
  const waitInSession = (check, what) =>
    waitFor(check, what).catch((err) => {
      throw new Error(`${err.message}; stderr:\n${run.stderr}`, { cause: err });
    });
  const loginUrl = await waitInSession(
    () => run.stderr.match(/Please authorize this client by visiting:\n(\S+)/)?.[1],
    "the browser login URL",
  );
  const callback = new URL(new URL(loginUrl).searchParams.get("redirect_uri"));
  const serverUp = new RegExp(`OAuth callback server running at http://127\\.0\\.0\\.1:${callback.port}\\b`);
  await waitInSession(
    () => serverUp.test(run.stderr),
    `the OAuth callback server on port ${callback.port}`,
  );
  callback.searchParams.set("code", code);
  let res;
  try {
    res = await fetch(callback);
  } catch (err) {
    throw new Error(
      `sending the code to ${callback.origin} failed: ${err.message}` +
        ` (cause: ${err.cause?.message ?? "none"}); stderr:\n${run.stderr}`,
      { cause: err },
    );
  }
  assert.equal(res.status, 200, `the callback must accept the code; stderr:\n${run.stderr}`);
  return Number(callback.port);
}

function waitForAnswer(run, id, what) {
  return waitFor(() => run.lines.map((l) => JSON.parse(l)).find((m) => m.id === id), what, 20000);
}

e2e("e2e: a second session can log in while a logged-in session stays open", { timeout: 60000 }, async () => {
  const fake = await startFakeMeho("browser");
  try {
    const configDir = tempDir("meho-guard-auth-");
    const env = { MCP_REMOTE_CONFIG_DIR: configDir, MEHO_MCP_LOGIN_TIMEOUT: "30" };
    const lockfiles = () =>
      readdirSync(join(configDir, `mcp-remote-${mcpRemoteConfigVersion()}`)).filter((n) =>
        n.endsWith("_lock.json"),
      );

    // Session A logs in in the browser and connects.
    const first = startGuard(REAL_ENTRY, mcpRemoteArgs(fake.url), env);
    first.send(INITIALIZE);
    const port = await finishBrowserLogin(first, "code-a");
    const answerA = await waitForAnswer(first, 1, "session A to connect");
    assert.ok(answerA.result, `session A must connect; stderr:\n${first.stderr}`);
    assert.equal(await waitForFreePort(port), true, "session A must free the callback port");
    assert.deepEqual(lockfiles(), [], "session A must remove its lockfile");

    // Session A's login expires. Session B starts and needs a new browser
    // login on the same callback port while A stays open.
    fake.expireLogin();
    const second = startGuard(REAL_ENTRY, mcpRemoteArgs(fake.url), env);
    second.send(INITIALIZE);
    const portB = await finishBrowserLogin(second, "code-b");
    assert.equal(portB, port, "both sessions use the same callback port");
    const answerB = await waitForAnswer(second, 1, "session B to connect");
    assert.ok(answerB.result, `session B must connect; stderr:\n${second.stderr}`);
    assert.doesNotMatch(second.stderr, /EADDRINUSE|already in use/);
    assert.equal(first.child.exitCode, null, "session A must still be running");
    assert.equal(await waitForFreePort(port), true, "session B must free the callback port too");

    first.child.kill("SIGTERM");
    second.child.kill("SIGTERM");
    await Promise.all([first.exited, second.exited]);
  } finally {
    fake.close();
  }
});

e2e("e2e: a connected session that stops leaves another session's lockfile alone", { timeout: 60000 }, async () => {
  const fake = await startFakeMeho("browser");
  try {
    const configDir = tempDir("meho-guard-auth-");
    const env = { MCP_REMOTE_CONFIG_DIR: configDir, MEHO_MCP_LOGIN_TIMEOUT: "30" };
    const hash = createHash("md5").update(fake.url).digest("hex");
    const lock = join(configDir, `mcp-remote-${mcpRemoteConfigVersion()}`, `${hash}_lock.json`);

    // Session A logs in in the browser and connects.
    const first = startGuard(REAL_ENTRY, mcpRemoteArgs(fake.url), env);
    first.send(INITIALIZE);
    await finishBrowserLogin(first, "code-a");
    const answerA = await waitForAnswer(first, 1, "session A to connect");
    assert.ok(answerA.result, `session A must connect; stderr:\n${first.stderr}`);

    // Session B starts a browser login and writes its lockfile.
    fake.expireLogin();
    const second = startGuard(REAL_ENTRY, mcpRemoteArgs(fake.url), env);
    second.send(INITIALIZE);
    await waitFor(() => /Creating lockfile/.test(second.stderr), "session B's lockfile");
    await waitFor(() => existsSync(lock), "session B's lockfile on disk");

    // Claude Code closes session A. B's lockfile must stay, so a third
    // session waits for B's login instead of failing on the busy port.
    first.child.stdin.end();
    const { code } = await first.exited;
    assert.equal(code, 0, first.stderr);
    assert.equal(existsSync(lock), true, "session B's lockfile must stay");
    assert.equal(JSON.parse(readFileSync(lock, "utf8")).pid, second.child.pid);

    second.child.kill("SIGTERM");
    await second.exited;
  } finally {
    fake.close();
  }
});
