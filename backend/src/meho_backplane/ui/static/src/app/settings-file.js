// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Shows an error answer of the settings file page.
//
// The upload form and the Apply form of the settings file page carry
// ``data-settings-form``. MEHO answers a refused file with an HTML fragment
// and status 403, and a failed audit write with an HTML fragment and status
// 500. Other errors (the CSRF check, a role refusal, a server error) are not
// HTML. htmx 2.0.9 does not swap an error answer (``responseHandling``
// classifies ``[45]..`` as ``{swap: false, error: true}``), so without this
// module the page would keep the old preview and show nothing.
//
// This module listens for ``htmx:beforeSwap`` on <body>. For an error answer
// to one of these forms it turns the swap on: an HTML answer is shown as it
// is, any other answer is replaced with a fixed error text. A 401 is left to
// ``session-expiry.js``. Every other answer is left as it is.
//
// Loaded with ``defer`` from the page's ``scripts`` block. Plain first-party
// JS, no inline script, so the console's CSP posture holds.

(function () {
  "use strict";

  var FORM_ATTR = "data-settings-form";

  // True when the request came from one of the two forms of the page.
  function fromSettingsForm(detail) {
    var config = detail && detail.requestConfig;
    var elt = config && config.elt;
    return !!(elt && typeof elt.closest === "function" && elt.closest("[" + FORM_ATTR + "]"));
  }

  function isHtml(xhr) {
    var contentType = xhr.getResponseHeader("content-type") || "";
    return contentType.toLowerCase().indexOf("text/html") === 0;
  }

  // The fixed error text. The status is a number, so the text holds no
  // value from the answer.
  function errorAlert(status) {
    return (
      '<div class="alert alert-error" role="alert" data-settings-error><span>' +
      "MEHO could not finish this step (HTTP " +
      String(Number(status)) +
      "). The file may or may not be saved: read the settings history " +
      "before you apply it again.</span></div>"
    );
  }

  // Turn the swap on for an error answer of the page. Returns true when it
  // did, so a test can check the decision.
  function onBeforeSwap(event) {
    var detail = event && event.detail;
    var xhr = detail && detail.xhr;
    if (!xhr || typeof xhr.getResponseHeader !== "function") {
      return false;
    }
    if (xhr.status < 400 || xhr.status === 401 || !fromSettingsForm(detail)) {
      return false;
    }
    if (!isHtml(xhr)) {
      detail.serverResponse = errorAlert(xhr.status);
    }
    detail.shouldSwap = true;
    detail.isError = false;
    return true;
  }

  document.body.addEventListener("htmx:beforeSwap", onBeforeSwap);

  // Exposed so a test can drive the handler with a synthetic event.
  window.mehoSettingsFile = { onBeforeSwap: onBeforeSwap };
})();
