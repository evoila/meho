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
    "declare_no_secret_value",
    "declare_op_classes",
    "declare_per_call_approval",
    "declare_root_shell",
    "declare_safe_writes",
    "declared_op_class",
    "declared_op_ids",
    "is_root_shell",
    "is_safe_write",
    "needs_per_call_approval",
    "returns_no_secret_value",
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
_SAFE_WRITES: set[str] = set()
_ROOT_SHELL: set[str] = set()
_PER_CALL_APPROVAL: set[str] = set()
_NO_SECRET_VALUE: set[str] = set()


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


def declare_safe_writes(op_ids: Iterable[str]) -> None:
    """Mark *op_ids* as ``safe`` writes: they need no approval, but they change things.

    Only a connector that registers such a write calls this, for example the
    GitHub ticket writes (``docs/decisions/2026-11-02-github-ticket-writes-are-safe.md``).
    A health check never runs a safe write, and a satellite runner never takes
    one as a read. Safe to call twice with the same ids.
    """
    _SAFE_WRITES.update(op_ids)


def is_safe_write(op_id: str) -> bool:
    """Return ``True`` when a connector marked *op_id* with :func:`declare_safe_writes`."""
    return op_id in _SAFE_WRITES


def declare_root_shell(op_ids: Iterable[str]) -> None:
    """Mark *op_ids* as operations that run a command as root on the host.

    A connector calls this for each operation that changes a host by running
    a command or script as root, for example over SSH with ``sudo``. A run
    approval never covers such an operation: a person approves each call
    (``docs/codebase/run-approvals.md``). The mark is separate from the op
    class, because an op id has only one class. Safe to call twice with the
    same ids.
    """
    _ROOT_SHELL.update(op_ids)


def is_root_shell(op_id: str) -> bool:
    """Return ``True`` when a connector marked *op_id* with :func:`declare_root_shell`."""
    return op_id in _ROOT_SHELL


def declare_per_call_approval(op_ids: Iterable[str]) -> None:
    """Mark *op_ids* as operations that a person approves call by call.

    Neither a standing grant nor a run approval covers such an operation:
    each call waits for its own approval. A connector calls this for an
    operation whose call gives rights, for example adding a member to a
    directory group. Safe to call twice with the same ids.
    """
    _PER_CALL_APPROVAL.update(op_ids)


def needs_per_call_approval(op_id: str) -> bool:
    """Return ``True`` when a connector marked *op_id* with :func:`declare_per_call_approval`."""
    return op_id in _PER_CALL_APPROVAL


def declare_no_secret_value(op_ids: Iterable[str]) -> None:
    """Mark *op_ids* as credential reads whose answer holds no secret value.

    A ``credential_read`` operation is at least ``caution`` and waits for a
    person's approval (:mod:`meho_backplane.operations.credential_read`).
    A read in that class whose answer holds no secret value is the
    exception: for example one that returns only key names, or only "same"
    or "different". Its connector marks it here, in code. A spec import or
    a tenant override cannot set the mark.

    Mark only a read whose answer holds no secret value also when the call
    asks for ``reveal_secret``. Safe to call twice with the same ids.
    """
    _NO_SECRET_VALUE.update(op_ids)


def returns_no_secret_value(op_id: str) -> bool:
    """Return ``True`` when a connector marked *op_id* with :func:`declare_no_secret_value`."""
    return op_id in _NO_SECRET_VALUE


def declared_op_ids(op_class: str) -> frozenset[str]:
    """Return every op id declared with *op_class*."""
    return frozenset(op_id for op_id, known in _DECLARED.items() if known == op_class)


def _reset_declared_op_classes_for_testing() -> None:
    """Forget every declaration. Test-only."""
    _DECLARED.clear()
    _SAFE_WRITES.clear()
    _ROOT_SHELL.clear()
    _PER_CALL_APPROVAL.clear()
    _NO_SECRET_VALUE.clear()
