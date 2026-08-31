# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Execute one centrally-authorized work item against a local handler.

The runner reuses the chassis's DB-free handler-resolution primitives
(:mod:`meho_backplane.operations._handler_resolve`) but not the DB-bound
:func:`~meho_backplane.operations.dispatcher.dispatch`: the assignment
already carries the centrally-resolved descriptor fields, so the executor
resolves the handler from the payload alone.

Two fail-closed guards make the runner a strictly bounded executor
(defence in depth — central mint is the real authorization boundary,
owned by #2500):

* **satellite-mint tier ladder** (#3188) — the item's ``safety_level`` is
  classified against the shared
  :mod:`~meho_backplane.runner.satellite_tier` ladder (the same source of
  truth the central mint and the assignment materialiser use). An
  ``EXCLUDED`` (``dangerous`` / ``destructive``) item is refused outright;
  a ``REMOTE_WRITE`` (``caution``) item has its centre-issued Ed25519
  signature verified offline (integrity + freshness + target scope, #3189)
  and is then re-screened through the allowlist gate, failing closed at
  every step until the runner is authorised for the tier; only a ``SAFE``
  item proceeds.
* **connector-tree-only** — a ``handler_ref`` that does not resolve inside
  ``meho_backplane.connectors.*`` is refused. The lexical prefix is
  checked *before* import (an import has module-load side effects) and the
  resolved callable's ``__module__`` is re-checked after.

A handler that raises becomes a structured ``error`` result — a failed
check is a result, never a crashed tick.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.wrapped_creds import screen_remote_write_credential
from meho_backplane.connectors.registry import all_connectors_v2
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    import_handler,
    is_unbound_method,
)
from meho_backplane.runner.satellite_tier import (
    SatelliteMintTier,
    classify_satellite_tier,
    evaluate_remote_write_gate,
)
from meho_backplane.runner.spool import ExecutedCommandStore
from meho_backplane.runner.wire import RunnerResult, RunnerWorkItem
from meho_backplane.runner.work_item_signing import (
    TARGETLESS_SCOPE,
    SigningKeyUnavailableError,
    load_verify_key,
    params_digest,
    verify_remote_write_item,
)

__all__ = ["execute_command_once", "execute_work_item"]

_log = structlog.get_logger(__name__)

_CONNECTOR_MODULE_PREFIX = "meho_backplane.connectors."


def _result(
    item: RunnerWorkItem,
    status: str,
    *,
    payload: dict[str, Any] | None,
    error: str | None,
) -> RunnerResult:
    return RunnerResult(
        result_uid=uuid.uuid4().hex,
        check_ref=item.check_ref,
        op_id=item.op_id,
        status=status,
        result=payload,
        error=error,
    )


def _screen_item(item: RunnerWorkItem) -> str | None:
    """Pre-import fail-closed screen: a refusal reason, or ``None`` to proceed.

    Mirrors the central mint's satellite-mint tier ladder (#3188) against the
    same shared classifier — defence in depth, the runner independently
    re-checking what the centre already gated. Every check runs before any
    import so an out-of-tree ``handler_ref`` never triggers a module-load side
    effect.
    """
    tier = classify_satellite_tier(item.safety_level)
    if tier is SatelliteMintTier.EXCLUDED:
        return (
            f"safety_level {item.safety_level!r} refused; dangerous/destructive "
            "ops are never dispatched to a runner"
        )
    if tier is SatelliteMintTier.REMOTE_WRITE:
        # Mechanism 1 edge check (#3189): verify the centre's signature +
        # freshness + target scope offline, before any allowlist re-check or
        # import. A bad / expired / out-of-scope (or unsigned) item fails
        # closed here.
        signature_refusal = _verify_remote_write_signature(item)
        if signature_refusal is not None:
            return signature_refusal
        # Mechanism 2 edge re-check (#3190): the per-runner allowlist. Still
        # fail-closed until #3190 wires it, so a validly-signed remote-write
        # item does not execute at the edge until the allowlist is provisioned
        # — the tier stays closed end-to-end.
        decision = evaluate_remote_write_gate(op_id=item.op_id)
        if not decision.permitted:
            return decision.reason
        # Mechanism 3 (#3191) edge check, composed after the gate: a
        # remote-write item must carry a per-work-item single-use wrapped
        # credential — a standing/broad secret_ref is refused, so no standing
        # runner credential ever rides the write tier.
        cred_refusal = screen_remote_write_credential(item.target_descriptor)
        if cred_refusal is not None:
            return cred_refusal
    if not item.handler_ref.startswith(_CONNECTOR_MODULE_PREFIX):
        return f"handler_ref {item.handler_ref!r} is outside {_CONNECTOR_MODULE_PREFIX}*"
    return None


def _verify_remote_write_signature(item: RunnerWorkItem) -> str | None:
    """Verify a remote-write item's signature, or a fail-closed refusal reason.

    Mechanism 1's edge half (#3189): the DB-free runner independently verifies
    the centre's Ed25519 signature over the canonical work-item payload
    (integrity over ``op_id`` + ``params_hash``, the ``target_scope`` binding)
    and the ``expires_at`` freshness bound, using the verification (public) key
    provisioned at enrollment. Every refusal reason names ``remote-write`` so
    the tier is legible in the refusal log.

    The order is integrity/scope first, then freshness: a tampered op/params or
    a re-pointed target breaks the signature (caught first); a genuine but
    stale capability has a valid signature but is refused by the ``expires_at``
    check. An unsigned item, a missing freshness bound, or an unprovisioned
    verification key all fail closed.
    """
    from meho_backplane.settings import get_settings

    prefix = f"remote-write op {item.op_id!r} refused"
    if not item.signature:
        return (
            f"{prefix}: work item is unsigned; the caution tier requires "
            "a centrally-signed capability"
        )
    if item.expires_at is None:
        return f"{prefix}: signed work item carries no expires_at freshness bound"
    try:
        verify_key = load_verify_key(get_settings().satellite_write_verify_key)
    except SigningKeyUnavailableError as exc:
        return f"{prefix}: {exc}"

    target_scope = (
        str(item.target_descriptor.id) if item.target_descriptor is not None else TARGETLESS_SCOPE
    )
    verified = verify_remote_write_item(
        verify_key,
        item.signature,
        op_id=item.op_id,
        params_hash=params_digest(item.params),
        target_scope=target_scope,
        expires_at=item.expires_at,
    )
    if not verified:
        return (
            f"{prefix}: signature verification failed "
            "(tampered op/params, out-of-scope target, or wrong key)"
        )
    if datetime.now(UTC) >= item.expires_at:
        return f"{prefix}: signed capability expired at {item.expires_at.isoformat()}"
    return None


async def _invoke(handler: Callable[..., Awaitable[Any]], item: RunnerWorkItem) -> RunnerResult:
    """Invoke *handler* and wrap its outcome as a structured result."""
    operator = _build_operator(item)
    try:
        payload = await handler(operator, item.target_descriptor, dict(item.params))
    except Exception as exc:  # a failed check is a result, never a crashed tick
        _log.warning(
            "runner_item_handler_raised",
            op_id=item.op_id,
            check_ref=item.check_ref,
            exc_info=True,
        )
        return _result(item, "error", payload=None, error=f"{type(exc).__name__}: {exc}")
    if not isinstance(payload, dict):
        return _result(
            item, "error", payload=None, error=f"handler returned non-dict {type(payload).__name__}"
        )
    return _result(item, "ok", payload=payload, error=None)


async def execute_command_once(
    command_id: str,
    item: RunnerWorkItem,
    store: ExecutedCommandStore,
) -> RunnerResult:
    """Execute a gateway command at most once, keyed on its UUID request id.

    The runner's half of the at-most-once guarantee (#2500): *command_id* is
    recorded in *store* **before** the local dispatch (record-before-execute),
    so a redelivery is never re-executed —

    * a redelivery whose result was spooled is re-submitted (the centre's
      ``consumed_at`` latch refuses a double-accept, so re-submission is safe);
    * a redelivery recorded but with no stored result (a crash mid-dispatch)
      returns a structured ``duplicate_delivery`` refusal — still no re-run.

    A first delivery records the id, executes via :func:`execute_work_item`,
    stores the result, and returns it.
    """
    if not store.record(command_id):
        prior = store.load_result(command_id)
        if prior is not None:
            _log.info(
                "runner_command_duplicate_resubmit",
                command_id=command_id,
                op_id=item.op_id,
                check_ref=item.check_ref,
            )
            return prior
        _log.warning(
            "runner_command_duplicate_no_result",
            command_id=command_id,
            op_id=item.op_id,
            check_ref=item.check_ref,
        )
        return _result(
            item,
            "refused",
            payload=None,
            error=f"duplicate_delivery: command {command_id} already recorded, "
            "no spooled result to re-submit",
        )

    result = await execute_work_item(item)
    store.store_result(command_id, result)
    return result


async def execute_work_item(item: RunnerWorkItem) -> RunnerResult:
    """Execute *item* locally and return a structured :class:`RunnerResult`."""
    refusal = _screen_item(item)
    if refusal is not None:
        _log.warning(
            "runner_item_refused",
            op_id=item.op_id,
            check_ref=item.check_ref,
            reason=refusal,
        )
        return _result(item, "refused", payload=None, error=refusal)

    try:
        handler = import_handler(item.handler_ref)
    except (ImportError, TypeError) as exc:
        _log.warning(
            "runner_item_handler_unresolved",
            op_id=item.op_id,
            check_ref=item.check_ref,
            handler_ref=item.handler_ref,
        )
        return _result(item, "error", payload=None, error=f"handler_unresolved: {exc}")

    module = getattr(handler, "__module__", "") or ""
    if not module.startswith(_CONNECTOR_MODULE_PREFIX):
        _log.warning(
            "runner_item_refused_out_of_tree",
            op_id=item.op_id,
            check_ref=item.check_ref,
            handler_ref=item.handler_ref,
            resolved_module=module,
        )
        return _result(
            item,
            "refused",
            payload=None,
            error=f"handler_ref {item.handler_ref!r} resolved outside {_CONNECTOR_MODULE_PREFIX}*",
        )

    return await _invoke(_maybe_bind_method(handler, item), item)


def _maybe_bind_method(
    handler: Callable[..., Awaitable[Any]], item: RunnerWorkItem
) -> Callable[..., Awaitable[Any]]:
    """Rebind a bound-method handler against its connector instance.

    Mirrors the dispatcher's rebinding (``_maybe_bind_method``) minus the
    DB descriptor lookup: the connector class comes from the in-memory
    registry keyed on the payload's ``(product, version, impl_id)``.
    Module-level function handlers (e.g. ``net.*``) are not on any
    connector's MRO and are returned unchanged.
    """
    connector_cls = all_connectors_v2().get((item.product, item.version, item.impl_id))
    if connector_cls is None:
        return handler
    if not is_unbound_method(handler, connector_cls):
        return handler
    instance = get_or_create_connector_instance(connector_cls)
    bound: Callable[..., Awaitable[Any]] = handler.__get__(instance, connector_cls)
    return bound


def _build_operator(item: RunnerWorkItem) -> Operator:
    """Reconstruct the acting :class:`Operator` from the principal context.

    ``raw_jwt`` is empty: the op was authorized centrally, so no bearer
    token for the acting principal exists on the runner, and the field is
    ``repr``-excluded so it never leaks even when empty.
    """
    principal = item.principal
    return Operator(
        sub=principal.sub,
        raw_jwt="",
        tenant_id=principal.tenant_id,
        tenant_role=principal.tenant_role,
        principal_kind=principal.principal_kind,
    )
