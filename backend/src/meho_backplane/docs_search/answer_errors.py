# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Structured answer-error envelope for the ``ask_docs`` pipeline (#1918).

The ``ask_docs`` answer pipeline runs four legs in order — **expand**
(#1916), **retrieve** (the corpus transport), **model** (synthesis), and
**synthesis** (parse + citation-resolution of the model's output) — and
each leg has its own typed failure. Before this module every leg collapsed
to one opaque JSON-RPC ``-32603`` ``"internal error: <ClassName>"`` at the
dispatcher's generic catch (``mcp/server.py``): a consumer hitting a
failure could not tell a config gap (no ``ANTHROPIC_API_KEY``) from a
backend outage (corpus down) from a model-output bug (malformed
synthesis), and so mis-diagnosed (``claude-rdc-hetzner-dc#1407`` gap 2).

This module is the **one** place that maps a raised leg exception onto a
structured, JSON-safe envelope naming *which* leg failed and *why*. It is
framework-agnostic on purpose — it imports neither FastAPI nor the MCP
wire types — so the same classification backs every surface:

* the MCP ``ask_docs`` tool raises :class:`AskDocsAnswerError` and the
  dispatcher emits its :meth:`~AskDocsAnswerError.to_error_data` payload on
  the JSON-RPC ``error.data`` member (spec §5.1), keeping the code
  ``-32603`` (a server-side answer fault, not invalid params);
* the forthcoming REST ``POST /api/v1/ask_docs`` (#1917, T2) reuses
  :func:`classify_answer_error` to build a 4xx/5xx detail with the same
  ``leg`` / ``cause`` fields (the envelope shape is the wire contract both
  faces share, exactly as
  :mod:`meho_backplane.operations.ingest.error_envelopes` is shared by the
  connector-ingest REST + MCP faces);
* the ``/ui/corpus`` Ask mode (#1917) reads the same ``leg`` to render its
  fail-open-to-chunks banner with the failed leg named.

Fail-closed is preserved end to end: classifying an error never produces an
answer. A leg failure is surfaced as an error envelope, never swallowed
into a degraded / ungrounded answer.

Envelope shape (load-bearing — the CLI, the MCP-driving agent, the REST
client, and the UI all parse it):

* ``detail`` — the stable ``snake_case`` classifier ``"ask_docs_failed"``
  so a client branches on the error family without re-parsing the message
  (the T11 #1141 convention every other structured envelope follows).
* ``leg`` — which pipeline leg failed: one of :data:`LEG_EXPAND` /
  :data:`LEG_CORPUS` / :data:`LEG_MODEL` / :data:`LEG_SYNTHESIS`.
* ``cause`` — a leg-scoped sub-cause classifier, finer than ``leg``: e.g.
  the synthesis leg splits ``parse`` vs ``citation_resolution`` (the
  #1918 sub-cause split on :class:`DocsSynthesisError`), the expand /
  model legs distinguish ``client_unavailable`` (no model configured) from
  ``expansion_invalid`` (the model ran but its output was unusable).
* ``message`` — the rendered human-readable detail for clients that ignore
  the structured fields. Never carries a corpus response body or a raw LLM
  output (the typed leg exceptions already guarantee neither is attached),
  so nothing upstream leaks through the envelope.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.corpus import CorpusUnavailable
from meho_backplane.docs_search.expansion import DocsQueryExpansionError
from meho_backplane.docs_search.synthesis import DocsSynthesisError
from meho_backplane.operations.ingest import LlmClientUnavailable

__all__ = [
    "ANSWER_ERROR_DETAIL",
    "CAUSE_CLIENT_UNAVAILABLE",
    "CAUSE_CORPUS_UNAVAILABLE",
    "CAUSE_EXPANSION_INVALID",
    "CAUSE_SYNTHESIS_CITATION_RESOLUTION",
    "CAUSE_SYNTHESIS_PARSE",
    "CAUSE_SYNTHESIS_TRUNCATED",
    "LEG_CORPUS",
    "LEG_EXPAND",
    "LEG_MODEL",
    "LEG_SYNTHESIS",
    "AskDocsAnswerError",
    "classify_answer_error",
]

#: Stable ``snake_case`` error-family classifier. Carried on every
#: envelope so a client branches on "this is an ask_docs answer failure"
#: without re-parsing the message (T11 #1141 convention).
ANSWER_ERROR_DETAIL: Final[str] = "ask_docs_failed"

#: The four answer-pipeline legs, in run order. The ``leg`` field is the
#: coarse "where did it break" axis; ``cause`` refines it.
LEG_EXPAND: Final[str] = "expand_failed"
LEG_CORPUS: Final[str] = "corpus_unavailable"
LEG_MODEL: Final[str] = "model_unavailable"
LEG_SYNTHESIS: Final[str] = "synthesis_malformed"

#: Sub-cause classifiers. ``client_unavailable`` is shared by the expand
#: and model legs (both reuse the #1386 fail-closed Anthropic client, so a
#: missing ``ANTHROPIC_API_KEY`` fails whichever leg reaches the model
#: first); the leg field disambiguates which one.
CAUSE_CLIENT_UNAVAILABLE: Final[str] = "client_unavailable"
CAUSE_EXPANSION_INVALID: Final[str] = "expansion_invalid"
CAUSE_CORPUS_UNAVAILABLE: Final[str] = "corpus_unavailable"
#: Mirror the synthesis sub-cause constants so callers branch on one
#: vocabulary; the values equal
#: :data:`meho_backplane.docs_search.synthesis.SYNTHESIS_CAUSE_PARSE` /
#: ``SYNTHESIS_CAUSE_TRUNCATED`` / ``SYNTHESIS_CAUSE_CITATION_RESOLUTION``
#: (asserted in tests).
CAUSE_SYNTHESIS_PARSE: Final[str] = "parse"
CAUSE_SYNTHESIS_TRUNCATED: Final[str] = "truncated"
CAUSE_SYNTHESIS_CITATION_RESOLUTION: Final[str] = "citation_resolution"


class AskDocsAnswerError(RuntimeError):
    """A leg-named ``ask_docs`` failure carrying a structured envelope.

    Raised by the ``ask_docs`` surfaces (MCP today; REST + UI Ask mode in
    #1917) after :func:`classify_answer_error` maps a raised leg exception
    onto a ``(leg, cause)`` pair. The MCP dispatcher surfaces it as
    JSON-RPC ``-32603`` with :meth:`to_error_data` on ``error.data``; a
    REST route renders the same dict as its ``HTTPException.detail`` (4xx
    for a client-config leg, 5xx for a backend leg — the route layer
    chooses the status, this model only names the leg).

    The original leg exception is preserved via ``raise ... from`` at the
    raise site (and on ``__cause__``), so the structlog ``exception``
    breadcrumb keeps the full traceback while the wire envelope stays
    scrubbed.
    """

    def __init__(self, *, leg: str, cause: str, message: str) -> None:
        self.leg = leg
        self.cause = cause
        super().__init__(message)

    def to_error_data(self) -> dict[str, Any]:
        """Render the JSON-safe ``error.data`` / REST-detail envelope.

        Pure ``dict`` of primitives (no Pydantic models, UUIDs, datetimes)
        so it serialises identically on the MCP ``error.data`` member and
        in a REST ``HTTPException.detail`` body.
        """
        return {
            "detail": ANSWER_ERROR_DETAIL,
            "leg": self.leg,
            "cause": self.cause,
            "message": str(self),
        }


def classify_answer_error(
    exc: Exception,
    *,
    llm_unavailable_leg: str = LEG_MODEL,
) -> AskDocsAnswerError | None:
    """Map a raised ``ask_docs`` leg exception onto a structured envelope.

    The single source of truth both the MCP handler and the forthcoming
    REST ``ask_docs`` route (#1917) call. Returns an
    :class:`AskDocsAnswerError` naming the failed leg + sub-cause for any
    of the four typed leg failures, or ``None`` for an exception that is
    **not** an answer-pipeline leg — letting the caller fall through to its
    generic catch (a true unexpected fault stays a plain ``-32603`` /
    500, not a mis-attributed leg).

    ``llm_unavailable_leg`` disambiguates the one type the exception class
    alone cannot place: a bare :class:`LlmClientUnavailable` is raised by
    the **same** #1386 client whether the expand leg or the synthesis leg
    reached it, so only the *caller* (which knows the pipeline position)
    can say which leg failed. The caller catches the expand leg with
    ``llm_unavailable_leg=LEG_EXPAND`` and the synthesis leg with the
    default (:data:`LEG_MODEL`). The leg's own typed shapes
    (:class:`DocsQueryExpansionError`, :class:`DocsSynthesisError`) are
    checked first and are never affected by this hint.
    """
    if isinstance(exc, DocsQueryExpansionError):
        # The expand model ran but produced unusable output (non-JSON /
        # wrong shape). Distinct from the no-model case below.
        return AskDocsAnswerError(
            leg=LEG_EXPAND,
            cause=CAUSE_EXPANSION_INVALID,
            message=f"expand leg failed: {exc}",
        )
    if isinstance(exc, CorpusUnavailable):
        # The retrieval backend is unconfigured / unreachable / non-2xx.
        # The transport never attaches the response body, so the message
        # is safe to surface verbatim.
        return AskDocsAnswerError(
            leg=LEG_CORPUS,
            cause=CAUSE_CORPUS_UNAVAILABLE,
            message=f"corpus leg failed: {exc}",
        )
    if isinstance(exc, DocsSynthesisError):
        # The synthesis model responded but its output broke the grounding
        # contract. ``exc.cause`` carries the #1918 sub-cause split
        # (parse vs citation-resolution) verbatim.
        return AskDocsAnswerError(
            leg=LEG_SYNTHESIS,
            cause=exc.cause,
            message=f"synthesis leg failed: {exc}",
        )
    if isinstance(exc, LlmClientUnavailable):
        # No model configured (no / non-Anthropic key). The caller's
        # ``llm_unavailable_leg`` names which leg reached the client; the
        # default is the synthesis (model) leg.
        leg_label = "expand" if llm_unavailable_leg == LEG_EXPAND else "model"
        return AskDocsAnswerError(
            leg=llm_unavailable_leg,
            cause=CAUSE_CLIENT_UNAVAILABLE,
            message=f"{leg_label} leg failed: {exc}",
        )
    return None
