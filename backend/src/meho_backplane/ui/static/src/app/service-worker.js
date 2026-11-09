// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Service worker of the installable MEHO app. One plain file, no build.
//
// Rules:
// * It keeps ONLY the app shell in its cache: the files in SHELL_URLS
//   (icons, CSS, fonts, the theme script, the manifest and the offline
//   page). That list is the whole allow-list. Nothing else is ever
//   written to the cache: no console page (approvals, broadcast, any
//   other), no /api answer, no request with a query string.
// * A page is always loaded from MEHO. When MEHO cannot be reached (for
//   example the VPN is off), the worker shows the offline page instead.
//   So the app never shows an old copy of an approval list.
// * Shell files are loaded from MEHO first, so a new release shows at
//   once; the cached copy is used only when MEHO cannot be reached.
// * Every other request is left to the browser, untouched.
(function () {
  "use strict";

  var CACHE_PREFIX = "meho-shell-";
  var CACHE_NAME = CACHE_PREFIX + "v1";
  var OFFLINE_URL = "/ui/static/src/pwa/offline.html";
  var SHELL_URLS = [
    OFFLINE_URL,
    "/ui/static/src/pwa/manifest.webmanifest",
    "/ui/static/dist/tailwind.css",
    "/ui/static/src/app/theme.js",
    "/ui/static/src/brand/favicon.svg",
    "/ui/static/src/brand/icon-192.png",
    "/ui/static/src/brand/icon-512.png",
    "/ui/static/src/brand/icon-maskable-512.png",
    "/ui/static/src/brand/apple-touch-icon.png",
    "/ui/static/src/fonts/schibsted-grotesk-latin-var.woff2",
    "/ui/static/src/fonts/bricolage-grotesque-latin-var.woff2",
    "/ui/static/src/fonts/ibm-plex-mono-latin-400.woff2",
  ];

  // True only for a file of the app shell. The one check that decides
  // whether anything may be written to the cache.
  function isShellUrl(url) {
    return (
      url.origin === self.location.origin &&
      url.search === "" &&
      SHELL_URLS.indexOf(url.pathname) !== -1
    );
  }

  // How a request is handled: "page" (load from MEHO, offline page when
  // MEHO cannot be reached), "shell" (load from MEHO, cached copy when it
  // cannot be reached), or "pass" (the worker does nothing).
  function routeFor(request) {
    if (request.method !== "GET") return "pass";
    var url = new URL(request.url);
    if (url.origin !== self.location.origin) return "pass";
    if (request.mode === "navigate") return "page";
    if (isShellUrl(url)) return "shell";
    return "pass";
  }

  function loadPage(request) {
    return fetch(request).catch(function () {
      return caches.match(OFFLINE_URL);
    });
  }

  function loadShell(request) {
    var url = new URL(request.url);
    return fetch(request)
      .then(function (response) {
        if (response.ok && response.type === "basic" && isShellUrl(url)) {
          var copy = response.clone();
          caches.open(CACHE_NAME).then(function (cache) {
            return cache.put(url.pathname, copy);
          });
        }
        return response;
      })
      .catch(function () {
        return caches.match(url.pathname).then(function (cached) {
          return cached || Response.error();
        });
      });
  }

  self.addEventListener("install", function (event) {
    event.waitUntil(
      caches.open(CACHE_NAME).then(function (cache) {
        // The offline page must be there; a missing optional file (for
        // example the CSS bundle in a dev checkout) must not stop the
        // install.
        var optional = SHELL_URLS.filter(function (u) {
          return u !== OFFLINE_URL;
        }).map(function (u) {
          return cache.add(u).catch(function () {});
        });
        return Promise.all([cache.add(OFFLINE_URL)].concat(optional));
      }).then(function () {
        return self.skipWaiting();
      })
    );
  });

  self.addEventListener("activate", function (event) {
    event.waitUntil(
      caches.keys().then(function (names) {
        return Promise.all(
          names
            .filter(function (n) {
              return n.indexOf(CACHE_PREFIX) === 0 && n !== CACHE_NAME;
            })
            .map(function (n) {
              return caches.delete(n);
            })
        );
      }).then(function () {
        return self.clients.claim();
      })
    );
  });

  self.addEventListener("fetch", function (event) {
    var route = routeFor(event.request);
    if (route === "page") event.respondWith(loadPage(event.request));
    else if (route === "shell") event.respondWith(loadShell(event.request));
  });

  // Read by the unit test (backend/tests/test_ui_pwa.py).
  self.mehoServiceWorker = {
    CACHE_NAME: CACHE_NAME,
    OFFLINE_URL: OFFLINE_URL,
    SHELL_URLS: SHELL_URLS,
    isShellUrl: isShellUrl,
    routeFor: routeFor,
  };
})();
