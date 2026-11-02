# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-coded VMware Cloud Director wire constants — the reconcile-lane surface.

VMware Cloud Director (vCD) is the legacy standalone product evoila migrates
customers *from* (initiative `evoila/meho#3056
<https://github.com/evoila/meho/issues/3056>`_, task #3057). Broadcom publishes
**no committable OpenAPI** for vCD's full REST surface — the classic ``/api/*``
query service is bundled into the UI JS at build time and the appliance serves
no ``/cloudapi/1.0.0/openapi*`` (evidenced 404 on the same vCloud-Director-derived
surface via the VCFA 9.0.2 Holodeck probe; no vCD-specific probe run) — so the
connector is a **typed**
read core (CLAUDE.md postulate 1: "no usable spec → typed"), the same shape the
``fleet-lcm`` (#3047) and ``vcf-automation`` provider-plane cores ship with.

Every request path the connector dispatches is declared here as a module-level
``_*_PATH`` string constant, so the spec-reconcile lane
(:mod:`tests.test_connectors_vcd_spec_reconcile`) can introspect the **live**
constants (the #2944 introspect-live-constants pattern — never a hardcoded
mirror that can drift) and pin the exact hand-coded surface. A running cell
does serve its own ``/cloudapi`` file (Swagger 2.0, pinned on the spec shelf as
``vmware-cloud-director-10.6/cloudapi-39.2.json``), so the lane also checks
every ``/cloudapi`` path against it. The ``/api`` paths have no file and stay in
the manifest pin only.

Surfaces
--------

vCD exposes two co-existing REST surfaces the connector reads, both
authenticated by the one provider Bearer token minted at
:data:`_SESSIONS_PROVIDER_PATH`:

* **Modern ``/cloudapi/1.0.0/*``** — clean JSON collections
  (:data:`_ORGS_PATH`). In-repo verified: the ``vcf-automation`` provider plane
  reads ``GET /cloudapi/1.0.0/orgs`` against the same vCloud-Director-derived
  surface.
* **Classic ``/api/*`` query service** (:data:`_QUERY_PATH`) — the uniform,
  provider/system-scoped inventory mechanism. One path, one paging model, a
  ``type=`` selector per entity (:data:`_QT_ADMIN_VM` &c., the
  ``go-vcloud-director`` SDK's canonical query-type constants). This is the
  migration-inventory backbone: a provider session over the admin query types
  sees the whole estate.

The two surfaces need **different ``Accept`` media types** (vCD content
negotiation): the classic ``/api/*`` paths take the versioned
``application/*+json`` form, the modern ``/cloudapi/*`` paths the
``application/json`` form. :func:`accept_for_path` picks by prefix — the
``vcf-automation`` ``provider_accept_for_path`` split (#517).
"""

from __future__ import annotations

import re
from urllib.parse import quote

__all__ = [
    "ORG_URN_PATTERN",
    "QUERY_RECORDS_FORMAT",
    "VCD_TOKEN_HEADER",
    "accept_for_path",
    "classic_api_accept",
    "cloudapi_accept",
    "org_path",
]

# -- Request paths reconciled by the manifest lane --------------------------
# Unauthenticated supported-versions probe (reachability + API surface). vCD
# serves this without auth (it is the version-discovery entry point), so the
# connector's fingerprint reads it without minting a session.
_VERSIONS_PATH = "/api/versions"
# Provider (System-org) session-create endpoint. POSTed with HTTP Basic; the
# minted JWT rides back in the X-VMWARE-VCLOUD-ACCESS-TOKEN response header.
_SESSIONS_PROVIDER_PATH = "/cloudapi/1.0.0/sessions/provider"
# Modern cloudapi organization collection (clean JSON, provider sees all orgs).
# GET lists the orgs; POST creates one.
_ORGS_PATH = "/cloudapi/1.0.0/orgs"
# One organization, by its urn:vcloud:org: id: GET reads it, PUT changes it,
# DELETE removes it. Fill ``{org_urn}`` with :func:`org_path` only.
_ORG_PATH = "/cloudapi/1.0.0/orgs/{org_urn}"
# Classic query service — the uniform inventory path; the entity is selected by
# the ``type=`` query param (see the _QT_* constants), never by the path.
_QUERY_PATH = "/api/query"

# -- Query-service selectors (go-vcloud-director SDK canonical constants) ----
# System-admin ("admin*") variants so a provider/System session enumerates every
# org's resources (the migration-inventory contract: see the whole estate, not
# one tenant). ``edgeGateway`` has no admin variant — a System admin sees all
# edge gateways via the plain type — but ``task`` does, so tasks use ``adminTask``
# (the cross-org, all-tenants view) to match the whole-estate contract.
_QT_ADMIN_ORG_VDC = "adminOrgVdc"
_QT_ADMIN_VAPP = "adminVApp"
_QT_ADMIN_VM = "adminVM"
_QT_ADMIN_CATALOG = "adminCatalog"
_QT_EDGE_GATEWAY = "edgeGateway"
_QT_ADMIN_TASK = "adminTask"

#: Query-service response format. ``records`` returns flattened attribute rows
#: (names, status, hrefs) — the right shape for inventory + JSONFlux drill-in;
#: ``references`` (the alternative) returns bare hrefs only.
QUERY_RECORDS_FORMAT = "records"

#: Provider-login response header carrying the session JWT. HTTP/2 lowercases
#: header names on the wire; httpx indexing is case-insensitive, so the
#: constant stays in the vendor's canonical mixed-case form (the
#: ``vcf-automation`` ``PROVIDER_TOKEN_HEADER`` precedent).
VCD_TOKEN_HEADER = "X-VMWARE-VCLOUD-ACCESS-TOKEN"

#: Default vCD API version for the ``Accept`` header's ``version=`` token.
#: 38.0 is served by vCD 10.5 and 10.6 (10.6 keeps back-compat for the prior
#: ~3 API versions), so it covers the common migration-source range with the
#: live 10.6 provider lab working out of the box. A target may override via
#: ``extras["vcd_api_version"]``; version *negotiation* (read the max
#: non-deprecated version from ``GET /api/versions`` and use it) is the
#: documented robustness follow-up (see the connector docstring).
_DEFAULT_VCD_API_VERSION = "38.0"


#: The form of an organization id: ``urn:vcloud:org:`` and a UUID.
ORG_URN_PATTERN = re.compile(
    r"urn:vcloud:org:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def org_path(org_urn: str) -> str:
    """Return :data:`_ORG_PATH` for *org_urn*, or raise :exc:`ValueError`.

    The id goes into the URL path, so it is checked against
    :data:`ORG_URN_PATTERN` first and percent-encoded (the colons stay).
    """
    if not ORG_URN_PATTERN.fullmatch(org_urn):
        raise ValueError(
            f"org_id {org_urn!r} is not an organization id. It must look like "
            "urn:vcloud:org:<uuid>; vcd.org.list returns it in the id field."
        )
    return _ORG_PATH.format(org_urn=quote(org_urn, safe=":"))


def cloudapi_accept(api_version: str) -> str:
    """``Accept`` for the modern ``/cloudapi/1.0.0/*`` JSON surface."""
    return f"application/json;version={api_version}"


def classic_api_accept(api_version: str) -> str:
    """``Accept`` for the classic ``/api/*`` (query service) JSON surface."""
    return f"application/*+json;version={api_version}"


def accept_for_path(path: str, api_version: str) -> str:
    """Return the vCD ``Accept`` media type for *path*.

    Classic ``/api/*`` paths (including the query service) take the versioned
    ``application/*+json`` form; everything else (the modern ``/cloudapi/*``
    surface) takes ``application/json``. The one provider Bearer token
    authenticates both. Mirrors ``vcf-automation``'s ``provider_accept_for_path``
    (#517) — a genuine wire requirement, not cosmetic: the wrong ``Accept``
    makes vCD answer XML (classic) or reject with a 406.
    """
    if path.startswith("/api/"):
        return classic_api_accept(api_version)
    return cloudapi_accept(api_version)
