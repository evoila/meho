// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// App-shell native-<dialog> modal controller (G0.26-T3 #1803).
//
// The operator console opens most modals by HTMX-swapping a native
// ``<dialog class="modal">`` fragment into a stable container. Closing
// any such modal -- a close/Cancel button, the Escape key, or the
// backdrop ``<form method="dialog">`` -- routes through the native
// ``HTMLDialogElement.close()``. ``.close()`` clears the ``[open]``
// attribute but does NOT touch CSS classes, so a dialog shown via
// DaisyUI's ``modal-open`` modifier ("Keeps the modal open") stayed
// visible after every close path and the operator had to reload the
// page (#1803). The fix is to align the injected dialogs with the
// native-dialog method DaisyUI 5 recommends: open them with
// ``showModal()`` (sets ``[open]``, enables native Escape) instead of a
// static ``modal-open`` class, so the existing ``.close()`` calls fully
// dismiss.
//
// This module wires that behaviour once for the whole shell, so no modal
// template carries per-instance open/close glue:
//
//   * OPEN -- on ``htmx:afterSwap`` (delegated on <body>; htmx swap
//     events bubble to the body, the same pattern kb/index.html and
//     topology-graph.js already rely on), any freshly swapped-in
//     ``<dialog class="modal">`` that is not already open is opened via
//     ``showModal()``. The ``!dialog.open`` guard is load-bearing:
//     ``showModal()`` on an already-open dialog throws
//     ``InvalidStateError``, and a re-render (e.g. the approvals detail
//     swapping over the panel, or a 422 validation re-render) swaps a
//     dialog that may already be open.
//
//   * CLOSE -- a delegated ``close`` listener removes any lingering
//     ``modal-open`` class when a dialog closes. The native ``close``
//     event fires on every close path (button ``.close()``, Escape, and
//     ``form[method="dialog"]`` submit) but does NOT bubble, so the
//     listener is registered in the CAPTURE phase at the document level
//     -- capture-phase listeners observe non-bubbling events as they
//     travel down to the target. This is a safety net: once the static
//     ``modal-open`` is gone from the injected fragments, ``.close()``
//     already fully dismisses; the class strip keeps any dialog that
//     toggles ``modal-open`` dynamically dismissable too.
//
// Loaded via ``<script src=... defer>`` from base.html's HEAD, OUTSIDE
// the overridable ``component_scripts`` block, so it ships on every
// console page regardless of what a surface template overrides. Plain
// first-party JS (no Alpine, no inline script) to preserve the chassis
// "zero inline script" CSP posture, mirroring theme.js.

(function () {
  "use strict";

  // Strip a stranded ``modal-open`` from a dialog the moment it closes.
  // Registered in the capture phase because the native ``close`` event
  // does not bubble (MDN: "does not bubble") -- a body/document listener
  // in the default bubbling phase would never observe it.
  document.addEventListener(
    "close",
    function (event) {
      var node = event.target;
      if (
        node &&
        node.tagName === "DIALOG" &&
        node.classList.contains("modal")
      ) {
        node.classList.remove("modal-open");
      }
    },
    true,
  );

  // Open any HTMX-injected modal dialog after its swap. The console's
  // injected modals are swapped (``innerHTML``) into a dedicated
  // container, so the swapped-in ``<dialog>`` is a DESCENDANT of the
  // swap target -- scanning ``detail.target``'s subtree finds it. A
  // dialog that replaces ITSELF via an ``outerHTML`` swap (the KB editor)
  // is deliberately not covered here: it owns its own open mechanism
  // (a button ``showModal()`` + its own re-bind), so it never lands as a
  // descendant of a container target and we must not double-drive it.
  document.body.addEventListener("htmx:afterSwap", function (event) {
    var root = (event.detail && event.detail.target) || event.target;
    // htmx 2.0.9 dispatches ``htmx:afterSwap`` with ``detail.target`` still
    // referencing the PRE-swap element. An ``outerHTML`` swap (e.g. the
    // runbook run driver's abort / reassign / advance forms, which target
    // ``#runbook-run-step`` with ``hx-swap="outerHTML"``) has already replaced
    // and DETACHED that element, so ``detail.target`` is now disconnected from
    // the document. Scanning its stale subtree would find the old, closed
    // descendant dialogs and call ``showModal()`` on a dialog that is no longer
    // connected -- which throws ``InvalidStateError``. Bail on a detached root:
    // the freshly swapped-in replacement ships its own dialogs closed and
    // button-driven, so there is nothing here to auto-open. This handler's
    // auto-open pattern always swaps modal fragments via ``innerHTML`` into a
    // STABLE container, whose ``detail.target`` stays connected, so the
    // ``isConnected`` check is a no-op for it.
    if (
      !root ||
      typeof root.querySelectorAll !== "function" ||
      !root.isConnected
    ) {
      return;
    }
    var dialogs = root.querySelectorAll("dialog.modal");
    for (var i = 0; i < dialogs.length; i++) {
      var dialog = dialogs[i];
      // ``showModal()`` throws InvalidStateError on an already-open
      // dialog, so only open the ones a swap just inserted closed.
      if (dialog.open || typeof dialog.showModal !== "function") {
        continue;
      }
      // Respect an explicit opt-out. A button-driven inline dialog (e.g.
      // the agent run console's Stop-confirm) ships INSIDE a swapped-in
      // fragment -- the run transcript, swapped over ``#agent-run-transcript``
      // on Run submit -- but must open only on its own trigger, never on the
      // swap. Without this guard the auto-open sweep pops the Stop dialog the
      // instant a run starts, blocking the live transcript the operator
      // wanted to watch (#2347). The dialog carries ``data-auto-open="false"``.
      if (dialog.dataset.autoOpen === "false") {
        continue;
      }
      // Never open a dialog nested inside another already-open dialog --
      // the operator-facing modal is the top-level one; a stray nested
      // duplicate must not steal the top layer.
      if (dialog.parentElement && dialog.parentElement.closest("dialog[open]")) {
        continue;
      }
      dialog.showModal();
    }
  });
})();
