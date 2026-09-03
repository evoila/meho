# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vmware.host.storage_devices`` typed op (#3332).

A ``source_kind="typed"`` bound method on :class:`VmwareRestConnector`
that resolves a host -- a vCenter name/moref via ``GET:/vcenter/host``,
or the standalone-ESXi ``ha-host`` when the target's probe fingerprint is
``product=esxi`` -- then reads ``config.storageDevice.scsiLun`` per host
directly on the connector session (no ``dispatch_child``, no ingested
descriptor), so it works on a fresh boot with zero catalog ingest -- the
pre-vCenter standalone-ESXi case #3332 needs.

Exercised against a fake connector that records
:meth:`mount_op_path` / :meth:`_get_json` / :meth:`_post_vmomi_json`
calls, so the assertion targets are the resolution branch (esxi vs
vCenter), the per-LUN mapping (ssd / local / capacity / model / vendor),
the fail-closed refusal paths, and the JSONFlux set-shaped output --
without a live httpx transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params
from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.host_target import STANDALONE_ESXI_HOST_MOID
from meho_backplane.connectors.vmware_rest.typed_ops import VMWARE_TYPED_OPS
from meho_backplane.connectors.vmware_rest.typed_ops_host_storage_devices import (
    HOST_STORAGE_DEVICES_GROUP_KEY,
    VMWARE_HOST_STORAGE_DEVICES_OP,
    _map_scsi_lun,
    build_host_storage_devices_retrieve_params,
    host_storage_devices_impl,
)

# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------


def _make_operator() -> Operator:
    return Operator(
        sub="op-host-storage-devices",
        name="Host Storage Devices Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0b0"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    """Duck-typed target the classifier + impl read from.

    ``fingerprint`` is the JSON dict the probe route persists; ``None``
    (unprobed) classifies as vCenter, ``{"product": "esxi", ...}`` as a
    standalone ESXi.
    """

    fingerprint: dict[str, Any] | None = None
    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _vcenter_target() -> _Target:
    return _Target(fingerprint={"product": "vcenter", "reachable": True, "version": "9.0"})


def _esxi_target() -> _Target:
    return _Target(
        fingerprint={"product": "esxi", "reachable": True, "version": "9.0"},
        name="esxi-standalone",
    )


# A HostScsiDisk (deviceType="disk") with the flash/local flags + capacity,
# and a non-disk ScsiLun (cdrom) that carries none of the HostScsiDisk-only
# fields -- so the mapping's null-coercion is exercised.
_DISK_LUN: dict[str, Any] = {
    "_typeName": "HostScsiDisk",
    "uuid": "0200000000600a",
    "canonicalName": "naa.6000c290",
    "deviceType": "disk",
    "ssd": True,
    "localDisk": True,
    "model": "Virtual disk   ",
    "vendor": "VMware  ",
    "capacity": {"blockSize": 512, "block": 209715200},
}
_CDROM_LUN: dict[str, Any] = {
    "_typeName": "ScsiLun",
    "uuid": "mpx.vmhba64:C0:T0:L0",
    "canonicalName": "mpx.vmhba64:C0:T0:L0",
    "deviceType": "cdrom",
    "model": "CD-ROM  ",
    "vendor": "NECVMWar",
}


def _boxed_scsi_luns(luns: list[dict[str, Any]]) -> dict[str, Any]:
    """VI-JSON ``ArrayOf*`` ``_value`` boxing of a scsiLun array."""
    return {"_typeName": "ArrayOfScsiLun", "_value": luns}


def _retrieve_result(
    moid: str, scsi_lun_val: Any, boot_system_moid: str | None = None
) -> dict[str, Any]:
    """A RetrievePropertiesEx ``RetrieveResult`` carrying scsiLun (+ optional bootDeviceSystem)."""
    prop_set: list[dict[str, Any]] = [{"name": "config.storageDevice.scsiLun", "val": scsi_lun_val}]
    if boot_system_moid is not None:
        prop_set.append(
            {
                "name": "configManager.bootDeviceSystem",
                "val": {
                    "_typeName": "ManagedObjectReference",
                    "type": "HostBootDeviceSystem",
                    "value": boot_system_moid,
                },
            }
        )
    return {"objects": [{"obj": {"type": "HostSystem", "value": moid}, "propSet": prop_set}]}


def _boot_info(current_key: str | None) -> dict[str, Any]:
    """A HostBootDeviceInfo QueryBootDevices response with the given currentBootDeviceKey."""
    info: dict[str, Any] = {"_typeName": "HostBootDeviceInfo", "bootDevices": []}
    if current_key is not None:
        info["currentBootDeviceKey"] = current_key
    return info


class _FakeConnector:
    """Records the transport calls ``host_storage_devices_impl`` makes.

    Serves the host listing on :meth:`_get_json` (keyed off the
    ``names`` / ``hosts`` filter, adapted to the mount flavor); on
    :meth:`_post_vmomi_json` it serves the per-host RetrievePropertiesEx
    result (keyed by the host MoRef in the request body's objectSet) and,
    for a ``.../QueryBootDevices`` path, the configured boot info.
    """

    def __init__(
        self,
        *,
        hosts: list[dict[str, str]] | None = None,
        props_by_host: dict[str, Any] | None = None,
        post_error: Exception | None = None,
        boot_info: dict[str, Any] | None = None,
        boot_error: Exception | None = None,
        mount_prefix: str = "/api",
    ) -> None:
        self._hosts = hosts if hosts is not None else [{"host": "host-15", "name": "esxi-01"}]
        self._props_by_host = props_by_host or {}
        self._post_error = post_error
        self._boot_info = boot_info
        self._boot_error = boot_error
        self._mount_prefix = mount_prefix
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        del target, operator
        return f"{self._mount_prefix}{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params(self._mount_prefix, query)

    async def _get_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> Any:
        del target, operator
        params = params or {}
        self.get_calls.append((path, params))
        names = params.get("names") or params.get("filter.names")
        if names:
            return [h for h in self._hosts if h["name"] in names]
        wanted = params.get("hosts") or params.get("filter.hosts")
        if wanted:
            return [h for h in self._hosts if h["host"] in wanted]
        return list(self._hosts)

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
    ) -> Any:
        del target, operator
        assert json is not None
        self.post_calls.append((path, json))
        if path.endswith("/QueryBootDevices"):
            if self._boot_error is not None:
                raise self._boot_error
            return self._boot_info
        if self._post_error is not None:
            raise self._post_error
        moid = json["specSet"][0]["objectSet"][0]["obj"]["value"]
        return self._props_by_host[moid]


# ---------------------------------------------------------------------------
# Resolution branch: standalone ESXi vs vCenter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_esxi_target_resolves_ha_host_without_listing() -> None:
    """A standalone-ESXi target reads ha-host directly -- no GET:/vcenter/host."""
    conn = _FakeConnector(
        props_by_host={
            STANDALONE_ESXI_HOST_MOID: _retrieve_result(
                STANDALONE_ESXI_HOST_MOID, _boxed_scsi_luns([_DISK_LUN])
            )
        }
    )
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _esxi_target(),  # type: ignore[arg-type]
        {},  # no host param -- the host is the target
    )
    assert out["status"] == "ok"
    assert out["host"] == STANDALONE_ESXI_HOST_MOID
    assert out["device_count"] == 1
    # No host listing was issued (the surface does not exist on a standalone ESXi).
    assert conn.get_calls == []
    # The RetrievePropertiesEx targeted ha-host reading the scsiLun path.
    assert len(conn.post_calls) == 1
    body = conn.post_calls[0][1]
    assert body["specSet"][0]["objectSet"][0]["obj"]["value"] == STANDALONE_ESXI_HOST_MOID
    assert body["specSet"][0]["propSet"][0]["pathSet"] == [
        "config.storageDevice.scsiLun",
        "configManager.bootDeviceSystem",
    ]


@pytest.mark.asyncio
async def test_esxi_ignores_supplied_host_param() -> None:
    """On an ESXi target a supplied ``host`` is ignored -- the host is the target."""
    conn = _FakeConnector(
        props_by_host={
            STANDALONE_ESXI_HOST_MOID: _retrieve_result(
                STANDALONE_ESXI_HOST_MOID, _boxed_scsi_luns([_DISK_LUN])
            )
        }
    )
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _esxi_target(),  # type: ignore[arg-type]
        {"host": "whatever-ignored"},
    )
    assert out["status"] == "ok"
    assert out["host"] == STANDALONE_ESXI_HOST_MOID
    assert conn.get_calls == []


@pytest.mark.asyncio
async def test_vcenter_resolves_host_by_display_name() -> None:
    """A vCenter target resolves the ``host`` display name via GET:/vcenter/host."""
    conn = _FakeConnector(
        hosts=[{"host": "host-15", "name": "esxi-01"}],
        props_by_host={"host-15": _retrieve_result("host-15", _boxed_scsi_luns([_DISK_LUN]))},
    )
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _vcenter_target(),  # type: ignore[arg-type]
        {"host": "esxi-01"},
    )
    assert out["status"] == "ok"
    assert out["host"] == "host-15"
    assert out["device_count"] == 1
    # A name lookup was issued (name-first ladder).
    assert conn.get_calls and (
        conn.get_calls[0][1].get("names") == ["esxi-01"]
        or conn.get_calls[0][1].get("filter.names") == ["esxi-01"]
    )


@pytest.mark.asyncio
async def test_vcenter_resolves_host_by_moref_fallback() -> None:
    """A ``host`` that is not a display name falls back to a moref match."""
    conn = _FakeConnector(
        hosts=[{"host": "host-15", "name": "esxi-01"}],
        props_by_host={"host-15": _retrieve_result("host-15", _boxed_scsi_luns([_DISK_LUN]))},
    )
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _vcenter_target(),  # type: ignore[arg-type]
        {"host": "host-15"},
    )
    assert out["status"] == "ok"
    assert out["host"] == "host-15"


# ---------------------------------------------------------------------------
# Per-LUN mapping
# ---------------------------------------------------------------------------


def test_map_scsi_lun_disk_flags_and_capacity() -> None:
    """A HostScsiDisk maps uuid / flags / trimmed model+vendor / byte capacity."""
    row = _map_scsi_lun(_DISK_LUN, None)
    assert row == {
        "uuid": "0200000000600a",
        "canonical_name": "naa.6000c290",
        "device_type": "disk",
        "capacity_bytes": 512 * 209715200,
        "ssd": True,
        "local": True,
        "model": "Virtual disk",
        "vendor": "VMware",
        "is_boot": None,
    }


def test_map_scsi_lun_non_disk_nulls_disk_only_fields() -> None:
    """A non-disk ScsiLun (cdrom) nulls ssd / local / capacity_bytes."""
    row = _map_scsi_lun(_CDROM_LUN, None)
    assert row["device_type"] == "cdrom"
    assert row["ssd"] is None
    assert row["local"] is None
    assert row["capacity_bytes"] is None
    assert row["is_boot"] is None


@pytest.mark.asyncio
async def test_mixed_device_set_maps_every_lun() -> None:
    """The op maps every scsiLun row, disk and non-disk alike."""
    conn = _FakeConnector(
        props_by_host={
            STANDALONE_ESXI_HOST_MOID: _retrieve_result(
                STANDALONE_ESXI_HOST_MOID, _boxed_scsi_luns([_DISK_LUN, _CDROM_LUN])
            )
        }
    )
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _esxi_target(),  # type: ignore[arg-type]
        {},
    )
    assert out["device_count"] == 2
    kinds = {d["device_type"] for d in out["devices"]}
    assert kinds == {"disk", "cdrom"}
    disk = next(d for d in out["devices"] if d["device_type"] == "disk")
    assert disk["ssd"] is True and disk["local"] is True


@pytest.mark.asyncio
async def test_soap_flavoured_array_boxing_tolerated() -> None:
    """A ``{"_typeName": "ArrayOfScsiLun", "ScsiLun": [...]}`` box also unwraps."""
    soap_box = {"_typeName": "ArrayOfScsiLun", "ScsiLun": [_DISK_LUN]}
    conn = _FakeConnector(
        props_by_host={
            STANDALONE_ESXI_HOST_MOID: _retrieve_result(STANDALONE_ESXI_HOST_MOID, soap_box)
        }
    )
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _esxi_target(),  # type: ignore[arg-type]
        {},
    )
    assert out["device_count"] == 1
    assert out["devices"][0]["uuid"] == "0200000000600a"


@pytest.mark.asyncio
async def test_absent_scsi_lun_property_yields_empty_set() -> None:
    """A host whose propSet omits scsiLun returns an empty (ok) device set."""
    empty_result = {"objects": [{"obj": {"type": "HostSystem", "value": "host-15"}, "propSet": []}]}
    conn = _FakeConnector(
        hosts=[{"host": "host-15", "name": "esxi-01"}],
        props_by_host={"host-15": empty_result},
    )
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _vcenter_target(),  # type: ignore[arg-type]
        {"host": "esxi-01"},
    )
    assert out["status"] == "ok"
    assert out["devices"] == []
    assert out["device_count"] == 0


# ---------------------------------------------------------------------------
# Fail-closed refusal paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vcenter_without_host_refuses_host_required() -> None:
    """A vCenter target with no ``host`` param fails closed (no read issued)."""
    conn = _FakeConnector()
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _vcenter_target(),  # type: ignore[arg-type]
        {},
    )
    assert out["status"] == "host_required"
    assert out["devices"] == []
    assert conn.post_calls == []


@pytest.mark.asyncio
async def test_vcenter_unknown_host_refuses_host_not_found() -> None:
    conn = _FakeConnector(hosts=[{"host": "host-15", "name": "esxi-01"}])
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _vcenter_target(),  # type: ignore[arg-type]
        {"host": "ghost-99"},
    )
    assert out["status"] == "host_not_found"
    assert out["host"] == "ghost-99"
    assert conn.post_calls == []


@pytest.mark.asyncio
async def test_vcenter_ambiguous_name_refuses_with_candidates() -> None:
    conn = _FakeConnector(
        hosts=[{"host": "host-15", "name": "dup"}, {"host": "host-16", "name": "dup"}]
    )
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _vcenter_target(),  # type: ignore[arg-type]
        {"host": "dup"},
    )
    assert out["status"] == "ambiguous_host"
    assert sorted(out["candidate_hosts"]) == ["host-15", "host-16"]
    assert conn.post_calls == []


@pytest.mark.asyncio
async def test_unsupported_host_target_fails_closed() -> None:
    """A reachable fingerprint that is neither vCenter nor esxi fails closed."""
    conn = _FakeConnector()
    target = _Target(fingerprint={"product": "frobnicator", "reachable": True})
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        target,  # type: ignore[arg-type]
        {"host": "esxi-01"},
    )
    assert out["status"] == "unsupported_host_target"
    assert out["target_product"] == "frobnicator"
    assert conn.get_calls == [] and conn.post_calls == []


@pytest.mark.asyncio
async def test_read_failure_returns_storage_devices_unreadable() -> None:
    """A VI-JSON read failure is a typed refusal, not a bare stack trace."""
    conn = _FakeConnector(post_error=httpx.ConnectError("vi-json seam unavailable"))
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _esxi_target(),  # type: ignore[arg-type]
        {},
    )
    assert out["status"] == "storage_devices_unreadable"
    assert out["host"] == STANDALONE_ESXI_HOST_MOID
    assert out["devices"] == []
    assert "vi-json seam unavailable" in out["read_note"]


# ---------------------------------------------------------------------------
# Registration metadata
# ---------------------------------------------------------------------------


def test_op_is_registered_in_typed_ops_tuple() -> None:
    op_ids = {op.op_id for op in VMWARE_TYPED_OPS}
    assert "vmware.host.storage_devices" in op_ids


def test_op_metadata_is_a_safe_read() -> None:
    op = VMWARE_HOST_STORAGE_DEVICES_OP
    assert op.op_id == "vmware.host.storage_devices"
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key == HOST_STORAGE_DEVICES_GROUP_KEY
    # The handler_attr resolves to a real bound method on the connector.
    assert getattr(VmwareRestConnector, op.handler_attr, None) is not None
    # The response status enum names both the success and the refusal states.
    statuses = op.response_schema["properties"]["status"]["enum"]
    assert "ok" in statuses
    assert "unsupported_host_target" in statuses
    assert "storage_devices_unreadable" in statuses


def test_retrieve_body_targets_the_scsi_lun_path() -> None:
    body = build_host_storage_devices_retrieve_params("host-15")
    assert body["specSet"][0]["objectSet"][0]["obj"]["value"] == "host-15"
    assert body["specSet"][0]["propSet"][0]["pathSet"] == [
        "config.storageDevice.scsiLun",
        "configManager.bootDeviceSystem",
    ]


# ---------------------------------------------------------------------------
# Boot-device identification (HostBootDeviceSystem.QueryBootDevices)
# ---------------------------------------------------------------------------

_BOOT_SYSTEM_MOID = "bootDeviceSystem-1"


def test_map_scsi_lun_matched_boot_key_sets_is_boot_true() -> None:
    """A boot key embedding the LUN's canonical name marks it is_boot=true."""
    row = _map_scsi_lun(_DISK_LUN, "key-vim.host.BootDevice-naa.6000c290:1")
    assert row["is_boot"] is True


def test_map_scsi_lun_unmatched_boot_key_sets_is_boot_false() -> None:
    row = _map_scsi_lun(_DISK_LUN, "key-vim.host.BootDevice-naa.ffffffff:1")
    assert row["is_boot"] is False


@pytest.mark.asyncio
async def test_boot_device_matched_flags_the_boot_lun() -> None:
    """QueryBootDevices' currentBootDeviceKey marks the matching LUN is_boot=true."""
    conn = _FakeConnector(
        props_by_host={
            STANDALONE_ESXI_HOST_MOID: _retrieve_result(
                STANDALONE_ESXI_HOST_MOID,
                _boxed_scsi_luns([_DISK_LUN, _CDROM_LUN]),
                boot_system_moid=_BOOT_SYSTEM_MOID,
            )
        },
        boot_info=_boot_info("key-vim.host.BootDevice-naa.6000c290:1"),
    )
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _esxi_target(),  # type: ignore[arg-type]
        {},
    )
    assert out["boot_device_resolution"] == "matched"
    assert out["current_boot_device_key"] == "key-vim.host.BootDevice-naa.6000c290:1"
    assert out["boot_device_note"] is None
    by_type = {d["device_type"]: d["is_boot"] for d in out["devices"]}
    assert by_type == {"disk": True, "cdrom": False}
    # The boot query rode the QueryBootDevices method on the bootDeviceSystem moid.
    boot_calls = [p for p, _ in conn.post_calls if p.endswith("/QueryBootDevices")]
    assert boot_calls == [f"/HostBootDeviceSystem/{_BOOT_SYSTEM_MOID}/QueryBootDevices"]


@pytest.mark.asyncio
async def test_boot_device_no_match_all_false_key_echoed() -> None:
    """A resolved key that matches no LUN -> all is_boot false, key echoed, no_match."""
    conn = _FakeConnector(
        props_by_host={
            STANDALONE_ESXI_HOST_MOID: _retrieve_result(
                STANDALONE_ESXI_HOST_MOID,
                _boxed_scsi_luns([_DISK_LUN, _CDROM_LUN]),
                boot_system_moid=_BOOT_SYSTEM_MOID,
            )
        },
        boot_info=_boot_info("key-vim.host.BootDevice-naa.ffffffff:1"),
    )
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _esxi_target(),  # type: ignore[arg-type]
        {},
    )
    assert out["boot_device_resolution"] == "no_match"
    assert out["current_boot_device_key"] == "key-vim.host.BootDevice-naa.ffffffff:1"
    assert all(d["is_boot"] is False for d in out["devices"])


@pytest.mark.asyncio
async def test_boot_device_unavailable_when_no_config_manager() -> None:
    """No configManager.bootDeviceSystem -> is_boot null, unavailable, no boot query."""
    conn = _FakeConnector(
        props_by_host={
            STANDALONE_ESXI_HOST_MOID: _retrieve_result(
                STANDALONE_ESXI_HOST_MOID, _boxed_scsi_luns([_DISK_LUN])
            )
        }
    )
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _esxi_target(),  # type: ignore[arg-type]
        {},
    )
    assert out["boot_device_resolution"] == "unavailable"
    assert out["current_boot_device_key"] is None
    assert "bootDeviceSystem" in out["boot_device_note"]
    assert out["devices"][0]["is_boot"] is None
    # No QueryBootDevices call was issued (nothing to query).
    assert all(not p.endswith("/QueryBootDevices") for p, _ in conn.post_calls)


@pytest.mark.asyncio
async def test_boot_device_unavailable_when_query_errors_is_fail_safe() -> None:
    """A QueryBootDevices failure nulls is_boot but still returns the device listing."""
    conn = _FakeConnector(
        props_by_host={
            STANDALONE_ESXI_HOST_MOID: _retrieve_result(
                STANDALONE_ESXI_HOST_MOID,
                _boxed_scsi_luns([_DISK_LUN]),
                boot_system_moid=_BOOT_SYSTEM_MOID,
            )
        },
        boot_error=httpx.ConnectError("boot query unsupported"),
    )
    out = await host_storage_devices_impl(
        conn,  # type: ignore[arg-type]
        _make_operator(),
        _esxi_target(),  # type: ignore[arg-type]
        {},
    )
    # Fail-safe: the listing is still returned, is_boot nulled with a reason.
    assert out["status"] == "ok"
    assert out["device_count"] == 1
    assert out["boot_device_resolution"] == "unavailable"
    assert out["devices"][0]["is_boot"] is None
    assert "QueryBootDevices" in out["boot_device_note"]
