# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval table: one row for each built-in typed operation.

``tests/snapshots/approval_table.json`` lists, for every operation that the
typed connectors of this repository register: the operation id, the
connector id, the op class, the risk level (``safety_level``), whether the
call needs approval, and why no grant can cover it (the kind, or ``null``).
``tests/test_approval_table_snapshot.py`` fails when the code and the file
differ. The CI job "Secret hiding compared with the base branch" also judges
the file against the base branch (``scripts/ci/approval_table_judge.py``).

Write the file again, from ``backend/``::

    MEHO_WRITE_APPROVAL_TABLE=1 uv run pytest -o tmp_path_retention_policy=failed \\
        tests/test_approval_table_snapshot.py

The table is the built-in default only: no install override (the test
removes ``SERVICE_GRANT_DELETE_SHAPED_PATTERNS``), no tenant policy row, no
operation made from an API file at run time. The test turns
``POLICY_CLASSIFICATION`` on, so the policy operations are in it.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

from sqlalchemy import select

from meho_backplane.broadcast.events import resolve_op_class
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.service_grants import never_covered_reason
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.policy.store import connector_id_of

WRITE_ENV = "MEHO_WRITE_APPROVAL_TABLE"
WRITE_COMMAND = (
    f"{WRITE_ENV}=1 uv run pytest -o tmp_path_retention_policy=failed "
    "tests/test_approval_table_snapshot.py"
)

Row = dict[str, Any]


async def register_all_descriptors() -> list[EndpointDescriptor]:
    """Register every typed operation in the test database and read the rows back."""
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await run_typed_op_registrars(embedding_service=embedding)
    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(EndpointDescriptor))).scalars())
        session.expunge_all()
    return rows


def build_rows(descriptors: list[EndpointDescriptor]) -> list[Row]:
    """One row for each built-in descriptor, sorted by operation id, then connector id."""
    rows: list[Row] = []
    for descriptor in descriptors:
        if descriptor.tenant_id is not None:
            continue
        never = never_covered_reason(descriptor)
        rows.append(
            {
                "op_id": descriptor.op_id,
                "connector_id": connector_id_of(descriptor),
                "class": resolve_op_class(descriptor.op_id, descriptor.op_class),
                "risk_level": descriptor.safety_level,
                "needs_approval": bool(descriptor.requires_approval),
                "never_covered": never[0] if never else None,
            }
        )
    return sorted(rows, key=lambda row: (row["op_id"], row["connector_id"]))


def render(rows: list[Row]) -> str:
    """The file text: a JSON list with one row on each line, so a diff shows one row."""
    lines = [json.dumps(row, sort_keys=True) for row in rows]
    return "[\n" + ",\n".join(f"  {line}" for line in lines) + "\n]\n"


def _key(row: Row) -> tuple[str, str]:
    return row["connector_id"], row["op_id"]


def describe_difference(expected: list[Row], actual: list[Row]) -> list[str]:
    """Plain lines that name the rows that were added, removed or changed."""
    before = {_key(row): row for row in expected}
    after = {_key(row): row for row in actual}
    lines = [f"new row: {key[1]} ({key[0]})" for key in sorted(after.keys() - before.keys())]
    lines += [f"removed row: {key[1]} ({key[0]})" for key in sorted(before.keys() - after.keys())]
    for key in sorted(before.keys() & after.keys()):
        changes = [
            f"{field} {before[key][field]!r} -> {after[key][field]!r}"
            for field in before[key]
            if before[key][field] != after[key].get(field)
        ]
        if changes:
            lines.append(f"changed row: {key[1]} ({key[0]}): " + ", ".join(changes))
    return lines
