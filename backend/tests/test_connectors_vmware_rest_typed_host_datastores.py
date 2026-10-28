# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vmware.host.datastores`` typed op (#3373).

The op lists the datastores mounted on one host, on a vCenter target
(VI-JSON) and on a standalone ESXi target (SOAP ``/sdk``). A fake connector
records the transport calls, so the tests check host resolution, the two
``RetrievePropertiesEx`` reads, the per-datastore mapping, the refusals and
the JSONFlux reduction without a live server. The SOAP tests run real
SOAP response bytes through the codec and then through this op's mappers.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params
from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.host_target import STANDALONE_ESXI_HOST_MOID
from meho_backplane.connectors.vmware_rest.soap import parse_retrieve_result
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_host_datastores import (
    HOST_DATASTORES_GROUP_KEY,
    VMWARE_HOST_DATASTORES_OP,
    _host_datastore_moids,
    _map_datastore_summaries,
    host_datastores_impl,
)
from meho_backplane.operations._request_preview import _is_previewable
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

_SOAP_FIXTURES = Path(__file__).parent / "fixtures" / "vmware_esxi_soap"
_RETRIEVE_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the settings the real JSONFlux reducer reads."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _operator() -> Operator:
    return Operator(
        sub="op-host-datastores",
        name="Host Datastores Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0b1"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    """Duck-typed target: ``fingerprint`` drives the host classifier."""

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
        fingerprint={"product": "esxi", "reachable": True, "version": "9.1"},
        name="esxi-standalone",
    )


def _moref(moid: str, mo_type: str = "Datastore") -> dict[str, Any]:
    return {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid}


def _host_result(host_moid: str, ds_moids: list[str]) -> dict[str, Any]:
    """VI-JSON ``HostSystem.datastore`` result (boxed ``ArrayOfManagedObjectReference``)."""
    return {
        "objects": [
            {
                "obj": _moref(host_moid, "HostSystem"),
                "propSet": [
                    {
                        "name": "datastore",
                        "val": {
                            "_typeName": "ArrayOfManagedObjectReference",
                            "_value": [_moref(m) for m in ds_moids],
                        },
                    }
                ],
            }
        ]
    }


def _boxed(type_name: str, value: Any) -> dict[str, Any]:
    return {"_typeName": type_name, "_value": value}


def _summary_object(moid: str, name: str, ds_type: str, cap: int, free: int) -> dict[str, Any]:
    """One VI-JSON ``Datastore.summary`` result object (boxed primitives)."""
    return {
        "obj": _moref(moid),
        "propSet": [
            {"name": "summary.name", "val": _boxed("string", name)},
            {"name": "summary.type", "val": _boxed("string", ds_type)},
            {"name": "summary.capacity", "val": _boxed("long", cap)},
            {"name": "summary.freeSpace", "val": _boxed("long", free)},
            {"name": "summary.accessible", "val": _boxed("boolean", True)},
        ],
    }


class _FakeConnector:
    """Records the calls ``host_datastores_impl`` makes and serves canned results."""

    def __init__(
        self,
        *,
        host_results: dict[str, Any] | None = None,
        summary_result: Any = None,
        hosts: list[dict[str, str]] | None = None,
        host_error: Exception | None = None,
        summary_error: Exception | None = None,
    ) -> None:
        self._host_results = host_results or {}
        self._summary_result = summary_result
        self._hosts = hosts if hosts is not None else [{"host": "host-15", "name": "esxi-01"}]
        self._host_error = host_error
        self._summary_error = summary_error
        self.get_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []
        self.promote_calls: list[bool] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        del target, operator
        return f"/api{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return adapt_filter_params("/api", query)

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: dict[str, Any] | None = None
    ) -> Any:
        del target, path, operator
        params = params or {}
        self.get_calls.append(params)
        names = params.get("names")
        if names:
            return [h for h in self._hosts if h["name"] in names]
        wanted = params.get("hosts")
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
        promote_managed_object_not_found: bool = False,
    ) -> Any:
        del target, operator
        assert path == _RETRIEVE_PATH
        assert json is not None
        self.post_calls.append(json)
        self.promote_calls.append(promote_managed_object_not_found)
        mo_type = json["specSet"][0]["propSet"][0]["type"]
        if mo_type == "HostSystem":
            if self._host_error is not None:
                raise self._host_error
            moid = json["specSet"][0]["objectSet"][0]["obj"]["value"]
            return self._host_results[moid]
        if self._summary_error is not None:
            raise self._summary_error
        return self._summary_result


async def _run(conn: _FakeConnector, target: _Target, params: dict[str, Any]) -> dict[str, Any]:
    return await host_datastores_impl(
        conn,  # type: ignore[arg-type]
        _operator(),
        target,  # type: ignore[arg-type]
        params,
    )


# ---------------------------------------------------------------------------
# Host resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_esxi_target_reads_ha_host_without_listing() -> None:
    """A standalone-ESXi target reads ha-host directly; the host param is ignored."""
    conn = _FakeConnector(
        host_results={STANDALONE_ESXI_HOST_MOID: _host_result(STANDALONE_ESXI_HOST_MOID, ["ds-a"])},
        summary_result={"objects": [_summary_object("ds-a", "nfs01", "NFS", 100, 40)]},
    )
    out = await _run(conn, _esxi_target(), {"host": "ignored"})
    assert out["status"] == "ok"
    assert out["host"] == STANDALONE_ESXI_HOST_MOID
    assert conn.get_calls == []
    host_body, summary_body = conn.post_calls
    assert host_body["specSet"][0]["objectSet"][0]["obj"]["value"] == STANDALONE_ESXI_HOST_MOID
    assert host_body["specSet"][0]["propSet"][0]["pathSet"] == ["datastore"]
    assert summary_body["specSet"][0]["propSet"][0] == {
        "_typeName": "PropertySpec",
        "type": "Datastore",
        "pathSet": [
            "summary.name",
            "summary.type",
            "summary.capacity",
            "summary.freeSpace",
            "summary.accessible",
        ],
    }
    assert [o["obj"]["value"] for o in summary_body["specSet"][0]["objectSet"]] == ["ds-a"]
    # Only the host read promotes a missing object to not_found.
    assert conn.promote_calls == [True, False]


@pytest.mark.asyncio
async def test_vcenter_target_resolves_host_by_name_and_returns_rows() -> None:
    """A vCenter target resolves the host via GET:/vcenter/host and maps every datastore."""
    conn = _FakeConnector(
        host_results={"host-15": _host_result("host-15", ["datastore-11", "datastore-12"])},
        summary_result={
            "objects": [
                _summary_object("datastore-12", "nfs01", "NFS", 2000, 500),
                _summary_object("datastore-11", "local-ds", "VMFS", 1000, 900),
            ]
        },
    )
    out = await _run(conn, _vcenter_target(), {"host": "esxi-01"})
    assert conn.get_calls == [{"names": ["esxi-01"]}]
    assert out == {
        "status": "ok",
        "host": "host-15",
        "datastores": [
            {
                "datastore": "datastore-11",
                "name": "local-ds",
                "type": "VMFS",
                "capacity_bytes": 1000,
                "free_space_bytes": 900,
                "accessible": True,
            },
            {
                "datastore": "datastore-12",
                "name": "nfs01",
                "type": "NFS",
                "capacity_bytes": 2000,
                "free_space_bytes": 500,
                "accessible": True,
            },
        ],
        "datastore_count": 2,
    }


@pytest.mark.asyncio
async def test_vcenter_target_resolves_host_by_moref_fallback() -> None:
    conn = _FakeConnector(
        host_results={"host-15": _host_result("host-15", [])},
    )
    out = await _run(conn, _vcenter_target(), {"host": "host-15"})
    assert out["status"] == "ok"
    assert out["host"] == "host-15"
    assert conn.get_calls == [{"names": ["host-15"]}, {"hosts": ["host-15"]}]


@pytest.mark.asyncio
async def test_host_without_datastores_skips_summary_read() -> None:
    conn = _FakeConnector(host_results={"host-15": _host_result("host-15", [])})
    out = await _run(conn, _vcenter_target(), {"host": "esxi-01"})
    assert out["datastores"] == []
    assert out["datastore_count"] == 0
    assert len(conn.post_calls) == 1


@pytest.mark.asyncio
async def test_vcenter_without_host_refuses_host_required() -> None:
    conn = _FakeConnector()
    out = await _run(conn, _vcenter_target(), {})
    assert out["status"] == "host_required"
    assert out["datastores"] == [] and out["datastore_count"] == 0
    assert conn.post_calls == []


@pytest.mark.asyncio
async def test_vcenter_unknown_host_refuses_host_not_found() -> None:
    conn = _FakeConnector()
    out = await _run(conn, _vcenter_target(), {"host": "nope"})
    assert out == {
        "status": "host_not_found",
        "host": "nope",
        "datastores": [],
        "datastore_count": 0,
    }


@pytest.mark.asyncio
async def test_vcenter_ambiguous_name_refuses_with_candidates() -> None:
    conn = _FakeConnector(
        hosts=[{"host": "host-15", "name": "esxi"}, {"host": "host-16", "name": "esxi"}]
    )
    out = await _run(conn, _vcenter_target(), {"host": "esxi"})
    assert out["status"] == "ambiguous_host"
    assert out["candidate_hosts"] == ["host-15", "host-16"]
    assert out["datastores"] == []


@pytest.mark.asyncio
async def test_unsupported_host_target_fails_closed_without_reads() -> None:
    """A reachable target that is neither vCenter nor ESXi gets the typed refusal."""
    conn = _FakeConnector()
    target = _Target(fingerprint={"product": "nsx", "reachable": True})
    out = await _run(conn, target, {"host": "esxi-01"})
    assert out["status"] == "unsupported_host_target"
    assert out["target_product"] == "nsx"
    assert out["datastores"] == [] and out["datastore_count"] == 0
    assert conn.get_calls == [] and conn.post_calls == []


# ---------------------------------------------------------------------------
# Read failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_read_failure_returns_datastores_unreadable() -> None:
    conn = _FakeConnector(host_error=RuntimeError("SOAP fault"))
    out = await _run(conn, _esxi_target(), {})
    assert out["status"] == "datastores_unreadable"
    assert out["datastores"] == [] and out["datastore_count"] == 0
    assert "HostSystem.datastore" in out["read_note"]


@pytest.mark.asyncio
async def test_summary_read_failure_returns_datastores_unreadable() -> None:
    conn = _FakeConnector(
        host_results={"host-15": _host_result("host-15", ["datastore-11"])},
        summary_error=httpx.ConnectError("boom"),
    )
    out = await _run(conn, _vcenter_target(), {"host": "esxi-01"})
    assert out["status"] == "datastores_unreadable"
    assert "Datastore.summary" in out["read_note"]


@pytest.mark.asyncio
async def test_datastore_missing_from_summary_keeps_its_row() -> None:
    """A datastore the summary read did not return still gets a row (null fields)."""
    conn = _FakeConnector(
        host_results={"host-15": _host_result("host-15", ["datastore-11", "datastore-12"])},
        summary_result={"objects": [_summary_object("datastore-11", "a", "VMFS", 1, 1)]},
    )
    out = await _run(conn, _vcenter_target(), {"host": "esxi-01"})
    assert out["datastore_count"] == 2
    assert out["datastores"][1] == {
        "datastore": "datastore-12",
        "name": None,
        "type": None,
        "capacity_bytes": None,
        "free_space_bytes": None,
        "accessible": None,
    }


# ---------------------------------------------------------------------------
# SOAP (standalone ESXi) deserialisation through this op's mappers
# ---------------------------------------------------------------------------


def _soap_retrieve_response(objects_xml: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<soapenv:Envelope "
        'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        '<soapenv:Body><RetrievePropertiesExResponse xmlns="urn:vim25"><returnval>'
        f"{objects_xml}"
        "</returnval></RetrievePropertiesExResponse></soapenv:Body></soapenv:Envelope>"
    )


def test_soap_datastore_summary_maps_one_row_per_datastore() -> None:
    """The SOAP summary fixture maps per object; longs become int, accessible bool."""
    xml = (_SOAP_FIXTURES / "retrieve_datastore_summary.xml").read_text()
    rows = _map_datastore_summaries(parse_retrieve_result(xml))
    assert rows == {
        "nas01.example.invalid:/exports/share": {
            "datastore": "nas01.example.invalid:/exports/share",
            "name": "t3363-validate",
            "type": "NFS",
            "capacity_bytes": 107374182400,
            "free_space_bytes": 53687091200,
            "accessible": True,
        },
        # The second object sends bare leaves (no xsi:type): the codec keeps
        # the numbers as text and the op turns them into int.
        "00000000-22222222-3333-444444444444": {
            "datastore": "00000000-22222222-3333-444444444444",
            "name": "datastore1",
            "type": "VMFS",
            "capacity_bytes": 21474836480,
            "free_space_bytes": 10737418240,
            "accessible": False,
        },
    }


def test_soap_host_datastore_array_unwraps_to_moids() -> None:
    """``ArrayOfManagedObjectReference`` over SOAP -> a list of Datastore moids."""
    xml = _soap_retrieve_response(
        '<objects><obj type="HostSystem">ha-host</obj><propSet><name>datastore</name>'
        '<val xsi:type="ArrayOfManagedObjectReference">'
        '<ManagedObjectReference type="Datastore">nas01.example.invalid:/exports/share'
        "</ManagedObjectReference>"
        '<ManagedObjectReference type="Datastore">00000000-22222222-3333-444444444444'
        "</ManagedObjectReference>"
        "</val></propSet></objects>"
    )
    assert _host_datastore_moids(parse_retrieve_result(xml)) == [
        "nas01.example.invalid:/exports/share",
        "00000000-22222222-3333-444444444444",
    ]


def test_soap_host_datastore_single_element_array_is_still_a_list() -> None:
    """A host with one datastore: the one-element array still yields one moid."""
    xml = _soap_retrieve_response(
        '<objects><obj type="HostSystem">ha-host</obj><propSet><name>datastore</name>'
        '<val xsi:type="ArrayOfManagedObjectReference">'
        '<ManagedObjectReference type="Datastore">ds-only</ManagedObjectReference>'
        "</val></propSet></objects>"
    )
    assert _host_datastore_moids(parse_retrieve_result(xml)) == ["ds-only"]


def test_soap_host_datastore_empty_array_yields_no_moids() -> None:
    xml = _soap_retrieve_response(
        '<objects><obj type="HostSystem">ha-host</obj><propSet><name>datastore</name>'
        '<val xsi:type="ArrayOfManagedObjectReference"/></propSet></objects>'
    )
    assert _host_datastore_moids(parse_retrieve_result(xml)) == []


def test_numeric_strings_coerce_and_bad_values_null() -> None:
    """VI-JSON or SOAP: numeric text -> int; non-numeric / bool / missing -> null."""
    rows = _map_datastore_summaries(
        {
            "objects": [
                {
                    "obj": _moref("ds-1"),
                    "propSet": [
                        {"name": "summary.name", "val": "x"},
                        {"name": "summary.capacity", "val": "123"},
                        {"name": "summary.freeSpace", "val": "n/a"},
                        {"name": "summary.accessible", "val": "true"},
                    ],
                }
            ]
        }
    )
    row = rows["ds-1"]
    assert row["capacity_bytes"] == 123
    assert row["free_space_bytes"] is None
    assert row["accessible"] is None
    assert row["type"] is None


# ---------------------------------------------------------------------------
# JSONFlux, preview, registration
# ---------------------------------------------------------------------------


def _ok_result(count: int) -> dict[str, Any]:
    rows = [
        {
            "datastore": f"datastore-{i}",
            "name": f"ds-{i:03d}",
            "type": "NFS",
            "capacity_bytes": 1_000_000_000,
            "free_space_bytes": 500_000_000,
            "accessible": True,
        }
        for i in range(count)
    ]
    return {"status": "ok", "host": "host-15", "datastores": rows, "datastore_count": count}


@pytest.mark.asyncio
async def test_small_result_stays_inline() -> None:
    out = _ok_result(3)
    reduced, handle = await JsonFluxReducer(sample_byte_budget=4096).reduce(out, None)
    assert handle is None
    assert reduced is out


@pytest.mark.asyncio
async def test_large_result_reduces_to_a_handle() -> None:
    out = _ok_result(80)
    reduced, handle = await JsonFluxReducer(sample_byte_budget=4096).reduce(out, None)
    assert handle is not None
    assert handle.total_rows == 80
    assert reduced["source_key"] == "datastores"
    assert "datastores" not in reduced


def test_safe_read_previews_as_unavailable() -> None:
    """Not ingested, not destructive, no approval: preview_operation says unavailable."""
    op = VMWARE_HOST_DATASTORES_OP
    descriptor = SimpleNamespace(
        source_kind="typed",
        safety_level=op.safety_level,
        requires_approval=op.requires_approval,
        op_id=op.op_id,
    )
    assert _is_previewable(descriptor) is False  # type: ignore[arg-type]


def test_op_is_registered_as_a_safe_read() -> None:
    assert VMWARE_HOST_DATASTORES_OP in VMWARE_TYPED_OPS
    op = VMWARE_HOST_DATASTORES_OP
    assert op.op_id == "vmware.host.datastores"
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key == HOST_DATASTORES_GROUP_KEY
    assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[HOST_DATASTORES_GROUP_KEY]
    assert getattr(VmwareRestConnector, op.handler_attr, None) is not None
    statuses = op.response_schema["properties"]["status"]["enum"]  # type: ignore[index]
    assert "unsupported_host_target" in statuses
    assert "datastores_unreadable" in statuses
