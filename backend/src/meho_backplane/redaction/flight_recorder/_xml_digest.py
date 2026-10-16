# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fault-type digest for XML bodies the flight recorder cannot redact.

Task #3708. The redaction engine (:mod:`.bodies`) can only prove a JSON
body clean, so it never records an XML body. Before this module it kept
nothing at all for an XML body -- just the omitted marker. That hid the
one fact an operator needs from a vim25 SOAP answer: *which faults came
back*. A standalone-ESXi ``RetrievePropertiesEx`` can answer HTTP 200
with every property in ``missingSet``, each carrying a per-property fault
(``NoPermission``, ``InvalidProperty``, ``NotAuthenticated`` ...), and
the trace showed no sign of it.

So for an XML body the engine now records a small **digest** in place of
the body:

* ``omitted`` -- the usual omitted marker; the body itself is still never
  recorded.
* ``content_sha256`` -- the SHA-256 of the captured bytes, so an operator
  can match the trace to a body captured elsewhere.
* ``fault_types`` -- ``{type name: count}`` for every fault element in
  the body: each vim ``fault`` element (``missingSet[].fault``, task
  errors) and each ``soapenv:Fault/detail`` child.

Only fault **type names** are read. Fault text (``faultstring``,
``localizedMessage``) and fault fields are never read, because they can
echo a property value or a private name. A type name must look like an
identifier or it is dropped.

Parsing uses ``defusedxml``'s parser (no DTDs, no entities, no external
references). Elements are read as they stream in, so a body cut at the
size cap or broken part-way still yields the fault types seen before the
break. Any parse fault just ends the walk; this module never raises.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any, Final

from defusedxml.ElementTree import DefusedXMLParser

__all__ = ["xml_fault_digest"]

#: Clark-notation key of the ``xsi:type`` attribute.
_XSI_TYPE_ATTR: Final[str] = "{http://www.w3.org/2001/XMLSchema-instance}type"

#: The vim wrapper around a concrete fault. It names no cause, so it is
#: skipped; the concrete fault nested inside it is counted.
_WRAPPER_TYPE: Final[str] = "LocalizedMethodFault"

#: What a recorded type name must look like. Anything else is dropped so
#: free text can never ride into the trace as a "type name".
_TYPE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def xml_fault_digest(body: str | bytes | bytearray, marker: str) -> dict[str, Any]:
    """Return the digest that stands in for an XML *body* in a trace."""
    data = body.encode("utf-8", "replace") if isinstance(body, str) else bytes(body)
    return {
        "omitted": marker,
        "content_sha256": hashlib.sha256(data).hexdigest(),
        "fault_types": dict(sorted(_fault_type_counts(data).items())),
    }


def _fault_type_counts(data: bytes) -> Counter[str]:
    collector = _FaultCollector()
    parser = DefusedXMLParser(target=collector, forbid_dtd=True)
    try:
        parser.feed(data)
        parser.close()
    except Exception:
        # A broken or cut body keeps the fault types read before the break.
        return collector.counts
    return collector.counts


def _local(name: str) -> str:
    """Local part of a Clark tag (``{ns}x``) or a QName value (``p:x``)."""
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


class _FaultCollector:
    """Streaming parser target: counts fault type names, reads nothing else."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self._stack: list[str] = []

    def start(self, tag: str, attrib: dict[str, str]) -> None:
        local = _local(tag)
        name = self._fault_type(local, attrib)
        if name is not None and name != _WRAPPER_TYPE and _TYPE_NAME_RE.fullmatch(name):
            self.counts[name] += 1
        self._stack.append(local)

    def end(self, tag: str) -> None:
        if self._stack:
            self._stack.pop()

    def data(self, data: str) -> None:
        return None

    def close(self) -> None:
        return None

    def _fault_type(self, local: str, attrib: dict[str, str]) -> str | None:
        xsi_type = attrib.get(_XSI_TYPE_ATTR)
        if local == "fault":
            return _local(xsi_type) if xsi_type is not None else None
        if self._stack[-2:] == ["Fault", "detail"]:
            return _local(xsi_type) if xsi_type is not None else local
        return None
