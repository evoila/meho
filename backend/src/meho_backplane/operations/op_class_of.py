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
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from meho_backplane.broadcast.events import resolve_op_class, resolve_operation_op_class
from meho_backplane.broadcast.history import WRITE_OP_CLASSES

if TYPE_CHECKING:
    from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "SECRET_CLASSES",
    "RanOpClass",
    "descriptor_op_class",
    "lookup_secret_op_class",
    "lookup_secret_op_class_any_version",
    "note_ran_op_class",
    "ran_op_class_var",
    "request_op_class",
    "request_write_note_op_class",
    "write_note_op_class",
]

#: The classes whose parameters or answer can hold a secret value.
SECRET_CLASSES: Final[frozenset[str]] = frozenset(
    {"credential_read", "credential_mint", "credential_write"}
)


@dataclass
class RanOpClass:
    """Receives the class of the operation that one call ran.

    The dispatcher can run another row than the one the caller named: it
    picks the catalog or version that serves the target. The outer MCP row
    of ``call_operation`` sets this holder, and the dispatcher fills it with
    the class of the row it picked (:func:`note_ran_op_class`).
    """

    op_class: str | None = None


#: The holder of the open MCP call, or ``None`` when no caller watches.
ran_op_class_var: ContextVar[RanOpClass | None] = ContextVar("ran_op_class", default=None)


def note_ran_op_class(op_class: str) -> None:
    """Store *op_class* in the open holder, when there is one.

    The first class wins: it is the operation the caller asked for. The
    steps of a multi-step operation run after it and change nothing.
    """
    holder = ran_op_class_var.get()
    if holder is not None and holder.op_class is None:
        holder.op_class = op_class


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


def request_write_note_op_class(op_id: str, proposed_effect: Mapping[str, Any] | None) -> str:
    """Return the class that decides whether an approval request changes something.

    The request's own version of :func:`write_note_op_class`. The class of
    the request (:func:`request_op_class`) is the class of the run, so a
    stored ``credential_read`` on an operation that also changes something
    (a ``POST``, ``PUT`` or ``DELETE``) says ``credential_read``. Such a
    request still changes something: this returns the class its op id
    gives. Only an ingested operation can store a class, and its op id
    names its HTTP method.
    """
    request_class = request_op_class(op_id, proposed_effect)
    if request_class in WRITE_OP_CLASSES:
        return request_class
    without_stored = resolve_operation_op_class(op_id, source_kind="ingested", safety_level=None)
    return without_stored if without_stored in WRITE_OP_CLASSES else request_class


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


async def lookup_secret_op_class_any_version(
    tenant_id: UUID, connector_id: str, op_id: str
) -> str | None:
    """Return a secret class stored on any version of an operation, else ``None``.

    For a caller that cannot see which row the dispatcher picks: a call that
    starts in the background, or a check of such a call. The dispatcher can
    run the same op id in another version of the named catalog (same product
    and implementation, :mod:`~meho_backplane.operations._catalog_guard`).
    So this reads the rows of every version that *tenant_id* can see. When
    one of them stores a secret class, that class is returned.
    """
    from sqlalchemy import select

    from meho_backplane.db.engine import get_sessionmaker
    from meho_backplane.db.models import EndpointDescriptor
    from meho_backplane.operations._lookup import parse_connector_id, visible_scope_clause

    product, _version, impl_id = parse_connector_id(connector_id)
    async with get_sessionmaker()() as session:
        stored = (
            await session.execute(
                select(EndpointDescriptor.op_class)
                .where(
                    visible_scope_clause(
                        EndpointDescriptor,
                        tenant_id=tenant_id,
                        product=product,
                        version=EndpointDescriptor.version,
                        impl_id=impl_id,
                    ),
                    EndpointDescriptor.product == product,
                    EndpointDescriptor.impl_id == impl_id,
                    EndpointDescriptor.op_id == op_id,
                    EndpointDescriptor.op_class.in_(sorted(SECRET_CLASSES)),
                )
                .order_by(EndpointDescriptor.op_class)
                .limit(1)
            )
        ).scalar_one_or_none()
    return stored
