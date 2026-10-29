# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure storage reads (group ``storage``).

The dispatch tests run the real dispatcher (policy, audit, result reduction)
with only the Azure registrar run. Microsoft's login service, Azure Resource
Manager and the blob data plane are mocked with respx. The answers follow
the shapes in Microsoft's documented examples: JSON on Resource Manager, XML
and headers on the blob host. Every id, name, key and secret here is made up.

The tests with more than 50 rows install the real result reducer
(``JsonFluxReducer``) and prove that the rows go to a stored result while
the fields next to them (count, the paging fields, ``account``) stay inline.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.operations.result_query as result_query_module
from meho_backplane.connectors.azure import (
    AZURE_CONNECTOR_ID,
    AZURE_IMPL_ID,
    AZURE_PRODUCT,
    AZURE_TYPED_OPS,
    AZURE_TYPED_WHEN_TO_USE_BY_GROUP,
    AZURE_VERSION,
    AzureConnector,
    register_azure_typed_operations,
    storage,
    storage_reads,
)
from meho_backplane.connectors.azure.storage_parse import (
    MAX_TEXT_BYTES,
    StorageParameterError,
    blob_host,
    blob_name,
    blob_page,
    container_name,
    http_date_to_iso,
    md5_hex,
    resource_group_name,
    text_content,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.delete_names import write_word_in_op_id
from meho_backplane.jsonflux.query.contract import ResultQuerySpec
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings
from tests._azure_fixtures import (
    ARM_BASE,
    ARM_SCOPE,
    SUBSCRIPTION_A,
    AzureTargetStub,
    CountingLoader,
    login_token,
    operator,
    token_answer,
    token_url,
)
from tests._result_store_helpers import MemoryResultStore

ACCOUNT = "examplestore01"
GROUP = "rg-storage-example"
CONTAINER = "backups"
BLOB_HOST = f"{ACCOUNT}.blob.core.windows.net"
STORAGE_SCOPE = "https://storage.azure.com/.default"

ACCOUNTS_URL = (
    f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.Storage/storageAccounts"
)
CONTAINERS_URL = (
    f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{GROUP}/providers/"
    f"Microsoft.Storage/storageAccounts/{ACCOUNT}/blobServices/default/containers"
)
BLOB_LIST_URL = f"https://{BLOB_HOST}/{CONTAINER}"
FILE_NAME = "2026-10/SHA256SUMS"
FILE_URL = f"https://{BLOB_HOST}/{CONTAINER}/{FILE_NAME}"
LIST_PARAMS = {"account": ACCOUNT, "container": CONTAINER}
READ_PARAMS = {"account": ACCOUNT, "container": CONTAINER, "name": FILE_NAME}

ARM_TOKEN = login_token(n=1)
STORAGE_TOKEN = login_token(n=2)

STORAGE_OP_IDS = [
    "azure.storage.account.list",
    "azure.storage.container.list",
    "azure.storage.blob.list",
    "azure.storage.blob.read",
]

#: Made-up values that look like storage secrets. None may reach a result.
FAKE_ACCOUNT_KEY = "ZmFrZS1hY2NvdW50LWtleS1mb3ItdGVzdHMtb25seQ=="
FAKE_CONNECTION = (
    f"DefaultEndpointsProtocol=https;AccountName={ACCOUNT};"
    f"AccountKey={FAKE_ACCOUNT_KEY};EndpointSuffix=core.windows.net"
)
FAKE_SAS_QUERY = "sv=2026-10-06&se=2026-10-28T00%3A00%3A00Z&sr=b&sp=r&sig=ZmFrZS1zaWc%3D"

TEXT = b"0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0  backup.tar\n"


def _md5_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()


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


@pytest.fixture
async def registered(_stub_embedding: AsyncMock, session: AsyncSession) -> AzureConnector:
    """Run the Azure registrar and give the dispatcher's instance a stub loader."""
    await register_azure_typed_operations()
    instance = get_or_create_connector_instance(AzureConnector)
    assert isinstance(instance, AzureConnector)
    instance._credentials_loader = CountingLoader()
    return instance


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryResultStore:
    """The real result reducer over an in-memory store that result_query reads."""
    memory = MemoryResultStore()
    set_default_reducer(JsonFluxReducer(store=memory))
    monkeypatch.setattr(result_query_module, "get_result_handle_store", lambda: memory)
    return memory


async def _call(
    op_id: str, params: dict[str, Any] | None = None, target: AzureTargetStub | None = None
) -> OperationResult:
    return await dispatch(
        operator=operator(),
        connector_id=AZURE_CONNECTOR_ID,
        op_id=op_id,
        target=target or AzureTargetStub(),
        params=params or {},
    )


async def _query(result: OperationResult, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Read rows of the stored result with ``result_query``."""
    assert result.handle is not None
    query = ResultQuerySpec.model_validate(spec)
    answer = await result_query_module.run_result_query(operator(), result.handle.handle_id, query)
    rows: list[dict[str, Any]] = answer["rows"]
    return rows


def _login(mock: respx.MockRouter) -> respx.Route:
    """Answer each token request with the token of the scope it asks for."""

    def answer(request: httpx.Request) -> httpx.Response:
        scope = parse_qs(request.content.decode())["scope"][0]
        token = STORAGE_TOKEN if scope == STORAGE_SCOPE else ARM_TOKEN
        return httpx.Response(200, json=token_answer(token))

    return mock.post(token_url()).mock(side_effect=answer)


def _bearer(request: httpx.Request) -> str:
    return request.headers["Authorization"].removeprefix("Bearer ")


# ----------------------------------------------------------------------
# Answers shaped like Microsoft's documented examples, with made-up values
# ----------------------------------------------------------------------


def _account_item(
    name: str = ACCOUNT,
    *,
    shared_key: bool | None = False,
    blob_endpoint: str | None = None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "provisioningState": "Succeeded",
        "creationTime": "2026-01-05T10:00:00.0000000Z",
        "accessTier": "Hot",
        "publicNetworkAccess": "Disabled",
        "networkAcls": {
            "bypass": "AzureServices",
            "defaultAction": "Deny",
            "ipRules": [],
            "virtualNetworkRules": [],
        },
        "primaryEndpoints": {
            "blob": blob_endpoint or f"https://{name}.blob.core.windows.net/",
            "dfs": f"https://{name}.dfs.core.windows.net/",
            "queue": f"https://{name}.queue.core.windows.net/",
        },
        "supportsHttpsTrafficOnly": True,
        "minimumTlsVersion": "TLS1_2",
    }
    if shared_key is not None:
        properties["allowSharedKeyAccess"] = shared_key
    return {
        "id": (
            f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{GROUP}/providers/"
            f"Microsoft.Storage/storageAccounts/{name}"
        ),
        "name": name,
        "type": "Microsoft.Storage/storageAccounts",
        "location": "westeurope",
        "kind": "StorageV2",
        "sku": {"name": "Standard_LRS", "tier": "Standard"},
        "tags": {},
        "properties": properties,
    }


def _container_item(name: str, *, legal_hold: bool = False) -> dict[str, Any]:
    return {
        "id": (
            f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{GROUP}/providers/"
            f"Microsoft.Storage/storageAccounts/{ACCOUNT}/blobServices/default/containers/{name}"
        ),
        "name": name,
        "type": "Microsoft.Storage/storageAccounts/blobServices/containers",
        "etag": '"0x8DE00000000AAAA"',
        "properties": {
            "publicAccess": "None",
            "leaseStatus": "Unlocked",
            "leaseState": "Available",
            "lastModifiedTime": "2026-10-01T08:00:00Z",
            "hasImmutabilityPolicy": False,
            "hasLegalHold": legal_hold,
            "immutableStorageWithVersioning": {"enabled": False},
            "deleted": False,
            "remainingRetentionDays": 0,
            "defaultEncryptionScope": "$account-encryption-key",
            "denyEncryptionScopeOverride": False,
        },
    }


def _blob_xml(
    name: str,
    data: bytes = TEXT,
    *,
    size: int | None = None,
    tier: str = "Hot",
    encoded: bool = False,
) -> str:
    name_element = f'<Name Encoded="true">{name}</Name>' if encoded else f"<Name>{name}</Name>"
    return (
        f"<Blob>{name_element}<Properties>"
        "<Creation-Time>Thu, 01 Oct 2026 08:00:00 GMT</Creation-Time>"
        "<Last-Modified>Fri, 02 Oct 2026 09:30:00 GMT</Last-Modified>"
        "<Etag>0x8DE00000000BBBB</Etag>"
        f"<Content-Length>{len(data) if size is None else size}</Content-Length>"
        "<Content-Type>text/plain</Content-Type><Content-Encoding /><Content-Language />"
        f"<Content-MD5>{_md5_b64(data)}</Content-MD5><Cache-Control /><Content-Disposition />"
        f"<BlobType>BlockBlob</BlobType><AccessTier>{tier}</AccessTier>"
        "<AccessTierInferred>true</AccessTierInferred><LeaseStatus>unlocked</LeaseStatus>"
        "<LeaseState>available</LeaseState><ServerEncrypted>true</ServerEncrypted>"
        "</Properties><OrMetadata /></Blob>"
    )


def _list_xml(blobs: list[str], next_marker: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<EnumerationResults ServiceEndpoint="https://{BLOB_HOST}/" ContainerName="{CONTAINER}">'
        f"<MaxResults>5000</MaxResults><Blobs>{''.join(blobs)}</Blobs>"
        f"<NextMarker>{next_marker}</NextMarker></EnumerationResults>"
    ).encode()


def _xml_answer(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"Content-Type": "application/xml"})


def _head_headers(
    size: int,
    data: bytes = TEXT,
    *,
    content_type: str = "text/plain",
    tier: str = "Hot",
    etag: str = '"0x8DE00000000CCCC"',
) -> dict[str, str]:
    return {
        "Content-Length": str(size),
        "Content-Type": content_type,
        "Content-MD5": _md5_b64(data),
        "Last-Modified": "Fri, 02 Oct 2026 09:30:00 GMT",
        "ETag": etag,
        "x-ms-blob-type": "BlockBlob",
        "x-ms-access-tier": tier,
        "x-ms-version": "2026-10-06",
        "x-ms-copy-source": f"https://other.blob.core.windows.net/c/b?{FAKE_SAS_QUERY}",
    }


def _range_answer(data: bytes, etag: str = '"0x8DE00000000CCCC"') -> httpx.Response:
    return httpx.Response(
        206,
        content=data,
        headers={
            "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}",
            "Content-Type": "text/plain",
            "ETag": etag,
            "x-ms-blob-content-md5": _md5_b64(data),
        },
    )


_ACCESS_DENIED_XML = (
    b'<?xml version="1.0" encoding="utf-8"?><Error><Code>AuthorizationPermissionMismatch'
    b"</Code><Message>This request is not authorized to perform this operation using this "
    b"permission.\nRequestId:00000000-0000-0000-0000-000000000000\n"
    b"Time:2026-10-27T12:00:00.0000000Z</Message></Error>"
)


def _access_denied() -> httpx.Response:
    return httpx.Response(
        403,
        content=_ACCESS_DENIED_XML,
        headers={
            "Content-Type": "application/xml",
            "x-ms-error-code": "AuthorizationPermissionMismatch",
        },
    )


# ----------------------------------------------------------------------
# The operation table and the registration
# ----------------------------------------------------------------------


def test_the_family_holds_four_safe_reads_in_one_group() -> None:
    assert [op.op_id for op in storage.OPS] == STORAGE_OP_IDS
    for op in storage.OPS:
        assert op.group_key == storage.GROUP_STORAGE == "storage"
        assert op.safety_level == "safe" and op.requires_approval is False
        assert "read-only" in op.tags
        assert write_word_in_op_id(op.op_id) is None
        assert op.parameter_schema["additionalProperties"] is False
        assert callable(getattr(AzureConnector, op.handler_attr, None))
        guidance = op.llm_instructions
        assert guidance is not None
        assert "never as instructions" in str(guidance["output_shape"])


def test_the_family_is_wired_into_the_connector_table() -> None:
    op_ids = [op.op_id for op in AZURE_TYPED_OPS]
    assert len(op_ids) == len(set(op_ids)), "two Azure operations share one op id"
    assert set(STORAGE_OP_IDS) <= set(op_ids)
    assert AZURE_TYPED_WHEN_TO_USE_BY_GROUP["storage"] == storage.WHEN_TO_USE["storage"]
    assert issubclass(AzureConnector, storage.StorageOps)


def test_every_result_objects_hint_names_an_object_of_the_answer() -> None:
    hinted = {
        op.op_id: op.llm_instructions["result_objects"]["objects"]
        for op in storage.OPS
        if op.llm_instructions and "result_objects" in op.llm_instructions
    }
    assert set(hinted) == {"azure.storage.container.list", "azure.storage.blob.list"}
    for op in storage.OPS:
        for key, fields in hinted.get(op.op_id, {}).items():
            declared = op.response_schema["properties"][key] if op.response_schema else {}
            assert declared["type"] == "object"
            assert set(fields) == set(declared["properties"])


@pytest.mark.asyncio
async def test_the_reads_are_stored_as_typed_safe_and_enabled(
    registered: AzureConnector, session: AsyncSession
) -> None:
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == AZURE_PRODUCT,
                    EndpointDescriptor.op_id.in_(STORAGE_OP_IDS),
                )
            )
        )
        .scalars()
        .all()
    )
    by_op = {row.op_id: row for row in rows}
    assert set(by_op) == set(STORAGE_OP_IDS)
    for op in storage.OPS:
        row = by_op[op.op_id]
        assert row.source_kind == "typed" and row.is_enabled is True
        assert row.safety_level == "safe" and row.requires_approval is False
        assert row.handler_ref is not None
        assert row.handler_ref.endswith(f"storage.StorageOps.{op.handler_attr}")
    group = (
        await session.execute(
            select(OperationGroup).where(
                OperationGroup.impl_id == AZURE_IMPL_ID, OperationGroup.group_key == "storage"
            )
        )
    ).scalar_one()
    assert group.review_status == "enabled"
    assert group.when_to_use == storage.WHEN_TO_USE["storage"]


# ----------------------------------------------------------------------
# Storage accounts (Resource Manager)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_list_shows_kind_sku_tier_and_the_access_flags(
    registered: AzureConnector, store: MemoryResultStore
) -> None:
    other = _account_item("examplestore02", shared_key=None)
    other["properties"]["publicNetworkAccess"] = "Enabled"
    other["properties"]["networkAcls"]["defaultAction"] = "Allow"

    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item(), other]})
        result = await _call("azure.storage.account.list")

    assert result.status == "ok", result.error
    assert result.handle is None  # two rows stay inline, also with the real reducer
    assert arm.calls[0].request.url.params["api-version"] == "2026-09-01"
    assert result.result["count"] == 2
    assert result.result["value"] == [
        {
            "name": ACCOUNT,
            "resource_group": GROUP,
            "location": "westeurope",
            "kind": "StorageV2",
            "sku": "Standard_LRS",
            "access_tier": "Hot",
            "shared_key_access": False,
            "public_network_access": "Disabled",
            "network_default_action": "Deny",
        },
        {
            "name": "examplestore02",
            "resource_group": GROUP,
            "location": "westeurope",
            "kind": "StorageV2",
            "sku": "Standard_LRS",
            "access_tier": "Hot",
            # Azure documents an empty allowSharedKeyAccess as true.
            "shared_key_access": True,
            "public_network_access": "Enabled",
            "network_default_action": "Allow",
        },
    ]


@pytest.mark.asyncio
async def test_no_key_sas_token_or_connection_string_reaches_a_result(
    registered: AzureConnector,
) -> None:
    leaky = _account_item()
    leaky["keys"] = [{"keyName": "key1", "value": FAKE_ACCOUNT_KEY, "permissions": "FULL"}]
    leaky["properties"]["primaryKey"] = FAKE_ACCOUNT_KEY
    leaky["properties"]["connectionString"] = FAKE_CONNECTION
    leaky["properties"]["sasToken"] = FAKE_SAS_QUERY

    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [leaky]})
        result = await _call("azure.storage.account.list")

    assert result.status == "ok", result.error
    text = json.dumps(result.result)
    for secret in ("ZmFrZS1", "AccountKey", "sig=", "keyName", "primaryKey", "sasToken"):
        assert secret not in text, secret


@pytest.mark.asyncio
async def test_account_list_over_50_rows_goes_to_a_stored_result(
    registered: AzureConnector, store: MemoryResultStore
) -> None:
    items = [
        _account_item(f"examplestore{n:03d}", shared_key=None if n < 5 else False)
        for n in range(60)
    ]

    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": items})
        result = await _call("azure.storage.account.list")

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["row_count"] == 60
    assert summary["count"] == 60
    assert summary["truncated"] is False
    assert summary["pages_read"] == 1
    keys_on = {"filter": [{"field": "shared_key_access", "op": "=", "value": True}]}
    rows = await _query(result, keys_on)
    assert sorted(row["name"] for row in rows) == [f"examplestore{n:03d}" for n in range(5)]


# ----------------------------------------------------------------------
# Containers (Resource Manager)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_container_list_shows_access_lease_and_hold_flags(
    registered: AzureConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        arm = mock.get(CONTAINERS_URL).respond(
            200, json={"value": [_container_item(CONTAINER, legal_hold=True)]}
        )
        result = await _call(
            "azure.storage.container.list", {"resource_group": GROUP, "account": ACCOUNT}
        )

    assert result.status == "ok", result.error
    assert _bearer(arm.calls[0].request) == ARM_TOKEN
    assert arm.calls[0].request.url.params["api-version"] == "2026-09-01"
    assert result.result["account"] == {"name": ACCOUNT, "resource_group": GROUP}
    assert result.result["value"] == [
        {
            "name": CONTAINER,
            "public_access": "None",
            "last_modified": "2026-10-01T08:00:00Z",
            "lease_state": "Available",
            "has_immutability_policy": False,
            "has_legal_hold": True,
            "version_immutability": False,
        }
    ]


@pytest.mark.asyncio
async def test_container_list_over_50_rows_keeps_the_account_next_to_the_stored_result(
    registered: AzureConnector, store: MemoryResultStore
) -> None:
    page = {"value": [_container_item(f"logs-{n:03d}", legal_hold=n < 3) for n in range(60)]}

    with respx.mock() as mock:
        _login(mock)
        mock.get(CONTAINERS_URL).respond(200, json=page)
        result = await _call(
            "azure.storage.container.list", {"resource_group": GROUP, "account": ACCOUNT}
        )

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["row_count"] == 60
    assert summary["count"] == 60
    assert summary["account"] == {"name": ACCOUNT, "resource_group": GROUP}
    rows = await _query(result, {"filter": [{"field": "has_legal_hold", "op": "=", "value": True}]})
    assert sorted(row["name"] for row in rows) == ["logs-000", "logs-001", "logs-002"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"resource_group": "rg/../other", "account": ACCOUNT},
        {"resource_group": "rg-ends-with-dot.", "account": ACCOUNT},
        {"resource_group": GROUP, "account": "Not-A-Storage-Name"},
    ],
)
async def test_container_list_refuses_a_bad_group_or_account_before_any_request(
    params: dict[str, Any],
) -> None:
    connector = AzureConnector(credentials_loader=CountingLoader())
    with respx.mock() as mock, pytest.raises(StorageParameterError, match="MEHO sent no request"):
        await storage_reads.container_list_impl(connector, operator(), AzureTargetStub(), params)
    assert not mock.calls


# ----------------------------------------------------------------------
# Blob list (blob data plane)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blob_list_sends_the_storage_token_to_the_blob_host_and_the_arm_token_to_arm(
    registered: AzureConnector,
) -> None:
    with respx.mock() as mock:
        login = _login(mock)
        arm = mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        blobs = mock.get(BLOB_LIST_URL).mock(
            return_value=_xml_answer(_list_xml([_blob_xml(FILE_NAME)]))
        )
        result = await _call("azure.storage.blob.list", LIST_PARAMS)

    assert result.status == "ok", result.error
    assert _bearer(arm.calls[0].request) == ARM_TOKEN
    data_request = blobs.calls[0].request
    assert data_request.url.host == BLOB_HOST
    assert _bearer(data_request) == STORAGE_TOKEN
    assert data_request.headers["x-ms-version"] == "2026-10-06"
    assert dict(data_request.url.params) == {
        "restype": "container",
        "comp": "list",
        "maxresults": "5000",
    }
    scopes = sorted(parse_qs(call.request.content.decode())["scope"][0] for call in login.calls)
    assert scopes == sorted([ARM_SCOPE, STORAGE_SCOPE])


@pytest.mark.asyncio
async def test_blob_list_returns_size_time_type_tier_and_the_md5_as_hex(
    registered: AzureConnector, store: MemoryResultStore
) -> None:
    odd_name = "odd%EF%BF%BEname.txt"  # U+FFFE, which XML cannot carry, percent-encoded
    page = _list_xml([_blob_xml(FILE_NAME), _blob_xml(odd_name, b"x", tier="Cool", encoded=True)])

    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        blobs = mock.get(BLOB_LIST_URL).mock(return_value=_xml_answer(page))
        result = await _call(
            "azure.storage.blob.list",
            {"account": ACCOUNT, "container": CONTAINER, "prefix": "2026-10/"},
        )

    assert result.status == "ok", result.error
    assert result.handle is None
    assert blobs.calls[0].request.url.params["prefix"] == "2026-10/"
    answer = result.result
    assert answer["account"] == {"name": ACCOUNT, "resource_group": GROUP}
    assert answer["container"] == CONTAINER and answer["prefix"] == "2026-10/"
    assert answer["value"] == [
        {
            "name": FILE_NAME,
            "size": len(TEXT),
            "last_modified": "2026-10-02T09:30:00Z",
            "content_md5": hashlib.md5(TEXT, usedforsecurity=False).hexdigest(),
            "content_type": "text/plain",
            "access_tier": "Hot",
        },
        {
            "name": "odd￾name.txt",
            "size": 1,
            "last_modified": "2026-10-02T09:30:00Z",
            "content_md5": hashlib.md5(b"x", usedforsecurity=False).hexdigest(),
            "content_type": "text/plain",
            "access_tier": "Cool",
        },
    ]
    assert answer["truncated"] is False and answer["pages_read"] == 1


@pytest.mark.asyncio
async def test_blob_list_follows_next_marker_to_the_end_of_the_list(
    registered: AzureConnector,
) -> None:
    marker = "2!88!MDAwMDE4IWV4YW1wbGUvZmlsZS0wMDIhMDAwMDI4ITk5OTktMTItMzFUMjM6NTk6NTkuOTk5OTk5OVoh"
    first = _list_xml([_blob_xml("file-000"), _blob_xml("file-001")], next_marker=marker)
    second = _list_xml([_blob_xml("file-002")])

    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        blobs = mock.get(BLOB_LIST_URL).mock(side_effect=[_xml_answer(first), _xml_answer(second)])
        result = await _call("azure.storage.blob.list", LIST_PARAMS)

    assert result.status == "ok", result.error
    assert blobs.call_count == 2
    assert "marker" not in blobs.calls[0].request.url.params
    assert blobs.calls[1].request.url.params["marker"] == marker
    assert blobs.calls[1].request.url.params["maxresults"] == "4998"
    assert [row["name"] for row in result.result["value"]] == ["file-000", "file-001", "file-002"]
    assert result.result["pages_read"] == 2
    assert result.result["truncated"] is False


@pytest.mark.asyncio
async def test_blob_list_stops_at_max_results_and_says_so(registered: AzureConnector) -> None:
    page = _list_xml([_blob_xml(f"file-{n:03d}") for n in range(3)], next_marker="more")

    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        blobs = mock.get(BLOB_LIST_URL).mock(return_value=_xml_answer(page))
        result = await _call(
            "azure.storage.blob.list",
            {"account": ACCOUNT, "container": CONTAINER, "max_results": 3},
        )

    assert result.status == "ok", result.error
    assert blobs.call_count == 1
    assert blobs.calls[0].request.url.params["maxresults"] == "3"
    assert result.result["count"] == 3
    assert result.result["truncated"] is True
    assert result.result["omission_kind"] == "row_limit"


@pytest.mark.asyncio
async def test_blob_list_over_50_rows_keeps_the_account_next_to_the_stored_result(
    registered: AzureConnector, store: MemoryResultStore
) -> None:
    page = _list_xml([_blob_xml(f"logs/file-{n:03d}.txt", size=100 + n) for n in range(60)])

    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        mock.get(BLOB_LIST_URL).mock(return_value=_xml_answer(page))
        result = await _call("azure.storage.blob.list", LIST_PARAMS)

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["row_count"] == 60
    assert summary["count"] == 60
    assert summary["account"] == {"name": ACCOUNT, "resource_group": GROUP}
    assert summary["container"] == CONTAINER
    assert summary["truncated"] is False
    rows = await _query(result, {"filter": [{"field": "size", "op": ">", "value": 150}]})
    assert len(rows) == 9


@pytest.mark.asyncio
async def test_blob_list_403_names_the_data_role_the_login_needs(
    registered: AzureConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        mock.get(BLOB_LIST_URL).mock(return_value=_access_denied())
        result = await _call("azure.storage.blob.list", LIST_PARAMS)

    assert result.status == "error"
    assert result.extras is not None
    assert result.extras["http_status"] == 403
    assert "Storage Blob Data Reader" in result.extras["exception_message"]
    assert "Storage Blob Data Reader" in result.extras["remediation"]
    assert "Reader role alone" in result.extras["remediation"]


@pytest.mark.asyncio
async def test_a_missing_container_is_not_found(registered: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        mock.get(BLOB_LIST_URL).respond(
            404, content=b"<Error><Code>ContainerNotFound</Code></Error>"
        )
        result = await _call("azure.storage.blob.list", LIST_PARAMS)

    assert result.status == "not_found"
    assert result.error is not None and "no container 'backups'" in result.error


@pytest.mark.asyncio
async def test_an_account_outside_the_subscription_is_never_called(
    registered: AzureConnector,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item("examplestore99")]})
        blobs = mock.get(BLOB_LIST_URL).mock(return_value=_xml_answer(_list_xml([])))
        result = await _call("azure.storage.blob.list", LIST_PARAMS)

    assert result.status == "not_found"
    assert blobs.call_count == 0


@pytest.mark.asyncio
async def test_an_account_with_another_blob_endpoint_is_never_called(
    registered: AzureConnector,
) -> None:
    zoned = _account_item(blob_endpoint=f"https://{ACCOUNT}.z07.blob.storage.azure.net/")

    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [zoned]})
        blobs = mock.get(BLOB_LIST_URL).mock(return_value=_xml_answer(_list_xml([])))
        result = await _call("azure.storage.blob.list", LIST_PARAMS)

    assert result.status == "error"
    assert result.extras is not None
    assert result.extras["exception_class"] == "StorageReadError"
    assert blobs.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "evil.example.com",
        f"{ACCOUNT}.blob.core.windows.net",
        "examplestore01@evil",
        "examplestore01:8443",
        "examplestore01/x",
        "Examplestore01",
        "ab",
        "a" * 25,
        "",
        None,
        7,
    ],
)
async def test_the_blob_host_is_built_only_from_a_checked_account_name(bad: object) -> None:
    """A parameter can never change the host: a bad account name stops every request."""
    connector = AzureConnector(credentials_loader=CountingLoader())
    params = {"account": bad, "container": CONTAINER}
    with respx.mock() as mock, pytest.raises(StorageParameterError, match="'account'"):
        await storage_reads.blob_list_impl(connector, operator(), AzureTargetStub(), params)
    assert not mock.calls
    if isinstance(bad, str):
        with pytest.raises(ValueError, match="checked storage account name"):
            blob_host(bad, "AzureCloud")


def test_the_blob_host_of_each_cloud() -> None:
    assert blob_host(ACCOUNT, "AzureCloud") == BLOB_HOST
    assert blob_host(ACCOUNT, "AzureUSGovernment") == f"{ACCOUNT}.blob.core.usgovcloudapi.net"
    assert blob_host(ACCOUNT, "AzureChinaCloud") == f"{ACCOUNT}.blob.core.chinacloudapi.cn"


@pytest.mark.asyncio
async def test_the_storage_token_and_the_arm_token_are_cached_apart(
    registered: AzureConnector,
) -> None:
    target = AzureTargetStub()
    page = _xml_answer(_list_xml([_blob_xml(FILE_NAME)]))
    params = LIST_PARAMS

    with respx.mock() as mock:
        login = _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        blobs = mock.get(BLOB_LIST_URL).mock(side_effect=[page, page, page])
        first = await _call("azure.storage.blob.list", params, target)
        second = await _call("azure.storage.blob.list", params, target)
        assert login.call_count == 2  # one token per scope, then both come from the cache
        await registered.invalidate_session(target)
        third = await _call("azure.storage.blob.list", params, target)

    assert first.status == second.status == third.status == "ok"
    assert login.call_count == 4  # a 401 hook drops both tokens of the target
    assert {_bearer(call.request) for call in blobs.calls} == {STORAGE_TOKEN}


# ----------------------------------------------------------------------
# Blob read (blob data plane)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blob_read_returns_a_small_text_file_and_no_header_secret(
    registered: AzureConnector, store: MemoryResultStore
) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        head = mock.head(FILE_URL).respond(200, headers=_head_headers(len(TEXT)))
        body = mock.get(FILE_URL).mock(return_value=_range_answer(TEXT))
        result = await _call("azure.storage.blob.read", READ_PARAMS)

    assert result.status == "ok", result.error
    assert result.handle is None
    assert _bearer(head.calls[0].request) == STORAGE_TOKEN
    assert body.calls[0].request.headers["x-ms-range"] == f"bytes=0-{MAX_TEXT_BYTES - 1}"
    assert result.result == {
        "account": {"name": ACCOUNT, "resource_group": GROUP},
        "container": CONTAINER,
        "name": FILE_NAME,
        "size": len(TEXT),
        "content_type": "text/plain",
        "content_md5": hashlib.md5(TEXT, usedforsecurity=False).hexdigest(),
        "last_modified": "2026-10-02T09:30:00Z",
        "encoding": "utf-8",
        "content": TEXT.decode(),
    }
    assert "sig=" not in json.dumps(result.result)


@pytest.mark.asyncio
async def test_a_long_file_comes_back_as_a_stored_result_read_by_path(
    registered: AzureConnector, store: MemoryResultStore
) -> None:
    data = ("0f1e2d3c4b5a69788796a5b4c3d2e1f0  part.bin\n" * 300).encode()

    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        mock.head(FILE_URL).respond(200, headers=_head_headers(len(data), data))
        mock.get(FILE_URL).mock(return_value=_range_answer(data))
        result = await _call("azure.storage.blob.read", READ_PARAMS)

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary["name"] == FILE_NAME
    assert summary["size"] == len(data)
    assert summary["content_md5"] == hashlib.md5(data, usedforsecurity=False).hexdigest()
    assert "content" not in summary
    query = ResultQuerySpec.model_validate({"path": "/content"})
    answer = await result_query_module.run_result_query(operator(), result.handle.handle_id, query)
    assert answer["total_chars"] == len(data)
    assert data.decode().startswith(answer["value"])


@pytest.mark.asyncio
async def test_blob_read_refuses_a_file_over_1_mib_and_names_its_size(
    registered: AzureConnector,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        mock.head(FILE_URL).respond(200, headers=_head_headers(2 * 1024 * 1024))
        body = mock.get(FILE_URL).mock(return_value=_range_answer(TEXT))
        result = await _call("azure.storage.blob.read", READ_PARAMS)

    assert result.status == "error"
    assert result.extras is not None
    assert "2,097,152 bytes" in result.extras["exception_message"]
    assert "1,048,576 bytes" in result.extras["exception_message"]
    assert body.call_count == 0


@pytest.mark.asyncio
async def test_blob_read_accepts_a_file_of_exactly_1_mib(registered: AzureConnector) -> None:
    data = b"a" * MAX_TEXT_BYTES

    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        mock.head(FILE_URL).respond(200, headers=_head_headers(len(data), data))
        mock.get(FILE_URL).mock(return_value=_range_answer(data))
        result = await _call("azure.storage.blob.read", READ_PARAMS)

    assert result.status == "ok", result.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        b"PK\x03\x04\x14\x00\x00\x00\x08\x00",  # the start of a zip archive
        b"\x1f\x8b\x08\x00\x00\x00\x00\x00",  # the start of a gzip file
        b"text with a \x00 in it",
    ],
)
async def test_blob_read_never_returns_binary_bytes_as_text(
    registered: AzureConnector, data: bytes
) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        mock.head(FILE_URL).respond(
            200, headers=_head_headers(len(data), data, content_type="application/octet-stream")
        )
        mock.get(FILE_URL).mock(return_value=_range_answer(data))
        result = await _call("azure.storage.blob.read", READ_PARAMS)

    assert result.status == "error"
    assert result.extras is not None
    assert result.extras["exception_class"] == "StorageReadError"
    assert "not a text file" in result.extras["exception_message"]


@pytest.mark.asyncio
async def test_blob_read_refuses_an_image_before_reading_its_bytes(
    registered: AzureConnector,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        mock.head(FILE_URL).respond(200, headers=_head_headers(300, content_type="image/png"))
        body = mock.get(FILE_URL).mock(return_value=_range_answer(TEXT))
        result = await _call("azure.storage.blob.read", READ_PARAMS)

    assert result.status == "error"
    assert result.extras is not None
    assert "(image/...) is never text" in result.extras["exception_message"]
    assert body.call_count == 0


@pytest.mark.asyncio
async def test_blob_read_refuses_a_compressed_file_before_reading_its_bytes(
    registered: AzureConnector,
) -> None:
    headers = {**_head_headers(len(TEXT)), "Content-Encoding": "gzip"}

    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        mock.head(FILE_URL).respond(200, headers=headers)
        body = mock.get(FILE_URL).mock(return_value=_range_answer(TEXT))
        result = await _call("azure.storage.blob.read", READ_PARAMS)

    assert result.status == "error"
    assert result.extras is not None
    assert "content encoding" in result.extras["exception_message"]
    assert body.call_count == 0


@pytest.mark.asyncio
async def test_blob_read_refuses_an_archived_file_before_reading_its_bytes(
    registered: AzureConnector,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        mock.head(FILE_URL).respond(200, headers=_head_headers(len(TEXT), tier="Archive"))
        body = mock.get(FILE_URL).mock(return_value=_range_answer(TEXT))
        result = await _call("azure.storage.blob.read", READ_PARAMS)

    assert result.status == "error"
    assert result.extras is not None
    assert "Archive tier" in result.extras["exception_message"]
    assert body.call_count == 0


@pytest.mark.asyncio
async def test_blob_read_of_an_empty_file_sends_no_range_read(registered: AzureConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        mock.head(FILE_URL).respond(200, headers=_head_headers(0, b""))
        body = mock.get(FILE_URL).mock(return_value=_range_answer(TEXT))
        result = await _call("azure.storage.blob.read", READ_PARAMS)

    assert result.status == "ok", result.error
    assert result.result["content"] == ""
    assert result.result["size"] == 0
    assert body.call_count == 0


@pytest.mark.asyncio
async def test_blob_read_refuses_a_file_that_changed_between_its_two_reads(
    registered: AzureConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        mock.head(FILE_URL).respond(200, headers=_head_headers(len(TEXT)))
        mock.get(FILE_URL).mock(return_value=_range_answer(TEXT, etag='"0x8DE00000000DDDD"'))
        result = await _call("azure.storage.blob.read", READ_PARAMS)

    assert result.status == "error"
    assert result.extras is not None
    assert "changed while MEHO read it" in result.extras["exception_message"]


@pytest.mark.asyncio
async def test_blob_read_403_names_the_data_role_the_login_needs(
    registered: AzureConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        mock.head(FILE_URL).mock(
            return_value=httpx.Response(
                403, headers={"x-ms-error-code": "AuthorizationPermissionMismatch"}
            )
        )
        result = await _call("azure.storage.blob.read", READ_PARAMS)

    assert result.status == "error"
    assert result.extras is not None
    assert result.extras["http_status"] == 403
    assert "Storage Blob Data Reader" in result.extras["remediation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["../other/secret.txt", "a/./b.txt", "a/..", ".", ""])
async def test_blob_read_refuses_a_name_that_would_change_the_path(name: str) -> None:
    connector = AzureConnector(credentials_loader=CountingLoader())
    params = {"account": ACCOUNT, "container": CONTAINER, "name": name}
    with respx.mock() as mock, pytest.raises(StorageParameterError, match="'name'"):
        await storage_reads.blob_read_impl(connector, operator(), AzureTargetStub(), params)
    assert not mock.calls


@pytest.mark.asyncio
async def test_blob_read_sends_special_characters_in_the_name_encoded(
    registered: AzureConnector,
) -> None:
    name = "reports/Q3 #1?.txt"
    encoded_url = f"https://{BLOB_HOST}/{CONTAINER}/reports/Q3%20%231%3F.txt"

    with respx.mock() as mock:
        _login(mock)
        mock.get(ACCOUNTS_URL).respond(200, json={"value": [_account_item()]})
        head = mock.head(encoded_url).respond(200, headers=_head_headers(len(TEXT)))
        mock.get(encoded_url).mock(return_value=_range_answer(TEXT))
        result = await _call(
            "azure.storage.blob.read", {"account": ACCOUNT, "container": CONTAINER, "name": name}
        )

    assert result.status == "ok", result.error
    assert head.calls[0].request.url.raw_path == b"/backups/reports/Q3%20%231%3F.txt"
    assert result.result["name"] == name


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------


def test_md5_hex_turns_azure_base64_into_hex() -> None:
    assert md5_hex(_md5_b64(TEXT)) == hashlib.md5(TEXT, usedforsecurity=False).hexdigest()
    assert md5_hex("not base64!") is None
    assert md5_hex(base64.b64encode(b"too short").decode()) is None
    assert md5_hex("") is None and md5_hex(None) is None


def test_http_dates_become_iso_utc() -> None:
    assert http_date_to_iso("Fri, 02 Oct 2026 09:30:00 GMT") == "2026-10-02T09:30:00Z"
    assert http_date_to_iso("not a date") is None
    assert http_date_to_iso(None) is None


def test_text_content_accepts_utf8_text_and_refuses_binary() -> None:
    assert text_content("Grüße\r\n\tok \x1b[0m".encode()) == "Grüße\r\n\tok \x1b[0m"
    assert text_content(b"\xef\xbb\xbfwith a byte order mark") == "with a byte order mark"
    assert text_content(b"") == ""
    assert text_content(b"\xff\xfe\x00t\x00x\x00t") is None
    assert text_content(b"bell \x07") is None


def test_a_list_answer_that_is_not_the_documented_xml_is_refused() -> None:
    with pytest.raises(ValueError, match="not the XML list"):
        blob_page(b"{not xml", "azure-sub-a")
    with pytest.raises(ValueError, match="not the XML list"):
        blob_page(b"<Error><Code>Odd</Code></Error>", "azure-sub-a")
    entity_trick = b'<!DOCTYPE x [<!ENTITY a "b">]><EnumerationResults>&a;</EnumerationResults>'
    with pytest.raises(ValueError, match="not the XML list"):
        blob_page(entity_trick, "azure-sub-a")


def test_names_follow_azure_rules() -> None:
    assert container_name("$web", "t") == "$web"
    assert container_name("logs-2026", "t") == "logs-2026"
    for bad in ("Logs", "lo", "logs--2026", "-logs", "logs-", "a" * 64, "$other"):
        with pytest.raises(StorageParameterError):
            container_name(bad, "t")
    assert resource_group_name("rg_(example).2026", "t") == "rg_(example).2026"
    for bad in ("", "rg.", "rg/x", "rg x", "a" * 91):
        with pytest.raises(StorageParameterError):
            resource_group_name(bad, "t")
    assert blob_name("a/b/c.txt", "t") == "a/b/c.txt"
    with pytest.raises(StorageParameterError):
        blob_name("x" * 1025, "t")
