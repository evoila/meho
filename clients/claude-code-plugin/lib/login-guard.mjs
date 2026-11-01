// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Login guard for the plugin's mcp-remote shim (#3961).
//
// bin/meho-mcp-remote runs this file instead of mcp-remote itself:
//
//   node login-guard.mjs <mcp-remote entry> <url> [mcp-remote flags...]
//
// The guard loads the vendored mcp-remote in this same process (nothing is
// spawned) and adds five rules that mcp-remote does not have:
//
// 1. A time limit for connecting. mcp-remote waits for a browser login with
//    no time limit, and it keeps the OAuth callback port while it waits. If
//    nobody finishes the login, that process stays forever and blocks the
//    port for every other Claude Code session. The guard stops the process
//    when it has not connected within MEHO_MCP_LOGIN_TIMEOUT seconds
//    (default 300, at most one day). "Connected" means mcp-remote wrote its
//    first message to stdout: it only does that after the connection to MEHO
//    works. Stopping the process frees the port, and mcp-remote's own exit
//    handler removes its lockfile. The same limit also ends a second session
//    that waits for another session's browser login (rule 4).
//
// 2. A clear message when the login has ended during a session. When a call
//    gets "Unauthorized" from MEHO, mcp-remote first renews the login and
//    sends the call again (rule 5 makes that renewal safe). Only when the
//    renewal fails too does the call fail, with the error "Failed to forward
//    message to remote server: Unauthorized", or with the guard's own
//    LOGIN_ENDED text (token-renewal.mjs). The guard replaces that text with
//    what the user must do, then stops the process. Stopping it means no
//    more browser tabs open for every later call, and /mcp shows that meho
//    needs a reconnect. A reconnect starts a fresh process, which runs a
//    normal browser login with a listening callback server.
//
// 3. Free the login port once connected (#4000). The pinned mcp-remote
//    keeps its OAuth callback server and its lockfile after a login works,
//    until the process stops. All sessions of one MEHO server use the same
//    callback port, so another session could not finish its own login while
//    this one runs. When the connection is up, the guard closes the servers
//    mcp-remote opened and removes the lockfile that holds this process's
//    pid. It also removes mcp-remote's exit hooks that would delete the
//    lockfile again later, when it may belong to another session. Nothing
//    needs the callback server after that: when the login expires later,
//    rule 2 stops this process, and a reconnect starts a fresh one.
//
// 4. One browser login at a time for all sessions on the computer (#5514).
//    Before it loads mcp-remote, the guard takes the sign-in lock
//    (signin-lock.mjs), and it releases the lock once connected. Another
//    session that starts meanwhile waits, then finds the new login on disk
//    and opens no browser. The Claude Desktop bundle runs this guard too,
//    so it shares the lock.
//
// 5. A failed renewal keeps the login (#5514). mcp-remote never deletes the
//    shared login file, a renewal is tried again on a network or server
//    error, and a refused renewal first looks for a newer login that
//    another session saved. Details in token-renewal.mjs.
//
// The guard only reads what mcp-remote writes to stdout. It changes one
// thing there: the text of that one error. Every other message passes
// unchanged.

import { readdirSync, readFileSync, unlinkSync } from "node:fs";
import { Server } from "node:net";
import { homedir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import { acquireSignInLock } from "./signin-lock.mjs";
import { LOGIN_ENDED, installRenewal } from "./token-renewal.mjs";

const DEFAULT_LIMIT_SECONDS = 300;
// One day. Node cannot hold a timer longer than about 24.8 days; a bigger
// value would make the timer fire at once.
const MAX_LIMIT_SECONDS = 86400;
const FORWARD_FAILED = "Failed to forward message to remote server:";

// The Claude Desktop bundle loads this same file (#4003). It sets these two
// variables so the messages name the bundle and say how to reconnect in
// Claude Desktop. The guard removes them, so mcp-remote never sees them.
const LOG_PREFIX = process.env.MEHO_LOGIN_GUARD_PREFIX || "meho plugin";
const RECONNECT =
  process.env.MEHO_LOGIN_GUARD_RECONNECT || "run /mcp, pick meho, and reconnect";
// Tests set the base wait between renewal attempts (default 1000 ms).
const RETRY_BASE_MS = Number(process.env.MEHO_LOGIN_GUARD_RETRY_BASE_MS) || 1000;
delete process.env.MEHO_LOGIN_GUARD_PREFIX;
delete process.env.MEHO_LOGIN_GUARD_RECONNECT;
delete process.env.MEHO_LOGIN_GUARD_RETRY_BASE_MS;
const LOGIN_EXPIRED_MESSAGE =
  `Your MEHO login has expired. ${RECONNECT[0].toUpperCase()}${RECONNECT.slice(1)} ` +
  "to log in again.";

function say(text) {
  process.stderr.write(`${LOG_PREFIX}: ${text}\n`);
}

function limitSeconds() {
  const raw = (process.env.MEHO_MCP_LOGIN_TIMEOUT ?? "").trim();
  if (!raw) return DEFAULT_LIMIT_SECONDS;
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) {
    say(
      `ignoring MEHO_MCP_LOGIN_TIMEOUT=${raw}: it must be a whole number ` +
        `of seconds above 0. Using ${DEFAULT_LIMIT_SECONDS}.`,
    );
    return DEFAULT_LIMIT_SECONDS;
  }
  if (value > MAX_LIMIT_SECONDS) {
    say(
      `MEHO_MCP_LOGIN_TIMEOUT=${raw} is more than one day. ` +
        `Using ${MAX_LIMIT_SECONDS}.`,
    );
    return MAX_LIMIT_SECONDS;
  }
  return value;
}

// Return the JSON-RPC line with the clear message, or null when the chunk is
// not mcp-remote's "needs a new login" error or the guard's "login ended"
// error.
function rewriteLoginError(chunk) {
  const text =
    typeof chunk === "string"
      ? chunk
      : Buffer.isBuffer(chunk) || chunk instanceof Uint8Array
        ? Buffer.from(chunk).toString("utf8")
        : null;
  if (text === null || !text.includes(FORWARD_FAILED)) return null;
  let message;
  try {
    message = JSON.parse(text);
  } catch {
    return null;
  }
  const errorText = message?.error?.message;
  if (
    typeof errorText !== "string" ||
    !errorText.startsWith(FORWARD_FAILED) ||
    !(errorText.includes("Unauthorized") || errorText.includes(LOGIN_ENDED))
  ) {
    return null;
  }
  message.error.message = LOGIN_EXPIRED_MESSAGE;
  return `${JSON.stringify(message)}\n`;
}

// Rule 3 helpers. Remember every server mcp-remote starts to listen on;
// the OAuth callback server is one of them (express app.listen ends in
// net.Server.prototype.listen).
const openedServers = new Set();
const originalListen = Server.prototype.listen;
Server.prototype.listen = function listen(...args) {
  openedServers.add(this);
  return originalListen.apply(this, args);
};

// mcp-remote keeps its lockfile at
// <MCP_REMOTE_CONFIG_DIR or ~/.mcp-auth>/mcp-remote-<version>/<hash>_lock.json
// with the pid of the process that wrote it. Remove only a lockfile with
// this process's pid, so another session's lockfile is never touched.
function removeOwnLockfiles() {
  const base = process.env.MCP_REMOTE_CONFIG_DIR || join(homedir(), ".mcp-auth");
  let dirs;
  try {
    dirs = readdirSync(base).filter((name) => name.startsWith("mcp-remote-"));
  } catch {
    return;
  }
  for (const dir of dirs) {
    let files;
    try {
      files = readdirSync(join(base, dir)).filter((name) => name.endsWith("_lock.json"));
    } catch {
      continue;
    }
    for (const file of files) {
      const path = join(base, dir, file);
      try {
        if (JSON.parse(readFileSync(path, "utf8"))?.pid === process.pid) {
          unlinkSync(path);
        }
      } catch {
        // Unreadable, half-written, or already gone: not ours to fix.
      }
    }
  }
}

// During a login, mcp-remote also adds a process.once("exit") hook and a
// process.once("SIGINT") hook that delete the lockfile without checking
// its pid. Once this process has removed its own lockfile, the lockfile on
// disk can belong to another session that is logging in, so these hooks
// must go. Only hooks added after the snapshot taken before mcp-remote
// loads are removed. On SIGINT only the once() hooks go (raw listeners
// with a .listener property); mcp-remote's normal shutdown handler stays.
const hooksBefore = {};
function snapshotLockfileHooks() {
  for (const event of ["exit", "SIGINT"]) {
    hooksBefore[event] = new Set(process.rawListeners(event));
  }
}
function removeLockfileHooks() {
  for (const fn of process.rawListeners("exit")) {
    if (!hooksBefore.exit.has(fn)) process.removeListener("exit", fn);
  }
  for (const fn of process.rawListeners("SIGINT")) {
    if (!hooksBefore.SIGINT.has(fn) && typeof fn.listener === "function") {
      process.removeListener("SIGINT", fn);
    }
  }
}

function freeLoginPort() {
  Server.prototype.listen = originalListen;
  for (const server of openedServers) {
    if (server.listening) server.close();
  }
  openedServers.clear();
  removeOwnLockfiles();
  removeLockfileHooks();
}

const [entry, ...mcpRemoteArgs] = process.argv.slice(2);
if (!entry) {
  say("login-guard needs the mcp-remote entry as its first argument.");
  process.exit(1);
}

// The MEHO URL is mcp-remote's first argument. It names the lock files, so
// the same URL gives the same locks in the plugin and the Desktop bundle.
let serverUrl = mcpRemoteArgs[0] ?? "";
try {
  serverUrl = new URL(serverUrl).href;
} catch {
  // Not a URL: mcp-remote reports it. The raw text still names a lock.
}

const limit = limitSeconds();
const limitText =
  limit % 60 === 0
    ? `${limit / 60} minute${limit === 60 ? "" : "s"}`
    : `${limit} second${limit === 1 ? "" : "s"}`;

// Rule 1: stop when the connection is not up within the limit. unref() so
// the timer alone never keeps the process alive.
const connectTimer = setTimeout(() => {
  say(
    `MEHO did not connect within ${limitText}, so this attempt stopped. ` +
      `Finish any open MEHO login in your browser, then ${RECONNECT}.`,
  );
  process.exit(1);
}, limit * 1000);
connectTimer.unref();

// Rules 2 and 3: watch what mcp-remote writes to stdout (the JSON-RPC
// channel to Claude Code).
const originalWrite = process.stdout.write.bind(process.stdout);
let connected = false;
let stopping = false;
let releaseSignInLock = () => {};
process.stdout.write = (chunk, encoding, callback) => {
  if (!connected) {
    connected = true;
    clearTimeout(connectTimer);
    freeLoginPort();
    releaseSignInLock();
  }
  const rewritten = rewriteLoginError(chunk);
  if (rewritten === null) {
    return originalWrite(chunk, encoding, callback);
  }
  const done = typeof encoding === "function" ? encoding : callback;
  if (stopping) {
    return originalWrite(rewritten, done);
  }
  stopping = true;
  say(LOGIN_EXPIRED_MESSAGE);
  return originalWrite(rewritten, (err) => {
    done?.(err);
    process.exit(1);
  });
};

// Hand mcp-remote its usual argv, then load it. It reads
// process.argv.slice(2) when it loads and owns stdio from then on.
process.argv = [process.argv[0], entry, ...mcpRemoteArgs];
// Rule 4. The connect timer above also limits this wait. The lock's own exit
// hook is added before the snapshot, so rule 3 keeps it.
releaseSignInLock = await acquireSignInLock(serverUrl, { say });
// Rule 5.
installRenewal({ serverUrl, isConnected: () => connected, say, retryBaseMs: RETRY_BASE_MS });
snapshotLockfileHooks();
try {
  await import(pathToFileURL(entry).href);
} catch (err) {
  say(`failed to start mcp-remote: ${err.message}`);
  process.exit(1);
}
