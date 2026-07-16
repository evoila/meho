# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MongoDbConnector -- read-only wire-protocol connector for MongoDB (#2237).

MEHO's **second wire-protocol (non-HTTP) connector**, following the DB-connector
shape the postgres connector (#2236) established: it subclasses the generic
:class:`~meho_backplane.connectors.base.Connector` ABC (not
:class:`~meho_backplane.connectors.adapters.http.HttpConnector`) and drives a
:class:`~pymongo.AsyncMongoClient` over the MongoDB wire protocol (default port
27017). Registry v2 triple ``("mongodb", "7", "mongodb-wire")``.

Design
------

* **Read-only by construction.** The connector registers a fixed set of read
  ops, each of which issues exactly one command from
  :data:`~meho_backplane.connectors.mongodb.session.MONGO_READ_COMMANDS`. There
  is no free-form command / ``eval`` / ``$where`` / aggregation-with-``$out``
  op, so read-only is guaranteed by the closed command set rather than a runtime
  gate. :func:`~meho_backplane.connectors.mongodb.session.assert_read_command`
  is the belt-and-suspenders check the query layer runs before every command.

* **Optional auth.** A target with ``secret_ref=None`` is a no-auth instance and
  connects with no credentials; a target with a ``secret_ref`` resolves
  ``{username, password}`` under the operator's identity and authenticates
  against the ``admin`` database. The password flows only into the client's
  connection params -- never a log line or an :class:`OperationResult`. See
  :func:`~meho_backplane.connectors.mongodb.session.connect_client`.

* **The DB-connector shape.** The package split (``session.py`` = wire +
  read-command allowlist, ``queries.py`` = command text + document shaping,
  ``ops.py`` = op metadata, ``connector.py`` = the thin op surface) is the same
  four-module split the postgres sibling uses, so the two wire connectors read
  in parallel.

Fingerprint reads ``buildInfo`` / ``hello`` / ``serverStatus``; probe is a
``hello`` handshake with structured failure reasons.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import structlog
from pymongo import AsyncMongoClient
from pymongo.errors import ConnectionFailure, OperationFailure, PyMongoError

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.mongodb import queries
from meho_backplane.connectors.mongodb.ops import MONGO_OPS, MONGO_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.mongodb.session import connect_client
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["MongoDbConnector"]

_log = structlog.get_logger(__name__)

# Forward declaration -- replaced with `from meho_backplane.targets import Target`
# once G0.3's Target model rollout lands, mirroring the sibling connectors.
type Target = Any

#: MongoDB auth error codes: Unauthorized (13) and AuthenticationFailed (18).
_AUTH_ERROR_CODES = frozenset({13, 18})


class MongoDbConnector(Connector):
    """Read-only MongoDB connector over the wire protocol via ``pymongo``.

    Registry v2 triple ``("mongodb", "7", "mongodb-wire")``. ``priority`` is
    ``1`` so a future ``GenericRestConnector`` auto-shim registering the same
    product loses the resolver tie-break. ``supported_version_range`` covers the
    maintained MongoDB releases; the diagnostic commands this connector reads
    are stable across them.
    """

    product = "mongodb"
    version = "7"
    impl_id = "mongodb-wire"
    supported_version_range = ">=5,<9"
    priority = 1

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _client(
        self, target: Target, operator: Operator | None
    ) -> AsyncIterator[AsyncMongoClient[dict[str, Any]]]:
        """Yield an ``AsyncMongoClient``, always closed on exit.

        Thin wrapper over
        :func:`~meho_backplane.connectors.mongodb.session.connect_client` so
        every op shares the connect (optional auth) and teardown path.
        """
        client = await connect_client(target, operator)
        try:
            yield client
        finally:
            await client.close()

    # ------------------------------------------------------------------
    # Curated read ops -- each threads ``operator`` for the (optional) auth read
    # ------------------------------------------------------------------

    async def list_databases(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mongodb.databases`` -- databases with on-disk sizes."""
        del params  # declared empty in schema
        async with self._client(target, operator) as client:
            return await queries.fetch_databases(client)

    async def list_collections(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mongodb.collections`` -- collections in a database."""
        async with self._client(target, operator) as client:
            return await queries.fetch_collections(client, params["database"])

    async def db_stats(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mongodb.db_stats`` -- storage statistics for a database."""
        async with self._client(target, operator) as client:
            return await queries.fetch_db_stats(client, params["database"])

    async def collection_stats(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mongodb.collection_stats`` -- storage statistics for a collection."""
        async with self._client(target, operator) as client:
            return await queries.fetch_collection_stats(
                client, params["database"], params["collection"]
            )

    async def list_indexes(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mongodb.indexes`` -- indexes for a collection, incl. TTL config."""
        async with self._client(target, operator) as client:
            return await queries.fetch_indexes(client, params["database"], params["collection"])

    async def estimated_count(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mongodb.count`` -- fast metadata document count."""
        async with self._client(target, operator) as client:
            return await queries.fetch_estimated_count(
                client, params["database"], params["collection"]
            )

    async def server_status(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mongodb.server_status`` -- slim ``serverStatus`` projection."""
        del params  # declared empty in schema
        async with self._client(target, operator) as client:
            return await queries.fetch_server_status(client)

    async def replica_status(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mongodb.replica_status`` -- replica-set health + member roles."""
        del params  # declared empty in schema
        async with self._client(target, operator) as client:
            return await queries.fetch_replica_status(client)

    # ------------------------------------------------------------------
    # Fingerprint / probe
    # ------------------------------------------------------------------

    async def fingerprint(
        self, target: Target, operator: Operator | None = None
    ) -> FingerprintResult:
        """Canonical fingerprint: version, gitVersion, edition, wire version, topology.

        Connects and reads ``buildInfo`` / ``hello`` / ``serverStatus``. A
        no-auth target fingerprints with no operator; a credentialled target
        reads its secret under *operator* (``None`` fails closed inside the
        loader). Any connection or credential failure maps to ``reachable=False``
        with the error under ``extras`` rather than raising (#986 discipline).
        """
        probed_at = datetime.now(UTC)
        auth_mode = "scram" if getattr(target, "secret_ref", None) else "none"
        try:
            async with self._client(target, operator) as client:
                identity = await queries.fetch_fingerprint(client, auth_mode=auth_mode)
        except (OSError, PyMongoError, VaultCredentialsReadError, ValueError) as exc:
            _log.warning(
                "mongodb_fingerprint_unreachable",
                target=getattr(target, "name", None),
                error=f"{type(exc).__name__}: {exc}",
            )
            return FingerprintResult(
                vendor="mongodb",
                product="mongodb",
                reachable=False,
                probed_at=probed_at,
                probe_method="pymongo: buildInfo/hello",
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )

        version = identity.pop("server_version", None)
        build = identity.pop("git_version", None)
        edition = identity.pop("edition", None)
        return FingerprintResult(
            vendor="mongodb",
            product="mongodb",
            version=version,
            build=build,
            edition=edition,
            reachable=True,
            probed_at=probed_at,
            probe_method="pymongo: buildInfo/hello",
            extras=identity,
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability check via a ``hello`` handshake.

        Distinct ``reason`` values on failure:

        * ``auth_failed`` -- the credential could not be resolved
          (:class:`VaultCredentialsReadError` / :exc:`ValueError` -- a
          credentialled target on an operator-less probe) or the server rejected
          it (:exc:`~pymongo.errors.OperationFailure` with an auth error code).
        * ``tcp_unreachable`` -- server selection / TCP connect failed (host
          down, firewall, wrong port; :exc:`~pymongo.errors.ConnectionFailure`,
          which covers :exc:`~pymongo.errors.ServerSelectionTimeoutError`).
        * ``connect_failed`` -- another server-side failure
          (:exc:`~pymongo.errors.PyMongoError`).

        ``probe`` carries no operator, so a credentialled target's secret read
        runs without an authenticated operator and fails closed to
        ``auth_failed`` -- reachability of a credentialled target is confirmed on
        the operator-carrying fingerprint / op path.
        """
        start = time.monotonic()
        probed_at = datetime.now(UTC)

        def _result(ok: bool, reason: str | None) -> ProbeResult:
            return ProbeResult(
                ok=ok,
                reason=reason,
                latency_ms=(time.monotonic() - start) * 1000.0,
                probed_at=probed_at,
            )

        try:
            async with self._client(target, None) as client:
                await client.get_database("admin").command("hello")
        except (VaultCredentialsReadError, ValueError):
            return _result(False, "auth_failed")
        except OperationFailure as exc:
            reason = "auth_failed" if exc.code in _AUTH_ERROR_CODES else "connect_failed"
            return _result(False, reason)
        except ConnectionFailure:
            return _result(False, "tcp_unreachable")
        except PyMongoError:
            return _result(False, "connect_failed")
        return _result(True, None)

    # ------------------------------------------------------------------
    # Registration + dispatch shim
    # ------------------------------------------------------------------

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`MONGO_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan (via the registrar queued in
        :mod:`meho_backplane.connectors.mongodb.__init__`) after the registry has
        eager-imported every connector module. Idempotent across pod restarts,
        mirroring the postgres / loki shape.
        """
        from meho_backplane.operations.typed_register import register_typed_operation

        for op in MONGO_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"MongoDbConnector op {op.op_id!r} declares handler_attr="
                    f"{op.handler_attr!r} but the class has no such attribute"
                )
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = MONGO_WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"MongoDbConnector op {op.op_id!r} declares group_key="
                        f"{op.group_key!r} but no curated when_to_use exists for that key. "
                        "Add an entry to MONGO_WHEN_TO_USE_BY_GROUP in mongodb/ops.py."
                    )
            await register_typed_operation(
                product=cls.product,
                version=cls.version,
                impl_id=cls.impl_id,
                op_id=op.op_id,
                handler=handler,
                summary=op.summary,
                description=op.description,
                parameter_schema=op.parameter_schema,
                response_schema=op.response_schema,
                group_key=op.group_key,
                when_to_use=when_to_use,
                tags=list(op.tags),
                safety_level=op.safety_level,
                requires_approval=op.requires_approval,
                llm_instructions=op.llm_instructions,
            )
        _log.info(
            "mongodb_operations_registered",
            count=len(MONGO_OPS),
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
        )

    async def execute(self, target: Target, op_id: str, params: dict[str, Any]) -> OperationResult:
        """Legacy shim -- delegates to the G0.6 dispatcher.

        Mirrors :meth:`PostgresConnector.execute`. Post-G0.6 callers
        (``/api/v1/operations/call``, MCP ``call_operation``, the CLI verbs)
        construct a real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly. The connector's
        natural key encodes as ``"mongodb-wire-7"`` per ``parse_connector_id``.
        The synthetic operator carries ``raw_jwt=""``, so a credentialled target
        reached through this shim fails closed in the credential loader -- the
        operator-context path is the real dispatch surface.
        """
        from uuid import UUID

        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:mongodb-wire-connector-shim",
            name=None,
            email=None,
            raw_jwt="",
            tenant_id=UUID(int=0),
            tenant_role=TenantRole.OPERATOR,
        )
        connector_id = f"{self.impl_id}-{self.version}"
        return await dispatch(
            operator=operator,
            connector_id=connector_id,
            op_id=op_id,
            target=target,
            params=params,
        )
