# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the host local-user and Active Directory calls against the pinned VI-JSON files.

* An always-on test pins the vendor-call lists of the five changes and the
  SOAP method set of the single-host path, so a change to them is seen.
* The shelf tests read the pinned ``vi-json.yaml`` of vCenter 9.0 and the
  three 8.0 files (U1, U2, U3): every vendor call is a ``POST`` path there,
  the request bodies MEHO sends use only fields of the vendor's request types,
  and the answer fields MEHO reads are fields of the vendor's types. They skip
  when the spec shelf is not configured and run in CI.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import pytest
import yaml

from meho_backplane.connectors.vmware_rest.composites._host_access_ops import (
    HOST_ACCESS_GOVERNED_SUBOPS,
)
from meho_backplane.connectors.vmware_rest.soap_host_access import HOST_ACCESS_SOAP_METHODS
from tests._spec_shelf import require_shelf_spec

_VI_JSON_FILES = (
    ("vcenter-9.0", "vi-json.yaml"),
    ("vcenter-8.0", "vi-json-8.0u1.yaml"),
    ("vcenter-8.0", "vi-json-8.0u2.yaml"),
    ("vcenter-8.0", "vi-json-8.0u3.yaml"),
)

_VIM_CALLS = sorted(
    {
        op_id
        for calls in HOST_ACCESS_GOVERNED_SUBOPS.values()
        for op_id in calls
        if not op_id.startswith("GET:")
    }
)


@functools.cache
def _spec(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _fields(spec: dict[str, Any], type_name: str) -> set[str]:
    """The fields of a vendor type, with the fields of its parent types."""
    schema = spec["components"]["schemas"][type_name]
    fields = set(schema.get("properties") or {})
    for part in schema.get("allOf") or []:
        ref = part.get("$ref")
        if isinstance(ref, str):
            fields |= _fields(spec, ref.rsplit("/", 1)[-1])
        fields |= set(part.get("properties") or {})
    return fields


def test_the_vendor_call_lists_are_the_expected_sets() -> None:
    assert set(HOST_ACCESS_GOVERNED_SUBOPS) == {
        "vmware.composite.host.local_user.create",
        "vmware.composite.host.local_user.password.set",
        "vmware.composite.host.local_user.delete",
        "vmware.composite.host.ad.join",
        "vmware.composite.host.ad.leave",
    }
    assert _VIM_CALLS == [
        "POST:/HostAccessManager/{moId}/RetrieveHostAccessControlEntries",
        "POST:/HostActiveDirectoryAuthentication/{moId}/JoinDomain_Task",
        "POST:/HostActiveDirectoryAuthentication/{moId}/LeaveCurrentDomain_Task",
        "POST:/HostLocalAccountManager/{moId}/CreateUser",
        "POST:/HostLocalAccountManager/{moId}/RemoveUser",
        "POST:/HostLocalAccountManager/{moId}/UpdateUser",
        "POST:/PropertyCollector/{moId}/RetrievePropertiesEx",
        "POST:/ServiceInstance/{moId}/RetrieveServiceContent",
        "POST:/UserDirectory/{moId}/RetrieveUserGroups",
    ]
    # Every host-access call except the property read has its own SOAP envelope.
    assert {op.rsplit("/", 1)[-1] for op in _VIM_CALLS} - {"RetrievePropertiesEx"} == (
        HOST_ACCESS_SOAP_METHODS
    )


@pytest.mark.parametrize(("product_dir", "file_name"), _VI_JSON_FILES)
def test_every_vendor_call_is_a_post_path_of_the_pinned_file(
    product_dir: str, file_name: str
) -> None:
    paths = _spec(require_shelf_spec(product_dir, file_name))["paths"]
    missing = [
        op_id for op_id in _VIM_CALLS if "post" not in paths.get(op_id.partition(":")[2], {})
    ]
    assert not missing, f"{product_dir}/{file_name} has no POST path for {missing}"


@pytest.mark.parametrize(("product_dir", "file_name"), _VI_JSON_FILES)
def test_the_request_bodies_use_vendor_fields(product_dir: str, file_name: str) -> None:
    spec = _spec(require_shelf_spec(product_dir, file_name))
    assert "user" in _fields(spec, "CreateUserRequestType")
    assert "user" in _fields(spec, "UpdateUserRequestType")
    assert {"id", "password", "description"} <= _fields(spec, "HostAccountSpec")
    assert "userName" in _fields(spec, "RemoveUserRequestType")
    assert {"searchStr", "exactMatch", "findUsers", "findGroups"} <= _fields(
        spec, "RetrieveUserGroupsRequestType"
    )
    assert {"domainName", "userName", "password"} <= _fields(spec, "JoinDomainRequestType")
    assert "force" in _fields(spec, "LeaveCurrentDomainRequestType")


@pytest.mark.parametrize(("product_dir", "file_name"), _VI_JSON_FILES)
def test_the_answer_fields_are_vendor_fields(product_dir: str, file_name: str) -> None:
    spec = _spec(require_shelf_spec(product_dir, file_name))
    assert {"principal", "fullName", "group", "id", "shellAccess"} <= _fields(
        spec, "PosixUserSearchResult"
    )
    assert {"principal", "group", "accessMode"} <= _fields(spec, "HostAccessControlEntry")
    assert {"accountManager", "userDirectory"} <= _fields(spec, "ServiceContent")
    assert {"enabled", "joinedDomain", "trustedDomain", "domainMembershipStatus"} <= _fields(
        spec, "HostActiveDirectoryInfo"
    )
    assert "authConfig" in _fields(spec, "HostAuthenticationManagerInfo")
    assert {"authenticationManager", "hostAccessManager"} <= _fields(spec, "HostConfigManager")
    paths = spec["paths"]
    for prop in ("info", "supportedStore"):
        assert "get" in paths[f"/HostAuthenticationManager/{{moId}}/{prop}"]
