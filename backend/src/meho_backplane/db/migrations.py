# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""DB-migration-state readiness probe + Alembic configuration helpers.

The probe answers a single operational question: *is the database
schema at the revision this code expects?* Three failure modes — DB
unreachable, ``alembic_version`` table missing, current revision
diverges from the code's head — collapse into a single
:class:`~meho_backplane.health.ProbeResult` with ``ok=False`` and a
short structured detail string. The probe is registered against the
shared readiness registry from :mod:`meho_backplane.main`'s lifespan
hook so ``/ready`` returns 503 (and the kubelet keeps the pod out of
service traffic) until the schema matches.

The helper :func:`alembic_config` resolves and caches the on-disk
``alembic.ini``; both the probe and the migration runner (T29) share
a single :class:`alembic.config.Config` instance to avoid drift
between what the runner *applied* and what the probe *expects*.

References
----------
* https://alembic.sqlalchemy.org/en/latest/api/runtime.html#alembic.runtime.migration.MigrationContext
* https://alembic.sqlalchemy.org/en/latest/api/script.html#alembic.script.ScriptDirectory
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

from meho_backplane.db.engine import get_engine
from meho_backplane.health import ProbeResult

__all__ = [
    "alembic_config",
    "db_migration_probe",
    "find_alembic_ini",
]

_log = logging.getLogger(__name__)

#: Filename Alembic conventionally reads from. The probe and the
#: migration runner both look this up via :func:`find_alembic_ini`,
#: so an ops-time relocation only requires one knob change.
_ALEMBIC_INI_NAME: str = "alembic.ini"


def find_alembic_ini() -> Path:
    """Resolve the on-disk path to ``alembic.ini``.

    Looks two places in order:

    1. The current working directory. This matches the ``alembic ...``
       CLI's own resolution rule and is the path the migration runner
       hits in containers (where ``WORKDIR`` is ``/app/backend``).
    2. The package layout — walks up from ``meho_backplane.db`` to
       find the ``backend/`` directory containing ``alembic.ini``.
       This is what the readiness probe hits when running under
       ``pytest`` from arbitrary cwds.

    Raises :class:`FileNotFoundError` when neither location holds the
    file; callers translate that to ``ok=False`` with a stable detail
    string.
    """
    cwd_candidate = Path.cwd() / _ALEMBIC_INI_NAME
    if cwd_candidate.is_file():
        return cwd_candidate
    # ``__file__`` is .../backend/src/meho_backplane/db/migrations.py;
    # parents[3] resolves to .../backend, which is where alembic.ini lives.
    package_candidate = Path(__file__).resolve().parents[3] / _ALEMBIC_INI_NAME
    if package_candidate.is_file():
        return package_candidate
    raise FileNotFoundError(
        f"alembic.ini not found at {cwd_candidate} or {package_candidate}",
    )


def alembic_config(ini_path: Path | None = None) -> Config:
    """Return an :class:`alembic.config.Config` rooted at *ini_path*.

    *ini_path* defaults to :func:`find_alembic_ini`. The returned
    Config is **not** cached — it is cheap to construct and the
    caller may want to override ``script_location`` for offline /
    test scenarios.
    """
    resolved = ini_path or find_alembic_ini()
    return Config(str(resolved))


def _read_current_revision(connection: Connection) -> str | None:
    """Return the database's currently-applied revision, or ``None``.

    Wraps :class:`alembic.runtime.migration.MigrationContext` to read
    the version. Returns ``None`` when the ``alembic_version`` table
    is absent (fresh DB before ``upgrade head``) — the table is
    created on the first migration run, so its absence is a
    legitimate "not yet migrated" signal rather than a probe failure.
    """
    context = MigrationContext.configure(connection)
    return context.get_current_revision()


async def db_migration_probe() -> ProbeResult:
    """Compare the DB's Alembic revision to the code's head.

    Three observable outcomes:

    * **healthy** — database reachable, ``alembic_version`` table
      readable, current revision matches the head defined by the
      ``versions/`` directory on disk. Detail carries
      ``revision=<sha>``.
    * **unhealthy (revision diverged)** — current and head both
      resolved but they differ. Detail carries
      ``current=<sha> head=<sha>``. Operators see this when a
      forward-deploy missed an ``alembic upgrade head`` or when a
      rollback returned the image but not the schema.
    * **unhealthy (DB / config error)** — DB unreachable, table
      missing, ini missing, etc. Detail carries
      ``check_failed: <ExcClass>`` — the class name only, not the
      message, mirroring the redaction discipline of the Vault
      federation probe in T24 (operator-controllable substrings must
      not leak into a ``/ready`` payload).

    The function is ``async`` because the canonical SQLAlchemy 2.x
    pattern reads the version through an ``AsyncEngine.connect()`` /
    ``run_sync`` pair; it is registered as an async probe by
    :mod:`meho_backplane.main`'s lifespan hook.
    """
    try:
        cfg = alembic_config()
        head = ScriptDirectory.from_config(cfg).get_current_head()
        engine = get_engine()
        async with engine.connect() as conn:
            current = await conn.run_sync(_read_current_revision)
    except Exception as exc:
        _log.warning(
            "db_migration_probe_failed",
            extra={"exc_type": type(exc).__name__},
        )
        return ProbeResult(
            name="db",
            ok=False,
            detail=f"check_failed: {type(exc).__name__}",
        )
    if head is None and current is None:
        # No migrations on disk and none applied. Treat as not-ready
        # rather than vacuously-ready: the chassis ships with an
        # empty ``versions/`` so the smoke test still flips ``/ready``
        # red until T28's first migration lands.
        return ProbeResult(
            name="db",
            ok=False,
            detail="no_migrations: head and current are both unset",
        )
    if current == head:
        return ProbeResult(
            name="db",
            ok=True,
            detail=f"revision={head}",
        )
    return ProbeResult(
        name="db",
        ok=False,
        detail=f"current={current} head={head}",
    )
