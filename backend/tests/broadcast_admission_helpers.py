# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared helpers for tests that publish announcements (#3736).

Publishing now needs a trusted agent identity. Wire-shape tests that are
not about admission patch the admission check with
:func:`patch_agent_admission`, so a plain test operator is admitted as a
registered agent principal. Admission itself is tested for real in
``test_broadcast_agent_admission.py``.
"""

from __future__ import annotations

import itertools
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from meho_backplane.auth.agent_admission import AgentAdmission
from meho_backplane.auth.operator import Operator
from meho_backplane.broadcast import AgentAnnouncementEvent
from meho_backplane.broadcast.archive_sequence import next_announcement_seq
from meho_backplane.broadcast.publisher import _announcement_row
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal, AgentRun

__all__ = [
    "FakeStreamClient",
    "fake_admission",
    "fake_stream_write",
    "patch_agent_admission",
    "seed_agent_principal",
    "seed_agent_run",
    "seed_announcement",
    "trusted_event",
]

#: The source module is patched too: the shared ``isolated_registry``
#: fixture reloads the MCP tool module, and a reload after this patch
#: re-imports the name from the (patched) source.
_ADMISSION_TARGETS = (
    "meho_backplane.auth.agent_admission.resolve_agent_admission",
    "meho_backplane.mcp.tools.broadcast.resolve_agent_admission",
    "meho_backplane.api.v1.broadcast.resolve_agent_admission",
)


def fake_admission(operator: Operator) -> AgentAdmission:
    """Admit *operator* as a registered agent principal named after its ``sub``."""
    return AgentAdmission(
        source="agent_principal",
        owner_sub=f"agent:{operator.sub}",
        principal_sub=operator.sub,
        actor_sub=None,
        run_id=None,
        work_ref=None,
    )


def patch_agent_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admit every caller as an agent principal on the MCP and REST paths."""

    async def _admit(operator: Operator) -> AgentAdmission:
        return fake_admission(operator)

    for target in _ADMISSION_TARGETS:
        monkeypatch.setattr(target, AsyncMock(side_effect=_admit))


def trusted_event(
    *,
    tenant_id: UUID,
    principal_sub: str = "op-test",
    activity: str = "investigating",
    phase: str = "start",
    claim_id: UUID | None = None,
    owner_sub: str | None = None,
    ts: datetime | None = None,
    **fields: Any,
) -> AgentAnnouncementEvent:
    """Build a trusted claim event, the only kind the publisher accepts."""
    return AgentAnnouncementEvent.model_validate(
        {
            "tenant_id": tenant_id,
            "principal_sub": principal_sub,
            "activity": activity,
            "phase": phase,
            "ts": ts or datetime.now(UTC),
            "claim_id": claim_id or uuid4(),
            "owner_sub": owner_sub or f"agent:{principal_sub}",
            "admission": "agent_principal",
            **fields,
        }
    )


async def seed_announcement(tenant_id: UUID, **fields: Any) -> tuple[AgentAnnouncementEvent, int]:
    """Store one archived announcement with the tenant's next sequence number.

    Takes the same fields as :func:`trusted_event`. Returns the event and
    its ``seq``. Writes the archive only, like a publish whose stream
    wake-up was never needed.
    """
    event = trusted_event(tenant_id=tenant_id, **fields)
    async with get_sessionmaker()() as session:
        seq = await next_announcement_seq(session, tenant_id)
        session.add(_announcement_row(event, None, seq))
        await session.commit()
    return event, seq


async def seed_agent_run(
    tenant_id: UUID,
    *,
    identity_sub: str,
    status: str = "running",
    work_ref: str | None = None,
    identity_act: str | None = None,
) -> UUID:
    """Insert one ``agent_run`` row, as the hosted-agent runtime would."""
    run_id = uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            AgentRun(
                id=run_id,
                tenant_id=tenant_id,
                identity_sub=identity_sub,
                identity_act=identity_act,
                trigger="direct",
                model_tier="standard",
                status=status,
                work_ref=work_ref,
            )
        )
        await session.commit()
    return run_id


async def seed_agent_principal(tenant_id: UUID, client_id: str, *, revoked: bool = False) -> None:
    """Insert one registered ``agent_principal`` row for *client_id*."""
    name = client_id.removeprefix("agent:")
    async with get_sessionmaker()() as session:
        session.add(
            AgentPrincipal(
                id=uuid4(),
                tenant_id=tenant_id,
                name=name,
                keycloak_client_id=client_id,
                keycloak_internal_id=f"kc-{uuid4().hex[:12]}",
                owner_sub="op-admin",
                revoked=revoked,
                created_by_sub="op-admin",
            )
        )
        await session.commit()


class FakeStreamClient:
    """In-memory stand-in for the Valkey client the publisher writes to.

    ``xadd`` is an ``AsyncMock`` that hands out increasing entry ids, so
    tests keep asserting on stream writes. ``register_script`` returns the
    publisher's deliver-once script as a coroutine: it keeps the per-event
    delivery markers in ``markers`` and makes the write through ``xadd``,
    like the real script does with ``XADD``. Set ``lose_next_response`` to
    make one script call finish on the "server" side and still raise, like
    a reply that never reaches the client.
    """

    def __init__(self, xadd: Callable[..., Awaitable[Any]] | None = None) -> None:
        counter = itertools.count(1)
        self.xadd = xadd or AsyncMock(
            side_effect=lambda *_a, **_k: f"1700000000000-{next(counter)}"
        )
        self.markers: dict[str, str] = {}
        self.lose_next_response = False

    def register_script(self, _script: str) -> Callable[..., Awaitable[list[Any]]]:
        return self._deliver_once

    async def _deliver_once(self, keys: Sequence[str], args: Sequence[Any]) -> list[Any]:
        stream_key, marker_key = keys
        payload, maxlen, _ttl_seconds = args
        known = self.markers.get(marker_key)
        if known is not None:
            return [0, known]
        entry_id = str(
            await self.xadd(stream_key, {"event": payload}, maxlen=int(maxlen), approximate=True)
        )
        self.markers[marker_key] = entry_id
        if self.lose_next_response:
            self.lose_next_response = False
            raise RedisTimeoutError("reply lost after the stream write")
        return [1, entry_id]


@contextmanager
def fake_stream_write(client: Any, xadd: AsyncMock) -> Iterator[AsyncMock]:
    """Patch ``xadd`` and the deliver-once script on a real client object.

    For tests that build the real (never connected) broadcast client and
    only want to fake the stream write.
    """
    fake = FakeStreamClient(xadd=xadd)
    with (
        patch.object(client, "xadd", new=xadd),
        patch.object(client, "register_script", new=fake.register_script),
    ):
        yield xadd
