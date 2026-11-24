# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Syntax-highlighting colours for code blocks, in both brand themes.

The knowledge base, runbook previews, memory entries and conventions
render fenced code through pygments. Pygments' spans carry only class
names; this module supplies the colours, with a light set and a dark
set, so a highlighted block reads in whichever theme the page shows.

* The block itself sits on ``base-200`` with ``base-content`` text, so
  plain (unhighlighted) code follows the theme. Pygments' own container
  rule -- a fixed background, sometimes a fixed text colour -- is
  dropped: it would put light text on near-white in dark mode, or dark
  text on navy in light mode.
* Light tokens use the ``xcode`` style, dark tokens ``lightbulb``. Both
  were picked because every token colour reaches 4.5:1 on the block
  background of its theme (``tests/test_ui_code_theme.py`` checks this).
  The old ``default`` style dropped to 1.15:1 in dark mode.
* The dark set is scoped the way the brand scopes its dark values:
  ``data-theme="evoila-dark"``, and ``data-theme="evoila-system"`` inside
  ``@media (prefers-color-scheme: dark)``.
"""

from __future__ import annotations

from functools import cache

from pygments.formatters import HtmlFormatter

__all__ = ["DARK_STYLE", "LIGHT_STYLE", "code_css"]

LIGHT_STYLE = "xcode"
DARK_STYLE = "lightbulb"

_DARK_THEME = '[data-theme="evoila-dark"]'
_SYSTEM_THEME = '[data-theme="evoila-system"]'


def _token_rules(style: str, scope: str) -> str:
    """Pygments' token rules for *style* under *scope*, nothing else.

    Drops the global rules pygments adds (``pre { line-height }`` and the
    line-number rules, which would restyle every ``<pre>`` on the page)
    and the container rule (``<scope> { background: ...; color: ... }``).
    """
    defs = HtmlFormatter(style=style).get_style_defs(scope)
    kept = [
        line
        for line in defs.splitlines()
        if line.startswith(scope + " .") and not line.startswith(scope + " .hll ")
    ]
    return "\n".join(kept)


@cache
def code_css(scope: str) -> str:
    """CSS for code blocks with class *scope* (for example ``.kb-code``).

    Safe to embed in a ``<style>`` element: built only from pygments'
    bundled styles and the fixed selectors above.
    """
    dark_system = "\n".join(
        "  " + line for line in _token_rules(DARK_STYLE, f"{_SYSTEM_THEME} {scope}").splitlines()
    )
    return "\n".join(
        [
            f"{scope} {{ background: var(--color-base-200); color: var(--color-base-content); }}",
            _token_rules(LIGHT_STYLE, scope),
            _token_rules(DARK_STYLE, f"{_DARK_THEME} {scope}"),
            "@media (prefers-color-scheme: dark) {",
            dark_system,
            "}",
        ]
    )
