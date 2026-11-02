# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What a real-looking value in an operation example is (contract rule 11).

An AI copies example values into real calls. So an example holds
placeholders such as ``<vm-id>``, documentation addresses (RFC 5737,
RFC 3849) and example host names (RFC 2606), never a value that looks like
a real system. Used by :mod:`tests.test_operation_contracts`.

A host name looks real when its last part is a common top-level domain
(``.com``, ``.local``, ...) or any two-letter country code (``.fr``, ``.ca``,
``.au``). A trailing dot, as in ``host.corp.local.``, does not hide a name.
A bare file name with a two-letter ending from :data:`FILE_ENDINGS`
(``notes.md``, ``docs/setup.sh``) is not a host name. It stops being a file
name when it has more than one dot (``nas.corp.pl``) or sits where a URL or
address puts its host (``https://notes.md/``, ``admin@setup.sh``,
``wiki.md:8443``).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Final

__all__ = ["FILE_ENDINGS", "PUBLIC_EXAMPLE_NAMES", "real_looking_parts"]

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
#: Common top-level domains that make a host name look real. Every two-letter
#: ending is a country code; :func:`_is_real_top_level_domain` covers them
#: all. ``.example``, ``.test`` and ``.invalid`` are reserved for examples, so
#: they are not here.
REAL_TOP_LEVEL_DOMAINS: Final[frozenset[str]] = frozenset(
    {"com", "net", "org", "local", "lan", "internal", "corp", "home", "intra"}
    | {"cloud", "dev", "app", "info", "biz", "gov", "mil", "edu", "int", "arpa", "pro"}
    | {"tech", "online", "site", "xyz", "systems", "solutions", "digital", "global"}
    | {"world", "live", "store", "shop", "ltd", "gmbh", "inc", "llc"}
)
#: Two-letter file endings that are also country codes. ``notes.md`` or
#: ``setup.sh`` in an example is a file name, not a host name, as long as
#: :func:`_is_file_name` says so.
FILE_ENDINGS: Final[frozenset[str]] = frozenset(
    {"md", "sh", "py", "go", "js", "ts", "tf", "rs", "cs", "rb", "pl", "pm", "ps", "so", "cc"}
)
EXAMPLE_DOMAINS: Final[tuple[str, ...]] = ("example.com", "example.net", "example.org")
_IN_ANGLE_BRACKETS: Final = re.compile(r"<[^<>]*>")
#: An address may end with a dot (end of a sentence), but not continue with
#: a fifth number.
_IPV4: Final = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?!\.?\w)")
_IPV6_PART: Final = re.compile(r"[0-9a-f.]*:[0-9a-f:.]*:[0-9a-f.]*", re.IGNORECASE)
#: A host name may end with the trailing dot of a fully qualified name.
_HOST_NAME: Final = re.compile(
    r"(?<![\w.-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}\.?(?![\w.-])",
    re.IGNORECASE,
)
#: What comes right before and after the host of a URL or address:
#: ``https://host``, ``user@host``, ``host/path``, ``host:443``.
_BEFORE_URL_HOST: Final = re.compile(r"(?://|@)$")
_AFTER_URL_HOST: Final = re.compile(r"^(?:/|:\d)")
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


def _is_real_top_level_domain(host: str) -> bool:
    """Whether the lower-case *host* ends with a real top-level domain.

    Every two-letter ending is a country code (``.fr``, ``.ca``, ``.au``).
    """
    top_level = host.rsplit(".", 1)[1]
    return top_level in REAL_TOP_LEVEL_DOMAINS or len(top_level) == 2


def _is_example_domain(host: str) -> bool:
    return any(host == domain or host.endswith(f".{domain}") for domain in EXAMPLE_DOMAINS)


def _is_file_name(text: str, match: re.Match[str]) -> bool:
    """Whether the host-name *match* in *text* is a file name such as ``notes.md``.

    A file name has one dot and an ending from :data:`FILE_ENDINGS`, and it
    does not sit where a URL or address puts its host: after ``://`` or
    ``@``, or before ``/`` or a port. ``nas.corp.pl`` has two dots, and
    ``https://notes.md/`` is a URL, so both are host names.
    """
    stem, _, ending = match.group(0).rstrip(".").rpartition(".")
    if "." in stem or ending.lower() not in FILE_ENDINGS:
        return False
    before, after = text[: match.start()], text[match.end() :]
    return not (_BEFORE_URL_HOST.search(before) or _AFTER_URL_HOST.match(after))


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
        name = match.group(0).rstrip(".")
        host = name.lower()
        if _is_file_name(text, match) or _is_example_domain(host):
            continue
        if _is_real_top_level_domain(host):
            found.append(name)
    found.extend(_VENDOR_ID.findall(text))
    found.extend(uuid for uuid in _UUID.findall(text) if uuid != _NIL_UUID)
    return [part for part in found if not _is_public_name(part)]
