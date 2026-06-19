# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Corpus-aware query expansion for the ``ask_docs`` answer pipeline (#1916).

``ask_docs`` (the synthesis sibling of ``search_docs``) used to pass the
operator's question **verbatim** to retrieval, so a terse / acronym-heavy
question under-retrieved: "NSX maximums" never matched a chunk that spells
out "VMware NSX configuration maximums". This module adds the missing step
*before* retrieval — it rewrites the question into a small set of bounded
query variants, **grounded in the target collection's manifest** so the
rewrite happens in the corpus's own domain terms (vendor name, product
synonyms, expanded acronyms).

The answer pipeline then runs retrieval once **per variant** and
RRF-merges the per-variant chunk lists (reusing
:func:`~meho_backplane.docs_search.fanout.rrf_merge`) before synthesis, so
a chunk surfaced by several variants is rank-boosted and a chunk found by
only one variant still reaches the answer. Expansion is the
**answer-pipeline's** job only: ``search_docs`` (the raw-chunks agent path)
is deliberately untouched (#1916 AC4).

Corpus-awareness without new data
=================================

The "manifest" is just the ``doc_collections`` row the caller already
resolved (:class:`~meho_backplane.docs_collections.DocCollection`): its
``vendor`` / ``products`` / ``description`` / ``when_to_use`` fields are
injected into the expansion prompt. No new table, no schema change — the
corpus-awareness data already exists (#1912), it was simply never put in
front of a model.

Fail-closed posture (mirrors synthesis)
=======================================

Expansion reuses the **same** #1386 fail-closed Anthropic Messages client
that synthesis uses (:func:`~meho_backplane.operations.ingest.build_anthropic_ingest_llm_client`),
via the shared ``generate_json`` seam. The fail-closed posture matches
synthesis exactly:

* **No client configured** — :class:`~meho_backplane.operations.ingest.LlmClientUnavailable`
  propagates **uncaught**. The answer pipeline never degrades to retrieving
  on the raw question alone and returning an answer that silently skipped
  expansion — a missing model is a 503-analogue, not a soft fallback.
* **Model ran but produced unusable output** (non-JSON, wrong shape, no
  usable variant) — :class:`DocsQueryExpansionError`. Distinct from the
  synthesis failure (:class:`~meho_backplane.docs_search.DocsSynthesisError`)
  and from :class:`LlmClientUnavailable` so a later structured-error
  envelope (#1918, the ``expand_failed`` leg) can map this one cleanly
  rather than burying it in a generic catch-all.

Substrate stays dumb: this module only frames the manifest + question into
a prompt and validates the returned variants. There is no DSL, no
per-collection weighting, no tunable knob (#1177 / #1178) — the LLM does
the expansion, the bound on the variant count is a fixed constant.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meho_backplane.operations.ingest import LlmClient, build_anthropic_ingest_llm_client

if TYPE_CHECKING:
    from meho_backplane.docs_collections import DocCollection

__all__ = [
    "MAX_QUERY_VARIANTS",
    "DocsQueryExpansionError",
    "expand_docs_query",
]

_log = structlog.get_logger(__name__)

#: Hard cap on the number of query variants retrieval fans out over,
#: **including** the operator's original question. Kept small on purpose
#: (#1916: "keep N small"): each variant is an independent backend
#: round-trip, and RRF's recall benefit flattens quickly past a handful of
#: well-chosen rewrites. The original question is always one of the
#: variants, so expansion can only *widen* recall, never drop the literal
#: query — at worst (a useless model) the pipeline retrieves on the
#: original alone.
MAX_QUERY_VARIANTS: Final[int] = 4

#: The number of *additional* rewrites the model is asked to produce
#: (the original question occupies one of the :data:`MAX_QUERY_VARIANTS`
#: slots). Derived so the two constants cannot drift.
_MAX_REWRITES: Final[int] = MAX_QUERY_VARIANTS - 1

#: Output-token ceiling for the expansion call. A handful of short query
#: strings is tiny; this bounds cost / latency without truncating a normal
#: variant set. Sized like the synthesis ceiling rather than invented.
_EXPANSION_MAX_OUTPUT_TOKENS: Final[int] = 512

_EXPANSION_SYSTEM_PROMPT: Final[str] = (
    "You are a vendor-documentation retrieval query expander. Given an "
    "operator's question and a short manifest describing the documentation "
    "collection it will be answered from, you rewrite the question into a "
    "few alternative search queries that improve retrieval recall against "
    "that collection.\n"
    "\n"
    "Use the manifest to expand in the collection's own domain terms: spell "
    "out acronyms, add vendor and product synonyms, and phrase variants the "
    "way the documentation would. Stay on the operator's actual information "
    "need — do not broaden into a different topic, and do not invent product "
    "or version facts that are not implied by the question or the manifest.\n"
    "\n"
    "Rules:\n"
    f"1. Produce at most {_MAX_REWRITES} alternative queries (fewer is fine). "
    "Do NOT repeat the operator's original question — it is always searched "
    "separately.\n"
    "2. Each query must be a self-contained search string, not a question to "
    "the operator and not prose.\n"
    "3. Return ONLY a JSON object, no prose around it, with exactly one key: "
    '"queries" (an array of the alternative query strings). Return an empty '
    "array if no useful rewrite exists."
)


class _ExpansionOutput(BaseModel):
    """The strict JSON contract the expansion model must return.

    ``extra="forbid"`` rejects a model that pads the object with stray keys,
    so a drifting output shape fails validation (→
    :class:`DocsQueryExpansionError`) rather than being silently accepted.
    ``queries`` defaults to empty so a model that legitimately finds no
    useful rewrite is valid (the pipeline then retrieves on the original
    question alone).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    queries: list[str] = Field(default_factory=list)


class DocsQueryExpansionError(RuntimeError):
    """Raised when expansion ran but produced unusable output.

    Covers a model that returned non-JSON or an object failing the strict
    :class:`_ExpansionOutput` shape. The MCP dispatcher surfaces this as
    JSON-RPC ``-32603`` — an expansion fault, not invalid client params:
    the request was well-formed, the model's output was not.

    Distinct from :class:`~meho_backplane.docs_search.DocsSynthesisError`
    (the synthesis-leg failure) and from
    :class:`~meho_backplane.operations.ingest.LlmClientUnavailable` (no
    model configured) so a later structured answer-error envelope (#1918)
    can attribute the failure to the ``expand`` leg specifically rather
    than a generic catch-all. The fail-closed contract is shared: an
    unusable expansion never degrades to an ungrounded / un-expanded answer.
    """


def _render_manifest_for_prompt(collection: DocCollection) -> str:
    """Render the collection's manifest fields as a labelled prompt block.

    Only the fields that carry retrieval-useful domain signal are framed —
    ``vendor`` / ``products`` (acronym + synonym source) and the optional
    ``description`` / ``when_to_use`` prose. Empty optional fields are
    omitted so the prompt never carries a bare ``description: None`` line
    the model would have to reason past. ``collection_key`` anchors the
    block to a concrete corpus.
    """
    lines: list[str] = [
        f"collection: {collection.collection_key}",
        f"vendor: {collection.vendor}",
    ]
    if collection.products:
        lines.append(f"products: {', '.join(collection.products)}")
    if collection.description:
        lines.append(f"description: {collection.description}")
    if collection.when_to_use:
        lines.append(f"when_to_use: {collection.when_to_use}")
    return "\n".join(lines)


def _parse_expansion_output(raw: str) -> _ExpansionOutput:
    """Parse + validate the model's raw text into the strict output shape.

    A model that returns non-JSON or a shape-violating object raises
    :class:`DocsQueryExpansionError` — an unparseable expansion cannot be
    trusted, so we fail closed rather than retrieve on a malformed variant
    set.
    """
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DocsQueryExpansionError(
            "expansion model returned non-JSON output; cannot derive query variants"
        ) from exc
    try:
        return _ExpansionOutput.model_validate(decoded)
    except ValidationError as exc:
        raise DocsQueryExpansionError(
            'expansion model output did not match the required {"queries": [...]} shape'
        ) from exc


def _dedupe_variants(query: str, rewrites: list[str]) -> list[str]:
    """Build the bounded, deduplicated variant list, original query first.

    The operator's original ``query`` is always the first variant (so
    expansion can only widen recall). Blank rewrites are dropped, and a
    rewrite that only re-casts the original (case-insensitive, whitespace-
    collapsed match against any kept variant) is skipped so it cannot waste
    a backend round-trip on a duplicate query. The result is capped at
    :data:`MAX_QUERY_VARIANTS`.
    """

    def _norm(text: str) -> str:
        return " ".join(text.split()).casefold()

    variants: list[str] = [query]
    seen: set[str] = {_norm(query)}
    for rewrite in rewrites:
        stripped = rewrite.strip()
        if not stripped:
            continue
        key = _norm(stripped)
        if key in seen:
            continue
        seen.add(key)
        variants.append(stripped)
        if len(variants) >= MAX_QUERY_VARIANTS:
            break
    return variants


async def expand_docs_query(
    query: str,
    collection: DocCollection,
    *,
    llm_client: LlmClient | None = None,
) -> list[str]:
    """Expand *query* into ≤:data:`MAX_QUERY_VARIANTS` corpus-aware variants.

    Runs *before* the answer pipeline's retrieval. The returned list always
    leads with the operator's original ``query`` and adds model-proposed
    rewrites grounded in *collection*'s manifest (``vendor`` / ``products``
    / ``description`` / ``when_to_use``), so the rewrites use the corpus's
    own domain terms (expanded acronyms, product synonyms). The caller runs
    retrieval once per returned variant and RRF-merges the chunk lists.

    Args:
        query: The operator's free-text question (passed to the model;
            never logged here — the dispatcher hashes it for the audit row).
        collection: The resolved doc collection whose manifest fields
            ground the expansion in domain terms.
        llm_client: Expansion client; defaults to the same fail-closed
            Anthropic Messages adapter synthesis uses (#1386). Injectable
            so tests pin a deterministic stub.

    Returns:
        A non-empty list of query variants, original first, deduplicated,
        capped at :data:`MAX_QUERY_VARIANTS`. A model that proposes no
        usable rewrite yields ``[query]`` — retrieval on the original
        question alone.

    Raises:
        LlmClientUnavailable: when no expansion model is configured
            (propagated from the default factory). The MCP dispatcher maps
            it to ``-32603`` (the analogue of the route's 503). Never caught
            here — a missing model fails closed, matching synthesis; the
            pipeline does not silently skip expansion.
        DocsQueryExpansionError: when the model ran but produced non-JSON or
            a shape-violating object. Also surfaces as ``-32603``, and is
            distinguishable for the #1918 ``expand_failed`` leg.
    """
    client = llm_client if llm_client is not None else build_anthropic_ingest_llm_client()

    user_prompt = (
        f"Collection manifest:\n{_render_manifest_for_prompt(collection)}\n\n"
        f"Operator question:\n{query}"
    )
    raw = await client.generate_json(
        system_prompt=_EXPANSION_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_output_tokens=_EXPANSION_MAX_OUTPUT_TOKENS,
    )

    output = _parse_expansion_output(raw)
    variants = _dedupe_variants(query, output.queries)
    _log.info(
        "docs_ask_query_expanded",
        collection_key=collection.collection_key,
        variant_count=len(variants),
    )
    return variants
