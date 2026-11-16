# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Entries of a run approval that each wait for a person.

A run approval never covers an operation that reads a credential (the
``credential_read`` class). A request that lists one is not refused: the
answer of request, show and approve names the entry in
``entries_that_wait``, so the add-on that asks and the person who approves
both see that each call of it still waits for a person.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from meho_backplane.operations.run_approval_schemas import (
    RunApprovalEntry,
    RunApprovalRead,
    RunApprovalWaitNote,
)
from meho_backplane.operations.service_grants import credential_read_grant_reason

__all__ = ["wait_notes", "with_wait_notes"]


async def wait_notes(
    tenant_id: uuid.UUID, entries: Sequence[RunApprovalEntry]
) -> list[RunApprovalWaitNote]:
    """The entries of *entries* that a run approval never covers, but does not refuse.

    That is an operation that reads a credential: the same rule as the call
    gate (:func:`~meho_backplane.operations.run_approval_gate.consult_and_record_run_approval`),
    with the class stored on the operation's row when MEHO finds it.
    """
    from meho_backplane.operations._lookup import lookup_descriptor, parse_connector_id

    notes: list[RunApprovalWaitNote] = []
    for index, entry in enumerate(entries):
        product, version, impl_id = parse_connector_id(entry.connector_id)
        descriptor = await lookup_descriptor(
            tenant_id=tenant_id,
            product=product,
            version=version,
            impl_id=impl_id,
            op_id=entry.op_id,
        )
        stored_class = getattr(descriptor, "op_class", None) if descriptor is not None else None
        if credential_read_grant_reason(entry.op_id, stored_class) is None:
            continue
        notes.append(
            RunApprovalWaitNote(
                entry=index,
                connector_id=entry.connector_id,
                op_id=entry.op_id,
                reason=(
                    f"op {entry.op_id!r} reads a credential. A run approval never covers it: "
                    "each call waits for a person"
                ),
            )
        )
    return notes


async def with_wait_notes(read: RunApprovalRead) -> RunApprovalRead:
    """*read* with ``entries_that_wait`` filled."""
    notes = await wait_notes(read.tenant_id, read.entries)
    return read.model_copy(update={"entries_that_wait": notes})
