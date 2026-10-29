# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read a SOAP 1.1 ``<soapenv:Fault>`` body, and carry it on an exception.

SOAP is a general protocol, so this part is not tied to one vendor. It
lives here, and not in a connector folder, because the core error builders
(:mod:`meho_backplane.operations._errors`) read SOAP faults too. They turn
a fault into the ``upstream_fault`` field of an error answer.

The module holds:

* :class:`SoapFault` and :func:`parse_soap_fault` -- parse a fault body.
* :func:`attach_soap_fault` / :func:`soap_fault_of` -- store a parsed
  fault on an exception, and read it back.
* A few small XML helpers (:func:`local_name`, :func:`find_child`,
  :func:`find_body`, :func:`strip_qname_prefix`). The SOAP codecs in the
  connector folders use them too.

Parsing uses ``defusedxml.ElementTree.fromstring`` (the XXE-hardened
parser). Parsed nodes are typed ``Any``: annotating them with the stdlib
``Element`` type would bring back the ``xml.etree`` import that Semgrep
flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from defusedxml.ElementTree import ParseError, fromstring

__all__ = [
    "XSI_TYPE_ATTR",
    "SoapFault",
    "attach_soap_fault",
    "find_body",
    "find_child",
    "local_name",
    "parse_soap_fault",
    "soap_fault_of",
    "strip_qname_prefix",
]

#: XML-Schema-instance namespace (the ``xsi:type`` attribute lives here).
_XSI_NS: Final = "http://www.w3.org/2001/XMLSchema-instance"
#: Clark-notation key of the ``xsi:type`` attribute on a parsed node.
XSI_TYPE_ATTR: Final = f"{{{_XSI_NS}}}type"


@dataclass(frozen=True, slots=True)
class SoapFault:
    """A parsed ``<soapenv:Fault>`` -- the connector maps it to an error.

    ``fault_type`` is the discriminator localName of the ``detail`` child
    (``InvalidLogin``, ``NoPermission``, ``PlatformConfigFault``,
    ``HostConfigFault``, ``InvalidArgument`` ...), taken from the child's
    ``xsi:type`` when present and otherwise from its element local-name.

    Carries only ``faultcode`` / ``faultstring`` / ``fault_type`` -- none
    of which echo a login envelope, so the default ``repr`` cannot leak
    a credential.
    """

    faultcode: str | None
    faultstring: str | None
    fault_type: str | None


#: Attribute name the parsed fault rides on, on the exception a connector
#: raises for it (#3567). Kept in one place so the writer and the reader agree.
_SOAP_FAULT_ATTR: Final = "soap_fault"


def attach_soap_fault(exc: BaseException, fault: SoapFault) -> None:
    """Store the parsed *fault* on *exc*, so the error envelope can show it (#3567).

    On a SOAP path no :exc:`httpx.HTTPStatusError` reaches the error
    builders: the connector raises a plain :exc:`RuntimeError` or a
    ``ConnectorAuthError`` instead. This keeps the fault type and the full
    faultstring with that exception, and keeps its class unchanged.
    """
    setattr(exc, _SOAP_FAULT_ATTR, fault)


def soap_fault_of(exc: BaseException) -> SoapFault | None:
    """The :class:`SoapFault` stored by :func:`attach_soap_fault`, else ``None``."""
    fault = getattr(exc, _SOAP_FAULT_ATTR, None)
    return fault if isinstance(fault, SoapFault) else None


# --- Small XML helpers -----------------------------------------------------


def local_name(tag: Any) -> str:
    """Local-name of a Clark-notation tag/attribute (``{ns}foo`` -> ``foo``)."""
    text = tag if isinstance(tag, str) else str(tag)
    return text.rpartition("}")[2]


def strip_qname_prefix(qname: str) -> str:
    """Local part of an ``xsi:type`` value (``xsd:boolean`` -> ``boolean``)."""
    return qname.rpartition(":")[2]


def find_child(parent: Any, localname: str) -> Any:
    """First direct child of *parent* whose local-name is *localname*, else ``None``."""
    if parent is None:
        return None
    for child in parent:
        if local_name(child.tag) == localname:
            return child
    return None


def find_body(root: Any) -> Any:
    """The ``soapenv:Body`` element (namespace-agnostic by local-name)."""
    if local_name(root.tag) == "Body":
        return root
    return find_child(root, "Body")


# --- Fault parser ----------------------------------------------------------


def parse_soap_fault(xml: str) -> SoapFault | None:
    """Parse a ``<soapenv:Fault>`` body; ``None`` when the body is not a fault.

    Run on **both** 200 and 500 responses *before* ``raise_for_status``: a
    SOAP fault is HTTP 500 with a ``<soapenv:Fault>`` body, but the parse
    is the authority, not the status code. A body that does not parse or
    carries no ``Fault`` returns ``None`` so the caller proceeds normally.
    """
    try:
        root = fromstring(xml)
    except ParseError:
        return None
    body = find_body(root)
    fault = find_child(body, "Fault")
    if fault is None:
        return None
    faultcode_el = find_child(fault, "faultcode")
    faultstring_el = find_child(fault, "faultstring")
    faultcode = faultcode_el.text if faultcode_el is not None else None
    faultstring = faultstring_el.text if faultstring_el is not None else None
    fault_type: str | None = None
    detail = find_child(fault, "detail")
    if detail is not None:
        detail_child = next(iter(detail), None)
        if detail_child is not None:
            xsi_type_raw = detail_child.get(XSI_TYPE_ATTR)
            fault_type = (
                strip_qname_prefix(xsi_type_raw)
                if xsi_type_raw is not None
                else local_name(detail_child.tag)
            )
    return SoapFault(faultcode=faultcode, faultstring=faultstring, fault_type=fault_type)
