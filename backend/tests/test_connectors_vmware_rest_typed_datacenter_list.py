# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``vmware.datacenter.list`` typed read.

* :func:`datacenter_list_impl` against a recording fake connector with saved
  answers: the rows, the three counts per datacenter, the ``datacenters``
  filter on the ``/api`` and the legacy ``/rest`` mount, the name pattern, a
  failed count (``null`` with ``read_note``) and a failed datacenter list (an
  error);
* the real connector over mocked HTTP: every call lands on ``/api`` with the
  bare ``datacenters`` query parameter (the same on vCenter 8 and 9);
* the operation metadata: ``safe``, runs at once, on both catalogs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest._mount import adapt_filter_params
from meho_backplane.connectors.vmware_rest.typed_ops import (
    VMWARE_TYPED_OPS,
    VMWARE_TYPED_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.vmware_rest.typed_ops_datacenter_list import (
    VMWARE_DATACENTER_LIST_OP,
    datacenter_list_impl,
)
from meho_backplane.operations._validate import validate_params

#: Saved answers of a small vCenter: two datacenters.
_DATACENTERS = [
    {"datacenter": "datacenter-3", "name": "Prod-DC"},
    {"datacenter": "datacenter-21", "name": "lab-dc"},
]
_PER_DATACENTER: dict[str, dict[str, list[dict[str, Any]]]] = {
    "datacenter-3": {
        "/vcenter/cluster": [{"cluster": "domain-c8", "name": "cl-1"}],
        "/vcenter/host": [{"host": f"host-{n}", "name": f"esx-{n}"} for n in range(3)],
        "/vcenter/datastore": [{"datastore": "datastore-11", "name": "ds-1"}] * 2,
    },
    "datacenter-21": {
        "/vcenter/cluster": [],
        "/vcenter/host": [{"host": "host-40", "name": "esx-40"}],
        "/vcenter/datastore": [{"datastore": "datastore-41", "name": "ds-local"}],
    },
}


def _operator() -> Operator:
    return Operator(
        sub="op-datacenter-list",
        name="Datacenter List Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Target:
    name: str = "vc-test"
    host: str = "vc.test.invalid"
    port: int | None = 443
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


class _FakeConnector:
    """Records the REST calls and answers them from the saved answers."""

    def __init__(
        self,
        *,
        mount_prefix: str = "/api",
        listing: Any = None,
        fail_path: str | None = None,
        envelope: bool = False,
    ) -> None:
        self._mount_prefix = mount_prefix
        self._listing = _DATACENTERS if listing is None else listing
        self._fail_path = fail_path
        self._envelope = envelope
        self.get_calls: list[tuple[str, dict[str, Any] | None]] = []

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
        self.get_calls.append((path, params))
        spec_path = path.removeprefix(self._mount_prefix)
        if spec_path == self._fail_path:
            request = httpx.Request("GET", f"https://vc.test.invalid{path}")
            raise httpx.HTTPStatusError(
                "503 Service Unavailable",
                request=request,
                response=httpx.Response(503, request=request),
            )
        if spec_path == "/vcenter/datacenter":
            answer: Any = self._listing
        else:
            assert params is not None
            (moid,) = next(iter(params.values()))
            answer = _PER_DATACENTER[moid][spec_path]
        return {"value": answer} if self._envelope else answer


async def test_lists_each_datacenter_with_its_counts() -> None:
    conn = _FakeConnector()
    out = await datacenter_list_impl(conn, _operator(), _Target(), {})
    assert out == {
        "datacenters": [
            {
                "id": "datacenter-3",
                "name": "Prod-DC",
                "cluster_count": 1,
                "host_count": 3,
                "datastore_count": 2,
                "read_note": None,
            },
            {
                "id": "datacenter-21",
                "name": "lab-dc",
                "cluster_count": 0,
                "host_count": 1,
                "datastore_count": 1,
                "read_note": None,
            },
        ]
    }
    Draft202012Validator(VMWARE_DATACENTER_LIST_OP.response_schema).validate(out)


async def test_counts_use_the_bare_datacenters_filter_on_api() -> None:
    conn = _FakeConnector()
    await datacenter_list_impl(conn, _operator(), _Target(), {})
    assert conn.get_calls[0] == ("/api/vcenter/datacenter", None)
    assert conn.get_calls[1:4] == [
        ("/api/vcenter/cluster", {"datacenters": ["datacenter-3"]}),
        ("/api/vcenter/host", {"datacenters": ["datacenter-3"]}),
        ("/api/vcenter/datastore", {"datacenters": ["datacenter-3"]}),
    ]
    assert len(conn.get_calls) == 7


async def test_counts_use_the_prefixed_filter_on_the_legacy_rest_mount() -> None:
    conn = _FakeConnector(mount_prefix="/rest", envelope=True)
    out = await datacenter_list_impl(conn, _operator(), _Target(), {})
    assert conn.get_calls[1] == ("/rest/vcenter/cluster", {"filter.datacenters": ["datacenter-3"]})
    assert [row["host_count"] for row in out["datacenters"]] == [3, 1]


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("prod*", ["datacenter-3"]),
        ("LAB-DC", ["datacenter-21"]),
        ("*dc", ["datacenter-3", "datacenter-21"]),
        ("?ab-dc", ["datacenter-21"]),
        ("nothing*", []),
    ],
)
async def test_name_pattern_keeps_only_matching_datacenters(
    pattern: str, expected: list[str]
) -> None:
    conn = _FakeConnector()
    out = await datacenter_list_impl(conn, _operator(), _Target(), {"name": pattern})
    assert [row["id"] for row in out["datacenters"]] == expected
    # Only a matching datacenter costs the three count calls.
    assert len(conn.get_calls) == 1 + 3 * len(expected)


async def test_a_failed_count_is_null_with_a_note_and_the_rest_stays() -> None:
    conn = _FakeConnector(fail_path="/vcenter/host")
    out = await datacenter_list_impl(conn, _operator(), _Target(), {})
    first = out["datacenters"][0]
    assert first["host_count"] is None
    assert first["cluster_count"] == 1
    assert first["datastore_count"] == 2
    assert "host_count not read" in first["read_note"]
    assert "HTTPStatusError" in first["read_note"]
    Draft202012Validator(VMWARE_DATACENTER_LIST_OP.response_schema).validate(out)


async def test_a_failed_datacenter_list_fails_the_operation() -> None:
    conn = _FakeConnector(fail_path="/vcenter/datacenter")
    with pytest.raises(httpx.HTTPStatusError):
        await datacenter_list_impl(conn, _operator(), _Target(), {})


async def test_a_datacenter_list_that_is_not_a_list_fails_the_operation() -> None:
    conn = _FakeConnector(listing={"unexpected": "shape"})
    with pytest.raises(RuntimeError, match="expected a list"):
        await datacenter_list_impl(conn, _operator(), _Target(), {})


async def test_entries_without_a_moid_are_skipped() -> None:
    conn = _FakeConnector(listing=[{"name": "no-id"}, "junk", _DATACENTERS[1]])
    out = await datacenter_list_impl(conn, _operator(), _Target(), {})
    assert [row["id"] for row in out["datacenters"]] == ["datacenter-21"]


# ---------------------------------------------------------------------------
# The real connector over mocked HTTP
# ---------------------------------------------------------------------------


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


@dataclass
class _StubTarget:
    name: str = "vc-example"
    host: str = "vc.example.test"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-example"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))
    fingerprint: dict[str, Any] | None = None


async def test_real_connector_reads_api_paths_with_the_bare_filter() -> None:
    """The REST paths are the same on vCenter 8 and 9; no release lookup is needed."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url="https://vc.example.test") as mock:
            mock.post("/api/session").respond(200, json="tok")
            mock.get("/api/vcenter/datacenter").respond(200, json=_DATACENTERS[:1])
            counts = {}
            for path, rows in _PER_DATACENTER["datacenter-3"].items():
                counts[path] = mock.get(
                    f"/api{path}", params={"datacenters": "datacenter-3"}
                ).respond(200, json=rows)
            out = await connector.datacenter_list(_operator(), _StubTarget(), {})
        assert all(route.call_count == 1 for route in counts.values())
        assert out["datacenters"][0]["host_count"] == 3
        assert out["datacenters"][0]["read_note"] is None
    finally:
        connector._session_tokens.clear()
        for client in connector._clients.values():
            await client.aclose()
        connector._clients.clear()


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def test_op_is_a_safe_typed_read_that_runs_at_once() -> None:
    op = VMWARE_DATACENTER_LIST_OP
    assert op in VMWARE_TYPED_OPS
    assert op.op_id == "vmware.datacenter.list"
    assert op.safety_level == "safe"
    assert op.requires_approval is False
    assert op.group_key is not None
    assert VMWARE_TYPED_WHEN_TO_USE_BY_GROUP[op.group_key].strip()
    assert callable(getattr(VmwareRestConnector, op.handler_attr))


def test_schema_takes_only_an_optional_name_pattern() -> None:
    schema = VMWARE_DATACENTER_LIST_OP.parameter_schema
    assert validate_params(schema, {}) == []
    assert validate_params(schema, {"name": "prod*"}) == []
    assert validate_params(schema, {"name": ""}) != []
    assert validate_params(schema, {"folder": "x"}) != []
    for example in schema["examples"]:
        assert validate_params(schema, example) == []
