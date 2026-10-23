# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the vSphere licence operations.

* ``vmware.license.list`` and ``vmware.license.assigned.list`` -- typed reads.
* ``vmware.composite.license.assign`` -- the write; it waits until a second
  person approves.

A licence key is a secret. Each test that feeds vendor data checks that no
full key reaches the result, a log line, the gate params, the preview, a
raised error, an audit row, an approval row or a flight-recorder span.

Every vCenter answer is a hand-written fake in the VI-JSON shape of the pinned
vendor files. The keys are made up (they start with ``FAKE``); the host and
vCenter names use the reserved ``.invalid`` domain.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors._shared.vcf_auth import SessionLoginError
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.connectors.vmware_rest import (
    VmwareRestConnector,
    register_vmware_typed_operations,
    register_vmware_typed_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.composites import (
    _write,
    register_vmware_composite_operations,
    register_vmware_composite_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.composites._license import (
    LICENSE_ASSIGN_PARAMETER_SCHEMA,
    LICENSE_ASSIGN_RESPONSE_SCHEMA,
    license_assign_composite,
    license_assign_preview,
)
from meho_backplane.connectors.vmware_rest.license_common import (
    LICENSE_GROUP_KEY,
    LICENSE_WHEN_TO_USE,
    assignment_row,
    hide_license_keys,
    license_row,
    mask_license_key,
    query_assigned_licenses,
)
from meho_backplane.connectors.vmware_rest.typed_ops_license import (
    VMWARE_LICENSE_ASSIGNED_LIST_OP,
    VMWARE_LICENSE_LIST_OP,
    license_assigned_list_impl,
    license_list_impl,
)
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor, OperationGroup
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.flight_recorder import capture
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.approval_queue import approve_request
from meho_backplane.redaction.flight_recorder import classify_body_exclusion
from meho_backplane.settings import get_settings
from tests.test_connectors_vmware_rest_composites_write_e2e import (
    _FakeVmwareTarget,
    _seed_connector,
)
from tests.test_connectors_vmware_rest_composites_write_e2e import (
    _make_operator as _make_e2e_operator,
)
from tests.test_connectors_vmware_rest_vmomi_mount import (
    _BASE,
    _make_connector,
    _patch_no_revoke_aclose,
    _StubTarget,
)

_ASSIGN_OP = "vmware.composite.license.assign"
_LIST_OP = "vmware.license.list"
_ASSIGNED_OP = "vmware.license.assigned.list"

# Made-up keys. The evaluation key is the fixed all-zero key vSphere shows.
_KEY_ENT = "FAKE1-FAKE2-FAKE3-FAKE4-AB12C"
_KEY_ENT_2 = "FAKE5-FAKE6-FAKE7-FAKE8-CD34E"
_KEY_VC = "FAKE9-FAKEA-FAKEB-FAKEC-EF56G"
_KEY_EVAL = "00000-00000-00000-00000-00000"
_SECRET_KEYS = (_KEY_ENT, _KEY_ENT_2, _KEY_VC)
_VCENTER_UUID = "00000000-0000-4000-8000-0000000000aa"
_LAM_MOID = "LicenseAssignmentManager"


# ---------------------------------------------------------------------------
# VI-JSON fixtures
# ---------------------------------------------------------------------------


def _boxed(type_name: str, value: Any) -> dict[str, Any]:
    return {"_typeName": type_name, "_value": value}


def _kv(key: str, value: Any) -> dict[str, Any]:
    return {"_typeName": "KeyAnyValue", "key": key, "value": value}


def _license(
    key: str,
    name: str,
    edition: str,
    *,
    total: int,
    used: int,
    unit: str,
    product: str = "VMware ESX Server",
    expires: str | None = None,
) -> dict[str, Any]:
    """A ``LicenseManagerLicenseInfo`` as VI-JSON sends it."""
    properties = [
        _kv("ProductName", _boxed("string", product)),
        _kv("ProductVersion", _boxed("string", "8.0")),
        _kv("feature", {"_typeName": "KeyValue", "key": "vmotion", "value": "vMotion"}),
    ]
    if expires is not None:
        properties.append(_kv("expirationDate", _boxed("dateTime", expires)))
    return {
        "_typeName": "LicenseManagerLicenseInfo",
        "licenseKey": key,
        "editionKey": edition,
        "name": name,
        "total": total,
        "used": used,
        "costUnit": unit,
        "properties": properties,
    }


_EVAL = _license(
    _KEY_EVAL, "Evaluation Mode", "eval", total=0, used=2, unit="", expires="2026-09-30T08:00:00Z"
)
_ENT = {
    **_license(
        _KEY_ENT,
        "vSphere 8 Enterprise Plus",
        "esx.enterprisePlus.cpuPackageCoreLimited",
        total=32,
        used=8,
        unit="cpuPackage:32core",
    ),
    "labels": [{"_typeName": "KeyValue", "key": "owner", "value": "<team>"}],
}
_ENT_2 = _license(
    _KEY_ENT_2,
    "vSphere 8 Enterprise Plus",
    "esx.enterprisePlus.cpuPackageCoreLimited",
    total=16,
    used=0,
    unit="cpuPackage:32core",
)
_VC = _license(
    _KEY_VC,
    "vCenter Server 8 Standard",
    "vc.standard.instance",
    total=1,
    used=1,
    unit="server",
    product="VMware VirtualCenter Server",
)


def _assignment(
    entity_id: str,
    name: str,
    info: dict[str, Any],
    *,
    product: str = "VMware ESX Server",
    evaluation_expires: str | None = None,
) -> dict[str, Any]:
    """A ``LicenseAssignmentManagerLicenseAssignment`` as VI-JSON sends it."""
    properties = [
        _kv("ProductName", _boxed("string", product)),
        _kv("ProductVersion", _boxed("string", "8.0.3")),
    ]
    if evaluation_expires is not None:
        evaluation = {
            "_typeName": "LicenseManagerEvaluationInfo",
            "properties": [_kv("expirationDate", _boxed("dateTime", evaluation_expires))],
        }
        properties.append(_kv("Evaluation", evaluation))
    return {
        "_typeName": "LicenseAssignmentManagerLicenseAssignment",
        "entityId": entity_id,
        "entityDisplayName": name,
        "assignedLicense": copy.deepcopy(info),
        "properties": properties,
    }


def _assignments() -> list[dict[str, Any]]:
    return [
        _assignment("host-12", "esx-01.example.invalid", _EVAL),
        _assignment("host-13", "esx-02.example.invalid", _ENT),
        _assignment(
            _VCENTER_UUID,
            "vcenter.example.invalid",
            _VC,
            product="VMware VirtualCenter Server",
        ),
    ]


class _FakeVcenter(VmwareRestConnector):
    """A vCenter's licence state behind the connector's VI-JSON seam.

    A real :class:`VmwareRestConnector` subclass, so the dispatcher binds the
    typed handlers to it. Only :meth:`_post_vmomi_json` is replaced; it serves
    the property read, ``QueryAssignedLicenses`` and ``UpdateAssignedLicense``
    from memory and records every call.
    """

    def __init__(
        self,
        *,
        licenses: list[dict[str, Any]] | None = None,
        assignments: list[dict[str, Any]] | None = None,
        assignment_manager: bool = True,
        hidden_licenses_fault: str | None = None,
        update_error: BaseException | None = None,
    ) -> None:
        super().__init__(session_loader=AsyncMock())
        self.licenses = copy.deepcopy(licenses if licenses is not None else [_EVAL, _ENT, _VC])
        self.assignments = {
            row["entityId"]: row for row in copy.deepcopy(assignments or _assignments())
        }
        self.assignment_manager = assignment_manager
        self.hidden_licenses_fault = hidden_licenses_fault
        self.update_error = update_error
        self.vmomi_calls: list[tuple[str, Any]] = []

    async def _post_vmomi_json(  # type: ignore[override]
        self, target: Any, vmomi_path: str, *, operator: Any, json: Any = None, **_: Any
    ) -> Any:
        self.vmomi_calls.append((vmomi_path, copy.deepcopy(json)))
        if vmomi_path.endswith("/RetrievePropertiesEx"):
            return self._license_manager_properties(json)
        if vmomi_path.endswith("/QueryAssignedLicenses"):
            wanted = (json or {}).get("entityId")
            return [
                copy.deepcopy(row)
                for entity_id, row in self.assignments.items()
                if wanted in (None, entity_id)
            ]
        if vmomi_path.endswith("/UpdateAssignedLicense"):
            if self.update_error is not None:
                raise self.update_error
            info = next(lic for lic in self.licenses if lic["licenseKey"] == json["licenseKey"])
            self.assignments[json["entity"]]["assignedLicense"] = copy.deepcopy(info)
            return copy.deepcopy(info)
        raise AssertionError(f"unexpected VI-JSON call {vmomi_path!r}")

    def _license_manager_properties(self, body: dict[str, Any]) -> dict[str, Any]:
        prop_spec = body["specSet"][0]["propSet"][0]
        assert prop_spec["type"] == "LicenseManager"
        assert body["specSet"][0]["objectSet"][0]["obj"]["value"] == "LicenseManager"
        assert set(prop_spec["pathSet"]) == {"licenses", "licenseAssignmentManager"}
        prop_set: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        if self.hidden_licenses_fault is None:
            prop_set.append(
                {
                    "_typeName": "DynamicProperty",
                    "name": "licenses",
                    "val": _boxed("ArrayOfLicenseManagerLicenseInfo", copy.deepcopy(self.licenses)),
                }
            )
        else:
            missing.append(
                {
                    "_typeName": "MissingProperty",
                    "path": "licenses",
                    "fault": {
                        "_typeName": "LocalizedMethodFault",
                        "fault": {"_typeName": self.hidden_licenses_fault},
                    },
                }
            )
        if self.assignment_manager:
            prop_set.append(
                {
                    "_typeName": "DynamicProperty",
                    "name": "licenseAssignmentManager",
                    "val": {
                        "_typeName": "ManagedObjectReference",
                        "type": "LicenseAssignmentManager",
                        "value": _LAM_MOID,
                    },
                }
            )
        content: dict[str, Any] = {
            "_typeName": "ObjectContent",
            "obj": {
                "_typeName": "ManagedObjectReference",
                "type": "LicenseManager",
                "value": "LicenseManager",
            },
            "propSet": prop_set,
        }
        if missing:
            content["missingSet"] = missing
        return {"_typeName": "RetrieveResult", "objects": [content]}

    def calls_to(self, method: str) -> list[Any]:
        return [body for path, body in self.vmomi_calls if path.endswith(f"/{method}")]


def _operator() -> Operator:
    return Operator(
        sub="op-license-test",
        name="Licence Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a1c0"),
        tenant_role=TenantRole.OPERATOR,
    )


_TARGET = SimpleNamespace(name="vc-test")


def _assert_no_full_key(value: Any) -> None:
    """No made-up secret key appears anywhere in *value* (rendered as text)."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    for key in _SECRET_KEYS:
        assert key not in text, f"full licence key {key[-5:]!r} leaked"
        assert key.lower() not in text.lower(), f"full licence key {key[-5:]!r} leaked"


class _GateRecorder:
    """Stands in for ``enforce_subop_policy``: records the call, returns a verdict."""

    def __init__(self, verdict: OperationResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.verdict = verdict

    async def __call__(self, **kwargs: Any) -> OperationResult | None:
        self.calls.append(kwargs)
        return self.verdict


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _GateRecorder:
    recorder = _GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


async def _assign(conn: _FakeVcenter, **params: Any) -> Any:
    return await license_assign_composite(
        operator=_operator(),
        target=_TARGET,
        params=params,
        connector=conn,
    )


# ---------------------------------------------------------------------------
# The key mask
# ---------------------------------------------------------------------------


def test_mask_keeps_the_last_five_characters_and_the_dashes() -> None:
    assert mask_license_key(_KEY_ENT) == "*****-*****-*****-*****-AB12C"
    assert mask_license_key(_KEY_EVAL) == "*****-*****-*****-*****-00000"


def test_mask_hides_a_short_value_in_full_and_ignores_non_strings() -> None:
    assert mask_license_key("AB-12-CD") == "********"
    assert mask_license_key("") is None
    assert mask_license_key(None) is None
    assert mask_license_key(12345) is None


def test_hide_license_keys_masks_the_given_key_and_any_key_shaped_value() -> None:
    text = (
        f"InvalidLicense: key {_KEY_ENT.lower()} refused; also tried {_KEY_ENT_2} "
        "for host-12 at 2026-10-01"
    )
    hidden = hide_license_keys(text, _KEY_ENT)
    _assert_no_full_key(hidden)
    assert "*****-*****-*****-*****-ab12c" in hidden
    assert "*****-*****-*****-*****-CD34E" in hidden
    assert "host-12 at 2026-10-01" in hidden


# ---------------------------------------------------------------------------
# Row shapes
# ---------------------------------------------------------------------------


def test_license_row_reads_the_vi_json_licence_info() -> None:
    row = license_row(unwrap_vim_value(_ENT))
    assert row == {
        "name": "vSphere 8 Enterprise Plus",
        "edition_key": "esx.enterprisePlus.cpuPackageCoreLimited",
        "product_name": "VMware ESX Server",
        "product_version": "8.0",
        "key_masked": "*****-*****-*****-*****-AB12C",
        "is_evaluation": False,
        "total": 32,
        "used": 8,
        "cost_unit": "cpuPackage:32core",
        "expires_at": None,
        "labels": [{"key": "owner", "value": "<team>"}],
    }
    eval_row = license_row(unwrap_vim_value(_EVAL))
    assert eval_row["is_evaluation"] is True
    assert eval_row["expires_at"] == "2026-09-30T08:00:00Z"
    assert eval_row["cost_unit"] is None


def test_assignment_row_takes_the_end_date_from_the_evaluation_info() -> None:
    no_date = {**_EVAL, "properties": []}
    raw = _assignment("host-12", "esx-01.example.invalid", no_date, evaluation_expires="2026-09-01")
    row = assignment_row(unwrap_vim_value(raw))
    assert row["entity_id"] == "host-12"
    assert row["entity_name"] == "esx-01.example.invalid"
    assert row["product_name"] == "VMware ESX Server"
    assert row["license_name"] == "Evaluation Mode"
    assert row["is_evaluation"] is True
    assert row["expires_at"] == "2026-09-01"


# ---------------------------------------------------------------------------
# vmware.license.list
# ---------------------------------------------------------------------------


async def test_license_list_returns_masked_rows() -> None:
    conn = _FakeVcenter()
    with capture_logs() as logs:
        out = await license_list_impl(conn, _operator(), _TARGET, {})  # type: ignore[arg-type]
    assert [row["name"] for row in out["licenses"]] == [
        "Evaluation Mode",
        "vSphere 8 Enterprise Plus",
        "vCenter Server 8 Standard",
    ]
    assert out["licenses"][1]["key_masked"] == "*****-*****-*****-*****-AB12C"
    Draft202012Validator(VMWARE_LICENSE_LIST_OP.response_schema).validate(out)
    _assert_no_full_key(out)
    _assert_no_full_key(repr(logs))
    # One property read, nothing else.
    assert [path for path, _ in conn.vmomi_calls] == [
        "/PropertyCollector/propertyCollector/RetrievePropertiesEx"
    ]


async def test_license_list_names_the_fault_when_vcenter_hides_the_licences() -> None:
    conn = _FakeVcenter(hidden_licenses_fault="NoPermission")
    with pytest.raises(RuntimeError, match="NoPermission"):
        await license_list_impl(conn, _operator(), _TARGET, {})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# vmware.license.assigned.list
# ---------------------------------------------------------------------------


async def test_license_assigned_list_reads_every_asset() -> None:
    conn = _FakeVcenter()
    out = await license_assigned_list_impl(conn, _operator(), _TARGET, {})  # type: ignore[arg-type]
    by_id = {row["entity_id"]: row for row in out["assignments"]}
    assert set(by_id) == {"host-12", "host-13", _VCENTER_UUID}
    assert by_id["host-12"]["is_evaluation"] is True
    assert by_id["host-12"]["expires_at"] == "2026-09-30T08:00:00Z"
    assert by_id["host-13"]["key_masked"] == "*****-*****-*****-*****-AB12C"
    assert by_id[_VCENTER_UUID]["product_name"] == "VMware VirtualCenter Server"
    Draft202012Validator(VMWARE_LICENSE_ASSIGNED_LIST_OP.response_schema).validate(out)
    _assert_no_full_key(out)
    # The assignment manager comes from the LicenseManager property.
    assert conn.vmomi_calls[1] == (
        f"/LicenseAssignmentManager/{_LAM_MOID}/QueryAssignedLicenses",
        {},
    )


async def test_license_assigned_list_for_one_asset() -> None:
    conn = _FakeVcenter()
    out = await license_assigned_list_impl(
        conn,  # type: ignore[arg-type]
        _operator(),
        _TARGET,  # type: ignore[arg-type]
        {"entity_id": "host-12"},
    )
    assert [row["entity_id"] for row in out["assignments"]] == ["host-12"]
    assert conn.calls_to("QueryAssignedLicenses") == [{"entityId": "host-12"}]


async def test_license_assigned_list_needs_a_licence_assignment_manager() -> None:
    conn = _FakeVcenter(assignment_manager=False)
    with pytest.raises(RuntimeError, match="no LicenseAssignmentManager"):
        await license_assigned_list_impl(conn, _operator(), _TARGET, {})  # type: ignore[arg-type]


@pytest.mark.parametrize("answer", [None, {}, []])
async def test_query_assigned_licenses_reads_an_empty_answer_as_no_rows(answer: Any) -> None:
    conn = SimpleNamespace(_post_vmomi_json=AsyncMock(return_value=answer))
    rows = await query_assigned_licenses(
        conn,  # type: ignore[arg-type]
        _TARGET,
        _operator(),
        assignment_manager=_LAM_MOID,
    )
    assert rows == []


# ---------------------------------------------------------------------------
# vmware.composite.license.assign
# ---------------------------------------------------------------------------


async def test_assign_sends_the_full_key_only_in_the_vendor_request(gate: _GateRecorder) -> None:
    conn = _FakeVcenter()
    with capture_logs() as logs:
        out = await _assign(conn, entity_id="host-12", license_name="vsphere 8 enterprise plus")
    assert out["status"] == "assigned"
    assert out["entity_name"] == "esx-01.example.invalid"
    assert out["license"]["key_masked"] == "*****-*****-*****-*****-AB12C"
    assert out["before"] == {
        "license_name": "Evaluation Mode",
        "key_masked": "*****-*****-*****-*****-00000",
        "is_evaluation": True,
        "expires_at": "2026-09-30T08:00:00Z",
    }
    Draft202012Validator(LICENSE_ASSIGN_RESPONSE_SCHEMA).validate(out)
    # The key goes to vCenter, in the request body only.
    assert conn.calls_to("UpdateAssignedLicense") == [
        {"entity": "host-12", "licenseKey": _KEY_ENT, "entityDisplayName": "esx-01.example.invalid"}
    ]
    assert conn.assignments["host-12"]["assignedLicense"]["licenseKey"] == _KEY_ENT
    # The gate (and so any approval request) sees the masked key only.
    (gate_call,) = gate.calls
    assert gate_call["op_id"] == "POST:/LicenseAssignmentManager/{moId}/UpdateAssignedLicense"
    assert gate_call["params"] == {
        "entity_id": "host-12",
        "license_name": "vSphere 8 Enterprise Plus",
        "key_masked": "*****-*****-*****-*****-AB12C",
    }
    _assert_no_full_key(out)
    _assert_no_full_key(repr(gate.calls))
    _assert_no_full_key(repr(logs))


async def test_assign_by_key_suffix_alone(gate: _GateRecorder) -> None:
    conn = _FakeVcenter(licenses=[_EVAL, _ENT, _ENT_2, _VC])
    out = await _assign(conn, entity_id="host-12", key_suffix="cd34e")
    assert out["status"] == "assigned"
    assert conn.calls_to("UpdateAssignedLicense")[0]["licenseKey"] == _KEY_ENT_2


async def test_assign_refuses_an_unknown_licence_before_any_change(gate: _GateRecorder) -> None:
    conn = _FakeVcenter()
    out = await _assign(conn, entity_id="host-12", license_name="vSphere 7 Standard")
    assert out["status"] == "license_not_found"
    assert "vmware.license.list" in out["guidance"]
    assert conn.calls_to("UpdateAssignedLicense") == []
    assert conn.calls_to("QueryAssignedLicenses") == []
    assert gate.calls == []


async def test_assign_refuses_an_ambiguous_name_and_lists_masked_candidates(
    gate: _GateRecorder,
) -> None:
    conn = _FakeVcenter(licenses=[_EVAL, _ENT, _ENT_2, _VC])
    out = await _assign(conn, entity_id="host-12", license_name="vSphere 8 Enterprise Plus")
    assert out["status"] == "license_ambiguous"
    assert [row["key_masked"] for row in out["candidates"]] == [
        "*****-*****-*****-*****-AB12C",
        "*****-*****-*****-*****-CD34E",
    ]
    assert "key_suffix" in out["guidance"]
    assert conn.calls_to("UpdateAssignedLicense") == []
    _assert_no_full_key(out)
    Draft202012Validator(LICENSE_ASSIGN_RESPONSE_SCHEMA).validate(out)

    # The name plus the suffix picks one.
    picked = await _assign(
        conn, entity_id="host-12", license_name="vSphere 8 Enterprise Plus", key_suffix="AB12C"
    )
    assert picked["status"] == "assigned"
    assert conn.calls_to("UpdateAssignedLicense")[0]["licenseKey"] == _KEY_ENT


async def test_assign_refuses_an_unknown_asset(gate: _GateRecorder) -> None:
    conn = _FakeVcenter()
    out = await _assign(conn, entity_id="host-99", license_name="vSphere 8 Enterprise Plus")
    assert out["status"] == "asset_not_found"
    assert "vmware.license.assigned.list" in out["guidance"]
    assert conn.calls_to("UpdateAssignedLicense") == []


async def test_assign_is_unchanged_when_the_asset_has_the_licence(gate: _GateRecorder) -> None:
    conn = _FakeVcenter()
    out = await _assign(conn, entity_id="host-13", license_name="vSphere 8 Enterprise Plus")
    assert out["status"] == "unchanged"
    assert out["before"]["key_masked"] == "*****-*****-*****-*****-AB12C"
    assert conn.calls_to("UpdateAssignedLicense") == []
    assert gate.calls == []


async def test_assign_refuses_without_a_name_or_a_suffix(gate: _GateRecorder) -> None:
    conn = _FakeVcenter(licenses=[_ENT])
    out = await _assign(conn, entity_id="host-12")
    assert out["status"] == "license_not_found"
    assert conn.calls_to("UpdateAssignedLicense") == []


async def test_assign_returns_the_gate_and_sends_nothing_when_it_must_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parked = OperationResult(status="awaiting_approval", op_id="POST:/x", duration_ms=1.0)
    monkeypatch.setattr(_write, "enforce_subop_policy", _GateRecorder(parked))
    conn = _FakeVcenter()
    out = await _assign(conn, entity_id="host-12", license_name="vSphere 8 Enterprise Plus")
    assert out is parked
    assert conn.calls_to("UpdateAssignedLicense") == []


def _vi_json_fault(type_name: str, faultstring: str, **fields: Any) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://vc.test.invalid/sdk/vim25/8.0.3.0/x")
    body = {"_typeName": type_name, "faultstring": faultstring, **fields}
    response = httpx.Response(500, json=body, request=request)
    return httpx.HTTPStatusError("Server error '500'", request=request, response=response)


async def test_assign_vendor_fault_reaches_the_error_without_the_key(gate: _GateRecorder) -> None:
    fault = _vi_json_fault(
        "InvalidLicense",
        f"The license key {_KEY_ENT} is not valid for this host",
        licenseContent=_KEY_ENT,
    )
    conn = _FakeVcenter(update_error=fault)
    with pytest.raises(RuntimeError) as raised:
        await _assign(conn, entity_id="host-12", license_name="vSphere 8 Enterprise Plus")
    message = str(raised.value)
    assert "InvalidLicense" in message
    assert "is not valid for this host" in message
    assert "*****-*****-*****-*****-AB12C" in message
    _assert_no_full_key(message)
    # The vendor error, whose body holds the key, is not chained.
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


async def test_assign_single_host_fault_text_is_masked(gate: _GateRecorder) -> None:
    conn = _FakeVcenter(update_error=RuntimeError(f"InvalidLicense: {_KEY_ENT} refused"))
    with pytest.raises(RuntimeError) as raised:
        await _assign(conn, entity_id="host-12", license_name="vSphere 8 Enterprise Plus")
    _assert_no_full_key(str(raised.value))
    assert "AB12C" in str(raised.value)


async def test_assign_login_fault_passes_through(gate: _GateRecorder) -> None:
    login_fault = SessionLoginError("NotAuthenticated after a new login")
    conn = _FakeVcenter(update_error=login_fault)
    with pytest.raises(SessionLoginError) as raised:
        await _assign(conn, entity_id="host-12", license_name="vSphere 8 Enterprise Plus")
    assert raised.value is login_fault


# ---------------------------------------------------------------------------
# Park-time preview
# ---------------------------------------------------------------------------


def _preview_ctx(conn: Any, params: dict[str, Any]) -> PreviewContext:
    return PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=conn,
        operator=_operator(),
        target=_TARGET,
        params=params,
    )


async def test_preview_shows_the_licence_now_and_the_new_one_masked() -> None:
    conn = _FakeVcenter()
    preview = await license_assign_preview(
        _preview_ctx(conn, {"entity_id": "host-12", "license_name": "vSphere 8 Enterprise Plus"})
    )
    assert preview is not None
    assert preview["entity_name"] == "esx-01.example.invalid"
    assert preview["current"]["is_evaluation"] is True
    assert preview["requested"]["key_masked"] == "*****-*****-*****-*****-AB12C"
    assert preview["requested"]["used"] == 8
    assert preview["requested"]["total"] == 32
    assert preview["outcome"] == "assign"
    assert conn.calls_to("UpdateAssignedLicense") == []
    _assert_no_full_key(preview)


async def test_preview_names_the_status_the_call_would_stop_with() -> None:
    conn = _FakeVcenter(licenses=[_EVAL, _ENT, _ENT_2, _VC])
    preview = await license_assign_preview(
        _preview_ctx(conn, {"entity_id": "host-12", "license_name": "vSphere 8 Enterprise Plus"})
    )
    assert preview is not None
    assert preview["outcome"] == "license_ambiguous"
    assert preview["requested"] is None


async def test_preview_declines_without_a_connector() -> None:
    assert await license_assign_preview(_preview_ctx(None, {"entity_id": "host-12"})) is None


# ---------------------------------------------------------------------------
# Parameter schemas
# ---------------------------------------------------------------------------


def test_examples_validate_and_the_assign_schema_needs_a_name_or_a_suffix() -> None:
    for schema in (
        VMWARE_LICENSE_LIST_OP.parameter_schema,
        VMWARE_LICENSE_ASSIGNED_LIST_OP.parameter_schema,
        LICENSE_ASSIGN_PARAMETER_SCHEMA,
    ):
        for example in schema["examples"]:
            assert validate_params(schema, example) == [], example
    assign = LICENSE_ASSIGN_PARAMETER_SCHEMA
    assert validate_params(assign, {"entity_id": "host-12"}), "a name or a suffix is needed"
    assert validate_params(assign, {"entity_id": "host-12", "key_suffix": "AB"})
    assert validate_params(assign, {"entity_id": "host-12", "licenseKey": _KEY_ENT}), (
        "a full key must never be accepted as a parameter"
    )


# ---------------------------------------------------------------------------
# Registration: risk level, approval, group, flight-recorder exclusion
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` requires for the DB-backed tests."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


_EXPECTED_POLICY = {
    _LIST_OP: ("safe", False),
    _ASSIGNED_OP: ("safe", False),
    _ASSIGN_OP: ("dangerous", True),
}


async def test_each_licence_operation_registers_its_risk_level_and_approval(
    stub_embedding_service: AsyncMock,
) -> None:
    for registrar in (
        register_vmware_typed_operations,
        register_vmware_typed_operations_8_0,
        register_vmware_composite_operations,
        register_vmware_composite_operations_8_0,
    ):
        await registrar(embedding_service=stub_embedding_service)
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(
                        EndpointDescriptor.op_id.in_(list(_EXPECTED_POLICY))
                    )
                )
            )
            .scalars()
            .all()
        )
        groups = {
            group.id: group for group in (await session.execute(select(OperationGroup))).scalars()
        }
    found = {(row.version, row.op_id) for row in rows}
    assert found == {(version, op_id) for version in ("8.0", "9.0") for op_id in _EXPECTED_POLICY}
    for row in rows:
        assert (row.safety_level, row.requires_approval) == _EXPECTED_POLICY[row.op_id], row.op_id
        group = groups[row.group_id]
        assert group.group_key == LICENSE_GROUP_KEY
        assert group.when_to_use == LICENSE_WHEN_TO_USE
        # The registered tags make the flight recorder drop every vCenter body.
        exclusion = classify_body_exclusion(
            row.op_id, tags=row.tags or (), method="POST", delete_shaped_patterns=()
        )
        assert exclusion.excluded, row.op_id
        assert not exclusion.uncertain


# ---------------------------------------------------------------------------
# The real connector over HTTP: vCenter 8.0.3 and 9.0 paths, flight recorder
# ---------------------------------------------------------------------------


def _vi_json_url(release: str, path: str) -> str:
    return f"/sdk/vim25/{release}{path}"


def _mock_vcenter(mock: respx.MockRouter, version: str, release: str) -> dict[str, Any]:
    fake = _FakeVcenter()
    mock.post("/api/session").respond(200, json="tok")
    mock.get("/api/about").respond(200, json={"version": version})
    props_body = fake._license_manager_properties(
        {
            "specSet": [
                {
                    "propSet": [
                        {
                            "type": "LicenseManager",
                            "pathSet": ["licenses", "licenseAssignmentManager"],
                        }
                    ],
                    "objectSet": [{"obj": {"value": "LicenseManager"}}],
                }
            ]
        }
    )
    return {
        "props": mock.post(
            _vi_json_url(release, "/PropertyCollector/propertyCollector/RetrievePropertiesEx")
        ).respond(200, json=props_body),
        "query": mock.post(
            _vi_json_url(release, f"/LicenseAssignmentManager/{_LAM_MOID}/QueryAssignedLicenses")
        ).respond(200, json=_assignments()),
    }


@pytest.mark.parametrize(("version", "release"), [("8.0.3", "8.0.3.0"), ("9.0.0", "9.0.0.0")])
async def test_licence_reads_use_the_vi_json_path_of_the_vcenter_release(
    version: str, release: str
) -> None:
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    try:
        async with respx.mock(base_url=_BASE) as mock:
            routes = _mock_vcenter(mock, version, release)
            listed = await connector.license_list(_operator(), _StubTarget(), {})
            assigned = await connector.license_assigned_list(_operator(), _StubTarget(), {})
        assert routes["props"].call_count == 2
        assert routes["query"].call_count == 1
        assert json.loads(routes["query"].calls[0].request.content) == {}
    finally:
        await connector.aclose()
    assert listed["licenses"][1]["key_masked"] == "*****-*****-*****-*****-AB12C"
    assert {row["entity_id"] for row in assigned["assignments"]} == {
        "host-12",
        "host-13",
        _VCENTER_UUID,
    }
    _assert_no_full_key(listed)
    _assert_no_full_key(assigned)


@contextmanager
def _capture_scope(op_id: str, tags: tuple[str, ...]) -> Iterator[Any]:
    """Bind a flight-recorder scope the way the dispatcher does, without a DB."""
    scope = capture._CaptureScope(audit_id=uuid.uuid4(), tenant_id=uuid.uuid4(), target_id=None)
    op = capture._OpContext(
        op_id=op_id,
        connector_id="vmware-rest-9.0",
        impl_id="vmware-rest",
        product="vmware",
        tags=tags,
        body_paths=(),
        delete_shaped_patterns=(),
    )
    cap_token = capture._active_capture_var.set(scope)
    op_token = capture._op_context_var.set(op)
    try:
        yield scope
    finally:
        capture._op_context_var.reset(op_token)
        capture._active_capture_var.reset(cap_token)


async def _list_with_capture(tags: tuple[str, ...]) -> list[Any]:
    connector = _make_connector()
    _patch_no_revoke_aclose(connector)
    try:
        async with respx.mock(base_url=_BASE) as mock:
            _mock_vcenter(mock, "8.0.3", "8.0.3.0")
            with _capture_scope(_LIST_OP, tags) as scope:
                await connector.license_list(_operator(), _StubTarget(), {})
                await connector.license_assigned_list(_operator(), _StubTarget(), {})
    finally:
        await connector.aclose()
    return [span for span in scope.spans if span.span_kind == "vendor_call"]


async def test_flight_recorder_spans_hold_no_licence_key() -> None:
    spans = await _list_with_capture(VMWARE_LICENSE_LIST_OP.tags)
    licence_spans = [span for span in spans if "/sdk/vim25/" in span.attributes["url"]]
    assert len(licence_spans) == 3
    for span in licence_spans:
        assert span.attributes["body_recorded"] is False
    _assert_no_full_key(repr(spans))


async def test_without_the_licence_tag_the_flight_recorder_would_keep_the_key() -> None:
    """Control: the tag is what keeps the key out, so the test above is not empty."""
    spans = await _list_with_capture(("read-only", "vmware"))
    assert _KEY_ENT in repr(spans)


# ---------------------------------------------------------------------------
# Through the dispatcher: wait for approval, approve, assign; no row has a key
# ---------------------------------------------------------------------------


@pytest.fixture
def _clean_registry() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


async def _db_rows_text() -> str:
    """Every audit and approval row of the test database, as one text."""
    async with get_sessionmaker()() as session:
        rows: list[dict[str, Any]] = []
        for model in (AuditLog, ApprovalRequest):
            for row in (await session.execute(select(model))).scalars():
                mapper = sa_inspect(row).mapper
                rows.append({attr.key: getattr(row, attr.key) for attr in mapper.column_attrs})
    return json.dumps(rows, default=str)


async def test_assign_waits_for_a_second_person_and_no_row_holds_a_key(
    _clean_registry: None,
    stub_embedding_service: AsyncMock,
) -> None:
    fake = _FakeVcenter()
    _seed_connector(fake)  # type: ignore[arg-type]
    await register_vmware_typed_operations(embedding_service=stub_embedding_service)
    await register_vmware_composite_operations(embedding_service=stub_embedding_service)
    tenant_id = _make_e2e_operator().tenant_id
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                id=target_id,
                tenant_id=tenant_id,
                name="test-vcenter",
                product="vmware",
                host="vcenter.example.invalid",
                aliases=[],
            )
        )
        await session.commit()
    target = _FakeVmwareTarget(target_id=target_id)
    requester = _make_e2e_operator(sub="licence-requester", principal_kind=PrincipalKind.USER)
    params = {"entity_id": "host-12", "license_name": "vSphere 8 Enterprise Plus"}

    # The two reads run at once.
    listed = await dispatch(
        operator=requester, connector_id="vmware-rest-9.0", op_id=_LIST_OP, target=target, params={}
    )
    assert listed.status == "ok", listed.error

    # A person's assign waits until a second person approves; nothing changes yet.
    parked = await dispatch(
        operator=requester,
        connector_id="vmware-rest-9.0",
        op_id=_ASSIGN_OP,
        target=target,
        params=params,
    )
    assert parked.status == "awaiting_approval", parked.error
    assert fake.calls_to("UpdateAssignedLicense") == []
    request_id = UUID(parked.extras["approval_request_id"])
    async with get_sessionmaker()() as session:
        pending = await session.get(ApprovalRequest, request_id)
        assert pending is not None
        assert pending.proposed_effect["preview"]["requested"]["key_masked"].endswith("AB12C")

    reviewer = _make_e2e_operator(sub="licence-reviewer", principal_kind=PrincipalKind.USER)
    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=reviewer, params=params)
        await session.commit()
    _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = fake  # the approval may reset caches
    resumed = await dispatch(
        operator=reviewer,
        connector_id="vmware-rest-9.0",
        op_id=_ASSIGN_OP,
        target=target,
        params=params,
        _approved=True,
    )
    assert resumed.status == "ok", resumed.error
    assert resumed.result["status"] == "assigned"
    assert fake.assignments["host-12"]["assignedLicense"]["licenseKey"] == _KEY_ENT

    rows_text = await _db_rows_text()
    assert "AB12C" in rows_text, "the masked key should be in the stored rows"
    _assert_no_full_key(rows_text)
