# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval previews for the two handoff steps.

When ``secret.handoff.export`` or ``secret.handoff.import`` waits for
approval, the approver sees only addresses and the reason: the source or
sink ``{kind, ref}``, the store target name, the handoff life and the
expected SHA-256. The handoff token in the import's params is never
copied into the preview. Neither builder reads a store, so no value can
reach the preview.

Both ops are pinned as credential classes in
:mod:`meho_backplane.broadcast.events`, which suppresses the generic
params echo; a bespoke builder is the only way the approver sees what is
moved (the same pattern as ``secret.move`` in :mod:`.move_preview`).
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.secret.endpoints import parse_secret_ref
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

__all__ = ["build_secret_handoff_export_preview", "build_secret_handoff_import_preview"]


def _ref_summary(raw: object) -> dict[str, str] | None:
    """Return ``{kind, ref}`` for a ``<kind>:<ref>`` string, or ``None`` when malformed."""
    if not isinstance(raw, str):
        return None
    try:
        spec = parse_secret_ref(raw)
    except ValueError:
        return None
    return {"kind": spec.kind, "ref": spec.ref}


def _add_reason(summary: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    reason = params.get("reason")
    if isinstance(reason, str) and reason:
        summary["reason"] = reason
    return summary


async def build_secret_handoff_export_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the source address and the handoff life. Declines on a malformed ref."""
    source = _ref_summary(ctx.params.get("from"))
    if source is None:
        return None
    summary: dict[str, Any] = {"action": "secret.handoff.export", "source": source}
    ttl = ctx.params.get("ttl_seconds")
    if isinstance(ttl, int):
        summary["handoff_ttl_seconds"] = ttl
    return _add_reason(summary, ctx.params)


async def build_secret_handoff_import_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Show the store target and the sink address — never the handoff token."""
    sink = _ref_summary(ctx.params.get("to"))
    store = ctx.params.get("store")
    if sink is None or not isinstance(store, str):
        return None
    summary: dict[str, Any] = {
        "action": "secret.handoff.import",
        "store_target": store,
        "sink": sink,
    }
    expected = ctx.params.get("expect_sha256")
    if isinstance(expected, str):
        summary["expect_sha256"] = expected.lower()
    return _add_reason(summary, ctx.params)


register_preview_builder("secret.handoff.export", build_secret_handoff_export_preview)
register_preview_builder("secret.handoff.import", build_secret_handoff_import_preview)
