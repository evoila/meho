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
// mode (a spawned process.execPath child) cannot recur. The stub also
// reports the fingerprints of the default CA list it sees at import time,
// which is how the suite proves the internal CA was injected in-process
// before mcp-remote loaded (F7). It also reports the sign-in lock files it
// sees at import time and when it writes its first message, which is how
// the suite proves only one copy signs in at a time (#3857).

import { spawn, spawnSync } from "node:child_process";
import { X509Certificate } from "node:crypto";
import {
  cpSync,
  existsSync,
  linkSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  realpathSync,
  rmSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import tls from "node:tls";
import { fileURLToPath, pathToFileURL } from "node:url";
import { after, test } from "node:test";
import assert from "node:assert/strict";

const HERE = dirname(fileURLToPath(import.meta.url));
const REAL_SERVER = join(HERE, "..", "server");
const REAL_INDEX = join(REAL_SERVER, "index.mjs");
const REAL_GUARD = join(HERE, "..", "..", "claude-code-plugin", "lib", "login-guard.mjs");
const MANIFEST = JSON.parse(readFileSync(join(HERE, "..", "manifest.json"), "utf8"));

// A throwaway self-signed CA (certificate only; its key was discarded at
// generation) that no trust store contains, so its presence in the default
// CA list can only come from the launcher. Inlined rather than a .pem file
// because the repo .gitignore excludes *.pem.
const TEST_CA_PEM = [
  "-----BEGIN CERTIFICATE-----",
  "MIICDTCCAbOgAwIBAgIUEHgJ/H8EVdNVnG679MNHnFqUPJIwCgYIKoZIzj0EAwIw",
  "UzEaMBgGA1UECgwRTUVITyB0ZXN0IGZpeHR1cmUxNTAzBgNVBAMMLE1FSE8gbGF1",
  "bmNoZXIgdGVzdCBDQSAobm90IHRydXN0ZWQgYW55d2hlcmUpMCAXDTI2MDkyNTEw",
  "MjgwNFoYDzIxMjYwOTAxMTAyODA0WjBTMRowGAYDVQQKDBFNRUhPIHRlc3QgZml4",
  "dHVyZTE1MDMGA1UEAwwsTUVITyBsYXVuY2hlciB0ZXN0IENBIChub3QgdHJ1c3Rl",
  "ZCBhbnl3aGVyZSkwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAATFERl1j74KHzHX",
  "orix1E8IC9kXxOk0vEULDHA9eu8L8zumpYr0qPUMHbm4yMl89ZiJ7WGINjhxUoKK",
  "HFNk+IKzo2MwYTAdBgNVHQ4EFgQU7ETK8tY9gLWuwucBDVw9WnqEw54wHwYDVR0j",
  "BBgwFoAU7ETK8tY9gLWuwucBDVw9WnqEw54wDwYDVR0TAQH/BAUwAwEB/zAOBgNV",
  "HQ8BAf8EBAMCAQYwCgYIKoZIzj0EAwIDSAAwRQIgD0+T4KOnw+cyaCtAOjtAwqRi",
  "PEzSYDT8Q4N6g6bDWvsCIQCLDwn/FJC/OjrVDrf9zcJLzqskQFzpZS1+8I4g/I5K",
  "eA==",
  "-----END CERTIFICATE-----",
  "",
].join("\n");
const CA_FINGERPRINT = new X509Certificate(TEST_CA_PEM).fingerprint256;

// The launcher runs under this same Node binary, so this decides which CA
// path it takes.
const NO_INJECT_API =
  typeof tls.setDefaultCACertificates !== "function" &&
  `tls.setDefaultCACertificates needs Node 22.19+ / 24.5+ (running ${process.version})`;

const URL_ARG = "https://meho.internal.example/mcp";

// A minimal PATH with no Homebrew/nvm entries — the GUI PATH shape.
const MINIMAL_PATH = "/usr/bin:/bin";

// Build one isolated bundle copy: the server/ folder plus a stub
// mcp-remote whose entry echoes how it was launched.
const BUNDLE = mkdtempSync(join(tmpdir(), "meho-mcpb-test-"));
cpSync(REAL_SERVER, join(BUNDLE, "server"), { recursive: true });
// build.sh stages the plugin's login guard next to index.mjs (#4003).
cpSync(REAL_GUARD, join(BUNDLE, "server", "login-guard.mjs"));

// mcp-remote's config folder (MCP_REMOTE_CONFIG_DIR), where the launcher
// keeps its sign-in lock. Every run uses a folder inside the bundle copy, so
// the suite never touches the real ~/.mcp-auth.
const AUTH_DIR = join(BUNDLE, "auth");

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
    "import { X509Certificate } from 'node:crypto';",
    "import { readdirSync } from 'node:fs';",
    "import tls from 'node:tls';",
    "const authDir = process.env.MCP_REMOTE_CONFIG_DIR;",
    "const locks = () => {",
    "  try { return readdirSync(authDir).filter((f) => f.endsWith('.lock')); }",
    "  catch { return []; }",
    "};",
    "const importedAt = Date.now();",
    "const locksAtImport = locks();",
    "// STUB_CONNECT_MS: pretend the connection to MEHO takes this long.",
    "const connectMs = Number(process.env.STUB_CONNECT_MS ?? 0);",
    "if (connectMs > 0) await new Promise((r) => setTimeout(r, connectMs));",
    "const caDefault = typeof tls.getCACertificates === 'function'",
    "  ? tls.getCACertificates('default').map((pem) => new X509Certificate(pem).fingerprint256)",
    "  : null;",
    "process.stdout.write(",
    "  JSON.stringify({",
    "    argv: process.argv,",
    "    importedAt,",
    "    wroteAt: Date.now(),",
    "    locksAtImport,",
    "    caDefault,",
    "    execPath: process.execPath,",
    "    pid: process.pid,",
    "    ppid: process.ppid,",
    "    runAsNode: process.env.ELECTRON_RUN_AS_NODE ?? null,",
    "    nodeExtraCaCerts: process.env.NODE_EXTRA_CA_CERTS ?? null,",
    "    mehoCaCert: process.env.MEHO_CA_CERT ?? null,",
    "    mehoClientId: process.env.MEHO_MCP_CLIENT_ID ?? null,",
    "    mehoScopes: process.env.MEHO_MCP_SCOPES ?? null,",
    "    mehoTenant: process.env.MEHO_TENANT ?? null,",
    "  }) + '\\n',",
    ");",
    "// STUB_REPORT_AFTER_WRITE: report the locks again after the first write.",
    "if (process.env.STUB_REPORT_AFTER_WRITE) {",
    "  process.stdout.write(JSON.stringify({ locksAfterWrite: locks() }) + '\\n');",
    "}",
    "process.exit(0);",
    "",
  ].join("\n"),
);

const CA_FIXTURE = join(BUNDLE, "internal-ca.pem");
writeFileSync(CA_FIXTURE, TEST_CA_PEM);

const INDEX = join(BUNDLE, "server", "index.mjs");
// require.resolve inside the launcher returns the realpath, so compare
// against the realpath here (macOS maps /var → /private/var via a symlink).
const STUB_ENTRY = realpathSync(join(STUB_DIR, "dist", "proxy.js"));

// Preloaded with --import to simulate a Node that predates
// tls.setDefaultCACertificates, so the fallback path runs on every Node.
const HIDE_INJECT_API = join(BUNDLE, "hide-inject-api.mjs");
writeFileSync(
  HIDE_INJECT_API,
  'import tls from "node:tls";\ndelete tls.setDefaultCACertificates;\n',
);
const WITHOUT_INJECT_API = ["--import", pathToFileURL(HIDE_INJECT_API).href];

after(() => rmSync(BUNDLE, { recursive: true, force: true }));

// Run the launcher with a controlled env; PATH defaults to the minimal set.
// The launcher is a direct child of this test process, so a diagnostic whose
// ppid equals our pid proves mcp-remote ran in the launcher (in-process),
// not in a grandchild the launcher spawned.
function runLauncher(args, extraEnv = {}, nodeArgs = []) {
  return spawnSync(process.execPath, [...nodeArgs, INDEX, ...args], {
    encoding: "utf8",
    env: { PATH: MINIMAL_PATH, MCP_REMOTE_CONFIG_DIR: AUTH_DIR, ...extraEnv },
  });
}

// Like runLauncher, but does not block, so several launchers can run at once.
function startLauncher(args, extraEnv = {}) {
  const child = spawn(process.execPath, [INDEX, ...args], {
    env: { PATH: MINIMAL_PATH, MCP_REMOTE_CONFIG_DIR: AUTH_DIR, ...extraEnv },
  });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (d) => (stdout += d));
  child.stderr.on("data", (d) => (stderr += d));
  const done = new Promise((resolve) =>
    child.on("close", (status) => resolve({ status, stdout, stderr })),
  );
  return { child, done, output: () => ({ stdout, stderr }) };
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
  // offline_access makes the login last past the realm's SSO idle limit.
  assert.deepEqual(JSON.parse(diag.argv[6]), {
    scope: "mcp:read mcp:execute offline_access",
  });
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

test("CA: NODE_EXTRA_CA_CERTS (the older-Node fallback) is passed through untouched", () => {
  // Node reads NODE_EXTRA_CA_CERTS once at startup, before launcher code
  // runs; on a Node without tls.setDefaultCACertificates the manifest's env
  // delivery of it is the only CA route. So the launcher must leave whatever
  // value it was started with intact — a non-empty path reaches mcp-remote,
  // and an empty value stays empty, which Node treats as "no extra certs"
  // (proven in #3341's spike).
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

test("manifest: ca_cert is argv[3] of the launcher, with NODE_EXTRA_CA_CERTS kept as the fallback", () => {
  // The launcher reads the CA path from argv[3]; reordering the manifest
  // args would silently drop the CA.
  const { args, env } = MANIFEST.server.mcp_config;
  assert.deepEqual(args, [
    "${__dirname}/server/index.mjs",
    "${user_config.backplane_url}",
    "${user_config.ca_cert}",
  ]);
  assert.equal(env.NODE_EXTRA_CA_CERTS, "${user_config.ca_cert}");
});

test("CA (F7): the ca_cert argument is injected into the default CA list in-process, before mcp-remote loads", { skip: NO_INJECT_API }, () => {
  // Plain Node, and Node with the OS store enabled the way Claude Desktop
  // runs it (NODE_USE_SYSTEM_CA=1): in both, the CA is added and nothing
  // that was trusted before (bundled roots, OS-store roots) is dropped.
  for (const env of [{}, { NODE_USE_SYSTEM_CA: "1" }]) {
    const baseline = runLauncher([URL_ARG], env);
    assert.equal(baseline.status, 0, baseline.stderr);
    const before = JSON.parse(baseline.stdout).caDefault;
    assert.ok(!before.includes(CA_FINGERPRINT), "fixture CA must not be trusted by default");

    const res = runLauncher([URL_ARG, CA_FIXTURE], env);
    assert.equal(res.status, 0, res.stderr);
    assert.equal(res.stderr, "");
    const diag = JSON.parse(res.stdout);
    assert.ok(
      diag.caDefault.includes(CA_FINGERPRINT),
      "mcp-remote must see the internal CA in the default CA list at import time",
    );
    const trusted = new Set(diag.caDefault);
    assert.deepEqual(
      before.filter((fp) => !trusted.has(fp)),
      [],
      "injection must append to the default CA list, never replace it",
    );
    // The CA path is consumed by the launcher, not forwarded to mcp-remote.
    assert.equal(diag.argv.length, 7);
    assert.equal(diag.argv[3], "--static-oauth-client-info");
  }
});

test("CA: an empty, whitespace, or unsubstituted ${user_config.ca_cert} argument means no CA", () => {
  for (const value of ["", "   ", "${user_config.ca_cert}"]) {
    const res = runLauncher([URL_ARG, value]);
    assert.equal(res.status, 0, `${JSON.stringify(value)}: ${res.stderr}`);
    assert.equal(res.stderr, "", JSON.stringify(value));
    const diag = JSON.parse(res.stdout);
    assert.equal(diag.argv.length, 7);
    if (diag.caDefault) {
      assert.ok(!diag.caDefault.includes(CA_FINGERPRINT));
    }
  }
});

test("CA: a missing CA file fails fast before importing mcp-remote", { skip: NO_INJECT_API }, () => {
  const missing = join(BUNDLE, "no-such-ca.pem");
  const res = runLauncher([URL_ARG, missing]);
  assert.equal(res.status, 1);
  assert.equal(res.stdout, ""); // never reached mcp-remote
  assert.match(res.stderr, /cannot load the internal CA bundle/);
  assert.ok(res.stderr.includes(missing), res.stderr);
});

test("CA: a typed path with a leading ~ resolves to the home folder; other paths are unchanged", { skip: NO_INJECT_API }, () => {
  // Desktop's file field stores a typed path as typed, and Node does not
  // expand `~`. HOME (USERPROFILE on Windows) decides os.homedir().
  const home = join(BUNDLE, "home");
  mkdirSync(home, { recursive: true });
  writeFileSync(join(home, "internal-ca.pem"), TEST_CA_PEM);
  const homeEnv = { HOME: home, USERPROFILE: home };

  const tilde = runLauncher([URL_ARG, "~/internal-ca.pem"], homeEnv);
  assert.equal(tilde.status, 0, tilde.stderr);
  assert.equal(tilde.stderr, "");
  assert.ok(JSON.parse(tilde.stdout).caDefault.includes(CA_FINGERPRINT));

  // A missing file under ~ names the expanded path, so the error shows
  // where the launcher looked.
  const missing = runLauncher([URL_ARG, "~/no-such-ca.pem"], homeEnv);
  assert.equal(missing.status, 1);
  assert.equal(missing.stdout, "");
  assert.ok(missing.stderr.includes(`${home}/no-such-ca.pem`), missing.stderr);

  // An absolute path is used as given.
  const absolute = runLauncher([URL_ARG, CA_FIXTURE], homeEnv);
  assert.equal(absolute.status, 0, absolute.stderr);
  assert.ok(JSON.parse(absolute.stdout).caDefault.includes(CA_FINGERPRINT));

  // `~name/…` (another user's home) is not expanded.
  const otherUser = runLauncher([URL_ARG, "~other/internal-ca.pem"], homeEnv);
  assert.equal(otherUser.status, 1);
  assert.ok(otherUser.stderr.includes("bundle ~other/internal-ca.pem:"), otherUser.stderr);
});

test("CA: a file without a valid PEM certificate fails fast", { skip: NO_INJECT_API }, () => {
  const noPem = join(BUNDLE, "not-a-cert.pem");
  writeFileSync(noPem, "this is not a certificate\n");
  const noPemRes = runLauncher([URL_ARG, noPem]);
  assert.equal(noPemRes.status, 1);
  assert.equal(noPemRes.stdout, "");
  assert.match(noPemRes.stderr, /no PEM certificate found/);

  const malformed = join(BUNDLE, "malformed.pem");
  writeFileSync(malformed, "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n");
  const malformedRes = runLauncher([URL_ARG, malformed]);
  assert.equal(malformedRes.status, 1);
  assert.equal(malformedRes.stdout, "");
  assert.match(malformedRes.stderr, /cannot load the internal CA bundle/);
});

test("CA fallback: without tls.setDefaultCACertificates the launcher relies on NODE_EXTRA_CA_CERTS", () => {
  // Nothing delivered at boot: warn with the remedy, but still launch — the
  // root may already be trusted in the OS store.
  const bare = runLauncher([URL_ARG, CA_FIXTURE], {}, WITHOUT_INJECT_API);
  assert.equal(bare.status, 0, bare.stderr);
  assert.match(bare.stderr, /cannot add the internal CA bundle in-process/);
  assert.match(bare.stderr, /OS trust store/);
  assert.equal(JSON.parse(bare.stdout).argv[2], URL_ARG);

  // Delivered at boot (a host that honours the manifest env): Node loaded
  // it before the launcher ran, so there is nothing to warn about.
  const viaEnv = runLauncher(
    [URL_ARG, CA_FIXTURE],
    { NODE_EXTRA_CA_CERTS: CA_FIXTURE },
    WITHOUT_INJECT_API,
  );
  assert.equal(viaEnv.status, 0, viaEnv.stderr);
  assert.equal(viaEnv.stderr, "");
  const diag = JSON.parse(viaEnv.stdout);
  assert.equal(diag.nodeExtraCaCerts, CA_FIXTURE);
  if (diag.caDefault) {
    assert.ok(diag.caDefault.includes(CA_FINGERPRINT));
  }
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
      scope: "mcp:read mcp:execute offline_access",
    });
  }
});

// The current tenant (#5242): the `tenant` setting becomes the Meho-Tenant
// header that MEHO reads (backend auth/current_tenant.py TENANT_HEADER).
test("tenant: the tenant setting is sent as the Meho-Tenant header and MEHO_TENANT scrubbed", () => {
  const res = runLauncher(["https://meho.internal.example/mcp"], {
    MEHO_TENANT: "  tenant-b  ",
  });
  assert.equal(res.status, 0, res.stderr);
  const diag = JSON.parse(res.stdout);
  assert.deepEqual(diag.argv.slice(7), ["--header", "Meho-Tenant: tenant-b"]);
  assert.equal(diag.argv[2], "https://meho.internal.example/mcp");
  assert.equal(diag.mehoTenant, null); // scrubbed from mcp-remote's env
});

test("tenant: an empty, whitespace, or unsubstituted ${user_config.tenant} sends no header", () => {
  for (const value of [undefined, "", "   ", "${user_config.tenant}"]) {
    const env = value === undefined ? {} : { MEHO_TENANT: value };
    const res = runLauncher(["https://meho.internal.example/mcp"], env);
    assert.equal(res.status, 0, res.stderr);
    const diag = JSON.parse(res.stdout);
    assert.equal(diag.argv.length, 7, `value ${JSON.stringify(value)}: ${diag.argv}`);
    assert.ok(!diag.argv.includes("--header"));
    assert.equal(diag.mehoTenant, null);
  }
});

test("manifest: the optional tenant setting is delivered as MEHO_TENANT", () => {
  const { tenant } = MANIFEST.user_config;
  assert.equal(tenant.type, "string");
  assert.equal(tenant.required, false);
  assert.equal(tenant.default, undefined, "no default: one tenant needs no setting");
  assert.equal(MANIFEST.server.mcp_config.env.MEHO_TENANT, "${user_config.tenant}");
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

// --- One sign-in at a time (#3857) -----------------------------------------

const { acquireSignInLock, signInLockPath } = await import(
  pathToFileURL(join(BUNDLE, "server", "signin-lock.mjs")).href
);
const LOCK = signInLockPath(new URL(URL_ARG).href, { MCP_REMOTE_CONFIG_DIR: AUTH_DIR });

function writeLock(pid) {
  mkdirSync(AUTH_DIR, { recursive: true });
  writeFileSync(LOCK, JSON.stringify({ pid, startedAt: new Date().toISOString() }));
}

function lockFiles() {
  try {
    return readdirSync(AUTH_DIR).filter((f) => f.endsWith(".lock"));
  } catch {
    return [];
  }
}

// The second names copies give the lock while they take it over.
function claimFiles() {
  try {
    return readdirSync(AUTH_DIR).filter((f) => f.includes(".lock.takeover."));
  } catch {
    return [];
  }
}

// A second name as another copy (pid) would make it.
function claimOf(pid) {
  return `${LOCK}.takeover.${pid}.0badf00d`;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test("sign-in lock: held while mcp-remote connects, released at its first message", () => {
  const res = runLauncher([URL_ARG], { STUB_REPORT_AFTER_WRITE: "1" });
  assert.equal(res.status, 0, res.stderr);
  assert.equal(res.stderr, "");
  const [first, second] = res.stdout.trim().split("\n").map((l) => JSON.parse(l));
  assert.equal(first.locksAtImport.length, 1, "the lock must be held when mcp-remote loads");
  assert.deepEqual(second.locksAfterWrite, [], "the first message must release the lock");
  assert.deepEqual(lockFiles(), []);
});

test("sign-in lock: two launchers started together never sign in at the same time", async () => {
  const env = { STUB_CONNECT_MS: "400" };
  const runs = await Promise.all([
    startLauncher([URL_ARG], env).done,
    startLauncher([URL_ARG], env).done,
  ]);
  for (const r of runs) assert.equal(r.status, 0, r.stderr);
  const [a, b] = runs
    .map((r) => JSON.parse(r.stdout))
    .sort((x, y) => x.importedAt - y.importedAt);
  assert.ok(
    b.importedAt >= a.wroteAt,
    `the second copy started mcp-remote at ${b.importedAt}, before the first was connected at ${a.wroteAt}`,
  );
  const waited = runs.filter((r) => /is signing in; waiting for it to finish/.test(r.stderr));
  assert.equal(waited.length, 1, "exactly one copy must wait");
  assert.match(waited[0].stderr, /the other MEHO instance finished; starting/);
  assert.deepEqual(lockFiles(), []);
});

test("sign-in lock: waits while a running instance holds the lock, then starts", async () => {
  writeLock(process.pid); // this test runner is alive
  try {
    const run = startLauncher([URL_ARG]);
    await sleep(800);
    assert.equal(run.output().stdout, "", "must not start mcp-remote while the lock is held");
    assert.match(run.output().stderr, new RegExp(`instance \\(pid ${process.pid}\\) is signing in`));
    rmSync(LOCK);
    const res = await run.done;
    assert.equal(res.status, 0, res.stderr);
    assert.equal(JSON.parse(res.stdout).argv[2], URL_ARG);
  } finally {
    rmSync(LOCK, { force: true });
  }
});

test("sign-in lock: a lock left by a stopped instance is taken over", () => {
  const stopped = spawnSync(process.execPath, ["-e", ""]).pid;
  writeLock(stopped);
  const res = runLauncher([URL_ARG]);
  assert.equal(res.status, 0, res.stderr);
  assert.match(res.stderr, /found an old sign-in lock: its instance \(pid \d+\) stopped; taking it over/);
  assert.equal(JSON.parse(res.stdout).locksAtImport.length, 1);
  assert.deepEqual(lockFiles(), []);
});

test("sign-in lock: a lock older than the wait limit is taken over", () => {
  writeLock(process.pid);
  const old = new Date(Date.now() - 6 * 60 * 1000);
  utimesSync(LOCK, old, old);
  const res = runLauncher([URL_ARG]);
  assert.equal(res.status, 0, res.stderr);
  assert.match(res.stderr, /found an old sign-in lock: it is older than five minutes; taking it over/);
  assert.deepEqual(lockFiles(), []);
});

// Two copies can read the same stale lock at the same moment. This forces
// that sequence in one process: the copy under test reads the stale lock and
// announces the takeover; at that moment "the other copy" (this test) wins
// the race and replaces the stale lock with its own live lock. The copy under
// test must leave that lock alone and wait.
test("regression: a copy that lost the stale-lock race must not delete the winner's lock", async () => {
  const stopped = spawnSync(process.execPath, ["-e", ""]).pid;
  writeLock(stopped);
  try {
    const said = [];
    let raced = false;
    const say = (t) => {
      said.push(t);
      if (!raced && /an old sign-in lock/.test(t)) {
        raced = true;
        rmSync(LOCK);
        writeLock(process.pid); // the other copy's new, live lock
      }
    };
    let settled = false;
    const pending = acquireSignInLock(new URL(URL_ARG).href, {
      env: { MCP_REMOTE_CONFIG_DIR: AUTH_DIR },
      say,
      waitLimitMs: 10_000,
      pollMs: 50,
    }).then((release) => {
      settled = true;
      return release;
    });
    await sleep(500);
    assert.ok(raced, "the test must have replaced the stale lock");
    assert.equal(settled, false, "must wait while the other copy signs in");
    assert.equal(
      JSON.parse(readFileSync(LOCK, "utf8")).pid,
      process.pid,
      "the other copy's lock must stay in place",
    );
    assert.match(said.join("\n"), /waiting for it to finish/);
    assert.deepEqual(lockFiles(), [basename(LOCK)], "no second lock may be left behind");
    assert.deepEqual(claimFiles(), [], "no second name may be left behind");
    rmSync(LOCK); // the other copy finished
    const release = await pending;
    assert.match(said.join("\n"), /the other MEHO instance finished; starting/);
    assert.equal(JSON.parse(readFileSync(LOCK, "utf8")).pid, process.pid);
    release();
    assert.deepEqual(lockFiles(), []);
  } finally {
    rmSync(LOCK, { force: true });
  }
});

// The takeover gives the stale lock a second name first. While a name made
// by a running copy exists, that copy is in the middle of its takeover; a
// new copy waits for it and then sees the live lock the other copy wrote.
test("sign-in lock: waits while another copy is taking over a stale lock", async () => {
  const stopped = spawnSync(process.execPath, ["-e", ""]).pid;
  writeLock(stopped);
  const claim = claimOf(process.pid); // this test runner is alive
  linkSync(LOCK, claim);
  try {
    const run = startLauncher([URL_ARG]);
    await sleep(800);
    assert.equal(run.output().stdout, "", "must not start mcp-remote during another takeover");
    assert.match(run.output().stderr, /found an old sign-in lock: its instance \(pid \d+\) stopped/);
    assert.match(run.output().stderr, /taking over the old sign-in lock; waiting for it to finish/);
    writeLock(process.pid); // the other copy finishes its takeover...
    rmSync(claim);
    await sleep(500);
    assert.equal(run.output().stdout, "", "must keep waiting for the live lock");
    rmSync(LOCK); // ...and later releases the lock
    const res = await run.done;
    assert.equal(res.status, 0, res.stderr);
    assert.equal(JSON.parse(res.stdout).argv[2], URL_ARG);
    assert.deepEqual(lockFiles(), []);
    assert.deepEqual(claimFiles(), []);
  } finally {
    rmSync(claim, { force: true });
    rmSync(LOCK, { force: true });
  }
});

test("sign-in lock: a second name left by a stopped copy does not block the takeover", async () => {
  const stopped = spawnSync(process.execPath, ["-e", ""]).pid;
  writeLock(stopped);
  const claim = claimOf(stopped);
  linkSync(LOCK, claim);
  try {
    const said = [];
    const release = await acquireSignInLock(new URL(URL_ARG).href, {
      env: { MCP_REMOTE_CONFIG_DIR: AUTH_DIR },
      say: (t) => said.push(t),
      waitLimitMs: 10_000,
      pollMs: 50,
    });
    assert.match(said.join("\n"), /found an old sign-in lock: its instance \(pid \d+\) stopped; taking it over/);
    assert.equal(JSON.parse(readFileSync(LOCK, "utf8")).pid, process.pid);
    assert.ok(!existsSync(claim), "the old second name must be gone");
    release();
    assert.deepEqual(lockFiles(), []);
    assert.deepEqual(claimFiles(), []);
  } finally {
    rmSync(claim, { force: true });
    rmSync(LOCK, { force: true });
  }
});

// Two copies can find the same second name left by a stopped copy. In the
// old code both read it, one removed it and made a new second name, and the
// other then removed that new name with its earlier read. This forces that
// sequence in one process: the copy under test finds the stale lock and the
// abandoned name; at that moment "the other copy" (this test) has already
// removed the abandoned name and made its own, and is in the middle of its
// takeover. The copy under test must clean up only the abandoned name, must
// not touch the other copy's name or the lock, and must wait.
test("regression: a copy must not remove the second name another copy just made", async () => {
  const stopped = spawnSync(process.execPath, ["-e", ""]).pid;
  writeLock(stopped);
  const stale = readFileSync(LOCK, "utf8");
  const abandoned = claimOf(stopped);
  linkSync(LOCK, abandoned);
  // The other copy runs in its own process. (The copy under test runs in
  // this process, and a copy cleans up names with its own process id.)
  const otherCopy = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], { stdio: "ignore" });
  const other = claimOf(otherCopy.pid);
  try {
    const said = [];
    let raced = false;
    const say = (t) => {
      said.push(t);
      if (!raced && /an old sign-in lock/.test(t)) {
        raced = true;
        linkSync(LOCK, other); // the other copy's fresh second name
      }
    };
    let settled = false;
    const pending = acquireSignInLock(new URL(URL_ARG).href, {
      env: { MCP_REMOTE_CONFIG_DIR: AUTH_DIR },
      say,
      waitLimitMs: 10_000,
      pollMs: 50,
    }).then((release) => {
      settled = true;
      return release;
    });
    await sleep(500);
    assert.ok(raced, "the test must have made the other copy's second name");
    assert.equal(settled, false, "must wait while the other copy takes over");
    assert.ok(!existsSync(abandoned), "the abandoned second name must be gone");
    assert.ok(existsSync(other), "the other copy's second name must stay");
    assert.equal(readFileSync(LOCK, "utf8"), stale, "the lock must be left to the other copy");
    assert.deepEqual(claimFiles(), [basename(other)], "no further second name may be left behind");
    assert.match(said.join("\n"), /taking over the old sign-in lock; waiting for it to finish/);
    writeLock(process.pid); // the other copy finishes its takeover...
    rmSync(other);
    await sleep(300);
    assert.equal(settled, false, "must keep waiting for the live lock");
    rmSync(LOCK); // ...and later releases the lock
    const release = await pending;
    assert.match(said.join("\n"), /the other MEHO instance finished; starting/);
    assert.equal(JSON.parse(readFileSync(LOCK, "utf8")).pid, process.pid);
    release();
    assert.deepEqual(lockFiles(), []);
    assert.deepEqual(claimFiles(), []);
  } finally {
    otherCopy.kill();
    rmSync(other, { force: true });
    rmSync(abandoned, { force: true });
    rmSync(LOCK, { force: true });
  }
});

test("sign-in lock: after the wait limit the launcher starts anyway", async () => {
  writeLock(process.pid);
  try {
    const said = [];
    const release = await acquireSignInLock(new URL(URL_ARG).href, {
      env: { MCP_REMOTE_CONFIG_DIR: AUTH_DIR },
      say: (t) => said.push(t),
      waitLimitMs: 300,
      pollMs: 50,
    });
    assert.match(said.join("\n"), /still signing in after 0.3 seconds; starting anyway/);
    release();
    // Not ours, so it must still be there.
    assert.equal(JSON.parse(readFileSync(LOCK, "utf8")).pid, process.pid);
  } finally {
    rmSync(LOCK, { force: true });
  }
});

test("sign-in lock: release only removes this process's own lock", async () => {
  const env = { MCP_REMOTE_CONFIG_DIR: AUTH_DIR };
  const release = await acquireSignInLock(new URL(URL_ARG).href, { env, say: () => {} });
  assert.equal(JSON.parse(readFileSync(LOCK, "utf8")).pid, process.pid);
  writeLock(process.pid + 1); // another instance took over meanwhile
  release();
  assert.equal(JSON.parse(readFileSync(LOCK, "utf8")).pid, process.pid + 1);
  rmSync(LOCK);
  release(); // safe to call twice
});

// Once a lock is older than five minutes, another copy may take it over
// while its owner releases it. The old release read its own pid and then
// deleted the file; a takeover that finished in between lost its lock. This
// forces that sequence: when the owner releases, "the other copy" (a running
// helper process) has already given the lock its second name and is about
// to write its pid. The owner must leave the lock alone, the other copy's
// takeover must land, and a later release must still leave it alone.
test("regression: release must not delete a lock another copy is taking over", async () => {
  const env = { MCP_REMOTE_CONFIG_DIR: AUTH_DIR };
  const release = await acquireSignInLock(new URL(URL_ARG).href, { env, say: () => {} });
  const old = new Date(Date.now() - 6 * 60 * 1000);
  utimesSync(LOCK, old, old); // the sign-in took longer than five minutes
  const otherCopy = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], { stdio: "ignore" });
  const other = claimOf(otherCopy.pid);
  linkSync(LOCK, other); // the other copy's takeover is in progress
  try {
    release();
    assert.deepEqual(lockFiles(), [basename(LOCK)], "the lock must stay during the takeover");
    assert.equal(JSON.parse(readFileSync(LOCK, "utf8")).pid, process.pid);
    assert.deepEqual(claimFiles(), [basename(other)], "only the other copy's second name may remain");
    const taken = JSON.stringify({ pid: otherCopy.pid, startedAt: new Date().toISOString() });
    writeFileSync(other, taken); // the other copy finishes its takeover
    rmSync(other);
    assert.equal(readFileSync(LOCK, "utf8"), taken, "the other copy's lock must be in place");
    release(); // the exit listener's turn: not ours any more
    assert.equal(readFileSync(LOCK, "utf8"), taken);
    assert.deepEqual(claimFiles(), []);
  } finally {
    otherCopy.kill();
    rmSync(other, { force: true });
    rmSync(LOCK, { force: true });
  }
});

test("sign-in lock: an unusable lock folder does not stop the launcher", () => {
  const notADir = join(BUNDLE, "not-a-dir");
  writeFileSync(notADir, "");
  const res = runLauncher([URL_ARG], { MCP_REMOTE_CONFIG_DIR: notADir });
  assert.equal(res.status, 0, res.stderr);
  assert.match(res.stderr, /cannot use the sign-in lock .*; starting without it/);
  assert.equal(JSON.parse(res.stdout).argv[2], URL_ARG);
});
