# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for generate_document_summary and build_chunk_prefix.

Tests cover the async LLM summary generator (success, timeout, exception,
input truncation) and the pure-function chunk prefix builder (all input
combinations).

Docling modules are injected as fakes since docling is not installed in
the test env. The pydantic-ai Agent is mocked to avoid real LLM calls.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Inject fake docling modules so document_converter.py can be imported.
# ---------------------------------------------------------------------------

_DOCLING_MODULES = [
    "docling",
    "docling.chunking",
    "docling.datamodel",
    "docling.datamodel.base_models",
    "docling.datamodel.document",
    "docling.document_converter",
    "docling_core",
    "docling_core.types",
    "docling_core.types.doc",
    "docling_core.types.doc.labels",
]

for _mod_name in _DOCLING_MODULES:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

# Now import the functions under test
from meho_app.modules.knowledge.document_converter import (
    build_chunk_prefix,
    generate_document_summary,
)


# ---------------------------------------------------------------------------
# Tests: generate_document_summary
# ---------------------------------------------------------------------------

@pytest.mark.unit
@patch("meho_app.core.config.get_config")
@patch("pydantic_ai.Agent")
async def test_generate_summary_success(mock_agent_cls, mock_get_config):
    """Summary generation returns trimmed LLM output on success."""
    mock_get_config.return_value.classifier_model = "anthropic:claude-sonnet-4-6"

    mock_result = MagicMock()
    mock_result.output = "  A runbook for K8s.  "
    mock_agent = mock_agent_cls.return_value
    mock_agent.run = AsyncMock(return_value=mock_result)

    summary = await generate_document_summary("Document text", "kubernetes", "prod")

    assert summary == "A runbook for K8s."
    mock_agent.run.assert_called_once()


@pytest.mark.unit
@patch("meho_app.core.config.get_config")
@patch("pydantic_ai.Agent")
async def test_generate_summary_timeout(mock_agent_cls, mock_get_config):
    """Summary generation returns empty string on timeout."""
    mock_get_config.return_value.classifier_model = "anthropic:claude-sonnet-4-6"

    mock_agent = mock_agent_cls.return_value
    mock_agent.run = AsyncMock(side_effect=asyncio.TimeoutError())

    summary = await generate_document_summary("Some text", "vmware", "dc1")

    assert summary == ""


@pytest.mark.unit
@patch("meho_app.core.config.get_config")
@patch("pydantic_ai.Agent")
async def test_generate_summary_exception(mock_agent_cls, mock_get_config):
    """Summary generation returns empty string on any exception."""
    mock_get_config.return_value.classifier_model = "anthropic:claude-sonnet-4-6"

    mock_agent = mock_agent_cls.return_value
    mock_agent.run = AsyncMock(side_effect=RuntimeError("LLM down"))

    summary = await generate_document_summary("Some text")

    assert summary == ""


@pytest.mark.unit
@patch("meho_app.core.config.get_config")
@patch("pydantic_ai.Agent")
async def test_generate_summary_truncates_input(mock_agent_cls, mock_get_config):
    """Input text is truncated to 16000 chars before passing to LLM."""
    mock_get_config.return_value.classifier_model = "anthropic:claude-sonnet-4-6"

    mock_result = MagicMock()
    mock_result.output = "Summary of long doc."
    mock_agent = mock_agent_cls.return_value
    mock_agent.run = AsyncMock(return_value=mock_result)

    long_text = "x" * 20000
    await generate_document_summary(long_text, "kubernetes", "prod")

    # The agent.run should have been called with text[:16000]
    call_args = mock_agent.run.call_args
    user_prompt = call_args[0][0]
    assert len(user_prompt) == 16000


@pytest.mark.unit
@patch("meho_app.core.config.get_config")
@patch("pydantic_ai.Agent")
async def test_generate_summary_includes_system_prompt(mock_agent_cls, mock_get_config):
    """Agent is constructed with a system_prompt containing 'Summarize'."""
    mock_get_config.return_value.classifier_model = "anthropic:claude-sonnet-4-6"

    mock_result = MagicMock()
    mock_result.output = "A summary."
    mock_agent = mock_agent_cls.return_value
    mock_agent.run = AsyncMock(return_value=mock_result)

    await generate_document_summary("Doc text")

    # Verify Agent was constructed with model and system_prompt
    agent_init_args = mock_agent_cls.call_args
    assert agent_init_args is not None
    # system_prompt should mention "Summarize"
    system_prompt = agent_init_args[1].get("system_prompt", agent_init_args[0][1] if len(agent_init_args[0]) > 1 else "")
    assert "Summarize" in system_prompt or "ummari" in str(system_prompt)


# ---------------------------------------------------------------------------
# Tests: build_chunk_prefix
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_build_chunk_prefix_full():
    """Full args: type + name + summary."""
    result = build_chunk_prefix("kubernetes", "prod-eu", "Runbook for K8s.")
    assert result == "kubernetes connector (prod-eu). Runbook for K8s."


@pytest.mark.unit
def test_build_chunk_prefix_type_and_name():
    """Type and name, no summary."""
    result = build_chunk_prefix("vmware", "dc-west")
    assert result == "vmware connector (dc-west)."


@pytest.mark.unit
def test_build_chunk_prefix_type_only():
    """Type only, no name, no summary."""
    result = build_chunk_prefix("kubernetes")
    assert result == "kubernetes connector."


@pytest.mark.unit
def test_build_chunk_prefix_summary_only():
    """Summary only, no type or name."""
    result = build_chunk_prefix(document_summary="A summary.")
    assert result == "A summary."


@pytest.mark.unit
def test_build_chunk_prefix_empty():
    """No args returns empty string."""
    result = build_chunk_prefix()
    assert result == ""
