# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``postgres.settings`` hides the value of a connection-text setting.

The stand-in server answers with a setting from ``CONNECTION_TEXT_SETTINGS``
next to a plain one. Through ``dispatch``, for a person, an agent login and
a service login, the fixed hide mark is in the result in place of the value.
The value is in none of the result, the flight recorder trace, the event
outbox (the activity feed) and the audit row. The trace of a typed read
holds its span names, and a dispatch adds no event to the feed, so the mark
is checked in the result. The plain setting keeps its value.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.postgres import PostgresConnector
from meho_backplane.connectors.postgres import connector as connector_module
from meho_backplane.connectors.postgres.queries import fetch_settings
from meho_backplane.connectors.postgres.query_guard import CONNECTION_TEXT_SETTINGS
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, DispatchTrace, DispatchTraceSpan, EventOutbox, Tenant
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "postgres-wire-16"
_OP_ID = "postgres.settings"
_SETTING = sorted(CONNECTION_TEXT_SETTINGS)[0]

#: A made-up value. It must never show up in any output.
_CONNECTION_TEXT = "made-up-connection-text-0815-abcdef"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    yield
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()


@pytest.fixture(autouse=True)
def _registry() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product="postgres", version="16", impl_id="postgres-wire", cls=PostgresConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
async def tenant_id(monkeypatch: pytest.MonkeyPatch) -> UUID:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    await PostgresConnector.register_operations()
    tenant = uuid.uuid4()
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            Tenant(
                id=tenant,
                slug=f"pg-settings-{tenant.hex[:8]}",
                name="Postgres settings",
                flight_recorder_enabled=True,
            )
        )
    return tenant


class _Target:
    def __init__(self, tenant_id: UUID) -> None:
        self.product = "postgres"
        self.fingerprint = type("_FP", (), {"version": "16"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id = tenant_id
        self.name = "pg-settings"
        self.host = "pg.example.test"
        self.port = 5432
        self.secret_ref = None
        self.auth_model = None
        self.verify_tls = True
        self.tls_ca_pin = None
        self.tls_server_name = None
        self.extras: dict[str, Any] = {}


def _setting_row(name: str, value: str) -> dict[str, Any]:
    return {
        "name": name,
        "setting": value,
        "unit": None,
        "category": "Replication / Standby Servers",
        "short_desc": "A setting of the stand-in server.",
        "context": "sighup",
        "vartype": "string",
        "source": "configuration file",
        "boot_val": value,
        "reset_val": value,
        "pending_restart": False,
    }


class _Conn:
    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [
            _setting_row(_SETTING, _CONNECTION_TEXT),
            _setting_row("work_mem", "4096"),
        ]

    async def close(self) -> None:
        return None


def _login(kind: PrincipalKind, tenant_id: UUID) -> Operator:
    return Operator(
        sub=f"pg-settings-{kind.value}",
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _rows(model: Any) -> list[Any]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(model))).scalars().all())


def _row_text(row: Any) -> str:
    return repr({c.key: getattr(row, c.key) for c in row.__table__.columns})


@pytest.mark.parametrize(
    "kind",
    [PrincipalKind.USER, PrincipalKind.AGENT, PrincipalKind.SERVICE],
    ids=["person", "agent", "service"],
)
@pytest.mark.asyncio
async def test_settings_put_the_hide_mark_in_place_of_a_connection_text(
    monkeypatch: pytest.MonkeyPatch, tenant_id: UUID, kind: PrincipalKind
) -> None:
    monkeypatch.setattr(connector_module, "connect_read_only", AsyncMock(return_value=_Conn()))
    result = await dispatch(
        operator=_login(kind, tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_Target(tenant_id),
        params={"names": [_SETTING, "work_mem"]},
    )
    assert result.status == "ok", result.error
    assert _CONNECTION_TEXT not in repr(result.model_dump())
    rows = {row["name"]: row for row in (result.result or {})["settings"]}
    hidden = rows[_SETTING]
    assert hidden["setting"] == hidden["boot_val"] == hidden["reset_val"] == SECRET_FIELD_MARKER
    assert rows["work_mem"]["setting"] == "4096"

    traces = await _rows(DispatchTrace) + await _rows(DispatchTraceSpan)
    audit = await _rows(AuditLog)
    assert traces
    assert audit
    for row in [*traces, *await _rows(EventOutbox), *audit]:
        assert _CONNECTION_TEXT not in _row_text(row), type(row).__name__


class _EmptyConn:
    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        row = _setting_row(_SETTING, "")
        row["boot_val"] = None
        return [row]


@pytest.mark.asyncio
async def test_settings_keep_an_empty_connection_text_setting_as_it_is() -> None:
    result = await fetch_settings(_EmptyConn(), [_SETTING])  # type: ignore[arg-type]
    row = result["settings"][0]
    assert row["setting"] == ""
    assert row["reset_val"] == ""
    assert row["boot_val"] is None
