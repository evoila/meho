// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// App-shell session-expiry safety net (#122).
//
// When a ``/ui/*`` htmx request (``hx-get`` / ``hx-post``) returns ``401``
// mid-session, htmx 2.0.9 silently swallows it: the default
// ``htmx.config.responseHandling`` classifies ``[45]..`` as
// ``{swap: false, error: true}``, so the target container stays empty and
// the only signal is a ``htmx:responseError`` event no global listener was
// catching. The operator is left with a dead control and no clue their
// session expired -- the headline session-expiry symptom #1694 fixed
// server-side for full-page navigations but which still slips through on
// background htmx XHRs (those send ``Accept: */*``, not ``text/html``, so
// the ``ui.auth.errors`` handler returns the JSON 401 body rather than the
// HTML login redirect -- the browser sees a swallowed error, not a page).
//
// This module is the client-side last-resort recovery path: any ``401`` on
// an htmx request surfaces a visible banner ("your session expired -- sign
// in again") with a button to ``/ui/auth/login?return_to=<current path>``.
// It is the safety net (Axis B); the server-side refresh seam (#1694, and
// the sibling #121) reduces how *often* a 401 occurs (Axis A). Both land.
//
// Seam: ``htmx:beforeOnLoad``. htmx fires this for EVERY response (including
// errors) before any swap, redirect-header handling, or ``responseError``,
// and ``preventDefault()`` on it aborts htmx's entire response processing
// for that request (vendored htmx 2.0.9: ``Vn()`` returns early when the
// event helper reports the default was prevented). We act ONLY on status
// ``401`` -- every other status (a ``2xx`` swap, a ``422`` form-validation
// re-render, a ``403`` CSRF error a form's ``hx-on::response-error`` handles)
// flows through untouched, so legitimate non-auth responses are never
// hijacked. Chosen over ``htmx.config.responseHandling`` (which would
// reclassify status codes app-wide and risk the finely-tuned 4xx swap
// behaviour the connectors/agents/kb form modals depend on -- #875, forms.py
// 422 re-render) precisely because it is surgical: one status, one action.
//
// Registered via a plain ``htmx:beforeOnLoad`` listener on ``document.body``
// (htmx events bubble to the body -- the same delegation pattern
// ``modal-dialogs.js`` and ``kb/index.html`` use for ``htmx:afterSwap``), so
// every ``hx-*`` element on every console page inherits it with no per-page
// wiring. Loaded once from ``_head_assets.html`` after ``htmx.min.js`` so the
// listener is registered before the first htmx request can resolve. Plain
// first-party JS (no Alpine, no inline script) to preserve the chassis
// "zero inline script" CSP posture, mirroring ``theme.js`` / ``modal-dialogs.js``.
//
// Idempotent banner: the recovery banner is created once and reused -- a
// burst of failing background polls (the approvals badge, an SSE reconnect)
// all hitting 401 must not stack N banners. ``preventDefault()`` also stops
// the dead swap, so the operator's current view stays intact behind the
// banner rather than being blanked.

(function () {
  "use strict";

  // Build ``/ui/auth/login?return_to=<encoded current path+query>``. The
  // login route's ``_safe_return_to`` accepts only same-origin paths under
  // ``/ui/`` and percent-decodes the value, so we send the path+search
  // (never a full URL -- an absolute URL is rejected as an open-redirect
  // guard) percent-encoded wholesale, matching the server handler's
  // ``quote(full_path, safe="")`` contract in ``ui.auth.errors``.
  function loginUrl() {
    var here = window.location.pathname + window.location.search;
    return "/ui/auth/login?return_to=" + encodeURIComponent(here);
  }

  var BANNER_ID = "meho-session-expired-banner";

  // Create (once) and reveal the recovery banner. Returns the existing node
  // on every call after the first so repeated 401s never stack banners.
  function showBanner() {
    var existing = document.getElementById(BANNER_ID);
    if (existing) {
      return existing;
    }
    var banner = document.createElement("div");
    banner.id = BANNER_ID;
    banner.setAttribute("role", "alert");
    banner.setAttribute("aria-live", "assertive");
    // Fixed, top-centered, above every surface (the approvals modal sits at
    // ``z-50``; the drawer sidebar at ``z-40``) so the recovery path is never
    // occluded by whatever was on screen when the session died.
    banner.className =
      "alert alert-error fixed top-3 left-1/2 z-[60] w-[min(36rem,calc(100vw-1.5rem))] " +
      "-translate-x-1/2 shadow-xl";
    var message = document.createElement("span");
    message.className = "flex-1";
    message.textContent = "Your session expired — please sign in again.";
    var action = document.createElement("a");
    action.className = "btn btn-sm";
    action.href = loginUrl();
    action.textContent = "Sign in";
    banner.appendChild(message);
    banner.appendChild(action);
    document.body.appendChild(banner);
    return banner;
  }

  // The single global handler. Acts ONLY on a 401; returns immediately for
  // every other status so normal swaps and non-auth 4xx/5xx handling are
  // byte-for-byte unchanged.
  function onBeforeOnLoad(event) {
    var xhr = event && event.detail && event.detail.xhr;
    if (!xhr || xhr.status !== 401) {
      return;
    }
    // Abort htmx's response processing for this 401: no dead swap, no
    // ``responseError`` confusion -- the operator's current view survives
    // behind the banner.
    event.preventDefault();
    showBanner();
  }

  document.body.addEventListener("htmx:beforeOnLoad", onBeforeOnLoad);

  // Exposed for the unit-style contract test (no JS test runner exists in
  // this repo; the Python suite serves this file and asserts on its content,
  // and a synthetic-event test can drive these directly). Not used by the
  // page wiring itself.
  window.mehoSessionExpiry = {
    loginUrl: loginUrl,
    showBanner: showBanner,
    onBeforeOnLoad: onBeforeOnLoad,
    BANNER_ID: BANNER_ID,
  };
})();
