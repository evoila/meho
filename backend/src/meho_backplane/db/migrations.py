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
import os
from importlib import resources
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
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

#: Env-var ops can set to point the probe / migration runner at an
#: arbitrary ``alembic.ini`` — escape hatch for deployments where the
#: file lives outside both the working directory and the installed
#: package (e.g. mounted from a ConfigMap in Kubernetes).
_ALEMBIC_CONFIG_ENV_VAR: str = "ALEMBIC_CONFIG"


def find_alembic_ini() -> Path:
    """Resolve the on-disk path to ``alembic.ini``.

    Looks four places in order so the helper works in every deployment
    shape — operator override, installed wheel, source-tree dev, and
    arbitrary-cwd test runners:

    1. ``$ALEMBIC_CONFIG`` env var — explicit ops override; used as-is
       (no existence check beyond the final :class:`FileNotFoundError`
       message), which lets operators get an actionable error message
       when they typo the path.
    2. Package data — :func:`importlib.resources.files` against
       :mod:`meho_backplane`. This is the path installed-wheel
       deployments hit; ``alembic.ini`` is shipped as package data
       via ``[tool.hatch.build.targets.wheel.force-include]`` in
       ``pyproject.toml``.
    3. The current working directory. Matches the ``alembic ...`` CLI's
       own resolution rule and is the path the migration runner hits
       in containers (where ``WORKDIR`` is ``/app/backend``).
    4. The source-tree layout — walks up from
       :mod:`meho_backplane.db` to find the ``backend/`` directory
       containing ``alembic.ini``. This is what the readiness probe
       hits when running under ``pytest`` from arbitrary cwds against
       an editable install.

    Raises :class:`FileNotFoundError` naming the env var and the
    package lookup when no candidate resolves.
    """
    env_override = os.environ.get(_ALEMBIC_CONFIG_ENV_VAR)
    if env_override:
        env_candidate = Path(env_override)
        if env_candidate.is_file():
            return env_candidate
        raise FileNotFoundError(
            f"alembic.ini not found at {env_candidate} (set via ${_ALEMBIC_CONFIG_ENV_VAR})",
        )
    # ``importlib.resources.files`` returns a ``Traversable``; for the
    # editable / unpacked-wheel case it is a real :class:`Path`. We
    # only honour it when it actually points at an existing file so
    # editable installs (where the package data is *not* materialised
    # adjacent to the package source) fall through to the cwd /
    # source-tree probes below.
    package_traversable = resources.files("meho_backplane").joinpath(_ALEMBIC_INI_NAME)
    package_candidate = Path(str(package_traversable))
    if package_candidate.is_file():
        return package_candidate
    cwd_candidate = Path.cwd() / _ALEMBIC_INI_NAME
    if cwd_candidate.is_file():
        return cwd_candidate
    # ``__file__`` is .../backend/src/meho_backplane/db/migrations.py;
    # parents[3] resolves to .../backend, which is where alembic.ini lives.
    source_tree_candidate = Path(__file__).resolve().parents[3] / _ALEMBIC_INI_NAME
    if source_tree_candidate.is_file():
        return source_tree_candidate
    raise FileNotFoundError(
        f"alembic.ini not found via ${_ALEMBIC_CONFIG_ENV_VAR}, "
        f"importlib.resources('meho_backplane') ({package_candidate}), "
        f"cwd ({cwd_candidate}), "
        f"or source tree ({source_tree_candidate})",
    )


def alembic_config(ini_path: Path | None = None) -> Config:
    """Return an :class:`alembic.config.Config` rooted at *ini_path*.

    *ini_path* defaults to :func:`find_alembic_ini`. The returned
    Config is **not** cached — it is cheap to construct and the
    caller may want to override ``script_location`` for offline /
    test scenarios.

    The on-disk ``alembic.ini`` ships ``script_location = alembic``
    (relative) for source-tree dev ergonomics — running
    ``alembic upgrade head`` from ``backend/`` Just Works.  Alembic's
    ``coerce_resource_to_filename`` only does package-resource
    resolution for values containing a colon (e.g. ``pkg:path``);
    plain ``alembic`` is treated as a cwd-relative filename. That
    breaks the installed-wheel path: the migration Job's container
    has ``WORKDIR /app`` but the wheel's ``alembic/`` lives under
    ``site-packages/meho_backplane/alembic/`` — Alembic fails with
    ``Path doesn't exist: alembic`` (issue #205).

    The convention in every resolution path of :func:`find_alembic_ini`
    is that ``alembic/`` lives **adjacent to** ``alembic.ini`` (the
    wheel's ``force-include`` ships them as siblings, the source
    tree has them as siblings under ``backend/``, the cwd-fallback
    and env-override patterns are the same). Override
    ``script_location`` to that absolute path so Alembic finds the
    scripts regardless of cwd. Guard with ``is_dir()`` so an exotic
    layout where someone points ``$ALEMBIC_CONFIG`` at an ini-only
    file falls through to whatever the ini said (caller can still
    set ``script_location`` themselves; we don't lock it).
    """
    resolved = ini_path or find_alembic_ini()
    cfg = Config(str(resolved))
    script_dir = resolved.parent / "alembic"
    if script_dir.is_dir():
        cfg.set_main_option("script_location", str(script_dir))
    return cfg


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


def _check_pgvector_extension(connection: Connection) -> bool:
    """Return ``True`` iff the ``vector`` extension is enabled.

    Queries ``pg_extension`` (the canonical PostgreSQL catalog for
    installed extensions). Migration ``0003`` (G0.4-T1) runs
    ``CREATE EXTENSION IF NOT EXISTS vector`` as part of
    ``alembic upgrade head``, so on a freshly-deployed cluster the
    extension is always present after the migration Job runs. This
    probe is the belt-and-suspenders second check that flips
    ``/ready`` red when:

    * An operator manually ran ``DROP EXTENSION vector CASCADE`` on
      a live cluster (which silently dropped the
      ``documents.embedding`` column type too — retrieval is
      broken until the column is recreated).
    * The cluster was restored from a backup taken before migration
      ``0003`` ran, leaving the schema present but the extension
      absent.
    * A managed-PG offering disabled the extension via an out-of-
      band configuration change.

    Called only on the PostgreSQL dialect path (gated by
    :func:`db_migration_probe`); SQLite has no ``pg_extension``
    catalog and the extension concept does not apply.
    """
    result = connection.execute(
        text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"),
    )
    return result.scalar_one_or_none() is not None


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

    On the PostgreSQL dialect the probe **additionally** verifies the
    ``vector`` extension is loaded (G0.4-T6, Task #263) — migration
    ``0003`` runs ``CREATE EXTENSION IF NOT EXISTS vector`` as part
    of ``alembic upgrade head``, so a successful migration implies
    the extension is present. The probe catches the post-deploy
    drift where an operator manually dropped the extension or a
    backup restore brought back the schema without the catalog
    entry. The detail in that case is ``revision=<sha>
    pgvector=missing`` (revision still matches head — only the
    extension is gone). SQLite skips this check; the dialect has no
    ``pg_extension`` catalog.

    The function is ``async`` because the canonical SQLAlchemy 2.x
    pattern reads the version through an ``AsyncEngine.connect()`` /
    ``run_sync`` pair; it is registered as an async probe by
    :mod:`meho_backplane.main`'s lifespan hook.
    """
    pgvector_ok = True  # default for non-PG dialects; PG branch overrides below
    try:
        cfg = alembic_config()
        head = ScriptDirectory.from_config(cfg).get_current_head()
        engine = get_engine()
        async with engine.connect() as conn:
            current = await conn.run_sync(_read_current_revision)
            if engine.dialect.name == "postgresql":
                pgvector_ok = await conn.run_sync(_check_pgvector_extension)
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
    if current != head:
        return ProbeResult(
            name="db",
            ok=False,
            detail=f"current={current} head={head}",
        )
    if not pgvector_ok:
        # Revision matches head but the pgvector extension is gone.
        # G0.4-T6 (#263) failure mode: operator dropped the extension
        # post-deploy or a backup restore brought back the schema
        # without the catalog entry. Surfaced loudly on ``/ready`` so
        # the kubelet pulls the pod out of service traffic; retrieval
        # writes / reads against the ``vector(384)`` column would
        # silently degrade otherwise.
        return ProbeResult(
            name="db",
            ok=False,
            detail=f"revision={head} pgvector=missing",
        )
    return ProbeResult(
        name="db",
        ok=True,
        detail=f"revision={head}",
    )
