# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Server-side Markdown -> HTML renderer for the memory detail view.

Initiative #341 (G10.4 Memory UI), Task #877 (T1). Mirrors the precedent
:mod:`~meho_backplane.ui.routes.kb.render` sets for the KB read surface
(G10.2-T1 #870): ``markdown-it-py`` (commonmark with ``table`` +
``strikethrough`` enabled, ``html=False``) for the parse + render, and
``pygments`` with :class:`HtmlFormatter` for code-block syntax
highlighting. The two modules duplicate ~50 LOC by design while #870 /
#877 are in flight on parallel branches; once both PRs land on ``main``
a follow-up consolidation can promote one of them to a shared
``ui.markdown`` module.

Design decisions
----------------

* ``markdown-it-py`` ``commonmark`` preset is constructed with
  ``html=False`` so a raw ``<script>`` (or ``<iframe`` / ``<object``)
  in a memory body is rendered as escaped text, not parsed as HTML.
  The ``commonmark`` preset's default for ``html`` is ``True``
  (verified against markdown-it-py 4.2.0 installed locally on
  2026-05-26), so the override is load-bearing -- a v0.2.next
  upgrade that flips the default elsewhere will be caught by the
  ``commonmark`` -> ``zero`` migration tests but not by this module's
  unit tests.
* ``table`` + ``strikethrough`` are enabled explicitly. The
  ``gfm-like`` preset would enable those plus ``html=True``; the
  explicit-enable shape keeps the raw-HTML surface closed while still
  giving operators GFM-flavoured Markdown.
* ``linkify`` (``https://example.com`` -> ``<a href="...">``) is
  enabled so memory bodies that paste a runbook URL render as a
  clickable link without forcing the operator to wrap it in
  ``[text](url)`` syntax.
* ``pygments`` :class:`HtmlFormatter` with ``nowrap=True`` emits bare
  ``<span>`` tokens -- the wrapping ``<pre class="memory-code"><code
  class="language-{lang}">`` block is controlled by the highlight
  callback so the styling matches DaisyUI's ``mockup-code`` aesthetics
  uniformly across the entry.
* :class:`TextLexer` fallback: an unknown ``lang`` attribute falls
  back to plain text (no decoration) rather than guessing. Pygments'
  language-guesser frequently mis-classifies prose snippets as
  another language and injects colourful noise; the memory surface
  prefers boring + correct over clever + wrong.
* ``Markup`` wrapping: the rendered HTML string is wrapped in
  :class:`markupsafe.Markup` so Jinja2's autoescape does not
  double-escape the already-sanitised HTML. The ``markdown-it-py``
  renderer HTML-encodes user-supplied text (heading content,
  paragraph text, link targets) before inserting it into the output;
  with ``html=False`` raw HTML in the body is stripped to escaped
  text. Both together mean the rendered output is safe to pass
  through ``Markup``.

Thread-safety
-------------

``MarkdownIt`` instances are not thread-safe for concurrent ``render()``
calls against the same instance: ``render`` mutates internal parser
state (the token stream is held on the instance for the duration of the
call). The module-level singleton is therefore guarded by a lock; the
lock is per-process (not per-request), so the overhead is negligible
compared to the I/O cost of the surrounding DB read. An alternative
would be constructing one ``MarkdownIt`` per request, but the per-
request construction cost dominates the lock-contention cost at
realistic QPS on an operator-facing surface.

References
----------

* markdown-it-py API: https://markdown-it-py.readthedocs.io/en/latest/
* pygments HtmlFormatter: https://pygments.org/docs/formatters/
"""

from __future__ import annotations

import re
import threading

from markdown_it import MarkdownIt
from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.util import ClassNotFound

__all__ = ["pygments_css", "render_markdown"]

_lock = threading.Lock()

#: Conservative allowlist for the fenced-code ``language-{lang}`` class
#: attribute. Pygments lexer aliases use this character set
#: (``python``, ``c++``, ``objective-c``, ``shell-session``), so the
#: filter passes every well-formed lang token through while dropping
#: anything that could break out of the ``class="..."`` attribute. A
#: ``lang`` string that doesn't fullmatch falls back to ``text``.
#: Defence against attribute-injection XSS:
#:   ```a"onmouseover="alert(1)"x
#: would otherwise interpolate verbatim into ``class="language-..."``
#: and ship a working JS handler to the browser.
_LANG_PATTERN_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_+\-.]+$")

#: Shared :class:`HtmlFormatter` instance. ``nowrap=True`` emits bare
#: ``<span>`` tokens so the wrapping ``<pre><code>`` block is
#: controlled by the highlight callback. ``cssclass`` is set to a
#: memory-namespaced value so the generated style rules don't collide
#: with DaisyUI's own ``.highlight`` utility class.
_FORMATTER = HtmlFormatter(nowrap=True, cssclass="memory-code")

#: Cached CSS for the current pygments style. Generated once at module
#: load; safe to embed in a ``<style>`` block on the detail page.
_PYGMENTS_CSS: str = _FORMATTER.get_style_defs(".memory-code")


def _highlight_code(code: str, lang: str, attrs: str) -> str:
    """Pygments callback for ``markdown-it-py``'s ``highlight`` option.

    Returns a ``<pre class="memory-code"><code class="language-{lang}">``
    block with pygments span annotations inside. Unknown ``lang``
    values fall back to :class:`TextLexer` (no decoration) rather than
    guessing. The ``attrs`` argument (fenced-code attributes) is
    ignored in v0.2 -- the task body does not require attribute
    parsing.
    """
    del attrs  # not consumed in v0.2; v0.2.next may use for ``hl_lines=...``
    if lang:
        try:
            lexer = get_lexer_by_name(lang, stripall=True)
        except ClassNotFound:
            lexer = TextLexer()
    else:
        lexer = TextLexer()
    highlighted = highlight(code, lexer, _FORMATTER)
    # Match the wrapping shape the KB renderer uses so a future
    # consolidation can collapse the two modules into one. The lang
    # token is dropped to ``text`` unless it fullmatches the
    # conservative allowlist -- markdown-it-py hands the fenced-code
    # info string through verbatim, so a body like
    # ```a"onmouseover="alert(1)"x  would otherwise inject a JS event
    # handler into the rendered ``class="language-..."`` attribute.
    lang_class = lang if lang and _LANG_PATTERN_RE.fullmatch(lang) else "text"
    return (
        f'<pre class="memory-code"><code class="language-{lang_class}">{highlighted}</code></pre>'
    )


#: Process-wide :class:`MarkdownIt` instance. Constructed once at
#: module import so the parser + the bound highlight callback are
#: hot-path-cheap. The ``commonmark`` preset's ``html`` default is
#: ``True``; the explicit ``html=False`` override is the load-bearing
#: defence against raw HTML smuggled inside memory bodies. ``linkify``
#: turns bare ``https://...`` URLs into ``<a>`` tags so a pasted
#: runbook URL is clickable without ``[text](url)`` wrapping.
_MD = MarkdownIt(
    "commonmark",
    {"html": False, "linkify": True, "highlight": _highlight_code},
).enable(["table", "strikethrough", "linkify"])


def render_markdown(body: str) -> Markup:
    """Render *body* as Markdown -> HTML and return safe Jinja markup.

    The :class:`markdown_it.MarkdownIt.render` call mutates internal
    parser state, so the call is guarded by a process-level lock. The
    returned :class:`Markup` flags the string as already-safe so
    Jinja2's autoescape doesn't double-escape the output. The renderer
    itself HTML-encodes every user-supplied text node and strips raw
    HTML (``html=False``), so the ``Markup`` wrap is sound.
    """
    with _lock:
        rendered = _MD.render(body)
    return Markup(rendered)


def pygments_css() -> str:
    """Return the pre-generated pygments CSS for the ``.memory-code`` class.

    Embedded as a ``<style>`` block on the entry-detail page so the
    syntax-highlight span colours render without a separate stylesheet
    round-trip. Cached at module load; safe to call on every request.
    """
    return _PYGMENTS_CSS
