// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Broadcast live-feed Alpine controller (Initiative #338; Task #867
// shipped the core; since #3762 it shows agent announcements only).
//
// Registered on ``alpine:init`` so the ``x-data="broadcastFeed(...)"``
// wrapper in ``broadcast/_feed.html`` resolves to this component. Loaded
// via ``<script src=... defer>`` from the page's HEAD-level
// ``component_scripts`` block (wall.html: directly in its head), which
// renders BEFORE ``vendor/alpine.min.js`` -- deferred scripts execute in
// document order, and Alpine's CDN bundle auto-starts in a microtask at
// the end of its own script task, so this listener must already be
// registered by then or the component never registers (#1692). Kept in
// a static file rather than an inline ``<script>`` block so a future
// nonce-based CSP needs no inline-script exception -- matching the
// chassis ``base.html`` "zero inline script" posture.
//
// Broadcast carries only announcements that trusted agents publish on
// purpose (#3736, #3758). People read them; they cannot publish here.
// Operations are not on Broadcast: the audit log is their record.
//
// Responsibilities:
//   * Subscribe (indirectly) to the SSE feed: the HTMX ``sse`` extension
//     owns the EventSource; this controller hooks
//     ``htmx:sseBeforeMessage`` to parse each ``event: broadcast`` frame
//     and route it into the ``events`` array instead of letting the
//     extension swap the raw JSON text into the DOM.
//   * Prepend newest-first and trim to the in-DOM row cap (work item #9).
//   * Show only announcements (``visibleEvents``): a frame of any other
//     kind is never rendered.
//   * Provide the row-render helpers the server-authored
//     ``_event_row.html`` partial binds to (timestamp, phase badge,
//     targets, claim details).
//
// The cap is passed in from the route context (``opts``) so the policy
// stays server-side.
//
// Two later (T3 #869) opts let the SAME component back three surfaces:
//   * ``opts.seedFrom`` -- the id of a ``<script type="application/json">``
//     data island holding the seed events array. The live feed omits it
//     and fills ``events`` from the SSE stream; the Last-24h replay pane
//     (``_history.html``) points it at ``#broadcast-history-data``, whose
//     ``textContent`` the controller ``JSON.parse``s on init, so a history
//     row renders identically to a live row. The seed events ride in a
//     script-block data island (NOT the ``x-data`` attribute) so an event
//     field containing ``"`` / ``'`` / ``<`` / ``>`` / ``&`` /
//     ``</script>`` cannot break out of the attribute and inject markup
//     -- Jinja's ``| tojson`` escapes all of those (plus U+2028/U+2029)
//     for the script-element text context (B1, PR #1044).
//   * ``opts.autoScroll`` -- when true (the wall-monitor feed), the
//     controller keeps the newest event (the top row, since events
//     ``unshift``) in view as the stream prepends, so a full-screen
//     team-room monitor always shows the latest activity without manual
//     scrolling.

document.addEventListener("alpine:init", () => {
  Alpine.data("broadcastFeed", (opts) => ({
    // The live feed starts empty and fills ``events`` from the SSE
    // stream. The Last-24h replay pane instead seeds these on ``init``
    // from the ``opts.seedFrom`` data island (see ``seedFromIsland``);
    // an array literal here is never passed through the ``x-data``
    // attribute, so untrusted event fields can never break out of it.
    events: [],
    connected: false,

    // The sse extension fires ``htmx:sseError`` on every stream error, with
    // no status code. After a tenant switch in another tab the server
    // refuses the stream URL (its ``meho_tenant`` marker is old) with 409
    // and the EventSource stops. ``page-tenant.js`` finds that out with one
    // HEAD probe and shows the reload banner; a plain network error shows
    // nothing and the stream keeps retrying (#5240).
    onSseError() {
      this.connected = false;
      var helper = window.mehoPageTenant;
      if (helper && typeof helper.probeTenantChanged === "function") {
        helper.probeTenantChanged();
      }
    },
    autoScroll: opts.autoScroll === true,
    cap: opts.cap,

    // Alpine invokes ``init`` automatically when the component mounts.
    init() {
      // Seed the replay pane from its ``<script type="application/json">``
      // data island (the live feed omits ``seedFrom`` and stays empty
      // until the SSE stream fills it). The events arrive as inert script
      // text -- never as an ``x-data`` attribute -- so a ``"`` / ``<`` /
      // ``</script>``-bearing event field cannot break out and inject
      // markup (B1, PR #1044).
      this.seedFromIsland();
    },

    // Seed ``events`` from a ``<script type="application/json">`` data
    // island whose id is ``opts.seedFrom``. The island's ``textContent``
    // is JSON the server emitted via Jinja ``| tojson`` (XSS-safe for the
    // script-element text context). No-op when ``seedFrom`` is unset (the
    // live feed) or the element / JSON is missing or malformed -- the
    // pane then renders its empty state rather than tearing down.
    seedFromIsland() {
      if (!opts.seedFrom) {
        return;
      }
      const el = document.getElementById(opts.seedFrom);
      if (!el) {
        return;
      }
      try {
        const parsed = JSON.parse(el.textContent || "[]");
        if (Array.isArray(parsed)) {
          this.events = parsed;
        }
      } catch (e) {
        // A malformed island leaves ``events`` empty (the pane shows its
        // empty state) rather than throwing during component init.
      }
    },

    // The rows the template renders: announcements only. A frame of any
    // other kind (an old operation event, a malformed object) is never
    // shown. Recomputed by Alpine reactivity whenever ``events`` changes.
    get visibleEvents() {
      return this.events.filter((ev) => this.isAnnouncement(ev));
    },

    // Parse one SSE ``event: broadcast`` frame and prepend it, trimming
    // to the in-DOM cap. ``$event.detail`` is the MessageEvent the sse
    // extension passes through ``htmx:sseBeforeMessage``; ``.data`` is
    // the announcement JSON. ``preventDefault`` stops the extension
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
      // Wall-monitor auto-scroll: newest events ``unshift`` to the top,
      // so "keep the latest in view" means scroll the list to its top.
      // ``$nextTick`` waits for Alpine to render the prepended row before
      // adjusting scrollTop; the ``$refs.list`` ref is only present in
      // the wall feed fragment (``wall=True``).
      if (this.autoScroll && this.$refs.list) {
        this.$nextTick(() => {
          this.$refs.list.scrollTop = 0;
        });
      }
    },

    // Locale time string for the row timestamp; falls back to the raw
    // ISO value if it can't be parsed.
    formatTs(ts) {
      const d = new Date(ts);
      return isNaN(d.getTime()) ? ts : d.toLocaleTimeString();
    },

    // True when the event is an agent-authored announcement
    // (``meho_broadcast_announce``). Reads the top-level ``kind``
    // discriminator, falling back to the historical ``event_kind`` alias.
    isAnnouncement(ev) {
      const kind = ev && (ev.kind || ev.event_kind);
      return kind === "agent_announcement";
    },

    // A stable ``x-for`` key for one row: the announcement's ``event_id``,
    // else its ``cursor`` / ``id``, else a composite of the fields that
    // identify one announcement. Without the fallback, rows without an id
    // would share the ``undefined`` key and Alpine would merge them.
    rowKey(ev) {
      if (!ev) {
        return "";
      }
      return (
        ev.event_id ||
        ev.cursor ||
        ev.id ||
        [ev.kind || "", ev.ts || "", ev.principal_sub || "", ev.activity || ""].join("|")
      );
    },

    // Accessible label for a row: names the phase.
    rowAriaLabel(ev) {
      return "Agent announcement (" + ((ev && ev.phase) || "update") + ")";
    },

    // DaisyUI badge variant for an announcement phase. ``start`` (intent)
    // and ``completion`` (wrap-up) stand out; ``update`` (the default) is
    // the neutral ghost. Unknown values fall back to ghost.
    phaseBadgeClass(phase) {
      switch (phase) {
        case "start":
          return "badge-info";
        case "completion":
          return "badge-success";
        default:
          return "badge-ghost";
      }
    },

    // Comma-joined target attribution for an announcement: the single
    // ``target`` plus any of the ``targets[]`` list (#2544). Empty when
    // the announcement is target-less. Each value is rendered through
    // ``x-text`` (escaped) by the template, so this only assembles the
    // string; it never touches innerHTML.
    announcementTargets(ev) {
      const out = [];
      if (ev && ev.target) {
        out.push(ev.target);
      }
      if (ev && Array.isArray(ev.targets)) {
        for (const t of ev.targets) {
          if (t) {
            out.push(t);
          }
        }
      }
      return out.join(", ");
    },

    // Compact claim-metadata summary for an announcement's payload column:
    // the declared op-class, the TTL (minutes), and the work_ref, in that
    // order, joined by a middot. Trusted structured fields
    // (``planned_op_class`` / ``ttl_minutes``) render clean; ``work_ref``
    // is agent-authored but rendered escaped via ``x-text``.
    announcementMeta(ev) {
      const parts = [];
      if (ev && ev.planned_op_class) {
        parts.push("will " + ev.planned_op_class);
      }
      if (ev && ev.ttl_minutes != null) {
        parts.push("TTL " + ev.ttl_minutes + "m");
      }
      if (ev && ev.work_ref) {
        parts.push(ev.work_ref);
      }
      return parts.join(" · ");
    },
  }));
});
