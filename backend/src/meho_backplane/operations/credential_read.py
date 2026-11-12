# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The floor for operations that show a secret value.

An operation whose class is ``credential_read`` runs at risk level
``caution`` or higher and waits for a person's approval. The floor only
raises: a connector may set a higher level, never a lower one.

The one exception is a read that is marked in code as one that returns no
secret value (:func:`~meho_backplane.op_classes.declare_no_secret_value`),
for example a read that returns only key names. ``vault.kv.read`` is marked
for a call without ``reveal_secret`` only: it hides every value by default,
and a call that asks to see the values gets the floor. A spec import and a
tenant override cannot set a mark.

MEHO applies the floor in three places:

* when an API description is ingested
  (:func:`~meho_backplane.operations.ingest.safety_floors.apply_safety_floor`);
* when an operation is looked up, after the operator overrides
  (:func:`~meho_backplane.operations.op_policy.effective_policy_map`). So it
  holds for typed and ingested operations, for a row stored before the
  floor existed, and when an override turns approval off;
* when a call asks for ``reveal_secret``
  (:func:`raise_descriptor_for_reveal`): in the dispatcher before the
  policy gate, and in the preview before it reports the risk level and the
  policy verdict. So ``vault.kv.read`` with the flag waits for approval,
  and its preview says so.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from meho_backplane.broadcast.events import classify_op
from meho_backplane.op_classes import returns_no_secret_value

if TYPE_CHECKING:
    from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "has_credential_read_floor",
    "raise_descriptor_for_reveal",
    "raise_for_credential_read",
]

FLOOR_SAFETY_LEVEL: Final[str] = "caution"

_SAFETY_RANK: Final[dict[str, int]] = {"safe": 0, "caution": 1, "dangerous": 2, "destructive": 3}

#: The Vault reads whose answer holds no secret value. Their class is set in
#: core code (``_CREDENTIAL_READ_OPS`` in :mod:`meho_backplane.broadcast.events`),
#: so their mark is set here, next to the rule. Other connectors mark their
#: reads in their own package with
#: :func:`~meho_backplane.op_classes.declare_no_secret_value`.
_VAULT_NO_SECRET_VALUE_OPS: Final[frozenset[str]] = frozenset(
    {
        # Returns key names only.
        "vault.kv.list",
        # Reads the values inside MEHO and returns only "same" or "different".
        "vault.kv.compare",
    }
)

#: The Vault reads whose answer hides every value unless the call asks for
#: ``reveal_secret``. The mark covers a call without the flag only: a call
#: that asks to see the values gets the floor.
_VAULT_HIDDEN_UNLESS_REVEALED_OPS: Final[frozenset[str]] = frozenset({"vault.kv.read"})


def has_credential_read_floor(op_id: str, *, reveal_secret: bool = False) -> bool:
    """Return whether the floor applies to a call of *op_id*.

    True for a ``credential_read`` operation, unless it is marked in code
    as one that returns no secret value. ``vault.kv.read`` hides every
    value by default, so its mark holds only for a call without
    *reveal_secret*.
    """
    if classify_op(op_id) != "credential_read":
        return False
    if op_id in _VAULT_NO_SECRET_VALUE_OPS or returns_no_secret_value(op_id):
        return False
    return reveal_secret or op_id not in _VAULT_HIDDEN_UNLESS_REVEALED_OPS


def raise_for_credential_read(
    op_id: str, *, safety_level: str, requires_approval: bool, reveal_secret: bool = False
) -> tuple[str, bool]:
    """Return the risk level and approval flag after the floor.

    When the floor applies (:func:`has_credential_read_floor`), the level
    is at least ``caution`` and approval is on. Otherwise both values come
    back unchanged.
    """
    if not has_credential_read_floor(op_id, reveal_secret=reveal_secret):
        return safety_level, requires_approval
    if _SAFETY_RANK.get(safety_level, 0) < _SAFETY_RANK[FLOOR_SAFETY_LEVEL]:
        safety_level = FLOOR_SAFETY_LEVEL
    return safety_level, True


def raise_descriptor_for_reveal(descriptor: EndpointDescriptor, *, reveal_secret: bool) -> None:
    """Apply the floor of a ``reveal_secret`` call to *descriptor*, in place.

    The lookup applied the floor for a call without the flag. A read that
    shows its hidden values only on request (``vault.kv.read``) gets the
    floor when the call asks for them. The dispatcher and the preview both
    call this, so the preview reports the level and the wait that the real
    call gets. *descriptor* is the detached row of one lookup, so the change
    is never written back.
    """
    if not reveal_secret:
        return
    descriptor.safety_level, descriptor.requires_approval = raise_for_credential_read(
        descriptor.op_id,
        safety_level=descriptor.safety_level,
        requires_approval=descriptor.requires_approval,
        reveal_secret=True,
    )
