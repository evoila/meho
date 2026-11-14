# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Guard tests for the operator-console static-asset surface.

These lock down the two ways a release has shipped a console the browser
could not style:

1. **A missing / stale-cached asset.** The console references its CSS,
   vendored JS, fonts and controllers at STABLE, unversioned
   ``/ui/static/...`` URLs. A template that references an asset the
   package does not ship, or an asset served without a revalidation
   directive, produces the "console lost its CSS after a deploy" symptom
   (evoila/meho#3647 and its recurrence). This module walks *every*
   ``/ui/static/...`` reference authored across the Jinja2 templates and
   asserts each committed asset is (a) present in the package tree, and
   (b) served by the real :class:`RevalidateStaticFiles` mount with a
   non-HTML content-type and ``Cache-Control: no-cache`` -- the header
   that stops a browser reusing a stale copy after a roll.

2. **An empty (utilities-less) Tailwind bundle.** ``dist/tailwind.css``
   is a build artifact (gitignored; produced by the Dockerfile Tailwind
   step or the local ``--watch`` loop), so when it is absent -- a fresh
   checkout / the unit-CI lane -- the content assertions ``skip`` and the
   Dockerfile build-time gate is the enforcing guard. When it IS present
   (image build, local dev) this module additionally asserts it clears a
   size floor and carries the console's utility/component selectors, so a
   #3647-class regression fails a plain ``pytest`` run too.

The mount is exercised in isolation (a minimal app mounting the real
:class:`RevalidateStaticFiles`) rather than through the full
``meho_backplane.main.app``: the unit under test is the static mount and
its cache policy, not the surrounding auth middleware (which already
exempts ``/ui/static/`` and is covered by the UI surface tests).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meho_backplane.ui.paths import (
    static_asset_path,
    static_dist_dir,
    static_mounts,
    templates_dir,
)
from meho_backplane.ui.static_files import RevalidateStaticFiles

# Prefix every console asset URL carries. Which directory a URL
# resolves under is no longer one answer -- the brand stylesheets and
# webfonts come out of the ``meho-ui`` package via mounts of their own
# -- so the mapping lives in ``ui.paths`` beside the mount table rather
# than being spelled out again here.
_STATIC_URL_PREFIX = "/ui/static/"

# Selectors the console templates author that a healthy Tailwind scan
# must emit. The evoila/meho#3647 empty bundle carried zero of these.
_REQUIRED_CSS_SELECTORS = (".btn", ".card", ".navbar", "bg-base", "badge-error")

# Size floor for the compiled bundle, kept in lockstep with the
# Dockerfile build-time gate. The empty #3647 bundle was ~35 KB; a
# healthy one is ~150 KB. 60 KB cleanly separates them without
# false-failing a legitimately-trimmed future bundle.
_CSS_MIN_BYTES = 61440


def _referenced_static_urls() -> list[str]:
    """Every distinct ``/ui/static/...`` URL authored across the templates."""
    pattern = re.compile(r"/ui/static/[A-Za-z0-9_./-]+")
    urls: set[str] = set()
    for html in templates_dir().rglob("*.html"):
        urls.update(pattern.findall(html.read_text(encoding="utf-8")))
    return sorted(urls)


def _disk_path(url: str) -> Path:
    """Map a ``/ui/static/<rest>`` URL to the file the app serves for it."""
    disk = static_asset_path(url)
    assert disk is not None, f"{url} does not start with {_STATIC_URL_PREFIX}"
    return disk


_REFERENCED_URLS = _referenced_static_urls()


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


def test_templates_reference_at_least_the_core_assets() -> None:
    """Sanity check that the scan found the head-critical assets.

    Guards against the regex silently matching nothing (e.g. a future
    templates-dir move) and turning the parametrized guard into a
    vacuous pass.
    """
    assert "/ui/static/dist/tailwind.css" in _REFERENCED_URLS
    assert any(u.endswith("/vendor/htmx.min.js") for u in _REFERENCED_URLS)


@pytest.mark.parametrize("url", _REFERENCED_URLS)
def test_referenced_asset_is_present_and_served_revalidatable(
    url: str, static_client: TestClient
) -> None:
    """Every referenced committed asset is shipped and served no-cache.

    A missing committed asset (anything outside the gitignored ``dist/``)
    fails loudly -- a template referencing an asset the wheel does not
    ship is exactly the "console without its assets" packaging bug the
    guard exists to catch. The built ``dist/`` bundle is only asserted
    when present (see :func:`test_compiled_css_carries_console_classes`).
    """
    disk = _disk_path(url)
    if disk.is_dir():
        # A mount point, not an asset -- the head-assets comment names
        # ``/ui/static/brand`` in prose while explaining the mount, and
        # the URL scan cannot tell prose from a reference. A directory
        # is never servable, so there is nothing here to assert.
        pytest.skip(f"{url} names a mount point, not an asset")
    if not disk.exists():
        if "/dist/" in url:
            pytest.skip(f"{url} is a build artifact not present in this checkout")
        pytest.fail(f"template references {url} but {disk} is not shipped in the package tree")

    resp = static_client.get(url)
    assert resp.status_code == 200, f"{url} -> {resp.status_code}"
    assert resp.content, f"{url} served an empty body"
    content_type = resp.headers.get("content-type", "")
    assert not content_type.startswith("text/html"), (
        f"{url} served as {content_type!r} -- a SPA/HTML fallback for a "
        "static asset URL is the classic 'missing CSS' symptom"
    )
    cache_control = resp.headers.get("cache-control", "")
    assert "no-cache" in cache_control, (
        f"{url} served without a revalidation directive (cache-control="
        f"{cache_control!r}); a stable-URL asset must be no-cache so a "
        "deploy can never leave a browser on a stale copy"
    )


def test_revalidation_request_still_carries_cache_control(
    static_client: TestClient,
) -> None:
    """A ``304`` revalidation response also carries ``no-cache``.

    The whole point of the header is the revalidation round-trip; assert
    the ``If-None-Match`` fast-path both returns ``304`` and re-states the
    directive so the browser keeps revalidating on the next load.
    """
    first = static_client.get("/ui/static/src/vendor/htmx.min.js")
    etag = first.headers.get("etag")
    assert etag, "static asset served without an ETag; revalidation impossible"

    revalidated = static_client.get(
        "/ui/static/src/vendor/htmx.min.js",
        headers={"If-None-Match": etag},
    )
    assert revalidated.status_code == 304
    assert "no-cache" in revalidated.headers.get("cache-control", "")


def test_compiled_css_carries_console_classes(static_client: TestClient) -> None:
    """When built, ``tailwind.css`` clears the size floor and has utilities.

    Skips in a checkout where the Tailwind build has not run (``dist/`` is
    gitignored); the Dockerfile build-time gate is the enforcing guard
    there. When the bundle IS present this fails a #3647-class empty
    bundle in a plain ``pytest`` run.
    """
    css_path = static_dist_dir() / "tailwind.css"
    if not css_path.exists():
        pytest.skip(
            "compiled tailwind.css not built in this checkout; the "
            "Dockerfile build-time gate enforces the content floor in the image"
        )

    resp = static_client.get("/ui/static/dist/tailwind.css")
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("text/css")
    body = resp.text
    assert len(resp.content) >= _CSS_MIN_BYTES, (
        f"tailwind.css is {len(resp.content)} B (< {_CSS_MIN_BYTES} B floor) "
        "-- the @source scan reached (near) no templates"
    )
    missing = [sel for sel in _REQUIRED_CSS_SELECTORS if sel not in body]
    assert not missing, f"tailwind.css is missing console selectors: {missing}"


def test_app_mount_table_matches_the_shared_one() -> None:
    """The running app mounts exactly what :func:`static_mounts` says.

    The brand stylesheets and webfonts are served out of the ``meho-ui``
    package by prefixes of their own, and a Starlette mount miss does
    **not** fall through to a later, more specific mount -- so the two
    brand prefixes registered after the broad ``/ui/static`` one would
    404 every brand URL while every test that resolves paths through
    the shared table kept passing.

    Reading the order off the real app rather than off the table is the
    point: the table alone cannot catch a mount added straight to
    ``main.py``, which is how the guard tests and the app drifted apart
    the first time.
    """
    from starlette.routing import Mount

    from meho_backplane.main import app

    mounted = [r.path for r in app.router.routes if isinstance(r, Mount)]
    static_mounted = [p for p in mounted if p.startswith("/ui/static")]
    expected = [prefix for prefix, _ in static_mounts()]

    assert static_mounted == expected, (
        "the app's static mount table has drifted from ui.paths.static_mounts(): "
        f"app has {static_mounted}, table has {expected}"
    )


@pytest.mark.parametrize(
    "url", ["/ui/static/brand/evoila-daisyui.css", "/ui/static/fonts/rubik-latin-var.woff2"]
)
def test_brand_asset_is_served_from_the_shared_package(url: str, static_client: TestClient) -> None:
    """A brand asset serves over HTTP, not merely resolves on disk.

    The disk check above passes for a file the app cannot reach -- a
    mount registered in the wrong order resolves fine and 404s. This
    asserts the served path, the revalidation header the rest of the
    console's assets carry, and that the bytes come from the shared
    package rather than a copy in this tree.
    """
    resp = static_client.get(url)
    assert resp.status_code == 200, f"{url} -> {resp.status_code}"
    assert resp.content, f"{url} served an empty body"
    assert "no-cache" in resp.headers.get("cache-control", ""), (
        f"{url} served without a revalidation directive"
    )

    disk = _disk_path(url)
    assert "meho_ui" in disk.parts, (
        f"{url} resolved to {disk}, which is not inside the shared brand "
        "package -- the console is serving a copy that can drift"
    )
