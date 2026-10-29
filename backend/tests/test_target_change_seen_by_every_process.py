# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every process that runs an operation notices a committed target change (#3879).

The target update route drops kept logins only in the process that serves
the PATCH. A second backplane server and a satellite runner have their own
connector caches. So each of them compares the target's ``updated_at`` with
the value it saw last (:func:`forget_target_if_changed`) and drops the kept
login when the row changed. These tests pin:

* two independent caches: the process that serves the PATCH drops at once,
  and a second process that only reads the committed row logs in with the
  new credential on its next call;
* the check itself: a first sighting, an unchanged row and a target without
  ``updated_at`` drop nothing; a new ``updated_at`` drops once;
* the dispatcher runs the check before the handler;
* the gateway sends ``updated_at`` to the runner, and the runner executor
  runs the check before the handler.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.github import GitHubPATCredentials, GitHubRestConnector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.gateway.assignment_service import descriptor_from_target
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    _CONNECTOR_INSTANCE_CACHE,
    _SEEN_TARGET_VERSIONS,
    forget_target_if_changed,
)
from meho_backplane.runner.executor import execute_work_item
from meho_backplane.runner.wire import ResolvedTargetDescriptor, RunnerPrincipal, RunnerWorkItem
from meho_backplane.targets.resolver import resolve_target
from meho_backplane.targets.schemas import Target

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _admin_token,
    _build_app,
    _empty_connector_registry,  # noqa: F401
    _insert_target,
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_WIDE_REF = f"tenants/{DEFAULT_TENANT_ID}/gh-wide"
_NARROW_REF = f"tenants/{DEFAULT_TENANT_ID}/gh-narrow"
_T0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
_T1 = _T0 + timedelta(minutes=1)


@dataclass(frozen=True)
class _Target:
    updated_at: datetime | None = _T0
    name: str = "t"
    host: str = "api.github.com"
    port: int | None = None
    secret_ref: str | None = _WIDE_REF
    auth_model: str | None = "shared_service_account"
    product: str = "demo"
    fingerprint: Any = None
    preferred_impl_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID = _TENANT


class _RecordingConnector:
    """Fake cached connector that records the drops it was told to run."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def invalidate_session(self, target: Any) -> None:
        self.calls.append("invalidate_session")

    async def invalidate_credentials(self, target: Any) -> None:
        self.calls.append("invalidate_credentials")


@dataclass
class _Process:
    """The per-process caches of one backplane server or runner."""

    instances: dict[Any, Any] = field(default_factory=dict)
    versions: dict[Any, Any] = field(default_factory=dict)


@contextmanager
def _in_process(process: _Process) -> Iterator[None]:
    """Swap the module-level caches for *process*'s own, and keep its state after."""
    saved_instances, saved_versions = dict(_CONNECTOR_INSTANCE_CACHE), dict(_SEEN_TARGET_VERSIONS)
    _CONNECTOR_INSTANCE_CACHE.clear()
    _CONNECTOR_INSTANCE_CACHE.update(process.instances)
    _SEEN_TARGET_VERSIONS.clear()
    _SEEN_TARGET_VERSIONS.update(process.versions)
    try:
        yield
    finally:
        process.instances = dict(_CONNECTOR_INSTANCE_CACHE)
        process.versions = dict(_SEEN_TARGET_VERSIONS)
        _CONNECTOR_INSTANCE_CACHE.clear()
        _CONNECTOR_INSTANCE_CACHE.update(saved_instances)
        _SEEN_TARGET_VERSIONS.clear()
        _SEEN_TARGET_VERSIONS.update(saved_versions)


@pytest.fixture(autouse=True)
def _clean_caches() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


def _operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="op.test.jwt",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# forget_target_if_changed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_a_new_updated_at_drops_the_kept_login() -> None:
    recorder = _RecordingConnector()
    _CONNECTOR_INSTANCE_CACHE[type("A", (), {})] = recorder  # type: ignore[index]
    target = _Target()

    assert await forget_target_if_changed(target) is False  # first sighting
    assert await forget_target_if_changed(target) is False  # unchanged
    assert recorder.calls == []

    changed = _Target(id=target.id, updated_at=_T1)
    assert await forget_target_if_changed(changed) is True
    assert recorder.calls == ["invalidate_session", "invalidate_credentials"]

    assert await forget_target_if_changed(changed) is False  # seen now
    assert len(recorder.calls) == 2


@pytest.mark.asyncio
async def test_a_target_without_updated_at_is_never_treated_as_changed() -> None:
    recorder = _RecordingConnector()
    _CONNECTOR_INSTANCE_CACHE[type("A", (), {})] = recorder  # type: ignore[index]
    target = _Target(updated_at=None)

    assert await forget_target_if_changed(target) is False
    assert await forget_target_if_changed(target) is False
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# Two independent caches
# ---------------------------------------------------------------------------


def _patch(client: TestClient, name: str, body: dict[str, Any]) -> Any:
    key = make_rsa_keypair("kid-A")
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        return client.patch(
            f"/api/v1/targets/{name}",
            json=body,
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )


async def _read_target(name: str) -> Target:
    async with get_sessionmaker()() as session:
        return await resolve_target(session, _TENANT, name)


def _github_connector(loads: list[str]) -> GitHubRestConnector:
    tokens_by_ref = {_WIDE_REF: "pat-wide-scope", _NARROW_REF: "pat-one-repo"}

    async def _loader(target: Any, operator: Operator) -> GitHubPATCredentials:
        loads.append(target.secret_ref)
        return GitHubPATCredentials(token=tokens_by_ref[target.secret_ref])

    return GitHubRestConnector(credentials_loader=_loader)


async def _token_after_check(conn: GitHubRestConnector, name: str) -> str:
    """What a dispatch in this process does: read the row, run the check, log in."""
    target = await _read_target(name)
    await forget_target_if_changed(target)
    return (await conn.auth_headers(target, _operator()))["Authorization"]


@pytest.mark.asyncio
async def test_a_second_process_uses_the_new_credential_on_its_next_call() -> None:
    """Two processes, two caches. One serves the PATCH; the other only reads the row."""
    row = await _insert_target(name="gh", product="github", secret_ref=_WIDE_REF)
    serving, other = _Process(), _Process()
    serving_loads: list[str] = []
    other_loads: list[str] = []

    with _in_process(other):
        other_conn = _github_connector(other_loads)
        _CONNECTOR_INSTANCE_CACHE[GitHubRestConnector] = other_conn
        assert await _token_after_check(other_conn, "gh") == "Bearer pat-wide-scope"

    with _in_process(serving):
        serving_conn = _github_connector(serving_loads)
        _CONNECTOR_INSTANCE_CACHE[GitHubRestConnector] = serving_conn
        assert await _token_after_check(serving_conn, "gh") == "Bearer pat-wide-scope"
        response = _patch(TestClient(_build_app()), "gh", {"secret_ref": _NARROW_REF})
        assert response.status_code == 200
        # The serving process dropped the kept token in the route.
        assert target_cache_key(row) not in serving_conn._pat_tokens
        assert await _token_after_check(serving_conn, "gh") == "Bearer pat-one-repo"

    with _in_process(other):
        # The other process still holds the old token ...
        assert other_conn._pat_tokens[target_cache_key(row)] == "pat-wide-scope"
        # ... and drops it on its next call, because the row's updated_at changed.
        assert await _token_after_check(other_conn, "gh") == "Bearer pat-one-repo"

    assert serving_loads == [_WIDE_REF, _NARROW_REF]
    assert other_loads == [_WIDE_REF, _NARROW_REF]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class _DemoConnector(Connector):
    product = "demo"
    version = "1.x"
    impl_id = "demo"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


_ORDER: list[str] = []


async def _demo_handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    _ORDER.append("handler")
    return {"done": True}


class _OrderedRecorder(_RecordingConnector):
    async def invalidate_credentials(self, target: Any) -> None:
        _ORDER.append("drop")


@pytest.mark.asyncio
async def test_dispatcher_drops_the_kept_login_before_the_handler_runs() -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    register_connector_v2(product="demo", version="", impl_id="", cls=_DemoConnector)
    await register_typed_operation(
        product="demo",
        version="1.x",
        impl_id="demo",
        op_id="demo.thing.get",
        handler=_demo_handler,
        summary="Demo op.",
        description="Demo op used by the target change tests.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="safe",
    )
    _CONNECTOR_INSTANCE_CACHE[type("A", (), {})] = _OrderedRecorder()  # type: ignore[index]
    _ORDER.clear()
    target = _Target()

    first = await dispatch(
        operator=_operator(),
        connector_id="demo-1.x",
        op_id="demo.thing.get",
        target=target,
        params={},
    )
    assert first.status == "ok"
    assert _ORDER == ["handler"]

    changed = _Target(id=target.id, updated_at=_T1)
    second = await dispatch(
        operator=_operator(),
        connector_id="demo-1.x",
        op_id="demo.thing.get",
        target=changed,
        params={},
    )
    assert second.status == "ok"
    assert _ORDER == ["handler", "drop", "handler"]


# ---------------------------------------------------------------------------
# Satellite runner
# ---------------------------------------------------------------------------


def test_the_gateway_sends_updated_at_to_the_runner() -> None:
    row = TargetORM(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        name="vc-a",
        aliases=[],
        product="vmware",
        host="vc-a.example",
        auth_model="shared_service_account",
        verify_tls=True,
        extras={},
        updated_at=_T1,
    )
    assert descriptor_from_target(row).updated_at == _T1


def _runner_item(descriptor: ResolvedTargetDescriptor, port: int) -> RunnerWorkItem:
    return RunnerWorkItem(
        check_ref="chk-1",
        op_id="net.tcp_check",
        product="net",
        version="1.x",
        impl_id="net-probe",
        handler_ref="meho_backplane.connectors.net.ops.net_tcp_check",
        params={"host": "127.0.0.1", "port": port},
        safety_level="safe",
        principal=RunnerPrincipal(
            sub="runner-svc", tenant_id=_TENANT, tenant_role=TenantRole.READ_ONLY
        ),
        target_descriptor=descriptor,
    )


@pytest.mark.asyncio
async def test_the_runner_drops_the_kept_login_when_the_descriptor_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEHO_NETDIAG_PROBE_ALLOWLIST", "127.0.0.1")
    recorder = _RecordingConnector()
    _CONNECTOR_INSTANCE_CACHE[type("A", (), {})] = recorder  # type: ignore[index]
    target_id = uuid.uuid4()

    def _descriptor(updated_at: datetime) -> ResolvedTargetDescriptor:
        return ResolvedTargetDescriptor(
            id=target_id,
            tenant_id=_TENANT,
            name="srv",
            product="net",
            host="127.0.0.1",
            updated_at=updated_at,
        )

    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        first = await execute_work_item(_runner_item(_descriptor(_T0), port))
        assert first.status == "ok"
        assert recorder.calls == []
        second = await execute_work_item(_runner_item(_descriptor(_T1), port))
        assert second.status == "ok"
        assert recorder.calls == ["invalidate_session", "invalidate_credentials"]
    finally:
        server.close()
        await server.wait_closed()
