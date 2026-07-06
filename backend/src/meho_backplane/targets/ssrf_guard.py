# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""SSRF destination guard for operator-registered targets.

A :class:`~meho_backplane.targets.schemas.TargetCreate` /
:class:`~meho_backplane.targets.schemas.TargetUpdate` body names the
``host`` (and optionally ``fqdn``) the connector transport will later
dial with an operator-forwarded, Vault-resolved credential attached.
Without a destination screen, anything that can drive the create/update
API can point a target at ``127.0.0.1``, RFC 1918 space,
``169.254.169.254`` (cloud metadata), or their IPv6 analogues and have
the backplane deliver a credential there — the classic server-side
request forgery shape (OWASP SSRF Prevention Cheat Sheet).

This module is the single guard both enforcement layers share:

* **Create/update time** — the schema validators call
  :func:`assert_public_destination` so a non-public destination is a
  structured 422 at the API boundary (best-effort early feedback).
* **Connect time** — ``HttpConnector._http_client`` calls
  :func:`assert_public_destination_async` immediately before the pooled
  client is built/served, re-resolving the stored literal. This is the
  *enforcement* point: a DNS answer that changed after create (rebind),
  or a hostname that was unresolvable at create time, is caught here on
  every dispatch.

The rejection classes mirror the existing spec-fetch guard at
``operations/ingest/openapi.py`` (``_assert_fetchable_remote_url``):
:mod:`ipaddress` ``is_private`` / ``is_loopback`` / ``is_link_local`` /
``is_reserved`` / ``is_multicast`` / ``is_unspecified`` — covering at
minimum ``127.0.0.0/8``, ``10.0.0.0/8``, ``172.16.0.0/12``,
``192.168.0.0/16``, ``169.254.0.0/16``, ``::1``, ``fc00::/7``,
``fe80::/10``. That guard is a different sink and is deliberately not
modified or reused directly (it fails closed on unresolvable hostnames
and rejects with an ingest-specific error type); the *pattern* is
shared, per evoila-bosnia/meho-internal#153.

**Allowlist override.** MEHO is an on-prem product — operators
legitimately register vCenter/Harbor/NSX targets on RFC 1918 space. The
guard is therefore overridable via a single env var,
:data:`TARGET_SSRF_ALLOWLIST_ENV` (``MEHO_TARGET_SSRF_ALLOWLIST``): a
comma-separated list of CIDR ranges, bare IPs, and/or hostname literals.
An address inside an allowlisted range (or a hostname named verbatim) is
accepted even though it is non-public; everything else stays blocked, so
a deployment can opt its LAN back in without disabling the guard
globally. There is deliberately no weighting/DSL beyond this — the
simplest override that preserves the security property.

**Fail-open on unresolvable hostnames — by design.** A hostname that
does not resolve is allowed through at both layers: at create time the
backplane may simply not share the target's resolver view (split-horizon
DNS is normal on-prem), and at connect time httpx's own resolution fails
the dispatch naturally with a DNS error. The security property is not
weakened: the moment such a name *does* resolve, the connect-time
re-check screens the answer before any request is issued.

**No topology oracle.** The rejection message never echoes the resolved
address(es) — a caller probing hostnames through the create API must not
be able to use the guard as an internal-DNS oracle. The message names
only the env-var remediation.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from typing import Final

__all__ = [
    "TARGET_SSRF_ALLOWLIST_ENV",
    "TargetDestinationBlockedError",
    "assert_public_destination",
    "assert_public_destination_async",
]

#: Env var holding the operator-configured destination allowlist:
#: comma-separated CIDR ranges (``10.0.0.0/8``), bare IPs
#: (``192.168.7.10``), and/or hostname literals
#: (``vcenter.lab.internal``). Unset/empty = no exemptions (guard fully
#: on). Read per call — no process-lifetime cache — so tests and
#: hot-reconfigured deployments see the current value without a
#: cache-clear hook.
TARGET_SSRF_ALLOWLIST_ENV: Final[str] = "MEHO_TARGET_SSRF_ALLOWLIST"

_REMEDIATION: Final[str] = (
    "if this is a trusted internal destination on this deployment, add "
    f"its address range or hostname to {TARGET_SSRF_ALLOWLIST_ENV}"
)


class TargetDestinationBlockedError(ValueError):
    """A target destination is (or resolves to) a non-public address.

    Subclasses :class:`ValueError` so the schema validators can let it
    propagate straight into pydantic's validation machinery — FastAPI
    renders it as a structured 422. The connect path catches it
    explicitly and re-raises as a transport-level error (see
    ``HttpConnector._http_client``).
    """


def _parse_allowlist() -> tuple[
    tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    frozenset[str],
]:
    """Parse :data:`TARGET_SSRF_ALLOWLIST_ENV` into networks + hostnames.

    A token containing ``/`` must be a valid CIDR (``strict=False`` so
    ``10.0.0.1/8`` normalises rather than errors); a bare-IP token
    becomes its single-host network. Any other token is a hostname
    literal, matched case-insensitively with a trailing dot stripped.
    A malformed CIDR raises :class:`ValueError` naming the offending
    token — loud, because silently dropping an entry would re-block a
    destination the operator explicitly opted in (same fail-fast posture
    as ``VAULT_KV_TENANT_SCOPE_PREFIX`` validation in ``settings.py``).
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    hostnames: set[str] = set()
    for token in os.environ.get(TARGET_SSRF_ALLOWLIST_ENV, "").split(","):
        entry = token.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
            continue
        except ValueError:
            pass
        if "/" in entry:
            raise ValueError(
                f"{TARGET_SSRF_ALLOWLIST_ENV} entry {entry!r} is not a valid "
                "CIDR range; fix the deployment configuration"
            )
        hostnames.add(entry.rstrip(".").lower())
    return tuple(networks), frozenset(hostnames)


def _is_blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when *addr* falls in any non-public class the guard rejects."""
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _resolve_addrs(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Best-effort DNS resolution seam for hostname destinations.

    Module-level function (not inlined) so tests monkeypatch a single
    seam to simulate a hostname resolving into private space without
    real DNS traffic. Unresolvable hostnames return ``[]`` (fail-open —
    see the module docstring for why); a resolved entry that is not a
    parseable IP is skipped rather than fatal, since the remaining
    entries still get screened and the transport can only dial real
    addresses.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for _family, _type, _proto, _canonname, sockaddr in infos:
        try:
            addrs.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    return addrs


def assert_public_destination(host: str) -> None:
    """Reject *host* when it is, or resolves to, a non-public address.

    *host* may be an IPv4/IPv6 literal (bracketed IPv6 URL form
    accepted) or a hostname. IP literals are checked directly; hostnames
    are first matched against the allowlist's hostname entries (an
    allowlisted name is trusted verbatim — no resolution round-trip) and
    otherwise resolved via :func:`_resolve_addrs`, with **every**
    resolved address screened (any blocked, non-allowlisted candidate
    rejects, matching the ingest guard's posture).

    Raises:
        TargetDestinationBlockedError: The destination is non-public and
            not exempted by :data:`TARGET_SSRF_ALLOWLIST_ENV`. The
            message never includes the resolved address (no
            internal-topology oracle).
    """
    candidate = host.strip()
    if not candidate:
        return
    networks, hostnames = _parse_allowlist()
    literal = (
        candidate[1:-1] if candidate.startswith("[") and candidate.endswith("]") else candidate
    )
    try:
        addrs = [ipaddress.ip_address(literal)]
    except ValueError:
        if candidate.rstrip(".").lower() in hostnames:
            return
        addrs = _resolve_addrs(candidate)
    for addr in addrs:
        if not _is_blocked(addr):
            continue
        if any(addr in network for network in networks):
            continue
        raise TargetDestinationBlockedError(
            "target destination is not a public address; refusing it as a "
            f"server-side request forgery risk ({_REMEDIATION})"
        )


async def assert_public_destination_async(host: str) -> None:
    """Async wrapper for the connect-time re-check.

    Runs :func:`assert_public_destination` in a worker thread via
    :func:`asyncio.to_thread` because ``socket.getaddrinfo`` is a
    blocking syscall — on the dispatch hot path it must not stall the
    event loop for the duration of a DNS round-trip. The sync body is
    resolved at call time, so a test that monkeypatches
    :func:`_resolve_addrs` (or the allowlist env var) affects this path
    identically.
    """
    await asyncio.to_thread(assert_public_destination, host)
