# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Azure public DNS family (group ``dns``, #5368).

Every dispatch test runs the real dispatcher (policy, approval, audit) with
only the Azure registrar run. Microsoft's login service and Azure Resource
Manager are mocked with respx; the answers follow the shapes of Microsoft's
documented examples (DNS API version 2018-05-01). Every id, name and address
here is made up; the addresses come from the ranges reserved for
documentation and the zone is ``example.com``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import AsyncMock

import jsonschema
import pytest
import respx
from sqlalchemy import select

from meho_backplane.connectors.azure import (
    AZURE_CONNECTOR_ID,
    AZURE_IMPL_ID,
    AZURE_PRODUCT,
    AZURE_TYPED_WHEN_TO_USE_BY_GROUP,
    AZURE_VERSION,
    AzureConnector,
    _http,
    _paging,
    dns,
    dns_schemas,
    register_azure_typed_operations,
)
from meho_backplane.connectors.azure.dns_parse import record_set_body
from meho_backplane.connectors.azure.dns_reads import dns_record_set_show_impl
from meho_backplane.connectors.azure.dns_writes import delete_card, upsert_card
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    PreviewContext,
    blast_radius_missing_reason,
)
from meho_backplane.settings import get_settings
from tests._azure_fixtures import (
    ARM_BASE,
    ARM_HOST,
    SUBSCRIPTION_A,
    SUBSCRIPTION_B,
    AzureTargetStub,
    CountingLoader,
    login_token,
    operator,
    token_answer,
    token_url,
)

GROUP = "rg-dns-example"
ZONE = "example.com"
ETAG = "00000000-0000-0000-0000-00000000e001"
NEW_ETAG = "00000000-0000-0000-0000-00000000e002"
DNS_VERSION = "2018-05-01"
ZONE_BASE = (
    f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{GROUP}"
    f"/providers/Microsoft.Network/dnsZones/{ZONE}"
)
ZONES_URL = f"{ARM_BASE}/subscriptions/{SUBSCRIPTION_A}/providers/Microsoft.Network/dnsZones"
RECORD_SET_ID = (
    f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{GROUP}"
    f"/providers/Microsoft.Network/dnsZones/{ZONE}"
)

SHOW = {"resource_group": GROUP, "zone": ZONE, "record_type": "A", "name": "old-host"}
DELETE = {**SHOW, "etag": ETAG}
UPSERT = {
    "resource_group": GROUP,
    "zone": ZONE,
    "record_type": "A",
    "name": "www",
    "ttl": 3600,
    "records": ["203.0.113.10"],
}


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
def _no_real_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repeated read after a 5xx waits for no time at all."""

    async def instant(seconds: float) -> None:
        del seconds

    monkeypatch.setattr(_http, "_sleep", instant)


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


async def _call(
    op_id: str, params: dict[str, Any] | None = None, *, approved: bool = True
) -> OperationResult:
    return await dispatch(
        operator=operator(),
        connector_id=AZURE_CONNECTOR_ID,
        op_id=op_id,
        target=AzureTargetStub(),
        params=params or {},
        _approved=approved,
    )


def _login(mock: respx.MockRouter) -> respx.Route:
    return mock.post(token_url()).respond(200, json=token_answer(login_token()))


def _set_url(record_type: str, name: str) -> str:
    return f"{ZONE_BASE}/{record_type}/{name}"


def _record_set(
    record_type: str, name: str, props: dict[str, Any], etag: str = ETAG
) -> dict[str, Any]:
    return {
        "id": f"{RECORD_SET_ID}/{record_type}/{name}",
        "etag": etag,
        "name": name,
        "type": f"Microsoft.Network/dnsZones/{record_type}",
        "properties": {
            "fqdn": f"{name}.{ZONE}." if name != "@" else f"{ZONE}.",
            "TTL": 300,
            "provisioningState": "Succeeded",
            **props,
        },
    }


def _a_set(name: str = "old-host", etag: str = ETAG) -> dict[str, Any]:
    return _record_set("A", name, {"ARecords": [{"ipv4Address": "198.51.100.77"}]}, etag)


def _zone(name: str, group: str = GROUP, count: int = 4) -> dict[str, Any]:
    return {
        "id": f"/subscriptions/{SUBSCRIPTION_A}/resourceGroups/{group}"
        f"/providers/Microsoft.Network/dnsZones/{name}",
        "etag": ETAG,
        "location": "global",
        "name": name,
        "type": "Microsoft.Network/dnsZones",
        "properties": {
            "maxNumberOfRecordSets": 10000,
            "numberOfRecordSets": count,
            "nameServers": ["ns1-01.azure-dns.example", "ns2-01.azure-dns.example"],
            "zoneType": "Public",
        },
    }


def _error(code: str, message: str = "made-up message") -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _full_error(result: OperationResult) -> str:
    """The error class, and every text the dispatcher kept for the caller."""
    return f"{result.error} {dict(result.extras)}"


def _context(azure: AzureConnector, params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=cast("Any", None),
        connector_instance=azure,
        operator=operator(),
        target=AzureTargetStub(),
        params=params,
    )


# ----------------------------------------------------------------------
# The operation table
# ----------------------------------------------------------------------


def test_the_table_has_three_reads_and_two_writes() -> None:
    by_id = {op.op_id: op for op in dns.OPS}
    assert set(by_id) == {
        "azure.network.dns.zone.list",
        "azure.network.dns.recordset.list",
        "azure.network.dns.recordset.show",
        "azure.network.dns.recordset.upsert",
        "azure.network.dns.recordset.delete",
    }
    for op_id in ("zone.list", "recordset.list", "recordset.show"):
        read = by_id[f"azure.network.dns.{op_id}"]
        assert read.safety_level == "safe" and read.requires_approval is False
    upsert = by_id["azure.network.dns.recordset.upsert"]
    assert upsert.safety_level == "dangerous" and upsert.requires_approval is True
    delete = by_id["azure.network.dns.recordset.delete"]
    assert delete.safety_level == "destructive" and delete.requires_approval is True
    for op in dns.OPS:
        assert op.group_key == "dns"
        assert "dns" in AZURE_TYPED_WHEN_TO_USE_BY_GROUP
        assert callable(getattr(AzureConnector, op.handler_attr, None))
        assert op.llm_instructions is not None
        assert {"when_to_call", "output_shape", "next_step"} <= set(op.llm_instructions)


def test_the_delete_has_an_approval_card_builder() -> None:
    assert "azure.network.dns.recordset.delete" in _PREVIEW_BUILDERS
    assert "azure.network.dns.recordset.upsert" in _PREVIEW_BUILDERS


@pytest.mark.asyncio
async def test_the_ops_are_stored_with_their_risk_levels(azure: AzureConnector) -> None:
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.product == AZURE_PRODUCT,
                        EndpointDescriptor.op_id.like("azure.network.dns.%"),
                    )
                )
            )
            .scalars()
            .all()
        )
    stored = {row.op_id: (row.safety_level, row.requires_approval) for row in rows}
    assert stored == {
        "azure.network.dns.zone.list": ("safe", False),
        "azure.network.dns.recordset.list": ("safe", False),
        "azure.network.dns.recordset.show": ("safe", False),
        "azure.network.dns.recordset.upsert": ("dangerous", True),
        "azure.network.dns.recordset.delete": ("destructive", True),
    }


# ----------------------------------------------------------------------
# Zone list
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zone_list_shows_group_counts_and_name_servers(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        route = mock.get(ZONES_URL).respond(
            200, json={"value": [_zone(ZONE), _zone("example.org")]}
        )
        result = await _call("azure.network.dns.zone.list")

    assert result.status == "ok", result.error
    assert route.calls.last.request.url.params["api-version"] == DNS_VERSION
    rows = result.result["value"]
    assert [row["name"] for row in rows] == [ZONE, "example.org"]
    assert rows[0]["resource_group"] == GROUP
    assert rows[0]["record_set_count"] == 4 and rows[0]["max_record_sets"] == 10000
    assert rows[0]["zone_type"] == "Public"
    assert rows[0]["name_servers"] == ["ns1-01.azure-dns.example", "ns2-01.azure-dns.example"]
    assert result.result["truncated"] is False


@pytest.mark.asyncio
async def test_zone_list_follows_the_next_page(azure: AzureConnector) -> None:
    second = f"{ZONES_URL}?api-version={DNS_VERSION}&$skipToken=page2"
    with respx.mock() as mock:
        _login(mock)
        mock.get(ZONES_URL, params={"api-version": DNS_VERSION, "$skipToken": "page2"}).respond(
            200, json={"value": [_zone("b.example.com")]}
        )
        mock.get(ZONES_URL, params={"api-version": DNS_VERSION}).respond(
            200, json={"value": [_zone("a.example.com")], "nextLink": second}
        )
        result = await _call("azure.network.dns.zone.list")

    assert result.status == "ok", result.error
    assert [row["name"] for row in result.result["value"]] == ["a.example.com", "b.example.com"]
    assert result.result["pages_read"] == 2


@pytest.mark.asyncio
async def test_zone_list_with_no_zone_is_an_empty_list_not_an_error(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(ZONES_URL).respond(200, json={"value": []})
        result = await _call("azure.network.dns.zone.list")

    assert result.status == "ok", result.error
    assert result.result["value"] == [] and result.result["truncated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 500])
async def test_zone_list_failure_is_an_error_not_an_empty_list(
    azure: AzureConnector, status: int
) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(ZONES_URL).respond(status, json=_error("Failure"))
        result = await _call("azure.network.dns.zone.list")

    assert result.status == "error", result
    assert result.result is None or "value" not in (result.result or {})


# ----------------------------------------------------------------------
# Record set list and show
# ----------------------------------------------------------------------


def _mixed_sets() -> list[dict[str, Any]]:
    return [
        _a_set("www"),
        _record_set("AAAA", "www", {"AAAARecords": [{"ipv6Address": "2001:db8::10"}]}),
        _record_set("CNAME", "blog", {"CNAMERecord": {"cname": "blog.example.net"}}),
        _record_set(
            "MX",
            "@",
            {"MXRecords": [{"preference": 10, "exchange": "mail.example.com"}]},
        ),
        _record_set("TXT", "@", {"TXTRecords": [{"value": ["v=spf1 ", "-all"]}]}),
        _record_set(
            "SRV",
            "_sip._tcp",
            {
                "SRVRecords": [
                    {"priority": 1, "weight": 2, "port": 5060, "target": "sip.example.com"}
                ]
            },
        ),
        _record_set(
            "CAA", "@", {"caaRecords": [{"flags": 0, "tag": "issue", "value": "ca.example"}]}
        ),
        _record_set("NS", "@", {"NSRecords": [{"nsdname": "ns1-01.azure-dns.example."}]}),
        _record_set(
            "SOA",
            "@",
            {
                "SOARecord": {
                    "host": "ns1-01.azure-dns.example.",
                    "email": "hostmaster.example.com",
                    "serialNumber": 1,
                    "refreshTime": 3600,
                    "retryTime": 300,
                    "expireTime": 2419200,
                    "minimumTTL": 300,
                }
            },
        ),
        _record_set(
            "A",
            "alias",
            {"targetResource": {"id": "/subscriptions/s/resourceGroups/g/providers/x/y/z"}},
        ),
    ]


@pytest.mark.asyncio
async def test_recordset_list_shows_every_type_as_texts(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        route = mock.get(f"{ZONE_BASE}/recordsets").respond(200, json={"value": _mixed_sets()})
        result = await _call(
            "azure.network.dns.recordset.list", {"resource_group": GROUP, "zone": ZONE}
        )

    assert result.status == "ok", result.error
    assert route.calls.last.request.url.params["api-version"] == DNS_VERSION
    rows = result.result["value"]
    by_key = {(row["type"], row["name"]): row for row in rows}
    assert by_key[("A", "www")]["values"] == ["198.51.100.77"]
    assert by_key[("AAAA", "www")]["values"] == ["2001:db8::10"]
    assert by_key[("CNAME", "blog")]["values"] == ["blog.example.net"]
    assert by_key[("MX", "@")]["values"] == ["10 mail.example.com"]
    assert by_key[("TXT", "@")]["values"] == ["v=spf1 -all"]
    assert by_key[("SRV", "_sip._tcp")]["values"] == ["1 2 5060 sip.example.com"]
    assert by_key[("CAA", "@")]["values"] == ["0 issue ca.example"]
    assert by_key[("NS", "@")]["values"] == ["ns1-01.azure-dns.example."]
    assert by_key[("SOA", "@")]["values"][0].startswith("ns1-01.azure-dns.example. ")
    alias = by_key[("A", "alias")]
    assert alias["values"] == [] and alias["alias_target_id"].endswith("/z")
    assert by_key[("A", "www")]["ttl"] == 300 and by_key[("A", "www")]["etag"] == ETAG
    assert by_key[("A", "www")]["fqdn"] == f"www.{ZONE}."
    assert result.result["zone"] == ZONE


@pytest.mark.asyncio
async def test_recordset_list_edge_inputs(azure: AzureConnector) -> None:
    """A null name, a record without its address field and a wrong-typed field read as gaps."""
    odd = [
        {"name": None, "type": None, "properties": None},
        _record_set(
            "A", "half", {"ARecords": [{}, {"ipv4Address": 7}, {"ipv4Address": "192.0.2.1"}]}
        ),
        _record_set("MX", "bad", {"MXRecords": [{"preference": True, "exchange": "x"}]}),
        "not a record set",
    ]
    with respx.mock() as mock:
        _login(mock)
        mock.get(f"{ZONE_BASE}/recordsets").respond(200, json={"value": odd})
        result = await _call(
            "azure.network.dns.recordset.list", {"resource_group": GROUP, "zone": ZONE}
        )

    assert result.status == "ok", result.error
    rows = result.result["value"]
    assert len(rows) == 3
    assert rows[0]["name"] is None and rows[0]["values"] == []
    assert rows[1]["values"] == ["192.0.2.1"]
    assert rows[2]["values"] == []


@pytest.mark.asyncio
async def test_recordset_list_reads_every_page_and_says_when_a_limit_stopped_it(
    azure: AzureConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"{ZONE_BASE}/recordsets"
    page = {
        "value": [_a_set(f"h{n}") for n in range(3)],
        "nextLink": f"{url}?api-version={DNS_VERSION}&$skipToken=page2",
    }
    clock = iter([0.0, 500.0])
    monkeypatch.setattr(_paging, "_monotonic", lambda: next(clock))
    with respx.mock() as mock:
        _login(mock)
        route = mock.get(url).respond(200, json=page)
        result = await _call(
            "azure.network.dns.recordset.list", {"resource_group": GROUP, "zone": ZONE}
        )

    assert result.status == "ok", result.error
    assert route.call_count == 1
    assert len(result.result["value"]) == 3
    assert result.result["truncated"] is True
    assert result.result["omission_kind"] == "time_limit"


@pytest.mark.asyncio
async def test_recordset_list_of_a_missing_zone_is_not_found(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(f"{ZONE_BASE}/recordsets").respond(404, json=_error("ResourceNotFound"))
        result = await _call(
            "azure.network.dns.recordset.list", {"resource_group": GROUP, "zone": ZONE}
        )

    assert result.status == "not_found", result.error


@pytest.mark.asyncio
async def test_show_returns_records_etag_and_metadata(azure: AzureConnector) -> None:
    item = _a_set()
    item["properties"]["metadata"] = {"owner": "web team"}
    with respx.mock() as mock:
        _login(mock)
        mock.get(_set_url("A", "old-host")).respond(200, json=item)
        result = await _call("azure.network.dns.recordset.show", SHOW)

    assert result.status == "ok", result.error
    shown = result.result
    assert shown["etag"] == ETAG and shown["zone"] == ZONE
    assert shown["records"] == ["198.51.100.77"]
    assert shown["values"] == ["198.51.100.77"]
    assert shown["metadata"] == {"owner": "web team"}


@pytest.mark.asyncio
async def test_show_encodes_the_apex_and_a_wildcard_name(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        apex = mock.get(_set_url("MX", "@")).respond(
            200, json=_record_set("MX", "@", {"MXRecords": []})
        )
        star = mock.get(url__regex=rf"{re.escape(ZONE_BASE)}/TXT/(\*|%2A)").respond(
            200, json=_record_set("TXT", "*", {"TXTRecords": []})
        )
        first = await _call(
            "azure.network.dns.recordset.show", {**SHOW, "record_type": "MX", "name": "@"}
        )
        second = await _call(
            "azure.network.dns.recordset.show", {**SHOW, "record_type": "TXT", "name": "*"}
        )

    assert first.status == second.status == "ok", (first.error, second.error)
    assert apex.called and star.called


@pytest.mark.asyncio
async def test_show_of_a_missing_record_set_is_not_found(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(_set_url("A", "old-host")).respond(404, json=_error("ResourceNotFound"))
        result = await _call("azure.network.dns.recordset.show", SHOW)

    assert result.status == "not_found", result.error


@pytest.mark.asyncio
async def test_show_with_an_empty_answer_is_an_error_not_an_empty_record_set(
    azure: AzureConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(_set_url("A", "old-host")).respond(200, json={})
        result = await _call("azure.network.dns.recordset.show", SHOW)

    assert result.status == "error", result


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"zone": "../other"}, {"name": "a/b"}, {"record_type": "ANY"}])
async def test_a_bad_name_is_refused_before_any_call(
    azure: AzureConnector, bad: dict[str, str]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        arm = mock.route(host=ARM_HOST).respond(200, json={})
        result = await _call("azure.network.dns.recordset.show", {**SHOW, **bad})

    assert result.status == "error"
    assert not arm.called and not login.called


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["name", "zone", "resource_group"])
@pytest.mark.parametrize("bad", ["..", "../x", "a/b", "name\n", "a#b", "example.com."])
async def test_the_body_checks_the_names_itself(key: str, bad: str) -> None:
    """A call that skips the dispatcher's schema check still sends nothing."""
    connector = AzureConnector(credentials_loader=CountingLoader())
    with respx.mock(assert_all_called=False) as mock:
        arm = mock.route(host=ARM_HOST).respond(200, json={})
        with pytest.raises(ValueError, match="not a valid Azure name"):
            await dns_record_set_show_impl(
                connector, operator(), AzureTargetStub(), {**SHOW, key: bad}
            )

    assert not arm.called


@pytest.mark.asyncio
async def test_the_path_uses_the_target_subscription_only(azure: AzureConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        other = mock.get(url__regex=rf".*{SUBSCRIPTION_B}.*").respond(200, json={})
        mine = mock.get(_set_url("A", "old-host")).respond(200, json=_a_set())
        result = await _call(
            "azure.network.dns.recordset.show",
            {**SHOW, "resource_group": GROUP},
        )

    assert result.status == "ok", result.error
    assert mine.called and not other.called


# ----------------------------------------------------------------------
# Delete
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_reads_then_deletes_the_approved_version(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        read = mock.get(_set_url("A", "old-host")).respond(200, json=_a_set())
        gone = mock.delete(_set_url("A", "old-host")).respond(200)
        result = await _call("azure.network.dns.recordset.delete", DELETE)

    assert result.status == "ok", result.error
    assert read.call_count == 1 and gone.call_count == 1
    request = gone.calls.last.request
    assert request.headers["If-Match"] == ETAG
    assert request.url.params["api-version"] == DNS_VERSION
    assert result.result["deleted"] is True and result.result["already_absent"] is False
    previous = result.result["previous"]
    assert previous["values"] == ["198.51.100.77"] and previous["ttl"] == 300
    assert previous["etag"] == ETAG and previous["name"] == "old-host"


@pytest.mark.asyncio
async def test_delete_needs_an_etag(azure: AzureConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        login = _login(mock)
        arm = mock.route(host=ARM_HOST).respond(200, json={})
        params = {key: value for key, value in DELETE.items() if key != "etag"}
        result = await _call("azure.network.dns.recordset.delete", params)

    assert result.status == "error"
    assert not arm.called and not login.called


@pytest.mark.asyncio
async def test_delete_refuses_when_the_record_set_changed_since_the_approval(
    azure: AzureConnector,
) -> None:
    """The record set now has another etag: nothing is deleted."""
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(_set_url("A", "old-host")).respond(200, json=_a_set(etag=NEW_ETAG))
        gone = mock.delete(_set_url("A", "old-host")).respond(200)
        result = await _call("azure.network.dns.recordset.delete", DELETE)

    assert result.status == "error", result
    assert not gone.called
    assert "changed" in _full_error(result)


@pytest.mark.asyncio
async def test_delete_of_a_record_set_that_is_gone_deletes_nothing(azure: AzureConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(_set_url("A", "old-host")).respond(404, json=_error("ResourceNotFound"))
        gone = mock.delete(_set_url("A", "old-host")).respond(200)
        result = await _call("azure.network.dns.recordset.delete", DELETE)

    assert result.status == "ok", result.error
    assert not gone.called
    assert result.result["deleted"] is False and result.result["already_absent"] is True
    assert result.result["previous"] is None


@pytest.mark.asyncio
async def test_delete_answered_204_means_it_was_not_there(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(_set_url("A", "old-host")).respond(200, json=_a_set())
        mock.delete(_set_url("A", "old-host")).respond(204)
        result = await _call("azure.network.dns.recordset.delete", DELETE)

    assert result.status == "ok", result.error
    assert result.result["deleted"] is False and result.result["already_absent"] is True


@pytest.mark.asyncio
async def test_delete_with_a_precondition_failure_is_an_error(azure: AzureConnector) -> None:
    """Azure's 412 (the etag changed between the read and the delete) never reads as done."""
    with respx.mock() as mock:
        _login(mock)
        mock.get(_set_url("A", "old-host")).respond(200, json=_a_set())
        mock.delete(_set_url("A", "old-host")).respond(412, json=_error("PreconditionFailed"))
        result = await _call("azure.network.dns.recordset.delete", DELETE)

    assert result.status == "error", result
    assert "changed nothing" in _full_error(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 500])
async def test_delete_with_a_failed_read_deletes_nothing(
    azure: AzureConnector, status: int
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(_set_url("A", "old-host")).respond(status, json=_error("Failure"))
        gone = mock.delete(_set_url("A", "old-host")).respond(200)
        result = await _call("azure.network.dns.recordset.delete", DELETE)

    assert result.status == "error", result
    assert not gone.called


@pytest.mark.asyncio
async def test_delete_with_an_empty_read_answer_deletes_nothing(azure: AzureConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(_set_url("A", "old-host")).respond(200, json={})
        gone = mock.delete(_set_url("A", "old-host")).respond(200)
        result = await _call("azure.network.dns.recordset.delete", DELETE)

    assert result.status == "error", result
    assert not gone.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {**DELETE, "record_type": "SOA", "name": "@"},
        {**DELETE, "record_type": "NS", "name": "@"},
    ],
)
async def test_delete_never_removes_the_soa_or_the_zone_name_servers(
    azure: AzureConnector, params: dict[str, Any]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        arm = mock.route(host=ARM_HOST).respond(200, json={})
        result = await _call("azure.network.dns.recordset.delete", params)

    assert result.status == "error"
    assert not arm.called


@pytest.mark.asyncio
async def test_delete_waits_for_approval_and_only_reads_for_the_card(azure: AzureConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(_set_url("A", "old-host")).respond(200, json=_a_set())
        gone = mock.delete(_set_url("A", "old-host")).respond(200)
        result = await _call("azure.network.dns.recordset.delete", DELETE, approved=False)

    assert result.status != "ok", result
    assert not gone.called


# ----------------------------------------------------------------------
# Upsert
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_without_an_etag_only_creates(azure: AzureConnector) -> None:
    created = _record_set("A", "www", {"ARecords": [{"ipv4Address": "203.0.113.10"}], "TTL": 3600})
    with respx.mock() as mock:
        _login(mock)
        put = mock.put(_set_url("A", "www")).respond(201, json=created)
        result = await _call("azure.network.dns.recordset.upsert", UPSERT)

    assert result.status == "ok", result.error
    request = put.calls.last.request
    assert request.headers["If-None-Match"] == "*"
    assert "If-Match" not in request.headers
    assert request.url.params["api-version"] == DNS_VERSION
    assert json.loads(request.content) == {
        "properties": {"TTL": 3600, "ARecords": [{"ipv4Address": "203.0.113.10"}]}
    }
    assert result.result["state"] == "created"
    assert result.result["record_set"]["values"] == ["203.0.113.10"]


@pytest.mark.asyncio
async def test_upsert_with_an_etag_changes_that_version(azure: AzureConnector) -> None:
    updated = _record_set("A", "www", {"ARecords": [{"ipv4Address": "203.0.113.11"}]}, NEW_ETAG)
    with respx.mock() as mock:
        _login(mock)
        put = mock.put(_set_url("A", "www")).respond(200, json=updated)
        result = await _call(
            "azure.network.dns.recordset.upsert",
            {**UPSERT, "records": ["203.0.113.11"], "etag": ETAG},
        )

    assert result.status == "ok", result.error
    request = put.calls.last.request
    assert request.headers["If-Match"] == ETAG and "If-None-Match" not in request.headers
    assert result.result["state"] == "updated"
    assert result.result["record_set"]["etag"] == NEW_ETAG


@pytest.mark.asyncio
@pytest.mark.parametrize("etag", [None, ETAG])
async def test_upsert_refused_by_azure_changes_nothing_and_says_so(
    azure: AzureConnector, etag: str | None
) -> None:
    params = dict(UPSERT) if etag is None else {**UPSERT, "etag": etag}
    with respx.mock() as mock:
        _login(mock)
        put = mock.put(_set_url("A", "www")).respond(412, json=_error("PreconditionFailed"))
        result = await _call("azure.network.dns.recordset.upsert", params)

    assert result.status == "error", result
    assert put.call_count == 1
    assert "changed nothing" in _full_error(result)


@pytest.mark.asyncio
async def test_upsert_a_lost_answer_is_not_sent_twice(azure: AzureConnector) -> None:
    """A write is repeated only after a 429 or a 401, never after a 5xx."""
    with respx.mock() as mock:
        _login(mock)
        put = mock.put(_set_url("A", "www")).respond(500, json=_error("Failure"))
        result = await _call("azure.network.dns.recordset.upsert", UPSERT)

    assert result.status == "error", result
    assert put.call_count == 1


@pytest.mark.asyncio
async def test_upsert_with_an_empty_success_answer_still_shows_what_was_sent(
    azure: AzureConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.put(_set_url("A", "www")).respond(200, json={})
        result = await _call("azure.network.dns.recordset.upsert", UPSERT)

    assert result.status == "ok", result.error
    assert result.result["record_set"]["name"] == "www"
    assert result.result["record_set"]["values"] == ["203.0.113.10"]


@pytest.mark.asyncio
async def test_upsert_waits_for_approval_and_only_reads_for_the_card(azure: AzureConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        mock.get(_set_url("A", "www")).respond(404, json=_error("ResourceNotFound"))
        put = mock.put(_set_url("A", "www")).respond(201, json=_a_set("www"))
        result = await _call("azure.network.dns.recordset.upsert", UPSERT, approved=False)

    assert result.status == "awaiting_approval", result
    assert not put.called


@pytest.mark.parametrize(
    "records",
    [
        ["203.0.113.10"],
        [{"preference": 10, "exchange": "mail.example.com"}],
        [{"priority": 1, "weight": 2, "port": 5060, "target": "sip.example.com"}],
        [{"flags": 0, "tag": "issue", "value": "ca.example"}],
    ],
)
def test_the_parameter_schema_accepts_each_record_shape(records: list[Any]) -> None:
    schema = dns_schemas.RECORD_SET_UPSERT_PARAMS
    jsonschema.validate({**UPSERT, "records": records}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**UPSERT, "records": [{"unknown": 1}]}, schema)


@pytest.mark.parametrize(
    ("record_type", "records", "properties"),
    [
        ("AAAA", ["2001:db8::1"], {"AAAARecords": [{"ipv6Address": "2001:db8::1"}]}),
        ("CNAME", ["target.example.net"], {"CNAMERecord": {"cname": "target.example.net"}}),
        ("PTR", ["host.example.com"], {"PTRRecords": [{"ptrdname": "host.example.com"}]}),
        ("TXT", ["hello world"], {"TXTRecords": [{"value": ["hello world"]}]}),
        (
            "MX",
            [{"preference": 10, "exchange": "mail.example.com"}],
            {"MXRecords": [{"preference": 10, "exchange": "mail.example.com"}]},
        ),
        (
            "SRV",
            [{"priority": 1, "weight": 2, "port": 5060, "target": "sip.example.com"}],
            {
                "SRVRecords": [
                    {"priority": 1, "weight": 2, "port": 5060, "target": "sip.example.com"}
                ]
            },
        ),
        (
            "CAA",
            [{"flags": 0, "tag": "issue", "value": "ca.example"}],
            {"caaRecords": [{"flags": 0, "tag": "issue", "value": "ca.example"}]},
        ),
    ],
)
def test_the_body_follows_azures_field_names(
    record_type: str, records: list[Any], properties: dict[str, Any]
) -> None:
    body = record_set_body({"ttl": 60, "records": records}, record_type)
    assert body == {"properties": {"TTL": 60, **properties}}


@pytest.mark.parametrize(
    ("record_type", "params"),
    [
        ("A", {"ttl": 60, "records": ["not-an-address"]}),
        ("A", {"ttl": 60, "records": ["2001:db8::1"]}),
        ("AAAA", {"ttl": 60, "records": ["192.0.2.1"]}),
        ("A", {"ttl": 60, "records": [7]}),
        ("A", {"ttl": 60, "records": []}),
        ("A", {"ttl": 60}),
        ("A", {"ttl": 0, "records": ["192.0.2.1"]}),
        ("A", {"ttl": True, "records": ["192.0.2.1"]}),
        ("A", {"ttl": "60", "records": ["192.0.2.1"]}),
        ("CNAME", {"ttl": 60, "records": ["a.example.net", "b.example.net"]}),
        ("TXT", {"ttl": 60, "records": ["x" * 256]}),
        ("TXT", {"ttl": 60, "records": [""]}),
        ("MX", {"ttl": 60, "records": [{"preference": "10", "exchange": "mail.example.com"}]}),
        ("MX", {"ttl": 60, "records": [{"preference": 10}]}),
        ("MX", {"ttl": 60, "records": [{"preference": True, "exchange": "m.example.com"}]}),
        ("SRV", {"ttl": 60, "records": [{"priority": 1, "weight": 2, "port": 5, "extra": 1}]}),
        ("NS", {"ttl": 60, "records": ["ns.example.com"]}),
        ("SOA", {"ttl": 60, "records": ["x"]}),
    ],
)
def test_a_wrong_record_is_refused_before_any_call(
    record_type: str, params: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        record_set_body(params, record_type)


@pytest.mark.asyncio
@pytest.mark.parametrize("record_type", ["NS", "SOA"])
async def test_upsert_never_changes_the_zone_name_servers_or_the_soa(
    azure: AzureConnector, record_type: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _login(mock)
        arm = mock.route(host=ARM_HOST).respond(200, json={})
        result = await _call(
            "azure.network.dns.recordset.upsert",
            {**UPSERT, "record_type": record_type, "name": "@"},
        )

    assert result.status == "error"
    assert not arm.called


# ----------------------------------------------------------------------
# The approval cards
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_delete_card_shows_the_current_values_and_that_it_cannot_be_undone(
    azure: AzureConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(_set_url("A", "old-host")).respond(200, json=_a_set())
        card = await delete_card(_context(azure, DELETE))

    assert card is not None
    assert blast_radius_missing_reason(card) is None
    block = card["blast_radius"]
    assert block["object"]["values"] == ["198.51.100.77"]
    assert block["object"]["etag"] == ETAG and block["object"]["ttl"] == 300
    assert block["object"]["fqdn"] == f"old-host.{ZONE}."
    assert block["object"]["exists"] is True
    assert block["irreversibility"].startswith("permanent")
    assert block["children"] == []


@pytest.mark.asyncio
async def test_the_delete_card_says_when_the_record_set_is_already_gone(
    azure: AzureConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(_set_url("A", "old-host")).respond(404, json=_error("ResourceNotFound"))
        card = await delete_card(_context(azure, DELETE))

    assert card is not None and blast_radius_missing_reason(card) is None
    assert card["blast_radius"]["object"]["exists"] is False
    assert card["blast_radius"]["irreversibility"].startswith("none")


@pytest.mark.asyncio
async def test_the_delete_card_refuses_a_record_set_that_is_not_the_named_version(
    azure: AzureConnector,
) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(_set_url("A", "old-host")).respond(200, json=_a_set(etag=NEW_ETAG))
        with pytest.raises(Exception, match="changed after the approved version"):
            await delete_card(_context(azure, DELETE))


@pytest.mark.asyncio
async def test_the_delete_card_fails_when_the_read_fails(azure: AzureConnector) -> None:
    """A failed read gives no card (so the delete does not wait), never an empty one."""
    with respx.mock() as mock:
        _login(mock)
        mock.get(_set_url("A", "old-host")).respond(500, json=_error("Failure"))
        with pytest.raises(Exception, match="HTTP 500"):
            await delete_card(_context(azure, DELETE))


@pytest.mark.asyncio
async def test_the_cards_decline_without_a_live_connector() -> None:
    context = PreviewContext(
        descriptor=cast("Any", None),
        connector_instance=None,
        operator=operator(),
        target=AzureTargetStub(),
        params=DELETE,
    )
    assert await delete_card(context) is None
    assert await upsert_card(context) is None


@pytest.mark.asyncio
async def test_the_upsert_card_shows_the_current_and_the_new_values(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(_set_url("A", "www")).respond(200, json=_a_set("www"))
        card = await upsert_card(_context(azure, {**UPSERT, "etag": ETAG}))

    assert card is not None
    assert card["current"]["values"] == ["198.51.100.77"]
    assert card["proposed"] == {"ttl": 3600, "values": ["203.0.113.10"]}
    assert card["etag_matches_current"] is True
    assert card["mode"].startswith("change")


@pytest.mark.asyncio
async def test_the_upsert_card_of_a_new_name_says_it_does_not_exist(azure: AzureConnector) -> None:
    with respx.mock() as mock:
        _login(mock)
        mock.get(_set_url("A", "www")).respond(404, json=_error("ResourceNotFound"))
        card = await upsert_card(_context(azure, UPSERT))

    assert card is not None
    assert card["current"]["exists"] is False
    assert card["mode"] == "create only"
