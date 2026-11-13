# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check a policy file against the operations MEHO knows and against the floors.

The floors are rules in the code that no policy file can loosen:

* An operation that shows a secret stays at least ``caution`` and always
  waits (:mod:`meho_backplane.operations.credential_read`).
* An operation that connects to an address the caller gives stays at
  least ``caution`` and always waits
  (:mod:`meho_backplane.operations.caller_address`).
* A file can raise a risk level, never lower it below the level in the
  code.

Each problem is one sentence that names the entry
(``classifications[N]``) and the operation. :func:`check_document` also
refuses an unknown category or risk level, a ``taxonomy.default`` in a
tenant's file, and two entries that give one operation different
categories at the same level. The ``bindings`` are checked by
:func:`~meho_backplane.policy.bindings.binding_mistakes`: no binding in the
install's file, no runner, no agent or service with a kind that may
approve, and no unknown category.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations.caller_address import has_caller_address
from meho_backplane.operations.credential_read import has_credential_read_floor
from meho_backplane.policy.bindings import binding_mistakes
from meho_backplane.policy.classification import (
    RISK_LEVELS,
    ClassificationConflictError,
    ClassificationEntry,
    OpCoordinates,
    Scope,
    Taxonomy,
    check_no_conflict,
    entries_from_document,
    taxonomy_from_documents,
)

__all__ = ["MAX_MISTAKES", "KnownOp", "check_document", "load_known_ops"]

#: At most this many mistakes are listed; one more line says how many were left out.
MAX_MISTAKES: Final[int] = 50

_LEVELS_TEXT: Final[str] = "safe, caution, dangerous or destructive"


@dataclass(frozen=True)
class KnownOp:
    """One operation row, with what the floors need to know about it."""

    coords: OpCoordinates
    safety_level: str
    shows_secret: bool
    caller_address: bool


async def load_known_ops(
    session: AsyncSession, *, scope: Scope, tenant_id: UUID | None
) -> list[KnownOp]:
    """Every operation an entry of *scope* can match.

    A tenant's file matches the built-in operations and the tenant's own.
    The install's file matches every operation, of every tenant.
    """
    group = aliased(OperationGroup)
    stmt = select(
        EndpointDescriptor.impl_id,
        EndpointDescriptor.version,
        group.group_key,
        EndpointDescriptor.op_id,
        EndpointDescriptor.safety_level,
        EndpointDescriptor.parameter_schema,
    ).outerjoin(group, EndpointDescriptor.group_id == group.id)
    if scope == "tenant":
        visible: ColumnElement[bool] = EndpointDescriptor.tenant_id.is_(None)
        if tenant_id is not None:
            visible = or_(visible, EndpointDescriptor.tenant_id == tenant_id)
        stmt = stmt.where(visible)
    return [
        KnownOp(
            coords=OpCoordinates(f"{impl_id}-{version}", group_key, op_id),
            safety_level=str(safety_level),
            shows_secret=has_credential_read_floor(op_id),
            caller_address=has_caller_address(parameter_schema),
        )
        for impl_id, version, group_key, op_id, safety_level, parameter_schema in (
            await session.execute(stmt)
        ).tuples()
    ]


def _taxonomy_mistakes(document: dict[str, Any], *, scope: Scope, taxonomy: Taxonomy) -> list[str]:
    raw = document.get("taxonomy") or {}
    if scope == "tenant":
        if raw.get("default") is None:
            return []
        return [
            "`taxonomy.default` belongs to the install's policy file; a tenant's file can "
            "only add categories."
        ]
    if taxonomy.default not in taxonomy.categories:
        known = ", ".join(taxonomy.categories)
        return [
            f"`taxonomy.default` `{taxonomy.default}` is not in the categories ({known}). "
            "Set `taxonomy.default` to one of them."
        ]
    return []


def _entry_value_mistakes(raw_entries: Sequence[dict[str, Any]], taxonomy: Taxonomy) -> list[str]:
    """An unknown risk level or category: one sentence each."""
    mistakes: list[str] = []
    known = ", ".join(taxonomy.categories)
    for index, raw in enumerate(raw_entries):
        risk = raw.get("risk")
        if risk is not None and risk not in RISK_LEVELS:
            mistakes.append(
                f"`classifications[{index}].risk` `{risk}` is not a risk level; use {_LEVELS_TEXT}."
            )
        category = raw.get("category")
        if category is not None and category not in taxonomy.categories:
            mistakes.append(
                f"`classifications[{index}].category` `{category}` is not in the "
                f"taxonomy ({known})."
            )
    return mistakes


def _floor_sentence(index: int, entry: ClassificationEntry, op: KnownOp) -> str | None:
    """Why *entry* may not apply to *op*, or ``None`` when it may."""
    op_id = op.coords.op_id
    loosens = entry.risk == "safe" or entry.always_wait is False
    if op.shows_secret and loosens:
        return (
            f"`classifications[{index}]` would make `{op_id}` safe or not waiting. "
            f"`{op_id}` shows a secret, so it stays at least caution and always waits."
        )
    if op.caller_address and loosens:
        return (
            f"`classifications[{index}]`: `{op_id}` connects to an address the caller "
            "gives, so it stays at least caution and always waits."
        )
    code_level = op.safety_level if op.safety_level in RISK_LEVELS else "safe"
    if entry.risk is not None and RISK_LEVELS.index(entry.risk) < RISK_LEVELS.index(code_level):
        return (
            f"`classifications[{index}]` would set `{op_id}` to {entry.risk}, below its "
            f"level in the code ({code_level}). A file can raise a risk level, not lower "
            "it. To lower it, open a pull request that changes the code."
        )
    return None


def _floor_mistakes(entries: Sequence[ClassificationEntry], ops: Sequence[KnownOp]) -> list[str]:
    mistakes: list[str] = []
    for index, entry in enumerate(entries):
        if entry.risk is None and entry.always_wait is not False:
            continue
        for op in ops:
            if not entry.matches(op.coords):
                continue
            sentence = _floor_sentence(index, entry, op)
            if sentence is not None and sentence not in mistakes:
                mistakes.append(sentence)
    return mistakes


def _install_wait_warnings(entries: Sequence[ClassificationEntry]) -> list[str]:
    return [
        f"`classifications[{index}]` makes operations always wait for the whole install. "
        "A tenant's own entry that is as specific or more specific can set it back to "
        "the operation's own value."
        for index, entry in enumerate(entries)
        if entry.always_wait is True
    ]


def _capped(mistakes: list[str]) -> list[str]:
    if len(mistakes) <= MAX_MISTAKES:
        return mistakes
    left_out = len(mistakes) - MAX_MISTAKES
    return [*mistakes[:MAX_MISTAKES], f"And {left_out} more mistakes like these."]


def check_document(
    document: dict[str, Any],
    *,
    scope: Scope,
    install_document: dict[str, Any] | None,
    ops: Sequence[KnownOp],
    other_entries: Sequence[ClassificationEntry] = (),
) -> tuple[list[str], list[str]]:
    """Return the mistakes and the warnings of *document*, a valid file's stored form.

    *install_document* is the stored install policy (for a tenant's file:
    its taxonomy is the base, and its entries take part in the conflict
    check). *other_entries* are more entries that apply next to the file
    (the category and labels set on one operation).
    """
    taxonomy = (
        taxonomy_from_documents(document, None)
        if scope == "install"
        else taxonomy_from_documents(install_document, document)
    )
    raw_entries: list[dict[str, Any]] = list(document.get("classifications") or [])
    mistakes = _taxonomy_mistakes(document, scope=scope, taxonomy=taxonomy)
    mistakes.extend(_entry_value_mistakes(raw_entries, taxonomy))
    mistakes.extend(binding_mistakes(document, scope=scope, taxonomy=taxonomy))
    if mistakes:
        return _capped(mistakes), []
    entries = entries_from_document(document, scope=scope)
    mistakes = _floor_mistakes(entries, ops)
    if not mistakes:
        base = entries_from_document(install_document, scope="install") if scope == "tenant" else []
        try:
            check_no_conflict(
                (op.coords for op in ops), [*base, *entries, *other_entries], taxonomy
            )
        except ClassificationConflictError as exc:
            mistakes.append(str(exc))
    warnings = _install_wait_warnings(entries) if scope == "install" else []
    return _capped(mistakes), warnings
