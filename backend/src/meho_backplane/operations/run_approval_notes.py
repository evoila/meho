# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Entries of a run approval that each wait for a person.

A run approval never covers an operation that reads, writes or makes a
credential. A new request that lists one is refused. A request made before
this rule can still list one: the answer of show and approve names the
entry in ``entries_that_wait`` when its calls wait for a person, so the
add-on that asks and the person who approves both see it. A read that
returns no secret value runs at once and gets no note. A read that shows a
secret only with ``reveal_secret`` (``vault.kv.read``) gets a note that
says only those calls wait.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from meho_backplane.operations.credential_read import has_credential_read_floor
from meho_backplane.operations.run_approval_schemas import (
    RunApprovalEntry,
    RunApprovalRead,
    RunApprovalWaitNote,
)
from meho_backplane.operations.service_grants import credential_call_waits, only_uses_credential

__all__ = ["wait_notes", "with_wait_notes"]


async def wait_notes(
    tenant_id: uuid.UUID, entries: Sequence[RunApprovalEntry]
) -> list[RunApprovalWaitNote]:
    """The entries of *entries* whose calls wait for a person under a run approval.

    The call gate (``run_approval_gate.consult_and_record_run_approval``)
    never lets a run approval cover a call that reads, writes or makes a
    credential. Such a call waits when
    :func:`~meho_backplane.operations.service_grants.credential_call_waits`
    says so, with the class stored on the operation's row when MEHO finds it.
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
        reason = _wait_reason(entry.op_id, stored_class)
        if reason is None:
            continue
        notes.append(
            RunApprovalWaitNote(
                entry=index,
                connector_id=entry.connector_id,
                op_id=entry.op_id,
                reason=reason,
            )
        )
    return notes


def _wait_reason(op_id: str, stored_class: str | None) -> str | None:
    """The note for *op_id*, or ``None`` when none of its calls wait."""
    if credential_call_waits(op_id, stored_class):
        action = (
            "uses a credential"
            if only_uses_credential(op_id, stored_class)
            else "reads, writes or makes a credential"
        )
        return (
            f"op {op_id!r} {action}. A run approval never covers it: each call waits for a person"
        )
    if has_credential_read_floor(op_id, reveal_secret=True, stored_op_class=stored_class):
        return (
            f"op {op_id!r} reads a credential. A run approval never covers it: "
            "each call with reveal_secret waits for a person"
        )
    return None


async def with_wait_notes(read: RunApprovalRead) -> RunApprovalRead:
    """*read* with ``entries_that_wait`` filled."""
    notes = await wait_notes(read.tenant_id, read.entries)
    return read.model_copy(update={"entries_that_wait": notes})
