# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The plan of a policy file import (:func:`meho_backplane.policy.service.build_plan`).

* A stored policy whose entries are not maps is "not valid": the plan
  says so, shows those entries as removed, and the import replaces it.
* Two different matches never share one line of the plan, also when the
  text of one pattern looks like the parts of another.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantPolicy
from meho_backplane.policy.document import file_sha256
from meho_backplane.policy.service import apply_policy, build_plan

from ._targets_helpers import _settings_env  # noqa: F401

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000633c")

_FILE = "policy: v1\nclassifications:\n  - {match: {connector: polfix-1.x}, category: network}\n"


async def _store(document: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=_TENANT, document=document, set_by_sub="fixture"))
        await session.commit()


async def _plan(content: str) -> Any:
    async with get_sessionmaker()() as session:
        return await build_plan(
            session, scope="tenant", tenant_id=_TENANT, content=content.encode()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        pytest.param({"classifications": ["not a map", 7]}, id="entries-not-maps"),
        pytest.param({"classifications": [{"match": "x", "category": "network"}]}, id="match-text"),
        pytest.param({"classifications": "not a list"}, id="list-not-a-list"),
    ],
)
async def test_a_stored_policy_with_entries_that_are_not_maps_is_replaced(
    stored: dict[str, Any],
) -> None:
    await _store(stored)
    plan = await _plan(_FILE)
    assert plan.mistakes == []
    assert any("is not valid" in warning for warning in plan.warnings), plan.warnings
    assert plan.diff is not None
    added = plan.diff["classifications"]["added"]
    assert [entry["entry"]["match"] for entry in added] == [{"connector": "polfix-1.x"}]
    raw = stored["classifications"]
    removed = plan.diff["classifications"]["removed"]
    assert [entry["entry"] for entry in removed] == (raw if isinstance(raw, list) else [])
    assert all(entry["matches"] is None for entry in removed)

    async with get_sessionmaker()() as session, session.begin():
        result = await apply_policy(
            session,
            scope="tenant",
            tenant_id=_TENANT,
            content=_FILE.encode(),
            expected_sha256=file_sha256(_FILE.encode()),
            based_on=plan.based_on,
            imported_by="admin-a",
            approved_by="admin-b",
        )
    assert result["applied"] is True
    async with get_sessionmaker()() as session:
        row = (await session.execute(select(TenantPolicy))).scalar_one()
    assert row.document["classifications"] == [
        {"match": {"connector": "polfix-1.x"}, "category": "network"}
    ]


@pytest.mark.asyncio
async def test_two_matches_that_look_alike_are_two_lines() -> None:
    content = (
        "policy: v1\n"
        "classifications:\n"
        "  - {match: {connector: 'a|group=b'}, category: network}\n"
        "  - {match: {connector: a, group: 'b|group='}, category: network}\n"
    )
    plan = await _plan(content)
    assert plan.mistakes == []
    assert plan.diff is not None
    added = [entry["entry"]["match"] for entry in plan.diff["classifications"]["added"]]
    assert added == [{"connector": "a|group=b"}, {"connector": "a", "group": "b|group="}]


@pytest.mark.asyncio
async def test_a_changed_entry_is_one_line_with_before_and_after() -> None:
    await _store({"classifications": [{"match": {"connector": "polfix-1.x"}, "category": "other"}]})
    plan = await _plan(_FILE)
    assert plan.diff is not None
    diff = plan.diff["classifications"]
    assert (diff["added"], diff["removed"]) == ([], [])
    (changed,) = diff["changed"]
    assert (changed["before"]["category"], changed["after"]["category"]) == ("other", "network")
