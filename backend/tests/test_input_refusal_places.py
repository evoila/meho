# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Each place that words a refused input with the shared rule has a test.

Each test sends a made-up value that the place refuses. The value is in no
answer, no stored text and no log line, and the text names the field and
the kind of error.
"""

from __future__ import annotations

import json
import logging
import secrets
import types
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from pydantic_ai import ModelRetry

from meho_backplane.agent import toolset_discovery
from meho_backplane.agent.toolset_operations import check_arguments
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks import runner as checks_runner
from meho_backplane.checks.dashboard_schemas import DashboardCreate
from meho_backplane.checks.sensor_edit import SensorEditInvalidError, SensorUpdate, _merge
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.connectors.targets.ops import model_params_problem
from meho_backplane.gateway.runner_checks import _outcome_for
from meho_backplane.input_refusal import refusal_text
from meho_backplane.mcp.registry import get_tool
from meho_backplane.operations.jsonflux_reducer import _resolve_pagination_hint
from meho_backplane.runner.client import RunnerClientError
from meho_backplane.runner.wire import RunnerResult
from meho_backplane.targets.schemas import TargetCreate
from tests.test_runner_client import _client

_ASSERTION = {
    "select": {"path": "$.count"},
    "compare": {"kind": "threshold", "op": "<=", "value": 1},
}


def _made() -> str:
    return f"zz{secrets.token_hex(6)}"


def _operator() -> Operator:
    return Operator(
        sub="person:places",
        name="Person",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=str(uuid.uuid4()),
        tenant_role=TenantRole.TENANT_ADMIN,
        scopes=frozenset({"mcp:admin"}),
    )


async def _never(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("the handler must not run for refused arguments")


def _sensor_row(**fields: Any) -> Any:
    row = {
        "name": "s1",
        "connector_id": "net-probe-1.x",
        "op_id": "net.tcp_check",
        "params": {},
        "assertion": _ASSERTION,
        "cadence_kind": "interval",
        "interval_seconds": 60,
        "cron_expr": None,
        "timezone": "UTC",
        "severity": "critical",
        "target": None,
        "for_seconds": 0,
        "retry_times": 0,
        "retry_backoff_seconds": 0,
        "identity_sub": "__sensor__",
    }
    row.update(fields)
    return types.SimpleNamespace(**row)


def test_a_sensor_edit_refusal_names_field_and_kind() -> None:
    made = _made()
    with pytest.raises(SensorEditInvalidError) as caught:
        _merge(_sensor_row(target=made), SensorUpdate(name="s2"))

    text = " ".join(caught.value.reasons)
    assert made not in text
    assert text.startswith("target: ")
    assert "(dict_type)" in text


async def test_a_runner_assignment_refusal_names_field_and_kind() -> None:
    made = _made()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"assignment_version": "v1", "items": made})

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(RunnerClientError) as caught:
            await client.fetch_assignment(known_version=None)

    assert made not in str(caught.value)
    assert "items: " in str(caught.value)
    assert "(list_type)" in str(caught.value)


async def test_a_central_check_assertion_refusal_names_field_and_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = _made()

    async def _no_floor(_snap: Any) -> None:
        return None

    async def _operator_for(_snap: Any) -> Operator:
        return _operator()

    async def _resolved(*_args: Any, **_kwargs: Any) -> tuple[None, None]:
        return None, None

    async def _ok(**_kwargs: Any) -> OperationResult:
        return OperationResult(
            status="ok", op_id="net.tcp_check", result={"count": 0}, duration_ms=1.0
        )

    monkeypatch.setattr(checks_runner, "_safe_tier_floor", _no_floor)
    monkeypatch.setattr(checks_runner, "_sensor_operator", _operator_for)
    monkeypatch.setattr(checks_runner, "_resolve_target_or_error", _resolved)
    monkeypatch.setattr(checks_runner, "dispatch", _ok)
    snap = checks_runner._SensorSnapshot(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="s1",
        connector_id="net-probe-1.x",
        op_id="net.tcp_check",
        target=None,
        params={},
        assertion={"select": made, "compare": made},
        identity_sub="__sensor__",
    )

    outcome = await checks_runner._run_evaluation(snap)

    error = str(outcome.evidence["error"])
    assert outcome.evidence["reason"] == "assertion_evaluation_error"
    assert made not in json.dumps(outcome.evidence, default=str)
    assert "select: " in error
    assert "(model_type)" in error


def test_a_runner_check_assertion_refusal_names_field_and_kind() -> None:
    made = _made()
    sensor: Any = types.SimpleNamespace(assertion={"select": made, "compare": made})
    item = RunnerResult(
        result_uid="r1", check_ref="c1", op_id="net.tcp_check", status="ok", result={"count": 0}
    )

    outcome = _outcome_for(sensor, item, too_large=False, received_at=datetime.now(UTC))

    error = str(outcome.evidence["error"])
    assert outcome.evidence["reason"] == "assertion_evaluation_error"
    assert made not in json.dumps(outcome.evidence, default=str)
    assert "select: " in error
    assert "(model_type)" in error


async def test_a_hosted_agent_operation_argument_refusal_names_field_and_kind() -> None:
    made = _made()
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}
    wrapped = check_arguments("call_operation", schema, _never)

    with pytest.raises(ModelRetry) as caught:
        await wrapped(_operator(), {"limit": made})

    assert made not in str(caught.value)
    assert "limit: type: integer (type)" in str(caught.value)


async def test_a_hosted_agent_discovery_argument_refusal_names_field_and_kind() -> None:
    made = _made()
    entry = get_tool("list_targets")
    assert entry is not None
    wrapped = toolset_discovery._governed_handler(entry[0], _never)

    with pytest.raises(ModelRetry) as caught:
        await wrapped(_operator(), {"limit": made})

    assert made not in str(caught.value)
    assert "limit: " in str(caught.value)
    assert "(type)" in str(caught.value)


def test_a_pagination_hint_refusal_logs_field_and_kind(
    capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    made = _made()
    caplog.set_level(logging.DEBUG)

    hint = _resolve_pagination_hint(
        {"op_id": "x.list", "pagination_hint": {"params": made, "example_next_call": {}}}
    )

    out, err = capfd.readouterr()
    logged = out + err + caplog.text
    assert hint is None
    assert "jsonflux_pagination_hint_validation_failed" in logged
    assert made not in logged
    assert "params" in logged
    assert "dict_type" in logged


def test_a_notify_email_refusal_keeps_its_fixed_text() -> None:
    made = _made()
    with pytest.raises(ValidationError) as caught:
        DashboardCreate(name="d1", notify_email=f"{made} at nowhere")

    text = refusal_text(caught.value)
    assert made not in text
    assert "notify_email: " in text
    assert "not a valid email address" in text
    assert "(value_error)" in text


def test_a_target_params_problem_cuts_the_map_key_in_its_path() -> None:
    made = _made()
    key = "k" * 60 + made
    fields = {"name": "t1", "product": "ssh", "host": "h", "secret_field_map": {key: 7}}

    problem = model_params_problem(TargetCreate, fields)

    assert problem is not None
    assert made not in json.dumps(problem, default=str)
    assert "secret_field_map." in json.dumps(problem)
    assert "string_type" in json.dumps(problem)
