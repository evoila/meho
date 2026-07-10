# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed read ops for :class:`VcfFleetConnector` (T4 · #2304, Initiative #2266).

The G3.6 v0.5 core historically exposed the Fleet operational surface as
``is_enabled`` curation over **ingested** ``endpoint_descriptor`` rows —
the operator had to ingest the vRSLCM-derived Fleet OpenAPI spec before
any op could dispatch. That spec is the #2272 datetime-crash artifact, so
the operational surface was hostage to a crash-prone ingest.

This module converts the **audited read set** (the adopter's real Fleet
usage from the #2294 T0 audit, row 21: *"about/health probe; component
inventory read ('what's deployed')"*) to **typed** ops that dispatch as
``source_kind="typed"`` off the connector's existing hand-rolled HTTP
Basic session against the LCM-local user store — no catalog ingest, no
spec dependency.

Two ops ship here, one per audited bucket:

* ``fleet.about`` — ``GET /lcm/lcops/api/v2/about`` — the appliance
  identity / health probe. **Known regression:** this endpoint returns
  HTTP 500 in VCF 9.0 builds (the connector's own probe therefore reads
  ``Lcm-API-Version`` off ``/lcm/lcops/api/v2/datacenters`` instead — see
  :class:`~meho_backplane.connectors.vcf_fleet.connector.VcfFleetConnector`).
  The op is converted for parity with the audited "about/health probe"
  ask; its ``llm_instructions`` carry the 9.0-500 warning and the
  reachability-probe fallback so the agent degrades gracefully.
* ``fleet.environment.list`` — ``GET /lcm/lcops/api/v2/environments`` —
  the component inventory ("what's deployed"). Every product deployment
  (vRA / vROps / vRLI / vIDM / …) lives under a Fleet environment, and
  each environment carries a ``products[]`` summary inline, so a single
  no-argument call answers "what has Fleet provisioned" — the audited
  lab-bookkeeping surface.

The remaining six curated ops (datacenter/vcenter list, environment
detail, product list, request list/detail) are **declined** from typed
conversion — they are outside the adopter's audited set. The decline
rationale is posted on #2304; the ingested breadth catalog still covers
their browse case until T7 retires the curation apparatus.

Mold
----

The dataclass + tuple + ``register_operations`` walk mirrors the
argocd read core (:mod:`meho_backplane.connectors.argocd.ops`) and the
vSphere typed reads (:mod:`meho_backplane.connectors.vmware_rest.typed_ops`):
each op names a ``handler_attr`` resolved against
:class:`VcfFleetConnector` at registration time, so the persisted
``handler_ref`` round-trips through the dispatcher's
:func:`~meho_backplane.operations._handler_resolve.import_handler` walk as
a ``module.ClassName.method`` dotted path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "FLEET_TYPED_OPS",
    "FLEET_TYPED_WHEN_TO_USE_BY_GROUP",
    "FleetTypedOp",
]


@dataclass(frozen=True)
class FleetTypedOp:
    """Metadata for one typed Fleet op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the registrar can splat the dataclass into the helper
    without per-op boilerplate. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.vcf_fleet.connector.VcfFleetConnector`
    exposing the async handler; the registrar resolves the bound method
    against the class at registration time so the dispatcher can recover
    the callable from the persisted ``module.ClassName.method`` path.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


#: Curated ``when_to_use`` blurbs per typed group. ``register_typed_operation``
#: requires a non-empty string whenever ``group_key`` is set; the registrar
#: looks each op's ``group_key`` up here.
FLEET_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "fleet-about": (
        "Use to read the VCF Fleet (vRSLCM) appliance's identity / health via "
        "fleet.about — API version, product version, build. WARNING: in VCF "
        "9.0 builds the /about endpoint returns HTTP 500; when that happens the "
        "appliance is still reachable — the connector's probe reads the "
        "Lcm-API-Version header off the datacenters surface, and the product "
        "version is cross-sourced from SDDC Manager's /v1/vcf-services entry. "
        "The right group to confirm which Fleet instance a target points at."
    ),
    "fleet-inventory": (
        "Use to read the Fleet component inventory — what products Fleet has "
        "deployed — via fleet.environment.list. Every product deployment (vRA, "
        "vROps, vRLI, vIDM, …) lives under a Fleet environment; the call "
        "returns every environment with its deployment status and an inline "
        "products[] summary, so one no-argument call answers 'what has Fleet "
        "provisioned' for lab bookkeeping and inventory snapshots."
    ),
}


_ABOUT = FleetTypedOp(
    op_id="fleet.about",
    handler_attr="about",
    summary="Read the VCF Fleet appliance identity / health (about probe).",
    description=(
        "Reads the vRSLCM appliance identity via GET /lcm/lcops/api/v2/about — "
        "API version, product version, build number, release date. KNOWN "
        "REGRESSION: in VCF 9.0 builds this endpoint returns HTTP 500; the call "
        "then surfaces as a connector_error and the caller should treat the "
        "appliance as reachable-but-about-broken (the connector's own probe "
        "confirms reachability off the datacenters surface). safety_level=safe, "
        "read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "apiVersion": {"type": "string"},
            "productVersion": {"type": "string"},
            "buildNumber": {"type": "string"},
            "releaseDate": {"type": "string"},
        },
        "additionalProperties": True,
    },
    group_key="fleet-about",
    tags=("read-only", "fleet", "vcf", "probe"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to confirm which Fleet instance a target points at — API "
            "version, product version, build. KNOWN REGRESSION: in VCF 9.0 "
            "builds this endpoint returns HTTP 500. When the call fails with a "
            "500, do not retry; the appliance is still reachable — the "
            "connector's probe reads the Lcm-API-Version header off the "
            "datacenters surface, and the product version cross-sources from "
            "SDDC Manager's /v1/vcf-services entry."
        ),
        "output_shape": (
            "On success: {apiVersion, productVersion, buildNumber, releaseDate}. "
            "On the 9.0 HTTP 500 regression: a connector_error result carrying "
            "the appliance's errorCode / errorMessage."
        ),
        "next_step": (
            "If 200, proceed with the intended Fleet workflow. If 500 (VCF 9.0), "
            "rely on the connector fingerprint/probe for reachability and "
            "cross-source the product version from SDDC Manager rather than "
            "retrying fleet.about."
        ),
    },
)


_ENVIRONMENT_LIST = FleetTypedOp(
    op_id="fleet.environment.list",
    handler_attr="environment_list",
    summary="List Fleet environments with their deployed products (inventory).",
    description=(
        "Lists every Fleet-managed environment via GET "
        "/lcm/lcops/api/v2/environments — the component inventory of what "
        "Fleet has deployed. An environment is the unit Fleet uses to group "
        "one or more product deployments (vRA, vROps, vRLI, vIDM, …); each "
        "entry carries environmentId, environmentName, environmentStatus "
        "(DEPLOY_SUCCESSFUL / DEPLOY_FAILED / …) and an inline products[] "
        "summary. Returns the array under an 'environments' key. "
        "safety_level=safe, read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "environments": {"type": "array"},
        },
        "additionalProperties": True,
    },
    group_key="fleet-inventory",
    tags=("read-only", "fleet", "vcf", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to answer 'what has Fleet deployed' / 'what environments has "
            "Fleet provisioned' — the primary component-inventory read. No "
            "arguments; returns every environment with its status and an inline "
            "products[] summary."
        ),
        "output_shape": (
            "{environments: [Environment, ...]}. Each Environment carries "
            "environmentId, environmentName, environmentStatus, and a "
            "products[] sub-array with per-product id/version summaries."
        ),
        "next_step": (
            "Surface each environment's products[] versions for the inventory "
            "snapshot. For a DEPLOY_FAILED environment, the deeper per-request "
            "diagnostics remain on the ingested breadth catalog (not part of "
            "the typed audited set)."
        ),
    },
)


#: The typed read ops :class:`VcfFleetConnector` registers at lifespan
#: startup — the #2304 audited set (about/health probe + component
#: inventory). Ordered probe → inventory to match the operator's typical
#: "is it up, and what's on it" drill path.
FLEET_TYPED_OPS: tuple[FleetTypedOp, ...] = (
    _ABOUT,
    _ENVIRONMENT_LIST,
)
