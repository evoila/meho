// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// App-shell session-expiry + CSRF-rejection safety net (#122, #2112).
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
// The same swallow-on-4xx behaviour hides the CSRF middleware's ``403``
// (#2112). A state-changing ``/ui/*`` POST whose ``meho_csrf`` double-submit
// cookie was dropped or aged out (the browser rarely hits this because every
// fragment render re-mints the cookie, but a stale tab or a cookie-cleared
// session can) gets a bare ``{"detail":"csrf_token_invalid"}`` JSON body at
// ``403`` that htmx will not swap -- so an operator aborting their own
// runbook run saw a dead button with no message. Returning an HTML fragment
// from the pure-ASGI middleware would not help: htmx 2.0.9 does not swap 4xx
// bodies (``{code:"[45]..", swap:false, error:true}`` in the vendored
// bundle). The client is the only place that can surface it. The middleware
// stamps every CSRF rejection with an ``x-csrf-rejection-reason`` response
// header (``missing_token`` / ``value_mismatch`` / ``signature_invalid`` /
// ``no_session``); that header is the discriminator -- an RBAC ``403`` (e.g.
// ``require_ui_admin`` on reassign) carries no such header and flows through
// untouched.
//
// This module is the client-side last-resort recovery path:
//   * any ``401`` -> a "your session expired -- sign in again" banner with a
//     button to ``/ui/auth/login?return_to=<current path>``.
//   * a ``403`` carrying ``x-csrf-rejection-reason`` -> a "your session
//     token expired -- refresh the page and retry" banner with a reload
//     button (a fresh render re-mints the ``meho_csrf`` cookie).
// It is the safety net (Axis B); the server-side refresh seam (#1694, and
// the sibling #121) reduces how *often* a 401 occurs (Axis A). Both land.
//
// Seam: ``htmx:beforeOnLoad``. htmx fires this for EVERY response (including
// errors) before any swap, redirect-header handling, or ``responseError``,
// and ``preventDefault()`` on it aborts htmx's entire response processing
// for that request (vendored htmx 2.0.9: ``Vn()`` returns early when the
// event helper reports the default was prevented). We act ONLY on a ``401``
// or a CSRF-rejection ``403`` -- every other status (a ``2xx`` swap, a
// ``422`` form-validation re-render, a bare RBAC ``403`` a form's
// ``hx-on::response-error`` handles) flows through untouched, so legitimate
// non-auth responses are never hijacked. Chosen over
// ``htmx.config.responseHandling`` (which would reclassify status codes
// app-wide and risk the finely-tuned 4xx swap behaviour the
// connectors/agents/kb form modals depend on -- #875, forms.py 422 re-render)
// precisely because it is surgical: two narrow triggers, one action each.
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
// Idempotent banners: each recovery banner is created once and reused -- a
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
  var CSRF_BANNER_ID = "meho-csrf-rejected-banner";

  // The custom response header the CSRF middleware stamps on every rejection
  // (``ui/csrf.py`` ``_forbidden_response``). Its presence -- not the bare
  // ``403`` status -- is what distinguishes a CSRF rejection from an RBAC
  // ``403`` (``require_ui_admin``), so this net never hijacks a genuine
  // permission denial.
  var CSRF_REASON_HEADER = "x-csrf-rejection-reason";

  // Create (once) and reveal a fixed recovery banner. Returns the existing
  // node on every call after the first so a burst of failing requests never
  // stacks N banners.
  function showBanner(id, messageText, actionText, actionHref) {
    var existing = document.getElementById(id);
    if (existing) {
      return existing;
    }
    var banner = document.createElement("div");
    banner.id = id;
    banner.setAttribute("role", "alert");
    banner.setAttribute("aria-live", "assertive");
    // Fixed, top-centered, above every surface (the approvals modal sits at
    // ``z-50``; the drawer sidebar at ``z-40``) so the recovery path is never
    // occluded by whatever was on screen when the request failed.
    banner.className =
      "alert alert-error fixed top-3 left-1/2 z-[60] w-[min(36rem,calc(100vw-1.5rem))] " +
      "-translate-x-1/2 shadow-xl";
    var message = document.createElement("span");
    message.className = "flex-1";
    message.textContent = messageText;
    var action = document.createElement("a");
    action.className = "btn btn-sm";
    action.href = actionHref;
    action.textContent = actionText;
    banner.appendChild(message);
    banner.appendChild(action);
    document.body.appendChild(banner);
    return banner;
  }

  // Reveal the session-expiry recovery banner (a 401).
  function showSessionExpiredBanner() {
    return showBanner(
      BANNER_ID,
      "Your session expired — please sign in again.",
      "Sign in",
      loginUrl(),
    );
  }

  // Reveal the CSRF-rejection recovery banner (a 403 carrying the
  // rejection-reason header). The action reloads the current page: a fresh
  // authenticated render re-mints the ``meho_csrf`` cookie, so the retried
  // action then clears the double-submit check.
  function showCsrfRejectedBanner() {
    var here = window.location.pathname + window.location.search;
    return showBanner(
      CSRF_BANNER_ID,
      "Your session token expired — refresh the page and retry.",
      "Refresh",
      here,
    );
  }

  // Return the CSRF-rejection reason header if this response is one, else
  // ``null``. ``getResponseHeader`` reads same-origin custom headers without
  // a CORS allow-list (the console is same-origin), and returns ``null`` when
  // the header is absent.
  function csrfRejectionReason(xhr) {
    if (!xhr || typeof xhr.getResponseHeader !== "function") {
      return null;
    }
    return xhr.getResponseHeader(CSRF_REASON_HEADER);
  }

  // The single global handler. Acts ONLY on a 401 or a CSRF-rejection 403;
  // returns immediately for every other status (and for a bare RBAC 403 with
  // no rejection header) so normal swaps and non-auth 4xx/5xx handling are
  // byte-for-byte unchanged.
  function onBeforeOnLoad(event) {
    var xhr = event && event.detail && event.detail.xhr;
    if (!xhr) {
      return;
    }
    if (xhr.status === 401) {
      // Abort htmx's response processing for this 401: no dead swap, no
      // ``responseError`` confusion -- the operator's current view survives
      // behind the banner.
      event.preventDefault();
      showSessionExpiredBanner();
      return;
    }
    if (xhr.status === 403 && csrfRejectionReason(xhr)) {
      // A CSRF double-submit rejection (a dropped/stale ``meho_csrf`` cookie),
      // NOT an RBAC denial -- surface the refresh path instead of a dead
      // control. The bare RBAC 403 (no reason header) is left untouched.
      event.preventDefault();
      showCsrfRejectedBanner();
      return;
    }
  }

  document.body.addEventListener("htmx:beforeOnLoad", onBeforeOnLoad);

  // Exposed for the unit-style contract test (no JS test runner exists in
  // this repo; the Python suite serves this file and asserts on its content,
  // and a synthetic-event test can drive these directly). Not used by the
  // page wiring itself.
  window.mehoSessionExpiry = {
    loginUrl: loginUrl,
    showBanner: showBanner,
    showSessionExpiredBanner: showSessionExpiredBanner,
    showCsrfRejectedBanner: showCsrfRejectedBanner,
    csrfRejectionReason: csrfRejectionReason,
    onBeforeOnLoad: onBeforeOnLoad,
    BANNER_ID: BANNER_ID,
    CSRF_BANNER_ID: CSRF_BANNER_ID,
    CSRF_REASON_HEADER: CSRF_REASON_HEADER,
  };
})();
