# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Call-time tests for the service-principal read scope (#4992).

A service principal runs every ``safe`` operation at once, on every target
of its tenant. A person can set a read scope: a list of targets. Then:

* a ``safe`` call on a target inside the scope runs; outside it is refused
  with the stable error code ``read_scope_denied`` and one audit row;
* a selector entry matches a target registered after the scope was set;
* a ``safe`` call with no target is refused unless the scope has the
  "no target" entry;
* a change keeps today's rules (grants or approval);
* without a scope, every verdict is the same as today;
* a person is never limited by a scope row.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AuditLog,
    EndpointDescriptor,
    PermissionVerdict,
    ServicePrincipalGrant,
    Target,
)
from meho_backplane.operations._errors import READ_SCOPE_DENIED_CODE, result_denied
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.operations.service_read_scope_schemas import (
    ReadScopeEntry,
    ServiceReadScopeSet,
)
from meho_backplane.operations.service_read_scopes import ServiceReadScopeService
from meho_backplane.settings import get_settings

_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-0000000049f2")
_CONNECTOR = "vmware-rest-9.0"
_PRINCIPAL = "svc:build-bot"
_READ_OP = "GET:/vcenter/vm"
_WRITE_OP = "vmware.composite.vm.create"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator(*, principal_kind: PrincipalKind = PrincipalKind.SERVICE) -> Operator:
    return Operator(
        sub=_PRINCIPAL,
        name="svc",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


def _descriptor(
    *,
    op_id: str = _READ_OP,
    safety_level: str = "safe",
    requires_approval: bool = False,
    method: str | None = "GET",
) -> EndpointDescriptor:
    return EndpointDescriptor(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        op_id=op_id,
        method=method,
        source_kind="ingested" if method else "composite",
        safety_level=safety_level,
        requires_approval=requires_approval,
        parameter_schema={},
    )


async def _register_target(name: str, *, product: str = "vmware") -> Target:
    async with get_sessionmaker()() as s:
        target = Target(
            id=uuid.uuid4(),
            tenant_id=_TENANT_ID,
            name=name,
            aliases=[],
            product=product,
            host=f"{name}.test",
            port=443,
        )
        s.add(target)
        await s.commit()
        await s.refresh(target)
        s.expunge(target)
    return target


async def _set_scope(*entries: ReadScopeEntry) -> None:
    await ServiceReadScopeService().set(
        _TENANT_ID, _PRINCIPAL, "op-admin", ServiceReadScopeSet(entries=list(entries))
    )


async def _verdict(
    target: Target | None,
    *,
    descriptor: EndpointDescriptor | None = None,
    operator: Operator | None = None,
    decide_only: bool = False,
) -> tuple[PermissionVerdict, str | None]:
    return await policy_gate(
        operator=operator or _operator(),
        descriptor=descriptor or _descriptor(),
        target=target,
        connector_id=_CONNECTOR,
        decide_only=decide_only,
    )


async def _denied_audit_rows(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as s:
        rows = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.operator_sub == _PRINCIPAL,
                        AuditLog.method == "DISPATCH",
                        AuditLog.path == op_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    return [row for row in rows if row.payload.get("result_status") == "denied"]


# ---------------------------------------------------------------------------
# No scope: unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "safety_level, requires_approval, method, with_target, expected",
    [
        ("safe", False, "GET", True, PermissionVerdict.AUTO_EXECUTE),
        ("safe", False, "GET", False, PermissionVerdict.AUTO_EXECUTE),
        ("caution", False, "GET", True, PermissionVerdict.AUTO_EXECUTE),
        ("safe", True, "GET", True, PermissionVerdict.NEEDS_APPROVAL),
        ("caution", False, None, True, PermissionVerdict.NEEDS_APPROVAL),
        ("dangerous", False, "POST", True, PermissionVerdict.NEEDS_APPROVAL),
        ("destructive", False, "DELETE", True, PermissionVerdict.NEEDS_APPROVAL),
    ],
)
async def test_without_scope_every_verdict_is_unchanged(
    safety_level: str,
    requires_approval: bool,
    method: str | None,
    with_target: bool,
    expected: PermissionVerdict,
) -> None:
    """A service principal without a read scope gets today's verdict table."""
    target = await _register_target("vc-a") if with_target else None
    verdict, reason = await _verdict(
        target,
        descriptor=_descriptor(
            op_id=_READ_OP if method else _WRITE_OP,
            safety_level=safety_level,
            requires_approval=requires_approval,
            method=method,
        ),
    )
    assert verdict is expected
    assert reason is None or READ_SCOPE_DENIED_CODE not in reason


# ---------------------------------------------------------------------------
# Scope = target A
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_allows_a_and_refuses_b_with_one_audit_row() -> None:
    """Read on A runs; the same read on B is refused with the stable code and one audit row."""
    target_a = await _register_target("vc-a")
    target_b = await _register_target("vc-b")
    await _set_scope(ReadScopeEntry(target_id=target_a.id))

    verdict, reason = await _verdict(target_a)
    assert verdict is PermissionVerdict.AUTO_EXECUTE
    assert reason is None

    verdict, reason = await _verdict(target_b)
    assert verdict is PermissionVerdict.DENY
    assert reason is not None
    assert reason.startswith(f"{READ_SCOPE_DENIED_CODE}:")
    assert "vc-b" in reason
    assert _PRINCIPAL in reason

    # Through the same gate the composite path runs: the result carries the
    # stable error code, and exactly one ``denied`` audit row is written.
    result = await enforce_subop_policy(
        operator=_operator(),
        connector_id=_CONNECTOR,
        op_id=_READ_OP,
        safety_level="safe",
        requires_approval=False,
        target=target_b,
        params={},
    )
    assert result is not None
    assert result.status == "denied"
    assert result.extras["error_code"] == READ_SCOPE_DENIED_CODE
    assert len(await _denied_audit_rows(_READ_OP)) == 1


@pytest.mark.asyncio
async def test_change_on_scoped_target_keeps_grants_and_approval() -> None:
    """A change on A still parks, and a standing grant still clears it."""
    target_a = await _register_target("vc-a")
    await _set_scope(ReadScopeEntry(target_id=target_a.id))
    write = _descriptor(op_id=_WRITE_OP, safety_level="caution", method=None)

    verdict, reason = await _verdict(target_a, descriptor=write)
    assert verdict is PermissionVerdict.NEEDS_APPROVAL
    assert reason is not None and READ_SCOPE_DENIED_CODE not in reason

    async with get_sessionmaker()() as s:
        s.add(
            ServicePrincipalGrant(
                tenant_id=_TENANT_ID,
                principal_sub=_PRINCIPAL,
                op_id=_WRITE_OP,
                connector_id=_CONNECTOR,
                target_id=target_a.id,
                reason="unattended build",
                created_by_sub="op-admin",
            )
        )
        await s.commit()
    verdict, reason = await _verdict(target_a, descriptor=write)
    assert verdict is PermissionVerdict.AUTO_EXECUTE
    assert reason is not None and "standing grant" in reason


@pytest.mark.asyncio
async def test_change_outside_scope_still_parks_not_denied() -> None:
    """The scope limits reads that run at once; a change outside it parks as today."""
    target_a = await _register_target("vc-a")
    target_b = await _register_target("vc-b")
    await _set_scope(ReadScopeEntry(target_id=target_a.id))
    write = _descriptor(op_id=_WRITE_OP, safety_level="caution", method=None)

    verdict, _ = await _verdict(target_b, descriptor=write)
    assert verdict is PermissionVerdict.NEEDS_APPROVAL


@pytest.mark.asyncio
async def test_read_only_caution_op_is_limited_too() -> None:
    """A read-only ``caution`` op runs at once today, so the scope limits it as well."""
    target_a = await _register_target("vc-a")
    target_b = await _register_target("vc-b")
    await _set_scope(ReadScopeEntry(target_id=target_a.id))
    read = _descriptor(op_id="GET:/vcenter/host", safety_level="caution", method="GET")

    assert (await _verdict(target_a, descriptor=read))[0] is PermissionVerdict.AUTO_EXECUTE
    assert (await _verdict(target_b, descriptor=read))[0] is PermissionVerdict.DENY


# ---------------------------------------------------------------------------
# Selectors and the "no target" entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        ReadScopeEntry(target_product="vmware"),
        ReadScopeEntry(target_name_pattern="dc-*"),
        ReadScopeEntry(target_product="vmware", target_name_pattern="dc-*"),
    ],
)
async def test_selector_matches_target_registered_later(entry: ReadScopeEntry) -> None:
    """A selector entry covers a target registered after the scope was set."""
    await _set_scope(entry)
    later = await _register_target("dc-new")
    other = await _register_target("lab-1", product="k8s")

    assert (await _verdict(later))[0] is PermissionVerdict.AUTO_EXECUTE
    verdict, reason = await _verdict(other)
    assert verdict is PermissionVerdict.DENY
    assert reason is not None and reason.startswith(f"{READ_SCOPE_DENIED_CODE}:")


@pytest.mark.asyncio
async def test_no_target_call_needs_the_no_target_entry() -> None:
    """A safe call without a target is refused unless the scope allows it."""
    target_a = await _register_target("vc-a")
    await _set_scope(ReadScopeEntry(target_id=target_a.id))

    verdict, reason = await _verdict(None)
    assert verdict is PermissionVerdict.DENY
    assert reason is not None
    assert reason.startswith(f"{READ_SCOPE_DENIED_CODE}:")
    assert "no target" in reason

    await _set_scope(ReadScopeEntry(target_id=target_a.id), ReadScopeEntry(no_target=True))
    assert (await _verdict(None))[0] is PermissionVerdict.AUTO_EXECUTE
    # The "no target" entry allows only targetless calls, not every target.
    target_b = await _register_target("vc-b")
    assert (await _verdict(target_b))[0] is PermissionVerdict.DENY


# ---------------------------------------------------------------------------
# Who is limited, clear, preview, error code
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_person_with_same_sub_is_not_limited() -> None:
    """The scope is checked for service principals only; a person is never limited."""
    target_b = await _register_target("vc-b")
    await _set_scope(ReadScopeEntry(target_id=uuid.uuid4()))

    verdict, reason = await _verdict(
        target_b, operator=_operator(principal_kind=PrincipalKind.USER)
    )
    assert verdict is PermissionVerdict.AUTO_EXECUTE
    assert reason is None


@pytest.mark.asyncio
async def test_cleared_scope_reads_every_target_again() -> None:
    """After clear, the principal is not limited any more."""
    target_b = await _register_target("vc-b")
    await _set_scope(ReadScopeEntry(target_id=uuid.uuid4()))
    assert (await _verdict(target_b))[0] is PermissionVerdict.DENY

    service = ServiceReadScopeService()
    assert await service.clear(_TENANT_ID, _PRINCIPAL, "op-admin") is True
    assert await service.clear(_TENANT_ID, _PRINCIPAL, "op-admin") is False
    assert (await _verdict(target_b))[0] is PermissionVerdict.AUTO_EXECUTE


@pytest.mark.asyncio
async def test_scope_is_tenant_scoped() -> None:
    """A scope row of another tenant never limits this tenant's principal."""
    target_b = await _register_target("vc-b")
    other_tenant = uuid.UUID("00000000-0000-0000-0000-0000000049f3")
    await ServiceReadScopeService().set(
        other_tenant,
        _PRINCIPAL,
        "op-admin",
        ServiceReadScopeSet(entries=[ReadScopeEntry(target_id=uuid.uuid4())]),
    )
    assert (await _verdict(target_b))[0] is PermissionVerdict.AUTO_EXECUTE


@pytest.mark.asyncio
async def test_preview_sees_the_deny_and_writes_nothing() -> None:
    """``decide_only`` (the preview) reports the deny; no audit row is written."""
    target_b = await _register_target("vc-b")
    await _set_scope(ReadScopeEntry(target_id=uuid.uuid4()))

    verdict, reason = await _verdict(target_b, decide_only=True)
    assert verdict is PermissionVerdict.DENY
    assert reason is not None and reason.startswith(f"{READ_SCOPE_DENIED_CODE}:")
    assert not await _denied_audit_rows(_READ_OP)


def test_result_denied_reports_the_stable_error_code() -> None:
    """The result builder turns the gate's marker into ``error_code``."""
    scoped = result_denied("op", f"{READ_SCOPE_DENIED_CODE}: outside the scope", 1.0)
    assert scoped.status == "denied"
    assert scoped.extras["error_code"] == READ_SCOPE_DENIED_CODE
    plain = result_denied("op", "policy denied", 1.0)
    assert plain.extras["error_code"] == "denied"


def test_entry_is_exactly_one_shape() -> None:
    """An entry is one target id, one selector, or the no-target flag; never two or none."""
    with pytest.raises(ValueError, match="exactly one"):
        ReadScopeEntry()
    with pytest.raises(ValueError, match="exactly one"):
        ReadScopeEntry(target_id=uuid.uuid4(), target_product="vmware")
    with pytest.raises(ValueError, match="exactly one"):
        ReadScopeEntry(no_target=True, target_name_pattern="dc-*")
    assert ReadScopeEntry(target_product="vmware", target_name_pattern="dc-*").no_target is False


@pytest.mark.asyncio
async def test_set_replaces_the_whole_scope() -> None:
    """Setting again replaces every entry and records who set it and when."""
    service = ServiceReadScopeService()
    first = uuid.uuid4()
    second = uuid.uuid4()
    await _set_scope(ReadScopeEntry(target_id=first))
    before = datetime.now(UTC)
    await service.set(
        _TENANT_ID,
        _PRINCIPAL,
        "op-two",
        ServiceReadScopeSet(entries=[ReadScopeEntry(target_id=second)]),
    )
    shown = await service.get(_TENANT_ID, _PRINCIPAL)
    assert shown is not None
    assert [entry.target_id for entry in shown.entries] == [second]
    assert shown.set_by_sub == "op-two"
    assert shown.set_at.replace(tzinfo=UTC) >= before
    assert await service.get(_TENANT_ID, "svc:other") is None
