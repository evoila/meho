// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Broadcast live-feed Alpine controller (Initiative #338; Task #867
// shipped the core, Task #868 added the op_id client filter + the
// click-to-open drawer + the PII 🔒 marker helper).
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
// Responsibilities:
//   * Subscribe (indirectly) to the SSE feed: the HTMX ``sse`` extension
//     owns the EventSource; this controller hooks
//     ``htmx:sseBeforeMessage`` to parse each ``event: broadcast`` frame
//     and route it into the ``events`` array instead of letting the
//     extension swap the raw JSON text into the DOM.
//   * Prepend newest-first and trim to the in-DOM row cap (work item #9).
//   * Apply the op_id client-side filter (work item #3): the stream has
//     no op_id parameter, so op_id narrows the streamed events in-browser
//     via ``visibleEvents``. The filter bar's op_id input dispatches a
//     ``broadcast-op-id-changed`` window event this controller listens for.
//     On a server-side filter re-render (op_class/principal/target change)
//     HTMX swaps ``#broadcast-feed`` and this controller re-initialises
//     with a fresh, empty ``opIdFilter`` -- so ``init`` re-reads the live
//     op_id input (which lives OUTSIDE the swapped fragment and keeps the
//     operator's typed value) and re-seeds the filter. Without that read
//     the op_id input would still show the typed text but active filtering
//     would silently stop after every server re-render.
//   * Open the event-detail drawer on a row click (work item #4):
//     ``htmx.ajax`` GET ``/ui/broadcast/event/{audit_id}?event_id=...``
//     into ``#event-drawer``.
//   * Provide the row-render helpers the server-authored
//     ``_event_row.html`` partial binds to (badge colour, timestamp,
//     payload summary, aggregate-only check).
//
// The colour table, cap, and the initial op_id filter are passed in from
// the route context (``opts``) so the policy stays server-side.
//
// Two later (T3 #869) opts let the SAME component back three surfaces:
//   * ``opts.seedFrom`` -- the id of a ``<script type="application/json">``
//     data island holding the seed events array. The live feed omits it
//     and fills ``events`` from the SSE stream; the Last-24h replay pane
//     (``_history.html``) points it at ``#broadcast-history-data``, whose
//     ``textContent`` the controller ``JSON.parse``s on init, so a history
//     row renders + opens the drawer identically to a live row. The seed
//     events ride in a script-block data island (NOT the ``x-data``
//     attribute) so an event field containing ``"`` / ``'`` / ``<`` /
//     ``>`` / ``&`` / ``</script>`` cannot break out of the attribute and
//     inject markup -- Jinja's ``| tojson`` escapes all of those (plus
//     U+2028/U+2029) for the script-element text context (B1, PR #1044).
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
    autoScroll: opts.autoScroll === true,
    cap: opts.cap,
    badgeClasses: opts.badgeClasses,
    // Lower-cased once; the op_id filter is a case-insensitive substring
    // match against ``ev.op_id``. Seeded from the server context so a
    // copy-pasted filtered URL renders the narrowed view on first paint.
    // ``init`` (below) then overrides this from the live op_id input so a
    // server re-render -- which omits op_id and therefore seeds an empty
    // ``opts.opIdFilter`` -- does not drop the operator's active filter.
    opIdFilter: (opts.opIdFilter || "").toLowerCase(),

    // Alpine invokes ``init`` automatically when the component mounts --
    // on the initial page load AND on every HTMX swap of the
    // ``#broadcast-feed`` fragment (a server-side op_class/principal/target
    // re-render). The op_id ``<input>`` lives outside the swapped fragment,
    // so it survives the swap with the operator's typed value intact; the
    // server fragment route, however, never receives op_id (it is excluded
    // from the form's ``hx-include``) and so re-seeds ``opIdFilter`` empty.
    // Reading the input here makes that input the single source of truth so
    // the client-side narrowing keeps applying across server re-renders.
    // ``$nextTick`` defers the read until Alpine has settled the swapped
    // node, guarding against any swap/init ordering edge.
    init() {
      // Seed the replay pane from its ``<script type="application/json">``
      // data island (the live feed omits ``seedFrom`` and stays empty
      // until the SSE stream fills it). The events arrive as inert script
      // text -- never as an ``x-data`` attribute -- so a ``"`` / ``<`` /
      // ``</script>``-bearing event field cannot break out and inject
      // markup (B1, PR #1044).
      this.seedFromIsland();

      // The op_id input + its server-swap reconciliation belong to the
      // LIVE feed only (``#broadcast-feed``). The Last-24h replay pane
      // (``#broadcast-history-pane``) is a separate controller instance
      // with no filter bar; re-reading the live feed's op_id input there
      // would wrongly leak the live filter onto the history rows. Gate
      // the re-read on the live-feed element id so each surface keeps
      // its own filter state.
      if (this.$el.id !== "broadcast-feed") {
        return;
      }
      this.$nextTick(() => {
        const input = document.querySelector('input[name="op_id"]');
        if (input) {
          this.opIdFilter = (input.value || "").toLowerCase();
        }
      });
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

    // Re-apply the op_id filter when the filter bar's input changes. The
    // ``.window`` listener is declared in the template; this handler
    // normalises + stores the new value. ``$event.detail.opId`` is the
    // payload the filter bar's ``$dispatch`` sends.
    onOpIdChanged(evt) {
      const next = (evt.detail && evt.detail.opId) || "";
      this.opIdFilter = next.toLowerCase();
    },

    // The op_id-filtered view of ``events`` the template renders. An
    // empty filter shows every streamed event; a non-empty filter keeps
    // only events whose ``op_id`` contains the substring. Recomputed by
    // Alpine reactivity whenever ``events`` or ``opIdFilter`` changes.
    get visibleEvents() {
      if (!this.opIdFilter) {
        return this.events;
      }
      return this.events.filter(
        (ev) => (ev.op_id || "").toLowerCase().includes(this.opIdFilter),
      );
    },

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

    // Open the event-detail drawer for a clicked row (work item #4). The
    // drawer is resolved by AUDIT id (the canonical PG row), not the
    // broadcast event_id (ephemeral Valkey id) -- see the event route's
    // docstring. The broadcast event_id rides along as a query param for
    // display only. ``htmx.ajax`` issues the GET and swaps the response
    // into ``#event-drawer`` (outerHTML) so the returned fragment's own
    // ``id="event-drawer"`` replaces the slot.
    openDrawer(ev) {
      if (!ev || !ev.audit_id) {
        return;
      }
      const params = new URLSearchParams();
      if (ev.event_id) {
        params.set("event_id", ev.event_id);
      }
      const qs = params.toString();
      const url =
        "/ui/broadcast/event/" +
        encodeURIComponent(ev.audit_id) +
        (qs ? "?" + qs : "");
      window.htmx.ajax("GET", url, {
        target: "#event-drawer",
        swap: "outerHTML",
      });
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

    // True when the event is aggregate-only (credential reads, audit
    // queries -- decision #3): its redacted payload carries no ``params``
    // key. The row renders the 🔒 marker + placeholder for these instead
    // of a param summary. Same signal ``payloadSummary`` keys off, lifted
    // to a named predicate the template's PII branch reads.
    isAggregateOnly(ev) {
      const p = (ev && ev.payload) || {};
      return !("params" in p);
    },

    // One-line payload summary for non-aggregate events. Aggregate-only
    // events never reach here (the template branches on
    // ``isAggregateOnly`` first), but the guard is kept so a direct call
    // stays safe.
    payloadSummary(ev) {
      const p = (ev && ev.payload) || {};
      if (!("params" in p)) {
        return "(aggregate-only)";
      }
      try {
        return JSON.stringify(p.params);
      } catch (e) {
        return "(aggregate-only)";
      }
    },

    // True when the event is an agent-authored announcement
    // (``meho.broadcast.announce``) rather than an audit-driven
    // operation. Two event kinds now share the stream (G6.4-T2 / #2549);
    // the row partial branches on this so an announcement renders its
    // agent-authored variant (phase chip + quoted activity + the #2544
    // claim fields) instead of blank audit cells. Reads the top-level
    // ``kind`` discriminator, falling back to the historical
    // ``event_kind`` alias for v0.8.0 in-flight frames.
    isAnnouncement(ev) {
      const kind = ev && (ev.kind || ev.event_kind);
      return kind === "agent_announcement";
    },

    // A stable ``x-for`` key for one row. Audit events key on their
    // durable ``event_id``; history rows also carry the Valkey stream
    // ``cursor`` / ``id``. Live announcement frames carry none of those
    // (announcements mint no UUID pre-T2), so fall back to a composite of
    // the fields that identify one announcement on the tenant stream.
    // Without this, ``:key="ev.event_id"`` would collapse every live
    // announcement onto the ``undefined`` key and Alpine would reconcile
    // them into a single row.
    rowKey(ev) {
      if (!ev) {
        return "";
      }
      return (
        ev.event_id ||
        ev.cursor ||
        ev.id ||
        [ev.kind || "op", ev.ts || "", ev.principal_sub || "", ev.activity || ""].join("|")
      );
    },

    // Accessible label for a row. Audit rows name the op_id; announcement
    // rows name the phase so the announcement variant does not read
    // "Inspect event undefined".
    rowAriaLabel(ev) {
      if (this.isAnnouncement(ev)) {
        return "Agent announcement (" + ((ev && ev.phase) || "update") + ")";
      }
      return "Inspect event " + ((ev && ev.op_id) || "");
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
