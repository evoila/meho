# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the vCD typed read core (#3057, initiative #3056).

The appliance-free proof that a VMware Cloud Director target dispatches a
migration-inventory read end-to-end on a fresh boot. Coverage:

* **Zero-catalog typed dispatch.** Each of the 7 reads dispatches through
  :func:`~meho_backplane.operations.dispatch` against a respx-mocked vCD (the
  provider login POST + the data GET) with **only** the typed registrar run (no
  ingested rows) and returns ``status="ok"``. The query-service reads carry the
  right ``type=`` selector; the modern org read hits ``/cloudapi/1.0.0/orgs``.
* **Session mint on the dispatch path.** The read rides
  :meth:`VcloudDirectorConnector.auth_headers`, minting the provider Bearer JWT
  from the ``X-VMWARE-VCLOUD-ACCESS-TOKEN`` login-response header and sending it
  as ``Authorization: Bearer`` with the per-surface ``Accept``.
* **Registration-shape invariants.** All 7 reads persist as
  ``source_kind="typed"``, ``is_enabled=True`` (enabled at register time — the
  "merge = fix" property), ``safe`` / ungated / ``read-only``, with a
  canonical-key ``llm_instructions`` blob; their 3 groups land
  ``review_status="enabled"``; no write op is registered.

Mirrors :mod:`tests.test_connectors_fleet_lcm_typed_reads`; the credential seam
replaces the resolved instance's ``_credentials_loader`` (vCD mints a token from
the loader creds, the ``vcf-automation`` shape), not a ``CredentialsCache``.
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
from meho_backplane.connectors.vcd import (
    VCD_CONNECTOR_ID,
    VCD_IMPL_ID,
    VCD_PRODUCT,
    VCD_TYPED_OPS,
    VCD_TYPED_WHEN_TO_USE_BY_GROUP,
    VCD_VERSION,
    VcloudDirectorConnector,
    register_vcd_typed_operations,
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

_VCD_HOST = "vcd-typed.test.invalid"
_VCD_BASE_URL = f"https://{_VCD_HOST}"
_LOGIN_PATH = "/cloudapi/1.0.0/sessions/provider"
_TOKEN_HEADER = "X-VMWARE-VCLOUD-ACCESS-TOKEN"

_EXPECTED_OP_IDS = {
    "vcd.org.list",
    "vcd.vdc.list",
    "vcd.vapp.list",
    "vcd.vm.list",
    "vcd.catalog.list",
    "vcd.edge-gateway.list",
    "vcd.task.list",
}

_EXPECTED_GROUPS = {"vcd-inventory", "vcd-networking", "vcd-tasks"}


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
        product=VCD_PRODUCT,
        version=VCD_VERSION,
        impl_id=VCD_IMPL_ID,
        cls=VcloudDirectorConnector,
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


class _VcdReadTarget:
    """Target satisfying both ``VcloudDirectorTargetLike`` and the resolver shape.

    ``fingerprint.version="10.6.1"`` is in the ``vcd-rest`` band ``">=10.0,<11.0"``,
    so a vCD target resolves to this impl (single-impl; the versioned entry or the
    wildcard both point at it).
    """

    def __init__(self) -> None:
        self.product = VCD_PRODUCT
        self.fingerprint = type("_FP", (), {"version": "10.6.1"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000000d0")
        self.name = "vcd-typed"
        self.host = _VCD_HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/vcd-typed"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {}


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-reads-vcd",
        name="VCD Reads Operator",
        email=None,
        raw_jwt="op.reads.vcd.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """Username/password for the provider Basic login."""
    return {"username": "vcd-svc", "password": "vcd-pw"}


async def _register_and_resolve() -> VcloudDirectorConnector:
    """Run the typed registrar (only) and return a credentials-stubbed instance."""
    await register_vcd_typed_operations()
    instance = get_or_create_connector_instance(VcloudDirectorConnector)
    instance._credentials_loader = _stub_loader  # type: ignore[attr-defined]
    return instance


# ---------------------------------------------------------------------------
# Zero-catalog typed dispatch
# ---------------------------------------------------------------------------

# (op_id, wire path, expected query ``type`` (None for the cloudapi org read), payload)
_DISPATCH_CASES: list[tuple[str, str, str | None, Any]] = [
    ("vcd.org.list", "/cloudapi/1.0.0/orgs", None, {"values": [{"name": "System"}]}),
    ("vcd.vdc.list", "/api/query", "adminOrgVdc", {"record": [{"name": "vdc-a"}]}),
    ("vcd.vapp.list", "/api/query", "adminVApp", {"record": [{"name": "vapp-a"}]}),
    ("vcd.vm.list", "/api/query", "adminVM", {"record": [{"name": "vm-a"}]}),
    ("vcd.catalog.list", "/api/query", "adminCatalog", {"record": [{"name": "cat-a"}]}),
    ("vcd.edge-gateway.list", "/api/query", "edgeGateway", {"record": [{"name": "edge-a"}]}),
    ("vcd.task.list", "/api/query", "adminTask", {"record": [{"name": "task-a"}]}),
]


@pytest.mark.parametrize(
    ("op_id", "path", "query_type", "payload"), _DISPATCH_CASES, ids=lambda v: v
)
@pytest.mark.asyncio
async def test_each_typed_op_dispatches_zero_catalog(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    path: str,
    query_type: str | None,
    payload: Any,
) -> None:
    """Each read dispatches typed (zero ingested catalog), minting the provider session."""
    await _register_and_resolve()

    async with respx.mock(base_url=_VCD_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_LOGIN_PATH).respond(200, headers={_TOKEN_HEADER: "disp-jwt"})
        route = mock.get(path).respond(200, json=payload)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=VCD_CONNECTOR_ID,
            op_id=op_id,
            target=_VcdReadTarget(),
            params={},
        )

    assert result.status == "ok", result.error
    assert result.result == payload
    assert login.called
    assert route.called and route.call_count == 1
    data_req = route.calls[0].request
    # The minted provider Bearer + the per-surface Accept rode the data call.
    assert data_req.headers["Authorization"] == "Bearer disp-jwt"
    if path.startswith("/api/"):
        assert data_req.headers["Accept"] == "application/*+json;version=38.0"
        assert data_req.url.params["type"] == query_type
        assert data_req.url.params["format"] == "records"
    else:
        assert data_req.headers["Accept"] == "application/json;version=38.0"


@pytest.mark.asyncio
async def test_data_path_401_remints_via_dispatcher(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """A data-path 401 (expired JWT) self-heals via the dispatcher's session-recovery arm.

    The load-bearing 401-recovery proof — the coverage the isolated eviction
    tests can't give. A stale minted JWT gets a 401 on the data read; the
    dispatcher evicts it via the connector's ``invalidate_session`` hook and
    re-dispatches once; the retry re-mints a fresh session and the op succeeds.
    Without ``invalidate_session`` on the connector this would fall through to
    ``connector_auth_failed`` and wedge on the stale token (dispatcher #2067) —
    so a green here proves the hook is wired to the arm the 401 path actually
    calls, not the establish-only ``invalidate_credentials``.
    """
    await _register_and_resolve()

    async with respx.mock(base_url=_VCD_BASE_URL) as mock:
        login = mock.post(_LOGIN_PATH).mock(
            side_effect=[
                httpx.Response(200, headers={_TOKEN_HEADER: "jwt-stale"}),
                httpx.Response(200, headers={_TOKEN_HEADER: "jwt-fresh"}),
            ]
        )
        orgs = mock.get("/cloudapi/1.0.0/orgs").mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json={"values": [{"name": "System"}]}),
            ]
        )
        result = await dispatch(
            operator=_make_operator(),
            connector_id=VCD_CONNECTOR_ID,
            op_id="vcd.org.list",
            target=_VcdReadTarget(),
            params={},
        )

    assert result.status == "ok", result.error
    assert result.result == {"values": [{"name": "System"}]}
    # Minted once, 401'd, re-minted, retried once → exactly 2 logins + 2 data GETs.
    assert login.call_count == 2
    assert orgs.call_count == 2
    # The first data GET carried the stale token; the retry carried the fresh one.
    assert orgs.calls[0].request.headers["Authorization"] == "Bearer jwt-stale"
    assert orgs.calls[1].request.headers["Authorization"] == "Bearer jwt-fresh"


# ---------------------------------------------------------------------------
# Registration-shape invariants — the "merge = fix" property
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_ops_are_typed_and_enabled(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """The 7 reads persist as source_kind='typed', is_enabled=True (live on fresh boot)."""
    await register_vcd_typed_operations()

    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == VCD_PRODUCT,
                    EndpointDescriptor.impl_id == VCD_IMPL_ID,
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
    """The 3 read-core groups land review_status='enabled' with a curated when_to_use."""
    await register_vcd_typed_operations()

    rows = (
        (
            await session.execute(
                select(OperationGroup).where(
                    OperationGroup.product == VCD_PRODUCT,
                    OperationGroup.impl_id == VCD_IMPL_ID,
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
    await register_vcd_typed_operations()

    result = await search_operations(
        _make_operator(),
        {
            "connector_id": VCD_CONNECTOR_ID,
            "query": "vcd cloud director org vdc vapp vm catalog edge gateway task inventory",
            "limit": 25,
        },
    )
    found = {hit["op_id"] for hit in result["hits"]}
    assert found >= _EXPECTED_OP_IDS, f"missing from search: {_EXPECTED_OP_IDS - found}"


# ---------------------------------------------------------------------------
# Pure-dataclass invariants (no DB, no async)
# ---------------------------------------------------------------------------


def test_typed_ops_table_is_exactly_the_read_core() -> None:
    assert {op.op_id for op in VCD_TYPED_OPS} == _EXPECTED_OP_IDS
    assert len(VCD_TYPED_OPS) == 7


def test_every_group_key_has_a_when_to_use() -> None:
    assert set(VCD_TYPED_WHEN_TO_USE_BY_GROUP) == _EXPECTED_GROUPS
    used_groups = {op.group_key for op in VCD_TYPED_OPS}
    assert used_groups == _EXPECTED_GROUPS


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_is_safe_no_approval_and_read_only(op_id: str) -> None:
    op = next(o for o in VCD_TYPED_OPS if o.op_id == op_id)
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert "read-only" in op.tags


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_has_llm_instructions_with_canonical_keys(op_id: str) -> None:
    op = next(o for o in VCD_TYPED_OPS if o.op_id == op_id)
    assert op.llm_instructions is not None
    for key in ("when_to_call", "output_shape", "next_step"):
        value = op.llm_instructions.get(key)
        assert isinstance(value, str) and value.strip(), f"{op_id}: {key}"


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_parameter_schema_disallows_additional_properties(op_id: str) -> None:
    op = next(o for o in VCD_TYPED_OPS if o.op_id == op_id)
    assert op.parameter_schema.get("additionalProperties") is False


def test_no_write_or_mutating_op_is_registered() -> None:
    """Read-only core: no create/write op ships here."""
    for op in VCD_TYPED_OPS:
        assert not any(
            token in op.op_id for token in (".create", ".delete", ".update", ".set", ".put")
        )
        assert "write" not in op.tags
