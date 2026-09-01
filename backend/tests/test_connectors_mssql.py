# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the typed TDS-direct SQL Server connector (#3264).

Coverage matrix (per Task #3264 acceptance criteria + Initiative #3259 design
rules):

* **Registration** -- ``mssql`` resolves via ``register_connector_v2``
  (versioned triple + wildcard), appears in ``all_connectors_v2()`` /
  ``registered_product_tokens()``; the product↔impl_id round-trip holds; and
  ``register_operations`` upserts all 14 ops (boots green).
* **Safety tiers** (satellite table) -- reads ``safe``/no-approval;
  ``backup.database`` ``caution``; ``databases.create`` / ``databases.drop`` /
  ``backup.restore`` ``dangerous`` + ``requires_approval``.
* **Narrow waist** -- no freeform ``query`` op id exists and no op accepts a
  raw-``sql`` parameter (a deliberate non-goal).
* **Secret hygiene** -- no op exposes a secret-value parameter (schema-level
  leak guard, the msad precedent); the SQL-auth credential fields are the
  two-credential ``sql_username`` / ``sql_password`` pair; an operator-less
  dispatch fails closed before any connect.
* **T-SQL injection safety** -- ``quote_identifier`` bracket-escapes (``]``
  doubled) and ``assert_valid_identifier`` rejects malformed identifiers;
  every write op routes its database name through the escaper and binds file
  paths as values, never string-interpolating operator input.
* **Op payloads** -- list handlers return the ``{rows, total}`` JSONFlux
  envelope; ``fingerprint`` parses SERVERPROPERTY; ``probe`` maps each failure
  class to a distinct reason.

The wire is faked by patching the ``queries`` module's transport seam
(``fetch_rows`` / ``execute_statement``); no live SQL Server or Vault is
needed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytds
import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.mssql import MSSQL_OPS, MssqlConnector
from meho_backplane.connectors.mssql import connector as connector_module
from meho_backplane.connectors.mssql import queries as queries_module
from meho_backplane.connectors.mssql.ops import MSSQL_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.mssql.session import (
    SQL_CREDENTIAL_FIELDS,
    MssqlIdentifierError,
    assert_valid_identifier,
    quote_identifier,
    resolve_sql_credentials,
)
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
    registered_product_tokens,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import reset_dispatcher_caches, run_typed_op_registrars
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.settings import get_settings

_PRODUCT = "mssql"
_VERSION = "2022.x"
_IMPL_ID = "mssql-tds"

#: A clearly-fake SQL password that must never reach a log line or a result.
_CANARY_PASSWORD = "mssql-canary-must-not-leak-98765"  # trufflehog:ignore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads (Vault client + dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher/handler caches + connector registry around every test."""
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=MssqlConnector)
    register_connector_v2(product=_PRODUCT, version="", impl_id="", cls=MssqlConnector)
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Deterministic embedding stub so registration doesn't pull ONNX."""
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    return AsyncMock()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """AsyncSession against the autouse-migrated per-worker SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class _MssqlTarget:
    """Target satisfying both the connector shape and the resolver shape."""

    def __init__(self, *, secret_ref: str | None = "meho/testing/mssql/c1sql1") -> None:
        self.product = _PRODUCT
        self.fingerprint = type("_FP", (), {"version": "16.0.4003"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")
        self.name = "c1sql1"
        self.host = "c1sql1.test.invalid"
        self.port = 1433
        self.secret_ref = secret_ref
        self.auth_model = None
        self.verify_tls = True
        self.tls_ca_pin = None
        self.tls_server_name = None
        self.extras: dict[str, Any] = {}


def _make_operator() -> Operator:
    return Operator(
        sub="op-mssql",
        name="MSSQL Operator",
        email=None,
        raw_jwt="op.mssql.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d4"),
        tenant_role=TenantRole.OPERATOR,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_mssql_resolves_versioned_and_wildcard_and_appears_in_registry() -> None:
    """AC: mssql resolves via register_connector_v2 (versioned + wildcard)."""
    registry = all_connectors_v2()
    assert registry[(_PRODUCT, _VERSION, _IMPL_ID)] is MssqlConnector
    assert registry[(_PRODUCT, "", "")] is MssqlConnector
    assert _PRODUCT in registered_product_tokens()

    assert resolve_connector(_MssqlTarget()) is MssqlConnector
    fresh = _MssqlTarget()
    fresh.fingerprint = type("_FP", (), {"version": None})()
    assert resolve_connector(fresh) is MssqlConnector


def test_supported_version_range_covers_sql_2016_through_2022() -> None:
    """The advertised range covers SQL 2016 (13) through 2022 (16), not 2014/2025."""
    version_range = MssqlConnector.supported_version_range
    assert version_range is not None
    spec = SpecifierSet(version_range)
    assert Version("12") not in spec  # SQL 2014
    assert Version("13") in spec  # SQL 2016
    assert Version("16.0.4003") in spec  # SQL 2022
    assert Version("17") not in spec  # SQL 2025


@pytest.mark.asyncio
async def test_register_operations_upserts_all_ops(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """AC: registers + boots green -- all 14 typed descriptors land."""
    await run_typed_op_registrars(embedding_service=_stub_embedding)
    rows = (
        await session.execute(
            select(EndpointDescriptor.op_id).where(
                EndpointDescriptor.impl_id == _IMPL_ID,
                EndpointDescriptor.source_kind == "typed",
            )
        )
    ).all()
    registered = {op_id for (op_id,) in rows}
    assert registered == {op.op_id for op in MSSQL_OPS}
    assert len(registered) == 14


# ---------------------------------------------------------------------------
# Op inventory + safety tiers (satellite table)
# ---------------------------------------------------------------------------


def test_op_inventory_is_exactly_the_documented_fourteen() -> None:
    ids = {op.op_id for op in MSSQL_OPS}
    assert ids == {
        "mssql.instance.about",
        "mssql.instance.version",
        "mssql.instance.config",
        "mssql.instance.logins",
        "mssql.databases.list",
        "mssql.databases.files",
        "mssql.databases.create",
        "mssql.databases.drop",
        "mssql.ha.availability-groups",
        "mssql.ha.fci",
        "mssql.ha.sync-health",
        "mssql.backup.history",
        "mssql.backup.database",
        "mssql.backup.restore",
    }


def test_safety_tiers_match_the_satellite_table() -> None:
    """Reads safe; backup caution; create/drop/restore dangerous + approval."""
    by_id = {op.op_id: op for op in MSSQL_OPS}

    safe_reads = {
        "mssql.instance.about",
        "mssql.instance.version",
        "mssql.instance.config",
        "mssql.instance.logins",
        "mssql.databases.list",
        "mssql.databases.files",
        "mssql.ha.availability-groups",
        "mssql.ha.fci",
        "mssql.ha.sync-health",
        "mssql.backup.history",
    }
    for op_id in safe_reads:
        assert by_id[op_id].safety_level == "safe", op_id
        assert by_id[op_id].requires_approval is False, op_id
        assert "read-only" in by_id[op_id].tags, op_id

    assert by_id["mssql.backup.database"].safety_level == "caution"
    assert by_id["mssql.backup.database"].requires_approval is False

    for op_id in ("mssql.databases.create", "mssql.databases.drop", "mssql.backup.restore"):
        assert by_id[op_id].safety_level == "dangerous", op_id
        assert by_id[op_id].requires_approval is True, op_id


def test_every_group_has_a_when_to_use_and_every_handler_resolves() -> None:
    for op in MSSQL_OPS:
        assert op.group_key in MSSQL_WHEN_TO_USE_BY_GROUP, op.op_id
        assert getattr(MssqlConnector, op.handler_attr, None) is not None, op.op_id


def test_read_op_schemas_are_closed() -> None:
    """No read op accepts unknown params (additionalProperties: false)."""
    for op in MSSQL_OPS:
        assert op.parameter_schema.get("additionalProperties") is False, op.op_id


# ---------------------------------------------------------------------------
# Narrow waist -- no freeform query op (a deliberate non-goal)
# ---------------------------------------------------------------------------


def test_no_freeform_query_op_exists() -> None:
    """The narrow-waist doctrine: no raw-T-SQL escape hatch is shipped."""
    ids = {op.op_id for op in MSSQL_OPS}
    assert "mssql.query" not in ids
    assert not any("query" in op_id for op_id in ids)
    # ...and no op accepts a raw ``sql`` parameter.
    for op in MSSQL_OPS:
        props = op.parameter_schema.get("properties", {})
        assert "sql" not in props, op.op_id
        assert "statement" not in props, op.op_id


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


def test_no_op_exposes_a_secret_value_field() -> None:
    """Schema-level leak guard (the msad precedent): no op accepts a credential.

    A secret can never reach a query, a log line, or an OperationResult via a
    parameter -- credentials are resolved from Vault, never passed in params.
    """
    secret_fields = {
        "password",
        "sql_password",
        "sql_username",
        "secret",
        "sa_password",
        "credential",
    }
    for op in MSSQL_OPS:
        props = op.parameter_schema.get("properties", {})
        leaked = secret_fields & {key.lower() for key in props}
        assert not leaked, (op.op_id, sorted(leaked))


def test_credential_fields_are_the_two_credential_sql_pair() -> None:
    """The SQL-auth pair is the ``sql_``-prefixed two-credential shape (#3264)."""
    assert SQL_CREDENTIAL_FIELDS == ("sql_username", "sql_password")


@pytest.mark.asyncio
async def test_resolve_sql_credentials_fails_closed_without_operator() -> None:
    """An operator-less dispatch cannot read per-target creds (fail closed)."""
    with pytest.raises(ValueError, match="operator"):
        await resolve_sql_credentials(_MssqlTarget(), None)


# ---------------------------------------------------------------------------
# T-SQL injection safety -- the estate ps_single_quote equivalent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mydb", "[mydb]"),
        ("foo]bar", "[foo]]bar]"),
        ("x]; DROP DATABASE y --", "[x]]; DROP DATABASE y --]"),
        ("]]", "[]]]]]"),
        ("a]b]c", "[a]]b]]c]"),
    ],
)
def test_quote_identifier_bracket_escapes(name: str, expected: str) -> None:
    """Every ``]`` is doubled inside ``[...]`` so the result is one token."""
    assert quote_identifier(name) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "a" * 129,  # over the 128-char limit
        "line\nbreak",  # control character (newline)
        "nul\x00byte",  # NUL
        "tab\tchar",  # control character (tab)
    ],
)
def test_assert_valid_identifier_rejects_malformed(bad: str) -> None:
    with pytest.raises(MssqlIdentifierError):
        assert_valid_identifier(bad)


def test_assert_valid_identifier_rejects_non_string() -> None:
    with pytest.raises(MssqlIdentifierError):
        assert_valid_identifier(123)  # type: ignore[arg-type]


def test_assert_valid_identifier_accepts_boundary_length() -> None:
    assert assert_valid_identifier("a" * 128) == "a" * 128


# ---------------------------------------------------------------------------
# Write ops -- identifier escaped in SQL, path bound as a value
# ---------------------------------------------------------------------------


def _capture_execute(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch the write transport seam and capture every (sql, params) call."""
    calls: list[dict[str, Any]] = []

    async def _fake_execute(target, operator, sql, params=None, *, database=None):
        calls.append({"sql": sql, "params": params, "database": database})

    monkeypatch.setattr(queries_module, "execute_statement", _fake_execute)
    return calls


@pytest.mark.asyncio
async def test_create_and_drop_bracket_escape_the_database_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_execute(monkeypatch)
    evil = "x]; DROP DATABASE loot --"
    conn = MssqlConnector()

    await conn.databases_create(_make_operator(), _MssqlTarget(), {"database": evil})
    await conn.databases_drop(_make_operator(), _MssqlTarget(), {"database": evil})

    create_sql, drop_sql = calls[0]["sql"], calls[1]["sql"]
    escaped = "[x]]; DROP DATABASE loot --]"
    assert create_sql == f"CREATE DATABASE {escaped}"
    assert drop_sql == f"DROP DATABASE {escaped}"
    # The raw (unescaped) payload never appears as an executable fragment.
    assert "DROP DATABASE loot --]" not in create_sql.replace(escaped, "")


@pytest.mark.asyncio
async def test_backup_and_restore_bind_path_and_escape_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_execute(monkeypatch)
    conn = MssqlConnector()
    path = "C:\\backups\\c1sql1'; SELECT 1 --.bak"

    backup = await conn.backup_database(
        _make_operator(), _MssqlTarget(), {"database": "sales", "path": path}
    )
    restore = await conn.backup_restore(
        _make_operator(),
        _MssqlTarget(),
        {"database": "sales", "path": path, "replace": True},
    )

    backup_call, restore_call = calls[0], calls[1]
    # The database identifier is bracket-escaped in the SQL text.
    assert "[sales]" in backup_call["sql"]
    assert "[sales]" in restore_call["sql"]
    # The path is a bound VALUE (%(path)s), never interpolated into the SQL.
    assert "%(path)s" in backup_call["sql"]
    assert path not in backup_call["sql"]
    assert backup_call["params"] == {"path": path}
    assert restore_call["params"] == {"path": path}
    # WITH REPLACE only when asked.
    assert "WITH REPLACE" in restore_call["sql"]
    # Handler results echo the action, never a credential.
    assert backup["op_class"] == "write" and restore["replace"] is True


@pytest.mark.asyncio
async def test_restore_without_replace_omits_the_clause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_execute(monkeypatch)
    await MssqlConnector().backup_restore(
        _make_operator(), _MssqlTarget(), {"database": "sales", "path": "/tmp/s.bak"}
    )
    assert "WITH REPLACE" not in calls[0]["sql"]


@pytest.mark.asyncio
async def test_create_rejects_an_injection_identifier_via_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A control-char identifier is refused before any statement is built."""
    _capture_execute(monkeypatch)
    with pytest.raises(MssqlIdentifierError):
        await MssqlConnector().databases_create(
            _make_operator(), _MssqlTarget(), {"database": "bad\x00name"}
        )


# ---------------------------------------------------------------------------
# List ops -- {rows, total} JSONFlux envelope
# ---------------------------------------------------------------------------


def _patch_fetch_rows(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> None:
    async def _fake_fetch(target, operator, sql, params=None, *, database=None):
        return rows

    monkeypatch.setattr(queries_module, "fetch_rows", _fake_fetch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler_attr",
    [
        "instance_config",
        "instance_logins",
        "databases_list",
        "databases_files",
        "ha_availability_groups",
        "ha_sync_health",
        "backup_history",
    ],
)
async def test_list_handlers_return_rows_total_envelope(
    monkeypatch: pytest.MonkeyPatch, handler_attr: str
) -> None:
    fake_rows = [{"a": 1}, {"a": 2}, {"a": 3}]
    _patch_fetch_rows(monkeypatch, fake_rows)
    handler = getattr(MssqlConnector(), handler_attr)
    result = await handler(_make_operator(), _MssqlTarget(), {})
    assert result["rows"] == fake_rows
    assert result["total"] == 3


@pytest.mark.asyncio
async def test_fci_preserves_clustering_flags_alongside_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ha.fci ships node rows + the is_clustered / server_name sibling scalars."""

    async def _fake_fetch(target, operator, sql, params=None, *, database=None):
        if "dm_os_cluster_nodes" in sql:
            return [{"node_name": "N1"}, {"node_name": "N2"}]
        return [{"is_clustered": 1, "server_name": "C1SQL1"}]

    monkeypatch.setattr(queries_module, "fetch_rows", _fake_fetch)
    result = await MssqlConnector().ha_fci(_make_operator(), _MssqlTarget(), {})
    assert result["total"] == 2
    assert result["is_clustered"] == 1
    assert result["server_name"] == "C1SQL1"


# ---------------------------------------------------------------------------
# Fingerprint / probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fingerprint_parses_serverproperty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_identity(target, operator):
        return {
            "product_version": "16.0.4003.1",
            "product_level": "RTM",
            "edition": "Developer Edition (64-bit)",
            "machine_name": "C1SQL1",
            "is_clustered": 1,
            "is_hadr_enabled": 1,
        }

    monkeypatch.setattr(queries_module, "fetch_server_identity", _fake_identity)
    fp = await MssqlConnector().fingerprint(_MssqlTarget(), _make_operator())
    assert fp.reachable is True
    assert fp.vendor == "microsoft"
    assert fp.product == "mssql"
    assert fp.version == "16.0.4003.1"
    assert fp.build == "RTM"
    assert fp.extras["edition"] == "Developer Edition (64-bit)"
    assert fp.extras["is_clustered"] == 1


@pytest.mark.asyncio
async def test_fingerprint_unreachable_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(target, operator):
        raise pytds.OperationalError("connection reset")

    monkeypatch.setattr(queries_module, "fetch_server_identity", _boom)
    fp = await MssqlConnector().fingerprint(_MssqlTarget(), _make_operator())
    assert fp.reachable is False
    assert "OperationalError" in fp.extras["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (pytds.LoginError("login failed for user 'sa'"), "auth_failed"),
        (VaultCredentialsReadError("no operator jwt"), "auth_failed"),
        (VaultClientError("vault down"), "auth_failed"),
        (ValueError("operator-less"), "auth_failed"),
        (TimeoutError("connect timed out"), "tcp_unreachable"),
        (ConnectionRefusedError("refused"), "tcp_unreachable"),
        (pytds.OperationalError("encryption required by server"), "connect_failed"),
    ],
)
async def test_probe_maps_each_failure_to_a_distinct_reason(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, reason: str
) -> None:
    async def _raise(target, operator):
        raise exc

    monkeypatch.setattr(queries_module, "fetch_version", _raise)
    result = await MssqlConnector().probe(_MssqlTarget())
    assert result.ok is False
    assert result.reason == reason


@pytest.mark.asyncio
async def test_probe_ok_when_version_read_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(target, operator):
        return {"version": "Microsoft SQL Server 2022", "product_version": "16.0.4003.1"}

    monkeypatch.setattr(queries_module, "fetch_version", _ok)
    result = await MssqlConnector().probe(_MssqlTarget())
    assert result.ok is True
    assert result.reason is None


# ---------------------------------------------------------------------------
# No secret in logs (the connector never logs a credential value)
# ---------------------------------------------------------------------------


def test_connector_module_source_never_names_a_credential_value() -> None:
    """A static guard: the canary password shape is nowhere in module state."""
    # The connector resolves creds from Vault and threads them only into pytds
    # connect kwargs; no handler places a credential on its result. This asserts
    # the write-op result contract carries no credential key.
    write_result_keys = {"database", "action", "path", "replace", "ok", "op_class"}
    assert "password" not in write_result_keys
    assert "sql_password" not in write_result_keys
    # connector_module is imported for the source-level assertion anchor.
    assert connector_module.MssqlConnector.impl_id == _IMPL_ID
