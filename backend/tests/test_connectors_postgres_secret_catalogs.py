# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``postgres.query`` refuses a statement that names a catalog that can hold a secret.

* Each name in ``DENIED_RELATIONS`` is refused before a connection opens: in
  upper case, in double quotes, with the ``pg_catalog.`` prefix, inside a
  comment, inside a sub-query and as a text value.
* ``SHOW ALL`` and a ``SHOW`` of a connection-text setting are refused, and
  the refusal of settings names ``postgres.settings``.
* The upgrade row and the connector page name every refused name.
* The refusal holds through the dispatcher for a person, an agent login and
  a service login, with and without a standing grant, on a target that
  allows a superuser login.
* A plain read of a user table, and views that show no secret value, keep
  running at once.
* A sensor of ``postgres.query`` whose statement names such a catalog reads
  ``unknown`` at its run, with the refusal as the cause.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.checks import runner as sensor_runner
from meho_backplane.checks.runner import _run_evaluation, _SensorSnapshot
from meho_backplane.connectors.postgres import PostgresConnector, PostgresReadOnlyError
from meho_backplane.connectors.postgres import connector as connector_module
from meho_backplane.connectors.postgres.query_guard import (
    ALLOW_SUPERUSER_QUERY_EXTRA,
    CONNECTION_TEXT_SETTINGS,
    DENIED_RELATIONS,
    assert_no_secret_catalogs,
    denied_relation_in,
)
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission, PermissionVerdict, ServicePrincipalGrant
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "postgres-wire-16"
_OP_ID = "postgres.query"
_TENANT = uuid.uuid4()
_SERVICE = "svc-postgres-catalogs"
_AGENT = "agent:postgres-catalogs"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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
async def _registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    await PostgresConnector.register_operations()


class _Target:
    def __init__(self) -> None:
        self.product = "postgres"
        self.fingerprint = type("_FP", (), {"version": "16"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id = _TENANT
        self.name = "pg-catalogs"
        self.host = "pg.example.test"
        self.port = 5432
        self.secret_ref = None
        self.auth_model = None
        self.verify_tls = True
        self.tls_ca_pin = None
        self.tls_server_name = None
        # A superuser login is allowed: the catalog check must still hold.
        self.extras: dict[str, Any] = {ALLOW_SUPERUSER_QUERY_EXTRA: True}


class _Cursor:
    async def fetch(self, n: int) -> list[dict[str, Any]]:
        return [{"n": 1}][:n]


class _Txn:
    async def __aenter__(self) -> _Txn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Conn:
    def __init__(self) -> None:
        self.cursor_sql: str | None = None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return True  # a superuser login

    def transaction(self, *, readonly: bool = False) -> _Txn:
        return _Txn()

    async def cursor(self, sql: str) -> _Cursor:
        self.cursor_sql = sql
        return _Cursor()

    async def close(self) -> None:
        return None


def _patch_connection(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, _Conn]:
    conn = _Conn()
    mock = AsyncMock(return_value=conn)
    monkeypatch.setattr(connector_module, "connect_read_only", mock)
    return mock, conn


def _person() -> Operator:
    return Operator(
        sub="op:postgres-catalogs",
        name="Caller",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _agent() -> Operator:
    return Operator(
        sub=str(uuid.uuid5(uuid.NAMESPACE_URL, _AGENT)),
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
        client_id=_AGENT,
    )


def _service() -> Operator:
    return Operator(
        sub=_SERVICE,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


async def _grant_everyone() -> None:
    """A standing grant for the service login and an auto-execute agent grant row."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            ServicePrincipalGrant(
                tenant_id=_TENANT,
                principal_sub=_SERVICE,
                op_id=_OP_ID,
                connector_id=_CONNECTOR_ID,
                target_id=None,
                reason="test: postgres.query",
                created_by_sub="test-admin",
            )
        )
        session.add(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub=_AGENT,
                op_pattern=_OP_ID,
                verdict=PermissionVerdict.AUTO_EXECUTE.value,
                target_scope="*",
                created_by_sub="test-admin",
            )
        )


def _spellings(name: str) -> list[str]:
    """The ways a statement can name *name* that the guard must still see."""
    return [
        f"SELECT * FROM {name}",
        f"SELECT * FROM {name.upper()}",
        f'SELECT * FROM "{name}"',
        f"SELECT * FROM pg_catalog.{name}",
        f"SELECT * FROM PG_CATALOG . {name.upper()}",
        f"SELECT 1 /* {name} */",
        f"SELECT 1 -- {name}",
        f"SELECT n FROM (SELECT count(*) AS n FROM {name}) AS s",
        f"WITH s AS (SELECT * FROM {name}) SELECT count(*) FROM s",
        f"SELECT '{name}'",
    ]


_CASES = [
    pytest.param(sql, name, id=f"{name}-{i}")
    for name in sorted(DENIED_RELATIONS)
    for i, sql in enumerate(_spellings(name))
]


@pytest.mark.parametrize(("sql", "name"), _CASES)
def test_denied_catalog_is_found_in_any_spelling(sql: str, name: str) -> None:
    assert denied_relation_in(sql) == name
    with pytest.raises(PostgresReadOnlyError, match=name):
        assert_no_secret_catalogs(sql)


@pytest.mark.parametrize(("sql", "name"), _CASES)
@pytest.mark.asyncio
async def test_run_query_refuses_the_catalog_before_a_connection_opens(
    monkeypatch: pytest.MonkeyPatch, sql: str, name: str
) -> None:
    mock, _ = _patch_connection(monkeypatch)
    with pytest.raises(PostgresReadOnlyError, match=name):
        await PostgresConnector().run_query(_person(), _Target(), {"sql": sql})
    mock.assert_not_awaited()


def test_a_unicode_escaped_identifier_is_refused() -> None:
    with pytest.raises(PostgresReadOnlyError, match="Unicode-escaped"):
        assert_no_secret_catalogs('SELECT * FROM U&"pg_\\0061uthid"')


@pytest.mark.parametrize(
    ("name", "hint"),
    [
        ("pg_stat_activity", "Use postgres.activity instead."),
        ("pg_stat_get_activity", "Use postgres.activity instead."),
        ("pg_stat_get_backend_activity", "Use postgres.activity instead."),
        ("pg_settings", "Use postgres.settings instead."),
        ("pg_file_settings", "Use postgres.settings instead."),
        ("pg_show_all_settings", "Use postgres.settings instead."),
        ("pg_show_all_file_settings", "Use postgres.settings instead."),
        ("current_setting", "Use postgres.settings instead."),
    ],
)
def test_the_refusal_for_sessions_names_the_curated_operation(name: str, hint: str) -> None:
    with pytest.raises(PostgresReadOnlyError) as refused:
        assert_no_secret_catalogs(f"SELECT * FROM {name}")
    assert str(refused.value).endswith(hint)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders",
        "SELECT o.id FROM public.orders o JOIN customers c ON c.id = o.customer_id",
        "SELECT rolname, rolsuper FROM pg_roles",
        "SELECT usename FROM pg_user",
        "SELECT * FROM pg_stat_ssl",
        "SELECT * FROM pg_stat_subscription",
        "SELECT * FROM pg_subscription_rel",
        "SELECT * FROM pg_stat_wal_receiver",
        "SELECT * FROM pg_stat_database",
        "SHOW statement_timeout",
        "SHOW work_mem",
        "SELECT * FROM my_pg_stats_copy",
        "SELECT * FROM pg_stat_user_tables",
        "SELECT srvname FROM pg_foreign_server",
        "SELECT * FROM my_pg_stat_activity_copy",
    ],
)
def test_reads_that_show_no_secret_pass(sql: str) -> None:
    assert denied_relation_in(sql) is None
    assert_no_secret_catalogs(sql)


def test_deny_list_entries_are_lower_case() -> None:
    assert all(name == name.lower() for name in DENIED_RELATIONS)
    assert all(name == name.lower() for name in CONNECTION_TEXT_SETTINGS)


def _show_spellings(word: str) -> list[str]:
    """The ways a ``SHOW`` statement can name *word* that the guard must still see."""
    return [
        f"SHOW {word}",
        f"show {word}",
        f"SHOW {word.upper()}",
        f'SHOW "{word}"',
        f'SHOW "{word.upper()}"',
        f"SHOW /* settings */ {word}",
        f"-- settings\nSHOW {word}",
        f"/* settings */ SHOW\n\t{word}",
    ]


_SHOW_CASES = [
    pytest.param(sql, id=f"{word}-{i}")
    for word in ["all", *sorted(CONNECTION_TEXT_SETTINGS)]
    for i, sql in enumerate(_show_spellings(word))
]


@pytest.mark.parametrize("sql", _SHOW_CASES)
def test_show_of_all_or_a_connection_text_setting_is_refused(sql: str) -> None:
    with pytest.raises(PostgresReadOnlyError) as refused:
        assert_no_secret_catalogs(sql)
    assert str(refused.value).endswith("Use postgres.settings instead.")


@pytest.mark.parametrize("sql", _SHOW_CASES)
@pytest.mark.asyncio
async def test_run_query_refuses_the_show_before_a_connection_opens(
    monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    mock, _ = _patch_connection(monkeypatch)
    with pytest.raises(PostgresReadOnlyError, match=r"postgres\.settings"):
        await PostgresConnector().run_query(_person(), _Target(), {"sql": sql})
    mock.assert_not_awaited()


_REPO = Path(__file__).resolve().parents[2]
_UPGRADE_ROW = _REPO / "docs/upgrade-rows/v0.49.0/postgres-query-refuses-secret-catalogs.md"
_CONNECTOR_PAGE = _REPO / "docs/codebase/connectors-postgres.md"

#: Names in the upgrade row that are refused through another list.
_OTHER_REFUSED_NAMES = {"table_to_xml", "schema_to_xml", "database_to_xml"}


def _code_names(text: str) -> set[str]:
    return set(re.findall(r"`([a-z_][a-z0-9_]*)`", text))


def test_the_upgrade_row_names_every_refused_name() -> None:
    note = _UPGRADE_ROW.read_text(encoding="utf-8").split("## Action")[0]
    names = _code_names(note)
    assert names >= DENIED_RELATIONS
    assert names >= CONNECTION_TEXT_SETTINGS
    assert names - DENIED_RELATIONS - CONNECTION_TEXT_SETTINGS == _OTHER_REFUSED_NAMES


def test_the_connector_page_names_every_refused_name() -> None:
    names = _code_names(_CONNECTOR_PAGE.read_text(encoding="utf-8"))
    assert names >= DENIED_RELATIONS
    assert names >= CONNECTION_TEXT_SETTINGS


_LOGINS = [_person, _agent, _service]
_LOGIN_IDS = ["person", "agent", "service"]


@pytest.mark.parametrize("granted", [False, True], ids=["no-grant", "standing-grant"])
@pytest.mark.parametrize("login", _LOGINS, ids=_LOGIN_IDS)
@pytest.mark.usefixtures("_registered")
@pytest.mark.asyncio
async def test_dispatch_refuses_the_catalog_for_every_login(
    monkeypatch: pytest.MonkeyPatch, login: Any, granted: bool
) -> None:
    if granted:
        await _grant_everyone()
    mock, _ = _patch_connection(monkeypatch)
    result = await dispatch(
        operator=login(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_Target(),
        params={"sql": "SELECT * FROM pg_authid"},
    )
    assert result.status == "error"
    assert result.error == "connector_error: PostgresReadOnlyError"
    assert "'pg_authid'" in str((result.extras or {}).get("exception_message"))
    mock.assert_not_awaited()


@pytest.mark.parametrize("granted", [False, True], ids=["no-grant", "standing-grant"])
@pytest.mark.parametrize("login", _LOGINS, ids=_LOGIN_IDS)
@pytest.mark.usefixtures("_registered")
@pytest.mark.asyncio
async def test_dispatch_runs_a_plain_read_at_once_for_every_login(
    monkeypatch: pytest.MonkeyPatch, login: Any, granted: bool
) -> None:
    if granted:
        await _grant_everyone()
    _, conn = _patch_connection(monkeypatch)
    result = await dispatch(
        operator=login(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_Target(),
        params={"sql": "SELECT count(*) AS n FROM orders"},
    )
    assert result.status == "ok", result.error
    assert conn.cursor_sql == "SELECT count(*) AS n FROM orders"


@pytest.mark.usefixtures("_registered")
@pytest.mark.asyncio
async def test_a_sensor_of_a_denied_catalog_reads_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _Target()

    async def _resolve(*args: Any, **kwargs: Any) -> tuple[Any, None]:
        return target, None

    monkeypatch.setattr(sensor_runner, "_resolve_target_or_error", _resolve)
    mock, _ = _patch_connection(monkeypatch)
    snap = _SensorSnapshot(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        name="pg-sessions",
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target={"name": target.name},
        params={"sql": "SELECT count(*) AS n FROM pg_stat_activity"},
        assertion={
            "select": {"path": "$.row_count"},
            "compare": {"type": "threshold", "op": "gt", "critical": 10},
        },
        identity_sub="__sensor__",
    )
    outcome = await _run_evaluation(snap)
    assert outcome.state == "unknown"
    assert outcome.evidence["reason"] == "dispatch_not_ok"
    assert outcome.evidence["dispatch_error"] == "connector_error: PostgresReadOnlyError"
    mock.assert_not_awaited()
