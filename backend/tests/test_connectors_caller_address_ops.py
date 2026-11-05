# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Connector operations whose target connects to an address the caller gives.

Each such field carries the caller-address marker, and the registered
operation is at least ``caution`` and waits for a person's approval.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from meho_backplane.connectors.winsrv import WinsrvConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import typed_register as tr_module
from meho_backplane.operations.caller_address import CALLER_ADDRESS_MARKER
from meho_backplane.settings import get_settings

_SAFETY_RANK = {"safe": 0, "caution": 1, "dangerous": 2, "destructive": 3}

#: (connector class, operation id, marked field)
_CASES = [
    (WinsrvConnector, "winsrv.storage.iscsi.connect", "target_portal_address"),
]


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(("connector", "op_id", "field"), _CASES)
async def test_registered_op_waits_for_approval(
    connector: type[WinsrvConnector], op_id: str, field: str
) -> None:
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await connector.register_operations()
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == op_id, EndpointDescriptor.tenant_id.is_(None)
                )
            )
        ).scalar_one()
    assert row.parameter_schema["properties"][field][CALLER_ADDRESS_MARKER] is True
    assert _SAFETY_RANK[row.safety_level] >= _SAFETY_RANK["caution"]
    assert row.requires_approval is True
