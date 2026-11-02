# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""NSX two-implementation test: ``nsx-rest-4.2`` for NSX 4.x, ``nsx-rest-9.0`` for 9.x.

Two classes register under ``product="nsx"`` and ``impl_id="nsx-rest"``
with bands that do not overlap:

* :class:`~meho_backplane.connectors.nsx.connector.NsxConnector`
  (``nsx-rest-9.0``, ``>=9.0,<10.0``). It also owns the
  ``("nsx", "", "")`` wildcard.
* :class:`~meho_backplane.connectors.nsx.connector_4x.Nsx4Connector`
  (``nsx-rest-4.2``, ``>=4.0,<9.0``), a thin subclass.

The resolver picks one per target by the target's detected version. A
target with no detected version resolves to the 9.x class through the
wildcard. Both classes register the same ten typed reads, and the
dispatcher runs a read on the class the target resolves to, whichever
connector id the caller named. Mirrors
:mod:`tests.test_connectors_vrops8_dual_impl_resolution`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.nsx import (
    NSX4_CONNECTOR_ID,
    NSX4_VERSION,
    NSX_CONNECTOR_ID,
    NSX_IMPL_ID,
    NSX_PRODUCT,
    NSX_TYPED_OPS,
    NSX_VERSION,
    Nsx4Connector,
    NsxConnector,
    register_nsx4_typed_operations,
    register_nsx_typed_operations,
)
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.settings import get_settings

_NSX_HOST = "nsx-dual.test.invalid"
_XSRF_HEADERS = {
    "X-XSRF-TOKEN": "dual-xsrf-token",
    "Set-Cookie": "JSESSIONID=dual-session-id; Path=/; HttpOnly",
}


@dataclass
class _FakeFingerprint:
    version: str | None


@dataclass
class _FakeTarget:
    product: str
    fingerprint: _FakeFingerprint | None = None
    preferred_impl_id: str | None = None
    version: str | None = None


def _register_both_impls() -> None:
    """Register both NSX impls exactly as the package ``__init__`` does."""
    register_connector_v2(
        product=NSX_PRODUCT, version=NSX_VERSION, impl_id=NSX_IMPL_ID, cls=NsxConnector
    )
    register_connector_v2(
        product=NSX_PRODUCT, version=NSX4_VERSION, impl_id=NSX_IMPL_ID, cls=Nsx4Connector
    )
    register_connector_v2(product=NSX_PRODUCT, version="", impl_id="", cls=NsxConnector)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    _register_both_impls()
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )


# ---------------------------------------------------------------------------
# Registration and resolution (no database)
# ---------------------------------------------------------------------------


def test_both_impls_register_under_distinct_triples() -> None:
    """Same product and impl_id, different version; the 9.x class owns the wildcard."""
    assert (NsxConnector.product, NsxConnector.version, NsxConnector.impl_id) == (
        "nsx",
        "9.0",
        "nsx-rest",
    )
    assert (Nsx4Connector.product, Nsx4Connector.version, Nsx4Connector.impl_id) == (
        "nsx",
        "4.2",
        "nsx-rest",
    )
    assert issubclass(Nsx4Connector, NsxConnector)
    assert NsxConnector.supported_version_range == ">=9.0,<10.0"
    assert Nsx4Connector.supported_version_range == ">=4.0,<9.0"
    assert NSX_CONNECTOR_ID == "nsx-rest-9.0"
    assert NSX4_CONNECTOR_ID == "nsx-rest-4.2"

    registry = all_connectors_v2()
    assert registry[("nsx", "9.0", "nsx-rest")] is NsxConnector
    assert registry[("nsx", "4.2", "nsx-rest")] is Nsx4Connector
    assert registry[("nsx", "", "")] is NsxConnector


@pytest.mark.parametrize("version", ["4.0", "4.1.2", "4.2", "4.2.1", "4.2.1.0.0.24304122"])
def test_4x_target_resolves_nsx4(version: str) -> None:
    target = _FakeTarget(product="nsx", fingerprint=_FakeFingerprint(version))
    assert resolve_connector(target) is Nsx4Connector


@pytest.mark.parametrize("version", ["9.0", "9.0.0", "9.1.0", "9.1.0.0.25318225"])
def test_9x_target_resolves_nsx_rest_9(version: str) -> None:
    target = _FakeTarget(product="nsx", fingerprint=_FakeFingerprint(version))
    assert resolve_connector(target) is NsxConnector


def test_unversioned_target_resolves_via_9x_wildcard() -> None:
    target = _FakeTarget(product="nsx", fingerprint=None, version=None)
    assert resolve_connector(target) is NsxConnector


def test_operator_asserted_version_picks_the_band() -> None:
    """A target with no fingerprint but an operator-set version uses that version."""
    assert resolve_connector(_FakeTarget(product="nsx", version="4.1")) is Nsx4Connector
    assert resolve_connector(_FakeTarget(product="nsx", version="9.1")) is NsxConnector


def test_shared_preferred_impl_id_does_not_change_the_band() -> None:
    """Both classes share ``impl_id``; the preference cannot cross the band."""
    target = _FakeTarget(
        product="nsx", fingerprint=_FakeFingerprint("4.2.1"), preferred_impl_id="nsx-rest"
    )
    assert resolve_connector(target) is Nsx4Connector


# ---------------------------------------------------------------------------
# Typed reads on both implementations (database)
# ---------------------------------------------------------------------------


class _NsxTarget:
    def __init__(self, version: str) -> None:
        self.product = NSX_PRODUCT
        self.fingerprint = type("_FP", (), {"version": version})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
        self.name = "nsx-dual"
        self.host = _NSX_HOST
        self.port = 443
        self.secret_ref = "targets/dual/nsx-dual"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    return Operator(
        sub="op-dual-nsx",
        name="NSX Dual Operator",
        email=None,
        raw_jwt="op.dual.nsx.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000b4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _static_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "nsx-dual-svc", "password": "nsx-dual-pw"}


@pytest.mark.asyncio
async def test_every_typed_op_is_registered_on_both_versions(_stub_embedding: None) -> None:
    await register_nsx_typed_operations()
    await register_nsx4_typed_operations()

    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(EndpointDescriptor.version, EndpointDescriptor.op_id).where(
                    EndpointDescriptor.product == NSX_PRODUCT,
                    EndpointDescriptor.impl_id == NSX_IMPL_ID,
                    EndpointDescriptor.source_kind == "typed",
                )
            )
        ).all()
    expected = {op.op_id for op in NSX_TYPED_OPS}
    assert len(expected) == 10
    assert {op_id for version, op_id in rows if version == NSX_VERSION} == expected
    assert {op_id for version, op_id in rows if version == NSX4_VERSION} == expected


_OP_CALLS: list[tuple[str, dict[str, Any], str]] = [
    ("nsx.node.status", {}, "/api/v1/node"),
    ("nsx.cluster.status", {}, "/api/v1/cluster/status"),
    ("nsx.backup.config", {}, "/api/v1/cluster/backups/config"),
    ("nsx.backup.status", {}, "/api/v1/cluster/backups/status"),
    (
        "nsx.transport_zone.list",
        {},
        "/policy/api/v1/infra/sites/default/enforcement-points/default/transport-zones",
    ),
    ("nsx.tier1.list", {}, "/policy/api/v1/infra/tier-1s"),
    ("nsx.segment.list", {}, "/policy/api/v1/infra/segments"),
    ("nsx.transport_node.list", {}, "/api/v1/transport-nodes"),
    ("nsx.transport_node.state", {"id": "tn-1"}, "/api/v1/transport-nodes/tn-1/state"),
    ("nsx.alarm.list", {}, "/api/v1/alarms"),
]


def test_op_call_table_covers_every_typed_op() -> None:
    assert {op_id for op_id, _, _ in _OP_CALLS} == {op.op_id for op in NSX_TYPED_OPS}


@pytest.mark.parametrize(
    ("connector_id", "target_version", "expected_cls", "other_cls"),
    [
        (NSX4_CONNECTOR_ID, "4.2.1.0.0.24304122", Nsx4Connector, NsxConnector),
        (NSX4_CONNECTOR_ID, "4.1.2", Nsx4Connector, NsxConnector),
        (NSX_CONNECTOR_ID, "9.1.0.0.25318225", NsxConnector, Nsx4Connector),
        (NSX_CONNECTOR_ID, "9.0.0", NsxConnector, Nsx4Connector),
        # The dispatcher picks the class by the target's version, not by
        # the connector id the caller named.
        (NSX_CONNECTOR_ID, "4.2.1", Nsx4Connector, NsxConnector),
    ],
)
@pytest.mark.parametrize(("op_id", "params", "path"), _OP_CALLS)
@pytest.mark.asyncio
async def test_each_typed_op_dispatches_on_the_resolved_impl(
    _stub_embedding: None,
    connector_id: str,
    target_version: str,
    expected_cls: type[NsxConnector],
    other_cls: type[NsxConnector],
    op_id: str,
    params: dict[str, Any],
    path: str,
) -> None:
    await register_nsx_typed_operations()
    await register_nsx4_typed_operations()
    expected = get_or_create_connector_instance(expected_cls)
    other = get_or_create_connector_instance(other_cls)
    assert type(expected) is expected_cls
    expected._session_loader = _static_loader  # type: ignore[attr-defined]

    async with respx.mock(base_url=f"https://{_NSX_HOST}", assert_all_called=False) as mock:
        mock.post("/api/session/create").respond(200, headers=_XSRF_HEADERS)
        route = mock.get(path).respond(200, json={"results": [], "result_count": 0})
        result = await dispatch(
            operator=_make_operator(),
            connector_id=connector_id,
            op_id=op_id,
            target=_NsxTarget(target_version),
            params=params,
        )

    assert result.status == "ok", result.error
    assert route.call_count == 1
    # The session was opened on the expected class's instance only.
    assert expected._session_tokens  # type: ignore[attr-defined]
    assert not other._session_tokens  # type: ignore[attr-defined]
