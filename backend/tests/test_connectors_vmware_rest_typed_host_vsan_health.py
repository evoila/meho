# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the ``vmware.host.vsan_health`` typed op (#2258).

Re-shipped from the former ``vmware.composite.host.vsan_health`` read: a
``source_kind="typed"`` bound method on :class:`VmwareRestConnector` that
queries ``VsanQueryVcClusterHealthSummary`` on the
``vsan-cluster-health-system`` singleton directly on the connector
session (no ``dispatch_child``, no ingested descriptor), so it works on a
fresh boot with zero catalog ingest.

The handler logic is exercised against a fake connector that records
the :meth:`_post_vsan` calls, so the assertion targets are the call-shape
contract (one cluster-scoped query on the vSAN health method, errors not
hidden) and the parse/aggregation contract, without a live httpx
transport. The real transport (VI-JSON on vCenter 9.x, SOAP on 8.0) is
covered by ``tests/test_connectors_vmware_rest_vsan_transport.py``; the
legacy ``/rest`` mount by the respx integration test in
``tests/integration/test_connectors_vmware_rest_vcsim.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest._mount import VsanMethod
from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.typed_ops import VMWARE_TYPED_OPS
from meho_backplane.connectors.vmware_rest.typed_ops_host_vsan_health import (
    HOST_VSAN_HEALTH_GROUP_KEY,
    VMWARE_HOST_VSAN_HEALTH_OP,
    VSAN_QUERY_HEALTH_SUMMARY,
    build_vsan_query_health_params,
    host_vsan_health_impl,
)

# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------


def _make_operator() -> Operator:
    """Synthetic operator for the typed-op handler unit tests."""
    return Operator(
        sub="op-host-vsan",
        name="Host vSAN Test",
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
    """Records the vSAN calls ``host_vsan_health_impl`` makes.

    Serves one canned ``VsanClusterHealthSummary`` on :meth:`_post_vsan`
    (or raises ``post_error``).
    """

    def __init__(
        self,
        *,
        summary: Any = None,
        post_error: Exception | None = None,
    ) -> None:
        self._summary = summary
        self._post_error = post_error
        self.post_calls: list[tuple[VsanMethod, dict[str, Any]]] = []

    async def _post_vsan(
        self,
        target: Any,
        vsan_method: VsanMethod,
        *,
        operator: Operator,
        json: dict[str, Any],
    ) -> Any:
        # The connector picks the transport (VI-JSON on 9.x, SOAP on 8.0);
        # the handler only names the vSAN method and the arguments (#4441).
        del target, operator
        self.post_calls.append((vsan_method, json))
        if self._post_error is not None:
            raise self._post_error
        return self._summary


def _vsan_summary(overall: str, groups: list[Any]) -> dict[str, Any]:
    """Build a ``VsanClusterHealthSummary`` payload for one cluster."""
    return {"overallHealth": overall, "groups": groups}


# ---------------------------------------------------------------------------
# Pure builders
# ---------------------------------------------------------------------------


def test_build_query_params_scopes_to_cluster_moref() -> None:
    """Pins the exact ``_typeName``-annotated MoRef body (#3103)."""
    body = build_vsan_query_health_params("domain-c123")
    assert body == {
        "cluster": {
            "_typeName": "ManagedObjectReference",
            "type": "ClusterComputeResource",
            "value": "domain-c123",
        }
    }


# ---------------------------------------------------------------------------
# host_vsan_health_impl — call-shape + aggregation
# ---------------------------------------------------------------------------


def test_health_method_is_the_vsan_health_summary_on_the_health_system() -> None:
    """The method, its vSAN object and the object's fixed id (vendor sample ``vsanapiutils.py``)."""
    assert isinstance(VSAN_QUERY_HEALTH_SUMMARY, VsanMethod)
    assert VSAN_QUERY_HEALTH_SUMMARY.mo_type == "VsanVcClusterHealthSystem"
    assert VSAN_QUERY_HEALTH_SUMMARY.moid == "vsan-cluster-health-system"
    assert VSAN_QUERY_HEALTH_SUMMARY.method == "VsanQueryVcClusterHealthSummary"


@pytest.mark.asyncio
async def test_queries_health_summary_for_cluster() -> None:
    """One health-service query fires, scoped to the cluster; groups + tests flatten."""
    summary = _vsan_summary(
        overall="green",
        groups=[
            {
                "groupId": "com.vmware.vsan.health.test.network",
                "groupName": "Network",
                "groupHealth": "green",
                "groupTests": [
                    {
                        "testId": "com.vmware.vsan.health.test.hostconnectivity",
                        "testName": "vSAN cluster partition",
                        "testHealth": "green",
                        "testShortDescription": "Checks host connectivity.",
                    },
                ],
            },
            {
                "groupId": "com.vmware.vsan.health.test.physicaldisks",
                "groupName": "Physical disk",
                "groupHealth": "yellow",
                "groupTests": [],
            },
        ],
    )
    conn = _FakeConnector(summary=summary)

    out = await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c123"})

    # One call on the vSAN transport; the cluster MoRef is the body arg.
    assert len(conn.post_calls) == 1
    vsan_method, body = conn.post_calls[0]
    assert vsan_method == VSAN_QUERY_HEALTH_SUMMARY
    assert body == {
        "cluster": {
            "_typeName": "ManagedObjectReference",
            "type": "ClusterComputeResource",
            "value": "domain-c123",
        }
    }

    assert out["cluster"] == "domain-c123"
    assert out["overall_health"] == "green"
    assert out["groups"] == [
        {
            "group_id": "com.vmware.vsan.health.test.network",
            "group_name": "Network",
            "group_health": "green",
            "tests": [
                {
                    "test_id": "com.vmware.vsan.health.test.hostconnectivity",
                    "test_name": "vSAN cluster partition",
                    "test_health": "green",
                    "test_short_description": "Checks host connectivity.",
                },
            ],
        },
        {
            "group_id": "com.vmware.vsan.health.test.physicaldisks",
            "group_name": "Physical disk",
            "group_health": "yellow",
            "tests": [],
        },
    ]
    assert "read_note" not in out


@pytest.mark.asyncio
async def test_failed_vsan_call_raises_instead_of_empty_health() -> None:
    """#4441: a failed vSAN call is not turned into empty health with a note."""
    error = httpx.HTTPStatusError(
        "vSAN fault",
        request=httpx.Request("POST", "https://vc.test.invalid/sdk/vim25/9.1.0.0/vsan/x"),
        response=httpx.Response(500, json={"_typeName": "VsanFault"}),
    )
    conn = _FakeConnector(post_error=error)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c404"})

    assert raised.value is error


@pytest.mark.asyncio
async def test_answer_without_a_summary_raises() -> None:
    """No health summary in the answer is an error, never an empty healthy-looking result."""
    conn = _FakeConnector(summary=None)

    with pytest.raises(RuntimeError, match="no health summary"):
        await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c1"})


@pytest.mark.asyncio
async def test_single_group_and_test_become_lists() -> None:
    """SOAP (vCenter 8.0) sends a list with one entry as a single dict; the op still lists it."""
    conn = _FakeConnector(
        summary={
            "overallHealth": "yellow",
            "groups": {
                "groupId": "com.vmware.vsan.health.test.data",
                "groupName": "Data",
                "groupHealth": "yellow",
                "groupTests": {
                    "testId": "com.vmware.vsan.health.test.objecthealth",
                    "testName": "vSAN object health",
                    "testHealth": "yellow",
                    "testShortDescription": "Checks the health of vSAN objects.",
                },
            },
        }
    )

    out = await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c1"})

    assert out["groups"] == [
        {
            "group_id": "com.vmware.vsan.health.test.data",
            "group_name": "Data",
            "group_health": "yellow",
            "tests": [
                {
                    "test_id": "com.vmware.vsan.health.test.objecthealth",
                    "test_name": "vSAN object health",
                    "test_health": "yellow",
                    "test_short_description": "Checks the health of vSAN objects.",
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_tolerates_legacy_value_envelope() -> None:
    conn = _FakeConnector(summary={"value": _vsan_summary(overall="red", groups=[])})

    out = await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c1"})

    assert out["overall_health"] == "red"
    assert out["groups"] == []


@pytest.mark.asyncio
async def test_tolerates_missing_groups_key() -> None:
    """A summary without a ``groups`` list degrades to an empty group list."""
    conn = _FakeConnector(summary={"overallHealth": "green"})

    out = await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c1"})

    assert out["overall_health"] == "green"
    assert out["groups"] == []


@pytest.mark.asyncio
async def test_missing_group_tests_degrade_to_empty_list() -> None:
    conn = _FakeConnector(
        summary=_vsan_summary(
            overall="yellow",
            groups=[{"groupId": "g", "groupName": "G", "groupHealth": "yellow"}],
        )
    )

    out = await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c1"})

    assert out["groups"][0]["tests"] == []


# ---------------------------------------------------------------------------
# Op metadata / registration contract
# ---------------------------------------------------------------------------


def test_op_is_a_registered_typed_op() -> None:
    assert VMWARE_HOST_VSAN_HEALTH_OP in VMWARE_TYPED_OPS
    assert VMWARE_HOST_VSAN_HEALTH_OP.op_id == "vmware.host.vsan_health"
    assert VMWARE_HOST_VSAN_HEALTH_OP.safety_level == "safe"
    assert VMWARE_HOST_VSAN_HEALTH_OP.requires_approval is False
    assert VMWARE_HOST_VSAN_HEALTH_OP.group_key == HOST_VSAN_HEALTH_GROUP_KEY


def test_contract_has_no_read_note_any_more() -> None:
    """#4441: failures are errors, so the answer has no ``read_note`` and always a group list."""
    schema = VMWARE_HOST_VSAN_HEALTH_OP.response_schema or {}
    assert "read_note" not in schema["properties"]
    assert schema["properties"]["groups"]["type"] == "array"
    assert schema["required"] == ["cluster", "overall_health", "groups"]
    guidance = VMWARE_HOST_VSAN_HEALTH_OP.llm_instructions or {}
    assert "read_note" not in guidance["output_shape"]
    assert "read_note" not in VMWARE_HOST_VSAN_HEALTH_OP.description


def test_handler_attr_resolves_to_a_connector_bound_method() -> None:
    handler = getattr(VmwareRestConnector, VMWARE_HOST_VSAN_HEALTH_OP.handler_attr, None)
    assert handler is not None
    assert callable(handler)


def test_handler_signature_has_no_dispatch_child() -> None:
    """Typed handler must NOT accept dispatch_child (else it'd be a composite)."""
    import inspect

    params = inspect.signature(VmwareRestConnector.host_vsan_health).parameters
    assert "dispatch_child" not in params
    assert "operator" in params
