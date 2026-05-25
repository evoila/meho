# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Audit ``actor_sub`` persistence tests (G11.2-T2 #816).

Proves the audit write path records the RFC 8693 actor (acting agent) read
from the delegation contextvar: present when a delegated agent run is in
scope (``operator_sub``=human + ``actor_sub``=agent), ``NULL`` otherwise
(direct human request, or autonomous run where the agent is the subject).

Exercises the chassis write path
(:func:`meho_backplane.audit._write_audit_row`); the dispatcher
(``operations/_audit.py``) and MCP (``mcp/audit.py``) paths build the row
through the identical :func:`~meho_backplane.auth.delegation.resolve_actor_sub`
call, covered by ``test_auth_delegation.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
import structlog
from sqlalchemy import select

from meho_backplane.audit import _write_audit_row
from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Settings env this module needs (conftest provides the DB)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _read_row(audit_id: uuid.UUID) -> AuditLog:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.id == audit_id))
        return result.scalar_one()


def _write_kwargs(audit_id: uuid.UUID, operator_sub: str) -> dict[str, object]:
    return {
        "audit_id": audit_id,
        "operator_sub": operator_sub,
        "tenant_id": None,
        "target_id": None,
        "method": "DISPATCH",
        "path": "vmware.vm.list",
        "status_code": 200,
        "request_id": None,
        "duration_ms": 1.0,
        "payload": {},
    }


@pytest.mark.asyncio
async def test_delegated_run_records_human_and_agent() -> None:
    """A user-initiated agent run: operator_sub=human, actor_sub=agent."""
    structlog.contextvars.clear_contextvars()
    audit_id = uuid.uuid4()
    with actor_delegation("agent:incident-triage"):
        await _write_audit_row(**_write_kwargs(audit_id, "user-alice"))  # type: ignore[arg-type]
    row = await _read_row(audit_id)
    assert row.operator_sub == "user-alice"
    assert row.actor_sub == "agent:incident-triage"


@pytest.mark.asyncio
async def test_direct_human_request_records_null_actor() -> None:
    """No delegation context bound: actor_sub stays NULL."""
    structlog.contextvars.clear_contextvars()
    audit_id = uuid.uuid4()
    await _write_audit_row(**_write_kwargs(audit_id, "user-bob"))  # type: ignore[arg-type]
    row = await _read_row(audit_id)
    assert row.operator_sub == "user-bob"
    assert row.actor_sub is None


@pytest.mark.asyncio
async def test_autonomous_run_records_agent_subject_null_actor() -> None:
    """Autonomous (client_credentials) run: agent is the subject, no actor."""
    structlog.contextvars.clear_contextvars()
    audit_id = uuid.uuid4()
    # An autonomous run executes under the agent's own Operator and binds no
    # actor delegation, so the agent is operator_sub and actor_sub is NULL.
    await _write_audit_row(**_write_kwargs(audit_id, "agent:nightly-sweep"))  # type: ignore[arg-type]
    row = await _read_row(audit_id)
    assert row.operator_sub == "agent:nightly-sweep"
    assert row.actor_sub is None
