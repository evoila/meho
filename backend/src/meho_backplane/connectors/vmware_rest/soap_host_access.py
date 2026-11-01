# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SOAP envelopes and answers of the host user and Active Directory calls.

On a single ESXi host, MEHO sends every vim call as SOAP over ``POST /sdk``
(:meth:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector._post_soap`).
This module builds the envelopes of the calls the host user and Active
Directory operations make, from the same VI-JSON body the vCenter path
sends, and turns the SOAP answers back into the VI-JSON shape:

* ``ServiceInstance.RetrieveServiceContent`` -- the ids of the account
  manager and the user directory of the host;
* ``UserDirectory.RetrieveUserGroups`` -- the local users (a list);
* ``HostAccessManager.RetrieveHostAccessControlEntries`` -- the access role of
  each user (a list);
* ``HostLocalAccountManager.CreateUser`` / ``UpdateUser`` / ``RemoveUser``;
* ``HostActiveDirectoryAuthentication.JoinDomain_Task`` /
  ``LeaveCurrentDomain_Task`` (each returns a task).

Child elements follow the order of the vim25 WSDL. A password is only in the
envelope, which the connector never logs and never records (the vendor-call
span of ``_soap_post`` has no request body).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from defusedxml.ElementTree import fromstring

from meho_backplane.connectors._shared.soap_fault import find_body as _find_body
from meho_backplane.connectors._shared.soap_fault import find_child as _find_child
from meho_backplane.connectors._shared.soap_fault import local_name as _local
from meho_backplane.connectors.vmware_rest.soap import (
    _envelope,
    _method,
    _soap_val_to_json,
    _this,
    _xml_escape,
    build_service_content_envelope,
    parse_moref_result,
    parse_service_content,
)
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY

__all__ = [
    "HOST_ACCESS_SOAP_METHODS",
    "build_host_access_envelope",
    "parse_host_access_response",
]

#: The order of the ``HostAccountSpec`` fields (vim25 WSDL sequence).
_ACCOUNT_SPEC_FIELDS: Final = ("id", "password", "description")

#: The order of the ``RetrieveUserGroups`` arguments (vim25 WSDL sequence).
_USER_GROUP_ARGS: Final = (
    "domain",
    "searchStr",
    "belongsToGroup",
    "belongsToUser",
    "exactMatch",
    "findUsers",
    "findGroups",
)

#: Methods whose answer is a list: method -> the vim type of one item.
_LIST_METHODS: Final[dict[str, str]] = {
    "RetrieveUserGroups": "UserSearchResult",
    "RetrieveHostAccessControlEntries": "HostAccessControlEntry",
}

#: Methods that answer nothing (``void``).
_VOID_METHODS: Final = frozenset({"CreateUser", "UpdateUser", "RemoveUser"})

#: Every method this module builds an envelope for.
HOST_ACCESS_SOAP_METHODS: Final = frozenset(
    {
        "RetrieveServiceContent",
        *_LIST_METHODS,
        *_VOID_METHODS,
        "JoinDomain_Task",
        "LeaveCurrentDomain_Task",
    }
)


def _leaf(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return f"<{name}>{'true' if value else 'false'}</{name}>"
    return f"<{name}>{_xml_escape(str(value))}</{name}>"


def _fields(values: Mapping[str, Any], order: tuple[str, ...]) -> str:
    return "".join(_leaf(name, values[name]) for name in order if values.get(name) is not None)


def _arguments(method: str, body: Mapping[str, Any]) -> str:
    """The method's argument elements (everything after ``_this``)."""
    if method in ("CreateUser", "UpdateUser"):
        user = body.get("user") or {}
        return f"<user>{_fields(user, _ACCOUNT_SPEC_FIELDS)}</user>"
    if method == "RemoveUser":
        return _leaf("userName", body.get("userName", ""))
    if method == "RetrieveUserGroups":
        return _fields(body, _USER_GROUP_ARGS)
    if method == "JoinDomain_Task":
        return _fields(body, ("domainName", "userName", "password"))
    if method == "LeaveCurrentDomain_Task":
        return _leaf("force", bool(body.get("force")))
    return ""


def build_host_access_envelope(
    mo_type: str, moid: str, method: str, body: Mapping[str, Any]
) -> str:
    """The SOAP envelope of *method* on ``mo_type:moid`` with the VI-JSON *body*."""
    if method not in HOST_ACCESS_SOAP_METHODS:
        raise RuntimeError(f"vmware vim method {method!r} is not a host access call")
    if method == "RetrieveServiceContent":
        return build_service_content_envelope()
    inner = _this(mo_type, moid) + _arguments(method, body)
    return _envelope(method, _method(method, inner))


def _parse_list(xml: str, method: str) -> list[Any]:
    """Every ``returnval`` of ``{method}Response``, each turned into its VI-JSON shape."""
    body = _find_body(fromstring(xml))
    response = _find_child(body, f"{method}Response") if body is not None else None
    if response is None:
        return []
    return [_soap_val_to_json(child) for child in response if _local(child.tag) == "returnval"]


def parse_host_access_response(method: str, xml: str) -> dict[str, Any]:
    """The VI-JSON shape of the SOAP answer of *method*.

    A list answer comes back as an ``ArrayOf<item type>`` box, which
    :func:`~meho_backplane.connectors.vmware_rest.vim_body.unwrap_vim_value`
    turns into the bare list (the VI-JSON path gives the bare list). A void
    method gives ``{}``; a task method the task reference.
    """
    if method == "RetrieveServiceContent":
        return parse_service_content(xml)
    if method in _LIST_METHODS:
        item_type = _LIST_METHODS[method]
        return {VIM_TYPE_NAME_KEY: f"ArrayOf{item_type}", item_type: _parse_list(xml, method)}
    if method in _VOID_METHODS:
        return {}
    return parse_moref_result(xml, method) or {}
