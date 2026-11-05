# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Probe allowlist for the ``net.*`` network-diagnostics connector.

The ``net.*`` ops give the backplane (and, since they register
``safety_level="safe"``, an autonomous agent) a network vantage: they
open a socket toward an operator-named ``host``/``port``. Unbounded,
that is a recon primitive against everything the deployment can reach.
:data:`PROBE_ALLOWLIST_ENV` (``MEHO_NETDIAG_PROBE_ALLOWLIST``) is the
instance-wide floor that scopes it.

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

**Per-tenant bound (#3498).** ``net.*`` ops are targetless (the
destination is a param, ``target=None``) and ``safe`` +
``requires_approval=False``, so an agent principal auto-runs them with no
grant and they bypass the per-target tenant boundary entirely. The
instance-wide :data:`PROBE_ALLOWLIST_ENV` is a single knob: on a shared
instance it cannot be narrowed for one (untrusted) tenant without
removing the probe scope other tenants' sensors depend on.
:data:`TENANT_PROBE_ALLOWLIST_ENV`
(``MEHO_NETDIAG_PROBE_ALLOWLIST_TENANTS``) is a per-tenant map layered
**in front of** the instance floor:

* a tenant **absent** from the map inherits the instance allowlist —
  the default, so existing deployments are unchanged;
* a tenant **present** with a populated scope is bounded to exactly that
  scope (the instance allowlist is not consulted for it);
* a tenant **present** with an *empty* scope (``<uuid>=``) has **every**
  targetless ``net.*`` probe denied.

The map keys on the caller's ``tenant_id`` (threaded into
:func:`assert_probe_allowed` by every handler from its ``Operator``), so
the bound holds for an agent principal exactly as for an operator — it is
tenant-scoped, not principal-scoped. Like the instance allowlist it is a
deployment-config knob (a chart value → ConfigMap → env), read per call
with no process-lifetime cache; there is no agent-surface or MCP path to
it (postulate 5).

There is intentionally no port dimension in v1 — the allowlist scopes
*hosts*, and a per-op ``timeout`` bounds the probe. A port-scoped
allowlist is a follow-up only if an operator needs it (#1177: one
closed-set config, no DSL).
"""

from __future__ import annotations

import ipaddress
import os
from typing import Final
from uuid import UUID

__all__ = [
    "PROBE_ALLOWLIST_ENV",
    "TENANT_PROBE_ALLOWLIST_ENV",
    "ProbeNotAllowedError",
    "assert_probe_allowed",
    "parse_probe_allowlist",
    "parse_tenant_probe_allowlist",
]

#: Comma-separated CIDR ranges, bare IP literals, and/or hostname
#: literals naming the **entire** space ``net.*`` probes may dial.
#: Unset/empty = deny-all (the connector is inert). Read per call — no
#: process-lifetime cache — so tests and hot-reconfigured deployments
#: see the current value without a cache-clear hook (mirrors the SSRF
#: guard's read-per-call posture).
PROBE_ALLOWLIST_ENV: Final[str] = "MEHO_NETDIAG_PROBE_ALLOWLIST"

#: Per-tenant probe-scope map (#3498). Semicolon-separated
#: ``<tenant-uuid>=<comma-separated CIDRs/IPs/hostnames>`` entries; an
#: entry's value uses the same token grammar as
#: :data:`PROBE_ALLOWLIST_ENV`. A tenant absent from the map inherits the
#: instance allowlist; a tenant present with an empty value
#: (``<uuid>=``) has every targetless ``net.*`` probe denied. Unset/empty
#: ⇒ no per-tenant bounds, every tenant inherits the instance allowlist
#: (the default — existing deployments unchanged). Read per call, same as
#: :data:`PROBE_ALLOWLIST_ENV`.
TENANT_PROBE_ALLOWLIST_ENV: Final[str] = "MEHO_NETDIAG_PROBE_ALLOWLIST_TENANTS"

#: The parsed shape of one allowlist scope: (networks, hostnames).
_Scope = tuple[
    tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
    frozenset[str],
]


class ProbeNotAllowedError(ValueError):
    """A probe destination is not inside the caller's permitted probe scope.

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

    The refusal may come from the instance-wide :data:`PROBE_ALLOWLIST_ENV`
    or from the caller tenant's :data:`TENANT_PROBE_ALLOWLIST_ENV` scope
    (#3498); the message names which, so the operator knows which knob to
    widen. Either way it never echoes a resolved address (no
    internal-topology oracle). *host* carries the caller's own destination
    string verbatim — :func:`assert_probe_allowed` resolves nothing, so
    handing it back reveals nothing the caller did not supply. The
    dispatcher surfaces it as ``extras["host"]``, which keeps the
    connector's audit-visible-host foundation intact now that a refusal
    writes no ``raw_payload``.
    """

    def __init__(self, message: str, *, host: str | None = None) -> None:
        super().__init__(message)
        self.host = host


def _parse_tokens(raw: str, *, env_label: str) -> _Scope:
    """Parse a comma-separated allowlist token string into networks + hostnames.

    A token containing ``/`` must be a valid CIDR (``strict=False`` so
    ``10.0.0.1/8`` normalises rather than errors); a bare-IP token
    becomes its single-host network; any other token is a hostname
    literal, matched case-insensitively with a trailing dot stripped.
    A malformed CIDR raises :class:`ValueError` naming the offending
    token and *env_label* — loud, because silently dropping an entry would
    narrow the permitted probe space the operator explicitly opted in
    (same fail-fast posture as ``ssrf_guard._parse_allowlist``). Shared by
    the instance allowlist and each per-tenant scope so both grammars stay
    identical.
    """
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    hostnames: set[str] = set()
    for token in raw.split(","):
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
                f"{env_label} entry {entry!r} is not a valid CIDR "
                "range; fix the deployment configuration"
            )
        hostnames.add(entry.rstrip(".").lower())
    return tuple(networks), frozenset(hostnames)


def parse_probe_allowlist() -> _Scope:
    """Parse :data:`PROBE_ALLOWLIST_ENV` into networks + hostnames."""
    return _parse_tokens(os.environ.get(PROBE_ALLOWLIST_ENV, ""), env_label=PROBE_ALLOWLIST_ENV)


def parse_tenant_probe_allowlist() -> dict[str, _Scope]:
    """Parse :data:`TENANT_PROBE_ALLOWLIST_ENV` into a per-tenant scope map.

    Entries are semicolon-separated; each is ``<tenant-uuid>=<tokens>``
    where ``<tokens>`` follows the same comma-separated grammar as
    :data:`PROBE_ALLOWLIST_ENV`. Keys are normalised to the canonical
    dashed-lowercase UUID form (``str(UUID(...))``) so a lookup by an
    ``Operator.tenant_id`` matches regardless of the casing the operator
    typed. An entry with an empty value (``<uuid>=``) parses to an empty
    scope, which :func:`assert_probe_allowed` reads as deny-all for that
    tenant. Unset/empty ⇒ an empty map (no per-tenant bounds).

    A malformed entry (missing ``=``, a non-UUID key, or a bad CIDR in
    the value) raises :class:`ValueError` naming the offending piece —
    the same fail-closed-loud posture as :func:`_parse_tokens`: a broken
    security config must not silently widen the probe space.
    """
    scopes: dict[str, _Scope] = {}
    for entry in os.environ.get(TENANT_PROBE_ALLOWLIST_ENV, "").split(";"):
        chunk = entry.strip()
        if not chunk:
            continue
        key, sep, value = chunk.partition("=")
        if not sep:
            raise ValueError(
                f"{TENANT_PROBE_ALLOWLIST_ENV} entry {chunk!r} is not "
                "'<tenant-uuid>=<comma-separated hosts/CIDRs>'; fix the "
                "deployment configuration"
            )
        tenant_key = key.strip()
        try:
            normalised = str(UUID(tenant_key))
        except ValueError:
            raise ValueError(
                f"{TENANT_PROBE_ALLOWLIST_ENV} tenant key {tenant_key!r} is "
                "not a UUID; fix the deployment configuration"
            ) from None
        scopes[normalised] = _parse_tokens(value, env_label=TENANT_PROBE_ALLOWLIST_ENV)
    return scopes


def _tenant_scope_for(tenant_id: UUID | str | None) -> _Scope | None:
    """Return the caller tenant's explicit probe scope, or ``None`` to inherit.

    ``None`` means "no per-tenant bound applies" — the caller passed no
    tenant, the map is unset, or this tenant has no entry — and
    :func:`assert_probe_allowed` falls through to the instance allowlist.
    A returned scope (possibly empty) means the tenant is bounded and the
    instance allowlist is **not** consulted for it.
    """
    if tenant_id is None:
        return None
    scopes = parse_tenant_probe_allowlist()
    if not scopes:
        return None
    try:
        key = str(UUID(str(tenant_id)))
    except ValueError:
        # An unparseable tenant id cannot match a normalised map key;
        # inherit the instance allowlist rather than invent a scope.
        return None
    return scopes.get(key)


def _scope_contains(candidate: str, scope: _Scope) -> bool:
    """Whether *candidate* (a non-empty host string) is inside *scope*.

    An IP literal (bracketed IPv6 URL form accepted) matches iff it falls
    inside a listed network/CIDR/bare-IP; a hostname matches iff it is
    listed verbatim (case-insensitive, trailing dot stripped). An empty
    scope contains nothing. A name is never resolved-then-network-matched
    (rebind/TOCTOU floor — see :func:`assert_probe_allowed`).
    """
    networks, hostnames = scope
    if not networks and not hostnames:
        return False
    literal = (
        candidate[1:-1] if candidate.startswith("[") and candidate.endswith("]") else candidate
    )
    try:
        addr = ipaddress.ip_address(literal)
    except ValueError:
        return candidate.rstrip(".").lower() in hostnames
    return any(addr in network for network in networks)


def assert_probe_allowed(host: str, *, tenant_id: UUID | str | None = None) -> None:
    """Raise :class:`ProbeNotAllowedError` unless *host* is allowlisted.

    Called by a ``net.*`` handler on the **exact host it is about to
    dial**, *before* any socket opens. *tenant_id* is the caller's
    ``Operator.tenant_id`` (#3498); the per-tenant scope in
    :data:`TENANT_PROBE_ALLOWLIST_ENV` is consulted **first**:

    * the tenant has an explicit scope → *host* must be inside it (an
      empty tenant scope denies every probe for that tenant); the instance
      allowlist is not consulted;
    * the tenant has no scope (or *tenant_id* is ``None``) → the
      instance-wide :data:`PROBE_ALLOWLIST_ENV` governs, unchanged.

    In both cases the decision is absolute membership — public or private
    is irrelevant, only "listed or not":

    * empty scope → always refuse (deny-all);
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
            applicable scope, or that scope is empty.
    """
    tenant_scope = _tenant_scope_for(tenant_id)
    if tenant_scope is not None:
        _assert_within_tenant_scope(host, tenant_scope)
        return

    # No per-tenant bound applies — the instance-wide allowlist governs
    # (behaviour is byte-for-byte what it was before #3498).
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
    if _scope_contains(candidate, (networks, hostnames)):
        return
    listed_as = "address" if _looks_like_ip(candidate) else "host"
    raise ProbeNotAllowedError(
        f"probe destination refused: {listed_as} is not listed in {PROBE_ALLOWLIST_ENV}",
        host=host,
    )


def _assert_within_tenant_scope(host: str, scope: _Scope) -> None:
    """Raise unless *host* is inside the caller tenant's explicit probe scope.

    Mirrors the instance-allowlist decision in :func:`assert_probe_allowed`
    but names :data:`TENANT_PROBE_ALLOWLIST_ENV` in the refusal so the
    operator knows the per-tenant bound (not the instance floor) refused,
    and treats an empty scope as deny-all for the tenant. Messages stay
    address-free; the destination rides ``host`` for the audit row.
    """
    networks, hostnames = scope
    candidate = host.strip()
    if not candidate:
        raise ProbeNotAllowedError("probe destination refused: empty host", host=host)
    if not networks and not hostnames:
        raise ProbeNotAllowedError(
            f"probe destination refused: this tenant's {TENANT_PROBE_ALLOWLIST_ENV} "
            "scope is empty, so every targetless net.* probe is denied for the tenant",
            host=host,
        )
    if _scope_contains(candidate, scope):
        return
    listed_as = "address" if _looks_like_ip(candidate) else "host"
    raise ProbeNotAllowedError(
        f"probe destination refused: {listed_as} is not within this tenant's "
        f"{TENANT_PROBE_ALLOWLIST_ENV} probe scope",
        host=host,
    )


def _looks_like_ip(candidate: str) -> bool:
    """Whether *candidate* (already ``.strip()``-ed) parses as an IP literal.

    Only used to pick the ``address`` vs ``host`` wording in a refusal
    message; the bracketed IPv6 URL form is unwrapped first to match
    :func:`_scope_contains`.
    """
    literal = (
        candidate[1:-1] if candidate.startswith("[") and candidate.endswith("]") else candidate
    )
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        return False
    return True
