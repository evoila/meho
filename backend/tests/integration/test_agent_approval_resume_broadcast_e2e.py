# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Valkey delivery check for the agent approval-resume wait (#1501).

Initiative G0.20 (#1500), Task #1501. The agent runtime's resume-on-approval
path (:mod:`meho_backplane.agent.approval_wait`) blocks on a real per-tenant
Valkey stream via ``XREAD BLOCK`` until the operator's
``approval.{approved,rejected}`` decision event arrives. The unit suite
(:mod:`tests.test_agent_approval_resume`) stubs
:func:`~meho_backplane.broadcast.client.get_broadcast_blocking_client` with an
``AsyncMock`` whose ``xread`` replays a hand-built entry, so a *real* Valkey
delivery gap -- a wire-shape mismatch between
:func:`~meho_backplane.operations.approval_queue.publish_approval_event` (the
``XADD`` side) and :func:`~meho_backplane.agent.approval_wait.wait_for_approval_decision`
(the ``XREAD`` side), or a cursor bug -- would be invisible to it.

This integration test closes that gap. It boots a real Valkey container,
publishes a genuine ``approval.approved`` event through the production
publisher, and asserts the production wait observes it end to end across the
real stream. The publish ordering mirrors production: the wait parks on
``XREAD`` with the ``"$"`` tail cursor *before* the decision is published, so
the test exercises exactly the live-delivery sequence (park → operator decides
→ event lands → wait returns ``"approved"``).

The Postgres / app stack is deliberately not booted: the only substrate the
broadcast-delivery contract depends on is Valkey. The durable decision row +
the in-process resume re-dispatch are covered by the unit suite; the open
question this test answers is "does a real Valkey carry the publisher's event
to the agent's waiter unchanged?".
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from meho_backplane.agent.approval_wait import wait_for_approval_decision
from meho_backplane.broadcast.client import (
    dispose_broadcast_blocking_client,
    dispose_broadcast_client,
    get_broadcast_client,
    reset_broadcast_blocking_client_for_testing,
    reset_broadcast_client_for_testing,
)
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations.approval_queue import publish_approval_event
from meho_backplane.settings import get_settings

pytestmark = pytest.mark.asyncio

_TENANT: uuid.UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _docker_socket_present() -> bool:
    """Docker usable when the unix socket (or ``DOCKER_HOST``) is present.

    Mirrors :func:`tests.integration.conftest._docker_socket_present` so the
    skip condition stays uniform across the testcontainer suites.
    """
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_DOCKER_AVAILABLE: bool = _docker_socket_present()
_DOCKER_SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


@pytest.fixture(scope="module")
def valkey_url() -> Iterator[str]:
    """Boot a Valkey 8 container; yield ``redis://host:port``.

    Same image-override knob (``MEHO_TEST_VALKEY_IMAGE``) and module-scoped
    boot-once shape :mod:`tests.integration.test_broadcast_overrides_e2e` and
    :mod:`tests.integration.test_broadcast_load` use.
    """
    if not _DOCKER_AVAILABLE:
        pytest.skip(_DOCKER_SKIP_REASON)
    from testcontainers.redis import RedisContainer

    image = os.environ.get("MEHO_TEST_VALKEY_IMAGE", "valkey/valkey:8")
    with RedisContainer(image) as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}"


@pytest.fixture
async def broadcast_runtime(
    valkey_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[str]:
    """Pin ``BROADCAST_REDIS_URL`` at the container, reset both client caches.

    The publisher uses the fast client (``XADD``); the agent wait uses the
    blocking client (``XREAD BLOCK``). Both honour ``BROADCAST_REDIS_URL`` but
    are cached per-process, so both caches are reset here so the next getter on
    either path builds against the testcontainer. ``FLUSHDB`` wipes any prior
    stream state so the wait's ``"$"`` tail cursor starts from a clean stream.
    """
    monkeypatch.setenv("BROADCAST_REDIS_URL", valkey_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    reset_broadcast_blocking_client_for_testing()
    await get_broadcast_client().flushdb()
    try:
        yield valkey_url
    finally:
        await dispose_broadcast_client()
        await dispose_broadcast_blocking_client()
        get_settings.cache_clear()


def _approval_request(*, request_id: uuid.UUID) -> ApprovalRequest:
    """Build a minimal in-memory ``ApprovalRequest`` for the publisher.

    :func:`publish_approval_event` reads only ``id`` / ``connector_id`` /
    ``op_id`` off the request to shape the broadcast payload, so a row that is
    never persisted is sufficient for the delivery check -- the DB-backed
    decision row is the unit suite's concern, not this test's.
    """
    return ApprovalRequest(
        id=request_id,
        tenant_id=_TENANT,
        principal_sub="agent:reader",
        op_id="vault.kv.write",
        connector_id="vault-1.x",
        params_hash="deadbeef",
        proposed_effect={},
        status="approved",
        created_at=datetime.now(UTC),
    )


async def test_approve_event_reaches_agent_wait_over_real_valkey(
    broadcast_runtime: str,
) -> None:
    """#1501 AC3: a real ``approval.approved`` delivery resumes the agent wait.

    Park the production wait on the real stream first (so its ``"$"`` cursor
    tails from before the decision, exactly as production does -- the
    ``approval.pending`` event precedes the wait), then publish a genuine
    ``approval.approved`` event through the production publisher. The wait must
    observe it across the real Valkey and return ``"approved"``. A wire-shape
    or cursor mismatch that the broadcast-stubbing unit test cannot see fails
    here.
    """
    request_id = uuid.uuid4()

    wait_task = asyncio.create_task(
        wait_for_approval_decision(
            tenant_id=_TENANT,
            approval_request_id=request_id,
            timeout_seconds=20.0,
        )
    )
    # Let the waiter reach its first ``XREAD BLOCK`` so its ``"$"`` cursor is
    # anchored before we publish -- otherwise the publish could land in the
    # gap between task creation and the first read.
    await asyncio.sleep(0.5)

    await publish_approval_event(
        tenant_id=_TENANT,
        request=_approval_request(request_id=request_id),
        decision="approved",
        principal_sub="op-human",
        audit_id=uuid.uuid4(),
    )

    decision = await asyncio.wait_for(wait_task, timeout=20.0)
    assert decision == "approved"


async def test_other_request_decision_does_not_resume_then_times_out(
    broadcast_runtime: str,
) -> None:
    """A real decision for a *different* request id does not satisfy the wait.

    Proves the request-id filter holds across the real wire (not just against
    the stubbed entry): an unrelated tenant-mate's ``approval.approved`` lands
    in the same stream but the wait keeps blocking and ends in ``"timeout"``
    once its (short) cap lapses. Guards against a delivery path that matched on
    stream membership alone.
    """
    waited_for = uuid.uuid4()
    other_request = uuid.uuid4()

    wait_task = asyncio.create_task(
        wait_for_approval_decision(
            tenant_id=_TENANT,
            approval_request_id=waited_for,
            timeout_seconds=2.0,
        )
    )
    await asyncio.sleep(0.5)

    await publish_approval_event(
        tenant_id=_TENANT,
        request=_approval_request(request_id=other_request),
        decision="approved",
        principal_sub="op-human",
        audit_id=uuid.uuid4(),
    )

    decision = await asyncio.wait_for(wait_task, timeout=10.0)
    assert decision == "timeout"
