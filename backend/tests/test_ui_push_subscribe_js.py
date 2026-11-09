# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The account page's push switch script, run in ``node`` against fake browser objects.

* On: asks for permission, subscribes with the public key from the page,
  posts the endpoint and both keys with the CSRF token and the page tenant.
* Permission denied: nothing is subscribed and nothing is posted.
* MEHO refuses the subscription: the browser drops it again and the switch
  shows off, with MEHO's sentence.
* Off: the browser drops the subscription and MEHO is told the endpoint.

Skipped where ``node`` is missing; CI runners have it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from meho_backplane.ui.paths import static_root_dir

_SCRIPT = static_root_dir() / "src" / "app" / "push-subscribe.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None and os.environ.get("MEHO_REQUIRE_NODE") != "1",
    reason="node not on PATH (set MEHO_REQUIRE_NODE=1 to force)",
)

_DRIVER = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const scenario = process.argv[3];

const out = { posts: [], events: [], subscribedWith: null, unsubscribed: false };
let subscription = scenario === "off" ? makeSub() : null;
function makeSub() {
  return {
    endpoint: "https://fcm.googleapis.com/fcm/send/made-up",
    toJSON() {
      return { endpoint: this.endpoint, keys: { p256dh: "made-up-p256dh", auth: "made-up-auth" } };
    },
    unsubscribe: async () => { out.unsubscribed = true; subscription = null; return true; },
  };
}
const registration = {
  pushManager: {
    getSubscription: async () => subscription,
    subscribe: async (opts) => {
      out.subscribedWith = {
        userVisibleOnly: opts.userVisibleOnly,
        keyLength: opts.applicationServerKey.length,
      };
      subscription = makeSub();
      return subscription;
    },
  },
};
let changeHandler = null;
const toggle = {
  checked: false, disabled: true,
  getAttribute: (name) =>
    (name === "data-vapid-public-key" ? Buffer.alloc(65, 4).toString("base64url") : null),
  addEventListener: (name, fn) => { if (name === "change") changeHandler = fn; },
};
const status = { textContent: "" };
const document = {
  readyState: "complete",
  cookie: "meho_csrf=tok123",
  querySelector: (sel) =>
    (sel === "[data-push-switch]" ? toggle : sel === "[data-push-status]" ? status : null),
  body: { dispatchEvent: (e) => { out.events.push(e.type); } },
};
const window = {
  atob: (s) => Buffer.from(s, "base64").toString("binary"),
  mehoCsrfToken: {
    readCookie: () => "tok123", CSRF_COOKIE_NAME: "meho_csrf", CSRF_HEADER_NAME: "X-CSRF-Token",
  },
  mehoPageTenant: { pageTenant: () => "tenant-1", TENANT_HEADER: "Meho-Tenant" },
  PushManager: function () {},
  Notification: {},
  fetch: async (path, init) => {
    out.posts.push({ path, headers: init.headers, body: init.body });
    if (scenario === "on-refused") {
      const text = async () => "The push endpoint is not a push service.";
      return { ok: false, status: 422, text };
    }
    return { ok: true, status: 200, text: async () => "" };
  },
};
const Notification = {
  requestPermission: async () => (scenario === "on-denied" ? "denied" : "granted"),
};
const navigator = { serviceWorker: { ready: Promise.resolve(registration) } };
class CustomEvent { constructor(type) { this.type = type; } }
const context = {
  window, document, navigator, Notification, URLSearchParams, Uint8Array, CustomEvent, Promise,
};
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);

const settle = () => new Promise((r) => setTimeout(r, 20));
(async () => {
  await settle();
  out.startChecked = toggle.checked;
  toggle.checked = scenario !== "off";
  changeHandler();
  await settle();
  out.checked = toggle.checked;
  out.disabled = toggle.disabled;
  out.status = status.textContent;
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run(tmp_path: Path, scenario: str) -> dict[str, Any]:
    driver = tmp_path / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(driver), str(_SCRIPT), scenario],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result: dict[str, Any] = json.loads(proc.stdout)
    return result


def test_switch_on_posts_the_subscription_with_csrf_and_tenant(tmp_path: Path) -> None:
    out = _run(tmp_path, "on-ok")
    assert out["startChecked"] is False
    assert out["subscribedWith"] == {"userVisibleOnly": True, "keyLength": 65}
    assert len(out["posts"]) == 1
    post = out["posts"][0]
    assert post["path"] == "/ui/account/push/subscribe"
    assert post["headers"]["X-CSRF-Token"] == "tok123"
    assert post["headers"]["Meho-Tenant"] == "tenant-1"
    assert "endpoint=https%3A%2F%2Ffcm.googleapis.com" in post["body"]
    assert "p256dh=made-up-p256dh" in post["body"]
    assert "auth=made-up-auth" in post["body"]
    assert out["events"] == ["meho-push-changed"]
    assert out["checked"] is True
    assert out["disabled"] is False


def test_permission_denied_subscribes_nothing(tmp_path: Path) -> None:
    out = _run(tmp_path, "on-denied")
    assert out["subscribedWith"] is None
    assert out["posts"] == []
    assert out["checked"] is False
    assert "blocked" in out["status"]


def test_a_refused_subscription_is_dropped_in_the_browser_too(tmp_path: Path) -> None:
    out = _run(tmp_path, "on-refused")
    assert out["unsubscribed"] is True
    assert out["checked"] is False
    assert out["status"] == "The push endpoint is not a push service."
    assert out["events"] == []


def test_switch_off_drops_the_subscription_and_tells_meho(tmp_path: Path) -> None:
    out = _run(tmp_path, "off")
    assert out["startChecked"] is True
    assert out["unsubscribed"] is True
    assert [p["path"] for p in out["posts"]] == ["/ui/account/push/unsubscribe"]
    assert "endpoint=https%3A%2F%2Ffcm.googleapis.com" in out["posts"][0]["body"]
    assert out["checked"] is False
