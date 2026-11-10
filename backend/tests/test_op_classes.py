# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector declares the class of its own operations (#5579).

Checks the small registry in :mod:`meho_backplane.op_classes`, that the
broadcast classifier reads it first, and that the lists left in
``broadcast/events.py`` name only operations of the core connectors.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from meho_backplane import op_classes
from meho_backplane.broadcast import events
from meho_backplane.broadcast.events import classify_op
from meho_backplane.connectors.github.op_classes import TICKET_WRITE_OP_IDS
from meho_backplane.connectors.registry import _eager_import_connectors
from meho_backplane.op_classes import (
    declare_op_classes,
    declare_root_shell,
    declare_safe_writes,
    declared_op_class,
    declared_op_ids,
)

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
    op_classes._reset_declared_op_classes_for_testing()
    try:
        yield
    finally:
        op_classes._reset_declared_op_classes_for_testing()
        op_classes._DECLARED.update(saved)
        op_classes._SAFE_WRITES.update(saved_safe_writes)
        op_classes._ROOT_SHELL.update(saved_root_shell)


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
