// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group
//
// The "Notify me about approvals in this browser" switch on the account
// page (account/_push.html). Loaded only while the admin setting
// WEB_PUSH_ENABLED is on.
//
// On: the browser asks the person for permission, subscribes with MEHO's
// public push key, and sends the subscription (the endpoint and its two
// keys) to /ui/account/push/subscribe. Off: the browser drops the
// subscription and MEHO deletes it (/ui/account/push/unsubscribe).
// After each change the list below the switch reloads (event
// "meho-push-changed").
//
// The switch shows "on" only when MEHO has this browser saved for the
// signed-in person (/ui/account/push/status), not only when the browser
// holds a subscription: the row may have been removed, or another person
// may have switched it on. It checks again each time the list reloads, so
// a "Remove" in the list turns the switch off.
//
// The requests carry the CSRF token and the page tenant, the same as htmx
// requests (csrf-token.js, page-tenant.js). Plain first-party JS, no inline
// script.
(function () {
  "use strict";

  var SUBSCRIBE_PATH = "/ui/account/push/subscribe";
  var UNSUBSCRIBE_PATH = "/ui/account/push/unsubscribe";
  var STATUS_PATH = "/ui/account/push/status";
  var LIST_ID = "account-push-subscriptions";
  var CHECK_FAILED = "MEHO could not check this browser. Try again later.";

  // Decode a base64url text to the bytes the browser wants for
  // applicationServerKey.
  function keyBytes(text) {
    var base64 = (text + "===".slice((text.length + 3) % 4))
      .replace(/-/g, "+")
      .replace(/_/g, "/");
    var raw = window.atob(base64);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) {
      out[i] = raw.charCodeAt(i);
    }
    return out;
  }

  function headers() {
    var out = { "Content-Type": "application/x-www-form-urlencoded" };
    var csrf = window.mehoCsrfToken;
    var token = csrf && csrf.readCookie(csrf.CSRF_COOKIE_NAME);
    if (token) {
      out[csrf.CSRF_HEADER_NAME] = token;
    }
    var tenant = window.mehoPageTenant;
    var pageTenant = tenant && tenant.pageTenant();
    if (pageTenant) {
      out[tenant.TENANT_HEADER] = pageTenant;
    }
    return out;
  }

  function post(path, values) {
    return window
      .fetch(path, {
        method: "POST",
        credentials: "same-origin",
        headers: headers(),
        body: new URLSearchParams(values).toString(),
      })
      .then(function (response) {
        if (!response.ok) {
          return response.text().then(function (text) {
            throw new Error(response.status === 422 && text ? text : "MEHO did not accept it.");
          });
        }
        document.body.dispatchEvent(new CustomEvent("meho-push-changed"));
      });
  }

  // True when MEHO has this browser saved for the signed-in person.
  function savedForMe(subscription) {
    if (!subscription) {
      return Promise.resolve(false);
    }
    return window
      .fetch(STATUS_PATH, {
        method: "POST",
        credentials: "same-origin",
        headers: headers(),
        body: new URLSearchParams({ endpoint: subscription.endpoint }).toString(),
      })
      .then(function (response) {
        if (!response.ok) {
          throw new Error(CHECK_FAILED);
        }
        return response.json();
      })
      .then(function (body) {
        return !!(body && body.on);
      });
  }

  function switchOn(registration, publicKey) {
    return Notification.requestPermission().then(function (permission) {
      if (permission !== "granted") {
        throw new Error("Notifications are blocked for this site in the browser settings.");
      }
      return registration.pushManager
        .subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(publicKey) })
        .then(function (subscription) {
          var data = subscription.toJSON();
          var keys = data.keys || {};
          return post(SUBSCRIBE_PATH, {
            endpoint: data.endpoint || "",
            p256dh: keys.p256dh || "",
            auth: keys.auth || "",
          }).catch(function (error) {
            // MEHO did not store it: drop it in the browser too, so the
            // switch shows the truth.
            return subscription.unsubscribe().then(function () {
              throw error;
            });
          });
        });
    });
  }

  function switchOff(registration) {
    return registration.pushManager.getSubscription().then(function (subscription) {
      if (!subscription) {
        return null;
      }
      var endpoint = subscription.endpoint;
      return subscription.unsubscribe().then(function () {
        return post(UNSUBSCRIBE_PATH, { endpoint: endpoint });
      });
    });
  }

  function start() {
    var toggle = document.querySelector("[data-push-switch]");
    if (!toggle) {
      return;
    }
    var status = document.querySelector("[data-push-status]");
    function say(text) {
      if (status) {
        status.textContent = text;
      }
    }
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
      say("This browser cannot show notifications here. On an iPhone or iPad, install MEHO on the home screen first.");
      return;
    }
    var publicKey = toggle.getAttribute("data-vapid-public-key") || "";
    navigator.serviceWorker.ready
      .then(function (registration) {
        function showSaved() {
          return registration.pushManager
            .getSubscription()
            .then(savedForMe)
            .then(function (on) {
              toggle.checked = on;
            });
        }
        // The list reloads after every change, also after "Remove" in it.
        document.body.addEventListener("htmx:afterSettle", function (event) {
          if (event.target && event.target.id === LIST_ID && !toggle.disabled) {
            showSaved().catch(function () {
              say(CHECK_FAILED);
            });
          }
        });
        return showSaved()
          .catch(function () {
            toggle.checked = false;
            say(CHECK_FAILED);
          })
          .then(function () {
            toggle.disabled = false;
            toggle.addEventListener("change", function () {
              var wanted = toggle.checked;
              toggle.disabled = true;
              say("");
              var work = wanted ? switchOn(registration, publicKey) : switchOff(registration);
              work
                .then(function () {
                  say(wanted ? "Notifications are on in this browser." : "Notifications are off in this browser.");
                })
                .catch(function (error) {
                  say(error && error.message ? error.message : "That did not work. Try again.");
                  // Show what MEHO really has now.
                  return showSaved().catch(function () {
                    toggle.checked = false;
                  });
                })
                .finally(function () {
                  toggle.disabled = false;
                });
            });
          });
      })
      .catch(function () {
        say("This browser cannot show notifications here.");
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
