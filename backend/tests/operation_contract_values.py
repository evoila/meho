# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What a real-looking value in an operation example is (contract rule 11).

An AI copies example values into real calls. So an example holds
placeholders such as ``<vm-id>``, documentation addresses (RFC 5737,
RFC 3849) and example host names (RFC 2606), never a value that looks like
a real system. Used by :mod:`tests.test_operation_contracts`.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Final

__all__ = ["PUBLIC_EXAMPLE_NAMES", "real_looking_parts"]

#: Public names that rule 11 accepts in an example, with the reason. A host
#: name passes when it is an entry or ends with "." and an entry.
PUBLIC_EXAMPLE_NAMES: Final[Mapping[str, str]] = {
    "9.0.2.0": "a VCF version number, not an IP address",
    "cert-manager.io": "the public cert-manager API group",
    "iam.googleapis.com": "the public Google IAM API",
    "iam.gserviceaccount.com": "the public domain of every Google service account",
    "kubernetes.io": "the public Kubernetes API group and label prefix",
    "management.azure.com": "the public Azure Resource Manager API",
    "svc.cluster.local": "the fixed DNS suffix inside every Kubernetes cluster",
    "vsphere.local": "the default vCenter single sign-on domain",
}

#: Documentation address ranges (RFC 5737 and RFC 3849).
DOCUMENTATION_NETWORKS: Final = tuple(
    ipaddress.ip_network(net)
    for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")
)
#: Top-level domains that make a host name look real. ``.example``, ``.test``
#: and ``.invalid`` are reserved for examples, so they are not here.
REAL_TOP_LEVEL_DOMAINS: Final[frozenset[str]] = frozenset(
    {"com", "net", "org", "io", "local", "lan", "internal", "corp", "home", "intra"}
    | {"cloud", "dev", "app", "info", "biz", "eu", "de", "at", "ch", "uk", "us"}
)
EXAMPLE_DOMAINS: Final[tuple[str, ...]] = ("example.com", "example.net", "example.org")
_IN_ANGLE_BRACKETS: Final = re.compile(r"<[^<>]*>")
_IPV4: Final = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])")
_IPV6_PART: Final = re.compile(r"[0-9a-f.]*:[0-9a-f:.]*:[0-9a-f.]*", re.IGNORECASE)
_HOST_NAME: Final = re.compile(
    r"(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}(?![\w.-])", re.IGNORECASE
)
_VENDOR_ID: Final = re.compile(
    r"(?<![\w-])(?:vm|host|domain|datastore|resgroup|group|network|dvportgroup|dvs|datacenter"
    r"|folder)-[a-z]?\d+(?![\w-])"
)
_UUID: Final = re.compile(
    r"(?<![\w-])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![\w-])",
    re.IGNORECASE,
)
_NIL_UUID: Final[str] = "00000000-0000-0000-0000-000000000000"


def _is_public_name(name: str) -> bool:
    name = name.lower()
    return any(name == entry or name.endswith(f".{entry}") for entry in PUBLIC_EXAMPLE_NAMES)


def real_looking_parts(value: str) -> list[str]:
    """Every part of *value* that looks like a real address, host or id."""
    text = _IN_ANGLE_BRACKETS.sub(" ", value)
    found: list[str] = []
    for ip in [*_IPV4.findall(text), *_IPV6_PART.findall(text)]:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not any(address in net for net in DOCUMENTATION_NETWORKS):
            found.append(ip)
    for match in _HOST_NAME.finditer(text):
        host = match.group(0).lower()
        if host.rsplit(".", 1)[1] in REAL_TOP_LEVEL_DOMAINS and not any(
            host == domain or host.endswith(f".{domain}") for domain in EXAMPLE_DOMAINS
        ):
            found.append(match.group(0))
    found.extend(_VENDOR_ID.findall(text))
    found.extend(uuid for uuid in _UUID.findall(text) if uuid != _NIL_UUID)
    return [part for part in found if not _is_public_name(part)]
