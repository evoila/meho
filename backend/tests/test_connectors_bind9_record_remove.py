# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed-tier conformance for ``bind9.record.remove`` (#3247 operator ruling).

``bind9.record.remove`` clears EVERY A + AAAA record at a name in one write —
the broadest DNS removal MEHO exposes. It shipped caution-tier + approval-free,
which meant an agent could clear a whole name un-governed while a human had to
approve the *narrower* single-record ``bind9.record.delete`` (#3231). The
operator ruling (Option A, 2026-09-01) promotes it to the same governed-delete
tier: ``safety_level="destructive"`` + ``requires_approval=True`` with a
park-time whole-name blast-radius statement.

These tests prove the promotion holds on every surface, folded via the SINGLE
SOURCE (``safety_level="destructive"`` + the ``destructive`` tag), never a
local pattern list:

* **The park-time blast radius names the whole name + enumerates BOTH families'
  values** — the full set that dies — and declines (→ ``blast_radius_required``,
  fail-closed) when the zone is not writably served here.
* **The full governed flow**: preview → parked approval carrying the hash +
  whole-name blast radius → a *distinct human* approves → audited resume runs
  the atomic clear.
* **The tier holds**: agent ``DENY``, no ``ServicePrincipalGrant``, no
  self-approval under break-glass, no satellite mint, dispatch refused without a
  matching preview hash, park refused without a blast-radius block.

The SSH transport is mocked by ``_RecordingBind9Connector`` (the
``_SeededBind9Connector`` pattern), reusing the shape from
``test_connectors_bind9_record_delete.py``. All fixtures are synthetic
(``example.test`` / RFC 5737 + RFC 3849 documentation addresses) — no lab
names/IPs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.connectors.bind9  # noqa: F401 -- registers the connector at import
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.bind9 import Bind9Connector
from meho_backplane.connectors.bind9.ops_record_delete_preview import (
    _bind9_record_remove_preview,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations import typed_register as tr_module
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.operations.approval_queue import (
    SelfApprovalForbiddenError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.gateway_commands import MintRefusalCode, mint_gateway_command
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
)
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "bind9-ssh-9.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000003247")
_OP_ID = "bind9.record.remove"
_ZONE = "example.test"
_ZONEFILE_PATH = "/etc/bind/db.example.test"
_TARGET_NAME = "test-dns"

# `named-checkconf -p` declaring one writable master zone.
_CHECKCONF = 'zone "example.test" {\n\ttype master;\n\tfile "/etc/bind/db.example.test";\n};\n'

# A checkconf whose only zone does NOT suffix the requested FQDN.
_CHECKCONF_OTHER = 'zone "other.test" {\n\ttype master;\n\tfile "/etc/bind/db.other.test";\n};\n'

# Zonefile: `dual` carries an A AND an AAAA (the whole-name clear removes both);
# `web` a single A. Addresses from RFC 5737 + RFC 3849 (documentation ranges).
_ZONEFILE = """$TTL 3600
@ IN SOA ns1.example.test. admin.example.test. (
    2026090101 3600 600 604800 86400 )
@   IN NS ns1.example.test.
ns1 IN A 192.0.2.1
web IN A 192.0.2.20
dual IN A 192.0.2.30
dual IN AAAA 2001:db8::30
"""

# atomic_apply pipeline success output (sentinel-delimited state snapshots).
_SUDO_SUCCESS = (
    "===STATE_BEFORE_BEGIN===\n<old>\n===STATE_BEFORE_END===\n"
    "===STATE_AFTER_BEGIN===\n<new>\n===STATE_AFTER_END===\n"
    "===SUCCESS===\n"
)


# ---------------------------------------------------------------------------
# Fixtures + doubles
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_caches() -> Iterator[None]:
    reset_dispatcher_caches()
    yield
    reset_dispatcher_caches()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


def _proc(stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    p = MagicMock()
    p.stdout = stdout
    p.stderr = stderr
    p.exit_status = exit_status
    return p


class _RecordingBind9Connector(Bind9Connector):
    """A Bind9Connector whose SSH transport is canned + recorded.

    Overrides the IO seams the remove handler + its preview builder touch:
    ``_resolve_secret`` (sudo password), ``_run_command`` (``named-checkconf
    -p`` + ``cat``), ``_remote_bash_with_sudo`` (the atomic-apply pipeline).
    A subclass (not a duck type) so the resolver's bound-method rebind + the
    ``_CONNECTOR_INSTANCE_CACHE`` seeding behave exactly as in production.
    """

    def __init__(
        self,
        *,
        checkconf: str = _CHECKCONF,
        zonefile: str = _ZONEFILE,
        sudo_output: str = _SUDO_SUCCESS,
    ) -> None:
        super().__init__()
        self._checkconf = checkconf
        self._zonefile = zonefile
        self._sudo_output = sudo_output
        self.run_calls: list[str] = []
        self.sudo_calls: list[str] = []

    async def _resolve_secret(
        self, target: Any, operator: Operator | None = None
    ) -> dict[str, Any]:
        return {"username": "root", "password": "test-sudo-pwd"}  # NOSONAR -- unit stub

    async def _run_command(
        self, target: Any, cmd: str, *, operator: Operator | None = None, timeout: float = 30.0
    ) -> Any:
        self.run_calls.append(cmd)
        if cmd.startswith("named-checkconf"):
            return _proc(self._checkconf)
        if cmd.startswith("cat "):
            return _proc(self._zonefile)
        raise AssertionError(f"unexpected _run_command {cmd!r}")

    async def _remote_bash_with_sudo(
        self,
        target: Any,
        script: str,
        *,
        operator: Operator | None = None,
        sudo_password: str,
        timeout: float = 60.0,
    ) -> Any:
        self.sudo_calls.append(script)
        return _proc(self._sudo_output)


def _make_operator(
    *, sub: str = "op-1", principal_kind: PrincipalKind = PrincipalKind.USER
) -> Operator:
    return Operator(
        sub=sub,
        name="bind9 remove conformance",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _FakeFingerprint:
    def __init__(self, version: str | None = "9.18.24") -> None:
        self.version = version


class _FakeBind9Target:
    """Duck-typed target for direct ``dispatch(...)`` (no DB name-resolve)."""

    def __init__(self, target_id: UUID | None = None) -> None:
        self.product = "bind9"
        self.fingerprint = _FakeFingerprint()
        self.preferred_impl_id: str | None = "bind9-ssh"
        self.id: UUID = target_id or uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = _TARGET_NAME
        self.host = "dns.example.test"
        self.port = 22
        self.auth_model = "shared_service_account"
        self.secret_ref = "meho/testing/bind9/test-dns"


def _seed_instance(recorder: _RecordingBind9Connector) -> None:
    _CONNECTOR_INSTANCE_CACHE[Bind9Connector] = recorder  # type: ignore[assignment]


async def _register_ops() -> None:
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await Bind9Connector.register_operations()


async def _bootstrap(recorder: _RecordingBind9Connector) -> None:
    await _register_ops()
    _seed_instance(recorder)


async def _seed_target() -> UUID:
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="bind9",
                host="dns.example.test",
                port=22,
                fqdn=None,
                secret_ref="meho/testing/bind9/test-dns",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "9.18.24"},
                preferred_impl_id="bind9-ssh",
                notes="seeded by test_connectors_bind9_record_remove",
            )
        )
        await s.commit()
    return target_id


async def _pending_count() -> int:
    async with get_sessionmaker()() as s:
        return int(
            (await s.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


def _args(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "connector_id": _CONNECTOR_ID,
        "op_id": _OP_ID,
        "target": _TARGET_NAME,
        "params": {"fqdn": "dual.example.test"},
    }
    base.update(overrides)
    return base


def _preview_ctx(recorder: _RecordingBind9Connector, *, params: dict[str, Any]) -> PreviewContext:
    """A PreviewContext wired to the recording connector for a direct builder call.

    ``descriptor`` is unused by the bind9 builder (it reads params + the
    connector seams), so a lightweight stand-in keeps the unit test off the DB.
    """
    return PreviewContext(
        descriptor=MagicMock(op_id=_OP_ID),
        connector_instance=recorder,
        operator=_make_operator(),
        target=_FakeBind9Target(),
        params=params,
        connector_id=_CONNECTOR_ID,
    )


# ===========================================================================
# Preview builder — the whole-name blast radius
# ===========================================================================


async def test_preview_enumerates_both_families_at_the_name() -> None:
    recorder = _RecordingBind9Connector()
    preview = await _bind9_record_remove_preview(
        _preview_ctx(recorder, params={"fqdn": "dual.example.test"})
    )
    assert preview is not None
    blast = preview["blast_radius"]
    assert blast["object"] == {
        "kind": "dns_name",
        "zone": _ZONE,
        "name": "dual.example.test.",
        "types": ["A", "AAAA"],
        "view": None,
    }
    # Children enumerate the whole set that dies: the A and the AAAA value.
    assert blast["children"] == [
        {"kind": "record_value", "type": "A", "rdata": "192.0.2.30"},
        {"kind": "record_value", "type": "AAAA", "rdata": "2001:db8::30"},
    ]
    assert blast["irreversibility"] == "recreatable"
    assert blast["match_count"] == 2


async def test_preview_single_family_name_counts_one() -> None:
    recorder = _RecordingBind9Connector()
    preview = await _bind9_record_remove_preview(
        _preview_ctx(recorder, params={"fqdn": "web.example.test"})
    )
    assert preview is not None
    blast = preview["blast_radius"]
    assert blast["children"] == [{"kind": "record_value", "type": "A", "rdata": "192.0.2.20"}]
    assert blast["match_count"] == 1


async def test_preview_absent_name_is_empty_children_not_decline() -> None:
    """A name with no A/AAAA is a valid empty blast radius, not a decline.

    The park still proceeds (children is a list, empty is legitimate); the
    handler no-ops post-approval.
    """
    recorder = _RecordingBind9Connector()
    preview = await _bind9_record_remove_preview(
        _preview_ctx(recorder, params={"fqdn": "nope.example.test"})
    )
    assert preview is not None
    assert preview["blast_radius"]["children"] == []
    assert preview["blast_radius"]["match_count"] == 0


async def test_preview_declines_on_unmanaged_zone() -> None:
    recorder = _RecordingBind9Connector(checkconf=_CHECKCONF_OTHER)
    preview = await _bind9_record_remove_preview(
        _preview_ctx(recorder, params={"fqdn": "web.example.test"})
    )
    assert preview is None


async def test_preview_declines_without_connector_instance() -> None:
    ctx = PreviewContext(
        descriptor=MagicMock(op_id=_OP_ID),
        connector_instance=None,
        operator=_make_operator(),
        target=_FakeBind9Target(),
        params={"fqdn": "dual.example.test"},
        connector_id=_CONNECTOR_ID,
    )
    assert await _bind9_record_remove_preview(ctx) is None


# ===========================================================================
# Registration — the op is destructive + requires_approval
# ===========================================================================


async def test_op_registered_destructive_requires_approval() -> None:
    await _register_ops()
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(select(EndpointDescriptor).where(EndpointDescriptor.op_id == _OP_ID))
        ).scalar_one()
    assert row.safety_level == "destructive"
    assert row.requires_approval is True
    assert row.source_kind == "typed"
    assert "destructive" in (row.tags or [])


# ===========================================================================
# The full governed flow (the keystone)
# ===========================================================================


async def test_full_governed_flow_preview_park_approve_resume() -> None:
    """preview → park (hash + whole-name blast radius) → distinct human → atomic clear."""
    recorder = _RecordingBind9Connector()
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = _args()  # dual.example.test — an A + AAAA name

    # 1) Preview binds a param-sensitive hash even for a typed op.
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    bound_hash = preview["preview_hash"]
    assert isinstance(bound_hash, str) and len(bound_hash) == 64

    # 2) Governed call presenting the bound hash parks; no write ran.
    call = await call_operation(requester, {**args, "preview_hash": bound_hash})
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])
    assert recorder.sudo_calls == []  # nothing staged pre-approval

    # 3) The parked row carries the bound hash + the whole-name blast radius.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        assert row.preview_hash == bound_hash
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    blast = effect["blast_radius"]
    assert blast["object"]["kind"] == "dns_name"
    assert blast["object"]["name"] == "dual.example.test."
    assert {c["rdata"] for c in blast["children"]} == {"192.0.2.30", "2001:db8::30"}
    assert blast["irreversibility"] == "recreatable"

    # 4) A DIFFERENT operator approves (four-eyes) — the binding re-verifies.
    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"

    # 5) Audited resume → the atomic clear stages exactly once.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["op_class"] == "write"
    assert resume.result["fqdn"] == "dual.example.test"
    assert len(recorder.sudo_calls) == 1


# ===========================================================================
# Requirement 2 — dispatch refused without a matching preview hash
# ===========================================================================


async def test_dispatch_refused_without_preview_hash() -> None:
    recorder = _RecordingBind9Connector()
    await _bootstrap(recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeBind9Target(),
        params={"fqdn": "dual.example.test"},
    )
    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_binding_required"
    assert recorder.sudo_calls == []
    assert await _pending_count() == 0


# ===========================================================================
# Requirement 3 — park refused without a blast-radius block
# ===========================================================================


async def test_park_refused_without_blast_radius_unmanaged_zone() -> None:
    """An unmanaged zone → preview builder declines → park refused (fail-closed)."""
    recorder = _RecordingBind9Connector(checkconf=_CHECKCONF_OTHER)
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = _args(params={"fqdn": "web.example.test"})
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok"

    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert recorder.sudo_calls == []
    assert await _pending_count() == 0


# ===========================================================================
# No agent execution path — an AGENT principal is DENY'd
# ===========================================================================


async def test_agent_principal_is_denied() -> None:
    recorder = _RecordingBind9Connector()
    await _bootstrap(recorder)

    result = await dispatch(
        operator=_make_operator(sub="agent-1", principal_kind=PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeBind9Target(),
        params={"fqdn": "dual.example.test"},
    )
    assert result.status == "denied", result
    assert recorder.sudo_calls == []  # never executed
    assert await _pending_count() == 0  # never parked either


# ===========================================================================
# No standing-grant path — ServicePrincipalGrant refuses via the single source
# ===========================================================================


async def test_service_grant_refuses_via_single_source_not_pattern_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grant is refused by ``safety_level="destructive"`` on the resolved
    descriptor, not by an op-id pattern list.

    Blank out the delete-shaped op-id glob list so the pattern check cannot
    fire, register the descriptor, and prove the grant is STILL refused — the
    #3213 single-source guard. ``record.remove`` has no ``delete``-verb suffix,
    so without the tier fold a blanked pattern list would let it through.
    """
    recorder = _RecordingBind9Connector()
    await _bootstrap(recorder)
    monkeypatch.setattr(get_settings(), "service_grant_delete_shaped_patterns", (), raising=False)

    svc = ServicePrincipalGrantService()
    payload = ServiceGrantCreate(
        principal_sub="svc-runner",
        op_id=_OP_ID,
        connector_id=_CONNECTOR_ID,
        target_id=None,
        reason="unattended teardown",
        expires_at=None,
    )
    with pytest.raises(GrantValidationError) as exc:
        await svc.create(_TENANT_ID, "creator", payload)
    assert "destructive" in str(exc.value).lower()


# ===========================================================================
# No self-approval, even under APPROVAL_ALLOW_SELF_APPROVAL
# ===========================================================================


async def test_no_self_approval_even_under_break_glass(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RecordingBind9Connector()
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="solo-operator")
    args = _args()
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    request_id = UUID(call["extras"]["approval_request_id"])

    # Break-glass ON — it does NOT reach the destructive tier.
    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true")
    get_settings.cache_clear()
    async with get_sessionmaker()() as s:
        with pytest.raises(SelfApprovalForbiddenError):
            await approve_request(s, request_id, operator=requester, params=None)


# ===========================================================================
# No satellite mint — the gateway refuses with OP_NOT_SAFE
# ===========================================================================


async def test_satellite_mint_refuses_op_not_safe() -> None:
    recorder = _RecordingBind9Connector()
    await _bootstrap(recorder)

    async with get_sessionmaker()() as s:
        result = await mint_gateway_command(
            s,
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params={"fqdn": "dual.example.test"},
            runner_id="runner-1",
        )
        await s.commit()
    assert not result.minted
    assert result.refusal_code is MintRefusalCode.OP_NOT_SAFE
