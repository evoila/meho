// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Topology Cytoscape.js graph controller (Initiative #342; Task #881).
//
// Loaded via ``<script src=... defer>`` from ``topology/graph.html``'s
// ``{% block scripts %}`` (NOT inline) so a future nonce-based CSP needs
// no inline-script exception -- matching the chassis ``base.html``
// "zero inline script" posture.
//
// Responsibilities:
//   * Register the two extension layouts (``cose-bilkent`` /
//     ``dagre``) onto the global ``cytoscape`` factory once.
//     ``cytoscape.use(extension)`` is the official plugin API (see
//     https://js.cytoscape.org/#extensions).
//   * Read the server-emitted elements from the
//     ``#topology-graph-data`` data island (Jinja ``| tojson`` -- safe
//     for the script-element text context) and instantiate Cytoscape
//     into ``#cy``.
//   * On node tap, issue ``htmx.ajax`` GET
//     ``/ui/topology/node/<id>`` into ``#node-drawer`` (the same
//     drawer fragment the tabular view uses, per the T1 (#880)
//     contract). Center + visually select the tapped node.
//   * Layout-switcher: the ``#topology-graph-layout`` ``<select>``
//     drives ``cy.layout({name}).run()`` on change.
//   * Cross-link from the table: ``?selected=<id>`` lands as a
//     non-empty ``#topology-graph-selected`` island; on init the
//     controller centers + selects the matching node, then issues
//     the same drawer ``htmx.ajax`` as a tap would.
//   * Tabular cross-link: a tap also rewrites the page URL via
//     ``history.replaceState`` so a copy/paste reproduces the
//     selected node, AND the table-view link in the page header
//     picks up the latest selection without a re-render.
//
// All work happens on ``DOMContentLoaded`` so every vendored script
// in the ``defer`` chain has executed (the chain is order-load-
// bearing -- see VENDOR.md + graph.html script-block comments).

(function () {
  "use strict";

  // Per-tick wheel zoom step (#167). Cytoscape scales the per-tick zoom
  // exponent LINEARLY by this value (default 1.0), so bigger = more zoom
  // per scroll notch — there's no diminishing-returns ceiling until a
  // single tick would span the whole minZoom..maxZoom range (~80 here).
  // The original 0.2 was sluggish; 25.0 gives a large, brisk step per
  // scroll. This is the single knob to tune: raise for faster, lower for
  // gentler. (Any non-1.0 value emits Cytoscape's "custom wheel
  // sensitivity" console warning by design — expected, not a regression.)
  const WHEEL_SENSITIVITY = 25.0;

  function readJsonIsland(id) {
    const el = document.getElementById(id);
    if (!el) {
      return null;
    }
    try {
      return JSON.parse(el.textContent || "");
    } catch (e) {
      // A malformed island leaves the controller idle rather than
      // throwing during init -- the page still renders, the graph is
      // simply empty.
      return null;
    }
  }

  // Cytoscape style sheet. Per-kind colouring uses generic Tailwind
  // colour values rather than DaisyUI semantic tokens because the
  // canvas-rendered nodes can't read CSS custom properties resolved
  // by DaisyUI's theme system. Hard-coded hex keeps the colour
  // deterministic regardless of active theme.
  function buildStyle() {
    return [
      {
        selector: "node",
        style: {
          "background-color": "#6366f1", // indigo-500 fallback
          "label": "data(name)",
          "color": "#1f2937",
          "font-size": 10,
          "text-valign": "bottom",
          "text-halign": "center",
          "text-margin-y": 4,
          "text-wrap": "ellipsis",
          "text-max-width": 100,
          "border-width": 1,
          "border-color": "#1f2937",
          "width": 28,
          "height": 28,
        },
      },
      // Per-kind shape/colour overrides. Cytoscape selectors match
      // the ``classes`` field on the element JSON
      // (see _node_to_cy_element).
      { selector: "node.kind-target", style: { "background-color": "#0ea5e9", "shape": "round-rectangle" } },
      { selector: "node.kind-vm", style: { "background-color": "#22c55e", "shape": "ellipse" } },
      { selector: "node.kind-host", style: { "background-color": "#f97316", "shape": "rectangle" } },
      { selector: "node.kind-cluster", style: { "background-color": "#a855f7", "shape": "hexagon" } },
      { selector: "node.kind-datastore", style: { "background-color": "#facc15", "shape": "barrel" } },
      { selector: "node.kind-network", style: { "background-color": "#14b8a6", "shape": "diamond" } },
      {
        selector: "node:selected",
        style: {
          "border-width": 3,
          "border-color": "#dc2626",
        },
      },
      {
        selector: "edge",
        style: {
          "width": 1,
          "line-color": "#94a3b8",
          "target-arrow-color": "#94a3b8",
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          "label": "data(kind)",
          "font-size": 8,
          "color": "#475569",
          "text-background-color": "#f8fafc",
          "text-background-opacity": 0.85,
          "text-background-padding": 2,
        },
      },
    ];
  }

  // Layout option payload for each supported algorithm. cose-bilkent
  // and dagre carry algorithm-specific knobs the defaults pick poorly
  // for the typical inventory shape (sparse graph, ~50-500 nodes).
  //
  // ``preserveViewport`` -- when ``true`` the layout is configured to
  // keep the operator's current pan/zoom and reuse existing node
  // positions. Used on the 30s polling refresh path (G10.5-T3 #882):
  //   * ``fit: false`` -- cose-bilkent defaults to ``fit: true``,
  //     which would re-center+zoom-to-fit asynchronously after the
  //     layout settles and override any synchronous ``cy.pan/zoom``
  //     restore the caller does. ``fit: false`` keeps the viewport.
  //   * ``randomize: false`` -- re-randomising positions on every
  //     poll tick breaks the "refresh data, keep view" contract
  //     regardless of pan/zoom restore; deterministic positions
  //     preserve the operator's mental map across refreshes.
  // dagre / circle are deterministic and grid-shaped respectively, so
  // ``fit: false`` is the only knob that matters there.
  function layoutOptions(name, preserveViewport) {
    if (name === "cose-bilkent") {
      return {
        name: "cose-bilkent",
        // Wider spacing so the graph reads as distinct nodes rather than a
        // tangled clump: more inter-node repulsion + longer ideal edges push
        // neighbours apart and reduce edge crossings.
        nodeRepulsion: 8000,
        idealEdgeLength: 120,
        edgeElasticity: 0.45,
        nestingFactor: 0.1,
        gravity: 0.25,
        numIter: 2500,
        animate: false,
        fit: !preserveViewport,
        randomize: !preserveViewport,
      };
    }
    if (name === "dagre") {
      return {
        name: "dagre",
        rankDir: "TB",
        nodeSep: 40,
        rankSep: 60,
        animate: false,
        fit: !preserveViewport,
      };
    }
    // circle is built-in; defaults are sane.
    return { name: "circle", animate: false, fit: !preserveViewport };
  }

  // Push the selected node id back into the page URL so a copy/paste
  // reproduces the view. ``history.replaceState`` (NOT pushState) so a
  // back-button does not need to reverse through every tap.
  function updateSelectedInUrl(nodeId) {
    const url = new URL(window.location.href);
    if (nodeId) {
      url.searchParams.set("selected", nodeId);
    } else {
      url.searchParams.delete("selected");
    }
    window.history.replaceState({}, "", url.toString());
  }

  // Sync the "Show in table" header link's ``?selected=`` so a click
  // from the header takes the operator to the matching row.
  function updateTableLink(nodeId) {
    const link = document.querySelector('a[href^="/ui/topology?view=table"]');
    if (!link) {
      return;
    }
    const href = new URL(link.href, window.location.origin);
    if (nodeId) {
      href.searchParams.set("selected", nodeId);
    } else {
      href.searchParams.delete("selected");
    }
    link.href = href.pathname + href.search;
  }

  function openDrawerForNode(nodeId) {
    if (!nodeId || typeof window.htmx === "undefined") {
      return;
    }
    window.htmx.ajax("GET", "/ui/topology/node/" + encodeURIComponent(nodeId), {
      target: "#node-drawer",
      swap: "outerHTML",
    });
  }

  // Style extensions for overlays (G10.5-T3 #882). Path overlay
  // edges with the ``highlight`` class render in red/thick; path
  // overlay nodes with the ``highlight`` class get a bold red border
  // so the path itself is visually picked out from the rest of the
  // subgraph; the ``root`` node class (on dependents/dependencies
  // subgraphs) renders with a thicker border so the anchor is
  // obvious.
  function buildOverlayStyle() {
    return [
      {
        selector: "edge.highlight",
        style: {
          "width": 3,
          "line-color": "#dc2626",
          "target-arrow-color": "#dc2626",
        },
      },
      {
        // Path-node highlight (matches the path edges' colour; bolder
        // border + outline keeps the node visible against the per-
        // kind background fill). Without this rule the ``highlight``
        // class added by ``highlightPathNodes`` would be a visual
        // no-op.
        selector: "node.highlight",
        style: {
          "border-width": 4,
          "border-color": "#dc2626",
          "border-opacity": 1,
        },
      },
      {
        selector: "node.root",
        style: {
          "border-width": 3,
          "border-color": "#dc2626",
        },
      },
    ];
  }

  // Apply the path-node highlight on the active path nodes -- the
  // server emits the path node id list as a separate data island so
  // the JS can paint them after the elements island re-renders
  // (a path subgraph emits *only* the path's nodes + edges, so this
  // is a small bookkeeping step rather than a filter).
  function highlightPathNodes(cy, pathNodeIds) {
    if (!Array.isArray(pathNodeIds) || pathNodeIds.length === 0) {
      return;
    }
    cy.elements("node").removeClass("highlight");
    for (const id of pathNodeIds) {
      const node = cy.getElementById(id);
      if (node && node.length > 0) {
        node.addClass("highlight");
      }
    }
  }

  function init() {
    const container = document.getElementById("cy");
    if (!container || typeof window.cytoscape !== "function") {
      return;
    }

    // Register the layout extensions exactly once. ``cytoscape.use``
    // is idempotent on the official API, but guarding against a
    // re-init (e.g. browser back-forward cache restore) keeps the
    // console clean.
    if (window.cytoscapeCoseBilkent && !window.__mehoCoseBilkentRegistered) {
      window.cytoscape.use(window.cytoscapeCoseBilkent);
      window.__mehoCoseBilkentRegistered = true;
    }
    if (window.cytoscapeDagre && !window.__mehoDagreRegistered) {
      window.cytoscape.use(window.cytoscapeDagre);
      window.__mehoDagreRegistered = true;
    }

    const elements = readJsonIsland("topology-graph-data") || [];
    const selectedRaw = readJsonIsland("topology-graph-selected");
    const selectedId = typeof selectedRaw === "string" && selectedRaw.length > 0 ? selectedRaw : null;
    const pathNodeIds = readJsonIsland("topology-graph-path-nodes") || [];

    // Construct WITHOUT the ``layout`` option so no layout runs during
    // ``cytoscape({...})``. The initial layout is run explicitly below,
    // AFTER the ``?selected`` cross-link ``layoutstop`` listener is
    // attached. A constructor-supplied ``layout`` with ``animate:false``
    // completes synchronously inside the constructor call (cose-bilkent
    // repositions nodes via cytoscape's non-animated ``layoutPositions``,
    // which emits ``layoutstop`` in the same tick), so any listener
    // attached on the returned instance would miss it -- the cross-link
    // handler never fired (#142).
    const cy = window.cytoscape({
      container: container,
      elements: elements,
      style: buildStyle().concat(buildOverlayStyle()),
      wheelSensitivity: WHEEL_SENSITIVITY,
      minZoom: 0.1,
      maxZoom: 4,
    });

    highlightPathNodes(cy, pathNodeIds);

    // Expose for debugging / a future ``/auto-implement-initiative``
    // E2E harness; non-enumerable so it stays out of the
    // operator-visible globals listing.
    Object.defineProperty(window, "__mehoCy", { value: cy, configurable: true });

    // Node-tap -> drawer swap + URL sync + table-link sync.
    cy.on("tap", "node", function (event) {
      const node = event.target;
      const nodeId = node.id();
      cy.elements("node:selected").unselect();
      node.select();
      openDrawerForNode(nodeId);
      updateSelectedInUrl(nodeId);
      updateTableLink(nodeId);
    });

    // Background tap clears the selection (consistent with macOS Finder
    // / most graph editors). The drawer slot keeps its last content so
    // the operator can still scroll the previously-opened detail.
    cy.on("tap", function (event) {
      if (event.target === cy) {
        cy.elements("node:selected").unselect();
        updateSelectedInUrl(null);
        updateTableLink(null);
      }
    });

    // Layout switcher. A deliberate user-driven layout change should
    // re-fit + re-randomize -- the operator explicitly asked for a
    // new arrangement, so the cose-bilkent / dagre / circle default
    // shape is the desired outcome.
    const layoutSelect = document.getElementById("topology-graph-layout");
    if (layoutSelect) {
      layoutSelect.addEventListener("change", function () {
        cy.layout(layoutOptions(layoutSelect.value, false)).run();
      });
    }

    // Cross-link from the table: if the page arrived with
    // ``?selected=<id>``, center + visually select that node + open
    // its drawer once the layout is settled. The listener MUST be
    // registered BEFORE the initial layout runs (below): a
    // ``animate:false`` cose-bilkent pass emits ``layoutstop`` in the
    // same synchronous turn as ``layout.run()``, so a listener attached
    // afterwards would never fire (#142). ``layoutstop`` gives the node
    // a final position to center on.
    if (selectedId) {
      cy.one("layoutstop", function () {
        const node = cy.getElementById(selectedId);
        if (node && node.length > 0) {
          node.select();
          cy.center(node);
          openDrawerForNode(selectedId);
        }
      });
    }

    // Run the initial layout now that every init-time listener --
    // including the ``?selected`` cross-link ``layoutstop`` handler
    // above -- is attached. Same shape as the old constructor
    // ``layout`` option: fit-to-canvas + randomized starting positions
    // so cose-bilkent finds a clean arrangement for first-seen data.
    cy.layout(layoutOptions("cose-bilkent", false)).run();

    // ----- G10.5-T3 (#882) polling-refresh handler -----
    //
    // The data island wrapper carries
    // ``hx-trigger="every 30s"`` + ``hx-swap="outerHTML"``. When
    // HTMX swaps in the new wrapper, we re-read the elements island
    // and replace the Cytoscape graph in place, preserving the
    // operator's current pan + zoom (the layout re-run would
    // otherwise center the graph and zoom-fit, throwing the
    // operator off the node they were inspecting).
    //
    // The handler is bound on ``document.body`` so it survives the
    // wrapper element being replaced (HTMX rebuilds it on every
    // swap). The ``detail.target`` check pins it to the topology
    // graph wrapper -- other surfaces' HTMX swaps on the same page
    // are no-ops here.
    function applyRefreshedIsland() {
      const fresh = readJsonIsland("topology-graph-data");
      if (!Array.isArray(fresh)) {
        return;
      }
      cy.batch(function () {
        cy.elements().remove();
        cy.add(fresh);
      });
      // Re-run the layout with ``preserveViewport=true`` so it lays
      // out incoming nodes WITHOUT re-fitting the canvas (cose-bilkent
      // defaults to ``fit: true`` which would zoom-to-fit
      // asynchronously and override any synchronous pan/zoom restore
      // a previous version of this code attempted) AND without
      // re-randomising positions (the operator's mental map of the
      // graph must survive each 30s tick). This is the "refresh data,
      // keep view" contract documented in #882.
      const layoutSelectEl = document.getElementById("topology-graph-layout");
      const layoutName = layoutSelectEl ? layoutSelectEl.value : "cose-bilkent";
      cy.layout(layoutOptions(layoutName, true)).run();
      // Re-apply the path highlight if the new payload carries one.
      const newPathNodes = readJsonIsland("topology-graph-path-nodes") || [];
      highlightPathNodes(cy, newPathNodes);
    }

    document.body.addEventListener("htmx:afterSwap", function (event) {
      const target = event && event.detail ? event.detail.target : null;
      if (!target || target.id !== "topology-graph-data-wrapper") {
        return;
      }
      applyRefreshedIsland();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
