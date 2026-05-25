// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Topology table cross-link helper (Initiative #342; Task #881).
//
// Loaded via ``<script src=... defer>`` from ``topology/table.html``'s
// ``{% block scripts %}`` (NOT inline) so the chassis CSP posture
// (zero inline JS) needs no exception.
//
// Single responsibility: when the table is rendered with a
// ``?selected=<id>`` cross-link payload from the graph view, find the
// ``<tr data-selected="true">`` row (T2 / #881; emitted by
// ``_table_rows.html`` when ``selected_id`` matches), scroll it into
// view, and apply a brief highlight pulse so the operator's eye
// catches it. No-ops cleanly when nothing matches (the operator
// arrived on the table view without a selection, or the selected id
// belongs to another tenant and the row therefore did not render).

(function () {
  "use strict";

  function highlightSelectedRow() {
    const row = document.querySelector('tr[data-selected="true"]');
    if (!row) {
      return;
    }
    // ``scrollIntoView({block: "center"})`` matches the same UX as
    // most search-result navigation -- the row lands roughly mid-screen
    // so the operator can see its neighbours, not at the top edge.
    row.scrollIntoView({ block: "center", behavior: "smooth" });
    // Brief outline pulse via a one-shot CSS animation. The class is
    // removed after 2s so a re-render does not stack copies.
    row.classList.add("ring", "ring-primary", "ring-offset-2");
    window.setTimeout(function () {
      row.classList.remove("ring", "ring-primary", "ring-offset-2");
    }, 2000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", highlightSelectedRow);
  } else {
    highlightSelectedRow();
  }
})();
