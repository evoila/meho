# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The typed writes of Argo CD, Proxmox and Harbor follow the change rule.

A change is sent at most twice, and a second time only after a 307 or 308 to
the same path (apart from one slash at the end or letter case). Every other
redirect ends the call. Each request that was sent gets its own trace entry.
Names and addresses are made up; every answer comes from ``respx``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.argocd import ArgoCdConnector
from meho_backplane.connectors.harbor import HarborConnector
from meho_backplane.connectors.harbor import connector as harbor_module
from meho_backplane.connectors.proxmox import ProxmoxConnector
from tests.test_connectors_http_change_redirect import _real_capture


@dataclass
class _Target:
    name: str = "typed-write-target"
    host: str = "writes.example.test"
    port: int | None = 443
    secret_ref: str = "targets/typed-write-target"
    id: UUID = field(default_factory=uuid.uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


_BASE = "https://writes.example.test"
_OPERATOR = Operator(
    sub="op-typed-writes",
    name=None,
    email=None,
    raw_jwt="made-up.jwt",
    tenant_id=UUID(int=0),
    tenant_role=TenantRole.OPERATOR,
)


async def _bearer(*_args: Any, **_kwargs: Any) -> dict[str, str]:
    return {"Authorization": "Bearer made-up-token"}


async def _argocd_write(conn: Any, method: str, path: str) -> Any:
    return await conn._write_json(_Target(), method, path, operator=_OPERATOR, json={"n": 1})


async def _proxmox_write(conn: Any, method: str, path: str) -> Any:
    return await conn._write_json(_Target(), method, path, operator=_OPERATOR, data={"n": "1"})


async def _harbor_delete(conn: Any, method: str, path: str) -> Any:
    del method, path
    return await conn.robot_delete(_OPERATOR, _Target(), {"id": 7, "project": "team-ci"})


def _argocd() -> Any:
    conn = ArgoCdConnector()
    conn.auth_headers = _bearer  # type: ignore[method-assign]
    return conn


def _proxmox() -> Any:
    conn = ProxmoxConnector()
    conn.auth_headers = _bearer  # type: ignore[method-assign]

    async def token_creds(*_args: Any) -> Any:
        return SimpleNamespace(mode="token")

    conn._resolve_creds = token_creds  # type: ignore[method-assign]
    return conn


def _harbor() -> Any:
    conn = HarborConnector()
    conn.auth_headers = _bearer  # type: ignore[method-assign]
    return conn


_HARBOR_PATH = "/api/v2.0/robots/7"

#: (name, connector factory, write call, method, path of the write)
_WRITES = [
    ("argocd", _argocd, _argocd_write, "POST", "/api/v1/applications/demo/sync"),
    ("proxmox", _proxmox, _proxmox_write, "PUT", "/api2/json/nodes/pve1/qemu/100/config"),
    ("harbor", _harbor, _harbor_delete, "DELETE", _HARBOR_PATH),
]


@pytest.fixture(autouse=True)
def _robot_in_project(monkeypatch: pytest.MonkeyPatch) -> None:
    # The project check of the Harbor robot is tested on its own; here it passes.
    monkeypatch.setattr(harbor_module, "check_robot_in_project", lambda *_a, **_k: None)


def _mock_harbor_read(mock: respx.MockRouter) -> None:
    mock.get(_HARBOR_PATH).respond(200, json={"id": 7, "level": "project"})


async def _status_of(call: Any) -> int:
    try:
        await call
    except httpx.HTTPStatusError as refused:
        return refused.response.status_code
    return 200


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 303])
@pytest.mark.parametrize(("name", "factory", "write", "method", "path"), _WRITES)
async def test_a_302_or_303_to_another_path_is_not_followed(
    name: str, factory: Any, write: Any, method: str, path: str, status: int
) -> None:
    conn = factory()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_harbor_read(mock)
        sent = mock.route(method=method, path=path).respond(
            status, headers={"Location": "/elsewhere/42"}
        )
        elsewhere = mock.route(path="/elsewhere/42").respond(200, json={"ok": True})
        answered = await _status_of(write(conn, method, path))
    await conn.aclose()
    assert answered == status
    assert (sent.call_count, elsewhere.call_count) == (1, 0), name


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "factory", "write", "method", "path"), _WRITES)
async def test_a_307_to_another_path_sends_nothing_more(
    name: str, factory: Any, write: Any, method: str, path: str
) -> None:
    conn = factory()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        _mock_harbor_read(mock)
        sent = mock.route(method=method, path=path).respond(
            307, headers={"Location": "/api/v2.0/robots/8"}
        )
        other = mock.route(path="/api/v2.0/robots/8").respond(200, json={"ok": True})
        answered = await _status_of(write(conn, method, path))
    await conn.aclose()
    assert answered == 307
    assert (sent.call_count, other.call_count) == (1, 0), name


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize(("name", "factory", "write", "method", "path"), _WRITES)
async def test_a_307_or_308_to_the_same_path_with_a_slash_sends_the_write_once_more(
    name: str, factory: Any, write: Any, method: str, path: str, status: int
) -> None:
    conn = factory()
    with _real_capture(f"{name}.write") as spans:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_harbor_read(mock)
            first = mock.route(method=method, path=path).respond(
                status, headers={"Location": f"{path}/"}
            )
            again = mock.route(method=method, path=f"{path}/").respond(200, json={})
            answered = await _status_of(write(conn, method, path))
    await conn.aclose()
    assert answered == 200
    assert (first.call_count, again.call_count) == (1, 1), name
    # One trace entry for each request that was sent: the write and its repeat
    # (the Harbor robot read before the delete has its own entry).
    writes = [span for span in spans if span.attributes["method"] == method]
    assert [span.status for span in writes] == [str(status), "200"]
    assert [span.attributes["url"] for span in writes] == [f"{_BASE}{path}", f"{_BASE}{path}/"]
