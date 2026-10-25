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
//   Each bell's badge span ``hx-get``s ``/ui/approvals/badge`` on ``load``,
//   on the ``meho:approval-badge-poll`` event this controller's timer
//   dispatches on <body>, and on ``meho:approval-bump`` (after a decision).
//   The count is always the server's tenant-scoped pending total; this file
//   never counts anything itself. The timer uses its own event so only the
//   two badges re-fetch: the approvals history list listens to
//   ``meho:approval-bump`` and must not reload (and lose its "Load more"
//   pages) every few seconds.
//
// Refresh (#3758): the console used to bump the badge on ``approval.*``
// frames from the Broadcast SSE stream. Broadcast now carries deliberate
// agent announcements only, so the bell polls instead:
//   * ONE timer for the whole shell (mobile and desktop badges both listen
//     for the same body event), every ``POLL_MS`` while the page is
//     visible. A hidden tab sends no requests.
//   * When the tab becomes visible again, refresh at once.
//   * After a decision (the approve/deny POST returns
//     ``HX-Trigger: meho:approval-decided``), close the modal and refresh.
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

    // After a decision: re-fetch both badges and the open history list.
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
