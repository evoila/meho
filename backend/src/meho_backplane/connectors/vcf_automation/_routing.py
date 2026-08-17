# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Plane classification, vhost validation, and protocol constants.

Split out from :mod:`.connector` to keep that module within the
file-size budget. The contents here are pure helpers + module
constants -- no class state, no I/O -- so they live cleanly outside
the connector class. Verified against ``scripts/vcf-automation.sh``
(2026-05-21 snapshot, line references inline below).
"""

from __future__ import annotations

import ipaddress
from typing import Any, Literal

from meho_backplane.connectors.schemas import AuthModel

__all__ = [
    "PROVIDER_CLASSIC_API_ACCEPT",
    "PROVIDER_CLOUDAPI_ACCEPT",
    "PROVIDER_SESSION_PATH",
    "PROVIDER_TOKEN_HEADER",
    "PROVIDER_VERSION_PATH",
    "TENANT_ACCEPT",
    "TENANT_SESSION_PATH",
    "TENANT_VERSION_PATH",
    "Plane",
    "VcfAutomationConfigurationError",
    "compose_base_url",
    "is_acceptable_auth_model",
    "plane_for_path",
    "provider_accept_for_path",
    "vhost_header",
]

# Per-plane login endpoints. Verified against scripts/vcf-automation.sh
# (provider login: line 381; tenant login: line 439).
PROVIDER_SESSION_PATH = "/cloudapi/1.0.0/sessions/provider"
TENANT_SESSION_PATH = "/iaas/api/login"

# Per-plane unauthenticated version probes. Verified against
# scripts/vcf-automation.sh (provider: line 252 GET /api/versions XML;
# tenant: line 309 GET /iaas/api/about JSON). Both self-describe the
# API surface without exercising the auth flow.
PROVIDER_VERSION_PATH = "/api/versions"
TENANT_VERSION_PATH = "/iaas/api/about"

# Provider login response header carrying the JWT (verified against
# scripts/vcf-automation.sh line 395). HTTP/2 lowercases header names
# on the wire; httpx's Headers indexing is case-insensitive, so the
# constant is preserved in the canonical mixed-case form.
PROVIDER_TOKEN_HEADER = "X-VMWARE-VCLOUD-ACCESS-TOKEN"

# Provider plane Accept media types -- path-family dependent. The
# split between Tm (/cloudapi/*) and classic vCD (/api/*) is the #517
# finding in the consumer repo: 40.0 is the highest non-deprecated
# classic vCD version on VCFA 9.0 (validated 2026-05-17).
PROVIDER_CLOUDAPI_ACCEPT = "application/json;version=9.0.0"
PROVIDER_CLASSIC_API_ACCEPT = "application/*+json;version=40.0"

# Tenant plane Accept media type -- plain JSON; no version negotiation.
TENANT_ACCEPT = "application/json"

Plane = Literal["provider", "tenant"]


class VcfAutomationConfigurationError(RuntimeError):
    """Raised when a target's configuration prevents the connector from running.

    The primary trigger is a target reached by IP with no ``fqdn`` set:
    VCFA enforces strict vhost routing and would return 404 on every
    path post-login otherwise (see :class:`.VcfAutomationConnector`
    docstring section on vhost routing). Subclassing :exc:`RuntimeError`
    keeps the connector's existing ``except (httpx.HTTPError, OSError,
    RuntimeError)`` chains catching the case cleanly.
    """


def is_acceptable_auth_model(value: Any) -> bool:
    """Return ``True`` iff *value* is the SHARED_SERVICE_ACCOUNT mode or unset.

    Accepts the enum member, the equivalent string, and ``None`` (the
    pre-G0.3 "auth_model column not yet populated" sentinel). Any
    other value (``"per_user"``, ``"impersonation"``, a typo, an int)
    is rejected by the caller. Same predicate the NSX / SDDC Manager /
    vSphere precedents use.
    """
    if value is None:
        return True
    if value is AuthModel.SHARED_SERVICE_ACCOUNT:
        return True
    return bool(value == AuthModel.SHARED_SERVICE_ACCOUNT.value)


def _is_ip_literal(host: str) -> bool:
    """Return ``True`` iff *host* parses as an IPv4 or IPv6 literal.

    IPv6 bracket-wrapped forms (``[::1]``) are accepted --
    :class:`ipaddress.ip_address` rejects them, so one matched pair of
    brackets is stripped before parsing.
    """
    candidate = host
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def plane_for_path(path: str) -> Plane:
    """Classify a request path into one of the two auth planes.

    The split is unambiguous: ``/iaas/api/*`` is the tenant plane;
    everything else (``/cloudapi/*``, ``/api/*``, anything that looks
    like the vCloud-Director-derived surface) is the provider plane.
    """
    if path.startswith("/iaas/api/"):
        return "tenant"
    return "provider"


def provider_accept_for_path(path: str) -> str:
    """Return the provider-plane Accept media type for *path*.

    Path-family-dependent per the consumer wrapper (#517 in the
    consumer repo, line 414-420 of scripts/vcf-automation.sh):
    classic vCD paths (``/api/*``) need the versioned
    ``application/*+json;version=40.0`` form; everything else under
    ``/cloudapi/*`` uses the Tm ``application/json;version=9.0.0``
    form. The provider Bearer JWT authenticates both surfaces.
    """
    if path.startswith("/api/"):
        return PROVIDER_CLASSIC_API_ACCEPT
    return PROVIDER_CLOUDAPI_ACCEPT


def _port_suffix(port: int | None) -> str:
    """Return ``":{port}"`` for a non-default HTTPS port, else ``""``.

    Shared by :func:`compose_base_url` (the dialled authority) and
    :func:`vhost_header` (the ``Host:`` authority) so both render the
    port identically -- matching httpx's own ``Host:``-header derivation,
    which appends the port only when it is not the scheme default (443).
    """
    return f":{port}" if port and port != 443 else ""


def compose_base_url(target_name: str, host: str, port: int | None, fqdn: str | None) -> str:
    """Build the per-target base URL -- always the dialled ``host``.

    VCFA enforces strict vhost routing, but the vhost belongs in the
    per-request ``Host:`` header and TLS SNI (see :func:`vhost_header`
    and :meth:`.connector.VcfAutomationConnector._request_extensions`),
    **not** in the pooled client's ``base_url``. So the base URL is
    always ``https://{host}[:port]`` -- the reachable address the
    transport dials (an IP behind a NAT alias, or an FQDN) -- and
    ``fqdn`` is presented per-request. Baking ``fqdn`` into ``base_url``
    is what made "connect by IP, route by vhost" structurally
    unreachable and let the connector dial an address the SSRF guard
    never screened (evoila/meho#2863); it also could not disambiguate
    several appliances that share one vhost FQDN behind distinct NAT
    aliases.

    The one shape still refused at construction time is an IP-literal
    *host* with no *fqdn*: there is no vhost to present, so every path
    would 404 with an empty body before the application sees it. Raising
    :exc:`VcfAutomationConfigurationError` here surfaces a clear
    configuration message rather than a post-login 404 storm.
    """
    if not fqdn and _is_ip_literal(host):
        raise VcfAutomationConfigurationError(
            f"vcf-automation target {target_name!r} is reached by IP ({host!r}) "
            "but has no fqdn set. VCFA enforces strict vhost routing, so the "
            "connector needs a vhost to present as the Host: header -- an IP "
            "alone matches no vhost and returns 404 on every path. Set "
            "target.fqdn (CLI: --fqdn; targets.yaml: fqdn:) to the appliance's "
            "canonical FQDN; the connector still dials this host address."
        )
    return f"https://{host}{_port_suffix(port)}"


def vhost_header(fqdn: str | None, port: int | None = None) -> dict[str, str]:
    """Return the per-request ``Host:`` header presenting the vhost, or ``{}``.

    When *fqdn* is set, VCFA's strict vhost routing needs it as the
    ``Host:`` header even though the transport dials ``target.host``
    (:func:`compose_base_url`). The port is appended on the same
    non-default-port rule httpx uses when it derives ``Host:`` from a
    ``base_url``, so a target on a non-443 port renders
    ``Host: {fqdn}:{port}`` -- byte-identical to the pre-#2863 shape that
    put the FQDN in ``base_url``. When *fqdn* is unset, returns an empty
    mapping so httpx derives ``Host:`` from ``base_url`` (the host, which
    in that shape already carries the right vhost).
    """
    if not fqdn:
        return {}
    return {"Host": f"{fqdn}{_port_suffix(port)}"}
