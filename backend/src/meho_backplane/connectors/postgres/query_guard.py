# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Extra guards for the free-form ``postgres.query`` operation.

The read-only session (``default_transaction_read_only=on``) stops writes
to tables. It does not stop server admin functions that PostgreSQL still
allows inside a read-only transaction: ending other sessions, reloading the
server config, reading files on the server, controlling backups and
replication, or opening a connection to another server. The login role's
rights decide what such a call can do, and a superuser role can do all of
it. So ``postgres.query`` adds three checks:

* :func:`assert_no_admin_functions` refuses, before a connection is opened,
  any statement that names one of the functions in :data:`DENIED_FUNCTIONS`
  or starts a name with one of :data:`DENIED_FUNCTION_PREFIXES`. The check
  looks at every identifier-like word in the raw statement text, also
  inside comments and string literals, so no quoting trick can hide a name.
  A false hit (a column that happens to be called ``pg_sleep``) only gives
  a clear refusal. Unicode-escaped identifiers (``U&"..."``) are refused as
  a whole, because they can spell a name letter by letter.

  The list also holds every built-in function that runs a SQL string given
  as an argument (``query_to_xml``, ``ts_stat``, ``ts_rewrite``, the
  ``tablefunc`` functions ``crosstab`` and ``connectby``). Without them a
  statement could build a denied name from pieces (``'pg_' ||
  'terminate_backend(...)'``) and hand it to the server as a string, which
  the word scan cannot see. For the same reason it holds the functions that
  read every row of a table or schema chosen by a value (``table_to_xml``,
  ``schema_to_xml``, ``database_to_xml`` and their variants): the value can
  be built from pieces too.

* :func:`assert_no_secret_catalogs` refuses, before a connection is opened,
  any statement that names one of the server catalogs, views or functions
  in :data:`DENIED_RELATIONS`. Each of them can show a stored password, a
  connection string with a password, the SQL text of other sessions, a
  server setting or sampled column values. It uses the same word scan and
  the same refusal of Unicode-escaped identifiers. It also refuses
  ``SHOW ALL`` and a ``SHOW`` of a setting in
  :data:`CONNECTION_TEXT_SETTINGS`. The check holds for every login, also
  on a target that allows a superuser. Settings are read with the curated
  ``postgres.settings``, which puts a fixed mark in place of a connection
  text.

* :func:`assert_not_superuser` refuses to run the statement when the login
  role is a superuser, unless the target sets
  ``extras["allow_superuser_query"] = true``. The curated operations keep
  running as any role: their SQL is fixed catalog reads.

Function names are pinned to the PostgreSQL 16 documentation: section
"System Administration Functions"
(https://www.postgresql.org/docs/16/functions-admin.html); the SQL-running
functions of "XML Functions"
(https://www.postgresql.org/docs/16/functions-xml.html, "Mapping Tables to
XML") and "Text Search Functions and Operators"
(https://www.postgresql.org/docs/16/functions-textsearch.html); plus the
``dblink`` and ``postgres_fdw`` extension functions that open remote
connections and the ``tablefunc`` extension functions that run SQL text
(https://www.postgresql.org/docs/16/tablefunc.html). Catalog names are
pinned to the PostgreSQL 16 pages "System Catalogs"
(https://www.postgresql.org/docs/16/catalogs.html), "System Views"
(https://www.postgresql.org/docs/16/views.html), "The Cumulative Statistics
System" (https://www.postgresql.org/docs/16/monitoring-stats.html), the
information schema view ``user_mapping_options``
(https://www.postgresql.org/docs/16/infoschema-user-mapping-options.html)
the ``pg_stat_statements`` extension
(https://www.postgresql.org/docs/16/pgstatstatements.html), the ``SHOW``
command (https://www.postgresql.org/docs/16/sql-show.html) and the setting
functions of "System Administration Functions".
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import asyncpg

from meho_backplane.connectors.postgres.session import (
    PostgresReadOnlyError,
    first_significant_keyword,
)

__all__ = [
    "ALLOW_SUPERUSER_QUERY_EXTRA",
    "CONNECTION_TEXT_SETTINGS",
    "DENIED_FUNCTIONS",
    "DENIED_FUNCTION_PREFIXES",
    "DENIED_RELATIONS",
    "PostgresSuperuserError",
    "assert_no_admin_functions",
    "assert_no_secret_catalogs",
    "assert_not_superuser",
    "denied_function_in",
    "denied_relation_in",
]

#: The target ``extras`` key that lets ``postgres.query`` run as a superuser.
ALLOW_SUPERUSER_QUERY_EXTRA = "allow_superuser_query"

#: Function names (lower case, no schema) that ``postgres.query`` refuses.
DENIED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # Server signalling.
        "pg_cancel_backend",
        "pg_terminate_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_log_backend_memory_contexts",
        # Session settings (could turn the read-only default or the
        # statement timeout off for a later transaction).
        "set_config",
        # Server file access.
        "pg_read_file",
        "pg_read_binary_file",
        "pg_stat_file",
        "pg_file_write",
        "pg_file_rename",
        "pg_file_unlink",
        "pg_file_sync",
        "lo_import",
        "lo_export",
        # Backup control and WAL.
        "pg_backup_start",
        "pg_backup_stop",
        "pg_start_backup",
        "pg_stop_backup",
        "pg_switch_wal",
        "pg_create_restore_point",
        "pg_logical_emit_message",
        # Recovery control.
        "pg_promote",
        "pg_wal_replay_pause",
        "pg_wal_replay_resume",
        # Replication slots and origins.
        "pg_create_physical_replication_slot",
        "pg_create_logical_replication_slot",
        "pg_copy_physical_replication_slot",
        "pg_copy_logical_replication_slot",
        "pg_drop_replication_slot",
        "pg_replication_slot_advance",
        "pg_replication_origin_create",
        "pg_replication_origin_drop",
        "pg_replication_origin_advance",
        "pg_replication_origin_session_setup",
        "pg_replication_origin_session_reset",
        "pg_replication_origin_xact_setup",
        "pg_replication_origin_xact_reset",
        # Statistics resets and notifications.
        "pg_stat_statements_reset",
        "pg_notify",
        # Built-ins that run a SQL string given as an argument. They could
        # run a denied name that the statement builds from pieces.
        "ts_stat",
        "ts_rewrite",
        "connectby",
    }
)

#: Name prefixes (lower case) that ``postgres.query`` refuses. Each prefix
#: covers one function family and no catalog view or role name.
DENIED_FUNCTION_PREFIXES: tuple[str, ...] = (
    "pg_ls_",  # pg_ls_dir, pg_ls_logdir, pg_ls_waldir, pg_ls_tmpdir, ...
    "pg_logical_slot_",  # read or consume a logical replication slot
    "pg_stat_reset",  # pg_stat_reset, pg_stat_reset_shared, ..._single_*
    "pg_sleep",  # pg_sleep, pg_sleep_for, pg_sleep_until
    "dblink",  # the dblink extension: remote connections and queries
    "postgres_fdw_",  # postgres_fdw_disconnect, ..._disconnect_all, ...
    "query_to_xml",  # query_to_xml, query_to_xmlschema, ..._and_xmlschema: run SQL text
    "cursor_to_xml",  # cursor_to_xml, cursor_to_xmlschema: read a cursor
    "crosstab",  # tablefunc crosstab, crosstab2..4, crosstab_N: run SQL text
    # Read every row of a table or schema chosen by a value, which a
    # statement can build from pieces: table_to_xml, table_to_xmlschema,
    # table_to_xml_and_xmlschema, and the schema_* and database_* variants.
    "table_to_xml",
    "schema_to_xml",
    "database_to_xml",
)

#: Server catalogs, views and functions (lower case, no schema) that
#: ``postgres.query`` refuses. Each can show a stored password, a connection
#: string with a password, the SQL text of other sessions, a server setting
#: or sampled column values. ``pg_roles``, ``pg_user``,
#: ``pg_stat_ssl``, ``pg_stat_subscription`` and ``pg_stat_wal_receiver``
#: (its connection string hides the password) stay allowed.
DENIED_RELATIONS: frozenset[str] = frozenset(
    {
        # The password hash (SCRAM verifier or MD5) of every role.
        "pg_authid",
        "pg_shadow",
        # The connection string of a logical replication subscription.
        "pg_subscription",
        # The options of a foreign server login, with its password.
        "pg_user_mapping",
        "pg_user_mappings",
        "_pg_user_mappings",
        "user_mapping_options",
        # The SQL text of other sessions.
        "pg_stat_activity",
        "pg_stat_get_activity",
        "pg_stat_get_backend_activity",
        # The pg_stat_statements extension: the SQL text of past statements.
        # Its info view is refused with it, so the extension is refused as
        # one unit.
        "pg_stat_statements",
        "pg_stat_statements_info",
        # Server settings. A setting can hold a connection string with a
        # password; postgres.settings hides such a value.
        "pg_settings",
        "pg_file_settings",
        "pg_show_all_settings",
        "pg_show_all_file_settings",
        "current_setting",
        # Column statistics: sampled values of table columns.
        "pg_statistic",
        "pg_stats",
        "pg_stats_ext",
        "pg_stats_ext_exprs",
        "pg_statistic_ext_data",
    }
)

#: Settings (lower case) whose value is a connection text. ``postgres.query``
#: refuses a ``SHOW`` of them, and ``postgres.settings`` puts a fixed mark in
#: place of their value.
CONNECTION_TEXT_SETTINGS: frozenset[str] = frozenset({"primary_conninfo"})

_SETTINGS_OP = "postgres.settings"

#: The curated operation to use instead, for a denied catalog that has one.
_CURATED_INSTEAD: dict[str, str] = {
    "pg_stat_activity": "postgres.activity",
    "pg_stat_get_activity": "postgres.activity",
    "pg_stat_get_backend_activity": "postgres.activity",
    "pg_settings": _SETTINGS_OP,
    "pg_file_settings": _SETTINGS_OP,
    "pg_show_all_settings": _SETTINGS_OP,
    "pg_show_all_file_settings": _SETTINGS_OP,
    "current_setting": _SETTINGS_OP,
}

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_UNICODE_ESCAPED_IDENTIFIER_RE = re.compile(r"[Uu]&\"")


class PostgresSuperuserError(ValueError):
    """``postgres.query`` refused to run because the login role is a superuser.

    Subclasses :class:`ValueError` so the dispatcher's ``connector_error``
    branch renders the message verbatim.
    """


def denied_function_in(sql: str) -> str | None:
    """Return the first denied function name found in *sql*, else ``None``.

    Every identifier-like word is lower-cased and compared with
    :data:`DENIED_FUNCTIONS` and :data:`DENIED_FUNCTION_PREFIXES`. Comments,
    string literals and double quotes are not stripped first: scanning the
    raw text cannot be tricked by an unclosed quote, and a false hit only
    costs a clear refusal.
    """
    for match in _IDENTIFIER_RE.finditer(sql):
        word = match.group(0).lower()
        if word in DENIED_FUNCTIONS or word.startswith(DENIED_FUNCTION_PREFIXES):
            return word
    return None


def denied_relation_in(sql: str) -> str | None:
    """Return the first denied catalog name found in *sql*, else ``None``.

    The same raw word scan as :func:`denied_function_in`, compared with
    :data:`DENIED_RELATIONS`.
    """
    for match in _IDENTIFIER_RE.finditer(sql):
        word = match.group(0).lower()
        if word in DENIED_RELATIONS:
            return word
    return None


def _refuse_unicode_escapes(sql: str) -> None:
    if _UNICODE_ESCAPED_IDENTIFIER_RE.search(sql):
        raise PostgresReadOnlyError(
            'postgres.query does not accept Unicode-escaped identifiers (U&"..."); '
            "write the identifier in plain form."
        )


def assert_no_admin_functions(sql: str) -> None:
    """Raise :class:`PostgresReadOnlyError` when *sql* names a denied function.

    Runs before a connection is opened, after the first-keyword gate.
    """
    _refuse_unicode_escapes(sql)
    name = denied_function_in(sql)
    if name is not None:
        raise PostgresReadOnlyError(
            f"postgres.query refuses the statement: it names {name!r}, a server "
            "admin, file, backup, replication, wait, remote-connection, "
            "SQL-running or table-dumping function. Only plain reads of tables "
            "and catalogs are accepted."
        )


def _refuse_show_of_secret_settings(sql: str) -> None:
    """Refuse ``SHOW ALL`` and a ``SHOW`` of a connection-text setting.

    Every identifier-like word of a ``SHOW`` statement is checked, so a
    comment, double quotes or upper case do not hide ``all`` or a name.
    """
    if first_significant_keyword(sql) != "SHOW":
        return
    for match in _IDENTIFIER_RE.finditer(sql):
        word = match.group(0).lower()
        if word == "all" or word in CONNECTION_TEXT_SETTINGS:
            raise PostgresReadOnlyError(
                "postgres.query refuses SHOW ALL and a SHOW of a setting that "
                f"holds a connection text. Use {_SETTINGS_OP} instead."
            )


def assert_no_secret_catalogs(sql: str) -> None:
    """Raise :class:`PostgresReadOnlyError` when *sql* names a denied catalog.

    Also refuses ``SHOW ALL`` and a ``SHOW`` of a setting in
    :data:`CONNECTION_TEXT_SETTINGS`. Runs before a connection is opened, for
    every login and every target.
    """
    _refuse_unicode_escapes(sql)
    _refuse_show_of_secret_settings(sql)
    name = denied_relation_in(sql)
    if name is None:
        return
    instead = _CURATED_INSTEAD.get(name)
    hint = f" Use {instead} instead." if instead else ""
    raise PostgresReadOnlyError(
        f"postgres.query refuses the statement: it names {name!r}, a server "
        "catalog, view or function that can show a stored password, a "
        "connection string, the SQL text of other sessions, a server setting "
        f"or sampled column values.{hint}"
    )


_IS_SUPERUSER_SQL = "SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user"


def _allows_superuser(target: Any) -> bool:
    extras = getattr(target, "extras", None)
    if not isinstance(extras, Mapping):
        return False
    return extras.get(ALLOW_SUPERUSER_QUERY_EXTRA) is True


async def assert_not_superuser(conn: asyncpg.Connection, target: Any) -> None:
    """Raise :class:`PostgresSuperuserError` when the login role is a superuser.

    Reads ``pg_roles.rolsuper`` for the current role over *conn*. Any answer
    other than ``False`` refuses: a missing row or an odd value is not taken
    as "not a superuser". A target with
    ``extras["allow_superuser_query"] = true`` skips the check.
    """
    if _allows_superuser(target):
        return
    is_superuser = await conn.fetchval(_IS_SUPERUSER_SQL)
    if is_superuser is False:
        return
    name = getattr(target, "name", target)
    if is_superuser is True:
        reason = "the login role is a superuser"
    else:
        reason = f"the login role's superuser flag could not be read (got {is_superuser!r})"
    raise PostgresSuperuserError(
        f"postgres.query refuses to run on target {name!r}: {reason}. Use a "
        "credential with a role that is not a superuser, or set "
        f"extras.{ALLOW_SUPERUSER_QUERY_EXTRA}=true on the target to allow it."
    )
