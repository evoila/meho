# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time ``proposed_effect`` preview builder for ``rke2.token.rotate`` (#2429).

Wires the approval-gated RKE2 token-rotate op onto the per-op preview hook
(:mod:`meho_backplane.operations._preview`) so a human approving the parked
rotate reads *what it will do* -- which node, which service, that a new token
will be minted -- rather than only the identifier-only default.

``rke2.token.rotate`` classifies as ``credential_mint`` (pinned in
``broadcast/events.py``), so the generic params-echo default is suppressed
for it. A **bespoke** builder is the deliberate exception (#1857): it is
trusted to own its own field discipline. This builder returns only
side-effect-free, non-secret metadata -- it neither mints a token nor runs
the rotate, and it never surfaces a token value -- so it is redaction-safe.
It is fail-soft: a raise is swallowed by :func:`build_proposed_effect` into a
``preview_unavailable`` marker and never blocks the park.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from meho_backplane.operations._preview import register_preview_builder

if TYPE_CHECKING:
    from typing import Any

    from meho_backplane.operations._preview import PreviewContext

__all__ = ["rke2_token_rotate_preview"]


async def rke2_token_rotate_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview builder for ``rke2.token.rotate``.

    Returns a non-secret summary of the approved rotate: the node, the
    affected service, the rotate semantics, and that a new token will be
    minted server-side (never the value). Declines (``None``) when no target
    resolved, so the caller falls back to the identifier-only default.
    """
    if ctx.target is None:
        return None
    # Lazy import: keeps this preview module's top-level imports free of the
    # ops <-> ops_write chain so importing it (a registration side-effect in
    # __init__) never trips the connector package's partial-import order.
    from meho_backplane.connectors.rke2.ops_write import _node_label

    return {
        "node": _node_label(ctx.target),
        "service": "rke2-server",
        "semantics": "rotate",
        "new_token_minted": True,
    }


register_preview_builder("rke2.token.rotate", rke2_token_rotate_preview)
