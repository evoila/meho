# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A parameter that waits behind a switch (``x-meho-switch``).

These tests check:

* with the switch off (the default), the parameter is left out of
  ``search_operations`` (ranked and exact-op), ``preview_operation`` and
  the operation read, also when it sits in a nested object;
* a call that gives the parameter is refused like any unknown parameter,
  before it can wait for approval, and no approval request is stored;
* with the switch on, the parameter shows and a call that gives it waits
  for approval as usual;
* a keyword that names an unknown switch stops the operation from
  registering, for a typed and for an ingested operation;
* the stored schema is never changed.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations.ingest.safety_floors import apply_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.operations.meta_tools import (
    describe_descriptor,
    preview_operation,
    search_operations,
)
from meho_backplane.operations.switch_params import (
    FILE_REF_PARAMS_SWITCH,
    SWITCH_MARKER,
    check_switch_names,
    switch_names,
    visible_parameter_schema,
)
from meho_backplane.settings import get_settings

_SWITCH = "FILE_REF_PARAMS"
_TENANT = UUID("00000000-0000-0000-0000-00000000a0a0")
_CONNECTOR = "widget-1.x"
_OP = "widget.upload"
_HIDDEN = "expected_checksum"
_NESTED_HIDDEN = "file_record"


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            _HIDDEN: {"type": "string", SWITCH_MARKER: FILE_REF_PARAMS_SWITCH},
            "options": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string"},
                    _NESTED_HIDDEN: {"type": "string", SWITCH_MARKER: FILE_REF_PARAMS_SWITCH},
                },
                "required": ["mode", _NESTED_HIDDEN],
            },
        },
        "required": ["name", _HIDDEN],
        "additionalProperties": False,
    }


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("SEARCH_HIT_CALL_HINTS", "true")
    monkeypatch.setenv("PREVIEW_TYPED_WRITES", "true")
    monkeypatch.delenv(_SWITCH, raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return service


def _switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SWITCH, "true")
    get_settings.cache_clear()


class _WidgetConnector(Connector):
    product = "widget"
    version = "1.x"
    impl_id = "widget"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> OperationResult:
        raise NotImplementedError


async def _handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {"echo": params}


async def _register(embedding: AsyncMock, schema: dict[str, Any] | None = None) -> None:
    register_connector_v2(product="widget", version="1.x", impl_id="widget", cls=_WidgetConnector)
    await register_typed_operation(
        product="widget",
        version="1.x",
        impl_id="widget",
        op_id=_OP,
        handler=_handler,
        summary="Upload a widget file.",
        description="Upload a widget file.",
        parameter_schema=schema if schema is not None else _schema(),
        safety_level="caution",
        when_to_use=None,
        embedding_service=embedding,
    )


def _operator(kind: PrincipalKind = PrincipalKind.AGENT) -> Operator:
    return Operator(
        sub="op-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _approval_count() -> int:
    async with get_sessionmaker()() as session:
        return int(
            (await session.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


def _no_trace(value: Any, *names: str) -> None:
    text = json.dumps(value, default=str)
    for name in names:
        assert name not in text, name


# ---------------------------------------------------------------------------
# The schema helpers
# ---------------------------------------------------------------------------


def test_switch_names_counts_any_depth() -> None:
    assert switch_names(_schema()) == {FILE_REF_PARAMS_SWITCH}
    assert switch_names({"type": "object"}) == set()
    assert switch_names([{"a": {SWITCH_MARKER: "other"}}]) == {"other"}


def test_visible_schema_off_drops_the_parameter_at_every_depth() -> None:
    stored = _schema()
    snapshot = copy.deepcopy(stored)
    shown = visible_parameter_schema(stored)
    _no_trace(shown, _HIDDEN, _NESTED_HIDDEN)
    assert set(shown["properties"]) == {"name", "options"}
    assert shown["required"] == ["name"]
    assert shown["properties"]["options"]["required"] == ["mode"]
    assert stored == snapshot, "the stored schema is never changed"


def test_visible_schema_off_closes_an_open_schema() -> None:
    stored = _schema()
    del stored["additionalProperties"]
    shown = visible_parameter_schema(stored)
    assert shown["additionalProperties"] is False
    # A nested object that lost nothing stays as it was.
    assert "additionalProperties" in shown["properties"]["options"]


def test_visible_schema_on_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch_on(monkeypatch)
    stored = _schema()
    assert visible_parameter_schema(stored) is stored


@pytest.mark.parametrize("schema", [{}, {"type": "object"}, None, [], "text", {"properties": 3}])
def test_visible_schema_without_the_keyword_is_returned_as_it_is(schema: Any) -> None:
    assert visible_parameter_schema(schema) is schema


def test_a_keyword_that_is_not_a_name_hides_nothing_and_does_not_register() -> None:
    schema = {"type": "object", "properties": {"x": {"type": "string", SWITCH_MARKER: True}}}
    assert visible_parameter_schema(schema) == schema
    with pytest.raises(ValueError, match="unknown switch"):
        check_switch_names(schema, op_id="widget.x")


# ---------------------------------------------------------------------------
# Switch off: no trace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_shows_no_trace(stub_embedding_service: AsyncMock) -> None:
    await _register(stub_embedding_service)
    operator = _operator()

    ranked = await search_operations(
        operator, {"connector_id": _CONNECTOR, "query": "upload widget", "limit": 10}
    )
    assert [hit["op_id"] for hit in ranked["hits"]] == [_OP]
    assert ranked["hits"][0]["required_params"] == ["name"]
    _no_trace(ranked, _HIDDEN, _NESTED_HIDDEN, SWITCH_MARKER)

    exact = await search_operations(operator, {"connector_id": _CONNECTOR, "op_id": _OP})
    assert exact["operation"]["parameter_schema"]["required"] == ["name"]
    _no_trace(exact, _HIDDEN, _NESTED_HIDDEN, SWITCH_MARKER)

    preview = await preview_operation(
        operator, {"connector_id": _CONNECTOR, "op_id": _OP, "params": {"name": "a"}}
    )
    _no_trace(preview, _HIDDEN, _NESTED_HIDDEN, SWITCH_MARKER)

    async with get_sessionmaker()() as session:
        row_id = (
            await session.execute(
                select(EndpointDescriptor.id).where(EndpointDescriptor.op_id == _OP)
            )
        ).scalar_one()
    described = await describe_descriptor(operator, row_id)
    assert described is not None
    _no_trace(described.parameter_schema, _HIDDEN, _NESTED_HIDDEN, SWITCH_MARKER)


@pytest.mark.asyncio
async def test_switch_off_refuses_the_parameter_before_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    await _register(stub_embedding_service)

    for params in (
        {"name": "a", _HIDDEN: "abc"},
        {"name": "a", "options": {"mode": "m", _NESTED_HIDDEN: "r"}},
    ):
        result = await dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR,
            op_id=_OP,
            target=None,
            params=params,
        )
        assert result.status == "error"
        assert result.error is not None
        assert result.error.startswith("invalid_params")
    assert await _approval_count() == 0

    preview = await preview_operation(
        _operator(), {"connector_id": _CONNECTOR, "op_id": _OP, "params": {_HIDDEN: "x"}}
    )
    assert preview["status"] == "error"
    assert "invalid_params" in json.dumps(preview)

    # The same call without the parameter still waits for approval.
    ok = await dispatch(
        operator=_operator(), connector_id=_CONNECTOR, op_id=_OP, target=None, params={"name": "a"}
    )
    assert ok.status == "awaiting_approval"
    assert await _approval_count() == 1


@pytest.mark.asyncio
async def test_switch_off_refuses_the_parameter_of_an_open_schema(
    stub_embedding_service: AsyncMock,
) -> None:
    schema = _schema()
    del schema["additionalProperties"]
    await _register(stub_embedding_service, schema)
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR,
        op_id=_OP,
        target=None,
        params={"name": "a", _HIDDEN: "abc"},
    )
    assert result.error is not None
    assert result.error.startswith("invalid_params")
    assert await _approval_count() == 0


# ---------------------------------------------------------------------------
# Switch on: shown and accepted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_on_shows_and_accepts_the_parameter(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch_on(monkeypatch)
    await _register(stub_embedding_service)
    operator = _operator()

    ranked = await search_operations(
        operator, {"connector_id": _CONNECTOR, "query": "upload widget", "limit": 10}
    )
    assert ranked["hits"][0]["required_params"] == ["name", _HIDDEN]
    exact = await search_operations(operator, {"connector_id": _CONNECTOR, "op_id": _OP})
    assert _HIDDEN in exact["operation"]["parameter_schema"]["properties"]

    params = {"name": "a", _HIDDEN: "abc", "options": {"mode": "m", _NESTED_HIDDEN: "r"}}
    result = await dispatch(
        operator=operator, connector_id=_CONNECTOR, op_id=_OP, target=None, params=params
    )
    assert result.status == "awaiting_approval"
    assert await _approval_count() == 1


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_switch_stops_a_typed_operation_from_registering(
    stub_embedding_service: AsyncMock,
) -> None:
    schema = _schema()
    schema["properties"][_HIDDEN][SWITCH_MARKER] = "file_ref_param"
    with pytest.raises(ValueError, match="file_ref_param"):
        await _register(stub_embedding_service, schema)
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP))
        ).all()
    assert rows == []


def test_unknown_switch_stops_an_ingested_operation_from_registering() -> None:
    schema = _schema()
    schema["properties"]["options"]["properties"][_NESTED_HIDDEN][SWITCH_MARKER] = "typo"

    def proto(parameter_schema: dict[str, Any]) -> EndpointDescriptorProto:
        return EndpointDescriptorProto(
            op_id="GET:/v1/widget",
            method="GET",
            path="/v1/widget",
            summary="Read a widget.",
            description="Read a widget.",
            parameter_schema=parameter_schema,
            safety_level="safe",
            requires_approval=False,
        )

    with pytest.raises(ValueError, match="typo"):
        apply_safety_floor(product="widget", version="1.x", impl_id="widget", proto=proto(schema))
    apply_safety_floor(product="widget", version="1.x", impl_id="widget", proto=proto(_schema()))
