# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Operation Inheritance Resolution Service.

Resolves the effective set of operations for a connector instance by merging:
1. Type-level operations (source='type') -- inherited from connector type definition
2. Instance-level overrides (source='custom') -- per-instance customizations

Merge rules:
- Type-level ops are the base set (inherited by all instances of that type)
- Instance overrides match via type_operation_id pointing to the original
- If an override has is_enabled_override=False, the operation is excluded
- If an override exists, it replaces the type-level version
- Custom ops with no type_operation_id are purely instance-specific additions
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import and_, select

from meho_app.core.otel.logging import get_logger
from meho_app.modules.connectors.models import ConnectorOperationModel

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from meho_app.modules.connectors.base import OperationDefinition

logger = get_logger(__name__)


async def resolve_operations(
    session: AsyncSession,
    connector_id: UUID,
    connector_type: str,
    tenant_id: str,
) -> list[ConnectorOperationModel]:
    """Resolve the effective operations for a connector instance.

    Merges type-level operations with instance-level overrides to produce
    the final set of operations the agent should see.

    Merge logic:
    a. Load all type-level ops (source='type') for this connector
    b. Load all instance-level overrides (source='custom') for this connector
    c. For each type-level op, check if a custom override exists (via type_operation_id)
       - If override with is_enabled_override=False -> exclude (disabled)
       - If override exists -> use override instead of type-level
       - Otherwise -> use type-level as-is
    d. Add any custom ops that don't override a type-level op (purely instance-specific)

    Args:
        session: SQLAlchemy async session.
        connector_id: UUID of the connector instance.
        connector_type: Connector type string (for logging).
        tenant_id: Tenant ID.

    Returns:
        Merged, deduplicated list of ConnectorOperationModel instances.
    """
    # a. Load type-level operations
    type_query = select(ConnectorOperationModel).where(
        and_(
            ConnectorOperationModel.connector_id == connector_id,
            ConnectorOperationModel.source == "type",
        )
    )
    type_result = await session.execute(type_query)
    type_ops = list(type_result.scalars().all())

    # b. Load instance-level overrides
    custom_query = select(ConnectorOperationModel).where(
        and_(
            ConnectorOperationModel.connector_id == connector_id,
            ConnectorOperationModel.source == "custom",
        )
    )
    custom_result = await session.execute(custom_query)
    custom_ops = list(custom_result.scalars().all())

    # Build lookup: type_operation_id -> custom override
    override_by_type_op_id: dict[UUID, ConnectorOperationModel] = {}
    purely_custom: list[ConnectorOperationModel] = []

    for custom_op in custom_ops:
        if custom_op.type_operation_id is not None:
            override_by_type_op_id[custom_op.type_operation_id] = custom_op
        else:
            # No type_operation_id = purely instance-specific custom op
            purely_custom.append(custom_op)

    # c. Merge: for each type-level op, check for override
    merged: list[ConnectorOperationModel] = []

    for type_op in type_ops:
        override = override_by_type_op_id.get(type_op.id)
        if override is not None:
            # Check if disabled
            if override.is_enabled_override is False:
                # Operation disabled for this instance -- exclude
                continue
            # Override exists -- use it instead of type-level
            merged.append(override)
        else:
            # No override -- use type-level as-is
            if type_op.is_enabled:
                merged.append(type_op)

    # d. Add purely instance-specific custom ops
    for custom_op in purely_custom:
        if custom_op.is_enabled:
            merged.append(custom_op)

    logger.debug(
        f"Resolved {len(merged)} operations for connector {connector_id} "
        f"({len(type_ops)} type-level, {len(custom_ops)} custom, "
        f"{len(override_by_type_op_id)} overrides)"
    )

    return merged


async def sync_type_operations(
    session: AsyncSession,
    connector_id: UUID,
    connector_type: str,
    tenant_id: str,
    type_operations: list[OperationDefinition],
) -> int:
    """Sync type-level operations for a connector instance.

    Upserts operations from the type definition with source='type'.
    Removes type-level ops that no longer exist in the definition,
    but preserves custom overrides.

    This generalizes the existing sync pattern used in kubernetes/sync.py,
    vmware/sync.py, etc. for all connector types.

    Args:
        session: SQLAlchemy async session.
        connector_id: UUID of the connector.
        connector_type: Connector type string.
        tenant_id: Tenant ID.
        type_operations: Operation definitions from the type-level code.

    Returns:
        Count of operations synced (added + updated).
    """
    from meho_app.modules.connectors.repositories import ConnectorOperationRepository

    op_repo = ConnectorOperationRepository(session)

    # Get existing type-level ops for this connector
    existing_query = select(ConnectorOperationModel).where(
        and_(
            ConnectorOperationModel.connector_id == connector_id,
            ConnectorOperationModel.source == "type",
        )
    )
    existing_result = await session.execute(existing_query)
    existing_ops = {op.operation_id: op for op in existing_result.scalars().all()}

    # Build set of incoming operation IDs for stale detection
    incoming_op_ids = {op.operation_id for op in type_operations}

    synced = 0

    for op_def in type_operations:
        search_content = (
            f"{op_def.name} {op_def.operation_id} {op_def.description} {op_def.category}"
        )

        if op_def.operation_id in existing_ops:
            # Update existing type-level op
            db_op = existing_ops[op_def.operation_id]
            db_op.name = op_def.name
            db_op.description = op_def.description
            db_op.category = op_def.category
            db_op.parameters = list(op_def.parameters)
            db_op.example = op_def.example
            db_op.search_content = search_content
            db_op.response_entity_type = op_def.response_entity_type
            db_op.response_identifier_field = op_def.response_identifier_field
            db_op.response_display_name_field = op_def.response_display_name_field
            db_op.source = "type"
            await session.flush()
        else:
            # Create new type-level op
            from meho_app.modules.connectors.schemas import ConnectorOperationCreate

            await op_repo.create_operation(
                ConnectorOperationCreate(
                    connector_id=str(connector_id),
                    tenant_id=tenant_id,
                    operation_id=op_def.operation_id,
                    name=op_def.name,
                    description=op_def.description,
                    category=op_def.category,
                    parameters=list(op_def.parameters),
                    example=op_def.example,
                    search_content=search_content,
                    response_entity_type=op_def.response_entity_type,
                    response_identifier_field=op_def.response_identifier_field,
                    response_display_name_field=op_def.response_display_name_field,
                )
            )
        synced += 1

    # Remove stale type-level ops (no longer in type definition)
    # Only remove source='type' -- custom overrides are preserved
    stale_op_ids = set(existing_ops.keys()) - incoming_op_ids
    if stale_op_ids:
        for stale_id in stale_op_ids:
            stale_op = existing_ops[stale_id]
            await session.delete(stale_op)
        logger.info(
            f"Removed {len(stale_op_ids)} stale type-level ops for "
            f"connector {connector_id}: {stale_op_ids}"
        )

    logger.info(
        f"Synced {synced} type-level operations for {connector_type} "
        f"connector {connector_id} (removed {len(stale_op_ids) if stale_op_ids else 0} stale)"
    )

    return synced
