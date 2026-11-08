# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the operation search and call records (#4750).

* A search, then a call to the hit at rank 2, gives "in results, rank 2, ok".
* A call to an op that was not returned gives "not in results"; a call with
  no search before it gives "no search"; a search in another session or on
  another connector does not link.
* An ``unknown_op`` call, a raised error and exact-op mode are recorded.
* The query text is stored only when the setting is on, and never logged;
  the retention job deletes it with the record.
* Nothing is recorded without ``REQUEST_USAGE_RETENTION_DAYS``, and a failed
  write never changes the answer.
* The MCP tools, the REST routes and the hosted agent tools all record.
* The report counts per connector and day, for one tenant, over REST.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.api.v1 import operations as operations_api
from meho_backplane.api.v1 import retrieve_operation_picks as picks_api
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationCallRecord, OperationSearchRecord
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools import operations as operation_tools
from meho_backplane.operation_picks import recorder
from meho_backplane.operation_picks.recorder import (
    LINK_WINDOW,
    recorded_call,
    recorded_meta_tool,
    recorded_search,
)
from meho_backplane.operation_picks.report import compute_pick_report
from meho_backplane.operations._audit import agent_session_id_var
from meho_backplane.operations.meta_tools import UnknownConnectorError
from meho_backplane.request_usage.retention import prune_request_usage_once
from meho_backplane.retrieval.usage import CONVERSION_WINDOW
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_CONNECTOR = "k8s-1.x"
_OTHER_CONNECTOR = "vault-1.x"
_QUERY = "list the pods in a namespace"
_SESSION_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_SESSION_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000b0b0")


@pytest.fixture
def usage_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Turn collection on (30-day retention)."""
    monkeypatch.setenv("REQUEST_USAGE_RETENTION_DAYS", "30")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def operator() -> Operator:
    return build_operator(TenantRole.OPERATOR)


@pytest.fixture(autouse=True)
def _no_session() -> Iterator[None]:
    """Start each test with no MCP or agent session bound."""
    structlog.contextvars.unbind_contextvars("mcp_session_id")
    token = agent_session_id_var.set(None)
    yield
    agent_session_id_var.reset(token)
    structlog.contextvars.unbind_contextvars("mcp_session_id")


def _ranked(*op_ids: str, outcome: str = "matched") -> dict[str, Any]:
    return {
        "hits": [{"op_id": op_id} for op_id in op_ids],
        "query_duration_ms": 1.0,
        "outcome": outcome,
        "advisory": None,
        "narrowing": [],
    }


def _ok(op_id: str) -> dict[str, Any]:
    return {"status": "ok", "op_id": op_id, "data": {}}


async def _search(
    operator: Operator,
    *op_ids: str,
    connector_id: str = _CONNECTOR,
    transport: recorder.Transport = "mcp",
) -> dict[str, Any]:
    answer = _ranked(*op_ids)

    async def run() -> dict[str, Any]:
        return answer

    arguments = {"connector_id": connector_id, "query": _QUERY, "group": "core", "limit": 5}
    return await recorded_search(operator, arguments, transport=transport, run=run)


async def _call(
    operator: Operator,
    op_id: str,
    *,
    connector_id: str = _CONNECTOR,
    answer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = answer if answer is not None else _ok(op_id)

    async def run() -> dict[str, Any]:
        return result

    arguments = {"connector_id": connector_id, "op_id": op_id, "params": {}}
    return await recorded_call(operator, arguments, transport="mcp", run=run)


async def _searches() -> list[OperationSearchRecord]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(OperationSearchRecord).order_by(OperationSearchRecord.occurred_at)
        )
        return list(rows.scalars())


async def _calls() -> list[OperationCallRecord]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(OperationCallRecord).order_by(OperationCallRecord.occurred_at)
        )
        return list(rows.scalars())


def _bind_mcp_session(session_id: uuid.UUID) -> None:
    structlog.contextvars.bind_contextvars(mcp_session_id=str(session_id))


# ---------------------------------------------------------------------------
# The search record and the link
# ---------------------------------------------------------------------------


def test_link_window_matches_retrieval_usage() -> None:
    assert LINK_WINDOW == CONVERSION_WINDOW


@pytest.mark.usefixtures("usage_on")
async def test_search_record_holds_the_returned_ids_in_rank_order(operator: Operator) -> None:
    _bind_mcp_session(_SESSION_A)
    await _search(operator, "k8s.pod.list", "k8s.pod.get")

    [search] = await _searches()
    assert search.tenant_id == OPERATOR_TENANT_ID
    assert search.principal_sub == operator.sub
    assert search.session_id == _SESSION_A
    assert search.transport == "mcp"
    assert search.connector_id == _CONNECTOR
    assert search.mode == "ranked"
    assert search.group_key == "core"
    assert search.search_limit == 5
    assert search.outcome == "matched"
    assert search.hit_count == 2
    assert search.op_ids == ["k8s.pod.list", "k8s.pod.get"]
    assert search.query_hash is not None and len(search.query_hash) == 64
    assert search.query_text is None


@pytest.mark.usefixtures("usage_on")
async def test_call_to_rank_two_is_in_results_rank_two_ok(operator: Operator) -> None:
    _bind_mcp_session(_SESSION_A)
    await _search(operator, "k8s.pod.list", "k8s.pod.get", "k8s.pod.logs")
    await _call(operator, "k8s.pod.get")

    [search] = await _searches()
    [call] = await _calls()
    assert call.search_id == search.id
    assert call.rank == 2
    assert call.outcome == "ok"
    assert call.session_id == _SESSION_A


@pytest.mark.usefixtures("usage_on")
async def test_call_to_an_op_not_returned_is_not_in_results(operator: Operator) -> None:
    await _search(operator, "k8s.pod.list")
    await _call(operator, "k8s.node.list")

    [search] = await _searches()
    [call] = await _calls()
    assert call.search_id == search.id
    assert call.rank is None


@pytest.mark.usefixtures("usage_on")
async def test_call_without_a_search_has_no_link(operator: Operator) -> None:
    await _call(operator, "k8s.pod.list")

    [call] = await _calls()
    assert call.search_id is None
    assert call.rank is None


@pytest.mark.usefixtures("usage_on")
async def test_search_in_another_session_does_not_link(operator: Operator) -> None:
    _bind_mcp_session(_SESSION_A)
    await _search(operator, "k8s.pod.list")
    _bind_mcp_session(_SESSION_B)
    await _call(operator, "k8s.pod.list")

    [call] = await _calls()
    assert call.session_id == _SESSION_B
    assert call.search_id is None


@pytest.mark.usefixtures("usage_on")
async def test_search_without_session_does_not_link_to_a_session_call(
    operator: Operator,
) -> None:
    await _search(operator, "k8s.pod.list")
    _bind_mcp_session(_SESSION_A)
    await _call(operator, "k8s.pod.list")

    [call] = await _calls()
    assert call.search_id is None


@pytest.mark.usefixtures("usage_on")
async def test_search_on_another_connector_does_not_link(operator: Operator) -> None:
    await _search(operator, "k8s.pod.list", connector_id=_OTHER_CONNECTOR)
    await _call(operator, "k8s.pod.list")

    [call] = await _calls()
    assert call.search_id is None


@pytest.mark.usefixtures("usage_on")
async def test_search_of_another_caller_does_not_link(operator: Operator) -> None:
    other = operator.model_copy(update={"sub": "someone-else"})
    await _search(other, "k8s.pod.list")
    await _call(operator, "k8s.pod.list")

    [call] = await _calls()
    assert call.search_id is None


@pytest.mark.usefixtures("usage_on")
async def test_search_older_than_the_window_does_not_link(operator: Operator) -> None:
    await _search(operator, "k8s.pod.list")
    async with get_sessionmaker()() as session:
        [search] = (await session.execute(select(OperationSearchRecord))).scalars()
        search.occurred_at = datetime.now(UTC) - LINK_WINDOW - timedelta(seconds=5)
        await session.commit()
    await _call(operator, "k8s.pod.list")

    [call] = await _calls()
    assert call.search_id is None


@pytest.mark.usefixtures("usage_on")
async def test_call_links_to_the_newest_search(operator: Operator) -> None:
    await _search(operator, "k8s.pod.list")
    await _search(operator, "k8s.node.list", "k8s.pod.list")
    await _call(operator, "k8s.pod.list")

    _, newest = await _searches()
    [call] = await _calls()
    assert call.search_id == newest.id
    assert call.rank == 2


# ---------------------------------------------------------------------------
# Outcomes: unknown_op, errors, exact mode, other statuses
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("usage_on")
async def test_unknown_op_call_is_recorded(operator: Operator) -> None:
    await _search(operator, "k8s.pod.list")
    answer = {
        "status": "error",
        "op_id": "k8s.pod.lst",
        "error": "unknown_op: k8s.pod.lst",
        "extras": {"error_code": "unknown_op"},
    }
    assert await _call(operator, "k8s.pod.lst", answer=answer) == answer

    [call] = await _calls()
    assert call.outcome == "unknown_op"
    assert call.rank is None
    assert call.search_id is not None


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("error", "error"),
        ("denied", "denied"),
        ("awaiting_approval", "awaiting_approval"),
        ("pending", "pending"),
        ("something_new", "other"),
    ],
)
@pytest.mark.usefixtures("usage_on")
async def test_call_outcome_is_the_status(operator: Operator, status: str, outcome: str) -> None:
    await _call(operator, "k8s.pod.list", answer={"status": status})
    [call] = await _calls()
    assert call.outcome == outcome


@pytest.mark.usefixtures("usage_on")
async def test_raised_call_is_recorded_as_error_and_raised_again(operator: Operator) -> None:
    async def run() -> dict[str, Any]:
        raise RuntimeError("boom")

    arguments = {"connector_id": _CONNECTOR, "op_id": "k8s.pod.list"}
    with pytest.raises(RuntimeError, match="boom"):
        await recorded_call(operator, arguments, transport="rest", run=run)
    [call] = await _calls()
    assert call.outcome == "error"
    assert call.transport == "rest"


@pytest.mark.usefixtures("usage_on")
async def test_raised_search_is_recorded_and_raised_again(operator: Operator) -> None:
    error = UnknownConnectorError("no such connector", connector_id="nope-1.x")

    async def run() -> dict[str, Any]:
        raise error

    arguments = {"connector_id": "nope-1.x", "query": _QUERY}
    with pytest.raises(UnknownConnectorError) as raised:
        await recorded_search(operator, arguments, transport="mcp", run=run)
    assert raised.value is error
    [search] = await _searches()
    assert search.outcome == "unknown_connector"
    assert search.op_ids == []
    assert search.hit_count == 0


@pytest.mark.usefixtures("usage_on")
async def test_exact_op_search_returns_that_op_at_rank_one(operator: Operator) -> None:
    async def run() -> dict[str, Any]:
        return {"operation": {"op_id": "k8s.pod.get"}}

    arguments = {"connector_id": _CONNECTOR, "op_id": "k8s.pod.get", "query": _QUERY}
    await recorded_search(operator, arguments, transport="rest", run=run)
    await _call(operator, "k8s.pod.get")

    [search] = await _searches()
    assert search.mode == "exact"
    assert search.outcome == "exact_op"
    assert search.op_ids == ["k8s.pod.get"]
    assert search.query_hash is None
    assert search.search_limit is None
    [call] = await _calls()
    assert call.rank == 1


@pytest.mark.usefixtures("usage_on")
async def test_rate_limited_search_is_recorded(operator: Operator) -> None:
    async def run() -> dict[str, Any]:
        return {"status": "rate_limited", "retry_after_seconds": 3}

    await recorded_search(
        operator, {"connector_id": _CONNECTOR, "query": _QUERY}, transport="mcp", run=run
    )
    [search] = await _searches()
    assert search.outcome == "rate_limited"
    assert search.hit_count == 0


# ---------------------------------------------------------------------------
# Privacy, the on/off switch and failures
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("usage_on")
async def test_query_text_is_not_stored_or_logged_by_default(operator: Operator) -> None:
    with structlog.testing.capture_logs() as logs:
        await _search(operator, "k8s.pod.list")
        await _call(operator, "k8s.pod.list")

    [search] = await _searches()
    assert search.query_text is None
    stored = [str(value) for value in vars(search).values()]
    stored += [str(value) for call in await _calls() for value in vars(call).values()]
    assert not any(_QUERY in value for value in stored)
    assert not any(_QUERY in str(entry) for entry in logs)


@pytest.mark.usefixtures("usage_on")
async def test_query_text_is_stored_when_on_and_pruned_with_the_record(
    operator: Operator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPERATION_SEARCH_STORE_QUERY_TEXT", "true")
    get_settings.cache_clear()
    await _search(operator, "k8s.pod.list")
    await _call(operator, "k8s.pod.list")
    [search] = await _searches()
    assert search.query_text == _QUERY

    assert await prune_request_usage_once(now=datetime.now(UTC) + timedelta(days=31)) == 0
    assert await _searches() == []
    assert await _calls() == []


@pytest.mark.usefixtures("usage_on")
async def test_prune_keeps_records_inside_the_window(operator: Operator) -> None:
    await _search(operator, "k8s.pod.list")
    await _call(operator, "k8s.pod.list")
    await prune_request_usage_once(now=datetime.now(UTC) + timedelta(days=29))
    assert len(await _searches()) == 1
    assert len(await _calls()) == 1


async def test_nothing_is_recorded_without_retention(operator: Operator) -> None:
    assert get_settings().request_usage_retention_days is None
    await _search(operator, "k8s.pod.list")
    await _call(operator, "k8s.pod.list")
    assert await _searches() == []
    assert await _calls() == []


@pytest.mark.usefixtures("usage_on")
async def test_failed_write_never_changes_the_answer(
    operator: Operator, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> Any:
        raise RuntimeError("database is down")

    monkeypatch.setattr(recorder, "get_sessionmaker", broken)
    with structlog.testing.capture_logs() as logs:
        assert await _search(operator, "k8s.pod.list") == _ranked("k8s.pod.list")
        assert await _call(operator, "k8s.pod.list") == _ok("k8s.pod.list")
    failures = [entry for entry in logs if entry["event"] == "operation_pick_record_failed"]
    assert [entry["record"] for entry in failures] == ["search", "call"]
    assert all(entry["error_type"] == "RuntimeError" for entry in failures)


# ---------------------------------------------------------------------------
# The entry points: MCP, REST, hosted agent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retired", ["false", "true"])
@pytest.mark.usefixtures("usage_on")
async def test_mcp_tools_record_search_and_call(
    operator: Operator, monkeypatch: pytest.MonkeyPatch, retired: str
) -> None:
    monkeypatch.setenv("AUDIT_WRAPPER_ROWS_RETIRED", retired)
    get_settings.cache_clear()

    async def fake_search(_operator: Operator, _arguments: dict[str, Any]) -> dict[str, Any]:
        return _ranked("k8s.pod.list", "k8s.pod.get")

    async def fake_call(_operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        return _ok(arguments["op_id"])

    monkeypatch.setattr(operation_tools, "search_operations", fake_search)
    monkeypatch.setattr(operation_tools, "call_operation", fake_call)
    _bind_mcp_session(_SESSION_A)

    search_answer = await operation_tools._search_operations_handler(
        operator, {"connector_id": _CONNECTOR, "query": _QUERY}
    )
    call_answer = await operation_tools._call_operation_handler(
        operator, {"connector_id": _CONNECTOR, "op_id": "k8s.pod.get", "params": {}}
    )

    assert [hit["op_id"] for hit in search_answer["hits"]] == ["k8s.pod.list", "k8s.pod.get"]
    assert call_answer["status"] == "ok"
    [search] = await _searches()
    [call] = await _calls()
    assert (search.transport, call.transport) == ("mcp", "mcp")
    assert call.search_id == search.id
    assert call.rank == 2


@pytest.mark.usefixtures("usage_on")
async def test_mcp_unknown_op_in_exact_mode_is_recorded(
    operator: Operator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meho_backplane.operations._contract import UnknownOperationError

    async def fake_search(_operator: Operator, _arguments: dict[str, Any]) -> dict[str, Any]:
        raise UnknownOperationError("no such op", connector_id=_CONNECTOR, op_id="k8s.nope")

    monkeypatch.setattr(operation_tools, "search_operations", fake_search)
    with pytest.raises(McpInvalidParamsError):
        await operation_tools._search_operations_handler(
            operator, {"connector_id": _CONNECTOR, "op_id": "k8s.nope"}
        )
    [search] = await _searches()
    assert search.outcome == "unknown_op"
    assert search.mode == "exact"


@pytest.mark.usefixtures("usage_on")
async def test_mcp_status_check_of_a_run_writes_no_call_record(
    operator: Operator, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_poll(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "pending"}

    monkeypatch.setattr(operation_tools, "poll_call", fake_poll)
    await operation_tools._call_operation_handler(
        operator,
        {"connector_id": _CONNECTOR, "op_id": "k8s.pod.list", "run_id": str(uuid.uuid4())},
    )
    assert await _calls() == []


@pytest.mark.usefixtures("usage_on")
async def test_hosted_agent_tools_record_with_the_agent_session(operator: Operator) -> None:
    token = agent_session_id_var.set(_SESSION_A)
    try:

        async def search() -> dict[str, Any]:
            return _ranked("k8s.pod.list")

        async def call() -> dict[str, Any]:
            return _ok("k8s.pod.list")

        async def groups() -> dict[str, Any]:
            return {"groups": []}

        await recorded_meta_tool(
            "search_operations", operator, {"connector_id": _CONNECTOR, "query": _QUERY}, run=search
        )
        await recorded_meta_tool(
            "call_operation",
            operator,
            {"connector_id": _CONNECTOR, "op_id": "k8s.pod.list"},
            run=call,
        )
        await recorded_meta_tool(
            "list_operation_groups", operator, {"connector_id": _CONNECTOR}, run=groups
        )
    finally:
        agent_session_id_var.reset(token)

    [search_row] = await _searches()
    [call_row] = await _calls()
    assert (search_row.transport, call_row.transport) == ("agent", "agent")
    assert search_row.session_id == call_row.session_id == _SESSION_A
    assert call_row.rank == 1


def _rest_app(operator: Operator) -> FastAPI:
    app = FastAPI()
    app.include_router(operations_api.router)
    app.include_router(picks_api.router)
    app.dependency_overrides[operations_api._require_operator.dependency] = lambda: operator
    app.dependency_overrides[picks_api._require_operator.dependency] = lambda: operator
    return app


@pytest.mark.usefixtures("usage_on")
async def test_rest_routes_record_search_and_call(
    operator: Operator, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_search(_operator: Operator, _arguments: dict[str, Any]) -> dict[str, Any]:
        return _ranked("k8s.pod.list", "k8s.pod.get")

    async def fake_call(_operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        return _ok(arguments["op_id"])

    monkeypatch.setattr(operations_api, "search_operations", fake_search)
    monkeypatch.setattr(operations_api, "call_operation", fake_call)
    client = TestClient(_rest_app(operator))

    search = client.get(
        "/api/v1/operations/search", params={"connector_id": _CONNECTOR, "q": _QUERY}
    )
    call = client.post(
        "/api/v1/operations/call",
        json={"connector_id": _CONNECTOR, "op_id": "k8s.pod.list", "params": {}},
    )
    assert search.status_code == 200
    assert call.status_code == 200

    [search_row] = await _searches()
    [call_row] = await _calls()
    assert (search_row.transport, call_row.transport) == ("rest", "rest")
    assert call_row.search_id == search_row.id
    assert call_row.rank == 1


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


async def _seed_report_records(operator: Operator) -> None:
    """Two searches and five calls on one connector, one search elsewhere."""
    await _search(operator, "k8s.pod.list", "k8s.pod.get", "k8s.pod.logs", "k8s.node.list")
    await _call(operator, "k8s.pod.list")  # rank 1, ok
    await _call(operator, "k8s.pod.logs", answer={"status": "error"})  # rank 3, error
    await _call(operator, "k8s.node.list")  # rank 4, ok
    await _call(
        operator, "k8s.pod.lst", answer={"status": "error", "extras": {"error_code": "unknown_op"}}
    )
    await _search(operator)  # no hits; nothing follows it
    await _search(operator, "vault.kv.read", connector_id=_OTHER_CONNECTOR)
    async with get_sessionmaker()() as session:
        session.add(
            OperationCallRecord(
                id=uuid.uuid4(),
                occurred_at=datetime.now(UTC),
                tenant_id=operator.tenant_id,
                principal_sub=operator.sub,
                transport="mcp",
                connector_id=_CONNECTOR,
                op_id="k8s.pod.list",
                outcome="ok",
            )
        )
        session.add(
            OperationSearchRecord(
                id=uuid.uuid4(),
                occurred_at=datetime.now(UTC),
                tenant_id=_OTHER_TENANT,
                principal_sub="other",
                transport="mcp",
                connector_id=_CONNECTOR,
                mode="ranked",
                outcome="matched",
                hit_count=0,
                op_ids=[],
            )
        )
        await session.commit()


@pytest.mark.usefixtures("usage_on")
async def test_report_counts_per_connector_and_day(operator: Operator) -> None:
    await _seed_report_records(operator)
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        report = await compute_pick_report(
            session,
            tenant_id=operator.tenant_id,
            since=now - timedelta(days=1),
            until=now + timedelta(minutes=1),
        )

    by_connector = {bucket.connector_id: bucket for bucket in report.buckets}
    assert set(by_connector) == {_CONNECTOR, _OTHER_CONNECTOR}
    k8s = by_connector[_CONNECTOR]
    assert k8s.date == now.date()
    assert k8s.searches == 2
    # Every call links to the newest search at its time: the first search.
    assert k8s.searches_followed_by_call == 1
    assert k8s.calls == 5
    assert k8s.calls_in_results == 3
    assert k8s.calls_at_rank_1 == 1
    assert k8s.calls_in_top_3 == 2
    assert k8s.calls_in_results_ok == 2
    assert k8s.calls_not_in_results == 1
    assert k8s.calls_unknown_op == 1
    assert k8s.calls_without_search == 1
    vault = by_connector[_OTHER_CONNECTOR]
    assert (vault.searches, vault.calls, vault.searches_followed_by_call) == (1, 0, 0)


@pytest.mark.usefixtures("usage_on")
async def test_report_reads_one_tenant_and_one_connector(operator: Operator) -> None:
    await _seed_report_records(operator)
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        other = await compute_pick_report(
            session,
            tenant_id=_OTHER_TENANT,
            since=now - timedelta(days=1),
            until=now + timedelta(minutes=1),
        )
        narrowed = await compute_pick_report(
            session,
            tenant_id=operator.tenant_id,
            since=now - timedelta(days=1),
            until=now + timedelta(minutes=1),
            connector_id=_OTHER_CONNECTOR,
        )
    [bucket] = other.buckets
    assert (bucket.searches, bucket.calls) == (1, 0)
    assert [b.connector_id for b in narrowed.buckets] == [_OTHER_CONNECTOR]


@pytest.mark.usefixtures("usage_on")
async def test_report_route_returns_the_numbers(operator: Operator) -> None:
    await _seed_report_records(operator)
    client = TestClient(_rest_app(operator))
    response = client.get(
        "/api/v1/retrieve/operation-picks", params={"since": "1d", "connector_id": _CONNECTOR}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == str(operator.tenant_id)
    [bucket] = body["buckets"]
    assert bucket["calls_in_results_ok"] == 2
    assert bucket["calls_without_search"] == 1


def test_report_route_refuses_another_tenant(operator: Operator) -> None:
    client = TestClient(_rest_app(operator))
    response = client.get(
        "/api/v1/retrieve/operation-picks", params={"tenant_filter": str(_OTHER_TENANT)}
    )
    assert response.status_code == 403


def test_report_route_refuses_a_bad_since(operator: Operator) -> None:
    client = TestClient(_rest_app(operator))
    response = client.get("/api/v1/retrieve/operation-picks", params={"since": "tomorrow"})
    assert response.status_code == 400


def test_report_route_with_no_records_is_empty(operator: Operator) -> None:
    client = TestClient(_rest_app(operator))
    response = client.get("/api/v1/retrieve/operation-picks")
    assert response.status_code == 200
    assert response.json()["buckets"] == []
