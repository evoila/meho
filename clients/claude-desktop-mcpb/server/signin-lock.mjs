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
//   the next copy takes it over. It never deletes the stale file, because
//   two copies can see the same stale lock, and the second delete would hit
//   the lock the first copy had just made. Instead the copy gives the lock
//   file a second name (a hard link; only one process can create that
//   name), checks that the file is still the stale lock it saw, and writes
//   its own process id into it. Other copies then see a live lock.
// - The lock never blocks for good: after the wait limit, or when the lock
//   file cannot be used at all, the launcher starts mcp-remote anyway.

import { createHash } from "node:crypto";
import {
  closeSync,
  linkSync,
  mkdirSync,
  openSync,
  readFileSync,
  statSync,
  unlinkSync,
  writeFileSync,
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
// yet written). After this long it is treated as stale. The same grace
// applies to the second name a copy creates while it takes over a stale lock.
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

function lockText() {
  return JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() });
}

// `age` counts from the last write. `changedAge` also counts from the last
// change to the file's names (a hard link updates ctime, not mtime).
function readLock(path) {
  let text;
  let stat;
  try {
    text = readFileSync(path, "utf8");
    stat = statSync(path);
  } catch (err) {
    if (err.code === "ENOENT") return null;
    throw err;
  }
  let pid;
  try {
    pid = JSON.parse(text).pid;
  } catch {
    pid = undefined;
  }
  const now = Date.now();
  return {
    pid: Number.isInteger(pid) ? pid : undefined,
    text,
    age: now - stat.mtimeMs,
    changedAge: now - Math.max(stat.mtimeMs, stat.ctimeMs),
  };
}

// Why the lock is stale, or null when it is not.
function staleReason(lock, graceMs) {
  if (lock.pid === undefined) {
    return lock.age > graceMs ? "it has no readable process id" : null;
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
    writeSync(fd, lockText());
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

// Take over a stale lock without deleting it. Returns true when this process
// owns the lock afterwards.
//
// Two copies can read the same stale lock at the same moment. If both
// deleted it and created their own, the second delete would remove the first
// copy's new lock, and both would sign in. So the lock file is taken over in
// place:
//
// 1. Give the lock file a second name with a hard link. Only one process can
//    create that name. While it exists, no other copy touches the lock.
// 2. Check that the file is still the stale lock this copy saw. Another copy
//    may have taken it over, or the owner may have released it and a new
//    copy created a fresh lock, before step 1 ran. Then leave it alone.
// 3. Write this process id into the file. The lock and the second name are
//    the same file, so other copies now read a live lock and wait.
// 4. Remove the second name.
//
// A second name left behind by a copy that stopped between steps 1 and 4 is
// stale too (its pid stopped, or it is older than the grace period) and is
// removed by the next copy.
function takeOverStaleLock(path, stale, graceMs) {
  const claim = `${path}.takeover`;
  try {
    linkSync(path, claim);
  } catch (err) {
    if (err.code === "ENOENT") return false;
    if (err.code !== "EEXIST") throw err;
    const left = readLock(claim);
    if (left && staleReason(left, graceMs) && left.changedAge > graceMs) removeQuietly(claim);
    return false;
  }
  try {
    if (readLock(claim)?.text !== stale.text) return false;
    writeFileSync(claim, lockText());
    return true;
  } finally {
    removeQuietly(claim);
  }
}

// Wait for the lock, then take it. Returns a release function that is safe
// to call more than once. It only removes the lock while this process still
// owns it.
export async function acquireSignInLock(
  serverUrl,
  {
    env = process.env,
    say = sayDefault,
    waitLimitMs = WAIT_LIMIT_MS,
    pollMs = POLL_MS,
    graceMs = UNREADABLE_GRACE_MS,
  } = {},
) {
  const path = signInLockPath(serverUrl, env);
  const noLock = () => {};
  const deadline = Date.now() + waitLimitMs;
  let foundStale = false;
  let waiting = false;
  try {
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    for (;;) {
      if (tryCreate(path)) break;
      const lock = readLock(path);
      if (lock === null) continue;
      const reason = staleReason(lock, graceMs);
      if (reason) {
        if (!foundStale) {
          foundStale = true;
          say(`found an old sign-in lock: ${reason}; taking it over`);
        }
        if (takeOverStaleLock(path, lock, graceMs)) break;
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
          reason
            ? "another MEHO instance is taking over the old sign-in lock; waiting for it to finish"
            : `another MEHO instance (pid ${lock.pid ?? "unknown"}) is signing in; ` +
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
