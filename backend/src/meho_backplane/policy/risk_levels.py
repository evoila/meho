# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``risk_levels`` part of the policy file: the risk-level edits of one scope.

A risk-level edit is an
:class:`~meho_backplane.db.models.OperationPolicyOverride` row that sets a
risk level (``safety_level``) or the approval flag
(``requires_approval``) of one operation. Rows that set only a category or
labels are no risk-level edit.

* :func:`stored_risk_levels` reads the edits of one scope: a tenant's own
  rows for a tenant, the install-wide rows for the install. Each edit is
  one item with its rule id (``override:<row id>``), the operation, the
  scope, the level, the approval flag, the reason, who set it and when.
* :func:`risk_levels_mistakes` checks the ``risk_levels`` part of a file
  on import. A part that holds exactly the stored edits changes nothing.
  Any other part is refused, with the line named: this version of MEHO
  does not change the edits from a file. A file without the part leaves
  the edits as they are. The check compares each value and its type with
  the stored text, so ``1`` is not ``true``.
* :func:`exact_texts` marks the texts of the edits for the export, and
  :func:`represent_exact_text` writes such a text so that every
  character reads back as stored (U+0085 in double quotes).

Both run only while the ``CUSTOM_POLICY`` switch is on; the caller decides
(:mod:`meho_backplane.policy.service`). This module never reads a setting
and writes nothing.
"""

from __future__ import annotations

import uuid
from typing import Any, Final, Literal

import yaml
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import OperationPolicyOverride

__all__ = [
    "RISK_LEVELS_KEY",
    "RISK_LEVELS_NOT_IN_THIS_VERSION",
    "ExactText",
    "exact_texts",
    "represent_exact_text",
    "risk_levels_mistakes",
    "stored_risk_levels",
]

#: The reserved top-level key of the part.
RISK_LEVELS_KEY: Final[str] = "risk_levels"

#: The end of each refusal of a changed ``risk_levels`` part.
RISK_LEVELS_NOT_IN_THIS_VERSION: Final[str] = (
    "Changing risk-level edits with a policy file is not in this version of MEHO. "
    "Keep the part as `meho policy export` wrote it, or remove it."
)

_STR_TAG: Final[str] = "tag:yaml.org,2002:str"
_MERGE_TAG: Final[str] = "tag:yaml.org,2002:merge"


class ExactText(str):
    """A text of a stored edit; the export writes it so that it reads back as stored."""

    __slots__ = ()


def exact_texts(value: Any) -> Any:
    """*value* with each text in it as an :class:`ExactText`."""
    if isinstance(value, str):
        return ExactText(value)
    if isinstance(value, dict):
        return {key: exact_texts(item) for key, item in value.items()}
    if isinstance(value, list):
        return [exact_texts(item) for item in value]
    return value


def represent_exact_text(dumper: yaml.SafeDumper, data: ExactText) -> yaml.ScalarNode:
    """Write *data* as usual, or in double quotes when YAML would read it back changed.

    YAML reads a bare U+0085 as a line break; in double quotes it is the
    escape ``\\N`` and reads back as stored.
    """
    text = str(data)
    if yaml.safe_load(yaml.safe_dump(text, allow_unicode=True)) == text:
        return dumper.represent_str(text)
    return dumper.represent_scalar(_STR_TAG, text, style='"')


def _item(row: OperationPolicyOverride) -> dict[str, Any]:
    item: dict[str, Any] = {
        "rule": f"override:{row.id}",
        "operation": {
            "product": row.product,
            "version": row.version,
            "impl_id": row.impl_id,
            "op_id": row.op_id,
        },
        "scope": "install" if row.tenant_id is None else "tenant",
    }
    if row.safety_level is not None:
        item["level"] = row.safety_level
    if row.requires_approval is not None:
        item["requires_approval"] = row.requires_approval
    if row.reason:
        item["reason"] = row.reason
    item["set_by"] = row.set_by_sub
    item["set_at"] = row.set_at.isoformat()
    return item


async def stored_risk_levels(
    session: AsyncSession, *, scope: Literal["tenant", "install"], tenant_id: uuid.UUID | None
) -> list[dict[str, Any]]:
    """The risk-level edits of *scope*, one item each, in a fixed order.

    A tenant gets only its own edits, not the install-wide ones; the
    install gets only the install-wide edits. Reads only.
    """
    column = OperationPolicyOverride.tenant_id
    where: ColumnElement[bool]
    if scope == "install":
        where = column.is_(None)
    elif tenant_id is None:
        return []
    else:
        where = column == tenant_id
    stmt = (
        select(OperationPolicyOverride)
        .where(where)
        .where(
            or_(
                OperationPolicyOverride.safety_level.is_not(None),
                OperationPolicyOverride.requires_approval.is_not(None),
            )
        )
        .order_by(
            OperationPolicyOverride.impl_id,
            OperationPolicyOverride.version,
            OperationPolicyOverride.op_id,
            OperationPolicyOverride.set_at,
            OperationPolicyOverride.id,
        )
    )
    return [_item(row) for row in (await session.execute(stmt)).scalars()]


def _part_node(text: str) -> tuple[yaml.Node, yaml.Node] | None:
    """The key node and the value node of the top-level ``risk_levels``; ``None`` without it."""
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if not isinstance(root, yaml.MappingNode):
        return None
    for key_node, value_node in root.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == RISK_LEVELS_KEY:
            return key_node, value_node
    return None


def _line(node: yaml.Node) -> int:
    return int(node.start_mark.line) + 1


def _merge_key_line(text: str) -> int:
    """The line of the last top-level merge key (``<<``) that brings in ``risk_levels``.

    The last one wins when several bring it in. Line 1 without one.
    """
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    pairs = root.value if isinstance(root, yaml.MappingNode) else []
    line = 1
    loader = yaml.SafeLoader("")
    try:
        for key_node, value_node in pairs:
            if key_node.tag != _MERGE_TAG:
                continue
            merged = loader.construct_object(value_node, deep=True)
            parts = merged if isinstance(merged, list) else [merged]
            if any(isinstance(part, dict) and RISK_LEVELS_KEY in part for part in parts):
                line = _line(key_node)
    finally:
        loader.dispose()
    return line


def _same(left: Any, right: Any) -> bool:
    """*left* equals *right*, each value also in its type: ``1`` is not ``true``."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(map(_same, left, right))
    return bool(left == right)


def _items(value_node: yaml.SequenceNode) -> list[tuple[int, Any]]:
    """Each item of the part with its line, built as the safe loader builds it."""
    loader = yaml.SafeLoader("")
    try:
        return [(_line(n), loader.construct_object(n, deep=True)) for n in value_node.value]
    finally:
        loader.dispose()


async def risk_levels_mistakes(
    session: AsyncSession,
    *,
    content: bytes,
    in_file: bool,
    scope: Literal["tenant", "install"],
    tenant_id: uuid.UUID | None,
) -> list[str]:
    """The mistakes of the ``risk_levels`` part of *content*, a readable YAML mapping.

    No mistake when the file has no such part, or when the part holds
    exactly the stored edits of *scope* (in any order). Each item that is
    not a stored edit or repeats one is a sentence that names its line. An
    item is a stored edit when each value equals the stored one, in its
    type too: the reason is compared with the stored text, character by
    character. The stored edits the part misses are one sentence that
    names the line of ``risk_levels``, and so is a part that is not a list.
    The sentences never repeat a value of the file.

    *in_file* says the parser saw the key in the file. When it came in
    through a merge key (``<<``), the part is refused at the line of the
    merge key that brings it in, whatever it holds.
    """
    if not in_file:
        return []
    text = content.decode("utf-8")
    found = _part_node(text)
    if found is None:
        return [
            f"Line {_merge_key_line(text)}: `risk_levels` comes in through a merge key (`<<`). "
            f"{RISK_LEVELS_NOT_IN_THIS_VERSION}"
        ]
    key_node, value_node = found
    stored = {
        item["rule"]: item
        for item in await stored_risk_levels(session, scope=scope, tenant_id=tenant_id)
    }
    if not isinstance(value_node, yaml.SequenceNode):
        return [
            f"Line {_line(key_node)}: `risk_levels` is not the list of risk-level edits. "
            f"{RISK_LEVELS_NOT_IN_THIS_VERSION}"
        ]
    mistakes: list[str] = []
    seen: set[str] = set()
    for index, (line, item) in enumerate(_items(value_node)):
        rule = item.get("rule") if isinstance(item, dict) else None
        same = isinstance(rule, str) and rule not in seen and _same(stored.get(rule), item)
        if isinstance(rule, str):
            seen.add(rule)
        if not same:
            mistakes.append(
                f"Line {line}: `risk_levels[{index}]` is not a stored risk-level edit. "
                f"{RISK_LEVELS_NOT_IN_THIS_VERSION}"
            )
    missing = [rule for rule in stored if rule not in seen]
    if missing:
        names = ", ".join(f"`{rule}`" for rule in missing)
        mistakes.append(
            f"Line {_line(key_node)}: `risk_levels` misses the stored edits {names}. "
            f"{RISK_LEVELS_NOT_IN_THIS_VERSION}"
        )
    return mistakes
