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
//   file a second name of its own (a hard link), checks that it holds the
//   only second name, checks that the file is still the stale lock it saw,
//   and writes its own process id into it. Other copies then see a live
//   lock. See takeOverStaleLock for the details.
// - The owner releases the lock through the same second-name step, so a
//   takeover and a release never overlap: the release deletes the file
//   only while no other copy can be writing its process id into it. See
//   releaseLock for the details.
// - The lock never blocks for good: after the wait limit, or when the lock
//   file cannot be used at all, the launcher starts mcp-remote anyway.

import { createHash, randomBytes } from "node:crypto";
import {
  closeSync,
  linkSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  statSync,
  unlinkSync,
  writeFileSync,
  writeSync,
} from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join } from "node:path";
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

function lockText() {
  return JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() });
}

// `age` counts from the last write.
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

// The second name a copy gives the lock while it takes it over. The name
// holds the copy's process id, so other copies can tell whether the copy
// that made it still runs, and a random part, so a new process with the
// same process id never reuses a name an old process left behind.
function claimName(path) {
  return `${path}.takeover.${process.pid}.${randomBytes(4).toString("hex")}`;
}

// Remove second names left behind by copies that stopped mid-takeover.
//
// A name is only removed when the process in its name is not running (or
// is this process: an earlier process with the same id left it). Only that
// process could have made that exact name, so the removal never hits a
// name a running copy is using. A name with a running process id stays,
// even when it is old; a copy that has it never waits for good, because the
// wait limit still applies.
function removeAbandonedClaims(path) {
  const dir = dirname(path);
  const prefix = `${basename(path)}.takeover.`;
  for (const name of readdirSync(dir)) {
    if (!name.startsWith(prefix)) continue;
    const pid = Number(name.slice(prefix.length).split(".")[0]);
    if (!Number.isInteger(pid)) continue;
    if (pid === process.pid || !isRunning(pid)) removeQuietly(join(dir, name));
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
// 1. Remove second names left behind by stopped copies (see above).
// 2. Give the lock file a second name of this process's own, with a hard
//    link. No other copy ever removes that name, so from here on the file
//    cannot slip away under this copy.
// 3. Count the file's names. Exactly two (the lock and this name) means no
//    other copy is taking it over right now. More means another copy is;
//    then give up this turn and try again on the next poll. While this
//    name exists no other copy can count two, so at most one copy goes on.
// 4. Check that the file is still the stale lock this copy saw. Another
//    copy may have taken it over, or the owner may have released it and a
//    new copy created a fresh lock, before step 2 ran. Then leave it alone.
// 5. Write this process id into the file through the second name, and
//    check that the lock path still shows it. The owner's release also
//    counts the names (see releaseLock), so it cannot delete the lock
//    while this name exists; the check is a safety net.
// 6. Remove the second name.
function takeOverStaleLock(path, stale) {
  removeAbandonedClaims(path);
  const claim = claimName(path);
  try {
    linkSync(path, claim);
  } catch (err) {
    if (err.code === "ENOENT") return false;
    throw err;
  }
  try {
    if (statSync(claim).nlink !== 2) return false;
    if (readLock(claim)?.text !== stale.text) return false;
    const mine = lockText();
    writeFileSync(claim, mine);
    return readLock(path)?.text === mine;
  } finally {
    removeQuietly(claim);
  }
}

// Release the lock without deleting another copy's lock. Returns true when
// the release is done (the lock is gone, or it belongs to another copy) and
// false when another copy is in the middle of a takeover, so the caller
// should try again later.
//
// Once the lock is older than five minutes, another copy may take it over
// while the owner releases it. If the owner read its own process id and
// then deleted the file, the other copy could finish its takeover in
// between, and the delete would remove the new owner's lock. So the release
// uses the same second-name step as the takeover:
//
// 1. Remove second names left behind by stopped copies.
// 2. Give the lock file a second name of this process's own. From here on
//    no takeover can go on, because a takeover counts the names and needs
//    exactly two.
// 3. Count the names. More than two means another copy is in the middle of
//    a takeover and may still write its process id; leave the lock alone
//    and report that the release is not done.
// 4. Check that the file still holds this process's id (another copy may
//    have taken it over before step 2), then remove the lock path.
// 5. Remove the second name.
//
// A file system without hard links makes step 2 fail. There no takeover can
// succeed either, so the plain check and delete is safe.
function releaseLock(path) {
  removeAbandonedClaims(path);
  const claim = claimName(path);
  try {
    linkSync(path, claim);
  } catch (err) {
    if (err.code === "ENOENT") return true;
    if (readLock(path)?.pid === process.pid) removeQuietly(path);
    return true;
  }
  try {
    if (statSync(claim).nlink !== 2) return false;
    if (readLock(claim)?.pid === process.pid) removeQuietly(path);
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
        if (takeOverStaleLock(path, lock)) break;
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
    try {
      // Another copy is taking the lock over: leave it, and try again on
      // the next call (the exit listener at the latest).
      if (!releaseLock(path)) return;
    } catch {
      // Best effort: a lock left behind is stale once this process stops.
    }
    released = true;
    process.removeListener("exit", release);
  };
  process.on("exit", release);
  return release;
}
