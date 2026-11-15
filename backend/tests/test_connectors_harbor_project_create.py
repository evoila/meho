# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for ``harbor.project.create`` with a faked Harbor API."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.harbor import HarborConnector
from meho_backplane.connectors.harbor.ops_project_create import (
    _PROJECT_CREATE_PARAMETER_SCHEMA,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_PROJECTS_URL = "https://harbor.test.invalid/api/v2.0/projects"


@pytest.fixture(autouse=True)
def _clean_harbor_registry() -> None:
    clear_registry()
    register_connector_v2(
        product=HarborConnector.product,
        version=HarborConnector.version,
        impl_id=HarborConnector.impl_id,
        cls=HarborConnector,
    )
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _StubTarget:
    name: str
    host: str
    port: int | None
    secret_ref: str
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_TARGET = _StubTarget(
    name="harbor-test",
    host="harbor.test.invalid",
    port=443,
    secret_ref="targets/harbor/harbor-test",
)


def _operator() -> Operator:
    return Operator(
        sub="op-project-create",
        name="Harbor Project Operator",
        email=None,
        raw_jwt="op.project.create.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0b4"),
        tenant_role=TenantRole.OPERATOR,
    )


def _install_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_client(
        monkeypatch,
        secret={"username": "svc-harbor-canary", "password": "p4ss-canary-project-create"},
    )


@pytest.mark.asyncio
async def test_project_create_posts_private_project_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    sent: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> respx.MockResponse:
        sent.update(json.loads(request.content))
        return respx.MockResponse(201)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(_PROJECTS_URL).mock(side_effect=_capture)
        result = await connector.project_create(
            _operator(), _TARGET, {"project_name": "demo-images"}
        )

    assert sent == {"project_name": "demo-images", "metadata": {"public": "false"}}
    assert result == {
        "project_name": "demo-images",
        "created": True,
        "already_existed": False,
        "read_back": "harbor.project.info",
    }
    await connector.aclose()


@pytest.mark.asyncio
async def test_project_create_public_and_storage_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    sent: dict[str, Any] = {}

    def _capture(request: httpx.Request) -> respx.MockResponse:
        sent.update(json.loads(request.content))
        return respx.MockResponse(201)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(_PROJECTS_URL).mock(side_effect=_capture)
        await connector.project_create(
            _operator(),
            _TARGET,
            {"project_name": "shared", "public": True, "storage_limit": 1073741824},
        )

    assert sent["metadata"] == {"public": "true"}
    assert sent["storage_limit"] == 1073741824
    await connector.aclose()


@pytest.mark.asyncio
async def test_project_create_taken_name_reports_created_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_PROJECTS_URL).mock(
            return_value=respx.MockResponse(
                409, json={"errors": [{"code": "CONFLICT", "message": "already exists"}]}
            )
        )
        result = await connector.project_create(
            _operator(), _TARGET, {"project_name": "demo-images"}
        )

    assert route.call_count == 1
    assert result["created"] is False
    assert result["already_existed"] is True
    assert result["read_back"] == "harbor.project.info"
    await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 500, 502])
async def test_project_create_other_errors_are_raised(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _install_vault(monkeypatch)
    connector = HarborConnector()
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_PROJECTS_URL).mock(return_value=respx.MockResponse(status))
        with pytest.raises(httpx.HTTPStatusError):
            await connector.project_create(_operator(), _TARGET, {"project_name": "demo-images"})
    assert route.call_count == 1
    await connector.aclose()


def test_project_create_schema_refuses_bad_input() -> None:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(_PROJECT_CREATE_PARAMETER_SCHEMA)
    assert not list(validator.iter_errors({"project_name": "demo-images"}))
    for bad in (
        {},
        {"project_name": ""},
        {"project_name": "  "},
        {"project_name": "x", "public": "false"},
        {"project_name": "x", "storage_limit": -2},
        {"project_name": "x", "extra": 1},
    ):
        assert list(validator.iter_errors(bad)), bad
