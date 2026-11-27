// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// App-shell approvals-bell Alpine controller (G10.7-T3 #1778, #3758).
// Registers on ``alpine:init`` so the ``x-data="approvalsBell()"`` wrapper
// in ``base.html`` resolves to this component. Loaded via
// ``<script src=... defer>`` from the page's HEAD, before
// ``vendor/alpine.min.js`` -- deferred scripts execute in document order,
// and Alpine's bundle auto-starts in a microtask at the end of its own
// script task, so this listener must already be registered by then (#1692).
// Kept in a static file (not an inline <script>) so the console needs no
// inline-script exception in its Content-Security-Policy.
//
// Design: the badge is server-authoritative, not a client counter.
//   The bell's badge span ``hx-get``s ``/ui/approvals/badge`` on ``load``,
//   on the ``meho:approval-badge-poll`` event this controller's timer
//   dispatches on <body>, and on ``meho:approval-bump`` (after a decision).
//   The count is always the server's tenant-scoped pending total; this file
//   never counts anything itself. The badge sits in the shared top bar's
//   notification slot, the one bell on the page. The timer uses
//   its own event so only the badge re-fetches: the approvals history list
//   listens to ``meho:approval-bump`` and must not reload (and lose its
//   "Load more" pages) every few seconds.
//
// Refresh (#3758): the console used to bump the badge on ``approval.*``
// frames from the Broadcast SSE stream. Broadcast now carries deliberate
// agent announcements only, so the bell polls instead:
//   * ONE timer for the whole shell, every ``POLL_MS`` while the page is
//     visible. The badge listens for the event on <body>, so where the
//     bell sits does not matter. A hidden tab sends no requests.
//   * When the tab becomes visible again, refresh at once.
//   * After a decision (the approve/deny POST returns
//     ``HX-Trigger: meho:approval-decided``), close the modal and refresh.
// New requests (#6932): the poll does not fetch the history list (that
// would drop its "Load more" pages). The server draws the bell's count,
// taken before the rows were read, as ``data-baseline`` on the list's
// new-requests line. When a badge swap brings another count, the line
// shows "N new requests." (or "The waiting list changed.") with a "Show"
// button that fetches the list. This file keeps no count of its own and
// sends no request of its own.
// Plain JS timers, not ``hx-trigger="every 5s [filter]"``: htmx evaluates
// trigger filters with ``eval``, which the console's CSP does not allow.

document.addEventListener("alpine:init", () => {
  const POLL_MS = 5000;

  Alpine.data("approvalsBell", () => ({
    open: false,
    timer: null,
    onVisibility: null,

    // Start the single shell timer. Alpine calls ``init`` once per
    // component instance, and ``base.html`` renders exactly one
    // ``approvalsBell`` root, so there is one timer per page.
    init() {
      this.timer = window.setInterval(() => this.refreshIfVisible(), POLL_MS);
      this.onVisibility = () => this.refreshIfVisible();
      document.addEventListener("visibilitychange", this.onVisibility);
    },

    destroy() {
      window.clearInterval(this.timer);
      document.removeEventListener("visibilitychange", this.onVisibility);
    },

    refreshIfVisible() {
      if (document.visibilityState === "visible") {
        document.body.dispatchEvent(new CustomEvent("meho:approval-badge-poll"));
      }
    },

    // After a decision: re-fetch the badge and the open history list.
    bumpBadge() {
      document.body.dispatchEvent(new CustomEvent("meho:approval-bump"));
    },

    // A decision (approve/deny) completed in the modal. Close the dialog
    // and re-fetch the count so the badge updates without a reload.
    onDecided() {
      const modal = document.getElementById("meho-approvals-modal");
      if (modal && typeof modal.close === "function") {
        modal.close();
      }
      this.bumpBadge();
    },
  }));
});

// Show the new-requests line of the Approvals page when a bell badge swap
// brings a count that differs from the count the list was drawn with.
document.addEventListener("htmx:afterSwap", (event) => {
  const target = event.detail && event.detail.target;
  if (!target || typeof target.id !== "string" || !target.id.endsWith("-approvals-badge")) {
    return;
  }
  const counter = target.querySelector("[data-pending-count]");
  const notice = document.querySelector("[data-approvals-new-notice]");
  if (!counter || !notice) {
    return;
  }
  const count = Number(counter.dataset.pendingCount);
  if (Number.isNaN(count)) {
    return;
  }
  // A line without the server's count stays hidden.
  if (notice.dataset.baseline === undefined) {
    return;
  }
  const baseline = Number(notice.dataset.baseline);
  if (Number.isNaN(baseline)) {
    return;
  }
  const text = notice.querySelector("[data-approvals-new-notice-text]");
  if (count === baseline) {
    notice.hidden = true;
    return;
  }
  const added = count - baseline;
  text.textContent =
    added > 0
      ? `${added} new request${added === 1 ? "" : "s"}.`
      : "The waiting list changed.";
  notice.hidden = false;
});
