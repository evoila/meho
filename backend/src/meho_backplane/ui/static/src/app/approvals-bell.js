// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// App-shell approvals-bell Alpine controller (G10.7-T3 #1778).
// Registers on ``alpine:init`` so the ``x-data="approvalsBell()"`` wrapper
// in ``base.html`` resolves to this component. Loaded via
// ``<script src=... defer>`` from the page's HEAD-level
// ``component_scripts`` block, which renders BEFORE
// ``vendor/alpine.min.js`` -- deferred scripts execute in document order,
// and Alpine's CDN bundle auto-starts in a microtask at the end of its own
// script task, so this listener must already be registered by then or the
// component never registers (#1692). Kept in a static file (not an inline
// <script>) so a future nonce-based CSP needs no inline-script exception --
// matching the chassis ``base.html`` "zero inline script" posture.
//
// Design: the badge is server-authoritative, not a client counter.
//   The bell's ``#meho-approvals-badge`` span is an HTMX swap target that
//   ``hx-get``s ``/ui/approvals/badge`` on ``load`` and again on the
//   ``meho:approval-bump`` event this controller dispatches on <body>.
//   So the displayed count is always the server's tenant-scoped truth
//   (``list_pending``), and the "live, without reload" behaviour comes from
//   re-fetching on every relevant signal rather than incrementing a JS
//   variable that could drift out of sync with the queue.
//
// Responsibilities:
//   * Subscribe (indirectly) to the session-gated SSE bridge filtered to
//     ``op_class=approval``: the HTMX ``sse`` extension owns the
//     EventSource pointed at ``/ui/broadcast/stream?op_class=approval``;
//     this controller hooks ``htmx:sseBeforeMessage`` to inspect each
//     ``event: broadcast`` frame, ``preventDefault()`` the raw swap (the
//     BroadcastEvent JSON must never be parsed into DOM -- the same
//     injection class PR #1044 closed; event fields stay inert), and bump
//     the badge when the frame is an ``approval.*`` lifecycle event.
//   * Re-fetch the badge after a decision completes: the approve/deny POST
//     returns ``HX-Trigger: meho:approval-decided``, which HTMX dispatches
//     on <body>; the bell listens for it, closes the modal, and bumps.

document.addEventListener("alpine:init", () => {
  Alpine.data("approvalsBell", () => ({
    // Open the modal: load the pending-requests panel into the modal
    // container. HTMX owns the swap; the shared modal controller
    // (``app/modal-dialogs.js``) opens the swapped-in ``<dialog>`` via
    // ``showModal()`` on ``htmx:afterSwap`` (#1803).
    open: false,

    // Ask the badge target to re-fetch its authoritative count. Dispatched
    // on <body> so the badge's ``hx-trigger="... meho:approval-bump
    // from:body"`` picks it up regardless of where the bell sits in the DOM.
    bumpBadge() {
      document.body.dispatchEvent(new CustomEvent("meho:approval-bump"));
    },

    // Parse one SSE ``event: broadcast`` frame. Bad JSON (a mid-stream
    // truncation, an upstream serialisation glitch) is dropped silently --
    // a single bad frame must not break the bell. Mirrors
    // ``dashboard-feed.js``. We cancel the raw swap unconditionally for
    // frames carrying data (the sink must never render JSON into the DOM),
    // then bump the badge only for ``approval.*`` op-ids -- the stream is
    // already server-filtered to ``op_class=approval``, but matching the
    // op-id prefix keeps the bell correct even if the filter ever widens.
    onSseMessage($event) {
      const raw = $event && $event.detail && $event.detail.data;
      if (!raw) {
        return;
      }
      $event.preventDefault();
      let frame;
      try {
        frame = JSON.parse(raw);
      } catch {
        return;
      }
      if (frame && typeof frame.op_id === "string" && frame.op_id.indexOf("approval.") === 0) {
        this.bumpBadge();
      }
    },

    // A decision (approve/deny) completed in the modal. Close the dialog
    // and re-fetch the count so the badge decrements without a reload.
    // ``.close()`` fires the native ``close`` event the shared modal
    // controller listens for, which strips any ``modal-open`` class so
    // the dialog fully dismisses (#1803).
    onDecided() {
      const modal = document.getElementById("meho-approvals-modal");
      if (modal && typeof modal.close === "function") {
        modal.close();
      }
      this.bumpBadge();
    },
  }));
});
