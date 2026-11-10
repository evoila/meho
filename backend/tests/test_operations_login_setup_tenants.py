# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector's login setup is kept per tenant.

``meho connector ingest --auth-scheme <name>`` builds a login setup (a
:class:`ProfiledRestConnector` class) from the chosen login type. These
tests check that:

* each tenant's calls use that tenant's own login setup, also when two
  tenants ingest the same product, version and implementation;
* a second login setup for the same connector and scope replaces the first
  one with an audit row that shows the old and the new login type, and is
  never dropped without a word;
* a delete in one tenant leaves every other tenant's login setup alone;
* the ingest answer says when hand-written code or a built-in login setup
  serves the connector instead;
* the restore at start and before a call puts each saved row into the
  table of its own tenant.

The tests run against ``sqlite+aiosqlite`` through the autouse fixtures in
:mod:`tests.conftest`. Names and secrets are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.base import Connector, shim_kind
from meho_backplane.connectors.profile import AuthSchemeName
from meho_backplane.connectors.profiled import ProfiledRestConnector
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    connectors_for_tenant,
    get_tenant_connector,
    register_connector_v2,
    set_tenant_connector,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, IngestedAuthProfile
from meho_backplane.operations.dispatcher import _resolve_connector_instance
from meho_backplane.operations.ingest._internals import OP_PROFILE_STAMP
from meho_backplane.operations.ingest.api_schemas import SpecSource
from meho_backplane.operations.ingest.ingest_auth_store import (
    restore_ingested_profiled_connectors,
)
from meho_backplane.operations.ingest.llm_groups import GroupingResult
from meho_backplane.operations.ingest.pipeline import (
    IngestionPipelineResult,
    IngestionPipelineService,
)
from meho_backplane.operations.ingest.service import ReviewService

_PRODUCT = "gadgetco"
_VERSION = "1.0"
_IMPL_ID = "gadgetco-rest"
_TRIPLE = (_PRODUCT, _VERSION, _IMPL_ID)
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"

_SPEC_YAML = """
openapi: "3.1.0"
info:
  title: Gadgetco fixture
  version: "1.0.0"
paths:
  /items:
    get:
      operationId: listItems
      summary: List items
      responses:
        "200":
          description: Items.
"""

#: Made-up secrets for the header checks.
_SECRETS = {"username": "made-up-user", "password": "made-up-pass", "token": "made-up-token"}


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


@dataclass
class _Target:
    """The fields the resolver and the login code read from a target."""

    tenant_id: uuid.UUID | None
    product: str = _PRODUCT
    version: str | None = _VERSION
    fingerprint: dict[str, Any] | None = None
    preferred_impl_id: str | None = None
    auth_model: str | None = None
    name: str = "gadget-target"


class _HandWrittenGadget(HttpConnector):
    """Hand-written connector code for the same key (runs ingested ops too)."""

    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL_ID
    supported_version_range = ">=1.0,<2.0"
    accepts_ingested_operations = True


def _operator(tenant_id: uuid.UUID | None) -> Operator:
    """A tenant admin of *tenant_id*; a platform admin for the built-in scope."""
    return Operator(
        sub=f"test-operator-{uuid.uuid4()}",
        name="Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id if tenant_id is not None else uuid.uuid4(),
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=tenant_id is None,
    )


def _stub_embedding() -> Any:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _ingest(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tenant_id: uuid.UUID | None,
    auth_scheme: AuthSchemeName | None,
) -> IngestionPipelineResult:
    """Run the real register phase in *tenant_id*'s scope; only grouping is stubbed."""
    service = IngestionPipelineService(
        _operator(tenant_id),
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
    return await service.ingest(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        specs=[SpecSource(uri="fixture://gadgetco/spec.yaml", content=_SPEC_YAML)],
        tenant_id=tenant_id,
        dry_run=False,
        auth_scheme=auth_scheme,
    )


async def _descriptor(tenant_id: uuid.UUID | None) -> EndpointDescriptor:
    async with get_sessionmaker()() as session:
        stmt = select(EndpointDescriptor).where(EndpointDescriptor.product == _PRODUCT)
        if tenant_id is None:
            stmt = stmt.where(EndpointDescriptor.tenant_id.is_(None))
        else:
            stmt = stmt.where(EndpointDescriptor.tenant_id == tenant_id)
        return (await session.execute(stmt)).scalars().one()


async def _resolve(tenant_id: uuid.UUID) -> Connector | None:
    """Resolve the connector a call of *tenant_id* to its own op would use."""
    instance, _label, _msg = await _resolve_connector_instance(
        await _descriptor(tenant_id), _Target(tenant_id=tenant_id)
    )
    return instance


def _scheme(instance: Connector | None) -> str | None:
    assert isinstance(instance, ProfiledRestConnector), instance
    assert instance.profile is not None
    return instance.profile.auth.scheme


async def _headers(instance: Connector | None, tenant_id: uuid.UUID) -> dict[str, str]:
    assert isinstance(instance, ProfiledRestConnector)

    async def _made_up_secrets(_target: Any, _operator: Operator) -> dict[str, str]:
        return dict(_SECRETS)

    instance._injected_loader = _made_up_secrets
    return await instance.auth_headers(_Target(tenant_id=tenant_id), _operator(tenant_id))


async def _stamp_audit_payloads() -> list[dict[str, Any]]:
    async with get_sessionmaker()() as session:
        stmt = (
            select(AuditLog)
            .where(AuditLog.path == OP_PROFILE_STAMP)
            .order_by(AuditLog.occurred_at, AuditLog.id)
        )
        return [row.payload for row in (await session.execute(stmt)).scalars().all()]


# ---------------------------------------------------------------------------
# Two tenants, the same connector key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_tenant_calls_with_its_own_login_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    result_a = await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")
    result_b = await _ingest(monkeypatch, tenant_id=tenant_b, auth_scheme="static_header")

    assert result_a.ingestion.login_setup == "registered"
    assert result_b.ingestion.login_setup == "registered"
    assert result_b.ingestion.connector_registered is True
    instance_a, instance_b = await _resolve(tenant_a), await _resolve(tenant_b)
    assert _scheme(instance_a) == "basic"
    assert _scheme(instance_b) == "static_header"
    assert (await _headers(instance_a, tenant_a))["Authorization"].startswith("Basic ")
    assert (await _headers(instance_b, tenant_b))["Authorization"] == "Bearer made-up-token"


@pytest.mark.asyncio
async def test_login_type_after_a_stand_in_of_another_tenant_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme=None)
    result_b = await _ingest(monkeypatch, tenant_id=tenant_b, auth_scheme="basic")

    assert result_b.ingestion.login_setup == "registered"
    assert _scheme(await _resolve(tenant_b)) == "basic"
    # Tenant A chose no login type: its calls still reach only the stand-in.
    assert shim_kind(await _resolve(tenant_a)) == "bare"


@pytest.mark.asyncio
async def test_tenant_without_login_type_never_gets_another_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()

    await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")
    result_b = await _ingest(monkeypatch, tenant_id=tenant_b, auth_scheme=None)

    assert result_b.ingestion.login_setup is None
    assert shim_kind(await _resolve(tenant_b)) == "bare"
    assert _scheme(await _resolve(tenant_a)) == "basic"


@pytest.mark.asyncio
async def test_delete_in_one_tenant_keeps_the_other_tenants_login_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")
    await _ingest(monkeypatch, tenant_id=tenant_b, auth_scheme="session_login")

    await ReviewService(_operator(tenant_a)).delete_connector(_CONNECTOR_ID, tenant_id=tenant_a)

    assert get_tenant_connector(tenant_a, *_TRIPLE) is None
    assert _scheme(await _resolve(tenant_b)) == "session_login"


# ---------------------------------------------------------------------------
# A second login setup in the same scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_login_type_in_a_tenant_replaces_the_first_with_an_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    await _ingest(monkeypatch, tenant_id=tenant_b, auth_scheme="session_login")
    await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")

    result = await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="static_header")

    assert result.ingestion.login_setup == "replaced"
    assert result.to_api_models()[0].login_setup == "replaced"
    assert _scheme(await _resolve(tenant_a)) == "static_header"
    assert _scheme(await _resolve(tenant_b)) == "session_login"
    last = (await _stamp_audit_payloads())[-1]
    assert last["tenant_scope"] == str(tenant_a)
    assert last["replaced"] is True
    assert last["previous_auth_scheme"] == "basic"
    assert last["previous_secret_fields"] == ["username", "password"]
    assert last["auth_scheme"] == "static_header"
    assert last["secret_fields"] == ["token"]


@pytest.mark.asyncio
async def test_same_login_type_twice_is_unchanged_and_audited_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a = uuid.uuid4()
    await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")

    result = await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")

    assert result.ingestion.login_setup == "unchanged"
    assert result.ingestion.connector_registered is False
    payloads = await _stamp_audit_payloads()
    assert len(payloads) == 1
    assert payloads[0]["replaced"] is False


@pytest.mark.asyncio
async def test_second_login_type_in_the_built_in_scope_replaces_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ingest(monkeypatch, tenant_id=None, auth_scheme="basic")

    result = await _ingest(monkeypatch, tenant_id=None, auth_scheme="session_login")

    assert result.ingestion.login_setup == "replaced"
    shared = all_connectors_v2()[_TRIPLE]
    assert issubclass(shared, ProfiledRestConnector)
    assert shared.profile is not None
    assert shared.profile.auth.scheme == "session_login"
    last = (await _stamp_audit_payloads())[-1]
    assert last["tenant_scope"] is None
    assert (last["previous_auth_scheme"], last["auth_scheme"]) == ("basic", "session_login")


@pytest.mark.asyncio
async def test_built_in_login_type_after_a_built_in_stand_in_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ingest(monkeypatch, tenant_id=None, auth_scheme=None)
    assert shim_kind(all_connectors_v2()[_TRIPLE]) == "bare"

    result = await _ingest(monkeypatch, tenant_id=None, auth_scheme="basic")

    assert result.ingestion.login_setup == "registered"
    assert shim_kind(all_connectors_v2()[_TRIPLE]) == "profiled"


# ---------------------------------------------------------------------------
# The answer says when another class serves the key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_says_a_built_in_login_type_serves_the_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_b = uuid.uuid4()
    await _ingest(monkeypatch, tenant_id=None, auth_scheme="basic")

    result = await _ingest(monkeypatch, tenant_id=tenant_b, auth_scheme="session_login")

    assert result.ingestion.login_setup == "served_by_builtin_login_setup"
    assert result.ingestion.connector_registered is False
    assert get_tenant_connector(tenant_b, *_TRIPLE) is None
    assert _scheme(await _resolve(tenant_b)) == "basic"


@pytest.mark.asyncio
async def test_answer_says_connector_code_serves_the_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_b = uuid.uuid4()
    register_connector_v2(
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=_HandWrittenGadget
    )

    result = await _ingest(monkeypatch, tenant_id=tenant_b, auth_scheme="basic")

    assert result.ingestion.login_setup == "served_by_connector_code"
    assert result.ingestion.connector_registered is False
    assert all_connectors_v2()[_TRIPLE] is _HandWrittenGadget
    assert get_tenant_connector(tenant_b, *_TRIPLE) is None
    assert await _stamp_audit_payloads() == []


# ---------------------------------------------------------------------------
# Restore at start and before a call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_puts_each_saved_row_in_its_own_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")
    await _ingest(monkeypatch, tenant_id=tenant_b, auth_scheme="static_header")
    clear_registry()

    assert await restore_ingested_profiled_connectors() == 2

    assert _scheme(await _resolve(tenant_a)) == "basic"
    assert _scheme(await _resolve(tenant_b)) == "static_header"
    assert await restore_ingested_profiled_connectors() == 0


@pytest.mark.asyncio
async def test_restore_before_a_call_reads_only_the_targets_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")
    await _ingest(monkeypatch, tenant_id=tenant_b, auth_scheme="static_header")
    clear_registry()

    assert await restore_ingested_profiled_connectors(product=_PRODUCT, tenant_id=tenant_a) == 1

    assert get_tenant_connector(tenant_a, *_TRIPLE) is not None
    assert get_tenant_connector(tenant_b, *_TRIPLE) is None


@pytest.mark.asyncio
async def test_restore_takes_a_login_type_changed_on_another_replica(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a = uuid.uuid4()
    await _ingest(monkeypatch, tenant_id=tenant_a, auth_scheme="basic")
    # Another replica took a second ingest with another login type.
    async with get_sessionmaker()() as session:
        await session.execute(
            update(IngestedAuthProfile).values(auth_scheme="static_header", secret_fields=["token"])
        )
        await session.commit()

    assert await restore_ingested_profiled_connectors(product=_PRODUCT, tenant_id=tenant_a) == 1

    assert _scheme(await _resolve(tenant_a)) == "static_header"
    assert (await _stamp_audit_payloads())[-1]["previous_auth_scheme"] == "basic"


# ---------------------------------------------------------------------------
# The tenant view of the connector table
# ---------------------------------------------------------------------------


class _TenantProfiled(ProfiledRestConnector):
    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL_ID
    supported_version_range = ">=1.0,<2.0"


class _SharedProfiled(ProfiledRestConnector):
    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL_ID
    supported_version_range = ">=1.0,<2.0"


def test_tenant_view_shows_a_tenant_class_only_to_its_tenant() -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    set_tenant_connector(
        tenant_id=tenant_a,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=_TenantProfiled,
    )

    assert connectors_for_tenant(tenant_a)[_TRIPLE] is _TenantProfiled
    assert _TRIPLE not in connectors_for_tenant(tenant_b)
    assert _TRIPLE not in connectors_for_tenant(None)
    assert _TRIPLE not in all_connectors_v2()


@pytest.mark.parametrize("shared", [_HandWrittenGadget, _SharedProfiled])
def test_tenant_view_keeps_a_shared_class_that_serves_every_tenant(
    shared: type[Connector],
) -> None:
    tenant_a = uuid.uuid4()
    register_connector_v2(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=shared)
    set_tenant_connector(
        tenant_id=tenant_a,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        cls=_TenantProfiled,
    )

    assert connectors_for_tenant(tenant_a)[_TRIPLE] is shared
