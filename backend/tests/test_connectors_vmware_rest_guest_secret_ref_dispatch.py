# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Privacy proof for ``guest_secret_ref`` through the production dispatch path (#3364).

:mod:`tests.test_connectors_vmware_rest_guest_ops` calls the guest handlers
directly. This module drives one safe read (``process.list``) and one
approval-gated write (``file.write``) through
:func:`~meho_backplane.operations.dispatch` with a ``guest_secret_ref`` and
captures the surfaces the issue names:

* the durable audit row (every column, including the raw payload and the
  redaction manifest);
* everything sent to the broadcast store (a recording stand-in for the
  Valkey client), plus the broadcast-safe payload view built from the
  audit row;
* every log event emitted during the dispatch: the structlog events the
  backplane writes and the stdlib records, at DEBUG level.

The resolved guest login (user name and password) appears on none of them.
The only trace of the override is the reference string in the params.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import redis.exceptions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import client as broadcast_client
from meho_backplane.broadcast.events import classify_op, redact_payload
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import (
    _guest,
    _guest_secret_ref,
    register_vmware_composite_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000003364")
_PROCESS_LIST = "vmware.composite.vm.guest.process.list"
_FILE_WRITE = "vmware.composite.vm.guest.file.write"

_OVERRIDE_REF = "targets/app-vm-01"
_OVERRIDE_USER = "guest-user-only-in-the-store-7c1d"
_OVERRIDE_PASSWORD = "guest-pw-only-in-the-store-9f2a"
_TARGET_USER = "target-user-only-in-the-store-41b0"
_TARGET_PASSWORD = "target-pw-only-in-the-store-e55c"
_SENTINELS = (_OVERRIDE_USER, _OVERRIDE_PASSWORD, _TARGET_USER, _TARGET_PASSWORD)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


def _operator() -> Operator:
    return Operator(
        sub="human-guest-secret-ref",
        name="Guest Secret Ref Dispatch",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


class _Fingerprint:
    version = "9.0"


@dataclass
class _Target:
    """The target the dispatcher resolves the connector from and the handler reads."""

    product: str = "vmware"
    fingerprint: Any = field(default_factory=_Fingerprint)
    preferred_impl_id: str | None = "vmware-rest"
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID = _TENANT_ID
    name: str = "vc-test"
    host: str = "vc.example.test"
    port: int = 443
    auth_model: str = "shared_service_account"
    secret_ref: str = "targets/vc"
    verify_tls: bool = False
    extras: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Doubles: connector, secret stores, broadcast store
# ---------------------------------------------------------------------------


def _manager_props(prop: str, mo_type: str, moid: str) -> dict[str, Any]:
    return {
        "objects": [
            {
                "obj": {"type": "GuestOperationsManager", "value": "guestOperationsManager"},
                "propSet": [
                    {
                        "name": prop,
                        "val": {
                            "_typeName": "ManagedObjectReference",
                            "type": mo_type,
                            "value": moid,
                        },
                    }
                ],
            }
        ]
    }


class _FakeTransferClient:
    def __init__(self, put_calls: list[dict[str, Any]]) -> None:
        self._put_calls = put_calls

    async def put(self, url: str, *, content: bytes | None = None) -> httpx.Response:
        self._put_calls.append({"url": url, "content": content})
        return httpx.Response(200, request=httpx.Request("PUT", url))


@dataclass
class _RecordingConnector:
    """Serves canned VI-JSON answers and records every guest call."""

    vmomi: dict[str, Any] = field(default_factory=dict)
    vmomi_calls: list[dict[str, Any]] = field(default_factory=list)
    put_calls: list[dict[str, Any]] = field(default_factory=list)

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Operator, json: Any = None
    ) -> Any:
        self.vmomi_calls.append({"path": path, "json": json})
        if path.endswith("/RetrievePropertiesEx"):
            spec_type = json["specSet"][0]["propSet"][0]["type"]
            return self.vmomi[spec_type]
        return self.vmomi[path]

    async def _http_client(self, target: Any) -> _FakeTransferClient:
        return _FakeTransferClient(self.put_calls)


class _OverrideStore:
    """Stand-in for ``load_param_secret_data`` -- records the ref it was handed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []

    async def __call__(self, target: Any, operator: Operator, *, param: str) -> dict[str, Any]:
        self.calls.append((target.secret_ref, param))
        return {"guest_username": _OVERRIDE_USER, "guest_password": _OVERRIDE_PASSWORD}


class _TargetStore:
    """Stand-in for ``load_basic_credentials`` -- must stay unused with an override."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def __call__(
        self, target: Any, operator: Operator, *, fields: tuple[str, ...]
    ) -> dict[str, str]:
        self.calls.append(fields)
        return {"guest_username": _TARGET_USER, "guest_password": _TARGET_PASSWORD}


class _FakePipeline:
    def __init__(self, commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]]) -> None:
        self._commands = commands

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        def _queue(*args: Any, **kwargs: Any) -> _FakePipeline:
            self._commands.append((f"pipeline.{name}", args, kwargs))
            return self

        return _queue

    async def execute(self) -> Any:
        raise redis.exceptions.ConnectionError("recording broadcast store: no server")


class _RecordingBroadcastStore:
    """Stands in for the Valkey broadcast client and records every command.

    Reads answer like an unreachable store (the state every other dispatch
    test runs under); a write (``XADD``) is accepted and recorded so a
    published event would be captured here.
    """

    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def pipeline(self, *args: Any, **kwargs: Any) -> _FakePipeline:
        self.commands.append(("pipeline", args, kwargs))
        return _FakePipeline(self.commands)

    def __getattr__(self, name: str) -> Any:
        async def _command(*args: Any, **kwargs: Any) -> Any:
            self.commands.append((name, args, kwargs))
            if name == "xadd":
                return "0-1"
            raise redis.exceptions.ConnectionError("recording broadcast store: no server")

        return _command


@pytest.fixture
def broadcast_store(monkeypatch: pytest.MonkeyPatch) -> _RecordingBroadcastStore:
    store = _RecordingBroadcastStore()
    monkeypatch.setattr(broadcast_client, "_CLIENT", store)
    return store


@pytest.fixture
def override_store(monkeypatch: pytest.MonkeyPatch) -> _OverrideStore:
    store = _OverrideStore()
    monkeypatch.setattr(_guest_secret_ref, "load_param_secret_data", store)
    return store


@pytest.fixture
def target_store(monkeypatch: pytest.MonkeyPatch) -> _TargetStore:
    store = _TargetStore()
    monkeypatch.setattr(_guest, "load_basic_credentials", store)
    return store


async def _bootstrap(connector: _RecordingConnector, embedding: AsyncMock) -> None:
    register_connector_v2(
        product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
    )
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = connector  # type: ignore[assignment]
    await register_vmware_composite_operations(embedding_service=embedding)


async def _audit_rows(op_id: str) -> list[dict[str, Any]]:
    """Every column of every audit row this tenant wrote for *op_id*."""
    async with get_sessionmaker()() as fresh:
        rows = (
            (
                await fresh.execute(
                    select(AuditLog).where(AuditLog.tenant_id == _TENANT_ID, AuditLog.path == op_id)
                )
            )
            .scalars()
            .all()
        )
    return [{c.name: getattr(row, c.name) for c in AuditLog.__table__.columns} for row in rows]


def _assert_no_credential_value(label: str, surface: Any) -> None:
    text = json.dumps(surface, default=str)
    for sentinel in _SENTINELS:
        assert sentinel not in text, f"{label} carries a guest credential value"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_list_with_guest_secret_ref_keeps_the_value_off_every_surface(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    broadcast_store: _RecordingBroadcastStore,
    override_store: _OverrideStore,
    target_store: _TargetStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A safe read through ``dispatch``: audit row, broadcast store, logs stay clean."""
    procs = [{"name": "psql", "pid": 41, "owner": "svc", "cmdLine": "/usr/bin/psql"}]
    connector = _RecordingConnector(
        vmomi={
            "GuestOperationsManager": _manager_props(
                "processManager", "GuestProcessManager", "pm-1"
            ),
            "/GuestProcessManager/pm-1/ListProcessesInGuest": procs,
        }
    )
    await _bootstrap(connector, stub_embedding_service)
    params = {"vm": "vm-42", "guest_secret_ref": _OVERRIDE_REF}

    caplog.set_level(logging.DEBUG)
    with capture_logs() as events:
        result = await dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_PROCESS_LIST,
            target=_Target(),
            params=params,
        )

    assert result.status == "ok", result.error
    # The override was read under the param name; the target secret was not touched.
    assert override_store.calls == [(_OVERRIDE_REF, "guest_secret_ref")]
    assert target_store.calls == []
    # The login reached the guest -- that is the only place the value may be.
    listing = next(c for c in connector.vmomi_calls if c["path"].endswith("/ListProcessesInGuest"))
    assert listing["json"]["auth"]["username"] == _OVERRIDE_USER
    assert listing["json"]["auth"]["password"] == _OVERRIDE_PASSWORD

    rows = await _audit_rows(_PROCESS_LIST)
    assert len(rows) == 1
    assert rows[0]["payload"]["result_status"] == "ok"
    _assert_no_credential_value("the audit row", rows)
    # No operation event is published since #3758; nothing may be written either way.
    assert not [c for c in broadcast_store.commands if c[0].endswith("xadd")]
    _assert_no_credential_value("the broadcast store commands", broadcast_store.commands)
    broadcast_view = redact_payload(classify_op(_PROCESS_LIST), params, result.status)
    _assert_no_credential_value("the broadcast payload view", broadcast_view)
    _assert_no_credential_value("the log events", events)
    _assert_no_credential_value("the log records", [r.getMessage() for r in caplog.records])
    _assert_no_credential_value("the params", params)
    _assert_no_credential_value("the result", result.model_dump(mode="json"))
    assert params["guest_secret_ref"] == _OVERRIDE_REF


@pytest.mark.asyncio
async def test_file_write_with_guest_secret_ref_keeps_the_value_off_every_surface(
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    broadcast_store: _RecordingBroadcastStore,
    override_store: _OverrideStore,
    target_store: _TargetStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The approval-gated write, resumed as approved: same surfaces, same proof."""
    connector = _RecordingConnector(
        vmomi={
            "GuestOperationsManager": _manager_props("fileManager", "GuestFileManager", "fm-1"),
            "/GuestFileManager/fm-1/InitiateFileTransferToGuest": (
                "https://*/guestFile?id=9&token=xyz"
            ),
        }
    )
    await _bootstrap(connector, stub_embedding_service)
    params = {
        "vm": "vm-42",
        "guest_path": "/etc/mtu.conf",
        "content": "MTU=1400\n",
        "guest_secret_ref": _OVERRIDE_REF,
    }

    caplog.set_level(logging.DEBUG)
    with capture_logs() as events:
        result = await dispatch(
            operator=_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_FILE_WRITE,
            target=_Target(),
            params=params,
            _approved=True,
        )

    assert result.status == "ok", result.error
    assert override_store.calls == [(_OVERRIDE_REF, "guest_secret_ref")]
    assert target_store.calls == []
    assert len(connector.put_calls) == 1

    rows = await _audit_rows(_FILE_WRITE)
    assert rows, "the write must leave an audit row"
    _assert_no_credential_value("the audit rows", rows)
    # No operation event is published since #3758; nothing may be written either way.
    assert not [c for c in broadcast_store.commands if c[0].endswith("xadd")]
    _assert_no_credential_value("the broadcast store commands", broadcast_store.commands)
    broadcast_view = redact_payload(classify_op(_FILE_WRITE), params, result.status)
    _assert_no_credential_value("the broadcast payload view", broadcast_view)
    _assert_no_credential_value("the log events", events)
    _assert_no_credential_value("the log records", [r.getMessage() for r in caplog.records])
    _assert_no_credential_value("the result", result.model_dump(mode="json"))
