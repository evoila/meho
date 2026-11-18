# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The secret-hiding pass of a script or a command line runs in a worker thread.

* One very long line is hidden in time that grows in a straight line.
* While the pass runs, the app still answers other requests.
* A caller the policy refuses never starts the pass.
"""

from __future__ import annotations

import asyncio
import threading
import time
import types
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

import meho_backplane.connectors.kubernetes.ops_write_preview as k8s_preview
import meho_backplane.connectors.linux  # noqa: F401 -- registers the script preview builder
import meho_backplane.connectors.linux.ops_write as linux_write
import meho_backplane.connectors.linux.script_preview as script_preview
import meho_backplane.operations._request_preview as request_preview
from meho_backplane.db.models import PermissionVerdict
from meho_backplane.main import app
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.redaction.command_line import HIDDEN
from tests.mcp_test_fixtures import build_operator
from tests.test_operations_preview_typed_writes import (  # noqa: F401 -- fixtures
    _env_and_state,
    _preview,
    _register,
    stub_embedding_service,
)

_ONE_MIB = 1 << 20
_TIME_LIMIT_SECONDS = 2.0
_FAKE = "made-up-value-not-real-4471"


@pytest.mark.parametrize("unit", ["x", "-x"])
def test_command_line_with_one_one_mib_item_is_hidden_in_linear_time(unit: str) -> None:
    item = unit * (_ONE_MIB // len(unit))
    started = time.perf_counter()
    hidden = k8s_preview._hide_command_secrets(["sh", "-c", item, "--password", _FAKE])
    assert time.perf_counter() - started < _TIME_LIMIT_SECONDS
    assert hidden == ["sh", "-c", item, "--password", HIDDEN]


@pytest.fixture
def health_client() -> Iterator[httpx.AsyncClient]:
    async def _fake_verify() -> Any:
        return build_operator()

    app.dependency_overrides[verify_jwt_and_bind] = _fake_verify
    try:
        yield httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test")
    finally:
        app.dependency_overrides.pop(verify_jwt_and_bind, None)


async def _answers_while_blocked(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    name: str,
    client: httpx.AsyncClient,
    run_builder: Any,
) -> None:
    """Block the pass in its thread, then check the app answers a request meanwhile."""
    started = threading.Event()
    release = threading.Event()
    real = getattr(module, name)

    def blocked(*args: Any, **kwargs: Any) -> Any:
        started.set()
        release.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, blocked)
    builder = asyncio.create_task(run_builder())
    try:
        while not started.is_set():
            await asyncio.sleep(0.01)
        response = await asyncio.wait_for(client.get("/api/v1/health/live"), timeout=5)
        assert response.status_code == 200
        assert not builder.done()
    finally:
        release.set()
    assert await builder is not None


@pytest.mark.asyncio
async def test_script_preview_runs_in_a_worker_thread(
    monkeypatch: pytest.MonkeyPatch, health_client: httpx.AsyncClient
) -> None:
    ctx = types.SimpleNamespace(
        params={"script": "echo hi\n"}, target=types.SimpleNamespace(name="linux-1")
    )
    await _answers_while_blocked(
        monkeypatch,
        linux_write,
        "script_preview_fields",
        health_client,
        lambda: linux_write._linux_script_run_preview(ctx),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_job_preview_runs_in_a_worker_thread(
    monkeypatch: pytest.MonkeyPatch, health_client: httpx.AsyncClient
) -> None:
    ctx = types.SimpleNamespace(
        params={"name": "j", "namespace": "n", "spec": {}},
        operator=build_operator(),
        connector_id="kubernetes-1.x",
        descriptor=types.SimpleNamespace(op_id="k8s.job.create"),
    )
    await _answers_while_blocked(
        monkeypatch,
        k8s_preview,
        "_job_spec_echo",
        health_client,
        lambda: k8s_preview._job_create_preview(ctx),  # type: ignore[arg-type]
    )


async def _preview_script_run(
    stub: AsyncMock, monkeypatch: pytest.MonkeyPatch, verdict: PermissionVerdict
) -> tuple[dict[str, Any], list[str]]:
    await _register(
        stub,
        "linux.script.run",
        safety_level="dangerous",
        requires_approval=True,
        schema={"type": "object", "properties": {"script": {"type": "string"}}},
    )
    calls: list[str] = []
    real = script_preview.hide_shell_script

    def spy(script: str) -> str:
        calls.append(script)
        return real(script)

    monkeypatch.setattr(script_preview, "hide_shell_script", spy)

    async def gate(**_: Any) -> tuple[PermissionVerdict, str | None]:
        return verdict, "by test"

    monkeypatch.setattr(request_preview, "policy_gate", gate)
    return await _preview("linux.script.run", {"script": f"echo {_FAKE}\n"}), calls


@pytest.mark.asyncio
async def test_preview_of_a_refused_caller_never_runs_the_pass(
    stub_embedding_service: AsyncMock,  # noqa: F811 -- fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope, calls = await _preview_script_run(
        stub_embedding_service, monkeypatch, PermissionVerdict.DENY
    )
    assert envelope["status"] == "error"
    assert calls == []


@pytest.mark.asyncio
async def test_preview_of_a_caller_who_waits_for_approval_runs_the_pass(
    stub_embedding_service: AsyncMock,  # noqa: F811 -- fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope, calls = await _preview_script_run(
        stub_embedding_service, monkeypatch, PermissionVerdict.NEEDS_APPROVAL
    )
    assert envelope["status"] == "ok"
    assert len(calls) == 1
