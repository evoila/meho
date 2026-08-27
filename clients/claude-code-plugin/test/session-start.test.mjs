// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Stubbed-`meho` behavioral suite for bin/session-start.sh — the SessionStart
// reflex hook. The hook shells out to the `meho` CLI four times (status gate,
// audit recent, memory list, kb list) and prints a compact digest to stdout
// that Claude Code injects as session context.
//
// `meho` is stubbed: a fake `meho` on a controlled PATH dispatches on its
// first arg and prints a per-subcommand fixture the test writes. No live
// backplane, no CLI build. The fixtures let the suite assert the one contract
// this task pins — the recent-activity window excludes `__sensor__`
// (sensor-principal) rows and stays full of real activity via
// filter-then-truncate — plus the hook's fail-open guarantees.

import { spawnSync } from "node:child_process";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { after, test } from "node:test";
import assert from "node:assert/strict";

const HERE = dirname(fileURLToPath(import.meta.url));
const HOOK = join(HERE, "..", "bin", "session-start.sh");

// The hook budget: `head -n 12` keeps one table header + 11 data rows.
const WINDOW_ROWS = 11;

// One temp dir per suite holds the stub `meho` plus the per-subcommand
// fixture files it cats. /usr/bin:/bin keeps sh/grep/head/cat/env/bash (and
// coreutils `timeout` on the Linux CI runner) resolvable under the stub PATH.
const STUB_DIR = mkdtempSync(join(tmpdir(), "meho-session-start-test-"));
writeFileSync(
  join(STUB_DIR, "meho"),
  [
    "#!/usr/bin/env bash",
    // Fixture dir is passed by env, not derived from $0: PATH resolution
    // (and `timeout`) exec the stub with argv[0]=\"meho\", so $0 is not a path.
    'd="${MEHO_STUB_DIR:-/nonexistent}"',
    'case "$1" in',
    '  status) [ -f "$d/status.txt" ] && cat "$d/status.txt" ;;',
    '  audit)  [ -f "$d/audit.txt" ]  && cat "$d/audit.txt" ;;',
    '  list)   [ -f "$d/list.txt" ]   && cat "$d/list.txt" ;;',
    '  kb)     [ -f "$d/kb.txt" ]     && cat "$d/kb.txt" ;;',
    "esac",
    "exit 0",
    "",
  ].join("\n"),
);
chmodSync(join(STUB_DIR, "meho"), 0o755);
const PATH_WITH_STUB = `${STUB_DIR}:/usr/bin:/bin`;

after(() => rmSync(STUB_DIR, { recursive: true, force: true }));

// Render one `meho audit recent` table row the way the Go CLI's
// printQueryTable does (space-padded columns). Only the PRINCIPAL column is
// load-bearing for the filter; the rest is realistic filler.
function auditRow(principal, opID) {
  return [
    "2026-08-27T12:00:00Z ",
    principal.padEnd(12),
    "vcenter-01".padEnd(18),
    opID.padEnd(26),
    "read".padEnd(16),
    "ok",
  ].join(" ");
}

// A page shaped like the field-test pathology: a burst of sensor heartbeats
// FIRST, then the real operator/agent activity. A naive `--limit 10 | head`
// (filter-after-truncate) would surface only sensor rows; filter-then-
// truncate must instead fill the window with the op-NN rows that trail them.
function mixedAuditPage(sensorCount, realCount) {
  const lines = [
    "TIME                   PRINCIPAL    TARGET             OP_ID                      CLASS            STATUS",
  ];
  for (let i = 0; i < sensorCount; i++) {
    lines.push(auditRow("__sensor__", "checks.sensor.eval"));
  }
  for (let n = 1; n <= realCount; n++) {
    const id = String(n).padStart(2, "0");
    lines.push(auditRow(`op-${id}`, "vsphere.vm.list"));
  }
  return lines.join("\n") + "\n";
}

// Write the per-subcommand fixtures the stub cats. Absent keys => that
// subcommand prints nothing (drives the fail-open / empty-section paths).
function seedFixtures({ status, audit, list, kb }) {
  for (const name of ["status", "audit", "list", "kb"]) {
    rmSync(join(STUB_DIR, `${name}.txt`), { force: true });
  }
  if (status !== undefined) writeFileSync(join(STUB_DIR, "status.txt"), status);
  if (audit !== undefined) writeFileSync(join(STUB_DIR, "audit.txt"), audit);
  if (list !== undefined) writeFileSync(join(STUB_DIR, "list.txt"), list);
  if (kb !== undefined) writeFileSync(join(STUB_DIR, "kb.txt"), kb);
}

function runHook(pathOverride = PATH_WITH_STUB) {
  return spawnSync(HOOK, [], {
    encoding: "utf8",
    env: { PATH: pathOverride, MEHO_STUB_DIR: STUB_DIR },
  });
}

test("recent-activity window drops __sensor__ rows and fills with real activity", () => {
  // 20 sensor rows precede 15 real rows: without filter-then-truncate the
  // window would be all heartbeats.
  seedFixtures({
    status: "operator=tester  backplane=https://meho.example  health=ok\n",
    audit: mixedAuditPage(20, 15),
  });
  const res = runHook();
  assert.equal(res.status, 0, `hook exited non-zero: ${res.stderr}`);

  // (1) No sensor-principal row survives into the injected digest.
  assert.ok(
    !res.stdout.includes("__sensor__"),
    `sensor rows leaked into the digest:\n${res.stdout}`,
  );

  // (2) The window filled with real rows despite the sensor burst leading the
  // page — filter happened BEFORE the truncation, not after.
  assert.match(res.stdout, /op-01\b/, `op-01 absent:\n${res.stdout}`);
  assert.match(
    res.stdout,
    new RegExp(`op-${String(WINDOW_ROWS).padStart(2, "0")}\\b`),
    `window did not fill to the budget (op-${WINDOW_ROWS} absent):\n${res.stdout}`,
  );

  // (3) Truncation still bounds the window: the 12th real row is cut
  // (header + 11 rows = 12 lines), so it is filter-THEN-truncate, not a
  // filter that forgot to cap.
  assert.ok(
    !res.stdout.includes(`op-${String(WINDOW_ROWS + 1).padStart(2, "0")}`),
    `window exceeded the ${WINDOW_ROWS}-row budget:\n${res.stdout}`,
  );
});

test("real activity shorter than the budget survives intact (no sensor rows)", () => {
  seedFixtures({
    status: "health=ok\n",
    audit: mixedAuditPage(5, 3),
  });
  const res = runHook();
  assert.equal(res.status, 0, res.stderr);
  assert.ok(!res.stdout.includes("__sensor__"), res.stdout);
  for (const id of ["op-01", "op-02", "op-03"]) {
    assert.ok(res.stdout.includes(id), `${id} absent:\n${res.stdout}`);
  }
});

test("the other digest sections are untouched by the activity filter", () => {
  seedFixtures({
    status: "health=ok\n",
    audit: mixedAuditPage(3, 2),
    list: "memory-note-alpha\nmemory-note-beta\n",
    kb: "kb-entry-gamma\nkb-entry-delta\n",
  });
  const res = runHook();
  assert.equal(res.status, 0, res.stderr);
  // Activity window filtered as before.
  assert.ok(!res.stdout.includes("__sensor__"), res.stdout);
  assert.match(res.stdout, /op-01\b/, res.stdout);
  // Memory + knowledge sections render verbatim (the filter is scoped to the
  // activity window only — it never touches these two).
  assert.match(res.stdout, /## Your MEHO memory \(scoped\)/, res.stdout);
  assert.ok(res.stdout.includes("memory-note-alpha"), res.stdout);
  assert.match(res.stdout, /## Recent MEHO knowledge/, res.stdout);
  assert.ok(res.stdout.includes("kb-entry-gamma"), res.stdout);
});

test("fail-open: no `meho` on PATH => silent, exit 0", () => {
  // A PATH without the stub dir: the hook's `command -v meho` guard fires.
  const res = runHook("/usr/bin:/bin");
  assert.equal(res.status, 0, res.stderr);
  assert.equal(res.stdout, "", `expected no output, got:\n${res.stdout}`);
});

test("fail-open: empty `meho status` gate => silent, exit 0", () => {
  // status.txt absent => the status gate reads empty => hook exits before the
  // activity/memory/knowledge reads, printing nothing.
  seedFixtures({ audit: mixedAuditPage(3, 5) });
  const res = runHook();
  assert.equal(res.status, 0, res.stderr);
  assert.equal(res.stdout, "", `expected no output, got:\n${res.stdout}`);
});

test("all-sensor page => no sensor rows leak (window may be header-only)", () => {
  // Degenerate tenant: every recent row is a heartbeat. The contract that
  // must hold is that no `__sensor__` row is ever injected as context.
  seedFixtures({
    status: "health=ok\n",
    audit: mixedAuditPage(30, 0),
  });
  const res = runHook();
  assert.equal(res.status, 0, res.stderr);
  assert.ok(
    !res.stdout.includes("__sensor__"),
    `sensor rows leaked on an all-sensor page:\n${res.stdout}`,
  );
});
