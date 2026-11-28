# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A refused input names the field and the kind of error, never the input.

One test for each place that turns a refused input into text: the shared
module, the REST 422 body, the MCP tool input check, the ``call_operation``
check, the target service checks after an approval, the run after an
approval that is refused now (it writes its audit row), the MCP tools that
check their arguments with a model, and the settings and catalog readers.
Every test value is made up at run time.
"""

from __future__ import annotations

import json
import secrets
from typing import Annotated, Any, Literal

import jsonschema
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from meho_backplane.api.request_validation import request_validation_exception_handler
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.targets.ops import (
    TARGETS_REGISTER_OP_ID,
    TARGETS_REGISTRY_CONNECTOR_ID,
    targets_register,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.input_refusal import (
    PATH_CAP,
    PATH_PART_CAP,
    FixedMessageError,
    refusal_entries,
    refusal_path,
    refusal_text,
)
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.meta_tools import call_operation
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)
from tests.test_targets_model_refusal_stores_no_value import (
    _agent,
    _mcp_client,  # noqa: F401 — pytest-discovered fixture
    _registered_ops,  # noqa: F401 — pytest-discovered autouse fixture
    _stored_text,
    _target,  # noqa: F401 — pytest-discovered fixture
)


def _made() -> str:
    return f"zz{secrets.token_hex(6)}"


def _person() -> Operator:
    return Operator(
        sub="person:refused",
        name="Person",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.TENANT_ADMIN,
        scopes=frozenset({"mcp:admin"}),
    )


# ------------------------------------------------------------- shared module


class _Cat(BaseModel):
    kind: Literal["cat"]


class _Dog(BaseModel):
    kind: Literal["dog"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: int | None = None
    notes: str | None = None
    labels: dict[str, int] = {}
    pet: Annotated[_Cat | _Dog, Field(discriminator="kind")] | None = None
    rule: str | None = None

    @field_validator("notes")
    @classmethod
    def _notes(cls, value: str | None) -> str | None:
        raise ValueError(f"notes {value!r} is not allowed")

    @field_validator("rule")
    @classmethod
    def _rule(cls, value: str | None) -> str | None:
        raise FixedMessageError("rule must be empty")


def _refused(**fields: Any) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        _Body.model_validate(fields)
    return caught.value


def test_a_model_refusal_names_field_and_kind_never_the_value() -> None:
    made = _made()
    exc = _refused(port=made, notes=made, pet={"kind": made}, zz_extra=made)
    assert made in str(exc)

    text = refusal_text(exc)

    assert made not in text
    assert "port: Input should be a valid integer" in text
    assert "(int_parsing)" in text
    assert "notes: the value is not valid (value_error)" in text
    assert "pet: the value is not valid (union_tag_invalid)" in text
    assert "(extra_forbidden)" in text


def test_a_long_map_key_is_cut_in_the_path() -> None:
    key = _made() + "k" * 300
    text = refusal_text(_refused(labels={key: "x"}))

    assert key not in text
    assert text.startswith(f"labels.{key[: PATH_PART_CAP - 1]}…: ")
    assert "(int_parsing)" in text
    assert len(refusal_path(["a" * 30] * 20)) == PATH_CAP


def test_a_fixed_message_stays() -> None:
    assert refusal_text(_refused(rule="x")) == "rule: Value error, rule must be empty (value_error)"


def test_refusal_entries_keep_the_shape_without_the_input() -> None:
    made = _made()
    entries = refusal_entries(_refused(port=made, notes=made, rule="x").errors())

    assert made not in json.dumps(entries, default=str)
    assert all("input" not in entry for entry in entries)
    by_field = {entry["loc"][0]: entry for entry in entries}
    assert by_field["port"]["type"] == "int_parsing"
    assert by_field["notes"] == {
        "type": "value_error",
        "loc": ["notes"],
        "msg": "the value is not valid",
        "url": by_field["notes"]["url"],
    }
    assert by_field["rule"]["msg"] == "Value error, rule must be empty"


@pytest.mark.parametrize(
    ("instance", "expected"),
    [
        ({"port": "MADE"}, "port: type: integer (type)"),
        ({"mode": "MADE"}, 'mode: one of: "a", "b" (enum)'),
        ({"MADE": 1}, "params: undeclared property: "),
    ],
)
def test_a_schema_refusal_names_field_and_kind_never_the_value(
    instance: dict[str, Any], expected: str
) -> None:
    made = _made() + "q" * 60
    schema = {
        "type": "object",
        "properties": {"port": {"type": "integer"}, "mode": {"enum": ["a", "b"]}},
        "additionalProperties": False,
    }
    sent = {key.replace("MADE", made): value for key, value in instance.items()}
    sent = {key: made if value == "MADE" else value for key, value in sent.items()}
    with pytest.raises(jsonschema.ValidationError) as caught:
        jsonschema.validate(sent, schema)

    text = refusal_text(caught.value)

    assert made not in text
    assert text.startswith(expected)


# ------------------------------------------------------------- REST 422 body


def _rest_app() -> TestClient:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)

    @app.post("/call")
    async def _call(body: _Body) -> dict[str, str]:  # pragma: no cover - never reached
        return {}

    return TestClient(app)


def test_the_rest_422_keeps_its_list_and_drops_the_input() -> None:
    made = _made()
    response = _rest_app().post(
        "/call",
        json={"port": made, "notes": made, "labels": {"k" * 50 + made: made}, "zz_extra": made},
    )

    assert response.status_code == 422
    assert made not in response.text
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    for error in detail:
        assert {"loc", "msg", "type"} <= set(error)
        assert "input" not in error
    kinds = {error["type"] for error in detail}
    assert {"int_parsing", "value_error", "extra_forbidden"} <= kinds


# ------------------------------------------------------- MCP tool input check


async def test_the_mcp_input_check_names_field_and_kind_never_the_value(
    _target: TargetORM,  # noqa: F811
    _mcp_client: TestClient,  # noqa: F811
) -> None:
    made = _made()
    response = _mcp_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meho_targets_register",
                "arguments": {"name": "new-01", "product": "ssh", "host": "h", "port": made},
            },
        },
    )

    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS, body
    assert "arguments failed inputSchema: port: type: " in body["error"]["message"]
    assert "(type)" in body["error"]["message"]
    assert made not in response.text
    assert made not in await _stored_text()


async def test_a_path_key_that_cannot_be_encoded_gets_invalid_params(
    _target: TargetORM,  # noqa: F811
    _mcp_client: TestClient,  # noqa: F811
) -> None:
    key = "\ud800" + _made()
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "meho_targets_register",
            "arguments": {
                "name": "new-01",
                "product": "ssh",
                "host": "93.184.216.34",
                "secret_field_map": {key: 7},
            },
        },
    }
    response = _mcp_client.post(
        "/mcp",
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200, response.text
    error = response.json()["error"]
    assert error["code"] == INVALID_PARAMS, error
    assert "secret_field_map" in error["message"]
    assert "(type)" in error["message"]


# ------------------------------------------- call_operation: a long map key


async def test_call_operation_cuts_a_long_map_key_in_the_path(_target: TargetORM) -> None:  # noqa: F811
    key = _made() + "k" * 220
    params = {
        "name": "new-01",
        "product": "ssh",
        "host": "93.184.216.34",
        "secret_field_map": {key: 7},
    }

    answer = await call_operation(
        _agent(),
        {
            "connector_id": TARGETS_REGISTRY_CONNECTOR_ID,
            "op_id": TARGETS_REGISTER_OP_ID,
            "params": params,
        },
    )

    assert answer["extras"]["error_code"] == "invalid_params", answer
    text = json.dumps(answer, default=str)
    assert "secret_field_map" in text
    assert key not in text
    assert key[:PATH_PART_CAP] not in text
    assert key not in await _stored_text()


# -------------------------------------- service checks of the target handler


async def test_an_unknown_product_names_the_kind_not_the_value(_target: TargetORM) -> None:  # noqa: F811
    made = _made()
    with pytest.raises(ValueError) as caught:
        await targets_register(
            _person(), None, {"name": "new-01", "product": made, "host": "93.184.216.34"}
        )

    assert made not in str(caught.value)
    assert "kind='unknown_product'" in str(caught.value)
    assert "valid_products=" in str(caught.value)


async def test_a_secret_ref_outside_the_tenant_names_the_kind_not_the_value(
    _target: TargetORM,  # noqa: F811
) -> None:
    made = _made()
    params = {
        "name": "new-01",
        "product": "postgres",
        "host": "93.184.216.34",
        "secret_ref": f"secret/meho/other-team/{made}",
    }
    with pytest.raises(ValueError) as caught:
        await targets_register(_person(), None, params)

    assert made not in str(caught.value)
    assert "kind='secret_ref_outside_tenant_scope'" in str(caught.value)


# --------------------------------- a run after an approval that is refused now


async def test_an_approved_run_refused_by_the_params_check_writes_its_audit_row(
    _target: TargetORM,  # noqa: F811
) -> None:
    made = _made()
    params = {"name": "new-01", "product": "ssh", "host": "93.184.216.34", "port": made}

    result = await dispatch(
        operator=_person(),
        connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        op_id=TARGETS_REGISTER_OP_ID,
        target=None,
        params=params,
        _approved=True,
    )

    assert result.extras["error_code"] == "invalid_params"
    assert result.extras.get("audit_id") is not None or result.audit_id is not None
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].payload.get("result_status") == "error", rows[0].payload
    assert rows[0].policy_decision == "needs-approval"
    assert made not in json.dumps(result.model_dump(), default=str)
    assert made not in await _stored_text()


async def test_a_new_call_refused_by_the_params_check_writes_no_row(
    _target: TargetORM,  # noqa: F811
) -> None:
    params = {"name": "new-01", "product": "ssh", "host": "93.184.216.34", "port": _made()}

    result = await dispatch(
        operator=_person(),
        connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        op_id=TARGETS_REGISTER_OP_ID,
        target=None,
        params=params,
    )

    assert result.extras["error_code"] == "invalid_params"
    async with get_sessionmaker()() as session:
        assert (await session.execute(select(AuditLog))).scalars().all() == []


# ------------------------------------------- MCP tools that check with a model


def _model_tools() -> list[tuple[str, Any, dict[str, Any]]]:
    from meho_backplane.mcp.tools import (
        agent_grants,
        agent_principals,
        agents,
        broadcast_overrides,
        sensors,
    )

    sensor_id = "00000000-0000-4000-8000-000000000001"
    return [
        ("agents.create", agents._create_handler, {"MADE_KEY": "MADE"}),
        ("agents.edit", agents._edit_handler, {"name": "a1", "MADE_KEY": "MADE"}),
        ("broadcast_overrides.set", broadcast_overrides._set_handler, {"MADE_KEY": "MADE"}),
        ("agent_grants.create", agent_grants._create_grant_handler, {"MADE_KEY": "MADE"}),
        (
            "agent_principals.register",
            agent_principals._register_handler,
            {"name": "p1", "starting_grants": {"MADE_KEY": "MADE"}},
        ),
        ("sensors.create", sensors._create_handler, {"MADE_KEY": "MADE"}),
        ("sensors.results", sensors._results_handler, {"sensor_id": sensor_id, "MADE_KEY": "MADE"}),
    ]


def _fill(value: Any, made: str) -> Any:
    if isinstance(value, dict):
        return {
            key.replace("MADE_KEY", "zz_extra"): _fill(item, made) for key, item in value.items()
        }
    return made if value == "MADE" else value


@pytest.mark.parametrize(
    ("tool", "handler", "arguments"), _model_tools(), ids=[t[0] for t in _model_tools()]
)
async def test_an_mcp_tool_model_refusal_names_no_value(
    tool: str, handler: Any, arguments: dict[str, Any]
) -> None:
    made = _made()
    with pytest.raises(McpInvalidParamsError) as caught:
        await handler(_person(), _fill(arguments, made))

    text = str(caught.value) + json.dumps(getattr(caught.value, "data", None), default=str)
    assert made not in text, tool
    assert "input_value" not in text, tool
    assert ": " in str(caught.value) and str(caught.value).endswith(")"), (tool, str(caught.value))


@pytest.mark.parametrize("module", ["doc_collections_create", "doc_collections_update"])
async def test_a_doc_collection_refusal_lists_errors_without_the_input(module: str) -> None:
    from meho_backplane.mcp.tools import doc_collections_create, doc_collections_update

    made = _made()
    if module == "doc_collections_create":
        handler = doc_collections_create._create_doc_collections_handler
        arguments: dict[str, Any] = {"collection_key": made, "vendor": 7, "backend": [made]}
    else:
        handler = doc_collections_update._update_doc_collections_handler
        arguments = {"collection_key": "c1", "backend": [made], "zz_extra": made}

    with pytest.raises(McpInvalidParamsError) as caught:
        await handler(_person(), arguments)

    errors = caught.value.data["errors"]  # type: ignore[attr-defined]
    assert errors
    assert all("input" not in error and {"loc", "type"} <= set(error) for error in errors)
    assert made not in json.dumps(errors, default=str)


# ----------------------------------------------- settings and catalog readers


def test_the_runner_settings_refusal_names_the_field_not_the_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.runner.settings import RunnerConfigError, get_runner_settings

    monkeypatch.setenv("MEHO_RUNNER_CENTRAL_URL", "https://central.invalid")
    monkeypatch.setenv("MEHO_RUNNER_ID", "r1")
    monkeypatch.setenv("MEHO_RUNNER_TICK_INTERVAL_SECONDS", "-731.0091")
    get_runner_settings.cache_clear()
    try:
        with pytest.raises(RunnerConfigError) as caught:
            get_runner_settings()
    finally:
        get_runner_settings.cache_clear()

    assert "-731.0091" not in str(caught.value)
    assert "tick_interval_seconds: " in str(caught.value)
    assert "(greater_than)" in str(caught.value)


def test_the_catalog_refusal_names_the_field_not_the_value() -> None:
    from meho_backplane.operations.ingest.catalog import CatalogError, parse_catalog

    made = _made()
    with pytest.raises(CatalogError) as caught:
        parse_catalog(f"entries:\n  - product: [{made}]\n")

    assert made not in str(caught.value)
    assert "entries[0].product: " in str(caught.value)


def test_error_text_uses_the_rule_for_a_refused_input_only() -> None:
    from meho_backplane.input_refusal import error_text

    made = _made()
    assert made not in error_text(_refused(port=made))
    assert error_text(RuntimeError("plain text")) == "plain text"
