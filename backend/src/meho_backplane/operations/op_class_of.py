# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The one place that gives the class of an operation (#6517).

An operation has a class (``read``, ``write``, ``credential_read``,
``credential_write``, ``credential_mint`` and a few more). Other rules read
it: what an audit row says, what an approval card shows, which secrets are
hidden, which notes a call gets.

The class comes from two sources, in this order:

1. The row of the operation (``EndpointDescriptor``). A connector's safety
   floor can store a class on an ingested row, and a hand-written operation
   states its risk level there.
2. The op id, through :func:`~meho_backplane.broadcast.events.classify_op`.

Every code place that needs the class calls a function here. A test
(``tests/test_op_class_readers.py``) fails when new code calls ``classify_op``
on an op id alone, in a place that could have the row.

A stored class never replaces a credential or audit class that the op id
already gives (:func:`~meho_backplane.broadcast.events.resolve_op_class`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from meho_backplane.broadcast.events import resolve_op_class, resolve_operation_op_class
from meho_backplane.broadcast.history import WRITE_OP_CLASSES

if TYPE_CHECKING:
    from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "SECRET_CLASSES",
    "descriptor_op_class",
    "lookup_secret_op_class",
    "request_op_class",
    "write_note_op_class",
]

#: The classes whose parameters or answer can hold a secret value.
SECRET_CLASSES: Final[frozenset[str]] = frozenset(
    {"credential_read", "credential_mint", "credential_write"}
)


def descriptor_op_class(descriptor: EndpointDescriptor) -> str:
    """Return the op class of one run of *descriptor*.

    The audit row stores this class. The approval request stores the same
    value, and the dispatcher hands it to the announce gate, the announce
    reminder and the target activity note. See
    :func:`~meho_backplane.broadcast.events.resolve_operation_op_class`.
    A descriptor stand-in without a ``source_kind`` or a ``safety_level``
    gets the class from its op id alone (and its stored class).
    """
    return resolve_operation_op_class(
        descriptor.op_id,
        source_kind=getattr(descriptor, "source_kind", "ingested"),
        safety_level=getattr(descriptor, "safety_level", None),
        stored_op_class=getattr(descriptor, "op_class", None),
    )


def write_note_op_class(descriptor: EndpointDescriptor) -> str:
    """Return the class that decides whether a call gets the write notes.

    The write notes are the target activity note, the announce reminder and
    the announce gate. They apply to a class in ``WRITE_OP_CLASSES``. The
    class of the run (:func:`descriptor_op_class`) is the same, except for
    one case: a stored ``credential_read`` on an operation that also
    changes something (a ``POST``, ``PUT`` or ``DELETE``). The run row then
    says ``credential_read``, which is not a write class. Such an
    operation keeps the write notes: this returns the class it has without
    the stored one.
    """
    run_class = descriptor_op_class(descriptor)
    if run_class in WRITE_OP_CLASSES or getattr(descriptor, "op_class", None) is None:
        return run_class
    without_stored = resolve_operation_op_class(
        descriptor.op_id,
        source_kind=getattr(descriptor, "source_kind", "ingested"),
        safety_level=getattr(descriptor, "safety_level", None),
    )
    return without_stored if without_stored in WRITE_OP_CLASSES else run_class


def request_op_class(op_id: str, proposed_effect: Mapping[str, Any] | None) -> str:
    """Return the op class of an approval request.

    The request has no row. It carries the class that
    :func:`descriptor_op_class` gave when it was made, in
    ``proposed_effect["op_class"]``. A request made before that field
    existed has none: the op id gives the class. A credential or audit
    class of the op id always wins, so a changed field cannot make a secret
    operation look plain.
    """
    stored = proposed_effect.get("op_class") if isinstance(proposed_effect, Mapping) else None
    return resolve_op_class(op_id, stored if isinstance(stored, str) and stored else None)


async def lookup_secret_op_class(tenant_id: UUID, connector_id: str, op_id: str) -> str | None:
    """Return the secret class of the stored row of an operation, else ``None``.

    For a caller that has only the names of the operation and must still hide
    a secret: the outer MCP row of ``call_operation`` decides what the
    broadcast shows from the operation inside. The class is read from the
    row the lookup finds for *tenant_id*. ``None`` when there is no such row
    or its class is not a secret class: the op id then gives the class.
    """
    from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id

    product, version, impl_id = parse_connector_id(connector_id)
    descriptor = await lookup_descriptor(
        tenant_id=tenant_id, product=product, version=version, impl_id=impl_id, op_id=op_id
    )
    if descriptor is None:
        return None
    op_class = descriptor_op_class(descriptor)
    return op_class if op_class in SECRET_CLASSES else None
