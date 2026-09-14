# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Registry conformance for JSONFlux verdict preservation (#3425)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.connectors.registry import _eager_import_connectors
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.settings import get_settings

# A result schema qualifies when it explicitly describes both a top-level
# collection and a complete caller-visible outcome bundle.  One ``status`` is
# not enough: it can be an opaque vendor state, while two outcome fields form
# a verdict a caller must retain beside the spilled detail.
_VERDICT_FIELD_NAMES = frozenset(
    {
        "handshake",
        "reachable",
        "reason",
        "success",
        "ok",
        "healthy",
        "status",
        "state",
        "resultStatus",
        "executionStatus",
    }
)


def _schema_type_includes(schema: dict[str, Any], expected: str) -> bool:
    value = schema.get("type")
    return value == expected or (isinstance(value, list) and expected in value)


def _required_result_scalars(schema: dict[str, Any] | None) -> set[str]:
    """Return outcome fields that must survive an explicitly shaped spill.

    Schemas using only ``additionalProperties`` do not describe enough shape
    for this static conformance rule.  Connector authors can still opt into
    ``result_scalars`` for those dynamic vendor payloads.
    """
    if not isinstance(schema, dict) or not _schema_type_includes(schema, "object"):
        return set()
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return set()
    has_collection = any(
        isinstance(definition, dict) and _schema_type_includes(definition, "array")
        for definition in properties.values()
    )
    if not has_collection:
        return set()
    verdict_fields = set(properties).intersection(_VERDICT_FIELD_NAMES)
    return verdict_fields if len(verdict_fields) >= 2 else set()


def _missing_verdict_scalar_hints(descriptors: list[EndpointDescriptor]) -> dict[str, list[str]]:
    """List qualifying typed descriptors missing declared outcome scalars."""
    missing: dict[str, list[str]] = {}
    for descriptor in descriptors:
        required = _required_result_scalars(descriptor.response_schema)
        if not required:
            continue
        instructions = descriptor.llm_instructions
        hint = instructions.get("result_scalars") if isinstance(instructions, dict) else None
        keys = hint.get("keys") if isinstance(hint, dict) else None
        declared = (
            {key for key in keys if isinstance(key, str)} if isinstance(keys, list) else set()
        )
        absent = sorted(required - declared)
        if absent:
            missing[descriptor.op_id] = absent
    return missing


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide the settings the real registrar path reads during the sweep."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_typed_verdict_plus_collection_descriptors_preserve_verdict_scalars(
    stub_embedding_service: AsyncMock,
) -> None:
    """Every registered typed result schema preserves its declared outcome."""
    _eager_import_connectors()
    await run_typed_op_registrars(embedding_service=stub_embedding_service)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        descriptors = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.source_kind == "typed")
                )
            )
            .scalars()
            .all()
        )

    assert descriptors, "typed registrar sweep produced no descriptors"
    missing = _missing_verdict_scalar_hints(descriptors)
    assert not missing, (
        "typed verdict-plus-collection descriptors must declare "
        "llm_instructions.result_scalars.keys; missing: "
        f"{missing}"
    )


def test_verdict_plus_collection_conformance_rejects_new_unhinted_descriptor() -> None:
    """A future typed registrar cannot add a qualifying shape without hints."""
    descriptor = type("Descriptor", (), {})()
    descriptor.op_id = "example.health.inspect"
    descriptor.response_schema = {
        "type": "object",
        "properties": {
            "reachable": {"type": "boolean"},
            "status": {"type": "string"},
            "reason": {"type": ["string", "null"]},
            "checks": {"type": "array", "items": {"type": "object"}},
        },
    }
    descriptor.llm_instructions = {}

    assert _missing_verdict_scalar_hints([descriptor]) == {
        "example.health.inspect": ["reachable", "reason", "status"]
    }
