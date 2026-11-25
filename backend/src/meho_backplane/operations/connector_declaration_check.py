# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Start check of what each connector class declares for its target form.

Runs in the start-up registration transaction, after every connector wrote
its operation rows. For each registered connector class:

* the shape of the declaration is checked (names, duplicates, kinds), see
  :func:`~meho_backplane.connectors.target_form.check_connector_declaration`;
* a declared ``test_operation`` must be a built-in operation row of the same
  connector, with risk level ``safe`` and no required input.

A broken declaration stops the start with a message that names the connector
class. The core only reads the declarations; it never changes how a connector
logs in.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.target_form import (
    ConnectorDeclarationError,
    check_connector_declaration,
)
from meho_backplane.db.models import EndpointDescriptor

__all__ = ["assert_connector_declarations_valid", "registered_connector_classes"]


def registered_connector_classes() -> list[type[Connector]]:
    """Every registered class once, in a stable order (a class can have two keys)."""
    unique = {cls: key for key, cls in sorted(all_connectors_v2().items())}
    return sorted(unique, key=lambda cls: (cls.product, cls.version, cls.impl_id, cls.__name__))


def _required_inputs(parameter_schema: object) -> list[str]:
    if not isinstance(parameter_schema, dict):
        return []
    required = parameter_schema.get("required")
    return [str(name) for name in required] if isinstance(required, list) else []


async def _check_test_operation(session: AsyncSession, cls: type[Connector]) -> None:
    op_id = cls.test_operation
    if op_id is None:
        return
    who = f"Connector {cls.__name__}"
    row = (
        await session.execute(
            select(EndpointDescriptor.safety_level, EndpointDescriptor.parameter_schema).where(
                EndpointDescriptor.tenant_id.is_(None),
                EndpointDescriptor.product == cls.product,
                EndpointDescriptor.version == cls.version,
                EndpointDescriptor.impl_id == cls.impl_id,
                EndpointDescriptor.op_id == op_id,
            )
        )
    ).first()
    if row is None:
        raise ConnectorDeclarationError(
            f"{who}: test_operation {op_id!r} is not a registered operation of this connector."
        )
    safety_level, parameter_schema = row
    if safety_level != "safe":
        raise ConnectorDeclarationError(
            f"{who}: test_operation {op_id!r} has risk level {safety_level!r}; it must be 'safe'."
        )
    required = _required_inputs(parameter_schema)
    if required:
        raise ConnectorDeclarationError(
            f"{who}: test_operation {op_id!r} needs input ({', '.join(sorted(required))}); "
            "it must need nothing beyond the target."
        )


async def assert_connector_declarations_valid(session: AsyncSession) -> None:
    """Raise :class:`ConnectorDeclarationError` for the first broken declaration."""
    classes = registered_connector_classes()
    for cls in classes:
        check_connector_declaration(cls)
    for cls in classes:
        await _check_test_operation(session, cls)
