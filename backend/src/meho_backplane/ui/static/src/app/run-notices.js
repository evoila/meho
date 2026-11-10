// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// "The run has ended" notice on every console page (#5110).
//
// A page that starts a background run marks it with
// ``data-meho-watch-run="<run id>"`` (today: the outcome card of an
// approve). This script keeps those run ids for the browser tab
// (sessionStorage, so they survive a move to another page). On the
// console's 5-second timer -- the ``meho:approval-badge-poll`` event that
// ``approvals-bell.js`` sends on <body> only while the page is visible --
// it asks ``GET /ui/runs/notices?run_id=...`` and adds the answer to
// ``#meho-run-notices`` in the corner of the page. Every element with
// ``data-run-ended`` in the answer ends the asking for that run; a hidden
// marker (``data-run-notice-drop``) is removed at once.
//
// ``data-meho-run-seen="<run id>"`` on an element in an OPEN dialog means
// the person already sees how that run ended, so no notice follows. A run
// id is watched at most once per tab: a run that got its notice or was
// seen is never watched again.
//
// The Close button of a notice (``data-run-notice-close``) is handled
// here, not in the template, so the console needs no inline script.
// Plain JS, loaded with ``defer`` from ``base.html``.

(() => {
  const WATCH_KEY = "meho.runNotices.watch";
  const DONE_KEY = "meho.runNotices.done";
  const MAX_WATCHED = 20; // the server takes at most 20 ids per ask
  const MAX_DONE = 200;
  let inFlight = false;

  function read(key) {
    try {
      const value = JSON.parse(window.sessionStorage.getItem(key) || "[]");
      return Array.isArray(value) ? value : [];
    } catch (_err) {
      return [];
    }
  }

  function write(key, ids) {
    try {
      window.sessionStorage.setItem(key, JSON.stringify(ids));
    } catch (_err) {
      // Storage full or blocked: the notice is a help, not a must.
    }
  }

  function watch(runId) {
    if (!runId || read(DONE_KEY).includes(runId)) {
      return;
    }
    const ids = read(WATCH_KEY);
    if (!ids.includes(runId)) {
      ids.push(runId);
      write(WATCH_KEY, ids.slice(-MAX_WATCHED));
    }
  }

  function done(runId) {
    if (!runId) {
      return;
    }
    write(WATCH_KEY, read(WATCH_KEY).filter((id) => id !== runId));
    const finished = read(DONE_KEY);
    if (!finished.includes(runId)) {
      finished.push(runId);
      write(DONE_KEY, finished.slice(-MAX_DONE));
    }
  }

  // Read the run marks of new content: watch running runs, and end the
  // watch of a run whose end the person sees in an open dialog.
  function scan(root) {
    if (!root || typeof root.querySelectorAll !== "function") {
      return;
    }
    const marked = Array.from(
      root.querySelectorAll("[data-meho-watch-run], [data-meho-run-seen]"),
    );
    if (root.matches && root.matches("[data-meho-watch-run], [data-meho-run-seen]")) {
      marked.push(root);
    }
    for (const el of marked) {
      if (el.dataset.mehoWatchRun) {
        watch(el.dataset.mehoWatchRun);
      }
      if (el.dataset.mehoRunSeen) {
        const dialog = el.closest("dialog");
        if (dialog && dialog.open) {
          done(el.dataset.mehoRunSeen);
        }
      }
    }
  }

  function readAnswer(box) {
    box.querySelectorAll("[data-run-ended]").forEach((el) => {
      done(el.dataset.runEnded);
      if (el.hasAttribute("data-run-notice-drop")) {
        el.remove();
      }
    });
  }

  function ask() {
    const box = document.getElementById("meho-run-notices");
    const ids = read(WATCH_KEY);
    if (!box || ids.length === 0 || inFlight) {
      return;
    }
    inFlight = true;
    const query = ids.map((id) => "run_id=" + encodeURIComponent(id)).join("&");
    htmx
      .ajax("GET", "/ui/runs/notices?" + query, { source: box, target: box, swap: "beforeend" })
      .then(() => readAnswer(box))
      .catch(() => {
        // Try again on the next tick.
      })
      .finally(() => {
        inFlight = false;
      });
  }

  document.body.addEventListener("htmx:load", (event) => {
    scan(event.detail && event.detail.elt);
  });
  document.body.addEventListener("meho:approval-badge-poll", ask);
  document.body.addEventListener("click", (event) => {
    const button = event.target.closest && event.target.closest("[data-run-notice-close]");
    const notice = button && button.closest("[data-run-notice]");
    if (notice) {
      notice.remove();
    }
  });
})();
