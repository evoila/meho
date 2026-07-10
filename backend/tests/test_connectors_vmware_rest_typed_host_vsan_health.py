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
:meth:`mount_op_path` / :meth:`_post_json` calls, so the assertion
targets are the call-shape contract (single cluster-scoped query,
mounted, best-effort) and the parse/aggregation contract, without a live
httpx transport. The end-to-end transport + modern/legacy mount routing
is covered by the respx integration test in
``tests/integration/test_connectors_vmware_rest_vcsim.py``. Output parity
against the composite version is preserved byte-for-byte by these
assertions (they mirror the pre-#2258 composite-read coverage).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.typed_ops import VMWARE_TYPED_OPS
from meho_backplane.connectors.vmware_rest.typed_ops_host_vsan_health import (
    HOST_VSAN_HEALTH_GROUP_KEY,
    VMWARE_HOST_VSAN_HEALTH_OP,
    build_vsan_query_health_params,
    host_vsan_health_impl,
)

_VSAN_PATH = (
    "/api/VsanVcClusterHealthSystem/vsan-cluster-health-system/VsanQueryVcClusterHealthSummary"
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
    """Records the transport calls ``host_vsan_health_impl`` makes.

    Serves one canned ``VsanClusterHealthSummary`` on :meth:`_post_json`
    (or raises ``post_error`` to exercise the best-effort path).
    """

    def __init__(
        self,
        *,
        summary: Any = None,
        post_error: Exception | None = None,
        mount_prefix: str = "/api",
    ) -> None:
        self._summary = summary
        self._post_error = post_error
        self._mount_prefix = mount_prefix
        self.mount_calls: list[str] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        del target, operator
        self.mount_calls.append(path)
        return f"{self._mount_prefix}{path}"

    async def _post_json(
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
    body = build_vsan_query_health_params("domain-c123")
    assert body == {"cluster": {"type": "ClusterComputeResource", "value": "domain-c123"}}


# ---------------------------------------------------------------------------
# host_vsan_health_impl — call-shape + aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queries_health_summary_for_cluster_mounted() -> None:
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

    # The singleton moId rides the mounted path; the cluster MoRef is the body arg.
    assert conn.mount_calls == [
        "/VsanVcClusterHealthSystem/vsan-cluster-health-system/VsanQueryVcClusterHealthSummary"
    ]
    assert len(conn.post_calls) == 1
    path, body = conn.post_calls[0]
    assert path == _VSAN_PATH
    assert body == {"cluster": {"type": "ClusterComputeResource", "value": "domain-c123"}}

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
async def test_read_is_best_effort_on_error() -> None:
    """A failed health-service read nulls groups/overall + records a ``read_note``."""
    conn = _FakeConnector(
        post_error=httpx.HTTPStatusError(
            "boom",
            request=httpx.Request("POST", "https://vc/vsanHealth"),
            response=httpx.Response(400),
        )
    )

    out = await host_vsan_health_impl(conn, _make_operator(), _Target(), {"cluster": "domain-c404"})

    assert out["cluster"] == "domain-c404"
    assert out["overall_health"] is None
    assert out["groups"] is None
    assert "VsanQueryVcClusterHealthSummary" in out["read_note"]


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
