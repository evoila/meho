# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Wire-session plumbing + read-only enforcement for the Postgres connector (#2236).

This module owns the two concerns that make the Postgres connector safe by
construction, kept out of ``connector.py`` so the connector body stays a thin
op surface and so the mongodb sibling (#2237) can mirror the shape:

* **The read-only SQL gate** — :func:`assert_read_only_sql` rejects any
  free-form statement whose first significant keyword is not in
  :data:`ALLOWED_FIRST_KEYWORDS` (``SELECT`` / ``SHOW`` / ``EXPLAIN`` /
  ``WITH`` / ``TABLE`` / ``VALUES``). This is the *first* of the two
  read-only defences and the one that fires before a byte reaches the wire:
  ``INSERT`` / ``UPDATE`` / ``DELETE`` / ``CREATE`` / ``DROP`` / … are
  refused up front. The gate is a pure function raising
  :class:`PostgresReadOnlyError`, so a rejected write never opens a
  connection and the unit tests can prove rejection without a live server.

* **The connection factory** — :func:`connect_read_only` opens an
  :class:`asyncpg.Connection` with ``default_transaction_read_only=on`` in
  ``server_settings``. This is the *second*, server-enforced defence: even a
  statement that slips past the keyword gate (or a write attempted directly
  on the connection) is rejected by PostgreSQL itself with
  ``ReadOnlySqlTransactionError`` (SQLSTATE 25006). The two defences are
  independent on purpose — the keyword allowlist is a fast, transport-free
  filter; the session flag is the authoritative backstop.

Auth is optional. A target with ``secret_ref=None`` is a *trust-auth*
instance (``pg_hba.conf`` ``trust`` / peer, or a port-forward to a
dev instance) and connects with no password. A target with a
``secret_ref`` resolves ``{username, password}`` from the operator-context
credential broker; the password flows only into asyncpg's connect params
and never into a log line or an :class:`OperationResult`.
"""

from __future__ import annotations

import re
from typing import Any

import asyncpg

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials

__all__ = [
    "ALLOWED_FIRST_KEYWORDS",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_DATABASE",
    "DEFAULT_PORT",
    "DEFAULT_TRUST_USER",
    "PostgresReadOnlyError",
    "assert_read_only_sql",
    "connect_read_only",
    "first_significant_keyword",
]

#: The wire-protocol default port (PostgreSQL frontend/backend protocol v3).
DEFAULT_PORT = 5432

#: The maintenance database every cluster ships; the connector connects here
#: when no ``database`` param scopes the op. Cluster-wide catalogs
#: (``pg_database``, ``pg_stat_activity``, ``pg_settings``) are identical from
#: any database, so the default is a safe landing spot for inventory ops.
DEFAULT_DATABASE = "postgres"

#: The role a trust-auth (``secret_ref=None``) target connects as. PostgreSQL
#: always requires a role even when ``pg_hba.conf`` demands no password, and no
#: role name is carried on the ``Target`` model, so the connector falls back to
#: the conventional superuser role. An operator needing a different trust role
#: stores it as the ``username`` field of a password-less secret.
DEFAULT_TRUST_USER = "postgres"

#: Connect timeout (seconds) — bounds a hung TCP handshake so a probe or op
#: fails fast rather than hanging the dispatcher.
DEFAULT_CONNECT_TIMEOUT_S = 10.0

#: The first-keyword allowlist for the free-form ``postgres.query`` op. Every
#: entry begins a read-only statement in PostgreSQL:
#:
#: * ``SELECT`` / ``VALUES`` / ``TABLE`` — the three set-returning read forms.
#: * ``WITH`` — a CTE; a ``WITH ... SELECT``. (A ``WITH ... INSERT`` /
#:   ``UPDATE`` / ``DELETE`` data-modifying CTE is still caught by the
#:   server-side ``default_transaction_read_only`` backstop, so the keyword
#:   gate admitting ``WITH`` does not weaken the read-only guarantee.)
#: * ``SHOW`` — read a runtime parameter.
#: * ``EXPLAIN`` — a query plan. (``EXPLAIN ANALYZE`` of a writing statement
#:   would execute it, but that too is refused by the read-only session.)
ALLOWED_FIRST_KEYWORDS: frozenset[str] = frozenset(
    {"SELECT", "SHOW", "EXPLAIN", "WITH", "TABLE", "VALUES"}
)

#: The per-session server settings applied to every connection. asyncpg sends
#: these as ``startup`` parameters, so ``default_transaction_read_only`` is in
#: force for the connection's whole lifetime (every implicit
#: single-statement transaction inherits it).
_SERVER_SETTINGS: dict[str, str] = {
    "default_transaction_read_only": "on",
    "application_name": "meho-backplane",
}

_KEYWORD_RE = re.compile(r"[A-Za-z_]+")


class PostgresReadOnlyError(ValueError):
    """A free-form statement's first keyword is not a read verb.

    Raised by :func:`assert_read_only_sql` before any connection is opened.
    Subclasses :class:`ValueError` so the dispatcher's ``connector_error``
    branch renders the message verbatim.
    """


def first_significant_keyword(sql: str) -> str:
    """Return the upper-cased first keyword of *sql*, skipping comments/whitespace.

    Leading whitespace, SQL line comments (``-- …`` to end of line), C-style
    block comments (``/* … */``), and wrapping ``(`` characters are stepped
    over so a query like ``/* audit */ (SELECT 1)`` resolves to ``SELECT``.
    Returns ``""`` when no alphabetic keyword is found (an empty or
    punctuation-only statement), which :func:`assert_read_only_sql` treats as
    a rejection.
    """
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace() or ch == "(":
            i += 1
            continue
        if sql.startswith("--", i):
            newline = sql.find("\n", i)
            i = n if newline == -1 else newline + 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        break
    match = _KEYWORD_RE.match(sql, i)
    return match.group(0).upper() if match else ""


def assert_read_only_sql(sql: str) -> None:
    """Raise :class:`PostgresReadOnlyError` unless *sql* begins with a read verb.

    The first significant keyword (comments and wrapping parens skipped) must
    be one of :data:`ALLOWED_FIRST_KEYWORDS`. This is the transport-free half
    of the double read-only enforcement; the server-side
    ``default_transaction_read_only`` session flag is the backstop for
    anything that begins with an allowed keyword but still tries to mutate
    (a data-modifying CTE, ``EXPLAIN ANALYZE <write>``).
    """
    keyword = first_significant_keyword(sql)
    if keyword not in ALLOWED_FIRST_KEYWORDS:
        allowed = ", ".join(sorted(ALLOWED_FIRST_KEYWORDS))
        shown = keyword or "(none)"
        raise PostgresReadOnlyError(
            f"postgres connector is read-only: statement begins with {shown!r}, "
            f"which is not an allowed read verb ({allowed}). Rephrase as a "
            "SELECT/SHOW/EXPLAIN/WITH/TABLE/VALUES query."
        )


async def connect_read_only(
    target: Any,
    operator: Operator | None,
    *,
    database: str | None = None,
    timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
) -> asyncpg.Connection:
    """Open a read-only asyncpg connection to *target*.

    ``default_transaction_read_only=on`` is set as a startup parameter so the
    session is read-only for its whole lifetime — the server-enforced half of
    the double read-only guarantee.

    Auth is optional:

    * ``target.secret_ref is None`` — trust-auth. Connect as
      :data:`DEFAULT_TRUST_USER` with no password (``pg_hba.conf`` ``trust`` /
      ``peer``). This is the net-new "execute without a secret_ref" branch;
      every other execute path fails closed on an unresolved credential.
    * ``target.secret_ref`` set — resolve ``{username, password}`` from the
      operator-context credential broker
      (:func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`),
      which reads the secret under *operator*'s identity. A credentialled
      target dispatched without an authenticated operator (``operator is
      None`` or ``operator.raw_jwt == ""``) fails closed inside the loader.

    The resolved password is passed only to :func:`asyncpg.connect`; it is
    never logged, returned, or placed on an :class:`OperationResult`.
    """
    connect_kwargs: dict[str, Any] = {
        "host": target.host,
        "port": getattr(target, "port", None) or DEFAULT_PORT,
        "database": database or DEFAULT_DATABASE,
        "timeout": timeout,
        "server_settings": _SERVER_SETTINGS,
    }

    if getattr(target, "secret_ref", None):
        if operator is None:
            # Mirror the loader's fail-closed contract with a message naming
            # the operator-context requirement rather than raising a bare
            # AttributeError deep in the loader.
            raise ValueError(
                f"postgres target {getattr(target, 'name', target)!r} has a secret_ref "
                "but no authenticated operator was supplied; a credentialled target "
                "cannot be reached on an operator-less dispatch path"
            )
        creds = await load_basic_credentials(target, operator)
        connect_kwargs["user"] = creds["username"]
        connect_kwargs["password"] = creds["password"]
    else:
        connect_kwargs["user"] = DEFAULT_TRUST_USER

    return await asyncpg.connect(**connect_kwargs)
