// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// In-process checks of lib/token-renewal.mjs (#5514). installRenewal()
// changes the global fetch and fs.promises for the whole process, so this
// file holds only tests that need that, and node --test runs it in its own
// process. The end-to-end cases with the real mcp-remote are in
// login-guard.test.mjs.

import fsPromises from "node:fs/promises";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";
import assert from "node:assert/strict";

import { installRenewal } from "../lib/token-renewal.mjs";

const configDir = mkdtempSync(join(tmpdir(), "meho-renewal-"));
after(() => rmSync(configDir, { recursive: true, force: true }));
process.env.MCP_REMOTE_CONFIG_DIR = configDir;
const dir = join(configDir, "mcp-remote-test");
mkdirSync(dir);
const file = join(dir, "abc_tokens.json");
const read = () => JSON.parse(readFileSync(file, "utf8"));
const save = (login) => writeFileSync(file, JSON.stringify({ token_type: "Bearer", expires_in: 300, ...login }));

// A fake login server: a live refresh token gets the next login.
let issued = 0;
const live = new Set(["refresh-old"]);
globalThis.fetch = async (_url, init) => {
  const token = init.body.get("refresh_token");
  if (!live.has(token)) {
    return Response.json({ error: "invalid_grant", error_description: "Token is not active" }, { status: 400 });
  }
  live.delete(token);
  issued += 1;
  live.add(`refresh-${issued}`);
  return Response.json({
    access_token: `access-${issued}`,
    token_type: "Bearer",
    expires_in: 300,
    refresh_token: `refresh-${issued}`,
  });
};
const said = [];
installRenewal({
  serverUrl: "http://meho.test/mcp",
  isConnected: () => true,
  say: (text) => said.push(text),
  retryBaseMs: 1,
});

// What mcp-remote does: a refresh request through the global fetch, then
// its own save of the result through fs.promises.writeFile.
function renewLike(token) {
  return fetch("http://login.test/token", {
    method: "POST",
    body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: token }),
  });
}
function saveLike(login) {
  return fsPromises.writeFile(file, JSON.stringify(login, null, 2), { encoding: "utf-8", mode: 0o600 });
}

test("a late save by mcp-remote never writes over a newer login another session saved", async () => {
  save({ access_token: "access-old", refresh_token: "refresh-old" });
  await fsPromises.readFile(file, "utf8");

  // Session A renews. The guard saves the result under the lock at once.
  const renewed = await (await renewLike("refresh-old")).json();
  assert.equal(renewed.refresh_token, "refresh-1");
  assert.equal(read().refresh_token, "refresh-1");

  // Session B renews with that login before session A's mcp-remote saves.
  save({ access_token: "access-other", refresh_token: "refresh-other" });

  // Session A's late save must leave session B's login alone.
  await saveLike(renewed);
  assert.equal(read().refresh_token, "refresh-other", "the newer login must stay");
  assert.equal(read().access_token, "access-other");

  // A login the guard did not hand out (a browser login) is still saved,
  // all at once.
  await saveLike({ access_token: "access-browser", token_type: "Bearer", expires_in: 300, refresh_token: "refresh-browser" });
  assert.equal(read().refresh_token, "refresh-browser");
  assert.deepEqual(readdirSync(dir), ["abc_tokens.json"], "no temp file is left behind");
});
