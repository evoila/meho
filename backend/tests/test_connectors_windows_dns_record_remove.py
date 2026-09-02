# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed-tier conformance for ``windns.record.remove`` (#3288 operator ruling).

``windns.record.remove`` clears EVERY value of an RRType at a name in one
``Remove-DnsServerResourceRecord ... -Force`` write. It shipped caution-tier +
approval-free; the #3288 operator ruling promotes it to the governed-delete
tier: ``safety_level="destructive"`` + ``requires_approval=True`` with a
park-time RRset blast-radius statement (mirroring #3247 for
``bind9.record.remove``).

These tests prove the promotion holds on every surface, folded via the SINGLE
SOURCE (``safety_level="destructive"`` + the ``destructive`` tag), never a local
pattern list:

* **The park-time blast radius names the RRset + enumerates every value that
  dies**, and declines (→ ``blast_radius_required``, fail-closed) when the
  current records cannot be read.
* **The full governed flow**: preview → parked approval carrying the hash +
  RRset blast radius → a *distinct human* approves → audited resume runs the
  ``-Force`` clear.
* **The tier holds**: agent ``DENY``, no ``ServicePrincipalGrant``, no
  self-approval under break-glass, no satellite mint, dispatch refused without a
  matching preview hash, park refused without a blast-radius block.

The PowerShell-over-SSH transport is mocked by ``_RecordingWindowsDnsConnector``
(overriding the ``_run_command`` seam ``pwsh_run`` calls, decoding the
``-EncodedCommand`` payload to branch Get-vs-Remove). All fixtures are synthetic
(``example.test`` / RFC 5737 documentation addresses) — no lab names/IPs.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy import func, select

import meho_backplane.connectors.windows_dns  # noqa: F401 -- registers the connector at import
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors._shared.pwsh import PwshRunError
from meho_backplane.connectors.windows_dns import WindowsDnsConnector
from meho_backplane.connectors.windows_dns.ops_record_remove_preview import (
    _windns_record_remove_preview,
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

_CONNECTOR_ID = "windns-ssh-2016.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000003288")
_OP_ID = "windns.record.remove"
_ZONE = "example.test"
_TARGET_NAME = "test-windns"

# Two A values at the same name — the RRset the -Force clear removes together.
_GET_TWO_A = json.dumps(
    {
        "rows": [
            {"type": "A", "rdata": "192.0.2.30"},
            {"type": "A", "rdata": "192.0.2.31"},
        ],
        "total": 2,
    }
)
_GET_EMPTY = json.dumps({"rows": [], "total": 0})
_REMOVE_OK = json.dumps({"ok": True})


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


def _proc(stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    p = MagicMock()
    p.stdout = stdout
    p.stderr = stderr
    p.exit_status = exit_status
    return p


def _decode_encoded_command(cmd: str) -> str:
    """Recover the PowerShell script text from a ``-EncodedCommand`` invocation."""
    b64 = cmd.split("-EncodedCommand", 1)[1].strip()
    return base64.b64decode(b64).decode("utf-16-le")


class _RecordingWindowsDnsConnector(WindowsDnsConnector):
    """A WindowsDnsConnector whose pwsh transport is canned + recorded.

    Overrides the single IO seam the preview builder + the remove handler both
    reach — ``_run_command`` (which ``pwsh_run`` calls). Decodes the
    ``-EncodedCommand`` payload to branch the read (``Get-DnsServerResourceRecord``
    → the RRset) from the write (``Remove-DnsServerResourceRecord`` → ``ok``). A
    subclass (not a duck type) so the resolver's bound-method rebind + the
    ``_CONNECTOR_INSTANCE_CACHE`` seeding behave exactly as in production.
    """

    def __init__(self, *, get_output: str = _GET_TWO_A, get_raises: bool = False) -> None:
        super().__init__()
        self._get_output = get_output
        self._get_raises = get_raises
        self.run_calls: list[str] = []
        self.remove_calls: list[str] = []

    async def _run_command(
        self, target: Any, cmd: str, *, operator: Operator | None = None, timeout: float = 30.0
    ) -> Any:
        self.run_calls.append(cmd)
        script = _decode_encoded_command(cmd)
        if "Get-DnsServerResourceRecord" in script:
            if self._get_raises:
                raise PwshRunError("read failed", exit_status=1, stderr="boom")
            return _proc(self._get_output)
        if "Remove-DnsServerResourceRecord" in script:
            self.remove_calls.append(script)
            return _proc(_REMOVE_OK)
        raise AssertionError(f"unexpected pwsh script: {script!r}")


def _make_operator(
    *, sub: str = "op-1", principal_kind: PrincipalKind = PrincipalKind.USER
) -> Operator:
    return Operator(
        sub=sub,
        name="windns remove conformance",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=principal_kind,
    )


class _FakeFingerprint:
    def __init__(self, version: str | None = "2016.x") -> None:
        self.version = version


class _FakeWindnsTarget:
    """Duck-typed target for direct ``dispatch(...)`` (no DB name-resolve)."""

    def __init__(self, target_id: UUID | None = None) -> None:
        self.product = "windns"
        self.fingerprint = _FakeFingerprint()
        self.preferred_impl_id: str | None = "windns-ssh"
        self.id: UUID = target_id or uuid.uuid4()
        self.tenant_id: UUID = _TENANT_ID
        self.name = _TARGET_NAME
        self.host = "dns.example.test"
        self.port = 22
        self.auth_model = "shared_service_account"
        self.secret_ref = "meho/testing/windns/test-windns"


def _seed_instance(recorder: _RecordingWindowsDnsConnector) -> None:
    _CONNECTOR_INSTANCE_CACHE[WindowsDnsConnector] = recorder  # type: ignore[assignment]


async def _register_ops() -> None:
    with patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)):
        await WindowsDnsConnector.register_operations()


async def _bootstrap(recorder: _RecordingWindowsDnsConnector) -> None:
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
                product="windns",
                host="dns.example.test",
                port=22,
                fqdn=None,
                secret_ref="meho/testing/windns/test-windns",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "2016.x"},
                preferred_impl_id="windns-ssh",
                notes="seeded by test_connectors_windows_dns_record_remove",
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
        "params": {"zone": _ZONE, "name": "web", "type": "A"},
    }
    base.update(overrides)
    return base


def _preview_ctx(
    recorder: _RecordingWindowsDnsConnector, *, params: dict[str, Any]
) -> PreviewContext:
    """A PreviewContext wired to the recording connector for a direct builder call."""
    return PreviewContext(
        descriptor=MagicMock(op_id=_OP_ID),
        connector_instance=recorder,
        operator=_make_operator(),
        target=_FakeWindnsTarget(),
        params=params,
        connector_id=_CONNECTOR_ID,
    )


# ===========================================================================
# Preview builder — the RRset blast radius
# ===========================================================================


async def test_preview_enumerates_every_value_at_the_rrset() -> None:
    recorder = _RecordingWindowsDnsConnector()
    preview = await _windns_record_remove_preview(
        _preview_ctx(recorder, params={"zone": _ZONE, "name": "web", "type": "A"})
    )
    assert preview is not None
    blast = preview["blast_radius"]
    assert blast["object"] == {
        "kind": "dns_record",
        "zone": _ZONE,
        "name": "web",
        "type": "A",
    }
    assert blast["children"] == [
        {"kind": "record_value", "type": "A", "rdata": "192.0.2.30"},
        {"kind": "record_value", "type": "A", "rdata": "192.0.2.31"},
    ]
    assert blast["irreversibility"] == "recreatable"
    assert blast["match_count"] == 2


async def test_preview_absent_name_is_empty_children_not_decline() -> None:
    """A name with no matching record is a valid empty blast radius, not a decline."""
    recorder = _RecordingWindowsDnsConnector(get_output=_GET_EMPTY)
    preview = await _windns_record_remove_preview(
        _preview_ctx(recorder, params={"zone": _ZONE, "name": "nope", "type": "A"})
    )
    assert preview is not None
    assert preview["blast_radius"]["children"] == []
    assert preview["blast_radius"]["match_count"] == 0


async def test_preview_declines_on_read_failure() -> None:
    recorder = _RecordingWindowsDnsConnector(get_raises=True)
    preview = await _windns_record_remove_preview(
        _preview_ctx(recorder, params={"zone": _ZONE, "name": "web", "type": "A"})
    )
    assert preview is None


async def test_preview_declines_on_unsupported_type() -> None:
    recorder = _RecordingWindowsDnsConnector()
    preview = await _windns_record_remove_preview(
        _preview_ctx(recorder, params={"zone": _ZONE, "name": "web", "type": "SOA"})
    )
    assert preview is None


async def test_preview_declines_without_connector_instance() -> None:
    ctx = PreviewContext(
        descriptor=MagicMock(op_id=_OP_ID),
        connector_instance=None,
        operator=_make_operator(),
        target=_FakeWindnsTarget(),
        params={"zone": _ZONE, "name": "web", "type": "A"},
        connector_id=_CONNECTOR_ID,
    )
    assert await _windns_record_remove_preview(ctx) is None


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
    """preview → park (hash + RRset blast radius) → distinct human → -Force clear."""
    recorder = _RecordingWindowsDnsConnector()
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = _args()

    # 1) Preview binds a param-sensitive hash even for a typed op.
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok", preview
    bound_hash = preview["preview_hash"]
    assert isinstance(bound_hash, str) and len(bound_hash) == 64

    # 2) Governed call presenting the bound hash parks; no write ran.
    call = await call_operation(requester, {**args, "preview_hash": bound_hash})
    assert call["status"] == "awaiting_approval", call
    request_id = UUID(call["extras"]["approval_request_id"])
    assert recorder.remove_calls == []  # nothing cleared pre-approval

    # 3) The parked row carries the bound hash + the RRset blast radius.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        assert row.preview_hash == bound_hash
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    blast = effect["blast_radius"]
    assert blast["object"]["kind"] == "dns_record"
    assert blast["object"]["name"] == "web"
    assert blast["object"]["type"] == "A"
    assert {c["rdata"] for c in blast["children"]} == {"192.0.2.30", "192.0.2.31"}
    assert blast["irreversibility"] == "recreatable"

    # 4) A DIFFERENT operator approves (four-eyes) — the binding re-verifies.
    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"

    # 5) Audited resume → the -Force clear runs exactly once.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["op_class"] == "write"
    assert resume.result["type"] == "A"
    assert len(recorder.remove_calls) == 1


# ===========================================================================
# Requirement 2 — dispatch refused without a matching preview hash
# ===========================================================================


async def test_dispatch_refused_without_preview_hash() -> None:
    recorder = _RecordingWindowsDnsConnector()
    await _bootstrap(recorder)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeWindnsTarget(),
        params={"zone": _ZONE, "name": "web", "type": "A"},
    )
    assert result.status == "denied"
    assert result.extras["error_code"] == "preview_binding_required"
    assert recorder.remove_calls == []
    assert await _pending_count() == 0


# ===========================================================================
# Requirement 3 — park refused without a blast-radius block
# ===========================================================================


async def test_park_refused_without_blast_radius_on_read_failure() -> None:
    """A read failure → preview builder declines → park refused (fail-closed)."""
    recorder = _RecordingWindowsDnsConnector(get_raises=True)
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = _args()
    preview = await preview_operation(requester, args)
    assert preview["status"] == "ok"

    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "denied", call
    assert call["extras"]["error_code"] == "blast_radius_required"
    assert recorder.remove_calls == []
    assert await _pending_count() == 0


# ===========================================================================
# No agent execution path — an AGENT principal is DENY'd
# ===========================================================================


async def test_agent_principal_is_denied() -> None:
    recorder = _RecordingWindowsDnsConnector()
    await _bootstrap(recorder)

    result = await dispatch(
        operator=_make_operator(sub="agent-1", principal_kind=PrincipalKind.AGENT),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=_FakeWindnsTarget(),
        params={"zone": _ZONE, "name": "web", "type": "A"},
    )
    assert result.status == "denied", result
    assert recorder.remove_calls == []  # never executed
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
    recorder = _RecordingWindowsDnsConnector()
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
    recorder = _RecordingWindowsDnsConnector()
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
    recorder = _RecordingWindowsDnsConnector()
    await _bootstrap(recorder)

    async with get_sessionmaker()() as s:
        result = await mint_gateway_command(
            s,
            operator=_make_operator(),
            connector_id=_CONNECTOR_ID,
            op_id=_OP_ID,
            target=None,
            params={"zone": _ZONE, "name": "web", "type": "A"},
            runner_id="runner-1",
        )
        await s.commit()
    assert not result.minted
    assert result.refusal_code is MintRefusalCode.OP_NOT_SAFE
