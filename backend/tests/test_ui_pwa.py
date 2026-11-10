# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the installable MEHO app: manifest, icons, service worker, offline page.

What these pin:

* The manifest and the service worker are served by the real static
  mount with the right content types, and the worker file carries
  ``Service-Worker-Allowed: /ui/`` (no other file does).
* The manifest has what browsers need to offer "Install": a name, a
  192 px and a 512 px PNG icon, the start page (approvals), the ``/ui/``
  scope and ``display: standalone``.
* The offline page says to connect to the VPN and uses only files the
  worker keeps in its cache.
* The service worker's cache rules, run in ``node`` against fake browser
  objects: approvals, broadcast and ``/api`` requests are never written
  to the cache, a page that cannot be loaded shows the offline page, and
  only the app shell files are cached.

No JS test runner ships in this repo. The worker tests run the real file
with ``node`` when it is on the PATH (CI runners have it), and skip
otherwise, like ``test_ui_corpus_collection_action_alerts.py``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.ui.paths import (
    static_asset_path,
    static_mounts,
    static_src_dir,
    templates_dir,
)
from meho_backplane.ui.static_files import RevalidateStaticFiles

_MANIFEST_URL = "/ui/static/src/pwa/manifest.webmanifest"
_WORKER_URL = "/ui/static/src/app/service-worker.js"
_OFFLINE_URL = "/ui/static/src/pwa/offline.html"
_WORKER = static_src_dir() / "app" / "service-worker.js"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None and os.environ.get("MEHO_REQUIRE_NODE") != "1",
    reason="node not on PATH (set MEHO_REQUIRE_NODE=1 to force)",
)


@pytest.fixture(scope="module")
def static_client() -> TestClient:
    app = FastAPI()
    for prefix, directory in static_mounts():
        app.mount(
            prefix,
            RevalidateStaticFiles(directory=str(directory), check_dir=False),
            name=prefix.strip("/").replace("/", "_"),
        )
    return TestClient(app)


def _disk_path(url: str) -> Path:
    """The file the app serves for *url*, across all three static mounts."""
    disk = static_asset_path(url)
    assert disk is not None, f"{url} is not a /ui/static/ URL"
    return disk


def _png_size(data: bytes) -> tuple[int, int]:
    """Width and height from a PNG's IHDR chunk."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG file"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


# ---------------------------------------------------------------------------
# Served with the right types
# ---------------------------------------------------------------------------


def test_manifest_is_served_as_a_web_app_manifest(static_client: TestClient) -> None:
    resp = static_client.get(_MANIFEST_URL)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")
    assert "no-cache" in resp.headers["cache-control"]
    assert "service-worker-allowed" not in resp.headers


def test_service_worker_is_served_as_javascript_with_the_ui_scope(
    static_client: TestClient,
) -> None:
    resp = static_client.get(_WORKER_URL)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/javascript")
    assert resp.headers["service-worker-allowed"] == "/ui/"
    assert "no-cache" in resp.headers["cache-control"]


def test_other_scripts_do_not_get_the_service_worker_header(
    static_client: TestClient,
) -> None:
    resp = static_client.get("/ui/static/src/app/pwa-register.js")
    assert resp.status_code == 200
    assert "service-worker-allowed" not in resp.headers


def test_service_worker_304_keeps_the_scope_header(static_client: TestClient) -> None:
    etag = static_client.get(_WORKER_URL).headers["etag"]
    resp = static_client.get(_WORKER_URL, headers={"If-None-Match": etag})
    assert resp.status_code == 304
    assert resp.headers["service-worker-allowed"] == "/ui/"


def test_missing_service_worker_path_is_a_404(static_client: TestClient) -> None:
    resp = static_client.get("/ui/static/src/app/no-such-worker.js")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Manifest content
# ---------------------------------------------------------------------------


def _manifest() -> dict[str, Any]:
    data = json.loads(_disk_path(_MANIFEST_URL).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_manifest_has_what_browsers_need_to_install() -> None:
    manifest = _manifest()
    assert manifest["name"] == "MEHO"
    assert manifest["short_name"] == "MEHO"
    assert manifest["start_url"] == "/ui/approvals"
    assert manifest["scope"] == "/ui/"
    assert manifest["display"] == "standalone"
    assert "prefer_related_applications" not in manifest
    for key in ("theme_color", "background_color"):
        assert re.fullmatch(r"#[0-9a-f]{6}", manifest[key]), key


def test_manifest_icons_exist_and_match_their_sizes(static_client: TestClient) -> None:
    sizes: dict[str, set[str]] = {}
    for entry in _manifest()["icons"]:
        resp = static_client.get(entry["src"])
        assert resp.status_code == 200, entry["src"]
        assert resp.headers["content-type"].startswith(entry["type"]), entry["src"]
        if entry["type"] == "image/png":
            width, height = _png_size(resp.content)
            assert entry["sizes"] == f"{width}x{height}", entry["src"]
        sizes.setdefault(entry["purpose"], set()).add(entry["sizes"])
    # Chrome needs a 192 px and a 512 px icon; Android uses the maskable one.
    assert {"192x192", "512x512"} <= sizes["any"]
    assert "512x512" in sizes["maskable"]


def test_apple_touch_icon_is_a_180px_png() -> None:
    data = (static_src_dir() / "brand" / "apple-touch-icon.png").read_bytes()
    assert _png_size(data) == (180, 180)


def test_head_links_the_manifest_and_the_worker_registration() -> None:
    head = (templates_dir() / "_head_assets.html").read_text(encoding="utf-8")
    assert f'<link rel="manifest" href="{_MANIFEST_URL}" />' in head
    assert "/ui/static/src/brand/apple-touch-icon.png" in head
    assert '<script src="/ui/static/src/app/pwa-register.js" defer></script>' in head


def test_registration_uses_the_served_worker_and_ui_scope() -> None:
    source = (static_src_dir() / "app" / "pwa-register.js").read_text(encoding="utf-8")
    assert f'register("{_WORKER_URL}", {{ scope: "/ui/" }})' in source
    assert '"serviceWorker" in navigator' in source


# ---------------------------------------------------------------------------
# Offline page
# ---------------------------------------------------------------------------


def _shell_urls() -> list[str]:
    source = _WORKER.read_text(encoding="utf-8")
    block = source.split("var SHELL_URLS = [", 1)[1].split("];", 1)[0]
    return ["/ui/static/src/pwa/offline.html", *re.findall(r'"(/ui/static/[^"]+)"', block)]


def test_offline_page_says_to_connect_to_the_vpn(static_client: TestClient) -> None:
    resp = static_client.get(_OFFLINE_URL)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "Connect to the VPN and try again." in resp.text


def test_offline_page_uses_only_cached_files() -> None:
    html = _disk_path(_OFFLINE_URL).read_text(encoding="utf-8")
    used = set(re.findall(r'(?:src|href)="(/ui/static/[^"]+)"', html))
    assert used, "offline page names no static file"
    assert used <= set(_shell_urls())


def test_tailwind_scans_the_offline_page() -> None:
    """The offline page's classes get into the CSS bundle."""
    styles = (static_src_dir() / "styles.css").read_text(encoding="utf-8")
    assert '@source "./pwa"' in styles


def test_every_shell_file_is_shipped() -> None:
    for url in _shell_urls():
        if "/dist/" in url:
            continue  # the CSS bundle is built in the image, not committed
        assert _disk_path(url).is_file(), url


def test_service_worker_has_no_eval() -> None:
    assert "eval(" not in _WORKER.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Service worker cache rules (run in node)
# ---------------------------------------------------------------------------

# Fake browser objects: ``self`` records the event listeners, ``caches``
# records every write, ``fetch`` answers from a table (or fails, like a
# network that cannot reach MEHO). Each case dispatches a real fetch
# event to the worker and reports what it did.
_DRIVER = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");

const ORIGIN = "https://meho.example";
const listeners = {};
const writes = [];
const store = {};
const cacheObj = (name) => ({
  put: async (key, resp) => { writes.push({ cache: name, key }); store[key] = resp; },
  add: async (url) => {
    writes.push({ cache: name, key: url, add: true });
    store[url] = { body: url };
  },
});
const caches = {
  open: async (name) => cacheObj(name),
  match: async (key) => store[key] || undefined,
  keys: async () => ["meho-shell-old", "meho-shell-v1", "other-app"],
  deleted: [],
  delete: async (name) => { caches.deleted.push(name); return true; },
};
let online = true;
const fetchFn = async (request) => {
  if (!online) throw new TypeError("Failed to fetch");
  return { ok: true, type: "basic", body: request.url, clone() { return { body: request.url }; } };
};
const self = {
  location: { origin: ORIGIN },
  addEventListener: (name, fn) => { listeners[name] = fn; },
  skipWaiting: async () => {},
  clients: { claim: async () => {} },
};
const Response = { error: () => ({ error: true }) };
const context = { self, caches, fetch: fetchFn, URL, Promise, Response };
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);
const api = self.mehoServiceWorker;

async function dispatch(path, opts) {
  opts = opts || {};
  const request = { url: (opts.origin || ORIGIN) + path, method: opts.method || "GET",
                    mode: opts.mode || "cors" };
  let answer = null;
  let responded = false;
  listeners.fetch({ request, respondWith: (p) => { responded = true; answer = p; } });
  const resp = responded ? await answer : null;
  await new Promise((r) => setTimeout(r, 0));
  return { responded, body: resp ? (resp.body || (resp.error ? "error" : null)) : null };
}

(async () => {
  const out = { routes: {}, cases: {} };
  const paths = ["/ui/approvals", "/ui/approvals/badge", "/ui/approvals/abc?tab=pending",
                 "/ui/broadcast", "/ui/broadcast/stream", "/ui/broadcast/history",
                 "/api/v1/approvals", "/api/v1/feed", "/ui/static/src/pwa/offline.html",
                 "/ui/static/dist/tailwind.css", "/ui/static/dist/tailwind.css?v=2",
                 "/ui/static/src/app/broadcast-feed.js"];
  for (const p of paths) {
    out.routes[p] = {
      cors: api.routeFor({ url: ORIGIN + p, method: "GET", mode: "cors" }),
      navigate: api.routeFor({ url: ORIGIN + p, method: "GET", mode: "navigate" }),
      shell: api.isShellUrl(new URL(ORIGIN + p)),
    };
  }
  const css = "/ui/static/dist/tailwind.css";
  out.postRoute = api.routeFor({ url: ORIGIN + css, method: "POST", mode: "cors" });
  const foreign = "https://push.example" + css;
  out.otherOrigin = api.routeFor({ url: foreign, method: "GET", mode: "navigate" });

  // Install caches the shell; activate drops only our old caches.
  let installDone;
  listeners.install({ waitUntil: (p) => { installDone = p; } });
  await installDone;
  out.installed = writes.filter((w) => w.add).map((w) => w.key);
  let activateDone;
  listeners.activate({ waitUntil: (p) => { activateDone = p; } });
  await activateDone;
  out.deleted = caches.deleted;
  writes.length = 0;

  online = true;
  out.cases.approvalsPageOnline = await dispatch("/ui/approvals", { mode: "navigate" });
  out.cases.broadcastPageOnline = await dispatch("/ui/broadcast", { mode: "navigate" });
  out.cases.approvalsFragment = await dispatch("/ui/approvals/badge");
  out.cases.apiCall = await dispatch("/api/v1/approvals");
  out.cases.cssOnline = await dispatch("/ui/static/dist/tailwind.css");
  out.writesOnline = writes.map((w) => w.key);
  writes.length = 0;

  online = false;
  out.cases.approvalsPageOffline = await dispatch("/ui/approvals", { mode: "navigate" });
  out.cases.broadcastPageOffline = await dispatch("/ui/broadcast", { mode: "navigate" });
  out.cases.cssOffline = await dispatch("/ui/static/dist/tailwind.css");
  out.writesOffline = writes.map((w) => w.key);
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.fixture(scope="module")
def worker_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    driver = tmp_path_factory.mktemp("sw") / "driver.js"
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


@requires_node
@pytest.mark.parametrize(
    "path",
    [
        "/ui/approvals",
        "/ui/approvals/badge",
        "/ui/approvals/abc?tab=pending",
        "/ui/broadcast",
        "/ui/broadcast/stream",
        "/ui/broadcast/history",
        "/api/v1/approvals",
        "/api/v1/feed",
    ],
)
def test_approvals_broadcast_and_api_are_never_cacheable(
    worker_run: dict[str, Any], path: str
) -> None:
    route = worker_run["routes"][path]
    assert route["shell"] is False
    assert route["cors"] == "pass"
    # A page is loaded from MEHO (offline page when it cannot), never cached.
    assert route["navigate"] == "page"


@requires_node
def test_only_shell_files_without_a_query_are_cacheable(worker_run: dict[str, Any]) -> None:
    routes = worker_run["routes"]
    assert routes["/ui/static/dist/tailwind.css"]["shell"] is True
    assert routes["/ui/static/src/pwa/offline.html"]["shell"] is True
    assert routes["/ui/static/dist/tailwind.css?v=2"]["shell"] is False
    assert routes["/ui/static/src/app/broadcast-feed.js"]["shell"] is False
    assert worker_run["postRoute"] == "pass"
    assert worker_run["otherOrigin"] == "pass"


@requires_node
def test_install_caches_the_shell_and_activate_drops_old_caches(
    worker_run: dict[str, Any],
) -> None:
    assert sorted(worker_run["installed"]) == sorted(_shell_urls())
    assert worker_run["deleted"] == ["meho-shell-old"]


@requires_node
def test_online_pages_and_api_answers_are_not_written_to_the_cache(
    worker_run: dict[str, Any],
) -> None:
    cases = worker_run["cases"]
    assert cases["approvalsPageOnline"] == {
        "responded": True,
        "body": "https://meho.example/ui/approvals",
    }
    assert cases["broadcastPageOnline"]["body"] == "https://meho.example/ui/broadcast"
    # Fragments and API calls are left to the browser.
    assert cases["approvalsFragment"]["responded"] is False
    assert cases["apiCall"]["responded"] is False
    # The only write is the shell CSS, refreshed from MEHO.
    assert worker_run["writesOnline"] == ["/ui/static/dist/tailwind.css"]


@requires_node
def test_offline_pages_show_the_offline_page_never_an_old_copy(
    worker_run: dict[str, Any],
) -> None:
    cases = worker_run["cases"]
    assert cases["approvalsPageOffline"] == {"responded": True, "body": _OFFLINE_URL}
    assert cases["broadcastPageOffline"] == {"responded": True, "body": _OFFLINE_URL}
    # The shell CSS comes from the cache; nothing is written while offline.
    assert cases["cssOffline"]["responded"] is True
    assert worker_run["writesOffline"] == []
