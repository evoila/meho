// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Dashboard recent-activity tray Alpine controller (G0.25 #1696).
// Registers on ``alpine:init`` so the ``x-data="dashboardFeedTray(...)"``
// wrapper in ``dashboard.html`` resolves to this component. Loaded via
// ``<script src=... defer>`` from the page's HEAD-level
// ``component_scripts`` block, which renders BEFORE
// ``vendor/alpine.min.js`` -- deferred scripts execute in document
// order, and Alpine's CDN bundle auto-starts in a microtask at the end
// of its own script task, so this listener must already be registered
// by then or the component never registers (#1692). Kept in a static
// file rather than an inline ``<script>`` block so a future nonce-based
// CSP needs no inline-script exception -- matching the chassis
// ``base.html`` "zero inline script" posture.
//
// Responsibilities:
//   * Subscribe (indirectly) to the session-gated SSE bridge: the HTMX
//     ``sse`` extension owns the EventSource pointed at
//     ``/ui/broadcast/stream``; this controller hooks
//     ``htmx:sseBeforeMessage`` to parse each ``event: broadcast``
//     frame and route it into the ``events`` array instead of letting
//     the extension swap the raw JSON text into the DOM. The
//     ``preventDefault()`` is load-bearing twice over: the raw frame
//     is unreadable as tray content, and an event field containing
//     markup would otherwise be parsed into live DOM by the swap
//     (the same injection class the broadcast surface closed in
//     PR #1044) -- ``x-text`` bindings render every field inert.
//   * Prepend newest-first and trim to the in-DOM cap so an all-day
//     dashboard tab doesn't grow the tray unboundedly -- same hygiene
//     as ``connectors-feed.js`` (cap 50) and ``broadcast-feed.js``
//     (cap 1000). The richer bounded-tray surface work (row counters,
//     filters) stays with G10.1 follow-ups per #1696's out-of-scope.
//
// Wire-shape note
//   The bridge validates every stream entry as a ``BroadcastEvent``
//   (G6.1) before emitting, so each frame's JSON carries:
//     { kind, event_id, ts, tenant_id, principal_sub, principal_name,
//       target_name, op_id, op_class, result_status, audit_id,
//       payload }
//   The tray renders the compact subset (ts / principal_sub / op_id /
//   result_status) directly -- no projection step like the connectors
//   recent-ops card needs, because the tray's row shape is the wire
//   shape.

document.addEventListener("alpine:init", () => {
  Alpine.data("dashboardFeedTray", (opts) => ({
    // Fills from the SSE stream only -- the dashboard tray has no
    // server-seeded rows (the bridge's backlog prelude replays recent
    // history into a fresh connection, so a quiet-but-nonempty tenant
    // still paints rows immediately).
    events: [],
    connected: false,
    cap: typeof opts.cap === "number" && opts.cap > 0 ? opts.cap : 50,

    // Parse one SSE ``event: broadcast`` frame and prepend it, trimming
    // to the in-DOM cap. Mirrors ``connectors-feed.js``: bad JSON (a
    // mid-stream truncation, an upstream serialisation glitch) is
    // dropped silently rather than thrown into the user-visible
    // surface -- a single bad frame must not break the live tray.
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
      // #2549: the shared ``/ui/broadcast/stream`` now also carries
      // agent-authored announcements. This tray is an operations-activity
      // glance surface whose row shape is the audit ``BroadcastEvent``
      // (ts / principal_sub / op_id / result_status); an announcement
      // carries none of the op fields, so drop it here and let it render
      // on the broadcast feed / history (its first-class home) rather than
      // as a blank operation row.
      if (
        frame &&
        (frame.kind === "agent_announcement" || frame.event_kind === "agent_announcement")
      ) {
        return;
      }
      this.events.unshift(frame);
      if (this.events.length > this.cap) {
        this.events.length = this.cap;
      }
    },

    // Locale time string for the row timestamp; falls back to the raw
    // ISO value if it can't be parsed. Same helper shape as
    // ``broadcast-feed.js`` -- the tray is a same-day glance surface,
    // so the time alone is the right density.
    formatTs(ts) {
      const d = new Date(ts);
      return isNaN(d.getTime()) ? ts : d.toLocaleTimeString();
    },
  }));
});
