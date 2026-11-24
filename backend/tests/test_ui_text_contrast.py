# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secondary text is a solid colour that passes WCAG AA, never faded text.

The console used to mute text with ``opacity-60`` / ``opacity-70`` and
``text-base-content/55``-``/65``: about 860 uses. On the light theme that
put most labels, descriptions and metadata at 2.5-3.5:1, below the 4.5:1
AA asks of normal text, and the screens read washed out. No alpha both
looks muted and passes (base-content needs 86% on the page), so muted
text is now one of two solid tokens, ``text-muted`` and ``text-subtle``,
defined in ``styles.css`` with a light and a dark value.

Two guards: no template fades text again, and every text token reaches
4.5:1 on each surface it sits on, computed from the brand package's
own colours.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from meho_backplane.ui.paths import brand_css_dir, static_src_dir, templates_dir

_ROUTES_DIR = templates_dir().parent / "routes"

#: ``opacity-<n>`` or ``text-base-content/<n>`` as a class token.
_FADED = re.compile(r"(?<![\w:/-])(?:opacity-\d+|text-base-content/\d+)(?![\w/-])")
_CLASS_ATTR = re.compile(r"""class=(\\?["'])(.*?)\1""", re.S)

#: Faded on purpose: icons are not text, and the dashboard tile arrow is
#: decoration (the whole tile is the link and its title names it).
_ALLOWED_CONTEXT = re.compile(r"icon\([^()]*$")
_ALLOWED_TOKENS = frozenset({"text-base-content/25"})


def _markup_files() -> list[Path]:
    files = list(templates_dir().rglob("*.html"))
    files += list((static_src_dir() / "pwa").rglob("*.html"))
    files += list(_ROUTES_DIR.rglob("*.py"))
    return files


def _faded_text(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    found = []
    for m in _CLASS_ATTR.finditer(text):
        if _ALLOWED_CONTEXT.search(text[max(0, m.start() - 80) : m.start()]):
            continue
        for token in _FADED.findall(m.group(2)):
            if token not in _ALLOWED_TOKENS:
                line = text.count("\n", 0, m.start()) + 1
                found.append(f"{path.name}:{line}: {token}")
    return found


def test_no_template_fades_text() -> None:
    """Use ``text-muted`` or ``text-subtle``, not opacity or base-content/<n>."""
    found = [hit for path in _markup_files() for hit in _faded_text(path)]
    assert not found, "faded text:\n" + "\n".join(found)


def test_the_guard_finds_faded_text() -> None:
    """The guard itself: it must see the patterns it is meant to stop."""
    sample = '<p class="text-sm opacity-60">x</p><p class="text-base-content/55">y</p>'
    hits = [t for m in _CLASS_ATTR.finditer(sample) for t in _FADED.findall(m.group(2))]
    assert hits == ["opacity-60", "text-base-content/55"]


# ---------------------------------------------------------------- contrast


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    channels = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _mix(a: str, b: str, share_of_a: float) -> str:
    """sRGB mix, as the browser composites an alpha or a colour-mix."""
    ca = [int(a.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    mixed = (round(share_of_a * x + (1 - share_of_a) * y) for x, y in zip(ca, cb, strict=True))
    return "#" + "".join(f"{c:02X}" for c in mixed)


_TEXT_TOKENS = ("text-muted", "text-subtle", "success-text", "warning-text", "error-text")


def _block(css: str, selector: str) -> dict[str, str]:
    body = css.split(selector + " {", 1)[1].split("}", 1)[0]
    return dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", body))


def _brand(theme: str) -> dict[str, str]:
    css = (brand_css_dir() / "evoila-tokens.css").read_text(encoding="utf-8")
    values = _block(css, ":root")
    if theme == "dark":
        values |= _block(css, '[data-theme="evoila-dark"]')
    return values


def _meho(theme: str, brand: dict[str, str]) -> dict[str, str]:
    css = (static_src_dir() / "styles.css").read_text(encoding="utf-8")
    values = _block(css, ":root")
    if theme == "dark":
        values |= _block(css, '[data-theme="evoila-dark"]')
    fills = {
        "var(--color-success)": brand["evoila-green"],
        "var(--color-warning)": brand["evoila-warning"],
        "var(--color-error)": brand["evoila-error"],
    }
    return {k: fills.get(v.strip(), v.strip()) for k, v in values.items()}


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_text_token_passes_on_every_surface(theme: str) -> None:
    brand = _brand(theme)
    meho = _meho(theme, brand)
    surface, page, accent = brand["evoila-surface"], brand["evoila-bg"], brand["evoila-bg-accent"]
    backgrounds = {
        "card": surface,
        "page": page,
        "card sheen": _mix(accent, surface, 0.45),  # top of .meho-card
    }
    soft = {  # DaisyUI badge-soft: 8% of the fill over the card
        "success": _mix(brand["evoila-green"], surface, 0.08),
        "warning": _mix(brand["evoila-warning"], surface, 0.08),
        "error": _mix(brand["evoila-error"], surface, 0.08),
    }
    failures = []
    for token in _TEXT_TOKENS:
        colour = meho[f"meho-{token}"]
        pairs = dict(backgrounds)
        kind = token.removesuffix("-text")
        if kind in soft:
            pairs[f"soft {kind} badge"] = soft[kind]
        for name, bg in pairs.items():
            r = _ratio(colour, bg)
            if r < 4.5:
                failures.append(f"{token} {colour} on {name} {bg}: {r:.2f}")
    assert not failures, "\n".join(failures)


def test_the_dark_blocks_agree() -> None:
    """evoila-dark and the dark half of evoila-system carry the same text values."""
    css = (static_src_dir() / "styles.css").read_text(encoding="utf-8")
    dark = _block(css, '[data-theme="evoila-dark"]')
    system = _block(css, '  [data-theme="evoila-system"]')
    for key in _TEXT_TOKENS:
        assert dark[f"meho-{key}"] == system[f"meho-{key}"], key


# ------------------------------------------------- text on solid surfaces


def _daisy(theme: str) -> dict[str, str]:
    css = (brand_css_dir() / "evoila-daisyui.css").read_text(encoding="utf-8")
    selector = '[data-theme="evoila-system"]' if theme == "light" else '[data-theme="evoila-dark"]'
    return {k: v.strip() for k, v in _block(css, selector).items()}


def test_text_tokens_inherit_inside_solid_alerts_and_fills() -> None:
    """A text-muted hint in a solid red alert must not be grey-on-red: the
    alert's own *-content colour is the readable one, so the tokens inherit."""
    styles = (static_src_dir() / "styles.css").read_text(encoding="utf-8")
    rule = styles.split(":is(.alert-info, .alert-success, .alert-warning, .alert-error)", 1)
    assert len(rule) == 2, "the solid-surface inherit rule is missing"
    body = rule[1].split("\n}\n", 1)[0]
    assert "& :is(.text-muted, .text-subtle, .text-success, .text-warning, .text-error)" in body
    assert "color: inherit;" in body


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("kind", ["info", "success", "warning", "error"])
def test_alert_text_reads_on_its_alert(theme: str, kind: str) -> None:
    """What the inherit rule falls back to: the alert's content colour on
    the alert's fill, as the approval error message and its hint show."""
    daisy = _daisy(theme)
    fill, text = daisy[f"color-{kind}"], daisy[f"color-{kind}-content"]
    assert _ratio(text, fill) >= 4.5, f"{kind}-content {text} on {fill}"
