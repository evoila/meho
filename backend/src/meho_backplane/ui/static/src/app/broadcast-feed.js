// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Broadcast live-feed Alpine controller (Initiative #338 Task #867).
//
// Registered on ``alpine:init`` so the ``x-data="broadcastFeed(...)"``
// wrapper in ``broadcast/feed.html`` resolves to this component. Kept in
// a static file (loaded via ``<script src=... defer>`` from the page's
// ``{% block scripts %}``) rather than an inline ``<script>`` block so a
// future nonce-based CSP needs no inline-script exception -- matching the
// chassis ``base.html`` "zero inline script" posture.
//
// Responsibilities:
//   * Subscribe (indirectly) to the SSE feed: the HTMX ``sse`` extension
//     owns the EventSource; this controller hooks
//     ``htmx:sseBeforeMessage`` to parse each ``event: broadcast`` frame
//     and route it into the Alpine ``events`` array instead of letting
//     the extension swap the raw JSON text into the DOM.
//   * Prepend newest-first and trim to the in-DOM row cap (work item #9).
//   * Provide the row-render helpers the server-authored
//     ``_event_row.html`` partial binds to (badge colour, timestamp,
//     payload summary).
//
// The colour table + cap are passed in from the route context
// (``opts``) so the policy stays server-side.

document.addEventListener("alpine:init", () => {
  Alpine.data("broadcastFeed", (opts) => ({
    events: [],
    connected: false,
    cap: opts.cap,
    badgeClasses: opts.badgeClasses,

    // Parse one SSE ``event: broadcast`` frame and prepend it, trimming
    // to the in-DOM cap. ``$event.detail`` is the MessageEvent the sse
    // extension passes through ``htmx:sseBeforeMessage``; ``.data`` is
    // the BroadcastEvent JSON. ``preventDefault`` stops the extension
    // from also swapping the raw JSON into the (hidden) sink element.
    onSseMessage(evt) {
      evt.preventDefault();
      const raw = evt.detail && evt.detail.data;
      if (!raw) {
        return;
      }
      let parsed;
      try {
        parsed = JSON.parse(raw);
      } catch (e) {
        // A malformed frame is dropped rather than tearing the feed
        // down; the API stream already filters these upstream, so this
        // is a belt-and-suspenders client guard.
        return;
      }
      this.events.unshift(parsed);
      if (this.events.length > this.cap) {
        this.events.length = this.cap;
      }
    },

    // DaisyUI badge variant for an op_class; unknown classes fall back
    // to the neutral ghost badge.
    badgeClass(opClass) {
      return this.badgeClasses[opClass] || "badge-ghost";
    },

    // Locale time string for the row timestamp; falls back to the raw
    // ISO value if it can't be parsed.
    formatTs(ts) {
      const d = new Date(ts);
      return isNaN(d.getTime()) ? ts : d.toLocaleTimeString();
    },

    // One-line payload summary. Aggregate-only events (credential reads,
    // audit queries -- decision #3) carry no ``params`` key in their
    // redacted payload, so they render the explicit ``(aggregate-only)``
    // placeholder instead of an empty cell.
    payloadSummary(ev) {
      const p = ev.payload || {};
      if (!("params" in p)) {
        return "(aggregate-only)";
      }
      try {
        return JSON.stringify(p.params);
      } catch (e) {
        return "(aggregate-only)";
      }
    },
  }));
});
