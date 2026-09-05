# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Hand-coded vRealize Automation 8.x wire constants — the reconcile-lane surface.

vRealize Automation 8.x (vRA 8) is the **legacy** VMware automation product evoila
migrates customers *from* — the direct predecessor of the Broadcom-era **VCF
Automation 9.x** (:mod:`~meho_backplane.connectors.vcf_automation`, ``vcfa-rest``).
Both are the same product line, renamed across a major-version rebuild, so this
connector is the **legacy dual-impl** of ``product="vcfa"`` (``impl_id="vcfa-vra8"``,
band ``>=8.0,<9.0``), fingerprint-resolved against the modern ``vcfa-rest``
(``>=9.0,<10.0``) — the second real two-impl case after ``fleet`` (initiative
`evoila/meho#3056 <https://github.com/evoila/meho/issues/3056>`_, task #3058,
policy #3033/#3038).

Why typed (not generic / ingested)
----------------------------------

``vmware/vra-sdk-go`` (Apache-2.0) publishes machine-readable specs for the vRA 8
services (IaaS / Service Broker / Blueprint / Project), but they are **Swagger
2.0**, and MEHO's ingest parser accepts OpenAPI 3.0/3.1 only (#2090). So the
surface cannot be operator-ingested as a generic connector; the read core is
**typed** (CLAUDE.md postulate 1: "no *usable* spec → typed"), the same shape the
sibling ``vcf-automation`` tenant plane, ``fleet-lcm`` (#3047), and ``vcd`` (#3057)
ship with.

Every request path the connector dispatches is declared here as a module-level
``_*_PATH`` string constant, so the spec-reconcile lane
(:mod:`tests.test_connectors_vra8_spec_reconcile`) introspects the **live**
constants (the #2944 introspect-live-constants pattern — never a hardcoded mirror
that can drift) and pins the exact hand-coded surface. Because the vendor spec is
Swagger 2.0 (OA3-only ingest can't consume it), that lane is a **manifest pin +
evidenced exclusion** (the ``vcd`` / ``vcf-automation``-provider precedent), not a
served-op-id compare — see ``docs/decisions/spec-reconcile-guards-standard.md``.

Auth surface (two-step token exchange)
--------------------------------------

vRA 8 mints a bearer via a **two-step** exchange (distinct from VCFA 9's
``/cloudapi/1.0.0/sessions/provider`` + ``/oauth/*`` flow — the divergence that
justifies a separate impl):

1. :data:`_CSP_LOGIN_PATH` — ``POST /csp/gateway/am/api/login?access_token`` with a
   JSON ``{username, password, domain?}`` body → a long-lived ``refresh_token``
   (snake_case) in the response body.
2. :data:`_IAAS_LOGIN_PATH` — ``POST /iaas/api/login`` with ``{refreshToken}``
   (camelCase) → a short-lived (8h) bearer ``token`` in the response body.

The one bearer authenticates **every** vRA 8 service (``/iaas``, ``/deployment``,
``/blueprint``, ``/catalog``) uniformly via ``Authorization: Bearer <token>`` — all
the service specs declare the same ``Authorization``-header security scheme. Reads
send ``Accept: application/json``; no per-surface Accept negotiation is needed
(unlike ``vcd``).

Read paths (migration-inventory core)
-------------------------------------

All GET, all ``application/json``. The lists are server-paged (``$top`` / ``$skip``
OData params, a self-describing ``{content, totalElements}`` wrapper), so the
connector forwards those and the op ``llm_instructions`` tell the agent to page —
never a silent truncation. Sorting/filtering is done through the JSONFlux result
handle, not vendor query params (MEHO postulate 6).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["ODATA_LIST_PARAM_KEYS", "odata_list_query"]

# -- Auth (two-step token exchange) -----------------------------------------
# CSP identity service: username/password -> refresh_token. POSTed by
# ``._auth.vra8_login`` with the literal valueless ``?access_token`` query flag
# (added at the call site). Not in vra-sdk-go (CSP identity is outside the SDK),
# so the reconcile lane pins it as an evidenced exclusion.
_CSP_LOGIN_PATH = "/csp/gateway/am/api/login"
# IaaS token exchange: {refreshToken} -> {tokenType, token}. The bearer authenticates
# every vRA 8 service.
_IAAS_LOGIN_PATH = "/iaas/api/login"

# -- Fingerprint probe ------------------------------------------------------
# IaaS about endpoint — {latestApiVersion, supportedApis}. Read unauthenticated for
# reachability (the vcf-automation tenant-plane probe reads the same path); it
# reports the *API* version (a date label, e.g. 2021-07-15), not the product build,
# so the connector does not fabricate a product version from it. Doubles as the
# ``vcfa.vra8.about`` op (collapses onto one GET:/iaas/api/about by value).
_ABOUT_PATH = "/iaas/api/about"

# -- Read paths (migration-inventory core) ----------------------------------
# Projects — the tenancy/quota boundary. The IaaS surface (documented public API),
# not the lower-level /project-service (which paginates page/size, not $top/$skip).
_PROJECTS_PATH = "/iaas/api/projects"
# Deployments — the running workloads. The Service Broker surface (rich record with
# resources / lastRequest / inprogressRequests), not the thin /iaas/api/deployments.
_DEPLOYMENTS_PATH = "/deployment/api/deployments"
# One deployment's detail — carries the embedded lastRequest / inprogressRequests
# (the quiescence signal) and resources. There is no top-level list-all-requests
# endpoint in vRA 8, so request state is read here, per deployment.
_DEPLOYMENT_DETAIL_PATH = "/deployment/api/deployments/{deployment_id}"
# Blueprints — the Cloud Assembly IaC definitions a migration must recreate.
_BLUEPRINTS_PATH = "/blueprint/api/blueprints"
# Catalog items — the Service Broker self-service offerings. The admin view
# (whole-estate, not caller-project-scoped) matches the operator migration-inventory
# contract: see everything, like vCD's admin* query types.
_CATALOG_ITEMS_PATH = "/catalog/api/admin/items"

#: The OData pagination params the list reads forward — ``$top`` / ``$skip``, which
#: every vRA 8 list surface accepts with identical (lowercase) casing and which bound
#: the fetch size before the dispatcher reduces the result to a JSONFlux handle.
#: **Sorting/filtering is deliberately NOT forwarded to the vendor:** the result
#: handle is the agent's post-fetch surface, and ``result_query`` pages it by
#: ``offset`` / ``limit`` only — server-side sort/filter over a handle is not
#: available. A vendor ``$orderby`` would be inconsistent anyway: vRA 8's casing
#: for it is *not* uniform (IaaS ``/iaas/api/projects`` uses ``$orderBy``, the
#: Service Broker / Blueprint services ``$orderby``), so forwarding one spelling
#: would silently drop the sort on the other. The agent pages the handle and
#: reads rows client-side instead.
ODATA_LIST_PARAM_KEYS: tuple[str, ...] = ("$top", "$skip")


def odata_list_query(params: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract the forwarded OData ``$top`` / ``$skip`` pagination query params.

    Returns ``None`` when neither is present so the request carries no query string
    and vRA 8 applies its appliance-default pagination (the first page). The response
    wrapper self-describes ``totalElements`` / ``numberOfElements``, so a default
    (unpaged) call is never a silent truncation — the op ``llm_instructions`` tell the
    agent to page via these params; sorting/filtering is done through the dispatcher's
    JSONFlux handle, not the vendor query (see :data:`ODATA_LIST_PARAM_KEYS`).
    """
    query = {key: params[key] for key in ODATA_LIST_PARAM_KEYS if params.get(key) is not None}
    return query or None
