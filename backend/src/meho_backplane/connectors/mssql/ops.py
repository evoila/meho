# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`MssqlConnector` (#3264).

The connector manages **Microsoft SQL Server 2022** over a direct TDS
connection (port 1433) via the pure-Python ``python-tds`` driver — the
postgres / mongodb direct-protocol mold (#2236 / #2237), not the
PowerShell-over-SSH estate seam. 14 ops across four groups; the safety tier per
op follows the Initiative #3259 satellite table — reads ``safe``, a recoverable
write ``caution``, destructive / data-overwriting writes ``dangerous`` +
``requires_approval``:

* ``mssql.instance.about``         [safe]       SERVERPROPERTY identity (version/edition/HA).
* ``mssql.instance.version``       [safe]       ``@@VERSION`` banner + parsed product version.
* ``mssql.instance.config``        [safe]       ``sys.configurations`` (list → JSONFlux).
* ``mssql.instance.logins``        [safe]       ``sys.server_principals`` (no password material).
* ``mssql.databases.list``         [safe]       ``sys.databases`` + size (list → JSONFlux).
* ``mssql.databases.files``        [safe]       ``sys.master_files`` (list → JSONFlux).
* ``mssql.databases.create``       [danger+appr] ``CREATE DATABASE``.
* ``mssql.databases.drop``         [danger+appr] ``DROP DATABASE``.
* ``mssql.ha.availability-groups`` [safe]       AG topology + per-replica state (list → JSONFlux).
* ``mssql.ha.fci``                 [safe]       FCI cluster nodes + clustering flags.
* ``mssql.ha.sync-health``         [safe]       per-database AG sync health (list → JSONFlux).
* ``mssql.backup.history``         [safe]       ``msdb.dbo.backupset`` (list → JSONFlux).
* ``mssql.backup.database``        [caution]    ``BACKUP DATABASE ... TO DISK``.
* ``mssql.backup.restore``         [danger+appr] ``RESTORE DATABASE ... FROM DISK`` (overwrites).

**No freeform ``query`` op is shipped** — the narrow-waist doctrine (CLAUDE.md
postulate 5): only curated, individually-safety-tiered ops reach the agent
surface, never a raw-T-SQL escape hatch. This is a deliberate non-goal recorded
in ``docs/codebase/connectors-mssql.md`` and pinned by a test asserting no such
op id exists.

The dataclass + tuple shape mirrors
:mod:`~meho_backplane.connectors.postgres.ops` so the registration walk in
:meth:`MssqlConnector.register_operations` reads identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "MSSQL_OPS",
    "MSSQL_WHEN_TO_USE_BY_GROUP",
    "TDS_TRANSPORT_NOTE",
    "MssqlOp",
]


#: Transport reminder copied into every op's ``llm_instructions``. SQL Server
#: management has no unified REST surface the agent could compose against; the
#: connector is a curated op table over the TDS wire protocol, so the agent
#: must call these ops rather than reach for raw T-SQL (no freeform query op
#: exists by design).
TDS_TRANSPORT_NOTE: str = (
    "SQL Server is reached over the TDS wire protocol (port 1433) via the "
    "pure-Python python-tds driver; there is no per-op REST surface and no "
    "freeform T-SQL op — call these curated ops."
)


#: Curated ``when_to_use`` blurbs per group key, consumed by
#: :meth:`MssqlConnector.register_operations` (the registration walk fails
#: closed with a :class:`ValueError` if a ``group_key`` lacks an entry — the
#: postgres / winsrv precedent).
MSSQL_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "instance": (
        "Use for SQL Server instance-level facts before any drill-in: identity "
        "and version/edition/clustering (``mssql.instance.about``), the full "
        "``@@VERSION`` banner (``mssql.instance.version``), the effective "
        "instance configuration (``mssql.instance.config``, sys.configurations), "
        "and the server logins (``mssql.instance.logins``, no password "
        "material). All read-only. Call ``mssql.instance.about`` first to "
        "confirm the instance is reachable and read its edition/version."
    ),
    "databases": (
        "Use to inventory databases and (with approval) create/drop them: list "
        "every database with state, recovery model, and on-disk size "
        "(``mssql.databases.list``) or the data/log files of one "
        "(``mssql.databases.files``) -- both read-only -- and ``create`` / "
        "``drop`` a database (both ``dangerous`` + ``requires_approval``: a "
        "dispatch parks for a human decision, and the tier ladder excludes them "
        "from satellite runners). The right group for 'what databases exist and "
        "how big are they?' on a migration source or target."
    ),
    "ha": (
        "Use for the migration-validation reads: Availability Group topology "
        "and per-replica operational/synchronization state "
        "(``mssql.ha.availability-groups``), Failover Cluster Instance nodes + "
        "clustering flags (``mssql.ha.fci``), and per-database AG replication "
        "sync health with queue depths and last-commit time "
        "(``mssql.ha.sync-health``). All read-only. The right group to confirm "
        "an AG/FCI is healthy and caught up before a migration cutover or a "
        "planned failover."
    ),
    "backup": (
        "Use to read backup history and (governed) back up / restore a "
        "database: recent backup history newest-first "
        "(``mssql.backup.history``, read-only), ``mssql.backup.database`` "
        "(``caution`` -- creates a backup file, recoverable), and "
        "``mssql.backup.restore`` (``dangerous`` + ``requires_approval`` -- "
        "OVERWRITES the target database, so a dispatch parks for approval). "
        "Backup/restore is the migration data-movement primitive."
    ),
}


@dataclass(frozen=True)
class MssqlOp:
    """Metadata for one mssql op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the connector's ``register_operations()`` classmethod can splat
    the dataclass into the helper. ``handler_attr`` is the async-handler
    attribute name on
    :class:`~meho_backplane.connectors.mssql.connector.MssqlConnector`.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

_NO_PARAMS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

#: The ``{rows, total}`` envelope every list op returns; the JSONFlux reducer
#: keys on the single ``rows`` collection.
_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {"type": "array", "items": {"type": "object"}},
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": True,
}

_DATABASE_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "pattern": "\\S",
    "description": "A SQL Server database name (max 128 characters).",
}

_BACKUP_PATH_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "The backup device path as the SQL Server service account sees it "
        "(e.g. a local path or a UNC share the instance can reach)."
    ),
}


def _write_response_schema(*extra: str) -> dict[str, Any]:
    """An action-shaped write response schema with the given *extra* keys."""
    properties: dict[str, Any] = {
        "database": {"type": "string"},
        "action": {"type": "string"},
        "ok": {"type": "boolean"},
        "op_class": {"type": "string", "enum": ["write"]},
    }
    for key in extra:
        properties[key] = {"type": ["string", "boolean"]}
    return {
        "type": "object",
        "properties": properties,
        "required": ["database", "action", "op_class"],
        "additionalProperties": True,
    }


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def _mssql_ops() -> tuple[MssqlOp, ...]:
    """Merge the per-group op tuples into the registration tuple.

    The per-group imports are deferred into this function (the winsrv / msad
    precedent): each group module imports the ``MssqlOp`` dataclass + the shared
    schema fragments back from this module, so importing them at the top would
    close a cycle. By the time ``MSSQL_OPS = _mssql_ops()`` runs at the bottom of
    this module, every shared name above is already bound.
    """
    from meho_backplane.connectors.mssql.ops_backup import BACKUP_OPS
    from meho_backplane.connectors.mssql.ops_databases import DATABASE_OPS
    from meho_backplane.connectors.mssql.ops_ha import HA_OPS
    from meho_backplane.connectors.mssql.ops_instance import INSTANCE_OPS

    return (*INSTANCE_OPS, *DATABASE_OPS, *HA_OPS, *BACKUP_OPS)


#: The ops :class:`MssqlConnector` registers at lifespan startup.
MSSQL_OPS: tuple[MssqlOp, ...] = _mssql_ops()
