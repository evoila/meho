# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Platform-admin ingest into another tenant (#3800).

A ``platform_admin`` may ingest a tenant-scoped connector row for another
tenant, but only when that tenant id is given explicitly. The write is
audited with the acting operator and the target tenant. Everyone else keeps
today's rule: a foreign tenant id is rejected, and an omitted tenant id
means the built-in scope.

The DB-backed tests run against ``sqlite+aiosqlite`` via the autouse
``_default_database_url`` fixture in :mod:`tests.conftest`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import all_connectors_v2, clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations.ingest import IngestJobNotFoundError, IngestJobRegistry
from meho_backplane.operations.ingest._internals import OP_CROSS_TENANT_INGEST
from meho_backplane.operations.ingest.api_schemas import SpecSource
from meho_backplane.operations.ingest.llm_groups import GroupingResult
from meho_backplane.operations.ingest.pipeline import IngestionPipelineService

_PRODUCT = "xtenant"
_VERSION = "1.0"
_IMPL_ID = "xtenant-rest"
_CONNECTOR_ID = "xtenant-rest-1.0"

_SPEC_YAML = """
openapi: "3.1.0"
info:
  title: Cross-tenant fixture
  version: "1.0.0"
paths:
  /things:
    get:
      operationId: listThings
      summary: List things
      responses:
        "200":
          description: A list of things.
"""


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from meho_backplane.settings import get_settings

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    clear_registry()
    yield
    clear_registry()


def _operator(*, platform_admin: bool, role: TenantRole = TenantRole.TENANT_ADMIN) -> Operator:
    return Operator(
        sub=f"op-{uuid.uuid4()}",
        raw_jwt="header.payload.signature",
        tenant_id=uuid.uuid4(),
        tenant_role=role,
        platform_admin=platform_admin,
    )


def _stub_embedding() -> Any:
    from unittest.mock import AsyncMock

    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


def _service(operator: Operator, monkeypatch: pytest.MonkeyPatch) -> IngestionPipelineService:
    """Build a pipeline whose grouping phase is stubbed (it needs an LLM)."""
    service = IngestionPipelineService(
        operator,
        sessionmaker=get_sessionmaker(),
        embedding_service=_stub_embedding(),
    )

    async def _no_grouping(**_kwargs: Any) -> GroupingResult:
        return GroupingResult(
            connector_id=_CONNECTOR_ID,
            groups_created=0,
            operations_assigned=0,
            operations_unassigned=1,
            llm_call_count=0,
            llm_duration_ms=0.0,
        )

    monkeypatch.setattr(service, "_run_grouping_phase", _no_grouping)
    return service


async def _ingest(
    service: IngestionPipelineService,
    *,
    tenant_id: UUID | None,
    auth_scheme: Any = None,
) -> None:
    await service.ingest(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        specs=[SpecSource(uri="fixture://xtenant/spec.yaml", content=_SPEC_YAML)],
        tenant_id=tenant_id,
        auth_scheme=auth_scheme,
    )


async def _descriptor_tenants() -> list[UUID | None]:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == _PRODUCT)
                )
            )
            .scalars()
            .all()
        )
        return [row.tenant_id for row in rows]


async def _cross_tenant_audit_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.path == OP_CROSS_TENANT_INGEST)))
            .scalars()
            .all()
        )
        return list(rows)


# ---------------------------------------------------------------------------
# authorize_scope: the single predicate every ingest surface shares
# ---------------------------------------------------------------------------


def test_platform_admin_may_name_a_foreign_tenant() -> None:
    service = IngestionPipelineService(_operator(platform_admin=True))
    service.authorize_scope(uuid.uuid4())


def test_non_platform_admin_foreign_tenant_still_rejected() -> None:
    operator = _operator(platform_admin=False)
    other = uuid.uuid4()
    service = IngestionPipelineService(operator)
    with pytest.raises(PermissionError) as exc:
        service.authorize_scope(other)
    assert str(exc.value) == (
        f"operator tenant_id={operator.tenant_id} cannot ingest into tenant_id={other}"
    )


@pytest.mark.parametrize("platform_admin", [False, True])
def test_omitted_tenant_keeps_builtin_rule(platform_admin: bool) -> None:
    """``None`` is the built-in scope: tenant_admin passes, a lower role does not."""
    IngestionPipelineService(_operator(platform_admin=platform_admin)).authorize_scope(None)
    lower = _operator(platform_admin=platform_admin, role=TenantRole.OPERATOR)
    with pytest.raises(PermissionError, match="built-in connector ingest requires tenant_admin"):
        IngestionPipelineService(lower).authorize_scope(None)


# ---------------------------------------------------------------------------
# ingest(): rows land under the target tenant, with an audit row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_admin_ingest_into_foreign_tenant_is_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = _operator(platform_admin=True)
    target = uuid.uuid4()

    await _ingest(_service(operator, monkeypatch), tenant_id=target)

    assert await _descriptor_tenants() == [target]
    rows = await _cross_tenant_audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.operator_sub == operator.sub
    assert row.tenant_id == operator.tenant_id
    assert row.payload == {
        "connector_id": _CONNECTOR_ID,
        "target_tenant_id": str(target),
        "platform_admin": True,
    }


@pytest.mark.asyncio
async def test_platform_admin_foreign_ingest_with_auth_scheme_stamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The profile stamp follows the same allowance instead of failing mid-ingest."""
    operator = _operator(platform_admin=True)
    target = uuid.uuid4()

    await _ingest(_service(operator, monkeypatch), tenant_id=target, auth_scheme="basic")

    assert (_PRODUCT, _VERSION, _IMPL_ID) in all_connectors_v2()
    assert await _descriptor_tenants() == [target]


@pytest.mark.asyncio
async def test_non_platform_admin_foreign_ingest_rejected_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(_operator(platform_admin=False), monkeypatch)

    with pytest.raises(PermissionError, match="cannot ingest into"):
        await _ingest(service, tenant_id=uuid.uuid4())

    assert await _descriptor_tenants() == []
    assert await _cross_tenant_audit_rows() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["own", "builtin"])
async def test_same_tenant_and_builtin_ingest_write_no_cross_tenant_row(
    scope: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform_admin ingesting into its own tenant or the built-in scope is unchanged."""
    operator = _operator(platform_admin=True)
    tenant_id = operator.tenant_id if scope == "own" else None

    await _ingest(_service(operator, monkeypatch), tenant_id=tenant_id)

    assert await _descriptor_tenants() == [tenant_id]
    assert await _cross_tenant_audit_rows() == []


@pytest.mark.asyncio
async def test_dry_run_into_foreign_tenant_writes_nothing() -> None:
    service = IngestionPipelineService(_operator(platform_admin=True))

    await service.ingest(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        specs=[SpecSource(uri="fixture://xtenant/spec.yaml", content=_SPEC_YAML)],
        tenant_id=uuid.uuid4(),
        dry_run=True,
    )

    assert await _cross_tenant_audit_rows() == []


# ---------------------------------------------------------------------------
# Job poll gate: the platform admin can poll the job it started
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_admin_can_poll_foreign_tenant_job() -> None:
    registry = IngestJobRegistry()
    target = uuid.uuid4()
    job = await registry.create(
        operator_sub="op-platform",
        tenant_id=target,
        catalog_entry=None,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        spec_uris=["fixture://xtenant/spec.yaml"],
    )
    caller_tenant = uuid.uuid4()

    found = await registry.get(
        job.job_id, tenant_id=caller_tenant, is_tenant_admin=True, is_platform_admin=True
    )
    assert found.job_id == job.job_id

    with pytest.raises(IngestJobNotFoundError):
        await registry.get(job.job_id, tenant_id=caller_tenant, is_tenant_admin=True)
