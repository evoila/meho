# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Edit the classification of many operations at once: dry run, then request.

One edit sets the category, labels or "always wait" mark of one entry of
the tenant's classification. Its ``match`` can name one operation, one
group, one connector, or many operations by pattern (for example
``{"op": "k8s.deployment.*"}``). A pattern can match more than the admin
expects, so every edit surface works in two steps:

1. :func:`preview_classification_edit` is the dry run. It lists every
   operation the entry matches, with its values before and after the
   edit. It writes nothing.
2. :func:`request_classification_edit` asks for the change. It runs the
   dry run again and refuses an edit that is not valid, that matches no
   operation, that would give an operation a conflict, or that sets back
   an "always wait" of the install's policy. Otherwise it
   calls the ``policy.classify`` operation through the normal call path.
   That call is a change of rights: it waits until a second tenant admin
   approves it (:mod:`meho_backplane.connectors.policy`).

REST, the CLI, the console and the admin MCP tool all use these two
functions, so every surface gives the same result.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import batched
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations.op_policy import (
    EffectivePolicy,
    OpKey,
    load_overrides,
    policy_in_force,
)
from meho_backplane.policy.classification import (
    Classification,
    OpCoordinates,
    classify,
    has_wildcard,
)
from meho_backplane.policy.store import (
    SWITCH_OFF_REFUSAL,
    ClassificationEdit,
    EditPlan,
    classification_enabled,
    connector_id_of,
    plan_edit,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "ClassificationEditError",
    "ClassificationOffError",
    "ClassificationPreview",
    "MatchedOperation",
    "edit_from_params",
    "one_thing_refusal",
    "params_of",
    "preview_classification_edit",
    "request_classification_edit",
]


class ClassificationOffError(RuntimeError):
    """The ``POLICY_CLASSIFICATION`` switch is off."""

    def __init__(self) -> None:
        super().__init__(SWITCH_OFF_REFUSAL)


class ClassificationEditError(ValueError):
    """The edit is refused before any request is made; nothing changed."""


#: How many operation ids one query of the dry run asks for.
_ROWS_PER_QUERY: Final[int] = 500


@dataclass(frozen=True)
class MatchedOperation:
    """One operation an edit matches, with its classified values before and after.

    ``before_in_force`` and ``after_in_force`` are the risk level and the
    wait that apply with those values: after the code's value, the
    overrides and the floors (the minimum-level rules). ``None`` when the
    operation's row is gone.
    """

    connector_id: str
    group_key: str | None
    op_id: str
    before: Classification
    after: Classification
    before_in_force: EffectivePolicy | None = None
    after_in_force: EffectivePolicy | None = None

    @property
    def changes(self) -> bool:
        """True when the edit changes a value of this operation."""
        return _values(self.before) != _values(self.after)

    def to_dict(self) -> dict[str, Any]:
        """The wire shape of one row."""
        return {
            "connector_id": self.connector_id,
            "group_key": self.group_key,
            "op_id": self.op_id,
            "before": {**_values(self.before), "in_force": _in_force(self.before_in_force)},
            "after": {**_values(self.after), "in_force": _in_force(self.after_in_force)},
            "changes": self.changes,
        }


@dataclass(frozen=True)
class ClassificationPreview:
    """The dry run of one edit. ``conflict`` is the reason the edit would be refused."""

    match: dict[str, str]
    rule_ref: str
    entry: dict[str, Any]
    operations: list[MatchedOperation]
    conflict: str | None

    def to_dict(self) -> dict[str, Any]:
        """The wire shape: every matched operation, never cut."""
        return {
            "match": self.match,
            "rule_ref": self.rule_ref,
            "entry": self.entry,
            "matched_operations": len(self.operations),
            "changed_operations": sum(1 for op in self.operations if op.changes),
            "conflict": self.conflict,
            "operations": [op.to_dict() for op in self.operations],
        }


def _values(classified: Classification) -> dict[str, Any]:
    return {
        "category": classified.category,
        "labels": dict(classified.labels),
        "risk": classified.risk,
        "always_wait": classified.always_wait,
        "rule_ref": classified.rule_ref,
    }


def _in_force(policy: EffectivePolicy | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    return {"risk": policy.safety_level, "waits": policy.requires_approval}


def edit_from_params(params: Mapping[str, Any]) -> ClassificationEdit:
    """Build an edit from the ``policy.classify`` parameter shape."""
    match = params.get("match")
    if not isinstance(match, Mapping):
        raise ClassificationEditError("`match` needs at least one of connector, group or op.")
    return ClassificationEdit(
        match=match,
        category=params.get("category"),
        labels=params.get("labels"),
        always_wait=params.get("always_wait"),
    )


def params_of(edit: ClassificationEdit) -> dict[str, Any]:
    """The ``policy.classify`` parameters of *edit*: only the fields it sets."""
    params: dict[str, Any] = {"match": dict(edit.match)}
    if edit.category is not None:
        params["category"] = edit.category
    if edit.labels is not None:
        params["labels"] = dict(edit.labels)
    if edit.always_wait is not None:
        params["always_wait"] = edit.always_wait
    return params


def one_thing_refusal(match: Mapping[str, str]) -> str | None:
    """Why a door that edits one connector, group or operation refuses *match*, or ``None``.

    Such a door takes the id as plain text. ``*``, ``?`` and ``[`` would
    make it a pattern that can match more, so they are refused there; the
    pattern edit (dry run first) is the door for a pattern.
    """
    for key, value in match.items():
        if has_wildcard(value):
            return (
                f"The {key} {value!r} holds *, ? or [. This edits one {key} only. To edit "
                "by pattern, use POST /api/v1/policy/classification/preview, "
                "`meho connector classify` or the classify page of the console."
            )
    return None


def _first_conflict(rows: list[MatchedOperation]) -> str | None:
    return next((row.after.conflict for row in rows if row.after.conflict is not None), None)


async def _descriptor_rows(
    session: AsyncSession, tenant_id: UUID, wanted: Iterable[OpCoordinates]
) -> dict[OpCoordinates, EndpointDescriptor]:
    """The operation row of each of *wanted*; the tenant's own row before a built-in one."""
    wanted_set = set(wanted)
    group = aliased(OperationGroup)
    visible = or_(EndpointDescriptor.tenant_id.is_(None), EndpointDescriptor.tenant_id == tenant_id)
    found: dict[OpCoordinates, EndpointDescriptor] = {}
    for op_ids in batched(sorted({c.op_id for c in wanted_set}), _ROWS_PER_QUERY, strict=False):
        stmt = (
            select(EndpointDescriptor, group.group_key)
            .outerjoin(group, EndpointDescriptor.group_id == group.id)
            .where(visible, EndpointDescriptor.op_id.in_(op_ids))
        )
        for row, group_key in (await session.execute(stmt)).tuples():
            coords = OpCoordinates(connector_id_of(row), group_key, row.op_id)
            if coords in wanted_set and (coords not in found or row.tenant_id is not None):
                found[coords] = row
    return found


async def _matched_operations(
    session: AsyncSession, tenant_id: UUID, plan: EditPlan
) -> list[MatchedOperation]:
    """Each matched operation, with its values and the values in force, before and after."""
    rows = await _descriptor_rows(session, tenant_id, plan.matched)
    keys = map(OpKey.of, rows.values())
    overrides = await load_overrides(session, tenant_id=tenant_id, keys=keys)
    matched: list[MatchedOperation] = []
    for coords in plan.matched:
        before = classify(coords, plan.before, plan.taxonomy)
        after = classify(coords, plan.after, plan.taxonomy)
        row = rows.get(coords)
        in_force: tuple[EffectivePolicy | None, EffectivePolicy | None] = (None, None)
        if row is not None:
            row_overrides = overrides.get(OpKey.of(row), [])
            in_force = (
                policy_in_force(row, row_overrides, before),
                policy_in_force(row, row_overrides, after),
            )
        matched.append(
            MatchedOperation(
                connector_id=coords.connector_id,
                group_key=coords.group_key,
                op_id=coords.op_id,
                before=before,
                after=after,
                before_in_force=in_force[0],
                after_in_force=in_force[1],
            )
        )
    return matched


async def preview_classification_edit(
    operator: Operator, edit: ClassificationEdit
) -> ClassificationPreview:
    """Return the dry run of *edit* for *operator*'s tenant. Writes nothing.

    Raises :class:`ClassificationOffError` while the switch is off and
    :class:`ClassificationEditError` for an edit that is not valid (no
    pattern, no value, an unknown category, bad labels).
    """
    if not classification_enabled():
        raise ClassificationOffError
    async with get_sessionmaker()() as session:
        try:
            plan = await plan_edit(session, tenant_id=operator.tenant_id, edit=edit)
        except ValueError as exc:
            raise ClassificationEditError(str(exc)) from exc
        rows = await _matched_operations(session, operator.tenant_id, plan)
    return ClassificationPreview(
        match=dict(edit.match),
        rule_ref=plan.rule_ref,
        entry=plan.entry,
        operations=rows,
        conflict=_first_conflict(rows) or plan.install_wait_refusal,
    )


async def request_classification_edit(
    operator: Operator, edit: ClassificationEdit
) -> dict[str, Any]:
    """Ask for *edit*; return the call result, which waits for a second admin.

    Refuses (:class:`ClassificationEditError`, no request made) an edit
    that is not valid, that matches no operation, that would give an
    operation a conflict, or that sets back an "always wait" of the
    install's policy. The request is a ``policy.classify`` call
    through :func:`~meho_backplane.operations.meta_tools.call_operation`,
    so the role check, the approval and the audit row are the ones of
    every other call. The returned dict is the call result plus
    ``matched_operations`` and ``changed_operations`` of the dry run.
    """
    preview = await preview_classification_edit(operator, edit)
    if not preview.operations:
        raise ClassificationEditError(
            "The match finds no operation your tenant can see. Check the connector id, "
            "the group key and the operation id."
        )
    if preview.conflict is not None:
        raise ClassificationEditError(preview.conflict)
    # Imported here: the policy connector package imports this module for
    # its dry-run operation, and the call path loads every connector package.
    from meho_backplane.connectors.policy.classify import (
        POLICY_CLASSIFY_OP_ID,
        POLICY_REGISTRY_CONNECTOR_ID,
    )
    from meho_backplane.operations.meta_tools import call_operation

    result = await call_operation(
        operator,
        {
            "connector_id": POLICY_REGISTRY_CONNECTOR_ID,
            "op_id": POLICY_CLASSIFY_OP_ID,
            "target": None,
            "params": params_of(edit),
        },
    )
    summary = preview.to_dict()
    return {
        **result,
        "matched_operations": summary["matched_operations"],
        "changed_operations": summary["changed_operations"],
    }
