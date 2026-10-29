# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vmware.host.datastores`` typed op (#3373).

The op lists the datastores mounted on one host, on a vCenter target
(VI-JSON) and on a standalone ESXi target (SOAP ``/sdk``). A fake connector
records the transport calls, so the tests check host resolution, the two
``RetrievePropertiesEx`` reads (the second one over every page), the
per-datastore mapping with the selected host's own ``accessible`` flag, the
refusals and the JSONFlux reduction without a live server. The SOAP tests
run real SOAP response bytes through the codec and then through this op's
mappers.
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
    _host_datastore_read_fault,
    _map_datastore_summaries,
    host_datastores_impl,
)
from meho_backplane.operations._request_preview import _is_previewable
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

_SOAP_FIXTURES = Path(__file__).parent / "fixtures" / "vmware_esxi_soap"
_RETRIEVE_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
_CONTINUE_PATH = "/PropertyCollector/propertyCollector/ContinueRetrievePropertiesEx"
_SUMMARY_PATH_SET = [
    "summary.name",
    "summary.type",
    "summary.capacity",
    "summary.freeSpace",
    "host",
]


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


def _host_mount(host_moid: str, accessible: bool) -> dict[str, Any]:
    """One VI-JSON ``DatastoreHostMount``: the mount entry of *host_moid*."""
    return {
        "_typeName": "DatastoreHostMount",
        "key": _moref(host_moid, "HostSystem"),
        "mountInfo": {
            "_typeName": "HostMountInfo",
            "path": f"/vmfs/volumes/{host_moid}",
            "accessMode": "readWrite",
            "mounted": True,
            "accessible": accessible,
        },
    }


def _summary_object(
    moid: str,
    name: str,
    ds_type: str,
    cap: int,
    free: int,
    mounts: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """One VI-JSON ``Datastore`` result object: ``summary.*`` leaves plus ``host``.

    *mounts* maps a host moid to its ``mountInfo.accessible``; by default the
    two hosts the tests use can both reach the datastore.
    """
    if mounts is None:
        mounts = {"host-15": True, STANDALONE_ESXI_HOST_MOID: True}
    return {
        "obj": _moref(moid),
        "propSet": [
            {"name": "summary.name", "val": _boxed("string", name)},
            {"name": "summary.type", "val": _boxed("string", ds_type)},
            {"name": "summary.capacity", "val": _boxed("long", cap)},
            {"name": "summary.freeSpace", "val": _boxed("long", free)},
            {
                "name": "host",
                "val": _boxed(
                    "ArrayOfDatastoreHostMount",
                    [_host_mount(h, accessible) for h, accessible in mounts.items()],
                ),
            },
        ],
    }


class _FakeConnector:
    """Records the calls ``host_datastores_impl`` makes and serves canned results.

    The datastore read is served page by page from *summary_pages*: the first
    page answers ``RetrievePropertiesEx``, every later one a
    ``ContinueRetrievePropertiesEx`` call. An exception in the list is raised
    in its turn. *summary_result* / *summary_error* are the one-page shorthand.
    """

    def __init__(
        self,
        *,
        host_results: dict[str, Any] | None = None,
        summary_result: Any = None,
        summary_pages: list[Any] | None = None,
        hosts: list[dict[str, str]] | None = None,
        host_error: Exception | None = None,
        summary_error: Exception | None = None,
    ) -> None:
        self._host_results = host_results or {}
        if summary_pages is None:
            summary_pages = [summary_error if summary_error is not None else summary_result]
        self._summary_pages = list(summary_pages)
        self._hosts = hosts if hosts is not None else [{"host": "host-15", "name": "esxi-01"}]
        self._host_error = host_error
        self.get_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []
        self.post_paths: list[str] = []
        self.promote_calls: list[bool] = []
        self.continue_sessions: list[str | None] = []

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

    def _next_summary_page(self) -> Any:
        page = self._summary_pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    async def _post_vmomi_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        json: dict[str, Any] | None = None,
        promote_managed_object_not_found: bool = False,
        session_used: Any = None,
        session_token: str | None = None,
    ) -> Any:
        del target, operator
        assert json is not None
        self.post_calls.append(json)
        self.post_paths.append(path)
        self.promote_calls.append(promote_managed_object_not_found)
        if session_used is not None:
            session_used.token = "<session-1>"
        if path == _CONTINUE_PATH:
            self.continue_sessions.append(session_token)
            return self._next_summary_page()
        assert path == _RETRIEVE_PATH
        mo_type = json["specSet"][0]["propSet"][0]["type"]
        if mo_type == "HostSystem":
            if self._host_error is not None:
                raise self._host_error
            moid = json["specSet"][0]["objectSet"][0]["obj"]["value"]
            return self._host_results[moid]
        return self._next_summary_page()


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
        "pathSet": _SUMMARY_PATH_SET,
    }
    assert [o["obj"]["value"] for o in summary_body["specSet"][0]["objectSet"]] == ["ds-a"]
    # Only the host read promotes a missing object to not_found.
    assert conn.promote_calls == [True, False]
    assert out["datastores"][0]["accessible"] is True


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
    """An empty ``datastore`` array (no ``missingSet``) is a real, successful empty list."""
    conn = _FakeConnector(host_results={"host-15": _host_result("host-15", [])})
    out = await _run(conn, _vcenter_target(), {"host": "esxi-01"})
    assert out["status"] == "ok"
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


def _host_missing_result(host_moid: str, fault_type: str | None) -> dict[str, Any]:
    """VI-JSON answer where ``HostSystem.datastore`` is in ``missingSet`` (9.x fault wrapper)."""
    entry: dict[str, Any] = {"_typeName": "MissingProperty", "path": "datastore"}
    if fault_type is not None:
        entry["fault"] = {
            "_typeName": "LocalizedMethodFault",
            "fault": {
                "_typeName": fault_type,
                "object": _moref(host_moid, "HostSystem"),
                "privilegeId": "System.View",
            },
            "localizedMessage": "PLACEHOLDER fault text that must not be echoed",
        }
    return {"objects": [{"obj": _moref(host_moid, "HostSystem"), "missingSet": [entry]}]}


@pytest.mark.asyncio
async def test_unreadable_host_datastore_property_is_not_an_empty_list() -> None:
    """A ``missingSet`` entry for ``datastore`` fails the read instead of answering ok + 0.

    vCenter answers HTTP 200 while the collector could not read the property
    (here: no permission). The op must not take that for a host without
    datastores; it returns ``datastores_unreadable``, names the fault class
    only, and makes no summary read.
    """
    conn = _FakeConnector(host_results={"host-15": _host_missing_result("host-15", "NoPermission")})
    out = await _run(conn, _vcenter_target(), {"host": "esxi-01"})
    assert out["status"] == "datastores_unreadable"
    assert out["datastores"] == [] and out["datastore_count"] == 0
    assert "HostSystem.datastore" in out["read_note"] and "NoPermission" in out["read_note"]
    assert "PLACEHOLDER" not in out["read_note"]
    assert len(conn.post_calls) == 1


@pytest.mark.asyncio
async def test_unreadable_host_datastore_property_without_fault_still_fails() -> None:
    """A ``missingSet`` entry with no usable fault still fails the read."""
    conn = _FakeConnector(host_results={"host-15": _host_missing_result("host-15", None)})
    out = await _run(conn, _vcenter_target(), {"host": "esxi-01"})
    assert out["status"] == "datastores_unreadable"
    assert "unknown fault" in out["read_note"]


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
# accessible is the selected host's own mount flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accessible_comes_from_the_selected_hosts_own_mount_entry() -> None:
    """Two hosts share a datastore; each host's answer carries its own mount state.

    On vCenter ``summary.accessible`` is true as soon as any host reaches the
    datastore. The op reads ``Datastore.host`` instead and picks the entry of
    the host that was asked for, so host-15 sees ``False`` while host-16 sees
    ``True`` for the same datastore.
    """
    shared = _summary_object(
        "datastore-12", "nfs01", "NFS", 2000, 500, mounts={"host-15": False, "host-16": True}
    )
    hosts = [{"host": "host-15", "name": "esxi-01"}, {"host": "host-16", "name": "esxi-02"}]
    host_results = {
        "host-15": _host_result("host-15", ["datastore-12"]),
        "host-16": _host_result("host-16", ["datastore-12"]),
    }

    def _conn() -> _FakeConnector:
        return _FakeConnector(
            host_results=host_results, summary_result={"objects": [shared]}, hosts=hosts
        )

    first = await _run(_conn(), _vcenter_target(), {"host": "esxi-01"})
    second = await _run(_conn(), _vcenter_target(), {"host": "esxi-02"})

    assert first["host"] == "host-15" and first["datastores"][0]["accessible"] is False
    assert second["host"] == "host-16" and second["datastores"][0]["accessible"] is True


@pytest.mark.asyncio
async def test_datastore_without_a_mount_entry_for_the_host_has_null_accessible() -> None:
    """A mount list that names only other hosts gives ``accessible: null``, never a guess."""
    conn = _FakeConnector(
        host_results={"host-15": _host_result("host-15", ["datastore-12"])},
        summary_result={
            "objects": [
                _summary_object("datastore-12", "nfs01", "NFS", 1, 1, mounts={"host-16": True})
            ]
        },
    )
    out = await _run(conn, _vcenter_target(), {"host": "esxi-01"})
    assert out["datastores"][0]["name"] == "nfs01"
    assert out["datastores"][0]["accessible"] is None


# ---------------------------------------------------------------------------
# The datastore read follows every page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_read_follows_every_continuation_page() -> None:
    """A split answer still fills every row; the token goes once to the continue call."""
    conn = _FakeConnector(
        host_results={"host-15": _host_result("host-15", ["datastore-11", "datastore-12"])},
        summary_pages=[
            {
                "objects": [_summary_object("datastore-11", "local-ds", "VMFS", 1000, 900)],
                "token": "<token-1>",
            },
            {"objects": [_summary_object("datastore-12", "nfs01", "NFS", 2000, 500)]},
        ],
    )
    out = await _run(conn, _vcenter_target(), {"host": "esxi-01"})
    assert out["status"] == "ok"
    assert [(d["datastore"], d["name"], d["accessible"]) for d in out["datastores"]] == [
        ("datastore-11", "local-ds", True),
        ("datastore-12", "nfs01", True),
    ]
    assert conn.post_paths == [_RETRIEVE_PATH, _RETRIEVE_PATH, _CONTINUE_PATH]
    assert conn.post_calls[2] == {"token": "<token-1>"}
    # The continuation is pinned to the session the first page used.
    assert conn.continue_sessions == ["<session-1>"]


@pytest.mark.asyncio
async def test_failed_continuation_page_returns_datastores_unreadable() -> None:
    """A lost page fails the whole read: no row comes back with null fields."""
    conn = _FakeConnector(
        host_results={"host-15": _host_result("host-15", ["datastore-11", "datastore-12"])},
        summary_pages=[
            {
                "objects": [_summary_object("datastore-11", "local-ds", "VMFS", 1000, 900)],
                "token": "<token-1>",
            },
            httpx.ConnectError("page lost"),
        ],
    )
    out = await _run(conn, _vcenter_target(), {"host": "esxi-01"})
    assert out["status"] == "datastores_unreadable"
    assert out["datastores"] == [] and out["datastore_count"] == 0
    assert "Datastore.summary" in out["read_note"] and "page lost" in out["read_note"]
    assert conn.post_paths == [_RETRIEVE_PATH, _RETRIEVE_PATH, _CONTINUE_PATH]


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
    """The SOAP fixture maps per object; longs become int, ha-host's mount flag becomes bool."""
    xml = (_SOAP_FIXTURES / "retrieve_datastore_summary.xml").read_text()
    rows = _map_datastore_summaries(parse_retrieve_result(xml), STANDALONE_ESXI_HOST_MOID)
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
        # the numbers as text and the op turns them into int; the bare
        # mountInfo accessible=false still lands as a bool.
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
    """An empty array over SOAP: no moids, and no read fault (a true empty list)."""
    xml = _soap_retrieve_response(
        '<objects><obj type="HostSystem">ha-host</obj><propSet><name>datastore</name>'
        '<val xsi:type="ArrayOfManagedObjectReference"/></propSet></objects>'
    )
    result = parse_retrieve_result(xml)
    assert _host_datastore_moids(result) == []
    assert _host_datastore_read_fault(result) is None


_SOAP_HOST_DATASTORE_MISSING_XML = _soap_retrieve_response(
    '<objects><obj type="HostSystem">ha-host</obj>'
    "<missingSet><path>datastore</path>"
    '<fault xsi:type="LocalizedMethodFault"><fault xsi:type="NoPermission">'
    '<object type="HostSystem">ha-host</object><privilegeId>System.View</privilegeId>'
    "</fault><localizedMessage>PLACEHOLDER fault text that must not be echoed"
    "</localizedMessage></fault></missingSet></objects>"
)


def test_soap_unreadable_host_datastore_property_names_the_fault_class() -> None:
    """A lone SOAP ``missingSet`` entry (force-listed by the codec) is seen as a read fault."""
    result = parse_retrieve_result(_SOAP_HOST_DATASTORE_MISSING_XML)
    assert _host_datastore_moids(result) == []
    assert _host_datastore_read_fault(result) == "NoPermission"


@pytest.mark.asyncio
async def test_soap_unreadable_host_datastore_property_is_not_an_empty_list() -> None:
    """On standalone ESXi the same unreadable property returns ``datastores_unreadable``."""
    conn = _FakeConnector(
        host_results={
            STANDALONE_ESXI_HOST_MOID: parse_retrieve_result(_SOAP_HOST_DATASTORE_MISSING_XML)
        }
    )
    out = await _run(conn, _esxi_target(), {})
    assert out["status"] == "datastores_unreadable"
    assert out["datastores"] == [] and out["datastore_count"] == 0
    assert "NoPermission" in out["read_note"] and "PLACEHOLDER" not in out["read_note"]
    assert len(conn.post_calls) == 1


@pytest.mark.asyncio
async def test_soap_host_without_datastores_is_ok_and_empty() -> None:
    """A standalone ESXi host with an empty ``datastore`` array answers ok with no rows."""
    xml = _soap_retrieve_response(
        '<objects><obj type="HostSystem">ha-host</obj><propSet><name>datastore</name>'
        '<val xsi:type="ArrayOfManagedObjectReference"/></propSet></objects>'
    )
    conn = _FakeConnector(host_results={STANDALONE_ESXI_HOST_MOID: parse_retrieve_result(xml)})
    out = await _run(conn, _esxi_target(), {})
    assert out["status"] == "ok"
    assert out["datastores"] == [] and out["datastore_count"] == 0
    assert len(conn.post_calls) == 1


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
                        {
                            "name": "host",
                            "val": [
                                {
                                    "key": _moref("ds-1-host", "HostSystem"),
                                    "mountInfo": {"accessible": "true"},
                                }
                            ],
                        },
                    ],
                }
            ]
        },
        "ds-1-host",
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
