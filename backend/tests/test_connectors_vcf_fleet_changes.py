# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the fleet upgrade, binary and environment ops.

The seven ops of ``changes_ops`` on ``fleet-rest-9.0`` (Aria Suite
Lifecycle 8.x and VCF 9.0 fleet management). HTTP is mocked with ``respx``;
the payloads are written by hand from the vendor API reference, with made-up
ids.

* Registration: each op has the risk and approval the Task table names.
* Every change waits for approval when a person calls it.
* A spec with a clear secret is refused by the dispatcher before the call
  can wait for approval: no approval row, no vendor call.
* The status read a start names (``fleet.request.get``) dispatches on the
  typed registration alone, with no spec ingested.
* The approved path (the handler called directly): the wire call, the
  ``accepted`` answer with the request id and the status read, the running
  request that stops a second start (a running pre-check blocks an upgrade
  and the other way round), and the vendor error text.
* ``fleet.binary.map`` reports its steps when a step fails, and says the
  outcome is unknown when the appliance does not answer the second step.
* ``fleet.environment.delete``: the preview reads the environment and fills
  the blast radius, refuses a missing one, and the call is refused without a
  matching ``preview_hash``; an agent is refused.
* A spec with a clear password, licence key or private key is refused.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vcf_fleet import FLEET_CONNECTOR_ID, VcfFleetConnector
from meho_backplane.connectors.vcf_fleet.changes import (
    environment_delete_preview,
    environment_spec_check,
    spec_secret_problem,
)
from meho_backplane.connectors.vcf_fleet.changes_ops import (
    CHANGE_OPS,
    CHANGE_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vcf_fleet.typed_ops import FLEET_TYPED_OPS
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._errors import result_connector_error
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    PreviewContext,
    blast_radius_missing_reason,
)
from meho_backplane.operations._validate import _PARAMS_CHECKS
from meho_backplane.settings import get_settings

_HOST = "fleet-lifecycle.test.invalid"
_BASE = f"https://{_HOST}"
_ENV = "env-0000-aaaa"
_PRODUCT = "vrops"
_REQ = "req-1111-bbbb"
_RUNNING_REQ = "req-2222-cccc"
_UPGRADE_URL = f"/lcm/lcops/api/v2/environments/{_ENV}/products/{_PRODUCT}/upgrade"
_REQUESTS_URL = "/lcm/request/api/v2/requests"
_BINARIES_URL = "/lcm/lcops/api/v2/settings/product-binaries"

#: The Task table: op id -> (safety level, requires approval).
_EXPECTED_RISK: dict[str, tuple[str, bool]] = {
    "fleet.product.upgrade": ("dangerous", True),
    "fleet.product.upgrade.precheck": ("caution", True),
    "fleet.binary.list": ("safe", False),
    "fleet.binary.map": ("caution", True),
    "fleet.environment.precheck": ("caution", True),
    "fleet.environment.create": ("dangerous", True),
    "fleet.environment.delete": ("destructive", True),
}

_UPGRADE_PARAMS: dict[str, Any] = {
    "environment_id": _ENV,
    "product_id": _PRODUCT,
    "product_version": "9.9.9",
    "repository_url": "/data/upgrade/product-9.9.9.pak",
}

_SPEC: dict[str, Any] = {
    "environmentName": "env-new",
    "infrastructure": {
        "properties": {
            "dataCenterVmid": "dc-0000",
            "defaultPassword": "locker:password:pw-0000:default",
        }
    },
    "products": [{"id": "vrli", "version": "9.9.9", "nodes": [{"type": "vrli-master"}]}],
}

_PRIVATE_KEY_TEXT = "-----BEGIN " + "PRIVATE KEY-----\nabc\n-----END " + "PRIVATE KEY-----"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product="fleet", version="9.0", impl_id="fleet-rest", cls=VcfFleetConnector
    )
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


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


class _Target:
    def __init__(self) -> None:
        self.product = "fleet"
        self.fingerprint = type("_FP", (), {"version": "9.0"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000000fe")
        self.name = "fleet-lifecycle"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/fleet/fleet-lifecycle"
        self.auth_model = "shared_service_account"


def _target() -> Any:
    return _Target()


def _operator(principal_kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub="op-fleet-lifecycle",
        name="Fleet Lifecycle Operator",
        email=None,
        raw_jwt="op.fleet.lifecycle.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000ab"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


async def _creds(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "admin@local", "password": "made-up-pw"}


def _connector() -> VcfFleetConnector:
    return VcfFleetConnector(credentials_loader=_creds)


async def _register_and_bind() -> VcfFleetConnector:
    await VcfFleetConnector.register_operations()
    instance = cast(VcfFleetConnector, get_or_create_connector_instance(VcfFleetConnector))
    instance._creds = type(instance._creds)(_creds, product_label="fleet")
    return instance


def _request_row(state: str, request_type: str, input_map: dict[str, Any]) -> dict[str, Any]:
    return {
        "vmid": _RUNNING_REQ,
        "state": state,
        "requestType": request_type,
        "requestName": request_type.lower(),
        "requestReason": "made-up reason",
        "inputMap": input_map,
    }


_ERROR_BODY = {"errorCode": "LCM_ERR_0001", "message": "made-up vendor failure text"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op", CHANGE_OPS, ids=lambda op: op.op_id)
def test_risk_and_approval_match_the_table(op: Any) -> None:
    safety, approval = _EXPECTED_RISK[op.op_id]
    assert op.safety_level == safety
    assert op.requires_approval is approval
    if safety != "safe":
        assert op.llm_instructions and op.llm_instructions.get("preconditions")


def test_all_seven_ops_and_their_groups_exist() -> None:
    assert {op.op_id for op in CHANGE_OPS} == set(_EXPECTED_RISK)
    for op in CHANGE_OPS:
        assert op.group_key in CHANGE_WHEN_TO_USE_BY_GROUP
        assert hasattr(VcfFleetConnector, op.handler_attr), op.op_id


def test_destructive_delete_has_a_preview_builder() -> None:
    assert _PREVIEW_BUILDERS["fleet.environment.delete"] is environment_delete_preview


def test_environment_ops_register_the_secret_check() -> None:
    for op_id in ("fleet.environment.precheck", "fleet.environment.create"):
        assert _PARAMS_CHECKS[op_id] is environment_spec_check


def test_the_status_read_every_start_names_is_registered() -> None:
    """The connector itself brings ``fleet.request.get`` and ``fleet.request.list``."""
    op_ids = {op.op_id for op in FLEET_TYPED_OPS}
    assert {"fleet.request.get", "fleet.request.list"} <= op_ids


# ---------------------------------------------------------------------------
# Every change waits for approval
# ---------------------------------------------------------------------------


_CHANGE_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("fleet.product.upgrade", _UPGRADE_PARAMS),
    ("fleet.product.upgrade.precheck", _UPGRADE_PARAMS),
    ("fleet.binary.map", {"source_location": "/data/depot", "source_type": "LOCAL"}),
    ("fleet.environment.precheck", {"spec": _SPEC}),
    ("fleet.environment.create", {"spec": _SPEC}),
]


@pytest.mark.parametrize(("op_id", "params"), _CHANGE_CALLS, ids=[c[0] for c in _CHANGE_CALLS])
@pytest.mark.asyncio
async def test_change_waits_for_approval(
    _stub_embedding: None, session: AsyncSession, op_id: str, params: dict[str, Any]
) -> None:
    instance = await _register_and_bind()
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            route = mock.route().respond(200, json={"requestId": _REQ})
            result = await dispatch(
                operator=_operator(),
                connector_id=FLEET_CONNECTOR_ID,
                op_id=op_id,
                target=_target(),
                params=params,
            )
        assert result.status == "awaiting_approval", result
        assert not route.called
    finally:
        await instance.aclose()


@pytest.mark.asyncio
async def test_binary_list_runs_at_once(_stub_embedding: None, session: AsyncSession) -> None:
    instance = await _register_and_bind()
    rows = [{"filePath": "/data/depot/a.ova", "name": "a.ova", "type": "install"}]
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            mock.get(_BINARIES_URL).respond(200, json=rows)
            result = await dispatch(
                operator=_operator(),
                connector_id=FLEET_CONNECTOR_ID,
                op_id="fleet.binary.list",
                target=_target(),
                params={},
            )
        assert result.status == "ok", result.error
        assert result.result == {"binaries": rows}
    finally:
        await instance.aclose()


# ---------------------------------------------------------------------------
# Upgrade and its pre-check (the approved path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "path", "pre_validate"),
    [
        ("product_upgrade", _UPGRADE_URL, True),
        ("product_upgrade_precheck", f"{_UPGRADE_URL}/pre-validate", True),
    ],
)
@pytest.mark.asyncio
async def test_upgrade_start_is_accepted(handler: str, path: str, pre_validate: bool) -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        mock.get(_REQUESTS_URL).respond(200, json=[])
        route = mock.post(path).respond(200, json={"requestId": _REQ})
        result = await getattr(connector, handler)(_operator(), _target(), dict(_UPGRADE_PARAMS))
    assert result["status"] == "accepted"
    assert result["request_id"] == _REQ
    assert result["status_read"] == {"op_id": "fleet.request.get", "params": {"request_id": _REQ}}
    body = json.loads(route.calls[0].request.content)
    assert body["preValidate"] is pre_validate
    assert body["productVersion"] == "9.9.9"
    assert body["repositoryType"] == "lcmRepository"
    await connector.aclose()


@pytest.mark.asyncio
async def test_upgrade_skip_precheck_sends_prevalidate_false() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        mock.get(_REQUESTS_URL).respond(200, json=[])
        route = mock.post(_UPGRADE_URL).respond(200, json={"requestId": _REQ})
        await connector.product_upgrade(
            _operator(), _target(), {**_UPGRADE_PARAMS, "skip_precheck": True}
        )
    assert json.loads(route.calls[0].request.content)["preValidate"] is False
    await connector.aclose()


@pytest.mark.asyncio
async def test_upgrade_returns_the_running_request_and_starts_nothing() -> None:
    connector = _connector()
    rows = [
        _request_row("COMPLETED", "PRODUCT_UPGRADE", {"environmentId": _ENV}),
        _request_row(
            "INPROGRESS", "PRODUCT_UPGRADE", {"environmentId": _ENV, "productId": _PRODUCT}
        ),
    ]
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.get(_REQUESTS_URL).respond(200, json=rows)
        start = mock.post(_UPGRADE_URL).respond(200, json={"requestId": _REQ})
        result = await connector.product_upgrade(_operator(), _target(), dict(_UPGRADE_PARAMS))
    assert result["status"] == "already_running"
    assert result["request_id"] == _RUNNING_REQ
    assert result["request_state"] == "INPROGRESS"
    assert not start.called
    await connector.aclose()


@pytest.mark.parametrize(
    ("handler", "running_type", "status"),
    [
        ("product_upgrade", "PRODUCT_UPGRADE_PRE_VALIDATE", "blocked"),
        ("product_upgrade_precheck", "PRODUCT_UPGRADE", "blocked"),
        ("product_upgrade_precheck", "PRODUCT_UPGRADE_PRE_VALIDATE", "already_running"),
    ],
)
@pytest.mark.asyncio
async def test_a_running_upgrade_or_precheck_of_the_product_stops_the_start(
    handler: str, running_type: str, status: str
) -> None:
    """A pre-check is not an upgrade: it blocks the upgrade, it does not stand in for it."""
    connector = _connector()
    input_map = {"environmentId": _ENV, "productId": _PRODUCT}
    rows = [_request_row("INPROGRESS", running_type, input_map)]
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.get(_REQUESTS_URL).respond(200, json=rows)
        start = mock.route(method="POST").respond(200, json={"requestId": _REQ})
        result = await getattr(connector, handler)(_operator(), _target(), dict(_UPGRADE_PARAMS))
    assert result["status"] == status
    assert result["request_id"] == _RUNNING_REQ
    assert result["status_read"]["params"] == {"request_id": _RUNNING_REQ}
    if status == "blocked":
        assert "still running" in result["reason"]
    assert not start.called
    await connector.aclose()


@pytest.mark.asyncio
async def test_upgrade_ignores_a_running_request_on_another_environment() -> None:
    connector = _connector()
    rows = [_request_row("INPROGRESS", "PRODUCT_UPGRADE", {"environmentId": "env-other"})]
    async with respx.mock(base_url=_BASE) as mock:
        mock.get(_REQUESTS_URL).respond(200, json=rows)
        mock.post(_UPGRADE_URL).respond(200, json={"requestId": _REQ})
        result = await connector.product_upgrade(_operator(), _target(), dict(_UPGRADE_PARAMS))
    assert result["status"] == "accepted"
    await connector.aclose()


@pytest.mark.asyncio
async def test_upgrade_ignores_a_running_request_on_a_longer_product_id() -> None:
    connector = _connector()
    input_map = {"environmentId": _ENV, "productId": f"{_PRODUCT}-2"}
    rows = [_request_row("INPROGRESS", "PRODUCT_UPGRADE", input_map)]
    async with respx.mock(base_url=_BASE) as mock:
        mock.get(_REQUESTS_URL).respond(200, json=rows)
        mock.post(_UPGRADE_URL).respond(200, json={"requestId": _REQ})
        result = await connector.product_upgrade(_operator(), _target(), dict(_UPGRADE_PARAMS))
    assert result["status"] == "accepted"
    await connector.aclose()


@pytest.mark.asyncio
async def test_upgrade_vendor_error_text_reaches_the_answer() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        mock.get(_REQUESTS_URL).respond(200, json=[])
        mock.post(_UPGRADE_URL).respond(400, json=_ERROR_BODY)
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await connector.product_upgrade(_operator(), _target(), dict(_UPGRADE_PARAMS))
    answer = result_connector_error("fleet.product.upgrade", caught.value, 1.0)
    assert "made-up vendor failure text" in str(answer.extras.get("upstream_message"))
    await connector.aclose()


@pytest.mark.asyncio
async def test_upgrade_refuses_a_licence_key_value() -> None:
    connector = _connector()
    params = {**_UPGRADE_PARAMS, "license_ref": "AAAAA-BBBBB-CCCCC"}
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        start = mock.post(_UPGRADE_URL).respond(200, json={"requestId": _REQ})
        with pytest.raises(ValueError, match="locker"):
            await connector.product_upgrade(_operator(), _target(), params)
    assert not start.called
    await connector.aclose()


@pytest.mark.asyncio
async def test_the_status_read_of_a_start_dispatches_without_a_spec(
    _stub_embedding: None, session: AsyncSession
) -> None:
    """A start, then its returned status call: both run on the typed registration."""
    instance = await _register_and_bind()
    try:
        async with respx.mock(base_url=_BASE) as mock:
            mock.get(_REQUESTS_URL).respond(200, json=[])
            mock.post(_UPGRADE_URL).respond(200, json={"requestId": _REQ})
            mock.get(f"{_REQUESTS_URL}/{_REQ}").respond(
                200, json={"vmid": _REQ, "state": "INPROGRESS", "requestType": "PRODUCT_UPGRADE"}
            )
            started = await instance.product_upgrade(_operator(), _target(), dict(_UPGRADE_PARAMS))
            result = await dispatch(
                operator=_operator(),
                connector_id=FLEET_CONNECTOR_ID,
                op_id=started["status_read"]["op_id"],
                target=_target(),
                params=started["status_read"]["params"],
            )
        assert result.status == "ok", result.error
        assert result.result["request_id"] == _REQ
        assert result.result["outcome"] == "running"
    finally:
        await instance.aclose()


# ---------------------------------------------------------------------------
# Binaries
# ---------------------------------------------------------------------------


_FOUND = [
    {"filePath": "/data/depot/a.ova", "name": "a.ova", "type": "install"},
    {"filePath": "/data/depot/b.pak", "name": "b.pak", "type": "upgrade"},
]


@pytest.mark.asyncio
async def test_binary_map_finds_then_maps_the_chosen_files() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        find = mock.post(_BINARIES_URL).respond(200, json=_FOUND)
        map_route = mock.post(f"{_BINARIES_URL}/download").respond(200, json={"requestId": _REQ})
        result = await connector.binary_map(
            _operator(),
            _target(),
            {"source_location": "/data/depot", "source_type": "LOCAL", "names": ["b.pak"]},
        )
    assert json.loads(find.calls[0].request.content) == {
        "sourceLocation": "/data/depot",
        "sourceType": "LOCAL",
    }
    assert json.loads(map_route.calls[0].request.content) == [_FOUND[1]]
    assert result["status"] == "accepted"
    assert result["request_id"] == _REQ
    assert [s["step"] for s in result["steps"]] == ["find", "map"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_binary_map_failure_at_step_two_lists_the_steps() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        mock.post(_BINARIES_URL).respond(200, json=_FOUND)
        mock.post(f"{_BINARIES_URL}/download").respond(500, json=_ERROR_BODY)
        result = await connector.binary_map(
            _operator(), _target(), {"source_location": "/data/depot", "source_type": "NFS"}
        )
    assert result["status"] == "failed"
    assert result["steps"] == [
        {"step": "find", "result": "ok", "found": 2},
        {"step": "map", "result": "failed", "error": "made-up vendor failure text"},
    ]
    await connector.aclose()


@pytest.mark.asyncio
async def test_binary_map_failure_at_step_one_lists_the_step_not_run() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.post(_BINARIES_URL).respond(400, json=_ERROR_BODY)
        map_route = mock.post(f"{_BINARIES_URL}/download").respond(200, json={"requestId": _REQ})
        result = await connector.binary_map(
            _operator(), _target(), {"source_location": "/data/depot", "source_type": "NFS"}
        )
    assert result["steps"] == [
        {"step": "find", "result": "failed", "error": "made-up vendor failure text"},
        {"step": "map", "result": "not_run"},
    ]
    assert not map_route.called
    await connector.aclose()


@pytest.mark.asyncio
async def test_binary_map_timeout_at_step_two_says_the_outcome_is_unknown() -> None:
    """A lost answer of the map step keeps the step report; the map may have run."""
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        mock.post(_BINARIES_URL).respond(200, json=_FOUND)
        mock.post(f"{_BINARIES_URL}/download").mock(side_effect=httpx.ReadTimeout("timed out"))
        result = await connector.binary_map(
            _operator(), _target(), {"source_location": "/data/depot", "source_type": "NFS"}
        )
    assert result["status"] == "unknown"
    assert result["steps"][0] == {"step": "find", "result": "ok", "found": 2}
    assert result["steps"][1]["step"] == "map"
    assert result["steps"][1]["result"] == "unknown"
    assert "ReadTimeout" in result["steps"][1]["error"]
    assert result["binaries"] == _FOUND
    assert "fleet.binary.list" in result["reason"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_binary_map_connection_loss_at_step_one_lists_the_step_not_run() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.post(_BINARIES_URL).mock(side_effect=httpx.ConnectError("no route"))
        map_route = mock.post(f"{_BINARIES_URL}/download").respond(200, json={"requestId": _REQ})
        result = await connector.binary_map(
            _operator(), _target(), {"source_location": "/data/depot", "source_type": "NFS"}
        )
    assert result["status"] == "failed"
    assert result["steps"][0]["step"] == "find"
    assert result["steps"][0]["result"] == "failed"
    assert "ConnectError" in result["steps"][0]["error"]
    assert result["steps"][1] == {"step": "map", "result": "not_run"}
    assert not map_route.called
    await connector.aclose()


# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_environment_precheck_and_create_are_accepted() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        mock.get(_REQUESTS_URL).respond(200, json=[])
        check = mock.post("/lcm/lcops/api/v2/environments/pre-validate").respond(
            200, json={"requestId": "req-check"}
        )
        create = mock.post("/lcm/lcops/api/v2/environments").respond(200, json={"requestId": _REQ})
        checked = await connector.environment_precheck(_operator(), _target(), {"spec": _SPEC})
        created = await connector.environment_create(_operator(), _target(), {"spec": _SPEC})
    assert checked["status"] == "accepted" and checked["request_id"] == "req-check"
    assert created["status"] == "accepted" and created["request_id"] == _REQ
    assert created["environment_name"] == "env-new"
    assert json.loads(check.calls[0].request.content) == _SPEC
    assert json.loads(create.calls[0].request.content) == _SPEC
    await connector.aclose()


@pytest.mark.asyncio
async def test_environment_create_returns_the_running_create() -> None:
    connector = _connector()
    rows = [_request_row("INPROGRESS", "CREATE_ENVIRONMENT", {"environmentName": "env-new"})]
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        mock.get(_REQUESTS_URL).respond(200, json=rows)
        create = mock.post("/lcm/lcops/api/v2/environments").respond(200, json={"requestId": _REQ})
        result = await connector.environment_create(_operator(), _target(), {"spec": _SPEC})
    assert result["status"] == "already_running"
    assert result["request_id"] == _RUNNING_REQ
    assert not create.called
    await connector.aclose()


@pytest.mark.asyncio
async def test_environment_create_ignores_a_running_create_of_a_longer_name() -> None:
    connector = _connector()
    rows = [_request_row("INPROGRESS", "CREATE_ENVIRONMENT", {"environmentName": "env-new-2"})]
    async with respx.mock(base_url=_BASE) as mock:
        mock.get(_REQUESTS_URL).respond(200, json=rows)
        create = mock.post("/lcm/lcops/api/v2/environments").respond(200, json={"requestId": _REQ})
        result = await connector.environment_create(_operator(), _target(), {"spec": _SPEC})
    assert result["status"] == "accepted"
    assert create.called
    await connector.aclose()


@pytest.mark.asyncio
async def test_environment_create_without_a_name_ignores_running_creates() -> None:
    connector = _connector()
    rows = [_request_row("INPROGRESS", "CREATE_ENVIRONMENT", {"environmentName": "env-new"})]
    spec = {key: value for key, value in _SPEC.items() if key != "environmentName"}
    async with respx.mock(base_url=_BASE) as mock:
        mock.get(_REQUESTS_URL).respond(200, json=rows)
        create = mock.post("/lcm/lcops/api/v2/environments").respond(200, json={"requestId": _REQ})
        result = await connector.environment_create(_operator(), _target(), {"spec": spec})
    assert result["status"] == "accepted"
    assert create.called
    await connector.aclose()


@pytest.mark.asyncio
async def test_environment_create_vendor_error_text_reaches_the_answer() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        mock.get(_REQUESTS_URL).respond(200, json=[])
        mock.post("/lcm/lcops/api/v2/environments").respond(500, json=_ERROR_BODY)
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await connector.environment_create(_operator(), _target(), {"spec": _SPEC})
    answer = result_connector_error("fleet.environment.create", caught.value, 1.0)
    assert "made-up vendor failure text" in str(answer.extras.get("upstream_message"))
    await connector.aclose()


@pytest.mark.parametrize(
    "bad",
    [
        {"infrastructure": {"properties": {"vcPassword": "plain-text"}}},
        {"products": [{"properties": {"licenseRef": "AAAAA-BBBBB"}}]},
        {"infrastructure": {"properties": {"certificate": _PRIVATE_KEY_TEXT}}},
    ],
)
@pytest.mark.asyncio
async def test_environment_spec_with_a_clear_secret_is_refused(bad: dict[str, Any]) -> None:
    connector = _connector()
    spec = {**_SPEC, **bad}
    assert spec_secret_problem(spec) is not None
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        route = mock.route().respond(200, json={"requestId": _REQ})
        with pytest.raises(ValueError):
            await connector.environment_create(_operator(), _target(), {"spec": spec})
    assert not route.called
    await connector.aclose()


def test_locker_references_pass_the_secret_check() -> None:
    assert spec_secret_problem(_SPEC) is None
    assert environment_spec_check({"spec": _SPEC}) is None


def test_the_params_check_names_the_field_and_not_the_value() -> None:
    bad = {**_SPEC, "infrastructure": {"properties": {"defaultPassword": "plain-text-pw"}}}
    problem = environment_spec_check({"spec": bad})
    assert problem is not None
    assert problem["path"] == "spec.infrastructure.properties.defaultPassword"
    assert problem["validator"] == "params_check"
    assert "plain-text-pw" not in json.dumps(problem)


@pytest.mark.parametrize("op_id", ["fleet.environment.precheck", "fleet.environment.create"])
@pytest.mark.asyncio
async def test_a_clear_secret_is_refused_before_the_call_waits_for_approval(
    _stub_embedding: None, session: AsyncSession, op_id: str
) -> None:
    """The dispatcher refuses the spec as invalid params: no approval row, no vendor call."""
    instance = await _register_and_bind()
    bad = {**_SPEC, "infrastructure": {"properties": {"defaultPassword": "plain-text-pw"}}}
    rows_before = (
        await session.execute(select(func.count()).select_from(ApprovalRequest))
    ).scalar_one()
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            route = mock.route().respond(200, json={"requestId": _REQ})
            result = await dispatch(
                operator=_operator(),
                connector_id=FLEET_CONNECTOR_ID,
                op_id=op_id,
                target=_target(),
                params={"spec": bad},
            )
        assert result.status == "error", result
        assert result.extras["error_code"] == "invalid_params"
        assert result.extras["validation_errors"][0]["validator"] == "params_check"
        assert "plain-text-pw" not in str(result.extras)
        assert not route.called
    finally:
        await instance.aclose()
    rows_after = (
        await session.execute(select(func.count()).select_from(ApprovalRequest))
    ).scalar_one()
    assert rows_after == rows_before
    stored = (
        await session.execute(select(ApprovalRequest).where(ApprovalRequest.op_id == op_id))
    ).scalars()
    assert all("plain-text-pw" not in json.dumps(row.params or {}) for row in stored)


@pytest.mark.asyncio
async def test_environment_delete_sends_the_delete_body() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        route = mock.delete(f"/lcm/lcops/api/v2/environments/{_ENV}").respond(
            200, json={"requestId": _REQ}
        )
        result = await connector.environment_delete(
            _operator(), _target(), {"environment_id": _ENV, "delete_vms": True}
        )
    assert json.loads(route.calls[0].request.content)["deleteFromVcenter"] is True
    assert result["status"] == "accepted" and result["delete_vms"] is True
    await connector.aclose()


def _preview_ctx(connector: VcfFleetConnector, params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=MagicMock(op_id="fleet.environment.delete"),
        connector_instance=connector,
        operator=_operator(),
        target=_target(),
        params=params,
        connector_id=FLEET_CONNECTOR_ID,
    )


@pytest.mark.asyncio
async def test_delete_preview_names_the_environment_and_its_products() -> None:
    connector = _connector()
    env = {
        "environmentId": _ENV,
        "environmentName": "env-made-up",
        "products": [{"id": "vrops", "version": "9.9.9", "nodes": [{}, {}]}],
    }
    async with respx.mock(base_url=_BASE) as mock:
        mock.get(f"/lcm/lcops/api/v2/environments/{_ENV}").respond(200, json=env)
        effect = await environment_delete_preview(
            _preview_ctx(connector, {"environment_id": _ENV, "delete_vms": True})
        )
    assert blast_radius_missing_reason(effect) is None
    assert effect is not None
    block = effect["blast_radius"]
    assert block["object"] == {"environment_id": _ENV, "environment_name": "env-made-up"}
    assert block["children"] == [{"product_id": "vrops", "version": "9.9.9", "nodes": 2}]
    assert "vCenter" in block["irreversibility"]
    await connector.aclose()


@pytest.mark.asyncio
async def test_delete_preview_refuses_a_missing_environment() -> None:
    connector = _connector()
    async with respx.mock(base_url=_BASE) as mock:
        mock.get(f"/lcm/lcops/api/v2/environments/{_ENV}").respond(404, json=_ERROR_BODY)
        with pytest.raises(ValueError, match="does not exist"):
            await environment_delete_preview(_preview_ctx(connector, {"environment_id": _ENV}))
    await connector.aclose()


@pytest.mark.asyncio
async def test_delete_is_refused_without_a_preview_hash(
    _stub_embedding: None, session: AsyncSession
) -> None:
    instance = await _register_and_bind()
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            route = mock.delete(f"/lcm/lcops/api/v2/environments/{_ENV}").respond(
                200, json={"requestId": _REQ}
            )
            mock.get(f"/lcm/lcops/api/v2/environments/{_ENV}").respond(200, json={})
            result = await dispatch(
                operator=_operator(),
                connector_id=FLEET_CONNECTOR_ID,
                op_id="fleet.environment.delete",
                target=_target(),
                params={"environment_id": _ENV},
            )
        assert result.status == "denied", result
        assert result.extras["error_code"] == "preview_binding_required"
        assert not route.called
    finally:
        await instance.aclose()


@pytest.mark.asyncio
async def test_delete_is_refused_for_an_agent(_stub_embedding: None, session: AsyncSession) -> None:
    instance = await _register_and_bind()
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            route = mock.delete(f"/lcm/lcops/api/v2/environments/{_ENV}").respond(
                200, json={"requestId": _REQ}
            )
            mock.get(f"/lcm/lcops/api/v2/environments/{_ENV}").respond(200, json={})
            result = await dispatch(
                operator=_operator(PrincipalKind.AGENT),
                connector_id=FLEET_CONNECTOR_ID,
                op_id="fleet.environment.delete",
                target=_target(),
                params={"environment_id": _ENV},
            )
        assert result.status == "denied", result
        assert not route.called
    finally:
        await instance.aclose()
