# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit contract for the structured ``ask_docs`` answer-error envelope (#1918).

Exercises :func:`meho_backplane.docs_search.answer_errors.classify_answer_error`
and :class:`~meho_backplane.docs_search.answer_errors.AskDocsAnswerError`
directly — the framework-agnostic core both the MCP ``ask_docs`` tool and
the forthcoming REST ``ask_docs`` route (#1917) build their wire error on,
without any JSON-RPC / HTTP plumbing.

The contract: each of the four pipeline legs (expand / corpus / model /
synthesis) maps to a **distinct** ``(leg, cause)`` pair; the synthesis leg
carries the parse-vs-citation-resolution sub-cause through verbatim; a
non-leg exception is not classified (``None``) so the caller falls through
to its generic catch.
"""

from __future__ import annotations

import pytest

from meho_backplane.auth.corpus import CorpusAnswerError, CorpusUnavailable
from meho_backplane.docs_search.answer_errors import (
    ANSWER_ERROR_DETAIL,
    CAUSE_CLIENT_UNAVAILABLE,
    CAUSE_CORPUS_UNAVAILABLE,
    CAUSE_EXPANSION_INVALID,
    CAUSE_SYNTHESIS_CITATION_RESOLUTION,
    CAUSE_SYNTHESIS_PARSE,
    CAUSE_SYNTHESIS_TRUNCATED,
    CAUSE_UPSTREAM_ANSWER_UNAVAILABLE,
    CAUSE_UPSTREAM_ERROR,
    CAUSE_UPSTREAM_RATE_LIMITED,
    CAUSE_UPSTREAM_REJECTED,
    LEG_CORPUS,
    LEG_EXPAND,
    LEG_MODEL,
    LEG_SYNTHESIS,
    AskDocsAnswerError,
    classify_answer_error,
)
from meho_backplane.docs_search.expansion import DocsQueryExpansionError
from meho_backplane.docs_search.synthesis import (
    SYNTHESIS_CAUSE_CITATION_RESOLUTION,
    SYNTHESIS_CAUSE_PARSE,
    SYNTHESIS_CAUSE_TRUNCATED,
    DocsSynthesisError,
)
from meho_backplane.operations.ingest import LlmClientUnavailable


def test_expand_invalid_output_classifies_to_expand_leg() -> None:
    """A ``DocsQueryExpansionError`` names the ``expand_failed`` leg."""
    err = classify_answer_error(DocsQueryExpansionError("bad expansion json"))
    assert isinstance(err, AskDocsAnswerError)
    assert err.leg == LEG_EXPAND
    assert err.cause == CAUSE_EXPANSION_INVALID


def test_corpus_unavailable_classifies_to_corpus_leg() -> None:
    """A ``CorpusUnavailable`` names the ``corpus_unavailable`` leg."""
    err = classify_answer_error(CorpusUnavailable("corpus_url is not configured"))
    assert isinstance(err, AskDocsAnswerError)
    assert err.leg == LEG_CORPUS
    assert err.cause == CAUSE_CORPUS_UNAVAILABLE


def test_llm_unavailable_defaults_to_model_leg() -> None:
    """A bare ``LlmClientUnavailable`` defaults to the ``model_unavailable`` leg."""
    err = classify_answer_error(LlmClientUnavailable("no ANTHROPIC_API_KEY"))
    assert isinstance(err, AskDocsAnswerError)
    assert err.leg == LEG_MODEL
    assert err.cause == CAUSE_CLIENT_UNAVAILABLE


def test_llm_unavailable_pinned_to_expand_leg_by_hint() -> None:
    """The caller can pin a bare ``LlmClientUnavailable`` to the expand leg.

    A missing model fails whichever leg reaches the shared #1386 client
    first; only the caller knows the pipeline position. The expand leg
    passes ``llm_unavailable_leg=LEG_EXPAND`` so the same exception type is
    attributed to ``expand_failed`` there and ``model_unavailable`` at
    synthesis.
    """
    err = classify_answer_error(
        LlmClientUnavailable("no ANTHROPIC_API_KEY"),
        llm_unavailable_leg=LEG_EXPAND,
    )
    assert isinstance(err, AskDocsAnswerError)
    assert err.leg == LEG_EXPAND
    assert err.cause == CAUSE_CLIENT_UNAVAILABLE


def test_synthesis_parse_failure_carries_parse_sub_cause() -> None:
    """A parse-class ``DocsSynthesisError`` names ``synthesis_malformed`` / ``parse``."""
    err = classify_answer_error(DocsSynthesisError("non-JSON output", cause=SYNTHESIS_CAUSE_PARSE))
    assert isinstance(err, AskDocsAnswerError)
    assert err.leg == LEG_SYNTHESIS
    assert err.cause == CAUSE_SYNTHESIS_PARSE


def test_synthesis_citation_failure_carries_citation_sub_cause() -> None:
    """A citation-resolution ``DocsSynthesisError`` carries that sub-cause through."""
    err = classify_answer_error(
        DocsSynthesisError(
            "cited id not in retrieved set",
            cause=SYNTHESIS_CAUSE_CITATION_RESOLUTION,
        )
    )
    assert isinstance(err, AskDocsAnswerError)
    assert err.leg == LEG_SYNTHESIS
    assert err.cause == CAUSE_SYNTHESIS_CITATION_RESOLUTION


def test_synthesis_truncated_failure_carries_truncated_sub_cause() -> None:
    """A truncated ``DocsSynthesisError`` surfaces ``cause=truncated`` (#1999).

    The envelope distinguishes a 1024-token cutoff from a framing fault so
    the ``synthesis_malformed`` leg names a token-ceiling problem rather
    than folding it into the generic ``parse`` sub-cause.
    """
    err = classify_answer_error(
        DocsSynthesisError("response truncated", cause=SYNTHESIS_CAUSE_TRUNCATED)
    )
    assert isinstance(err, AskDocsAnswerError)
    assert err.leg == LEG_SYNTHESIS
    assert err.cause == CAUSE_SYNTHESIS_TRUNCATED


def test_synthesis_sub_cause_constants_agree_across_modules() -> None:
    """The answer-error sub-cause constants equal the synthesis ones.

    Two modules name the same values; this pins them together so a rename
    in one is caught rather than silently diverging the wire contract.
    """
    assert CAUSE_SYNTHESIS_PARSE == SYNTHESIS_CAUSE_PARSE
    assert CAUSE_SYNTHESIS_TRUNCATED == SYNTHESIS_CAUSE_TRUNCATED
    assert CAUSE_SYNTHESIS_CITATION_RESOLUTION == SYNTHESIS_CAUSE_CITATION_RESOLUTION


def test_each_leg_is_distinct() -> None:
    """All four legs are distinct codes — a caller can branch on the leg."""
    legs = {LEG_EXPAND, LEG_CORPUS, LEG_MODEL, LEG_SYNTHESIS}
    assert len(legs) == 4


def test_non_leg_exception_is_not_classified() -> None:
    """An unrelated exception is not classified, so the caller falls through.

    A genuinely unexpected fault must stay a plain ``-32603`` / 500, not be
    mis-labelled as one of the four answer legs.
    """
    assert classify_answer_error(ValueError("something else entirely")) is None
    assert classify_answer_error(RuntimeError("generic")) is None


def test_to_error_data_is_json_safe_envelope() -> None:
    """``to_error_data`` renders the stable ``{detail, leg, cause, message}`` dict.

    This dict is the wire contract shared by the MCP ``error.data`` member
    and the REST ``HTTPException.detail`` body (#1917), so it must be plain
    primitives and carry the stable family classifier.
    """
    err = classify_answer_error(CorpusUnavailable("corpus returned HTTP 503"))
    assert err is not None
    data = err.to_error_data()
    assert data == {
        "detail": ANSWER_ERROR_DETAIL,
        "leg": LEG_CORPUS,
        "cause": CAUSE_CORPUS_UNAVAILABLE,
        "message": str(err),
    }
    # Every value is a JSON primitive.
    assert all(isinstance(v, str) for v in data.values())
    # The family classifier is the stable snake_case token.
    assert data["detail"] == "ask_docs_failed"


# ---------------------------------------------------------------------------
# Upstream answer endpoint failures (#3911)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "status", "leg", "cause"),
    [
        (CorpusAnswerError.KIND_REJECTED, 422, LEG_CORPUS, CAUSE_UPSTREAM_REJECTED),
        (CorpusAnswerError.KIND_SERVER_ERROR, 502, LEG_CORPUS, CAUSE_UPSTREAM_ERROR),
        (
            CorpusAnswerError.KIND_ANSWER_UNAVAILABLE,
            503,
            LEG_MODEL,
            CAUSE_UPSTREAM_ANSWER_UNAVAILABLE,
        ),
        (CorpusAnswerError.KIND_RATE_LIMITED, 503, LEG_MODEL, CAUSE_UPSTREAM_RATE_LIMITED),
        (CorpusAnswerError.KIND_MALFORMED, 200, LEG_SYNTHESIS, CAUSE_SYNTHESIS_PARSE),
    ],
)
def test_upstream_answer_error_maps_to_leg_and_cause(
    kind: str, status: int, leg: str, cause: str
) -> None:
    """Each upstream-answer failure kind lands in one existing leg with its own cause."""
    err = classify_answer_error(CorpusAnswerError("upstream failed", kind=kind, status=status))
    assert isinstance(err, AskDocsAnswerError)
    assert (err.leg, err.cause) == (leg, cause)


def test_upstream_rejection_carries_upstream_status() -> None:
    """A 4xx names the upstream status on the envelope (no body)."""
    err = classify_answer_error(
        CorpusAnswerError("rejected", kind=CorpusAnswerError.KIND_REJECTED, status=404)
    )
    assert err is not None
    data = err.to_error_data()
    assert data["upstream_status"] == 404
    assert "retry_after" not in data


def test_upstream_rate_limit_carries_retry_after() -> None:
    """A rate-limited upstream answer carries ``retry_after`` on the envelope."""
    err = classify_answer_error(
        CorpusAnswerError(
            "rate limited",
            kind=CorpusAnswerError.KIND_RATE_LIMITED,
            status=503,
            retry_after=7,
        )
    )
    assert err is not None
    assert err.retry_after == 7
    data = err.to_error_data()
    assert data["retry_after"] == 7
    assert "upstream_status" not in data


def test_local_envelope_has_no_upstream_fields() -> None:
    """The local pipeline's envelope keeps exactly its four keys."""
    err = classify_answer_error(CorpusUnavailable("corpus unreachable: ConnectError"))
    assert err is not None
    assert set(err.to_error_data()) == {"detail", "leg", "cause", "message"}
