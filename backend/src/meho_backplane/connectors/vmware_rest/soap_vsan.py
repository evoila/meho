# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SOAP codec for vSAN methods on vCenter 8.0 (#4441).

vCenter 8.0 has no vSAN methods in its VI-JSON API: the vendor VI/JSON files
of 8.0 U1, U2 and U3 have no ``/vsan`` paths. On 8.0 a vSAN method is a SOAP
call to the vSAN service of vCenter at ``/vsanHealth`` (namespace
``urn:vsan``). It is authenticated by the cookie of a normal vCenter SOAP
login on ``/sdk``, and the ``SOAPAction`` names the vSAN API version from
``/sdk/vsanServiceVersions.xml``. Source: the vendor sample
``vsanapiutils.py`` of the vSAN Management SDK for Python 8.0 U1 and U2, and
the method definitions in its ``vsanmgmtObjects.py``.

This module only builds the request and reads the answer. The connector
sends it (``VmwareRestConnector._post_vsan``). It reuses the helpers of the
vim25 codec (:mod:`.soap`), the same way :mod:`.soap_pbm` does.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from defusedxml.ElementTree import ParseError, fromstring

from meho_backplane.connectors._shared.soap_fault import find_body as _find_body
from meho_backplane.connectors._shared.soap_fault import find_child as _find_child
from meho_backplane.connectors._shared.soap_fault import local_name as _local
from meho_backplane.connectors.vmware_rest.soap import (
    _envelope,
    _soap_val_to_json,
    _this,
    _xml_escape,
)
from meho_backplane.connectors.vmware_rest.vim_body import MOREF_TYPE_NAME, VIM_TYPE_NAME_KEY

__all__ = [
    "VSAN_NAMESPACE",
    "VSAN_SERVICE_VERSIONS_PATH",
    "VSAN_SOAP_PATH",
    "build_vsan_envelope",
    "parse_fault_object",
    "parse_vsan_result",
    "vsan_soap_action",
]

#: The vSAN SOAP endpoint of vCenter.
VSAN_SOAP_PATH: Final = "/vsanHealth"
#: The document that lists the vSAN API versions a vCenter serves.
VSAN_SERVICE_VERSIONS_PATH: Final = "/sdk/vsanServiceVersions.xml"
#: The vSAN SOAP namespace: the method element and the version document use it.
VSAN_NAMESPACE: Final = "urn:vsan"


def vsan_soap_action(api_version: str | None) -> str:
    """The ``SOAPAction`` header of a vSAN call: ``urn:vsan/<version>``.

    Empty when the version is not known. The vSAN service then uses its
    default version.
    """
    return f"{VSAN_NAMESPACE}/{api_version}" if api_version else ""


def _argument_xml(name: str, value: Any) -> str:
    """One method argument (or one field of a data object) as XML.

    The value is in the VI-JSON body form:

    * ``True`` / ``False`` becomes ``<name>true</name>``, as ``xsd:boolean``
      writes it (``fetchFromCache``, #4922).
    * A string or a whole number becomes its text (``xsd:string``,
      ``xsd:int``, ``xsd:long``; a date and time is a string).
    * A managed object reference (``cluster``) becomes
      ``<name type="ClusterComputeResource">domain-c7</name>``.
    * A list repeats the element once per entry, which is how SOAP sends an
      array (``fields``, ``querySpecs``).
    * Another data object becomes the element with one child per field, in
      the order of the dict, which must be the order the vendor type defines.
      Its ``_typeName`` is not sent: each data object MEHO sends is of the
      type the method declares, and then SOAP needs no ``xsi:type``.

    A ``None`` field is left out. Any other value raises :class:`TypeError`.
    """
    if isinstance(value, bool):
        return f"<{name}>{'true' if value else 'false'}</{name}>"
    if isinstance(value, (str, int)):
        return f"<{name}>{_xml_escape(str(value))}</{name}>"
    if isinstance(value, list):
        return "".join(_argument_xml(name, item) for item in value)
    if isinstance(value, Mapping):
        if value.get(VIM_TYPE_NAME_KEY) == MOREF_TYPE_NAME:
            mo_type = _xml_escape(str(value.get("type", "")))
            moid = _xml_escape(str(value.get("value", "")))
            return f'<{name} type="{mo_type}">{moid}</{name}>'
        fields = "".join(
            _argument_xml(str(key), item)
            for key, item in value.items()
            if key != VIM_TYPE_NAME_KEY and item is not None
        )
        return f"<{name}>{fields}</{name}>"
    raise TypeError(f"vSAN SOAP argument {name!r}: {type(value).__name__} values are not supported")


def build_vsan_envelope(
    mo_type: str,
    moid: str,
    method: str,
    arguments: Mapping[str, Any],
) -> str:
    """One vSAN method call as a SOAP 1.1 envelope in the ``urn:vsan`` namespace.

    ``_this`` names the vSAN object (*mo_type*, *moid*). *arguments* are the
    method arguments in the same form as the VI-JSON request body, in the
    order the method defines them.
    """
    inner = _this(mo_type, moid) + "".join(
        _argument_xml(name, value) for name, value in arguments.items()
    )
    return _envelope(method, f'<{method} xmlns="{VSAN_NAMESPACE}">{inner}</{method}>')


def parse_vsan_result(xml: str, method: str) -> Any:
    """The ``returnval`` of a vSAN SOAP answer, in the VI-JSON dict form.

    A method that returns an array sends one ``returnval`` element per entry.
    Two or more come back as a list. SOAP has no list marker, so one entry
    comes back as a single dict, and a list field with one entry inside a data
    object does too (VI-JSON always sends a list). The caller turns the list
    fields it reads into lists. ``None`` when the answer has no ``returnval``.
    """
    body = _find_body(fromstring(xml))
    if body is None:
        return None
    response = _find_child(body, f"{method}Response")
    if response is None:
        # As the vim25 codec does: fall back to the first element of the body.
        response = next(iter(body), None)
    if response is None:
        return None
    values = [_soap_val_to_json(child) for child in response if _local(child.tag) == "returnval"]
    if not values:
        return None
    return values[0] if len(values) == 1 else values


def parse_fault_object(xml: str) -> dict[str, str] | None:
    """The ``obj`` of a SOAP fault such as ``ManagedObjectNotFound``, else ``None``.

    The fault names the object it is about in ``detail``, for example
    ``<obj type="ClusterComputeResource">domain-c7</obj>``. Returns
    ``{"type": ..., "value": ...}``.
    """
    try:
        root = fromstring(xml)
    except ParseError:
        return None
    detail = _find_child(_find_child(_find_body(root), "Fault"), "detail")
    detail_fault = next(iter(detail), None) if detail is not None else None
    obj = _find_child(detail_fault, "obj")
    if obj is None:
        return None
    return {"type": obj.get("type") or "", "value": (obj.text or "").strip()}
