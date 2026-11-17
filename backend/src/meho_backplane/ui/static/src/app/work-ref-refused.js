// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Shows why a ticket id search value was refused.
//
// The ticket id (work_ref) search boxes of the agent runs, scheduler, audit
// and approvals pages, and the approvals "in order" window, answer a value
// that breaks the ticket id rule with their usual HTML (the list with no
// rows and the rule text, or the window with the rule text) and status 422.
// That answer carries the header ``Meho-Work-Ref-Refused``. htmx 2.0.9 does
// not swap an error answer (``responseHandling`` classifies ``[45]..`` as
// ``{swap: false, error: true}``), so without this module a search-as-you-
// type box would show no change at all.
//
// This module listens for ``htmx:beforeSwap`` on <body> and turns the swap
// on for a 422 HTML answer with that header. Every other answer is left as
// it is.
//
// Loaded with ``defer`` from ``_head_assets.html`` on every console page.
// Plain first-party JS, no inline script, so the console's CSP posture holds.

(function () {
  "use strict";

  var HEADER = "Meho-Work-Ref-Refused";

  // True when the answer is a refused ticket id search value.
  function isRefusal(xhr) {
    if (!xhr || typeof xhr.getResponseHeader !== "function" || xhr.status !== 422) {
      return false;
    }
    var contentType = xhr.getResponseHeader("content-type") || "";
    return (
      xhr.getResponseHeader(HEADER) === "1" &&
      contentType.toLowerCase().indexOf("text/html") === 0
    );
  }

  // Turn the swap on for a refusal. Returns true when it did, so a test can
  // check the decision.
  function onBeforeSwap(event) {
    var detail = event && event.detail;
    if (!detail || detail.shouldSwap || !isRefusal(detail.xhr)) {
      return false;
    }
    detail.shouldSwap = true;
    detail.isError = false;
    return true;
  }

  document.body.addEventListener("htmx:beforeSwap", onBeforeSwap);

  // Exposed so a test can drive the handler with a synthetic event.
  window.mehoWorkRefRefused = { onBeforeSwap: onBeforeSwap };
})();
