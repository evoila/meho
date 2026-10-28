# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector ingested with a login type survives a restart (#5016).

``meho connector ingest --auth-scheme <name>`` registers a
:class:`ProfiledRestConnector` in the in-memory connector table. Before
#5016 nothing saved the login type, so after a restart the connector had no
class and every call failed with ``no_connector``. These tests ingest a
small spec, empty the connector table the way a restart does, and check
that the class comes back with the same login type and secret field names:
at start (``restore_ingested_profiled_connectors``) and on another replica
that never ran the ingest (the dispatcher's restore before a call).

The tests run against ``sqlite+aiosqlite`` through the autouse fixtures in
:mod:`tests.conftest`. Names are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import shim_kind
from meho_backplane.connectors.profile import AuthSchemeName
from meho_backplane.connectors.profiled import ProfiledRestConnector
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, IngestedAuthProfile
from meho_backplane.operations.dispatcher import _resolve_connector_instance
from meho_backplane.operations.ingest.api_schemas import SpecSource
from meho_backplane.operations.ingest.connector_registration import resolve_authoring_kind
from meho_backplane.operations.ingest.ingest_auth_store import (
    restore_ingested_profiled_connectors,
)
from meho_backplane.operations.ingest.llm_groups import GroupingResult
from meho_backplane.operations.ingest.pipeline import IngestionPipelineService
from meho_backplane.operations.ingest.service import ReviewService

_PRODUCT = "widgetco"
_VERSION = "3.1"
_IMPL_ID = "widgetco-rest"
_TRIPLE = (_PRODUCT, _VERSION, _IMPL_ID)
#: A second connector for the same product and version (see the test with
#: two matching connectors).
_OTHER_IMPL_ID = "widgetco-cloud"

_SPEC_YAML = """
openapi: "3.1.0"
info:
  title: Widgetco fixture
  version: "3.1.0"
paths:
  /stats:
    get:
      operationId: getStats
      summary: Read server stats
      responses:
        "200":
          description: Stats.
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


@dataclass
class _Target:
    """The fields the resolver reads from a target."""

    product: str = _PRODUCT
    version: str | None = _VERSION
    fingerprint: dict[str, Any] | None = None
    preferred_impl_id: str | None = None


def _operator(tenant_id: uuid.UUID | None = None) -> Operator:
    return Operator(
        sub=f"test-operator-{uuid.uuid4()}",
        name="Test Operator",
        email=None,
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id if tenant_id is not None else uuid.uuid4(),
        tenant_role=TenantRole.TENANT_ADMIN,
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
    auth_scheme: AuthSchemeName | None,
    secret_fields: tuple[str, ...] | None = None,
    impl_id: str = _IMPL_ID,
    tenant_id: uuid.UUID | None = None,
) -> None:
    """Run the real register phase; only the LLM grouping is stubbed.

    With *tenant_id* the ingest goes into that tenant's own scope, by an
    operator of that tenant.
    """
    service = IngestionPipelineService(
        _operator(tenant_id),
        sessionmaker=get_sessionmaker(),
        embedding_service=_stub_embedding(),
    )

    async def _no_grouping(**_kwargs: Any) -> GroupingResult:
        return GroupingResult(
            connector_id=f"{impl_id}-{_VERSION}",
            groups_created=0,
            operations_assigned=0,
            operations_unassigned=1,
            llm_call_count=0,
            llm_duration_ms=0.0,
        )

    monkeypatch.setattr(service, "_run_grouping_phase", _no_grouping)
    await service.ingest(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=impl_id,
        specs=[SpecSource(uri="fixture://widgetco/spec.yaml", content=_SPEC_YAML)],
        tenant_id=tenant_id,
        dry_run=False,
        auth_scheme=auth_scheme,
        auth_secret_fields=secret_fields,
    )


async def _descriptor(impl_id: str = _IMPL_ID) -> EndpointDescriptor:
    async with get_sessionmaker()() as session:
        stmt = select(EndpointDescriptor).where(
            EndpointDescriptor.product == _PRODUCT, EndpointDescriptor.impl_id == impl_id
        )
        return (await session.execute(stmt)).scalars().one()


async def _saved_rows() -> list[IngestedAuthProfile]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(IngestedAuthProfile))).scalars().all())


def _registered_profiled() -> type[ProfiledRestConnector]:
    cls = all_connectors_v2()[_TRIPLE]
    assert issubclass(cls, ProfiledRestConnector)
    return cls


@pytest.mark.asyncio
async def test_profiled_connector_comes_back_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug: after a restart the class is gone and calls fail with no_connector."""
    await _ingest(monkeypatch, auth_scheme="basic")
    assert shim_kind(_registered_profiled()) == "profiled"

    # A restart empties the in-memory connector table.
    clear_registry()
    assert _TRIPLE not in all_connectors_v2()

    # The start-up step builds the class again from the saved login type.
    assert await restore_ingested_profiled_connectors() == 1
    restored = _registered_profiled()
    assert restored.profile is not None
    assert restored.profile.auth.scheme == "basic"
    assert restored.profile.auth.secret_fields == ("username", "password")

    # A call reaches the restored class instead of no_connector.
    instance, label, _msg = await _resolve_connector_instance(await _descriptor(), _Target())
    assert label is None
    assert isinstance(instance, restored)

    # Once an operator has enabled an op, the list shows it as profiled and
    # dispatchable.
    assert resolve_authoring_kind(
        product=_PRODUCT, version=_VERSION, enabled_operation_count=1
    ) == ("profiled", True)


@pytest.mark.asyncio
async def test_restore_keeps_custom_secret_field_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator's own secret field names survive the restart too."""
    await _ingest(monkeypatch, auth_scheme="static_header", secret_fields=("api_key",))
    clear_registry()

    await restore_ingested_profiled_connectors()

    profile = _registered_profiled().profile
    assert profile is not None
    assert profile.auth.scheme == "static_header"
    assert profile.auth.secret_fields == ("api_key",)


@pytest.mark.asyncio
async def test_other_replica_builds_class_on_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replica that never ran the ingest (and did not restart) still finds it."""
    await _ingest(monkeypatch, auth_scheme="session_login_token")
    # The other replica's connector table never had the class.
    clear_registry()

    instance, label, _msg = await _resolve_connector_instance(await _descriptor(), _Target())

    assert label is None
    assert isinstance(instance, ProfiledRestConnector)
    profile = _registered_profiled().profile
    assert profile is not None
    assert profile.auth.scheme == "session_login_token"


@pytest.mark.asyncio
async def test_other_replica_loads_the_connector_the_target_prefers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replica that already holds a matching connector still loads the new one.

    Two connectors match the same product and version, with different
    login types. This replica saw only the first ingest. Without the restore
    before the call, the resolver would pick the first connector alone and
    never load the second one, even though the target prefers it.
    """
    await _ingest(monkeypatch, auth_scheme="basic")
    first = all_connectors_v2()[_TRIPLE]
    await _ingest(monkeypatch, auth_scheme="session_login_token", impl_id=_OTHER_IMPL_ID)
    # This replica's connector table holds only the first connector.
    clear_registry()
    register_connector_v2(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=first)
    target = _Target(preferred_impl_id=_OTHER_IMPL_ID)

    instance, label, _msg = await _resolve_connector_instance(
        await _descriptor(_OTHER_IMPL_ID), target
    )

    assert label is None
    assert isinstance(instance, ProfiledRestConnector)
    assert instance.profile is not None
    assert instance.profile.auth.scheme == "session_login_token"
    assert all_connectors_v2()[_TRIPLE] is first


@pytest.mark.asyncio
async def test_restore_scoped_to_a_product_leaves_other_rows_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read before a call only builds the classes of the target's product."""
    await _ingest(monkeypatch, auth_scheme="basic")
    clear_registry()

    assert await restore_ingested_profiled_connectors(product="other-product") == 0
    assert _TRIPLE not in all_connectors_v2()
    assert await restore_ingested_profiled_connectors(product=_PRODUCT) == 1
    assert _TRIPLE in all_connectors_v2()


@pytest.mark.asyncio
async def test_restore_never_enables_an_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring registers a class only; the op stays staged."""
    await _ingest(monkeypatch, auth_scheme="basic")
    clear_registry()

    await restore_ingested_profiled_connectors()

    assert (await _descriptor()).is_enabled is False


@pytest.mark.asyncio
async def test_restore_is_safe_to_run_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key that already has a class is left alone."""
    await _ingest(monkeypatch, auth_scheme="basic")
    before = all_connectors_v2()[_TRIPLE]

    assert await restore_ingested_profiled_connectors() == 0
    assert all_connectors_v2()[_TRIPLE] is before


@pytest.mark.asyncio
async def test_reingest_replaces_the_saved_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """One row per connector; the latest ingest with a login type wins."""
    await _ingest(monkeypatch, auth_scheme="basic")
    await _ingest(monkeypatch, auth_scheme="session_login")

    rows = await _saved_rows()
    assert [(r.auth_scheme, r.secret_fields) for r in rows] == [
        ("session_login", ["username", "password"])
    ]


@pytest.mark.asyncio
async def test_ingest_without_login_type_saves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bare-shim path is unchanged and writes no row."""
    await _ingest(monkeypatch, auth_scheme=None)

    assert await _saved_rows() == []


@pytest.mark.asyncio
async def test_delete_removes_the_saved_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deleted connector does not come back at the next start."""
    await _ingest(monkeypatch, auth_scheme="basic")

    await ReviewService(_operator()).delete_connector(f"{_IMPL_ID}-{_VERSION}", tenant_id=None)
    clear_registry()

    assert await _saved_rows() == []
    assert await restore_ingested_profiled_connectors() == 0
    assert _TRIPLE not in all_connectors_v2()


@pytest.mark.asyncio
async def test_tenant_connector_comes_back_and_is_deleted_in_its_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant's own connector is saved, restored, and deleted under its tenant.

    The restore stamps the row with a system operator of the row's tenant.
    With the wrong tenant on that operator the stamp would raise and the
    backplane would not start.
    """
    tenant_id = uuid.uuid4()
    await _ingest(monkeypatch, auth_scheme="basic", tenant_id=tenant_id)
    assert [row.tenant_id for row in await _saved_rows()] == [tenant_id]
    clear_registry()

    assert await restore_ingested_profiled_connectors() == 1
    profile = _registered_profiled().profile
    assert profile is not None
    assert profile.auth.scheme == "basic"
    assert profile.auth.secret_fields == ("username", "password")

    await ReviewService(_operator(tenant_id)).delete_connector(
        f"{_IMPL_ID}-{_VERSION}", tenant_id=tenant_id
    )

    assert await _saved_rows() == []


@pytest.mark.asyncio
async def test_bad_saved_row_is_skipped() -> None:
    """A row with a login type that is no longer allowed does not stop the start."""
    async with get_sessionmaker()() as session:
        session.add(
            IngestedAuthProfile(
                tenant_id=None,
                product=_PRODUCT,
                version=_VERSION,
                impl_id=_IMPL_ID,
                auth_scheme="no_such_scheme",
                secret_fields=["token"],
                operator_sub="test-operator",
            )
        )
        await session.commit()

    assert await restore_ingested_profiled_connectors() == 0
    assert _TRIPLE not in all_connectors_v2()
