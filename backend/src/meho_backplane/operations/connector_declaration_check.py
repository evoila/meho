# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Start check of what each connector class declares for its target form.

Runs in the start-up registration transaction, after every connector wrote
its operation rows. For each registered connector class:

* the shape of the declaration is checked (names, duplicates, kinds), see
  :func:`~meho_backplane.connectors.target_form.check_connector_declaration`;
* a declared ``test_operation`` must be a built-in operation row of the same
  connector, with risk level ``safe``, and a call with no input must pass the
  same parameter check a real call runs (the schema with its ``oneOf`` /
  ``anyOf`` rules, and the op's own params check).

A broken declaration stops the start with a message that names the connector
class. The core only reads the declarations; it never changes how a connector
logs in.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.target_form import (
    ConnectorDeclarationError,
    check_connector_declaration,
)
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._validate import (
    InvalidOpSchemaError,
    ingested_schema_for_validation,
    params_errors,
    validate_params,
)
from meho_backplane.operations.switch_params import checked_parameter_schema

__all__ = ["assert_connector_declarations_valid", "registered_connector_classes"]


def registered_connector_classes() -> list[type[Connector]]:
    """Every registered class once, in a stable order (a class can have two keys)."""
    unique = {cls: key for key, cls in sorted(all_connectors_v2().items())}
    return sorted(unique, key=lambda cls: (cls.product, cls.version, cls.impl_id, cls.__name__))


def _empty_call_problems(op_id: str, parameter_schema: Any, source_kind: str) -> list[str]:
    """What a call of *op_id* with no input would be refused for; ``[]`` when it passes.

    Runs the same steps as the parameter check of a real call: the schema as
    calls see it, the stricter form for an ingested op, the JSON Schema
    check, and the op's own params check.
    """
    schema = checked_parameter_schema(parameter_schema)
    if not isinstance(schema, dict):
        schema = {}
    if source_kind == "ingested":
        schema = ingested_schema_for_validation(schema)
    try:
        errors = params_errors(op_id, {}, validate_params(schema, {}))
    except InvalidOpSchemaError as exc:
        return [f"its parameter schema has a broken reference {exc.missing_ref}"]
    return sorted({f"{err['path']}: {err['expected']}" for err in errors})


async def _check_test_operation(
    session: AsyncSession, cls: type[Connector], *, allow_missing_rows: bool
) -> None:
    op_id = cls.test_operation
    if op_id is None:
        return
    who = f"Connector {cls.__name__}"
    row = (
        await session.execute(
            select(
                EndpointDescriptor.safety_level,
                EndpointDescriptor.parameter_schema,
                EndpointDescriptor.source_kind,
            ).where(
                EndpointDescriptor.tenant_id.is_(None),
                EndpointDescriptor.product == cls.product,
                EndpointDescriptor.version == cls.version,
                EndpointDescriptor.impl_id == cls.impl_id,
                EndpointDescriptor.op_id == op_id,
            )
        )
    ).first()
    if row is None:
        if allow_missing_rows:
            return
        raise ConnectorDeclarationError(
            f"{who}: test_operation {op_id!r} is not a registered operation of this connector."
        )
    safety_level, parameter_schema, source_kind = row
    if safety_level != "safe":
        raise ConnectorDeclarationError(
            f"{who}: test_operation {op_id!r} has risk level {safety_level!r}; it must be 'safe'."
        )
    problems = _empty_call_problems(op_id, parameter_schema, source_kind)
    if problems:
        raise ConnectorDeclarationError(
            f"{who}: test_operation {op_id!r} needs input ({'; '.join(problems)}); "
            "it must need nothing beyond the target."
        )


async def assert_connector_declarations_valid(
    session: AsyncSession, *, allow_missing_rows: bool = False
) -> None:
    """Raise :class:`ConnectorDeclarationError` for the first broken declaration.

    ``allow_missing_rows`` is for the test-only registrar snapshot
    (``MEHO_TEST_AMORTIZE_TYPED_OP_REGISTRARS``): a test that wrote some
    built-in rows before the start leaves the snapshot replay with nothing to
    add, so the rows of the shipped connectors can be missing. A missing row
    is then not an error. A row that exists is still checked. Production never
    sets it.
    """
    classes = registered_connector_classes()
    for cls in classes:
        check_connector_declaration(cls)
    for cls in classes:
        await _check_test_operation(session, cls, allow_missing_rows=allow_missing_rows)
