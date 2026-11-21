# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``postgres.settings`` hides each secret value of a connection-text setting.

The value of a setting from ``CONNECTION_TEXT_SETTINGS`` is read with the
rules of libpq (the PostgreSQL pages "Connection Strings" and "Parameter Key
Words"). The fixed hide mark stands in place of each secret value, in the key
word form and in the address form. Every other character stays as the server
sent it, so each test compares the whole result with the expected text. A
text that libpq cannot read (also one with a name that is not a libpq key
word) makes the call refused when it holds a secret name or an address
password, and stays as it is otherwise.

Through ``dispatch``, for a person, an agent login and a service login, the
made-up password is in none of the result or the error, the flight recorder
trace, the event outbox (the activity feed) and the audit row. The plain
setting keeps its value. A call without ``names`` is never refused.
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
from meho_backplane.checks import runner as sensor_runner
from meho_backplane.checks.runner import _run_evaluation, _SensorSnapshot
from meho_backplane.connectors.postgres import PostgresConnector
from meho_backplane.connectors.postgres import connector as connector_module
from meho_backplane.connectors.postgres.queries import (
    CURATED_SETTINGS,
    PostgresSettingsRefusedError,
    fetch_settings,
)
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
_PASSWORD = "made-up-pw-0815-abcdef"  # trufflehog:ignore
_VISIBLE_PART = "host=standby.example.test port=5432 user=replicator password="
_CONNECTION_TEXT = _VISIBLE_PART + _PASSWORD


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


_M = SECRET_FIELD_MARKER

#: Key word form: (value, expected). Each id names the libpq rule it checks.
_KEY_WORD_CASES = [
    pytest.param(
        "host=db.example.test password=pw-0815 port=5432",  # trufflehog:ignore
        f"host=db.example.test password={_M} port=5432",  # trufflehog:ignore
        id="space-separates",
    ),
    pytest.param(
        "host=db.example.test\tpassword=pw-0815\tport=5432",  # trufflehog:ignore
        f"host=db.example.test\tpassword={_M}\tport=5432",  # trufflehog:ignore
        id="tab-separates",
    ),
    pytest.param(
        "host=db.example.test\npassword=pw-0815\nport=5432",  # trufflehog:ignore
        f"host=db.example.test\npassword={_M}\nport=5432",  # trufflehog:ignore
        id="line-end-separates",
    ),
    pytest.param(
        "host=db.example.test\rpassword=pw-0815\rport=5432",  # trufflehog:ignore
        f"host=db.example.test\rpassword={_M}\rport=5432",  # trufflehog:ignore
        id="carriage-return-separates",
    ),
    pytest.param(
        "host=db.example.test\vpassword=pw-0815\vport=5432",  # trufflehog:ignore
        f"host=db.example.test\vpassword={_M}\vport=5432",  # trufflehog:ignore
        id="vertical-tab-separates",
    ),
    pytest.param(
        "host=db.example.test\fpassword=pw-0815\fport=5432",  # trufflehog:ignore
        f"host=db.example.test\fpassword={_M}\fport=5432",  # trufflehog:ignore
        id="form-feed-separates",
    ),
    pytest.param(
        "host=db.example.test password = pw-0815 port=5432",  # trufflehog:ignore
        f"host=db.example.test password = {_M} port=5432",  # trufflehog:ignore
        id="spaces-around-equals",
    ),
    pytest.param(
        "password=  port=5432 host=db.example.test",  # trufflehog:ignore
        f"password=  {_M} host=db.example.test",  # trufflehog:ignore
        id="spaces-after-equals-belong-to-the-separator",
    ),
    pytest.param(
        "password=pw\u00a00815 port=5432",  # trufflehog:ignore
        f"password={_M} port=5432",  # trufflehog:ignore
        id="no-break-space-is-part-of-a-bare-value",
    ),
    pytest.param(
        "password=pw\x1f0815 port=5432",  # trufflehog:ignore
        f"password={_M} port=5432",  # trufflehog:ignore
        id="unit-separator-is-part-of-a-bare-value",
    ),
    pytest.param(
        "application_name='walreceiver'password=pw-0815 port=5432",  # trufflehog:ignore
        f"application_name='walreceiver'password={_M} port=5432",  # trufflehog:ignore
        id="name-right-after-a-closing-quote",
    ),
    pytest.param(
        "password=pw\\\n0815 port=5432",  # trufflehog:ignore
        f"password={_M} port=5432",  # trufflehog:ignore
        id="backslash-before-a-line-end-in-a-bare-value",
    ),
    pytest.param(
        "password='pw\\\n0815' port=5432",  # trufflehog:ignore
        f"password='{_M}' port=5432",  # trufflehog:ignore
        id="backslash-before-a-line-end-in-a-quoted-value",
    ),
    pytest.param(
        "password=pw\\ 0815 port=5432",  # trufflehog:ignore
        f"password={_M} port=5432",  # trufflehog:ignore
        id="backslash-before-a-space-in-a-bare-value",
    ),
    pytest.param(
        "sslpassword = 'pw \\'q\\' 0815' port=5432",  # trufflehog:ignore
        f"sslpassword = '{_M}' port=5432",  # trufflehog:ignore
        id="quoted-value-keeps-its-quotes",
    ),
    pytest.param(
        "PASSWORD=pw-0815 SslPassword=key-0815 port=5432",  # trufflehog:ignore
        f"PASSWORD={_M} SslPassword={_M} port=5432",  # trufflehog:ignore
        id="upper-case-name",
    ),
    pytest.param(
        "host=db.example.test oauth_client_secret=cs-0815 oauth_client_id=meho",
        f"host=db.example.test oauth_client_secret={_M} oauth_client_id=meho",
        id="oauth-client-secret",
    ),
    pytest.param(
        "host=db.example.test password='' port=5432",  # trufflehog:ignore
        "host=db.example.test password='' port=5432",  # trufflehog:ignore
        id="empty-quoted-secret-value",
    ),
    pytest.param(
        "host=db.example.test port=5432 password=",  # trufflehog:ignore
        "host=db.example.test port=5432 password=",  # trufflehog:ignore
        id="empty-bare-secret-value",
    ),
    pytest.param(
        "application_name='app password=pw-0815' host=db.example.test",  # trufflehog:ignore
        "application_name='app password=pw-0815' host=db.example.test",  # trufflehog:ignore
        id="quoted-value-of-another-name",
    ),
]

#: Address form: (value, expected).
_ADDRESS_CASES = [
    pytest.param(
        "postgresql://repl:pw-0815@db.test:5433/app?sslmode=require",  # trufflehog:ignore
        f"postgresql://repl:{_M}@db.test:5433/app?sslmode=require",  # trufflehog:ignore
        id="postgresql-start-word-with-password",
    ),
    pytest.param(
        "postgres://repl:pw-0815@db.test/app",  # trufflehog:ignore
        f"postgres://repl:{_M}@db.test/app",  # trufflehog:ignore
        id="postgres-start-word-with-password",
    ),
    pytest.param(
        "POSTGRESQL://repl:pw-0815@db.test/app",  # trufflehog:ignore
        f"POSTGRESQL://repl:{_M}@db.test/app",  # trufflehog:ignore
        id="upper-case-start-word",
    ),
    pytest.param(
        "postgresql://repl:pw?0815@db.test/app",  # trufflehog:ignore
        f"postgresql://repl:{_M}@db.test/app",  # trufflehog:ignore
        id="question-mark-inside-the-user-part",
    ),
    pytest.param(
        "postgresql://db.test/app?password=pw-0815&sslmode=require",  # trufflehog:ignore
        f"postgresql://db.test/app?password={_M}&sslmode=require",  # trufflehog:ignore
        id="secret-parameter-first-after-the-question-mark",
    ),
    pytest.param(
        "postgresql://db.test/app?sslmode=require&pass%77ord=pw-0815",  # trufflehog:ignore
        f"postgresql://db.test/app?sslmode=require&pass%77ord={_M}",
        id="percent-encoded-secret-name",
    ),
    pytest.param(
        "postgresql://db.test/app?password=pw#x-0815&sslmode=require",  # trufflehog:ignore
        f"postgresql://db.test/app?password={_M}&sslmode=require",  # trufflehog:ignore
        id="hash-inside-a-secret-value",
    ),
    pytest.param(
        "postgresql://db.test/app?PASSWORD=pw-0815&SSLPassword=key-0815",  # trufflehog:ignore
        f"postgresql://db.test/app?PASSWORD={_M}&SSLPassword={_M}",  # trufflehog:ignore
        id="upper-case-parameter-name",
    ),
    pytest.param(
        "postgresql://db.test/app?oauth_client_secret=cs-0815",
        f"postgresql://db.test/app?oauth_client_secret={_M}",
        id="oauth-client-secret-parameter",
    ),
    pytest.param(
        "postgresql://db.test/app?password=&sslmode=require",  # trufflehog:ignore
        "postgresql://db.test/app?password=&sslmode=require",  # trufflehog:ignore
        id="empty-secret-parameter-value",
    ),
    pytest.param(
        "postgresql://repl:pw 0815@db.test/app",  # trufflehog:ignore
        f"postgresql://repl:{_M}@db.test/app",  # trufflehog:ignore
        id="space-in-the-address-password",
    ),
    pytest.param(
        "postgresql://repl:pw\t0815@db.test/app",  # trufflehog:ignore
        f"postgresql://repl:{_M}@db.test/app",  # trufflehog:ignore
        id="tab-in-the-address-password",
    ),
    pytest.param(
        "postgresql://re\u00a0pl:pw\u00a00815@db.test/app",  # trufflehog:ignore
        f"postgresql://re\u00a0pl:{_M}@db.test/app",  # trufflehog:ignore
        id="no-break-space-in-the-user-and-the-password",
    ),
    pytest.param(
        "postgresql://repl:pw-0815@db.test/app?application_name=wal receiver",  # trufflehog:ignore
        f"postgresql://repl:{_M}@db.test/app?application_name=wal receiver",  # trufflehog:ignore
        id="space-in-a-parameter-value-of-an-address-with-a-password",
    ),
    pytest.param(
        "postgresql://db.test/a?application_name=wal rcv&password=pw-0815",  # trufflehog:ignore
        f"postgresql://db.test/a?application_name=wal rcv&password={_M}",  # trufflehog:ignore
        id="space-in-a-parameter-value-before-a-secret-parameter",
    ),
]

#: Texts that libpq cannot read and that hold a secret: the call is refused.
_REFUSED_CASES = [
    pytest.param(
        "host=db.example.test password='pw-0815",  # trufflehog:ignore
        id="unclosed-quote-with-a-secret-name",
    ),
    pytest.param(
        "host=db.example.test password pw-0815",  # trufflehog:ignore
        id="missing-equals-with-a-secret-name",
    ),
    pytest.param(
        "=pw-0815 password=pw-0815",  # trufflehog:ignore
        id="empty-name-with-a-secret-name",
    ),
    pytest.param(
        "application_name='password rotation",
        id="unclosed-quote-with-a-secret-name-in-a-value",
    ),
    pytest.param(
        "host=db.example.test unknownopt=1 password=pw-0815",  # trufflehog:ignore
        id="unknown-key-word-with-a-secret-name",
    ),
    pytest.param(
        "postgresql://db.test/app?password=pw%zz0815",  # trufflehog:ignore
        id="broken-percent-code-with-a-secret-name",
    ),
    pytest.param(
        "postgresql://db.test/app?pass%77ord=pw-0815&broken",  # trufflehog:ignore
        id="percent-encoded-secret-name-in-an-unreadable-text",
    ),
    pytest.param(
        "postgresql://db.test/app?password=pw=0815",  # trufflehog:ignore
        id="two-equals-in-a-secret-parameter",
    ),
    pytest.param(
        "postgresql://db.test/app?unknownopt=1&password=pw-0815",  # trufflehog:ignore
        id="unknown-address-parameter-with-a-secret-name",
    ),
    pytest.param(
        "postgresql://repl:pw-0815@db.test/app?ssl=false",  # trufflehog:ignore
        id="unknown-address-parameter-with-an-address-password",
    ),
]

#: Texts that libpq cannot read and that hold no secret: they stay as they are.
_UNREADABLE_KEPT_CASES = [
    pytest.param("host='db.example.test port=5432", id="unclosed-quote"),
    pytest.param("host=db.example.test port", id="missing-equals"),
    pytest.param("postgresql://db.test/app?sslmode=require&broken", id="parameter-without-equals"),
    pytest.param("host=db.example.test unknownopt=1", id="unknown-key-word"),
    pytest.param("postgresql://db.test/app?unknownopt=1", id="unknown-address-parameter"),
]

#: Texts with no secret part, in each form: they stay equal to the input.
_NO_SECRET_CASES = [
    pytest.param(
        "host=db.example.test port=5432 user=replicator application_name=walreceiver",
        id="key-words",
    ),
    pytest.param(
        "host = 'db.example.test' user='repl \\'x\\'' sslmode=require",
        id="key-words-with-quotes",
    ),
    pytest.param(
        "postgres://replicator@db.example.test:5432/postgres?sslmode=require",
        id="address",
    ),
    pytest.param(
        "postgresql://db.test/app?application_name=wal%20receiver#x",
        id="address-with-percent-codes",
    ),
    pytest.param(
        "postgresql://db.example.test/my db?application_name=wal receiver",
        id="address-with-white-space",
    ),
    pytest.param(
        "postgresql://db.test/app?ssl=true",
        id="address-with-ssl-true",
    ),
    pytest.param(
        "postgresql://db.test:5432/app@repl:x",  # trufflehog:ignore
        id="address-with-an-at-sign-after-the-slash",
    ),
    pytest.param(
        "postgresql://[2001:db8::1?password=x]/app",  # trufflehog:ignore
        id="address-with-a-question-mark-inside-a-bracket-host",
    ),
]


@pytest.mark.parametrize(("value", "expected"), _KEY_WORD_CASES)
@pytest.mark.asyncio
async def test_settings_hide_each_secret_key_word_value(value: str, expected: str) -> None:
    assert await _hidden(value) == expected


@pytest.mark.parametrize(("value", "expected"), _ADDRESS_CASES)
@pytest.mark.asyncio
async def test_settings_hide_each_secret_value_of_an_address(value: str, expected: str) -> None:
    assert await _hidden(value) == expected


@pytest.mark.parametrize("value", _REFUSED_CASES)
@pytest.mark.asyncio
async def test_settings_hide_an_unreadable_text_whole_only_with_a_secret(value: str) -> None:
    with pytest.raises(PostgresSettingsRefusedError) as refused:
        await fetch_settings(_ValueConn(value), [_SETTING])  # type: ignore[arg-type]
    message = str(refused.value)
    assert _SETTING in message
    assert "out of names" in message
    assert "0815" not in message
    assert "rotation" not in message


@pytest.mark.parametrize("value", _UNREADABLE_KEPT_CASES)
@pytest.mark.asyncio
async def test_settings_keep_an_unreadable_text_without_a_secret(value: str) -> None:
    assert await _hidden(value) == value


@pytest.mark.parametrize("value", _NO_SECRET_CASES)
@pytest.mark.asyncio
async def test_settings_keep_a_connection_text_with_no_secret_part(value: str) -> None:
    assert await _hidden(value) == value


@pytest.mark.asyncio
async def test_settings_hide_the_value_for_a_setting_name_in_upper_case() -> None:
    row = _setting_row(_SETTING.upper(), _CONNECTION_TEXT)

    class _UpperConn:
        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            return [row]

    result = await fetch_settings(_UpperConn(), [_SETTING])  # type: ignore[arg-type]
    assert result["settings"][0]["setting"] == f"{_VISIBLE_PART}{_M}"


#: A value of the setting that libpq cannot read and that holds the password.
_UNREADABLE_TEXT = f"host=standby.example.test password='{_PASSWORD}"


class _UnreadableConn:
    """Answers each asked name; the connection-text setting holds a broken value."""

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        wanted = list(args[0])
        rows = [_setting_row(name, "4096") for name in wanted if name != _SETTING]
        if _SETTING in wanted:
            rows.append(_setting_row(_SETTING, _UNREADABLE_TEXT))
        return rows

    async def close(self) -> None:
        return None


@pytest.mark.parametrize(
    "kind",
    [PrincipalKind.USER, PrincipalKind.AGENT, PrincipalKind.SERVICE],
    ids=["person", "agent", "service"],
)
@pytest.mark.asyncio
async def test_settings_refuse_an_unreadable_text_with_a_secret_for_every_login(
    monkeypatch: pytest.MonkeyPatch, tenant_id: UUID, kind: PrincipalKind
) -> None:
    monkeypatch.setattr(
        connector_module, "connect_read_only", AsyncMock(return_value=_UnreadableConn())
    )
    result = await dispatch(
        operator=_login(kind, tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_Target(tenant_id),
        params={"names": [_SETTING, "work_mem"]},
    )
    assert result.status != "ok"
    assert result.error == "connector_error: PostgresSettingsRefusedError"
    assert result.result is None
    assert _PASSWORD not in repr(result.model_dump())

    traces = await _rows(DispatchTrace) + await _rows(DispatchTraceSpan)
    audit = await _rows(AuditLog)
    assert traces
    assert audit
    for row in [*traces, *await _rows(EventOutbox), *audit]:
        assert _PASSWORD not in _row_text(row), type(row).__name__


@pytest.mark.asyncio
async def test_settings_without_names_are_never_refused(
    monkeypatch: pytest.MonkeyPatch, tenant_id: UUID
) -> None:
    assert not CONNECTION_TEXT_SETTINGS & {name.lower() for name in CURATED_SETTINGS}
    monkeypatch.setattr(
        connector_module, "connect_read_only", AsyncMock(return_value=_UnreadableConn())
    )
    result = await dispatch(
        operator=_login(PrincipalKind.AGENT, tenant_id),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_Target(tenant_id),
        params={},
    )
    assert result.status == "ok", result.error
    names = [row["name"] for row in (result.result or {})["settings"]]
    assert names == list(CURATED_SETTINGS)


@pytest.mark.asyncio
async def test_a_sensor_of_a_refused_setting_reads_unknown(
    monkeypatch: pytest.MonkeyPatch, tenant_id: UUID
) -> None:
    target = _Target(tenant_id)

    async def _resolve(*args: Any, **kwargs: Any) -> tuple[Any, None]:
        return target, None

    monkeypatch.setattr(sensor_runner, "_resolve_target_or_error", _resolve)
    monkeypatch.setattr(
        connector_module, "connect_read_only", AsyncMock(return_value=_UnreadableConn())
    )
    snap = _SensorSnapshot(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="pg-standby-link",
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target={"name": target.name},
        params={"names": [_SETTING]},
        assertion={
            "select": {"path": "$.settings[0].setting"},
            "compare": {"type": "equals", "value": "x"},
        },
        identity_sub="__sensor__",
    )
    outcome = await _run_evaluation(snap)
    assert outcome.state == "unknown"
    assert outcome.evidence["dispatch_error"] == "connector_error: PostgresSettingsRefusedError"
    assert _PASSWORD not in repr(outcome.evidence)
