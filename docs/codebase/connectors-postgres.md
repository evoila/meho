# Connector: postgres (PostgreSQL 12–17, read-only)

## Overview

The `postgres` connector is MEHO's **first wire-protocol (non-HTTP) connector**.
It subclasses the generic `Connector` ABC (not `HttpConnector`) and drives an
`asyncpg` client over the PostgreSQL frontend/backend protocol v3 (default port
5432), under the `(product="postgres", version="16", impl_id="postgres-wire")`
registry triple plus the `("postgres", "", "")` wildcard fallback. It brings a
self-hosted PostgreSQL instance inside the MEHO dispatch → policy-gate → audit
seam, so an operator can triage "why did this query break / why is this table
bloated" through the same governed surface every other connector uses (#2236,
under Initiative #2228). It establishes the DB-connector shape the mongodb
sibling (#2237) reuses for a different wire driver.

The connector is **read-only, doubly enforced**: every session is opened with
`default_transaction_read_only=on` (server-enforced), and the free-form
`postgres.query` op additionally runs its statement through a first-keyword
allowlist before it reaches the wire. Auth is **optional** — a trust-auth
instance (no `secret_ref`) connects with no password.

Source: `backend/src/meho_backplane/connectors/postgres/`.

## Key types

- **`PostgresConnector`** (`connector.py`) — `Connector` subclass. Class
  attributes: `product="postgres"`, `version="16"`, `impl_id="postgres-wire"`,
  `supported_version_range=">=12,<18"`, `priority=1` (outranks a
  `GenericRestConnector` auto-shim). Owns the connect/close lifecycle
  (`_connection` async context manager), `fingerprint`, `probe`, the seven op
  handlers, `register_operations`, and the `execute` dispatcher shim.
- **`PostgresOp`** (`ops.py`) — frozen dataclass carrying one op's registration
  metadata. `PG_OPS` is the tuple the registrar walks;
  `PG_WHEN_TO_USE_BY_GROUP` supplies the per-group `when_to_use` blurb.
- **`connect_read_only` / `assert_read_only_sql` / `PostgresReadOnlyError`**
  (`session.py`) — the wire-session factory (server-enforced read-only + the
  optional-auth branch) and the pure first-keyword gate + its violation type.
- **`queries.py`** — the SQL text, row-shaping, and the `_jsonable` normaliser
  that coerces asyncpg's rich types (`datetime`, `Decimal`, `UUID`, `inet`,
  `bytes`) to JSON-serialisable primitives.

## Control flow

### Registration (two-phase, mirrors loki/pfsense)

- **Import time** — `postgres/__init__.py` calls `register_connector_v2` twice
  (versioned triple + wildcard). `_eager_import_connectors` discovers the
  subpackage by directory name. The `product="postgres"` token enters the
  `TargetCreate.product` OpenAPI enum via `registered_product_tokens()`
  (regenerated CLI snapshot at `cli/api/openapi.json`).
- **Lifespan** — `register_postgres_typed_operations` (queued via
  `register_typed_op_registrar`) delegates to
  `PostgresConnector.register_operations`, which upserts the seven descriptors
  into `endpoint_descriptor`. Idempotent across restarts.

### Dispatch

An op dispatches through `meho_backplane.operations.dispatch`, which resolves
the connector, runs the policy gate, validates params, and invokes the bound
handler `(operator, target, params)`. Each handler opens a read-only connection
via `self._connection(...)` (which delegates to `connect_read_only`), runs one
query function from `queries.py`, shapes the rows, and closes the connection.

Ops:

| op | reads | notes |
|----|-------|-------|
| `postgres.databases` | `pg_database` + `pg_database_size` | non-template dbs, owner, encoding, size |
| `postgres.schemas` | `pg_namespace` | user schemas (system + `information_schema` excluded) |
| `postgres.tables` | `pg_stat_user_tables` + size helpers | vacuum/analyze stats + total/table/index bytes |
| `postgres.indexes` | `pg_stat_user_indexes` + `pg_relation_size` | scan counters + index size |
| `postgres.activity` | `pg_stat_activity` | sessions; **query text omitted** (may hold literal secrets) |
| `postgres.settings` | `pg_settings` | curated set by default; `names` filter overrides |
| `postgres.query` | any read-only statement | first-keyword allowlisted + server read-only; row-capped |

Schemas/tables/indexes/query accept an optional `database` param (catalog stats
are per-database); tables/indexes accept an optional `schema` filter.

### Read-only enforcement (double)

1. **Server-enforced** — `connect_read_only` sets
   `server_settings={"default_transaction_read_only": "on"}` as a startup
   parameter, so every implicit single-statement transaction on the connection
   inherits it. A write (including one attempted directly on the connection)
   is rejected by PostgreSQL with `ReadOnlySqlTransactionError` (SQLSTATE
   25006).
2. **First-keyword allowlist** — `assert_read_only_sql` (used by
   `postgres.query`) parses the first significant keyword (skipping leading
   comments and wrapping parens) and rejects anything not in
   `{SELECT, SHOW, EXPLAIN, WITH, TABLE, VALUES}` **before a connection is
   opened**. `WITH`/`EXPLAIN` are admitted at the keyword level; a
   data-modifying CTE or `EXPLAIN ANALYZE <write>` that slips past is still
   caught by the server-side backstop.

The two defences are independent: the allowlist is a fast, transport-free
filter; the session flag is the authoritative backstop for the whole session
(not just the free-form op).

### Auth (optional)

`connect_read_only` branches on `target.secret_ref`:

- **`None`** — trust-auth. Connects as `DEFAULT_TRUST_USER` (`"postgres"`) with
  no password (`pg_hba.conf` `trust`/`peer`, or a dev port-forward). This is
  the net-new "execute without a `secret_ref`" branch — every other execute
  path fails closed on an unresolved credential.
- **set** — resolves `{username, password}` via `load_basic_credentials`
  (operator-context Vault read). The password flows only into the asyncpg
  connect params; it never enters a log line or an `OperationResult`. A
  credentialled target reached without an authenticated operator (the
  operator-less `execute` shim / `probe`) fails closed inside the loader.

### Fingerprint / probe

`fingerprint()` connects to the default maintenance database and reads
`server_version`, `pg_is_in_recovery()`, `server_encoding`, `data_checksums`,
and per-database sizes; any connect/credential failure maps to
`reachable=False` with the error under `extras` (never raises). `probe()` is a
`SELECT 1` handshake carrying no operator, so its failure reasons are
`auth_failed` (bad/unresolvable credential — a credentialled target on the
operator-less probe path resolves here), `tcp_unreachable` (`OSError`), and
`connect_failed` (other `PostgresError`).

## Dependencies

- `asyncpg` (already a backend dependency for the SQLAlchemy engine; used
  directly by a connector for the first time here) — the wire client.
- `_shared/vault_creds.py` — `load_basic_credentials` for the optional
  operator-context credential read.
- `operations/typed_register.py` — `register_typed_operation` and the registrar
  queue.
- `operations.dispatch` — the `execute` shim delegates to it.

## Known issues / scope

- **TLS is not yet wired.** `connect_read_only` passes no `ssl` argument, so it
  connects unencrypted (fine for a trust-auth port-forward or an in-cluster
  instance). A `verify_tls` / SNI-aware SSL path is a follow-up when a
  TLS-required target is registered.
- `postgres.activity` deliberately omits the in-flight `query` text (it can
  contain literal credential values); state/wait/timing columns are returned.
- Catalog stats are per-database — pass `database` to inspect a database other
  than the default `postgres`.

## References

- asyncpg: <https://magicstack.github.io/asyncpg/current/>
- Read-only transactions:
  <https://www.postgresql.org/docs/current/sql-set-transaction.html>
- Monitoring stats (`pg_stat_activity` / `pg_stat_user_tables` /
  `pg_stat_user_indexes`):
  <https://www.postgresql.org/docs/current/monitoring-stats.html>
- Sibling read-only connector: `docs/codebase/connectors-loki.md`.
- Task #2236; Initiative #2228 (data-tier + hypervisor connector coverage);
  establishes the DB-connector shape for mongodb (#2237).
