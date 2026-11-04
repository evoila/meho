# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The flight recorder masks the OVF property values of a vSphere OVF import.

An OVF import through the VI/JSON API carries the operator's OVF property
values three times: in the ``propertyMapping`` list of
``OvfManager.CreateImportSpec`` (``KeyValue`` pairs), in the
``VAppPropertyInfo`` objects of the import spec that call answers, and
again in the ``spec`` that ``ResourcePool.ImportVApp`` sends back. The
structural rule in :mod:`meho_backplane.redaction.flight_recorder.bodies`
masks the value in every one of them, keeps the key / id, and leaves the
trace certain (agent-readable).

Every value below is made up and deliberately not credential-shaped, so
the tests prove the structural rule fires, not the value-shape net.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from meho_backplane.redaction.flight_recorder import (
    OVF_PROPERTY_VALUE_MARKER,
    redact_body,
    redact_span,
)
from meho_backplane.settings import get_settings


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the settings env so ``get_settings()`` resolves in the span classifier."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


_CONNECTOR_ID = "vmware-rest-9.0"
_CREATE_IMPORT_SPEC = "POST:/OvfManager/{moId}/CreateImportSpec"
_IMPORT_VAPP = "POST:/ResourcePool/{moId}/ImportVApp"
_PASSWORD = "PLACEHOLDER-root-pw-value"
_USER_DATA = "PLACEHOLDER-user-data-value"
_DEFAULT = "PLACEHOLDER-default-value"


def _assert_no_value(obj: Any) -> None:
    blob = repr(obj)
    for needle in (_PASSWORD, _USER_DATA, _DEFAULT):
        assert needle not in blob, f"value {needle!r} survived in {blob!r}"


def _create_import_spec_request() -> dict[str, Any]:
    return {
        "ovfDescriptor": "<Envelope/>",
        "resourcePool": {
            "_typeName": "ManagedObjectReference",
            "type": "ResourcePool",
            "value": "resgroup-9",
        },
        "datastore": {
            "_typeName": "ManagedObjectReference",
            "type": "Datastore",
            "value": "datastore-1",
        },
        "cisp": {
            "_typeName": "OvfCreateImportSpecParams",
            "entityName": "keep-vm-name",
            "networkMapping": [
                {
                    "_typeName": "OvfNetworkMapping",
                    "name": "NetA",
                    "network": {
                        "_typeName": "ManagedObjectReference",
                        "type": "Network",
                        "value": "network-11",
                    },
                }
            ],
            "propertyMapping": [
                {"_typeName": "KeyValue", "key": "guestinfo.hostname", "value": "keep-host"},
                {"_typeName": "KeyValue", "key": "guestinfo.root.passwd", "value": _PASSWORD},
                {"_typeName": "KeyValue", "key": "user-data", "value": _USER_DATA},
            ],
            "deploymentOption": "small",
        },
    }


def _import_spec() -> dict[str, Any]:
    """The import spec ``CreateImportSpec`` answers and ``ImportVApp`` sends back."""
    return {
        "_typeName": "VirtualMachineImportSpec",
        "configSpec": {
            "_typeName": "VirtualMachineConfigSpec",
            "name": "keep-vm-name",
            "vAppConfig": {
                "_typeName": "VmConfigSpec",
                "property": [
                    {
                        "_typeName": "VAppPropertySpec",
                        "operation": "add",
                        "info": {
                            "_typeName": "VAppPropertyInfo",
                            "key": 0,
                            "id": "guestinfo.root.passwd",
                            "type": "password",
                            "value": _PASSWORD,
                            "defaultValue": _DEFAULT,
                            "userConfigurable": True,
                        },
                    },
                    {
                        "_typeName": "VAppPropertySpec",
                        "operation": "add",
                        "info": {
                            "_typeName": "VAppPropertyInfo",
                            "key": 1,
                            "id": "user-data",
                            "type": "string",
                            "value": _USER_DATA,
                        },
                    },
                ],
            },
        },
    }


def test_create_import_spec_request_masks_property_mapping_values() -> None:
    out = redact_body(_create_import_spec_request(), paths=(), content_type="application/json")

    cisp = out.value["cisp"]
    assert [p["value"] for p in cisp["propertyMapping"]] == [OVF_PROPERTY_VALUE_MARKER] * 3
    assert [p["key"] for p in cisp["propertyMapping"]] == [
        "guestinfo.hostname",
        "guestinfo.root.passwd",
        "user-data",
    ]
    assert all(p["_typeName"] == "KeyValue" for p in cisp["propertyMapping"])
    # Every other field survives, also a ``value`` that is a managed object id.
    assert cisp["entityName"] == "keep-vm-name"
    assert cisp["networkMapping"][0]["network"]["value"] == "network-11"
    assert out.value["resourcePool"]["value"] == "resgroup-9"
    _assert_no_value(out.value)
    assert out.uncertain is False


def test_create_import_spec_answer_masks_vapp_property_values() -> None:
    answer = {
        "_typeName": "OvfCreateImportSpecResult",
        "importSpec": _import_spec(),
        "fileItem": [
            {"_typeName": "OvfFileItem", "deviceId": "/disk/vmdisk1", "path": "disk1.vmdk"}
        ],
        "warning": [],
        "error": [],
    }
    out = redact_body(answer, paths=(), content_type="application/json")

    props = out.value["importSpec"]["configSpec"]["vAppConfig"]["property"]
    assert props[0]["info"]["value"] == OVF_PROPERTY_VALUE_MARKER
    assert props[0]["info"]["defaultValue"] == OVF_PROPERTY_VALUE_MARKER
    assert props[1]["info"]["value"] == OVF_PROPERTY_VALUE_MARKER
    assert "defaultValue" not in props[1]["info"]
    assert [p["info"]["id"] for p in props] == ["guestinfo.root.passwd", "user-data"]
    assert props[0]["info"]["userConfigurable"] is True
    assert out.value["fileItem"][0]["path"] == "disk1.vmdk"
    assert out.value["importSpec"]["configSpec"]["name"] == "keep-vm-name"
    _assert_no_value(out.value)
    assert out.uncertain is False


def test_import_trace_is_clean_in_every_call_and_direction() -> None:
    """Through redact_span: the two vendor calls record no OVF property value."""
    create = redact_span(
        op_id=_CREATE_IMPORT_SPEC,
        connector_id=_CONNECTOR_ID,
        method="POST",
        request_body=_create_import_spec_request(),
        response_body={"_typeName": "OvfCreateImportSpecResult", "importSpec": _import_spec()},
        request_content_type="application/json",
        response_content_type="application/json",
    )
    import_vapp = redact_span(
        op_id=_IMPORT_VAPP,
        connector_id=_CONNECTOR_ID,
        method="POST",
        request_body={
            "spec": _import_spec(),
            "folder": {
                "_typeName": "ManagedObjectReference",
                "type": "Folder",
                "value": "group-v3",
            },
        },
        response_body={
            "_typeName": "ManagedObjectReference",
            "type": "HttpNfcLease",
            "value": "session[1]",
        },
        request_content_type="application/json",
        response_content_type="application/json",
    )

    for span in (create, import_vapp):
        assert span.body_recorded is True
        assert span.uncertain is False
        _assert_no_value(span.request_body)
        _assert_no_value(span.response_body)
    assert import_vapp.request_body["folder"]["value"] == "group-v3"
    assert import_vapp.response_body["value"] == "session[1]"


def test_other_value_fields_are_unchanged() -> None:
    """The rule is structural: a ``value`` outside the three shapes stays."""
    body = {
        "propertyMapping": {"not": "a list", "value": "keep-1"},
        "items": [{"_typeName": "KeyValue", "key": "k", "value": "keep-2"}],
        "info": {"_typeName": "OptionValue", "key": "k", "value": "keep-3"},
    }
    out = redact_body(body, paths=(), content_type="application/json")
    assert out.value == body
    assert out.uncertain is False
