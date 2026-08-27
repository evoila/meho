# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Gate-matrix tests for the service-principal safety_level + standing grants (#3151 / #3152).

The non-agent policy gate is exercised through
:func:`~meho_backplane.operations.composite.enforce_subop_policy` (which
builds a composite descriptor and calls ``policy_gate`` with the
``connector_id`` the grant lookup needs). The matrix:

* **service principal x safety_level** — a mutating ``caution`` / a
  ``dangerous`` / a ``requires_approval`` op **parks** without a grant
  (#3152 tightening); a ``safe`` read still auto-executes.
* **service principal x standing grant** — a live matching grant clears
  the gate (auto-execute) and writes a ``approval.decision`` /
  ``auto-approved`` audit row carrying the ``grant_id`` (#3151).
* **expiry / revocation respected at dispatch** — an expired or revoked
  grant does not clear the gate.
* **operator-interactive USER is unchanged** — a human ``USER`` operator
  never consults safety_level or a standing grant.

Plus unit coverage of the two classification helpers.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.broadcast.publisher as _publisher
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor, ServicePrincipalGrant
from meho_backplane.operations._validate import _is_mutating, _service_safety_gate_reason
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.settings import get_settings

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000032a4")
_CONNECTOR = "vmware-rest-9.0"
_PRINCIPAL = "svc:deploy-bot"
_OP = "vmware.composite.vm.create"
_PARAMS = {"name": "vm-under-test", "guest_OS": "OTHER"}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _noop_broadcast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence the broadcast publisher (park + grant-use both publish)."""

    async def _noop(*_a: object, **_kw: object) -> None:
        pass

    monkeypatch.setattr(_publisher, "publish_event", _noop)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


def _operator(*, principal_kind: PrincipalKind, sub: str = _PRINCIPAL) -> Operator:
    return Operator(
        sub=sub,
        name="svc",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


async def _seed_grant(
    *,
    op_id: str = _OP,
    connector_id: str = _CONNECTOR,
    target_id: uuid.UUID | None = None,
    principal_sub: str = _PRINCIPAL,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> uuid.UUID:
    grant_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            ServicePrincipalGrant(
                id=grant_id,
                tenant_id=_TENANT_ID,
                principal_sub=principal_sub,
                op_id=op_id,
                connector_id=connector_id,
                target_id=target_id,
                reason="unattended build",
                created_by_sub="op-admin",
                expires_at=expires_at,
                revoked_at=revoked_at,
            )
        )
        await s.commit()
    return grant_id


async def _grant_use_rows(grant_id: uuid.UUID | None = None) -> list[AuditLog]:
    async with get_sessionmaker()() as s:
        result = await s.execute(
            select(AuditLog).where(
                AuditLog.method == "APPROVAL",
                AuditLog.path == "approval.decision",
            )
        )
        rows = [
            r
            for r in result.scalars().all()
            if r.payload.get("decision") == "auto-approved"
            and (grant_id is None or r.payload.get("grant_id") == str(grant_id))
        ]
    return rows


# ---------------------------------------------------------------------------
# safety_level tightening (#3152) — service principal parks without a grant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "safety_level, requires_approval",
    [
        ("caution", False),  # mutating caution — new #3152 tightening
        ("dangerous", False),  # dangerous — parks always
        ("safe", True),  # requires_approval — unchanged
    ],
)
async def test_service_principal_parks_without_grant(
    safety_level: str, requires_approval: bool
) -> None:
    """A gated op parks (awaiting_approval) for a service principal absent a grant."""
    result = await enforce_subop_policy(
        operator=_operator(principal_kind=PrincipalKind.SERVICE),
        connector_id=_CONNECTOR,
        op_id=_OP,
        safety_level=safety_level,
        requires_approval=requires_approval,
        target=None,
        params=_PARAMS,
    )
    assert result is not None
    assert result.status == "awaiting_approval"
    assert not await _grant_use_rows()


@pytest.mark.asyncio
async def test_service_principal_safe_read_auto_executes() -> None:
    """A ``safe`` read still auto-executes for a service principal (unchanged)."""
    result = await enforce_subop_policy(
        operator=_operator(principal_kind=PrincipalKind.SERVICE),
        connector_id=_CONNECTOR,
        op_id="GET:/vcenter/vm",
        safety_level="safe",
        requires_approval=False,
        target=None,
        params={"filter.names": ["vm-under-test"]},
    )
    assert result is None


# ---------------------------------------------------------------------------
# standing grant clears the gate + records grant-use audit (#3151)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "safety_level, requires_approval",
    [("caution", False), ("dangerous", False), ("safe", True)],
)
async def test_live_grant_auto_approves_and_audits(
    safety_level: str, requires_approval: bool
) -> None:
    """A live matching grant clears the gate and writes a grant-use audit row."""
    grant_id = await _seed_grant()
    result = await enforce_subop_policy(
        operator=_operator(principal_kind=PrincipalKind.SERVICE),
        connector_id=_CONNECTOR,
        op_id=_OP,
        safety_level=safety_level,
        requires_approval=requires_approval,
        target=None,
        params=_PARAMS,
    )
    # Gate cleared → seam returns None (proceed to execute).
    assert result is None

    rows = await _grant_use_rows(grant_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.payload["reviewed_by"] == f"grant:{grant_id}"
    assert row.payload["op_id"] == _OP
    assert row.payload["connector_id"] == _CONNECTOR
    assert row.payload["principal_sub"] == _PRINCIPAL
    assert f"standing grant {grant_id}" in row.payload["reason"]
    assert row.status_code == 200


# ---------------------------------------------------------------------------
# expiry / revocation respected at dispatch time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_grant_does_not_clear_gate() -> None:
    """An expired grant does not auto-approve — the op parks."""
    await _seed_grant(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    result = await enforce_subop_policy(
        operator=_operator(principal_kind=PrincipalKind.SERVICE),
        connector_id=_CONNECTOR,
        op_id=_OP,
        safety_level="caution",
        requires_approval=False,
        target=None,
        params=_PARAMS,
    )
    assert result is not None
    assert result.status == "awaiting_approval"
    assert not await _grant_use_rows()


@pytest.mark.asyncio
async def test_revoked_grant_does_not_clear_gate() -> None:
    """A revoked grant does not auto-approve — the op parks."""
    await _seed_grant(revoked_at=datetime.now(UTC) - timedelta(minutes=1))
    result = await enforce_subop_policy(
        operator=_operator(principal_kind=PrincipalKind.SERVICE),
        connector_id=_CONNECTOR,
        op_id=_OP,
        safety_level="caution",
        requires_approval=False,
        target=None,
        params=_PARAMS,
    )
    assert result is not None
    assert result.status == "awaiting_approval"
    assert not await _grant_use_rows()


@pytest.mark.asyncio
async def test_grant_target_scope_is_exact() -> None:
    """A grant scoped to one target does not cover a targetless dispatch."""
    scoped_target = uuid.uuid4()
    await _seed_grant(target_id=scoped_target)

    # Targetless dispatch: the targeted grant must NOT match → parks.
    result = await enforce_subop_policy(
        operator=_operator(principal_kind=PrincipalKind.SERVICE),
        connector_id=_CONNECTOR,
        op_id=_OP,
        safety_level="caution",
        requires_approval=False,
        target=None,
        params=_PARAMS,
    )
    assert result is not None
    assert result.status == "awaiting_approval"

    # Dispatch to the scoped target: the grant matches → auto-executes.
    matched = await enforce_subop_policy(
        operator=_operator(principal_kind=PrincipalKind.SERVICE),
        connector_id=_CONNECTOR,
        op_id=_OP,
        safety_level="caution",
        requires_approval=False,
        target=SimpleNamespace(id=scoped_target, name="dc-target"),
        params=_PARAMS,
    )
    assert matched is None


# ---------------------------------------------------------------------------
# operator-interactive USER is unchanged (their own approver)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_principal_caution_mutation_auto_executes() -> None:
    """A human USER operator is NOT subject to the safety_level tightening."""
    result = await enforce_subop_policy(
        operator=_operator(principal_kind=PrincipalKind.USER, sub="human-op"),
        connector_id=_CONNECTOR,
        op_id=_OP,
        safety_level="caution",
        requires_approval=False,
        target=None,
        params=_PARAMS,
    )
    # USER keeps the v0.2 default-allow: a caution op with requires_approval=False
    # auto-executes (the seam returns None). No standing grant is consulted.
    assert result is None


@pytest.mark.asyncio
async def test_user_principal_requires_approval_parks_and_ignores_grant() -> None:
    """A USER hitting requires_approval parks even with a grant on their own sub.

    A standing grant is enforced only for service principals; a human
    operator keeps the queue-on-approval contract (they are their own
    approver via a colleague, never via a grant).
    """
    await _seed_grant(principal_sub="human-op")
    result = await enforce_subop_policy(
        operator=_operator(principal_kind=PrincipalKind.USER, sub="human-op"),
        connector_id=_CONNECTOR,
        op_id=_OP,
        safety_level="safe",
        requires_approval=True,
        target=None,
        params=_PARAMS,
    )
    assert result is not None
    assert result.status == "awaiting_approval"
    # The grant was NOT consulted → no auto-approved audit row.
    assert not await _grant_use_rows()


# ---------------------------------------------------------------------------
# classification helper unit tests (method-based branch)
# ---------------------------------------------------------------------------


def _descriptor(*, method: str | None, safety_level: str) -> EndpointDescriptor:
    return EndpointDescriptor(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id="op",
        source_kind="ingested" if method else "composite",
        method=method,
        safety_level=safety_level,
    )


@pytest.mark.parametrize(
    "method, safety_level, expected",
    [
        ("GET", "safe", False),
        ("HEAD", "safe", False),
        ("POST", "caution", True),
        ("PUT", "caution", True),
        ("DELETE", "dangerous", True),
        (None, "safe", False),  # typed/composite: safety_level stands in
        (None, "caution", True),
        (None, "dangerous", True),
    ],
)
def test_is_mutating(method: str | None, safety_level: str, expected: bool) -> None:
    assert _is_mutating(_descriptor(method=method, safety_level=safety_level)) is expected


@pytest.mark.parametrize(
    "method, safety_level, gated",
    [
        ("POST", "caution", True),  # mutating caution parks
        ("GET", "caution", False),  # a caution *read* does not park
        ("DELETE", "dangerous", True),  # dangerous parks always
        ("GET", "safe", False),  # safe read unchanged
    ],
)
def test_service_safety_gate_reason(method: str, safety_level: str, gated: bool) -> None:
    reason = _service_safety_gate_reason(_descriptor(method=method, safety_level=safety_level))
    assert (reason is not None) is gated
