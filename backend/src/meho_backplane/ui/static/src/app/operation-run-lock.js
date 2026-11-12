// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Keeps the Run button of the operations Run dialog disabled while its
// run goes on (#5111), so one click is one run.
//
// The result region ``#operations-run-result-region`` holds the view of
// the run. While the run goes on, the view has an element with
// ``data-run-lock`` (``operations/_run_state.html``). Three listeners on
// <body> do the work:
//
// - ``htmx:load``: when new content reaches the region, the Run button is
//   disabled when such an element is there, and enabled when it is not
//   (the run ended, or the form had an error). The change is made one
//   tick later: htmx enables the button again when its own request ends,
//   right after the swap.
// - ``htmx:beforeRequest``: a request that the Run form sends while the
//   region still holds a run that goes on is stopped. The asking of the
//   view itself (it sits inside the region) is never stopped.
// - ``htmx:beforeSwap``: a form error is an HTML answer with status 400.
//   htmx 2.0.9 does not swap an error answer (``responseHandling``
//   classifies ``[45]..`` as ``{swap: false, error: true}``), so without
//   this the error never shows, the view keeps asking and Run stays
//   disabled. For a 400 HTML answer swapped into the region (by the Run
//   form or by the asking view), the swap is turned on. The error view has
//   no ``hx-get``, so the asking stops.
// Plain JS, loaded with ``defer`` from ``base.html``.

(() => {
  const REGION_ID = "operations-run-result-region";

  const resultRegion = (el) =>
    el && typeof el.closest === "function" ? el.closest(`#${REGION_ID}`) : null;

  const runGoesOn = (region) => region.querySelector("[data-run-lock]") !== null;

  const runButton = (region) => {
    const dialog = region.closest("dialog");
    return dialog ? dialog.querySelector("button[data-action='run']") : null;
  };

  const isHtmlResponse = (xhr) => {
    const contentType = (xhr && xhr.getResponseHeader && xhr.getResponseHeader("content-type")) || "";
    return contentType.toLowerCase().startsWith("text/html");
  };

  const onLoad = (event) => {
    const region = resultRegion(event.detail && event.detail.elt);
    if (!region) {
      return;
    }
    window.setTimeout(() => {
      const button = runButton(region);
      if (button) {
        button.disabled = runGoesOn(region);
      }
    }, 0);
  };

  // Returns true when it stopped the request, so a test can check it.
  const onBeforeRequest = (event) => {
    const detail = event.detail;
    if (!detail || resultRegion(detail.elt)) {
      return false;
    }
    const region = resultRegion(detail.target);
    if (!region || !runGoesOn(region)) {
      return false;
    }
    event.preventDefault();
    return true;
  };

  // Returns true when it turned the swap on, so a test can check it.
  const onBeforeSwap = (event) => {
    const detail = event.detail;
    if (!detail || !detail.xhr || detail.shouldSwap || detail.xhr.status !== 400) {
      return false;
    }
    if (!resultRegion(detail.target) || !isHtmlResponse(detail.xhr)) {
      return false;
    }
    detail.shouldSwap = true;
    detail.isError = false;
    return true;
  };

  document.body.addEventListener("htmx:load", onLoad);
  document.body.addEventListener("htmx:beforeRequest", onBeforeRequest);
  document.body.addEventListener("htmx:beforeSwap", onBeforeSwap);
})();
