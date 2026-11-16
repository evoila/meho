# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``CONNECTOR_LIST_PRODUCT_ALIASES`` switch of ``meho_connector_list`` (#6560).

These tests check:

* switch off: the answer is the same as before, for every filter case,
  also on the MCP wire and for a long list behind a handle;
* switch on: a filter by another product name (``vcenter``) finds the
  connector and says which name matched; case is ignored;
* switch on: a filter that matches nothing lists the products that exist
  and a hint;
* the names come from the connector classes, and only from the classes
  registered for that product;
* the hosted agent's allow-list narrows the ``product_filter`` block.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import meho_backplane.mcp.tools._connector_product_filter as pf_mod
import meho_backplane.mcp.tools.connector_admin as ca_mod
from meho_backplane.agent.toolset_discovery import keep_allowed_connectors
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import Connector
from meho_backplane.mcp.tools._connector_product_filter import (
    connector_list_product_aliases,
    filter_by_product,
)
from meho_backplane.mcp.tools.connector_admin import (
    list_connector_rows,
    reduce_connector_list,
)
from meho_backplane.operations.ingest import ConnectorListItem
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_SWITCH = "CONNECTOR_LIST_PRODUCT_ALIASES"


class _VmwareConnector(Connector):
    """A stand-in class that declares other names for its product."""

    product = "vmware"
    product_aliases = ("vcenter", "vSphere", "", "  ")

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError


class _PlainConnector(Connector):
    """A stand-in class with no other names."""

    product = "vault"

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError


def _item(connector_id: str, product: str, version: str, impl_id: str) -> ConnectorListItem:
    return ConnectorListItem(
        connector_id=connector_id,
        product=product,
        version=version,
        impl_id=impl_id,
        tenant_id=None,
        group_count=2,
        staged_group_count=0,
        enabled_group_count=2,
        disabled_group_count=0,
        operation_count=10,
        enabled_operation_count=10,
    )


_ITEMS = [
    _item("vmware-rest-9.0", "vmware", "9.0", "vmware-rest"),
    _item("vmware-pyvmomi-7.0", "vmware", "7.0", "vmware-pyvmomi"),
    _item("vault-1.x", "vault", "1.x", "vault"),
]


@pytest.fixture(autouse=True)
def _stub_list_and_registry(
    monkeypatch: pytest.MonkeyPatch,
    isolated_registry: None,  # noqa: F811 — runs first: it reloads the tool module
) -> Iterator[list[UUID | None]]:
    """Stub the connector rows and the registry the filter reads.

    Yields the tenant ids the filter asked the registry for.
    """
    asked: list[UUID | None] = []

    async def _fake_list(**_kwargs: Any) -> list[ConnectorListItem]:
        return list(_ITEMS)

    def _fake_registry(tenant_id: UUID | None) -> dict[tuple[str, str, str], type[Connector]]:
        asked.append(tenant_id)
        return {
            ("vmware", "9.0", "vmware-rest"): _PlainConnector,
            ("vmware", "7.0", "vmware-pyvmomi"): _VmwareConnector,
            ("vault", "1.x", "vault"): _PlainConnector,
        }

    monkeypatch.setattr(ca_mod, "list_ingested_connectors", _fake_list)
    monkeypatch.setattr(pf_mod, "connectors_for_tenant", _fake_registry)
    monkeypatch.delenv(_SWITCH, raising=False)
    get_settings.cache_clear()
    yield asked
    get_settings.cache_clear()


def _switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SWITCH, "true")
    get_settings.cache_clear()


def _old_answer(product: str, detail: str = "short") -> dict[str, Any]:
    """The answer of the filter before #6560: exact product, no extra block."""
    return {
        "connectors": [
            ca_mod._connector_row(item, detail)
            for item in _ITEMS
            if not product or item.product == product
        ]
    }


def _ids(result: dict[str, Any]) -> list[str]:
    return [row["connector_id"] for row in result["connectors"]]


# ---------------------------------------------------------------------------
# Switch off: nothing changes
# ---------------------------------------------------------------------------


def test_switch_is_off_by_default() -> None:
    assert connector_list_product_aliases() is False


@pytest.mark.parametrize("product", ["", "vmware", "vcenter", "VMware", "nothing"])
@pytest.mark.parametrize("detail", ["short", "full"])
async def test_switch_off_answer_is_the_same_as_before(product: str, detail: str) -> None:
    operator = build_operator(TenantRole.OPERATOR)
    result = await list_connector_rows(operator, {"product": product, "detail": detail})
    assert json.dumps(result) == json.dumps(_old_answer(product, detail))


async def test_switch_off_does_not_read_the_registry(
    _stub_list_and_registry: list[UUID | None],
) -> None:
    await list_connector_rows(build_operator(TenantRole.OPERATOR), {"product": "vcenter"})
    assert _stub_list_and_registry == []


async def test_switch_off_with_target_keeps_the_old_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    target = SimpleNamespace(
        name="t1", product="vmware", version=None, fingerprint=None, preferred_impl_id=None
    )

    async def _fake_load(_operator: Operator, _name: str) -> Any:
        return target

    monkeypatch.setattr(ca_mod, "_load_target", _fake_load)
    result = await list_connector_rows(
        build_operator(TenantRole.OPERATOR), {"product": "vcenter", "target": "t1"}
    )
    assert list(result) == ["connectors", "target_fit"]
    assert result["connectors"] == []


async def test_switch_off_long_list_handle_is_the_same(monkeypatch: pytest.MonkeyPatch) -> None:
    many = [_item(f"vmware-rest-{n}.0", "vmware", f"{n}.0", "vmware-rest") for n in range(80)]

    async def _fake_list(**_kwargs: Any) -> list[ConnectorListItem]:
        return many

    captured: list[dict[str, Any]] = []

    async def _fake_reduce(rows: dict[str, Any], *_args: Any) -> tuple[dict[str, Any], Any]:
        captured.append(rows)
        return {"preview": "p"}, SimpleNamespace(model_dump=lambda mode: {"id": "h"})

    monkeypatch.setattr(ca_mod, "list_ingested_connectors", _fake_list)
    monkeypatch.setattr(ca_mod._CONNECTOR_LIST_REDUCER, "reduce", _fake_reduce)
    operator = build_operator(TenantRole.OPERATOR)
    reduced = await reduce_connector_list(
        operator, await list_connector_rows(operator, {"product": "vmware"})
    )
    assert reduced == {"preview": "p", "handle": {"id": "h"}}
    assert list(captured[0]) == ["connectors"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_switch_off_mcp_wire_answer_is_the_same(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho_connector_list", "arguments": {"product": "vcenter"}},
        },
    )
    assert response.status_code == 200
    [content] = response.json()["result"]["content"]
    assert json.loads(content["text"]) == {"connectors": []}


# ---------------------------------------------------------------------------
# Switch on: other names, case, and the product_filter block
# ---------------------------------------------------------------------------


async def test_switch_on_vcenter_finds_the_vmware_connectors(
    monkeypatch: pytest.MonkeyPatch,
    _stub_list_and_registry: list[UUID | None],
) -> None:
    _switch_on(monkeypatch)
    operator = build_operator(TenantRole.OPERATOR)
    result = await list_connector_rows(operator, {"product": "vcenter"})
    assert _ids(result) == ["vmware-rest-9.0", "vmware-pyvmomi-7.0"]
    assert result["product_filter"] == {
        "query": "vcenter",
        "matches": [{"product": "vmware", "matched_name": "vcenter"}],
    }
    assert _stub_list_and_registry == [operator.tenant_id]


@pytest.mark.parametrize(
    ("query", "matched_name"),
    [("VCENTER", "vcenter"), ("vsphere", "vSphere"), ("VMware", "vmware"), ("vmware", "vmware")],
)
async def test_switch_on_ignores_case(
    monkeypatch: pytest.MonkeyPatch, query: str, matched_name: str
) -> None:
    _switch_on(monkeypatch)
    result = await list_connector_rows(build_operator(TenantRole.OPERATOR), {"product": query})
    assert _ids(result) == ["vmware-rest-9.0", "vmware-pyvmomi-7.0"]
    assert result["product_filter"]["matches"] == [
        {"product": "vmware", "matched_name": matched_name}
    ]


async def test_switch_on_no_match_names_the_products_that_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    result = await list_connector_rows(build_operator(TenantRole.OPERATOR), {"product": "esxi"})
    assert result["connectors"] == []
    block = result["product_filter"]
    assert block["query"] == "esxi"
    assert block["matches"] == []
    assert block["known_products"] == ["vault", "vmware"]
    assert "known_products" in block["hint"]


async def test_switch_on_blank_product_is_no_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank ``product`` is no filter, as before; the blank names are never matched."""
    _switch_on(monkeypatch)
    result = await list_connector_rows(build_operator(TenantRole.OPERATOR), {"product": " "})
    # Blank text is "no filter", as before, with no product_filter block.
    assert "product_filter" not in result
    assert len(result["connectors"]) == 3


async def test_switch_on_without_product_answer_is_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    result = await list_connector_rows(build_operator(TenantRole.OPERATOR), {})
    assert json.dumps(result) == json.dumps(_old_answer(""))


async def test_switch_on_no_rows_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _empty(**_kwargs: Any) -> list[ConnectorListItem]:
        return []

    monkeypatch.setattr(ca_mod, "list_ingested_connectors", _empty)
    _switch_on(monkeypatch)
    result = await list_connector_rows(build_operator(TenantRole.OPERATOR), {"product": "vcenter"})
    assert result["connectors"] == []
    assert result["product_filter"]["known_products"] == []


def test_names_of_one_product_never_match_another() -> None:
    """``vcenter`` is a name of ``vmware`` only; a ``vault`` row never matches it."""
    rows, block = filter_by_product(
        [_item("vault-1.x", "vault", "1.x", "vault")], "vcenter", tenant_id=None
    )
    assert rows == []
    assert block["known_products"] == ["vault"]


async def test_switch_on_long_list_keeps_the_block_next_to_the_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    many = [_item(f"vmware-rest-{n}.0", "vmware", f"{n}.0", "vmware-rest") for n in range(80)]

    async def _fake_list(**_kwargs: Any) -> list[ConnectorListItem]:
        return many

    captured: list[dict[str, Any]] = []

    async def _fake_reduce(rows: dict[str, Any], *_args: Any) -> tuple[dict[str, Any], Any]:
        captured.append(rows)
        return {"preview": "p"}, SimpleNamespace(model_dump=lambda mode: {"id": "h"})

    def _registry(_tenant_id: UUID | None) -> dict[tuple[str, str, str], type[Connector]]:
        return {("vmware", "0.0", "vmware-rest"): _VmwareConnector}

    monkeypatch.setattr(ca_mod, "list_ingested_connectors", _fake_list)
    monkeypatch.setattr(pf_mod, "connectors_for_tenant", _registry)
    monkeypatch.setattr(ca_mod._CONNECTOR_LIST_REDUCER, "reduce", _fake_reduce)
    _switch_on(monkeypatch)
    operator = build_operator(TenantRole.OPERATOR)
    reduced = await reduce_connector_list(
        operator, await list_connector_rows(operator, {"product": "vcenter"})
    )
    assert list(captured[0]) == ["connectors"]
    assert reduced["product_filter"]["matches"] == [
        {"product": "vmware", "matched_name": "vcenter"}
    ]
    assert reduced["handle"] == {"id": "h"}


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_switch_on_mcp_wire_answer(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _op = client_with_operator
    _switch_on(monkeypatch)
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho_connector_list", "arguments": {"product": "vcenter"}},
        },
    )
    assert response.status_code == 200
    [content] = response.json()["result"]["content"]
    payload = json.loads(content["text"])
    assert [row["connector_id"] for row in payload["connectors"]] == [
        "vmware-rest-9.0",
        "vmware-pyvmomi-7.0",
    ]
    assert payload["product_filter"]["matches"] == [
        {"product": "vmware", "matched_name": "vcenter"}
    ]


# ---------------------------------------------------------------------------
# Hosted agent allow-list
# ---------------------------------------------------------------------------


async def test_allow_list_keeps_matches_of_allowed_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    result = await list_connector_rows(build_operator(TenantRole.OPERATOR), {"product": "vcenter"})
    narrowed = keep_allowed_connectors(result, frozenset({"vmware-rest-9.0"}))
    assert _ids(narrowed) == ["vmware-rest-9.0"]
    assert narrowed["product_filter"]["matches"] == [
        {"product": "vmware", "matched_name": "vcenter"}
    ]


async def test_allow_list_hides_products_the_agent_may_not_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch_on(monkeypatch)
    operator = build_operator(TenantRole.OPERATOR)
    for query in ("vcenter", "esxi"):
        result = await list_connector_rows(operator, {"product": query})
        narrowed = keep_allowed_connectors(result, frozenset({"vault-1.x"}))
        assert narrowed["connectors"] == []
        block = narrowed["product_filter"]
        assert block["query"] == query
        assert block["matches"] == []
        assert "known_products" not in block
        assert "this agent may use" in block["hint"]


async def test_allow_list_switch_off_has_no_block() -> None:
    result = await list_connector_rows(build_operator(TenantRole.OPERATOR), {"product": "vcenter"})
    narrowed = keep_allowed_connectors(result, frozenset({"vault-1.x"}))
    assert narrowed == {"connectors": []}
