# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handler + schema tests for the Supervisor (WCP) composites (#3281).

Covers the three ``vmware.composite.supervisor.*`` composites in
:mod:`~meho_backplane.connectors.vmware_rest.composites._supervisor`:
``enable`` / ``disable`` (dangerous, approval-gated writes) and ``status``
(safe read). Two concerns:

* **Handler behaviour** with a stubbed gate (``_GateRecorder``) and a
  recording connector double: the enable happy path (spec flows to the
  ``enable_on_compute_cluster`` POST body, Supervisor id returned, async
  ``enabling`` status, no blocking poll), the **loud** server-side refusals
  (unknown network stack / edge provider / missing control-plane essentials
  never reach the wire), the parked-gate short-circuit, the disable path,
  and the poll-friendly status reshaping (scalar ``config_status`` /
  ``kubernetes_status`` / ``ready`` inline, capped message arrays).
* **Parameter-schema conformance**: the registered JSON Schema accepts a
  well-formed enable/disable/status payload and rejects unknown top-level
  keys + missing required fields, so a malformed ``params`` is caught by the
  dispatcher's Draft202012Validator before the handler runs.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites import _supervisor, _write
from meho_backplane.connectors.vmware_rest.composites._supervisor import (
    supervisor_disable_composite,
    supervisor_enable_composite,
    supervisor_status_composite,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    SUPERVISOR_DISABLE_PARAMETER_SCHEMA,
    SUPERVISOR_ENABLE_PARAMETER_SCHEMA,
    SUPERVISOR_STATUS_PARAMETER_SCHEMA,
)

_ENABLE_OP_ID = (
    "POST:/vcenter/namespace-management/supervisors/{cluster}?action=enable_on_compute_cluster"
)
_DISABLE_OP_ID = "POST:/vcenter/namespace-management/clusters/{cluster}?action=disable"


def _operator() -> Operator:
    return Operator(
        sub="agent-supervisor-composite",
        name="Supervisor Composite Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-0000000032a1"),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
    )


class _GateRecorder:
    """Recording stub for :func:`enforce_subop_policy` (auto-execute by default)."""

    def __init__(self, gate_for: dict[str, OperationResult] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._gate_for = gate_for or {}

    async def __call__(
        self,
        *,
        operator: Operator,
        connector_id: str,
        op_id: str,
        safety_level: str,
        requires_approval: bool,
        target: Any,
        params: dict[str, Any],
    ) -> OperationResult | None:
        self.calls.append(
            {
                "op_id": op_id,
                "connector_id": connector_id,
                "safety_level": safety_level,
                "requires_approval": requires_approval,
                "params": dict(params),
            }
        )
        return self._gate_for.get(op_id)

    @property
    def gated_op_ids(self) -> list[str]:
        return [c["op_id"] for c in self.calls]


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _GateRecorder:
    """Install a default (auto-execute) gate recorder on the ``_write`` module.

    The supervisor writes route through ``_write._write_sub_op``, which
    resolves ``enforce_subop_policy`` in the ``_write`` namespace.
    """
    recorder = _GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


class _RecordingConnector:
    """Recording connector double serving the status read + recording writes."""

    def __init__(
        self,
        *,
        enable_result: Any = "supervisor-42",
        status_info: dict[str, Any] | None = None,
    ) -> None:
        self._enable_result = enable_result
        self._status_info = status_info or {}
        self.writes: list[dict[str, Any]] = []
        self.gets: list[str] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        del target, operator
        return f"/api{path}"

    async def adapt_op_query(
        self, target: Any, query: dict[str, Any] | None, operator: Operator
    ) -> dict[str, Any] | None:
        del target, operator
        return query or None

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        del target, operator, params
        self.gets.append(path)
        return self._status_info

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: Any = None,
        timeout: Any = None,
    ) -> Any:
        del target, operator, timeout
        self.writes.append({"path": path, "verb": verb, "json": json})
        if path.endswith("?action=enable_on_compute_cluster"):
            return self._enable_result
        return None


def _valid_enable_params(
    *, network_type: str = "VSPHERE", provider: str = "VSPHERE_FOUNDATION"
) -> dict[str, Any]:
    return {
        "cluster": "domain-c8",
        "name": "envision-supervisor",
        "control_plane": {
            "network": {"backing": {"network": "dvportgroup-30"}},
            "storage_policy": "nfs-policy-1",
            "size": "TINY",
            "count": 3,
        },
        "workloads": {
            "network": {"network_type": network_type},
            "edge": {
                "provider": provider,
                "load_balancer_address_ranges": [{"address": "10.0.0.10", "count": 16}],
            },
            "storage": {
                "ephemeral_storage_policy": "nfs-policy-1",
                "image_storage_policy": "nfs-policy-1",
            },
        },
    }


# ---------------------------------------------------------------------------
# enable
# ---------------------------------------------------------------------------


async def test_enable_happy_path_dispatches_spec_body(gate: _GateRecorder) -> None:
    connector = _RecordingConnector(enable_result="supervisor-42")
    result = await supervisor_enable_composite(
        operator=_operator(),
        target=object(),
        params=_valid_enable_params(),
        connector=connector,  # type: ignore[arg-type]
    )
    assert result == {
        "status": "enabling",
        "cluster": "domain-c8",
        "supervisor": "supervisor-42",
        "network_type": "VSPHERE",
        "edge_provider": "VSPHERE_FOUNDATION",
        "guidance": result["guidance"],  # type: ignore[index]
    }
    # Gate ran against the canonical enable op_id with the composite posture.
    assert gate.gated_op_ids == [_ENABLE_OP_ID]
    assert gate.calls[0]["safety_level"] == "dangerous"
    # Exactly one write reached the wire: the enable POST with the nested body
    # (cluster is the path var; zone omitted).
    assert len(connector.writes) == 1
    write = connector.writes[0]
    expected_path = "/api" + _ENABLE_OP_ID.split(":", 1)[1].replace("{cluster}", "domain-c8")
    assert write["path"] == expected_path
    assert write["verb"] == "POST"
    assert set(write["json"]) == {"name", "control_plane", "workloads"}
    assert "cluster" not in write["json"]
    assert write["json"]["workloads"]["edge"]["provider"] == "VSPHERE_FOUNDATION"


async def test_enable_nsx_vpc_stack_is_accepted(gate: _GateRecorder) -> None:
    connector = _RecordingConnector()
    result = await supervisor_enable_composite(
        operator=_operator(),
        target=object(),
        params=_valid_enable_params(network_type="NSX_VPC", provider="NSX_VPC"),
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == "enabling"
    assert result["network_type"] == "NSX_VPC"
    assert len(connector.writes) == 1


async def test_enable_zone_flows_into_body_when_present(gate: _GateRecorder) -> None:
    connector = _RecordingConnector()
    params = _valid_enable_params()
    params["zone"] = "zone-1"
    await supervisor_enable_composite(
        operator=_operator(),
        target=object(),
        params=params,
        connector=connector,  # type: ignore[arg-type]
    )
    assert connector.writes[0]["json"]["zone"] == "zone-1"


@pytest.mark.parametrize(
    ("network_type", "provider"),
    [
        ("VSPHERE_NETWORK", "VSPHERE_FOUNDATION"),  # deprecated flat-spec term, not 9.x
        ("VSPHERE", "AVI"),  # AVI is a post-enable derived provider, not an input
        ("bogus", "VSPHERE_FOUNDATION"),
    ],
)
async def test_enable_refuses_unknown_network_provider_loudly(
    gate: _GateRecorder, network_type: str, provider: str
) -> None:
    connector = _RecordingConnector()
    result = await supervisor_enable_composite(
        operator=_operator(),
        target=object(),
        params=_valid_enable_params(network_type=network_type, provider=provider),
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == "unknown_network_provider"
    assert result["supervisor"] is None
    assert result["guidance"]
    # Fail-closed: nothing reached the wire and the gate was never consulted.
    assert connector.writes == []
    assert gate.calls == []


async def test_enable_refuses_missing_control_plane_storage_policy(gate: _GateRecorder) -> None:
    connector = _RecordingConnector()
    params = _valid_enable_params()
    del params["control_plane"]["storage_policy"]
    result = await supervisor_enable_composite(
        operator=_operator(),
        target=object(),
        params=params,
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == "invalid_spec"
    assert connector.writes == []
    assert gate.calls == []


async def test_enable_parked_gate_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    parked = OperationResult(
        status="awaiting_approval", op_id="vmware.composite.supervisor.enable", duration_ms=0
    )
    recorder = _GateRecorder(gate_for={_ENABLE_OP_ID: parked})
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    connector = _RecordingConnector()
    result = await supervisor_enable_composite(
        operator=_operator(),
        target=object(),
        params=_valid_enable_params(),
        connector=connector,  # type: ignore[arg-type]
    )
    assert result is parked
    assert connector.writes == []


# ---------------------------------------------------------------------------
# disable
# ---------------------------------------------------------------------------


async def test_disable_happy_path(gate: _GateRecorder) -> None:
    connector = _RecordingConnector()
    result = await supervisor_disable_composite(
        operator=_operator(),
        target=object(),
        params={"cluster": "domain-c8"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == "disabling"
    assert result["cluster"] == "domain-c8"
    assert gate.gated_op_ids == [_DISABLE_OP_ID]
    assert len(connector.writes) == 1
    write = connector.writes[0]
    assert write["path"] == "/api/vcenter/namespace-management/clusters/domain-c8?action=disable"
    # No request body on disable.
    assert write["json"] is None


async def test_disable_parked_gate_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    parked = OperationResult(
        status="awaiting_approval", op_id="vmware.composite.supervisor.disable", duration_ms=0
    )
    recorder = _GateRecorder(gate_for={_DISABLE_OP_ID: parked})
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    connector = _RecordingConnector()
    result = await supervisor_disable_composite(
        operator=_operator(),
        target=object(),
        params={"cluster": "domain-c8"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result is parked
    assert connector.writes == []


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


async def test_status_running_ready_is_pollable_inline() -> None:
    connector = _RecordingConnector(
        status_info={
            "config_status": "RUNNING",
            "kubernetes_status": "READY",
            "messages": [],
            "conditions": [{"type": "InfrastructureInitialized", "status": "TRUE"}],
        }
    )
    result = await supervisor_status_composite(
        operator=_operator(),
        target=object(),
        params={"cluster": "domain-c8"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["config_status"] == "RUNNING"
    assert result["kubernetes_status"] == "READY"
    assert result["ready"] is True
    assert result["condition_count"] == 1
    assert connector.gets == ["/api/vcenter/namespace-management/clusters/domain-c8"]


async def test_status_configuring_is_not_ready() -> None:
    connector = _RecordingConnector(
        status_info={"config_status": "CONFIGURING", "kubernetes_status": "ERROR"}
    )
    result = await supervisor_status_composite(
        operator=_operator(),
        target=object(),
        params={"cluster": "domain-c8"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["ready"] is False
    assert result["messages"] == []
    assert result["message_count"] == 0


async def test_status_caps_message_arrays_but_reports_true_count() -> None:
    conditions = [{"type": f"c{i}"} for i in range(40)]
    connector = _RecordingConnector(
        status_info={
            "config_status": "CONFIGURING",
            "kubernetes_status": "WARNING",
            "conditions": conditions,
        }
    )
    result = await supervisor_status_composite(
        operator=_operator(),
        target=object(),
        params={"cluster": "domain-c8", "messages_limit": 5},
        connector=connector,  # type: ignore[arg-type]
    )
    assert len(result["conditions"]) == 5
    assert result["condition_count"] == 40


async def test_status_handles_absent_status_fields() -> None:
    connector = _RecordingConnector(status_info={})
    result = await supervisor_status_composite(
        operator=_operator(),
        target=object(),
        params={"cluster": "domain-c8"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["config_status"] is None
    assert result["kubernetes_status"] is None
    assert result["ready"] is False


# ---------------------------------------------------------------------------
# parameter-schema conformance
# ---------------------------------------------------------------------------


def test_enable_schema_accepts_valid_and_rejects_unknown_key() -> None:
    validator = Draft202012Validator(SUPERVISOR_ENABLE_PARAMETER_SCHEMA)
    validator.validate(_valid_enable_params())
    bad = _valid_enable_params()
    bad["network_provider"] = "VSPHERE_NETWORK"  # deprecated flat-spec key not on the surface
    assert list(validator.iter_errors(bad))


def test_enable_schema_requires_control_plane_and_workloads() -> None:
    validator = Draft202012Validator(SUPERVISOR_ENABLE_PARAMETER_SCHEMA)
    missing = {"cluster": "domain-c8", "name": "x"}
    assert list(validator.iter_errors(missing))
    no_provider = _valid_enable_params()
    del no_provider["workloads"]["edge"]["provider"]
    assert list(validator.iter_errors(no_provider))


def test_disable_schema_requires_cluster_only() -> None:
    validator = Draft202012Validator(SUPERVISOR_DISABLE_PARAMETER_SCHEMA)
    validator.validate({"cluster": "domain-c8"})
    assert list(validator.iter_errors({"cluster": "domain-c8", "extra": 1}))
    assert list(validator.iter_errors({}))


def test_status_schema_accepts_optional_limit() -> None:
    validator = Draft202012Validator(SUPERVISOR_STATUS_PARAMETER_SCHEMA)
    validator.validate({"cluster": "domain-c8"})
    validator.validate({"cluster": "domain-c8", "messages_limit": 10})
    assert list(validator.iter_errors({"cluster": "domain-c8", "messages_limit": -1}))


def test_governed_subop_manifest_references_supervisor_writes() -> None:
    assert _supervisor._SUB_OPS_SUPERVISOR_ENABLE == (_ENABLE_OP_ID,)
    assert _supervisor._SUB_OPS_SUPERVISOR_DISABLE == (_DISABLE_OP_ID,)
