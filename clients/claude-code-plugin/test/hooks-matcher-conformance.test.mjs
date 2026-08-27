// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Static conformance guard for the plugin's hooks.json matchers.
//
// A plugin-bundled MCP tool is matched as `mcp__plugin_meho_meho__<tool>`,
// where `<tool>` must be the tool's *exact* registered name. The prefix
// asymmetry is a trap: some MEHO tools carry the `meho_` prefix
// (`meho_broadcast_announce`) and some do not (`call_operation`), so a
// matcher that names `broadcast_announce` compiles fine yet matches nothing
// — the hook silently never fires (field-test finding F3, #3143).
//
// This guard cross-checks, statically and in-repo, that every
// `mcp__plugin_meho_meho__<tool>` matcher in hooks.json names a `<tool>`
// that is actually registered under
// `backend/src/meho_backplane/mcp/tools/`. No live server, no MCP round
// trip: the registered set is derived from the `register_mcp_tool(...)`
// call sites the same way the deploy does — from source.

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import assert from "node:assert/strict";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(HERE, "..", "..", "..");
const HOOKS_JSON = join(HERE, "..", "hooks", "hooks.json");
const TOOLS_DIR = join(
  REPO_ROOT,
  "backend",
  "src",
  "meho_backplane",
  "mcp",
  "tools",
);

// The plugin and its bundled MCP server are both named `meho`, so a
// plugin-scoped tool name is `mcp__plugin_meho_meho__<tool>`. A tool name
// is a lowercase snake identifier; the character class stops naturally at a
// regex metachar, so a wildcard matcher (`...__.*`) captures nothing and is
// correctly ignored (it is a server-wide match, not a single-tool claim).
const SCOPED_MATCHER = /mcp__plugin_meho_meho__([a-z][a-z0-9_]*)/g;

// Collect every `matcher` string across all hook events in hooks.json.
function matcherStrings(hooksDoc) {
  const out = [];
  for (const events of Object.values(hooksDoc.hooks ?? {})) {
    for (const group of events ?? []) {
      if (typeof group.matcher === "string") out.push(group.matcher);
    }
  }
  return out;
}

// Derive the set of registered MCP tool names from the tool source files.
// Registration is `register_mcp_tool(definition=ToolDefinition(... name=X
// ...))`, where X is either a string literal or a module-level
// `Final[str]` constant (e.g. audit.py, topology.py). Resolve both so a
// future matcher that legitimately targets a variable-registered tool does
// not trip a false failure.
function registeredToolNames() {
  const names = new Set();
  for (const entry of readdirSync(TOOLS_DIR)) {
    if (!entry.endsWith(".py")) continue;
    const src = readFileSync(join(TOOLS_DIR, entry), "utf8");

    // Module-level string constants: `NAME[: ann] = "literal"`. Keys are
    // constant-case (optionally leading underscore) so lowercase params
    // like `name=name` are never mistaken for a tool-name source.
    const consts = new Map();
    const constRe = /^\s*(_?[A-Z][A-Za-z0-9_]*)\s*(?::[^=\n]+)?=\s*"([^"]+)"/gm;
    for (const m of src.matchAll(constRe)) consts.set(m[1], m[2]);

    // `name="<literal>"` — the common direct registration. `\bname`
    // excludes `audit_agent_name=`, `agent_name=`, etc.
    for (const m of src.matchAll(/\bname\s*=\s*"([a-z][a-z0-9_]*)"/g)) {
      names.add(m[1]);
    }
    // `name=<CONST>` — variable registration; resolve via the const map.
    for (const m of src.matchAll(/\bname\s*=\s*(_?[A-Z][A-Za-z0-9_]*)\b/g)) {
      const resolved = consts.get(m[1]);
      if (resolved) names.add(resolved);
    }
  }
  return names;
}

test("every plugin-scoped hooks.json matcher names a registered MCP tool", () => {
  const hooksDoc = JSON.parse(readFileSync(HOOKS_JSON, "utf8"));
  const registered = registeredToolNames();

  // Guard the guard: a broken path or a format change must fail loudly,
  // not vacuously pass.
  assert.ok(
    registered.size > 0,
    `no registered tool names derived from ${TOOLS_DIR}`,
  );

  const scopedTools = matcherStrings(hooksDoc)
    .flatMap((m) => [...m.matchAll(SCOPED_MATCHER)].map((g) => g[1]));
  assert.ok(
    scopedTools.length > 0,
    "no mcp__plugin_meho_meho__<tool> matcher found in hooks.json",
  );

  for (const tool of scopedTools) {
    assert.ok(
      registered.has(tool),
      `hooks.json matches mcp__plugin_meho_meho__${tool}, but no tool ` +
        `named "${tool}" is registered under backend/src/meho_backplane/` +
        `mcp/tools/. The plugin-scoped name must be the exact registered ` +
        `tool name (mind the meho_ prefix asymmetry).`,
    );
  }
});
