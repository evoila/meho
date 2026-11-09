# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Send push messages about requests that wait for approval.

:func:`notify_approval_step` is called by
:func:`~meho_backplane.operations.approval_queue.record_approval_step_event`,
which every call site runs only **after** the transaction with the audit row
has committed. It starts the sending in the background and returns at once,
so the request never waits for a push service.

* A new request (``pending``): "An approval waits" to every browser of the
  people who may approve it in that tenant
  (:func:`~meho_backplane.web_push.recipients.subscriptions_for`).
* A decision (``approved``, ``rejected``, ``expired``, ``withdrawn``): the
  result, with the same notification tag, so it replaces the first message
  on the device. The ``Topic`` header replaces it at the push service too
  when the first one was not delivered yet.
* The message holds the words, the tenant name, the request id and the
  address of the approvals page. Never the request's operation, parameters
  or target. It is encrypted for each browser
  (:func:`~meho_backplane.web_push.encryption.encrypt`).
* A person gets at most one "An approval waits" message per
  :data:`RATE_LIMIT_SECONDS`. The ones held back in that time become one
  summary at its end. A decision is never held back: it replaces a message
  the person already has.
* A push service that answers 404 or 410 (the browser dropped the
  subscription) gets its row deleted.
* Messages go only to the browser makers' push services (checked again here,
  not only when the subscription was stored), without following redirects.
  Logs name the row id, the person, the push service and the status. Never
  the endpoint or a key.

The rate limit is kept in memory, per process.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant
from meho_backplane.settings import get_settings
from meho_backplane.web_push.encryption import encrypt, vapid_authorization
from meho_backplane.web_push.endpoints import push_service_name
from meho_backplane.web_push.keys import push_public_key
from meho_backplane.web_push.recipients import (
    PushTarget,
    subscriptions_for,
    subscriptions_of_person,
)
from meho_backplane.web_push.store import delete_gone

if TYPE_CHECKING:
    from meho_backplane.db.models import ApprovalRequest

__all__ = [
    "APPROVALS_PAGE",
    "RATE_LIMIT_SECONDS",
    "notify_approval_step",
    "reset_for_testing",
    "set_transport_for_testing",
    "wait_for_sends_for_testing",
]

_log = structlog.get_logger(__name__)

#: The address the notification opens (on the MEHO the browser subscribed to).
APPROVALS_PAGE = "/ui/approvals"
#: At most one message per person in this many seconds; then one summary.
RATE_LIMIT_SECONDS = 30.0
#: How long a push service keeps an undelivered message (seconds).
_TTL_SECONDS = 24 * 60 * 60
_TIMEOUT = httpx.Timeout(10.0)
_SUMMARY_TAG = "approval-summary"
_SUMMARY_TOPIC = "approvalsummary"
_HIGH_RISK = frozenset({"dangerous", "destructive"})
_DECISION_TITLES = {
    "approved": "Approval approved",
    "rejected": "Approval rejected",
    "expired": "Approval expired",
    "withdrawn": "Approval withdrawn",
}

_TASKS: set[asyncio.Task[None]] = set()
_transport: httpx.AsyncBaseTransport | None = None


@dataclass(frozen=True)
class _Step:
    tenant_id: uuid.UUID
    request_id: uuid.UUID
    requester_sub: str
    high_risk: bool
    decision: str


@dataclass(frozen=True)
class _Message:
    title: str
    body: str
    tag: str
    topic: str

    def payload(self) -> bytes:
        data = {"title": self.title, "body": self.body, "tag": self.tag, "url": APPROVALS_PAGE}
        return json.dumps(data, separators=(",", ":")).encode()


class _RateLimit:
    """One message per person per window; count the held-back ones."""

    def __init__(self, window: float) -> None:
        self.window = window
        self._last: dict[str, float] = {}
        self._held: dict[str, int] = {}

    def admit(self, person: str) -> bool:
        now = time.monotonic()
        last = self._last.get(person)
        if last is not None and now - last < self.window:
            return False
        self._last[person] = now
        return True

    def hold(self, person: str) -> bool:
        """Count one held message; ``True`` when it is the first of the window."""
        self._held[person] = self._held.get(person, 0) + 1
        return self._held[person] == 1

    def wait_seconds(self, person: str) -> float:
        last = self._last.get(person, 0.0)
        return max(0.0, last + self.window - time.monotonic())

    def take_held(self, person: str) -> int:
        self._last[person] = time.monotonic()
        return self._held.pop(person, 0)


_limit = _RateLimit(RATE_LIMIT_SECONDS)


def _spawn(coro: Coroutine[Any, Any, None], *, name: str) -> None:
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def notify_approval_step(*, tenant_id: uuid.UUID, request: ApprovalRequest, decision: str) -> None:
    """Start sending the push messages for one approval step; never raises.

    Call only after the transaction with the audit row committed. Does
    nothing while the push feature is off (``WEB_PUSH_ENABLED``, or no
    usable push key).
    """
    try:
        if push_public_key() is None:
            return
        level = (request.proposed_effect or {}).get("safety_level")
        step = _Step(
            tenant_id=tenant_id,
            request_id=request.id,
            requester_sub=request.principal_sub,
            high_risk=level in _HIGH_RISK,
            decision=decision,
        )
    except Exception as exc:
        _log.warning("web_push_step_skipped", error=type(exc).__name__)
        return
    _spawn(_send_step(step), name=f"web-push-{step.request_id}")


def _message_for(step: _Step, tenant_name: str) -> _Message:
    title = _DECISION_TITLES.get(step.decision, "An approval waits")
    return _Message(
        title=title,
        body=f"Tenant {tenant_name}. Request {step.request_id}.",
        tag=f"approval-{step.request_id}",
        topic=step.request_id.hex,
    )


async def _send_step(step: _Step) -> None:
    try:
        async with get_sessionmaker()() as db:
            name = await db.scalar(select(Tenant.name).where(Tenant.id == step.tenant_id))
            targets = await subscriptions_for(
                db,
                tenant_id=step.tenant_id,
                requester_sub=step.requester_sub,
                high_risk=step.high_risk,
            )
        message = _message_for(step, name or str(step.tenant_id))
        by_person: dict[str, list[PushTarget]] = defaultdict(list)
        for target in targets:
            by_person[target.operator_sub].append(target)
        for person, rows in by_person.items():
            # A decision replaces a message the person already has (same tag
            # and Topic), so the rate limit holds back only new requests.
            if step.decision != "pending" or _limit.admit(person):
                await _deliver_all(rows, message)
            elif _limit.hold(person):
                _spawn(_send_summary_later(person), name="web-push-summary")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning(
            "web_push_step_failed",
            approval_request_id=str(step.request_id),
            error=type(exc).__name__,
        )


async def _send_summary_later(person: str) -> None:
    try:
        await asyncio.sleep(_limit.wait_seconds(person))
        held = _limit.take_held(person)
        if held == 0:
            return
        async with get_sessionmaker()() as db:
            targets = await subscriptions_of_person(db, operator_sub=person)
        noun = "message" if held == 1 else "messages"
        message = _Message(
            title="Approvals changed",
            body=f"{held} more approval {noun}. Open the approvals page.",
            tag=_SUMMARY_TAG,
            topic=_SUMMARY_TOPIC,
        )
        await _deliver_all(targets, message)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.warning("web_push_summary_failed", operator_sub=person, error=type(exc).__name__)


async def _deliver_all(targets: list[PushTarget], message: _Message) -> None:
    settings = get_settings()
    private_key = settings.web_push_vapid_private_key
    base = settings.backplane_url.rstrip("/")
    subject = base if base.startswith("https://") else None
    payload = message.payload()
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, follow_redirects=False, transport=_transport
    ) as client:
        for target in targets:
            await _deliver(client, target, payload, message, private_key, subject)


async def _deliver(
    client: httpx.AsyncClient,
    target: PushTarget,
    payload: bytes,
    message: _Message,
    private_key: str,
    subject: str | None,
) -> None:
    service = push_service_name(target.endpoint)
    fields = {"subscription_id": str(target.id), "operator_sub": target.operator_sub}
    if service is None:
        _log.warning("web_push_endpoint_refused", **fields)
        return
    headers = {
        "Authorization": vapid_authorization(
            target.endpoint, private_key=private_key, subject=subject
        ),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(_TTL_SECONDS),
        "Urgency": "high",
        "Topic": message.topic,
    }
    try:
        body = encrypt(payload, p256dh=target.p256dh, auth=target.auth)
        response = await client.post(target.endpoint, content=body, headers=headers)
    except (httpx.HTTPError, ValueError) as exc:
        _log.warning("web_push_send_failed", service=service, error=type(exc).__name__, **fields)
        return
    status = response.status_code
    if status in (404, 410):
        async with get_sessionmaker()() as db, db.begin():
            await delete_gone(db, subscription_id=target.id)
        _log.info("web_push_subscription_gone", service=service, status=status, **fields)
    elif not 200 <= status < 300:
        _log.warning("web_push_send_refused", service=service, status=status, **fields)


def set_transport_for_testing(transport: httpx.AsyncBaseTransport | None) -> None:
    """Send through *transport* (a fake push service) instead of the network."""
    global _transport
    _transport = transport


def reset_for_testing(*, window: float = RATE_LIMIT_SECONDS) -> None:
    """Forget the rate limit state; use *window* seconds from now on."""
    global _limit
    _limit = _RateLimit(window)


async def wait_for_sends_for_testing() -> None:
    """Wait until every background send (and summary) has finished."""
    while _TASKS:
        await asyncio.gather(*list(_TASKS), return_exceptions=True)
