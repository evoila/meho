# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The sensor runner checks its rule against the full result (#4117).

A result over the reduction threshold (50 rows / 4 KB) reaches every caller
as a short summary plus a handle. Before #4117 the runner checked its rule
against that summary, so a rule on the rows or on a nested object read
``unknown`` (``missing key``) as soon as the answer grew. These tests drive
the real dispatcher with the production :class:`JsonFluxReducer` and an
in-memory result store, so the capture seam, the reduce, and the runner's
evaluation all run as in production.

The harness (tenant, typed connector, target, sensor rows, tick draining)
is the one ``test_sensor_runner.py`` already uses for its real-dispatch
tests; this module registers its own typed op whose payload a test chooses.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.assertions import AssertionSpec
from meho_backplane.checks.repository import create_sensor
from meho_backplane.checks.runner import run_one_sensor_tick
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import SensorCadenceKind
from meho_backplane.jsonflux.query.result_catalog import AdmissionGuard
from meho_backplane.operations import register_typed_operation, set_default_reducer
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.operations.reducer import FullResultCapture, full_result_capture_var
from tests._result_store_helpers import MemoryResultStore
from tests.test_sensor_runner import (
    _TENANT,
    _drain_in_flight,
    _embedding_stub,
    _force_due,
    _get_sensor,
    _K8sMouldConnector,
    _reset_registry,
    _seed_target,
    _seed_tenant,
    _settings_env,
)

__all__ = ["_embedding_stub", "_reset_registry", "_settings_env"]

_PRODUCT = "k8smould"
_CONNECTOR_ID = f"{_PRODUCT}-1.x"
_OP = f"{_PRODUCT}.host.list"

#: The payload the registered op returns; each test sets it.
_PAYLOAD: dict[str, Any] = {}


async def _payload_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    return _PAYLOAD


def _hosts(count: int, *, failing: set[int] = frozenset()) -> dict[str, Any]:
    """A long list: ``count`` host rows, ``connected`` false for ``failing``."""
    return {
        "hosts": [
            {"name": f"host-{index:03d}", "connected": index not in failing}
            for index in range(count)
        ],
        "total": count,
    }


def _prometheus(series: int, *, max_value: float) -> dict[str, Any]:
    """A big object with no top-level list, Prometheus-style, over 4 KB."""
    result = [
        {
            "metric": {
                "__name__": "node_filesystem_avail_bytes",
                "instance": f"node-{index:03d}.example.internal:9100",
                "job": "node-exporter",
                "mountpoint": "/var/lib/containers/storage/overlay",
            },
            "value_num": 1.0 + index,
        }
        for index in range(series)
    ]
    result[-1]["value_num"] = max_value
    return {
        "status": "success",
        "data": {"resultType": "vector", "result": result},
        "first_sample_value": result[0]["value_num"],
    }


def _assertion(path: str, aggregate: str | None, compare: dict[str, Any]) -> dict[str, Any]:
    select: dict[str, Any] = {"path": path}
    if aggregate is not None:
        select["aggregate"] = aggregate
    return AssertionSpec.model_validate({"select": select, "compare": compare}).model_dump(
        mode="json"
    )


_ALL_CONNECTED = _assertion("$.hosts[*].connected", "all", {"type": "bool", "expect": True})
_MAX_VALUE_UNDER_100 = _assertion(
    "$.data.result[*].value_num", "max", {"type": "threshold", "op": "gte", "critical": 100}
)


def _operator() -> Operator:
    return Operator(
        sub="op-1",
        name="Op",
        email=None,
        raw_jwt="",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _register_op(embedding_service: AsyncMock) -> None:
    register_connector_v2(product=_PRODUCT, version="", impl_id="", cls=_K8sMouldConnector)
    await register_typed_operation(
        product=_PRODUCT,
        version="1.x",
        impl_id=_PRODUCT,
        op_id=_OP,
        handler=_payload_handler,
        summary="List hosts.",
        description="Return every host of the resolved target.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding_service,
    )


async def _create_sensor(assertion: dict[str, Any]) -> uuid.UUID:
    await _seed_tenant()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await create_sensor(
            session,
            tenant_id=_TENANT,
            name=f"sensor-{uuid.uuid4().hex[:8]}",
            connector_id=_CONNECTOR_ID,
            op_id=_OP,
            target={"name": "k8s-prod"},
            params={},
            assertion=assertion,
            cadence_kind=SensorCadenceKind.INTERVAL,
            interval_seconds=300,
            cron_expr=None,
            timezone="UTC",
            severity="critical",
            for_seconds=0,
            retry_times=0,
            retry_backoff_seconds=15,
            identity_sub="__sensor__",
            created_by_sub="op-admin",
        )
        await session.commit()
        return row.id


async def _evaluate_once(
    embedding_stub: AsyncMock,
    *,
    payload: dict[str, Any],
    assertion: dict[str, Any],
    reducer: JsonFluxReducer | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run one real tick over *payload* and return ``(last_state, last_evidence)``."""
    _PAYLOAD.clear()
    _PAYLOAD.update(payload)
    set_default_reducer(
        reducer if reducer is not None else JsonFluxReducer(store=MemoryResultStore())
    )
    await _register_op(embedding_stub)
    await _seed_target(name="k8s-prod")
    sensor_id = await _create_sensor(assertion)
    await _force_due(sensor_id, datetime.now(UTC) - timedelta(seconds=1))

    await run_one_sensor_tick()
    await _drain_in_flight()

    row = await _get_sensor(sensor_id)
    assert row.last_evidence is not None
    return str(row.last_state), dict(row.last_evidence)


# --------------------------------------------------------------------------- #
# Long lists (over 50 rows)
# --------------------------------------------------------------------------- #


async def test_long_list_with_one_failing_row_reads_critical(
    _reset_registry: None, _embedding_stub: AsyncMock
) -> None:
    """AC1: 200 host rows, row 150 fails -> ``critical`` (was ``unknown``)."""
    state, evidence = await _evaluate_once(
        _embedding_stub, payload=_hosts(200, failing={150}), assertion=_ALL_CONNECTED
    )
    assert state == "critical", evidence
    assert "checked" not in evidence


async def test_long_list_with_all_rows_passing_reads_ok(
    _reset_registry: None, _embedding_stub: AsyncMock
) -> None:
    """AC2: 200 host rows, all connected -> ``ok``."""
    state, evidence = await _evaluate_once(
        _embedding_stub, payload=_hosts(200), assertion=_ALL_CONNECTED
    )
    assert state == "ok", evidence
    assert evidence["observed"] is True


async def test_evidence_sample_stays_bounded_at_five_rows(
    _reset_registry: None, _embedding_stub: AsyncMock
) -> None:
    """AC5: a breaching aggregate over 200 rows samples at most 5 of them."""
    state, evidence = await _evaluate_once(
        _embedding_stub,
        payload=_hosts(200, failing=set(range(20, 160))),
        assertion=_ALL_CONNECTED,
    )
    assert state == "critical", evidence
    assert len(evidence["sample"]) == 5
    assert evidence["sample_truncated"] is True


# --------------------------------------------------------------------------- #
# Big objects with no top-level list (over 4 KB, #4119)
# --------------------------------------------------------------------------- #


async def test_big_object_rule_on_nested_list_matches_full_answer(
    _reset_registry: None, _embedding_stub: AsyncMock
) -> None:
    """AC3: a Prometheus-style answer over 4 KB reads the same as the full answer."""
    payload = _prometheus(40, max_value=250.0)
    assert len(str(payload)) > 4096
    state, evidence = await _evaluate_once(
        _embedding_stub, payload=payload, assertion=_MAX_VALUE_UNDER_100
    )
    assert state == "critical", evidence
    assert evidence["observed"] == 250.0


async def test_big_object_rule_on_nested_list_reads_ok_when_healthy(
    _reset_registry: None, _embedding_stub: AsyncMock
) -> None:
    payload = _prometheus(40, max_value=50.0)
    state, evidence = await _evaluate_once(
        _embedding_stub, payload=payload, assertion=_MAX_VALUE_UNDER_100
    )
    assert state == "ok", evidence
    assert evidence["observed"] == 50.0


# --------------------------------------------------------------------------- #
# Summary rules, too-large results, small results
# --------------------------------------------------------------------------- #


async def test_rule_on_summary_row_count_still_works(
    _reset_registry: None, _embedding_stub: AsyncMock
) -> None:
    """AC4: ``$.row_count`` reads the shortened summary, as before #4117."""
    assertion = _assertion("$.row_count", None, {"type": "threshold", "op": "gt", "critical": 100})
    state, evidence = await _evaluate_once(
        _embedding_stub, payload=_hosts(200), assertion=assertion
    )
    assert state == "critical", evidence
    assert evidence["observed"] == 200
    assert evidence["checked"] == "summary"


async def test_rule_missing_in_both_views_reports_the_full_result_reason(
    _reset_registry: None, _embedding_stub: AsyncMock
) -> None:
    assertion = _assertion("$.nodes[*].ready", "all", {"type": "bool", "expect": True})
    state, evidence = await _evaluate_once(
        _embedding_stub, payload=_hosts(200), assertion=assertion
    )
    assert state == "unknown"
    assert evidence["reason"] == "missing key 'nodes'"
    assert "checked" not in evidence


async def test_result_over_admission_limit_reads_result_too_large(
    _reset_registry: None, _embedding_stub: AsyncMock
) -> None:
    """A payload the reducer refused under its work limit is not checked."""
    reducer = JsonFluxReducer(
        store=MemoryResultStore(), admission_guard=AdmissionGuard(max_nodes=50)
    )
    state, evidence = await _evaluate_once(
        _embedding_stub, payload=_hosts(200), assertion=_ALL_CONNECTED, reducer=reducer
    )
    assert state == "unknown"
    assert evidence["reason"] == "result_too_large"
    assert evidence["total_rows"] == 200


async def test_small_result_is_checked_as_before(
    _reset_registry: None, _embedding_stub: AsyncMock
) -> None:
    """Under the threshold nothing is shortened and nothing changes."""
    state, evidence = await _evaluate_once(
        _embedding_stub, payload=_hosts(40, failing={3}), assertion=_ALL_CONNECTED
    )
    assert state == "critical", evidence
    assert "checked" not in evidence


# --------------------------------------------------------------------------- #
# The dispatcher capture seam
# --------------------------------------------------------------------------- #


async def test_dispatch_fills_the_capture_and_still_shortens_the_result(
    _reset_registry: None, _embedding_stub: AsyncMock
) -> None:
    """The capture holds the full payload; the caller's result is unchanged."""
    _PAYLOAD.clear()
    _PAYLOAD.update(_hosts(200))
    set_default_reducer(JsonFluxReducer(store=MemoryResultStore()))
    await _register_op(_embedding_stub)
    operator = _operator()

    capture = FullResultCapture()
    token = full_result_capture_var.set(capture)
    try:
        result = await dispatch(
            operator=operator, connector_id=_CONNECTOR_ID, op_id=_OP, target=None, params={}
        )
    finally:
        full_result_capture_var.reset(token)

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert isinstance(result.result, dict)
    assert "hosts" not in result.result
    assert result.result["row_count"] == 200
    assert capture.captured is True
    assert capture.payload == _hosts(200)


async def test_dispatch_without_a_capture_is_unchanged(
    _reset_registry: None, _embedding_stub: AsyncMock
) -> None:
    _PAYLOAD.clear()
    _PAYLOAD.update(_hosts(200))
    set_default_reducer(JsonFluxReducer(store=MemoryResultStore()))
    await _register_op(_embedding_stub)
    operator = _operator()

    assert full_result_capture_var.get() is None
    result = await dispatch(
        operator=operator, connector_id=_CONNECTOR_ID, op_id=_OP, target=None, params={}
    )

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert full_result_capture_var.get() is None
