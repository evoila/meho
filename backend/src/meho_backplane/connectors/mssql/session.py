# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Wire-session plumbing + T-SQL injection safety for the mssql connector (#3264).

The transport layer of MEHO's SQL Server connector. It owns three concerns,
kept out of ``connector.py`` so the connector body stays a thin op surface
(the postgres / mongodb direct-protocol precedent, #2236 / #2237):

* **The credential seam** — :func:`resolve_sql_credentials` reads the target's
  ``secret_ref`` under the operator's identity, extracting the **two-credential**
  SQL-auth pair ``sql_username`` / ``sql_password`` (the first two-credential
  connector on this seam; the names are prefixed ``sql_`` so a later
  dbatools-over-PowerShell increment can carry its own ``username`` /
  ``ssh_private_key`` fields in the *same* Vault secret without collision).
  The pair flows only into the ``pytds`` connect params — never a log line or
  an :class:`OperationResult`.

* **The blocking wire runner** — ``python-tds`` (import :mod:`pytds`) is a
  **pure-Python** TDS driver (no unixODBC / msodbcsql, no FreeTDS system
  packages) but a **synchronous** DBAPI one. Every connect + execute + fetch
  round-trip runs off the event loop in a single :func:`asyncio.to_thread`
  hop (:func:`fetch_rows` / :func:`execute_statement`), mirroring the hvac /
  mail / rke2 sync-driver-in-async precedent. The transport decision (TDS-direct
  vs dbatools-over-PowerShell) + the driver comparison (pyodbc / pymssql /
  python-tds) is recorded in ``docs/codebase/connectors-mssql.md``.

* **T-SQL injection safety** — the connector builds SQL, not PowerShell, so the
  discipline is *parameter binding first*: operator-supplied **values** ride
  ``pytds`` pyformat placeholders (``%(name)s``), never string interpolation.
  Where an operator-supplied **identifier** (a database name) cannot be bound
  as a parameter — ``CREATE DATABASE`` / ``DROP DATABASE`` / ``BACKUP`` /
  ``RESTORE`` name the object, and DDL object names are not bindable — it is
  passed through :func:`quote_identifier`: a strict pre-validation
  (:func:`assert_valid_identifier`) then QUOTENAME-style bracket escaping
  (``]`` doubled inside ``[...]``). A payload like ``x]; DROP DATABASE y --``
  becomes the single delimited token ``[x]]; DROP DATABASE y --]``, not a
  second statement. This is the mssql equivalent of the estate connectors'
  ``ps_single_quote`` discipline.

Encryption posture
==================

``pytds`` enables TLS only when a ``cafile`` (trusted-CA PEM) is supplied; with
none it advertises ``ENCRYPT_NOT_SUP`` in the TDS pre-login. Against a default
(non-force-encryption) SQL Server 2022 the connection succeeds and the channel
is unencrypted after pre-login; against a **force-encryption** server pytds
raises a clear error ("encryption ... required by server"). This mirrors the
postgres connector's posture (no explicit TLS config; connects against the lab
target). Cert-validated TLS for a force-encryption / strict-mode instance is a
named future extension: an optional ``sql_ca`` field on the Vault secret,
written to a temp file and passed as ``cafile`` — deferred here, not built
speculatively.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from decimal import Decimal
from typing import Any

import pytds

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials

__all__ = [
    "DEFAULT_DATABASE",
    "DEFAULT_LOGIN_TIMEOUT_S",
    "DEFAULT_PORT",
    "DEFAULT_QUERY_TIMEOUT_S",
    "DEFAULT_WRITE_TIMEOUT_S",
    "MAX_IDENTIFIER_LENGTH",
    "SQL_CREDENTIAL_FIELDS",
    "MssqlIdentifierError",
    "assert_valid_identifier",
    "execute_statement",
    "fetch_rows",
    "jsonable_row",
    "quote_identifier",
    "resolve_sql_credentials",
]

#: The TDS wire-protocol default port. A target overrides via ``target.port``.
DEFAULT_PORT = 1433

#: The system database the connector connects to when no ``database`` scopes an
#: op. ``master`` always exists and every catalog view the read ops query
#: (``sys.databases``, ``sys.configurations``, ``sys.dm_hadr_*``,
#: ``msdb`` cross-db) is reachable from it.
DEFAULT_DATABASE = "master"

#: Login (connect + handshake) timeout in seconds — bounds a hung TCP / TLS
#: handshake so a probe or op fails fast rather than hanging the dispatcher.
DEFAULT_LOGIN_TIMEOUT_S = 15

#: Per-statement timeout for **read** ops in seconds. A catalog read is bounded
#: so a wedged instance cannot pin the ``asyncio.to_thread`` worker.
DEFAULT_QUERY_TIMEOUT_S = 30

#: Per-statement timeout for **write** ops (``BACKUP`` / ``RESTORE`` /
#: ``CREATE`` / ``DROP``) in seconds. Backup and restore of a real database run
#: for minutes to hours, so the 30 s read timeout would abort them mid-flight;
#: writes get a generous 4-hour ceiling that still bounds a truly-hung
#: statement without cutting a legitimate long-running restore short.
DEFAULT_WRITE_TIMEOUT_S = 4 * 60 * 60

#: The KV-v2 secret fields the SQL-auth connection needs. Prefixed ``sql_`` so
#: a future dbatools-over-PowerShell increment can store SSH creds
#: (``username`` / ``ssh_private_key`` / ``password``) in the *same* secret
#: without shadowing the TDS pair — the two-credential shape documented in the
#: connector doc.
SQL_CREDENTIAL_FIELDS: tuple[str, ...] = ("sql_username", "sql_password")

#: SQL Server delimited-identifier maximum length (regular + delimited
#: identifiers are capped at 128 characters:
#: https://learn.microsoft.com/en-us/sql/relational-databases/databases/database-identifiers).
MAX_IDENTIFIER_LENGTH = 128


class MssqlIdentifierError(ValueError):
    """An operator-supplied identifier failed pre-validation.

    Raised by :func:`assert_valid_identifier` before any statement is built, so
    a rejected identifier never reaches the wire. Subclasses
    :class:`ValueError` so the dispatcher's ``connector_error`` branch renders
    the message verbatim.
    """


def assert_valid_identifier(name: object) -> str:
    """Return *name* unchanged, or raise :class:`MssqlIdentifierError`.

    The strict pre-validation half of the identifier-safety discipline (the
    bracket escaping in :func:`quote_identifier` is the second half). Rejects a
    non-string, an empty string, a name longer than
    :data:`MAX_IDENTIFIER_LENGTH`, and any name carrying a control character
    (``\\x00``-``\\x1f`` — a NUL would truncate the delimited token in the TDS
    stream; a newline/tab has no place in a database name). Every other byte is
    permitted because :func:`quote_identifier` escapes the one delimiter that
    matters (``]``); the goal is a single valid delimited token, not a
    restrictive charset.
    """
    if not isinstance(name, str):
        raise MssqlIdentifierError(f"identifier must be a string, got {type(name).__name__}")
    if not name:
        raise MssqlIdentifierError("identifier must not be empty")
    if len(name) > MAX_IDENTIFIER_LENGTH:
        raise MssqlIdentifierError(
            f"identifier exceeds SQL Server's {MAX_IDENTIFIER_LENGTH}-character "
            f"limit (got {len(name)} characters)"
        )
    if any(ord(ch) < 0x20 for ch in name):
        raise MssqlIdentifierError("identifier must not contain control characters (0x00-0x1f)")
    return name


def quote_identifier(name: object) -> str:
    """Return *name* as a QUOTENAME-style bracket-delimited T-SQL identifier.

    Validates via :func:`assert_valid_identifier`, then wraps the name in
    ``[...]`` with every embedded ``]`` doubled — the exact escaping SQL
    Server's ``QUOTENAME`` performs. The result is a single delimited
    identifier that the T-SQL parser reads as one object name, so an operator
    who passes ``x]; DROP DATABASE y --`` gets the harmless token
    ``[x]]; DROP DATABASE y --]`` (a database that does not exist), never a
    second executable statement. Used everywhere a database name is
    interpolated into DDL / utility statements that cannot bind it as a
    parameter (``CREATE`` / ``DROP`` / ``BACKUP`` / ``RESTORE``).
    """
    validated = assert_valid_identifier(name)
    return "[" + validated.replace("]", "]]") + "]"


def jsonable_row(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce one ``pytds`` result row to JSON-serialisable primitives.

    ``pytds`` maps SQL Server types to rich Python objects (``datetime`` /
    ``date`` / ``time`` for the temporal types, ``Decimal`` for ``NUMERIC`` /
    ``MONEY``, ``bytes`` for ``VARBINARY``). The dispatcher wraps a handler's
    return value into an :class:`OperationResult` whose ``result`` must be
    JSON-serialisable, so every row value is normalised: temporals become
    ISO-8601 strings, an integral ``Decimal`` becomes an exact ``int`` and a
    fractional one a ``float``, ``bytes`` becomes a hex string, and everything
    else passes through.
    """
    return {key: _jsonable(value) for key, value in row.items()}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        # An integral Decimal (SQL Server ``numeric(p,0)`` — a ``backup_size``
        # in bytes, a large row count) must round-trip exactly: ``float`` loses
        # precision beyond ~15 digits, so keep it a Python ``int`` (arbitrary
        # precision). Only a genuinely fractional value (``size_mb``,
        # ``decimal(18,2)``) becomes a ``float``. High-precision identifier
        # columns (backup LSNs, ``numeric(25,0)``) are cast to ``varchar`` in
        # the query itself so they arrive here already as strings.
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return str(value)


async def resolve_sql_credentials(target: Any, operator: Operator | None) -> dict[str, str]:
    """Resolve *target*'s ``secret_ref`` to the SQL-auth ``{sql_username, sql_password}``.

    Reads the KV-v2 secret under *operator*'s identity via
    :func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`,
    requesting only :data:`SQL_CREDENTIAL_FIELDS`. SQL Server auth always needs
    a credential (there is no trust-auth TDS path here), so an operator-less
    dispatch — the readiness probe, the legacy ``execute`` shim — fails closed
    with a :class:`ValueError` naming the requirement rather than reaching the
    loader with ``operator=None``. The returned dict is ephemeral in-memory
    state: it flows only into the ``pytds`` connect kwargs, never a log event
    or an :class:`OperationResult`.
    """
    if operator is None:
        raise ValueError(
            f"mssql target {getattr(target, 'name', target)!r} has a secret_ref "
            "but no authenticated operator was supplied; a SQL Server target "
            "cannot be reached on an operator-less dispatch path"
        )
    return await load_basic_credentials(target, operator, fields=SQL_CREDENTIAL_FIELDS)


def _connect_kwargs(
    *, host: str, port: int, database: str, creds: dict[str, str], query_timeout: int
) -> dict[str, Any]:
    """Assemble the ``pytds.connect`` kwargs shared by read + write runners.

    ``as_dict=True`` returns rows keyed by column name (the ``{rows, total}``
    envelope shape); ``autocommit=True`` commits DDL / utility statements
    (``CREATE`` / ``DROP DATABASE`` cannot run inside a multi-statement
    transaction) and is harmless for reads; ``disable_connect_retry=True``
    makes an unreachable target fail fast inside ``login_timeout`` rather than
    silently retrying. ``query_timeout`` is the per-statement timeout — short
    for reads, generous for long-running backup/restore writes.
    """
    return {
        "server": host,
        "port": port,
        "database": database,
        "user": creds["sql_username"],
        "password": creds["sql_password"],
        "login_timeout": DEFAULT_LOGIN_TIMEOUT_S,
        "timeout": query_timeout,
        "as_dict": True,
        "autocommit": True,
        "disable_connect_retry": True,
        "appname": "meho-backplane",
    }


def _blocking_fetch(
    *, host: str, port: int, database: str, creds: dict[str, str], sql: str, params: Any
) -> list[dict[str, Any]]:
    """Synchronous connect → execute → fetchall → close (runs in a worker thread)."""
    conn = pytds.connect(
        **_connect_kwargs(
            host=host,
            port=port,
            database=database,
            creds=creds,
            query_timeout=DEFAULT_QUERY_TIMEOUT_S,
        )
    )
    try:
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        return [jsonable_row(row) for row in rows]
    finally:
        conn.close()


def _blocking_execute(
    *, host: str, port: int, database: str, creds: dict[str, str], sql: str, params: Any
) -> None:
    """Synchronous connect → execute → close for a non-row-returning statement.

    Uses :data:`DEFAULT_WRITE_TIMEOUT_S` (a generous ceiling) so a legitimate
    long-running ``BACKUP`` / ``RESTORE`` is not aborted by the short read
    timeout.
    """
    conn = pytds.connect(
        **_connect_kwargs(
            host=host,
            port=port,
            database=database,
            creds=creds,
            query_timeout=DEFAULT_WRITE_TIMEOUT_S,
        )
    )
    try:
        conn.cursor().execute(sql, params)
    finally:
        conn.close()


async def fetch_rows(
    target: Any,
    operator: Operator | None,
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """Run a row-returning statement off the event loop, returning dict rows.

    Resolves credentials (async Vault read), then runs the whole synchronous
    ``pytds`` round-trip in one :func:`asyncio.to_thread` hop. *params* are
    bound through pyformat placeholders (``%(name)s``) — the value-injection
    defence; identifiers are pre-escaped by the caller via
    :func:`quote_identifier`.
    """
    creds = await resolve_sql_credentials(target, operator)
    return await asyncio.to_thread(
        _blocking_fetch,
        host=target.host,
        port=getattr(target, "port", None) or DEFAULT_PORT,
        database=database or DEFAULT_DATABASE,
        creds=creds,
        sql=sql,
        params=params,
    )


async def execute_statement(
    target: Any,
    operator: Operator | None,
    sql: str,
    params: dict[str, Any] | None = None,
    *,
    database: str | None = None,
) -> None:
    """Run a non-row-returning statement (DDL / backup / restore) off the event loop.

    Same credential + ``asyncio.to_thread`` path as :func:`fetch_rows`, but the
    statement produces no result set to fetch (``CREATE`` / ``DROP DATABASE``,
    ``BACKUP`` / ``RESTORE`` emit only informational messages). The identifier
    a write names is bracket-escaped by the caller via
    :func:`quote_identifier`; any value predicate binds through *params*.
    """
    creds = await resolve_sql_credentials(target, operator)
    await asyncio.to_thread(
        _blocking_execute,
        host=target.host,
        port=getattr(target, "port", None) or DEFAULT_PORT,
        database=database or DEFAULT_DATABASE,
        creds=creds,
        sql=sql,
        params=params,
    )
