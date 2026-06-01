# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-op ``proposed_effect`` builder hook for the approval-park path.

G11.7 follow-up (#1437). When the policy gate routes a dispatch to
``needs-approval`` (:func:`~meho_backplane.operations.dispatcher.dispatch`
Step 4 → :func:`_handle_needs_approval`), most ops have nothing to show
the reviewer beyond the call's identity, so the durable
:class:`~meho_backplane.db.models.ApprovalRequest` row stores the
identifier-only default ``{op_id, connector_id, target_id}``.

A small set of ops *can* compute a side-effect-free **preview** of what
the approved call would do -- notably ``k8s.apply``'s server-side-apply
dry-run (``dry_run="server"`` → the API's ``?dryRun=All``), which returns
the would-be object without persisting anything. This module is the
general, opt-in seam that lets such an op populate
``ApprovalRequest.proposed_effect`` at queue time, so the human reviewer
reads the diff in the approval queue rather than only in the
post-approval op result.

Design contract
---------------

* **Opt-in per op.** A builder is registered against an ``op_id``; ops
  without a registered builder fall through to the identifier-only
  default exactly as before -- no extra work, no new failure mode.
* **Fail-soft.** A builder that raises (a dry-run that hits the API
  server and errors, a transient connector fault) must never block the
  park: :func:`build_proposed_effect` swallows the exception, logs it,
  and returns ``None`` so the caller uses the default. Parking an
  approval is the durable, safety-relevant action; a missing preview is
  a degraded-but-acceptable outcome, an exception is not.
* **Redaction-safe.** The preview lands in a durable row surfaced over
  REST / MCP / CLI, so it must not carry secret material. The hook
  reuses the single-sourced sensitivity classification from
  :func:`~meho_backplane.broadcast.events.classify_op` (shipped by
  G11.7-T1 #1401): an op that classifies as a credential class
  (``credential_read`` / ``credential_mint`` / ``credential_write``)
  never has a raw preview stored -- it collapses to an aggregate marker
  the same way the broadcast layer collapses such ops. Builders are
  themselves expected to return identity-only summaries (``k8s.apply``'s
  dry-run echoes resource identity + ``resourceVersion`` + ``uid``, never
  Secret ``data``), so this gate is defence-in-depth, not the only
  guard.

The k8s.apply builder is wired here (the only op in scope per #1437);
additional ops register their own builders as the need arises (argocd
writes are the separate follow-up #1452).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from meho_backplane.broadcast.events import classify_op

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.base import Connector
    from meho_backplane.db.models import EndpointDescriptor

__all__ = [
    "PreviewContext",
    "build_proposed_effect",
    "register_preview_builder",
]

_log = structlog.get_logger(__name__)

#: Sensitivity classes whose preview must never be stored verbatim.
#: Mirrors the aggregate-only set in
#: :func:`~meho_backplane.broadcast.events.redact_payload`.
_SENSITIVE_CLASSES: frozenset[str] = frozenset(
    {"credential_read", "credential_mint", "credential_write"}
)


@dataclass(frozen=True)
class PreviewContext:
    """Everything a preview builder needs to compute a dry-run.

    The dispatcher assembles this at the approval-park point and hands it
    to the registered builder. The builder owns the connector call (e.g.
    re-invoking the op's handler with a dry-run flag forced on); the
    dispatcher only resolves the connector instance + descriptor.

    Attributes:
        descriptor: The looked-up :class:`EndpointDescriptor` for the op.
        connector_instance: The resolved connector singleton, or ``None``
            for module-level handlers that bind no connector.
        operator: The authenticated operator whose dispatch parked.
        target: The dispatch target (or ``None`` for tenant-wide ops).
        params: The original dispatch params.
    """

    descriptor: EndpointDescriptor
    connector_instance: Connector | None
    operator: Operator
    target: Any
    params: dict[str, Any]


class PreviewBuilder(Protocol):
    """An op's opt-in preview computation.

    Returns the preview dict to store in
    :attr:`~meho_backplane.db.models.ApprovalRequest.proposed_effect`, or
    ``None`` to decline (caller falls back to the identifier-only
    default). May raise -- :func:`build_proposed_effect` treats a raise
    as "no preview" rather than failing the park.
    """

    async def __call__(self, ctx: PreviewContext) -> dict[str, Any] | None: ...


#: ``op_id`` -> registered preview builder. Opt-in: absence ⇒ no preview.
_PREVIEW_BUILDERS: dict[str, PreviewBuilder] = {}


def register_preview_builder(op_id: str, builder: PreviewBuilder) -> None:
    """Register *builder* as the preview computation for *op_id*.

    Idempotent re-registration overwrites -- registration happens at
    import time so a re-import (test reload) is a no-op-equivalent.
    """
    _PREVIEW_BUILDERS[op_id] = builder


async def build_proposed_effect(ctx: PreviewContext) -> dict[str, Any] | None:
    """Compute the ``proposed_effect`` preview for a parking dispatch.

    Looks up a builder for ``ctx.descriptor.op_id``; returns ``None``
    (caller uses the identifier-only default) when no builder is
    registered, when the op classifies as a credential class (the
    preview is suppressed to stay redaction-safe), when the builder
    declines, or when the builder raises (fail-soft -- the park proceeds
    regardless).

    On success the returned dict is wrapped with a ``"preview"`` envelope
    key + the op's sensitivity ``"op_class"`` so the approval surface can
    label it as a computed preview rather than the bare identifier
    default.
    """
    op_id = ctx.descriptor.op_id
    builder = _PREVIEW_BUILDERS.get(op_id)
    if builder is None:
        return None

    op_class = classify_op(op_id)
    if op_class in _SENSITIVE_CLASSES:
        # A credential-class op must not surface request/response detail
        # in a durable row. Refuse the preview outright rather than risk
        # a builder that echoes secret material; the reviewer still sees
        # the identifier-only default via the caller's fallback.
        _log.info(
            "proposed_effect_preview_suppressed",
            op_id=op_id,
            op_class=op_class,
            reason="sensitive_op_class",
        )
        return None

    try:
        preview = await builder(ctx)
    except Exception:
        # Fail-soft: a preview is a convenience, the park is the
        # safety-relevant action. Never let a dry-run fault block it.
        _log.warning(
            "proposed_effect_preview_failed",
            op_id=op_id,
            operator_sub=getattr(ctx.operator, "sub", None),
            exc_info=True,
        )
        return None

    if preview is None:
        return None

    return {
        "op_class": op_class,
        "preview": preview,
    }


async def _k8s_apply_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview builder for ``k8s.apply`` -- the server-side-apply dry-run.

    Re-invokes the ``k8s_apply`` handler with ``dry_run="server"`` forced
    on (the API's ``?dryRun=All``) so nothing is persisted; the returned
    per-document summary (resource identity + ``resourceVersion`` +
    ``uid``) is the diff-preview the reviewer reads. The handler echoes
    no Secret ``data`` -- it operates on the manifest's GVK + metadata --
    so the summary is identity-only and redaction-safe.

    Returns ``None`` when no connector instance resolved (a k8s.apply
    without a target can't dry-run) so the caller falls back to the
    identifier-only default.
    """
    if ctx.connector_instance is None or ctx.target is None:
        return None

    from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
    from meho_backplane.connectors.kubernetes.ops_write_dangerous import k8s_apply

    if not isinstance(ctx.connector_instance, KubernetesConnector):
        return None

    # Force the dry-run flag on regardless of what the caller passed:
    # the preview must never persist, even if the parked call itself was
    # a real apply (dry_run="none").
    preview_params = {**ctx.params, "dry_run": "server"}
    return await k8s_apply(
        ctx.connector_instance,
        ctx.target,
        ctx.operator,
        preview_params,
    )


def _register_builtin_builders() -> None:
    """Wire the in-tree preview builders. Called at import time.

    Only ``k8s.apply`` is wired in #1437; other ops register their own
    builders as the need arises (the hook is general).
    """
    register_preview_builder("k8s.apply", _k8s_apply_preview)


_register_builtin_builders()
