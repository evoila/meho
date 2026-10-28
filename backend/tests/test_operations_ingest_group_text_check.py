# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Group texts written at ingest make no safety claim their ops break (#4971).

Pass 1 of the grouping run sees only ``op_id`` / ``summary`` / ``tags``,
so the model can write "Write operations are approval-required." for a
group whose writes run at once. After Pass 2 files the ops into the new
groups, the run removes each sentence whose claim the group's ops break.
Texts of groups that already exist, including texts an operator edited,
are never changed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationGroup
from meho_backplane.operations.ingest import register_ingested_operations, run_llm_grouping
from meho_backplane.operations.ingest.group_truth import drop_unsupported_claims
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.settings import get_settings

_PRODUCT = "acme"
_VERSION = "1.0"
_IMPL = "acme-rest"
_OPERATOR_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c4c4")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_connector_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


class _StubLlm:
    """Return canned responses in order."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def generate_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> str:
        if not self._responses:
            raise AssertionError("stub LLM ran out of responses")
        return self._responses.pop(0)


def _proto(
    method: str, path: str, summary: str, safety_level: Any, requires_approval: bool
) -> EndpointDescriptorProto:
    return EndpointDescriptorProto(
        op_id=f"{method}:{path}",
        method=method,
        path=path,
        summary=summary,
        tags=["Widget"],
        parameter_schema={"type": "object", "properties": {}},
        safety_level=safety_level,
        requires_approval=requires_approval,
    )


_LIST = _proto("GET", "/widgets", "List widgets", "safe", False)
_GET = _proto("GET", "/widgets/{id}", "Get a widget", "safe", False)
_CREATE = _proto("POST", "/widgets", "Create a widget", "caution", False)
_STOP = _proto("POST", "/widgets/{id}/stop", "Stop a widget", "caution", False)
_DELETE = _proto("DELETE", "/widgets/{id}", "Delete a widget", "dangerous", True)
_CREATE_GATED = _proto("POST", "/gadgets", "Create a gadget", "caution", True)
_DELETE_GATED = _proto("DELETE", "/gadgets/{id}", "Delete a gadget", "dangerous", True)


def _embedding_service() -> Any:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _register(protos: list[EndpointDescriptorProto]) -> None:
    await register_ingested_operations(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL,
        spec_source="acme.yaml",
        operations=protos,
        embedding_service=_embedding_service(),
    )


async def _run(responses: list[str]) -> Any:
    return await run_llm_grouping(
        llm_client=_StubLlm(responses),
        operator_sub="op-test-group-text",
        operator_tenant_id=_OPERATOR_TENANT,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL,
    )


async def _texts() -> dict[str, str]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(OperationGroup))).scalars().all()
        return {row.group_key: row.when_to_use for row in rows}


def _group(key: str, when_to_use: str) -> dict[str, str]:
    return {"group_key": key, "name": key.replace("_", " ").title(), "when_to_use": when_to_use}


@pytest.mark.asyncio
async def test_false_approval_claim_is_removed_from_a_new_group() -> None:
    """Writes that run at once: the "approval-required" sentence is not stored."""
    await _register([_LIST, _CREATE, _STOP, _DELETE])
    propose = json.dumps(
        [
            _group(
                "widgets",
                "Use to list, create, stop, and delete widgets. "
                "Write operations are approval-required.",
            )
        ]
    )
    assign = json.dumps({op.op_id: "widgets" for op in (_LIST, _CREATE, _STOP, _DELETE)})

    await _run([propose, assign])

    text = (await _texts())["widgets"]
    assert text == "Use to list, create, stop, and delete widgets."
    assert "approval" not in text.lower()


@pytest.mark.asyncio
async def test_false_read_only_claim_is_removed_from_a_new_group() -> None:
    """A group with one ``caution`` write: the "Read-only." sentence is not stored."""
    await _register([_LIST, _GET, _CREATE])
    propose = json.dumps([_group("widgets", "Use to browse widgets. Read-only.")])
    assign = json.dumps({op.op_id: "widgets" for op in (_LIST, _GET, _CREATE)})

    await _run([propose, assign])

    text = (await _texts())["widgets"]
    assert text == "Use to browse widgets."
    assert "read-only" not in text.lower()


@pytest.mark.asyncio
async def test_true_approval_claim_stays() -> None:
    """Every change in the group needs approval, so the text may say so."""
    await _register([_LIST, _CREATE_GATED, _DELETE_GATED])
    text = "Use to create and delete gadgets. Write operations are approval-required."
    propose = json.dumps([_group("gadgets", text)])
    assign = json.dumps({op.op_id: "gadgets" for op in (_LIST, _CREATE_GATED, _DELETE_GATED)})

    await _run([propose, assign])

    assert (await _texts())["gadgets"] == text


@pytest.mark.asyncio
async def test_text_with_only_broken_claims_gets_a_plain_text() -> None:
    """When no sentence is left, the group gets a plain text with no claim."""
    await _register([_CREATE, _STOP])
    propose = json.dumps([_group("widget_power", "Writes are dangerous / approval-required.")])
    assign = json.dumps({op.op_id: "widget_power" for op in (_CREATE, _STOP)})

    await _run([propose, assign])

    assert (await _texts())["widget_power"] == "Operations for Widget Power."


@pytest.mark.asyncio
async def test_operator_edited_text_is_not_changed_by_a_new_ingest() -> None:
    """A later ingest files new ops into an edited group and keeps the edited text."""
    await _register([_LIST, _GET])
    propose = json.dumps([_group("widgets", "Use to browse widgets.")])
    assign = json.dumps({op.op_id: "widgets" for op in (_LIST, _GET)})
    await _run([propose, assign])

    edited = "Widget work. Write operations are approval-required."
    async with get_sessionmaker()() as session:
        group = (
            await session.execute(
                select(OperationGroup).where(OperationGroup.group_key == "widgets")
            )
        ).scalar_one()
        # The same write ``meho connector edit-group --when-to-use`` makes.
        group.when_to_use = edited
        await session.commit()

    await _register([_LIST, _GET, _CREATE, _STOP])
    result = await _run([json.dumps({_CREATE.op_id: "widgets", _STOP.op_id: "widgets"})])

    assert result.groups_created == 0
    assert result.operations_assigned == 2
    assert (await _texts())["widgets"] == edited


def _ops(*levels: tuple[str, bool]) -> list[SimpleNamespace]:
    return [SimpleNamespace(safety_level=lvl, requires_approval=appr) for lvl, appr in levels]


def test_dangerous_claim_is_removed_when_a_change_is_caution() -> None:
    ops = _ops(("safe", False), ("caution", False), ("dangerous", True))
    text = "Use to stop services. Service stop operations are dangerous."
    assert drop_unsupported_claims(text, ops) == "Use to stop services."


def test_dangerous_claim_stays_when_every_change_is_dangerous_or_higher() -> None:
    ops = _ops(("safe", False), ("dangerous", True), ("destructive", True))
    text = "Use to remove widgets. Delete operations are dangerous."
    assert drop_unsupported_claims(text, ops) == text


def test_text_stays_when_the_group_has_no_ops() -> None:
    text = "Write operations are approval-required."
    assert drop_unsupported_claims(text, []) == text


def test_negative_approval_sentence_is_not_a_claim() -> None:
    ops = _ops(("caution", False))
    text = "Use to create widgets. These writes do not need approval. No approval required."
    assert drop_unsupported_claims(text, ops) == text
