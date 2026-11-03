# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Extra guards for the free-form ``postgres.query`` operation.

The read-only session (``default_transaction_read_only=on``) stops writes
to tables. It does not stop server admin functions that PostgreSQL still
allows inside a read-only transaction: ending other sessions, reloading the
server config, reading files on the server, controlling backups and
replication, or opening a connection to another server. The login role's
rights decide what such a call can do, and a superuser role can do all of
it. So ``postgres.query`` adds two checks:

* :func:`assert_no_admin_functions` refuses, before a connection is opened,
  any statement that names one of the functions in :data:`DENIED_FUNCTIONS`
  or starts a name with one of :data:`DENIED_FUNCTION_PREFIXES`. The check
  looks at every identifier-like word in the raw statement text, also
  inside comments and string literals, so no quoting trick can hide a name.
  A false hit (a column that happens to be called ``pg_sleep``) only gives
  a clear refusal. Unicode-escaped identifiers (``U&"..."``) are refused as
  a whole, because they can spell a name letter by letter.

* :func:`assert_not_superuser` refuses to run the statement when the login
  role is a superuser, unless the target sets
  ``extras["allow_superuser_query"] = true``. The curated operations keep
  running as any role: their SQL is fixed catalog reads.

Function names are pinned to the PostgreSQL 16 documentation, section
"System Administration Functions"
(https://www.postgresql.org/docs/16/functions-admin.html), plus the
``dblink`` and ``postgres_fdw`` extension functions that open remote
connections.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import asyncpg

from meho_backplane.connectors.postgres.session import PostgresReadOnlyError

__all__ = [
    "ALLOW_SUPERUSER_QUERY_EXTRA",
    "DENIED_FUNCTIONS",
    "DENIED_FUNCTION_PREFIXES",
    "PostgresSuperuserError",
    "assert_no_admin_functions",
    "assert_not_superuser",
    "denied_function_in",
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
)

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


def assert_no_admin_functions(sql: str) -> None:
    """Raise :class:`PostgresReadOnlyError` when *sql* names a denied function.

    Runs before a connection is opened, after the first-keyword gate.
    """
    if _UNICODE_ESCAPED_IDENTIFIER_RE.search(sql):
        raise PostgresReadOnlyError(
            'postgres.query does not accept Unicode-escaped identifiers (U&"..."); '
            "write the identifier in plain form."
        )
    name = denied_function_in(sql)
    if name is not None:
        raise PostgresReadOnlyError(
            f"postgres.query refuses the statement: it names {name!r}, a server "
            "admin, file, backup, replication, wait or remote-connection function. "
            "Only plain reads of tables and catalogs are accepted."
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
