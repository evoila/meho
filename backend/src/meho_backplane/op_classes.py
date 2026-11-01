# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Op classes a connector declares for its own operations.

The broadcast classifier (:func:`meho_backplane.broadcast.events.classify_op`)
decides how much of a call MEHO shows on the feed, in the approval queue,
in the preview, in the audit row and in the flight recorder. Most
operations get their class from a pattern rule on the op id. The
operations that need an exact class, for example a write whose request
carries a secret, declare it here.

A connector declares the classes in one module of its own package,
``connectors/<name>/op_classes.py``, and imports that module from its
``__init__.py``. So the classes are known as soon as the connector
package is imported, which the server does for every connector at
start. No core file lists another connector's op ids.

This module imports nothing from MEHO, so a connector and the classifier
can both import it without a cycle.

Example::

    from meho_backplane.op_classes import declare_op_classes

    declare_op_classes(
        credential_write=("example.user.set_password",),
        write=("example.cache.flush",),
    )
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

__all__ = [
    "DECLARABLE_OP_CLASSES",
    "declare_op_classes",
    "declared_op_class",
    "declared_op_ids",
]

#: The classes a connector may declare. ``credential_read``: the answer
#: holds a secret. ``credential_mint``: the answer holds a new secret.
#: ``credential_write``: the request holds a secret, or the operation sends
#: one to the target. ``write``: the operation changes the target, but its
#: op id does not end in a write word the pattern rules know.
DECLARABLE_OP_CLASSES: Final[frozenset[str]] = frozenset(
    {"credential_read", "credential_mint", "credential_write", "write"}
)

_DECLARED: dict[str, str] = {}


def declare_op_classes(
    *,
    credential_read: Iterable[str] = (),
    credential_mint: Iterable[str] = (),
    credential_write: Iterable[str] = (),
    write: Iterable[str] = (),
) -> None:
    """Declare the class of the given op ids.

    Safe to call twice with the same ids and classes, so a re-import of
    the connector module changes nothing. Raises :exc:`ValueError` when an
    op id already has a different class: two declarations for one op id
    must not disagree.
    """
    for op_class, op_ids in (
        ("credential_read", credential_read),
        ("credential_mint", credential_mint),
        ("credential_write", credential_write),
        ("write", write),
    ):
        for op_id in op_ids:
            known = _DECLARED.get(op_id)
            if known is not None and known != op_class:
                raise ValueError(
                    f"op id {op_id!r} is already declared as {known!r}; "
                    f"cannot redeclare it as {op_class!r}"
                )
            _DECLARED[op_id] = op_class


def declared_op_class(op_id: str) -> str | None:
    """Return the declared class of *op_id*, or ``None`` when none was declared."""
    return _DECLARED.get(op_id)


def declared_op_ids(op_class: str) -> frozenset[str]:
    """Return every op id declared with *op_class*."""
    return frozenset(op_id for op_id, known in _DECLARED.items() if known == op_class)


def _reset_declared_op_classes_for_testing() -> None:
    """Forget every declaration. Test-only."""
    _DECLARED.clear()
