# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A probe of a target does not make MEHO drop its kept sign-ins.

Every process compares a change version of the target with the one it saw
last (:func:`forget_target_if_changed`). The version is the time a person
last changed the target (``connection_changed_at``) and the connector that
serves it. A probe moves ``updated_at`` but not that time. These tests pin:

* a probe that stores the same result, or only a new probe time, drops
  nothing;
* a probe that finds a new version drops the kept sign-ins only when
  another connector serves the target now;
* a target update with a new secret reference, seen by a second process,
  drops the kept sign-ins;
* a target update that sends the same values again drops them too;
* an update of a field that is no part of the connection (``notes``) does
  not move the time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    _CONNECTOR_INSTANCE_CACHE,
    _SEEN_TARGET_VERSIONS,
    forget_target_if_changed,
)

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
_T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
_PROBE_TIME = _T0 + timedelta(minutes=5)


class _OldConnector(Connector):
    product = "probe-demo"
    version = "1.x"
    impl_id = "probe-demo-old"
    supported_version_range = ">=1.0,<2.0"
    strict_version_range = True

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


class _ProbedConnector(_OldConnector):
    product = "probe-route"
    impl_id = "probe-route"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        return FingerprintResult(
            vendor="demo",
            product="probe-route",
            version="1.0",
            reachable=True,
            probed_at=datetime.now(UTC),
            probe_method="test",
        )


class _NewConnector(_OldConnector):
    version = "2.x"
    impl_id = "probe-demo-new"
    supported_version_range = ">=2.0,<3.0"


@dataclass(frozen=True)
class _Target:
    """What the check reads from a target row."""

    version: str | None = "1.0"
    connection_changed_at: datetime | None = _T0
    updated_at: datetime | None = _T0
    name: str = "t"
    host: str = "demo.example"
    secret_ref: str | None = f"tenants/{DEFAULT_TENANT_ID}/demo"
    product: str = "probe-demo"
    fingerprint: Any = None
    preferred_impl_id: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID = _TENANT


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def invalidate_session(self, target: Any) -> None:
        self.calls.append("invalidate_session")

    async def invalidate_credentials(self, target: Any) -> None:
        self.calls.append("invalidate_credentials")


@pytest.fixture(autouse=True)
def _clean_caches() -> Any:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


def _recorder() -> _Recorder:
    # Registered here, not in a fixture: the registry fixture of the target
    # helpers empties the registry before each test body.
    register_connector_v2(product="probe-demo", version="1.x", impl_id="", cls=_OldConnector)
    register_connector_v2(product="probe-demo", version="2.x", impl_id="", cls=_NewConnector)
    recorder = _Recorder()
    _CONNECTOR_INSTANCE_CACHE[type("A", (), {})] = recorder  # type: ignore[index]
    return recorder


@pytest.mark.asyncio
async def test_a_probe_with_the_same_result_drops_nothing() -> None:
    recorder = _recorder()
    before = _Target()
    assert await forget_target_if_changed(before) is False  # first sighting

    # A probe: a new ``updated_at`` and a new probe time, the same product and version.
    after = replace(
        before,
        updated_at=_PROBE_TIME,
        fingerprint={"version": "1.0", "probed_at": _PROBE_TIME.isoformat(), "reachable": True},
    )
    assert await forget_target_if_changed(after) is False
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_a_probe_that_finds_the_target_not_reachable_drops_nothing() -> None:
    recorder = _recorder()
    before = _Target()
    await forget_target_if_changed(before)

    after = replace(
        before,
        updated_at=_PROBE_TIME,
        fingerprint={"version": None, "reachable": False, "probed_at": _PROBE_TIME.isoformat()},
    )
    assert await forget_target_if_changed(after) is False
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_a_probe_that_moves_the_target_to_another_connector_drops() -> None:
    recorder = _recorder()
    before = _Target()
    await forget_target_if_changed(before)

    after = replace(before, updated_at=_PROBE_TIME, version="2.0")
    assert await forget_target_if_changed(after) is True
    assert recorder.calls == ["invalidate_session", "invalidate_credentials"]


@pytest.mark.asyncio
async def test_a_probe_that_finds_a_new_version_on_the_same_connector_drops_nothing() -> None:
    recorder = _recorder()
    before = _Target()
    await forget_target_if_changed(before)

    after = replace(before, updated_at=_PROBE_TIME, version="1.1")
    assert await forget_target_if_changed(after) is False
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_a_target_without_any_change_time_is_never_treated_as_changed() -> None:
    recorder = _recorder()
    target = _Target(connection_changed_at=None, updated_at=None)
    assert await forget_target_if_changed(target) is False
    assert await forget_target_if_changed(target) is False
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_a_row_without_connection_time_falls_back_to_updated_at() -> None:
    """An older gateway sends no ``connection_changed_at``."""
    recorder = _recorder()
    before = _Target(connection_changed_at=None)
    await forget_target_if_changed(before)
    after = replace(before, updated_at=_PROBE_TIME)
    assert await forget_target_if_changed(after) is True
    assert recorder.calls == ["invalidate_session", "invalidate_credentials"]


_KEY = make_rsa_keypair("kid-A")


def _patch(client: TestClient, name: str, body: dict[str, Any]) -> Any:
    import respx

    key = _KEY
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        return client.patch(
            f"/api/v1/targets/{name}",
            json=body,
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )


async def _row(row_id: uuid.UUID) -> TargetORM:
    async with get_sessionmaker()() as session:
        row = await session.get(TargetORM, row_id)
        assert row is not None
        session.expunge(row)
        return row


@pytest.mark.asyncio
async def test_an_update_is_seen_by_a_second_process_also_with_the_same_values() -> None:
    inserted = await _insert_target(name="demo-a", product="github", secret_ref="tenants/x/a")
    first_seen = await _row(inserted.id)
    key = target_cache_key(first_seen)

    # The second process saw the row before the updates.
    recorder = _recorder()
    assert await forget_target_if_changed(first_seen) is False
    other_versions = dict(_SEEN_TARGET_VERSIONS)

    # A new secret reference.
    new_ref = f"tenants/{DEFAULT_TENANT_ID}/other"
    assert _patch(TestClient(_build_app()), "demo-a", {"secret_ref": new_ref}).status_code == 200
    _SEEN_TARGET_VERSIONS.clear()
    _SEEN_TARGET_VERSIONS.update(other_versions)  # the second process's own store
    recorder.calls.clear()
    assert await forget_target_if_changed(await _row(inserted.id)) is True
    assert recorder.calls == ["invalidate_session", "invalidate_credentials"]
    other_versions = dict(_SEEN_TARGET_VERSIONS)
    assert key in other_versions

    # The same values again.
    assert _patch(TestClient(_build_app()), "demo-a", {"secret_ref": new_ref}).status_code == 200
    _SEEN_TARGET_VERSIONS.clear()
    _SEEN_TARGET_VERSIONS.update(other_versions)
    recorder.calls.clear()
    assert await forget_target_if_changed(await _row(inserted.id)) is True
    assert recorder.calls == ["invalidate_session", "invalidate_credentials"]


@pytest.mark.asyncio
async def test_an_update_of_notes_does_not_move_the_connection_time() -> None:
    inserted = await _insert_target(name="demo-b", product="github", secret_ref="tenants/x/b")
    before = await _row(inserted.id)
    assert _patch(TestClient(_build_app()), "demo-b", {"notes": "hello"}).status_code == 200
    after = await _row(inserted.id)
    assert after.connection_changed_at == before.connection_changed_at
    assert after.updated_at != before.updated_at


@pytest.mark.asyncio
async def test_a_probe_moves_updated_at_but_not_the_connection_time() -> None:
    register_connector_v2(product="probe-route", version="", impl_id="", cls=_ProbedConnector)
    inserted = await _insert_target(name="demo-c", product="probe-route", host="demo.example")
    before = await _row(inserted.id)
    key = _KEY
    import respx

    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        response = TestClient(_build_app()).post(
            "/api/v1/targets/demo-c/probe",
            headers={"Authorization": f"Bearer {_admin_token(key)}"},
        )
    assert response.status_code == 200
    after = await _row(inserted.id)
    assert after.fingerprint is not None
    assert after.updated_at != before.updated_at
    assert after.connection_changed_at == before.connection_changed_at


@pytest.mark.asyncio
async def test_an_update_of_secret_field_map_moves_the_connection_time() -> None:
    inserted = await _insert_target(name="demo-d", product="github", secret_ref="tenants/x/d")
    before = await _row(inserted.id)
    body = {"secret_field_map": {"password": "token"}}
    assert _patch(TestClient(_build_app()), "demo-d", body).status_code == 200
    after = await _row(inserted.id)
    assert after.connection_changed_at != before.connection_changed_at
