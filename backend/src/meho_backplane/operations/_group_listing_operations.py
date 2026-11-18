# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Each group's typed operations in the group listing, behind a switch (#4713).

Without this, a model must search to see any operation, and a search needs
good words. With the switch ``GROUP_LISTING_OPERATIONS``
(:attr:`~meho_backplane.settings.Settings.group_listing_operations`, off by
default), ``list_operation_groups`` also lists, for each group, its typed and
multi-step operations (``source_kind`` ``typed`` and ``composite``). So a
model can walk down without a search query: connector, group, operation,
contract (``search_operations`` with ``op_id``), call.

Each listed operation has four fields: ``op_id``, a one-line ``summary``,
``safety_level`` and ``requires_approval``. The risk level and the approval
flag are the ones a call uses (the operator overrides included), the same
values ``search_operations`` shows. The full contract stays in
``search_operations`` with ``op_id``.

Raw operations (made from a vendor API description) are never listed one by
one: a big connector has thousands. ``operation_count`` still counts them
when the listing shows them (see :mod:`._search_typed_first`), and search
finds them.

Size limits, measured as compact JSON:

* one group stays under :data:`GROUP_MAX_BYTES`. When a group has more
  operations than fit, the rest are left out and ``operations_more`` says
  how many. ``search_operations`` with ``group`` finds them;
* one answer stays under :data:`ANSWER_MAX_BYTES`. When the groups do not
  fit, the answer ends early and ``next_cursor`` names the last group shown,
  like a full page. An answer always holds at least one group.

The summary line is the first sentence of the operation's summary, cut to
:data:`SUMMARY_MAX_CHARS`. An operation without a summary has ``summary:
null``. With the summary cut at that length, every group of every built-in
connector fits its limit whole (``tests/test_group_listing_operations.py``
checks it).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import EndpointDescriptor, OperationGroup
from meho_backplane.operations._lookup import (
    enabled_for_tenant_clause,
    installed_owner_clause,
    visible_scope_clause,
)
from meho_backplane.operations._search_typed_first import TYPED_KINDS
from meho_backplane.operations.op_policy import OpKey, effective_policy_map
from meho_backplane.settings import get_settings

__all__ = [
    "ANSWER_MAX_BYTES",
    "GROUP_MAX_BYTES",
    "GROUP_OPERATIONS_LINE",
    "SUMMARY_MAX_CHARS",
    "add_group_operations",
    "compact_size",
    "fit_group",
    "group_listing_operations",
    "load_group_operations",
    "page_groups",
    "summary_line",
]

#: One group, as compact JSON, stays under this many bytes.
GROUP_MAX_BYTES: Final[int] = 4 * 1024

#: One answer, as compact JSON, stays under this many bytes.
ANSWER_MAX_BYTES: Final[int] = 12 * 1024

#: The longest summary line, in characters, the cut mark included.
SUMMARY_MAX_CHARS: Final[int] = 100

_CUT_MARK: Final[str] = "…"

#: A sentence ends at ``.``, ``!`` or ``?`` before a space and a capital letter.
_SENTENCE_END: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")

#: The line the tool description ends with while the switch is on.
GROUP_OPERATIONS_LINE: Final[str] = (
    " Each group also lists its typed and multi-step `operations` (`op_id`, a "
    "one-line `summary`, `safety_level`, `requires_approval`). Pick one, then "
    "call `search_operations` with its `op_id` for the full contract. "
    "`operations_more` counts operations left out to keep the answer small; "
    "find them with `search_operations` and `group`. Pass `group` to list "
    "one group only."
)


def group_listing_operations() -> bool:
    """Return the state of the ``GROUP_LISTING_OPERATIONS`` switch."""
    return get_settings().group_listing_operations


def compact_size(value: Any) -> int:
    """Return the size of *value* as compact JSON, in bytes.

    ``ensure_ascii`` stays on, so a character outside ASCII counts as its
    escape. That is never less than its UTF-8 size.
    """
    return len(json.dumps(value, separators=(",", ":")))


def summary_line(summary: str | None) -> str | None:
    """Return the first sentence of *summary*, cut to :data:`SUMMARY_MAX_CHARS`."""
    text = " ".join((summary or "").split())
    if not text:
        return None
    first = _SENTENCE_END.split(text, maxsplit=1)[0]
    if len(first) <= SUMMARY_MAX_CHARS:
        return first
    return first[: SUMMARY_MAX_CHARS - len(_CUT_MARK)].rstrip() + _CUT_MARK


async def load_group_operations(
    session: AsyncSession,
    groups: Sequence[OperationGroup],
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """Return the typed and multi-step operation lines of each group, by group key.

    Only operations that are on for *tenant_id* and whose connector package
    is installed, the same rule as the operation counts, and only rows in
    the caller's tenant scope, the same scope as dispatch. Ordered by
    ``op_id``.
    """
    if not groups:
        return {}
    group_keys = {g.id: g.group_key for g in groups}
    stmt = (
        select(EndpointDescriptor)
        .where(
            EndpointDescriptor.group_id.in_(list(group_keys)),
            visible_scope_clause(
                EndpointDescriptor,
                tenant_id=tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
            ),
            EndpointDescriptor.product == product,
            EndpointDescriptor.version == version,
            EndpointDescriptor.impl_id == impl_id,
            EndpointDescriptor.source_kind.in_(TYPED_KINDS),
            enabled_for_tenant_clause(tenant_id=tenant_id),
            installed_owner_clause(),
        )
        .order_by(EndpointDescriptor.op_id)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    row_keys = {row.id: group_keys[row.group_id] for row in rows if row.group_id is not None}
    policies = await effective_policy_map(
        session, tenant_id=tenant_id, rows=rows, group_keys=row_keys
    )
    lines: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        policy = policies[OpKey.of(row)]
        lines.setdefault(row_keys[row.id], []).append(
            {
                "op_id": row.op_id,
                "summary": summary_line(row.summary),
                "safety_level": policy.safety_level,
                "requires_approval": policy.requires_approval,
            }
        )
    return lines


def fit_group(group: dict[str, Any], lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Return *group* with as many of *lines* as fit under :data:`GROUP_MAX_BYTES`.

    When some do not fit, ``operations_more`` says how many were left out.
    The size is counted with that field in place, so the result never goes
    over the limit because of it. A group whose own text is already over the
    limit keeps its text and lists no operation.
    """
    size = compact_size({**group, "operations": [], "operations_more": len(lines)})
    kept: list[dict[str, Any]] = []
    for line in lines:
        added = compact_size(line) + (1 if kept else 0)
        if size + added >= GROUP_MAX_BYTES:
            break
        size += added
        kept.append(line)
    fitted = {**group, "operations": kept}
    if len(kept) < len(lines):
        fitted["operations_more"] = len(lines) - len(kept)
    return fitted


def page_groups(answer: dict[str, Any], groups: list[dict[str, Any]]) -> int:
    """Return how many of *groups* fit into *answer* under :data:`ANSWER_MAX_BYTES`.

    *answer* is the answer without its groups. The size is counted with the
    longest ``next_cursor`` the answer could carry. At least one group always
    fits, so paging always moves on.
    """
    longest_cursor = max((len(g["group_key"]) for g in groups), default=0)
    size = compact_size({**answer, "groups": [], "next_cursor": "x" * longest_cursor})
    count = 0
    for group in groups:
        added = compact_size(group) + (1 if count else 0)
        if count and size + added >= ANSWER_MAX_BYTES:
            break
        size += added
        count += 1
    return count


def add_group_operations(
    answer: dict[str, Any],
    lines: dict[str, list[dict[str, Any]]],
    *,
    group_filter: str | None,
) -> None:
    """Put the operation lines into *answer* and end it at the size limit.

    *answer* is a ``list_operation_groups`` answer. Each group gets its
    ``operations`` (:func:`fit_group`). When not all groups fit
    (:func:`page_groups`), the answer ends early and ``next_cursor`` is the
    last group shown. When *group_filter* matched no group, ``note`` says so.
    """
    fitted = [fit_group(g, lines.get(g["group_key"], [])) for g in answer["groups"]]
    rest = {k: v for k, v in answer.items() if k not in ("groups", "next_cursor")}
    count = page_groups(rest, fitted)
    answer["groups"] = fitted[:count]
    if count < len(fitted):
        answer["next_cursor"] = fitted[count - 1]["group_key"]
    if group_filter is not None and not fitted:
        answer["note"] = (
            f"Connector {answer['connector_id']!r} shows no group {group_filter!r}. "
            "Call `list_operation_groups` again without `group` to see the group keys."
        )
