// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Shows the answer of a refused Docs Corpus collection action.
//
// The Re-probe button, the Enable button and the Disable confirm form on
// the collection detail page carry ``data-collection-action``. When MEHO
// refuses one of these actions, it answers with an HTML alert fragment
// (``corpus/_probe_alert.html``) and an error status: 403 for a global
// collection that needs a platform admin, 409 for a forbidden status
// change, 503 for an unreachable backend. htmx 2.0.9 does not swap an
// error response (``responseHandling`` classifies ``[45]..`` as
// ``{swap: false, error: true}``), so without this module the alert never
// reaches the page and the operator sees a dead button.
//
// This module listens for ``htmx:beforeSwap`` on <body> and, for a response
// to one of these controls that is an error AND an HTML fragment, turns the
// swap on. The target and swap style come from the control itself: every
// action control targets ``#collection-readiness-card`` with ``outerHTML``,
// and the alert fragment carries that same id, so the alert replaces the
// readiness card and a later successful re-probe swaps the card back.
// A JSON error (a session gone, the CSRF check, a role refusal) has no
// HTML content type and is left to the app-shell handlers; a response to
// any other element is never touched.
//
// When the refused action came from the Disable confirm modal, the modal is
// closed first so the alert in the readiness card is not hidden behind it.
//
// Loaded with ``defer`` from the detail page's ``scripts`` block. Plain
// first-party JS, no inline script, so the console's CSP posture holds.

(function () {
  "use strict";

  var ACTION_ATTR = "data-collection-action";
  var CARD_ID = "collection-readiness-card";
  var DISABLE_MODAL_ID = "corpus-disable-modal";

  // The action control that issued the request, or ``null`` when the
  // request came from any other element.
  function actionControl(elt) {
    if (!elt || typeof elt.closest !== "function") {
      return null;
    }
    return elt.closest("[" + ACTION_ATTR + "]");
  }

  // True when the response body is an HTML fragment made for the page.
  function isHtmlResponse(xhr) {
    if (!xhr || typeof xhr.getResponseHeader !== "function") {
      return false;
    }
    var contentType = xhr.getResponseHeader("content-type") || "";
    return contentType.toLowerCase().indexOf("text/html") === 0;
  }

  function closeDisableModal() {
    var modal = document.getElementById(DISABLE_MODAL_ID);
    if (modal && modal.open && typeof modal.close === "function") {
      modal.close();
    }
  }

  // Turn the swap on for an error answer to an action control. Returns
  // true when it did, so a test can check the decision.
  function onBeforeSwap(event) {
    var detail = event && event.detail;
    if (!detail || !detail.xhr || detail.shouldSwap) {
      return false;
    }
    if (!actionControl(detail.elt)) {
      return false;
    }
    if (detail.xhr.status < 400 || !isHtmlResponse(detail.xhr)) {
      return false;
    }
    detail.shouldSwap = true;
    detail.isError = false;
    closeDisableModal();
    return true;
  }

  document.body.addEventListener("htmx:beforeSwap", onBeforeSwap);

  // Exposed so a test can drive the handler with a synthetic event.
  window.mehoCorpusCollectionActions = {
    onBeforeSwap: onBeforeSwap,
    actionControl: actionControl,
    isHtmlResponse: isHtmlResponse,
    ACTION_ATTR: ACTION_ATTR,
    CARD_ID: CARD_ID,
    DISABLE_MODAL_ID: DISABLE_MODAL_ID,
  };
})();
