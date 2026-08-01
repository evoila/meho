# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``checks.transition`` broadcast events on the tenant feed (#2720).

Task #2720 under Initiative #2716 (parent goal #221). The third consumer
of #2507's compare-and-swap transition claim, beside the diagnose-only
investigator (:mod:`meho_backplane.checks.investigate`) and the email
notifier (:mod:`meho_backplane.checks.notify`): every claimed Dashboard
rollup edge is published to ``meho:feed:{tenant_id}`` as one
:class:`~meho_backplane.broadcast.events.BroadcastEvent` with op-id
``checks.transition``.

Why the claim is the publish point
==================================

The compare-and-swap in
:func:`~meho_backplane.checks.investigate._claim_dashboard_transition`
already returns to exactly one caller per edge, replica-safe. Publishing
from the claim-win branch therefore inherits exactly-once-per-transition
with no coordination here -- the same property the notifier gets, and the
reason neither module needs a dedupe key.

Both edge directions publish, unconditionally: a watcher who saw a
Dashboard go ``critical`` must see it clear. Unlike the mail notifier
there is no configured floor -- a feed event costs one ``XADD`` against a
``MAXLEN ~`` -trimmed stream, so the filtering belongs to the consumer
(``op_class=checks``), not to the producer.

Not audit-derived
=================

Every other :class:`BroadcastEvent` producer sits downstream of an audit
row and passes its id as ``audit_id`` (the field is documented as an FK
to ``audit_log.id``). A rollup edge is not an audited operation -- it is
derived state, folded from Sensor evaluations that were each audited on
their own dispatch -- so there is no row to point at. The event carries
:data:`_NO_AUDIT_ROW`, the nil UUID, rather than a fabricated id a
consumer could not tell from a real one. The one surface that
dereferences the field, the UI event drawer
(``/ui/broadcast/event/{audit_id}``), already renders its not-found
fragment for an id with no row, so the nil id degrades rather than
breaks.

``principal_sub`` is :data:`_CHECKS_PRINCIPAL_SUB` for the same reason:
the edge has no operator behind it, and a Dashboard folds many Sensors
that may each dispatch under a different ``identity_sub``, so attributing
the edge to any one of them would be a lie. ``"__sensor__"`` is
deliberately not reused -- that value is a *per-Sensor configurable
dispatch identity*, not a subsystem identity.

Failure posture
===============

Fail-open, twice over, and the two halves log in different places.
:func:`~meho_backplane.broadcast.publisher.publish_event` already swallows
every Valkey error (at-most-once delivery is the feed's documented
contract) and never re-raises, so a transport failure surfaces on its own
``broadcast_publish_failed`` warning and ``broadcast_publish_errors_total``
counter, not here. This module wraps the body in a second guard so a
malformed field or an unexpected lineage failure -- anything *before* the
publish -- cannot reach the caller either; that is the only thing
``checks_transition_broadcast_failed`` reports.
The publish is awaited inline rather than backgrounded: the claim is a
rare edge, the fast broadcast client pins both its connect and read
timeouts, and awaiting keeps the publish ordered with the claim that
caused it. A broadcast outage must never convert a committed transition
into a persist-path failure.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final, cast

import structlog

from meho_backplane.broadcast.events import BroadcastEvent, classify_op
from meho_backplane.broadcast.publisher import publish_event
from meho_backplane.operations._audit import resolve_broadcast_lineage

__all__ = ["CHECK_TRANSITION_OP_ID", "publish_check_transition_event"]


#: Broadcast op-id for a claimed Dashboard rollup edge. Pinned in
#: :data:`meho_backplane.broadcast.events._CHECK_EVENT_OPS` so
#: :func:`~meho_backplane.broadcast.events.classify_op` maps it to the
#: ``checks`` op-class; ``tests/test_checks_broadcast.py`` asserts the two
#: cannot drift.
CHECK_TRANSITION_OP_ID: Final[str] = "checks.transition"

#: Principal recorded on a transition event. A subsystem identity, not an
#: operator and not a Sensor's ``identity_sub`` -- see the module
#: docstring.
_CHECKS_PRINCIPAL_SUB: Final[str] = "__checks__"

#: ``audit_id`` sentinel for an event with no audit row behind it. The nil
#: UUID is self-describing: a consumer that joins on it finds nothing and
#: can tell that from a stale id.
_NO_AUDIT_ROW: Final[uuid.UUID] = uuid.UUID(int=0)


def _log() -> structlog.typing.FilteringBoundLogger:
    """Resolve the structlog logger per call (not a module-level proxy).

    A module-level proxy caches its bound methods on first use under the
    production ``cache_logger_on_first_use=True`` config, which orphans
    them from a later :func:`structlog.testing.capture_logs`. Same
    discipline as :mod:`meho_backplane.checks.notify`.
    """
    return cast("structlog.typing.FilteringBoundLogger", structlog.get_logger(__name__))


async def publish_check_transition_event(
    *,
    tenant_id: uuid.UUID,
    dashboard_id: uuid.UUID,
    dashboard_name: str,
    previous_state: str,
    new_state: str,
) -> None:
    """Publish one ``checks.transition`` event for a claimed rollup edge.

    **Never raises.** Called from the claim-win branch of
    :func:`~meho_backplane.checks.investigate._process_transition`, which
    runs on the check runner's result-persist path.

    The payload carries server-derived values only -- the Dashboard id,
    its operator-authored name (bounded at 128 chars by
    :data:`meho_backplane.checks.dashboard_schemas._NAME_MAX_LENGTH` at
    the create boundary), and the two states. No Sensor values, evidence,
    or member names: the class is not sensitive precisely because the
    payload stays at this altitude, and a consumer that wants the failing
    members reads the Dashboard.

    Args:
        tenant_id: Owning tenant; selects the ``meho:feed:{tenant_id}``
            stream, so the event is tenant-isolated by construction.
        dashboard_id: The Dashboard whose rollup moved.
        dashboard_name: Its display name, for a feed row that reads
            without a lookup.
        previous_state: Rollup state before the edge (``"ok"`` when the
            memo was NULL).
        new_state: Rollup state after the edge.
    """
    try:
        lineage = resolve_broadcast_lineage()
        op_class = classify_op(CHECK_TRANSITION_OP_ID)
        event = BroadcastEvent(
            event_id=uuid.uuid4(),
            ts=datetime.now(UTC),
            tenant_id=tenant_id,
            principal_sub=_CHECKS_PRINCIPAL_SUB,
            op_id=CHECK_TRANSITION_OP_ID,
            op_class=op_class,
            result_status="ok",
            audit_id=_NO_AUDIT_ROW,
            payload={
                "op_class": op_class,
                "result_status": "ok",
                "dashboard_id": str(dashboard_id),
                "dashboard_name": dashboard_name,
                "previous_state": previous_state,
                "new_state": new_state,
            },
            actor_sub=lineage.actor_sub,
            agent_session_id=lineage.agent_session_id,
            work_ref=lineage.work_ref,
        )
        await publish_event(event)
    except Exception:
        # Fail-open: the durable truth is the committed last_rollup_state
        # memo. A feed miss must not fail the runner's persist path.
        _log().warning(
            "checks_transition_broadcast_failed",
            dashboard_id=str(dashboard_id),
            previous_state=previous_state,
            new_state=new_state,
            exc_info=True,
        )
