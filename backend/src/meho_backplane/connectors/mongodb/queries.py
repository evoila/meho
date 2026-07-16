# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Command text + document-shaping for the read-only MongoDB connector (#2237).

Every function here issues one fixed read command (from
:data:`~meho_backplane.connectors.mongodb.session.MONGO_READ_COMMANDS`) against
an already-open :class:`~pymongo.AsyncMongoClient`, and returns a
JSON-serialisable dict — the connector methods own the connect/close lifecycle
so this module stays a pure command surface (easy to read against the MongoDB
command reference and to unit-test with a fake client).

Command facts are pinned to the MongoDB manual:
``listDatabases`` / ``listCollections`` / ``dbStats`` / ``collStats`` /
``listIndexes`` / ``serverStatus`` / ``buildInfo`` / ``hello`` /
``replSetGetStatus`` (https://www.mongodb.com/docs/manual/reference/command/).
"""

from __future__ import annotations

import datetime as _dt
import uuid
from collections.abc import Mapping
from typing import Any

from bson import Binary, Decimal128, ObjectId, Timestamp
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import OperationFailure

from meho_backplane.connectors.mongodb.session import DEFAULT_AUTH_SOURCE, assert_read_command

__all__ = [
    "NO_REPLICATION_ENABLED_CODE",
    "SERVER_STATUS_SLIM_FIELDS",
    "SERVER_STATUS_SUPPRESSED_SECTIONS",
    "fetch_collection_stats",
    "fetch_collections",
    "fetch_databases",
    "fetch_db_stats",
    "fetch_estimated_count",
    "fetch_fingerprint",
    "fetch_indexes",
    "fetch_replica_status",
    "fetch_server_status",
]

#: ``replSetGetStatus`` returns this error code on a standalone (non-replica-set)
#: server. Treated as "not a replica set" rather than an error.
NO_REPLICATION_ENABLED_CODE = 76

#: Heavy ``serverStatus`` sections suppressed over the wire. Passing a top-level
#: section name with value ``0`` tells the server to omit it, so the response
#: stays a slim triage payload instead of the multi-hundred-KB full document
#: (the ``wiredTiger`` and ``metrics`` sections dominate the size). Unknown
#: section names are ignored by the server, so this is version-tolerant.
SERVER_STATUS_SUPPRESSED_SECTIONS: tuple[str, ...] = (
    "wiredTiger",
    "metrics",
    "tcmalloc",
    "locks",
    "transactions",
    "electionMetrics",
    "mirroredReads",
    "latchAnalysis",
)

#: The curated top-level ``serverStatus`` fields the slim projection keeps — the
#: ones an operator reaches for first when triaging a Mongo instance.
SERVER_STATUS_SLIM_FIELDS: tuple[str, ...] = (
    "host",
    "version",
    "process",
    "pid",
    "uptime",
    "uptimeMillis",
    "localTime",
    "connections",
    "network",
    "opcounters",
    "opcountersRepl",
    "mem",
    "storageEngine",
    "repl",
    "ok",
)


def _jsonable(value: Any) -> Any:
    """Coerce a BSON scalar to a JSON-serialisable primitive.

    pymongo maps BSON types to rich Python objects (``ObjectId``, ``datetime``,
    ``Decimal128``, ``Timestamp``, ``Binary``/``bytes``, ``UUID``). The
    dispatcher wraps a handler's return value into an :class:`OperationResult`
    whose ``result`` must be JSON-serialisable, so this normaliser runs over
    every document value: temporals become ISO-8601 strings, ``Decimal128``
    becomes its string form, and the remaining rich types become their
    canonical string.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal128):
        return str(value.to_decimal())
    if isinstance(value, Timestamp):
        return {"t": value.time, "i": value.inc}
    if isinstance(value, (ObjectId, uuid.UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview, Binary)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return str(value)


def _doc(document: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a whole BSON document to a JSON-serialisable dict."""
    return {str(key): _jsonable(val) for key, val in document.items()}


def _admin(client: AsyncMongoClient[dict[str, Any]]) -> AsyncDatabase[dict[str, Any]]:
    """Return the ``admin`` database handle (cluster-wide commands run here)."""
    return client.get_database(DEFAULT_AUTH_SOURCE)


async def fetch_databases(client: AsyncMongoClient[dict[str, Any]]) -> dict[str, Any]:
    """List databases with on-disk sizes via ``listDatabases``."""
    assert_read_command("listDatabases")
    result = await _admin(client).command({"listDatabases": 1})
    return {
        "databases": [_doc(db) for db in result.get("databases", [])],
        "total_size_bytes": _jsonable(result.get("totalSize")),
    }


async def fetch_collections(
    client: AsyncMongoClient[dict[str, Any]], database: str
) -> dict[str, Any]:
    """List collections in *database* with type + options via ``listCollections``."""
    assert_read_command("listCollections")
    cursor = await client.get_database(database).list_collections()
    collections = [_doc(info) async for info in cursor]
    return {"database": database, "collections": collections}


async def fetch_db_stats(client: AsyncMongoClient[dict[str, Any]], database: str) -> dict[str, Any]:
    """Return storage statistics for *database* via ``dbStats``."""
    assert_read_command("dbStats")
    stats = await client.get_database(database).command("dbStats")
    return {"database": database, "stats": _doc(stats)}


async def fetch_collection_stats(
    client: AsyncMongoClient[dict[str, Any]], database: str, collection: str
) -> dict[str, Any]:
    """Return storage statistics for *collection* via ``collStats``.

    ``collStats`` is deprecated as a diagnostic command since MongoDB 6.2 but is
    still served through MongoDB 8.x; it is the single-round-trip way to read a
    collection's size / storage-size / index sizes without the ``$collStats``
    aggregation stage's extra shaping.
    """
    assert_read_command("collStats")
    stats = await client.get_database(database).command("collStats", collection)
    return {"database": database, "collection": collection, "stats": _doc(stats)}


async def fetch_indexes(
    client: AsyncMongoClient[dict[str, Any]], database: str, collection: str
) -> dict[str, Any]:
    """List indexes for *collection* via ``listIndexes``, surfacing TTL config.

    Each index document carries its ``key`` spec, ``name``, uniqueness, and —
    for a TTL index — the ``expireAfterSeconds`` value, so an operator can see
    which collections expire documents and after how long.
    """
    assert_read_command("listIndexes")
    cursor = await client.get_database(database).get_collection(collection).list_indexes()
    indexes = [_doc(index) async for index in cursor]
    return {"database": database, "collection": collection, "indexes": indexes}


async def fetch_estimated_count(
    client: AsyncMongoClient[dict[str, Any]], database: str, collection: str
) -> dict[str, Any]:
    """Return the fast metadata-based document count via ``estimatedDocumentCount``.

    Uses the collection's metadata (the ``count`` command) rather than a full
    scan, so it is O(1) on WiredTiger and safe to run on a large collection.
    """
    assert_read_command("count")
    count = (
        await client.get_database(database).get_collection(collection).estimated_document_count()
    )
    return {"database": database, "collection": collection, "estimated_count": int(count)}


async def fetch_server_status(client: AsyncMongoClient[dict[str, Any]]) -> dict[str, Any]:
    """Return a slim ``serverStatus`` projection.

    Heavy sections (:data:`SERVER_STATUS_SUPPRESSED_SECTIONS`) are suppressed
    over the wire, then the response is projected down to
    :data:`SERVER_STATUS_SLIM_FIELDS` — the connections / network / opcounters /
    memory / storage-engine / replication summary an operator triages first,
    without the multi-hundred-KB internal-metrics payload.
    """
    assert_read_command("serverStatus")
    command: dict[str, Any] = {"serverStatus": 1}
    for section in SERVER_STATUS_SUPPRESSED_SECTIONS:
        command[section] = 0
    status = await _admin(client).command(command)
    slim = {
        field: _jsonable(status[field]) for field in SERVER_STATUS_SLIM_FIELDS if field in status
    }
    return {"server_status": slim}


async def _hello(client: AsyncMongoClient[dict[str, Any]]) -> dict[str, Any]:
    """Run the ``hello`` handshake command (the modern ``isMaster`` replacement)."""
    assert_read_command("hello")
    return await _admin(client).command("hello")


def _members_from_hello(hello: dict[str, Any]) -> list[dict[str, str]]:
    """Derive per-host member roles from a ``hello`` response.

    ``hello`` reports the replica-set membership as ``hosts`` (voting
    data-bearing members), ``passives`` (priority-0 data-bearing members), and
    ``arbiters``, plus the current ``primary``. Classify each host into its role
    so ``fingerprint`` can surface member roles without the elevated
    ``replSetGetStatus`` privilege.
    """
    primary = hello.get("primary")
    members: list[dict[str, str]] = []
    for host in hello.get("hosts", []) or []:
        members.append({"host": host, "role": "primary" if host == primary else "secondary"})
    for host in hello.get("passives", []) or []:
        members.append({"host": host, "role": "passive"})
    for host in hello.get("arbiters", []) or []:
        members.append({"host": host, "role": "arbiter"})
    return members


async def fetch_replica_status(client: AsyncMongoClient[dict[str, Any]]) -> dict[str, Any]:
    """Return replica-set health from ``hello`` + ``replSetGetStatus``.

    ``hello`` always answers (it needs no auth and works on a standalone), so it
    is the reachable baseline: set name, primary, and derived member roles.
    ``replSetGetStatus`` adds each member's live ``stateStr`` / health /
    replication lag, but is only meaningful on a replica set and requires the
    ``clusterMonitor`` privilege; on a standalone it raises
    :data:`NO_REPLICATION_ENABLED_CODE` and the connector reports
    ``is_replica_set=False`` rather than surfacing an error.
    """
    hello = await _hello(client)
    set_name = hello.get("setName")
    payload: dict[str, Any] = {
        "is_replica_set": set_name is not None,
        "set_name": set_name,
        "primary": hello.get("primary"),
        "me": hello.get("me"),
        "members": _members_from_hello(hello),
        "repl_set_status": None,
    }
    if set_name is None:
        return payload

    assert_read_command("replSetGetStatus")
    try:
        status = await _admin(client).command("replSetGetStatus")
    except OperationFailure as exc:
        if exc.code == NO_REPLICATION_ENABLED_CODE:
            payload["is_replica_set"] = False
            return payload
        raise
    payload["repl_set_status"] = {
        "set": status.get("set"),
        "members": [
            {
                "name": member.get("name"),
                "state": member.get("stateStr"),
                "health": member.get("health"),
                "uptime": member.get("uptime"),
            }
            for member in status.get("members", [])
        ],
    }
    return payload


async def fetch_fingerprint(
    client: AsyncMongoClient[dict[str, Any]], *, auth_mode: str
) -> dict[str, Any]:
    """Return the canonical fingerprint fields.

    Reads ``buildInfo`` (version, gitVersion, edition), ``hello`` (wire-protocol
    version range, replica-set name + primary + member roles), and the slim
    ``serverStatus`` (storage engine). *auth_mode* is derived by the caller from
    whether the target carries a ``secret_ref``.
    """
    assert_read_command("buildInfo")
    build = await _admin(client).command("buildInfo")
    hello = await _hello(client)
    server_status = await fetch_server_status(client)
    storage = server_status["server_status"].get("storageEngine") or {}

    modules = build.get("modules", []) or []
    edition = "enterprise" if "enterprise" in modules else "community"

    return {
        "server_version": build.get("version"),
        "git_version": build.get("gitVersion"),
        "edition": edition,
        "max_wire_version": hello.get("maxWireVersion"),
        "min_wire_version": hello.get("minWireVersion"),
        "auth_mode": auth_mode,
        "storage_engine": storage.get("name") if isinstance(storage, dict) else None,
        "replica_set": hello.get("setName"),
        "primary": hello.get("primary"),
        "members": _members_from_hello(hello),
    }
