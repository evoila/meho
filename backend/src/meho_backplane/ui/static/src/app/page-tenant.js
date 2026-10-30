// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// Page tenant marker (#5240).
//
// A person with several tenants can switch the tenant in one browser tab
// while an older tab still shows a page of the old tenant. A form sent from
// that old tab must not run in the new tenant. So every page carries the
// tenant it was made for: ``base.html`` renders it as
// ``<body data-meho-tenant="<tenant-uuid>">``. This module sends it back:
//
// * in the ``Meho-Tenant`` header of every htmx request (a global
//   ``htmx:configRequest`` listener, the same hook ``csrf-token.js`` uses);
// * as a hidden ``meho_tenant`` field in every plain ``<form method="post">``
//   (a global ``submit`` listener), for the few forms outside htmx.
//
// The server (``ui/csrf.py``) compares the value with the session's current
// tenant. On a mismatch it answers ``409`` with the ``x-meho-tenant-changed``
// header and runs nothing. For an htmx request this module then shows a
// banner "The tenant changed. Reload the page." with a reload button, using
// the banner helper of ``session-expiry.js``.
//
// The value is read once from the page and never from a cookie: the point is
// to send the tenant the page was made for, not the session's current one.
// Plain first-party JS (no inline script), like the other app-shell modules.

(function () {
  "use strict";

  var TENANT_HEADER = "Meho-Tenant";
  var FORM_FIELD = "meho_tenant";
  var CHANGED_HEADER = "x-meho-tenant-changed";
  var BANNER_ID = "meho-tenant-changed-banner";

  function pageTenant() {
    var body = document.body;
    return (body && body.dataset && body.dataset.mehoTenant) || null;
  }

  function onConfigRequest(event) {
    var detail = event && event.detail;
    var tenant = pageTenant();
    if (!detail || !detail.headers || !tenant) {
      return;
    }
    detail.headers[TENANT_HEADER] = tenant;
  }

  function onSubmit(event) {
    var form = event && event.target;
    var tenant = pageTenant();
    if (!form || !tenant || typeof form.getAttribute !== "function") {
      return;
    }
    if ((form.getAttribute("method") || "get").toLowerCase() !== "post") {
      return;
    }
    var field = form.querySelector('input[name="' + FORM_FIELD + '"]');
    if (!field) {
      field = document.createElement("input");
      field.type = "hidden";
      field.name = FORM_FIELD;
      form.appendChild(field);
    }
    field.value = tenant;
  }

  function showTenantChangedBanner() {
    var here = window.location.pathname + window.location.search;
    var helper = window.mehoSessionExpiry;
    if (helper && typeof helper.showBanner === "function") {
      return helper.showBanner(
        BANNER_ID,
        "The tenant changed. Reload the page.",
        "Reload",
        here,
      );
    }
    return null;
  }

  function onBeforeOnLoad(event) {
    var xhr = event && event.detail && event.detail.xhr;
    if (!xhr || xhr.status !== 409 || typeof xhr.getResponseHeader !== "function") {
      return;
    }
    if (xhr.getResponseHeader(CHANGED_HEADER)) {
      event.preventDefault();
      showTenantChangedBanner();
    }
  }

  document.body.addEventListener("htmx:configRequest", onConfigRequest);
  document.body.addEventListener("htmx:beforeOnLoad", onBeforeOnLoad);
  document.addEventListener("submit", onSubmit, true);

  // Exposed for the served-content contract test, like csrf-token.js.
  window.mehoPageTenant = {
    pageTenant: pageTenant,
    onConfigRequest: onConfigRequest,
    onSubmit: onSubmit,
    onBeforeOnLoad: onBeforeOnLoad,
    TENANT_HEADER: TENANT_HEADER,
    FORM_FIELD: FORM_FIELD,
    CHANGED_HEADER: CHANGED_HEADER,
  };
})();
