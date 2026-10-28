# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pure helpers for the Holodeck 9.0 DNS name ops (#4066).

No I/O here: input checks, parsing of the dnsmasq ``hosts`` value, the
add / remove plans, and parsing of ``kubectl get pods`` and ``dig``
answers. The router steps live in :mod:`.ops_dns`.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any

__all__ = [
    "DEPLOYMENT",
    "NAME_MAX",
    "HolodeckDnsError",
    "bound_dns_entry",
    "canonical_address",
    "dig_answers",
    "dnsmasq_pods",
    "parse_hosts",
    "plan_add",
    "plan_remove",
    "public_rows",
]

#: The dnsmasq deployment in namespace ``default`` on a Holodeck 9.0 router.
DEPLOYMENT = "dnsmasq-dns-deployment"

_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_NAME_RE = re.compile(rf"{_LABEL}(\.{_LABEL})*")
NAME_MAX = 253


class HolodeckDnsError(ValueError):
    """A DNS op step failed or an input was refused.

    ``step`` names where it happened (``input``, ``read``, ``conflict``,
    ``save``, ``write``, ``verify``, ``restart``, ``wait``).
    """

    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"{step}: {message}")
        self.step = step


def bound_dns_entry(params: dict[str, Any]) -> tuple[str, str]:
    """Return ``(address, name)`` in canonical form, or raise.

    ``address`` must be an IPv4 or IPv6 address (not unspecified, not
    multicast); it is returned in :mod:`ipaddress` canonical form. ``name``
    must be a DNS name of letters, digits and ``-`` labels joined by
    ``.``, at most 253 characters, no trailing dot; it is returned in lower
    case (dnsmasq compares names without case).
    """
    raw_address = params.get("address")
    raw_name = params.get("name")
    try:
        address = ipaddress.ip_address(raw_address if isinstance(raw_address, str) else "")
    except ValueError:
        raise HolodeckDnsError("input", "address must be an IPv4 or IPv6 address") from None
    if address.is_unspecified or address.is_multicast:
        raise HolodeckDnsError("input", "address must not be unspecified or multicast")
    name = raw_name.lower() if isinstance(raw_name, str) else ""
    if len(name) > NAME_MAX or not _NAME_RE.fullmatch(name):
        raise HolodeckDnsError(
            "input", "name must be a DNS name: letters, digits and '-' labels joined by '.'"
        )
    return str(address), name


def canonical_address(token: str) -> str:
    try:
        return str(ipaddress.ip_address(token))
    except ValueError:
        return token


def parse_hosts(text: str) -> list[dict[str, Any]]:
    """Parse a dnsmasq hosts value into ``{index, address, names}`` rows.

    ``index`` is the position in ``text.splitlines(keepends=True)``. Blank
    lines and ``#`` comments are skipped; a trailing ``# ...`` is cut off.
    Addresses are in canonical form, names in lower case.
    """
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines(keepends=True)):
        tokens = line.split("#", 1)[0].split()
        if not tokens:
            continue
        rows.append(
            {
                "index": index,
                "address": canonical_address(tokens[0]),
                "names": [t.lower() for t in tokens[1:]],
            }
        )
    return rows


def public_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"address": r["address"], "names": r["names"]} for r in rows]


def plan_add(text: str, address: str, name: str) -> str | None:
    """Return the new hosts value, or ``None`` when the line is already there.

    Raises :class:`HolodeckDnsError` (step ``conflict``) when the name is
    already used with another address, or the address with another name.
    """
    rows = parse_hosts(text)
    exact = [r for r in rows if r["address"] == address and r["names"] == [name]]
    conflicts = [
        r for r in rows if r not in exact and (r["address"] == address or name in r["names"])
    ]
    if conflicts:
        raise HolodeckDnsError(
            "conflict",
            f"the name or the address is already used with another partner: "
            f"{json.dumps(public_rows(conflicts))}",
        )
    if exact:
        return None
    prefix = text if not text or text.endswith("\n") else text + "\n"
    return f"{prefix}{address} {name}\n"


def plan_remove(text: str, address: str, name: str) -> str | None:
    """Return the new hosts value, or ``None`` when the line is not there.

    Removes every line that is exactly ``<address> <name>``. When the pair
    is missing but the name or the address sits in another line, raises
    :class:`HolodeckDnsError` (step ``conflict``): that line is not one this
    op wrote, so a person must change it.
    """
    rows = parse_hosts(text)
    exact = [r for r in rows if r["address"] == address and r["names"] == [name]]
    if not exact:
        partial = [r for r in rows if r["address"] == address or name in r["names"]]
        if partial:
            raise HolodeckDnsError(
                "conflict",
                f"no line is exactly '{address} {name}'; these lines use the name "
                f"or the address: {json.dumps(public_rows(partial))}",
            )
        return None
    drop = {r["index"] for r in exact}
    lines = text.splitlines(keepends=True)
    return "".join(line for i, line in enumerate(lines) if i not in drop)


def dig_answers(stdout: str) -> list[str]:
    return [
        line.strip()
        for line in stdout.splitlines()
        if line.strip() and not line.strip().startswith(";")
    ]


def dnsmasq_pods(payload: Any) -> list[dict[str, Any]]:
    """Return the deployment's pods as ``{name, hash, deleting, ip}``."""
    pods: list[dict[str, Any]] = []
    items = payload.get("items") if isinstance(payload, dict) else None
    for item in items or []:
        meta = item.get("metadata") or {}
        pod_hash = (meta.get("labels") or {}).get("pod-template-hash")
        if not pod_hash or not str(meta.get("name", "")).startswith(f"{DEPLOYMENT}-{pod_hash}-"):
            continue
        pods.append(
            {
                "name": meta["name"],
                "hash": pod_hash,
                "deleting": bool(meta.get("deletionTimestamp")),
                "ip": (item.get("status") or {}).get("podIP"),
            }
        )
    return pods
