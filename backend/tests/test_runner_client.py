# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the runner poll/report HTTP client (#2497).

Exercised against :class:`httpx.MockTransport` (the central routes land
in #2499). Asserts the request shape — path, ``runner`` /
``known_version`` query params, ``Authorization: Bearer`` header — and
the ``304``-as-unchanged and error-as-``RunnerClientError`` contracts.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from meho_backplane.auth.operator import TenantRole
from meho_backplane.runner import wire
from meho_backplane.runner.client import (
    ASSIGNMENT_UNCHANGED,
    RunnerClient,
    RunnerClientError,
    RunnerWireVersionMismatchError,
)
from meho_backplane.runner.wire import (
    RunnerAssignment,
    RunnerPrincipal,
    RunnerResult,
    RunnerResultBatch,
    RunnerWorkItem,
)

_TOKEN = "runner-bearer-token"


def _client(handler: httpx.MockTransport, runner_id: str = "runner-7") -> RunnerClient:
    return RunnerClient(
        central_url="https://central.example/",
        runner_id=runner_id,
        token=_TOKEN,
        transport=handler,
    )


def _assignment_json() -> dict[str, Any]:
    item = RunnerWorkItem(
        check_ref="c1",
        op_id="net.tcp_check",
        product="net",
        version="1.x",
        impl_id="net-probe",
        handler_ref="meho_backplane.connectors.net.ops.net_tcp_check",
        params={"host": "127.0.0.1", "port": 443},
        safety_level="safe",
        principal=RunnerPrincipal(
            sub="s", tenant_id=uuid.uuid4(), tenant_role=TenantRole.READ_ONLY
        ),
    )
    payload: dict[str, Any] = RunnerAssignment(
        assignment_version="digest-abc", items=[item]
    ).model_dump(mode="json")
    return payload


async def test_fetch_assignment_shape_and_auth() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_assignment_json())

    async with _client(httpx.MockTransport(handler)) as client:
        assignment = await client.fetch_assignment(known_version=None)

    assert isinstance(assignment, RunnerAssignment)
    assert assignment.assignment_version == "digest-abc"
    assert len(assignment.items) == 1
    (req,) = captured
    assert req.method == "GET"
    assert req.url.path == "/api/v1/checks/assignment"
    assert req.url.params["runner"] == "runner-7"
    assert "known_version" not in req.url.params
    assert req.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert req.headers[wire.WIRE_VERSION_HEADER] == wire.SCHEMA_VERSION


async def test_fetch_assignment_sends_cached_known_version() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_assignment_json())

    async with _client(httpx.MockTransport(handler)) as client:
        await client.fetch_assignment(known_version="digest-abc")

    (req,) = captured
    assert req.url.params["known_version"] == "digest-abc"


async def test_fetch_assignment_304_returns_unchanged_sentinel() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(304)

    async with _client(httpx.MockTransport(handler)) as client:
        result = await client.fetch_assignment(known_version="digest-abc")

    assert result is ASSIGNMENT_UNCHANGED


async def test_fetch_assignment_error_status_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(RunnerClientError):
            await client.fetch_assignment(known_version=None)


async def test_fetch_assignment_transport_error_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("uplink down")

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(RunnerClientError):
            await client.fetch_assignment(known_version=None)


async def test_post_results_shape_and_auth() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(202)

    batch = RunnerResultBatch(
        runner_id="runner-7",
        results=[RunnerResult(result_uid="u1", check_ref="c1", op_id="net.tcp_check", status="ok")],
    )
    async with _client(httpx.MockTransport(handler)) as client:
        await client.post_results(batch)

    (req,) = captured
    assert req.method == "POST"
    assert req.url.path == "/api/v1/checks/results"
    assert req.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert req.headers[wire.WIRE_VERSION_HEADER] == wire.SCHEMA_VERSION
    assert b'"runner_id":"runner-7"' in req.content.replace(b" ", b"")


async def test_post_results_error_status_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    batch = RunnerResultBatch(runner_id="runner-7", results=[])
    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(RunnerClientError):
            await client.post_results(batch)


async def test_client_sends_the_current_schema_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """The header carries ``SCHEMA_VERSION`` itself, so a bump reaches the wire (#5020)."""
    monkeypatch.setattr(wire, "SCHEMA_VERSION", "3")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(304)

    async with _client(httpx.MockTransport(handler)) as client:
        await client.fetch_assignment(known_version="digest-abc")

    assert captured[0].headers[wire.WIRE_VERSION_HEADER] == "3"


_MISMATCH = wire.wire_version_mismatch_message("1", "2")


async def test_wire_version_refusal_raises_mismatch_error() -> None:
    """Central's 409 version refusal on either call raises the mismatch error with its text."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": _MISMATCH})

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(RunnerWireVersionMismatchError) as fetch_exc:
            await client.fetch_assignment(known_version=None)
        with pytest.raises(RunnerWireVersionMismatchError) as post_exc:
            await client.post_results(RunnerResultBatch(runner_id="runner-7", results=[]))

    assert str(fetch_exc.value) == str(post_exc.value) == _MISMATCH
    assert "runner wire version 1" in _MISMATCH
    assert "central MEHO needs 2: update the runner" in _MISMATCH


async def test_other_conflict_is_a_plain_client_error() -> None:
    """A 409 that is not a version refusal stays a plain ``RunnerClientError``."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "gateway_command_already_consumed"})

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(RunnerClientError) as exc_info:
            await client.fetch_assignment(known_version=None)

    assert not isinstance(exc_info.value, RunnerWireVersionMismatchError)
