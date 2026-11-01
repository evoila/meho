// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Renewal rules for the login guard (#5514).
//
// All sessions on one computer share one saved login file:
// <MCP_REMOTE_CONFIG_DIR or ~/.mcp-auth>/mcp-remote-<version>/<hash>_tokens.json.
// The pinned mcp-remote handles a failed renewal badly for a shared file:
//
// - When the login server refuses one renewal (invalid_grant), it deletes
//   the file. Every session on the computer is logged out at once. Two
//   sessions that renew at the same moment can cause this too.
// - When a renewal fails because of the network or a server error, it does
//   not try again. It opens a browser login at once.
//
// The guard calls installRenewal() before it loads mcp-remote. It changes
// mcp-remote from the inside, without editing its files:
//
// 1. mcp-remote never deletes the login file. A new login (a renewal or a
//    browser login) writes over it, all at once (a new file, then a
//    rename), so no session ever reads half a login.
// 2. Only one renewal runs at a time for all sessions of one MEHO server
//    (a lock file next to the login file). Under the lock the guard reads
//    the login file again and renews with the newest refresh token in it,
//    so a session uses a login that another session renewed.
// 3. A network error or an HTTP 5xx answer is tried again after 1, 2, 4 and
//    8 seconds, for about 30 seconds in total. If the login server still does
//    not answer, the call fails with a clear message and the login is kept.
//    No browser opens.
// 4. invalid_grant: the guard reads the login file again. If another session
//    wrote a newer login, the guard renews with that one. Only when the same
//    refresh token is refused does the login count as ended.
// 5. An ended login:
//    - Before the session is connected, the guard hides the ended login file
//      from mcp-remote, so mcp-remote runs a browser login. The guard holds
//      the sign-in lock during that time (signin-lock.mjs), so only one
//      browser login runs at a time.
//    - In a connected session, the guard first waits while another session
//      runs a browser login, then reads the file again and uses the new
//      login. When there is no new login, the call fails with a clear
//      message (LOGIN_ENDED), and the guard stops the process. A reconnect
//      starts a new process, which runs the browser login.

import { readFileSync, renameSync, writeFileSync } from "node:fs";
import fsPromises from "node:fs/promises";
import { setTimeout as sleep } from "node:timers/promises";

import { acquireSignInLock } from "./signin-lock.mjs";

export const LOGIN_ENDED = "Your MEHO login has ended at the login server.";
const SERVER_DOWN =
  "The MEHO login server did not answer, so the login could not be renewed. " +
  "Your login is kept. Try again in a moment.";

// Wait after attempt 1, 2, 3 and 4, in units of the base delay (1 second).
const RETRY_STEPS = [1, 2, 4, 8];
// All attempts of one renewal end within this many base delays.
const BUDGET_STEPS = 30;
// The renewal lock holder needs at most about 30 seconds.
const RENEW_WAIT_LIMIT_MS = 60 * 1000;

function isTokensFile(path) {
  return typeof path === "string" && path.endsWith("_tokens.json");
}

function parse(text) {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function oauthAnswer(error, description) {
  return new Response(JSON.stringify({ error, error_description: description }), {
    status: 400,
    headers: { "content-type": "application/json" },
  });
}

// The refresh token of a renewal request, or null for every other request.
// mcp-remote sends the token request as a URLSearchParams body.
function refreshTokenOf(init) {
  const body = init?.body;
  if (!(body instanceof URLSearchParams)) return null;
  if (body.get("grant_type") !== "refresh_token") return null;
  return body.get("refresh_token");
}

export function installRenewal({ serverUrl, isConnected, say, retryBaseMs = 1000 }) {
  const originalFetch = globalThis.fetch;
  const originalReadFile = fsPromises.readFile;
  const originalUnlink = fsPromises.unlink;
  const originalWriteFile = fsPromises.writeFile;
  const endedTokens = new Set();
  let tokensPath = null;
  let queue = Promise.resolve();
  let temps = 0;
  const tempName = (path) => `${path}.${process.pid}.${(temps += 1)}.tmp`;

  function readSaved() {
    if (!tokensPath) return null;
    try {
      return parse(readFileSync(tokensPath, "utf8"));
    } catch {
      return null;
    }
  }

  // Write the whole file at once (a new file, then rename), so another
  // session never reads half a login.
  function writeSaved(tokens) {
    if (!tokensPath) return;
    const temp = tempName(tokensPath);
    try {
      writeFileSync(temp, JSON.stringify(tokens, null, 2), { mode: 0o600 });
      renameSync(temp, tokensPath);
    } catch (err) {
      say(`could not save the renewed login: ${err.message}`);
    }
  }

  // Learn where mcp-remote keeps the login file, and hide an ended login
  // from mcp-remote before the session is connected (rule 5).
  fsPromises.readFile = async function readFile(path, ...rest) {
    const content = await originalReadFile.call(this, path, ...rest);
    if (isTokensFile(path)) {
      tokensPath = path;
      const token = parse(String(content))?.refresh_token;
      if (!isConnected() && token && endedTokens.has(token)) {
        const err = new Error(`ENOENT: no such file or directory, open '${path}'`);
        err.code = "ENOENT";
        throw err;
      }
    }
    return content;
  };

  // Rule 1: never delete the shared login file.
  fsPromises.unlink = async function unlink(path, ...rest) {
    if (isTokensFile(path)) return undefined;
    return originalUnlink.call(this, path, ...rest);
  };

  fsPromises.writeFile = async function writeFile(path, ...rest) {
    if (!isTokensFile(path)) return originalWriteFile.call(this, path, ...rest);
    const temp = tempName(path);
    await originalWriteFile.call(this, temp, ...rest);
    return fsPromises.rename(temp, path);
  };

  // Rule 3: one request, tried again on a network error or HTTP 5xx.
  // Returns { response } or { problem } when every attempt failed.
  async function post(input, init, refreshToken) {
    const body = new URLSearchParams(init.body);
    body.set("refresh_token", refreshToken);
    const deadline = Date.now() + BUDGET_STEPS * retryBaseMs;
    for (let attempt = 0; ; attempt += 1) {
      let problem;
      try {
        const response = await originalFetch(input, {
          ...init,
          body: new URLSearchParams(body),
          signal: AbortSignal.timeout(Math.max(1, deadline - Date.now())),
        });
        if (response.status < 500) return { response };
        problem = `HTTP ${response.status}`;
      } catch (err) {
        problem = err.cause?.message ?? err.message;
      }
      const delay = (RETRY_STEPS[attempt] ?? Infinity) * retryBaseMs;
      if (Date.now() + delay >= deadline) return { problem };
      say(`renewing the MEHO login failed (${problem}); trying again in ${delay / 1000} s`);
      await sleep(delay);
    }
  }

  async function isInvalidGrant(response) {
    if (response.status !== 400 && response.status !== 401) return false;
    try {
      return (await response.clone().json())?.error === "invalid_grant";
    } catch {
      return false;
    }
  }

  // Save a renewed login for every session, and hand it to mcp-remote.
  async function accept(response, refreshToken) {
    const tokens = parse(await response.clone().text());
    if (!tokens || typeof tokens.access_token !== "string") return response;
    tokens.refresh_token ??= refreshToken;
    writeSaved(tokens);
    return new Response(JSON.stringify(tokens), {
      status: response.status,
      headers: { "content-type": "application/json" },
    });
  }

  // Rules 2 to 4, under the renewal lock. Returns { response, ended }.
  async function renewOnce(input, init, sentToken) {
    let token = readSaved()?.refresh_token || sentToken;
    for (let tries = 0; tries < 2; tries += 1) {
      if (endedTokens.has(token)) break;
      const { response, problem } = await post(input, init, token);
      if (!response) {
        say(`the MEHO login server did not answer (${problem}); keeping the login`);
        return { response: oauthAnswer("temporarily_unavailable", SERVER_DOWN) };
      }
      if (response.ok) return { response: await accept(response, token) };
      if (!(await isInvalidGrant(response))) return { response };
      const newer = readSaved()?.refresh_token;
      if (!newer || newer === token) {
        endedTokens.add(token);
        say("the login server refused the saved MEHO login; it has ended");
        break;
      }
      token = newer;
    }
    return { response: oauthAnswer("invalid_grant", LOGIN_ENDED), ended: true };
  }

  async function underLock(options, work) {
    const release = await acquireSignInLock(serverUrl, { say, ...options });
    try {
      return await work();
    } finally {
      release();
    }
  }

  async function renew(input, init, sentToken) {
    const renewLock = {
      kind: "renew",
      lockName: "renewal lock",
      doing: "renewing the login",
      waitLimitMs: RENEW_WAIT_LIMIT_MS,
    };
    const first = await underLock(renewLock, () => renewOnce(input, init, sentToken));
    if (!first.ended || !isConnected()) return first.response;
    // Rule 5: another session may be running a browser login right now.
    // Wait for it (the renewal lock is free meanwhile), then look again.
    await underLock({}, async () => {});
    const second = await underLock(renewLock, () => renewOnce(input, init, sentToken));
    return second.response;
  }

  globalThis.fetch = function fetch(input, init) {
    const sentToken = refreshTokenOf(init);
    if (sentToken === null) return originalFetch(input, init);
    const run = queue.then(() => renew(input, init, sentToken));
    queue = run.catch(() => {});
    return run;
  };
}
