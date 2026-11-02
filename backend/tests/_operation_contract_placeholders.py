# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Rule 11 of the operation contract check: example values look made up.

Agents copy example values into real calls. So an example must never hold a
value that looks real. This module finds such values in one example:

- an IPv4 or IPv6 address outside the documentation ranges
  (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32);
- a host name with a real top-level domain: any public one (the official
  list in ``tests/data/top_level_domains.txt``) or a private-network ending
  such as ``.local`` or ``.lan``. ``example.com``, ``example.org``,
  ``example.net`` and the reserved ``.test``, ``.example`` and ``.invalid``
  names are fine, and so are the fixed vendor names in :data:`VENDOR_NAMES`.
  A field whose name holds ``path`` or ``file`` holds a file name, which may
  end like a domain (``release-notes.md``), so it is not checked for hosts;
- a vendor-shaped object id such as ``vm-42``, ``host-12``, ``domain-c8``,
  ``datastore-15``, ``resgroup-8`` or ``group-v3``;
- a UUID other than the all-zero one.

A placeholder in angle brackets, such as ``<vm-id>``, always passes. Used by
``test_operation_contracts.py``; tested with fake operations in
``test_operation_contract_rules.py``.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final

#: A placeholder in an example: ``<vm-id>``, ``<device-key>``, ``<one-of-possible_answers>``.
PLACEHOLDER: Final = re.compile(r"^<[a-z0-9][a-z0-9_.-]*>$")

DOCUMENTATION_NETWORKS: Final[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]] = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("2001:db8::/32"),
)

TOP_LEVEL_DOMAINS_FILE: Final[Path] = Path(__file__).parent / "data" / "top_level_domains.txt"


def load_top_level_domains(path: Path = TOP_LEVEL_DOMAINS_FILE) -> frozenset[str]:
    """The public top-level domains in *path*: one per line, ``#`` lines are comments."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return frozenset(line.strip().lower() for line in lines if line.strip() and line[0] != "#")


#: Endings of a host name on a private network. They are no public domains,
#: but a host with one of them is a real host of a user all the same.
PRIVATE_NETWORK_ENDINGS: Final[frozenset[str]] = frozenset(
    ["local", "lan", "internal", "corp", "home", "localdomain"]
)

#: Top-level domains that make a host name look real: every public one and
#: the private-network endings.
REAL_TOP_LEVEL_DOMAINS: Final[frozenset[str]] = load_top_level_domains() | PRIVATE_NETWORK_ENDINGS

#: Fixed names of a vendor product that end like a domain: its fixed domains,
#: and dotted type names whose last part is a top-level domain by chance.
#: They are the same on every system, so they are no real host of a user.
#: Each one has the reason.
VENDOR_NAMES: Final[Mapping[str, str]] = {
    "azure.com": "the Azure management API host, the same for every user",
    "googleapis.com": "the fixed Google Cloud API and identity names",
    "gserviceaccount.com": "the fixed domain of every Google service account email",
    "cluster.local": "the fixed DNS suffix inside every Kubernetes cluster",
    "kubernetes.io": "the prefix of the built-in Kubernetes API groups and labels",
    "cert-manager.io": "the API group of the cert-manager Kubernetes add-on",
    "vsphere.local": "the default vCenter single sign-on domain",
    "microsoft.storage": "an Azure resource provider name; '.storage' is a top-level domain",
    "compute.provision.post": "a VCF Automation event topic; '.post' is a top-level domain",
    "com.vmware.policy.deployment.lease": (
        "a VCF Automation policy type; '.lease' is a top-level domain"
    ),
}

_EXAMPLE_DOMAIN: Final = re.compile(r"(?:^|\.)example\.(?:com|org|net)$")
_IPV4: Final = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])")
_IPV6: Final = re.compile(r"(?<![\w:.])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])")
_HOST: Final = re.compile(
    r"(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+([a-z]{2,})(?![\w-])", re.IGNORECASE
)
_VENDOR_ID: Final = re.compile(
    r"(?<![\w-])(?:(?:vm|host|datastore|resgroup|dvportgroup|datacenter)-\d+"
    r"|resgroup-v\d+|domain-[cs]\d+|group-[a-z]\d+)(?![\w-])"
)
_UUID: Final = re.compile(
    r"(?<![0-9A-Fa-f-])[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f-])"
)


def _strings(value: object, path: str) -> Iterator[tuple[str, str]]:
    """Every string in *value*, top level and nested, with its dotted path."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item, f"{path}[]")


def _is_documentation_address(text: str) -> bool | None:
    """``True``/``False`` for an IP address, ``None`` when *text* is none."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    return any(address in network for network in DOCUMENTATION_NETWORKS)


def _is_fine_host(host: str) -> bool:
    host = host.lower()
    if _EXAMPLE_DOMAIN.search(host):
        return True
    return any(host == domain or host.endswith("." + domain) for domain in VENDOR_NAMES)


def real_looking_values(
    text: str, *, holds_a_version: bool = False, holds_a_path: bool = False
) -> list[str]:
    """The parts of *text* that look real. ``[]`` when it looks made up.

    A field whose name holds ``version`` may hold ``9.0.2.0``; that is a
    version, not an address, so IPv4 is not checked there. A field whose name
    holds ``path`` or ``file`` may hold ``release-notes.md``; that is a file
    name, not a host, so host names are not checked there.
    """
    if PLACEHOLDER.match(text):
        return []
    found: list[str] = []
    if not holds_a_version:
        found += [
            m.group() for m in _IPV4.finditer(text) if _is_documentation_address(m.group()) is False
        ]
    found += [
        m.group() for m in _IPV6.finditer(text) if _is_documentation_address(m.group()) is False
    ]
    if not holds_a_path:
        found += [
            m.group()
            for m in _HOST.finditer(text)
            if m.group(1).lower() in REAL_TOP_LEVEL_DOMAINS and not _is_fine_host(m.group())
        ]
    found += [m.group() for m in _VENDOR_ID.finditer(text)]
    found += [m.group() for m in _UUID.finditer(text) if set(m.group()) != {"0", "-"}]
    return found


def example_value_problems(example: Mapping[str, Any]) -> list[str]:
    """One message per real-looking value in *example*, naming its field."""
    problems: list[str] = []
    for path, text in _strings(example, ""):
        field = path.rsplit(".", 1)[-1].lower()
        values = real_looking_values(
            text,
            holds_a_version="version" in field,
            holds_a_path="path" in field or "file" in field,
        )
        problems.extend(
            f"{path or 'value'} holds {value!r}, which looks real; use a placeholder "
            "in angle brackets, such as <vm-id>"
            for value in values
        )
    return problems
