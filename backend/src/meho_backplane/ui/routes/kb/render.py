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

#: Shared HtmlFormatter instance: ``nowrap=True`` emits bare ``<span>``
#: tokens so the wrapping ``<pre><code>`` block is controlled by the
#: highlight callback. ``cssclass`` is set to a non-default value so
#: the generated style rules don't collide with DaisyUI's own ``.highlight``
#: utility class. The CSS is emitted once via :func:`pygments_css` and
#: injected as a ``<style>`` block in the entry-detail template.
_FORMATTER = HtmlFormatter(nowrap=True, cssclass="kb-code")

#: Cached CSS for the current pygments style. Generated once at module
#: load; safe to embed in a ``<style>`` block.
_PYGMENTS_CSS: str = _FORMATTER.get_style_defs(".kb-code")


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
    """Return the CSS rules for the ``kb-code`` pygments formatter.

    Callers inject this into a ``<style>`` block in the entry-detail
    template so code blocks render with the default pygments style.
    The string is pure CSS (no ``<style>`` wrapper) so callers can
    compose it with other inline styles as needed.
    """
    return _PYGMENTS_CSS
