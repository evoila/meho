# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``postgres.settings`` hides the secret parts of a connection-text setting.

In a value of a setting from ``CONNECTION_TEXT_SETTINGS`` the fixed hide mark
stands in place of the password (and of ``sslpassword`` and
``oauth_client_secret``), in the key word form and in the address form. The
rest of the value stays character for character, and a value with no secret
part stays as it is.

Through ``dispatch``, for a person, an agent login and a service login, the
made-up password is in none of the result, the flight recorder trace, the
event outbox (the activity feed) and the audit row. The trace of a typed read
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

#: A made-up password. It must never show up in any output.
_PASSWORD = "made-up-pw-0815-abcdef"
_CONNECTION_TEXT = f"host=standby.example.test port=5432 user=replicator password={_PASSWORD}"
_VISIBLE_PART = "host=standby.example.test port=5432 user=replicator password="


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
    assert _PASSWORD not in repr(result.model_dump())
    rows = {row["name"]: row for row in (result.result or {})["settings"]}
    hidden = rows[_SETTING]
    expected = f"{_VISIBLE_PART}{SECRET_FIELD_MARKER}"
    assert hidden["setting"] == hidden["boot_val"] == hidden["reset_val"] == expected
    assert rows["work_mem"]["setting"] == "4096"

    traces = await _rows(DispatchTrace) + await _rows(DispatchTraceSpan)
    audit = await _rows(AuditLog)
    assert traces
    assert audit
    for row in [*traces, *await _rows(EventOutbox), *audit]:
        assert _PASSWORD not in _row_text(row), type(row).__name__


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


class _ValueConn:
    def __init__(self, value: str) -> None:
        self._value = value

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [_setting_row(_SETTING, self._value)]


async def _hidden(value: str) -> str:
    result = await fetch_settings(_ValueConn(value), [_SETTING])  # type: ignore[arg-type]
    row = result["settings"][0]
    assert row["setting"] == row["boot_val"] == row["reset_val"]
    return str(row["setting"])


@pytest.mark.asyncio
async def test_settings_hide_only_the_secret_key_word_values() -> None:
    value = (
        "user=replicator password=made-up-bare-0815 host = standby.example.test "
        "sslpassword = 'made-up \\'quoted\\' 0815' application_name=walreceiver port=5432"
    )
    expected = (
        f"user=replicator password={SECRET_FIELD_MARKER} host = standby.example.test "
        f"sslpassword = '{SECRET_FIELD_MARKER}' application_name=walreceiver port=5432"
    )
    assert await _hidden(value) == expected


@pytest.mark.asyncio
async def test_settings_hide_an_oauth_client_secret_value() -> None:
    value = "host=standby.example.test oauth_client_secret=made-up-0815 oauth_client_id=meho"
    expected = (
        f"host=standby.example.test oauth_client_secret={SECRET_FIELD_MARKER} oauth_client_id=meho"
    )
    assert await _hidden(value) == expected


@pytest.mark.asyncio
async def test_settings_keep_a_quoted_value_of_another_key_word() -> None:
    value = "application_name='app password=made-up' host=standby.example.test"
    assert await _hidden(value) == value


@pytest.mark.asyncio
async def test_settings_hide_only_the_secret_parts_of_an_address() -> None:
    value = (
        "postgresql://repl:made-up-pw-0815@db.example.test:5433/postgres"  # trufflehog:ignore
        "?sslmode=require&password=made-up-param-0815&application_name=walreceiver"
    )
    expected = (
        f"postgresql://repl:{SECRET_FIELD_MARKER}@db.example.test:5433/postgres"
        f"?sslmode=require&password={SECRET_FIELD_MARKER}&application_name=walreceiver"
    )
    assert await _hidden(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "host=standby.example.test port=5432 user=replicator application_name=walreceiver",
        "postgres://replicator@standby.example.test:5432/postgres?sslmode=require",
    ],
    ids=["key-words", "address"],
)
@pytest.mark.asyncio
async def test_settings_keep_a_connection_text_with_no_secret_part(value: str) -> None:
    assert await _hidden(value) == value
