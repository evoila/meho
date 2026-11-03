# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ingested reads of licence keys stay off.

A vendor API file can describe a read whose answer carries licence keys
unmasked. The ingest gives such a read the ``safe`` level of its HTTP
method. MEHO keeps it off instead (``operations/license_reads.py``):

* the rule: a ``GET`` or ``HEAD`` whose path names licences;
* ``enable-reads`` leaves such a row off and turns the other reads on;
* a call and a preview of such a read answer ``license_read_off`` before
  any vendor call, when the row is off and when it was turned on by hand,
  so a made-up key the vendor would return reaches nothing: not the
  result, not the audit log; a row another tenant owns stays unknown;
* the flight recorder never records the body of such a read, whatever
  the case of the letters in its path.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._errors import result_license_read_off
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.ingest._internals import ConnectorScope, bulk_enable_read_ops
from meho_backplane.operations.license_reads import is_license_read, is_license_read_op_id
from meho_backplane.redaction.flight_recorder import classify_body_exclusion
from meho_backplane.settings import get_settings

_TENANT: UUID = UUID("00000000-0000-0000-0000-00000000a560")
_OTHER_TENANT: UUID = UUID("00000000-0000-0000-0000-00000000b560")
_PRODUCT = "licfixture"
_VERSION = "1"
_IMPL_ID = "licfixture-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"

_KEY_LIST = "GET:/v1/license-keys"
_KEY_ONE = "GET:/v1/license-keys/{key}"
_HOSTS = "GET:/v1/hosts"

#: A made-up key in the vendor's format. It must reach nothing.
_MADE_UP_KEY = "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _make_operator() -> Operator:
    return Operator(
        sub="op-license-reads",
        name="Licence Reads Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


class _FakeFingerprint:
    def __init__(self, version: str | None = None) -> None:
        self.version = version


class _FakeTarget:
    """The target shape the resolver, the dispatcher and the connector read."""

    def __init__(self) -> None:
        self.product = _PRODUCT
        self.fingerprint = _FakeFingerprint(version=_VERSION)
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.name = "lic-lab"
        self.host = "lic.lab.internal"
        self.port = 443
        self.auth_model: str | None = "shared_service_account"
        self.secret_ref: str | None = None


class _KeyReturningConnector(HttpConnector):
    """A connector whose vendor answer carries the made-up key unmasked.

    Counts its calls: the dispatcher must never reach it for a licence read.
    """

    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL_ID
    supported_version_range = ">=1,<2"
    priority = 1

    calls: ClassVar[int] = 0

    async def _request_json(
        self,
        target: Any,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        type(self).calls += 1
        return {
            "elements": [
                {
                    "key": _MADE_UP_KEY,
                    "productType": "VCENTER",
                    "licenseKeyUsage": {"total": 10, "used": 2},
                }
            ]
        }

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        type(self).calls += 1
        return {}

    async def fingerprint(  # type: ignore[override]
        self,
        target: Any,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError


def _descriptor(
    *,
    op_id: str,
    tenant_id: UUID | None,
    is_enabled: bool,
    embedding: list[float] | None = None,
) -> EndpointDescriptor:
    method, _, path = op_id.partition(":")
    return EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=op_id,
        source_kind="ingested",
        method=method,
        path=path,
        handler_ref=None,
        summary=f"{method} {path}",
        description="Ingested read test op.",
        tags=["licensing"],
        parameter_schema={"type": "object", "properties": {}},
        response_schema=None,
        llm_instructions=None,
        safety_level="safe",
        requires_approval=False,
        is_enabled=is_enabled,
        embedding=embedding,
        custom_description=None,
        custom_notes=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id",
    [
        _KEY_LIST,
        _KEY_ONE,
        "HEAD:/v1/license-keys",
        "GET:/LicenseManager/{moId}/licenses",
        "GET:/api/v1/licenses",
        "GET:/pairings/{pairing_id}/srms/{srm_id}/licensing",
        "GET:/api/licence",
    ],
)
def test_rule_matches_a_read_whose_path_names_licences(op_id: str) -> None:
    assert is_license_read_op_id(op_id) is True


@pytest.mark.parametrize(
    "op_id",
    [
        "POST:/v1/license-keys",
        "DELETE:/v1/license-keys/{key}",
        _HOSTS,
        "GET:/v1/credentials",
        "sddc.license.list",
        "vmware.license.list",
    ],
)
def test_rule_leaves_writes_other_reads_and_hand_written_ops_alone(op_id: str) -> None:
    assert is_license_read_op_id(op_id) is False


def test_rule_needs_both_a_method_and_a_path() -> None:
    assert is_license_read(None, "/v1/license-keys") is False
    assert is_license_read("GET", None) is False
    assert is_license_read("get", "/v1/License-Keys") is True


# ---------------------------------------------------------------------------
# The error result
# ---------------------------------------------------------------------------


def test_result_license_read_off_shape() -> None:
    out = result_license_read_off(_KEY_LIST, 1.0)
    assert out.status == "error"
    assert out.op_id == _KEY_LIST
    assert out.error is not None
    assert out.error.startswith("license_read_off:")
    assert "hand-written" in out.error
    assert "masked" in out.error
    assert out.extras == {"error_code": "license_read_off", "op_id": _KEY_LIST}


# ---------------------------------------------------------------------------
# enable-reads leaves licence reads off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_enable_reads_leaves_licence_reads_off(session: AsyncSession) -> None:
    for op_id in (_KEY_LIST, _KEY_ONE, _HOSTS):
        session.add(_descriptor(op_id=op_id, tenant_id=_TENANT, is_enabled=False))
    await session.commit()

    scope = ConnectorScope(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, tenant_id=_TENANT)
    enabled = await bulk_enable_read_ops(session, scope)
    await session.commit()
    assert enabled == 1

    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.product == _PRODUCT)
            )
        )
        .scalars()
        .all()
    )
    state = {row.op_id: row.is_enabled for row in rows}
    assert state == {_KEY_LIST: False, _KEY_ONE: False, _HOSTS: True}


# ---------------------------------------------------------------------------
# A call and a preview refuse the read before any vendor call
# ---------------------------------------------------------------------------


async def _seed_licence_reads(
    session: AsyncSession,
    embedding: list[float],
    *,
    is_enabled: bool,
    tenant_id: UUID | None = None,
) -> None:
    """Seed both licence reads and the hosts read, and register the connector."""
    _KeyReturningConnector.calls = 0
    register_connector_v2(
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=_KeyReturningConnector
    )
    for op_id in (_KEY_LIST, _KEY_ONE):
        session.add(
            _descriptor(
                op_id=op_id, tenant_id=tenant_id, is_enabled=is_enabled, embedding=embedding
            )
        )
    session.add(
        _descriptor(op_id=_HOSTS, tenant_id=tenant_id, is_enabled=True, embedding=embedding)
    )
    await session.commit()


async def _seed_enabled_licence_reads(session: AsyncSession, embedding: list[float]) -> None:
    """Seed both licence reads turned on by hand (built-in rows)."""
    await _seed_licence_reads(session, embedding, is_enabled=True)


async def _seed_ingested_licence_reads_after_enable_reads(
    session: AsyncSession, embedding: list[float]
) -> None:
    """Seed the rows the way an ingest leaves them, then run ``enable-reads``."""
    await _seed_licence_reads(session, embedding, is_enabled=False, tenant_id=_TENANT)
    scope = ConnectorScope(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, tenant_id=_TENANT)
    await bulk_enable_read_ops(session, scope)
    await session.commit()


async def _audit_rows_for(op_id: str) -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as fresh:
        return list(
            (await fresh.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", [_KEY_LIST, _KEY_ONE])
async def test_call_refuses_a_licence_read_turned_on_by_hand(
    op_id: str,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    await _seed_enabled_licence_reads(session, stub_embedding_service.encode_one.return_value)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=_FakeTarget(),
        params={},
    )

    assert result.status == "error"
    assert result.extras["error_code"] == "license_read_off"
    assert _KeyReturningConnector.calls == 0
    assert _MADE_UP_KEY not in json.dumps(result.model_dump(mode="json"))
    assert await _audit_rows_for(op_id) == []


@pytest.mark.asyncio
async def test_call_refuses_the_licence_read_before_the_target_check(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """Without a target the answer is still ``license_read_off``: the read never runs."""
    await _seed_enabled_licence_reads(session, stub_embedding_service.encode_one.return_value)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_KEY_LIST,
        target=None,
        params={},
    )

    assert result.extras["error_code"] == "license_read_off"


@pytest.mark.asyncio
async def test_preview_refuses_a_licence_read_the_same_way(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    await _seed_enabled_licence_reads(session, stub_embedding_service.encode_one.return_value)

    out = await preview_dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_KEY_LIST,
        target=_FakeTarget(),
        params={},
    )

    assert out["status"] == "error"
    assert out["extras"]["error_code"] == "license_read_off"
    assert out["source_kind"] == "ingested"
    assert _KeyReturningConnector.calls == 0


# ---------------------------------------------------------------------------
# A call and a preview of the row that is off give the same answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", [_KEY_LIST, _KEY_ONE])
async def test_call_of_a_licence_read_that_is_off_answers_license_read_off(
    op_id: str,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """After the ingest and ``enable-reads`` the row is off; the answer names the masked read."""
    await _seed_ingested_licence_reads_after_enable_reads(
        session, stub_embedding_service.encode_one.return_value
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=_FakeTarget(),
        params={},
    )

    assert result.status == "error"
    assert result.extras["error_code"] == "license_read_off"
    assert result.error is not None
    assert "hand-written" in result.error
    assert _KeyReturningConnector.calls == 0
    assert _MADE_UP_KEY not in json.dumps(result.model_dump(mode="json"))
    assert await _audit_rows_for(op_id) == []


@pytest.mark.asyncio
async def test_preview_of_a_licence_read_that_is_off_answers_license_read_off(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    await _seed_ingested_licence_reads_after_enable_reads(
        session, stub_embedding_service.encode_one.return_value
    )

    out = await preview_dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_KEY_LIST,
        target=_FakeTarget(),
        params={},
    )

    assert out["status"] == "error"
    assert out["extras"]["error_code"] == "license_read_off"
    assert _KeyReturningConnector.calls == 0


@pytest.mark.asyncio
async def test_a_licence_read_of_another_tenant_stays_unknown(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """The rows of another tenant are not visible, so the answer says nothing about them."""
    await _seed_licence_reads(
        session,
        stub_embedding_service.encode_one.return_value,
        is_enabled=False,
        tenant_id=_OTHER_TENANT,
    )

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_KEY_LIST,
        target=_FakeTarget(),
        params={},
    )

    assert result.status == "error"
    assert result.extras["error_code"] == "unknown_op"
    assert _KeyReturningConnector.calls == 0


@pytest.mark.asyncio
async def test_an_absent_licence_read_stays_unknown(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
) -> None:
    """Only the other read is ingested: a licence read that has no row is ``unknown_op``."""
    _KeyReturningConnector.calls = 0
    register_connector_v2(
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=_KeyReturningConnector
    )
    session.add(
        _descriptor(
            op_id=_HOSTS,
            tenant_id=_TENANT,
            is_enabled=True,
            embedding=stub_embedding_service.encode_one.return_value,
        )
    )
    await session.commit()

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_KEY_LIST,
        target=_FakeTarget(),
        params={},
    )

    assert result.extras["error_code"] == "unknown_op"


# ---------------------------------------------------------------------------
# The flight recorder never records the body of a licence read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op_id",
    [
        _KEY_LIST,
        _KEY_ONE,
        "GET:/LicenseManager/{moId}/licenses",
        # the vendor's own spelling, in any case
        "GET:/v1/License-Keys",
        "POST:/v1/License-Keys",
        "GET:/api/LICENSES",
        "HEAD:/API/Licensing",
    ],
)
def test_flight_recorder_records_no_body_for_a_licence_read(op_id: str) -> None:
    out = classify_body_exclusion(op_id)
    assert out.excluded is True
    assert out.family == "secret-bearing"
    assert out.uncertain is False


def test_flight_recorder_keeps_the_body_of_a_read_that_names_no_licence() -> None:
    assert classify_body_exclusion("GET:/v1/Hosts").excluded is False
