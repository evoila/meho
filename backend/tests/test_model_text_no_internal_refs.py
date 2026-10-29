# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Text that MEHO sends to a model holds no issue numbers or planning codes.

A model cannot open an issue link, and a code like ``G0.18-T5`` means
nothing to it. Such codes only cost room in the model's context. This test
checks two kinds of text a model reads:

* every MCP tool: its description and the field descriptions of its input
  and output schemas (what ``ToolDefinition.to_wire()`` sends), and
* every operation group that MEHO registers at start-up: its name and its
  ``when_to_use`` text (what ``list_operation_groups`` returns).

The pattern is the one the public docs generator uses to strip these
codes, so the two cannot drift apart.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from scripts.generate_reference_docs import _INTERNAL_REF
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationGroup
from meho_backplane.mcp.registry import _TOOLS, eager_import_mcp_modules
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.settings import get_settings

_PATTERN = re.compile(_INTERNAL_REF)


def test_pattern_finds_codes_and_skips_ticket_references() -> None:
    """The pattern finds issue numbers and planning codes, not ``repo#7``."""
    for text in ("(#3197)", "G0.18-T5", "G11.2", "§6", "pre-#2471"):
        assert _PATTERN.search(text), text
    for text in ("gh:evoila/meho#7", "SHA-256", "PG16", "a 4 GB limit"):
        assert not _PATTERN.search(text), text


def test_tool_texts_hold_no_internal_refs() -> None:
    """No tool description or schema text holds an issue number or code."""
    eager_import_mcp_modules()
    assert _TOOLS, "no MCP tool was registered"
    found = {}
    for name, (definition, _handler) in sorted(_TOOLS.items()):
        hits = sorted(set(_PATTERN.findall(json.dumps(definition.to_wire(), ensure_ascii=False))))
        if hits:
            found[name] = hits
    assert not found, (
        "tool texts hold issue numbers or planning codes; say the meaning in "
        f"plain words instead: {found}"
    )


@pytest.fixture
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set the env vars :class:`Settings` needs (the registrars read it)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.mark.asyncio
@pytest.mark.usefixtures("_required_settings_env")
async def test_operation_group_texts_hold_no_internal_refs() -> None:
    """No operation group registered in code holds an issue number or code."""
    await run_typed_op_registrars(embedding_service=_stub_embedding_service())
    async with get_sessionmaker()() as session:
        built_in = select(OperationGroup).where(OperationGroup.tenant_id.is_(None))
        groups = (await session.execute(built_in)).scalars().all()
    assert groups, "no operation group was registered"
    found = {}
    for group in groups:
        hits = sorted(set(_PATTERN.findall(f"{group.name}\n{group.when_to_use}")))
        if hits:
            found[f"{group.impl_id} {group.version} {group.group_key}"] = hits
    assert not found, (
        "operation group texts hold issue numbers or planning codes; say the "
        f"meaning in plain words instead: {dict(sorted(found.items()))}"
    )
