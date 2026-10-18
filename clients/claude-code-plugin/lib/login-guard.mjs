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
// spawned) and adds two rules that mcp-remote does not have:
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
//    that waits for another session's login: mcp-remote 0.1.39 does not pick
//    up that login, it keeps waiting.
//
// 2. A clear message when the login expires during a session. When a call
//    needs a new login, the pinned mcp-remote answers the call at once with
//    the error "Failed to forward message to remote server: Unauthorized".
//    The guard replaces that text with what the user must do, then stops
//    the process. Stopping it means no more browser tabs open for every
//    later call, and /mcp shows that meho needs a reconnect. A reconnect
//    starts a fresh process, which runs a normal browser login with a
//    listening callback server.
//
// The guard only reads what mcp-remote writes to stdout. It changes one
// thing: the text of that one error. Every other message passes unchanged.

import { pathToFileURL } from "node:url";

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
delete process.env.MEHO_LOGIN_GUARD_PREFIX;
delete process.env.MEHO_LOGIN_GUARD_RECONNECT;
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
// not mcp-remote's "needs a new login" error.
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
    !errorText.includes("Unauthorized")
  ) {
    return null;
  }
  message.error.message = LOGIN_EXPIRED_MESSAGE;
  return `${JSON.stringify(message)}\n`;
}

const [entry, ...mcpRemoteArgs] = process.argv.slice(2);
if (!entry) {
  say("login-guard needs the mcp-remote entry as its first argument.");
  process.exit(1);
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

// Rule 2: watch what mcp-remote writes to stdout (the JSON-RPC channel to
// Claude Code).
const originalWrite = process.stdout.write.bind(process.stdout);
let connected = false;
let stopping = false;
process.stdout.write = (chunk, encoding, callback) => {
  if (!connected) {
    connected = true;
    clearTimeout(connectTimer);
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
try {
  await import(pathToFileURL(entry).href);
} catch (err) {
  say(`failed to start mcp-remote: ${err.message}`);
  process.exit(1);
}
