# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Grounded, cited answer synthesis over retrieved docs chunks (G4.5-T7 #1526).

``ask_docs`` is the synthesis fast-follow to ``search_docs`` (T4, #1523):
``search_docs`` returns the ranked cited chunks; ``ask_docs`` composes a
single grounded answer **over those same chunks** and returns it alongside
the chunks it actually cited. This module owns the synthesis step only —
retrieval stays in :mod:`meho_backplane.docs_search.service` (T3, #1521),
so the REQUIRE_FILTERS posture, corpus federation, and citation shape are
never re-derived here.

Three load-bearing invariants, each an acceptance criterion:

* **No claim without a citation.** The model is constrained to answer
  *only* from the supplied chunks and to name the chunk ids it relied on;
  every returned citation resolves to a chunk the T3 retrieval returned.
  A model that cites an id not in the retrieved set is treated as a
  synthesis failure (:class:`DocsSynthesisError`), not silently dropped —
  an unverifiable citation is worse than none.

* **Zero retrieved chunks → no grounded answer, never a hallucinated one.**
  When retrieval returns nothing there is nothing to ground on, so the
  synthesis short-circuits to :data:`NO_GROUNDED_ANSWER` *without calling
  the model at all*. The empty-evidence path cannot invent an answer
  because the model is never asked.

* **Synthesis model unconfigured / unreachable → fail-closed.** The
  synthesis client is the same Anthropic Messages adapter the spec-
  ingestion grouping pass uses (#1386): no ``ANTHROPIC_API_KEY`` raises
  :class:`~meho_backplane.operations.ingest.LlmClientUnavailable`, which
  the MCP dispatcher surfaces as JSON-RPC ``-32603`` (the analogue of the
  route's 503). We never degrade to an ungrounded answer when the model
  is missing — a fail-closed 503 is the correct posture for an add-on
  whose whole value is grounded, cited reference.

Why structured JSON rather than parsed inline prose
===================================================

The model returns a small JSON object — ``{"answer": str,
"cited_chunk_ids": [str, ...]}`` — rather than prose with ``[1]``-style
inline markers we'd have to parse back out. Structured output makes the
"no claim without a citation" rule *machine-enforceable*: we validate the
cited ids against the retrieved set before trusting the answer, instead of
regex-scraping citation markers from free text (brittle, and silently
wrong when the model's marker scheme drifts). The ``generate_json`` seam
is the same one the grouping pass uses, so the synthesis client needs no
new method.
"""

from __future__ import annotations

import json
from typing import Final

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meho_backplane.docs_search.service import DocsChunk, DocsSearchResult
from meho_backplane.operations.ingest import LlmClient, build_anthropic_ingest_llm_client

__all__ = [
    "NO_GROUNDED_ANSWER",
    "SYNTHESIS_CAUSE_CITATION_RESOLUTION",
    "SYNTHESIS_CAUSE_PARSE",
    "DocsAnswer",
    "DocsSynthesisError",
    "synthesize_docs_answer",
]

_log = structlog.get_logger(__name__)

#: The deterministic answer returned when retrieval found no chunks to
#: ground on. Returned *without* calling the model — the empty-evidence
#: path is the one place a synthesis answer is produced with no LLM call,
#: precisely so it cannot hallucinate. ``citations`` is empty alongside it.
NO_GROUNDED_ANSWER: Final[str] = (
    "No grounded answer: the vendor-document corpus returned no chunks "
    "matching this query within the given product/version scope."
)

#: Output-token ceiling for the synthesis call. A cited answer over a
#: handful of chunks is short prose plus an id list; this bounds cost and
#: latency without truncating a normal answer. Sized to the grouping
#: pass's per-call ceilings rather than invented fresh.
_SYNTHESIS_MAX_OUTPUT_TOKENS: Final[int] = 1024

_SYNTHESIS_SYSTEM_PROMPT: Final[str] = (
    "You are a vendor-documentation answering assistant. You answer "
    "STRICTLY and ONLY from the numbered documentation chunks provided in "
    "the user message. You never use outside knowledge, never guess, and "
    "never state a fact that is not supported by at least one provided "
    "chunk.\n"
    "\n"
    "Rules:\n"
    "1. Ground every claim in the provided chunks. If the chunks do not "
    "contain enough to answer, say so plainly in the answer rather than "
    "filling the gap from memory.\n"
    "2. List the chunk_id of every chunk you actually relied on in "
    "cited_chunk_ids. Do not cite a chunk you did not use, and do not "
    "invent a chunk_id that is not in the provided set.\n"
    "3. Return ONLY a JSON object, no prose around it, with exactly two "
    'keys: "answer" (a string) and "cited_chunk_ids" (an array of the '
    "chunk_id strings you used). Cite at least one chunk whenever you "
    "make any factual claim."
)


class _SynthesisOutput(BaseModel):
    """The strict JSON contract the synthesis model must return.

    ``extra="forbid"`` rejects a model that pads the object with stray
    keys, so a drifting output shape fails validation (→
    :class:`DocsSynthesisError`) rather than being silently accepted with
    an unverifiable citation set.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer: str = Field(min_length=1)
    cited_chunk_ids: list[str] = Field(default_factory=list)


class DocsAnswer(BaseModel):
    """A synthesized, grounded answer plus the chunks it cited.

    ``citations`` is a subset of the chunks T3 retrieval returned — the
    ones the model relied on — preserving their retrieval order. Every
    entry is a full :class:`DocsChunk`, so a caller rendering the answer
    has the citation text + ``source_url`` without a second lookup. An
    answer with no grounding (empty retrieval) carries
    :data:`NO_GROUNDED_ANSWER` and an empty ``citations`` list.
    """

    model_config = ConfigDict(frozen=True)

    answer: str
    citations: list[DocsChunk] = Field(default_factory=list)


#: A model that responded but did not parse into the strict output shape
#: (non-JSON, or JSON failing :class:`_SynthesisOutput`). The output is
#: structurally unusable.
SYNTHESIS_CAUSE_PARSE: Final[str] = "parse"

#: A model whose output parsed but cited a ``chunk_id`` absent from the
#: retrieved set. The shape was fine; the grounding link was fabricated.
SYNTHESIS_CAUSE_CITATION_RESOLUTION: Final[str] = "citation_resolution"


class DocsSynthesisError(RuntimeError):
    """Raised when synthesis ran but produced an untrustworthy answer.

    Covers a model that returned non-JSON, an object failing the strict
    :class:`_SynthesisOutput` shape, or one citing a ``chunk_id`` absent
    from the retrieved set. All three mean the grounding contract was
    broken, so the answer is rejected rather than returned. The MCP
    dispatcher surfaces this as JSON-RPC ``-32603`` — a synthesis fault,
    not invalid client params: the request was well-formed, the model's
    output was not.

    ``cause`` splits the two structurally-distinct failure modes the
    string message previously buried (#1918): :data:`SYNTHESIS_CAUSE_PARSE`
    (output didn't parse into the required shape) vs.
    :data:`SYNTHESIS_CAUSE_CITATION_RESOLUTION` (output parsed but a cited
    id did not resolve to a retrieved chunk). A caller building a
    structured answer-error envelope (the ``synthesis_malformed`` leg in
    :mod:`meho_backplane.docs_search.answer_errors`) reads ``cause`` to
    name the sub-cause without re-parsing the message — an operator can
    then tell "the model emitted garbage JSON" apart from "the model
    cited a chunk that isn't in the corpus result", which point at
    different fixes (prompt / model vs. retrieval / index drift).
    """

    def __init__(self, message: str, *, cause: str) -> None:
        self.cause = cause
        super().__init__(message)


def _render_chunks_for_prompt(chunks: list[DocsChunk]) -> str:
    """Render retrieved chunks as a numbered, id-tagged evidence block.

    Each chunk is labelled with its ``chunk_id`` (the value the model must
    echo into ``cited_chunk_ids``) and its ``source_url`` so the model can
    attribute precisely. The content is passed verbatim — the corpus is
    the source of truth; this function only frames it.
    """
    parts: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        source = chunk.source_url or "(no source url)"
        parts.append(f"[{index}] chunk_id={chunk.chunk_id} source={source}\n{chunk.content}")
    return "\n\n".join(parts)


def _parse_synthesis_output(raw: str) -> _SynthesisOutput:
    """Parse + validate the model's raw text into the strict output shape.

    A model that returns non-JSON or a shape-violating object raises
    :class:`DocsSynthesisError` — the grounding contract cannot be checked
    against an unparseable answer, so we fail closed rather than return it.
    """
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DocsSynthesisError(
            "synthesis model returned non-JSON output; cannot verify citations",
            cause=SYNTHESIS_CAUSE_PARSE,
        ) from exc
    try:
        return _SynthesisOutput.model_validate(decoded)
    except ValidationError as exc:
        raise DocsSynthesisError(
            "synthesis model output did not match the required {answer, cited_chunk_ids} shape",
            cause=SYNTHESIS_CAUSE_PARSE,
        ) from exc


def _resolve_citations(
    cited_ids: list[str],
    chunks: list[DocsChunk],
) -> list[DocsChunk]:
    """Map model-cited ids back to retrieved chunks, rejecting unknown ids.

    Every cited id MUST resolve to a chunk the T3 retrieval returned. An
    id outside that set means the model invented (or hallucinated) a
    citation, which breaks the no-claim-without-a-real-citation contract —
    so we raise :class:`DocsSynthesisError` rather than drop the bad id and
    return a partially-verified answer. Order follows retrieval ranking
    (not the model's mention order), and a chunk cited twice is emitted
    once.
    """
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    unknown = [cid for cid in cited_ids if cid not in by_id]
    if unknown:
        raise DocsSynthesisError(
            f"synthesis cited chunk id(s) not in the retrieved set: {unknown}",
            cause=SYNTHESIS_CAUSE_CITATION_RESOLUTION,
        )
    cited = set(cited_ids)
    return [chunk for chunk in chunks if chunk.chunk_id in cited]


async def synthesize_docs_answer(
    query: str,
    retrieval: DocsSearchResult,
    *,
    llm_client: LlmClient | None = None,
) -> DocsAnswer:
    """Compose a grounded, cited answer over *retrieval*'s chunks.

    Runs *after* the shared T3 retrieval; never retrieves itself. The
    answer is grounded strictly in ``retrieval.chunks`` and the returned
    ``citations`` are exactly the subset the model relied on.

    Args:
        query: The operator's free-text question (passed to the model;
            never logged here — the dispatcher hashes it for the audit row).
        retrieval: The cited chunks from
            :func:`meho_backplane.docs_search.search_docs`.
        llm_client: Synthesis client; defaults to the same fail-closed
            Anthropic Messages adapter the spec-ingestion grouping pass
            uses (#1386). Injectable so tests pin a deterministic stub.

    Returns:
        A :class:`DocsAnswer`. With no retrieved chunks, the answer is
        :data:`NO_GROUNDED_ANSWER` and ``citations`` is empty — produced
        without calling the model.

    Raises:
        LlmClientUnavailable: when no synthesis model is configured
            (propagated from the default factory). The MCP dispatcher maps
            it to ``-32603`` (the analogue of the route's 503). Never
            caught here — a missing model must fail closed, not degrade to
            an ungrounded answer.
        DocsSynthesisError: when the model ran but produced non-JSON, a
            shape-violating object, or a citation outside the retrieved
            set. Also surfaces as ``-32603``.
    """
    chunks = list(retrieval.chunks)
    if not chunks:
        # Empty evidence: the only answer path that produces text without
        # calling the model, precisely so it cannot hallucinate.
        _log.info("docs_ask_no_grounding", hit_count=0)
        return DocsAnswer(answer=NO_GROUNDED_ANSWER, citations=[])

    client = llm_client if llm_client is not None else build_anthropic_ingest_llm_client()

    user_prompt = (
        f"Question:\n{query}\n\n"
        f"Documentation chunks (answer only from these):\n"
        f"{_render_chunks_for_prompt(chunks)}"
    )
    raw = await client.generate_json(
        system_prompt=_SYNTHESIS_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_output_tokens=_SYNTHESIS_MAX_OUTPUT_TOKENS,
    )

    output = _parse_synthesis_output(raw)
    citations = _resolve_citations(output.cited_chunk_ids, chunks)
    _log.info(
        "docs_ask_synthesized",
        hit_count=len(chunks),
        citation_count=len(citations),
    )
    return DocsAnswer(answer=output.answer, citations=citations)
