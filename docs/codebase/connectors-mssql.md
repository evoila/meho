# Connector: mssql (mssql-2022.x / `mssql-tds`)

## Overview

`mssql` is MEHO's typed **Microsoft SQL Server** connector: governed
instance / database / high-availability / backup reads plus governed
backup / restore and database create / drop, for the Hyper-V→VMware
migration service line and the c1sql1 lab FCI
(evoila-bosnia/claude-rdc-hetzner-dc#2794). It is a **direct wire-protocol**
connector — it subclasses the generic
`meho_backplane.connectors.base.Connector` ABC (not the SSH / HTTP adapters)
and drives a direct **TDS** connection on port 1433, exactly like the
`postgres` (#2236) and `mongodb` (#2237) siblings.

Registry v2 triple: `("mssql", "2022.x", "mssql-tds")` plus the wildcard
fallback `("mssql", "", "")`. `supported_version_range = ">=13,<17"` covers
SQL Server 2016 (product version 13) through 2022 (16): the catalog / DMV
surface every read op queries is stable across those releases, so the same
connector serves an older migration *source* while the `2022.x` label names
its primary target. The product token is separator-free so
`connector_id="mssql-tds-2022.x"` round-trips through `parse_connector_id`
(product = first hyphen-segment of `impl_id` = `mssql`); a hyphen in the token
would crash `_eager_import_connectors` at boot on the registry's
`_assert_product_impl_id_round_trips` guard.

## Transport decision (the pivotal in-task decision)

The Initiative #3259 working recommendation — **TDS-direct on 1433 for the
core op table**, with dbatools-over-PowerShell-SSH evaluated as a possible
*second increment* for migration-grade verbs (`Start-DbaMigration`,
login/agent-job copy) — is **confirmed, not overturned**. The core ops
(instance / database / AG / FCI / backup facts + governed backup/restore) are
clean typed T-SQL against catalog views and utility statements; they need no
guest shell and work against any reachable instance, including
non-Windows-managed ones. dbatools is deferred to a later increment and is
**not** in this connector.

### Python driver choice: `python-tds` (pytds)

Three DBAPI drivers reach SQL Server from Python. The decision criteria are
the backplane image's packaging cost, maintenance state, async fit, and the
SQL 2022 TLS story.

| Driver | Native / system deps | Async | Maintenance | Notes |
|---|---|---|---|---|
| **python-tds** (`pytds`) | **none — pure Python** | sync (wrap in `asyncio.to_thread`) | 1.17.1, 2025-09-13; MIT | pure-Python TDS; DBAPI `pyformat`; TLS via `cafile` |
| pymssql | FreeTDS (C extension; wheels bundle it statically) | sync | active | wraps FreeTDS; encryption defaults track ODBC Driver 18 |
| pyodbc | **unixODBC + msodbcsql18** at runtime (apt layer + MS EULA binary) | sync | active | best SQL 2022 strict-encryption story; heaviest image cost |
| _(mssql-python)_ | bundled ODBC 18.6.x (native) | sync | GA 2025-11 (brand-new) | Microsoft's official driver; too new to adopt as the first driver |

**Chosen: `python-tds`.** It is the only **pure-Python** option — it adds a
single pip dependency exactly the way `asyncpg` does for the postgres
connector, with **no apt layer** (no unixODBC/msodbcsql, no FreeTDS) in the
backplane image. That packaging property is the decisive factor: pyodbc's
`msodbcsql18` runtime requirement (a Microsoft-EULA'd native binary) and
pymssql's FreeTDS C extension both add a native build/security surface the
pure-Python driver avoids. python-tds is actively maintained (1.17.1,
2025-09-13), MIT-licensed (permissive, compatible with the project's
Apache-2.0), and its DBAPI `pyformat` paramstyle gives clean parameter
binding for injection safety.

It is a **synchronous** driver, so every connect + execute + fetch round-trip
runs off the event loop in one `asyncio.to_thread` hop
(`meho_backplane.connectors.mssql.session`) — the same sync-driver-in-async
pattern the hvac Vault reads, the mail transport, and the rke2 write ops
already use.

Sources (fresh-cited at implementation time, 2026-09):
- python-tds 1.17.1 / release date / MIT / Python support:
  <https://pypi.org/project/python-tds/>
- pure-Python TDS, no FreeTDS/ADO; `cafile` TLS; `pyformat` paramstyle;
  `connect()` signature (verified by installed-library introspection of
  `pytds.connect` / `pytds.paramstyle`):
  <https://python-tds.readthedocs.io/en/stable/pytds.html>
- pyodbc / pymssql / SQL 2022 strict-encryption comparison:
  <https://learn.microsoft.com/en-us/sql/relational-databases/security/networking/tds-8>

## Two-credential Vault secret shape (first on this seam)

`mssql` is the **first two-credential connector** on the estate seam. SQL
authentication needs a `sql_username` / `sql_password` pair, resolved from the
target's `secret_ref` under the operator's identity via
`load_basic_credentials(target, operator, fields=("sql_username",
"sql_password"))`. The pair flows only into the `pytds` connect kwargs — never
a log line, an error string, or an `OperationResult`.

The fields are **prefixed `sql_`** deliberately: a later
dbatools-over-PowerShell increment will reach the same host over SSH and can
store its SSH credentials (`username` / `ssh_private_key` / `password` — the
`SshConnector._auth_config` shape) in the **same** Vault secret without
shadowing the TDS pair. So one target's secret can carry both credential sets.

Vault KV-v2 secret (path is the target's `secret_ref`, under the default
`secret` mount — the vault-store convention
`secret/rdc-hetzner-dc/<area>/<item>`):

```
secret/rdc-hetzner-dc/mssql/c1sql1
  sql_username = "meho_svc"
  sql_password = "<password>"
  # (future dbatools-over-SSH increment, same secret):
  # username        = "Administrator"
  # ssh_private_key = "-----BEGIN OPENSSH PRIVATE KEY----- ..."
```

Store it with the `/vault-store` skill (`scripts/vault-store.sh put`); never
pass a credential on argv. An operator-less dispatch (the readiness probe, the
legacy `execute` shim) fails closed before any connect — there is no
trust-auth TDS path.

## Op surface (14 ops across four groups)

Safety tier per op follows the Initiative #3259 satellite table — reads
`safe`, a recoverable write `caution`, destructive / data-overwriting writes
`dangerous` + `requires_approval` (satellite-excluded; a dispatch parks for a
human decision).

| op_id | group | tier | T-SQL / DMV |
|---|---|---|---|
| `mssql.instance.about` | instance | safe | `SERVERPROPERTY` (version / edition / clustering) |
| `mssql.instance.version` | instance | safe | `@@VERSION` + parsed product version |
| `mssql.instance.config` | instance | safe | `sys.configurations` |
| `mssql.instance.logins` | instance | safe | `sys.server_principals` (no password material) |
| `mssql.databases.list` | databases | safe | `sys.databases` + `sys.master_files` size |
| `mssql.databases.files` | databases | safe | `sys.master_files` (optional `database`) |
| `mssql.databases.create` | databases | **dangerous + approval** | `CREATE DATABASE` |
| `mssql.databases.drop` | databases | **dangerous + approval** | `DROP DATABASE` |
| `mssql.ha.availability-groups` | ha | safe | `sys.availability_groups` + `sys.dm_hadr_availability_replica_states` |
| `mssql.ha.fci` | ha | safe | `sys.dm_os_cluster_nodes` + `SERVERPROPERTY('IsClustered')` |
| `mssql.ha.sync-health` | ha | safe | `sys.dm_hadr_database_replica_states` |
| `mssql.backup.history` | backup | safe | `msdb.dbo.backupset` |
| `mssql.backup.database` | backup | **caution** | `BACKUP DATABASE ... TO DISK` |
| `mssql.backup.restore` | backup | **dangerous + approval** | `RESTORE DATABASE ... FROM DISK` (overwrites) |

The `ha` group is the migration-validation surface: an operator confirms an
AG/FCI is healthy and every database is `SYNCHRONIZED` / `HEALTHY` with shallow
send/redo queues before a cutover or planned failover.

Note the two deliberate tier choices, per the issue text: **`create` is
`dangerous`+approval** (not `caution`) — a database create is a schema-level
mutation on a governed instance that consumes resources and can collide with
existing state, so policy treats it as dangerous alongside `drop`; **`restore`
is `dangerous`+approval** because it overwrites the target database, while
`backup` is only `caution` (it produces a file and is recoverable).

Every list op returns the `{rows, total}` envelope the JSONFlux reducer keys
on (it detects the single `rows` collection and materialises it into a result
handle past the 50-row / 4 KB threshold). Every group carries a
`_WHEN_TO_USE_BY_GROUP` entry (registration fails closed without one).

## Non-goal: no freeform `query` op

There is **deliberately no** `mssql.query` (or any raw-T-SQL) op. This is the
narrow-waist doctrine (CLAUDE.md postulate 5): the agent surface is a curated,
individually-safety-tiered op table, never an arbitrary-SQL escape hatch that
would bypass per-op policy, tiering, and JSONFlux shaping. A test
(`test_no_freeform_query_op_exists`) pins the absence — it asserts no op id
contains `query` and no op accepts a `sql` / `statement` parameter. A future
read-only guarded-SELECT op (the postgres `postgres.query` shape: first-keyword
allowlist + a read-only session) could be added if a concrete consumer need
appears; it is not shipped now.

## T-SQL injection safety

The connector builds SQL, not PowerShell, so the discipline is
**parameter binding first** — the equivalent of the estate connectors'
`ps_single_quote`:

- **Values** (a file path, a `database` filter in a `WHERE` / `TOP`) bind
  through `pytds` pyformat placeholders (`%(name)s`). Operator input is never
  string-interpolated into the statement.
- **Identifiers** a DDL / utility statement names (`CREATE` / `DROP` /
  `BACKUP` / `RESTORE DATABASE [x]`) cannot be bound as parameters, so they
  ride `session.quote_identifier`: a strict pre-validation
  (`assert_valid_identifier` — non-empty, ≤128 chars per SQL Server's
  identifier limit, no control characters) then QUOTENAME-style bracket
  escaping (every `]` doubled inside `[...]`). A payload like
  `x]; DROP DATABASE loot --` becomes the single delimited token
  `[x]]; DROP DATABASE loot --]` — a database that does not exist, never a
  second statement.

## `fingerprint(target)` / `probe(target)`

`fingerprint` reads `SERVERPROPERTY` (product version, product level, edition,
machine / server / instance name, `IsClustered` / `IsHadrEnabled`) in one
round-trip → `vendor="microsoft"`, `product="mssql"`, `version=<product
version>`, `build=<product level>`. A connection or credential failure maps to
`reachable=False` with the error under `extras` (the #986 discipline), never a
raise. The `extras["error"]` value and the paired `mssql_fingerprint_unreachable`
warning log carry only the exception type name plus a classified reason
(`auth_failed` / `tcp_unreachable` / `connect_failed`, mirroring the probe
taxonomy below), never `str(exc)` — a pytds `LoginError` echoes the
Vault-sourced username (`Login failed for user '<u>'`), so the raw driver
message must not reach either surface (#3297; redaction seam
`connectors/_shared/fingerprint.py`).

`probe` is a `SELECT @@VERSION` reachability check with distinct reasons:
`auth_failed` (`pytds.LoginError`, or an unresolvable credential —
`CredentialsReadError` / `VaultClientError` / the operator-less `ValueError`),
`tcp_unreachable` (`OSError`, covering `TimeoutError`), `connect_failed`
(`pytds.Error` — e.g. the server requires encryption the connector did not
offer). Like the postgres probe, `probe` carries no operator, so a
credentialled target's secret read fails closed to `auth_failed`; reachability
is confirmed on the operator-carrying fingerprint / op path.

## Encryption posture

`pytds` enables TLS only when a `cafile` (trusted-CA PEM) is supplied; the
connector currently connects with `cafile=None`, so the client advertises
`ENCRYPT_NOT_SUP` in the TDS pre-login (`pytds.__init__` line 248). Against a
default (non-force-encryption) SQL Server 2022 the connection succeeds and the
channel is unencrypted after pre-login; against a **force-encryption** server
`pytds` raises a clear error and the connector reports `connect_failed`
(`tds_session.py` negotiation, line 1298). This mirrors the postgres
connector's posture (no explicit TLS config; connects against the lab target).

Cert-validated TLS for a force-encryption / strict-mode instance is a **named
future extension**, not built speculatively here: add an optional `sql_ca`
field to the Vault secret, write it to a mode-0600 temp file, and pass it as
`pytds.connect(cafile=...)` (which sets `enc_flag=ENCRYPT_ON` and validates the
server cert via `validate_host`). The lab c1sql1 FCI is a default-config
instance, so the current posture connects; production instances that force
encryption need the `sql_ca` follow-up.

## Tests

`backend/tests/test_connectors_mssql.py` — registration (versioned + wildcard
+ round-trip + `register_operations` upserts all 14), safety tiers vs the
satellite table, the no-freeform-query guard, schema-level secret-leak guard +
the two-credential field shape + operator-less fail-closed, `quote_identifier`
bracket-escaping + `assert_valid_identifier` rejection, every write op escaping
its identifier and binding its path, the `{rows, total}` envelopes, and
fingerprint / probe reason mapping. The wire is faked by patching the `queries`
transport seam — no live SQL Server or Vault is needed. There is **no
spec-reconcile lane** (a typed connector ships no OpenAPI; the Initiative #3259
convention).

## CLI

Every op is reachable as `meho mssql ...` through the generated CLI. The CLI
is generated from the backend OpenAPI, so adding the `mssql` product token
grows the CLI snapshot (`cli/api/openapi.json` + the generated Go client);
regenerate with `cd cli && make snapshot-openapi && make generate`.

## Known issues / scope

- **Consumer proof (live c1sql1 FCI probe) is OPEN** — the acceptance criterion
  that requires probing the live c1sql1 FCI (fingerprint version/edition,
  governed backup against a test database, AG/FCI state reads) is **not
  satisfiable in the sandbox** (no reachable lab instance + no Vault-provisioned
  secret). It is declared OPEN honestly (the estate-connector precedent), to be
  closed against the deployed lab.
- **dbatools-over-PowerShell increment is deferred** — migration-grade verbs
  (`Start-DbaMigration`, login/agent-job copy) that are prohibitively complex
  over raw T-SQL are a possible second increment, not this connector.
- **Cert-validated TLS is deferred** — see the encryption-posture section.

## References

- `backend/src/meho_backplane/connectors/mssql/` — `session.py` (transport +
  creds + injection helpers), `queries.py` (T-SQL + `{rows, total}` shaping),
  `ops.py` (op metadata), `connector.py` (op surface + fingerprint / probe),
  `__init__.py` (self-registration).
- `connectors-winsrv.md` / `connectors-bind9.md` — sibling connector docs.
- `connectors/postgres/` + `connectors/mongodb/` — the direct-protocol mold.
- Initiative #3259 (Microsoft estate connectors) design rules; Task #3264.
- SQL Server system catalog views:
  <https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/>
