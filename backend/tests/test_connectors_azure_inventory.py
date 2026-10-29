# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure inventory family: lists, tags, Resource Graph over a
checked subscription set, and the subscription candidates of
``meho targets discover``.

The dispatch tests run the real dispatcher (permission check, audit row,
result reduction) with only the Azure registrar run. Microsoft's login service
and Azure Resource Manager are mocked with respx. Every id, name and secret
here is made up.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.azure import (
    AZURE_CONNECTOR_ID,
    AZURE_IMPL_ID,
    AZURE_PRODUCT,
    AZURE_VERSION,
    AzureConnector,
    _graph_set,
    _http,
    _paging,
    register_azure_typed_operations,
)
from meho_backplane.connectors.azure._errors import AzureUrlRefusedError
from meho_backplane.connectors.azure._graph_set import read_over_set
from meho_backplane.connectors.azure._paging import PageLimits
from meho_backplane.connectors.azure.inventory_graph import (
    AKS_QUERY,
    GRAPH_QUERY_OP_ID,
    NETWORK_QUERY,
    SUMMARY_QUERY,
    VM_QUERY,
)
from meho_backplane.connectors.azure.inventory_reads import (
    checked_resource_id,
    list_visible_subscriptions,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.result_handle_store import SpillResult
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    AuditLog,
    PermissionVerdict,
    ServicePrincipalReadScope,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings
from tests._azure_fixtures import (
    ARM_BASE,
    ARM_HOST,
    MEHO_TENANT,
    SUBSCRIPTION_A,
    SUBSCRIPTION_B,
    TENANT_A,
    AzureTargetStub,
    CountingLoader,
    login_token,
    operator,
    token_answer,
    token_url,
)

SUBSCRIPTION_C = "11111111-0000-0000-0000-00000000e003"
SUBSCRIPTION_D = "11111111-0000-0000-0000-00000000e004"
LOGIN_A = "tenants/example/azure-login-a"
LOGIN_B = "tenants/example/azure-login-b"
GRAPH_URL = f"{ARM_BASE}/providers/Microsoft.ResourceGraph/resources"
SUBSCRIPTIONS_URL = f"{ARM_BASE}/subscriptions"
RESOURCE_ID = (
    f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg-example/providers/"
    "Microsoft.Storage/storageAccounts/stexample"
)
TAGS_URL = f"{ARM_BASE}{RESOURCE_ID}/providers/Microsoft.Resources/tags/default"


# ----------------------------------------------------------------------
# Fixtures and helpers
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


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Clock]:
    fake = _Clock()
    for module in (_http, _paging, _graph_set):
        monkeypatch.setattr(module, "_monotonic", fake.monotonic)
    monkeypatch.setattr(_http, "_sleep", fake.sleep)
    yield fake


@pytest.fixture
async def azure(monkeypatch: pytest.MonkeyPatch) -> AzureConnector:
    """Register the Azure operations; return the dispatcher's connector instance."""
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


async def _add_target(
    name: str,
    subscription_id: str | None,
    *,
    secret_ref: str = LOGIN_A,
    product: str = "azure",
    extras: dict[str, Any] | None = None,
    deleted: bool = False,
) -> TargetORM:
    now = datetime.now(UTC)
    row = TargetORM(
        id=uuid.uuid4(),
        tenant_id=MEHO_TENANT,
        name=name,
        aliases=[],
        product=product,
        host=ARM_HOST,
        port=443,
        secret_ref=secret_ref,
        auth_model="shared_service_account",
        vpn_required=False,
        verify_tls=True,
        extras=extras if extras is not None else {"subscription_id": subscription_id},
        created_at=now,
        updated_at=now,
        deleted_at=now if deleted else None,
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()
    return row


def _caller(kind: PrincipalKind, sub: str) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="caller.jwt.value",
        tenant_id=MEHO_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _call(
    op_id: str,
    params: dict[str, Any],
    *,
    caller: Operator | None = None,
    approved: bool = False,
) -> OperationResult:
    return await dispatch(
        operator=caller or operator(),
        connector_id=AZURE_CONNECTOR_ID,
        op_id=op_id,
        target=AzureTargetStub(),
        params=params,
        _approved=approved,
    )


def _vm_rows(subscription: str, count: int, start: int = 0) -> list[dict[str, Any]]:
    return [
        {
            "id": (
                f"/subscriptions/{subscription}/resourceGroups/rg-example/providers/"
                f"Microsoft.Compute/virtualMachines/vm-{n}"
            ),
            "name": f"vm-{n}",
            "subscriptionId": subscription,
        }
        for n in range(start, start + count)
    ]


def _graph(rows: list[Any], token: str | None = None, **extra: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "data": rows,
        "count": len(rows),
        "totalRecords": len(rows),
        "resultTruncated": "false",
        **extra,
    }
    if token is not None:
        answer["$skipToken"] = token
    return answer


def _bodies(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(call.request.content) for call in route.calls]


def _login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(token_url()).respond(200, json=token_answer(login_token()))


class _RecordingStore:
    """Stand-in for the result handle store: keeps what the reducer stores."""

    def __init__(self) -> None:
        self.documents: list[dict[str, Any] | None] = []

    async def spill(self, *, rows: list[dict[str, Any]], **kwargs: Any) -> SpillResult:
        self.documents.append(kwargs.get("document"))
        return SpillResult(
            stored_rows=len(rows), document_stored=kwargs.get("document") is not None
        )


# ----------------------------------------------------------------------
# Lists of the target's own subscription
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resource_groups_follow_the_next_link(azure: AzureConnector) -> None:
    path = f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/resourcegroups"
    second = f"{path}?api-version=2021-04-01&%24skiptoken=page2"
    with respx.mock() as mock:
        _login(mock)
        groups = mock.get(url__startswith=path).mock(
            side_effect=[
                httpx.Response(200, json={"value": [{"name": "rg-1"}], "nextLink": second}),
                httpx.Response(200, json={"value": [{"name": "rg-2"}]}),
            ]
        )
        result = await _call("azure.resource_group.list", {})

    assert result.status == "ok", result.error
    assert result.result is not None
    assert [row["name"] for row in result.result["value"]] == ["rg-1", "rg-2"]
    assert result.result["pages_read"] == 2 and result.result["truncated"] is False
    assert groups.calls[0].request.url.params["api-version"] == "2021-04-01"


@pytest.mark.asyncio
async def test_resource_list_of_one_type_in_one_group(azure: AzureConnector) -> None:
    group_path = f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg-example/resources"
    with respx.mock() as mock:
        _login(mock)
        listed = mock.get(group_path).respond(200, json={"value": [{"name": "vm-1"}]})
        result = await _call(
            "azure.resource.list",
            {"type": "Microsoft.Compute/virtualMachines", "resource_group": "rg-example"},
        )

    assert result.status == "ok", result.error
    params = listed.calls[0].request.url.params
    assert params["$filter"] == "resourceType eq 'Microsoft.Compute/virtualMachines'"
    assert params["api-version"] == "2021-04-01"


@pytest.mark.parametrize(
    "params",
    [
        {"type": "Microsoft.Compute/virtualMachines' or name eq 'x"},
        {"type": "virtualMachines"},
        {"resource_group": "rg/../other"},
        {"resource_group": ".."},
        {"resource_group": "."},
        {"resource_group": "rg-example."},
    ],
)
@pytest.mark.asyncio
async def test_a_filter_or_group_that_could_change_the_request_is_refused(
    azure: AzureConnector, params: dict[str, Any]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        result = await _call("azure.resource.list", params)

    assert result.status == "error"
    assert result.extras["error_code"] == "invalid_params"
    assert not login.called


@pytest.mark.parametrize(
    ("op_id", "path", "api_version"),
    [
        ("azure.tag.list", f"/subscriptions/{SUBSCRIPTION_A}/tagNames", "2021-04-01"),
        (
            "azure.lock.list",
            f"/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.Authorization/locks",
            "2020-05-01",
        ),
        ("azure.resource.list", f"/subscriptions/{SUBSCRIPTION_A}/resources", "2021-04-01"),
    ],
)
@pytest.mark.asyncio
async def test_each_list_reads_its_path_with_its_api_version(
    azure: AzureConnector, op_id: str, path: str, api_version: str
) -> None:
    with respx.mock() as mock:
        _login(mock)
        listed = mock.get(f"{ARM_BASE}{path}").respond(200, json={"value": [{"name": "x"}]})
        result = await _call(op_id, {})

    assert result.status == "ok", result.error
    assert result.result is not None and result.result["value"] == [{"name": "x"}]
    assert listed.calls[0].request.url.params["api-version"] == api_version


@pytest.mark.parametrize(
    ("op_id", "path"),
    [
        ("azure.subscription.list", "/subscriptions"),
        ("azure.resource_group.list", f"/subscriptions/{SUBSCRIPTION_A}/resourcegroups"),
        ("azure.resource.list", f"/subscriptions/{SUBSCRIPTION_A}/resources"),
        ("azure.tag.list", f"/subscriptions/{SUBSCRIPTION_A}/tagNames"),
        (
            "azure.lock.list",
            f"/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.Authorization/locks",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_long_list_goes_behind_a_link_and_keeps_its_paging_values(
    azure: AzureConnector, op_id: str, path: str
) -> None:
    """The real dispatcher and reducer: 60 rows become a handle; the scalars stay."""
    store = _RecordingStore()
    set_default_reducer(JsonFluxReducer(sample_size=5, store=store))  # type: ignore[arg-type]
    rows = [{"name": f"item-{n}"} for n in range(60)]
    with respx.mock() as mock:
        _login(mock)
        mock.get(f"{ARM_BASE}{path}").respond(200, json={"value": rows})
        result = await _call(op_id, {})

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result is not None
    assert result.result["row_count"] == 60
    assert result.result["truncated"] is False
    assert result.result["pages_read"] == 1


# ----------------------------------------------------------------------
# The subscription list and its own next link
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_subscription_list_follows_its_own_next_link(azure: AzureConnector) -> None:
    second = f"{SUBSCRIPTIONS_URL}?api-version=2022-12-01&%24skiptoken=page2"
    with respx.mock() as mock:
        _login(mock)
        listed = mock.get(SUBSCRIPTIONS_URL).mock(
            side_effect=[
                httpx.Response(
                    200, json={"value": [{"subscriptionId": SUBSCRIPTION_A}], "nextLink": second}
                ),
                httpx.Response(200, json={"value": [{"subscriptionId": SUBSCRIPTION_B}]}),
            ]
        )
        result = await _call("azure.subscription.list", {})

    assert result.status == "ok", result.error
    assert result.result is not None
    assert [row["subscriptionId"] for row in result.result["value"]] == [
        SUBSCRIPTION_A,
        SUBSCRIPTION_B,
    ]
    first, follow = (call.request.url for call in listed.calls)
    assert first.params["api-version"] == "2022-12-01"
    assert follow.path == "/subscriptions"
    assert follow.params["$skiptoken"] == "page2"
    assert follow.params["api-version"] == "2022-12-01"


@pytest.mark.parametrize(
    "bad_link",
    [
        "https://attacker.example.net/subscriptions?%24skiptoken=x",
        f"https://{ARM_HOST}/subscriptions/{SUBSCRIPTION_B}/resources?%24skiptoken=x",
        f"http://{ARM_HOST}/subscriptions?%24skiptoken=x",
        f"https://someone:pw@{ARM_HOST}/subscriptions?%24skiptoken=x",
        f"https://{ARM_HOST}:8443/subscriptions?%24skiptoken=x",
        "/subscriptions?%24skiptoken=x",
    ],
)
@pytest.mark.asyncio
async def test_a_subscription_list_link_elsewhere_is_refused(bad_link: str) -> None:
    connector = AzureConnector(credentials_loader=CountingLoader())
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        listed = mock.get(SUBSCRIPTIONS_URL).respond(
            200, json={"value": [{"subscriptionId": SUBSCRIPTION_A}], "nextLink": bad_link}
        )
        elsewhere = mock.route(host="attacker.example.net").respond(200)
        with pytest.raises(AzureUrlRefusedError):
            await list_visible_subscriptions(connector, operator(), AzureTargetStub())

    assert listed.call_count == 1
    assert not elsewhere.called


# ----------------------------------------------------------------------
# Tags of one resource
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tag_change_waits_for_approval(azure: AzureConnector) -> None:
    params = {"resource_id": RESOURCE_ID, "mode": "merge", "tags": {"owner": "team-a"}}
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        result = await _call("azure.tag.update", params)

    assert result.status == "awaiting_approval", result
    assert not login.called


@pytest.mark.asyncio
async def test_merge_sends_one_patch_with_the_tags(azure: AzureConnector) -> None:
    after = {"properties": {"tags": {"owner": "team-a", "env": "test"}}}
    params = {"resource_id": RESOURCE_ID, "mode": "merge", "tags": {"owner": "team-a"}}
    with respx.mock() as mock:
        _login(mock)
        patch = mock.patch(TAGS_URL).respond(200, json=after)
        result = await _call("azure.tag.update", params, approved=True)

    assert result.status == "ok", result.error
    request = patch.calls[0].request
    assert request.url.params["api-version"] == "2021-04-01"
    assert json.loads(request.content) == {
        "operation": "Merge",
        "properties": {"tags": {"owner": "team-a"}},
    }
    assert result.result == {
        "resource_id": RESOURCE_ID,
        "mode": "merge",
        "tags": {"owner": "team-a", "env": "test"},
        "not_present": [],
    }


@pytest.mark.asyncio
async def test_remove_deletes_the_named_tags_with_their_current_values(
    azure: AzureConnector,
) -> None:
    current = {"properties": {"tags": {"owner": "team-a", "Temp": "yes"}}}
    params = {"resource_id": RESOURCE_ID, "mode": "remove", "tags": {"temp": "", "gone": ""}}
    with respx.mock() as mock:
        _login(mock)
        mock.get(TAGS_URL).respond(200, json=current)
        patch = mock.patch(TAGS_URL).respond(
            200, json={"properties": {"tags": {"owner": "team-a"}}}
        )
        result = await _call("azure.tag.update", params, approved=True)

    assert result.status == "ok", result.error
    assert json.loads(patch.calls[0].request.content) == {
        "operation": "Delete",
        "properties": {"tags": {"Temp": "yes"}},
    }
    assert result.result is not None
    assert result.result["tags"] == {"owner": "team-a"}
    assert result.result["not_present"] == ["gone"]


@pytest.mark.asyncio
async def test_removing_again_sends_no_second_change(azure: AzureConnector) -> None:
    params = {"resource_id": RESOURCE_ID, "mode": "remove", "tags": {"temp": ""}}
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(TAGS_URL).respond(200, json={"properties": {"tags": {"owner": "team-a"}}})
        patch = mock.patch(TAGS_URL).respond(200, json={})
        result = await _call("azure.tag.update", params, approved=True)

    assert result.status == "ok", result.error
    assert not patch.called
    assert result.result is not None
    assert result.result["tags"] == {"owner": "team-a"}
    assert result.result["not_present"] == ["temp"]


@pytest.mark.parametrize(
    "resource_id",
    [
        RESOURCE_ID.replace(SUBSCRIPTION_A, SUBSCRIPTION_B),
        f"/subscriptions/{SUBSCRIPTION_A}",
        f"/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.Compute/virtualMachines/vm-1",
        f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg-example/../../{SUBSCRIPTION_B}",
        f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg-example/providers/x?api-version=1",
        f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg-example/%2e%2e/x/y/z",
        f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups//providers/A.B/type/name",
        f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg-example/providers/A.B",
        f"{RESOURCE_ID}/providers/Microsoft.Resources/tags/default",
        "subscriptions/" + SUBSCRIPTION_A + "/resourceGroups/rg-example",
    ],
)
def test_a_resource_id_outside_the_target_is_refused(resource_id: str) -> None:
    with pytest.raises(ValueError, match="own subscription"):
        checked_resource_id(AzureTargetStub(), resource_id)


@pytest.mark.parametrize(
    "resource_id",
    [
        RESOURCE_ID,
        RESOURCE_ID.upper().replace("/SUBSCRIPTIONS/", "/subscriptions/"),
        f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/rg-example",
        f"{RESOURCE_ID}/blobServices/default",
    ],
)
def test_a_resource_of_the_target_is_accepted(resource_id: str) -> None:
    assert checked_resource_id(AzureTargetStub(), resource_id) == resource_id


@pytest.mark.asyncio
async def test_a_resource_of_another_subscription_sends_nothing(azure: AzureConnector) -> None:
    other = RESOURCE_ID.replace(SUBSCRIPTION_A, SUBSCRIPTION_B)
    params = {"resource_id": other, "mode": "merge", "tags": {"owner": "team-a"}}
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        anything = mock.route(host=ARM_HOST).respond(200, json={})
        result = await _call("azure.tag.update", params, approved=True)

    assert result.status == "error"
    assert "own subscription" in str(result.extras["exception_message"])
    assert not login.called
    assert not anything.called


# ----------------------------------------------------------------------
# The subscription set
# ----------------------------------------------------------------------


def _reasons(result: OperationResult) -> dict[str, str]:
    assert result.result is not None
    return {entry["name"]: entry["reason"] for entry in result.result["subscriptions"]["denied"]}


@pytest.mark.asyncio
async def test_the_set_checks_every_name(azure: AzureConnector) -> None:
    await _add_target("azure-sub-b", SUBSCRIPTION_B)
    await _add_target("azure-other-login", SUBSCRIPTION_C, secret_ref=LOGIN_B)
    await _add_target("vcenter-1", None, product="vmware", extras={})
    await _add_target("azure-broken", None, extras={"subscription_id": "not-a-guid"})
    await _add_target("azure-retired", SUBSCRIPTION_D, deleted=True)
    names = [
        "azure-sub-b",
        "azure-other-login",
        "vcenter-1",
        "azure-broken",
        "azure-retired",
        "ghost",
        "azure-sub-a",
        "azure-sub-b",
    ]
    with respx.mock() as mock:
        _login(mock)
        graph = mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph([]))
        result = await _call("azure.graph.query", {"query": "Resources", "subscriptions": names})

    assert result.status == "ok", result.error
    assert _bodies(graph)[0]["subscriptions"] == [SUBSCRIPTION_A, SUBSCRIPTION_B]
    assert result.result is not None
    block = result.result["subscriptions"]
    assert block["requested"] == [
        "azure-sub-a",
        "azure-sub-b",
        "azure-other-login",
        "vcenter-1",
        "azure-broken",
        "azure-retired",
        "ghost",
    ]
    assert block["completed"] == ["azure-sub-a", "azure-sub-b"]
    reasons = _reasons(result)
    assert set(reasons) == {
        "azure-other-login",
        "vcenter-1",
        "azure-broken",
        "azure-retired",
        "ghost",
    }
    assert "another login" in reasons["azure-other-login"]
    assert "not an azure target" in reasons["vcenter-1"]
    assert "not a usable Azure target" in reasons["azure-broken"]
    assert "No live target" in reasons["azure-retired"]
    assert "No live target" in reasons["ghost"]
    assert result.result["complete"] is False
    assert result.result["omissions"]["items"][0]["kind"] == "subscriptions_denied"
    assert result.result["omissions"]["items"][0]["count"] == 5


@pytest.mark.asyncio
async def test_without_names_only_the_calling_target_is_read(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        graph = mock.post(url__startswith=GRAPH_URL).respond(
            200, json=_graph(_vm_rows(SUBSCRIPTION_A, 2))
        )
        result = await _call("azure.graph.query", {"query": "Resources"})

    assert result.status == "ok", result.error
    assert result.result is not None
    assert _bodies(graph)[0]["subscriptions"] == [SUBSCRIPTION_A]
    assert result.result["subscriptions"]["requested"] == ["azure-sub-a"]
    assert result.result["complete"] is True
    assert result.result["omissions"] == {"count": 0, "items": []}


@pytest.mark.asyncio
async def test_an_agent_cannot_read_a_subscription_it_may_not_use(azure: AzureConnector) -> None:
    """A deny grant on target B keeps B out of the query; a needs-approval grant too."""
    agent = _caller(PrincipalKind.AGENT, "agent-azure-reader")
    target_b = await _add_target("azure-sub-b", SUBSCRIPTION_B)
    target_c = await _add_target("azure-sub-c", SUBSCRIPTION_C)
    async with get_sessionmaker()() as session:
        for target, verdict in (
            (target_b, PermissionVerdict.DENY),
            (target_c, PermissionVerdict.NEEDS_APPROVAL),
        ):
            session.add(
                AgentPermission(
                    tenant_id=MEHO_TENANT,
                    principal_sub=agent.sub,
                    op_pattern="azure.*",
                    target_scope=str(target.id),
                    verdict=verdict.value,
                    created_by_sub="ops-admin",
                )
            )
        await session.commit()

    with respx.mock() as mock:
        _login(mock)
        graph = mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph([]))
        result = await _call(
            "azure.inventory.summary",
            {"subscriptions": ["azure-sub-b", "azure-sub-c"]},
            caller=agent,
        )

    assert result.status == "ok", result.error
    assert _bodies(graph)[0]["subscriptions"] == [SUBSCRIPTION_A]
    reasons = _reasons(result)
    assert "may not run azure.inventory.summary" in reasons["azure-sub-b"]
    assert "wait for approval" in reasons["azure-sub-c"]


@pytest.mark.asyncio
async def test_a_service_principal_read_scope_limits_the_set(azure: AzureConnector) -> None:
    service = _caller(PrincipalKind.SERVICE, "svc-azure-reader")
    await _add_target("azure-sub-b", SUBSCRIPTION_B)
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalReadScope(
                tenant_id=MEHO_TENANT,
                principal_sub=service.sub,
                entries=[{"target_name_pattern": "azure-sub-a"}],
                set_by_sub="ops-admin",
                set_at=datetime.now(UTC),
            )
        )
        await session.commit()

    with respx.mock() as mock:
        _login(mock)
        graph = mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph([]))
        result = await _call(
            "azure.graph.query",
            {"query": "Resources", "subscriptions": ["azure-sub-b"]},
            caller=service,
        )

    assert result.status == "ok", result.error
    assert _bodies(graph)[0]["subscriptions"] == [SUBSCRIPTION_A]
    assert "read_scope_denied" in _reasons(result)["azure-sub-b"]


@pytest.mark.asyncio
async def test_a_query_naming_another_subscription_stays_inside_the_set(
    azure: AzureConnector,
) -> None:
    """The query text cannot widen the set; a row of another subscription is dropped."""
    await _add_target("azure-sub-b", SUBSCRIPTION_B)
    query = f"Resources | where subscriptionId == '{SUBSCRIPTION_B}'"
    rows = [*_vm_rows(SUBSCRIPTION_A, 1), *_vm_rows(SUBSCRIPTION_B, 1, start=5)]
    with respx.mock() as mock:
        _login(mock)
        graph = mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph(rows))
        result = await _call("azure.graph.query", {"query": query})

    assert result.status == "ok", result.error
    body = _bodies(graph)[0]
    assert body["subscriptions"] == [SUBSCRIPTION_A]
    assert "managementGroups" not in body
    assert result.result is not None
    assert [row["subscriptionId"] for row in result.result["value"]] == [SUBSCRIPTION_A]
    kinds = {item["kind"]: item for item in result.result["omissions"]["items"]}
    assert kinds["outside_set"]["count"] == 1


@pytest.mark.asyncio
async def test_two_names_of_one_subscription_query_it_once(azure: AzureConnector) -> None:
    await _add_target("azure-sub-a-again", SUBSCRIPTION_A)
    with respx.mock() as mock:
        _login(mock)
        graph = mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph([]))
        result = await _call(
            "azure.graph.query", {"query": "Resources", "subscriptions": ["azure-sub-a-again"]}
        )

    assert result.status == "ok", result.error
    assert _bodies(graph)[0]["subscriptions"] == [SUBSCRIPTION_A]
    assert result.result is not None
    assert result.result["subscriptions"]["completed"] == ["azure-sub-a", "azure-sub-a-again"]


# ----------------------------------------------------------------------
# Resource Graph paging, batches and omissions
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_query_follows_the_skip_token(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        graph = mock.post(url__startswith=GRAPH_URL).mock(
            side_effect=[
                httpx.Response(200, json=_graph(_vm_rows(SUBSCRIPTION_A, 2), token="next-1")),
                httpx.Response(200, json=_graph(_vm_rows(SUBSCRIPTION_A, 1, start=2))),
            ]
        )
        result = await _call("azure.graph.query", {"query": "Resources", "page_size": 2})

    assert result.status == "ok", result.error
    first, second = _bodies(graph)
    assert first["options"] == {"resultFormat": "objectArray", "$top": 2}
    assert second["options"] == {"resultFormat": "objectArray", "$top": 2, "$skipToken": "next-1"}
    assert second["query"] == "Resources"
    assert second["subscriptions"] == [SUBSCRIPTION_A]
    assert graph.calls[0].request.url.params["api-version"] == "2024-04-01"
    assert result.result is not None
    assert [row["name"] for row in result.result["value"]] == ["vm-0", "vm-1", "vm-2"]
    assert result.result["pages_read"] == 2
    assert result.result["complete"] is True


def test_a_batch_holds_fewer_than_300_subscriptions() -> None:
    assert _graph_set.GRAPH_BATCH_SIZE < 300


@pytest.mark.asyncio
async def test_many_subscriptions_go_in_batches(
    azure: AzureConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_graph_set, "GRAPH_BATCH_SIZE", 2)
    await _add_target("azure-sub-b", SUBSCRIPTION_B)
    await _add_target("azure-sub-c", SUBSCRIPTION_C)
    with respx.mock() as mock:
        _login(mock)
        graph = mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph([]))
        result = await _call(
            "azure.graph.query",
            {"query": "Resources", "subscriptions": ["azure-sub-b", "azure-sub-c"]},
        )

    assert result.status == "ok", result.error
    assert [body["subscriptions"] for body in _bodies(graph)] == [
        [SUBSCRIPTION_A, SUBSCRIPTION_B],
        [SUBSCRIPTION_C],
    ]


@pytest.mark.asyncio
async def test_a_failed_page_is_named_and_the_other_batches_still_count(
    azure: AzureConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_graph_set, "GRAPH_BATCH_SIZE", 1)
    await _add_target("azure-sub-b", SUBSCRIPTION_B)
    refused = {"error": {"code": "AuthorizationFailed", "message": "no access"}}
    with respx.mock() as mock:
        _login(mock)
        mock.post(url__startswith=GRAPH_URL).mock(
            side_effect=[
                httpx.Response(200, json=_graph(_vm_rows(SUBSCRIPTION_A, 2), token="next-1")),
                httpx.Response(403, json=refused),
                httpx.Response(200, json=_graph(_vm_rows(SUBSCRIPTION_B, 1))),
            ]
        )
        result = await _call(
            "azure.graph.query", {"query": "Resources", "subscriptions": ["azure-sub-b"]}
        )

    assert result.status == "ok", result.error
    assert result.result is not None
    assert len(result.result["value"]) == 3  # the rows read before the failed page stay
    block = result.result["subscriptions"]
    assert block["completed"] == ["azure-sub-b"]
    assert [entry["name"] for entry in block["failed"]] == ["azure-sub-a"]
    assert "Page 2" in block["failed"][0]["reason"]
    assert "AuthorizationFailed" in block["failed"][0]["reason"]
    failed = [item for item in result.result["omissions"]["items"] if item["kind"] == "page_failed"]
    assert failed[0]["page"] == 2 and failed[0]["subscription_count"] == 1


@pytest.mark.asyncio
async def test_when_nothing_could_be_read_the_caller_gets_azures_error(
    azure: AzureConnector,
) -> None:
    refused = {"error": {"code": "BadRequest", "message": "Query is invalid."}}
    with respx.mock() as mock:
        _login(mock)
        mock.post(url__startswith=GRAPH_URL).respond(400, json=refused)
        result = await _call("azure.graph.query", {"query": "Resources | bad"})

    assert result.status == "error"
    assert result.extras["upstream_message"] == "BadRequest: Query is invalid."
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(AuditLog).where(AuditLog.path == GRAPH_QUERY_OP_ID))
        ).scalar_one()
    assert row.payload["result_status"] == "error"
    failed = row.payload["subscriptions"]["failed"]  # type: ignore[index]
    assert [entry["name"] for entry in failed] == ["azure-sub-a"]


@pytest.mark.asyncio
async def test_the_cut_at_10000_subscriptions_is_named(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.post(url__startswith=GRAPH_URL).respond(
            200,
            json=_graph(_vm_rows(SUBSCRIPTION_A, 1)),
            headers={"x-ms-tenant-subscription-limit-hit": "true"},
        )
        result = await _call("azure.graph.query", {"query": "Resources"})

    assert result.status == "ok", result.error
    assert result.result is not None
    kinds = [item["kind"] for item in result.result["omissions"]["items"]]
    assert kinds == ["subscription_limit"]
    assert result.result["complete"] is False


@pytest.mark.asyncio
async def test_a_result_cut_by_resource_graph_is_named(azure: AzureConnector) -> None:
    answer = _graph(_vm_rows(SUBSCRIPTION_A, 2), totalRecords=9, resultTruncated="true")
    with respx.mock() as mock:
        _login(mock)
        mock.post(url__startswith=GRAPH_URL).respond(200, json=answer)
        result = await _call("azure.graph.query", {"query": "Resources | take 2"})

    assert result.result is not None
    item = result.result["omissions"]["items"][0]
    assert item["kind"] == "result_truncated" and item["total_records"] == 9
    assert result.result["subscriptions"]["completed"] == ["azure-sub-a"]


@pytest.mark.asyncio
async def test_the_stored_result_cap_stops_the_read_and_is_named(
    azure: AzureConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_graph_set, "GRAPH_BATCH_SIZE", 1)
    await _add_target("azure-sub-b", SUBSCRIPTION_B)
    await _add_target("azure-sub-c", SUBSCRIPTION_C)
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        graph = mock.post(url__startswith=GRAPH_URL).mock(
            side_effect=[
                httpx.Response(200, json=_graph(_vm_rows(SUBSCRIPTION_A, 2))),
                httpx.Response(200, json=_graph(_vm_rows(SUBSCRIPTION_B, 2))),
            ]
        )
        result = await read_over_set(
            azure,
            operator(),
            AzureTargetStub(),
            ["azure-sub-b", "azure-sub-c"],
            op_id=GRAPH_QUERY_OP_ID,
            query="Resources",
            limits=PageLimits(max_rows=3),
        )

    assert graph.call_count == 2
    assert len(result["value"]) == 3
    block = result["subscriptions"]
    assert block["completed"] == ["azure-sub-a"]
    assert [entry["name"] for entry in block["failed"]] == ["azure-sub-b", "azure-sub-c"]
    row_limit = [item for item in result["omissions"]["items"] if item["kind"] == "row_limit"]
    assert len(row_limit) == 1 and row_limit[0]["limit"] == 3
    assert "stored result" in row_limit[0]["message"]


@pytest.mark.parametrize(
    ("limits", "kind"),
    [(PageLimits(max_pages=1), "page_limit"), (PageLimits(max_seconds=0.0), "time_limit")],
)
@pytest.mark.asyncio
async def test_the_page_and_time_limits_are_named(
    azure: AzureConnector, limits: PageLimits, kind: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.post(url__startswith=GRAPH_URL).respond(
            200, json=_graph(_vm_rows(SUBSCRIPTION_A, 1), token="next-1")
        )
        result = await read_over_set(
            azure,
            operator(),
            AzureTargetStub(),
            [],
            op_id=GRAPH_QUERY_OP_ID,
            query="Resources",
            limits=limits,
        )

    kinds = [item["kind"] for item in result["omissions"]["items"]]
    assert kinds == [kind]
    assert [entry["name"] for entry in result["subscriptions"]["failed"]] == ["azure-sub-a"]


# ----------------------------------------------------------------------
# The blocks at the link to the stored result, and in the audit row
# ----------------------------------------------------------------------

_SET_OPS: list[tuple[str, dict[str, Any]]] = [
    ("azure.graph.query", {"query": "Resources"}),
    ("azure.inventory.summary", {}),
    ("azure.inventory.vm.list", {}),
    ("azure.inventory.aks.list", {}),
    ("azure.inventory.network.list", {}),
]


@pytest.mark.parametrize(("op_id", "params"), _SET_OPS)
@pytest.mark.asyncio
async def test_the_blocks_stay_next_to_the_link_to_the_stored_result(
    azure: AzureConnector, op_id: str, params: dict[str, Any]
) -> None:
    """The real dispatcher and reducer: 60 rows become a handle; both blocks stay."""
    store = _RecordingStore()
    set_default_reducer(JsonFluxReducer(sample_size=5, store=store))  # type: ignore[arg-type]
    with respx.mock() as mock:
        _login(mock)
        mock.post(url__startswith=GRAPH_URL).respond(
            200, json=_graph(_vm_rows(SUBSCRIPTION_A, 60), totalRecords=60)
        )
        result = await _call(op_id, {**params, "subscriptions": ["ghost"]})

    assert result.status == "ok", result.error
    assert result.handle is not None
    summary = result.result
    assert summary is not None
    assert summary["row_count"] == 60
    assert summary["complete"] is False
    assert summary["omissions"]["count"] == 1
    assert summary["omissions"]["items"][0]["kind"] == "subscriptions_denied"
    assert summary["subscriptions"]["completed"] == ["azure-sub-a"]
    assert summary["subscriptions"]["requested"] == ["azure-sub-a", "ghost"]
    assert summary["subscriptions"]["denied"][0]["name"] == "ghost"
    assert "result_object_truncations" not in summary
    document = store.documents[0]
    assert document is not None
    assert document["subscriptions"]["denied"][0]["name"] == "ghost"


@pytest.mark.asyncio
async def test_a_short_result_comes_back_whole(azure: AzureConnector) -> None:
    store = _RecordingStore()
    set_default_reducer(JsonFluxReducer(sample_size=5, store=store))  # type: ignore[arg-type]
    with respx.mock() as mock:
        _login(mock)
        mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph(_vm_rows(SUBSCRIPTION_A, 2)))
        result = await _call("azure.graph.query", {"query": "Resources"})

    assert result.handle is None
    assert result.result is not None
    assert len(result.result["value"]) == 2
    assert result.result["subscriptions"]["completed"] == ["azure-sub-a"]


@pytest.mark.asyncio
async def test_the_audit_row_lists_the_subscriptions(azure: AzureConnector) -> None:
    await _add_target("azure-sub-b", SUBSCRIPTION_B)
    with respx.mock() as mock:
        _login(mock)
        mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph(_vm_rows(SUBSCRIPTION_A, 1)))
        result = await _call(
            "azure.graph.query",
            {"query": "Resources", "subscriptions": ["azure-sub-b", "ghost"]},
        )

    assert result.status == "ok", result.error
    assert result.result is not None
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(AuditLog).where(AuditLog.path == GRAPH_QUERY_OP_ID))
        ).scalar_one()
    assert row.payload["subscriptions"] == result.result["subscriptions"]
    assert row.payload["subscriptions"]["denied"][0]["name"] == "ghost"  # type: ignore[index]
    assert row.payload["subscriptions"]["completed"] == ["azure-sub-a", "azure-sub-b"]  # type: ignore[index]


# ----------------------------------------------------------------------
# The fixed queries
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op_id", "query"),
    [
        ("azure.inventory.summary", SUMMARY_QUERY),
        ("azure.inventory.vm.list", VM_QUERY),
        ("azure.inventory.aks.list", AKS_QUERY),
        ("azure.inventory.network.list", NETWORK_QUERY),
    ],
)
@pytest.mark.asyncio
async def test_each_fixed_list_sends_its_query(
    azure: AzureConnector, op_id: str, query: str
) -> None:
    with respx.mock() as mock:
        _login(mock)
        graph = mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph([]))
        result = await _call(op_id, {})

    assert result.status == "ok", result.error
    body = _bodies(graph)[0]
    assert body["query"] == query
    assert body["subscriptions"] == [SUBSCRIPTION_A]
    assert set(body) == {"query", "subscriptions", "options"}


@pytest.mark.asyncio
async def test_the_summary_adds_up_the_counts(azure: AzureConnector) -> None:
    rows = [
        {"subscriptionId": SUBSCRIPTION_A, "type": "microsoft.compute/disks", "count_": 3},
        {
            "subscriptionId": SUBSCRIPTION_A,
            "type": "microsoft.network/virtualnetworks",
            "count_": 4,
        },
    ]
    with respx.mock() as mock:
        _login(mock)
        mock.post(url__startswith=GRAPH_URL).respond(200, json=_graph(rows))
        result = await _call("azure.inventory.summary", {})

    assert result.result is not None
    assert result.result["resource_total"] == 7


# ----------------------------------------------------------------------
# Candidates for `meho targets discover`
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_lists_the_subscriptions_that_are_not_targets_yet(
    azure: AzureConnector,
) -> None:
    seed = await _add_target(
        "azure-sub-a",
        SUBSCRIPTION_A,
        extras={"subscription_id": SUBSCRIPTION_A, "tenant_id": TENANT_A},
    )
    await _add_target("azure-sub-b", SUBSCRIPTION_B.upper())
    listed = [
        {"subscriptionId": SUBSCRIPTION_A, "displayName": "Seed", "state": "Enabled"},
        {"subscriptionId": SUBSCRIPTION_B, "displayName": "Known", "state": "Enabled"},
        {"subscriptionId": SUBSCRIPTION_C, "displayName": "Example Prod (EU)", "state": "Enabled"},
        {"subscriptionId": SUBSCRIPTION_D, "displayName": "Old!", "state": "Disabled"},
    ]
    with respx.mock() as mock:
        _login(mock)
        mock.get(SUBSCRIPTIONS_URL).respond(200, json={"value": listed})
        candidates = await azure.list_candidates(seed, operator=operator())

    assert [candidate.name for candidate in candidates] == [
        "azure-example-prod-eu-11111111",
        "azure-old-11111111",
    ]
    first, second = candidates
    assert (first.host, first.port, first.secret_ref) == (ARM_HOST, 443, LOGIN_A)
    assert dict(first.extras) == {
        "subscription_id": SUBSCRIPTION_C,
        "cloud": "AzureCloud",
        "tenant_id": TENANT_A,
    }
    assert first.confidence == "high" and second.confidence == "low"
    assert first.evidence["seed_target"] == "azure-sub-a"
    assert first.evidence["display_name"] == "Example Prod (EU)"


@pytest.mark.asyncio
async def test_discover_without_an_azure_seed_lists_nothing(azure: AzureConnector) -> None:
    other = await _add_target("vcenter-1", None, product="vmware", extras={})
    assert await azure.list_candidates(None, operator=operator()) == []
    assert await azure.list_candidates(other, operator=operator()) == []
