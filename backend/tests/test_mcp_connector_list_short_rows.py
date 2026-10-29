# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_connector_list`` short rows, ``product`` filter, and result handle (#4121).

A small model called ``meho_connector_list`` often, and each answer was about
24,000 characters for 55 connectors. These tests check, through
``tools/call``:

* no arguments gives short rows (six fields, no counts);
* ``detail="full"`` gives every ``ConnectorListItem`` field;
* ``product`` keeps only that product's rows, and empty text means no filter;
* the short ``status`` word matches the ``status`` filter, plus
  ``registered`` and ``mixed``;
* a long list comes back as a ``handle`` whose rows ``result_query`` reads.

The connector query is stubbed. The result-handle store is an in-memory
Valkey stand-in wired into both the reducer and ``result_query``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import meho_backplane.mcp.tools.connector_admin as connector_admin
import meho_backplane.operations.jsonflux_reducer as jsonflux_reducer_module
import meho_backplane.operations.result_query as result_query_core
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.result_handle_store import ResultHandleStore
from meho_backplane.operations.ingest import ConnectorListItem
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_SHORT_FIELDS = {"connector_id", "product", "version", "status", "dispatchable", "scope"}


class _MemoryValkey:
    """Minimal async Valkey shape the :class:`ResultHandleStore` reads and writes."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, name: str, value: Any, ex: int | None = None) -> None:
        del ex
        self.values[name] = value if isinstance(value, bytes) else str(value).encode()

    async def get(self, name: str) -> bytes | None:
        return self.values.get(name)


@pytest.fixture(autouse=True)
def result_store(monkeypatch: pytest.MonkeyPatch) -> ResultHandleStore:
    """One store wired into both the reducer's spill and ``result_query``'s read."""
    store = ResultHandleStore(_MemoryValkey())  # type: ignore[arg-type]
    monkeypatch.setattr(jsonflux_reducer_module, "get_result_handle_store", lambda: store)
    monkeypatch.setattr(result_query_core, "get_result_handle_store", lambda: store)
    return store


def _item(
    product: str,
    version: str,
    *,
    groups: tuple[int, int, int] = (0, 2, 0),
    state: str = "ingested",
) -> ConnectorListItem:
    """One row; *groups* is (staged, enabled, disabled) group counts."""
    staged, enabled, disabled = groups
    return ConnectorListItem(
        connector_id=f"{product}-rest-{version}",
        product=product,
        version=version,
        impl_id=f"{product}-rest",
        tenant_id=None,
        group_count=staged + enabled + disabled,
        staged_group_count=staged,
        enabled_group_count=enabled,
        disabled_group_count=disabled,
        operation_count=10,
        enabled_operation_count=4,
        state=state,  # type: ignore[arg-type]
        dispatchable=state == "ingested",
    )


_ROWS = [
    _item("vmware", "9.0"),
    _item("vmware", "8.0", groups=(1, 1, 0)),
    _item("k8s", "1.x", groups=(0, 0, 3)),
    _item("vault", "1.x", groups=(0, 1, 1)),
    _item("nsx", "9.0", groups=(0, 0, 0), state="registered"),
]


def _stub_rows(monkeypatch: pytest.MonkeyPatch, rows: list[ConnectorListItem]) -> None:
    async def _fake_list(**_kwargs: Any) -> list[ConnectorListItem]:
        return rows

    monkeypatch.setattr(connector_admin, "list_ingested_connectors", _fake_list)


def _call(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    body = response.json()
    assert "error" not in body, body
    result: dict[str, Any] = json.loads(body["result"]["content"][0]["text"])
    return result


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_no_arguments_returns_short_rows(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _op = client_with_operator
    _stub_rows(monkeypatch, _ROWS)

    rows = _call(client, "meho_connector_list", {})["connectors"]

    assert len(rows) == len(_ROWS)
    for row in rows:
        assert set(row) == _SHORT_FIELDS
        assert "operation_count" not in row
    assert rows[0] == {
        "connector_id": "vmware-rest-9.0",
        "product": "vmware",
        "version": "9.0",
        "status": "enabled",
        "dispatchable": True,
        "scope": "builtin",
    }


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_short_status_uses_the_filter_words(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _op = client_with_operator
    _stub_rows(monkeypatch, _ROWS)

    rows = _call(client, "meho_connector_list", {})["connectors"]

    assert [row["status"] for row in rows] == [
        "enabled",
        "staged",
        "disabled",
        "mixed",
        "registered",
    ]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_detail_full_returns_every_field(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _op = client_with_operator
    _stub_rows(monkeypatch, _ROWS)

    rows = _call(client, "meho_connector_list", {"detail": "full"})["connectors"]

    assert rows == [item.model_dump(mode="json") for item in _ROWS]
    assert rows[0]["operation_count"] == 10


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_product_filter_keeps_only_that_product(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _op = client_with_operator
    _stub_rows(monkeypatch, _ROWS)

    rows = _call(client, "meho_connector_list", {"product": "vmware"})["connectors"]

    assert [row["connector_id"] for row in rows] == ["vmware-rest-9.0", "vmware-rest-8.0"]


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_empty_product_means_no_filter(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _op = client_with_operator
    _stub_rows(monkeypatch, _ROWS)

    unfiltered = _call(client, "meho_connector_list", {})
    assert _call(client, "meho_connector_list", {"product": ""}) == unfiltered
    assert _call(client, "meho_connector_list", {"product": "  "}) == unfiltered


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_long_list_returns_a_handle_that_result_query_reads(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _op = client_with_operator
    rows = [_item(f"svc{i:02d}", "1.x") for i in range(60)]
    _stub_rows(monkeypatch, rows)

    payload = _call(client, "meho_connector_list", {})

    assert "connectors" not in payload
    handle = payload["handle"]
    assert handle["total_rows"] == 60
    assert handle["fetch_more"]["drill_in"]["available"] is True
    assert handle["fetch_more"]["drill_in"]["mcp_tool"] == "result_query"
    next_call = handle["fetch_more"]["native_pagination"]["example_next_call"]
    assert next_call["tool"] == "meho_connector_list"

    window = _call(
        client,
        "result_query",
        {"handle_id": handle["handle_id"], "offset": 0, "limit": 100},
    )
    assert [row["connector_id"] for row in window["rows"]] == [r.connector_id for r in rows]
    assert set(window["rows"][0]) == _SHORT_FIELDS
