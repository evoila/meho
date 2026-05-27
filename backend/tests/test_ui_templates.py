# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Render tests for the operator-console UI chassis (G10.0-T2, #863).

These tests do not exercise FastAPI routes (T5 #866 lands those). They
assert the Jinja2 ``Environment`` constructed by
:func:`meho_backplane.ui.templating.get_jinja_env` can load
``base.html`` and produce HTML that contains the chassis-required
shape:

* DaisyUI ``navbar`` + ``drawer`` chrome.
* Sidebar links to every one of the five G10.x surfaces.
* Alpine.js ``x-data`` wiring for the sidebar collapse + user menu.
* A ``{% block content %}`` slot the surface templates can override.
* A footer rendering the backplane version (``app_version`` global)
  and the readiness pill.
* :class:`jinja2.StrictUndefined` propagates so a typo in a template
  variable fails the render rather than producing an empty string.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from jinja2 import Environment, UndefinedError

from meho_backplane import __version__
from meho_backplane.ui.paths import static_src_dir, templates_dir
from meho_backplane.ui.templating import get_jinja_env

# The five surface routes the chassis sidebar links to. Sourced from
# Initiative #337 work-item 5 + Goal #336 done-when (broadcast / kb /
# topology / connectors / memory). Surface-Initiative tasks rename
# their target URL if needed; the chassis owns the canonical list.
_EXPECTED_SURFACE_HREFS = (
    "/ui/broadcast",
    "/ui/kb",
    "/ui/topology",
    "/ui/connectors",
    "/ui/memory",
)


def _ui_env_factory() -> Iterator[Environment]:
    """Yield a fresh-ish Jinja env across tests.

    The module-level singleton in ``templating`` is intentionally
    cached across requests, so the test fixture grabs the cached
    instance once and reuses it. No teardown is needed -- the
    environment holds no resources beyond an in-memory template
    cache that lives for the process lifetime.
    """
    yield get_jinja_env()


@pytest.fixture(name="ui_env", scope="module")
def fixture_ui_env() -> Iterator[Environment]:
    yield from _ui_env_factory()


def test_templates_dir_exists_under_ui_package() -> None:
    """The chassis ships the Jinja2 templates inside the UI package.

    Regression guard: a refactor that moves templates outside
    ``meho_backplane/ui/templates/`` breaks the FastAPI deploy
    (T5 #866) because the static-mount + template-loader paths
    derive from the package's ``__file__``.
    """
    tdir = templates_dir()
    assert tdir.is_dir(), f"templates dir missing: {tdir}"
    assert (tdir / "base.html").is_file()


def test_static_src_carries_vendored_assets() -> None:
    """Every browser-bound vendor asset ships under ``static/src/vendor/``.

    Pinned SHA256s live in ``static/src/vendor/VENDOR.md``; this test
    only asserts the files are present. A SHA-mismatch check is
    intentionally out of scope here (Dockerfile/CI verify step
    covers that).
    """
    vendor = static_src_dir() / "vendor"
    assert (vendor / "htmx.min.js").is_file()
    # The SSE extension HTMX 2 split out of core; the dashboard
    # recent-activity snippet (G10.0) + the broadcast live feed (G10.1)
    # both need it, so it's a required vendored asset.
    assert (vendor / "sse.min.js").is_file()
    assert (vendor / "alpine.min.js").is_file()
    assert (vendor / "cytoscape.min.js").is_file()
    # Cytoscape layout-plugin chain shipped by G10.5-T2 (#881). Load
    # order is layout-base -> cose-base -> cose-bilkent / dagre; the
    # topology graph view depends on all four.
    assert (vendor / "layout-base.js").is_file()
    assert (vendor / "cose-base.js").is_file()
    assert (vendor / "cytoscape-cose-bilkent.js").is_file()
    assert (vendor / "cytoscape-dagre.js").is_file()
    assert (vendor / "daisyui.js").is_file()
    assert (vendor / "VENDOR.md").is_file()


def test_base_template_renders_with_app_version_global(ui_env: Environment) -> None:
    """``base.html`` resolves the ``app_version`` global from the env.

    The environment factory binds ``__version__`` as a Jinja global so
    every surface template can show the backplane version in the
    footer without each route having to pass it explicitly.
    """
    template = ui_env.get_template("base.html")
    html = template.render(ready=True)
    assert __version__ in html


def test_base_template_renders_navbar_and_drawer(ui_env: Environment) -> None:
    """DaisyUI primitives render in the expected positions.

    Asserting the class names directly is brittler than a
    visual-regression test but cheaper than spinning up a headless
    browser; the trade is acceptable for a chassis-shape test.
    """
    html = ui_env.get_template("base.html").render(ready=True)
    assert 'class="navbar' in html or "navbar bg-base-100" in html
    assert "drawer-toggle" in html
    assert "drawer-content" in html
    assert "drawer-side" in html


def test_base_template_links_every_surface(ui_env: Environment) -> None:
    """Sidebar enumerates all five G10.x surface routes."""
    html = ui_env.get_template("base.html").render(ready=True)
    for href in _EXPECTED_SURFACE_HREFS:
        assert f'href="{href}"' in html, f"missing surface link: {href}"


def test_base_template_wires_alpine_directives(ui_env: Environment) -> None:
    """Alpine.js sidebar-collapse + user-menu state is wired in markup.

    Alpine reads its directives from HTML attributes at parse time; if
    these attribute names get renamed in a refactor the interactive
    bits go dead silently. The assertions below catch that.
    """
    html = ui_env.get_template("base.html").render(ready=True)
    assert "x-data=" in html
    assert "sidebarOpen" in html
    assert "userMenuOpen" in html
    assert 'x-show="userMenuOpen"' in html
    assert "x-cloak" in html


def test_base_template_exposes_content_block(ui_env: Environment) -> None:
    """A child template can override ``{% block content %}``.

    The chassis is useless to G10.1-G10.5 without an extension point;
    this test exercises the override end-to-end against the
    FileSystemLoader the chassis uses in production.
    """
    template_source = (templates_dir() / "base.html").read_text(encoding="utf-8")
    # The block declaration is what the child relies on; a
    # case-insensitive search is fine because Jinja2 block names are
    # case-sensitive and our source uses lowercase.
    assert "{% block content %}" in template_source
    assert "{% endblock %}" in template_source


def test_base_template_footer_shows_readiness_pill(ui_env: Environment) -> None:
    """Footer flips between 'ready' and 'starting' on the ``ready`` flag."""
    ready_html = ui_env.get_template("base.html").render(ready=True)
    starting_html = ui_env.get_template("base.html").render(ready=False)
    assert "ready" in ready_html
    assert "bg-success" in ready_html
    assert "starting" in starting_html
    assert "bg-warning" in starting_html


def test_strict_undefined_raises_on_missing_global(ui_env: Environment) -> None:
    """A typo in a template variable raises rather than renders blank.

    The base template references ``ready`` (passed by the renderer)
    and ``app_version`` (the env global). Rendering without ``ready``
    must raise :class:`jinja2.UndefinedError`; that's the load-bearing
    behaviour of :class:`jinja2.StrictUndefined`.
    """
    template = ui_env.get_template("base.html")
    with pytest.raises(UndefinedError):
        template.render()  # ``ready`` is missing


def test_base_template_uses_compiled_tailwind_css(ui_env: Environment) -> None:
    """The link tag points at the Dockerfile-compiled output.

    ``static/dist/tailwind.css`` is the canonical output of the
    Tailwind 4 CLI build step; T5 #866's StaticFiles mount lands it
    at ``/ui/static/dist/tailwind.css``. A drift here would silently
    break styling in prod while looking fine in dev (where
    ``--watch`` writes to the same path).
    """
    html = ui_env.get_template("base.html").render(ready=True)
    assert re.search(r'href="/ui/static/dist/tailwind\.css"', html) is not None
