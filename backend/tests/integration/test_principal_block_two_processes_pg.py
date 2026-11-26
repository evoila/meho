# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres check: a block of an agent name reaches a second MEHO process in time.

Process 1 (this test) blocks ``agent:<name>`` through the REST route and
later removes the block. Process 2 is a real second Python process with
its own app and its own copy of the block list
(:mod:`tests.integration._block_second_process`). It reads with the
agent's token all the time. It must refuse the token within the refresh
time of the block list (``BLOCK_CACHE_SECONDS``) after the block answer,
keep refusing it, and accept it again within the refresh time after the
unblock answer. The test fails when process 2 never sees the block.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from httpx import ASGITransport

from meho_backplane.auth.operator import TenantRole
from meho_backplane.auth.principal_block import BLOCK_CACHE_SECONDS, reset_block_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPrincipal
from meho_backplane.main import app
from tests._oidc_jwt_helpers import (
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from tests._time_limits import check_time_limit

from .conftest import pg_engine  # noqa: F401 — pytest-discovered fixture

pytestmark = [pytest.mark.asyncio, pytest.mark.slow_runner_tolerant]

#: One of the two tenants ``pg_engine`` seeds on entry.
_TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
_AGENT = "agent:two-process-sample"
_AGENT_SUB = "9a4c0e52-0000-4000-8000-0000000b10c2"
#: A read that was already on its way when the list changed may take this long.
_ONE_READ = 1.0
#: How long the test waits for a line of process 2. Long enough for a busy
#: runner: only :func:`check_time_limit` checks the refresh time.
_WAIT_FOR_LINE = 180.0
_BACKEND = Path(__file__).resolve().parents[2]


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


async def _seed_agent() -> None:
    async with get_sessionmaker()() as session:
        session.add(
            AgentPrincipal(
                tenant_id=_TENANT,
                name="two-process-sample",
                keycloak_client_id=_AGENT,
                keycloak_internal_id="two-process-sample-internal-id",
                owner_sub="owner-person",
                created_by_sub="owner-person",
            )
        )
        await session.commit()


async def _next_line(process: asyncio.subprocess.Process, timeout: float) -> tuple[int, float]:
    """The next ``answer <status> <time>`` line of process 2; log lines are skipped."""
    assert process.stdout is not None
    stop_at = time.monotonic() + timeout
    while True:
        left = stop_at - time.monotonic()
        if left <= 0:
            raise TimeoutError
        raw = await asyncio.wait_for(process.stdout.readline(), timeout=left)
        assert raw, "process 2 ended without an answer"
        words = raw.decode().split()
        if len(words) == 3 and words[0] == "answer":
            return int(words[1]), float(words[2])


@pytest.mark.skipif(
    not _docker_socket_present(),
    reason="Docker socket unavailable in this sandbox; runs in CI where Postgres is provisioned.",
)
async def test_block_and_unblock_by_name_reach_a_second_process_within_the_refresh_time(
    pg_engine: None,  # noqa: F811 — the fixture imported above
) -> None:
    await _seed_agent()
    reset_block_cache()
    key = make_rsa_keypair("kid-two-processes")
    agent = mint_token(
        key,
        sub=_AGENT_SUB,
        name=None,
        email=None,
        tenant_id=str(_TENANT),
        tenant_role=TenantRole.OPERATOR.value,
        principal_kind="agent",
        extra_claims={"azp": _AGENT, "jti": str(uuid.uuid4())},
    )
    admin = mint_token(
        key, sub="admin-person", tenant_id=str(_TENANT), tenant_role=TenantRole.TENANT_ADMIN.value
    )
    env = {
        **os.environ,
        "SECOND_PROCESS_TOKEN": agent,
        "SECOND_PROCESS_JWKS": json.dumps(public_jwks(key)),
        "SECOND_PROCESS_SECONDS": "600",
        "PYTHONPATH": str(_BACKEND),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.integration._block_second_process",
        cwd=_BACKEND,
        env=env,
        stdout=asyncio.subprocess.PIPE,
    )
    headers = {"Authorization": f"Bearer {admin}"}
    try:
        assert (await _next_line(process, _WAIT_FOR_LINE))[0] == 200
        with respx.mock(assert_all_called=False) as router:
            mock_discovery_and_jwks(router, public_jwks(key))
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app), base_url="https://testserver"
            ) as ac:
                blocked = await ac.post(
                    "/api/v1/principals/blocked",
                    json={"sub": _AGENT, "reason": "stop it"},
                    headers=headers,
                )
                blocked_at = time.time()
                assert blocked.status_code == 201, blocked.text

                status, refused_at = await _next_line(process, _WAIT_FOR_LINE)
                assert status == 403
                check_time_limit(
                    refused_at - blocked_at,
                    BLOCK_CACHE_SECONDS + _ONE_READ,
                    "process 2 refused the agent later than the refresh time",
                )
                with pytest.raises(TimeoutError):
                    await _next_line(process, BLOCK_CACHE_SECONDS + 1)

                removed = await ac.delete(f"/api/v1/principals/blocked/{_AGENT}", headers=headers)
                removed_at = time.time()
                assert removed.status_code == 200, removed.text

                status, works_at = await _next_line(process, _WAIT_FOR_LINE)
                assert status == 200
                check_time_limit(
                    works_at - removed_at,
                    BLOCK_CACHE_SECONDS + _ONE_READ,
                    "process 2 took the agent again later than the refresh time",
                )
    finally:
        process.kill()
        await process.wait()
