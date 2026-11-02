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
//    rename), so no session ever reads half a login. The guard saves a
//    renewed login itself, under the renewal lock. mcp-remote saves the
//    same login once more afterwards, outside the lock; the guard drops
//    that save, so a late save never writes over a newer login that
//    another session saved in between.
// 2. Only one renewal runs at a time for all sessions of one MEHO server
//    (a lock file next to the login file). Under the lock the guard reads
//    the login file again and renews with the newest refresh token in it,
//    so a session uses a login that another session renewed.
// 3. A network error or an HTTP 5xx answer is tried again after 1, 2, 4 and
//    8 seconds, for about 30 seconds in total. A connection that drops while
//    the answer is read counts as a network error too. If the login server
//    still does not answer, the call fails with a clear message and the
//    login is kept. No browser opens.
// 4. invalid_grant: the guard reads the login file again. If another session
//    wrote a newer login, the guard renews with that one. Otherwise it
//    waits a moment and tries the same token once more. Only when the
//    token read again is refused does the login count as ended.
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
// Requests of one renewal: a refused token is read again and tried once
// more, and a newer saved token starts over. This bounds a long chain.
const MAX_POSTS = 4;

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
  // Access tokens of renewed logins the guard already saved under the lock.
  const savedByGuard = new Set();
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
    if (!tokensPath) return false;
    const temp = tempName(tokensPath);
    try {
      writeFileSync(temp, JSON.stringify(tokens, null, 2), { mode: 0o600 });
      renameSync(temp, tokensPath);
      return true;
    } catch (err) {
      say(`could not save the renewed login: ${err.message}`);
      return false;
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

  // Rule 1: a renewed login was saved under the lock already (accept);
  // mcp-remote's own save of it comes later and may be stale by then.
  // Every other login (a browser login) is written all at once.
  fsPromises.writeFile = async function writeFile(path, data, ...rest) {
    if (!isTokensFile(path)) return originalWriteFile.call(this, path, data, ...rest);
    tokensPath = path;
    const accessToken = parse(String(data))?.access_token;
    if (accessToken && savedByGuard.has(accessToken)) return undefined;
    const temp = tempName(path);
    await originalWriteFile.call(this, temp, data, ...rest);
    return fsPromises.rename(temp, path);
  };

  // Rule 3: one request, tried again on a network error or HTTP 5xx. The
  // answer is read here too: the connection can drop after the headers.
  // Returns { status, text } or { problem } when every attempt failed.
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
        if (response.status < 500) {
          return { status: response.status, text: await response.text() };
        }
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

  function isInvalidGrant(status, text) {
    if (status !== 400 && status !== 401) return false;
    return parse(text)?.error === "invalid_grant";
  }

  function answer(status, text) {
    return new Response(text, { status, headers: { "content-type": "application/json" } });
  }

  // Save a renewed login for every session, and hand it to mcp-remote.
  function accept(status, text, refreshToken) {
    const tokens = parse(text);
    if (!tokens || typeof tokens.access_token !== "string") return answer(status, text);
    tokens.refresh_token ??= refreshToken;
    if (writeSaved(tokens)) savedByGuard.add(tokens.access_token);
    return answer(status, JSON.stringify(tokens));
  }

  // Rules 2 to 4, under the renewal lock. Returns { response, ended }.
  async function renewOnce(input, init, sentToken) {
    let token = readSaved()?.refresh_token || sentToken;
    let refused = null;
    for (let posts = 0; posts < MAX_POSTS && !endedTokens.has(token); posts += 1) {
      const { status, text, problem } = await post(input, init, token);
      if (text === undefined) {
        say(`the MEHO login server did not answer (${problem}); keeping the login`);
        return { response: oauthAnswer("temporarily_unavailable", SERVER_DOWN) };
      }
      if (status >= 200 && status < 300) return { response: accept(status, text, token) };
      if (!isInvalidGrant(status, text)) return { response: answer(status, text) };
      const saved = readSaved()?.refresh_token || token;
      if (saved !== token) {
        token = saved;
        refused = null;
        continue;
      }
      if (refused === token) {
        endedTokens.add(token);
        say("the login server refused the saved MEHO login again; it has ended");
        break;
      }
      refused = token;
      say("the login server refused the saved MEHO login; trying once more");
      await sleep(retryBaseMs);
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
