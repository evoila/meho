# Connector: mongodb (MongoDB 5–8, read-only)

## Overview

The `mongodb` connector is MEHO's **second wire-protocol (non-HTTP) connector**,
following the DB-connector shape the postgres connector (#2236) established. It
subclasses the generic `Connector` ABC (not `HttpConnector`) and drives a
`pymongo.AsyncMongoClient` over the MongoDB wire protocol (default port 27017),
under the `(product="mongodb", version="7", impl_id="mongodb-wire")` registry
triple plus the `("mongodb", "", "")` wildcard fallback. It brings a self-hosted
MongoDB instance inside the MEHO dispatch → policy-gate → audit seam, so an
operator can triage replica-set health, index/TTL config, and collection storage
through the same governed surface every other connector uses (#2237, under
Initiative #2228).

The connector is **read-only by construction**: it registers a fixed set of read
ops, each issuing exactly one command from a closed allowlist. There is **no**
free-form command / `eval` / `$where` / aggregation-with-`$out` op, so read-only
is guaranteed by the command set itself rather than a runtime SQL-style gate.
Auth is **optional** — a no-auth instance (no `secret_ref`) connects with no
credentials.

Source: `backend/src/meho_backplane/connectors/mongodb/`.

## Driver choice: PyMongo native async, not Motor

The connector uses PyMongo's **native async API** (`AsyncMongoClient`, GA since
PyMongo 4.13), not the Motor driver the task originally suggested. MongoDB
deprecated Motor on 2025-05-14 in favour of the unified PyMongo Async API and
set Motor's end-of-life at 2026-05-14, so a brand-new connector adopts the
supported client directly and pulls **no separate `motor` dependency**.
`AsyncMongoClient` runs commands natively on the event loop, so no
`asyncio.to_thread` wrapper is needed (unlike the synchronous `hvac` precedent).

## Key types

- **`MongoDbConnector`** (`connector.py`) — `Connector` subclass. Class
  attributes: `product="mongodb"`, `version="7"`, `impl_id="mongodb-wire"`,
  `supported_version_range=">=5,<9"`, `priority=1` (outranks a
  `GenericRestConnector` auto-shim). Owns the connect/close lifecycle
  (`_client` async context manager), `fingerprint`, `probe`, the eight op
  handlers, `register_operations`, and the `execute` dispatcher shim.
- **`MongoOp`** (`ops.py`) — frozen dataclass carrying one op's registration
  metadata. `MONGO_OPS` is the tuple the registrar walks;
  `MONGO_WHEN_TO_USE_BY_GROUP` supplies the per-group `when_to_use` blurb.
- **`connect_client` / `MONGO_READ_COMMANDS` / `assert_read_command` /
  `MongoReadOnlyError`** (`session.py`) — the client factory (with the
  optional-auth branch) and the closed read-command allowlist + its
  belt-and-suspenders check and violation type.
- **`queries.py`** — the command runners, document row-shaping, and the
  `_jsonable` normaliser that coerces BSON types (`ObjectId`, `datetime`,
  `Decimal128`, `Timestamp`, `Binary`) to JSON-serialisable primitives.

## Control flow

### Registration (two-phase, mirrors postgres/loki)

- **Import time** — `mongodb/__init__.py` calls `register_connector_v2` twice
  (versioned triple + wildcard). `_eager_import_connectors` discovers the
  subpackage by directory name. The `product="mongodb"` token enters the
  `TargetCreate.product` OpenAPI enum via `registered_product_tokens()`
  (regenerated CLI snapshot at `cli/api/openapi.json`).
- **Lifespan** — `register_mongodb_typed_operations` (queued via
  `register_typed_op_registrar`) delegates to
  `MongoDbConnector.register_operations`, which upserts the eight descriptors
  into `endpoint_descriptor`. Idempotent across restarts.

### Dispatch

An op dispatches through `meho_backplane.operations.dispatch`, which resolves the
connector, runs the policy gate, validates params, and invokes the bound handler
`(operator, target, params)`. Each handler opens a client via
`self._client(...)` (which delegates to `connect_client`), runs one command
function from `queries.py`, shapes the documents, and closes the client.

Ops:

| op | command | notes |
|----|---------|-------|
| `mongodb.databases` | `listDatabases` | databases + on-disk sizes + cluster total |
| `mongodb.collections` | `listCollections` | collections/views in a database (`database` required) |
| `mongodb.db_stats` | `dbStats` | database storage stats (`database` required) |
| `mongodb.collection_stats` | `collStats` | collection storage + per-index sizes (`database`, `collection`) |
| `mongodb.indexes` | `listIndexes` | indexes incl. TTL `expireAfterSeconds` (`database`, `collection`) |
| `mongodb.count` | `estimatedDocumentCount` | fast metadata count, O(1) (`database`, `collection`) |
| `mongodb.server_status` | `serverStatus` | slim projection (heavy sections suppressed) |
| `mongodb.replica_status` | `hello` + `replSetGetStatus` | replica-set health + member roles |

### Read-only enforcement (fixed command set)

Unlike a SQL database there is no free-form query surface at all. Every op maps
to exactly one command in `MONGO_READ_COMMANDS` (`listDatabases`,
`listCollections`, `dbStats`, `collStats`, `listIndexes`, `count`,
`serverStatus`, `buildInfo`, `hello`, `replSetGetStatus`), and no op's parameter
schema accepts a caller-supplied command name, aggregation pipeline, filter, or
`eval` body — the only params are `database` / `collection` selectors. Read-only
is therefore a property of the closed command set. `assert_read_command` is a
belt-and-suspenders check the query layer runs before every command, so a future
op wired to a command off the allowlist fails closed.

### Auth (optional)

`connect_client` branches on `target.secret_ref`:

- **`None`** — a no-auth instance. Connects with no credentials
  (`directConnection=True`, bounded server-selection timeout). This is the
  net-new "execute without a `secret_ref`" branch — every other execute path
  fails closed on an unresolved credential.
- **set** — resolves `{username, password}` via `load_basic_credentials`
  (operator-context Vault read) and authenticates against the `admin` database
  (`DEFAULT_AUTH_SOURCE`). The password flows only into the client's connection
  params; it never enters a log line or an `OperationResult`. A credentialled
  target reached without an authenticated operator (the operator-less `execute`
  shim / `probe`) fails closed inside the loader.

### Fingerprint / probe

`fingerprint()` reads `buildInfo` (version, gitVersion, edition), `hello`
(wire-protocol version range, replica-set name + primary + derived member
roles), and the slim `serverStatus` (storage engine); any connect/credential
failure maps to `reachable=False` with the error under `extras` (never raises).
`probe()` is a `hello` handshake carrying no operator, so its failure reasons are
`auth_failed` (bad/unresolvable credential, or a MongoDB auth error code 13/18 —
a credentialled target on the operator-less probe path resolves here),
`tcp_unreachable` (`ConnectionFailure` / `ServerSelectionTimeoutError`), and
`connect_failed` (other `PyMongoError`).

Member roles are derived from `hello` (`hosts`/`passives`/`arbiters` + `primary`)
so the fingerprint does not need the elevated `clusterMonitor` privilege
`replSetGetStatus` requires; `mongodb.replica_status` additionally calls
`replSetGetStatus` for each member's live `stateStr`/health, and treats the
standalone code-76 (`NoReplicationEnabled`) as `is_replica_set=False`.

## Dependencies

- `pymongo` (>=4.13, added for this connector) — the async wire client
  (`AsyncMongoClient`) + BSON types. Native async replaces Motor (deprecated).
- `_shared/vault_creds.py` — `load_basic_credentials` for the optional
  operator-context credential read.
- `operations/typed_register.py` — `register_typed_operation` and the registrar
  queue.
- `operations.dispatch` — the `execute` shim delegates to it.

## Known issues / scope

- **TLS is not yet wired.** `connect_client` passes no TLS argument, so it
  connects unencrypted (fine for a no-auth port-forward or an in-cluster
  instance). A `verify_tls` / SNI-aware TLS path is a follow-up when a
  TLS-required target is registered.
- **`collStats` is deprecated** as a diagnostic command since MongoDB 6.2 but is
  still served through 8.x; if a future server removes it, `mongodb.collection_stats`
  moves to the `$collStats` aggregation stage.
- `serverStatus` is projected to a curated slim subset; the full internal
  metrics (`wiredTiger`, `metrics`, `locks`, `transactions`) are suppressed over
  the wire, not just dropped in code.
- Member roles in `fingerprint` come from `hello`; a live replica set's detailed
  per-member state is on `mongodb.replica_status`.

## References

- PyMongo async API: <https://pymongo.readthedocs.io/en/stable/api/pymongo/asynchronous/index.html>
- Motor deprecation / migrate to PyMongo async:
  <https://www.mongodb.com/docs/languages/python/pymongo-driver/current/reference/migration/>
- MongoDB database commands:
  <https://www.mongodb.com/docs/manual/reference/command/>
- `replSetGetStatus`:
  <https://www.mongodb.com/docs/manual/reference/command/replSetGetStatus/>
- Sibling wire-protocol connector: `docs/codebase/connectors-postgres.md`.
- Task #2237; Initiative #2228 (data-tier + hypervisor connector coverage);
  reuses the DB-connector shape from postgres (#2236).
