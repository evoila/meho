# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Instance-Level Operation CRUD Service.

Provides operations for managing per-instance operation customizations:
- add_custom_operation: Add a purely instance-specific operation
- override_operation: Override a type-level operation for this instance
- disable_operation: Disable a type-level operation for this instance
- reset_operation: Delete a custom override, reverting to type-level definition
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import and_, select

from meho_app.core.otel.logging import get_logger
from meho_app.modules.connectors.models import ConnectorOperationModel

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


async def add_custom_operation(
    session: AsyncSession,
    connector_id: UUID,
    tenant_id: str,
    operation_data: dict[str, Any],
) -> ConnectorOperationModel:
    """Create a new custom operation for this connector instance only.

    The operation has source='custom' and no type_operation_id, meaning
    it is purely instance-specific and not an override of any type-level op.

    Args:
        session: SQLAlchemy async session.
        connector_id: UUID of the connector.
        tenant_id: Tenant ID.
        operation_data: Dict with operation fields (operation_id, name,
            description, category, parameters, example, safety_level, etc.).

    Returns:
        The created ConnectorOperationModel.
    """
    search_content = " ".join(
        filter(
            None,
            [
                operation_data.get("name", ""),
                operation_data.get("operation_id", ""),
                operation_data.get("description", ""),
                operation_data.get("category", ""),
            ],
        )
    )

    db_op = ConnectorOperationModel(
        id=uuid4(),
        connector_id=connector_id,
        tenant_id=tenant_id,
        operation_id=operation_data["operation_id"],
        name=operation_data["name"],
        description=operation_data.get("description"),
        category=operation_data.get("category"),
        parameters=operation_data.get("parameters", []),
        example=operation_data.get("example"),
        search_content=search_content,
        is_enabled=operation_data.get("is_enabled", True),
        safety_level=operation_data.get("safety_level", "safe"),
        requires_approval=operation_data.get("requires_approval", False),
        source="custom",
        type_operation_id=None,
        is_enabled_override=None,
    )

    session.add(db_op)
    await session.flush()
    await session.refresh(db_op)

    logger.info(f"Added custom operation '{db_op.operation_id}' for connector {connector_id}")
    return db_op


async def override_operation(  # NOSONAR (cognitive complexity)
    session: AsyncSession,
    connector_id: UUID,
    type_operation_id: UUID,
    overrides: dict[str, Any],
) -> ConnectorOperationModel:
    """Create or update an instance override of a type-level operation.

    Copies the type-level operation, sets source='custom', and applies
    the provided overrides. The type_operation_id links back to the original.

    If an override already exists for this type_operation_id, updates it.

    Args:
        session: SQLAlchemy async session.
        connector_id: UUID of the connector.
        type_operation_id: UUID of the type-level operation to override.
        overrides: Dict of fields to override (name, description, parameters, etc.).

    Returns:
        The created/updated override ConnectorOperationModel.

    Raises:
        ValueError: If the type-level operation is not found.
    """
    # Load the original type-level operation
    type_op_query = select(ConnectorOperationModel).where(
        and_(
            ConnectorOperationModel.id == type_operation_id,
            ConnectorOperationModel.connector_id == connector_id,
            ConnectorOperationModel.source == "type",
        )
    )
    type_op_result = await session.execute(type_op_query)
    type_op = type_op_result.scalar_one_or_none()

    if type_op is None:
        raise ValueError(
            f"Type-level operation {type_operation_id} not found for connector {connector_id}"
        )

    # Check if an override already exists
    existing_query = select(ConnectorOperationModel).where(
        and_(
            ConnectorOperationModel.connector_id == connector_id,
            ConnectorOperationModel.source == "custom",
            ConnectorOperationModel.type_operation_id == type_operation_id,
        )
    )
    existing_result = await session.execute(existing_query)
    existing_override = existing_result.scalar_one_or_none()

    if existing_override is not None:
        # Update existing override
        for field, value in overrides.items():
            if hasattr(existing_override, field) and field not in (
                "id",
                "connector_id",
                "tenant_id",
                "source",
                "type_operation_id",
            ):
                setattr(existing_override, field, value)

        # Recompute search content
        existing_override.search_content = " ".join(  # type: ignore[assignment]  # SQLAlchemy ORM attribute assignment
            filter(
                None,
                [
                    existing_override.name or "",  # type: ignore[list-item]  # SQLAlchemy ORM attribute access
                    existing_override.operation_id or "",  # type: ignore[list-item]  # SQLAlchemy ORM attribute access
                    existing_override.description or "",  # type: ignore[list-item]  # SQLAlchemy ORM attribute access
                    existing_override.category or "",  # type: ignore[list-item]  # SQLAlchemy ORM attribute access
                ],
            )
        )

        await session.flush()
        await session.refresh(existing_override)

        logger.info(
            f"Updated override for type operation {type_operation_id} on connector {connector_id}"
        )
        return existing_override

    # Create new override by copying the type-level op and applying overrides
    override_op = ConnectorOperationModel(
        id=uuid4(),
        connector_id=type_op.connector_id,
        tenant_id=type_op.tenant_id,
        operation_id=overrides.get("operation_id", type_op.operation_id),
        name=overrides.get("name", type_op.name),
        description=overrides.get("description", type_op.description),
        category=overrides.get("category", type_op.category),
        parameters=overrides.get("parameters", type_op.parameters),
        example=overrides.get("example", type_op.example),
        is_enabled=overrides.get("is_enabled", type_op.is_enabled),
        safety_level=overrides.get("safety_level", type_op.safety_level),
        requires_approval=overrides.get("requires_approval", type_op.requires_approval),
        response_entity_type=overrides.get("response_entity_type", type_op.response_entity_type),
        response_identifier_field=overrides.get(
            "response_identifier_field", type_op.response_identifier_field
        ),
        response_display_name_field=overrides.get(
            "response_display_name_field", type_op.response_display_name_field
        ),
        source="custom",
        type_operation_id=type_operation_id,
        is_enabled_override=overrides.get("is_enabled_override", True),
    )

    # Compute search content for the override
    override_op.search_content = " ".join(  # type: ignore[assignment]  # SQLAlchemy ORM attribute assignment
        filter(
            None,
            [
                override_op.name or "",  # type: ignore[list-item]  # SQLAlchemy ORM attribute access
                override_op.operation_id or "",  # type: ignore[list-item]  # SQLAlchemy ORM attribute access
                override_op.description or "",  # type: ignore[list-item]  # SQLAlchemy ORM attribute access
                override_op.category or "",  # type: ignore[list-item]  # SQLAlchemy ORM attribute access
            ],
        )
    )

    session.add(override_op)
    await session.flush()
    await session.refresh(override_op)

    logger.info(
        f"Created override for type operation {type_operation_id} on connector {connector_id}"
    )
    return override_op


async def disable_operation(
    session: AsyncSession,
    connector_id: UUID,
    type_operation_id: UUID,
) -> ConnectorOperationModel:
    """Disable a type-level operation for this connector instance.

    Creates a custom override with is_enabled_override=False if one
    doesn't already exist. If an override exists, updates it to disabled.

    Args:
        session: SQLAlchemy async session.
        connector_id: UUID of the connector.
        type_operation_id: UUID of the type-level operation to disable.

    Returns:
        The override ConnectorOperationModel with is_enabled_override=False.

    Raises:
        ValueError: If the type-level operation is not found.
    """
    return await override_operation(
        session=session,
        connector_id=connector_id,
        type_operation_id=type_operation_id,
        overrides={"is_enabled_override": False},
    )


async def reset_operation(
    session: AsyncSession,
    connector_id: UUID,
    operation_id: UUID,
) -> bool:
    """Delete a custom override, reverting to the type-level definition.

    Only deletes operations with source='custom'. Does nothing for
    type-level operations.

    Args:
        session: SQLAlchemy async session.
        connector_id: UUID of the connector.
        operation_id: UUID of the custom override to delete.

    Returns:
        True if an override was deleted, False if not found or not custom.
    """
    query = select(ConnectorOperationModel).where(
        and_(
            ConnectorOperationModel.id == operation_id,
            ConnectorOperationModel.connector_id == connector_id,
            ConnectorOperationModel.source == "custom",
        )
    )
    result = await session.execute(query)
    custom_op = result.scalar_one_or_none()

    if custom_op is None:
        logger.warning(
            f"No custom override found with id={operation_id} for connector {connector_id}"
        )
        return False

    await session.delete(custom_op)
    await session.flush()

    logger.info(
        f"Reset operation {operation_id} (deleted custom override) for connector {connector_id}"
    )
    return True
