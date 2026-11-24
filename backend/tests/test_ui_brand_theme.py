# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The evoila themes come from the shared package, and only from there.

The console used to ship two DaisyUI themes of its own and a switch
between them. The evoila brand replaced both. Since meho-ui 0.9.0 the
brand has a light and a dark theme, and every page follows the
computer's setting with ``data-theme="evoila-system"`` -- CSS only, no
script, no switch yet.

These are guards, not examples. The removal touched a stylesheet, six
templates, a service worker, an offline page and a syntax highlighter;
the way it regresses is one of those growing a second theme back, which
reads as working until somebody opens the page it broke.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from meho_backplane.ui.paths import brand_css_dir, brand_fonts_dir, static_src_dir, templates_dir

#: The theme names the brand package defines (meho-ui 0.9.0).
_BRAND_THEMES = frozenset({"evoila", "evoila-dark", "evoila-system"})

#: The machinery the brand replaced. None of it may come back.
_RETIRED = ("meho-dark", "meho-light", "mehoToggleTheme", "Toggle color theme")

#: Built output is regenerated from source on every image build, so a
#: stale copy in a working tree is not a finding.
_GENERATED = ("dist",)


def _source_files() -> list[Path]:
    roots = [templates_dir(), static_src_dir()]
    files: list[Path] = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".html", ".css", ".js"}:
                continue
            if any(part in _GENERATED for part in path.parts):
                continue
            files.append(path)
    return files


@pytest.mark.parametrize("retired", _RETIRED)
def test_the_retired_theme_machinery_is_gone(retired: str) -> None:
    """A second theme, or a switch, must not reappear in the UI sources."""
    offenders = [
        str(p.relative_to(p.parents[len(p.parts) - 1]))
        for p in _source_files()
        if retired in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"{retired!r} is back in: {offenders}"


def test_theme_js_is_deleted_and_unreferenced() -> None:
    """The bootstrap script read a stored preference. There is none now."""
    assert not (static_src_dir() / "app" / "theme.js").exists()
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        assert "app/theme.js" not in text, f"{path.name} still loads the deleted script"


def test_every_hardcoded_theme_is_the_brand_theme() -> None:
    """Every document follows the computer: ``data-theme="evoila-system"``.

    ``evoila`` would keep a page light on a dark computer, and any other
    value is not a brand theme at all.

    Five documents still set ``data-theme`` themselves: the pages that do
    not inherit ``base.html`` -- the session-ended page, both tenant
    pages, the broadcast wall and the offline page. They are the ones a
    sweep over ``templates/`` alone misses; the offline page does not
    even live there.

    ``base.html`` used to be the sixth. It now inherits the attribute
    from the shared page frame, which is asserted separately below --
    the count alone would have quietly dropped to five either way, so
    the two halves are checked apart.
    """
    found = 0
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".css":
            # A stylesheet may give its own colours a value per brand theme
            # (the topology graph does), but only for the brand's names.
            for value in re.findall(r'data-theme="([^"]+)"', text):
                assert value in _BRAND_THEMES, f"{path.name} styles data-theme={value!r}"
            continue
        for value in re.findall(r'<html[^>]*\sdata-theme="([^"]+)"', text):
            found += 1
            assert value == "evoila-system", f"{path.name} renders with data-theme={value!r}"
    assert found >= 5, (
        f"expected at least five standalone documents to pin the theme, found {found}"
    )


def test_base_html_gets_the_theme_from_the_shared_frame() -> None:
    """The other half of the guard above.

    ``base.html`` stopped declaring ``data-theme`` when it moved onto the
    shared page frame. That is correct -- the frame sets it -- but it
    means the sweep above can no longer see the attribute for the 67
    pages that inherit it. Without this test, a frame that stopped
    setting the theme, or a ``base.html`` that stopped extending it,
    would leave every console page unthemed and every assertion green.
    """
    from jinja2 import Environment

    from meho_backplane.ui.templating import get_jinja_env

    env: Environment = get_jinja_env()
    source = (templates_dir() / "base.html").read_text(encoding="utf-8")
    assert '{% extends "_page_frame.html" %}' in source, (
        "base.html no longer extends the shared frame, so nothing sets its theme"
    )

    # The frame is resolved through the app's own loader, so this also
    # proves the shared templates are reachable at render time.
    frame = env.get_template("_page_frame.html").render()
    assert 'data-theme="evoila"' in frame, "the shared frame stopped setting the brand theme"

    # base.html overrides the frame's default (always light) with the
    # theme that follows the computer.
    assert "{% block theme %}evoila-system{% endblock %}" in source


def test_the_head_links_the_packaged_brand_stylesheets() -> None:
    """The theme is linked, not compiled in -- it is variables, not utilities."""
    head = (templates_dir() / "_head_assets.html").read_text(encoding="utf-8")
    for sheet in ("evoila-daisyui.css", "evoila-tokens.css", "evoila-fonts.css"):
        assert f"/ui/static/brand/{sheet}" in head
    # After the bundle: unlayered author CSS has to beat DaisyUI's @layer base.
    assert head.index("/ui/static/dist/tailwind.css") < head.index("/ui/static/brand/")


def test_the_preloaded_fonts_exist_in_the_package() -> None:
    """A preload for a file the package does not ship is a wasted request.

    It also fails silently: the browser fetches, 404s, and falls back to a
    system font that looks close enough to miss in a screenshot.
    """
    head = (templates_dir() / "_head_assets.html").read_text(encoding="utf-8")
    preloaded = re.findall(r'href="/ui/static/fonts/([^"]+)"', head)
    assert preloaded, "no brand fonts are preloaded"
    for name in preloaded:
        assert (brand_fonts_dir() / name).is_file(), f"{name} is preloaded but not shipped"


def test_the_service_worker_precaches_only_files_that_exist() -> None:
    """A 404 in the pre-cache list fails the whole install, not just one entry."""
    worker = (static_src_dir() / "app" / "service-worker.js").read_text(encoding="utf-8")
    for name in re.findall(r'"/ui/static/fonts/([^"]+)"', worker):
        assert (brand_fonts_dir() / name).is_file(), f"{name} is pre-cached but not shipped"
    for name in re.findall(r'"/ui/static/brand/([^"]+)"', worker):
        assert (brand_css_dir() / name).is_file(), f"{name} is pre-cached but not shipped"


def test_the_brand_package_is_the_only_source_of_the_theme() -> None:
    """The console must not *define* a DaisyUI slot the package owns.

    Two definitions of ``--color-primary`` is how a shared look drifts
    back into a per-app one. Reading them with ``var(--color-primary)`` is
    the point of the package and stays -- only a declaration is a finding,
    so this matches an assignment at the start of a declaration rather
    than the token name anywhere.
    """
    styles = (static_src_dir() / "styles.css").read_text(encoding="utf-8")
    declared = re.findall(r"(?m)^\s*(--color-[a-z0-9-]+)\s*:", styles)
    assert not declared, (
        f"styles.css redefines slots the brand package owns: {sorted(set(declared))}"
    )


def test_every_document_tells_the_browser_both_themes_exist() -> None:
    """``<meta name="color-scheme" content="light dark">`` on every document.

    The browser reads it before any stylesheet arrives, so a dark computer
    does not get a white flash while the page loads.
    """
    meta = '<meta name="color-scheme" content="light dark" />'
    documents = [
        p
        for p in _source_files()
        if p.suffix == ".html" and re.search(r"<html[\s>]", p.read_text(encoding="utf-8"))
    ]
    assert len(documents) >= 5
    for path in documents:
        assert meta in path.read_text(encoding="utf-8"), path.name
    assert meta in (templates_dir() / "base.html").read_text(encoding="utf-8")


def test_the_brand_package_has_the_system_theme() -> None:
    """``evoila-system`` is meho-ui's, from 0.9.0 on.

    With an older meho-ui the attribute matches nothing, and every page
    renders with DaisyUI's fallback instead of the brand.
    """
    css = (brand_css_dir() / "evoila-daisyui.css").read_text(encoding="utf-8")
    assert '[data-theme="evoila-system"]' in css
    assert "@media (prefers-color-scheme: dark)" in css


def _graph_colour_names() -> set[str]:
    script = (static_src_dir() / "app" / "topology-graph.js").read_text(encoding="utf-8")
    return set(re.findall(r'graphColour\("([a-z-]+)"', script))


def test_the_topology_graph_takes_every_colour_from_the_theme() -> None:
    """The graph used to hard-code its colours; dark grey names on a dark
    page were close to unreadable. Every colour now goes through
    ``graphColour``, whose second argument is only the fallback."""
    script = (static_src_dir() / "app" / "topology-graph.js").read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*", "", script)
    bare = re.findall(r'"(#[0-9a-fA-F]{3,8})"', code)
    in_calls = re.findall(r'graphColour\("[a-z-]+", "(#[0-9a-fA-F]{3,8})"\)', code)
    assert sorted(bare) == sorted(in_calls), "a colour bypasses graphColour"
    assert len(_graph_colour_names()) >= 13


@pytest.mark.parametrize(
    "selector",
    [":root", '[data-theme="evoila-dark"]', '  [data-theme="evoila-system"]'],
)
def test_every_graph_colour_has_a_value_in_each_theme(selector: str) -> None:
    """A colour missing from the dark blocks would fall back to its light
    value -- the unreadable case this whole change is about."""
    styles = (static_src_dir() / "styles.css").read_text(encoding="utf-8")
    block = styles.split(selector + " {", 1)[1].split("}", 1)[0]
    declared = set(re.findall(r"--meho-graph-([a-z-]+):", block))
    missing = _graph_colour_names() - declared
    assert not missing, f"{selector} has no value for {sorted(missing)}"
