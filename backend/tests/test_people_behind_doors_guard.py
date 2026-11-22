# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every place that decides an approval uses the people-behind check.

The check that a request of an agent, service or runner login counts as a
request of the people behind it lives in a few shared functions. This guard
lists every door that decides or answers "may I approve?":

* :func:`~meho_backplane.operations.approval_queue.approve_request` (REST
  approve and decide, the CLI, the web console, approve in order);
* :func:`~meho_backplane.operations.approval_rights.approve_right` and
  :func:`~meho_backplane.operations.approval_rights.approve_right_any_status`
  (the button, the lists, the waiting count, web push);
* :meth:`~meho_backplane.operations.run_approvals.RunApprovalService.approve`;
* :func:`~meho_backplane.policy.approve.may_approve` (the policy evaluator).

It fails when a new function writes an approval without being on the list,
or when a listed door stops calling the shared check.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import meho_backplane
from meho_backplane.operations import approval_queue, approval_rights, run_approvals
from meho_backplane.policy import approve as policy_approve

_SRC = Path(meho_backplane.__file__).parent

#: The functions that write an approval decision, by module and name.
_KNOWN_WRITERS = {
    ("operations/approval_queue.py", "approve_request"),
    ("operations/run_approvals.py", "approve"),
}


def _writes_an_approval(node: ast.AST) -> bool:
    """True when *node* sets ``status`` to APPROVED or sets ``approved_by_sub``."""
    for child in ast.walk(node):
        if not isinstance(child, ast.Assign):
            continue
        for target in child.targets:
            if not isinstance(target, ast.Attribute):
                continue
            if target.attr == "approved_by_sub":
                return True
            if target.attr == "status" and "APPROVED" in ast.unparse(child.value):
                return True
    return False


def test_no_new_function_writes_an_approval_without_the_check() -> None:
    found: set[tuple[str, str]] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _writes_an_approval(
                node
            ):
                found.add((path.relative_to(_SRC).as_posix(), node.name))

    assert found == _KNOWN_WRITERS, (
        "A function that writes an approval decision must use the people-behind check "
        "(_check_approver_group, or _check_people_behind for run approvals). Add it to "
        f"_KNOWN_WRITERS and to the call checks below. Found: {sorted(found)}"
    )


def _calls(function: object) -> set[str]:
    tree = ast.parse(inspect.getsource(function).lstrip())  # type: ignore[arg-type]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            names.add(func.attr if isinstance(func, ast.Attribute) else ast.unparse(func))
    return names


def test_the_single_request_doors_reach_the_shared_check() -> None:
    assert "_load_pending_for_approval" in _calls(approval_queue.approve_request)
    assert "_check_approver_group" in _calls(approval_queue._load_pending_for_approval)
    assert "_right" in _calls(approval_rights.approve_right)
    assert "_right" in _calls(approval_rights.approve_right_any_status)
    assert "_check_approver_group" in _calls(approval_rights._right)
    shared = _calls(approval_queue._check_approver_group)
    assert {"check_rights_change_approval", "_check_requester_known", "_check_self_approval"} <= (
        shared
    )
    assert "is_person_behind" in _calls(approval_queue._check_self_approval)
    assert "is_self_approval" in shared


def test_the_run_approval_door_reaches_the_check() -> None:
    calls = _calls(run_approvals.RunApprovalService.approve)
    assert {"_check_people_behind", "_check_approver_group"} <= calls


def test_the_policy_evaluator_knows_the_people_behind() -> None:
    source = inspect.getsource(policy_approve._group_rule)
    assert "people_behind" in source
    assert "requester_unknown" in source
    assert "_is_own" in _calls(policy_approve._group_rule)
