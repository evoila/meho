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
//   * Stop affordance (T9 #1833): a Stop button cancels the in-flight run
//     over the cookie-authed BFF proxy
//     ``POST /ui/agents/{name}/run/{run_id}/cancel`` (which drives the same
//     ``invoker.cancel`` the T8 #1828 REST route does). The button is
//     visible only while the run is live (a ``run_id`` has landed and no
//     terminal frame has). Confirmation is a native ``<dialog>`` (the
//     destructive-action pattern the console's siblings use). The cancel
//     POST carries the CSRF double-submit token in the ``X-CSRF-Token``
//     header explicitly -- the page-level ``hx-headers`` directive is an
//     HTMX construct and is NOT inherited by an Alpine ``fetch``, so the
//     token is threaded into the component via ``x-data`` and echoed here.
//     The proxy's 404 / 409 / 403 map to inline feedback: a 409 means the
//     run already reached a terminal state (the controller flips to
//     ``cancelled``/done rather than erroring), a 404 means the run no
//     longer exists, a 403 means the operator may not cancel it.
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

    // ---- Stop / cancel state (T9 #1833) ----
    // Threaded in via x-data so the cancel POST can build its URL + carry
    // the CSRF double-submit header (NOT inherited from the page-level
    // hx-headers, which is HTMX-only). Defaults keep the component working
    // if a caller omits them (the button just stays hidden / inert).
    agentName: (opts && opts.agentName) || "",
    csrfToken: (opts && opts.csrfToken) || "",
    cancelUrlTemplate: (opts && opts.cancelUrlTemplate) || "",
    // Operator-cancelled: set once the BFF proxy confirms the transition
    // (204) or reports the run already terminal (409). Hides the Stop
    // button and shows the cancelled pill.
    cancelled: false,
    // In-flight guard so a double-click cannot fire two cancel POSTs.
    cancelling: false,
    // Inline feedback for a 404 / 403 (an actionable message, not a torn
    // surface). A 409 is not an error -- it just means "already done".
    cancelError: "",

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

    // ---- Stop / cancel (T9 #1833) ----

    // The Stop affordance shows only while the run is live: a run_id has
    // landed (so there is something to cancel), no terminal frame has
    // arrived (final / error), the stream has not dropped, and we have
    // not already cancelled. A terminal run is not cancellable -- the BFF
    // proxy would 409 it -- so the button must vanish the moment a
    // terminal frame lands.
    canStop() {
      return (
        !!this.runId &&
        !this.finalStatus &&
        !this.cancelled &&
        !this.streamErrored &&
        !!this.cancelUrlTemplate
      );
    },

    // Cancel the in-flight run over the cookie-authed BFF proxy. Confirmed
    // by the host element's native <dialog> before this runs. POSTs with
    // the CSRF double-submit header; on 204 (cancelled) or 409 (already
    // terminal) the console transitions to cancelled and closes the
    // stream. A 404 / 403 surfaces as inline feedback.
    async cancel() {
      if (this.cancelling || !this.canStop()) {
        return;
      }
      this.cancelling = true;
      this.cancelError = "";
      const url = this.cancelUrlTemplate.replace(
        "__RUN_ID__",
        encodeURIComponent(this.runId),
      );
      let response;
      try {
        response = await fetch(url, {
          method: "POST",
          credentials: "same-origin",
          headers: { "X-CSRF-Token": this.csrfToken },
        });
      } catch (e) {
        this.cancelling = false;
        this.cancelError = "Could not reach the server to stop the run. Try again.";
        return;
      }
      this.cancelling = false;
      // 204 (cancelled) or 409 (already terminal) both mean "this run is
      // no longer running" -- transition the console and stop streaming.
      if (response.ok || response.status === 409) {
        this.markCancelled();
        return;
      }
      if (response.status === 404) {
        this.cancelError = "This run no longer exists.";
        this.markCancelled();
        return;
      }
      if (response.status === 403) {
        this.cancelError = "You do not have permission to stop this run.";
        return;
      }
      this.cancelError = "Could not stop the run. Refresh and try again.";
    },

    // Transition the console to cancelled: stop the connection-state dot
    // pulsing "live" and close the EventSource so no further frames swap.
    markCancelled() {
      this.cancelled = true;
      this.connected = false;
      this.closeStream();
    },

    // Close the EventSource the HTMX sse extension opened on the sink
    // element. The extension stores the connection on the element under
    // ``__htmx_internal_data`` (an internal contract), so we fall back to
    // dispatching ``htmx:abort`` -- the documented way to tell the sse
    // extension to close -- when the internal handle is not reachable.
    closeStream() {
      const sink = this.$root.querySelector('[hx-ext="sse"], [data-hx-ext="sse"]');
      if (!sink) {
        return;
      }
      const internal = sink.__htmx_internal_data;
      const source = internal && internal.sseEventSource;
      if (source && typeof source.close === "function") {
        source.close();
        return;
      }
      sink.dispatchEvent(new CustomEvent("htmx:abort"));
    },

    // ---- Render helpers (presentation only) ----

    statusLabel() {
      if (this.cancelled) {
        return "cancelled";
      }
      if (this.finalStatus) {
        return "complete";
      }
      if (this.streamErrored) {
        return "connection lost";
      }
      return this.connected ? "running…" : "connecting…";
    },

    statusDotClass() {
      if (this.cancelled) {
        return "bg-base-300";
      }
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
