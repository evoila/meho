// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// One sign-in at a time (#3857).
//
// Claude Desktop can start this extension twice at almost the same moment
// (its transport version probe). Without coordination both copies of
// mcp-remote run the browser sign-in: two browser tabs open, and the copy
// that loses the race fails with "InvalidGrantError: Code not valid".
// mcp-remote 0.1.38 has its own lockfile, but it checks for the lockfile and
// creates it in two separate steps, so two copies that start together both
// miss it, and on Windows it skips the check completely.
//
// The launcher therefore takes this lock before it loads mcp-remote:
//
// - The lock is a file created with the exclusive "wx" flag. Only one
//   process can create it, on every platform.
// - The holder releases it when mcp-remote is connected (index.mjs calls the
//   release function on mcp-remote's first stdout message), and when the
//   process exits.
// - Any other copy waits. When the lock is gone it starts mcp-remote, which
//   finds the saved login on disk and opens no browser tab.
// - A lock whose process has stopped, or that is older than five minutes
//   (the process id may belong to a new process after a restart), is stale;
//   the next copy removes it and takes over.
// - The lock never blocks for good: after the wait limit, or when the lock
//   file cannot be used at all, the launcher starts mcp-remote anyway.

import { createHash } from "node:crypto";
import {
  closeSync,
  mkdirSync,
  openSync,
  readFileSync,
  statSync,
  unlinkSync,
  writeSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

// How long a copy waits for another copy's sign-in before it starts anyway.
export const WAIT_LIMIT_MS = 5 * 60 * 1000;
// A lock older than this is stale, even when its process id is in use.
const STALE_AGE_MS = 5 * 60 * 1000;
const POLL_MS = 250;
// A lock file without a readable pid is normal for a moment (created, not
// yet written). After this long it is treated as stale.
const UNREADABLE_GRACE_MS = 5000;

function sayDefault(text) {
  process.stderr.write(`meho-claude-desktop: ${text}\n`);
}

// The lock lives next to mcp-remote's saved logins (the same folder
// mcp-remote uses, including its MCP_REMOTE_CONFIG_DIR override), one lock
// per MEHO endpoint.
export function signInLockPath(serverUrl, env = process.env) {
  const base = env.MCP_REMOTE_CONFIG_DIR || join(homedir(), ".mcp-auth");
  const hash = createHash("sha256").update(serverUrl).digest("hex").slice(0, 16);
  return join(base, `meho-claude-desktop-${hash}.lock`);
}

function isRunning(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    // EPERM: the process exists but belongs to someone else.
    return err.code === "EPERM";
  }
}

function readLock(path) {
  let raw;
  let mtimeMs;
  try {
    raw = readFileSync(path, "utf8");
    mtimeMs = statSync(path).mtimeMs;
  } catch (err) {
    if (err.code === "ENOENT") return null;
    throw err;
  }
  let pid;
  try {
    pid = JSON.parse(raw).pid;
  } catch {
    pid = undefined;
  }
  return { pid: Number.isInteger(pid) ? pid : undefined, age: Date.now() - mtimeMs };
}

// Why the lock is stale, or null when it is not.
function staleReason(lock) {
  if (lock.pid === undefined) {
    return lock.age > UNREADABLE_GRACE_MS ? "it has no readable process id" : null;
  }
  if (!isRunning(lock.pid)) return `its instance (pid ${lock.pid}) stopped`;
  if (lock.age > STALE_AGE_MS) return "it is older than five minutes";
  return null;
}

function tryCreate(path) {
  let fd;
  try {
    fd = openSync(path, "wx", 0o600);
  } catch (err) {
    if (err.code === "EEXIST") return false;
    throw err;
  }
  try {
    writeSync(fd, JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() }));
  } finally {
    closeSync(fd);
  }
  return true;
}

function removeQuietly(path) {
  try {
    unlinkSync(path);
  } catch (err) {
    if (err.code !== "ENOENT") throw err;
  }
}

// Wait for the lock, then take it. Returns a release function that is safe
// to call more than once. It only removes the lock while this process still
// owns it.
export async function acquireSignInLock(
  serverUrl,
  { env = process.env, say = sayDefault, waitLimitMs = WAIT_LIMIT_MS, pollMs = POLL_MS } = {},
) {
  const path = signInLockPath(serverUrl, env);
  const noLock = () => {};
  const deadline = Date.now() + waitLimitMs;
  let waiting = false;
  try {
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    for (;;) {
      if (tryCreate(path)) break;
      const lock = readLock(path);
      if (lock === null) continue;
      const reason = staleReason(lock);
      if (reason) {
        say(`removing an old sign-in lock: ${reason}`);
        removeQuietly(path);
        continue;
      }
      if (Date.now() >= deadline) {
        say(
          `another MEHO instance is still signing in after ${waitLimitMs / 1000} seconds; ` +
            "starting anyway",
        );
        return noLock;
      }
      if (!waiting) {
        waiting = true;
        say(
          `another MEHO instance (pid ${lock.pid ?? "unknown"}) is signing in; ` +
            "waiting for it to finish",
        );
      }
      await sleep(pollMs);
    }
  } catch (err) {
    say(`cannot use the sign-in lock ${path}: ${err.message}; starting without it`);
    return noLock;
  }
  if (waiting) say("the other MEHO instance finished; starting");

  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    process.removeListener("exit", release);
    try {
      if (readLock(path)?.pid === process.pid) removeQuietly(path);
    } catch {
      // Best effort: a lock left behind is stale once this process stops.
    }
  };
  process.on("exit", release);
  return release;
}
