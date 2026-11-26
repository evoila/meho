# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector declares the class of its own operations (#5579).

Checks the small registry in :mod:`meho_backplane.op_classes`, that the
broadcast classifier reads it first, and that the lists left in
``broadcast/events.py`` name only operations of the core connectors.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args

import pytest

from meho_backplane import op_classes
from meho_backplane.api.v1.broadcast import BroadcastAnnounceRequest
from meho_backplane.broadcast import events
from meho_backplane.broadcast.agent_events import PLANNED_OP_CLASS_VALUES
from meho_backplane.broadcast.events import classify_op
from meho_backplane.broadcast.history import OP_CLASS_ENUM
from meho_backplane.connectors.github.op_classes import TICKET_WRITE_OP_IDS
from meho_backplane.connectors.registry import _eager_import_connectors
from meho_backplane.op_classes import (
    DECLARABLE_OP_CLASSES,
    declare_op_classes,
    declare_per_call_approval,
    declare_root_shell,
    declare_safe_writes,
    declared_op_class,
    declared_op_ids,
)
from meho_backplane.ui.routes.audit.op_class_badges import OP_CLASS_BADGE_CLASSES

#: The connectors the backplane itself imports, or MEHO's own planes. Only
#: their op ids may stay in ``broadcast/events.py``
#: (``docs/decisions/2026-10-28-own-connectors-leave-the-public-tree.md``).
_CORE_OP_PREFIXES = ("vault.", "keycloak.", "secret.", "targets.", "topology.", "checks.")


@pytest.fixture
def fresh_registry() -> Iterator[None]:
    """Empty the registry for one test and put the real declarations back after."""
    saved = dict(op_classes._DECLARED)
    saved_safe_writes = set(op_classes._SAFE_WRITES)
    saved_root_shell = set(op_classes._ROOT_SHELL)
    saved_per_call = set(op_classes._PER_CALL_APPROVAL)
    saved_no_secret_value = set(op_classes._NO_SECRET_VALUE)
    saved_wait_when_set = dict(op_classes._WAIT_WHEN_SET)
    saved_credential_capable = set(op_classes._CREDENTIAL_CAPABLE)
    saved_capable_matches = list(op_classes._CREDENTIAL_CAPABLE_MATCHES)
    saved_credential_checks = dict(op_classes._CREDENTIAL_CHECKS)
    saved_hidden_stored_params = dict(op_classes._HIDDEN_STORED_PARAMS)
    op_classes._reset_declared_op_classes_for_testing()
    try:
        yield
    finally:
        op_classes._reset_declared_op_classes_for_testing()
        op_classes._DECLARED.update(saved)
        op_classes._SAFE_WRITES.update(saved_safe_writes)
        op_classes._ROOT_SHELL.update(saved_root_shell)
        op_classes._PER_CALL_APPROVAL.update(saved_per_call)
        op_classes._NO_SECRET_VALUE.update(saved_no_secret_value)
        op_classes._WAIT_WHEN_SET.update(saved_wait_when_set)
        op_classes._CREDENTIAL_CAPABLE.update(saved_credential_capable)
        op_classes._CREDENTIAL_CAPABLE_MATCHES.extend(saved_capable_matches)
        op_classes._CREDENTIAL_CHECKS.update(saved_credential_checks)
        op_classes._HIDDEN_STORED_PARAMS.update(saved_hidden_stored_params)


def test_declared_class_wins_over_the_pattern_rules(fresh_registry: None) -> None:
    op_id = "example.cache.flush"
    assert classify_op(op_id) == "other"
    declare_op_classes(write=(op_id,))
    assert classify_op(op_id) == "write"
    assert declared_op_class(op_id) == "write"
    assert op_id in declared_op_ids("write")


def test_declared_credential_class_beats_a_write_suffix(fresh_registry: None) -> None:
    op_id = "example.user.create"
    assert classify_op(op_id) == "write"
    declare_op_classes(credential_write=(op_id,))
    assert classify_op(op_id) == "credential_write"


def test_declaring_the_same_class_twice_is_fine(fresh_registry: None) -> None:
    declare_op_classes(credential_mint=("example.token.create",))
    declare_op_classes(credential_mint=("example.token.create",))
    assert declared_op_class("example.token.create") == "credential_mint"


def test_declaring_a_different_class_for_the_same_op_fails(fresh_registry: None) -> None:
    declare_op_classes(credential_read=("example.secret.show",))
    with pytest.raises(ValueError, match="already declared"):
        declare_op_classes(write=("example.secret.show",))
    assert declared_op_class("example.secret.show") == "credential_read"


def test_undeclared_op_has_no_declared_class(fresh_registry: None) -> None:
    assert declared_op_class("example.nothing.here") is None
    assert declared_op_ids("write") == frozenset()


def test_core_lists_name_only_core_connector_ops() -> None:
    stray = sorted(
        op_id
        for pins in (
            events._CREDENTIAL_READ_OPS,
            events._CREDENTIAL_MINT_OPS,
            events._CREDENTIAL_WRITE_OPS,
            events._WRITE_OPS,
            events._CHECK_EVENT_OPS,
        )
        for op_id in pins
        if not op_id.startswith(_CORE_OP_PREFIXES)
    )
    assert not stray, (
        "broadcast/events.py may pin only core connector operations; declare these in the "
        f"connector's own op_classes.py instead: {stray}"
    )


def test_connectors_declare_their_sensitive_ops_at_import() -> None:
    _eager_import_connectors()
    assert declared_op_class("k8s.secret.create") == "credential_write"
    assert declared_op_class("harbor.robot.create") == "credential_mint"
    assert declared_op_class("bind9.config.show") == "credential_read"
    assert declared_op_class("argocd.app.sync") == "write"
    assert classify_op("k8s.secret.create") == "credential_write"


def test_only_declared_safe_writes_count_as_safe_writes(fresh_registry: None) -> None:
    # A declared class alone does not make a safe write: only declare_safe_writes does.
    declare_op_classes(write=("example.cache.flush", "example.ticket.create"))
    declare_safe_writes(("example.ticket.create",))
    declare_safe_writes(("example.ticket.create",))
    assert op_classes.is_safe_write("example.ticket.create")
    assert not op_classes.is_safe_write("example.cache.flush")
    assert not op_classes.is_safe_write("example.never.declared")


def test_the_only_safe_writes_are_the_github_ticket_writes() -> None:
    # Ops of other connectors declared as a change (for example the Kubernetes
    # secret create) stay reads for health checks and satellite runners.
    _eager_import_connectors()
    assert set(TICKET_WRITE_OP_IDS) == op_classes._SAFE_WRITES
    assert declared_op_class("k8s.secret.create") == "credential_write"
    assert not op_classes.is_safe_write("k8s.secret.create")


def test_root_shell_mark_is_separate_from_the_op_class(fresh_registry: None) -> None:
    # #6092 -- one op id has one class, and it can carry the root mark too.
    declare_op_classes(credential_write=("example.script.run",))
    declare_root_shell(("example.script.run",))
    declare_root_shell(("example.script.run",))
    assert op_classes.is_root_shell("example.script.run")
    assert declared_op_class("example.script.run") == "credential_write"
    assert not op_classes.is_root_shell("example.script.list")


def test_per_call_approval_mark_is_separate_from_the_op_class(fresh_registry: None) -> None:
    declare_op_classes(write=("example.group.add",))
    declare_per_call_approval(("example.group.add",))
    declare_per_call_approval(("example.group.add",))
    assert op_classes.needs_per_call_approval("example.group.add")
    assert declared_op_class("example.group.add") == "write"
    assert not op_classes.needs_per_call_approval("example.group.list")


def test_msad_marks_its_member_writes_at_import() -> None:
    _eager_import_connectors()
    assert op_classes.needs_per_call_approval("msad.group.add-member")
    assert op_classes.needs_per_call_approval("msad.group.remove-member")
    assert not op_classes.needs_per_call_approval("msad.group.create")


def test_ssh_connectors_mark_their_root_writes_at_import() -> None:
    # #6092 -- every write that runs a command as root on the host is marked.
    _eager_import_connectors()
    for op_id in (
        "linux.file.write",
        "linux.service.control",
        "linux.script.run",
        "linux.sysctl.write",
        "linux.firewall.load",
        "linux.firewall.confirm",
        "bind9.record.add",
        "bind9.record.remove",
        "bind9.record.delete",
        "bind9.config.apply_file",
        "bind9.config.apply_views",
        "bind9.config.backup",
        "bind9.config.reload",
        "rke2.token.rotate",
        "rke2.node.service.restart",
        "rke2.node.config.update",
        "rke2.etcd-snapshot.save",
        "pfsense.gateway.add",
        "pfsense.route.static.add",
        "pfsense.filter.rule.add",
        "pfsense.filter.rule.delete",
        "pfsense.user.enable",
        "pfsense.user.disable",
        "pfsense.gateway.delete",
        "pfsense.route.static.delete",
        "pfsense.alias.delete",
        "pfsense.alias.member.remove",
        "pfsense.nat.delete",
    ):
        assert op_classes.is_root_shell(op_id), op_id
    # A read with a fixed command never waits for approval, so it has no mark.
    assert not op_classes.is_root_shell("linux.firewall.show")
    assert not op_classes.is_root_shell("linux.file.read")
    assert not op_classes.is_root_shell("pfsense.config.show")
    assert not op_classes.is_root_shell("pfsense.diag.ping")


def test_no_secret_value_mark(fresh_registry: None) -> None:
    assert not op_classes.returns_no_secret_value("example.secret.names")

    op_classes.declare_no_secret_value(("example.secret.names",))
    op_classes.declare_no_secret_value(("example.secret.names",))

    assert op_classes.returns_no_secret_value("example.secret.names")
    assert not op_classes.returns_no_secret_value("example.secret.read")


# ---------------------------------------------------------------------------
# The credential class sets live in op_classes.py only
# ---------------------------------------------------------------------------

_CREDENTIAL_CLASS_NAMES = frozenset(
    {"credential_read", "credential_write", "credential_mint", "credential_use"}
)
_SOURCE_ROOT = Path(op_classes.__file__).parent

#: The only places outside ``op_classes.py`` that may write two or more
#: credential class names in one set, tuple, list or dict, each with why.
_ALLOWED_CLASS_LISTS: dict[tuple[str, str], str] = {
    ("broadcast/history.py", "OP_CLASS_ENUM"): (
        "the closed list of every class, for the op_class filter of the audit query"
    ),
    ("broadcast/agent_events.py", "PlannedOpClass"): (
        "the closed list of classes an agent may name in an announcement"
    ),
    ("api/v1/broadcast.py", "BroadcastAnnounceRequest.planned_op_class"): (
        "the same closed list, as the type of the REST announce field"
    ),
    ("ui/routes/audit/op_class_badges.py", "OP_CLASS_BADGE_CLASSES"): (
        "one badge colour for every class, so a closed list of every class"
    ),
    ("broadcast/history.py", "WRITE_OP_CLASSES"): (
        "the classes that count as a write; credential_use is not one of them"
    ),
    ("reflex/adoption.py", "WRITE_OP_CLASSES"): (
        "the classes the adoption count treats as a write; credential_use is not one"
    ),
    ("ui/routes/broadcast/aggregate_gate.py", "AGGREGATE_ONLY_OP_CLASSES"): (
        "the audit drawer's own list for rows that carry no recorded detail"
    ),
}


def _string_values(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Dict):
        items: list[ast.expr | None] = list(node.keys)
    elif isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        items = list(node.elts)
    else:
        return set()
    return {i.value for i in items if isinstance(i, ast.Constant) and isinstance(i.value, str)}


def _assigned_name(stmt: ast.stmt, owner: str) -> str | None:
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        name = stmt.target.id
    elif (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
    ):
        name = stmt.targets[0].id
    else:
        return None
    return f"{owner}.{name}" if owner else name


def _class_lists(tree: ast.Module) -> list[tuple[str, int]]:
    """Each literal with two or more credential class names: (owner name, line)."""
    found: list[tuple[str, int]] = []
    named: set[int] = set()

    def visit(body: list[ast.stmt], owner: str) -> None:
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                visit(stmt.body, stmt.name)
                continue
            name = _assigned_name(stmt, owner)
            if name is None:
                continue
            for node in ast.walk(stmt):
                if len(_string_values(node) & _CREDENTIAL_CLASS_NAMES) >= 2:
                    found.append((name, node.lineno))
                    named.add(id(node))

    visit(tree.body, "")
    for node in ast.walk(tree):
        if id(node) not in named and len(_string_values(node) & _CREDENTIAL_CLASS_NAMES) >= 2:
            found.append((f"<line {node.lineno}>", node.lineno))
    return found


def test_only_op_classes_py_writes_a_set_of_credential_classes() -> None:
    """Every other module reads a named set from ``op_classes.py``.

    A new class is then added in one place, and no step that hides a
    secret misses it.
    """
    offenders: list[str] = []
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(_SOURCE_ROOT).as_posix()
        if relative == "op_classes.py":
            continue
        for name, line in _class_lists(ast.parse(path.read_text(encoding="utf-8"))):
            if (relative, name) not in _ALLOWED_CLASS_LISTS:
                offenders.append(f"{relative}:{line} ({name})")
    assert offenders == [], (
        "use CREDENTIAL_OP_CLASSES, CREDENTIAL_CHANGE_OP_CLASSES or "
        f"SECRET_REQUEST_OP_CLASSES from op_classes.py: {offenders}"
    )


def test_the_scan_finds_a_new_class_list() -> None:
    """The scan above sees a set written in a function and one in a class."""
    tree = ast.parse(
        "def f(c):\n"
        "    return c in {'credential_read', 'credential_use'}\n"
        "class K:\n"
        "    SECRET = ('credential_write', 'credential_mint')\n"
    )

    assert _class_lists(tree) == [("K.SECRET", 4), ("<line 2>", 2)]


def _literal_values(annotation: Any) -> set[str]:
    values: set[str] = set()
    for arg in get_args(annotation):
        values |= {arg} if isinstance(arg, str) else _literal_values(arg)
    return values


def test_the_closed_class_lists_hold_every_declarable_class() -> None:
    announce_field = BroadcastAnnounceRequest.model_fields["planned_op_class"].annotation
    closed_lists = {
        "OP_CLASS_ENUM": set(OP_CLASS_ENUM),
        "PlannedOpClass": set(PLANNED_OP_CLASS_VALUES),
        "BroadcastAnnounceRequest.planned_op_class": _literal_values(announce_field),
        "OP_CLASS_BADGE_CLASSES": set(OP_CLASS_BADGE_CLASSES),
    }

    for name, values in closed_lists.items():
        assert values >= DECLARABLE_OP_CLASSES, (name, sorted(DECLARABLE_OP_CLASSES - values))
