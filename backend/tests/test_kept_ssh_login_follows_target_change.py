# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A kept SSH login follows a change of its target, also across paths.

A topology refresh takes the cached connector and logs in to the target.
These tests pin what the next dispatch in the same process does:

* when the target's host and secret reference changed since the refresh
  (a newer change version), the dispatch dials the new host with the new
  secret;
* with no change, the dispatch reuses the login the refresh keeps.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters.ssh import SshConnector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import TopologyHints
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.topology import refresh as refresh_module
from meho_backplane.topology.refresh import refresh_target_topology

from ._oidc_jwt_helpers import DEFAULT_TENANT_ID
from ._targets_helpers import _empty_connector_registry, _settings_env  # noqa: F401

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
_T1 = _T0 + timedelta(minutes=5)
_OLD_REF = f"tenants/{DEFAULT_TENANT_ID}/ssh-old"
_NEW_REF = f"tenants/{DEFAULT_TENANT_ID}/ssh-new"
_SECRET_BY_REF = {_OLD_REF: "old-login-value", _NEW_REF: "new-login-value"}


@dataclass(frozen=True)
class _Target:
    connection_changed_at: datetime = _T0
    updated_at: datetime = _T0
    name: str = "ssh-a"
    host: str = "old-host.test.invalid"
    port: int | None = 22
    secret_ref: str = _OLD_REF
    auth_model: str | None = "shared_service_account"
    product: str = "sshfollow"
    version: str | None = None
    fingerprint: Any = None
    preferred_impl_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID = _TENANT


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FollowSsh(SshConnector):
    """An SSH connector whose topology discovery and op both log in."""

    product = "sshfollow"
    version = "1.x"
    impl_id = "sshfollow"
    fingerprint = probe = execute = None  # type: ignore[assignment]

    async def _auth_config(self, target: Any, operator: Any = None) -> dict[str, Any]:
        return {
            "username": "u",
            "password": _SECRET_BY_REF[target.secret_ref],
            "known_hosts": None,
        }

    async def discover_topology(self, target: Any, *, operator: Any = None) -> TopologyHints:  # type: ignore[override]
        await self._connect(target, operator)
        return TopologyHints(discovered_at=datetime.now(UTC))


async def _login_handler(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    connector = get_or_create_connector_instance(_FollowSsh)
    assert isinstance(connector, _FollowSsh)
    await connector._connect(target, operator)
    return {"done": True}


def _operator() -> Operator:
    return Operator(
        sub="sshfollow-operator",
        name=None,
        email=None,
        raw_jwt="op.test.jwt",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.fixture(autouse=True)
def _clean_caches() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
def dials(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record every SSH dial as ``(host, login value)``; no network."""
    seen: list[tuple[str, str]] = []

    async def _dial(target: Any, auth_kwargs: dict[str, Any]) -> _FakeConnection:
        seen.append((target.host, auth_kwargs["password"]))
        return _FakeConnection()

    monkeypatch.setattr("meho_backplane.connectors.adapters.ssh._dial", _dial)
    # The graph write and its audit are not under test here.
    monkeypatch.setattr(refresh_module, "_apply_reconcile", AsyncMock())
    return seen


async def _register() -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    register_connector_v2(product="sshfollow", version="", impl_id="", cls=_FollowSsh)
    await register_typed_operation(
        product="sshfollow",
        version="1.x",
        impl_id="sshfollow",
        op_id="sshfollow.login.check",
        handler=_login_handler,
        summary="Log in to the target.",
        description="Test op that logs in to the target over SSH.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="safe",
    )


async def _dispatch(target: _Target) -> str:
    result = await dispatch(
        operator=_operator(),
        connector_id="sshfollow-1.x",
        op_id="sshfollow.login.check",
        target=target,
        params={},
    )
    assert result.status == "ok", result
    return result.status


@pytest.mark.asyncio
async def test_a_dispatch_after_a_refresh_dials_the_changed_host_with_the_new_secret(
    dials: list[tuple[str, str]],
) -> None:
    await _register()
    target = _Target()
    with patch.object(refresh_module, "_has_populator", return_value=True):
        await refresh_target_topology(target, _operator())
    assert dials == [("old-host.test.invalid", "old-login-value")]

    changed = replace(
        target,
        host="new-host.test.invalid",
        secret_ref=_NEW_REF,
        connection_changed_at=_T1,
        updated_at=_T1,
    )
    assert await _dispatch(changed) == "ok"

    assert dials == [
        ("old-host.test.invalid", "old-login-value"),
        ("new-host.test.invalid", "new-login-value"),
    ]


@pytest.mark.asyncio
async def test_a_dispatch_after_a_refresh_reuses_the_login_when_nothing_changed(
    dials: list[tuple[str, str]],
) -> None:
    await _register()
    target = _Target()
    with patch.object(refresh_module, "_has_populator", return_value=True):
        await refresh_target_topology(target, _operator())

    assert await _dispatch(target) == "ok"
    assert await _dispatch(target) == "ok"

    assert dials == [("old-host.test.invalid", "old-login-value")]
