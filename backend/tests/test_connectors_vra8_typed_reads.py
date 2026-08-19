# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the vRA 8 typed read core (#3058, initiative #3056).

The appliance-free proof that a vRealize Automation 8 target dispatches a
migration-inventory read end-to-end on a fresh boot. Coverage:

* **Zero-catalog typed dispatch.** Each of the 6 reads dispatches through
  :func:`~meho_backplane.operations.dispatch` against a respx-mocked vRA 8 (the
  two-step login + the data GET) with **only** the typed registrar run (no ingested
  rows) and returns ``status="ok"``. The list reads hit their service path; the
  by-id read templates the deployment id into the path.
* **Session mint on the dispatch path.** The read rides
  :meth:`Vra8Connector.auth_headers`, minting the bearer from the two-step exchange
  and sending it as ``Authorization: Bearer`` with ``Accept: application/json``.
* **Data-path 401 self-heal.** A 401 on the data read re-mints via the dispatcher's
  ``invalidate_session`` arm and retries once (the load-bearing #2067 proof).
* **Registration-shape invariants.** All 6 reads persist as ``source_kind="typed"``,
  ``is_enabled=True``, ``safe`` / ungated / ``read-only``, with a canonical-key
  ``llm_instructions`` blob; their 2 groups land ``review_status="enabled"``; no
  write op is registered.

Mirrors :mod:`tests.test_connectors_vcd_typed_reads`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vra8 import (
    VRA8_CONNECTOR_ID,
    VRA8_IMPL_ID,
    VRA8_PRODUCT,
    VRA8_TYPED_OPS,
    VRA8_TYPED_WHEN_TO_USE_BY_GROUP,
    VRA8_VERSION,
    Vra8Connector,
    register_vra8_typed_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.settings import get_settings

_VRA_HOST = "vra-typed.test.invalid"
_VRA_BASE_URL = f"https://{_VRA_HOST}"
_CSP_LOGIN_PATH = "/csp/gateway/am/api/login"
_IAAS_LOGIN_PATH = "/iaas/api/login"

_EXPECTED_OP_IDS = {
    "vcfa.vra8.project.list",
    "vcfa.vra8.deployment.list",
    "vcfa.vra8.deployment.get",
    "vcfa.vra8.blueprint.list",
    "vcfa.vra8.catalog-item.list",
    "vcfa.vra8.about",
}

_EXPECTED_GROUPS = {"vcfa-vra8-inventory", "vcfa-vra8-content"}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads (Vault client + dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher/handler caches + connector registry around every test."""
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=VRA8_PRODUCT,
        version=VRA8_VERSION,
        impl_id=VRA8_IMPL_ID,
        cls=Vra8Connector,
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Deterministic embedding stub so registration/search don't pull ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384

    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        lambda: service,
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """AsyncSession against the autouse-migrated per-worker SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class _Vra8ReadTarget:
    """Target satisfying both ``Vra8TargetLike`` and the resolver shape.

    ``fingerprint.version="8.12"`` is in the ``vcfa-vra8`` band ``">=8.0,<9.0"``, so
    a vRA 8 target resolves to this legacy impl.
    """

    def __init__(self) -> None:
        self.product = VRA8_PRODUCT
        self.fingerprint = type("_FP", (), {"version": "8.12"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000000e0")
        self.name = "vra-typed"
        self.host = _VRA_HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/vra-typed"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {}


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-reads-vra",
        name="VRA Reads Operator",
        email=None,
        raw_jwt="op.reads.vra.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000e4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """Username/password for the two-step login."""
    return {"username": "vra-svc", "password": "vra-pw"}


async def _register_and_resolve() -> Vra8Connector:
    """Run the typed registrar (only) and return a credentials-stubbed instance."""
    await register_vra8_typed_operations()
    instance = get_or_create_connector_instance(Vra8Connector)
    instance._credentials_loader = _stub_loader  # type: ignore[attr-defined]
    return instance


# ---------------------------------------------------------------------------
# Zero-catalog typed dispatch
# ---------------------------------------------------------------------------

# (op_id, wire path, params, payload)
_DISPATCH_CASES: list[tuple[str, str, dict[str, Any], Any]] = [
    ("vcfa.vra8.project.list", "/iaas/api/projects", {}, {"content": [{"name": "proj-a"}]}),
    (
        "vcfa.vra8.deployment.list",
        "/deployment/api/deployments",
        {},
        {"content": [{"id": "dep-a"}], "totalElements": 1},
    ),
    (
        "vcfa.vra8.deployment.get",
        "/deployment/api/deployments/dep-123",
        {"deployment_id": "dep-123"},
        {"id": "dep-123", "inprogressRequests": []},
    ),
    ("vcfa.vra8.blueprint.list", "/blueprint/api/blueprints", {}, {"content": [{"id": "bp-a"}]}),
    (
        "vcfa.vra8.catalog-item.list",
        "/catalog/api/admin/items",
        {},
        {"content": [{"id": "ci-a"}]},
    ),
    ("vcfa.vra8.about", "/iaas/api/about", {}, {"latestApiVersion": "2021-07-15"}),
]


@pytest.mark.parametrize(("op_id", "path", "params", "payload"), _DISPATCH_CASES, ids=lambda v: v)
@pytest.mark.asyncio
async def test_each_typed_op_dispatches_zero_catalog(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    path: str,
    params: dict[str, Any],
    payload: Any,
) -> None:
    """Each read dispatches typed (zero ingested catalog), minting the two-step session."""
    await _register_and_resolve()

    async with respx.mock(base_url=_VRA_BASE_URL, assert_all_called=False) as mock:
        csp = mock.post(_CSP_LOGIN_PATH).respond(200, json={"refresh_token": "rt"})
        iaas = mock.post(_IAAS_LOGIN_PATH).respond(200, json={"token": "disp-bt"})
        route = mock.get(path).respond(200, json=payload)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=VRA8_CONNECTOR_ID,
            op_id=op_id,
            target=_Vra8ReadTarget(),
            params=params,
        )

    assert result.status == "ok", result.error
    assert result.result == payload
    assert csp.called and iaas.called
    assert route.called and route.call_count == 1
    data_req = route.calls[0].request
    # The minted bearer + the constant JSON Accept rode the data call.
    assert data_req.headers["Authorization"] == "Bearer disp-bt"
    assert data_req.headers["Accept"] == "application/json"


@pytest.mark.asyncio
async def test_data_path_401_remints_via_dispatcher(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """A data-path 401 (expired bearer) self-heals via the dispatcher's session-recovery arm.

    The load-bearing 401-recovery proof — the coverage the isolated eviction tests
    can't give. A stale bearer gets a 401 on the data read; the dispatcher evicts it
    via the connector's ``invalidate_session`` hook and re-dispatches once; the retry
    re-runs the two-step login and the op succeeds. Without ``invalidate_session`` on
    the connector this would fall through to ``connector_auth_failed`` and wedge on
    the stale token (dispatcher #2067) — so a green here proves the hook is wired to
    the arm the 401 path actually calls, not the establish-only
    ``invalidate_credentials``.
    """
    await _register_and_resolve()

    async with respx.mock(base_url=_VRA_BASE_URL) as mock:
        csp = mock.post(_CSP_LOGIN_PATH).mock(
            side_effect=[
                httpx.Response(200, json={"refresh_token": "rt-1"}),
                httpx.Response(200, json={"refresh_token": "rt-2"}),
            ]
        )
        iaas = mock.post(_IAAS_LOGIN_PATH).mock(
            side_effect=[
                httpx.Response(200, json={"token": "bt-stale"}),
                httpx.Response(200, json={"token": "bt-fresh"}),
            ]
        )
        projects = mock.get("/iaas/api/projects").mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json={"content": [{"name": "proj-a"}]}),
            ]
        )
        result = await dispatch(
            operator=_make_operator(),
            connector_id=VRA8_CONNECTOR_ID,
            op_id="vcfa.vra8.project.list",
            target=_Vra8ReadTarget(),
            params={},
        )

    assert result.status == "ok", result.error
    assert result.result == {"content": [{"name": "proj-a"}]}
    # Minted once, 401'd, re-minted (both steps), retried once → 2 logins each + 2 GETs.
    assert csp.call_count == 2
    assert iaas.call_count == 2
    assert projects.call_count == 2
    # The first data GET carried the stale bearer; the retry carried the fresh one.
    assert projects.calls[0].request.headers["Authorization"] == "Bearer bt-stale"
    assert projects.calls[1].request.headers["Authorization"] == "Bearer bt-fresh"


# ---------------------------------------------------------------------------
# Registration-shape invariants — the "merge = fix" property
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_ops_are_typed_and_enabled(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """The 6 reads persist as source_kind='typed', is_enabled=True (live on fresh boot)."""
    await register_vra8_typed_operations()

    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == VRA8_PRODUCT,
                    EndpointDescriptor.impl_id == VRA8_IMPL_ID,
                    EndpointDescriptor.op_id.in_(list(_EXPECTED_OP_IDS)),
                )
            )
        )
        .scalars()
        .all()
    )
    assert {r.op_id for r in rows} == _EXPECTED_OP_IDS
    assert all(r.source_kind == "typed" for r in rows)
    assert all(r.is_enabled is True for r in rows)
    assert all(r.handler_ref is not None for r in rows)
    # Typed ops carry no wire method/path on the descriptor (path lives in the handler).
    assert all(r.method is None and r.path is None for r in rows)


@pytest.mark.asyncio
async def test_registered_groups_are_enabled(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """The 2 read-core groups land review_status='enabled' with a curated when_to_use."""
    await register_vra8_typed_operations()

    rows = (
        (
            await session.execute(
                select(OperationGroup).where(
                    OperationGroup.product == VRA8_PRODUCT,
                    OperationGroup.impl_id == VRA8_IMPL_ID,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {g.group_key for g in rows} == _EXPECTED_GROUPS
    assert all(g.review_status == "enabled" for g in rows)
    assert all(g.when_to_use.strip() for g in rows)


@pytest.mark.asyncio
async def test_registered_ops_are_visible_to_search_operations(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """The registered typed reads are retrievable via search_operations."""
    await register_vra8_typed_operations()

    result = await search_operations(
        _make_operator(),
        {
            "connector_id": VRA8_CONNECTOR_ID,
            "query": "vra vrealize automation 8 project deployment blueprint catalog inventory",
            "limit": 25,
        },
    )
    found = {hit["op_id"] for hit in result["hits"]}
    assert found >= _EXPECTED_OP_IDS, f"missing from search: {_EXPECTED_OP_IDS - found}"


# ---------------------------------------------------------------------------
# Pure-dataclass invariants (no DB, no async)
# ---------------------------------------------------------------------------


def test_typed_ops_table_is_exactly_the_read_core() -> None:
    assert {op.op_id for op in VRA8_TYPED_OPS} == _EXPECTED_OP_IDS
    assert len(VRA8_TYPED_OPS) == 6


def test_every_group_key_has_a_when_to_use() -> None:
    assert set(VRA8_TYPED_WHEN_TO_USE_BY_GROUP) == _EXPECTED_GROUPS
    used_groups = {op.group_key for op in VRA8_TYPED_OPS}
    assert used_groups == _EXPECTED_GROUPS


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_is_safe_no_approval_and_read_only(op_id: str) -> None:
    op = next(o for o in VRA8_TYPED_OPS if o.op_id == op_id)
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert "read-only" in op.tags


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_has_llm_instructions_with_canonical_keys(op_id: str) -> None:
    op = next(o for o in VRA8_TYPED_OPS if o.op_id == op_id)
    assert op.llm_instructions is not None
    for key in ("when_to_call", "output_shape", "next_step"):
        value = op.llm_instructions.get(key)
        assert isinstance(value, str) and value.strip(), f"{op_id}: {key}"


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_parameter_schema_disallows_additional_properties(op_id: str) -> None:
    op = next(o for o in VRA8_TYPED_OPS if o.op_id == op_id)
    assert op.parameter_schema.get("additionalProperties") is False


def test_deployment_get_requires_deployment_id() -> None:
    """The only by-id read declares deployment_id required; the rest take no required param."""
    get_op = next(o for o in VRA8_TYPED_OPS if o.op_id == "vcfa.vra8.deployment.get")
    assert get_op.parameter_schema.get("required") == ["deployment_id"]
    for op in VRA8_TYPED_OPS:
        if op.op_id != "vcfa.vra8.deployment.get":
            assert "required" not in op.parameter_schema or op.parameter_schema["required"] == []


def test_no_write_or_mutating_op_is_registered() -> None:
    """Read-only core: no create/write op ships here."""
    for op in VRA8_TYPED_OPS:
        assert not any(
            token in op.op_id for token in (".create", ".delete", ".update", ".set", ".put")
        )
        assert "write" not in op.tags
