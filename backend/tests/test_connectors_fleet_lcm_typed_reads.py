# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the modern fleet-lcm typed read core (#3047, initiative #3033).

This is the appliance-free proof of modern fleet dispatch: a VCF Fleet 9.1
or newer target resolves to ``fleet-lcm`` (``>=9.1,<10.0``) and
dispatches a modern ``/v1/*`` read op end-to-end. Coverage:

* **Zero-catalog typed dispatch.** Each of the 13 reads dispatches through
  :func:`~meho_backplane.operations.dispatch` against a respx-mocked Fleet
  LCM service with **only** the typed registrar run (no ingested rows) and
  returns ``status="ok"``. By-id ops interpolate ``{sddcLcmId}`` /
  ``{componentId}`` / ``{taskId}`` / ``{planId}`` into the path via
  ``fill_path`` + ``encode_segment``.
* **Both token sources (#4476).** The reads ride
  :meth:`FleetLcmConnector.auth_headers` per request — ``Authorization:
  Bearer <jwtToken>`` from the VCF Operations login (acquire, then
  exchange) when the credentials carry only username/password, and the
  stored ``token`` when they carry one.
* **Session renewal.** A ``401`` on a read makes the dispatcher call
  :meth:`FleetLcmConnector.invalidate_session`; the connector logs in again
  and the read runs once more. No token reaches the result, the error text
  or a log line.
* **Registration-shape invariants.** All 13 reads persist as
  ``source_kind="typed"``, ``is_enabled=True`` (enabled at register time —
  the "fresh boot, zero ingest" property), ``safe`` / ungated / ``read-only``,
  with a canonical-key ``llm_instructions`` blob; their 5 groups land
  ``review_status="enabled"``. The six lifecycle changes in the same table
  are tested in :mod:`tests.test_connectors_fleet_lcm_lifecycle`.

Mirrors :mod:`tests.test_connectors_harbor_typed_reads` for the
dispatch-lifecycle + embedding-stub plumbing; the credential seam replaces
the whole ``CredentialsCache`` on the resolved instance (the shared
VCF-family pattern the vcf_fleet e2e / typed-read tests use), since
``FleetLcmConnector`` holds its loader inside ``self._creds`` rather than a
bare ``_credentials_loader``.
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
from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.fleet_lcm import (
    FLEET_LCM_CONNECTOR_ID,
    FLEET_LCM_IMPL_ID,
    FLEET_LCM_PRODUCT,
    FLEET_LCM_TYPED_OPS,
    FLEET_LCM_TYPED_WHEN_TO_USE_BY_GROUP,
    FLEET_LCM_VERSION,
    FleetLcmConnector,
    register_fleet_lcm_typed_operations,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.meta_tools import search_operations
from meho_backplane.settings import get_settings

_FLEET_HOST = "fleet-lcm-typed.test.invalid"
_FLEET_BASE_URL = f"https://{_FLEET_HOST}"

_EXPECTED_OP_IDS = {
    "fleet-lcm.health",
    "fleet-lcm.system.info",
    "fleet-lcm.config.info",
    "fleet-lcm.sddc-lcm.list",
    "fleet-lcm.sddc-lcm.info",
    "fleet-lcm.component.list",
    "fleet-lcm.component.info",
    "fleet-lcm.component.status",
    "fleet-lcm.task.list",
    "fleet-lcm.task.info",
    "fleet-lcm.upgrade-plan.list",
    "fleet-lcm.upgrade-plan.info",
    "fleet-lcm.release-version.list",
}

_EXPECTED_GROUPS = {
    "fleet-lcm-system",
    "fleet-lcm-sddc",
    "fleet-lcm-components",
    "fleet-lcm-tasks",
    "fleet-lcm-lifecycle",
}


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
        product=FLEET_LCM_PRODUCT,
        version=FLEET_LCM_VERSION,
        impl_id=FLEET_LCM_IMPL_ID,
        cls=FleetLcmConnector,
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


class _FleetLcmReadTarget:
    """Target satisfying both ``FleetLcmTargetLike`` and the resolver shape.

    ``fingerprint.version="9.1.0"`` is in the ``fleet-lcm`` range
    ``">=9.1,<10.0"`` and outside the legacy ``fleet-rest`` range
    ``">=8.0,<9.1"``, so a fleet target resolves to this modern impl.
    """

    def __init__(self) -> None:
        self.product = FLEET_LCM_PRODUCT
        self.fingerprint = type("_FP", (), {"version": "9.1.0"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000000e0")
        self.name = "fleet-lcm-typed"
        self.host = _FLEET_HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/fleet-lcm-typed"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-reads-fleet-lcm",
        name="Fleet LCM Reads Operator",
        email=None,
        raw_jwt="op.reads.fleet-lcm.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000e4"),
        tenant_role=TenantRole.OPERATOR,
    )


_PASSWORD = "fleet-lcm-pw-must-not-leak"
_OPS_TOKEN = "ops-token-must-not-leak"
_JWT = "fleet-jwt-must-not-leak"


async def _login_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """Username/password only — the connector logs in to VCF Operations."""
    return {"username": "fleet-lcm-svc", "password": _PASSWORD}


def _mock_login(mock: respx.MockRouter) -> tuple[respx.Route, respx.Route]:
    """Mock the VCF Operations acquire + exchange answers on *mock*."""
    acquire = mock.post("/suite-api/api/auth/token/acquire").respond(
        200, json={"token": _OPS_TOKEN}
    )
    exchange = mock.post("/suite-api/api/auth/token/exchange").respond(
        200, json={"jwtToken": _JWT, "services": []}
    )
    return acquire, exchange


async def _bearer_loader(_target: object, _operator: Operator) -> dict[str, str]:
    """Adds a stored ``token`` — sent as is, no login."""
    return {
        "username": "fleet-lcm-svc",
        "password": _PASSWORD,
        "token": "fleet-lcm-bearer-tok",
    }


async def _register_and_resolve(loader: Any) -> FleetLcmConnector:
    """Run the typed registrar (only) and return a credentials-stubbed instance.

    ``FleetLcmConnector`` keeps its loader inside ``self._creds`` (a
    ``CredentialsCache``), so the seam replaces the whole cache with one wired
    to *loader* — the shared VCF-family injection the vcf_fleet e2e / typed
    tests use.
    """
    await register_fleet_lcm_typed_operations()
    instance = get_or_create_connector_instance(FleetLcmConnector)
    instance._creds = type(instance._creds)(  # type: ignore[attr-defined]
        loader,
        product_label="fleet-lcm",
    )
    return instance


# ---------------------------------------------------------------------------
# Zero-catalog typed dispatch
# ---------------------------------------------------------------------------

_DISPATCH_CASES: list[tuple[str, str, dict[str, Any], Any]] = [
    ("fleet-lcm.health", "/v1/health", {}, {"up": True}),
    (
        "fleet-lcm.system.info",
        "/v1/system",
        {},
        {"upgrade": {"taskId": "t-1", "status": "PENDING"}, "bundles": []},
    ),
    ("fleet-lcm.config.info", "/v1/config", {}, {"deploymentFlavor": "VCF", "status": "HEALTHY"}),
    (
        "fleet-lcm.sddc-lcm.list",
        "/v1/sddc-lcms",
        {},
        {"pageMetadata": {"totalElements": 1}, "sddcLcms": [{"fqdn": "sddc-a"}]},
    ),
    (
        "fleet-lcm.sddc-lcm.info",
        "/v1/sddc-lcms/sddc-1",
        {"sddcLcmId": "sddc-1"},
        {"fqdn": "sddc-a", "isPrimary": True},
    ),
    (
        "fleet-lcm.component.list",
        "/v1/components",
        {},
        {"components": [{"componentType": "OPS", "version": "9.0.0"}]},
    ),
    (
        "fleet-lcm.component.info",
        "/v1/components/comp-1",
        {"componentId": "comp-1"},
        {"componentType": "OPS", "fqdn": "ops-a"},
    ),
    (
        "fleet-lcm.component.status",
        "/v1/components/comp-1/status",
        {"componentId": "comp-1"},
        {"id": "comp-1", "status": "Running"},
    ),
    (
        "fleet-lcm.task.list",
        "/v1/tasks",
        {},
        {"elements": [{"resourceId": "r-1", "type": "UPGRADE"}], "pageMetadata": {}},
    ),
    (
        "fleet-lcm.task.info",
        "/v1/tasks/task-1",
        {"taskId": "task-1"},
        {"resourceId": "r-1", "type": "UPGRADE", "status": "COMPLETED"},
    ),
    (
        "fleet-lcm.upgrade-plan.list",
        "/v1/upgrade-plans",
        {},
        {"elements": [{"id": "plan-1"}], "pageMetadata": {}},
    ),
    (
        "fleet-lcm.upgrade-plan.info",
        "/v1/upgrade-plans/plan-1",
        {"planId": "plan-1"},
        {"id": "plan-1", "spec": {"componentsFilter": []}},
    ),
    (
        "fleet-lcm.release-version.list",
        "/v1/release-versions",
        {},
        {"elements": [{"version": "9.1.0"}], "pageMetadata": {}},
    ),
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
    """Each read dispatches typed (zero ingested catalog) with the login's Bearer token."""
    await _register_and_resolve(_login_loader)

    async with respx.mock(base_url=_FLEET_BASE_URL, assert_all_called=False) as mock:
        _mock_login(mock)
        route = mock.get(path).respond(200, json=payload)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=FLEET_LCM_CONNECTOR_ID,
            op_id=op_id,
            target=_FleetLcmReadTarget(),
            params=params,
        )

    assert result.status == "ok", result.error
    # task.info adds MEHO's state/progress/errors to the vendor task.
    assert {key: result.result[key] for key in payload} == payload
    assert route.called and route.call_count == 1
    assert route.calls[0].request.headers.get("authorization") == f"Bearer {_JWT}"


@pytest.mark.asyncio
async def test_dispatch_uses_bearer_when_token_present(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """When the loaded credentials carry a stored ``token``, the read sends it as is."""
    await _register_and_resolve(_bearer_loader)

    async with respx.mock(base_url=_FLEET_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/v1/components").respond(200, json={"components": []})
        result = await dispatch(
            operator=_make_operator(),
            connector_id=FLEET_LCM_CONNECTOR_ID,
            op_id="fleet-lcm.component.list",
            target=_FleetLcmReadTarget(),
            params={},
        )

    assert result.status == "ok", result.error
    sent_auth = route.calls[0].request.headers.get("authorization")
    assert sent_auth == "Bearer fleet-lcm-bearer-tok"


@pytest.mark.asyncio
async def test_by_id_op_percent_encodes_path_segment(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """A by-id param with a reserved char is percent-encoded (encode_segment), not injected."""
    await _register_and_resolve(_login_loader)

    async with respx.mock(base_url=_FLEET_BASE_URL, assert_all_called=False) as mock:
        _mock_login(mock)
        route = mock.get("/v1/components/a%2Fb/status").respond(200, json={"id": "a/b"})
        result = await dispatch(
            operator=_make_operator(),
            connector_id=FLEET_LCM_CONNECTOR_ID,
            op_id="fleet-lcm.component.status",
            target=_FleetLcmReadTarget(),
            params={"componentId": "a/b"},
        )

    assert result.status == "ok", result.error
    assert route.called and route.call_count == 1
    # The "/" is encoded to %2F — a reserved char can never leak path structure.
    assert route.calls[0].request.url.raw_path.decode().startswith("/v1/components/a%2Fb/status")


# ---------------------------------------------------------------------------
# Session renewal — 401 -> invalidate_session -> new login -> success (#4476)
# ---------------------------------------------------------------------------


def test_connector_advertises_public_invalidate_session() -> None:
    """The dispatcher's duck-typed renewal hook is present."""
    assert callable(getattr(FleetLcmConnector, "invalidate_session", None))


@pytest.mark.asyncio
async def test_dispatch_renews_the_token_after_a_401(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """A 401 on a read → invalidate_session → a new login → the read succeeds."""
    await _register_and_resolve(_login_loader)

    async with respx.mock(base_url=_FLEET_BASE_URL, assert_all_called=False) as mock:
        acquire, exchange = _mock_login(mock)
        exchange.side_effect = [
            httpx.Response(200, json={"jwtToken": "jwt-expired"}),
            httpx.Response(200, json={"jwtToken": "jwt-fresh"}),
        ]
        route = mock.get("/v1/components")
        route.side_effect = [
            httpx.Response(401),
            httpx.Response(200, json={"components": []}),
        ]
        result = await dispatch(
            operator=_make_operator(),
            connector_id=FLEET_LCM_CONNECTOR_ID,
            op_id="fleet-lcm.component.list",
            target=_FleetLcmReadTarget(),
            params={},
        )

    assert result.status == "ok", result.error
    assert result.result == {"components": []}
    assert acquire.call_count == 2 and exchange.call_count == 2
    sent = [call.request.headers.get("authorization") for call in route.calls]
    assert sent == ["Bearer jwt-expired", "Bearer jwt-fresh"]


@pytest.mark.asyncio
async def test_failed_renewal_shows_no_token_in_result_or_logs(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """A read refused twice ends as connector_auth_failed, with no secret anywhere."""
    await _register_and_resolve(_login_loader)

    with capture_logs() as captured:
        async with respx.mock(base_url=_FLEET_BASE_URL, assert_all_called=False) as mock:
            _mock_login(mock)
            mock.get("/v1/components").respond(401)
            result = await dispatch(
                operator=_make_operator(),
                connector_id=FLEET_LCM_CONNECTOR_ID,
                op_id="fleet-lcm.component.list",
                target=_FleetLcmReadTarget(),
                params={},
            )

    assert result.status == "error"
    assert result.error is not None and result.error.startswith("connector_auth_failed:")
    blob = result.model_dump_json() + repr(captured)
    for secret in (_PASSWORD, _OPS_TOKEN, _JWT):
        assert secret not in blob


@pytest.mark.asyncio
async def test_refused_login_shows_no_secret_in_result(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """A refused exchange ends as connector_auth_failed, with no secret in the result."""
    await _register_and_resolve(_login_loader)

    async with respx.mock(base_url=_FLEET_BASE_URL, assert_all_called=False) as mock:
        _acquire, exchange = _mock_login(mock)
        exchange.respond(401)
        result = await dispatch(
            operator=_make_operator(),
            connector_id=FLEET_LCM_CONNECTOR_ID,
            op_id="fleet-lcm.component.list",
            target=_FleetLcmReadTarget(),
            params={},
        )

    assert result.status == "error"
    assert result.error is not None and result.error.startswith("connector_auth_failed:")
    blob = result.model_dump_json()
    for secret in (_PASSWORD, _OPS_TOKEN, _JWT):
        assert secret not in blob


# ---------------------------------------------------------------------------
# Registration-shape invariants — the "merge = fix" property
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_ops_are_typed_and_enabled(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """The 13 reads persist as source_kind='typed', is_enabled=True (live on fresh boot)."""
    await register_fleet_lcm_typed_operations()

    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == FLEET_LCM_PRODUCT,
                    EndpointDescriptor.impl_id == FLEET_LCM_IMPL_ID,
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
    # Typed ops carry no wire method/path on the descriptor (path lives in _paths).
    assert all(r.method is None and r.path is None for r in rows)


@pytest.mark.asyncio
async def test_registered_groups_are_enabled(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
) -> None:
    """The 5 read-core groups land review_status='enabled' (typed groups skip the queue)."""
    await register_fleet_lcm_typed_operations()

    rows = (
        (
            await session.execute(
                select(OperationGroup).where(
                    OperationGroup.product == FLEET_LCM_PRODUCT,
                    OperationGroup.impl_id == FLEET_LCM_IMPL_ID,
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
    await register_fleet_lcm_typed_operations()

    result = await search_operations(
        _make_operator(),
        {
            "connector_id": FLEET_LCM_CONNECTOR_ID,
            "query": "fleet health system components sddc tasks upgrade release versions",
            "limit": 25,
        },
    )
    found = {hit["op_id"] for hit in result["hits"]}
    assert found >= _EXPECTED_OP_IDS, f"missing from search: {_EXPECTED_OP_IDS - found}"


# ---------------------------------------------------------------------------
# Pure-dataclass invariants (no DB, no async)
# ---------------------------------------------------------------------------


def _read_ops() -> list[Any]:
    return [op for op in FLEET_LCM_TYPED_OPS if op.safety_level == "safe"]


def test_typed_ops_table_has_exactly_the_read_core() -> None:
    assert {op.op_id for op in _read_ops()} == _EXPECTED_OP_IDS
    assert len(_read_ops()) == 13


def test_every_group_key_has_a_when_to_use() -> None:
    assert set(FLEET_LCM_TYPED_WHEN_TO_USE_BY_GROUP) == _EXPECTED_GROUPS
    used_groups = {op.group_key for op in FLEET_LCM_TYPED_OPS}
    assert used_groups == _EXPECTED_GROUPS


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_is_safe_no_approval_and_read_only(op_id: str) -> None:
    op = next(o for o in FLEET_LCM_TYPED_OPS if o.op_id == op_id)
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert "read-only" in op.tags


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_has_llm_instructions_with_canonical_keys(op_id: str) -> None:
    op = next(o for o in FLEET_LCM_TYPED_OPS if o.op_id == op_id)
    assert op.llm_instructions is not None
    for key in ("when_to_call", "output_shape", "next_step"):
        value = op.llm_instructions.get(key)
        assert isinstance(value, str) and value.strip(), f"{op_id}: {key}"


@pytest.mark.parametrize("op", FLEET_LCM_TYPED_OPS, ids=lambda op: op.op_id)
def test_each_op_example_validates_against_its_parameter_schema(op: Any) -> None:
    """Every op ships one example call, and it is a valid call (Draft 2020-12)."""
    examples = op.parameter_schema.get("examples")
    assert isinstance(examples, list) and examples, op.op_id
    Draft202012Validator.check_schema(op.parameter_schema)
    validator = Draft202012Validator(op.parameter_schema)
    for example in examples:
        assert list(validator.iter_errors(example)) == [], (op.op_id, example)


@pytest.mark.parametrize("op", FLEET_LCM_TYPED_OPS, ids=lambda op: op.op_id)
def test_each_op_response_schema_names_its_fields(op: Any) -> None:
    """No response schema is a bare "any object"."""
    schema = op.response_schema
    assert schema is not None, op.op_id
    assert schema.get("properties"), op.op_id


@pytest.mark.parametrize("op_id", sorted(_EXPECTED_OP_IDS))
def test_each_op_parameter_schema_disallows_additional_properties(op_id: str) -> None:
    op = next(o for o in FLEET_LCM_TYPED_OPS if o.op_id == op_id)
    assert op.parameter_schema.get("additionalProperties") is False


def test_no_read_has_a_write_name_or_tag() -> None:
    """The safe reads carry no write word and no write tag."""
    for op in _read_ops():
        assert not any(
            token in op.op_id for token in (".create", ".delete", ".update", ".set", ".put")
        )
        assert "write" not in op.tags
