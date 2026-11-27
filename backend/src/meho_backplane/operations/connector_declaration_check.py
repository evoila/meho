# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Start check of what each connector class declares for its target form.

Runs in the start-up registration transaction, after every connector wrote
its operation rows. For each registered connector class:

* the shape of the declaration is checked (names, duplicates, kinds), see
  :func:`~meho_backplane.connectors.target_form.check_connector_declaration`;
* a declared ``test_operation`` must be written by a registrar of the same
  connector in this start, with risk level ``safe`` in the registrar's own
  code (when registration left it out there, because a mark does not fit
  its risk level, the connector starts without a test read); its built-in
  row must still exist after the start retired old operations; and a call
  with no input must pass the same parameter check a real call runs (the
  schema with its ``oneOf`` / ``anyOf`` rules, and the op's own params
  check).

These are facts of the code. A broken one stops the start with a message that
names the connector class. A value that lives only in the database (a stored
risk level on the row, the row's turned-off mark, an override) never stops the
start: MEHO logs one warning line per value and starts. While such a value
holds, the target form route answers ``test_operation: null`` for that class
(:func:`ready_test_operations`). The core only reads the declarations; it never
changes how a connector logs in.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.connectors.target_form import (
    ConnectorDeclarationError,
    check_connector_declaration,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationPolicyOverride
from meho_backplane.operations._left_out import LeftOutOp, left_out_op
from meho_backplane.operations._validate import (
    InvalidOpSchemaError,
    ingested_schema_for_validation,
    params_errors,
    validate_params,
)
from meho_backplane.operations.op_policy import OpKey, load_overrides
from meho_backplane.operations.switch_params import checked_parameter_schema

__all__ = [
    "CodeLevels",
    "assert_connector_declarations_valid",
    "collect_code_safety_levels",
    "note_code_safety_level",
    "ready_test_operations",
    "registered_connector_classes",
    "stored_state_problems",
]

#: ``(product, version, impl_id, op_id)`` of each operation a registrar wrote
#: in this start, with the risk level the registrar's own code gives it.
CodeLevels = Mapping[tuple[str, str, str, str], str]

_CODE_LEVELS: ContextVar[dict[tuple[str, str, str, str], str] | None] = ContextVar(
    "meho_connector_code_safety_levels", default=None
)


@contextmanager
def collect_code_safety_levels() -> Iterator[dict[tuple[str, str, str, str], str]]:
    """Collect the code's risk level of every operation registered while the block runs."""
    levels: dict[tuple[str, str, str, str], str] = {}
    token = _CODE_LEVELS.set(levels)
    try:
        yield levels
    finally:
        _CODE_LEVELS.reset(token)


def note_code_safety_level(
    *, product: str, version: str, impl_id: str, op_id: str, safety_level: str
) -> None:
    """Remember the risk level the registrar's code gives *op_id*, while a start collects."""
    levels = _CODE_LEVELS.get()
    if levels is not None:
        levels[(product, version, impl_id, op_id)] = safety_level


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
    left_out: Collection[LeftOutOp],
    allow_missing_rows: bool,
) -> None:
    op_id = cls.test_operation
    if op_id is None:
        return
    if (cls.product, cls.version, cls.impl_id, op_id) not in code_levels:
        dropped = left_out_op(
            product=cls.product, version=cls.version, impl_id=cls.impl_id, op_id=op_id, ops=left_out
        )
        if dropped is not None:
            structlog.get_logger(__name__).error(
                "connector_test_read_left_out",
                connector=cls.__name__,
                product=cls.product,
                version=cls.version,
                impl_id=cls.impl_id,
                connector_package=dropped.owner_package,
                op_id=op_id,
                reason="registration left the test read out; this connector has no test read now",
            )
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
    left_out: Collection[LeftOutOp] = (),
    allow_missing_rows: bool = False,
) -> None:
    """Raise :class:`ConnectorDeclarationError` for the first broken declaration.

    *code_levels* holds the operations the registrars wrote in this start,
    with the risk level of their code (:func:`collect_code_safety_levels`).

    *left_out* holds the operations registration left out in this start,
    because a mark does not fit the risk level
    (:mod:`meho_backplane.operations._left_out`). A connector whose test
    read is one of them starts without its test read: MEHO logs one
    ``connector_test_read_left_out`` line that names the connector and the
    op, and checks nothing more for it. A test read that no registrar
    writes and that was not left out (for example a typo in the op id)
    still stops the start.

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
            session,
            cls,
            code_levels=code_levels,
            left_out=left_out,
            allow_missing_rows=allow_missing_rows,
        )


async def ready_test_operations(
    tenant_id: UUID, classes: Iterable[type[Connector]]
) -> set[type[Connector]]:
    """Read each test read's stored row (risk level, turned-off mark) and overrides.

    The overrides read are those of the install and of *tenant_id*. A class is
    in the set when the stored row has risk level ``safe`` and is not turned
    off, and no override raises its risk level or makes it wait for approval.
    A class with no row, or with a problem from :func:`stored_state_problems`,
    is not in the set. Nor is a class whose test read registration left out
    in this start (:func:`~meho_backplane.operations._left_out.left_out_op`),
    whatever row is stored under that key: the connector starts without a
    test read. An imported row under that key still decides its own calls.
    """
    wanted = [cls for cls in classes if cls.test_operation is not None]
    if not wanted:
        return set()
    ready: set[type[Connector]] = set()
    async with get_sessionmaker()() as session:
        for cls in wanted:
            op_id = str(cls.test_operation)
            if (
                left_out_op(
                    product=cls.product, version=cls.version, impl_id=cls.impl_id, op_id=op_id
                )
                is not None
            ):
                continue
            row = await _test_row(session, cls, op_id)
            if row is None:
                continue
            key = OpKey(cls.product, cls.version, cls.impl_id, op_id)
            overrides = (await load_overrides(session, tenant_id=tenant_id, keys=[key])).get(
                key, []
            )
            if not stored_state_problems(row, overrides):
                ready.add(cls)
    return ready
