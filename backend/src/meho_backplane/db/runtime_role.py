# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The database login of the running backplane, and what it may do.

The migration runner (:mod:`meho_backplane.db.migrate`) logs in as the
owner of the schema. The running backplane can use a second, narrower
login instead: the runtime role. The chart names it in the environment
variable ``DATABASE_RUNTIME_ROLE`` of the migration container. After every
``alembic upgrade head`` the runner gives that role the rights it needs,
as the owner, in one transaction:

* ``SELECT``, ``INSERT``, ``UPDATE`` and ``DELETE`` on every table of the
  ``public`` schema, and the use of every sequence there. The same rights
  are set as the defaults for new objects, so a table a later migration
  creates gets them too.
* On ``audit_log`` only ``SELECT``, ``INSERT`` and ``UPDATE`` of the one
  column ``raw_payload`` (the weekly age-off of
  :mod:`meho_backplane.audit_retention`). No ``DELETE``, ``TRUNCATE`` or
  ``TRIGGER``. The role does not own the table, so it cannot disable the
  append-only trigger of migration ``0100`` either. The audit log stays a
  record the running service can add to, but not change or remove.

Who creates the role is not this module's job: the built-in PostgreSQL of
the chart creates it from its SQL file, and a bring-your-own install
creates it by hand (``docs/codebase/migrations.md``, "The runtime role").
The runner waits for the role to exist, because with the built-in
PostgreSQL the role is created by a sidecar that may start a moment after
the migration container. The statements are safe to run twice, so every
run applies them again.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

__all__ = [
    "RUNTIME_ROLE_ENV_VAR",
    "RuntimeRoleMissingError",
    "apply_runtime_grants",
    "runtime_role_from_env",
    "wait_for_runtime_role",
]

#: The environment variable of the migration container that names the
#: runtime role. Empty or unset means one login for everything, as before.
RUNTIME_ROLE_ENV_VAR: Final = "DATABASE_RUNTIME_ROLE"

#: A plain, lower-case SQL identifier. The name goes into ``GRANT``
#: statements, so it is checked here instead of quoted.
_ROLE_NAME: Final = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_ROLE_RETRY_SECONDS: Final = 2.0
_ROLE_REPORT_EVERY_SECONDS: Final = 15.0

#: The rights of the runtime role, in order. ``{role}`` is the checked name.
#: The ``REVOKE`` on ``audit_log`` comes after the schema-wide ``GRANT`` and
#: takes the table's column rights with it, so the two ``GRANT`` lines after
#: it leave exactly ``SELECT``, ``INSERT`` and ``UPDATE (raw_payload)``.
_GRANTS: Final[tuple[str, ...]] = (
    "GRANT USAGE ON SCHEMA public TO {role}",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
    "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {role}",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {role}",
    "REVOKE ALL ON TABLE audit_log FROM {role}",
    "GRANT SELECT, INSERT ON TABLE audit_log TO {role}",
    "GRANT UPDATE (raw_payload) ON TABLE audit_log TO {role}",
)


class RuntimeRoleMissingError(Exception):
    """The runtime role did not appear in ``pg_roles`` within the wait limit."""


def runtime_role_from_env(environ: Mapping[str, str] = os.environ) -> str | None:
    """Return the checked role name from :data:`RUNTIME_ROLE_ENV_VAR`, or ``None``.

    Unset or blank means no runtime role. A name that is not a plain
    lower-case identifier raises :class:`ValueError`, so a typo fails the
    run with a clear message instead of a ``GRANT`` to the wrong name.
    """
    raw = environ.get(RUNTIME_ROLE_ENV_VAR, "").strip()
    if not raw:
        return None
    if not _ROLE_NAME.match(raw):
        raise ValueError(
            f"{RUNTIME_ROLE_ENV_VAR} must be a lower-case PostgreSQL role name "
            f"(letters, digits and _, up to 63 characters); got {raw!r}"
        )
    return raw


async def _role_exists(conn: AsyncConnection, role: str) -> bool:
    found = await conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role})
    await conn.rollback()
    return found is not None


async def wait_for_runtime_role(
    conn: AsyncConnection,
    role: str,
    *,
    limit_seconds: float,
    say: Callable[[str], None],
) -> None:
    """Wait until *role* exists, for at most *limit_seconds*.

    Prints ``migration_runtime_role_waiting`` about every 15 seconds and
    raises :class:`RuntimeRoleMissingError` at the limit.
    """
    started = time.monotonic()
    deadline = started + limit_seconds
    last_report = -math.inf
    while not await _role_exists(conn, role):
        now = time.monotonic()
        if now >= deadline:
            raise RuntimeRoleMissingError(
                f"the database has no role {role!r} after {limit_seconds:g} s "
                f"({RUNTIME_ROLE_ENV_VAR}); create the role, or clear the variable"
            )
        if now - last_report >= _ROLE_REPORT_EVERY_SECONDS:
            say(
                f"migration_runtime_role_waiting: no role {role!r} yet; "
                f"waited {now - started:.0f} s of {limit_seconds:g} s"
            )
            last_report = now
        await asyncio.sleep(min(_ROLE_RETRY_SECONDS, deadline - now))


async def apply_runtime_grants(
    conn: AsyncConnection, role: str, *, say: Callable[[str], None]
) -> None:
    """Give *role* its rights (module docstring) in one committed transaction."""
    if not _ROLE_NAME.match(role):
        raise ValueError(f"not a plain role name: {role!r}")
    for statement in _GRANTS:
        await conn.execute(text(statement.format(role=role)))
    await conn.commit()
    say(f"migration_runtime_role_granted: {role}")
