# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the VCF 5.x SDDC Manager typed read core (#3059, initiative #3056).

The appliance-free proof that a VCF 5.x SDDC Manager target dispatches a
migration-inventory read end-to-end on a fresh boot. Coverage:

* **Zero-catalog typed dispatch.** Each of the 7 reads dispatches through
  :func:`~meho_backplane.operations.dispatch` against a respx-mocked SDDC Manager (the
  ``POST /v1/tokens`` login + the data GET) with **only** the typed registrar run and
  returns ``status="ok"``, carrying the minted Bearer.
* **Data-path 401 self-heal.** A 401 on the data read re-mints via the dispatcher's
  ``invalidate_session`` arm and retries once (the load-bearing #2067 proof).
* **Registration-shape invariants.** All 7 reads persist as ``source_kind="typed"``,
  ``is_enabled=True``, ``safe`` / ungated / ``read-only``; their 2 groups land
  ``review_status="enabled"``; no write op is registered.

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
from meho_backplane.connectors.sddc_vcf5 import (
    SDDC_VCF5_CONNECTOR_ID,
    SDDC_VCF5_IMPL_ID,
    SDDC_VCF5_PRODUCT,
    SDDC_VCF5_TYPED_OPS,
    SDDC_VCF5_TYPED_WHEN_TO_USE_BY_GROUP,
    SDDC_VCF5_VERSION,
    SddcVcf5Connector,
    register_sddc_vcf5_typed_operations,
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

_SDDC_HOST = "sddc-typed.test.invalid"
_SDDC_BASE_URL = f"https://{_SDDC_HOST}"
_TOKENS_PATH = "/v1/tokens"

_EXPECTED_OP_IDS = {
    "sddc.vcf5.domain.list",
    "sddc.vcf5.cluster.list",
    "sddc.vcf5.host.list",
    "sddc.vcf5.vcenter.list",
    "sddc.vcf5.nsxt_cluster.list",
    "sddc.vcf5.manager.list",
    "sddc.vcf5.task.list",
}

_EXPECTED_GROUPS = {"sddc-vcf5-inventory", "sddc-vcf5-tasks"}


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
        product=SDDC_VCF5_PRODUCT,
        version=SDDC_VCF5_VERSION,
        impl_id=SDDC_VCF5_IMPL_ID,
        cls=SddcVcf5Connector,
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


class _Sddc5ReadTarget:
    """Target satisfying both ``SddcTargetLike`` and the resolver shape.

    ``fingerprint.version="5.2.0.0-24276214"`` is in the ``sddc-vcf5`` band
    ``">=5.0,<9.0"``, so a VCF 5.x target resolves to this legacy impl.
    """

    def __init__(self) -> None:
        self.product = SDDC_VCF5_PRODUCT
        self.fingerprint = type("_FP", (), {"version": "5.2.0.0-24276214"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000000f0")
        self.name = "sddc-typed"
        self.host = _SDDC_HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/sddc-typed"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {}


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-reads-sddc",
        name="SDDC Reads Operator",
        email=None,
        raw_jwt="op.reads.sddc.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000f4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """Username/password for the token login."""
    return {"username": "sddc-svc", "password": "sddc-pw"}


async def _register_and_resolve() -> SddcVcf5Connector:
    """Run the typed registrar (only) and return a credentials-stubbed instance."""
    await register_sddc_vcf5_typed_operations()
    instance = get_or_create_connector_instance(SddcVcf5Connector)
    instance._credentials_loader = _stub_loader  # type: ignore[attr-defined]
    return instance


# ---------------------------------------------------------------------------
# Zero-catalog typed dispatch
# ---------------------------------------------------------------------------

# (op_id, wire path, payload)
_DISPATCH_CASES: list[tuple[str, str, Any]] = [
    ("sddc.vcf5.domain.list", "/v1/domains", {"elements": [{"id": "d1", "name": "MGMT"}]}),
    ("sddc.vcf5.cluster.list", "/v1/clusters", {"elements": [{"id": "c1"}]}),
    ("sddc.vcf5.host.list", "/v1/hosts", {"elements": [{"id": "h1"}]}),
    ("sddc.vcf5.vcenter.list", "/v1/vcenters", {"elements": [{"id": "vc1"}]}),
    ("sddc.vcf5.nsxt_cluster.list", "/v1/nsxt-clusters", {"elements": [{"id": "n1"}]}),
    ("sddc.vcf5.manager.list", "/v1/sddc-managers", {"elements": [{"version": "5.2.0.0"}]}),
    ("sddc.vcf5.task.list", "/v1/tasks", {"elements": [{"id": "t1", "status": "Successful"}]}),
]


@pytest.mark.parametrize(("op_id", "path", "payload"), _DISPATCH_CASES, ids=lambda v: v)
@pytest.mark.asyncio
async def test_each_typed_op_dispatches_zero_catalog(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    path: str,
    payload: Any,
) -> None:
    """Each read dispatches typed (zero ingested catalog), minting the token session."""
    await _register_and_resolve()

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        tokens = mock.post(_TOKENS_PATH).respond(200, json={"accessToken": "disp-tok"})
        route = mock.get(path).respond(200, json=payload)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=SDDC_VCF5_CONNECTOR_ID,
            op_id=op_id,
            target=_Sddc5ReadTarget(),
            params={},
        )

    assert result.status == "ok", result.error
    assert result.result == payload
    assert tokens.called
    assert route.called and route.call_count == 1
    assert route.calls[0].request.headers["Authorization"] == "Bearer disp-tok"


@pytest.mark.asyncio
async def test_data_path_401_remints_via_dispatcher(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """A data-path 401 (expired token) self-heals via the dispatcher's session-recovery arm.

    A stale token gets a 401 on the data read; the dispatcher evicts it via the
    connector's ``invalidate_session`` hook and re-dispatches once; the retry re-mints
    a fresh token and the op succeeds. Without ``invalidate_session`` this would fall
    through to ``connector_auth_failed`` and wedge (dispatcher #2067) — so a green here
    proves the hook is wired to the arm the 401 path actually calls, not the
    establish-only ``invalidate_credentials``.
    """
    await _register_and_resolve()

    async with respx.mock(base_url=_SDDC_BASE_URL) as mock:
        tokens = mock.post(_TOKENS_PATH).mock(
            side_effect=[
                httpx.Response(200, json={"accessToken": "tok-stale"}),
                httpx.Response(200, json={"accessToken": "tok-fresh"}),
            ]
        )
        domains = mock.get("/v1/domains").mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json={"elements": [{"id": "d1"}]}),
            ]
        )
        result = await dispatch(
            operator=_make_operator(),
            connector_id=SDDC_VCF5_CONNECTOR_ID,
            op_id="sddc.vcf5.domain.list",
            target=_Sddc5ReadTarget(),
            params={},
        )

    assert result.status == "ok", result.error
    assert result.result == {"elements": [{"id": "d1"}]}
    assert tokens.call_count == 2
    assert domains.call_count == 2
    assert domains.calls[0].request.headers["Authorization"] == "Bearer tok-stale"
    assert domains.calls[1].request.headers["Authorization"] == "Bearer tok-fresh"


@pytest.mark.asyncio
async def test_host_list_forwards_optional_filters(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """``sddc.vcf5.host.list`` forwards the optional domainId / clusterId / status filters."""
    await _register_and_resolve()

    async with respx.mock(base_url=_SDDC_BASE_URL, assert_all_called=False) as mock:
        mock.post(_TOKENS_PATH).respond(200, json={"accessToken": "tok"})
        hosts = mock.get("/v1/hosts").respond(200, json={"elements": []})
        await dispatch(
            operator=_make_operator(),
            connector_id=SDDC_VCF5_CONNECTOR_ID,
            op_id="sddc.vcf5.host.list",
            target=_Sddc5ReadTarget(),
            params={"domainId": "dom-1", "status": "ASSIGNED"},
        )

    req = hosts.calls[0].request
    assert req.url.params["domainId"] == "dom-1"
    assert req.url.params["status"] == "ASSIGNED"
    assert "clusterId" not in req.url.params


# ---------------------------------------------------------------------------
# Registration-shape invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_ops_are_typed_and_enabled(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """The 7 reads persist as source_kind='typed', is_enabled=True (live on fresh boot)."""
    await register_sddc_vcf5_typed_operations()

    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == SDDC_VCF5_PRODUCT,
                    EndpointDescriptor.impl_id == SDDC_VCF5_IMPL_ID,
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
    assert all(r.method is None and r.path is None for r in rows)


@pytest.mark.asyncio
async def test_registered_groups_are_enabled(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """The 2 read-core groups land review_status='enabled' with a curated when_to_use."""
    await register_sddc_vcf5_typed_operations()

    rows = (
        (
            await session.execute(
                select(OperationGroup).where(
                    OperationGroup.product == SDDC_VCF5_PRODUCT,
                    OperationGroup.impl_id == SDDC_VCF5_IMPL_ID,
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
    await register_sddc_vcf5_typed_operations()

    result = await search_operations(
        _make_operator(),
        {
            "connector_id": SDDC_VCF5_CONNECTOR_ID,
            "query": "vcf sddc manager domain cluster host vcenter nsxt inventory migration",
            "limit": 25,
        },
    )
    found = {hit["op_id"] for hit in result["hits"]}
    assert found >= _EXPECTED_OP_IDS, f"missing from search: {_EXPECTED_OP_IDS - found}"


# ---------------------------------------------------------------------------
# Pure-dataclass invariants (no DB, no async)
# ---------------------------------------------------------------------------


def test_typed_ops_table_is_exactly_the_read_core() -> None:
    assert {op.op_id for op in SDDC_VCF5_TYPED_OPS} == _EXPECTED_OP_IDS
    assert len(SDDC_VCF5_TYPED_OPS) == 7


def test_every_group_key_has_a_when_to_use() -> None:
    assert set(SDDC_VCF5_TYPED_WHEN_TO_USE_BY_GROUP) == _EXPECTED_GROUPS
    used_groups = {op.group_key for op in SDDC_VCF5_TYPED_OPS}
    assert used_groups == _EXPECTED_GROUPS


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_is_safe_no_approval_and_read_only(op_id: str) -> None:
    op = next(o for o in SDDC_VCF5_TYPED_OPS if o.op_id == op_id)
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert "read-only" in op.tags


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_has_llm_instructions_with_canonical_keys(op_id: str) -> None:
    op = next(o for o in SDDC_VCF5_TYPED_OPS if o.op_id == op_id)
    assert op.llm_instructions is not None
    for key in ("when_to_use", "output_shape"):
        value = op.llm_instructions.get(key)
        assert isinstance(value, str) and value.strip(), f"{op_id}: {key}"


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_parameter_schema_disallows_additional_properties(op_id: str) -> None:
    op = next(o for o in SDDC_VCF5_TYPED_OPS if o.op_id == op_id)
    assert op.parameter_schema.get("additionalProperties") is False


def test_no_write_or_mutating_op_is_registered() -> None:
    """Read-only core: no create/write op and no credential-read op ships here."""
    for op in SDDC_VCF5_TYPED_OPS:
        assert not any(
            token in op.op_id for token in (".create", ".delete", ".update", ".set", ".put")
        )
        assert "write" not in op.tags
        assert "credential-read" not in op.tags
