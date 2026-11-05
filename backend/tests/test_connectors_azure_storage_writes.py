# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure storage writes (group ``storage``, #4169).

The dispatch tests run the real dispatcher (policy, approval, audit) with
only the Azure registrar run. Microsoft's login service and Azure Resource
Manager are mocked with respx; the answers follow the shapes of
Microsoft's documented examples. Every id and name here is made up.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from defusedxml.ElementTree import fromstring
from sqlalchemy import select

import meho_backplane.connectors.vault.ops as vault_ops
from meho_backplane.broadcast.events import classify_op, redact_payload
from meho_backplane.connectors.azure import (
    AZURE_CONNECTOR_ID,
    AZURE_IMPL_ID,
    AZURE_PRODUCT,
    AZURE_VERSION,
    AzureConnector,
    register_azure_typed_operations,
    storage,
    storage_writes,
)
from meho_backplane.connectors.azure.storage_schemas import STORAGE_DATA_ROLES
from meho_backplane.connectors.azure.storage_writes import assignment_name
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.flight_recorder import capture
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.settings import get_settings
from tests._azure_fixtures import (
    ARM_BASE,
    ARM_HOST,
    MEHO_TENANT,
    SUBSCRIPTION_A,
    AzureTargetStub,
    CountingLoader,
    login_token,
    operator,
    token_answer,
    token_url,
)

ACCOUNT = "examplestore01"
GROUP = "rg-storage-example"
CONTAINER = "iso"
PRINCIPAL = "0a1b2c3d-0000-4000-8000-00000000aaaa"
OTHER_PRINCIPAL = "0a1b2c3d-0000-4000-8000-00000000bbbb"
READER = "Storage Blob Data Reader"
READER_GUID = STORAGE_DATA_ROLES[READER]

ACCOUNTS_URL = (
    f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.Storage/storageAccounts"
)
ACCOUNT_SCOPE = (
    f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{GROUP}/providers/"
    f"Microsoft.Storage/storageAccounts/{ACCOUNT}"
)
CONTAINER_SCOPE = f"{ACCOUNT_SCOPE}/blobServices/default/containers/{CONTAINER}"
ROLE_ID = (
    f"/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.Authorization/"
    f"roleDefinitions/{READER_GUID}"
)

CREATE = {
    "account": ACCOUNT,
    "principal_id": PRINCIPAL,
    "principal_type": "User",
    "role": READER,
}
DELETE = {"account": ACCOUNT, "principal_id": PRINCIPAL, "role": READER}


def _assignment_url(scope: str, name: str) -> str:
    return f"{ARM_BASE}{scope}/providers/Microsoft.Authorization/roleAssignments/{name}"


def _list_url(scope: str) -> str:
    return f"{ARM_BASE}{scope}/providers/Microsoft.Authorization/roleAssignments"


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


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
async def azure(monkeypatch: pytest.MonkeyPatch) -> AzureConnector:
    """Run the Azure registrar and give the dispatcher's instance a stub loader."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    await register_azure_typed_operations()
    instance = get_or_create_connector_instance(AzureConnector)
    assert isinstance(instance, AzureConnector)
    instance._credentials_loader = CountingLoader()
    return instance


async def _call(op_id: str, params: dict[str, Any], *, approved: bool = True) -> OperationResult:
    return await dispatch(
        operator=operator(),
        connector_id=AZURE_CONNECTOR_ID,
        op_id=op_id,
        target=AzureTargetStub(),
        params=params,
        _approved=approved,
    )


def _login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(token_url()).respond(200, json=token_answer(login_token()))


def _accounts(mock: respx.MockRouter) -> respx.Route:
    item = {
        "id": ACCOUNT_SCOPE,
        "name": ACCOUNT,
        "type": "Microsoft.Storage/storageAccounts",
        "location": "westeurope",
        "kind": "StorageV2",
        "properties": {"primaryEndpoints": {"blob": f"https://{ACCOUNT}.blob.core.windows.net/"}},
    }
    return mock.get(url__startswith=ACCOUNTS_URL).respond(200, json={"value": [item]})


def _assignment(name: str, principal: str, scope: str = ACCOUNT_SCOPE) -> dict[str, Any]:
    return {
        "id": f"{scope}/providers/Microsoft.Authorization/roleAssignments/{name}",
        "name": name,
        "type": "Microsoft.Authorization/roleAssignments",
        "properties": {
            "roleDefinitionId": ROLE_ID,
            "principalId": principal,
            "principalType": "User",
            "scope": scope,
        },
    }


# ----------------------------------------------------------------------
# The operation table
# ----------------------------------------------------------------------


def test_the_writes_are_dangerous_and_wait_for_approval() -> None:
    writes = {op.op_id: op for op in storage.OPS if "write" in op.tags}
    assert set(writes) == {
        "azure.storage.blob.link.create",
        "azure.role.assignment.create",
        "azure.role.assignment.delete",
    }
    for op in writes.values():
        assert op.group_key == "storage"
        assert op.safety_level == "dangerous" and op.requires_approval is True
        assert callable(getattr(AzureConnector, op.handler_attr, None))
        guidance = op.llm_instructions
        assert guidance is not None
        assert {"when_to_call", "preconditions", "output_shape", "next_step"} <= set(guidance)


@pytest.mark.asyncio
async def test_the_role_writes_are_stored_as_dangerous_with_approval(
    azure: AzureConnector,
) -> None:
    op_ids = ["azure.role.assignment.create", "azure.role.assignment.delete"]
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.product == AZURE_PRODUCT,
                        EndpointDescriptor.op_id.in_(op_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert sorted(row.op_id for row in rows) == op_ids
    for row in rows:
        assert row.source_kind == "typed" and row.is_enabled is True
        assert row.safety_level == "dangerous" and row.requires_approval is True


def test_assignment_name_is_stable_and_ignores_case() -> None:
    first = assignment_name(ACCOUNT_SCOPE, PRINCIPAL, READER_GUID)
    assert first == assignment_name(ACCOUNT_SCOPE.lower(), PRINCIPAL.upper(), READER_GUID)
    assert first != assignment_name(CONTAINER_SCOPE, PRINCIPAL, READER_GUID)
    assert first != assignment_name(ACCOUNT_SCOPE, OTHER_PRINCIPAL, READER_GUID)


# ----------------------------------------------------------------------
# Create
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_puts_the_derived_name_once(azure: AzureConnector) -> None:
    name = assignment_name(ACCOUNT_SCOPE, PRINCIPAL, READER_GUID)
    with respx.mock() as mock:
        _login(mock)
        _accounts(mock)
        put = mock.put(url__startswith=_assignment_url(ACCOUNT_SCOPE, name)).respond(
            201, json=_assignment(name, PRINCIPAL)
        )
        result = await _call("azure.role.assignment.create", CREATE)

    assert result.status == "ok", result.error
    assert put.call_count == 1
    request = put.calls.last.request
    assert request.url.params["api-version"] == "2022-04-01"
    assert json.loads(request.content) == {
        "properties": {
            "roleDefinitionId": ROLE_ID,
            "principalId": PRINCIPAL,
            "principalType": "User",
        }
    }
    assert result.result["state"] == "created"
    assert result.result["scope"] == ACCOUNT_SCOPE
    assert result.result["assignment_id"].endswith(f"/roleAssignments/{name}")
    assert result.result["role"] == READER


@pytest.mark.asyncio
async def test_create_again_is_the_same_put(azure: AzureConnector) -> None:
    name = assignment_name(ACCOUNT_SCOPE, PRINCIPAL, READER_GUID)
    with respx.mock() as mock:
        _login(mock)
        _accounts(mock)
        put = mock.put(url__startswith=f"{_list_url(ACCOUNT_SCOPE)}/").respond(
            200, json=_assignment(name, PRINCIPAL)
        )
        first = await _call("azure.role.assignment.create", CREATE)
        second = await _call("azure.role.assignment.create", CREATE)

    assert first.status == second.status == "ok"
    assert second.result["state"] == "created"
    assert {call.request.url.path for call in put.calls} == {
        f"{ACCOUNT_SCOPE}/providers/Microsoft.Authorization/roleAssignments/{name}"
    }


@pytest.mark.asyncio
async def test_create_answers_already_assigned_on_409(azure: AzureConnector) -> None:
    existing = _assignment("11111111-2222-4333-8444-555555555555", PRINCIPAL)
    exists = {"error": {"code": "RoleAssignmentExists", "message": "The role assignment exists."}}
    with respx.mock() as mock:
        _login(mock)
        _accounts(mock)
        mock.put(url__startswith=f"{_list_url(ACCOUNT_SCOPE)}/").respond(409, json=exists)
        listing = mock.get(url__startswith=_list_url(ACCOUNT_SCOPE)).respond(
            200, json={"value": [_assignment("x", OTHER_PRINCIPAL), existing]}
        )
        result = await _call("azure.role.assignment.create", CREATE)

    assert result.status == "ok", result.error
    assert listing.calls.last.request.url.params["$filter"] == "atScope()"
    assert result.result["state"] == "already_assigned"
    assert result.result["assignment_id"] == existing["id"]


@pytest.mark.asyncio
async def test_container_scope_is_built_from_the_checked_account(azure: AzureConnector) -> None:
    name = assignment_name(CONTAINER_SCOPE, PRINCIPAL, READER_GUID)
    with respx.mock() as mock:
        _login(mock)
        _accounts(mock)
        put = mock.put(url__startswith=_assignment_url(CONTAINER_SCOPE, name)).respond(
            201, json=_assignment(name, PRINCIPAL, CONTAINER_SCOPE)
        )
        result = await _call("azure.role.assignment.create", {**CREATE, "container": CONTAINER})

    assert result.status == "ok", result.error
    assert put.called
    assert result.result["scope"] == CONTAINER_SCOPE


@pytest.mark.asyncio
async def test_other_role_is_refused_before_any_call(azure: AzureConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        anything = mock.route(host=ARM_HOST).respond(200, json={})
        result = await _call("azure.role.assignment.create", {**CREATE, "role": "Owner"})

    assert result.status == "error"
    assert not login.called
    assert not anything.called


# ----------------------------------------------------------------------
# Delete
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_every_match_and_counts_204_as_gone(azure: AzureConnector) -> None:
    one = _assignment("aaaaaaaa-0000-4000-8000-000000000001", PRINCIPAL)
    two = _assignment("aaaaaaaa-0000-4000-8000-000000000002", PRINCIPAL)
    other = _assignment("aaaaaaaa-0000-4000-8000-000000000003", OTHER_PRINCIPAL)
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _accounts(mock)
        mock.get(url__startswith=_list_url(ACCOUNT_SCOPE)).respond(
            200, json={"value": [one, other, two]}
        )
        gone_one = mock.delete(url__startswith=f"{ARM_BASE}{one['id']}").respond(200, json=one)
        gone_two = mock.delete(url__startswith=f"{ARM_BASE}{two['id']}").respond(204)
        untouched = mock.delete(url__startswith=f"{ARM_BASE}{other['id']}").respond(200)
        result = await _call("azure.role.assignment.delete", DELETE)

    assert result.status == "ok", result.error
    assert gone_one.called and gone_two.called
    assert not untouched.called
    assert result.result["deleted"] == [one["id"]]
    assert result.result["not_found"] == [two["id"]]
    assert result.result["scope"] == ACCOUNT_SCOPE


# ----------------------------------------------------------------------
# Approval
# ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params"),
    [("azure.role.assignment.create", CREATE), ("azure.role.assignment.delete", DELETE)],
)
async def test_both_ops_wait_for_approval(
    azure: AzureConnector, op_id: str, params: dict[str, Any]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        anything = mock.route(host=ARM_HOST).respond(200, json={})
        result = await _call(op_id, params, approved=False)

    assert result.status == "awaiting_approval", result
    assert not login.called
    assert not anything.called


# ----------------------------------------------------------------------
# The download link (azure.storage.blob.link.create)
# ----------------------------------------------------------------------

BLOB_HOST = f"{ACCOUNT}.blob.core.windows.net"
FILE_NAME = "esxi/VMware-ESXi 8.0.iso"
FILE_URL = f"https://{BLOB_HOST}/{CONTAINER}/esxi/VMware-ESXi%208.0.iso"
KEY_URL = f"https://{BLOB_HOST}/?restype=service&comp=userdelegationkey"
ALLOWED_IP = "198.51.100.7"
STORE_AS = "esxi-iso"
LINK_REF = f"tenants/{MEHO_TENANT}/azure-blob-links/{STORE_AS}"
NOW = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)

#: A made-up user delegation key value. It may reach no result, trace or audit row.
KEY_VALUE = base64.b64encode(b"made-up-delegation-key-value-for-tests-0001").decode()

LINK = {
    "account": ACCOUNT,
    "container": CONTAINER,
    "name": FILE_NAME,
    "allowed_ip": ALLOWED_IP,
    "store_as": STORE_AS,
}


def _key_xml(start: str = "2026-11-05T09:55:00Z", expiry: str = "2026-11-05T14:00:00Z") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?><UserDelegationKey>'
        "<SignedOid>11111111-2222-3333-4444-555555555555</SignedOid>"
        "<SignedTid>66666666-7777-8888-9999-000000000000</SignedTid>"
        f"<SignedStart>{start}</SignedStart><SignedExpiry>{expiry}</SignedExpiry>"
        "<SignedService>b</SignedService><SignedVersion>2026-10-06</SignedVersion>"
        f"<Value>{KEY_VALUE}</Value></UserDelegationKey>"
    )


@pytest.fixture
def vault_put(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    put = AsyncMock(return_value={"version": 1})
    monkeypatch.setattr(vault_ops, "vault_kv_put", put)
    return put


@pytest.fixture
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(storage_writes, "_utcnow", lambda: NOW)
    return NOW


def _blob_head(mock: respx.MockRouter, status: int = 200) -> respx.Route:
    return mock.head(url__startswith=FILE_URL).respond(
        status,
        headers={"Content-Length": "1048576", "Content-Type": "application/octet-stream"},
    )


def _key(mock: respx.MockRouter, status: int = 200, body: str | None = None) -> respx.Route:
    return mock.post(url__startswith=KEY_URL).respond(
        status,
        text=_key_xml() if body is None else body,
        headers={"Content-Type": "application/xml"},
    )


def _stored_link(put: AsyncMock) -> str:
    (args, _kwargs) = put.call_args
    data: dict[str, str] = args[2]["data"]
    return data["url"]


@pytest.mark.asyncio
async def test_link_create_stores_the_link_in_vault_and_returns_only_the_ref(
    azure: AzureConnector, vault_put: AsyncMock, fixed_now: datetime
) -> None:
    with respx.mock() as mock:
        _login(mock)
        _accounts(mock)
        head = _blob_head(mock)
        key = _key(mock)
        result = await _call("azure.storage.blob.link.create", LINK)

    assert result.status == "ok", result.error
    assert head.called and key.call_count == 1
    assert key.calls.last.request.headers["x-ms-version"] == "2026-10-06"
    vault_put.assert_awaited_once()
    (args, _kwargs) = vault_put.call_args
    assert args[2]["mount"] == "secret"
    assert args[2]["path"] == LINK_REF
    assert set(args[2]["data"]) == {"url"}
    link = _stored_link(vault_put)
    assert link.startswith(f"{FILE_URL}?")
    assert "sig=" in link and f"sip={ALLOWED_IP}" in link
    answer = result.result
    assert answer["secret_ref"] == LINK_REF
    assert answer["field"] == "url"
    assert answer["allowed_ip"] == ALLOWED_IP
    assert answer["account"] == {"name": ACCOUNT, "resource_group": GROUP}
    assert answer["length"] == len(link.encode())
    assert answer["value_fingerprint"]
    dumped = json.dumps(answer)
    assert "sig=" not in dumped and link not in dumped and KEY_VALUE not in dumped


@pytest.mark.asyncio
async def test_link_times_follow_now_and_hours(
    azure: AzureConnector, vault_put: AsyncMock, fixed_now: datetime
) -> None:
    with respx.mock() as mock:
        _login(mock)
        _accounts(mock)
        _blob_head(mock)
        key = _key(mock, body=_key_xml(expiry="2026-11-05T16:00:00Z"))
        result = await _call("azure.storage.blob.link.create", {**LINK, "hours": 6})

    assert result.status == "ok", result.error
    sent = fromstring(key.calls.last.request.content)
    assert sent.findtext("Start") == "2026-11-05T09:55:00Z"
    assert sent.findtext("Expiry") == "2026-11-05T16:00:00Z"
    link = _stored_link(vault_put)
    assert "st=2026-11-05T09%3A55%3A00Z" in link
    assert "se=2026-11-05T16%3A00%3A00Z" in link
    assert result.result["expires_at"] == "2026-11-05T16:00:00Z"


@pytest.mark.asyncio
async def test_link_create_trace_keeps_no_key_and_no_link(
    azure: AzureConnector,
    vault_put: AsyncMock,
    fixed_now: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans: list[Any] = []

    async def _record_trace(**kwargs: Any) -> None:
        spans.extend(kwargs["spans"])

    monkeypatch.setattr(capture, "should_capture", AsyncMock(return_value=True))
    monkeypatch.setattr(capture, "record_trace", _record_trace)
    with respx.mock() as mock:
        _login(mock)
        _accounts(mock)
        _blob_head(mock)
        _key(mock)
        result = await _call("azure.storage.blob.link.create", LINK)

    assert result.status == "ok", result.error
    assert spans, "the flight recorder recorded nothing"
    traced = json.dumps([dataclasses.asdict(span) for span in spans], default=str)
    link = _stored_link(vault_put)
    assert KEY_VALUE not in traced
    assert link not in traced
    assert "sig=" not in traced


@pytest.mark.asyncio
async def test_link_create_audit_and_broadcast_hold_no_link(
    azure: AzureConnector, vault_put: AsyncMock, fixed_now: datetime
) -> None:
    with respx.mock() as mock:
        _login(mock)
        _accounts(mock)
        _blob_head(mock)
        _key(mock)
        result = await _call("azure.storage.blob.link.create", LINK)

    assert result.status == "ok", result.error
    link = _stored_link(vault_put)
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.path == "azure.storage.blob.link.create")
                )
            )
            .scalars()
            .all()
        )
    assert rows
    audited = json.dumps([row.payload for row in rows], default=str)
    assert link not in audited and KEY_VALUE not in audited and "sig=" not in audited
    broadcast = redact_payload(
        classify_op("azure.storage.blob.link.create"),
        {"params": LINK, "result": result.result},
        "ok",
    )
    assert broadcast == {"op_class": "credential_mint", "result_status": "ok"}


@pytest.mark.asyncio
async def test_vault_write_failure_names_the_path_not_the_link(
    azure: AzureConnector, fixed_now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def _refuse(operator: Any, target: Any, params: dict[str, Any]) -> dict[str, Any]:
        seen.append(params["data"]["url"])
        raise RuntimeError(f"permission denied writing {params['data']['url']}")

    monkeypatch.setattr(vault_ops, "vault_kv_put", _refuse)
    with respx.mock() as mock:
        _login(mock)
        _accounts(mock)
        _blob_head(mock)
        _key(mock)
        result = await _call("azure.storage.blob.link.create", LINK)

    assert result.status == "error"
    (link,) = seen
    shown = json.dumps(result.model_dump(mode="json"), default=str)
    assert LINK_REF in shown
    assert link not in shown and "sig=" not in shown and KEY_VALUE not in shown


@pytest.mark.asyncio
async def test_missing_blob_stops_before_the_key_request(
    azure: AzureConnector, vault_put: AsyncMock, fixed_now: datetime
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        _accounts(mock)
        _blob_head(mock, status=404)
        key = _key(mock)
        result = await _call("azure.storage.blob.link.create", LINK)

    assert result.status == "not_found", result
    assert not key.called
    vault_put.assert_not_awaited()


@pytest.mark.asyncio
async def test_key_request_403_names_the_role(
    azure: AzureConnector, vault_put: AsyncMock, fixed_now: datetime
) -> None:
    refused = (
        '<?xml version="1.0" encoding="utf-8"?><Error><Code>AuthorizationPermissionMismatch'
        "</Code><Message>This request is not authorized.</Message></Error>"
    )
    with respx.mock() as mock:
        _login(mock)
        _accounts(mock)
        _blob_head(mock)
        _key(mock, status=403, body=refused)
        result = await _call("azure.storage.blob.link.create", LINK)

    assert result.status == "error"
    message = str(result.extras["exception_message"])
    assert "Storage Blob Data Reader" in message and "Storage Blob Delegator" in message
    vault_put.assert_not_awaited()


def test_link_op_is_credential_mint() -> None:
    assert classify_op("azure.storage.blob.link.create") == "credential_mint"
