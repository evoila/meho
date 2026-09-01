# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Conformance + unit tests for the bind9 governed record-delete (#3231).

``bind9.record.delete`` is the bind9 arm of the governed-delete tier decided
in ``docs/decisions/governed-delete-operations.md`` and first modeled by
``vmware.composite.vm.destroy`` (#3198 / PR #3225). It is the first
``safety_level="destructive"`` op on a typed SSH connector. These tests prove:

* **Scoped to ONE record.** The delete only ever removes the single
  ``(zone, name, type, rdata?)`` record; a name with multiple values keeps its
  siblings, and a multi-value name with no ``rdata`` refuses ``ambiguous``.
* **Fail-closed structured refusals** (mirroring ``vm.destroy``'s
  ``not_powered_off``): ``not_found`` (no silent success), ``ambiguous``
  (candidates named), ``unmanaged_zone`` (no writable zone owns the FQDN).
* **The tier holds on every surface** for this op: agent ``DENY``,
  ``ServicePrincipalGrant`` refusal — proven to fold via the *single source*
  (``safety_level="destructive"``), not a local pattern list — no
  self-approval under break-glass, no satellite mint, dispatch refused without
  a matching preview hash, park refused without a blast-radius block.
* **The full governed flow**: preview → parked approval carrying the hash +
  blast radius → a *distinct human* approves → audited resume executes the
  atomic delete and verifies the value is gone.

The SSH transport is mocked by a ``_RecordingBind9Connector`` subclass seeded
as the resolved instance (the ``_SeededBind9Connector`` pattern from
``test_operations_handler_resolve.py``); the autouse-migrated SQLite engine
and the ``encode_endpoint_text`` patch (``test_connectors_bind9.py``) keep
registration off fastembed. All fixtures are synthetic (``example.test`` /
RFC 5737 + RFC 3849 documentation addresses) — no lab names/IPs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import meho_backplane.connectors.bind9  # noqa: F401 -- registers the connector at import
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.bind9 import Bind9Connector
from meho_backplane.connectors.bind9.ops_record import (
    _delete_one_record_from_zonefile,
    _dig_value_absent_verify,
    _find_record_matches,
    _resolve_delete_target,
    _soa_serial_from_text,
    bind9_record_delete,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations import typed_register as tr_module
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
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
from meho_backplane.operations.typed_register import register_composite_operation
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "bind9-ssh-9.x"
_TENANT_ID = UUID("00000000-0000-0000-0000-000000003231")
_OP_ID = "bind9.record.delete"
_ZONE = "example.test"
_ZONEFILE_PATH = "/etc/bind/db.example.test"
_TARGET_NAME = "test-dns"

# `named-checkconf -p` declaring one writable master zone.
_CHECKCONF = 'zone "example.test" {\n\ttype master;\n\tfile "/etc/bind/db.example.test";\n};\n'

# A checkconf whose only zone does NOT suffix the requested FQDN — the
# "zone not managed here" shape.
_CHECKCONF_OTHER = 'zone "other.test" {\n\ttype master;\n\tfile "/etc/bind/db.other.test";\n};\n'

# Split-horizon checkconf: example.test declared once per view (#2897).
_CHECKCONF_MULTIVIEW = (
    'view "internal" {\n'
    "\tmatch-clients { 10.0.0.0/8; localhost; };\n"
    '\tzone "example.test" {\n'
    "\t\ttype master;\n"
    '\t\tfile "/etc/bind/internal/db.example.test";\n'
    "\t};\n"
    "};\n"
    'view "external" {\n'
    "\tmatch-clients { any; };\n"
    '\tzone "example.test" {\n'
    "\t\ttype master;\n"
    '\t\tfile "/etc/bind/external/db.example.test";\n'
    "\t};\n"
    "};\n"
)

# Zonefile: `web` single A, `api` two A values (round-robin), `host6` AAAA.
# Addresses from RFC 5737 (192.0.2.0/24) + RFC 3849 (2001:db8::/32).
_ZONEFILE = """$TTL 3600
@ IN SOA ns1.example.test. admin.example.test. (
    2026080101 3600 600 604800 86400 )
@   IN NS ns1.example.test.
ns1 IN A 192.0.2.1
web IN A 192.0.2.20
api IN A 192.0.2.10
api IN A 192.0.2.11
host6 IN AAAA 2001:db8::1
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


def _staged_zonefile(sudo_script: str) -> str:
    """Decode the base64 staged zonefile bytes out of an atomic-apply script.

    ``atomic_apply`` base64-encodes the staged bytes into
    ``export BIND9_STAGED_B64='...'`` (the verify command rides plaintext in
    ``BIND9_VERIFY_CMD``), so staged-content assertions must decode this slot
    rather than grep the whole script (which also contains the verify text).
    """
    import base64
    import re

    m = re.search(r"BIND9_STAGED_B64='([^']*)'", sudo_script)
    assert m is not None, "no BIND9_STAGED_B64 in the atomic-apply script"
    return base64.b64decode(m.group(1)).decode("utf-8")


class _RecordingBind9Connector(Bind9Connector):
    """A Bind9Connector whose SSH transport is canned + recorded.

    Overrides only the three IO seams the delete handler + its preview
    builder touch: ``_resolve_secret`` (sudo password), ``_run_command``
    (``named-checkconf -p`` + ``cat``), ``_remote_bash_with_sudo`` (the
    atomic-apply pipeline). A subclass (not a duck type) so the resolver's
    bound-method rebind + the ``_CONNECTOR_INSTANCE_CACHE`` seeding behave
    exactly as in production (the ``_SeededBind9Connector`` pattern).
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
        name="bind9 delete conformance",
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
                notes="seeded by test_connectors_bind9_record_delete",
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
        "params": {"fqdn": "web.example.test", "type": "A"},
    }
    base.update(overrides)
    return base


# ===========================================================================
# Pure-function unit tests — scoping is the load-bearing safety property
# ===========================================================================


def test_find_record_matches_single_multi_and_absent() -> None:
    assert _find_record_matches(
        _ZONEFILE, zone_name=_ZONE, fqdn="web.example.test", record_type="A"
    ) == ["192.0.2.20"]
    assert _find_record_matches(
        _ZONEFILE, zone_name=_ZONE, fqdn="api.example.test", record_type="A"
    ) == ["192.0.2.10", "192.0.2.11"]
    assert (
        _find_record_matches(_ZONEFILE, zone_name=_ZONE, fqdn="nope.example.test", record_type="A")
        == []
    )


def test_find_record_matches_is_canonical_for_ipv6() -> None:
    assert _find_record_matches(
        _ZONEFILE, zone_name=_ZONE, fqdn="host6.example.test", record_type="AAAA"
    ) == ["2001:db8::1"]


def test_resolve_delete_target_ok_notfound_ambiguous() -> None:
    single = ["192.0.2.20"]
    multi = ["192.0.2.10", "192.0.2.11"]
    assert _resolve_delete_target(single, record_type="A", rdata_param=None) == ("192.0.2.20", "ok")
    assert _resolve_delete_target([], record_type="A", rdata_param=None) == (None, "not_found")
    assert _resolve_delete_target(multi, record_type="A", rdata_param=None) == (None, "ambiguous")
    # rdata pins exactly one of a multi-value set.
    assert _resolve_delete_target(multi, record_type="A", rdata_param="192.0.2.11") == (
        "192.0.2.11",
        "ok",
    )
    # rdata not present → not_found (never a wrong deletion).
    assert _resolve_delete_target(multi, record_type="A", rdata_param="192.0.2.99") == (
        None,
        "not_found",
    )


def test_resolve_delete_target_rdata_canonical_match() -> None:
    # A non-canonical IPv6 spelling still resolves to the stored value.
    assert _resolve_delete_target(
        ["2001:db8::1"], record_type="AAAA", rdata_param="2001:0DB8:0:0::1"
    ) == ("2001:db8::1", "ok")


def test_delete_one_record_keeps_siblings() -> None:
    """Deleting ONE value of a multi-value name leaves the other intact."""
    new = _delete_one_record_from_zonefile(
        _ZONEFILE, zone_name=_ZONE, fqdn="api.example.test", record_type="A", rdata="192.0.2.10"
    )
    remaining = _find_record_matches(new, zone_name=_ZONE, fqdn="api.example.test", record_type="A")
    assert remaining == ["192.0.2.11"]
    # Untouched names are preserved; SOA serial advances exactly once.
    assert _find_record_matches(new, zone_name=_ZONE, fqdn="web.example.test", record_type="A") == [
        "192.0.2.20"
    ]
    assert _soa_serial_from_text(new, _ZONE) == _soa_serial_from_text(_ZONEFILE, _ZONE) + 1


def test_delete_one_record_removes_last_value_rrset() -> None:
    new = _delete_one_record_from_zonefile(
        _ZONEFILE, zone_name=_ZONE, fqdn="web.example.test", record_type="A", rdata="192.0.2.20"
    )
    assert (
        _find_record_matches(new, zone_name=_ZONE, fqdn="web.example.test", record_type="A") == []
    )


def test_delete_one_record_absent_value_raises() -> None:
    with pytest.raises(ValueError, match="not present"):
        _delete_one_record_from_zonefile(
            _ZONEFILE, zone_name=_ZONE, fqdn="web.example.test", record_type="A", rdata="192.0.2.99"
        )


def test_dig_value_absent_verify_is_scoped_and_quoted() -> None:
    """The verify predicate checks the one VALUE, and shell-quotes its inputs."""
    cmd = _dig_value_absent_verify("web.example.test", "A", "192.0.2.20")
    assert "dig @localhost web.example.test A +short" in cmd
    assert "grep -qxF 192.0.2.20" in cmd  # literal whole-line, single value
    # A metacharacter-laden value is quoted, not interpolated raw.
    danger = _dig_value_absent_verify("x.example.test", "A", "1.2.3.4; rm -rf /")
    assert "'1.2.3.4; rm -rf /'" in danger


# ===========================================================================
# Handler-level tests (patched SSH; direct handler call)
# ===========================================================================


async def test_handler_happy_path_single_value() -> None:
    connector = _RecordingBind9Connector()
    result = await bind9_record_delete(
        connector, _FakeBind9Target(), {"fqdn": "web.example.test", "type": "A"}
    )
    assert result["status"] == "deleted"
    assert result["deleted"] is True
    assert result["rdata"] == "192.0.2.20"
    assert result["zone"] == _ZONE
    assert result["file"] == _ZONEFILE_PATH
    assert result["op_class"] == "write"
    assert result["result_state_before"] == "<old>"
    assert result["result_state_after"] == "<new>"
    assert len(connector.sudo_calls) == 1  # exactly one atomic apply


async def test_handler_rdata_pins_one_of_multi() -> None:
    connector = _RecordingBind9Connector()
    result = await bind9_record_delete(
        connector,
        _FakeBind9Target(),
        {"fqdn": "api.example.test", "type": "A", "rdata": "192.0.2.10"},
    )
    assert result["status"] == "deleted"
    assert result["rdata"] == "192.0.2.10"
    # The staged zonefile keeps the sibling value (192.0.2.11).
    staged = _staged_zonefile(connector.sudo_calls[0])
    assert "192.0.2.11" in staged
    assert "192.0.2.10" not in staged


async def test_handler_ambiguous_refuses_with_candidates_no_write() -> None:
    connector = _RecordingBind9Connector()
    result = await bind9_record_delete(
        connector, _FakeBind9Target(), {"fqdn": "api.example.test", "type": "A"}
    )
    assert result["status"] == "ambiguous"
    assert result["deleted"] is False
    assert result["candidates"] == ["192.0.2.10", "192.0.2.11"]
    assert connector.sudo_calls == []  # nothing staged


async def test_handler_not_found_refuses_no_write() -> None:
    connector = _RecordingBind9Connector()
    result = await bind9_record_delete(
        connector, _FakeBind9Target(), {"fqdn": "nope.example.test", "type": "A"}
    )
    assert result["status"] == "not_found"
    assert result["deleted"] is False
    assert connector.sudo_calls == []


async def test_handler_not_found_when_rdata_absent() -> None:
    connector = _RecordingBind9Connector()
    result = await bind9_record_delete(
        connector,
        _FakeBind9Target(),
        {"fqdn": "api.example.test", "type": "A", "rdata": "192.0.2.99"},
    )
    assert result["status"] == "not_found"
    assert result["deleted"] is False
    assert connector.sudo_calls == []


async def test_handler_unmanaged_zone_refuses_no_write() -> None:
    connector = _RecordingBind9Connector(checkconf=_CHECKCONF_OTHER)
    result = await bind9_record_delete(
        connector, _FakeBind9Target(), {"fqdn": "web.example.test", "type": "A"}
    )
    assert result["status"] == "unmanaged_zone"
    assert result["deleted"] is False
    assert connector.sudo_calls == []


async def test_handler_multiview_without_view_is_unmanaged() -> None:
    connector = _RecordingBind9Connector(checkconf=_CHECKCONF_MULTIVIEW)
    result = await bind9_record_delete(
        connector, _FakeBind9Target(), {"fqdn": "web.example.test", "type": "A"}
    )
    assert result["status"] == "unmanaged_zone"
    assert "view" in result["guidance"].lower()
    assert connector.sudo_calls == []


async def test_handler_view_uses_zonestatus_verify() -> None:
    connector = _RecordingBind9Connector(
        checkconf=_CHECKCONF_MULTIVIEW,
        zonefile="$TTL 3600\n@ IN SOA ns1.example.test. admin.example.test. "
        "( 5 3600 600 604800 86400 )\n@ IN NS ns1.example.test.\nweb IN A 192.0.2.20\n",
    )
    result = await bind9_record_delete(
        connector,
        _FakeBind9Target(),
        {"fqdn": "web.example.test", "type": "A", "view": "internal"},
    )
    assert result["status"] == "deleted"
    assert result["view"] == "internal"
    # A view switches the verify predicate to the view-precise zonestatus.
    assert "rndc zonestatus" in connector.sudo_calls[0]


async def test_handler_rejects_unsupported_type() -> None:
    connector = _RecordingBind9Connector()
    with pytest.raises(ValueError, match="A / AAAA"):
        await bind9_record_delete(
            connector, _FakeBind9Target(), {"fqdn": "web.example.test", "type": "CNAME"}
        )


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
    """preview → park (hash + blast radius) → distinct human approve → atomic delete."""
    recorder = _RecordingBind9Connector()
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    args = _args()  # web.example.test A — a single-value record

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

    # 3) The parked row carries the bound hash + the scoped blast radius.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        assert row.preview_hash == bound_hash
        effect = dict(row.proposed_effect)
    assert effect["safety_level"] == "destructive"
    blast = effect["blast_radius"]
    assert blast["object"] == {
        "kind": "dns_record",
        "zone": _ZONE,
        "name": "web.example.test.",
        "type": "A",
        "view": None,
    }
    assert blast["children"] == [{"kind": "record_value", "type": "A", "rdata": "192.0.2.20"}]
    assert blast["irreversibility"] == "recreatable"

    # 4) A DIFFERENT operator approves (four-eyes) — the binding re-verifies.
    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"

    # 5) Audited resume → the atomic delete stages exactly once.
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "deleted"
    assert resume.result["deleted"] is True
    assert resume.result["rdata"] == "192.0.2.20"
    assert len(recorder.sudo_calls) == 1
    # The verify predicate scopes to the deleted value, not the whole name.
    assert "grep -qxF 192.0.2.20" in recorder.sudo_calls[0]


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
        params={"fqdn": "web.example.test", "type": "A"},
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
    args = _args()
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
        params={"fqdn": "web.example.test", "type": "A"},
    )
    assert result.status == "denied", result
    assert recorder.sudo_calls == []  # never executed
    assert await _pending_count() == 0  # never parked either


# ===========================================================================
# No standing-grant path — ServicePrincipalGrant refuses the op
# ===========================================================================


async def test_service_grant_refuses_delete() -> None:
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
    assert "delete-shaped" in str(exc.value).lower() or "destructive" in str(exc.value).lower()


async def test_service_grant_refuses_via_single_source_not_pattern_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adversarial (b): the op folds destructive via the SINGLE SOURCE.

    Blank out the op-id pattern list (``service_grant_delete_shaped_patterns``)
    so the glob check cannot fire, register the descriptor, and prove the
    grant is STILL refused — by ``safety_level="destructive"`` on the resolved
    descriptor (``_delete_shaped_reason_by_descriptor``), never a local list.
    This is the #3213 fail-open guard: classification is single-sourced.
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
            params={"fqdn": "web.example.test", "type": "A"},
            runner_id="runner-1",
        )
        await s.commit()
    assert not result.minted
    assert result.refusal_code is MintRefusalCode.OP_NOT_SAFE


# ===========================================================================
# Structural gap 1 — registration fail-fast: destructive ⇒ requires_approval
# ===========================================================================


async def test_registration_rejects_destructive_without_requires_approval() -> None:
    with (
        pytest.raises(ValueError, match="requires_approval"),
        patch.object(tr_module, "encode_endpoint_text", AsyncMock(return_value=[0.1] * 384)),
    ):
        await register_composite_operation(
            product="bind9",
            version="9.x",
            impl_id="bind9-ssh",
            op_id="bind9.record.delete_broken",
            handler=bind9_record_delete,
            summary="broken destructive op",
            description="a destructive op that (illegally) does not require approval",
            parameter_schema={"type": "object", "properties": {"fqdn": {"type": "string"}}},
            when_to_use=None,
            group_key=None,
            safety_level="destructive",
            requires_approval=False,
        )


# ===========================================================================
# Structural gap 2 — a USER never auto-executes destructive, even if the
# stored descriptor is (mis)declared requires_approval=False
# ===========================================================================


async def test_user_never_auto_executes_destructive_without_requires_approval() -> None:
    recorder = _RecordingBind9Connector()
    await _bootstrap(recorder)
    await _seed_target()

    async with get_sessionmaker()() as s:
        await s.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id == _OP_ID)
            .values(requires_approval=False)
        )
        await s.commit()
    reset_dispatcher_caches()
    _seed_instance(recorder)

    requester = _make_operator(sub="op-requester")
    args = _args()
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})

    assert call["status"] == "awaiting_approval", call
    assert recorder.sudo_calls == []  # no write ran despite requires_approval=False


# ===========================================================================
# The typed-op preview hash is param-sensitive (the #3197 binding on a typed op)
# ===========================================================================


async def test_preview_hash_is_param_sensitive() -> None:
    recorder = _RecordingBind9Connector()
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    base = {"connector_id": _CONNECTOR_ID, "op_id": _OP_ID, "target": _TARGET_NAME}

    p1 = await preview_operation(
        requester, {**base, "params": {"fqdn": "web.example.test", "type": "A"}}
    )
    p1b = await preview_operation(
        requester, {**base, "params": {"fqdn": "web.example.test", "type": "A"}}
    )
    p2 = await preview_operation(
        requester,
        {**base, "params": {"fqdn": "web.example.test", "type": "A", "rdata": "192.0.2.20"}},
    )
    assert p1["status"] == "ok"
    assert p1["preview_hash"] == p1b["preview_hash"]  # stable for identical args
    assert p1["preview_hash"] != p2["preview_hash"]  # a different delete → different hash


# ===========================================================================
# Adversarial (a) — a multi-value park names the record + enumerates siblings;
# the resolved delete removes only ONE value
# ===========================================================================


async def test_multi_value_park_blast_radius_then_scoped_delete() -> None:
    recorder = _RecordingBind9Connector()
    await _bootstrap(recorder)
    await _seed_target()

    requester = _make_operator(sub="op-requester")
    # api.example.test has two A values; pin one with rdata.
    args = _args(params={"fqdn": "api.example.test", "type": "A", "rdata": "192.0.2.10"})
    preview = await preview_operation(requester, args)
    call = await call_operation(requester, {**args, "preview_hash": preview["preview_hash"]})
    request_id = UUID(call["extras"]["approval_request_id"])

    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        blast = dict(row.proposed_effect)["blast_radius"]
    # The object names the exact record incl. the pinned rdata; children
    # enumerate BOTH current values so the approver sees the full set.
    assert blast["object"]["name"] == "api.example.test."
    assert blast["object"]["rdata"] == "192.0.2.10"
    assert {c["rdata"] for c in blast["children"]} == {"192.0.2.10", "192.0.2.11"}

    approver = _make_operator(sub="op-approver")
    async with get_sessionmaker()() as s:
        await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        resume = await resume_dispatch_after_approval(operator=approver, request=row, params=None)
    assert resume.result["status"] == "deleted"
    # Only the pinned value is staged out; the sibling survives.
    staged = _staged_zonefile(recorder.sudo_calls[0])
    assert "192.0.2.11" in staged
    assert "192.0.2.10" not in staged
