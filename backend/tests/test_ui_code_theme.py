# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Highlighted code reads in both themes.

Knowledge entries and runbook previews used pygments' light ``default``
token colours on a block whose background follows the theme, so in dark
mode function and class names were deep blue on navy (1.15:1 at worst).
Memory entries and conventions had the reverse: pygments' fixed
``#f8f8f8`` block background, with plain code inheriting the dark theme's
light text. Both now take their colours from
:mod:`meho_backplane.ui.code_theme`. These tests read the CSS the
renderers really emit, work out each token's colour in each theme, and
check it against the block background.
"""

from __future__ import annotations

import re

import pytest

from meho_backplane.ui.paths import brand_css_dir
from meho_backplane.ui.routes.kb import render as kb_render
from meho_backplane.ui.routes.memory import render as memory_render

_SCOPES = {".kb-code": kb_render, ".memory-code": memory_render}
_DARK = '[data-theme="evoila-dark"]'
_SYSTEM = '[data-theme="evoila-system"]'
_RULE = re.compile(r"^\s*(?P<sel>[^{]+?)\s*\{(?P<decls>[^}]*)\}", re.M)


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    channels = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _theme(theme: str) -> dict[str, str]:
    css = (brand_css_dir() / "evoila-daisyui.css").read_text(encoding="utf-8")
    selector = _SYSTEM if theme == "light" else _DARK
    body = css.split(selector + " {", 1)[1].split("}", 1)[0]
    return {k: v.strip() for k, v in re.findall(r"--([a-z0-9-]+):\s*([^;]+);", body)}


def _token_colours(css: str, scope: str, theme: str) -> dict[str, tuple[str, str | None]]:
    """``{class: (colour, own background or None)}`` as the theme shows it."""
    light: dict[str, tuple[str, str | None]] = {}
    dark: dict[str, tuple[str, str | None]] = {}
    for m in _RULE.finditer(css):
        sel, decls = m.group("sel").strip(), m.group("decls")
        colour = re.search(r"(?<![-\w])color:\s*(#[0-9a-fA-F]{3,6})", decls)
        if not colour:
            continue
        bg = re.search(r"background-color:\s*(#[0-9a-fA-F]{3,6})", decls)
        value = (colour.group(1), bg.group(1) if bg else None)
        if sel.startswith(f"{scope} ."):
            light[sel.split(" .", 1)[1]] = value
        elif sel.startswith(f"{_DARK} {scope} ."):
            dark[sel.rsplit(" .", 1)[1]] = value
    return light | dark if theme == "dark" else light


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("scope", list(_SCOPES))
def test_every_token_colour_reads_on_the_block(scope: str, theme: str) -> None:
    css = _SCOPES[scope].pygments_css()
    block = _theme(theme)["color-base-200"]
    colours = _token_colours(css, scope, theme)
    assert len(colours) > 40, "the token rules did not parse"
    failures = [
        f".{cls} {fg} on {bg or block}: {_ratio(fg, bg or block):.2f}"
        for cls, (fg, bg) in colours.items()
        if cls != "w"  # whitespace tokens draw no text
        and _ratio(fg, bg or block) < 4.5
    ]
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("scope", list(_SCOPES))
def test_the_block_and_plain_code_follow_the_theme(scope: str) -> None:
    """No fixed pygments background: the block is base-200 and plain code
    is base-content, which reach 4.5:1 together in both themes."""
    css = _SCOPES[scope].pygments_css()
    assert (
        f"{scope} {{ background: var(--color-base-200); color: var(--color-base-content); }}" in css
    )
    assert not re.search(rf"^{re.escape(scope)} \{{ background: #", css, re.M)
    assert "pre { line-height" not in css  # no rules for every <pre> on the page
    for theme in ("light", "dark"):
        t = _theme(theme)
        assert _ratio(t["color-base-content"], t["color-base-200"]) >= 4.5, theme


@pytest.mark.parametrize("scope", list(_SCOPES))
def test_the_dark_set_is_scoped_like_the_brand(scope: str) -> None:
    css = _SCOPES[scope].pygments_css()
    dark = {
        m.group("sel").strip()
        for m in _RULE.finditer(css)
        if m.group("sel").strip().startswith(_DARK)
    }
    media = css.split("@media (prefers-color-scheme: dark) {", 1)
    assert len(media) == 2, "the system theme's dark rules are missing"
    system = {
        m.group("sel").strip().replace(_SYSTEM, _DARK)
        for m in _RULE.finditer(media[1])
        if m.group("sel").strip().startswith(_SYSTEM)
    }
    assert dark and dark == system


@pytest.mark.parametrize("scope", list(_SCOPES))
def test_a_function_name_has_a_colour_in_each_theme(scope: str) -> None:
    """The reviewer's case: a highlighted function name, then a plain block."""
    module = _SCOPES[scope]
    html = str(module.render_markdown("```python\ndef reconcile():\n    pass\n```"))
    assert f'<pre class="{scope[1:]}">' in html
    assert '<span class="nf">reconcile</span>' in html
    css = module.pygments_css()
    for theme in ("light", "dark"):
        fg, _ = _token_colours(css, scope, theme)["nf"]
        assert _ratio(fg, _theme(theme)["color-base-200"]) >= 4.5, theme
    plain = str(module.render_markdown("```\nkubectl get pods\n```"))
    assert f'<pre class="{scope[1:]}">' in plain
    assert "<span" not in plain.split("<code", 1)[1].split("</code>", 1)[0]
