# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Start check of what each connector class declares for its target form.

Runs in the start-up registration transaction, after every connector wrote
its operation rows. For each registered connector class:

* the shape of the declaration is checked (names, duplicates, kinds), see
  :func:`~meho_backplane.connectors.target_form.check_connector_declaration`;
* a declared ``test_operation`` must be written by a registrar of the same
  connector in this start, with risk level ``safe`` in the registrar's own
  code; its built-in row must still exist after the start retired old
  operations; and a call with no input must pass the same parameter check a real
  call runs (the schema with its ``oneOf`` / ``anyOf`` rules, and the op's
  own params check).

These are facts of the code. A broken one stops the start with a message that
names the connector class. A value that lives only in the database (a stored
risk level on the row, the row's turned-off mark, an override) never stops the
start: MEHO logs one warning line per value and starts. Whether the caller can
run the test read now is not decided here: the target form route asks the
call's own lookup and policy
(:func:`~meho_backplane.operations.run_now_check.run_now`). The core only reads
the declarations; it never changes how a connector logs in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.target_form import (
    ConnectorDeclarationError,
    check_connector_declaration,
)
from meho_backplane.db.models import EndpointDescriptor, OperationPolicyOverride
from meho_backplane.operations._validate import (
    InvalidOpSchemaError,
    ingested_schema_for_validation,
    params_errors,
    validate_params,
)
from meho_backplane.operations.switch_params import checked_parameter_schema

__all__ = [
    "CodeLevels",
    "assert_connector_declarations_valid",
    "code_safety_levels",
    "note_code_safety_level",
    "registered_connector_classes",
    "stored_state_problems",
]

#: ``(product, version, impl_id, op_id)`` of each operation a registrar wrote
#: in this start, with the risk level the registrar's own code gives it.
CodeLevels = Mapping[tuple[str, str, str, str], str]

#: The key under which the registrars' code levels live in ``session.info`` of
#: the start-up registration transaction.
_CODE_LEVELS_KEY = "meho_connector_code_safety_levels"


def code_safety_levels(session: AsyncSession) -> dict[tuple[str, str, str, str], str]:
    """The code's risk level of every operation registered through *session* so far."""
    levels: dict[tuple[str, str, str, str], str] = session.info.setdefault(_CODE_LEVELS_KEY, {})
    return levels


def note_code_safety_level(
    session: AsyncSession,
    *,
    product: str,
    version: str,
    impl_id: str,
    op_id: str,
    safety_level: str,
) -> None:
    """Remember the risk level the registrar's code gives *op_id*, on *session*.

    Every registration path ends in the one helper that calls this, so the
    start-up transaction (which owns *session*) sees each operation any
    registrar wrote.
    """
    code_safety_levels(session)[(product, version, impl_id, op_id)] = safety_level


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


def stored_state_problems(
    row: EndpointDescriptor, overrides: Iterable[OperationPolicyOverride]
) -> list[str]:
    """What in the database keeps *row* from being a ``safe``, enabled test read.

    ``[]`` when the row has risk level ``safe``, is turned on, and no
    override raises its risk level or makes it wait for approval.
    """
    problems: list[str] = []
    if row.safety_level != "safe":
        problems.append(f"stored risk level {row.safety_level!r}")
    if not row.is_enabled:
        problems.append("the row is turned off")
    for override in overrides:
        scope = "install" if override.tenant_id is None else f"tenant {override.tenant_id}"
        if override.safety_level not in (None, "safe"):
            problems.append(f"an override ({scope}) sets risk level {override.safety_level!r}")
        if override.requires_approval:
            problems.append(f"an override ({scope}) requires approval")
    return problems


async def _test_row(
    session: AsyncSession, cls: type[Connector], op_id: str
) -> EndpointDescriptor | None:
    return (
        await session.execute(
            select(EndpointDescriptor).where(
                EndpointDescriptor.tenant_id.is_(None),
                EndpointDescriptor.product == cls.product,
                EndpointDescriptor.version == cls.version,
                EndpointDescriptor.impl_id == cls.impl_id,
                EndpointDescriptor.op_id == op_id,
            )
        )
    ).scalar_one_or_none()


async def _all_overrides(
    session: AsyncSession, cls: type[Connector], op_id: str
) -> list[OperationPolicyOverride]:
    """The overrides of *op_id* in every scope: the install and each tenant."""
    stmt = select(OperationPolicyOverride).where(
        OperationPolicyOverride.product == cls.product,
        OperationPolicyOverride.version == cls.version,
        OperationPolicyOverride.impl_id == cls.impl_id,
        OperationPolicyOverride.op_id == op_id,
    )
    return list((await session.execute(stmt)).scalars().all())


def _check_code_level(
    cls: type[Connector], op_id: str, code_levels: CodeLevels, *, allow_missing_rows: bool
) -> None:
    who = f"Connector {cls.__name__}"
    code_level = code_levels.get((cls.product, cls.version, cls.impl_id, op_id))
    if code_level is None and not allow_missing_rows:
        raise ConnectorDeclarationError(
            f"{who}: test_operation {op_id!r} is not a registered operation of this "
            "connector: no registrar of this connector writes it."
        )
    if code_level is not None and code_level != "safe":
        raise ConnectorDeclarationError(
            f"{who}: test_operation {op_id!r} has risk level {code_level!r} in its code; "
            "it must be 'safe'."
        )


async def _check_test_operation(
    session: AsyncSession,
    cls: type[Connector],
    *,
    code_levels: CodeLevels,
    allow_missing_rows: bool,
) -> None:
    op_id = cls.test_operation
    if op_id is None:
        return
    _check_code_level(cls, op_id, code_levels, allow_missing_rows=allow_missing_rows)
    row = await _test_row(session, cls, op_id)
    if row is None:
        if allow_missing_rows:
            return
        raise ConnectorDeclarationError(
            f"Connector {cls.__name__}: test_operation {op_id!r} has no built-in operation "
            "row after the start wrote and retired its operations."
        )
    problems = _empty_call_problems(op_id, row.parameter_schema, row.source_kind)
    if problems:
        raise ConnectorDeclarationError(
            f"Connector {cls.__name__}: test_operation {op_id!r} needs input "
            f"({'; '.join(problems)}); it must need nothing beyond the target."
        )
    for problem in stored_state_problems(row, await _all_overrides(session, cls, op_id)):
        structlog.get_logger(__name__).warning(
            "connector_test_operation_not_ready",
            connector=cls.__name__,
            op_id=op_id,
            stored_value=problem,
        )


async def assert_connector_declarations_valid(
    session: AsyncSession,
    *,
    code_levels: CodeLevels,
    allow_missing_rows: bool = False,
) -> None:
    """Raise :class:`ConnectorDeclarationError` for the first broken declaration.

    *code_levels* holds the operations the registrars wrote in this start,
    with the risk level of their code (:func:`code_safety_levels`).

    ``allow_missing_rows`` is for the test-only registrar snapshot
    (``MEHO_TEST_AMORTIZE_TYPED_OP_REGISTRARS``): a snapshot replay runs no
    registrar, so *code_levels* is empty. A test read with no code level or
    with no row is then not an error. A row that exists is still checked.
    Production never sets it.
    """
    classes = registered_connector_classes()
    for cls in classes:
        check_connector_declaration(cls)
    for cls in classes:
        await _check_test_operation(
            session, cls, code_levels=code_levels, allow_missing_rows=allow_missing_rows
        )
