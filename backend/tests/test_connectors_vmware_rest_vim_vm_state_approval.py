# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Older-API VM power, clone, snapshot and migrate calls wait for approval (#3869).

The older vCenter API (VI-JSON) sends every method as a ``POST`` to
``/<Type>/{moId}/<Method>``. So the parser gave ``TerminateVM``,
``PowerOnMultiVM_Task``, ``CloneVM_Task``, ``CreateSnapshot_Task`` and their
neighbours ``caution`` without approval, and a person's call ran at once.

The VMware floor (``ingest_safety.py``) now raises the power, terminate,
clone, snapshot revert and migrate calls to ``dangerous`` with approval on the
8.0 and 9.0 catalogs. The two snapshot creates keep ``caution`` and wait for
approval, like the typed ``vm.snapshot.create``.

The pinned-spec lane checks every rule against the vendor file on the spec
shelf; it skips when the shelf is not configured (see ``tests/_spec_shelf.py``)
and runs in CI.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.connectors.vmware_rest.ingest_safety import (
    VMWARE_VIM_SNAPSHOT_CREATES,
    VMWARE_VIM_VM_STATE_WRITES,
    is_vmware_vim_snapshot_create,
    is_vmware_vim_vm_state_write,
    vmware_rest_safety_floor,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.ingest import parse_openapi
from meho_backplane.operations.ingest.safety_floors import (
    apply_safety_floor,
    register_ingest_safety_floor,
)
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto
from meho_backplane.settings import get_settings
from tests._spec_shelf import assert_op_ids_served, require_shelf_spec

_TERMINATE = "POST:/VirtualMachine/{moId}/TerminateVM"
_CREATE_SNAPSHOT = "POST:/VirtualMachine/{moId}/CreateSnapshot_Task"

#: Raised to ``dangerous`` with approval.
_DANGEROUS_OPS = frozenset(
    {
        "POST:/VirtualMachine/{moId}/PowerOnVM_Task",
        "POST:/VirtualMachine/{moId}/PowerOffVM_Task",
        "POST:/VirtualMachine/{moId}/ResetVM_Task",
        "POST:/VirtualMachine/{moId}/SuspendVM_Task",
        "POST:/VirtualMachine/{moId}/ShutdownGuest",
        "POST:/VirtualMachine/{moId}/RebootGuest",
        "POST:/VirtualMachine/{moId}/StandbyGuest",
        _TERMINATE,
        "POST:/VirtualMachine/{moId}/TerminateFaultTolerantVM_Task",
        "POST:/VirtualMachine/{moId}/CloneVM_Task",
        "POST:/VirtualMachine/{moId}/InstantClone_Task",
        "POST:/VirtualMachine/{moId}/MigrateVM_Task",
        "POST:/VirtualMachine/{moId}/RevertToCurrentSnapshot_Task",
        "POST:/VirtualMachineSnapshot/{moId}/RevertToSnapshot_Task",
        "POST:/Datacenter/{moId}/PowerOnMultiVM_Task",
        "POST:/VirtualApp/{moId}/PowerOnVApp_Task",
        "POST:/VirtualApp/{moId}/PowerOffVApp_Task",
        "POST:/VirtualApp/{moId}/SuspendVApp_Task",
        "POST:/VirtualApp/{moId}/CloneVApp_Task",
        "POST:/HostAutoStartManager/{moId}/AutoStartPowerOn",
        "POST:/HostAutoStartManager/{moId}/AutoStartPowerOff",
    }
)
#: Keep ``caution`` and wait for approval.
_SNAPSHOT_CREATES = frozenset(
    {_CREATE_SNAPSHOT, "POST:/VirtualMachine/{moId}/CreateSnapshotEx_Task"}
)
_ALL_GATED = _DANGEROUS_OPS | _SNAPSHOT_CREATES

#: Reads and checks next to the gated ones keep their parsed level.
_UNCHANGED = frozenset(
    {
        "POST:/VirtualMachineCompatibilityChecker/{moId}/CheckPowerOn_Task",
        "POST:/VirtualMachineProvisioningChecker/{moId}/CheckClone_Task",
        "POST:/VirtualMachine/{moId}/EstimateStorageForConsolidateSnapshots_Task",
        "POST:/VirtualMachineSnapshot/{moId}/RenameSnapshot",
        "POST:/GuestProcessManager/{moId}/TerminateProcessInGuest",
        "GET:/VirtualMachine/{moId}/rootSnapshot",
    }
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000003869")


def _hand_made_spec() -> str:
    """One small OpenAPI file with every gated op and the neighbours that stay."""
    lines = [
        "openapi: 3.0.3",
        "info: {title: vim vm state fixture, version: '1.0'}",
        "paths:",
    ]
    for op_id in sorted(_ALL_GATED | _UNCHANGED):
        method, path = op_id.split(":", 1)
        lines.append(f"  '{path}':")
        lines.append(f"    {method.lower()}:")
        lines.append("      responses: {'200': {description: ok}}")
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def _vmware_floor_registered() -> None:
    """Make sure the VMware floor is registered, whatever ran before this test."""
    register_ingest_safety_floor(
        product="vmware", impl_id="vmware-rest", floor=vmware_rest_safety_floor
    )


def _floored(
    protos: list[EndpointDescriptorProto], version: str
) -> dict[str, EndpointDescriptorProto]:
    return {
        p.op_id: apply_safety_floor(
            product="vmware", version=version, impl_id="vmware-rest", proto=p
        )
        for p in protos
    }


def _ingested(version: str) -> dict[str, EndpointDescriptorProto]:
    rows = parse_openapi("file:///vim-vm-state.yaml", content=_hand_made_spec())
    return _floored(rows, version)


def _expected_level(op_id: str) -> str:
    return "caution" if op_id in _SNAPSHOT_CREATES else "dangerous"


# ---------------------------------------------------------------------------
# Ingest of a hand-made spec
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["8.0", "9.0"])
def test_every_gated_operation_waits_for_approval(version: str) -> None:
    ops = _ingested(version)

    assert set(ops) == _ALL_GATED | _UNCHANGED
    for op_id in _ALL_GATED:
        assert ops[op_id].requires_approval is True, op_id
        assert ops[op_id].safety_level == _expected_level(op_id), op_id


@pytest.mark.parametrize("op_id", sorted(_UNCHANGED))
def test_reads_and_checks_keep_their_level(op_id: str) -> None:
    rows = {p.op_id: p for p in parse_openapi("file:///a.yaml", content=_hand_made_spec())}
    ops = _ingested("9.0")

    assert ops[op_id].safety_level == rows[op_id].safety_level
    assert ops[op_id].requires_approval is False


def test_rules_do_not_apply_to_an_unlisted_catalog_version() -> None:
    ops = _ingested("7.0")

    for op_id in (_TERMINATE, _CREATE_SNAPSHOT):
        assert ops[op_id].safety_level == "caution"
        assert ops[op_id].requires_approval is False


@pytest.mark.parametrize("op_id", sorted(_ALL_GATED))
def test_no_rule_lowers_a_level(op_id: str) -> None:
    """A row that is already ``destructive`` stays ``destructive`` with approval."""
    method, path = op_id.split(":", 1)
    proto = EndpointDescriptorProto(
        op_id=op_id,
        method=method,
        path=path,
        parameter_schema={"type": "object", "properties": {}},
        safety_level="destructive",
        requires_approval=True,
    )

    floored = apply_safety_floor(
        product="vmware", version="9.0", impl_id="vmware-rest", proto=proto
    )

    assert floored.safety_level == "destructive"
    assert floored.requires_approval is True


#: ``(method, path, the power/clone rule accepts it, the snapshot rule accepts it)``.
_RULE_CASES: list[tuple[str, str, bool, bool]] = [
    ("POST", "/VirtualMachine/{moId}/TerminateVM", True, False),
    ("POST", "/api/VirtualMachine/{vm}/TerminateVM", True, False),
    ("post", "/VirtualMachine/{moId}/PowerOnVM_Task", True, False),
    ("POST", "/VirtualMachine/{moId}/CreateSnapshot_Task", False, True),
    ("POST", "/api/VirtualMachine/{id}/CreateSnapshotEx_Task", False, True),
    ("GET", "/VirtualMachine/{moId}/TerminateVM", False, False),
    ("POST", "/VirtualMachine/{moId}/TerminateVM?x=1", False, False),
    ("POST", "/VirtualMachine/{moId}/ReconfigVM_Task", False, False),
    ("POST", "/HostSystem/{moId}/PowerOnVM_Task", False, False),
]


@pytest.mark.parametrize(("method", "path", "state_write", "snapshot_create"), _RULE_CASES)
def test_rule_matching(method: str, path: str, state_write: bool, snapshot_create: bool) -> None:
    assert is_vmware_vim_vm_state_write(method, path) is state_write
    assert is_vmware_vim_snapshot_create(method, path) is snapshot_create


def test_every_rule_is_in_the_test_table() -> None:
    """The floor lists and this test's lists are the same operations."""

    def keys(op_ids: frozenset[str]) -> set[tuple[str, str]]:
        out = set()
        for op_id in op_ids:
            method, path = op_id.split(":", 1)
            out.add((method, re.sub(r"\{[^/{}]+\}", "{}", path)))
        return out

    assert keys(_DANGEROUS_OPS) == VMWARE_VIM_VM_STATE_WRITES
    assert keys(_SNAPSHOT_CREATES) == VMWARE_VIM_SNAPSHOT_CREATES


# ---------------------------------------------------------------------------
# A person's call waits for approval
# ---------------------------------------------------------------------------


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


class _FakeTarget:
    def __init__(self) -> None:
        self.product = "vmware"
        self.fingerprint = type("_Fingerprint", (), {"version": "9.0"})()
        self.preferred_impl_id: str | None = "vmware-rest"
        self.id = uuid.uuid4()
        self.name = "vcenter-test"
        self.host = "vcenter.test"
        self.port = 443
        self.auth_model: str | None = "shared_service_account"
        self.secret_ref: str | None = None
        self.fqdn: str | None = None


class _FakeVcenter(HttpConnector):
    """vCenter stand-in that records every call it gets."""

    product = "vmware"
    version = "9.0"
    impl_id = "vmware-rest"
    supported_version_range = ">=9.0,<10.0"
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def _request_json(  # type: ignore[override]
        self, target: Any, method: str, path: str, **_kw: Any
    ) -> Any:
        self.calls.append(f"{method} {path}")
        return {}

    async def _post_json(  # type: ignore[override]
        self, target: Any, path: str, *, verb: str = "POST", **_kw: Any
    ) -> Any:
        self.calls.append(f"{verb} {path}")
        return {}

    async def fingerprint(  # type: ignore[override]
        self, target: Any, operator: Operator | None = None
    ) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError


async def _store_ingested(op_id: str) -> None:
    """Store *op_id* with the level a fresh ingest gives it."""
    proto = _ingested("9.0")[op_id]
    async with get_sessionmaker()() as session:
        session.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="vmware",
                version="9.0",
                impl_id="vmware-rest",
                op_id=op_id,
                source_kind="ingested",
                method=proto.method,
                path=proto.path,
                handler_ref=None,
                summary="Older-API VM call.",
                tags=[],
                parameter_schema={
                    "type": "object",
                    "properties": {"moId": {"type": "string", "x-meho-param-loc": "path"}},
                },
                safety_level=proto.safety_level,
                requires_approval=proto.requires_approval,
                is_enabled=True,
                embedding=[0.1] * 384,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.parametrize("op_id", [_TERMINATE, _CREATE_SNAPSHOT])
async def test_a_persons_call_waits_for_approval(op_id: str) -> None:
    register_connector_v2(product="vmware", version="9.0", impl_id="vmware-rest", cls=_FakeVcenter)
    connector = get_or_create_connector_instance(_FakeVcenter)
    assert isinstance(connector, _FakeVcenter)
    await _store_ingested(op_id)
    person = Operator(
        sub="op-requester",
        name="VM Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )

    result = await dispatch(
        operator=person,
        connector_id="vmware-rest-9.0",
        op_id=op_id,
        target=_FakeTarget(),
        params={"moId": "vm-42"},
    )

    assert result.status == "awaiting_approval", result
    assert connector.calls == []
    async with get_sessionmaker()() as session:
        count = (
            await session.execute(select(func.count()).select_from(ApprovalRequest))
        ).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# The pinned vendor file serves the rules (shelf-gated, runs in CI)
# ---------------------------------------------------------------------------


def test_pinned_vi_json_serves_the_rules() -> None:
    """Every rule matches an operation in the pinned 9.0 file, and lands waiting for approval."""
    spec_path = require_shelf_spec("vcenter-9.0", "vi-json.yaml")
    rows = parse_openapi(f"file://{spec_path}", content=spec_path.read_text(encoding="utf-8"))
    ops = _floored(rows, "9.0")

    assert_op_ids_served(_ALL_GATED, set(ops), spec_label="vcenter-9.0/vi-json.yaml")
    for op_id in _ALL_GATED:
        assert ops[op_id].requires_approval is True, op_id
        assert ops[op_id].safety_level == _expected_level(op_id), op_id
