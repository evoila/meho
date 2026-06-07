# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time ``proposed_effect`` preview builder for ``secret.move``.

G0.22-T3 (#1579). Wires the secret-broker ``secret.move`` op onto the
per-op preview hook shipped by #1437
(:mod:`meho_backplane.operations._preview`). When the policy gate parks
an unapproved move at ``awaiting_approval``, this builder populates the
durable :class:`~meho_backplane.db.models.ApprovalRequest.proposed_effect`
with a **ref-only** summary so the human reviewer sees *which* credential
is being moved *between which stores* — and nothing value-derived beyond
what the response already exposes.

Ref-only by construction
========================

The summary names only the parsed ``{kind, ref}`` of the move's
``--from`` / ``--to`` references (e.g. ``vault`` /
``secret/db/prod#password``). These come straight from the op params,
which carry references and never the value (#1577's core invariant:
``additionalProperties: false`` on the param schema keeps a value field
from being smuggled in, and the value is read server-side into a
redacting :class:`~.endpoints.SecretMaterial`). The builder reads neither
store, so it cannot observe a value to leak; it does no I/O at all. The
optional operator ``reason`` is echoed (it is operator-authored audit
text the schema requires not to carry a value).

Why ``secret.move`` reaches a builder at all
============================================

:func:`~meho_backplane.broadcast.events.classify_op` returns ``"other"``
for ``secret.move`` (``.move`` is in neither the read- nor the
write-suffix set, and ``secret.move`` is in no credential allowlist), so
the credential-class preview suppression in
:func:`~meho_backplane.operations._preview.build_proposed_effect` does
**not** fire — a registered builder runs and its ref-only dict lands on
the row. (For a credential-class op the preview is suppressed to ``None``;
``secret.move`` carries no value to suppress, so surfacing the refs is
both safe and the point.) ``build_proposed_effect`` wraps the returned
dict as ``{"op_class": "other", "preview": <this dict>}``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.secret.endpoints import parse_secret_ref
from meho_backplane.operations._preview import (
    PreviewContext,
    register_preview_builder,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.secret.endpoints import SecretRef

__all__ = ["build_secret_move_preview"]


def _ref_summary(spec: SecretRef) -> dict[str, str]:
    """Render a parsed ``<kind>:<ref>`` as a ref-only summary dict.

    Names the store-selecting ``kind`` and the store-specific ``ref``
    separately so a reviewer reads the move's endpoints structurally. The
    ``ref`` is an address (a Vault KV path + ``#<field>`` fragment), never
    a value.
    """
    return {"kind": spec.kind, "ref": spec.ref}


async def build_secret_move_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Build the ref-only ``proposed_effect`` preview for ``secret.move``.

    Names the move's source and sink as parsed ``{kind, ref}`` references
    plus the optional operator ``reason`` — all drawn from the params,
    which carry references only. Does no store I/O, so no value can enter
    the summary. Declines (returns ``None`` → identifier-only default)
    when a ref is absent or malformed; the dispatcher has already
    schema-validated the params at this point, so that path is defensive.
    """
    raw_from = ctx.params.get("from")
    raw_to = ctx.params.get("to")
    if not isinstance(raw_from, str) or not isinstance(raw_to, str):
        return None
    try:
        source = parse_secret_ref(raw_from)
        sink = parse_secret_ref(raw_to)
    except ValueError:
        return None

    summary: dict[str, Any] = {
        "action": "secret.move",
        "source": _ref_summary(source),
        "sink": _ref_summary(sink),
    }
    reason = ctx.params.get("reason")
    if isinstance(reason, str) and reason:
        summary["reason"] = reason
    return summary


def _register_secret_move_preview_builder() -> None:
    """Wire the ``secret.move`` park-time preview builder. Import-time."""
    register_preview_builder("secret.move", build_secret_move_preview)


_register_secret_move_preview_builder()
