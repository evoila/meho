# Connector: postgres (PostgreSQL 15–18, read-only)

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
`default_transaction_read_only=on` and a `statement_timeout` (server-enforced),
and the free-form `postgres.query` op additionally runs its statement through a
first-keyword allowlist, a deny list of server admin functions and a deny list
of catalogs that can hold a secret before it reaches the wire, and refuses to run as a superuser role unless the target
allows it. Auth is **optional** — a trust-auth instance (no `secret_ref`)
connects with no password.

Source: `backend/src/meho_backplane/connectors/postgres/`.

## Key types

- **`PostgresConnector`** (`connector.py`) — `Connector` subclass. Class
  attributes: `product="postgres"`, `version="16"`, `impl_id="postgres-wire"`,
  `supported_version_range=">=15,<19"`, `priority=1` (outranks a
  `GenericRestConnector` auto-shim). Owns the connect/close lifecycle
  (`_connection` async context manager), `fingerprint`, `probe`, the eight op
  handlers, `register_operations`, and the `execute` dispatcher shim.
- **`PostgresOp`** (`ops.py`) — frozen dataclass carrying one op's registration
  metadata. `PG_OPS` is the tuple the registrar walks;
  `PG_WHEN_TO_USE_BY_GROUP` supplies the per-group `when_to_use` blurb.
- **`connect_read_only` / `assert_read_only_sql` / `PostgresReadOnlyError`**
  (`session.py`) — the wire-session factory (server-enforced read-only +
  statement timeout + the optional-auth branch) and the pure first-keyword
  gate + its violation type.
- **`assert_no_admin_functions` / `assert_no_secret_catalogs` /
  `assert_not_superuser` / `PostgresSuperuserError`** (`query_guard.py`) —
  the three extra guards of `postgres.query`: the deny list of server admin
  functions (`DENIED_FUNCTIONS` + `DENIED_FUNCTION_PREFIXES`), the deny list
  of catalogs that can hold a secret (`DENIED_RELATIONS`), and the superuser
  refusal with its target flag `extras["allow_superuser_query"]`.
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
  `PostgresConnector.register_operations`, which upserts the eight descriptors
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
| `postgres.settings` | `pg_settings` | curated set by default; `names` filter overrides; **each secret value of a connection text is hidden**: a value of `primary_conninfo` is read with the rules of libpq, and the fixed mark `[REDACTED:param_name]` stands in place of each secret value (`password`, `sslpassword`, `oauth_client_secret`, and the password of an address); every other character stays; a value that libpq cannot read (also one with a parameter name that is not a libpq key word) makes `postgres.settings` refuse the call when it holds a secret name or an address password, and stays as it is otherwise; a call without `names` is never refused |
| `postgres.replication` | `pg_stat_replication` + `pg_stat_wal_receiver` + `pg_is_in_recovery()` / `pg_last_xact_replay_timestamp()` | physical streaming replication health + standby lag; symmetric (standbys on the primary, `wal_receiver` on the standby); LSNs cast to canonical `X/Y` text, `*_lag` as float seconds; **`conninfo` omitted** (holds the primary DSN) |
| `postgres.query` | any read-only statement | first-keyword allowlisted + admin functions and secret catalogs denied + no superuser role + server read-only; row-capped |

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
   opened**. It first refuses a statement in which a block comment holds the
   start of another block comment (`assert_no_nested_block_comment`).
   PostgreSQL nests block comments, so with that refusal the first keyword
   the gate reads is the one the server reads. White space and the end of a
   `--` line comment (a line feed or a carriage return) follow the PostgreSQL
   SQL scanner. The gate also checks a second reading that ends a line
   comment only at a line feed, and refuses the statement when either reading
   starts with a word that is not on the list. `WITH`/`EXPLAIN` are admitted at the keyword level; a
   data-modifying CTE or `EXPLAIN ANALYZE <write>` that slips past is still
   caught by the server-side backstop.

The two defences are independent: the allowlist is a fast, transport-free
filter; the session flag is the authoritative backstop for the whole session
(not just the free-form op).

### Statement timeout

Every session also sets `statement_timeout` as a startup parameter
(`DEFAULT_STATEMENT_TIMEOUT_MS`, 30 seconds, in `session.py`). PostgreSQL ends
a statement that runs longer, so one slow query cannot hold a backend open
for the curated ops or for `postgres.query`. The value is fixed; a target
cannot raise it.

### Extra guards of `postgres.query` (`query_guard.py`)

A read-only transaction stops writes to tables, but PostgreSQL still allows
some server admin functions inside it, and what they do depends on the login
role's rights. So the free-form op adds three checks:

1. **Admin function deny list** — `assert_no_admin_functions` runs after the
   first-keyword gate and before a connection is opened. It lower-cases every
   identifier-like word in the raw statement text and refuses the statement
   when a word is in `DENIED_FUNCTIONS` (server signalling such as
   `pg_terminate_backend` / `pg_reload_conf`, `set_config`, server file access
   such as `pg_read_file` / `lo_export`, backup and WAL control such as
   `pg_switch_wal`, recovery control such as `pg_promote`, replication slot
   and origin functions, statistics resets, `pg_notify`, and the built-ins
   that run a SQL string given as an argument: `ts_stat`, `ts_rewrite`,
   `connectby`, and the extension functions that read stored rows:
   `get_raw_page`, `pg_get_wal_block_info`) or starts with a prefix in `DENIED_FUNCTION_PREFIXES`
   (`pg_ls_`, `pg_logical_slot_`, `pg_stat_reset`, `pg_sleep`, `dblink`,
   `postgres_fdw_`, `query_to_xml`, `cursor_to_xml`, `crosstab`, and the
   table-dumping families `table_to_xml`, `schema_to_xml`,
   `database_to_xml`). The names
   are pinned to the PostgreSQL 16 pages "System Administration Functions",
   "XML Functions" (mapping tables to XML), "Text Search Functions" and the
   `tablefunc` extension. The SQL-running functions are on the list because
   a statement could build a denied name from string pieces (`'pg_' ||
   'terminate_backend(...)'`), which the word scan cannot see, and hand it
   to such a function; the server would join and run it. The scan does not
   strip comments or string literals first, so an unclosed quote cannot
   hide a later call; a false hit (a column named `pg_sleep`) only gives a
   clear refusal. Unicode-escaped identifiers (`U&"..."`) are refused as a
   whole. The prefixes cover no catalog view or role name
   (`pg_replication_origin_status`, `pg_read_all_data` stay allowed). The table-dumping functions are on the list because they
   read every row of a table or schema chosen by a value, and the statement
   can build that value from pieces, so a denied catalog name (next check)
   would not show in the text. For the same reason the list holds
   `get_raw_page` (the `pageinspect` extension: the stored page of a table
   named by a text value) and `pg_get_wal_block_info` (the `pg_walinspect`
   extension: the stored row data of the WAL records). The other functions
   of these extensions read a page that `get_raw_page` returns, or show WAL
   records without their row data.
2. **Catalog deny list** — `assert_no_secret_catalogs` runs right after the
   function check, also before a connection is opened, with the same word
   scan and the same refusal of Unicode-escaped identifiers. It refuses a
   statement that names a catalog in `DENIED_RELATIONS`:
   - `pg_authid`, `pg_shadow` — the password hash of every role;
   - `pg_subscription` — the connection string of a logical replication
     subscription, often with a password;
   - `pg_user_mapping`, `pg_user_mappings`, and the information schema views
     `user_mapping_options` and `_pg_user_mappings` — the options of a
     foreign server login (`postgres_fdw`), with its password;
   - `pg_stat_activity`, `pg_stat_get_activity`,
     `pg_stat_get_backend_activity` — the SQL text of other sessions, which
     can hold a password;
   - `pg_stat_statements`, `pg_stat_statements_info` — the extension that
     keeps the SQL text of past statements (its info view goes with it).
   - `pg_settings`, `pg_file_settings`, `pg_show_all_settings`,
     `pg_show_all_file_settings`, `current_setting` — MEHO refuses these
     reads of server settings and points to `postgres.settings`.
   - `pg_statistic`, `pg_stats`, `pg_stats_ext`, `pg_stats_ext_exprs`,
     `pg_statistic_ext_data` — MEHO refuses these reads of column
     statistics, which hold sampled column values.

   The check also refuses `SHOW ALL` and a `SHOW` of a setting in
   `CONNECTION_TEXT_SETTINGS` (`primary_conninfo`), in any case, in double
   quotes or after a comment, under both readings of the first keyword. The
   refusal for settings names
   `postgres.settings`. For every caller, it reads the value of such a setting
   with the rules of libpq (the PostgreSQL pages "Connection Strings" and
   "Parameter Key Words") and puts the fixed mark `[REDACTED:param_name]` in
   place of each secret value: the value of `password`, `sslpassword` and
   `oauth_client_secret` (any case) in the key word form and in the query part
   of the address form, and the password of the address. In the address form,
   white space is part of a value, as libpq reads it. Every other
   character stays as the server sent it. A value that libpq cannot read
   (also one with a parameter name that is not a libpq key word) makes
   `postgres.settings` refuse the call when it holds a secret name or an
   address password; the error names the setting and holds no part of the
   value. Else the value stays as it is. A call without `names` is never
   refused. The refusal for
   sessions names `postgres.activity`, which reads the same
   view without the `query` column. Views that show no secret value stay
   allowed: `pg_roles` and `pg_user` (the password is shown as stars),
   `pg_stat_ssl`, `pg_stat_subscription` (no connection string), and
   `pg_stat_wal_receiver` (its connection string hides the password). The
   names are pinned to the PostgreSQL 16 pages "System Catalogs", "System
   Views", "The Cumulative Statistics System", the information schema and
   `pg_stat_statements`. The check holds for every login and every target,
   also a target that allows a superuser. A sensor of `postgres.query` whose
   statement names one of these catalogs reads `unknown` at each run, with
   the refusal as the cause.
3. **No superuser role** — `assert_not_superuser` runs on the open connection
   before the statement. It reads `pg_roles.rolsuper` for `current_user` and
   lets the query run only on a clear `false`; a `true`, a missing row or an
   odd value refuses with `PostgresSuperuserError`. A target that sets
   `extras["allow_superuser_query"]` to the boolean `true` skips the check
   (an operator's explicit choice for a lab or dev instance). The curated
   ops are not affected: their SQL is fixed catalog reads, so they still run
   as any role, including the trust-auth default `postgres`.

   A superuser login that a target allows can still read every other table
   and catalog. A login that is not a superuser, with
   read rights only on the tables the operator wants to show (and
   `pg_monitor` or `pg_read_all_stats` only when needed), is the right
   choice for `postgres.query`.

Both errors are `ValueError` subclasses, so the dispatcher renders the
message under `connector_error` and the caller sees which name or role was
refused.

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

### Address check at connect time

`connect_read_only` hands the dial to `connect_checked`
(`connectors/_shared/dial_guard.py`). It checks the target address again
with the same rules and allowlist as when the target is saved. A host name
is looked up once, and asyncpg dials only the checked addresses, one after
the other until one connects. A host name that does not resolve is refused,
unless the exact name is on the allowlist. The refusal is a `ValueError`,
so it shows as a normal connector error. `fingerprint()` and `probe()`
report it as `auth_failed`, because both map every `ValueError` to that
reason.

### Fingerprint / probe

`fingerprint()` connects to the default maintenance database and reads
`server_version`, `pg_is_in_recovery()`, `server_encoding`, `data_checksums`,
and per-database sizes; any connect/credential failure maps to
`reachable=False` with the error under `extras` (never raises). The
`extras["error"]` value and the paired `postgres_fingerprint_unreachable`
warning log carry only the exception type name plus a classified reason
(the same `auth_failed` / `tcp_unreachable` / `connect_failed` taxonomy the
probe uses), never `str(exc)` — asyncpg's auth rejection echoes the connecting
username, so the raw driver message must not reach either surface (#3297;
redaction seam `connectors/_shared/fingerprint.py`). `probe()` is a
`SELECT 1` handshake carrying no operator, so its failure reasons are
`auth_failed` (bad/unresolvable credential — a credentialled target on the
operator-less probe path resolves here), `tcp_unreachable` (`OSError`), and
`connect_failed` (other `PostgresError`).

## Dependencies

- `asyncpg` (already a backend dependency for the SQLAlchemy engine; used
  directly by a connector for the first time here) — the wire client.
- `_shared/vault_creds.py` — `load_basic_credentials` for the optional
  operator-context credential read.
- `_shared/dial_guard.py` — `connect_checked` for the connect-time address
  check.
- `operations/typed_register.py` — `register_typed_operation` and the registrar
  queue.
- `operations.dispatch` — the `execute` shim delegates to it.

## Known issues / scope

- **Lower bound is PostgreSQL 15.** The range follows the releases the vendor
  still supports (see "Supported versions"). `postgres.replication` reads
  `pg_stat_wal_receiver.written_lsn` / `flushed_lsn`, which every release in
  the range has.
- **A user-defined function is not inspected.** The deny list matches names
  in the statement text. A function a database owner wrote that calls an
  admin function inside is not seen; what it can do is bounded by the login
  role's rights, which is why `postgres.query` refuses a superuser role.
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
  `pg_stat_user_indexes` / `pg_stat_replication` / `pg_stat_wal_receiver`):
  <https://www.postgresql.org/docs/current/monitoring-stats.html>
- Recovery information functions (`pg_is_in_recovery` /
  `pg_last_xact_replay_timestamp`, NULL outside recovery) and the admin
  function families on the deny list:
  <https://www.postgresql.org/docs/16/functions-admin.html>
- SQL-running built-ins on the deny list: `query_to_xml` family
  (<https://www.postgresql.org/docs/16/functions-xml.html>), `ts_stat` /
  `ts_rewrite` (<https://www.postgresql.org/docs/16/functions-textsearch.html>),
  `crosstab` / `connectby` (<https://www.postgresql.org/docs/16/tablefunc.html>)
- `statement_timeout`:
  <https://www.postgresql.org/docs/16/runtime-config-client.html>
- `pg_roles.rolsuper`: <https://www.postgresql.org/docs/16/view-pg-roles.html>
- Sibling read-only connector: `docs/codebase/connectors-loki.md`.
- Task #2236; Initiative #2228 (data-tier + hypervisor connector coverage);
  establishes the DB-connector shape for mongodb (#2237).

## Supported versions

The range is `>=15,<19`: the PostgreSQL releases the vendor still supports.
CI runs the container tests on PostgreSQL 16, 17 and 18
(`backend/tests/integration/test_connectors_postgres_container.py`). A target
detected as PostgreSQL 13 or 14 is not picked; the error names the range.
`strict_version_range = True` makes the fallback registration `("postgres",
"", "")` check the range too. It still serves a target whose version is not
known yet, and a probe still finds the connector.
The probe stores `server_version` as PostgreSQL reports it, which can carry
the distribution's build text (`13.16 (Debian 13.16-1.pgdg120+1)`). The
resolver checks the range against the leading number (`13.16`).
