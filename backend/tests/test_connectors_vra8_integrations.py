# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The Automation integration ops on Aria Automation 8 (``vcfa.vra8.*``).

The ops are written once and registered on both Automation connectors; the
``vcfa.tenant`` tests (``test_vcfa_integrations.py`` and the files next to
it) cover the logic. These tests prove the vRA 8 side: the ids dispatch on a
vRA 8 target with the two-step login, every call carries the bearer, and a
login that expires part-way logs in again. All names and ids are made up.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_shared import vcfa_integration_calls
from meho_backplane.connectors.vra8 import (
    VRA8_CONNECTOR_ID,
    VRA8_IMPL_ID,
    VRA8_PRODUCT,
    VRA8_VERSION,
    Vra8Connector,
    register_vra8_integration_operations,
)
from meho_backplane.operations import (
    PassThroughReducer,
    dispatch,
    reset_dispatcher_caches,
    set_default_reducer,
)
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.settings import get_settings

_HOST = "vra-integrations.test.invalid"
_BASE = f"https://{_HOST}"
_VAULT_VALUE = "Pw-sentinel-4501b"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr(vcfa_integration_calls, "POLL_SECONDS", 0.0)
    monkeypatch.setattr(
        vcfa_integration_calls,
        "load_param_secret_data",
        AsyncMock(return_value={"password": _VAULT_VALUE}),
    )
    get_settings.cache_clear()
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=VRA8_PRODUCT, version=VRA8_VERSION, impl_id=VRA8_IMPL_ID, cls=Vra8Connector
    )
    set_default_reducer(PassThroughReducer())
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    get_settings.cache_clear()


class _Target:
    def __init__(self) -> None:
        self.product = VRA8_PRODUCT
        self.fingerprint = type("_FP", (), {"version": "8.18"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000045a1")
        self.name = "vra-integrations"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/vra-integrations"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {}


_OPERATOR = Operator(
    sub="vra8-integrations",
    name="Integrations Test",
    email=None,
    raw_jwt="vra8.integrations.jwt",
    tenant_id=UUID("00000000-0000-0000-0000-0000000045a2"),
    tenant_role=TenantRole.TENANT_ADMIN,
)


async def _loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "vra-svc", "password": "vra-pw"}


async def _setup() -> None:
    await register_vra8_integration_operations()
    instance = get_or_create_connector_instance(Vra8Connector)
    instance._credentials_loader = _loader  # type: ignore[attr-defined]


async def _run(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=VRA8_CONNECTOR_ID,
        op_id=op_id,
        target=_Target(),
        params=params,
        _approved=True,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


def _logins(mock: respx.MockRouter) -> respx.Route:
    mock.post(f"{_BASE}/csp/gateway/am/api/login").respond(200, json={"refresh_token": "rt"})
    return mock.post(f"{_BASE}/iaas/api/login").respond(200, json={"token": "vra-bearer"})


async def test_list_dispatches_on_a_vra8_target() -> None:
    await _setup()
    with respx.mock(assert_all_called=False) as mock:
        _logins(mock)
        route = mock.get(f"{_BASE}/iaas/api/integrations").respond(
            200, json={"content": [{"id": "int-1", "name": "<vro>", "integrationType": "vro"}]}
        )
        result = await _run("vcfa.vra8.integration.list", {})
    assert result["status"] == "ok", result
    assert result["result"]["rows"][0]["integration_type"] == "vro"
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer vra-bearer"
    assert request.headers["Accept"] == "application/json"


async def test_set_runs_the_test_on_vra8_and_logs_in_again_on_401() -> None:
    await _setup()
    call = {
        "name": "<vro>",
        "integration_type": "vro",
        "integration_properties": {"hostName": "https://<orchestrator-host>"},
        "user_name": "<user>",
        "password_secret_ref": "tenants/example/vro",
    }
    with respx.mock(assert_all_called=False) as mock:
        login = _logins(mock)
        mock.get(f"{_BASE}/iaas/api/integrations").respond(200, json={"content": []})
        post = mock.post(f"{_BASE}/iaas/api/integrations").mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(202, json={"id": "t-1", "status": "FAILED", "message": "bad"}),
            ]
        )
        result = await _run("vcfa.vra8.integration.set", call)
    data = result["result"]
    assert data["status"] == "test_failed", data
    assert login.call_count == 2
    assert post.call_count == 2
    assert json.loads(post.calls.last.request.content)["privateKey"] == _VAULT_VALUE
    assert _VAULT_VALUE not in json.dumps(result)


async def test_git_status_read_on_vra8() -> None:
    await _setup()
    with respx.mock(assert_all_called=False) as mock:
        _logins(mock)
        mock.get(f"{_BASE}/content/api/sourcecontrol/sync-requests/sync-1").respond(
            200, json={"requestId": "sync-1", "status": "COMPLETED"}
        )
        result = await _run("vcfa.vra8.integration.list", {"sync_request_id": "sync-1"})
    job = result["result"]["job"]
    assert job["state"] == "COMPLETED"
    assert job["done"] is True
