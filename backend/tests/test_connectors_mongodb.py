# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the read-only MongoDB wire-protocol connector (#2237).

Coverage matrix (per Task #2237 acceptance criteria):

* **Registration** -- ``mongodb`` resolves via ``register_connector_v2``
  (versioned triple + wildcard), appears in ``all_connectors_v2()`` and
  ``registered_product_tokens()``; every op is safe/read-only/no-approval with a
  closed schema.
* **Fixed command set** -- the connector issues only commands from
  ``MONGO_READ_COMMANDS``; there is no arbitrary-command / eval op, and
  :func:`assert_read_command` rejects any command off the allowlist.
* **Optional auth** -- a ``secret_ref=None`` (no-auth) target connects with no
  credentials; a credentialled target passes the password to the client and it
  never appears in logs (``capture_logs`` assertion).
* **Op payloads** -- ``list_indexes`` surfaces TTL ``expireAfterSeconds``;
  ``fingerprint`` returns version/edition/wire-version/storage-engine plus
  replica-set name + member roles (recorded fixture).
* **Live dispatch** -- the connector registers, an unknown op returns the
  ``unknown_op`` envelope, a curated op dispatches end-to-end, and a missing
  required param returns ``invalid_params``.

The wire is faked with an in-memory client double; the in-process Vault fake
exercises the real credential loader. Mirrors
:mod:`tests.test_connectors_postgres`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
import structlog
from pymongo.errors import (
    ConnectionFailure,
    OperationFailure,
    ServerSelectionTimeoutError,
)
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.mongodb import (
    MONGO_OPS,
    MONGO_READ_COMMANDS,
    MongoDbConnector,
    MongoReadOnlyError,
    assert_read_command,
)
from meho_backplane.connectors.mongodb import connector as connector_module
from meho_backplane.connectors.mongodb import session as session_module
from meho_backplane.connectors.mongodb.session import (
    DEFAULT_AUTH_SOURCE,
    DEFAULT_PORT,
    connect_client,
)
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
    registered_product_tokens,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_PRODUCT = "mongodb"
_VERSION = "7"
_IMPL_ID = "mongodb-wire"
_CONNECTOR_ID = "mongodb-wire-7"

_MONGO_HOST = "mongo.test.invalid"
_MONGO_PORT = 27017

#: A clearly-fake password that must never reach a log line.
_CANARY_PASSWORD = "mongo-canary-must-not-leak-12345"  # trufflehog:ignore


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
    register_connector_v2(
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=MongoDbConnector
    )
    register_connector_v2(product=_PRODUCT, version="", impl_id="", cls=MongoDbConnector)
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


class _MongoTarget:
    """Target satisfying both the connector shape and the resolver shape."""

    def __init__(
        self,
        *,
        secret_ref: str | None = None,
        host: str = _MONGO_HOST,
        port: int | None = _MONGO_PORT,
    ) -> None:
        self.product = _PRODUCT
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000e0")
        self.name = "mongo-reads"
        self.host = host
        self.port = port
        self.secret_ref = secret_ref
        self.auth_model = None
        self.verify_tls = True
        self.tls_ca_pin = None
        self.tls_server_name = None
        self.extras: dict[str, Any] = {}


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-reads-mongo",
        name="Mongo Reads Operator",
        email=None,
        raw_jwt="op.reads.mongo.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000e4"),
        tenant_role=TenantRole.OPERATOR,
    )


class _AsyncCursor:
    """Minimal async-iterable stand-in for pymongo's AsyncCommandCursor."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        for doc in self._docs:
            yield doc


class _FakeCollection:
    def __init__(self, *, indexes: list[dict[str, Any]] | None = None, count: int = 0) -> None:
        self._indexes = indexes or []
        self._count = count

    async def list_indexes(self) -> _AsyncCursor:
        return _AsyncCursor(self._indexes)

    async def estimated_document_count(self) -> int:
        return self._count


class _FakeDb:
    """Minimal AsyncDatabase double: canned command responses + a collection."""

    def __init__(
        self,
        *,
        command_responses: dict[str, Any] | None = None,
        collections: list[dict[str, Any]] | None = None,
        collection: _FakeCollection | None = None,
    ) -> None:
        self._responses = command_responses or {}
        self._collections = collections or []
        self._collection = collection or _FakeCollection()
        self.commands: list[str] = []

    async def command(self, command: Any, value: Any = 1, **kwargs: Any) -> dict[str, Any]:
        name = command if isinstance(command, str) else next(iter(command))
        self.commands.append(name)
        resp = self._responses.get(name)
        if isinstance(resp, Exception):
            raise resp
        if resp is None:
            return {"ok": 1.0}
        return resp

    async def list_collections(self) -> _AsyncCursor:
        return _AsyncCursor(self._collections)

    def get_collection(self, name: str) -> _FakeCollection:
        return self._collection


class _FakeClient:
    """Minimal AsyncMongoClient double routing get_database to fake dbs."""

    def __init__(
        self, *, databases: dict[str, _FakeDb] | None = None, default: _FakeDb | None = None
    ) -> None:
        self._databases = databases or {}
        self._default = default or _FakeDb()
        self.closed = False

    def get_database(self, name: str) -> _FakeDb:
        return self._databases.get(name, self._default)

    async def close(self) -> None:
        self.closed = True


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> AsyncMock:
    """Patch the connector's ``connect_client`` to yield *client*."""
    mock = AsyncMock(return_value=client)
    monkeypatch.setattr(connector_module, "connect_client", mock)
    return mock


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_mongodb_resolves_versioned_and_wildcard_and_appears_in_registry() -> None:
    """AC: mongodb resolves via register_connector_v2 (versioned + wildcard)."""
    registry = all_connectors_v2()
    assert registry[("mongodb", "7", "mongodb-wire")] is MongoDbConnector
    assert registry[("mongodb", "", "")] is MongoDbConnector
    assert "mongodb" in registered_product_tokens()

    assert resolve_connector(_MongoTarget()) is MongoDbConnector
    fresh = _MongoTarget()
    fresh.fingerprint = type("_FP", (), {"version": None})()
    assert resolve_connector(fresh) is MongoDbConnector


def test_every_op_is_safe_read_only_with_closed_schema() -> None:
    """AC: no write op -- every registered op is safe/read-only/no-approval."""
    assert {op.op_id for op in MONGO_OPS} == {
        "mongodb.databases",
        "mongodb.collections",
        "mongodb.db_stats",
        "mongodb.collection_stats",
        "mongodb.indexes",
        "mongodb.count",
        "mongodb.server_status",
        "mongodb.replica_status",
    }
    for op in MONGO_OPS:
        assert op.safety_level == "safe", op.op_id
        assert op.requires_approval is False, op.op_id
        assert "read-only" in op.tags, op.op_id
        assert op.parameter_schema.get("additionalProperties") is False, op.op_id


# ---------------------------------------------------------------------------
# Fixed command set -- read-only by construction (AC #3)
# ---------------------------------------------------------------------------


def test_read_command_allowlist_is_the_closed_fixed_set() -> None:
    """AC: only the fixed command set is dispatchable (assert the allowlist)."""
    assert (
        frozenset(
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
        == MONGO_READ_COMMANDS
    )


@pytest.mark.parametrize(
    "command",
    ["eval", "aggregate", "find", "insert", "update", "delete", "$where", "mapReduce", ""],
)
def test_assert_read_command_rejects_off_allowlist(command: str) -> None:
    """A command outside the read allowlist (incl. eval/aggregate) is refused."""
    with pytest.raises(MongoReadOnlyError):
        assert_read_command(command)


def test_no_op_exposes_a_free_form_command_or_pipeline_param() -> None:
    """AC: there is no arbitrary-command / eval op.

    No op's parameter schema accepts a caller-supplied command name, aggregation
    pipeline, filter, or eval body -- the only params are database / collection
    selectors, so read-only cannot be subverted through op params.
    """
    forbidden = {"command", "pipeline", "filter", "query", "eval", "code", "$where", "aggregate"}
    for op in MONGO_OPS:
        props = set(op.parameter_schema.get("properties", {}))
        assert props <= {"database", "collection"}, op.op_id
        assert not (props & forbidden), op.op_id


# ---------------------------------------------------------------------------
# Optional auth (connect_client)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_no_auth_sends_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: a secret_ref=None (no-auth) target connects with no credentials."""
    fake_ctor = MagicMock(return_value=_FakeClient())
    monkeypatch.setattr(session_module, "AsyncMongoClient", fake_ctor)

    await connect_client(_MongoTarget(secret_ref=None), None)

    kwargs = fake_ctor.call_args.kwargs
    assert "username" not in kwargs
    assert "password" not in kwargs
    assert kwargs["port"] == DEFAULT_PORT
    assert kwargs["directConnection"] is True


@pytest.mark.asyncio
async def test_connect_credentialled_passes_password_and_never_logs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: a credentialled target passes the password to the client, unlogged."""
    fake_ctor = MagicMock(return_value=_FakeClient())
    monkeypatch.setattr(session_module, "AsyncMongoClient", fake_ctor)
    install_fake_client(monkeypatch, secret={"username": "mongo_ro", "password": _CANARY_PASSWORD})

    with structlog.testing.capture_logs() as logs:
        await connect_client(_MongoTarget(secret_ref="targets/op-reads/mongo"), _make_operator())

    kwargs = fake_ctor.call_args.kwargs
    assert kwargs["username"] == "mongo_ro"
    assert kwargs["password"] == _CANARY_PASSWORD
    assert kwargs["authSource"] == DEFAULT_AUTH_SOURCE

    # The password must not appear anywhere in the captured structured logs.
    assert _CANARY_PASSWORD not in repr(logs)


@pytest.mark.asyncio
async def test_connect_credentialled_without_operator_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credentialled target on an operator-less path fails closed (no connect)."""
    fake_ctor = MagicMock(return_value=_FakeClient())
    monkeypatch.setattr(session_module, "AsyncMongoClient", fake_ctor)

    with pytest.raises(ValueError, match="no authenticated operator"):
        await connect_client(_MongoTarget(secret_ref="targets/x/mongo"), None)
    fake_ctor.assert_not_called()


# ---------------------------------------------------------------------------
# Op payloads -- document shaping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_indexes_surfaces_ttl_expire_after_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: getIndexes surfaces TTL expireAfterSeconds."""
    coll = _FakeCollection(
        indexes=[
            {"v": 2, "key": {"_id": 1}, "name": "_id_"},
            {"v": 2, "key": {"createdAt": 1}, "name": "ttl_idx", "expireAfterSeconds": 3600},
        ]
    )
    client = _FakeClient(databases={"app": _FakeDb(collection=coll)})
    _patch_client(monkeypatch, client)

    result = await MongoDbConnector().list_indexes(
        _make_operator(), _MongoTarget(), {"database": "app", "collection": "events"}
    )
    ttl = next(i for i in result["indexes"] if i["name"] == "ttl_idx")
    assert ttl["expireAfterSeconds"] == 3600
    assert result["collection"] == "events"
    assert client.closed is True


@pytest.mark.asyncio
async def test_server_status_is_slim_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    """serverStatus returns only the curated slim fields, not heavy sections."""
    admin = _FakeDb(
        command_responses={
            "serverStatus": {
                "host": "mongo1:27017",
                "version": "7.0.5",
                "connections": {"current": 3, "available": 997},
                "opcounters": {"query": 100, "insert": 0},
                "wiredTiger": {"huge": "x" * 1000},  # must be dropped
                "ok": 1.0,
            }
        }
    )
    client = _FakeClient(default=admin)
    _patch_client(monkeypatch, client)

    result = await MongoDbConnector().server_status(_make_operator(), _MongoTarget(), {})
    slim = result["server_status"]
    assert slim["version"] == "7.0.5"
    assert slim["connections"]["current"] == 3
    assert "wiredTiger" not in slim


@pytest.mark.asyncio
async def test_replica_status_standalone_reports_not_replica_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a standalone (no setName), hello alone reports is_replica_set=False."""
    admin = _FakeDb(command_responses={"hello": {"isWritablePrimary": True, "ok": 1.0}})
    client = _FakeClient(default=admin)
    _patch_client(monkeypatch, client)

    result = await MongoDbConnector().replica_status(_make_operator(), _MongoTarget(), {})
    assert result["is_replica_set"] is False
    assert result["set_name"] is None
    assert result["repl_set_status"] is None
    # replSetGetStatus is not issued on a standalone.
    assert "replSetGetStatus" not in admin.commands


@pytest.mark.asyncio
async def test_replica_status_replica_set_returns_member_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replica set surfaces member roles from hello + replSetGetStatus."""
    admin = _FakeDb(
        command_responses={
            "hello": {
                "isWritablePrimary": True,
                "setName": "rs0",
                "primary": "m1:27017",
                "me": "m1:27017",
                "hosts": ["m1:27017", "m2:27017"],
            },
            "replSetGetStatus": {
                "set": "rs0",
                "members": [
                    {"name": "m1:27017", "stateStr": "PRIMARY", "health": 1, "uptime": 500},
                    {"name": "m2:27017", "stateStr": "SECONDARY", "health": 1, "uptime": 490},
                ],
            },
        }
    )
    client = _FakeClient(default=admin)
    _patch_client(monkeypatch, client)

    result = await MongoDbConnector().replica_status(_make_operator(), _MongoTarget(), {})
    assert result["is_replica_set"] is True
    assert result["set_name"] == "rs0"
    assert result["primary"] == "m1:27017"
    roles = {m["host"]: m["role"] for m in result["members"]}
    assert roles == {"m1:27017": "primary", "m2:27017": "secondary"}
    assert result["repl_set_status"]["members"][0]["state"] == "PRIMARY"


@pytest.mark.asyncio
async def test_fingerprint_returns_identity_and_member_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: fingerprint returns version/edition/wire + replica-set member roles."""
    admin = _FakeDb(
        command_responses={
            "buildInfo": {
                "version": "7.0.5",
                "gitVersion": "abc123",
                "modules": ["enterprise"],
            },
            "hello": {
                "maxWireVersion": 21,
                "minWireVersion": 0,
                "setName": "rs0",
                "primary": "m1:27017",
                "hosts": ["m1:27017", "m2:27017"],
            },
            "serverStatus": {"storageEngine": {"name": "wiredTiger"}, "ok": 1.0},
        }
    )
    client = _FakeClient(default=admin)
    _patch_client(monkeypatch, client)

    fp = await MongoDbConnector().fingerprint(
        _MongoTarget(secret_ref="targets/x"), _make_operator()
    )
    assert fp.reachable is True
    assert fp.vendor == "mongodb"
    assert fp.product == "mongodb"
    assert fp.version == "7.0.5"
    assert fp.build == "abc123"
    assert fp.edition == "enterprise"
    assert fp.extras["max_wire_version"] == 21
    assert fp.extras["auth_mode"] == "scram"
    assert fp.extras["storage_engine"] == "wiredTiger"
    assert fp.extras["replica_set"] == "rs0"
    roles = {m["host"]: m["role"] for m in fp.extras["members"]}
    assert roles == {"m1:27017": "primary", "m2:27017": "secondary"}


@pytest.mark.asyncio
async def test_fingerprint_unreachable_maps_to_not_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect failure maps to reachable=False with the error under extras."""
    monkeypatch.setattr(
        connector_module,
        "connect_client",
        AsyncMock(side_effect=ServerSelectionTimeoutError("no server")),
    )
    fp = await MongoDbConnector().fingerprint(_MongoTarget(), _make_operator())
    assert fp.reachable is False
    assert "error" in fp.extras


# ---------------------------------------------------------------------------
# Probe reasons
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable no-auth target with a working hello probes ok."""
    admin = _FakeDb(command_responses={"hello": {"isWritablePrimary": True, "ok": 1.0}})
    _patch_client(monkeypatch, _FakeClient(default=admin))
    result = await MongoDbConnector().probe(_MongoTarget(secret_ref=None))
    assert result.ok is True
    assert result.reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (OperationFailure("auth", code=18), "auth_failed"),
        (ServerSelectionTimeoutError("no route"), "tcp_unreachable"),
        (ConnectionFailure("reset"), "tcp_unreachable"),
        (OperationFailure("boom", code=999), "connect_failed"),
    ],
)
async def test_probe_failure_reasons(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, reason: str
) -> None:
    """Each connect/command failure maps to its distinct probe reason."""
    monkeypatch.setattr(connector_module, "connect_client", AsyncMock(side_effect=exc))
    result = await MongoDbConnector().probe(_MongoTarget(secret_ref=None))
    assert result.ok is False
    assert result.reason == reason


@pytest.mark.asyncio
async def test_probe_credentialled_without_operator_is_auth_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credentialled target on the operator-less probe path fails auth_failed."""

    async def _raise(target: Any, operator: Any, **_kw: Any) -> Any:
        raise ValueError("no authenticated operator was supplied")

    monkeypatch.setattr(connector_module, "connect_client", _raise)
    result = await MongoDbConnector().probe(_MongoTarget(secret_ref="targets/x"))
    assert result.ok is False
    assert result.reason == "auth_failed"


# ---------------------------------------------------------------------------
# Live dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_op_returns_unknown_op_envelope(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """After registration, an unknown op_id returns the unknown_op envelope."""
    await MongoDbConnector.register_operations()
    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="mongodb.nonexistent",
        target=_MongoTarget(secret_ref=None),
        params={},
    )
    assert result.status == "error"
    assert result.error.startswith("unknown_op")


@pytest.mark.asyncio
async def test_databases_op_dispatches_live_and_returns_payload(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: a curated op dispatches end-to-end and returns its payload."""
    await MongoDbConnector.register_operations()
    admin = _FakeDb(
        command_responses={
            "listDatabases": {
                "databases": [{"name": "app", "sizeOnDisk": 999, "empty": False}],
                "totalSize": 999,
            }
        }
    )
    client = _FakeClient(default=admin)
    _patch_client(monkeypatch, client)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="mongodb.databases",
        target=_MongoTarget(secret_ref=None),
        params={},
    )
    assert result.status == "ok", result.error
    assert result.result == {
        "databases": [{"name": "app", "sizeOnDisk": 999, "empty": False}],
        "total_size_bytes": 999,
    }
    assert client.closed is True


@pytest.mark.asyncio
async def test_collections_op_missing_database_returns_invalid_params(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """mongodb.collections without the required 'database' param is rejected."""
    await MongoDbConnector.register_operations()
    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="mongodb.collections",
        target=_MongoTarget(secret_ref=None),
        params={},
    )
    assert result.status == "error"
    assert result.error.startswith("invalid_params")
