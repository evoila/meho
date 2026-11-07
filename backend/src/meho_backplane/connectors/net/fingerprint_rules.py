# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The rules ``net.fingerprint`` uses to name a product from what it saw.

This module has no network code. It reads the answers that
:mod:`meho_backplane.connectors.net.fingerprint` collected (the TLS
certificate, the page title of ``/``, the vSphere "about" answer and the
SSH banner) and says which product they point to, and how sure it is.

How the decision works:

* Each rule looks at one field of one answer and names one product. A
  rule has a weight: the vSphere "about" answer is the system's own
  statement (3), a page title that names the product is strong (2), and a
  certificate field is weak (1).
* The weights of the rules that matched are added up per product. The
  product with the highest sum wins. The sum gives the confidence:
  3 or more is ``high``, 2 is ``medium``, 1 is ``low``.
* No rule matched, or two products have the same highest sum: the answer
  is ``unknown`` with confidence ``none``. MEHO never guesses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

__all__ = [
    "Evidence",
    "Match",
    "classify",
    "extract_title",
    "parse_vsphere_about",
]

#: The longest text MEHO keeps from any one answer field.
MAX_FIELD_CHARS: Final[int] = 256

#: The fields of the vSphere "about" answer MEHO keeps (XML tag → result key).
_ABOUT_FIELDS: Final[dict[str, str]] = {
    "name": "name",
    "fullName": "full_name",
    "vendor": "vendor",
    "version": "version",
    "build": "build",
    "apiType": "api_type",
    "productLineId": "product_line_id",
}

_TITLE_RE: Final[re.Pattern[str]] = re.compile(
    r"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL
)

#: Products this op can name. Each has the vendor MEHO reports with it.
_VENDORS: Final[dict[str, str]] = {
    "vcenter": "VMware",
    "esxi": "VMware",
    "nsx": "VMware",
    "sddc": "VMware",
}


@dataclass(frozen=True)
class _Rule:
    product: str
    source: str
    field: str
    needle: str
    weight: int


#: The rules. ``needle`` is matched as lower-case text inside the field.
#: For ``vsphere_about.api_type`` the whole value must be equal.
_RULES: Final[tuple[_Rule, ...]] = (
    _Rule("vcenter", "vsphere_about", "api_type", "virtualcenter", 3),
    _Rule("esxi", "vsphere_about", "api_type", "hostagent", 3),
    _Rule("sddc", "http", "title", "sddc manager", 2),
    _Rule("nsx", "http", "title", "nsx", 2),
    _Rule("vcenter", "http", "title", "vcenter", 2),
    _Rule("vcenter", "http", "title", "vsphere client", 2),
    _Rule("esxi", "http", "title", "esxi", 2),
    _Rule("esxi", "http", "title", "host client", 2),
    _Rule("esxi", "tls", "subject", "vmware esx server default certificate", 1),
    _Rule("nsx", "tls", "subject", "ou=nsx", 1),
)


@dataclass(frozen=True)
class Match:
    """One rule that matched: what it names and what it saw."""

    product: str
    source: str
    field: str
    value: str
    weight: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "source": self.source,
            "field": self.field,
            "value": self.value,
            "weight": self.weight,
        }


@dataclass
class Evidence:
    """What each check saw. A check that did not get an answer has ``checked=False``."""

    tls: dict[str, Any] = field(default_factory=dict)
    http: dict[str, Any] = field(default_factory=dict)
    vsphere_about: dict[str, Any] = field(default_factory=dict)
    ssh: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tls": self.tls,
            "http": self.http,
            "vsphere_about": self.vsphere_about,
            "ssh": self.ssh,
        }

    def any_answer(self) -> bool:
        return any(
            part.get("checked") is True
            for part in (self.tls, self.http, self.vsphere_about, self.ssh)
        )


def clip(value: Any) -> str | None:
    """Return *value* as stripped text of at most :data:`MAX_FIELD_CHARS`, or None."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text[:MAX_FIELD_CHARS]


def extract_title(body: str) -> str | None:
    """Return the text of the first ``<title>`` in *body*, or None."""
    found = _TITLE_RE.search(body)
    return clip(found.group(1)) if found else None


def _local_name(tag: Any) -> str:
    text = str(tag)
    return text.rsplit("}", 1)[-1]


def parse_vsphere_about(body: bytes) -> dict[str, str | None] | None:
    """Read the ``about`` block of a vSphere ``RetrieveServiceContent`` answer.

    Returns None when the body is not XML, has no ``about`` block, or the
    block has no ``name`` (then it is not a vSphere answer). The XML is
    read with ``defusedxml``, so a hostile answer cannot expand entities.
    """
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, DefusedXmlException, ValueError):
        return None
    for element in root.iter():
        if _local_name(element.tag) != "about":
            continue
        about: dict[str, str | None] = dict.fromkeys(_ABOUT_FIELDS.values())
        for child in element:
            key = _ABOUT_FIELDS.get(_local_name(child.tag))
            if key is not None:
                about[key] = clip(child.text)
        return about if about["name"] else None
    return None


def _rule_matches(rule: _Rule, value: str) -> bool:
    lowered = value.lower()
    if rule.source == "vsphere_about":
        return lowered == rule.needle
    return rule.needle in lowered


def _matches(evidence: Evidence) -> list[Match]:
    parts = evidence.as_dict()
    found: list[Match] = []
    seen: set[tuple[str, str, str]] = set()
    for rule in _RULES:
        raw = parts[rule.source].get(rule.field)
        if not isinstance(raw, str) or not _rule_matches(rule, raw):
            continue
        # Two needles for the same product on the same field count once.
        key = (rule.product, rule.source, rule.field)
        if key in seen:
            continue
        seen.add(key)
        found.append(Match(rule.product, rule.source, rule.field, raw, rule.weight))
    return found


def _confidence(score: int) -> str:
    if score >= 3:
        return "high"
    if score == 2:
        return "medium"
    return "low"


def classify(evidence: Evidence) -> dict[str, Any]:
    """Name the likely product, version and confidence from *evidence*.

    Returns ``{product, vendor, version, build, confidence, matched}``.
    ``product`` is ``"unknown"`` (and ``confidence`` is ``"none"``) when
    no rule matched or when two products tie.
    """
    matched = _matches(evidence)
    scores: dict[str, int] = {}
    for match in matched:
        scores[match.product] = scores.get(match.product, 0) + match.weight
    unknown = {
        "product": "unknown",
        "vendor": None,
        "version": None,
        "build": None,
        "confidence": "none",
        "matched": [m.as_dict() for m in matched],
    }
    if not scores:
        return unknown
    best = max(scores.values())
    leaders = [product for product, score in scores.items() if score == best]
    if len(leaders) != 1:
        return unknown
    product = leaders[0]
    version: str | None = None
    build: str | None = None
    # Only the system's own "about" answer gives a version; a page title does not.
    if any(m.product == product and m.source == "vsphere_about" for m in matched):
        version = evidence.vsphere_about.get("version")
        build = evidence.vsphere_about.get("build")
    return {
        "product": product,
        "vendor": _VENDORS[product],
        "version": version,
        "build": build,
        "confidence": _confidence(best),
        "matched": [m.as_dict() for m in matched],
    }
