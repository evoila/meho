// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// App-shell CSRF double-submit header hook (Task #2345).
//
// The ``/ui/*`` CSRF layer is a double-submit pair: the server sets a
// JS-readable ``meho_csrf`` cookie and every state-changing request must
// echo the SAME value back in the ``X-CSRF-Token`` header (``ui/csrf.py``).
// Templates bake that echo in at render time via
// ``hx-headers='{"X-CSRF-Token": "{{ csrf_token }}"}'`` on the fragment they
// were rendered with. That baked-in snapshot is the drift hazard: if the
// ``meho_csrf`` cookie ever changes after a modal was rendered (a sibling
// fragment re-render, a poll), the modal's stale echoed token no longer
// matches the live cookie and the write 403s ``csrf_token_invalid`` -- the
// "works twice, 403s the 3rd write" symptom (#2345).
//
// This module closes that gap on the client: a single global
// ``htmx:configRequest`` listener re-reads the ``meho_csrf`` cookie at
// REQUEST time and overrides ``X-CSRF-Token`` on the outgoing request with
// the live value, so the header echo is always in lockstep with the cookie
// regardless of which (possibly older) fragment the triggering element was
// rendered into. Paired with the server-side session-stable token (the
// cookie value no longer rotates mid-session), the double-submit pair can
// never drift.
//
// htmx 2.0.9 fires ``htmx:configRequest`` before every request and reads
// ``event.detail.headers`` back after the event (vendored bundle: the
// dispatch sets ``x=C.headers`` immediately after the event helper returns),
// so mutating ``event.detail.headers`` here changes the request that goes on
// the wire. See https://htmx.org/events/#htmx:configRequest.
//
// Registered as a delegated ``htmx:beforeRequest``-class listener on
// ``document.body`` (htmx events bubble to the body -- the same pattern
// ``session-expiry.js`` and ``modal-dialogs.js`` use), so every ``hx-*``
// element on every console page inherits it with no per-page wiring. Loaded
// once from ``_head_assets.html`` after ``htmx.min.js`` so the listener is
// registered before the first htmx request can resolve. Plain first-party JS
// (no Alpine, no inline script) to preserve the chassis "zero inline script"
// CSP posture, mirroring ``theme.js`` / ``session-expiry.js``.

(function () {
  "use strict";

  // The JS-readable double-submit cookie name (``ui/csrf.py``
  // ``CSRF_COOKIE_NAME``) and the custom echo header
  // (``CSRF_HEADER_NAME``). Kept in sync with the server constants.
  var CSRF_COOKIE_NAME = "meho_csrf";
  var CSRF_HEADER_NAME = "X-CSRF-Token";

  // Read a single cookie value out of ``document.cookie``. Returns the
  // decoded value, or ``null`` when the cookie is absent (e.g. a page that
  // never established a session). ``document.cookie`` is a ``; ``-joined
  // list of ``name=value`` pairs; the CSRF token is hex + ``.`` so it needs
  // no decoding, but ``decodeURIComponent`` is applied defensively for
  // parity with how the browser stored it.
  function readCookie(name) {
    var cookies = (document.cookie || "").split(";");
    for (var i = 0; i < cookies.length; i++) {
      var pair = cookies[i].trim();
      if (!pair) {
        continue;
      }
      var eq = pair.indexOf("=");
      if (eq === -1) {
        continue;
      }
      if (pair.slice(0, eq) === name) {
        try {
          return decodeURIComponent(pair.slice(eq + 1));
        } catch (_) {
          return pair.slice(eq + 1);
        }
      }
    }
    return null;
  }

  // The single global handler. On every htmx request, override the
  // ``X-CSRF-Token`` header with the live ``meho_csrf`` cookie value so the
  // double-submit echo is always current. When no cookie is present we leave
  // whatever the template's ``hx-headers`` set untouched (no regression on
  // the unauthenticated auth surfaces / a bare render). Safe methods carry
  // the header harmlessly -- the middleware bypasses CSRF on GET/HEAD/OPTIONS.
  function onConfigRequest(event) {
    var detail = event && event.detail;
    if (!detail || !detail.headers) {
      return;
    }
    var token = readCookie(CSRF_COOKIE_NAME);
    if (token) {
      detail.headers[CSRF_HEADER_NAME] = token;
    }
  }

  document.body.addEventListener("htmx:configRequest", onConfigRequest);

  // Exposed for the Python-served content/contract test (no JS test runner
  // exists in this repo; the suite serves this file and a synthetic-event
  // test can drive these directly). Not used by the page wiring itself.
  window.mehoCsrfToken = {
    readCookie: readCookie,
    onConfigRequest: onConfigRequest,
    CSRF_COOKIE_NAME: CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME: CSRF_HEADER_NAME,
  };
})();
