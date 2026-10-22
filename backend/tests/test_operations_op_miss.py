# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the lookup-miss errors of dispatch and preview (#4031).

When ``call_operation`` / ``preview_operation`` name a connector that does
not have the op, the ``unknown_op`` error names the other connectors the
caller can see that do have it (``extras.connectors_with_op``). When the
connector id itself is unknown, both answer ``unknown_connector``.

Rows are real ``endpoint_descriptor`` rows in the autouse-migrated SQLite
database, so the tenant scope runs as real SQL.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch
from meho_backplane.operations._lookup import find_connectors_with_op
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.settings import get_settings

_TENANT_A = UUID("00000000-0000-0000-0000-00000000000a")
_TENANT_B = UUID("00000000-0000-0000-0000-00000000000b")

_OP = "GET:/api/vcenter/cluster"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` against the autouse-migrated SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _operator(tenant_id: UUID) -> Operator:
    return Operator(
        sub="op-miss",
        name="Op Miss Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


async def _insert(
    session: AsyncSession,
    *,
    version: str,
    op_id: str,
    tenant_id: UUID | None = None,
    is_enabled: bool = True,
) -> None:
    """Insert one ingested ``vmware-rest-<version>`` descriptor row."""
    method, path = op_id.split(":", 1)
    session.add(
        EndpointDescriptor(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            product="vmware",
            version=version,
            impl_id="vmware-rest",
            op_id=op_id,
            source_kind="ingested",
            method=method,
            path=path,
            handler_ref=None,
            summary="fixture",
            description="fixture",
            group_id=None,
            tags=[],
            parameter_schema={"type": "object"},
            response_schema=None,
            llm_instructions=None,
            safety_level="safe",
            requires_approval=False,
            is_enabled=is_enabled,
            embedding=None,
            custom_description=None,
            custom_notes=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def _call(tenant_id: UUID, connector_id: str, op_id: str = _OP) -> dict[str, object]:
    result = await dispatch(
        operator=_operator(tenant_id),
        connector_id=connector_id,
        op_id=op_id,
        target=None,
        params={},
    )
    assert result.status == "error"
    return {"error": result.error, "extras": dict(result.extras)}


@pytest.mark.asyncio
async def test_unknown_op_names_the_connector_that_has_the_op(session: AsyncSession) -> None:
    """The op lives on 9.0 only; calling it on 8.0 names 9.0."""
    await _insert(session, version="8.0", op_id="GET:/vcenter/cluster")
    await _insert(session, version="9.0", op_id=_OP)

    out = await _call(_TENANT_A, "vmware-rest-8.0")

    assert out["extras"]["error_code"] == "unknown_op"
    assert out["extras"]["known_op_count"] == 1
    assert out["extras"]["connectors_with_op"] == ["vmware-rest-9.0"]
    assert str(out["error"]).startswith("unknown_op:")
    assert "vmware-rest-9.0" in str(out["error"])


@pytest.mark.asyncio
async def test_unknown_op_without_other_connector_points_at_search(
    session: AsyncSession,
) -> None:
    """No other connector has the op: the message names search_operations."""
    await _insert(session, version="8.0", op_id="GET:/vcenter/cluster")

    out = await _call(_TENANT_A, "vmware-rest-8.0")

    assert out["extras"]["error_code"] == "unknown_op"
    assert out["extras"]["connectors_with_op"] == []
    assert "search_operations" in str(out["error"])


@pytest.mark.asyncio
async def test_unknown_op_never_names_another_tenants_connector(
    session: AsyncSession,
) -> None:
    """A connector private to tenant B is not named to tenant A, but is to B."""
    await _insert(session, version="8.0", op_id="GET:/vcenter/cluster")
    await _insert(session, version="9.0", op_id=_OP, tenant_id=_TENANT_B)

    out_a = await _call(_TENANT_A, "vmware-rest-8.0")
    assert out_a["extras"]["connectors_with_op"] == []
    assert "vmware-rest-9.0" not in str(out_a["error"])

    out_b = await _call(_TENANT_B, "vmware-rest-8.0")
    assert out_b["extras"]["connectors_with_op"] == ["vmware-rest-9.0"]


@pytest.mark.asyncio
async def test_tenant_row_hides_the_built_in_op(session: AsyncSession) -> None:
    """The tenant's own connector row wins (#3870), so the built-in op is not named.

    Tenant A owns a ``vmware-rest-9.0`` row without the op. The built-in
    ``vmware-rest-9.0`` row has it, but tenant A cannot dispatch it, so it
    must not be suggested. Tenant B has no own row and sees the built-in one.
    """
    await _insert(session, version="8.0", op_id="GET:/vcenter/cluster")
    await _insert(session, version="9.0", op_id=_OP)
    await _insert(session, version="9.0", op_id="GET:/api/vcenter/vm", tenant_id=_TENANT_A)

    assert (
        await find_connectors_with_op(
            tenant_id=_TENANT_A, op_id=_OP, exclude=("vmware", "8.0", "vmware-rest")
        )
        == []
    )
    assert await find_connectors_with_op(
        tenant_id=_TENANT_B, op_id=_OP, exclude=("vmware", "8.0", "vmware-rest")
    ) == ["vmware-rest-9.0"]


@pytest.mark.asyncio
async def test_disabled_op_is_not_named(session: AsyncSession) -> None:
    """Only connectors where the op is enabled are named."""
    await _insert(session, version="8.0", op_id="GET:/vcenter/cluster")
    await _insert(session, version="9.0", op_id=_OP, is_enabled=False)

    out = await _call(_TENANT_A, "vmware-rest-8.0")

    assert out["extras"]["connectors_with_op"] == []


@pytest.mark.asyncio
async def test_unknown_connector_id_returns_unknown_connector(session: AsyncSession) -> None:
    """A connector id with no rows and no registered class is ``unknown_connector``."""
    await _insert(session, version="9.0", op_id=_OP)

    out = await _call(_TENANT_A, "vmware-rest-7.9")

    assert out["extras"]["error_code"] == "unknown_connector"
    assert out["extras"]["connector_id"] == "vmware-rest-7.9"
    assert str(out["error"]).startswith("unknown_connector:")


@pytest.mark.asyncio
async def test_preview_unknown_op_names_the_connector_that_has_the_op(
    session: AsyncSession,
) -> None:
    """``preview_operation`` gives the same answer as ``call_operation``."""
    await _insert(session, version="8.0", op_id="GET:/vcenter/cluster")
    await _insert(session, version="9.0", op_id=_OP)

    envelope = await preview_dispatch(
        operator=_operator(_TENANT_A),
        connector_id="vmware-rest-8.0",
        op_id=_OP,
        target=None,
        params={},
    )

    assert envelope["status"] == "error"
    assert envelope["connector_id"] == "vmware-rest-8.0"
    assert envelope["extras"]["error_code"] == "unknown_op"
    assert envelope["extras"]["connectors_with_op"] == ["vmware-rest-9.0"]
    assert type(envelope["extras"]) is dict
    assert "vmware-rest-9.0" in envelope["error"]


@pytest.mark.asyncio
async def test_preview_unknown_connector_id_returns_unknown_connector(
    session: AsyncSession,
) -> None:
    """``preview_operation`` on an unknown connector id is ``unknown_connector``."""
    envelope = await preview_dispatch(
        operator=_operator(_TENANT_A),
        connector_id="nope-1.0",
        op_id=_OP,
        target=None,
        params={},
    )

    assert envelope["status"] == "error"
    assert envelope["extras"]["error_code"] == "unknown_connector"
    assert envelope["error"].startswith("unknown_connector:")
