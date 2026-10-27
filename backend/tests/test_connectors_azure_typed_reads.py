# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``azure.about``, the fingerprint and the registered operations.

The dispatch tests run the real dispatcher (policy, audit, retry after a 401)
with only the Azure registrar run. Microsoft's login service and Azure
Resource Manager are mocked with respx. Every id, name and secret here is
made up.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors.azure import (
    AZURE_CONNECTOR_ID,
    AZURE_IMPL_ID,
    AZURE_PRODUCT,
    AZURE_TYPED_OPS,
    AZURE_TYPED_WHEN_TO_USE_BY_GROUP,
    AZURE_VERSION,
    AzureConnector,
    register_azure_typed_operations,
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
from tests._azure_fixtures import (
    CLIENT_A,
    OBJECT_A,
    SECRET_A,
    SUBSCRIPTION_A,
    TENANT_A,
    AzureTargetStub,
    CountingLoader,
    login_token,
    operator,
    subscription_answer,
    subscription_url,
    token_answer,
    token_url,
)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _azure_only_registry() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=AZURE_PRODUCT, version=AZURE_VERSION, impl_id=AZURE_IMPL_ID, cls=AzureConnector
    )
    register_connector_v2(product=AZURE_PRODUCT, version="", impl_id="", cls=AzureConnector)
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


async def _registered_connector(loader: CountingLoader) -> AzureConnector:
    """Run the Azure registrar and give the dispatcher's instance a stub loader."""
    await register_azure_typed_operations()
    instance = get_or_create_connector_instance(AzureConnector)
    assert isinstance(instance, AzureConnector)
    instance._credentials_loader = loader
    return instance


@pytest.mark.asyncio
async def test_about_answers_with_the_subscription_and_the_login(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _registered_connector(CountingLoader())

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        arm = mock.get(subscription_url()).respond(200, json=subscription_answer())
        result = await dispatch(
            operator=operator(),
            connector_id=AZURE_CONNECTOR_ID,
            op_id="azure.about",
            target=AzureTargetStub(),
            params={},
        )

    assert result.status == "ok", result.error
    assert result.result == {
        "subscription_id": SUBSCRIPTION_A,
        "display_name": "Example Subscription",
        "state": "Enabled",
        "tenant_id": TENANT_A,
        "cloud": "AzureCloud",
        "app_id": CLIENT_A,
        "object_id": OBJECT_A,
    }
    request = arm.calls[0].request
    assert request.url.params["api-version"] == "2022-12-01"
    assert request.headers["Authorization"] == f"Bearer {login_token()}"
    assert login_token() not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_a_401_from_azure_logs_in_again_and_retries_once(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    loader = CountingLoader()
    await _registered_connector(loader)

    with respx.mock() as mock:
        login = mock.post(token_url()).mock(
            side_effect=[
                httpx.Response(200, json=token_answer(login_token(n=1))),
                httpx.Response(200, json=token_answer(login_token(n=2))),
            ]
        )
        arm = mock.get(subscription_url()).mock(
            side_effect=[
                httpx.Response(401, json={"error": {"code": "ExpiredAuthenticationToken"}}),
                httpx.Response(200, json=subscription_answer()),
            ]
        )
        result = await dispatch(
            operator=operator(),
            connector_id=AZURE_CONNECTOR_ID,
            op_id="azure.about",
            target=AzureTargetStub(),
            params={},
        )

    assert result.status == "ok", result.error
    assert login.call_count == 2
    assert arm.call_count == 2
    assert loader.calls == 1
    assert arm.calls[0].request.headers["Authorization"] == f"Bearer {login_token(n=1)}"
    assert arm.calls[1].request.headers["Authorization"] == f"Bearer {login_token(n=2)}"


@pytest.mark.asyncio
async def test_a_refused_login_fails_clearly_and_the_secret_is_read_again(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    loader = CountingLoader()
    await _registered_connector(loader)
    target = AzureTargetStub()
    refusal = {"error": "invalid_client", "error_codes": [7000215]}

    with respx.mock() as mock:
        login = mock.post(token_url()).mock(
            side_effect=[
                httpx.Response(401, json=refusal),
                httpx.Response(200, json=token_answer(login_token())),
            ]
        )
        mock.get(subscription_url()).respond(200, json=subscription_answer())
        refused = await dispatch(
            operator=operator(),
            connector_id=AZURE_CONNECTOR_ID,
            op_id="azure.about",
            target=target,
            params={},
        )
        fixed = await dispatch(
            operator=operator(),
            connector_id=AZURE_CONNECTOR_ID,
            op_id="azure.about",
            target=target,
            params={},
        )

    assert refused.status == "error"
    assert refused.error is not None and refused.error.startswith("connector_auth_failed")
    refused_text = json.dumps(refused.model_dump(mode="json"))
    assert "AADSTS7000215" in refused_text
    assert SECRET_A not in refused_text
    assert fixed.status == "ok", fixed.error
    assert login.call_count == 2
    assert loader.calls == 2


@pytest.mark.asyncio
async def test_fingerprint_describes_the_subscription() -> None:
    connector = AzureConnector(credentials_loader=CountingLoader())

    with respx.mock() as mock:
        mock.post(token_url()).respond(200, json=token_answer(login_token()))
        mock.get(subscription_url()).respond(200, json=subscription_answer())
        fp = await connector.fingerprint(AzureTargetStub(), operator=operator())

    assert fp.reachable is True
    assert (fp.vendor, fp.product, fp.version) == ("microsoft", "azure", None)
    assert dict(fp.extras) == {
        "subscription_id": SUBSCRIPTION_A,
        "tenant_id": TENANT_A,
        "cloud": "AzureCloud",
        "state": "Enabled",
    }


@pytest.mark.asyncio
async def test_fingerprint_without_an_operator_reports_why_it_cannot_read() -> None:
    async def vault_refuses(_target: Any, _operator: Any) -> Any:
        raise VaultClientError("Vault refused the login of the system identity")

    connector = AzureConnector(credentials_loader=vault_refuses)

    with respx.mock(assert_all_called=False) as mock:
        login = mock.post(token_url()).respond(200, json=token_answer(login_token()))
        fp = await connector.fingerprint(AzureTargetStub())

    assert fp.reachable is False
    assert "VaultClientError" in fp.extras["error"]
    assert not login.called


@pytest.mark.asyncio
async def test_probe_uses_the_fingerprint_read() -> None:
    connector = AzureConnector(credentials_loader=CountingLoader())
    target = AzureTargetStub(host="attacker.example.net")

    probe = await connector.probe(target)

    assert probe.ok is False
    assert probe.reason is not None and "host must be" in probe.reason


@pytest.mark.asyncio
async def test_the_operation_is_stored_as_typed_and_enabled(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_azure_typed_operations()

    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == AZURE_PRODUCT,
                    EndpointDescriptor.impl_id == AZURE_IMPL_ID,
                )
            )
        )
        .scalars()
        .all()
    )
    by_op = {row.op_id: row for row in rows}
    assert set(by_op) == {op.op_id for op in AZURE_TYPED_OPS}
    for row in rows:
        assert row.source_kind == "typed"
        assert row.is_enabled is True
        assert row.safety_level == "safe"
        assert row.requires_approval is False
    about_ref = by_op["azure.about"].handler_ref
    status_ref = by_op["azure.operation.status"].handler_ref
    assert about_ref is not None and about_ref.endswith("AzureConnector.about")
    assert status_ref is not None and status_ref.endswith("AzureConnector.operation_status")


@pytest.mark.asyncio
async def test_the_account_group_is_enabled_with_its_text(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_azure_typed_operations()

    groups = (
        (
            await session.execute(
                select(OperationGroup).where(
                    OperationGroup.product == AZURE_PRODUCT,
                    OperationGroup.impl_id == AZURE_IMPL_ID,
                )
            )
        )
        .scalars()
        .all()
    )
    by_key = {group.group_key: group for group in groups}
    assert set(by_key) == set(AZURE_TYPED_WHEN_TO_USE_BY_GROUP)
    assert all(group.review_status == "enabled" for group in groups)
    assert by_key["account"].when_to_use == AZURE_TYPED_WHEN_TO_USE_BY_GROUP["account"]


@pytest.mark.asyncio
async def test_search_operations_finds_azure_about(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_azure_typed_operations()

    found = await search_operations(
        operator(),
        {"connector_id": AZURE_CONNECTOR_ID, "query": "azure subscription login about"},
    )

    assert "azure.about" in {hit["op_id"] for hit in found["hits"]}


def test_every_operation_is_a_safe_read_with_guidance() -> None:
    assert {"azure.about", "azure.operation.status"} <= {op.op_id for op in AZURE_TYPED_OPS}
    for op in AZURE_TYPED_OPS:
        assert op.safety_level == "safe" and op.requires_approval is False
        assert "read-only" in op.tags
        assert op.group_key in AZURE_TYPED_WHEN_TO_USE_BY_GROUP
        assert op.parameter_schema["additionalProperties"] is False
        assert op.llm_instructions is not None
        for key in ("when_to_call", "output_shape", "next_step"):
            assert str(op.llm_instructions[key]).strip()
