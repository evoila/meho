// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Connectors recent-ops Alpine controller (Initiative #340; Task #873
// G10.3-T1). Registers on ``alpine:init`` so the
// ``x-data="connectorsRecentOps(...)"`` wrapper in
// ``connectors/_recent_ops.html`` resolves to this component. Loaded
// via ``<script src=... defer>`` from the detail page's HEAD-level
// ``component_scripts`` block, which renders BEFORE
// ``vendor/alpine.min.js`` -- deferred scripts execute in document
// order, and Alpine's CDN bundle auto-starts in a microtask at the
// end of its own script task, so this listener must already be
// registered by then or the component never registers (#1692). Kept
// in a static file rather than an inline ``<script>`` block so a
// future nonce-based CSP needs no inline-script exception --
// matching the chassis ``base.html`` "zero inline script" posture.
//
// Responsibilities:
//   * Seed ``events`` from the server-rendered payload passed in
//     through ``opts.seed`` (the last 10 audit_log rows on this
//     target). The seed payload arrives pre-serialised by the
//     Python route's ``_project_audit_rows`` helper -- datetimes
//     are ISO strings, ids are stringified UUIDs.
//   * Subscribe (indirectly) to the SSE feed: the HTMX ``sse``
//     extension owns the EventSource pointed at
//     ``/ui/broadcast/stream?target=<name>``; this controller hooks
//     ``htmx:sseBeforeMessage`` to parse each ``event: broadcast``
//     frame and route it into the ``events`` array instead of
//     letting the extension swap the raw JSON text into the DOM.
//   * Prepend newest-first and trim to the in-DOM cap (default 50)
//     so a busy target doesn't grow the DOM unboundedly.
//
// Wire-shape note
//   The SSE bridge emits ``BroadcastEvent`` JSON (G6.1) which carries:
//     { event_id, ts, principal_sub, op_id, op_class, result_status,
//       audit_id, payload: { method, path, status_code, ... } }
//   We project that into the per-row shape the template renders
//   (``{ audit_id, occurred_at, method, path, status_code, op_id,
//   op_class }``) so streamed rows visually match the server-seeded
//   rows. ``payload.method`` / ``payload.path`` / ``payload.status_code``
//   are the same fields the audit middleware writes into the audit_log
//   row's ``payload`` JSONB column on the server side -- see
//   ``backend/src/meho_backplane/audit.py``'s
//   ``_resolve_audit_payload``.

document.addEventListener("alpine:init", () => {
  Alpine.data("connectorsRecentOps", (opts) => ({
    // Seed array materialises the server-rendered rows; the SSE
    // stream prepends new events on top. ``slice()`` defensive-copies
    // so a future re-init re-reading ``opts.seed`` doesn't see the
    // controller's mutations.
    events: Array.isArray(opts.seed) ? opts.seed.slice() : [],
    connected: false,
    cap: typeof opts.cap === "number" && opts.cap > 0 ? opts.cap : 50,

    // Parse the SSE frame and prepend to ``events``. Mirrors the
    // shape ``broadcast-feed.js`` uses for the live feed, narrowed
    // to the fields the recent-ops template renders. Bad JSON (a
    // mid-stream truncation, an upstream serialisation glitch) is
    // dropped silently rather than throwing into the user-visible
    // surface -- a single bad frame must not break the live row.
    onSseMessage($event) {
      // The HTMX 2 sse extension exposes the raw frame data on
      // ``$event.detail.data``. Falsy => malformed; bail and let
      // the extension's default no-op handle the rest. ``preventDefault``
      // cancels the extension's raw-text swap so the JSON does not
      // land in the hidden sink as literal text.
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
      // Project the BroadcastEvent wire shape into the row shape the
      // template renders. Missing fields render as empty -- never
      // throw.
      const payload = frame.payload && typeof frame.payload === "object"
        ? frame.payload
        : {};
      const row = {
        audit_id: frame.audit_id || frame.event_id || "",
        occurred_at: frame.ts || "",
        method: payload.method || "",
        path: payload.path || "",
        status_code: payload.status_code || "",
        op_id: frame.op_id || "",
        op_class: frame.op_class || "",
      };
      this.events.unshift(row);
      if (this.events.length > this.cap) {
        this.events.length = this.cap;
      }
    },
  }));
});
