# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handler + schema tests for the vSphere Namespace composites (#3502).

Covers the two ``vmware.composite.namespace.*`` composites in
:mod:`~meho_backplane.connectors.vmware_rest.composites._namespace`:
``create`` (caution, approval-gated) and ``delete`` (destructive,
approval-gated). Two concerns:

* **Handler behaviour** with a stubbed gate (``_GateRecorder``) and a
  recording connector double: the create happy path (the v2 create spec flows
  to the POST body, optional sub-objects included / omitted, read-back surfaces
  config_status), the parked-gate short-circuit, and the delete read-back-verify
  matrix (GET 404 -> ``deleted``; ``REMOVING`` -> ``removing``; a non-REMOVING
  status -> ``still_present``).
* **Parameter-schema conformance**: the registered JSON Schema accepts a
  well-formed create/delete payload and rejects unknown top-level keys + missing
  required fields, so a malformed ``params`` is caught by the dispatcher's
  Draft202012Validator before the handler runs.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites import _namespace, _write, _write_preview
from meho_backplane.connectors.vmware_rest.composites._namespace import (
    namespace_create_composite,
    namespace_delete_composite,
    namespace_status_composite,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    NAMESPACE_CREATE_PARAMETER_SCHEMA,
    NAMESPACE_DELETE_PARAMETER_SCHEMA,
    NAMESPACE_STATUS_PARAMETER_SCHEMA,
    NAMESPACE_STATUS_RESPONSE_SCHEMA,
)
from meho_backplane.operations._preview import PreviewContext, blast_radius_missing_reason

_CREATE_OP_ID = "POST:/vcenter/namespaces/instances/v2"
_DELETE_OP_ID = "DELETE:/vcenter/namespaces/instances/{namespace}"


def _operator() -> Operator:
    return Operator(
        sub="agent-namespace-composite",
        name="Namespace Composite Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=uuid.UUID("00000000-0000-0000-0000-0000000035a2"),
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

    The namespace writes route through ``_write._write_sub_op``, which resolves
    ``enforce_subop_policy`` in the ``_write`` namespace.
    """
    recorder = _GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


class _RecordingConnector:
    """Recording connector double: records writes, serves / 404s the read-back."""

    def __init__(self, *, get_info: dict[str, Any] | None = None, get_404: bool = False) -> None:
        self._get_info = get_info if get_info is not None else {}
        self._get_404 = get_404
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
        if self._get_404:
            request = httpx.Request("GET", f"https://vcenter.test{path}")
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("not found", request=request, response=response)
        return self._get_info

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
        return None


def _valid_create_params() -> dict[str, Any]:
    return {
        "supervisor": "domain-c8",
        "namespace": "envision-ns",
        "access_list": [
            {
                "subject_type": "USER",
                "subject": "administrator",
                "domain": "vsphere.local",
                "role": "EDIT",
            }
        ],
        "storage_specs": [{"policy": "nfs-policy-1"}],
        "vm_service_spec": {
            "content_libraries": ["lib-tkr-1"],
            "vm_classes": ["best-effort-small", "best-effort-medium"],
        },
    }


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


async def test_create_happy_path_dispatches_spec_body(gate: _GateRecorder) -> None:
    connector = _RecordingConnector(get_info={"config_status": "CONFIGURING"})
    result = await namespace_create_composite(
        operator=_operator(),
        target=object(),
        params=_valid_create_params(),
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == "created"
    assert result["namespace"] == "envision-ns"
    assert result["supervisor"] == "domain-c8"
    assert result["config_status"] == "CONFIGURING"
    # Gate ran against the canonical create op_id with the sub-op posture.
    assert gate.gated_op_ids == [_CREATE_OP_ID]
    assert gate.calls[0]["safety_level"] == "dangerous"
    # Exactly one write reached the wire: the v2 create POST with the full spec.
    assert len(connector.writes) == 1
    write = connector.writes[0]
    assert write["path"] == "/api/vcenter/namespaces/instances/v2"
    assert write["verb"] == "POST"
    assert set(write["json"]) == {
        "supervisor",
        "namespace",
        "access_list",
        "storage_specs",
        "vm_service_spec",
    }
    assert write["json"]["storage_specs"] == [{"policy": "nfs-policy-1"}]
    assert write["json"]["vm_service_spec"]["content_libraries"] == ["lib-tkr-1"]
    # Read-back GET by name happened.
    assert connector.gets == ["/api/vcenter/namespaces/instances/envision-ns"]


async def test_create_omits_optional_fields_when_absent(gate: _GateRecorder) -> None:
    connector = _RecordingConnector(get_info={"config_status": "RUNNING"})
    await namespace_create_composite(
        operator=_operator(),
        target=object(),
        params={"supervisor": "domain-c8", "namespace": "bare-ns"},
        connector=connector,  # type: ignore[arg-type]
    )
    body = connector.writes[0]["json"]
    assert set(body) == {"supervisor", "namespace"}


async def test_create_read_back_404_leaves_config_status_none(gate: _GateRecorder) -> None:
    # The namespace is not yet visible on the immediate read-back (async create).
    connector = _RecordingConnector(get_404=True)
    result = await namespace_create_composite(
        operator=_operator(),
        target=object(),
        params=_valid_create_params(),
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == "created"
    assert result["config_status"] is None


async def test_create_parked_gate_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    parked = OperationResult(
        status="awaiting_approval", op_id="vmware.composite.namespace.create", duration_ms=0
    )
    recorder = _GateRecorder(gate_for={_CREATE_OP_ID: parked})
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    connector = _RecordingConnector()
    result = await namespace_create_composite(
        operator=_operator(),
        target=object(),
        params=_valid_create_params(),
        connector=connector,  # type: ignore[arg-type]
    )
    assert result is parked
    assert connector.writes == []
    # No read-back after a parked create.
    assert connector.gets == []


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


async def test_delete_happy_path_verifies_absence(gate: _GateRecorder) -> None:
    connector = _RecordingConnector(get_404=True)
    result = await namespace_delete_composite(
        operator=_operator(),
        target=object(),
        params={"namespace": "envision-ns"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == "deleted"
    assert result["namespace"] == "envision-ns"
    assert gate.gated_op_ids == [_DELETE_OP_ID]
    assert gate.calls[0]["safety_level"] == "dangerous"
    assert len(connector.writes) == 1
    write = connector.writes[0]
    assert write["path"] == "/api/vcenter/namespaces/instances/envision-ns"
    assert write["verb"] == "DELETE"
    # No request body on delete.
    assert write["json"] is None
    # Read-back GET by name happened (and 404'd -> absent).
    assert connector.gets == ["/api/vcenter/namespaces/instances/envision-ns"]


async def test_delete_removing_when_read_back_reports_removing(gate: _GateRecorder) -> None:
    connector = _RecordingConnector(get_info={"config_status": "REMOVING"})
    result = await namespace_delete_composite(
        operator=_operator(),
        target=object(),
        params={"namespace": "envision-ns"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == "removing"
    assert result["config_status"] == "REMOVING"


async def test_delete_still_present_when_read_back_reports_running(gate: _GateRecorder) -> None:
    connector = _RecordingConnector(get_info={"config_status": "RUNNING"})
    result = await namespace_delete_composite(
        operator=_operator(),
        target=object(),
        params={"namespace": "envision-ns"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["status"] == "still_present"
    assert result["config_status"] == "RUNNING"


async def test_delete_parked_gate_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    parked = OperationResult(
        status="awaiting_approval", op_id="vmware.composite.namespace.delete", duration_ms=0
    )
    recorder = _GateRecorder(gate_for={_DELETE_OP_ID: parked})
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    connector = _RecordingConnector()
    result = await namespace_delete_composite(
        operator=_operator(),
        target=object(),
        params={"namespace": "envision-ns"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result is parked
    assert connector.writes == []
    assert connector.gets == []


# ---------------------------------------------------------------------------
# status (boot-enabled read composite)
# ---------------------------------------------------------------------------


async def test_status_running_is_ready_and_projects_fields() -> None:
    connector = _RecordingConnector(
        get_info={
            "config_status": "RUNNING",
            "stats": {"cpu_used": 1000, "memory_used": 2048, "storage_used": 4096},
            "description": "envision guest-cluster namespace",
            "messages": [{"severity": "INFO", "details": {"default_message": "ok"}}],
        }
    )
    result = await namespace_status_composite(
        operator=_operator(),
        target=object(),
        params={"namespace": "envision-ns"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["namespace"] == "envision-ns"
    assert result["exists"] is True
    assert result["config_status"] == "RUNNING"
    assert result["ready"] is True
    assert result["stats"] == {"cpu_used": 1000, "memory_used": 2048, "storage_used": 4096}
    assert result["description"] == "envision guest-cluster namespace"
    assert result["message_count"] == 1
    assert len(result["messages"]) == 1
    # A read never routes through the write gate.
    assert connector.writes == []
    assert connector.gets == ["/api/vcenter/namespaces/instances/envision-ns"]
    # Response is schema-valid.
    Draft202012Validator(NAMESPACE_STATUS_RESPONSE_SCHEMA).validate(result)


async def test_status_configuring_is_not_ready() -> None:
    connector = _RecordingConnector(get_info={"config_status": "CONFIGURING", "messages": []})
    result = await namespace_status_composite(
        operator=_operator(),
        target=object(),
        params={"namespace": "envision-ns"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["exists"] is True
    assert result["config_status"] == "CONFIGURING"
    assert result["ready"] is False


async def test_status_absent_when_read_404s() -> None:
    connector = _RecordingConnector(get_404=True)
    result = await namespace_status_composite(
        operator=_operator(),
        target=object(),
        params={"namespace": "gone-ns"},
        connector=connector,  # type: ignore[arg-type]
    )
    assert result["exists"] is False
    assert result["ready"] is False
    assert result["config_status"] is None
    assert result["stats"] is None
    assert result["messages"] == []
    assert result["message_count"] == 0
    Draft202012Validator(NAMESPACE_STATUS_RESPONSE_SCHEMA).validate(result)


async def test_status_caps_messages_inline() -> None:
    connector = _RecordingConnector(
        get_info={
            "config_status": "CONFIGURING",
            "messages": [{"severity": "INFO", "details": {"i": i}} for i in range(40)],
        }
    )
    result = await namespace_status_composite(
        operator=_operator(),
        target=object(),
        params={"namespace": "envision-ns", "messages_limit": 5},
        connector=connector,  # type: ignore[arg-type]
    )
    assert len(result["messages"]) == 5
    assert result["message_count"] == 40


# ---------------------------------------------------------------------------
# parameter-schema conformance
# ---------------------------------------------------------------------------


def test_create_schema_accepts_valid_and_rejects_unknown_key() -> None:
    validator = Draft202012Validator(NAMESPACE_CREATE_PARAMETER_SCHEMA)
    validator.validate(_valid_create_params())
    # A bare supervisor + namespace is also valid (optional spec sub-objects).
    validator.validate({"supervisor": "domain-c8", "namespace": "ns"})
    bad = _valid_create_params()
    bad["cluster"] = "domain-c8"  # v1 field name is not on the v2 surface
    assert list(validator.iter_errors(bad))


def test_create_schema_requires_supervisor_and_namespace() -> None:
    validator = Draft202012Validator(NAMESPACE_CREATE_PARAMETER_SCHEMA)
    assert list(validator.iter_errors({"namespace": "ns"}))
    assert list(validator.iter_errors({"supervisor": "domain-c8"}))
    assert list(validator.iter_errors({}))


def test_delete_schema_requires_namespace_only() -> None:
    validator = Draft202012Validator(NAMESPACE_DELETE_PARAMETER_SCHEMA)
    validator.validate({"namespace": "ns"})
    assert list(validator.iter_errors({"namespace": "ns", "extra": 1}))
    assert list(validator.iter_errors({}))


def test_status_schema_accepts_namespace_and_optional_limit() -> None:
    validator = Draft202012Validator(NAMESPACE_STATUS_PARAMETER_SCHEMA)
    validator.validate({"namespace": "ns"})
    validator.validate({"namespace": "ns", "messages_limit": 10})
    assert list(validator.iter_errors({"namespace": "ns", "extra": 1}))
    assert list(validator.iter_errors({}))


def test_governed_subop_manifest_references_namespace_writes() -> None:
    assert _namespace._SUB_OPS_NAMESPACE_CREATE == (_CREATE_OP_ID,)
    assert _namespace._SUB_OPS_NAMESPACE_DELETE == (_DELETE_OP_ID,)


# ---------------------------------------------------------------------------
# park-time preview builders
# ---------------------------------------------------------------------------


def _preview_ctx(params: dict[str, Any], *, connector_instance: Any = None) -> PreviewContext:
    return PreviewContext(
        descriptor=object(),  # type: ignore[arg-type]  # the builders ignore it
        connector_instance=connector_instance,
        operator=_operator(),
        target=object(),
        params=params,
    )


async def test_create_preview_echoes_spec_shape() -> None:
    preview = await _write_preview._namespace_create_preview(_preview_ctx(_valid_create_params()))
    assert preview == {
        "preview": {
            "action": "create_vsphere_namespace",
            "supervisor": "domain-c8",
            "namespace": "envision-ns",
            "storage_spec_count": 1,
            "access_list_count": 1,
            "content_libraries": ["lib-tkr-1"],
            "vm_classes": ["best-effort-small", "best-effort-medium"],
        }
    }


async def test_create_preview_declines_on_malformed_params() -> None:
    assert await _write_preview._namespace_create_preview(_preview_ctx({"namespace": "ns"})) is None


async def test_delete_preview_populates_well_formed_blast_radius() -> None:
    connector = _RecordingConnector(
        get_info={"config_status": "RUNNING", "supervisor": "domain-c8"}
    )
    preview = await _write_preview._namespace_delete_preview(
        _preview_ctx({"namespace": "envision-ns"}, connector_instance=connector)
    )
    assert preview is not None
    # The destructive park gate is satisfied (object truthy, children list,
    # irreversibility non-empty).
    assert blast_radius_missing_reason(preview) is None
    block = preview["blast_radius"]
    assert block["object"] == {
        "kind": "namespace",
        "name": "envision-ns",
        "config_status": "RUNNING",
        "supervisor": "domain-c8",
    }
    assert block["children"] == []
    assert block["irreversibility"]


async def test_delete_preview_degrades_to_name_only_on_404() -> None:
    # A 404 read-back (namespace already gone / not readable) still yields a
    # well-formed blast_radius so the destructive park is never refused.
    connector = _RecordingConnector(get_404=True)
    preview = await _write_preview._namespace_delete_preview(
        _preview_ctx({"namespace": "envision-ns"}, connector_instance=connector)
    )
    assert preview is not None
    assert blast_radius_missing_reason(preview) is None
    assert preview["blast_radius"]["object"] == {"kind": "namespace", "name": "envision-ns"}


async def test_delete_preview_declines_without_namespace() -> None:
    assert await _write_preview._namespace_delete_preview(_preview_ctx({})) is None
