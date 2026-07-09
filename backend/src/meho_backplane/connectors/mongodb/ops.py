# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Curated read ops exposed by :class:`MongoDbConnector` (#2237).

The read core an operator needs to triage a self-hosted MongoDB instance
through the same dispatch -> policy-gate -> audit seam every other connector
uses, without reaching for ``mongosh`` against ``:27017``:

* ``mongodb.databases`` -- databases with on-disk sizes (``listDatabases``).
* ``mongodb.collections`` -- collections in a database (``listCollections``).
* ``mongodb.db_stats`` -- storage statistics for a database (``dbStats``).
* ``mongodb.collection_stats`` -- storage statistics for a collection (``collStats``).
* ``mongodb.indexes`` -- indexes for a collection, incl. TTL ``expireAfterSeconds``
  (``listIndexes``).
* ``mongodb.count`` -- fast metadata document count (``estimatedDocumentCount``).
* ``mongodb.server_status`` -- slim ``serverStatus`` projection.
* ``mongodb.replica_status`` -- replica-set health (``hello`` + ``replSetGetStatus``).

Every op is ``safety_level="safe"`` + ``requires_approval=False`` and carries a
``read-only`` tag. Read-only is guaranteed by the **fixed command set**: each op
issues exactly one command from
:data:`~meho_backplane.connectors.mongodb.session.MONGO_READ_COMMANDS`, and the
connector exposes **no** arbitrary-command / ``eval`` / ``$where`` /
aggregation-with-``$out`` op. The dataclass + tuple shape mirrors the postgres
(#2236) and loki (#2235) siblings so the registration walk reads identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["MONGO_OPS", "MONGO_WHEN_TO_USE_BY_GROUP", "MongoOp"]


@dataclass(frozen=True)
class MongoOp:
    """Metadata for one MongoDB op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the registrar can splat the dataclass into the helper.
    ``handler_attr`` is the async-handler attribute name on
    :class:`~meho_backplane.connectors.mongodb.connector.MongoDbConnector`.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Curated ``when_to_use`` blurbs per group. ``register_typed_operation``
#: requires a non-empty string whenever ``group_key`` is set; the registrar
#: looks each op's ``group_key`` up here.
MONGO_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "mongodb-inventory": (
        "Use to inventory a MongoDB instance's logical layout: list databases "
        "with their on-disk size (mongodb.databases), the collections in a "
        "database (mongodb.collections), the indexes on a collection including "
        "any TTL expiry (mongodb.indexes), or a collection's fast estimated "
        "document count (mongodb.count). The right group for 'what databases / "
        "collections exist?', 'which collection has a TTL index and after how "
        "long does it expire?', or 'roughly how many documents are in this "
        "collection?'. Read-only."
    ),
    "mongodb-stats": (
        "Use to read a MongoDB instance's storage statistics: a database's data "
        "/ storage / index sizes and object count (mongodb.db_stats) or the same "
        "for a single collection (mongodb.collection_stats). The right group for "
        "'why is this database so large?' or 'which collection / its indexes "
        "dominate disk?'. Read-only."
    ),
    "mongodb-runtime": (
        "Use to inspect a MongoDB instance's live runtime: a slim serverStatus "
        "projection covering connections, network, opcounters, memory, and the "
        "storage engine (mongodb.server_status), or replica-set health with each "
        "member's role and state (mongodb.replica_status). The right group for "
        "'is this instance under connection pressure?', 'what is the opcounter "
        "mix?', or 'is the replica set healthy and who is primary?'. Read-only."
    ),
}


# ---------------------------------------------------------------------------
# Shared parameter-schema fragments
# ---------------------------------------------------------------------------

_DATABASE_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "The database to inspect (e.g. 'app', 'admin').",
}

_COLLECTION_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": "The collection to inspect within the database.",
}

_EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_DATABASE_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {"database": _DATABASE_PROPERTY},
    "required": ["database"],
    "additionalProperties": False,
}

_DATABASE_COLLECTION_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {"database": _DATABASE_PROPERTY, "collection": _COLLECTION_PROPERTY},
    "required": ["database", "collection"],
    "additionalProperties": False,
}

_OBJECT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}


_DATABASES = MongoOp(
    op_id="mongodb.databases",
    handler_attr="list_databases",
    summary="List databases with their on-disk sizes.",
    description=(
        "Lists every database on the instance with its on-disk size in bytes and "
        "whether it holds data (listDatabases), plus the cluster total size. The "
        "starting point for 'what databases exist and which is using the disk?'. "
        "safety_level=safe, read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema=_OBJECT_RESPONSE_SCHEMA,
    group_key="mongodb-inventory",
    tags=("read-only", "mongodb", "inventory", "databases"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call first to see the databases on a MongoDB instance and their "
            "sizes before drilling into collections of a specific one."
        ),
        "parameter_hints": {},
        "output_shape": ("{databases:[{name, sizeOnDisk, empty}], total_size_bytes}."),
    },
)


_COLLECTIONS = MongoOp(
    op_id="mongodb.collections",
    handler_attr="list_collections",
    summary="List the collections in a database.",
    description=(
        "Lists the collections (and views) in a database with their type and "
        "creation options (listCollections). Pass 'database'. safety_level=safe, "
        "read-only."
    ),
    parameter_schema=_DATABASE_PARAMS,
    response_schema=_OBJECT_RESPONSE_SCHEMA,
    group_key="mongodb-inventory",
    tags=("read-only", "mongodb", "inventory", "collections"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to enumerate the collections in a database before reading their stats/indexes."
        ),
        "parameter_hints": {"database": "The database to inspect."},
        "output_shape": "{database, collections:[{name, type, options, info}]}.",
    },
)


_DB_STATS = MongoOp(
    op_id="mongodb.db_stats",
    handler_attr="db_stats",
    summary="Return storage statistics for a database.",
    description=(
        "Returns a database's storage statistics (dbStats): collection count, "
        "object count, data size, storage size, index count and size, and total "
        "size. The op for 'why is this database so large?'. Pass 'database'. "
        "safety_level=safe, read-only."
    ),
    parameter_schema=_DATABASE_PARAMS,
    response_schema=_OBJECT_RESPONSE_SCHEMA,
    group_key="mongodb-stats",
    tags=("read-only", "mongodb", "stats", "storage"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": "Call to see a database's data/storage/index sizes and object counts.",
        "parameter_hints": {"database": "The database to inspect."},
        "output_shape": (
            "{database, stats:{collections, objects, dataSize, storageSize, "
            "indexes, indexSize, totalSize}}."
        ),
    },
)


_COLLECTION_STATS = MongoOp(
    op_id="mongodb.collection_stats",
    handler_attr="collection_stats",
    summary="Return storage statistics for a collection.",
    description=(
        "Returns a collection's storage statistics (collStats): document count, "
        "average object size, data / storage size, and per-index sizes. The op "
        "for 'which collection or its indexes dominate disk?'. Pass 'database' "
        "and 'collection'. safety_level=safe, read-only."
    ),
    parameter_schema=_DATABASE_COLLECTION_PARAMS,
    response_schema=_OBJECT_RESPONSE_SCHEMA,
    group_key="mongodb-stats",
    tags=("read-only", "mongodb", "stats", "storage"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": "Call to see a single collection's size, storage, and per-index sizes.",
        "parameter_hints": {
            "database": "The database holding the collection.",
            "collection": "The collection to inspect.",
        },
        "output_shape": (
            "{database, collection, stats:{count, size, storageSize, avgObjSize, "
            "totalIndexSize, indexSizes}}."
        ),
    },
)


_INDEXES = MongoOp(
    op_id="mongodb.indexes",
    handler_attr="list_indexes",
    summary="List a collection's indexes, including TTL expiry.",
    description=(
        "Lists the indexes on a collection (listIndexes) with each index's key "
        "spec, name, uniqueness, and — for a TTL index — its "
        "'expireAfterSeconds'. The op for 'which indexes exist / which collection "
        "expires documents and after how long?'. Pass 'database' and "
        "'collection'. safety_level=safe, read-only."
    ),
    parameter_schema=_DATABASE_COLLECTION_PARAMS,
    response_schema=_OBJECT_RESPONSE_SCHEMA,
    group_key="mongodb-inventory",
    tags=("read-only", "mongodb", "inventory", "indexes", "ttl"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to inspect a collection's indexes, including TTL 'expireAfterSeconds'."
        ),
        "parameter_hints": {
            "database": "The database holding the collection.",
            "collection": "The collection to inspect.",
        },
        "output_shape": (
            "{database, collection, indexes:[{name, key, unique, expireAfterSeconds}]}."
        ),
    },
)


_COUNT = MongoOp(
    op_id="mongodb.count",
    handler_attr="estimated_count",
    summary="Return a collection's fast estimated document count.",
    description=(
        "Returns a collection's document count from collection metadata "
        "(estimatedDocumentCount) — O(1) and safe on a large collection, unlike "
        "a full-scan count. Pass 'database' and 'collection'. safety_level=safe, "
        "read-only."
    ),
    parameter_schema=_DATABASE_COLLECTION_PARAMS,
    response_schema=_OBJECT_RESPONSE_SCHEMA,
    group_key="mongodb-inventory",
    tags=("read-only", "mongodb", "inventory", "count"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": "Call for a fast, metadata-based document count (not a full scan).",
        "parameter_hints": {
            "database": "The database holding the collection.",
            "collection": "The collection to count.",
        },
        "output_shape": "{database, collection, estimated_count}.",
    },
)


_SERVER_STATUS = MongoOp(
    op_id="mongodb.server_status",
    handler_attr="server_status",
    summary="Return a slim serverStatus projection.",
    description=(
        "Returns a slim serverStatus projection: host, version, process, uptime, "
        "connections, network, opcounters, memory, storage engine, and the "
        "replication summary. Heavy internal sections (wiredTiger, metrics, "
        "locks, transactions) are suppressed so the payload stays a triage-sized "
        "summary. The op for 'is this instance under connection pressure / what "
        "is the opcounter mix?'. safety_level=safe, read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema=_OBJECT_RESPONSE_SCHEMA,
    group_key="mongodb-runtime",
    tags=("read-only", "mongodb", "runtime", "server-status"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to triage live server health: connections, opcounters, memory, storage engine."
        ),
        "parameter_hints": {},
        "output_shape": (
            "{server_status:{host, version, connections, network, opcounters, mem, "
            "storageEngine, repl}}."
        ),
    },
)


_REPLICA_STATUS = MongoOp(
    op_id="mongodb.replica_status",
    handler_attr="replica_status",
    summary="Return replica-set health and member roles.",
    description=(
        "Returns replica-set health from the hello handshake plus "
        "replSetGetStatus: set name, current primary, each member's role, and "
        "(when available) each member's live state / health / uptime. On a "
        "standalone it reports is_replica_set=false rather than erroring. The op "
        "for 'is the replica set healthy and who is primary?'. safety_level=safe, "
        "read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema=_OBJECT_RESPONSE_SCHEMA,
    group_key="mongodb-runtime",
    tags=("read-only", "mongodb", "runtime", "replica-set"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": "Call to check replica-set health, membership, and which member is primary.",
        "parameter_hints": {},
        "output_shape": (
            "{is_replica_set, set_name, primary, me, members:[{host, role}], "
            "repl_set_status:{set, members:[{name, state, health, uptime}]}}."
        ),
    },
)


#: The ops :class:`MongoDbConnector` registers at lifespan startup.
MONGO_OPS: tuple[MongoOp, ...] = (
    _DATABASES,
    _COLLECTIONS,
    _DB_STATS,
    _COLLECTION_STATS,
    _INDEXES,
    _COUNT,
    _SERVER_STATUS,
    _REPLICA_STATUS,
)
