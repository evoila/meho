# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Instance-group ops (about / version / config / logins) for :class:`MssqlConnector`.

Registered by :meth:`MssqlConnector.register_operations` via the
:data:`~meho_backplane.connectors.mssql.ops.MSSQL_OPS` composer. See
:mod:`meho_backplane.connectors.mssql.ops` for the ``MssqlOp`` dataclass and
the shared schema fragments.
"""

from __future__ import annotations

from meho_backplane.connectors.mssql.ops import (
    _LIST_RESPONSE_SCHEMA,
    _NO_PARAMS,
    TDS_TRANSPORT_NOTE,
    MssqlOp,
)

INSTANCE_OPS: tuple[MssqlOp, ...] = (
    MssqlOp(
        op_id="mssql.instance.about",
        handler_attr="instance_about",
        summary="Return the SQL Server instance identity (version / edition / clustering).",
        description=(
            "Reads SERVERPROPERTY for the product version, product level, "
            "edition, machine / server / instance name, collation, and the "
            "IsClustered / IsHadrEnabled flags. Use to confirm the instance is "
            "reachable over TDS and to read its identity before any drill-in. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema={"type": "object", "additionalProperties": True},
        group_key="instance",
        tags=("read-only", "identity", "mssql"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call first to identify a SQL Server instance (edition / "
                "version / whether it is clustered or HADR-enabled) and confirm "
                "TDS reachability. " + TDS_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "Flat dict: {product_version, product_level, edition, "
                "machine_name, server_name, instance_name, collation, "
                "is_clustered, is_hadr_enabled}."
            ),
        },
    ),
    MssqlOp(
        op_id="mssql.instance.version",
        handler_attr="instance_version",
        summary="Return the full @@VERSION banner and parsed product version.",
        description=(
            "Returns the full ``@@VERSION`` banner (OS, build, architecture) "
            "alongside the parsed ProductVersion / ProductLevel / Edition. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema={"type": "object", "additionalProperties": True},
        group_key="instance",
        tags=("read-only", "version", "mssql"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call for the verbatim ``@@VERSION`` string (build number, OS, "
                "architecture) when the compact about identity is not enough. " + TDS_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": "{version, product_version, product_level, edition}.",
        },
    ),
    MssqlOp(
        op_id="mssql.instance.config",
        handler_attr="instance_config",
        summary="List instance configuration settings from sys.configurations.",
        description=(
            "Lists every instance-level configuration option (name, configured "
            "value, running value_in_use, min/max, is_dynamic, is_advanced, "
            "description) from sys.configurations. safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="instance",
        tags=("read-only", "config", "mssql"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to read effective instance configuration (max server "
                "memory, MAXDOP, cost threshold, etc.). " + TDS_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{name, value, value_in_use, minimum, maximum, "
                "is_dynamic, is_advanced, description}], 'total': <int>}."
            ),
        },
    ),
    MssqlOp(
        op_id="mssql.instance.logins",
        handler_attr="instance_logins",
        summary="List server logins from sys.server_principals (no password material).",
        description=(
            "Lists SQL / Windows / group / certificate / key server principals "
            "(name, type_desc, is_disabled, create/modify dates, default "
            "database) from sys.server_principals. Reads sys.server_principals, "
            "never sys.sql_logins, so no password hash is ever projected. "
            "safety_level=safe, read-only."
        ),
        parameter_schema=_NO_PARAMS,
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="instance",
        tags=("read-only", "logins", "mssql"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate server logins (which accounts can connect, "
                "which are disabled) before a migration. No password material "
                "is returned. " + TDS_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{name, type_desc, is_disabled, create_date, "
                "modify_date, default_database_name}], 'total': <int>}."
            ),
        },
    ),
)
