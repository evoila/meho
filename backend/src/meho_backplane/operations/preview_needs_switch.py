# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Ops whose agent preview opens only while ``PREVIEW_TYPED_WRITES`` is on.

A typed op that carries ``requires_approval`` gets the agent's synthetic
preview (``preview_operation``) also while the switch is off
(:func:`~meho_backplane.operations._request_preview._is_previewable`). An op
listed here gets it only while the switch is on, the same as a typed write
that needs no approval. The approval itself does not change: the call still
waits for a person.
"""

from __future__ import annotations

__all__ = ["preview_needs_switch", "register_preview_needs_switch"]

_OPS: set[str] = set()


def register_preview_needs_switch(op_id: str) -> None:
    """Open the agent preview of *op_id* only while ``PREVIEW_TYPED_WRITES`` is on."""
    _OPS.add(op_id)


def preview_needs_switch(op_id: str) -> bool:
    """True when the agent preview of *op_id* needs ``PREVIEW_TYPED_WRITES`` on."""
    return op_id in _OPS
