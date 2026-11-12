// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Keeps the Run button of the operations Run dialog disabled while its
// run goes on (#5111), so one click is one run.
//
// The result region ``#operations-run-result-region`` holds the view of
// the run. While the run goes on, the view has an element with
// ``data-run-lock`` (``operations/_run_state.html``). When new content
// reaches the region, the Run button is disabled when such an element is
// there, and enabled when it is not (the run ended, or the form had an
// error). The change is made one tick later: htmx enables the button
// again when its own request ends, right after the swap.
// Plain JS, loaded with ``defer`` from ``base.html``.

(() => {
  document.body.addEventListener("htmx:load", (event) => {
    const el = event.detail && event.detail.elt;
    const region = el && el.closest ? el.closest("#operations-run-result-region") : null;
    if (!region) {
      return;
    }
    window.setTimeout(() => {
      const dialog = region.closest("dialog");
      const button = dialog && dialog.querySelector("button[data-action='run']");
      if (button) {
        button.disabled = region.querySelector("[data-run-lock]") !== null;
      }
    }, 0);
  });
})();
