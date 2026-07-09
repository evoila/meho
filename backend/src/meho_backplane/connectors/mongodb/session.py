# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Wire-session plumbing + read-command allowlist for the MongoDB connector (#2237).

MEHO's second wire-protocol (non-HTTP) connector, mirroring the postgres shape
(#2236). This module owns the two concerns that keep the connector safe by
construction, kept out of ``connector.py`` so the connector body stays a thin
op surface:

* **The read-command allowlist** — :data:`MONGO_READ_COMMANDS` is the closed set
  of MongoDB database commands the connector will ever issue. Unlike a SQL
  database there is no free-form query surface here at all: every op maps to
  exactly one fixed read command (``listDatabases`` / ``listCollections`` /
  ``dbStats`` / ``collStats`` / ``listIndexes`` / ``count`` / ``serverStatus`` /
  ``buildInfo`` / ``hello`` / ``replSetGetStatus``). There is **no**
  arbitrary-command / ``eval`` / ``$where`` / aggregation passthrough, so
  read-only is guaranteed by the fixed command set rather than by a runtime gate.
  :func:`assert_read_command` is the belt-and-suspenders check the query layer
  runs before dispatching any command name to the wire.

* **The client factory** — :func:`connect_client` opens an
  :class:`~pymongo.AsyncMongoClient` with ``directConnection=True`` (a single
  node is triaged directly, not resolved into a replica-set topology) and a
  bounded server-selection timeout. Auth is optional: a ``secret_ref=None``
  target connects with no credentials (a no-auth instance); a target with a
  ``secret_ref`` resolves ``{username, password}`` from the operator-context
  credential broker and authenticates against the ``admin`` database. The
  resolved password flows only into the client's connection params — never a
  log line or an :class:`OperationResult`.
"""

from __future__ import annotations

from typing import Any

from pymongo import AsyncMongoClient

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import load_basic_credentials

__all__ = [
    "APP_NAME",
    "DEFAULT_AUTH_SOURCE",
    "DEFAULT_PORT",
    "DEFAULT_SERVER_SELECTION_TIMEOUT_MS",
    "MONGO_READ_COMMANDS",
    "MongoReadOnlyError",
    "assert_read_command",
    "connect_client",
]

#: The MongoDB wire-protocol default port.
DEFAULT_PORT = 27017

#: The database a credentialled target authenticates against. MongoDB stores
#: SCRAM users under the ``admin`` database by convention (and a target model
#: carries no per-target auth-source field), so a credentialled Mongo target
#: authenticates there. An operator whose user lives in another auth database
#: is out of scope for this read-only connector.
DEFAULT_AUTH_SOURCE = "admin"

#: Server-selection + connect timeout (milliseconds) — bounds a hung topology
#: scan / TCP handshake so a probe or op fails fast rather than hanging the
#: dispatcher. pymongo's default is 30 s, which is too long for an interactive
#: triage surface.
DEFAULT_SERVER_SELECTION_TIMEOUT_MS = 5000

#: ``appname`` sent on the connection handshake — surfaces in the server's
#: ``currentOp`` / log lines so an operator can attribute MEHO's reads.
APP_NAME = "meho-backplane"

#: The closed set of MongoDB database commands this connector issues. Every op
#: maps to exactly one of these fixed reads; there is no generic
#: command / eval / aggregation passthrough, so read-only is guaranteed by the
#: command set itself. ``$where`` and aggregation-with-``$out`` never appear
#: because no op accepts a caller-supplied command name or pipeline.
MONGO_READ_COMMANDS: frozenset[str] = frozenset(
    {
        "listDatabases",
        "listCollections",
        "dbStats",
        "collStats",
        "listIndexes",
        "count",
        "serverStatus",
        "buildInfo",
        "hello",
        "replSetGetStatus",
    }
)


class MongoReadOnlyError(ValueError):
    """A command name outside :data:`MONGO_READ_COMMANDS` was about to be issued.

    Raised by :func:`assert_read_command`. In normal operation this is
    unreachable — the connector never routes a caller-supplied command name to
    the wire — so the exception is a defensive invariant guarding against a
    future op wiring a command that is not on the read allowlist. Subclasses
    :class:`ValueError` so the dispatcher's ``connector_error`` branch renders
    the message verbatim.
    """


def assert_read_command(command: str) -> None:
    """Raise :class:`MongoReadOnlyError` unless *command* is on the read allowlist.

    The belt-and-suspenders half of the read-only guarantee: the closed op set
    already prevents an arbitrary command from being dispatched, and this check
    fails closed if a future op is wired to a command not in
    :data:`MONGO_READ_COMMANDS`.
    """
    if command not in MONGO_READ_COMMANDS:
        allowed = ", ".join(sorted(MONGO_READ_COMMANDS))
        raise MongoReadOnlyError(
            f"mongodb connector is read-only: command {command!r} is not on the "
            f"read allowlist ({allowed}). This connector issues only fixed read "
            "commands; there is no arbitrary-command / eval passthrough."
        )


async def connect_client(
    target: Any,
    operator: Operator | None,
    *,
    timeout_ms: int = DEFAULT_SERVER_SELECTION_TIMEOUT_MS,
) -> AsyncMongoClient[dict[str, Any]]:
    """Open an :class:`~pymongo.AsyncMongoClient` to *target*.

    ``directConnection=True`` triages the single addressed node rather than
    resolving a replica-set topology from it, so a read is served by exactly the
    member the operator named (a secondary is not silently redirected to the
    primary). The client is lazy — no I/O happens until the first command — so
    the credential read is the only awaited work here.

    Auth is optional:

    * ``target.secret_ref is None`` — a no-auth instance. Connect with no
      credentials. This is the net-new "execute without a secret_ref" branch;
      every other execute path fails closed on an unresolved credential.
    * ``target.secret_ref`` set — resolve ``{username, password}`` from the
      operator-context credential broker
      (:func:`~meho_backplane.connectors._shared.vault_creds.load_basic_credentials`),
      which reads the secret under *operator*'s identity, and authenticate
      against :data:`DEFAULT_AUTH_SOURCE`. A credentialled target dispatched
      without an authenticated operator (``operator is None`` or
      ``operator.raw_jwt == ""``) fails closed inside the loader.

    The resolved password is passed only to the client constructor; it is never
    logged, returned, or placed on an :class:`OperationResult`.
    """
    connect_kwargs: dict[str, Any] = {
        "host": target.host,
        "port": getattr(target, "port", None) or DEFAULT_PORT,
        "serverSelectionTimeoutMS": timeout_ms,
        "connectTimeoutMS": timeout_ms,
        "directConnection": True,
        "appname": APP_NAME,
    }

    if getattr(target, "secret_ref", None):
        if operator is None:
            # Mirror the loader's fail-closed contract with a message naming the
            # operator-context requirement rather than raising a bare
            # AttributeError deep in the loader.
            raise ValueError(
                f"mongodb target {getattr(target, 'name', target)!r} has a secret_ref "
                "but no authenticated operator was supplied; a credentialled target "
                "cannot be reached on an operator-less dispatch path"
            )
        creds = await load_basic_credentials(target, operator)
        connect_kwargs["username"] = creds["username"]
        connect_kwargs["password"] = creds["password"]
        connect_kwargs["authSource"] = DEFAULT_AUTH_SOURCE

    return AsyncMongoClient(**connect_kwargs)
