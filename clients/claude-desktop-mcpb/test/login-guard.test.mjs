// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// The bundle runs mcp-remote through the Claude Code plugin's login guard
// (#4003). These tests run the real launcher (server/index.mjs) with the
// guard staged next to it, the way build.sh packs it.
//
// Two layers:
//   - Stub tests: a tiny fake mcp-remote. No network, always run.
//   - End-to-end tests: the real mcp-remote pinned by package-lock.json,
//     against a fake MEHO + Keycloak on 127.0.0.1 over https (the launcher
//     only accepts https). They need the tree in
//     clients/claude-desktop-mcpb/node_modules (`npm ci --omit=dev`) and
//     `openssl` for a throwaway certificate. Without the tree they are
//     skipped, unless MEHO_MCPB_E2E=1 (set in CI), which makes a missing
//     tree a failure.

import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { createServer as createHttpsServer } from "node:https";
import { createServer as createTcpServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { after, test } from "node:test";
import assert from "node:assert/strict";

const HERE = dirname(fileURLToPath(import.meta.url));
const BUNDLE_ROOT = join(HERE, "..");
const GUARD = join(BUNDLE_ROOT, "..", "claude-code-plugin", "lib", "login-guard.mjs");
const RECONNECT =
  "turn the MEHO extension off and on again in Claude Desktop (Settings > Extensions)";
const LOGIN_EXPIRED =
  "Your MEHO login has expired. Turn the MEHO extension off and on again in " +
  "Claude Desktop (Settings > Extensions) to log in again.";

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
const STUB_BIN = tempDir("meho-mcpb-guard-bin-");
for (const name of ["open", "xdg-open", "browser"]) {
  writeFileSync(join(STUB_BIN, name), "#!/bin/sh\nexit 0\n");
  chmodSync(join(STUB_BIN, name), 0o755);
}

// A bundle copy the way build.sh stages it: server/ plus the guard.
// `nodeModules` is either a folder to link in or a stub entry source.
function makeBundle({ realNodeModules, stubSource }) {
  const dir = tempDir("meho-mcpb-guard-");
  cpSync(join(BUNDLE_ROOT, "server"), join(dir, "server"), { recursive: true });
  cpSync(GUARD, join(dir, "server", "login-guard.mjs"));
  if (realNodeModules) {
    symlinkSync(realNodeModules, join(dir, "node_modules"), "dir");
  } else {
    const pkg = join(dir, "node_modules", "mcp-remote");
    mkdirSync(join(pkg, "dist"), { recursive: true });
    writeFileSync(
      join(pkg, "package.json"),
      JSON.stringify({ name: "mcp-remote", type: "module", bin: { "mcp-remote": "dist/proxy.js" } }),
    );
    writeFileSync(join(pkg, "dist", "proxy.js"), stubSource);
  }
  return { index: join(dir, "server", "index.mjs"), authDir: join(dir, "auth") };
}

// Start the launcher. Collects stdout lines and stderr text; `exited`
// resolves with { code, ms } when the process ends.
function startLauncher(bundle, args, env = {}) {
  const started = Date.now();
  const child = spawn(process.execPath, [bundle.index, ...args], {
    env: {
      PATH: `${STUB_BIN}:/usr/bin:/bin`,
      BROWSER: join(STUB_BIN, "browser"),
      HOME: tempDir("meho-mcpb-guard-home-"),
      MCP_REMOTE_CONFIG_DIR: bundle.authDir,
      ...env,
    },
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

function signInLocks(authDir) {
  try {
    return readdirSync(authDir).filter((f) => f.startsWith("meho-claude-desktop-"));
  } catch {
    return [];
  }
}

const STUB_URL = "https://meho.internal.example/mcp";

// --- Stub tests ------------------------------------------------------------

test("an unfinished connect stops at the time limit with the Claude Desktop message", async () => {
  // Like mcp-remote waiting for a browser login: it holds a port and never
  // writes to stdout.
  const bundle = makeBundle({
    stubSource: `
      import { createServer } from "node:net";
      const s = createServer().listen(0, "127.0.0.1", () => {
        process.stderr.write("PORT " + s.address().port + "\\n");
      });
    `,
  });
  const run = startLauncher(bundle, [STUB_URL], { MEHO_MCP_LOGIN_TIMEOUT: "1" });
  const port = Number(await waitFor(() => run.stderr.match(/PORT (\d+)/)?.[1], "stub port"));
  const { code, ms } = await run.exited;
  assert.equal(code, 1);
  assert.ok(ms < 8000, `took ${ms} ms`);
  assert.match(
    run.stderr,
    new RegExp(
      "meho-claude-desktop: MEHO did not connect within 1 second, so this attempt stopped\\. " +
        "Finish any open MEHO login in your browser, then turn the MEHO extension off and on again",
    ),
  );
  assert.doesNotMatch(run.stderr, /meho plugin|\/mcp, pick meho/);
  assert.equal(await portIsFree(port), true, "the port must be free again");
  assert.deepEqual(signInLocks(bundle.authDir), [], "the sign-in lock must be gone");
});

test("an expired login answer gets the Claude Desktop message; mcp-remote never sees the wording variables", async () => {
  const bundle = makeBundle({
    stubSource: `
      const env = Object.keys(process.env).filter((k) => k.startsWith("MEHO_LOGIN_GUARD_"));
      process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: 1, result: { env } }) + "\\n");
      process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: 2, error: {
        code: -32603,
        message: "Failed to forward message to remote server: Unauthorized",
      } }) + "\\n");
      setInterval(() => {}, 1000);
    `,
  });
  const run = startLauncher(bundle, [STUB_URL]);
  const { code, ms } = await run.exited;
  assert.equal(code, 1);
  assert.ok(ms < 8000, `took ${ms} ms`);
  assert.deepEqual(JSON.parse(run.lines[0]).result.env, []);
  assert.deepEqual(JSON.parse(run.lines[1]), {
    jsonrpc: "2.0",
    id: 2,
    error: { code: -32603, message: LOGIN_EXPIRED },
  });
  assert.match(run.stderr, new RegExp(`meho-claude-desktop: ${LOGIN_EXPIRED.replace(/[().]/g, "\\$&")}`));
});

// --- End-to-end tests with the real pinned mcp-remote ----------------------

const REAL_NODE_MODULES = join(BUNDLE_ROOT, "node_modules");
const HAVE_TREE = existsSync(join(REAL_NODE_MODULES, "mcp-remote", "package.json"));
if (!HAVE_TREE && process.env.MEHO_MCPB_E2E === "1") {
  throw new Error(
    "MEHO_MCPB_E2E=1 but clients/claude-desktop-mcpb/node_modules/mcp-remote " +
      "is missing; run `npm ci --omit=dev` there first",
  );
}
const e2e = HAVE_TREE
  ? test
  : (name) =>
      test(name, { skip: "run `npm ci --omit=dev` in clients/claude-desktop-mcpb" }, () => {});

// mcp-remote keeps its files in <MCP_REMOTE_CONFIG_DIR>/mcp-remote-<v>, where
// <v> is the version string built into its code (not always the npm version).
function mcpRemoteConfigVersion() {
  const dist = join(REAL_NODE_MODULES, "mcp-remote", "dist");
  for (const f of readdirSync(dist).filter((n) => n.endsWith(".js"))) {
    const m = readFileSync(join(dist, f), "utf8").match(/var version\d* = "([^"]+)"/);
    if (m) return m[1];
  }
  throw new Error("cannot find mcp-remote's built-in version string");
}

// A throwaway self-signed certificate for 127.0.0.1. Made at run time, so no
// private key is ever committed. The launcher trusts it through its CA
// argument, the same way it trusts an internal CA.
function makeCertificate() {
  const dir = tempDir("meho-mcpb-guard-tls-");
  const key = join(dir, "key");
  const cert = join(dir, "cert");
  const res = spawnSync(
    "openssl",
    [
      "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
      "-nodes", "-keyout", key, "-out", cert, "-days", "1",
      "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
    ],
    { encoding: "utf8" },
  );
  assert.equal(res.status, 0, `openssl failed: ${res.error?.message ?? res.stderr}`);
  return { key: readFileSync(key), cert: readFileSync(cert), certPath: cert };
}

// A fake MEHO /mcp plus a fake Keycloak, over https.
//   "login": every /mcp call answers 401, so mcp-remote starts a browser login.
//   "expire": the token "good" works for initialize and tools/list; tools/call
//     answers 401, and every refresh answers invalid_grant. That is a login
//     that expired during the session.
async function startFakeMeho(mode, tls) {
  let base;
  const server = createHttpsServer({ key: tls.key, cert: tls.cert }, (req, res) => {
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
        return json(400, { error: "invalid_grant", error_description: "Token is not active" });
      }
      if (path !== "/mcp") {
        res.writeHead(404);
        return res.end();
      }
      const challenge = {
        "www-authenticate": `Bearer resource_metadata="${base}/.well-known/oauth-protected-resource"`,
      };
      if (mode === "login" || req.headers.authorization !== "Bearer good") {
        return json(401, { detail: "unauthorized" }, challenge);
      }
      if (req.method !== "POST") {
        res.writeHead(405);
        return res.end();
      }
      const msg = JSON.parse(body || "{}");
      if (msg.method === "tools/call") return json(401, { detail: "token_expired" }, challenge);
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
  base = `https://127.0.0.1:${server.address().port}`;
  return { url: `${base}/mcp`, close: () => server.close() };
}

// Save a login for `url`, the way mcp-remote stores it after a browser login.
function saveLogin(authDir, url) {
  const hash = createHash("md5").update(url).digest("hex");
  const dir = join(authDir, `mcp-remote-${mcpRemoteConfigVersion()}`);
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, `${hash}_tokens.json`),
    JSON.stringify({ access_token: "good", token_type: "Bearer", expires_in: 3600, refresh_token: "dead" }),
  );
}

const INITIALIZE = {
  jsonrpc: "2.0",
  id: 1,
  method: "initialize",
  params: {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "mcpb-login-guard-test", version: "0" },
  },
};

const TLS = HAVE_TREE ? makeCertificate() : null;

e2e("e2e: a login that expires during a session gets a clear error at once", { timeout: 30000 }, async () => {
  const fake = await startFakeMeho("expire", TLS);
  try {
    const bundle = makeBundle({ realNodeModules: REAL_NODE_MODULES });
    saveLogin(bundle.authDir, fake.url);
    const run = startLauncher(bundle, [fake.url, TLS.certPath]);
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
    assert.deepEqual(signInLocks(bundle.authDir), []);
  } finally {
    fake.close();
  }
});

e2e("e2e: an unfinished login stops at the time limit and frees the port", { timeout: 30000 }, async () => {
  const fake = await startFakeMeho("login", TLS);
  try {
    const bundle = makeBundle({ realNodeModules: REAL_NODE_MODULES });
    const run = startLauncher(bundle, [fake.url, TLS.certPath], { MEHO_MCP_LOGIN_TIMEOUT: "2" });
    run.send(INITIALIZE);
    const port = Number(
      await waitFor(
        () => run.stderr.match(/callback server running at http:\/\/127\.0\.0\.1:(\d+)/)?.[1],
        `the OAuth callback server; stderr:\n${run.stderr}`,
      ),
    );
    const { code, ms } = await run.exited;
    assert.equal(code, 1);
    assert.ok(ms < 15000, `took ${ms} ms`);
    assert.match(run.stderr, /meho-claude-desktop: MEHO did not connect within 2 seconds/);
    assert.ok(run.stderr.includes(RECONNECT));
    assert.equal(await portIsFree(port), true, "the callback port must be free again");
    const hash = createHash("md5").update(fake.url).digest("hex");
    const lock = join(bundle.authDir, `mcp-remote-${mcpRemoteConfigVersion()}`, `${hash}_lock.json`);
    assert.equal(existsSync(lock), false, "mcp-remote's lockfile must be gone");
    assert.deepEqual(signInLocks(bundle.authDir), [], "the sign-in lock must be gone");
  } finally {
    fake.close();
  }
});

e2e("e2e: a second start during an unfinished login waits, then stops cleanly", { timeout: 40000 }, async () => {
  // Claude Desktop can start the extension twice at once (#3857).
  const fake = await startFakeMeho("login", TLS);
  try {
    const bundle = makeBundle({ realNodeModules: REAL_NODE_MODULES });
    const env = { MEHO_MCP_LOGIN_TIMEOUT: "2" };
    const first = startLauncher(bundle, [fake.url, TLS.certPath], env);
    first.send(INITIALIZE);
    await waitFor(() => signInLocks(bundle.authDir).length === 1, "the first copy's sign-in lock");
    const second = startLauncher(bundle, [fake.url, TLS.certPath], env);
    second.send(INITIALIZE);
    const [a, b] = await Promise.all([first.exited, second.exited]);
    for (const [run, result] of [[first, a], [second, b]]) {
      assert.equal(result.code, 1, run.stderr);
      assert.match(run.stderr, /MEHO did not connect within 2 seconds/);
      assert.doesNotMatch(run.stderr, /EADDRINUSE|Fatal error|Unhandled|TypeError/);
    }
    assert.match(second.stderr, /another MEHO instance \(pid \d+\) is signing in; waiting/);
    assert.ok(b.ms > a.ms, "the second copy must start its login after the first one stopped");
    assert.deepEqual(signInLocks(bundle.authDir), []);
  } finally {
    fake.close();
  }
});

e2e("e2e: two starts at once with a saved login both connect", { timeout: 30000 }, async () => {
  const fake = await startFakeMeho("expire", TLS);
  try {
    const bundle = makeBundle({ realNodeModules: REAL_NODE_MODULES });
    saveLogin(bundle.authDir, fake.url);
    const runs = [0, 1].map(() => startLauncher(bundle, [fake.url, TLS.certPath]));
    for (const run of runs) run.send(INITIALIZE);
    for (const run of runs) {
      await waitFor(
        () => run.lines.some((l) => JSON.parse(l).id === 1 && JSON.parse(l).result),
        `an initialize answer; stderr:\n${run.stderr}`,
      );
      run.child.stdin.end();
    }
    for (const run of runs) {
      const { code } = await run.exited;
      assert.equal(code, 0, run.stderr);
      assert.doesNotMatch(run.stderr, /did not connect|Fatal error/);
    }
    assert.deepEqual(signInLocks(bundle.authDir), []);
  } finally {
    fake.close();
  }
});
