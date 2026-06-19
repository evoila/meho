# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit contract for the ``/ui/corpus`` ask-fail-open seam (#1918).

When the ``ask_docs`` answer pipeline fails, the ``/ui/corpus`` Ask mode
(landing in #1917) must **fail open to the retrieved chunks** — render the
raw evidence under a banner that **names the failed leg** — rather than
discard usable chunks behind a bare error. This is the reusable seam
``corpus_ask_fallback_context`` + the ``ask_fallback`` branch of
``corpus/_results.html`` that #1918 wires together so #1917 only has to
call the seam from its Ask toggle.

These tests cover the seam coherently *today* (the Ask toggle itself is
#1917): the helper builds the right context from the structured #1918
answer-error envelope, and the template renders the named-leg banner +
fail-open chunk cards. The render goes straight at the Jinja partial (no
session / JWT harness) since the seam is a pure function plus a template
branch, not yet an HTTP route.

Fail-open vs fail-closed: the operator never sees an ungrounded synthesized
answer (the answer stays fail-closed — there is no answer in the fallback
context), but they DO see the chunks the pipeline retrieved plus the named
reason synthesis did not complete.
"""

from __future__ import annotations

from meho_backplane.docs_search import DocsChunk
from meho_backplane.docs_search.answer_errors import (
    CAUSE_CORPUS_UNAVAILABLE,
    CAUSE_SYNTHESIS_PARSE,
    LEG_CORPUS,
    LEG_SYNTHESIS,
    AskDocsAnswerError,
)
from meho_backplane.ui.routes.corpus.routes import corpus_ask_fallback_context
from meho_backplane.ui.templating import get_templates

_RESULTS_TEMPLATE = "corpus/_results.html"


def _chunk(
    *,
    chunk_id: str = "c-1",
    content: str = "Snapshots quiesce the guest before capture.",
    source_url: str | None = "https://docs.vmware.test/snapshots",
) -> DocsChunk:
    """Build a minimal :class:`DocsChunk` for the fallback render."""
    return DocsChunk(
        chunk_id=chunk_id,
        document_id="vsphere-snapshots",
        content=content,
        source_url=source_url,
        score=0.87,
    )


def _render_results(**context: object) -> str:
    """Render the ``corpus/_results.html`` partial with *context*."""
    template = get_templates().env.get_template(_RESULTS_TEMPLATE)
    return template.render(**context)


def test_fallback_context_carries_leg_cause_and_resolved_chunks() -> None:
    """The seam projects the answer-error envelope + resolved-link chunks.

    ``cited`` is the #1919 ``[{chunk, link}]`` shape (each chunk paired with
    its resolved navigable link), so the fallback render reuses the exact
    citation card the search path renders.
    """
    err = AskDocsAnswerError(
        leg=LEG_SYNTHESIS,
        cause=CAUSE_SYNTHESIS_PARSE,
        message="synthesis leg failed: non-JSON output",
    )
    context = corpus_ask_fallback_context(err, [_chunk()])

    assert context["ask_fallback_leg"] == LEG_SYNTHESIS
    assert context["ask_fallback_cause"] == CAUSE_SYNTHESIS_PARSE
    assert "synthesis leg failed" in str(context["ask_fallback_message"])
    cited = context["cited"]
    assert isinstance(cited, list)
    assert len(cited) == 1
    # The #1919 pairing: chunk + resolved CitationLink.
    entry = cited[0]
    assert entry["chunk"].chunk_id == "c-1"  # type: ignore[index]
    assert entry["link"].clickable is True  # type: ignore[index]


def test_synthesis_fallback_renders_named_leg_banner_and_chunk_cards() -> None:
    """A synthesis failure renders the named-leg banner + the retrieved chunks.

    The fail-open case: synthesis broke, but retrieval produced chunks, so
    the operator gets the banner (naming ``synthesis_malformed`` / ``parse``)
    AND the chunk cards (content + resolved citation link).
    """
    err = AskDocsAnswerError(
        leg=LEG_SYNTHESIS,
        cause=CAUSE_SYNTHESIS_PARSE,
        message="synthesis leg failed: non-JSON output",
    )
    html = _render_results(**corpus_ask_fallback_context(err, [_chunk()]))

    # Banner names the failed leg + sub-cause.
    assert "Answer unavailable" in html
    assert LEG_SYNTHESIS in html
    assert CAUSE_SYNTHESIS_PARSE in html
    # Fail open: the retrieved chunk content + its resolved link are rendered.
    assert "Snapshots quiesce the guest" in html
    assert 'href="https://docs.vmware.test/snapshots"' in html


def test_corpus_leg_fallback_renders_banner_alone_when_no_chunks() -> None:
    """An expand/corpus leg failure (no chunks) renders the named banner alone.

    Those legs fail before retrieval produces usable chunks, so there is
    nothing to fail open *to* — the operator still gets the named-leg banner
    so the failure is diagnosable, just without chunk cards.
    """
    err = AskDocsAnswerError(
        leg=LEG_CORPUS,
        cause=CAUSE_CORPUS_UNAVAILABLE,
        message="corpus leg failed: corpus_url is not configured",
    )
    html = _render_results(**corpus_ask_fallback_context(err, []))

    assert "Answer unavailable" in html
    assert LEG_CORPUS in html
    # No chunk cards (the cited list is empty).
    assert 'role="list" aria-label="Cited chunks"' not in html


def test_fallback_branch_takes_precedence_over_plain_cited() -> None:
    """The ``ask_fallback`` branch wins over the plain ``cited`` branch.

    Both branches can carry a non-empty ``cited`` list, so first-match-wins
    ordering matters: when an answer leg failed, the render must show the
    named-leg banner, not silently fall through to the plain cited-chunks
    heading as if the search succeeded.
    """
    err = AskDocsAnswerError(
        leg=LEG_SYNTHESIS,
        cause=CAUSE_SYNTHESIS_PARSE,
        message="synthesis leg failed",
    )
    html = _render_results(**corpus_ask_fallback_context(err, [_chunk()]))

    assert "Answer unavailable" in html
    # The plain-cited heading ("N cited chunk(s) for ...") must NOT appear —
    # that branch is only for a clean search, not a failed answer.
    assert "cited chunk" not in html.replace("Cited chunks", "")
