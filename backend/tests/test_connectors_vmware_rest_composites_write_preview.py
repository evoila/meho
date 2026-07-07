# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time ``proposed_effect`` previews for the 8 vmware write composites.

G0.22-T3 (#1608) acceptance criteria:

1. A parked ``vmware.composite.vm.power.bulk`` request's
   ``proposed_effect`` carries the requested ``action``, the ``filter``
   echoed, and the ``resolved`` (capped) entity list + ``total_resolved``
   — asserted against the durable :class:`ApprovalRequest` row through
   the production :func:`~meho_backplane.operations.dispatch` park path.
2. The preview path issues **only reads**: the park records exactly one
   ``GET:/vcenter/vm`` leaf call and no power sub-op; the read-only
   adapter additionally refuses any non-``GET:`` op_id structurally.
3. The resolution logic is shared, not duplicated — the builders call
   the same :func:`._write._resolve_vm_list` /
   :func:`._write._resolve_cluster_hosts` helpers the handlers use
   (asserted via the wiring test + the helpers' single definition site).
4. All 8 write composites register a builder (4 live-read + 4 param
   echo); the wiring test pins the full set.

Plus the #1628 follow-up: a *failed* live-read preview parks with the
identifier fields **and** an explicit ``preview_unavailable`` marker +
reason (visible through the REST / MCP serialisation helpers), so the
reviewer can tell "blast-radius unknown" from a genuinely small action.

Harness: mirrors :mod:`tests.test_connectors_vmware_rest_composites_write_e2e`
(recording leaf typed-ops + in-process SQLite dispatcher), trimmed to the
two listing leaves the park-time previews actually read — at park time
the composite handler never runs, so no other leaf descriptor is needed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.connectors.vmware_rest.composites import (
    _write_preview,
    register_vmware_composite_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-00000000a0a8")

_WRITE_COMPOSITE_OP_IDS: frozenset[str] = frozenset(
    {
        "vmware.composite.vm.create",
        "vmware.composite.vm.clone",
        "vmware.composite.vm.snapshot.revert",
        "vmware.composite.vm.migrate",
        "vmware.composite.vm.power.bulk",
        "vmware.composite.host.evacuate",
        "vmware.composite.host.detach_from_vds",
        "vmware.composite.cluster.patch",
    }
)


# ---------------------------------------------------------------------------
# Fixtures (mirror tests.test_connectors_vmware_rest_composites_write_e2e)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for this module."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher caches + connector registry around each test."""
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    """Deterministic embedding stub so registrations skip ONNX."""
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture(autouse=True)
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Replace the audit path's :func:`publish_event` with a recording stub."""
    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(audit_module, "publish_event", _capture)
    return events


def _make_operator(*, sub: str = "op-vmware-write-preview") -> Operator:
    """Synthetic USER operator — the principal kind the approval park routes."""
    return Operator(
        sub=sub,
        name="VMware Write Preview Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


class _FakeFingerprint:
    """Duck-typed fingerprint for the resolver."""

    def __init__(self, version: str | None = "9.0") -> None:
        self.version = version


class _FakeVmwareTarget:
    """Minimal target the resolver / dispatcher reads from."""

    def __init__(self, target_id: UUID | None = None) -> None:
        self.product = "vmware"
        self.fingerprint = _FakeFingerprint(version="9.0")
        self.preferred_impl_id: str | None = "vmware-rest"
        self.id: UUID = target_id or uuid.uuid4()
        # Tenant-unique cache key component (#1642/#1672); without it
        # ``target_cache_key`` raises AttributeError at runtime.
        self.tenant_id: UUID = _TENANT_ID
        self.name = "test-vcenter"
        self.host = "vcenter.test"
        self.port = 443
        self.auth_model = "shared_service_account"


class _NoOpVmwareConnector(Connector):
    """Resolver-satisfying connector — the typed leaf path never invokes it."""

    product = "vmware"
    version = "9.0"
    impl_id = "vmware-rest"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self,
        target: Any,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Recording leaf stubs — only the two listing reads the previews issue
# ---------------------------------------------------------------------------

#: Ordered record of every leaf op_id dispatched in the current test.
_CALLS: list[tuple[str, dict[str, Any]]] = []

#: Per-op canned response payload (vSphere REST envelope shape).
_RESPONSES: dict[str, Any] = {}

#: op_ids whose stub should raise (drives the fail-soft branch).
_FAILURES: dict[str, str] = {}


def _record(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Append a leaf call and resolve its canned payload, raising on failure."""
    _CALLS.append((op_id, dict(params)))
    if op_id in _FAILURES:
        raise RuntimeError(_FAILURES[op_id])
    return _RESPONSES.get(op_id, {"value": []})


async def _h_list_vms(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("GET:/vcenter/vm", params)


async def _h_list_cluster_hosts(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _record("GET:/vcenter/cluster/{cluster}/host", params)


_LEAF_HANDLERS: dict[str, Any] = {
    "GET:/vcenter/vm": _h_list_vms,
    "GET:/vcenter/cluster/{cluster}/host": _h_list_cluster_hosts,
}


def _reset_recorder() -> None:
    _CALLS.clear()
    _RESPONSES.clear()
    _FAILURES.clear()


async def _bootstrap_registry(stub_embedding_service: AsyncMock) -> None:
    """Register the connector, the 15 composites, and the listing leaves."""
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=_NoOpVmwareConnector,
    )
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    permissive_schema = {"type": "object", "additionalProperties": True}
    for op_id, handler in _LEAF_HANDLERS.items():
        await register_typed_operation(
            product="vmware",
            version="9.0",
            impl_id="vmware-rest",
            op_id=op_id,
            handler=handler,
            summary=f"Stub leaf op {op_id} for the write-preview tests.",
            description=f"Stub leaf op {op_id} for the write-preview tests.",
            parameter_schema=permissive_schema,
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )
    _reset_recorder()


def _ops_in_calls() -> list[str]:
    return [op_id for op_id, _ in _CALLS]


async def _park(
    op_id: str,
    params: dict[str, Any],
    *,
    target: _FakeVmwareTarget | None = None,
) -> tuple[OperationResult, ApprovalRequest]:
    """Dispatch *op_id* as a USER (no grant) and return (result, parked row)."""
    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        target=target or _FakeVmwareTarget(),
        params=params,
    )
    assert result.status == "awaiting_approval", result.error
    request_id = UUID(result.extras["approval_request_id"])
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
    assert row is not None
    assert row.status == ApprovalRequestStatus.PENDING.value
    return result, row


# ===========================================================================
# Wiring — all 8 write composites register a builder (criterion 4)
# ===========================================================================


def test_all_eight_write_composites_register_a_preview_builder() -> None:
    """Importing the composites package wires a builder per write composite.

    The side-effect import in ``composites/__init__`` must register every
    ``requires_approval=True`` composite; a rename or dropped entry would
    silently regress that op to the identifier-only default.
    """
    assert set(_write_preview._WRITE_PREVIEW_BUILDERS) == set(_WRITE_COMPOSITE_OP_IDS)
    for op_id, builder in _write_preview._WRITE_PREVIEW_BUILDERS.items():
        assert _PREVIEW_BUILDERS.get(op_id) is builder, op_id


# ===========================================================================
# Read-only adapter — structural GET-only guard (criterion 2, belt)
# ===========================================================================


def _make_preview_ctx(params: dict[str, Any]) -> PreviewContext:
    """A :class:`PreviewContext` for builder-direct unit tests."""
    return PreviewContext(
        descriptor=object(),  # type: ignore[arg-type]  # echo builders ignore it
        connector_instance=None,
        operator=_make_operator(),
        target=_FakeVmwareTarget(),
        params=params,
    )


async def test_read_only_adapter_refuses_non_get_op_ids() -> None:
    """The adapter rejects any mutating op_id before lookup or I/O fires."""
    adapter = _write_preview._read_only_dispatch_child(_make_preview_ctx({}))
    for op_id in (
        "POST:/vcenter/vm/{vm}/power?action=start",
        "DELETE:/vcenter/vm/{vm}",
        "PATCH:/vcenter/vm/{vm}/network",
    ):
        result = await adapter(connector_id=_CONNECTOR_ID, op_id=op_id, params={})
        assert result.status == "error"
        assert "GET-only" in (result.error or "")


# ===========================================================================
# Echo builders — params fully name the blast radius (no I/O)
# ===========================================================================


async def test_vm_create_preview_echoes_creation_spec() -> None:
    preview = await _write_preview._vm_create_preview(
        _make_preview_ctx(
            {
                "folder_name": "prod",
                "name": "vm-new",
                "guest_os": "UBUNTU_64",
                "cpu_count": 4,
                "memory_mib": 8192,
                "nics": [{"network": "net-1"}, {"network": "net-2"}],
                "power_on_after_create": True,
            }
        )
    )
    assert preview == {
        "name": "vm-new",
        "guest_os": "UBUNTU_64",
        "folder_name": "prod",
        "cpu_count": 4,
        "memory_mib": 8192,
        "networks": ["net-1", "net-2"],
        "power_on_after_create": True,
    }


async def test_vm_create_preview_mirrors_handler_defaults() -> None:
    preview = await _write_preview._vm_create_preview(
        _make_preview_ctx({"folder_name": "prod", "name": "vm-new", "guest_os": "UBUNTU_64"})
    )
    assert preview is not None
    assert preview["cpu_count"] == 1
    assert preview["memory_mib"] == 1024
    assert preview["networks"] == []
    assert preview["power_on_after_create"] is False


async def test_vm_clone_preview_echoes_clone_coordinates() -> None:
    preview = await _write_preview._vm_clone_preview(
        _make_preview_ctx(
            {
                "source_vm": "vm-1",
                "target_name": "vm-clone",
                "library_item": "lib-1",
                "wait_for_completion": False,
            }
        )
    )
    assert preview == {
        "source_vm": "vm-1",
        "target_name": "vm-clone",
        "library_item": "lib-1",
        "wait_for_completion": False,
    }


async def test_vm_snapshot_revert_preview_echoes_revert_coordinates() -> None:
    preview = await _write_preview._vm_snapshot_revert_preview(
        _make_preview_ctx({"vm": "vm-1", "snapshot_name": "pre-upgrade"})
    )
    assert preview == {"vm": "vm-1", "snapshot_name": "pre-upgrade"}


async def test_vm_migrate_preview_names_target_host_resolution() -> None:
    """Operator-pinned host echoes verbatim; DRS path is honest about runtime resolution."""
    pinned = await _write_preview._vm_migrate_preview(
        _make_preview_ctx({"vm": "vm-1", "cluster": "domain-c1", "target_host": "host-9"})
    )
    assert pinned == {
        "vm": "vm-1",
        "cluster": "domain-c1",
        "target_host": "host-9",
        "target_host_source": "operator",
    }
    drs = await _write_preview._vm_migrate_preview(
        _make_preview_ctx({"vm": "vm-1", "cluster": "domain-c1"})
    )
    assert drs == {
        "vm": "vm-1",
        "cluster": "domain-c1",
        "target_host": None,
        "target_host_source": "drs_at_execution",
    }


async def test_echo_builders_decline_on_malformed_params() -> None:
    """Missing required params decline (→ identifier-only default), never raise."""
    assert await _write_preview._vm_create_preview(_make_preview_ctx({})) is None
    assert await _write_preview._vm_clone_preview(_make_preview_ctx({})) is None
    assert await _write_preview._vm_snapshot_revert_preview(_make_preview_ctx({})) is None
    assert await _write_preview._vm_migrate_preview(_make_preview_ctx({})) is None
    assert await _write_preview._vm_power_bulk_preview(_make_preview_ctx({})) is None
    assert await _write_preview._host_evacuate_preview(_make_preview_ctx({})) is None
    assert await _write_preview._host_detach_from_vds_preview(_make_preview_ctx({})) is None
    assert await _write_preview._cluster_patch_preview(_make_preview_ctx({})) is None


# ===========================================================================
# vm.power.bulk — the parked row carries action + filter + resolved set
# (criteria 1 + 2, through the production dispatch park path)
# ===========================================================================


async def test_power_bulk_park_carries_action_filter_and_resolved_set(
    stub_embedding_service: AsyncMock,
) -> None:
    """The parked row's ``proposed_effect`` names the blast radius (criterion 1).

    The reviewer sees the requested ``action``, the ``filter`` echoed, and
    the resolved VM identities with the uncapped count — not just
    ``{op_id, connector_id, target_id}``.
    """
    await _bootstrap_registry(stub_embedding_service)
    _RESPONSES["GET:/vcenter/vm"] = {
        "value": [
            {"vm": "vm-a", "name": "web-a", "power_state": "POWERED_ON", "cpu_count": 2},
            {"vm": "vm-b", "name": "web-b", "power_state": "POWERED_OFF", "cpu_count": 4},
        ]
    }

    _, row = await _park(
        "vmware.composite.vm.power.bulk",
        {"action": "stop", "filter": {"names": ["web-*"]}},
    )

    assert row.proposed_effect == {
        "op_class": "other",
        "preview": {
            "action": "stop",
            "filter": {"names": ["web-*"]},
            "resolved": [
                {"vm": "vm-a", "name": "web-a", "power_state": "POWERED_ON"},
                {"vm": "vm-b", "name": "web-b", "power_state": "POWERED_OFF"},
            ],
            "total_resolved": 2,
        },
        "safety_level": "dangerous",
    }
    # The filter reached the listing read in the handler's wire shape.
    assert _CALLS == [("GET:/vcenter/vm", {"filter.names": ["web-*"]})]


async def test_power_bulk_park_issues_only_the_listing_read(
    stub_embedding_service: AsyncMock,
) -> None:
    """No power mutation fires on the park path (criterion 2).

    The recorded leaf calls contain exactly the one listing GET — no
    ``POST:/vcenter/vm/{vm}/power?action=...`` — even though the listing
    resolved VMs the approved dispatch would act on.
    """
    await _bootstrap_registry(stub_embedding_service)
    _RESPONSES["GET:/vcenter/vm"] = {"value": [{"vm": "vm-a"}, {"vm": "vm-b"}]}

    await _park("vmware.composite.vm.power.bulk", {"action": "start"})

    assert _ops_in_calls() == ["GET:/vcenter/vm"]


async def test_power_bulk_resolved_list_is_capped_with_true_total(
    stub_embedding_service: AsyncMock,
) -> None:
    """A wide filter caps ``resolved`` at the preview cap; ``total_resolved`` is uncapped."""
    await _bootstrap_registry(stub_embedding_service)
    _RESPONSES["GET:/vcenter/vm"] = {
        "value": [{"vm": f"vm-{i}", "name": f"node-{i}"} for i in range(25)]
    }

    _, row = await _park("vmware.composite.vm.power.bulk", {"action": "suspend"})

    preview = row.proposed_effect["preview"]
    assert len(preview["resolved"]) == _write_preview._PREVIEW_RESOLVED_CAP
    assert preview["total_resolved"] == 25
    assert preview["resolved"][0] == {"vm": "vm-0", "name": "node-0"}


async def test_power_bulk_preview_failure_parks_with_unavailable_marker(
    stub_embedding_service: AsyncMock,
) -> None:
    """A failing listing read parks with identifiers + an explicit marker (#1628).

    Pre-#1628 the row degraded silently to the bare identifier-only
    default — indistinguishable from a genuinely small action, defeating
    the four-eyes purpose of the preview on any deploy where the L2 read
    can't execute. The park (the safety-relevant action) still proceeds
    — ``_park`` asserts ``awaiting_approval`` + a PENDING row — but the
    reviewer now sees "blast-radius unknown" plus the read's failure
    reason alongside the identifier fields.
    """
    await _bootstrap_registry(stub_embedding_service)
    _FAILURES["GET:/vcenter/vm"] = "vCenter listing unavailable"

    target = _FakeVmwareTarget()
    _, row = await _park(
        "vmware.composite.vm.power.bulk",
        {"action": "reset"},
        target=target,
    )

    effect = row.proposed_effect
    # The identifier fields stay — the marker rides alongside them.
    assert effect["op_id"] == "vmware.composite.vm.power.bulk"
    assert effect["connector_id"] == _CONNECTOR_ID
    assert effect["target_id"] == str(target.id)
    assert effect["op_class"] == "other"
    assert effect["preview_unavailable"] is True
    # The reason names the failed listing read and its error, not just
    # "failed" — the reviewer learns *what* could not be resolved.
    assert "GET:/vcenter/vm" in effect["preview_error"]
    assert "vCenter listing unavailable" in effect["preview_error"]
    # No "preview" envelope key: a failed preview is structurally
    # distinct from a successful one.
    assert "preview" not in effect


async def test_preview_unavailable_marker_reaches_reviewer_surfaces(
    stub_embedding_service: AsyncMock,
) -> None:
    """The marker is reviewer-visible on the REST view and the MCP row dict (#1628).

    ``GET /api/v1/approvals[/{id}]`` serialises the row through
    :func:`meho_backplane.api.v1.approvals._view` and
    ``meho.approvals.list`` / ``.get`` through
    :func:`meho_backplane.mcp.tools.approvals._row_to_dict`; both pass
    ``proposed_effect`` through verbatim. Pinning the passthrough for
    the marker keeps a future field projection from silently hiding a
    failed preview from the approval queue.
    """
    from meho_backplane.api.v1.approvals import _view
    from meho_backplane.mcp.tools.approvals import _row_to_dict

    await _bootstrap_registry(stub_embedding_service)
    _FAILURES["GET:/vcenter/vm"] = "vCenter listing unavailable"

    _, row = await _park("vmware.composite.vm.power.bulk", {"action": "reset"})

    rest_effect = _view(row).proposed_effect
    mcp_effect = _row_to_dict(row)["proposed_effect"]
    assert rest_effect["preview_unavailable"] is True
    assert mcp_effect["preview_unavailable"] is True
    assert (
        rest_effect["preview_error"]
        == mcp_effect["preview_error"]
        == row.proposed_effect["preview_error"]
    )


# ===========================================================================
# The other live-read previews — host.evacuate / host.detach / cluster.patch
# ===========================================================================


async def test_host_evacuate_park_resolves_vm_set_on_host(
    stub_embedding_service: AsyncMock,
) -> None:
    await _bootstrap_registry(stub_embedding_service)
    _RESPONSES["GET:/vcenter/vm"] = {
        "value": [{"vm": "vm-a", "name": "app-a", "cluster": "domain-c1"}]
    }

    _, row = await _park("vmware.composite.host.evacuate", {"host": "host-1"})

    assert row.proposed_effect == {
        "op_class": "other",
        "preview": {
            "host": "host-1",
            "tolerate_partial_failure": False,
            "resolved": [{"vm": "vm-a", "name": "app-a"}],
            "total_resolved": 1,
        },
        "safety_level": "dangerous",
    }
    # Only the listing read fired — no recursive migrate, no maintenance.
    assert _CALLS == [("GET:/vcenter/vm", {"filter.hosts": ["host-1"]})]


async def test_host_detach_from_vds_park_resolves_vm_set_on_host(
    stub_embedding_service: AsyncMock,
) -> None:
    await _bootstrap_registry(stub_embedding_service)
    _RESPONSES["GET:/vcenter/vm"] = {"value": [{"vm": "vm-a", "name": "app-a"}]}

    _, row = await _park(
        "vmware.composite.host.detach_from_vds",
        {"host": "host-1", "dvs": "dvs-1", "fallback_network": "net-fallback"},
    )

    assert row.proposed_effect == {
        "op_class": "other",
        "preview": {
            "host": "host-1",
            "dvs": "dvs-1",
            "fallback_network": "net-fallback",
            "resolved": [{"vm": "vm-a", "name": "app-a"}],
            "total_resolved": 1,
        },
        "safety_level": "dangerous",
    }
    assert _ops_in_calls() == ["GET:/vcenter/vm"]


async def test_cluster_patch_park_resolves_host_set(
    stub_embedding_service: AsyncMock,
) -> None:
    await _bootstrap_registry(stub_embedding_service)
    _RESPONSES["GET:/vcenter/cluster/{cluster}/host"] = {
        "value": [
            {"host": "host-1", "name": "esx-1"},
            {"host": "host-2", "name": "esx-2"},
        ]
    }

    _, row = await _park("vmware.composite.cluster.patch", {"cluster": "domain-c1"})

    assert row.proposed_effect == {
        "op_class": "write",
        "preview": {
            "cluster": "domain-c1",
            "patch_method": "default",
            "resolved": [
                {"host": "host-1", "name": "esx-1"},
                {"host": "host-2", "name": "esx-2"},
            ],
            "total_resolved": 2,
        },
        "safety_level": "dangerous",
    }
    # Only the host listing fired — no maintenance / patch sub-ops.
    assert _ops_in_calls() == ["GET:/vcenter/cluster/{cluster}/host"]


# ===========================================================================
# Echo preview through the production park path — zero leaf reads
# ===========================================================================


async def test_vm_create_park_carries_echo_preview_without_any_read(
    stub_embedding_service: AsyncMock,
) -> None:
    """The echo previews enrich the parked row with zero connector I/O."""
    await _bootstrap_registry(stub_embedding_service)

    _, row = await _park(
        "vmware.composite.vm.create",
        {"folder_name": "prod", "name": "vm-new", "guest_os": "UBUNTU_64"},
    )

    assert row.proposed_effect == {
        "op_class": "write",
        "preview": {
            "name": "vm-new",
            "guest_os": "UBUNTU_64",
            "folder_name": "prod",
            "cpu_count": 1,
            "memory_mib": 1024,
            "networks": [],
            "power_on_after_create": False,
        },
        "safety_level": "dangerous",
    }
    assert _CALLS == []
