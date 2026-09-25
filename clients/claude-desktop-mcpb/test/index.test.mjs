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
// before mcp-remote loaded (F7).

import { spawnSync } from "node:child_process";
import { X509Certificate } from "node:crypto";
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
import tls from "node:tls";
import { fileURLToPath, pathToFileURL } from "node:url";
import { after, test } from "node:test";
import assert from "node:assert/strict";

const HERE = dirname(fileURLToPath(import.meta.url));
const REAL_INDEX = join(HERE, "..", "server", "index.mjs");
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
    "import { X509Certificate } from 'node:crypto';",
    "import tls from 'node:tls';",
    "const caDefault = typeof tls.getCACertificates === 'function'",
    "  ? tls.getCACertificates('default').map((pem) => new X509Certificate(pem).fingerprint256)",
    "  : null;",
    "process.stdout.write(",
    "  JSON.stringify({",
    "    argv: process.argv,",
    "    caDefault,",
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
