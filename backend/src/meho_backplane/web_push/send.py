# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Send push messages about requests that wait for approval.

:func:`notify_approval_step` is called by
:func:`~meho_backplane.operations.approval_queue.record_approval_step_event`,
which every call site runs only **after** the transaction with the audit row
has committed. It starts the sending in the background and returns at once,
so the request never waits for a push service.

* A new request (``pending``): "An approval waits" to every browser of the
  people who may approve it in that tenant, by the same rule as the approve
  (:func:`~meho_backplane.web_push.recipients.subscriptions_for`).
* A decision (``approved``, ``rejected``, ``expired``, ``withdrawn``): the
  result, with the same notification tag, so it replaces the first message
  on the device. The ``Topic`` header replaces it at the push service too
  when the first one was not delivered yet. The steps of one request are
  sent in the order they happened: a step waits until the step before it
  was sent, so a late first message never replaces the result.
* The message holds the words, the tenant name, the request id and the
  address of the approvals page. Never the request's operation, parameters
  or target. It is encrypted for each browser
  (:func:`~meho_backplane.web_push.encryption.encrypt`).
* A person gets at most one "An approval waits" message per tenant per
  :data:`RATE_LIMIT_SECONDS`. The ones held back in that time become one
  summary at its end, sent only to the person's browsers of that tenant.
  A decision goes out at once only to a person who got
  the "An approval waits" message of that request, because it replaces that
  message. For everyone else a decision counts against the same limit, so a
  burst of decisions (for example many requests expiring at once) does not
  flood a phone.
* A push service that answers 404 or 410 (the browser dropped the
  subscription) gets its row deleted.
* Messages go only to the browser makers' push services (checked again here,
  not only when the subscription was stored), without following redirects.
  Logs name the row id, the person, the push service and the status. Never
  the endpoint or a key.

The rate limit and the order are kept in memory, per process.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import OrderedDict, defaultdict
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, Tenant
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
#: At most this many shown requests are remembered per person (oldest go first).
_SHOWN_LIMIT = 1000
#: How long a push service keeps an undelivered message (seconds).
_TTL_SECONDS = 24 * 60 * 60
_TIMEOUT = httpx.Timeout(10.0)
_SUMMARY_TAG = "approval-summary"
_SUMMARY_TOPIC = "approvalsummary"
_DECISION_TITLES = {
    "approved": "Approval approved",
    "rejected": "Approval rejected",
    "expired": "Approval expired",
    "withdrawn": "Approval withdrawn",
}

_TASKS: set[asyncio.Task[None]] = set()
#: The newest send task of each request, so its next step waits for it.
_LAST_STEP: dict[uuid.UUID, asyncio.Task[None]] = {}
_transport: httpx.AsyncBaseTransport | None = None


#: A person in one tenant. The rate limit and the summary count per pair.
_Person = tuple[str, uuid.UUID]


@dataclass(frozen=True)
class _Step:
    request: ApprovalRequest
    decision: str

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.request.tenant_id

    @property
    def request_id(self) -> uuid.UUID:
        return self.request.id


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
    """One message per person and tenant per window; count the held-back ones.

    Also remembers, per person and tenant, the requests whose "An approval waits"
    message was sent, so their decision can replace it at once. At most
    :data:`_SHOWN_LIMIT` per person; the oldest are forgotten first.
    """

    def __init__(self, window: float) -> None:
        self.window = window
        self._last: dict[_Person, float] = {}
        self._held: dict[_Person, int] = {}
        self._shown: dict[_Person, OrderedDict[uuid.UUID, None]] = {}

    def remember_shown(self, person: _Person, request_id: uuid.UUID) -> None:
        shown = self._shown.setdefault(person, OrderedDict())
        shown[request_id] = None
        while len(shown) > _SHOWN_LIMIT:
            shown.popitem(last=False)

    def take_shown(self, person: _Person, request_id: uuid.UUID) -> bool:
        """``True`` (and forget it) when the person got this request's first message."""
        shown = self._shown.get(person)
        if shown is None or request_id not in shown:
            return False
        del shown[request_id]
        if not shown:
            del self._shown[person]
        return True

    def admit(self, person: _Person) -> bool:
        now = time.monotonic()
        last = self._last.get(person)
        if last is not None and now - last < self.window:
            return False
        self._last[person] = now
        return True

    def hold(self, person: _Person) -> bool:
        """Count one held message; ``True`` when it is the first of the window."""
        self._held[person] = self._held.get(person, 0) + 1
        return self._held[person] == 1

    def wait_seconds(self, person: _Person) -> float:
        last = self._last.get(person, 0.0)
        return max(0.0, last + self.window - time.monotonic())

    def take_held(self, person: _Person) -> int:
        self._last[person] = time.monotonic()
        return self._held.pop(person, 0)


_limit = _RateLimit(RATE_LIMIT_SECONDS)


def _spawn(coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


def _snapshot(tenant_id: uuid.UUID, request: ApprovalRequest) -> ApprovalRequest:
    """A copy of the fields the approve rule reads, kept out of any session."""
    return ApprovalRequest(
        id=request.id,
        tenant_id=tenant_id,
        principal_sub=request.principal_sub,
        op_id=request.op_id,
        params=request.params,
        proposed_effect=request.proposed_effect,
        rights_change=request.rights_change,
        status=request.status,
    )


def notify_approval_step(*, tenant_id: uuid.UUID, request: ApprovalRequest, decision: str) -> None:
    """Start sending the push messages for one approval step; never raises.

    Call only after the transaction with the audit row committed. Does
    nothing while the push feature is off (``WEB_PUSH_ENABLED``, or no
    usable push key). The send starts after the send of the request's
    step before, so the steps arrive in the order of these calls.
    """
    try:
        if push_public_key() is None:
            return
        step = _Step(request=_snapshot(tenant_id, request), decision=decision)
    except Exception as exc:
        _log.warning("web_push_step_skipped", error=type(exc).__name__)
        return
    request_id = step.request_id
    task = _spawn(_send_step(step, _LAST_STEP.get(request_id)), name=f"web-push-{request_id}")
    _LAST_STEP[request_id] = task

    def _forget(done: asyncio.Task[None]) -> None:
        if _LAST_STEP.get(request_id) is done:
            del _LAST_STEP[request_id]

    task.add_done_callback(_forget)


def _message_for(step: _Step, tenant_name: str) -> _Message:
    title = _DECISION_TITLES.get(step.decision, "An approval waits")
    return _Message(
        title=title,
        body=f"Tenant {tenant_name}. Request {step.request_id}.",
        tag=f"approval-{step.request_id}",
        topic=step.request_id.hex,
    )


async def _send_step(step: _Step, before: asyncio.Task[None] | None = None) -> None:
    if before is not None:
        # asyncio.wait never raises for the task it waits on.
        await asyncio.wait({before})
    try:
        async with get_sessionmaker()() as db:
            name = await db.scalar(select(Tenant.name).where(Tenant.id == step.tenant_id))
            targets = await subscriptions_for(db, request=step.request)
        message = _message_for(step, name or str(step.tenant_id))
        by_person: dict[_Person, list[PushTarget]] = defaultdict(list)
        for target in targets:
            by_person[(target.operator_sub, step.tenant_id)].append(target)
        for person, rows in by_person.items():
            # A decision for a request whose first message this person got
            # replaces that message (same tag and Topic), so it goes out at
            # once. Every other message counts against the rate limit.
            pending = step.decision == "pending"
            if not pending and _limit.take_shown(person, step.request_id):
                await _deliver_all(rows, message)
            elif _limit.admit(person):
                if pending:
                    _limit.remember_shown(person, step.request_id)
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


async def _send_summary_later(person: _Person) -> None:
    operator_sub, tenant_id = person
    try:
        await asyncio.sleep(_limit.wait_seconds(person))
        held = _limit.take_held(person)
        if held == 0:
            return
        async with get_sessionmaker()() as db:
            targets = await subscriptions_of_person(
                db, operator_sub=operator_sub, tenant_id=tenant_id
            )
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
        _log.warning(
            "web_push_summary_failed",
            operator_sub=operator_sub,
            tenant_id=str(tenant_id),
            error=type(exc).__name__,
        )


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
    _LAST_STEP.clear()


async def wait_for_sends_for_testing() -> None:
    """Wait until every background send (and summary) has finished."""
    while _TASKS:
        await asyncio.gather(*list(_TASKS), return_exceptions=True)
