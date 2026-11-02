# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Both operations of the example widget connector, run through MEHO.

The tests run inside a MEHO checkout: copy the connector folder to
``backend/src/meho_backplane/connectors/`` and this folder to
``backend/tests/`` (see README.md). They use MEHO's own test setup (a test
database, no Vault) and a fake widget API (``respx``). MEHO's test helpers
carry no promise: only the connector code must stay inside the kit.

Every call goes the way an agent's call goes: through MEHO's dispatcher,
with policy, approvals, audit, secret removal and result handles.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.example_widget import CONNECTOR_ID, ExampleWidgetConnector
from meho_backplane.connectors.example_widget import connector as widget_connector
from meho_backplane.connectors.example_widget.connector import widget_path
from meho_backplane.connectors.example_widget.operations import (
    LIST_OP_ID,
    RENAME_OP_ID,
    register_example_widget_operations,
)
from meho_backplane.connectors.registry import list_connector_impls
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations.approval_queue import get_request
from meho_backplane.operations.dispatcher import set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.reducer import PassThroughReducer
from meho_backplane.settings import get_settings
from meho_backplane.targets.resolver import resolve_target

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-00000000e1d9")
_OPERATOR = Operator(
    sub="example-widget-test",
    name="Example widget test operator",
    email=None,
    raw_jwt="<example-widget-test-jwt>",
    tenant_id=_TENANT_ID,
    tenant_role=TenantRole.OPERATOR,
)
_TARGET_NAME = "example-widgets"
_HOST = "widgets.example.invalid"
_BASE_URL = f"https://{_HOST}"
_TOKEN = "made-up-widget-token-for-tests"


def _widgets(first: int, count: int) -> list[dict[str, Any]]:
    return [
        {"id": f"w-{n:04d}", "name": f"widget {n}", "color": "blue"}
        for n in range(first, first + count)
    ]


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.example.invalid/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.example.invalid")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fake_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token comes from a stub, not from Vault."""

    async def _load(target: Any, operator: Operator, **_: Any) -> dict[str, object]:
        assert operator is _OPERATOR, "the token must be read as the caller"
        return {"token": _TOKEN}

    monkeypatch.setattr(widget_connector, "load_vault_secret_data", _load)


@pytest.fixture
async def widgets() -> AsyncIterator[ExampleWidgetConnector]:
    """The operations registered, a target in the database, and the connector."""
    reset_dispatcher_caches()
    set_default_reducer(PassThroughReducer())
    await register_example_widget_operations()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="example",
                host=_HOST,
                port=443,
                fqdn=None,
                secret_ref="example/widgets/api-token",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "1.x"},
                preferred_impl_id="example-widget",
                notes="made up by test_example_widget",
            )
        )
        await session.commit()
    connector = ExampleWidgetConnector()
    _CONNECTOR_INSTANCE_CACHE[ExampleWidgetConnector] = connector
    yield connector
    await connector.aclose()
    set_default_reducer(PassThroughReducer())
    reset_dispatcher_caches()


async def _call(op_id: str, params: dict[str, Any], *, approved: bool = False) -> dict[str, Any]:
    """Run one operation through MEHO's dispatcher, as the operator."""
    async with get_sessionmaker()() as session:
        target = await resolve_target(session, _TENANT_ID, _TARGET_NAME)
    result = await dispatch(
        operator=_OPERATOR,
        connector_id=CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=params,
        _approved=approved,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped


def _fake_api() -> respx.MockRouter:
    """The fake widget API. A route that a test sets up but does not call is fine."""
    return respx.mock(base_url=_BASE_URL, assert_all_called=False)


def _mount_pages(mock: respx.MockRouter, pages: list[dict[str, Any]]) -> respx.Route:
    return mock.get("/api/v1/widgets").mock(
        side_effect=[respx.MockResponse(200, json=page) for page in pages]
    )


def test_meho_lists_the_connector() -> None:
    assert ("example", "1.x", "example-widget") in list_connector_impls()
    assert CONNECTOR_ID == "example-widget-1.x"


async def test_list_reads_every_page_and_answers_with_a_result_handle(
    widgets: ExampleWidgetConnector,
) -> None:
    set_default_reducer(JsonFluxReducer())
    with _fake_api() as mock:
        route = _mount_pages(
            mock,
            [
                {"items": _widgets(1, 50), "next_page": 2},
                {"items": _widgets(51, 50), "next_page": 3},
                {"items": _widgets(101, 20), "next_page": None},
            ],
        )
        result = await _call(LIST_OP_ID, {})
    assert result["status"] == "ok", result
    assert [call.request.url.params["page"] for call in route.calls] == ["1", "2", "3"]
    assert all(c.request.headers["Authorization"] == f"Bearer {_TOKEN}" for c in route.calls)
    handle = result["handle"]
    assert handle is not None, result
    assert handle["total_rows"] == 120
    assert _TOKEN not in json.dumps(result)


async def test_list_of_one_page_returns_the_widgets(widgets: ExampleWidgetConnector) -> None:
    with _fake_api() as mock:
        _mount_pages(mock, [{"items": _widgets(1, 2), "next_page": None}])
        result = await _call(LIST_OP_ID, {})
    assert result["status"] == "ok", result
    assert [w["id"] for w in result["result"]["items"]] == ["w-0001", "w-0002"]


async def test_list_with_no_widgets_is_an_empty_list(widgets: ExampleWidgetConnector) -> None:
    with _fake_api() as mock:
        _mount_pages(mock, [{"items": [], "next_page": None}])
        result = await _call(LIST_OP_ID, {})
    assert result["status"] == "ok", result
    assert result["result"] == {"items": []}


async def test_a_refused_page_is_an_error_not_a_short_list(
    widgets: ExampleWidgetConnector,
) -> None:
    with _fake_api() as mock:
        mock.get("/api/v1/widgets").mock(
            side_effect=[
                respx.MockResponse(200, json={"items": _widgets(1, 50), "next_page": 2}),
                respx.MockResponse(401, json={"error": "token refused"}),
            ]
        )
        result = await _call(LIST_OP_ID, {})
    assert result["status"] == "error", result
    assert result["result"] is None


@pytest.mark.parametrize(
    "page",
    [
        {"next_page": None},
        {"items": "not a list", "next_page": None},
        {"items": [{"name": "no id"}], "next_page": None},
    ],
)
async def test_a_page_of_the_wrong_shape_is_an_error(
    widgets: ExampleWidgetConnector, page: dict[str, Any]
) -> None:
    with _fake_api() as mock:
        _mount_pages(mock, [page])
        result = await _call(LIST_OP_ID, {})
    assert result["status"] == "error", result
    assert result["result"] is None


async def test_a_list_that_never_ends_is_an_error(
    widgets: ExampleWidgetConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(widget_connector, "MAX_PAGES", 3)
    with _fake_api() as mock:
        route = mock.get("/api/v1/widgets").respond(
            200, json={"items": _widgets(1, 1), "next_page": 2}
        )
        result = await _call(LIST_OP_ID, {})
    assert result["status"] == "error", result
    assert route.call_count == 3


async def test_rename_waits_for_approval_and_shows_a_preview(
    widgets: ExampleWidgetConnector,
) -> None:
    with _fake_api() as mock:
        read = mock.get("/api/v1/widgets/w-0007").respond(
            200, json={"id": "w-0007", "name": "old name", "color": "red"}
        )
        change = mock.patch("/api/v1/widgets/w-0007").respond(200, json={})
        result = await _call(RENAME_OP_ID, {"widget_id": "w-0007", "new_name": "new name"})
    assert result["status"] == "awaiting_approval", result
    assert read.called
    assert not change.called, "nothing may change before a person approves"
    async with get_sessionmaker()() as session:
        request = await get_request(
            session,
            tenant_id=_TENANT_ID,
            request_id=uuid.UUID(result["extras"]["approval_request_id"]),
        )
    assert request.proposed_effect["preview"] == {
        "widget_id": "w-0007",
        "current_name": "old name",
        "new_name": "new name",
    }


async def test_rename_after_approval_changes_the_widget(
    widgets: ExampleWidgetConnector,
) -> None:
    with _fake_api() as mock:
        change = mock.patch("/api/v1/widgets/w-0007").respond(
            200, json={"id": "w-0007", "name": "new name", "color": "red"}
        )
        result = await _call(
            RENAME_OP_ID, {"widget_id": "w-0007", "new_name": "new name"}, approved=True
        )
    assert result["status"] == "ok", result
    assert result["result"] == {"id": "w-0007", "name": "new name", "color": "red"}
    assert json.loads(change.calls.last.request.content) == {"name": "new name"}
    assert change.calls.last.request.headers["Authorization"] == f"Bearer {_TOKEN}"


@pytest.mark.parametrize("widget_id", ["../admin", "w-0007/../../admin", "", "w 7"])
async def test_rename_refuses_an_id_that_is_not_a_widget_id(
    widgets: ExampleWidgetConnector, widget_id: str
) -> None:
    with _fake_api() as mock:
        result = await _call(RENAME_OP_ID, {"widget_id": widget_id, "new_name": "x"}, approved=True)
    assert result["status"] == "error", result
    assert not mock.calls, "no call may reach the widget API"
    with pytest.raises(ValueError, match="widget_id"):
        widget_path(widget_id)
