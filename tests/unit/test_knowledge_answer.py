# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Unit tests for grounded knowledge answer generation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from meho_app.modules.knowledge.answer import (
    GroundedAnswer,
    GroundedCitation,
    INSUFFICIENT_CONTEXT_MESSAGE,
    generate_grounded_answer,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_grounded_answer_builds_context_and_sanitizes_citations() -> None:
    """The grounded answer helper should keep only valid, unique citations."""
    llm_result = GroundedAnswer(
        answer="  Supported answer.  ",
        citations=[
            GroundedCitation(chunk_index=1, quote=" Chunk one text "),
            GroundedCitation(chunk_index=8, quote="ignored"),
            GroundedCitation(chunk_index=1, quote="Chunk one text"),
            GroundedCitation(chunk_index=0, quote="hallucinated quote not in chunk"),
        ],
    )

    results = [
        {
            "text": "Chunk zero text",
            "filename": "guide.pdf",
            "heading_path": ["Overview"],
            "page_number": 1,
            "page_start": 1,
            "page_end": 1,
        },
        {
            "text": "Chunk one text",
            "filename": "guide.pdf",
            "heading_path": ["Operations", "Timeouts"],
            "page_number": 7,
            "page_start": 7,
            "page_end": 8,
            "source_chunk_index": 3,
        },
    ]

    with patch(
        "meho_app.modules.knowledge.answer.infer_structured",
        new=AsyncMock(return_value=llm_result),
    ) as infer_mock:
        answer = await generate_grounded_answer(
            query="How do timeouts work?",
            results=results,
        )

    assert answer.answer == "Supported answer."
    assert answer.citations == [GroundedCitation(chunk_index=1, quote="Chunk one text")]

    infer_mock.assert_awaited_once()
    prompt = infer_mock.await_args.args[0]
    assert "Question: How do timeouts work?" in prompt
    assert "[Chunk 0] (file: guide.pdf) (path: Overview) (pages: p.1)" in prompt
    assert "[Chunk 1] (file: guide.pdf) (path: Operations > Timeouts) (pages: p.7-8)" in prompt
    assert "(source chunk: 3)" in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_grounded_answer_falls_back_when_llm_answer_is_blank() -> None:
    """Blank LLM output should fall back to the insufficient-context message."""
    with patch(
        "meho_app.modules.knowledge.answer.infer_structured",
        new=AsyncMock(return_value=GroundedAnswer(answer="   ", citations=[])),
    ):
        answer = await generate_grounded_answer(
            query="Anything here?",
            results=[{"text": "Chunk text"}],
        )

    assert answer.answer == INSUFFICIENT_CONTEXT_MESSAGE
    assert answer.citations == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_grounded_answer_rejects_hallucinated_quotes() -> None:
    """Citations whose quote does not appear in the referenced chunk must be dropped."""
    llm_result = GroundedAnswer(
        answer="Answer with bad citations.",
        citations=[
            GroundedCitation(chunk_index=0, quote="this quote is fabricated"),
            GroundedCitation(chunk_index=0, quote="actual content"),
        ],
    )

    results = [{"text": "The actual content of chunk zero."}]

    with patch(
        "meho_app.modules.knowledge.answer.infer_structured",
        new=AsyncMock(return_value=llm_result),
    ):
        answer = await generate_grounded_answer(query="Q?", results=results)

    assert len(answer.citations) == 1
    assert answer.citations[0].quote == "actual content"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_grounded_answer_ignores_string_heading_path() -> None:
    """A bare string heading_path must not be split into characters."""
    llm_result = GroundedAnswer(answer="OK.", citations=[])
    results = [{"text": "Some text", "heading_path": "Not a list"}]

    with patch(
        "meho_app.modules.knowledge.answer.infer_structured",
        new=AsyncMock(return_value=llm_result),
    ) as infer_mock:
        await generate_grounded_answer(query="Q?", results=results)

    prompt = infer_mock.await_args.args[0]
    assert "N > o > t" not in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generate_grounded_answer_propagates_model_errors() -> None:
    """Model failures should propagate so the caller can handle them."""
    results = [{"text": "Some chunk text", "score": 0.74, "filename": "doc.pdf"}]

    with (
        patch(
            "meho_app.modules.knowledge.answer.infer_structured",
            new=AsyncMock(side_effect=RuntimeError("account_deactivated")),
        ),
        pytest.raises(RuntimeError, match="account_deactivated"),
    ):
        await generate_grounded_answer(query="What happened?", results=results)
