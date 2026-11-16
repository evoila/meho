# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The service worker shows a push message and opens only MEHO's own pages.

Runs ``static/src/app/service-worker.js`` in ``node`` against fake browser
objects, like ``test_ui_pwa.py``. Skipped when ``node`` is not on the PATH
(CI runners have it; ``MEHO_REQUIRE_NODE=1`` forces the run).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

import pytest

from meho_backplane.ui.paths import static_src_dir

_WORKER = static_src_dir() / "app" / "service-worker.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None and os.environ.get("MEHO_REQUIRE_NODE") != "1",
    reason="node not on PATH (set MEHO_REQUIRE_NODE=1 to force)",
)

_DRIVER = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");

const listeners = {};
const shown = [];
const opened = [];
const self = {
  location: { origin: "https://meho.example" },
  addEventListener: (name, fn) => { listeners[name] = fn; },
  registration: { showNotification: async (title, options) => { shown.push({ title, options }); } },
  clients: { claim: async () => {}, openWindow: async (url) => { opened.push(url); } },
};
const context = { self, caches: {}, fetch: async () => ({}), URL, Promise, Response: {} };
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);

async function push(data) {
  let done;
  listeners.push({ data, waitUntil: (p) => { done = p; } });
  await done;
}

async function click(url) {
  let done;
  let closed = false;
  listeners.notificationclick({
    notification: { data: { url }, close: () => { closed = true; } },
    waitUntil: (p) => { done = p; },
  });
  await done;
  return closed;
}

(async () => {
  const message = { title: "An approval waits", body: "Tenant A. Request r-1.",
                    tag: "approval-r-1", url: "/ui/approvals" };
  await push({ json: () => message });
  await push({ json: () => { throw new SyntaxError("not json"); } });
  await push(null);
  await push({ json: () => ({ title: 7, url: "https://elsewhere.example/" }) });
  const closed = await click("/ui/approvals");
  await click("https://elsewhere.example/phish");
  await click("//elsewhere.example/phish");
  await click("/\\elsewhere.example/phish");
  await click("/\\\\elsewhere.example/phish");
  await click(undefined);
  await click("/ui/approvals?id=r-1#top");
  process.stdout.write(JSON.stringify({ shown, opened, closed }));
})();
"""


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    driver = tmp_path_factory.mktemp("sw-push") / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(driver), str(_WORKER)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert isinstance(result, dict)
    return result


def test_a_push_message_shows_its_title_body_and_tag(run: dict[str, Any]) -> None:
    first = run["shown"][0]
    assert first["title"] == "An approval waits"
    assert first["options"]["body"] == "Tenant A. Request r-1."
    # The same tag makes the decision message replace this one.
    assert first["options"]["tag"] == "approval-r-1"
    assert first["options"]["data"] == {"url": "/ui/approvals"}


def test_a_message_that_cannot_be_read_still_shows_a_plain_notification(
    run: dict[str, Any],
) -> None:
    plain = run["shown"][1:3]
    assert [n["title"] for n in plain] == ["MEHO", "MEHO"]
    assert all(n["options"]["data"] == {"url": "/ui/approvals"} for n in plain)


def test_a_foreign_address_in_a_message_is_replaced(run: dict[str, Any]) -> None:
    odd = run["shown"][3]
    assert odd["title"] == "MEHO"
    assert odd["options"]["data"] == {"url": "/ui/approvals"}


def test_a_click_opens_only_a_page_of_this_meho(run: dict[str, Any]) -> None:
    assert run["closed"] is True
    assert run["opened"] == ["/ui/approvals"] * 6 + ["/ui/approvals?id=r-1#top"]
