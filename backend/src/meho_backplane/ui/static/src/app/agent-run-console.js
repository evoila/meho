// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Agent run-console transcript controller (Initiative #1824 Task #1829).
// Registers on ``alpine:init`` so the ``x-data="agentRunConsole(...)"``
// wrapper in ``agents/_run_transcript.html`` resolves to this component.
// Loaded via ``<script src=... defer>`` from the run-console page's
// HEAD-level ``component_scripts`` block, which renders BEFORE
// ``vendor/alpine.min.js`` -- deferred scripts execute in document order,
// and Alpine's CDN bundle auto-starts in a microtask at the end of its
// own script task, so this listener must already be registered by then or
// the component never registers (#1692). Kept in a static file rather than
// an inline ``<script>`` block so a future nonce-based CSP needs no
// inline-script exception -- matching the chassis ``base.html`` "zero
// inline script" posture.
//
// Responsibilities:
//   * Subscribe (indirectly) to the cookie-authed SSE bridge: the HTMX
//     ``sse`` extension owns the EventSource pointed at
//     ``/ui/agents/{name}/run/stream?token=...``; this controller hooks
//     ``htmx:sse-before-message`` for each run-event kind (the
//     ``sse-swap="turn,tool_call,tool_result,final,error"`` list), parses
//     each frame, and appends a typed transcript entry instead of letting
//     the extension swap the raw JSON text into the DOM. The
//     ``preventDefault()`` is load-bearing twice over: the raw frame is
//     unreadable as transcript content, and a frame field containing
//     markup would otherwise be parsed into live DOM by the swap (the same
//     injection class the broadcast surface closed in PR #1044) --
//     ``x-text`` bindings render every field inert.
//   * Track the run lifecycle the operator cares about: the ``run_id``
//     (surfaced as a deep-link to the run detail, T3 #1830), the terminal
//     status pill (``succeeded`` / ``failed`` from ``final`` / ``error``),
//     and the ``awaiting_approval`` pause (which deep-links to the
//     approvals console, T7, rather than re-implementing decide here).
//   * No Stop affordance: closing the page closes the EventSource but does
//     not cancel the run. T9 #1833 adds the Stop button over the T8 #1828
//     cancel backend.
//
// Frame wire-shape (from ``api/v1/agent_runs._format_event``)
//   Each SSE frame is ``event: <kind>`` with ``data:`` a single-line JSON
//   object ``{ run_id, ...event.data }`` where the per-kind payload is:
//     turn         -> {}                              (a turn boundary)
//     tool_call    -> { tool_name, args }
//     tool_result  -> { tool_name, content }
//     final        -> { output }
//     error        -> { error }
//   The kind is the EventSource event ``type`` (the ``sse-swap`` name),
//   read off ``$event.detail.type``; ``$event.detail.data`` is the JSON.

document.addEventListener("alpine:init", () => {
  Alpine.data("agentRunConsole", (opts) => ({
    // The transcript starts empty and fills from the SSE stream. An array
    // literal here is never passed through the ``x-data`` attribute, so an
    // untrusted frame field can never break out of it.
    entries: [],
    connected: false,
    runId: "",
    finalStatus: "",
    awaitingApproval: false,
    // Set when the stream errors AFTER a terminal frame already landed
    // (a normal end-of-stream close) vs. before (a genuine transport
    // drop) -- distinguishes "done" from "lost the connection".
    streamErrored: false,

    // Append one frame as a typed transcript entry. Cancels the raw swap,
    // then routes by kind. A bad frame (mid-stream truncation, upstream
    // glitch) is dropped silently rather than thrown into the surface --
    // a single bad frame must not break the live transcript.
    onFrame($event) {
      const detail = $event && $event.detail;
      if (!detail) {
        return;
      }
      $event.preventDefault();
      const kind = detail.type || "";
      let frame = {};
      if (detail.data) {
        try {
          frame = JSON.parse(detail.data);
        } catch (e) {
          return;
        }
      }
      if (frame && typeof frame.run_id === "string" && !this.runId) {
        this.runId = frame.run_id;
      }
      this.appendEntry(kind, frame);
    },

    // Map a parsed frame onto the transcript entry shape the template
    // renders (``kind`` / ``title`` / ``body``). ``title`` is a short
    // mono label (e.g. a tool name); ``body`` is the pretty-printed
    // payload. Terminal kinds also flip the status pill.
    appendEntry(kind, frame) {
      let title = "";
      let body = "";
      switch (kind) {
        case "turn":
          title = "model turn";
          break;
        case "tool_call":
          title = frame.tool_name || "tool";
          body = this.stringify(frame.args);
          break;
        case "tool_result":
          title = frame.tool_name || "tool";
          body = this.stringify(frame.content);
          break;
        case "final":
          title = "final output";
          body = this.stringify(frame.output);
          this.finalStatus = "succeeded";
          break;
        case "error":
          title = "error";
          body = typeof frame.error === "string" ? frame.error : this.stringify(frame.error);
          this.finalStatus = "failed";
          break;
        default:
          // Unknown kind: render it raw rather than dropping it, so a
          // future runtime event the bridge forwards is still visible.
          title = kind;
          body = this.stringify(frame);
      }
      // An ``awaiting_approval`` signal can ride on any frame's status;
      // surface the banner if the runtime ever stamps it on a payload.
      if (frame && frame.status === "awaiting_approval") {
        this.awaitingApproval = true;
      }
      this.entries.push({ kind: kind, title: title, body: body });
    },

    // Pretty-print a JSON-able payload for the transcript body. A string
    // renders as-is (no surrounding quotes); everything else is indented
    // JSON. ``undefined`` / ``null`` render as an empty body so the
    // template's ``x-show`` hides the block.
    stringify(value) {
      if (value === undefined || value === null) {
        return "";
      }
      if (typeof value === "string") {
        return value;
      }
      try {
        return JSON.stringify(value, null, 2);
      } catch (e) {
        return String(value);
      }
    },

    // The stream errors at end-of-run too (the server closes the
    // connection after the terminal frame); only treat it as a real drop
    // when no terminal status landed. Either way, mark the stream done so
    // the connection dot stops pulsing "live".
    onStreamError() {
      this.connected = false;
      if (!this.finalStatus) {
        this.streamErrored = true;
      }
    },

    // ---- Render helpers (presentation only) ----

    statusLabel() {
      if (this.finalStatus) {
        return "complete";
      }
      if (this.streamErrored) {
        return "connection lost";
      }
      return this.connected ? "running…" : "connecting…";
    },

    statusDotClass() {
      if (this.finalStatus) {
        return "bg-success";
      }
      if (this.streamErrored) {
        return "bg-error";
      }
      return this.connected ? "bg-info animate-pulse" : "bg-warning";
    },

    finalBadgeClass() {
      return this.finalStatus === "succeeded" ? "badge-success" : "badge-error";
    },

    kindBadgeClass(kind) {
      switch (kind) {
        case "tool_call":
          return "badge-info";
        case "tool_result":
          return "badge-ghost";
        case "final":
          return "badge-success";
        case "error":
          return "badge-error";
        default:
          return "badge-outline";
      }
    },
  }));
});
