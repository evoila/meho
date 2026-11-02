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
from meho_backplane.connectors.registry import _eager_import_connectors
from meho_backplane.op_classes import declare_op_classes, declared_op_class, declared_op_ids

#: The connectors the backplane itself imports, or MEHO's own planes. Only
#: their op ids may stay in ``broadcast/events.py``
#: (``docs/decisions/2026-10-28-own-connectors-leave-the-public-tree.md``).
_CORE_OP_PREFIXES = ("vault.", "keycloak.", "secret.", "targets.", "topology.", "checks.")


@pytest.fixture
def fresh_registry() -> Iterator[None]:
    """Empty the registry for one test and put the real declarations back after."""
    saved = dict(op_classes._DECLARED)
    op_classes._reset_declared_op_classes_for_testing()
    try:
        yield
    finally:
        op_classes._reset_declared_op_classes_for_testing()
        op_classes._DECLARED.update(saved)


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
    assert declared_op_class("GET:/v1/credentials") == "credential_read"
    assert declared_op_class("argocd.app.sync") == "write"
    assert classify_op("k8s.secret.create") == "credential_write"
