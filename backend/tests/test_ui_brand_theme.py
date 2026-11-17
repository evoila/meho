# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One theme, from the shared package, and no way back to two.

The console used to ship two DaisyUI themes and a switch between them.
The evoila brand replaced both with one light theme, which now arrives
from the ``meho-ui`` package rather than being defined here.

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
    """Six documents set ``data-theme`` themselves, three of them standalone.

    ``base.html`` plus the pages that do not inherit it: the session-ended
    page, both tenant pages, the broadcast wall and the offline page. They
    are the ones a sweep over ``templates/`` alone misses -- the offline
    page does not even live there.
    """
    found = 0
    for path in _source_files():
        for value in re.findall(r'data-theme="([^"]+)"', path.read_text(encoding="utf-8")):
            found += 1
            assert value == "evoila", f"{path.name} renders with data-theme={value!r}"
    assert found >= 6, f"expected at least six documents to pin the theme, found {found}"


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
