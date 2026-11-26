# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``postgres.settings`` shows a connection text whole or as one fixed mark.

The value of a setting from ``CONNECTION_TEXT_SETTINGS`` is shown whole only
when it holds no at sign, percent sign, backslash or quote, none of the
secret key words in any case, and no colon after a start word of the address
form. Else the whole value is the one mark that names the setting. No part of
the value is shown, and the call is not refused because of it.

Through ``dispatch``, for a person, an agent login and a service login, the
made-up password is in none of the result, the flight recorder trace, the
event outbox (the activity feed) and the audit row. The plain setting keeps
its value.
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
    CONNECTION_TEXT_MARK,
    CURATED_SETTINGS,
    fetch_settings,
)
from meho_backplane.connectors.postgres.query_guard import CONNECTION_TEXT_SETTINGS
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, DispatchTrace, DispatchTraceSpan, EventOutbox, Tenant
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "postgres-wire-16"
_OP_ID = "postgres.settings"
_SETTING = sorted(CONNECTION_TEXT_SETTINGS)[0]

#: A made-up password. It must never show up in any output.
_PASSWORD = "made-up-pw-" + "0815-abcdef"  # trufflehog:ignore
_CONNECTION_TEXT = "host=standby.example.test port=5432 user=replicator password=" + _PASSWORD
_MARK = CONNECTION_TEXT_MARK.format(setting=_SETTING)


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
async def test_settings_put_one_mark_in_place_of_a_connection_text_for_every_login(
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
    assert hidden["setting"] == hidden["boot_val"] == hidden["reset_val"] == _MARK
    assert _SETTING in _MARK
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


async def _shown(value: str) -> str:
    result = await fetch_settings(_ValueConn(value), [_SETTING])  # type: ignore[arg-type]
    row = result["settings"][0]
    assert row["setting"] == row["boot_val"] == row["reset_val"]
    return str(row["setting"])


#: Made-up password pieces, built at run time. No piece may show up.
_P1 = "pw" + "-one-" + "4711"  # trufflehog:ignore
_P2 = "pw" + "-two-" + "4712"  # trufflehog:ignore
_P3 = "pw" + "-three-" + "4713"  # trufflehog:ignore
_PIECES = (_P1, _P2, _P3, "0815", "4711", "4712", "4713")  # trufflehog:ignore
_A = "postgresql://"
_U = "repl"
_H = "db.test"
_D = "app"
_NON_ENGLISH = "Grüße-Žiška-Привет-日本"
_LONG = 10_000

#: Texts that get the one mark: each holds an at sign, a percent sign, a
#: backslash, a quote, a secret key word, or a colon after an address start
#: word.
_MARKED_CASES = [
    pytest.param(
        "host=db.example.test password=pw-0815 port=5432",  # trufflehog:ignore
        id="space-separates",
    ),
    pytest.param(
        "host=db.example.test\tpassword=pw-0815\tport=5432",  # trufflehog:ignore
        id="tab-separates",
    ),
    pytest.param(
        "host=db.example.test\npassword=pw-0815\nport=5432",  # trufflehog:ignore
        id="line-end-separates",
    ),
    pytest.param(
        "host=db.example.test\rpassword=pw-0815\rport=5432",  # trufflehog:ignore
        id="carriage-return-separates",
    ),
    pytest.param(
        "host=db.example.test\vpassword=pw-0815\vport=5432",  # trufflehog:ignore
        id="vertical-tab-separates",
    ),
    pytest.param(
        "host=db.example.test\fpassword=pw-0815\fport=5432",  # trufflehog:ignore
        id="form-feed-separates",
    ),
    pytest.param(
        "host=db.example.test password = pw-0815 port=5432",  # trufflehog:ignore
        id="spaces-around-equals",
    ),
    pytest.param(
        "password=  port=5432 host=db.example.test",  # trufflehog:ignore
        id="spaces-after-equals-belong-to-the-separator",
    ),
    pytest.param(
        "password=pw\u00a00815 port=5432",  # trufflehog:ignore
        id="no-break-space-is-part-of-a-bare-value",
    ),
    pytest.param(
        "password=pw\x1f0815 port=5432",  # trufflehog:ignore
        id="unit-separator-is-part-of-a-bare-value",
    ),
    pytest.param(
        "application_name='walreceiver'password=pw-0815 port=5432",  # trufflehog:ignore
        id="name-right-after-a-closing-quote",
    ),
    pytest.param(
        "password=pw\\\n0815 port=5432",  # trufflehog:ignore
        id="backslash-before-a-line-end-in-a-bare-value",
    ),
    pytest.param(
        "password='pw\\\n0815' port=5432",  # trufflehog:ignore
        id="backslash-before-a-line-end-in-a-quoted-value",
    ),
    pytest.param(
        "password=pw\\ 0815 port=5432",  # trufflehog:ignore
        id="backslash-before-a-space-in-a-bare-value",
    ),
    pytest.param(
        "sslpassword = 'pw \\'q\\' 0815' port=5432",  # trufflehog:ignore
        id="quoted-value-keeps-its-quotes",
    ),
    pytest.param(
        "PASSWORD=pw-0815 SslPassword=key-0815 port=5432",  # trufflehog:ignore
        id="upper-case-name",
    ),
    pytest.param(
        "host=db.example.test oauth_client_secret=cs-0815"  # trufflehog:ignore
        " oauth_client_id=meho",
        id="oauth-client-secret",
    ),
    pytest.param(
        "host=db.example.test password='' port=5432",  # trufflehog:ignore
        id="empty-quoted-secret-value",
    ),
    pytest.param(
        "host=db.example.test port=5432 password=",  # trufflehog:ignore
        id="empty-bare-secret-value",
    ),
    pytest.param(
        "application_name='app password=pw-0815' host=db.example.test",  # trufflehog:ignore
        id="quoted-value-of-another-name",
    ),
    pytest.param(
        "host=db.example.test sslnegotiation=direct password=pw-0815",  # trufflehog:ignore
        id="postgresql-17-name-sslnegotiation",
    ),
    pytest.param(
        "min_protocol_version=3.0 max_protocol_version=latest"  # trufflehog:ignore
        " sslkeylogfile=keys.log password=pw-0815",  # trufflehog:ignore
        id="postgresql-18-names",
    ),
    pytest.param(
        "host=db.test scram_client_key=ck-0815 scram_server_key=sk-0815",  # trufflehog:ignore
        id="postgresql-18-scram-keys",
    ),
    pytest.param(
        "postgresql://repl:pw-0815@db.test:5433/app?sslmode=require",  # trufflehog:ignore
        id="postgresql-start-word-with-password",
    ),
    pytest.param(
        "postgres://repl:pw-0815@db.test/app",  # trufflehog:ignore
        id="postgres-start-word-with-password",
    ),
    pytest.param(
        "POSTGRESQL://repl:pw-0815@db.test/app",  # trufflehog:ignore
        id="upper-case-start-word",
    ),
    pytest.param(
        "postgresql://repl:pw?0815@db.test/app",  # trufflehog:ignore
        id="question-mark-inside-the-user-part",
    ),
    pytest.param(
        "postgresql://db.test/app?password=pw-0815&sslmode=require",  # trufflehog:ignore
        id="secret-parameter-first-after-the-question-mark",
    ),
    pytest.param(
        "postgresql://db.test/app?sslmode=require&pass%77ord=pw-0815",  # trufflehog:ignore
        id="percent-encoded-secret-name",
    ),
    pytest.param(
        "postgresql://db.test/app?password=pw#x-0815&sslmode=require",  # trufflehog:ignore
        id="hash-inside-a-secret-value",
    ),
    pytest.param(
        "postgresql://db.test/app?PASSWORD=pw-0815&SSLPassword=key-0815",  # trufflehog:ignore
        id="upper-case-parameter-name",
    ),
    pytest.param(
        "postgresql://db.test/app?oauth_client_secret=cs-0815",  # trufflehog:ignore
        id="oauth-client-secret-parameter",
    ),
    pytest.param(
        "postgresql://db.test/app?password=&sslmode=require",  # trufflehog:ignore
        id="empty-secret-parameter-value",
    ),
    pytest.param(
        "postgresql://repl:pw 0815@db.test/app",  # trufflehog:ignore
        id="space-in-the-address-password",
    ),
    pytest.param(
        "postgresql://repl:pw\t0815@db.test/app",  # trufflehog:ignore
        id="tab-in-the-address-password",
    ),
    pytest.param(
        "postgresql://re\u00a0pl:pw\u00a00815@db.test/app",  # trufflehog:ignore
        id="no-break-space-in-the-user-and-the-password",
    ),
    pytest.param(
        "postgresql://repl:pw-0815@db.test/app?application_name=wal receiver",  # trufflehog:ignore
        id="space-in-a-parameter-value-of-an-address-with-a-password",
    ),
    pytest.param(
        "postgresql://db.test/a?application_name=wal rcv&password=pw-0815",  # trufflehog:ignore
        id="space-in-a-parameter-value-before-a-secret-parameter",
    ),
    pytest.param(
        "postgresql://repl:pw-0815@db.test/app?sslnegotiation=direct",  # trufflehog:ignore
        id="postgresql-17-parameter-sslnegotiation",
    ),
    pytest.param(
        "postgresql://db.test/app?sslnegotiation=direct&password=pw-0815",  # trufflehog:ignore
        id="postgresql-17-parameter-before-a-secret-parameter",
    ),
    pytest.param(
        "postgresql://db.test/a?scram_client_key=ck-1&scram_server_key=sk-1",  # trufflehog:ignore
        id="postgresql-18-scram-key-parameters",
    ),
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
        "application_name='password rotation",  # trufflehog:ignore
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
    pytest.param(
        "postgresql://db.test:5432/app@repl:x",  # trufflehog:ignore
        id="address-with-an-at-sign-after-the-slash",
    ),
    pytest.param(
        "postgresql://repl:pw-08@15-xy@db.test/app",  # trufflehog:ignore
        id="second-at-sign-in-the-host",
    ),
    pytest.param(
        "postgresql://repl@cloud:pw-0815@db.test/app",  # trufflehog:ignore
        id="at-sign-in-the-user-name-and-a-port-that-is-not-a-number",
    ),
    pytest.param(
        "postgresql://repl:pw-08/15-xy@db.test/app",  # trufflehog:ignore
        id="slash-in-the-password-and-a-port-that-is-not-a-number",
    ),
    pytest.param(
        "postgresql://repl:pw-08@db.test@15-xy/app",  # trufflehog:ignore
        id="at-sign-after-the-host",
    ),
    pytest.param(
        "postgresql://db.test:primary/app",  # trufflehog:ignore
        id="port-that-is-not-a-number",
    ),
    pytest.param(
        "postgresql://repl@db@test/app",  # trufflehog:ignore
        id="at-sign-in-the-host",
    ),
    pytest.param(
        "host='db.example.test port=5432",  # trufflehog:ignore
        id="unclosed-quote",
    ),
    pytest.param(
        "host = 'db.example.test' user='repl \\'x\\'' sslmode=require",  # trufflehog:ignore
        id="key-words-with-quotes",
    ),
    pytest.param(
        "postgres://replicator@db.example.test:5432/postgres?sslmode=require",  # trufflehog:ignore
        id="address",
    ),
    pytest.param(
        "postgresql://db.test/app?application_name=wal%20receiver#x",  # trufflehog:ignore
        id="address-with-percent-codes",
    ),
    pytest.param(
        "postgresql://[2001:db8::1?password=x]/app",  # trufflehog:ignore
        id="address-with-a-question-mark-inside-a-bracket-host",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}:5432/{_D}",  # trufflehog:ignore
        id="address-with-a-number-port",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}/{_D}",  # trufflehog:ignore
        id="address-with-no-port",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}:/{_D}",  # trufflehog:ignore
        id="address-with-an-empty-port",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}:primary/{_D}",  # trufflehog:ignore
        id="address-port-not-a-number",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}:primary/{_D}?port=5432",  # trufflehog:ignore
        id="address-port-not-a-number-replaced-by-a-port-parameter",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}:primary/{_D}?port=",  # trufflehog:ignore
        id="address-port-not-a-number-replaced-by-an-empty-port-parameter",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}/{_D}?port=primary",  # trufflehog:ignore
        id="port-parameter-not-a-number",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}:5432/{_D}?port=primary",  # trufflehog:ignore
        id="number-address-port-and-a-port-parameter-not-a-number",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}:%205432/{_D}",  # trufflehog:ignore
        id="address-port-with-an-encoded-space",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}:65536/{_D}",  # trufflehog:ignore
        id="address-port-out-of-range",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@a.test:5432,b.test:5433/{_D}",  # trufflehog:ignore
        id="host-list-and-port-list",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@a.test%2Cb.test:5432%2C5433/{_D}",  # trufflehog:ignore
        id="encoded-host-list-and-encoded-port-list",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}:5432%2C5433/{_D}",  # trufflehog:ignore
        id="one-host-and-an-encoded-list-of-two-ports",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@a.test:5432,b.test:primary/{_D}",  # trufflehog:ignore
        id="host-list-with-a-port-not-a-number",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}/{_D}?host=b.test",  # trufflehog:ignore
        id="host-parameter-replaces-the-host",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}/{_D}?hostaddr=127.0.0.1",  # trufflehog:ignore
        id="hostaddr-parameter",
    ),
    pytest.param(
        f"{_A}{_H}/{_D}?user={_U}&password={_P1}",  # trufflehog:ignore
        id="password-parameter-only",
    ),
    pytest.param(
        f"{_A}{_U}:{_P2}@{_H}/{_D}?password={_P1}",  # trufflehog:ignore
        id="address-password-and-password-parameter",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}%40{_P2}@{_H}/{_D}",  # trufflehog:ignore
        id="encoded-at-sign-in-the-password",
    ),
    pytest.param(
        f"{_A}{_H}/{_D}?password={_P1}@{_P2}",  # trufflehog:ignore
        id="raw-at-sign-in-a-password-parameter",
    ),
    pytest.param(
        f"{_A}{_H}:5432/{_D}?password={_P1}@{_P2}",  # trufflehog:ignore
        id="raw-at-sign-in-a-password-parameter-with-a-port",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}/{_D}?application_name=wal@rcv",  # trufflehog:ignore
        id="raw-at-sign-in-an-application-name",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@%40{_H}/{_D}",  # trufflehog:ignore
        id="encoded-at-sign-starts-the-host",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@db%40test/{_D}",  # trufflehog:ignore
        id="encoded-at-sign-inside-the-host",
    ),
    pytest.param(
        f"{_A}{_U}@{_H}/{_D}?pass%77ord={_P1}",  # trufflehog:ignore
        id="encoded-password-parameter-name",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@[::1]:5432/{_D}",  # trufflehog:ignore
        id="bracket-host-with-a-port",
    ),
    pytest.param(
        f"host={_H} port=5432 user={_U} password={_P1} dbname={_D}",  # trufflehog:ignore
        id="key-word-plain",
    ),
    pytest.param(
        f"host={_H} port=primary user={_U} password={_P1}",  # trufflehog:ignore
        id="key-word-port-not-a-number",
    ),
    pytest.param(
        f"host=a.test,b.test port=5432,5433 user={_U} password={_P1}",  # trufflehog:ignore
        id="key-word-host-list-and-port-list",
    ),
    pytest.param(
        f"host=db@test hostaddr=127.0.0.1 user={_U} password={_P1}",  # trufflehog:ignore
        id="key-word-at-sign-in-the-host-and-a-hostaddr",
    ),
    pytest.param(
        f"host={_H} password={_P1}@{_P2}",  # trufflehog:ignore
        id="key-word-at-sign-in-the-password",
    ),
    pytest.param(
        f"host={_H} password='{_P1} {_P2}'",  # trufflehog:ignore
        id="key-word-quoted-password-with-a-space",
    ),
    pytest.param(
        f"host={_H} port= user={_U} password={_P1}",  # trufflehog:ignore
        id="key-word-nothing-after-the-equals-sign-of-port",
    ),
    pytest.param(
        f"host=a.test,b.test port=5432,primary user={_U} password={_P1}",  # trufflehog:ignore
        id="key-word-port-list-with-a-port-not-a-number",
    ),
    pytest.param(
        f"{_A}{_H}:5432/{_D}?application_name=wal@rcv",  # trufflehog:ignore
        id="no-user-part-and-an-at-sign-in-a-parameter",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_P2}@{_H}/{_D}",  # trufflehog:ignore
        id="raw-at-sign-in-the-password",
    ),
    pytest.param(
        f"{_A}{_U}@{_P2}:{_P1}@{_H}/{_D}",  # trufflehog:ignore
        id="raw-at-sign-in-the-user-name",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}/{_P2}@{_H}/{_D}",  # trufflehog:ignore
        id="raw-slash-in-the-password",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}@{_P2}/{_D}",  # trufflehog:ignore
        id="raw-at-sign-after-the-host",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_P2}@{_H}/{_D}?hostaddr=127.0.0.1",  # trufflehog:ignore
        id="raw-at-sign-in-the-password-and-a-hostaddr-parameter",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_P2}@{_H}/{_D}?host=b.test",  # trufflehog:ignore
        id="raw-at-sign-in-the-password-and-a-host-parameter",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}/{_P2}@{_H}/{_D}?port=5432",  # trufflehog:ignore
        id="raw-slash-in-the-password-and-a-port-parameter",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}/{_P2}?x@{_H}/{_D}",  # trufflehog:ignore
        id="raw-slash-then-question-mark-in-the-password",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}/{_P2}?port=x@{_H}",  # trufflehog:ignore
        id="raw-slash-question-mark-and-key-word-in-the-password",
    ),
    pytest.param(
        f"{_A}{_H}?password={_P1}@{_P2}",  # trufflehog:ignore
        id="raw-at-sign-in-a-password-parameter-with-no-slash",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_P2}/x@{_H}/{_D}",  # trufflehog:ignore
        id="raw-at-sign-then-raw-slash-in-the-password",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@@{_H}/{_D}",  # trufflehog:ignore
        id="raw-at-sign-starts-the-host",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}/{_D}?unknownopt=1",  # trufflehog:ignore
        id="unknown-parameter-and-an-address-password",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_H}/d@b",  # trufflehog:ignore
        id="raw-at-sign-in-the-database-name",
    ),
    pytest.param(
        f"{_A}{_U}:4711/{_P2}@{_H}/{_D}",  # trufflehog:ignore
        id="digits-then-raw-slash-in-the-password",
    ),
    pytest.param(
        f"{_A}{_H}:5432/d:x@b",  # trufflehog:ignore
        id="no-user-part-and-an-at-sign-in-the-database-name",
    ),
    pytest.param(
        f"{_A}{_H}/x@y:{_P1}@{_H}/{_D}",  # trufflehog:ignore
        id="raw-at-sign-and-colon-after-a-slash-with-no-user-part",
    ),
    pytest.param(
        f"{_A}{_H}/x@y:{_P1}@{_H}",  # trufflehog:ignore
        id="raw-at-sign-and-colon-after-a-slash-with-no-user-part-or-database",
    ),
    pytest.param(
        f"{_A}{_U}@{_H}/x@y:{_P1}@{_H}/{_D}",  # trufflehog:ignore
        id="raw-at-sign-and-colon-after-a-slash-in-a-user-part-with-no-colon",
    ),
    pytest.param(
        f"host={_H} password={_P1} {_P2}",  # trufflehog:ignore
        id="key-word-raw-space-in-a-bare-password",
    ),
    pytest.param(
        f"{_A}{_H}:5432?user={_U}&password={_P1}@{_P2}",  # trufflehog:ignore
        id="server-port-no-slash-at-sign-in-password-parameter",
    ),
    pytest.param(
        f"{_A}[::1]?password={_P1}@{_P2}",  # trufflehog:ignore
        id="server-bracket-no-slash-at-sign-in-password-parameter",
    ),
    pytest.param(
        f"{_A}:5432?password={_P1}@{_P2}",  # trufflehog:ignore
        id="empty-host-port-no-slash-at-sign-in-password-parameter",
    ),
    pytest.param(
        f"{_A}a.test,b.test:5432?password={_P1}@{_P2}",  # trufflehog:ignore
        id="host-list-port-no-slash-at-sign-in-password-parameter",
    ),
    pytest.param(
        f"{_A}{_H}:5432?port=5432&password={_P1}?{_P2}@{_P3}",  # trufflehog:ignore
        id="port-parameter-then-password-with-question-mark-and-at-sign",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}@{_P2}?port=5@{_H}/{_D}",  # trufflehog:ignore
        id="question-mark-key-word-and-at-sign-after-the-host",
    ),
    pytest.param(
        f"host={_H} dbname={_A}{_U}:{_P1}@{_H}/{_D}",  # trufflehog:ignore
        id="key-word-text-with-an-address-as-database-name",
    ),
    pytest.param(
        f"{_A}{_U}:{_P1}/{_D}",  # trufflehog:ignore
        id="password-where-the-port-goes",
    ),
    pytest.param(
        f"{_A}{_H}:5432/{_D}",
        id="plain-address-with-a-port-gets-the-mark",
    ),
    pytest.param(
        f"host={_H} password={_P1}"
        + "x" * (_LONG - len(f"host={_H} password={_P1}")),  # trufflehog:ignore
        id="ten-thousand-characters-with-a-secret",
    ),
    pytest.param(
        f"host={_H} application_name={_NON_ENGLISH} password={_P1}",  # trufflehog:ignore
        id="non-english-letters-with-a-secret",
    ),
    pytest.param(
        f"host={_H} application_name={_NON_ENGLISH}@x",
        id="non-english-letters-with-an-at-sign",
    ),
]

#: Texts that are shown whole, character for character.
_WHOLE_CASES = [
    pytest.param(f"host={_H} port=5432 user={_U} dbname={_D}", id="plain-key-word-text"),
    pytest.param(f"{_A}{_H}/{_D}?sslmode=require", id="plain-address-with-no-port"),
    pytest.param(
        "host=db.example.test port=5432 user=replicator application_name=walreceiver",
        id="key-words",
    ),
    pytest.param(
        "postgresql://db.example.test/my db?application_name=wal receiver",
        id="address-with-white-space",
    ),
    pytest.param("postgresql://db.test/app?ssl=true", id="address-with-ssl-true"),
    pytest.param("host=db.example.test port", id="missing-equals"),
    pytest.param("postgresql://db.test/app?sslmode=require&broken", id="parameter-without-equals"),
    pytest.param("host=db.example.test unknownopt=1", id="unknown-key-word"),
    pytest.param("postgresql://db.test/app?unknownopt=1", id="unknown-address-parameter"),
    pytest.param("host=" + "a" * (_LONG - len("host=")), id="ten-thousand-characters"),
    pytest.param(f"host={_H} application_name={_NON_ENGLISH}", id="non-english-letters"),
]


@pytest.mark.parametrize("value", _MARKED_CASES)
@pytest.mark.asyncio
async def test_settings_put_one_mark_in_place_of_a_connection_text(value: str) -> None:
    shown = await _shown(value)
    assert shown == _MARK
    for piece in _PIECES:
        assert piece not in shown


@pytest.mark.parametrize("value", _WHOLE_CASES)
@pytest.mark.asyncio
async def test_settings_show_a_connection_text_with_no_secret_whole(value: str) -> None:
    assert await _shown(value) == value


def test_the_long_cases_hold_ten_thousand_characters() -> None:
    long_ids = {"ten-thousand-characters", "ten-thousand-characters-with-a-secret"}
    long_values = [p.values[0] for p in [*_MARKED_CASES, *_WHOLE_CASES] if p.id in long_ids]
    assert [len(value) for value in long_values] == [_LONG, _LONG]


@pytest.mark.asyncio
async def test_settings_put_the_mark_for_a_setting_name_in_upper_case() -> None:
    row = _setting_row(_SETTING.upper(), _CONNECTION_TEXT)

    class _UpperConn:
        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            return [row]

    result = await fetch_settings(_UpperConn(), [_SETTING])  # type: ignore[arg-type]
    assert result["settings"][0]["setting"] == _MARK


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


@pytest.mark.asyncio
async def test_settings_without_names_return_the_curated_list(
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
async def test_a_sensor_of_a_connection_text_setting_reads_the_mark(
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
            "compare": {"type": "equals", "value": _MARK},
        },
        identity_sub="__sensor__",
    )
    outcome = await _run_evaluation(snap)
    assert outcome.state == "ok", outcome.evidence
    assert outcome.value == _MARK
    assert _PASSWORD not in repr(outcome.evidence)
