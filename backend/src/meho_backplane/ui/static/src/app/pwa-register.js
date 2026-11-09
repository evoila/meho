// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Registers the service worker of the installable MEHO app. The worker
// file lives under /ui/static/ (the path the session check lets through),
// and the static mount sends "Service-Worker-Allowed: /ui/" with it, so
// it may control every console page under /ui/. A browser without
// service workers simply skips this: the console works the same.
(function () {
  "use strict";
  if (!("serviceWorker" in navigator)) return;
  window.addEventListener("load", function () {
    navigator.serviceWorker
      .register("/ui/static/src/app/service-worker.js", { scope: "/ui/" })
      .catch(function () {
        // Not installable in this browser (for example plain http).
        // The console itself is not affected.
      });
  });
})();
