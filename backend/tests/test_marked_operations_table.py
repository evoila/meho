# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The table of marked operations matches the registered operations.

``src/meho_backplane/policy/marked_operations.yaml`` gives each marked
built-in typed operation its risk level for when the switch
``CUSTOM_POLICY`` is on (``docs/codebase/policy-classification.md``).
Nothing reads the table yet. This test builds the three groups from the
registered operations, the same way as ``tests/approval_table.py``, and
checks the table against them:

1. ``caution`` with the plain "always ask" mark and no reason that blocks
   a grant;
2. "approve each call" (``declare_per_call_approval``);
3. ``dangerous`` with the plain mark and no reason that blocks a grant.
"""

from __future__ import annotations

from importlib.resources import files
from typing import Any

import pytest
import yaml

from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations.grant_approval import is_rights_change_op
from meho_backplane.operations.service_grants import (
    caller_address_grant_reason,
    delete_grant_reason,
    descriptor_credential_reason,
    never_covered_reason,
)
from meho_backplane.policy.store import connector_id_of
from meho_backplane.settings import get_settings
from tests.approval_table import register_all_descriptors

# The same settings as the approval table: the built-in defaults, with the
# policy operations registered.
from tests.test_approval_table_snapshot import _settings_env  # noqa: F401

_TABLE_NAME = "policy/marked_operations.yaml"
_LEVELS = ("safe", "caution", "dangerous", "destructive")
#: The ids of the fixed rules of decision 5 in
#: docs/decisions/2026-11-25-custom-policy-model.md.
_FIXED_RULES = frozenset(
    {
        "credential_waits",
        "rights_change_waits",
        "policy_change_waits",
        "agent_never_approves",
        "destructive_waits",
        "delete_waits",
        "caller_address_waits",
    }
)
_LOSES = frozenset({"nothing", "deletes", "stops", "overwrites"})
_POLICY_CONNECTOR = "policy-registry-1.x"

Key = tuple[str, str]


def _load_table() -> list[dict[str, Any]]:
    text = (files("meho_backplane") / _TABLE_NAME).read_text(encoding="utf-8")
    rows = yaml.safe_load(text)["operations"]
    assert isinstance(rows, list)
    return rows


def _key(row: dict[str, Any]) -> Key:
    return row["op_id"], row["connector_id"]


def _group_of(descriptor: EndpointDescriptor) -> int | None:
    """The group of *descriptor*, or ``None`` when it is not a marked operation.

    Group 2 is the operations whose first reason that blocks a grant is the
    per-call mark. An operation with the per-call mark that also reads,
    writes or makes a credential is not in it: the credential rule, a fixed
    rule, keeps it waiting.
    """
    never = never_covered_reason(descriptor)
    if never is not None:
        return 2 if never[0] == "per_call_approval" else None
    if not descriptor.requires_approval:
        return None
    return {"caution": 1, "dangerous": 3}.get(descriptor.safety_level)


def _code_fixed_rules(descriptor: EndpointDescriptor, level: str) -> set[str]:
    """The fixed rules that cover *descriptor* in the code today, at *level*."""
    patterns = get_settings().service_grant_delete_shaped_patterns
    rules: set[str] = set()
    if descriptor_credential_reason(descriptor) is not None:
        rules.add("credential_waits")
    if is_rights_change_op(descriptor.op_id):
        rules.add("rights_change_waits")
        if connector_id_of(descriptor) == _POLICY_CONNECTOR:
            rules.add("policy_change_waits")
    if level == "destructive":
        rules.add("destructive_waits")
    if delete_grant_reason(descriptor.op_id, patterns, descriptor) is not None:
        rules.add("delete_waits")
    if caller_address_grant_reason(descriptor) is not None:
        rules.add("caller_address_waits")
    return rules


def _approvers(level: str, rights_change: bool) -> list[str]:
    """Which default roles may approve another person's request.

    An Operator approves only another person's ``safe`` or ``caution``
    change; a change of rights and a high-risk change need an Admin
    (docs/decisions/2026-11-18-four-default-roles.md).
    """
    if rights_change or level in ("dangerous", "destructive"):
        return ["Admin"]
    return ["Operator", "Admin"]


def _add_row_hint(descriptor: EndpointDescriptor, group: int) -> str:
    mark = "per_call" if group == 2 else "plain"
    applies = '"yes"' if group == 1 else "proposed"
    return (
        f"{descriptor.op_id} ({connector_id_of(descriptor)}) is a marked operation "
        f"(group {group}) but has no row in {_TABLE_NAME}. Read its code and its "
        "connector docs, then add a row like this one, and fill in does, "
        "level_with_switch, fixed_rules, approves_* and why (see the file's header):\n"
        f"  - op_id: {descriptor.op_id}\n"
        f"    connector_id: {connector_id_of(descriptor)}\n"
        f"    group: {group}\n"
        f"    level_today: {descriptor.safety_level}\n"
        f"    mark_today: {mark}\n"
        f"    applies: {applies}"
    )


def _row_problems(row: dict[str, Any]) -> list[str]:
    """What is wrong with *row* on its own, without the registered operations."""
    name = f"{row.get('op_id')} ({row.get('connector_id')})"
    problems: list[str] = []
    group = row.get("group")
    level = row.get("level_with_switch")
    if group not in (1, 2, 3):
        return [f"{name}: group must be 1, 2 or 3, not {group!r}"]
    if level not in _LEVELS:
        return [f"{name}: level_with_switch {level!r} is not a risk level {_LEVELS}"]
    if not str(row.get("does") or "").strip():
        problems.append(f"{name}: does is empty")
    if _LEVELS.index(level) < _LEVELS.index(row["level_today"]):
        problems.append(f"{name}: level_with_switch {level} is below level_today")
    why = str(row.get("why") or "").strip()
    if (group != 1 or level != "dangerous") and not why:
        problems.append(f"{name}: why is empty")
    rules = row.get("fixed_rules")
    if not isinstance(rules, list) or not set(rules) <= _FIXED_RULES:
        problems.append(
            f"{name}: fixed_rules {rules!r} names a rule outside {sorted(_FIXED_RULES)}"
        )
    elif level in ("safe", "caution") and not rules:
        problems.append(
            f"{name}: at {level} it would wait less than today; "
            "name the fixed rule that keeps it waiting"
        )
    expected_applies = "yes" if group == 1 else "proposed"
    if row.get("applies") != expected_applies:
        problems.append(f"{name}: applies must be {expected_applies!r} in group {group}")
    if group == 3:
        loses = row.get("loses")
        if loses not in _LOSES:
            problems.append(f"{name}: loses must be one of {sorted(_LOSES)}")
        elif (
            loses != "nothing"
            and level != "destructive"
            and row.get("delete_sign_proposed") is not True
        ):
            problems.append(f"{name}: it {loses}; propose destructive or a delete sign")
    return problems


def test_every_row_is_well_formed() -> None:
    rows = _load_table()
    problems = [problem for row in rows for problem in _row_problems(row)]
    keys = [_key(row) for row in rows]
    problems += [
        f"two rows for {key[0]} ({key[1]})" for key in sorted(set(keys)) if keys.count(key) > 1
    ]
    assert not problems, "\n".join(problems)


def test_a_row_problem_is_named() -> None:
    row = {
        "op_id": "demo.thing.set",
        "connector_id": "demo-1.x",
        "group": 1,
        "level_today": "caution",
        "level_with_switch": "caution",
        "does": "Sets a thing.",
        "applies": "yes",
        "fixed_rules": [],
    }
    problems = _row_problems(row)
    assert "demo.thing.set (demo-1.x): why is empty" in problems
    assert any("would wait less than today" in problem for problem in problems)
    assert _row_problems({**row, "level_with_switch": "low"}) == [
        f"demo.thing.set (demo-1.x): level_with_switch 'low' is not a risk level {_LEVELS}"
    ]
    assert any("no_such_rule" in p for p in _row_problems({**row, "fixed_rules": ["no_such_rule"]}))
    lowered = {**row, "level_today": "dangerous", "group": 3, "applies": "proposed", "why": "x"}
    assert any("below level_today" in p for p in _row_problems({**lowered, "loses": "nothing"}))
    deletes = {**lowered, "level_with_switch": "dangerous", "loses": "deletes"}
    assert _row_problems(deletes) == [
        "demo.thing.set (demo-1.x): it deletes; propose destructive or a delete sign"
    ]
    assert _row_problems({**deletes, "delete_sign_proposed": True}) == []


def test_missing_row_text_says_how_to_add_it() -> None:
    descriptor = EndpointDescriptor(
        op_id="demo.thing.set", impl_id="demo-1.x", safety_level="caution", requires_approval=True
    )
    hint = _add_row_hint(descriptor, 1)
    assert "has no row in policy/marked_operations.yaml" in hint
    assert "  - op_id: demo.thing.set\n" in hint
    assert 'applies: "yes"' in hint


def _table_problems(
    marked: dict[Key, tuple[EndpointDescriptor, int]], rows: dict[Key, dict[str, Any]]
) -> list[str]:
    """Where the table and the registered marked operations differ."""
    problems = [_add_row_hint(*marked[key]) for key in sorted(marked.keys() - rows.keys())]
    problems += [
        f"{key[0]} ({key[1]}) has a row in {_TABLE_NAME} but is not a marked operation; "
        "remove the row"
        for key in sorted(rows.keys() - marked.keys())
    ]
    for key in sorted(marked.keys() & rows.keys()):
        descriptor, group = marked[key]
        row = rows[key]
        name = f"{key[0]} ({key[1]})"
        rights_change = is_rights_change_op(descriptor.op_id)
        expected = {
            "group": group,
            "level_today": descriptor.safety_level,
            "mark_today": "per_call" if group == 2 else "plain",
            "approves_today": _approvers(descriptor.safety_level, rights_change),
            "approves_with_switch": _approvers(row["level_with_switch"], rights_change),
        }
        problems += [
            f"{name}: {field} is {row.get(field)!r}, the code says {value!r}"
            for field, value in expected.items()
            if row.get(field) != value
        ]
        code_rules = _code_fixed_rules(descriptor, row["level_with_switch"])
        if set(row["fixed_rules"]) != code_rules:
            problems.append(
                f"{name}: fixed_rules {row['fixed_rules']} differ from the code: "
                f"{sorted(code_rules)}"
            )
    return problems


@pytest.mark.asyncio
async def test_the_table_matches_the_registered_operations() -> None:
    descriptors = [d for d in await register_all_descriptors() if d.tenant_id is None]
    marked = {
        (d.op_id, connector_id_of(d)): (d, group)
        for d in descriptors
        if (group := _group_of(d)) is not None
    }
    rows = {_key(row): row for row in _load_table()}
    assert not (problems := _table_problems(marked, rows)), "\n\n".join(problems)

    # A new marked operation without a row fails, with the text that says how to add it.
    dropped = next(iter(rows))
    fewer = {key: row for key, row in rows.items() if key != dropped}
    assert _table_problems(marked, fewer) == [_add_row_hint(*marked[dropped])]
    # A row whose fixed rule the code does not apply fails too.
    wrong = {**rows, dropped: {**rows[dropped], "fixed_rules": ["destructive_waits"]}}
    assert any("differ from the code" in problem for problem in _table_problems(marked, wrong))
