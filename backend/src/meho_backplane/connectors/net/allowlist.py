# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Probe allowlist for the ``net.*`` network-diagnostics connector.

The ``net.*`` ops give the backplane (and, since they register
``safety_level="safe"``, an autonomous agent) a network vantage: they
open a socket toward an operator-named ``host``/``port``. Unbounded,
that is a recon primitive against everything the deployment can reach.
:data:`PROBE_ALLOWLIST_ENV` (``MEHO_NETDIAG_PROBE_ALLOWLIST``) is the
single floor that scopes it.

**Inverted semantics vs. the SSRF guard.** The SSRF guard
(``targets/ssrf_guard.py``) is *deny-non-public-minus-allowlist*: a
destination is dialable unless it resolves into non-public space that
the operator has not opted back in. This module is the opposite —
*allow-only-what-is-listed*: the parsed set is the **whole permitted
probe space**, and an **empty** value means **deny everything**, so the
connector is inert until an operator deliberately opts a range in. That
opposite default is why this parser is a deliberate sibling of
``ssrf_guard._parse_allowlist`` rather than a shared call: reusing the
SSRF guard verbatim would carry its permissive-when-unset default into a
surface where unset must mean closed.

There is intentionally no port dimension in v1 — the allowlist scopes
*hosts*, and a per-op ``timeout`` bounds the probe. A port-scoped
allowlist is a follow-up only if an operator needs it (#1177: one
closed-set config, no DSL).
"""

from __future__ import annotations

import ipaddress
import os
from typing import Final

__all__ = [
    "PROBE_ALLOWLIST_ENV",
    "ProbeNotAllowedError",
    "assert_probe_allowed",
    "parse_probe_allowlist",
]

#: Comma-separated CIDR ranges, bare IP literals, and/or hostname
#: literals naming the **entire** space ``net.*`` probes may dial.
#: Unset/empty = deny-all (the connector is inert). Read per call — no
#: process-lifetime cache — so tests and hot-reconfigured deployments
#: see the current value without a cache-clear hook (mirrors the SSRF
#: guard's read-per-call posture).
PROBE_ALLOWLIST_ENV: Final[str] = "MEHO_NETDIAG_PROBE_ALLOWLIST"


class ProbeNotAllowedError(ValueError):
    """A probe destination is not inside :data:`PROBE_ALLOWLIST_ENV`.

    Subclasses :class:`ValueError`. A ``net.*`` handler asked to dial its
    **initial** destination lets this **propagate**, and the dispatcher
    classifies it as the structured ``connector_probe_refused`` error
    (#2784). It is deliberately *not* folded into the return-failures
    contract: that contract's rationale is "a failed probe is the
    product", and a probe the allowlist refused never ran, so it produced
    no product. Emitting it as a reading (``connected=false``) made a
    reverted allowlist read as a down host — a Sensor asserting on the
    reading flipped ``critical`` instead of the truthful ``unknown``.
    ``net.http_probe``'s mid-chain redirect re-gate is the one caller
    that still catches it: there the prior hop *did* answer, so
    ``blocked_redirect`` stays a ``status="ok"`` observation.

    The message never echoes a resolved address (no internal-topology
    oracle). *host* carries the caller's own destination string verbatim
    — :func:`assert_probe_allowed` resolves nothing, so handing it back
    reveals nothing the caller did not supply. The dispatcher surfaces it
    as ``extras["host"]``, which keeps the connector's audit-visible-host
    foundation intact now that a refusal writes no ``raw_payload``.
    """

    def __init__(self, message: str, *, host: str | None = None) -> None:
        super().__init__(message)
        self.host = host


def parse_probe_allowlist() -> tuple[
    tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    frozenset[str],
]:
    """Parse :data:`PROBE_ALLOWLIST_ENV` into networks + hostnames.

    A token containing ``/`` must be a valid CIDR (``strict=False`` so
    ``10.0.0.1/8`` normalises rather than errors); a bare-IP token
    becomes its single-host network; any other token is a hostname
    literal, matched case-insensitively with a trailing dot stripped.
    A malformed CIDR raises :class:`ValueError` naming the offending
    token — loud, because silently dropping an entry would narrow the
    permitted probe space the operator explicitly opted in (same
    fail-fast posture as ``ssrf_guard._parse_allowlist``).
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    hostnames: set[str] = set()
    for token in os.environ.get(PROBE_ALLOWLIST_ENV, "").split(","):
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
                f"{PROBE_ALLOWLIST_ENV} entry {entry!r} is not a valid CIDR "
                "range; fix the deployment configuration"
            )
        hostnames.add(entry.rstrip(".").lower())
    return tuple(networks), frozenset(hostnames)


def assert_probe_allowed(host: str) -> None:
    """Raise :class:`ProbeNotAllowedError` unless *host* is allowlisted.

    Called by a ``net.*`` handler on the **exact host it is about to
    dial**, *before* any socket opens. The decision is absolute
    membership in :func:`parse_probe_allowlist`'s parsed set — public or
    private is irrelevant, only "listed or not":

    * empty allowlist → always refuse (the connector is inert);
    * an IP literal (bracketed IPv6 URL form accepted) → allowed iff it
      falls inside a listed network/CIDR/bare-IP;
    * a hostname → allowed iff it is listed **verbatim**. A name is not
      resolved-then-network-matched: a resolve-to-allow step would let a
      DNS answer that changed between the check and the dial (rebind)
      widen the permitted space, and it would couple the floor to the
      resolver's current view. Verbatim matching keeps the floor
      fail-closed and TOCTOU-free; an operator who wants a name probed
      lists the name (or its address range).

    Raises:
        ProbeNotAllowedError: *host* is empty, or not covered by the
            allowlist, or the allowlist is empty.
    """
    networks, hostnames = parse_probe_allowlist()
    if not networks and not hostnames:
        raise ProbeNotAllowedError(
            f"probe destination refused: {PROBE_ALLOWLIST_ENV} is empty, so "
            "the net.* connector is inert; add the range or hostname to probe",
            host=host,
        )
    candidate = host.strip()
    if not candidate:
        raise ProbeNotAllowedError("probe destination refused: empty host", host=host)
    literal = (
        candidate[1:-1] if candidate.startswith("[") and candidate.endswith("]") else candidate
    )
    try:
        addr = ipaddress.ip_address(literal)
    except ValueError:
        if candidate.rstrip(".").lower() in hostnames:
            return
        raise ProbeNotAllowedError(
            f"probe destination refused: host is not listed in {PROBE_ALLOWLIST_ENV}",
            host=host,
        ) from None
    if any(addr in network for network in networks):
        return
    raise ProbeNotAllowedError(
        f"probe destination refused: address is not listed in {PROBE_ALLOWLIST_ENV}",
        host=host,
    )
