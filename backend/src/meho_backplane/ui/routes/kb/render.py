# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Server-side Markdown → HTML renderer for the KB entry view.

Initiative #339 (G10.2 Knowledge base UI), Task #870 (T1). The renderer
wraps ``markdown-it-py`` (GFM-compatible: tables, strikethrough) with a
``pygments``-backed syntax highlighter so code blocks are annotated with
CSS class spans — no client-side JS highlighter needed.

Design decisions
----------------

* ``markdown-it-py`` ``commonmark`` preset with ``html=False`` forced
  on construction and ``table`` + ``strikethrough`` enabled explicitly.
  The ``commonmark`` preset enables ``html`` by default (the CommonMark
  spec permits raw HTML passthrough), so it is overridden to
  ``html=False`` — raw HTML in a kb body is escaped, not rendered,
  keeping the stored-XSS surface closed.
* ``pygments`` ``HtmlFormatter`` with ``nowrap=True`` so the output is
  bare ``<span>`` tokens; the template wraps them in a
  ``<pre class="highlight"><code class="language-{lang}">`` block for
  consistent styling. The ``highlight`` callback is bound at module
  construction time (not per-request) so the ``HtmlFormatter`` instance
  is shared.
* ``Markup`` wrapping: the rendered HTML string is wrapped in
  :class:`markupsafe.Markup` so Jinja2's autoescape does not
  double-escape the already-sanitised HTML. The ``markdown-it-py``
  renderer itself HTML-encodes user-supplied text (heading content,
  paragraph text, link targets) before inserting it into the output;
  the explicit ``html=False`` (see :func:`_build_md`) escapes raw HTML
  in the body rather than passing it through. Both together mean the
  rendered output is safe to pass through ``Markup``.
* ``TextLexer`` fallback: an unknown / missing lang attribute falls back
  to ``pygments.lexers.TextLexer`` (plain text, no span decoration)
  rather than guessing. Guessing frequently mis-classifies prose
  snippets as another language and injects colourful noise.

Thread-safety
-------------

``MarkdownIt`` instances are not thread-safe for concurrent ``render()``
calls against the same instance because ``render`` mutates internal
parser state. The module-level singleton is therefore guarded by a
lock; the lock is per-process (not per-request), so the overhead is
negligible compared to the I/O cost of the surrounding DB read. An
alternative would be constructing one ``MarkdownIt`` per request, but
the per-request construction cost dominates the lock-contention cost
at realistic QPS on an operator-facing surface.

Reference
---------

* markdown-it-py API: https://markdown-it-py.readthedocs.io/en/latest/
* pygments HtmlFormatter: https://pygments.org/docs/formatters/
"""

from __future__ import annotations

import threading
from html import escape as _html_escape

from markdown_it import MarkdownIt
from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.util import ClassNotFound

__all__ = ["pygments_css", "render_markdown"]

_lock = threading.Lock()

#: Shared HtmlFormatter instance used for the highlight callback:
#: ``nowrap=True`` emits bare ``<span>`` tokens so the wrapping
#: ``<pre><code>`` block is controlled by the highlight callback.
#: ``cssclass`` is set to a non-default value so the generated style
#: rules don't collide with DaisyUI's own ``.highlight`` utility class.
#: The token *colours* are style-independent (the emitted spans carry
#: only class names); the theme-scoped colour rules come from
#: :data:`_PYGMENTS_CSS`.
_FORMATTER = HtmlFormatter(nowrap=True, cssclass="kb-code")

#: Pygments styles paired to the console's two DaisyUI 5 themes. The
#: default/dark scope uses a dark-background style (light token colours);
#: the ``meho-light`` scope keeps pygments' light ``default`` style (dark
#: token colours). Both surfaces sit on ``var(--color-base-200)``, which
#: the templates pin — so the code block follows the active theme instead
#: of the dead DaisyUI 4 ``--b2`` variable that used to force a light
#: background in both themes (#2452).
_DARK_STYLE = "github-dark"
_LIGHT_STYLE = "default"

#: Selector the light-theme rules are scoped under. Higher specificity
#: than the bare ``.kb-code`` default block, so it wins when the
#: ``data-theme="meho-light"`` attribute is present and is inert
#: otherwise.
_LIGHT_SCOPE = '[data-theme="meho-light"] .kb-code'


def _style_defs(style: str, scope: str, *, tokens_only: bool) -> str:
    """Return pygments colour rules for *style*, scoped under *scope*.

    The opaque container rule pygments emits (``<scope> { background:
    ...; color: ... }``) is always dropped: the template owns the code
    block background via ``var(--color-base-200)`` so it follows the
    active theme, and keeping pygments' hard-coded background/foreground
    would (a) fight the DaisyUI surface and (b) leak a light style's dark
    text — or a dark style's light text — into the wrong theme.

    ``tokens_only`` additionally drops the style-independent global rules
    (``pre { line-height }``, ``td.linenos`` / ``span.linenos``) so the
    second (light) block does not re-emit them; the first (dark) block
    keeps them once.
    """
    defs = HtmlFormatter(nowrap=True, cssclass="kb-code", style=style).get_style_defs(scope)
    container_prefix = f"{scope} {{ background:"
    kept: list[str] = []
    for line in defs.splitlines():
        if line.startswith(container_prefix):
            continue
        if tokens_only and not line.startswith(scope):
            continue
        kept.append(line)
    return "\n".join(kept)


#: Cached, theme-scoped CSS for the ``kb-code`` formatter. Generated once
#: at module load; safe to embed in a ``<style>`` block. Two blocks: the
#: default/dark token colours under ``.kb-code`` and the light-theme
#: overrides under ``[data-theme="meho-light"] .kb-code``.
_PYGMENTS_CSS: str = "\n".join(
    (
        _style_defs(_DARK_STYLE, ".kb-code", tokens_only=False),
        _style_defs(_LIGHT_STYLE, _LIGHT_SCOPE, tokens_only=True),
    )
)


def _highlight_code(code: str, lang: str, attrs: str) -> str:
    """Pygments callback for ``markdown-it-py``'s ``highlight`` option.

    Returns a ``<pre class="kb-code"><code class="language-{lang}">``
    block with pygments span annotations inside. Unknown ``lang`` values
    fall back to ``TextLexer`` (no decoration) rather than guessing.
    The ``attrs`` argument (HTMX fenced-code attributes) is ignored in
    v0.2 — the task body does not require attribute parsing.
    """
    if lang:
        try:
            lexer = get_lexer_by_name(lang, stripall=True)
        except ClassNotFound:
            lexer = TextLexer()
    else:
        lexer = TextLexer()
    highlighted = highlight(code, lexer, _FORMATTER)
    lang_attr = _html_escape(lang, quote=True) if lang else ""
    return f'<pre class="kb-code"><code class="language-{lang_attr}">{highlighted}</code></pre>'


def _build_md() -> MarkdownIt:
    """Construct the shared ``MarkdownIt`` instance.

    Called once at module load. The ``commonmark`` preset enables
    ``html`` (the CommonMark spec permits raw HTML passthrough), so it is
    overridden to ``html=False`` explicitly here — raw HTML in a kb body
    is escaped, not rendered, closing the stored-XSS surface. ``table``
    enables GFM-style pipe tables; ``strikethrough`` enables ``~~text~~``.
    """
    return (
        MarkdownIt("commonmark", {"html": False, "highlight": _highlight_code})
        .enable("table")
        .enable("strikethrough")
    )


_MD: MarkdownIt = _build_md()


def render_markdown(body: str) -> Markup:
    """Render *body* (a kb entry's raw Markdown) to safe HTML.

    Returns :class:`markupsafe.Markup` so Jinja2's autoescape does not
    double-escape the generated HTML spans. The rendered string is safe
    because ``markdown-it-py`` HTML-encodes user text and the explicit
    ``html=False`` escapes any raw HTML tags in the source.

    Thread-safe: holds the module-level lock for the duration of the
    ``render()`` call.
    """
    with _lock:
        html = _MD.render(body)
    return Markup(html)


def pygments_css() -> str:
    """Return the theme-scoped CSS rules for the ``kb-code`` formatter.

    Callers inject this into a ``<style>`` block in the entry-detail
    template. The string carries two token-colour rule sets: a
    default/dark set under ``.kb-code`` and a ``[data-theme="meho-light"]
    .kb-code`` override, so code blocks stay legible in both console
    themes. The container background is deliberately omitted — the
    template pins it to ``var(--color-base-200)`` so the block follows
    the active theme. The string is pure CSS (no ``<style>`` wrapper) so
    callers can compose it with other inline styles as needed.
    """
    return _PYGMENTS_CSS
