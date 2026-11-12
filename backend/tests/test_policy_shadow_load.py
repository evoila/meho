# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Load check: what the policy evaluator in shadow mode adds to each call.

20,000 operations are registered. For a person, an agent and a service
login, the policy gate runs on calls of those operations, once with
``POLICY_ENGINE_SHADOW`` off and once on. The test prints the time per
call and the time the shadow adds. The pull request reports the numbers.

Opt-in: ``MEHO_RUN_LOAD_TESTS=1 uv run pytest -m load tests/test_policy_shadow_load.py -s``.
"""

from __future__ import annotations

import os
import random
import statistics
import time
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    EndpointDescriptor,
    PermissionVerdict,
    ServicePrincipalGrant,
)
from meho_backplane.operations._validate import policy_gate
from meho_backplane.settings import get_settings
from tests._sandbox_mode_helpers import seed_grant_row

pytestmark = [
    pytest.mark.load,
    pytest.mark.skipif(
        os.environ.get("MEHO_RUN_LOAD_TESTS") != "1",
        reason="load check; set MEHO_RUN_LOAD_TESTS=1 and pass -m load",
    ),
]

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000016042")
_CONNECTOR = "bench-rest-1.0"
_OPS = 20_000
_CALLS = 600
_LEVELS = ("safe", "caution", "dangerous", "destructive")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _register() -> list[EndpointDescriptor]:
    async with get_sessionmaker()() as session:
        session.add_all(
            EndpointDescriptor(
                product="bench",
                version="1.0",
                impl_id="bench-rest",
                op_id=f"POST:/bench/{i}",
                method="POST" if i % 2 else "GET",
                path=f"/bench/{i}",
                source_kind="ingested",
                safety_level=_LEVELS[i % 4],
                requires_approval=i % 7 == 0,
                parameter_schema={},
                summary=f"bench op {i}",
            )
            for i in range(_OPS)
        )
        await session.commit()
    for i in range(0, _OPS, 100):
        await seed_grant_row(
            model=ServicePrincipalGrant,
            tenant_id=_TENANT,
            principal_sub="svc-bench",
            op_id=f"POST:/bench/{i}",
            connector_id=_CONNECTOR,
        )
    async with get_sessionmaker()() as session:
        session.add_all(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub="agent-bench",
                op_pattern=f"POST:/bench/{i}*",
                verdict=PermissionVerdict.AUTO_EXECUTE.value,
                created_by_sub="op-admin",
            )
            for i in range(50)
        )
        await session.commit()
        rows = (await session.execute(select(EndpointDescriptor))).scalars().all()
    return list(rows)


def _operator(kind: PrincipalKind, sub: str) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="<jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _time(operator: Operator, rows: list[EndpointDescriptor]) -> list[float]:
    times: list[float] = []
    for row in rows:
        start = time.perf_counter()
        await policy_gate(operator=operator, descriptor=row, target=None, connector_id=_CONNECTOR)
        times.append((time.perf_counter() - start) * 1000)
    return times


def _set_shadow(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setenv("POLICY_ENGINE_SHADOW", "true" if on else "false")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_shadow_added_time_with_20000_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = await _register()
    assert len(rows) >= _OPS
    sample = random.Random(6042).sample(rows, _CALLS)
    callers = {
        "person": _operator(PrincipalKind.USER, "person-bench"),
        "agent": _operator(PrincipalKind.AGENT, "agent-bench"),
        "service": _operator(PrincipalKind.SERVICE, "svc-bench"),
    }
    report: list[str] = []
    with capture_logs() as logs:
        for name, operator in callers.items():
            _set_shadow(monkeypatch, False)
            await _time(operator, sample[:50])
            off = await _time(operator, sample)
            _set_shadow(monkeypatch, True)
            await _time(operator, sample[:50])
            on = await _time(operator, sample)
            added = statistics.median(on) - statistics.median(off)
            report.append(
                f"{name}: off median {statistics.median(off):.3f} ms, "
                f"on median {statistics.median(on):.3f} ms, added {added:.3f} ms; "
                f"off p95 {statistics.quantiles(off, n=20)[-1]:.3f} ms, "
                f"on p95 {statistics.quantiles(on, n=20)[-1]:.3f} ms"
            )
    print("\npolicy shadow load check, 20,000 registered operations, per call:")
    print("\n".join(report))
    assert [e for e in logs if e.get("event") == "policy_shadow_mismatch"] == []
