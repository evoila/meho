# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time ``proposed_effect`` previews for the 16 vmware write composites.

G0.22-T3 (#1608) acceptance criteria, under the post-#2256 direct-session
model:

1. A parked ``vmware.composite.vm.power.bulk`` request's
   ``proposed_effect`` carries the requested ``action``, the ``filter``
   echoed, and the ``resolved`` (capped) entity list + ``total_resolved``
   — asserted against the durable :class:`ApprovalRequest` row through
   the production :func:`~meho_backplane.operations.dispatch` park path.
2. The preview path issues **only reads**: the park records exactly one
   listing ``GET`` on the connector session and no mutating sub-op.
3. The resolution logic is shared, not duplicated — the builders call
   the same :func:`._write._resolve_vm_list` /
   :func:`._write._resolve_cluster_hosts` helpers the handlers use, now
   directly on the connector session (no ``dispatch_child``, no ingested
   descriptor), so the park-time preview works on a fresh boot with zero
   catalog ingest.
4. All 16 write composites register a builder (9 live-read + 7 param
   echo); the wiring test pins the full set. ``vm.disk.grow`` is a
   single-entity live-read builder — its from→to capacity delta is a live
   vCenter read. ``vm.clone_from_template`` is a param-echo builder — its
   params fully name the blast radius (secret-bearing GOSC contents are
   never echoed).

Plus the #1628 follow-up: a *failed* live-read preview parks with the
identifier fields **and** an explicit ``preview_unavailable`` marker +
reason (visible through the REST / MCP serialisation helpers).

Harness: the resolved connector instance the dispatcher hands the preview
builder is a recording double seeded into the connector-instance cache;
the park-time listing reads run directly on it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

import meho_backplane.operations._audit as audit_module
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params
from meho_backplane.connectors.vmware_rest.composites import (
    _write_preview,
    register_vmware_composite_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, ApprovalRequestStatus
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "vmware-rest-9.0"
_TENANT_ID = UUID("00000000-0000-0000-0000-00000000a0a8")

_WRITE_COMPOSITE_OP_IDS: frozenset[str] = frozenset(
    {
        "vmware.composite.vm.create",
        "vmware.composite.vm.clone",
        "vmware.composite.vm.clone_from_template",
        "vmware.composite.vm.snapshot.revert",
        "vmware.composite.vm.migrate",
        "vmware.composite.vm.power",
        "vmware.composite.vm.power.bulk",
        "vmware.composite.vm.disk.grow",
        "vmware.composite.vm.resize",
        "vmware.composite.vm.nic.repoint",
        "vmware.composite.vm.device.cdrom",
        "vmware.composite.host.evacuate",
        "vmware.composite.host.detach_from_vds",
        "vmware.composite.cluster.patch",
        "vmware.composite.cluster.drs_rule.create",
        "vmware.composite.folder.create",
    }
)


# ---------------------------------------------------------------------------
# Fixtures
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
        self.tenant_id: UUID = _TENANT_ID
        self.name = "test-vcenter"
        self.host = "vcenter.test"
        self.port = 443
        self.auth_model = "shared_service_account"


# ---------------------------------------------------------------------------
# Recording connector double (seeded as the dispatcher's resolved instance)
# ---------------------------------------------------------------------------


class _RecordingConnector:
    """Records the listing reads the park-time preview builders issue.

    Seeded as the dispatcher's resolved connector instance so the migrated
    preview builders' shared read helpers (:func:`._write._resolve_vm_list` /
    :func:`._write._resolve_cluster_hosts`) run directly on it. Records
    ``(verb, spec-relative path, query)`` and serves a canned envelope keyed
    by spec path. A spec path registered in ``failures`` raises to drive the
    #1628 ``preview_unavailable`` fail-soft branch; the message carries the
    op-id-shaped ``GET:<path>`` string so the reviewer-facing
    ``preview_error`` names the read that could not resolve.
    """

    _MOUNT = "/api"

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.failures: dict[str, str] = {}
        self.calls: list[tuple[str, str, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"{self._MOUNT}{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params(self._MOUNT, query)

    def _spec(self, path: str) -> str:
        return path[len(self._MOUNT) :] if path.startswith(self._MOUNT) else path

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        spec = self._spec(path)
        self.calls.append(("GET", spec, params))
        if spec in self.failures:
            raise RuntimeError(f"GET:{spec} listing failed: {self.failures[spec]}")
        return self.responses.get(spec, {"value": []})

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: Any = None,
        data: Any = None,
        extra_headers: Any = None,
    ) -> Any:
        spec = self._spec(path)
        self.calls.append((verb, spec, None))
        return {"value": {}}

    @property
    def read_calls(self) -> list[tuple[str, Any]]:
        """(spec-path, query) for every recorded GET."""
        return [(spec, query) for verb, spec, query in self.calls if verb == "GET"]

    @property
    def specs(self) -> list[str]:
        return [spec for _, spec, _ in self.calls]


async def _bootstrap_registry(stub_embedding_service: AsyncMock) -> _RecordingConnector:
    """Register the connector + 17 composites and seed the recording instance."""
    register_connector_v2(
        product="vmware",
        version="9.0",
        impl_id="vmware-rest",
        cls=VmwareRestConnector,
    )
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    recorder = _RecordingConnector()
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = recorder  # type: ignore[assignment]
    return recorder


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


def _strip_uniform_identity(effect: dict[str, Any], *, op_id: str) -> dict[str, Any]:
    """Assert the uniform op-identity envelope fields (#2681), return the rest.

    ``op_id`` / ``connector_id`` / ``target_id`` are stamped onto every parked
    envelope since #2681 (one schema per connector regardless of the per-op
    preview outcome), so the content+metadata equality checks below strip them
    — ``target_id`` is a runtime UUID — after verifying they are present.
    """
    assert effect["op_id"] == op_id
    assert effect["connector_id"] == _CONNECTOR_ID
    assert isinstance(effect["target_id"], str)
    return {k: v for k, v in effect.items() if k not in {"op_id", "connector_id", "target_id"}}


# ===========================================================================
# Wiring — all 16 write composites register a builder (criterion 4)
# ===========================================================================


def test_all_sixteen_write_composites_register_a_preview_builder() -> None:
    """Importing the composites package wires a builder per write composite."""
    assert set(_write_preview._WRITE_PREVIEW_BUILDERS) == set(_WRITE_COMPOSITE_OP_IDS)
    assert len(_WRITE_COMPOSITE_OP_IDS) == 16
    for op_id, builder in _write_preview._WRITE_PREVIEW_BUILDERS.items():
        assert _PREVIEW_BUILDERS.get(op_id) is builder, op_id


# ===========================================================================
# Live-read builders decline without a connector (structural guard)
# ===========================================================================


def _make_preview_ctx(params: dict[str, Any], *, connector_instance: Any = None) -> PreviewContext:
    """A :class:`PreviewContext` for builder-direct unit tests."""
    return PreviewContext(
        descriptor=object(),  # type: ignore[arg-type]  # echo builders ignore it
        connector_instance=connector_instance,
        operator=_make_operator(),
        target=_FakeVmwareTarget(),
        params=params,
    )


async def test_live_read_builders_decline_without_a_connector_instance() -> None:
    """The live-read builders decline (return None) when no connector resolved.

    Post-#2256 the live-read builders resolve their blast radius on the
    connector session; with ``connector_instance=None`` there is nothing to
    read, so they decline to the identifier-only default rather than raise.
    Covers the four fan-out builders, the cluster.drs_rule.create builder,
    and the three #2891 hardware builders.
    """
    for builder, params in (
        (_write_preview._vm_power_bulk_preview, {"action": "stop"}),
        (_write_preview._host_evacuate_preview, {"host": "host-1"}),
        (
            _write_preview._host_detach_from_vds_preview,
            {"host": "host-1", "dvs": "dvs-1", "fallback_network": "net"},
        ),
        (_write_preview._cluster_patch_preview, {"cluster": "domain-c1"}),
        (
            _write_preview._cluster_drs_rule_create_preview,
            {
                "cluster": "domain-c1",
                "rule_name": "keep-apart",
                "rule_type": "anti_affinity",
                "vms": ["web-01", "web-02"],
            },
        ),
        (_write_preview._vm_resize_preview, {"vm": "vm-1", "cpu_count": 4}),
        (
            _write_preview._vm_nic_repoint_preview,
            {"vm": "vm-1", "nic": "4000", "portgroup_name": "pg"},
        ),
        (
            _write_preview._vm_device_cdrom_preview,
            {"vm": "vm-1", "cdrom": "16000", "action": "remove"},
        ),
    ):
        assert await builder(_make_preview_ctx(params, connector_instance=None)) is None


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


async def test_vm_clone_from_template_preview_echoes_clone_coordinates() -> None:
    """The clone-coordinates echo names the full blast radius, minus GOSC secrets."""
    preview = await _write_preview._vm_clone_from_template_preview(
        _make_preview_ctx(
            {
                "source_template": "ubuntu-2404-template",
                "new_vm_name": "web-01",
                "folder": "group-v10",
                "resource_pool": "resgroup-8",
                "datastore": "datastore-15",
                "host": "host-19",
                "power_on": True,
                "customization_spec_name": "linux-web-gosc",
            }
        )
    )
    assert preview == {
        "source_template": "ubuntu-2404-template",
        "new_vm_name": "web-01",
        "folder": "group-v10",
        "resource_pool": "resgroup-8",
        "datastore": "datastore-15",
        "host": "host-19",
        "power_on": True,
        "customization_spec_name": "linux-web-gosc",
    }


async def test_vm_clone_from_template_preview_defaults_optional_fields_to_none() -> None:
    """Only the required placement params supplied → host / customization echo as None."""
    preview = await _write_preview._vm_clone_from_template_preview(
        _make_preview_ctx(
            {
                "source_template": "ubuntu-2404-template",
                "new_vm_name": "web-01",
                "folder": "group-v10",
                "resource_pool": "resgroup-8",
                "datastore": "datastore-15",
            }
        )
    )
    assert preview == {
        "source_template": "ubuntu-2404-template",
        "new_vm_name": "web-01",
        "folder": "group-v10",
        "resource_pool": "resgroup-8",
        "datastore": "datastore-15",
        "host": None,
        "power_on": False,
        "customization_spec_name": None,
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


async def test_vm_power_preview_names_the_hard_vs_soft_power_kind() -> None:
    """The single-VM power echo surfaces the soft-vs-hard distinction to the approver."""
    hard = await _write_preview._vm_power_preview(_make_preview_ctx({"vm": "vm-1", "verb": "off"}))
    assert hard == {"vm": "vm-1", "verb": "off", "power_kind": "hard"}
    soft = await _write_preview._vm_power_preview(
        _make_preview_ctx({"vm": "vm-1", "verb": "guest_shutdown"})
    )
    assert soft == {"vm": "vm-1", "verb": "guest_shutdown", "power_kind": "guest"}


async def test_echo_builders_decline_on_malformed_params() -> None:
    """Missing required params decline (→ identifier-only default), never raise."""
    assert await _write_preview._vm_create_preview(_make_preview_ctx({})) is None
    assert await _write_preview._vm_clone_preview(_make_preview_ctx({})) is None
    assert await _write_preview._vm_clone_from_template_preview(_make_preview_ctx({})) is None
    assert await _write_preview._vm_snapshot_revert_preview(_make_preview_ctx({})) is None
    assert await _write_preview._vm_migrate_preview(_make_preview_ctx({})) is None
    assert await _write_preview._vm_power_preview(_make_preview_ctx({})) is None
    assert await _write_preview._vm_power_bulk_preview(_make_preview_ctx({})) is None
    assert await _write_preview._host_evacuate_preview(_make_preview_ctx({})) is None
    assert await _write_preview._host_detach_from_vds_preview(_make_preview_ctx({})) is None
    assert await _write_preview._cluster_patch_preview(_make_preview_ctx({})) is None
    assert await _write_preview._folder_create_preview(_make_preview_ctx({})) is None
    assert await _write_preview._cluster_drs_rule_create_preview(_make_preview_ctx({})) is None


# ===========================================================================
# vm.power.bulk — the parked row carries action + filter + resolved set
# (criteria 1 + 2, through the production dispatch park path)
# ===========================================================================


async def test_power_bulk_park_carries_action_filter_and_resolved_set(
    stub_embedding_service: AsyncMock,
) -> None:
    """The parked row's ``proposed_effect`` names the blast radius (criterion 1)."""
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.responses["/vcenter/vm"] = {
        "value": [
            {"vm": "vm-a", "name": "web-a", "power_state": "POWERED_ON", "cpu_count": 2},
            {"vm": "vm-b", "name": "web-b", "power_state": "POWERED_OFF", "cpu_count": 4},
        ]
    }

    _, row = await _park(
        "vmware.composite.vm.power.bulk",
        {"action": "stop", "filter": {"names": ["web-*"]}},
    )

    assert _strip_uniform_identity(row.proposed_effect, op_id="vmware.composite.vm.power.bulk") == {
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
        "preview_populated": True,
        "safety_level": "dangerous",
    }
    # The filter reached the listing read in the handler's wire shape;
    # bare param name on the modern /api mount (#2298).
    assert recorder.read_calls == [("/vcenter/vm", {"names": ["web-*"]})]


async def test_power_bulk_park_issues_only_the_listing_read(
    stub_embedding_service: AsyncMock,
) -> None:
    """No power mutation fires on the park path (criterion 2)."""
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.responses["/vcenter/vm"] = {"value": [{"vm": "vm-a"}, {"vm": "vm-b"}]}

    await _park("vmware.composite.vm.power.bulk", {"action": "start"})

    assert recorder.specs == ["/vcenter/vm"]


async def test_power_bulk_resolved_list_is_capped_with_true_total(
    stub_embedding_service: AsyncMock,
) -> None:
    """A wide filter caps ``resolved`` at the preview cap; ``total_resolved`` is uncapped."""
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.responses["/vcenter/vm"] = {
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
    """A failing listing read parks with identifiers + an explicit marker (#1628)."""
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.failures["/vcenter/vm"] = "vCenter listing unavailable"

    target = _FakeVmwareTarget()
    _, row = await _park(
        "vmware.composite.vm.power.bulk",
        {"action": "reset"},
        target=target,
    )

    effect = row.proposed_effect
    assert effect["op_id"] == "vmware.composite.vm.power.bulk"
    assert effect["connector_id"] == _CONNECTOR_ID
    assert effect["target_id"] == str(target.id)
    assert effect["op_class"] == "other"
    assert effect["preview_unavailable"] is True
    assert "GET:/vcenter/vm" in effect["preview_error"]
    assert "vCenter listing unavailable" in effect["preview_error"]
    assert "preview" not in effect


async def test_preview_unavailable_marker_reaches_reviewer_surfaces(
    stub_embedding_service: AsyncMock,
) -> None:
    """The marker is reviewer-visible on the REST view and the MCP row dict (#1628)."""
    from meho_backplane.api.v1.approvals import _view
    from meho_backplane.mcp.tools.approvals import _row_to_dict

    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.failures["/vcenter/vm"] = "vCenter listing unavailable"

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
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.responses["/vcenter/vm"] = {
        "value": [{"vm": "vm-a", "name": "app-a", "cluster": "domain-c1"}]
    }

    _, row = await _park("vmware.composite.host.evacuate", {"host": "host-1"})

    assert _strip_uniform_identity(row.proposed_effect, op_id="vmware.composite.host.evacuate") == {
        "op_class": "other",
        "preview": {
            "host": "host-1",
            "tolerate_partial_failure": False,
            "resolved": [{"vm": "vm-a", "name": "app-a"}],
            "total_resolved": 1,
        },
        "preview_populated": True,
        "safety_level": "dangerous",
    }
    # Only the listing read fired — no recursive migrate, no maintenance.
    assert recorder.read_calls == [("/vcenter/vm", {"hosts": ["host-1"]})]


async def test_host_detach_from_vds_park_resolves_vm_set_on_host(
    stub_embedding_service: AsyncMock,
) -> None:
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.responses["/vcenter/vm"] = {"value": [{"vm": "vm-a", "name": "app-a"}]}

    _, row = await _park(
        "vmware.composite.host.detach_from_vds",
        {"host": "host-1", "dvs": "dvs-1", "fallback_network": "net-fallback"},
    )

    assert _strip_uniform_identity(
        row.proposed_effect, op_id="vmware.composite.host.detach_from_vds"
    ) == {
        "op_class": "other",
        "preview": {
            "host": "host-1",
            "dvs": "dvs-1",
            "fallback_network": "net-fallback",
            "resolved": [{"vm": "vm-a", "name": "app-a"}],
            "total_resolved": 1,
        },
        "preview_populated": True,
        "safety_level": "dangerous",
    }
    assert recorder.specs == ["/vcenter/vm"]


async def test_cluster_patch_park_resolves_host_set(
    stub_embedding_service: AsyncMock,
) -> None:
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.responses["/vcenter/cluster/domain-c1/host"] = {
        "value": [
            {"host": "host-1", "name": "esx-1"},
            {"host": "host-2", "name": "esx-2"},
        ]
    }

    _, row = await _park("vmware.composite.cluster.patch", {"cluster": "domain-c1"})

    assert _strip_uniform_identity(row.proposed_effect, op_id="vmware.composite.cluster.patch") == {
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
        "preview_populated": True,
        "safety_level": "dangerous",
    }
    # Only the host listing fired — no maintenance / patch sub-ops.
    assert recorder.specs == ["/vcenter/cluster/domain-c1/host"]


# ===========================================================================
# Echo preview through the production park path — zero leaf reads
# ===========================================================================


async def test_vm_create_park_carries_echo_preview_without_any_read(
    stub_embedding_service: AsyncMock,
) -> None:
    """The echo previews enrich the parked row with zero connector I/O."""
    recorder = await _bootstrap_registry(stub_embedding_service)

    _, row = await _park(
        "vmware.composite.vm.create",
        {"folder_name": "prod", "name": "vm-new", "guest_os": "UBUNTU_64"},
    )

    assert _strip_uniform_identity(row.proposed_effect, op_id="vmware.composite.vm.create") == {
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
        "preview_populated": True,
        "safety_level": "dangerous",
    }
    assert recorder.calls == []


# ===========================================================================
# vm.disk.grow — the from→to capacity delta preview (live-read, #2893)
# ===========================================================================


_TEN_GIB = 10 * 1024**3
_TWENTY_GIB = 20 * 1024**3


async def test_disk_grow_park_carries_from_to_capacity_delta(
    stub_embedding_service: AsyncMock,
) -> None:
    """The parked row's preview names the current→requested delta (the decision)."""
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.responses["/vcenter/vm/vm-1/hardware/disk/2000"] = {
        "label": "Hard disk 1",
        "type": "SCSI",
        "capacity": _TEN_GIB,
    }
    recorder.responses["/vcenter/vm/vm-1"] = {"name": "web-01"}

    _, row = await _park(
        "vmware.composite.vm.disk.grow",
        {"vm": "vm-1", "disk": "2000", "capacity_bytes": _TWENTY_GIB},
    )

    assert _strip_uniform_identity(row.proposed_effect, op_id="vmware.composite.vm.disk.grow") == {
        "op_class": "other",
        "preview": {
            "vm": "vm-1",
            "name": "web-01",
            "disk": "2000",
            "disk_label": "Hard disk 1",
            "current_capacity_bytes": _TEN_GIB,
            "requested_capacity_bytes": _TWENTY_GIB,
            "delta_bytes": _TWENTY_GIB - _TEN_GIB,
        },
        "preview_populated": True,
        "safety_level": "dangerous",
    }
    # Two read-only GETs: the disk detail (load-bearing) + the VM name
    # (cosmetic). No ReconfigVM_Task mutation fires on the park path.
    assert recorder.read_calls == [
        ("/vcenter/vm/vm-1/hardware/disk/2000", None),
        ("/vcenter/vm/vm-1", None),
    ]
    assert all(verb == "GET" for verb, _, _ in recorder.calls)


async def test_disk_grow_preview_failure_parks_with_unavailable_marker(
    stub_embedding_service: AsyncMock,
) -> None:
    """A failing disk read parks with identifiers + an explicit marker (#1628).

    The current capacity is the load-bearing half of the delta, so a disk
    read that faults makes the blast radius unknowable — the park is honest
    about it rather than showing a bare identifier default.
    """
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.failures["/vcenter/vm/vm-1/hardware/disk/2000"] = "disk detail unavailable"

    target = _FakeVmwareTarget()
    _, row = await _park(
        "vmware.composite.vm.disk.grow",
        {"vm": "vm-1", "disk": "2000", "capacity_bytes": _TWENTY_GIB},
        target=target,
    )

    effect = row.proposed_effect
    assert effect["op_id"] == "vmware.composite.vm.disk.grow"
    assert effect["target_id"] == str(target.id)
    assert effect["preview_unavailable"] is True
    assert "disk detail unavailable" in effect["preview_error"]
    assert "preview" not in effect


async def test_disk_grow_preview_declines_without_a_connector_instance() -> None:
    """A live-read builder with no resolved connector declines to the identifier default."""
    assert (
        await _write_preview._vm_disk_grow_preview(
            _make_preview_ctx(
                {"vm": "vm-1", "disk": "2000", "capacity_bytes": _TWENTY_GIB},
                connector_instance=None,
            )
        )
        is None
    )


async def test_disk_grow_preview_declines_on_malformed_params() -> None:
    """Missing / wrong-typed params decline (→ identifier-only default), never raise."""
    recorder = _RecordingConnector()
    for params in (
        {},
        {"vm": "vm-1", "disk": "2000"},  # no capacity
        {"vm": "vm-1", "disk": "2000", "capacity_bytes": "20g"},  # non-int
        {"vm": "vm-1", "disk": "2000", "capacity_bytes": True},  # bool is not a capacity
    ):
        assert (
            await _write_preview._vm_disk_grow_preview(
                _make_preview_ctx(params, connector_instance=recorder)
            )
            is None
        )


async def test_disk_grow_preview_vm_name_is_best_effort() -> None:
    """A VM-name read fault nulls the name but still builds the delta preview."""

    class _DiskOkVmFailConnector(_RecordingConnector):
        async def _get_json(
            self, target: Any, path: str, *, operator: Any, params: Any = None
        ) -> Any:
            spec = self._spec(path)
            self.calls.append(("GET", spec, params))
            if spec.endswith("/hardware/disk/2000"):
                return {"label": "Hard disk 1", "capacity": _TEN_GIB}
            # The VM summary read faults — best-effort nulls the name.
            request = httpx.Request("GET", f"https://vc{path}")
            raise httpx.HTTPStatusError(
                "boom", request=request, response=httpx.Response(500, request=request)
            )

    preview = await _write_preview._vm_disk_grow_preview(
        _make_preview_ctx(
            {"vm": "vm-1", "disk": "2000", "capacity_bytes": _TWENTY_GIB},
            connector_instance=_DiskOkVmFailConnector(),
        )
    )
    assert preview is not None
    assert preview["name"] is None
    assert preview["disk_label"] == "Hard disk 1"
    assert preview["current_capacity_bytes"] == _TEN_GIB
    assert preview["delta_bytes"] == _TWENTY_GIB - _TEN_GIB


# ===========================================================================
# cluster.drs_rule.create — the capped VM fan-out + cluster-name preview (#2895)
# ===========================================================================


async def test_drs_rule_create_park_carries_capped_vm_fanout(
    stub_embedding_service: AsyncMock,
) -> None:
    """The parked row's preview names the cluster + resolved VM set (fan-out pattern)."""
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.responses["/vcenter/vm"] = {
        "value": [
            {"vm": "vm-1", "name": "web-01", "power_state": "POWERED_ON"},
            {"vm": "vm-2", "name": "web-02", "power_state": "POWERED_OFF"},
        ]
    }
    recorder.responses["/vcenter/cluster/domain-c1"] = {"name": "prod-cluster"}

    _, row = await _park(
        "vmware.composite.cluster.drs_rule.create",
        {
            "cluster": "domain-c1",
            "rule_name": "keep-apart",
            "rule_type": "anti_affinity",
            "vms": ["web-01", "web-02"],
        },
    )

    assert _strip_uniform_identity(
        row.proposed_effect, op_id="vmware.composite.cluster.drs_rule.create"
    ) == {
        "op_class": "write",
        "preview": {
            "cluster": "domain-c1",
            "cluster_name": "prod-cluster",
            "rule_type": "anti_affinity",
            "rule_name": "keep-apart",
            "enabled": True,
            "resolved": [
                {"vm": "vm-1", "name": "web-01", "power_state": "POWERED_ON"},
                {"vm": "vm-2", "name": "web-02", "power_state": "POWERED_OFF"},
            ],
            "total_resolved": 2,
        },
        "preview_populated": True,
        "safety_level": "dangerous",
    }
    # The VM resolution is load-bearing (first read); the cluster name is a
    # second best-effort GET. No ReconfigureComputeResource_Task mutation fires.
    assert recorder.read_calls[0][0] == "/vcenter/vm"
    assert ("/vcenter/cluster/domain-c1", None) in recorder.read_calls
    assert all(verb == "GET" for verb, _, _ in recorder.calls)


async def test_drs_rule_create_preview_caps_resolved_at_twenty(
    stub_embedding_service: AsyncMock,
) -> None:
    """A rule over >20 VMs caps ``resolved`` at 20 but keeps the true ``total_resolved``."""
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.responses["/vcenter/vm"] = {
        "value": [{"vm": f"vm-{i}", "name": f"web-{i}"} for i in range(25)]
    }
    _, row = await _park(
        "vmware.composite.cluster.drs_rule.create",
        {
            "cluster": "domain-c1",
            "rule_name": "spread",
            "rule_type": "anti_affinity",
            "vms": [f"web-{i}" for i in range(25)],
        },
    )
    preview = row.proposed_effect["preview"]
    assert len(preview["resolved"]) == 20
    assert preview["total_resolved"] == 25


async def test_folder_create_preview_echoes_parent_and_new_name() -> None:
    """folder.create preview is a param echo — parent + new folder name, no I/O."""
    preview = await _write_preview._folder_create_preview(
        _make_preview_ctx({"parent_folder": "prod", "folder_name": "cluster-nodes"})
    )
    assert preview == {"parent_folder": "prod", "new_folder_name": "cluster-nodes"}


# ===========================================================================
# #2891 hardware-write builders — live-read from->to diffs (builder-direct)
# ===========================================================================


async def test_vm_resize_preview_live_reads_current_sizing() -> None:
    """The resize preview pairs the live-read current sizing with the request."""
    recorder = _RecordingConnector()
    recorder.responses["/vcenter/vm/vm-1"] = {
        "value": {
            "name": "web-1",
            "power_state": "POWERED_OFF",
            "cpu": {"count": 2, "cores_per_socket": 1},
            "memory": {"size_MiB": 2048},
        }
    }
    preview = await _write_preview._vm_resize_preview(
        _make_preview_ctx(
            {"vm": "vm-1", "cpu_count": 4, "memory_mib": 8192}, connector_instance=recorder
        )
    )
    assert preview == {
        "vm": "vm-1",
        "name": "web-1",
        "power_state": "POWERED_OFF",
        "current": {"cpu_count": 2, "cores_per_socket": 1, "memory_MiB": 2048},
        "requested": {"cpu_count": 4, "cores_per_socket": None, "memory_MiB": 8192},
    }
    assert recorder.specs == ["/vcenter/vm/vm-1"]


async def test_vm_nic_repoint_preview_reads_backing_and_resolves_portgroup() -> None:
    """The NIC preview surfaces the from->to network pair the reviewer needs."""
    recorder = _RecordingConnector()
    recorder.responses.update(
        {
            "/vcenter/vm/vm-1": {"value": {"name": "web-1"}},
            "/vcenter/vm/vm-1/hardware/ethernet/4000": {
                "value": {
                    "mac_address": "00:50:56:aa:bb:cc",
                    "backing": {
                        "type": "DISTRIBUTED_PORTGROUP",
                        "network": "dvportgroup-1",
                        "network_name": "old-net",
                    },
                }
            },
            "/vcenter/network": {
                "value": [
                    {
                        "network": "dvportgroup-9",
                        "name": "prod-net",
                        "type": "DISTRIBUTED_PORTGROUP",
                    }
                ]
            },
        }
    )
    preview = await _write_preview._vm_nic_repoint_preview(
        _make_preview_ctx(
            {"vm": "vm-1", "nic": "4000", "portgroup_name": "prod-net"}, connector_instance=recorder
        )
    )
    assert preview == {
        "vm": "vm-1",
        "name": "web-1",
        "nic": "4000",
        "mac_address": "00:50:56:aa:bb:cc",
        "current_backing": {
            "type": "DISTRIBUTED_PORTGROUP",
            "network": "dvportgroup-1",
            "network_name": "old-net",
        },
        "requested_backing": {"portgroup_id": "dvportgroup-9", "portgroup_name": "prod-net"},
    }
    assert recorder.specs == [
        "/vcenter/vm/vm-1",
        "/vcenter/vm/vm-1/hardware/ethernet/4000",
        "/vcenter/network",
    ]


async def test_vm_device_cdrom_preview_live_reads_current_backing() -> None:
    """The CD-ROM preview surfaces the host-local ISO backing + state to the approver."""
    recorder = _RecordingConnector()
    recorder.responses.update(
        {
            "/vcenter/vm/vm-1": {"value": {"name": "web-1"}},
            "/vcenter/vm/vm-1/hardware/cdrom/16000": {
                "value": {
                    "backing": {"type": "ISO_FILE", "iso_file": "[esx-1-local] installer.iso"},
                    "state": "CONNECTED",
                }
            },
        }
    )
    preview = await _write_preview._vm_device_cdrom_preview(
        _make_preview_ctx(
            {"vm": "vm-1", "cdrom": "16000", "action": "remove"}, connector_instance=recorder
        )
    )
    assert preview == {
        "vm": "vm-1",
        "name": "web-1",
        "cdrom": "16000",
        "action": "remove",
        "current_backing": {"type": "ISO_FILE", "iso_file": "[esx-1-local] installer.iso"},
        "state": "CONNECTED",
    }
    assert recorder.specs == ["/vcenter/vm/vm-1", "/vcenter/vm/vm-1/hardware/cdrom/16000"]


async def test_hardware_builders_decline_on_malformed_params() -> None:
    """Missing required params decline (→ identifier-only default), even with a connector."""
    recorder = _RecordingConnector()
    assert (
        await _write_preview._vm_resize_preview(_make_preview_ctx({}, connector_instance=recorder))
        is None
    )
    assert (
        await _write_preview._vm_nic_repoint_preview(
            _make_preview_ctx({"vm": "vm-1", "nic": "4000"}, connector_instance=recorder)
        )
        is None
    )
    assert (
        await _write_preview._vm_device_cdrom_preview(
            _make_preview_ctx({"vm": "vm-1", "cdrom": "16000"}, connector_instance=recorder)
        )
        is None
    )
    # Declined before issuing any read.
    assert recorder.calls == []


# ===========================================================================
# #2891 hardware writes — park path carries the from->to diff (criterion 3)
# ===========================================================================


async def test_vm_resize_park_carries_sizing_from_to_diff(
    stub_embedding_service: AsyncMock,
) -> None:
    """The parked resize row names the current + requested sizing (from->to)."""
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.responses["/vcenter/vm/vm-1"] = {
        "value": {
            "name": "web-1",
            "power_state": "POWERED_OFF",
            "cpu": {"count": 1, "cores_per_socket": 1},
            "memory": {"size_MiB": 1024},
        }
    }

    _, row = await _park(
        "vmware.composite.vm.resize", {"vm": "vm-1", "cpu_count": 4, "memory_mib": 8192}
    )

    assert _strip_uniform_identity(row.proposed_effect, op_id="vmware.composite.vm.resize") == {
        "op_class": "other",
        "preview": {
            "vm": "vm-1",
            "name": "web-1",
            "power_state": "POWERED_OFF",
            "current": {"cpu_count": 1, "cores_per_socket": 1, "memory_MiB": 1024},
            "requested": {"cpu_count": 4, "cores_per_socket": None, "memory_MiB": 8192},
        },
        "preview_populated": True,
        "safety_level": "dangerous",
    }
    # Only the read-only VM info GET fired — no CPU / memory PATCH.
    assert recorder.specs == ["/vcenter/vm/vm-1"]


async def test_vm_nic_repoint_park_carries_network_from_to_pair(
    stub_embedding_service: AsyncMock,
) -> None:
    """The parked NIC row names the current backing + resolved target portgroup."""
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.responses.update(
        {
            "/vcenter/vm/vm-1": {"value": {"name": "web-1"}},
            "/vcenter/vm/vm-1/hardware/ethernet/4000": {
                "value": {"mac_address": "aa:bb", "backing": {"type": "STANDARD_PORTGROUP"}}
            },
            "/vcenter/network": {
                "value": [
                    {
                        "network": "dvportgroup-9",
                        "name": "prod-net",
                        "type": "DISTRIBUTED_PORTGROUP",
                    }
                ]
            },
        }
    )

    _, row = await _park(
        "vmware.composite.vm.nic.repoint",
        {"vm": "vm-1", "nic": "4000", "portgroup_name": "prod-net"},
    )

    assert _strip_uniform_identity(
        row.proposed_effect, op_id="vmware.composite.vm.nic.repoint"
    ) == {
        "op_class": "other",
        "preview": {
            "vm": "vm-1",
            "name": "web-1",
            "nic": "4000",
            "mac_address": "aa:bb",
            "current_backing": {"type": "STANDARD_PORTGROUP"},
            "requested_backing": {"portgroup_id": "dvportgroup-9", "portgroup_name": "prod-net"},
        },
        "preview_populated": True,
        "safety_level": "dangerous",
    }
    # Only read-only resolution GETs fired — no NIC PATCH.
    assert recorder.specs == [
        "/vcenter/vm/vm-1",
        "/vcenter/vm/vm-1/hardware/ethernet/4000",
        "/vcenter/network",
    ]


async def test_vm_device_cdrom_park_carries_current_backing(
    stub_embedding_service: AsyncMock,
) -> None:
    """The parked CD-ROM row names the current backing (host-local ISO) + action."""
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.responses.update(
        {
            "/vcenter/vm/vm-1": {"value": {"name": "web-1"}},
            "/vcenter/vm/vm-1/hardware/cdrom/16000": {
                "value": {
                    "backing": {"type": "ISO_FILE", "iso_file": "[esx-1-local] pinned.iso"},
                    "state": "CONNECTED",
                }
            },
        }
    )

    _, row = await _park(
        "vmware.composite.vm.device.cdrom",
        {"vm": "vm-1", "cdrom": "16000", "action": "remove"},
    )

    assert _strip_uniform_identity(
        row.proposed_effect, op_id="vmware.composite.vm.device.cdrom"
    ) == {
        "op_class": "other",
        "preview": {
            "vm": "vm-1",
            "name": "web-1",
            "cdrom": "16000",
            "action": "remove",
            "current_backing": {"type": "ISO_FILE", "iso_file": "[esx-1-local] pinned.iso"},
            "state": "CONNECTED",
        },
        "preview_populated": True,
        "safety_level": "dangerous",
    }
    # Only the read-only backing GETs fired — no DELETE.
    assert recorder.specs == ["/vcenter/vm/vm-1", "/vcenter/vm/vm-1/hardware/cdrom/16000"]


async def test_vm_resize_preview_failure_parks_with_unavailable_marker(
    stub_embedding_service: AsyncMock,
) -> None:
    """A failing VM-info read parks with identifiers + the preview_unavailable marker (#1628)."""
    recorder = await _bootstrap_registry(stub_embedding_service)
    recorder.failures["/vcenter/vm/vm-1"] = "vCenter VM read unavailable"

    target = _FakeVmwareTarget()
    _, row = await _park(
        "vmware.composite.vm.resize",
        {"vm": "vm-1", "cpu_count": 4},
        target=target,
    )

    effect = row.proposed_effect
    assert effect["op_id"] == "vmware.composite.vm.resize"
    assert effect["connector_id"] == _CONNECTOR_ID
    assert effect["target_id"] == str(target.id)
    assert effect["safety_level"] == "dangerous"
    assert effect["preview_unavailable"] is True
    assert "GET:/vcenter/vm/vm-1" in effect["preview_error"]
    assert "preview" not in effect
