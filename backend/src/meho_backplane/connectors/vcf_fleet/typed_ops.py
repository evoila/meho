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

Three ops ship here: one per audited bucket and one for the vendor files:

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
* ``fleet.api_file.get`` — ``GET {path}`` — reads one API description
  page or file the appliance serves (the Lifecycle APIs under
  ``/api/swagger-ui/``, the Content Management APIs under
  ``/lcm/cms/api/swagger-ui/``, and the Swagger or OpenAPI files those
  pages load). The body lives in
  :mod:`meho_backplane.connectors.vcf_fleet.api_file`. It exists so the
  vendor's ``/lcm`` files, which no vendor download ships, can be pinned.

Seven more ops are in :mod:`.lifecycle_ops` (metadata) and
:mod:`.lifecycle` (handlers): the lifecycle requests (list, read, retry), one
environment, a product inventory sync, the appliance health and the
datacenters with their vCenters. Two of them are changes and wait for a
person's approval.

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

from typing import Any

from meho_backplane.connectors.vcf_fleet._typed_op import FleetTypedOp
from meho_backplane.connectors.vcf_fleet.lifecycle_ops import LIFECYCLE_OPS
from meho_backplane.connectors.vcf_fleet.typed_ops_locker import (
    FLEET_LOCKER_OPS,
    FLEET_LOCKER_WHEN_TO_USE_BY_GROUP,
)

__all__ = [
    "FLEET_TYPED_OPS",
    "FLEET_TYPED_WHEN_TO_USE_BY_GROUP",
    "FleetTypedOp",
]


#: Curated ``when_to_use`` blurbs per typed group. ``register_typed_operation``
#: requires a non-empty string whenever ``group_key`` is set; the registrar
#: looks each op's ``group_key`` up here.
FLEET_TYPED_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "fleet-about": (
        "Two reads: the VCF Fleet (vRSLCM) appliance's API version, product version and "
        "build (fleet.about; on VCF 9.0 builds it fails with HTTP 500 although the "
        "appliance is reachable), and its health: version, disk use and the state of each "
        "service (fleet.health)."
    ),
    "fleet-inventory": (
        "Reads: the Fleet environments and the products deployed in each "
        "(fleet.environment.list), one environment with its products, versions and nodes "
        "(fleet.environment.get), and the datacenters with their vCenters "
        "(fleet.datacenter.list). One change: start an inventory sync of one product "
        "(fleet.product.inventory_sync); it waits for a person's approval."
    ),
    "fleet-requests": (
        "The lifecycle requests the appliance runs (deployments, upgrades, syncs, day-2 "
        "actions). Reads: list requests (fleet.request.list) and read one with the error "
        "causes of a failure (fleet.request.get). One change: retry a failed request "
        "(fleet.request.retry); it waits for a person's approval."
    ),
    "fleet-api-docs": (
        "One read: the API descriptions a running Aria Suite Lifecycle or VCF Fleet "
        "appliance serves on its /lcm surface (fleet.api_file.get). It reads the "
        "Lifecycle APIs page under /api/swagger-ui/, the Content Management APIs page "
        "under /lcm/cms/api/swagger-ui/, or a Swagger or OpenAPI file they load, and "
        "returns the file's size, SHA-256 and the appliance version, with the text in "
        "parts behind a result handle, so a vendor API file can be pinned and compared. "
        "It refuses every other path."
    ),
    **FLEET_LOCKER_WHEN_TO_USE_BY_GROUP,
}

_PART_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "index": {"type": "integer", "description": "Position of the part, from 0."},
        "text": {"type": "string", "description": "The part's text, at most 64 KiB."},
    },
}


_ABOUT = FleetTypedOp(
    op_id="fleet.about",
    handler_attr="about",
    summary="Read the VCF Fleet appliance's API version, product version and build.",
    description=(
        "Reads GET /lcm/lcops/api/v2/about: API version, product version, build "
        "number and release date. On VCF 9.0 builds this call fails with HTTP 500; "
        "the appliance is still reachable. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "apiVersion": {"type": "string", "description": "The LCM API version."},
            "productVersion": {"type": "string", "description": "The product version."},
            "buildNumber": {"type": "string", "description": "The build number."},
            "releaseDate": {"type": "string", "description": "The release date."},
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
    summary="List the Fleet environments with the products deployed in each.",
    description=(
        "Lists every Fleet environment via GET /lcm/lcops/api/v2/environments. An "
        "environment groups one or more product deployments (vRA, vROps, vRLI, "
        "vIDM and more). Each entry has its id, name, status and products. "
        "Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "environments": {
                "type": "array",
                "description": "The Fleet environments. MEHO puts the vendor list under this key.",
                "items": {
                    "type": "object",
                    "properties": {
                        "environmentId": {"type": "string", "description": "The environment id."},
                        "environmentName": {
                            "type": "string",
                            "description": "The environment name.",
                        },
                        "environmentStatus": {
                            "type": "string",
                            "description": (
                                "The deployment state, for example DEPLOY_SUCCESSFUL or "
                                "DEPLOY_FAILED."
                            ),
                        },
                        "products": {
                            "type": "array",
                            "description": "The products deployed in the environment.",
                            "items": {"type": "object"},
                        },
                    },
                    "additionalProperties": True,
                },
            },
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
            "snapshot. Call fleet.environment.get for one environment's nodes. For a "
            "DEPLOY_FAILED environment, find the failed request with fleet.request.list "
            "(outcome failed) and read it with fleet.request.get."
        ),
    },
)


_API_FILE_GET = FleetTypedOp(
    op_id="fleet.api_file.get",
    handler_attr="api_file_get",
    summary="Read one API documentation page or API description file the /lcm appliance serves.",
    description=(
        "Reads one documentation page or Swagger/OpenAPI file from the /lcm appliance and "
        "returns its path, HTTP status, content type, size, SHA-256 and the appliance "
        "version. The text comes back in parts behind a result handle. Only the "
        "documentation paths and the usual API file paths are allowed."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path on the appliance, starting with '/'. Allowed: the pages "
                    "/api/swagger-ui.html and /lcm/cms/api/swagger-ui.html, any path under "
                    "the folders /api/swagger-ui/ and /lcm/cms/api/swagger-ui/, or a "
                    "Swagger/OpenAPI file path (a part named api-docs, swagger-resources or "
                    "swagger-config, or a name ending in swagger.json, swagger.yaml, "
                    "openapi.json or openapi.yaml). A query string may hold only group and "
                    "apiVersion. No '..', '//', ';', scheme or host."
                ),
            },
        },
        "required": ["path"],
        "additionalProperties": False,
        "examples": [{"path": "<path>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "The path that was read."},
            "status": {"type": "integer", "description": "HTTP status the appliance returned."},
            "content_type": {
                "type": ["string", "null"],
                "description": "Content-Type header of the answer.",
            },
            "size": {"type": "integer", "description": "Size of the checked copy in bytes."},
            "sha256": {"type": "string", "description": "SHA-256 of the checked copy, hex."},
            "secrets_replaced": {
                "type": "integer",
                "description": (
                    "How many values under a secret name (password, token, licence key, "
                    "...) were replaced by [REDACTED:param_name] in the copy."
                ),
            },
            "version": {
                "type": ["string", "null"],
                "description": "Product version from GET /lcm/lcops/api/v2/about.",
            },
            "build": {
                "type": ["string", "null"],
                "description": "Build number from GET /lcm/lcops/api/v2/about, when it has one.",
            },
            "version_error": {
                "type": ["string", "null"],
                "description": (
                    "Why the version read failed (on VCF 9.0 builds it answers HTTP 500); "
                    "null when it worked."
                ),
            },
            "lcm_api_version": {
                "type": ["string", "null"],
                "description": "The Lcm-API-Version header the appliance sent with the file.",
            },
            "redirect_to": {
                "type": ["string", "null"],
                "description": (
                    "Path of a redirect answer, resolved against the path that was read; "
                    "read that path next. Not followed."
                ),
            },
            "parts": {"type": "integer", "description": "How many text parts there are."},
            "items": {
                "type": "array",
                "description": (
                    "The text parts of the checked copy, in order. Joined, they are the copy."
                ),
                "items": _PART_ITEM_SCHEMA,
            },
        },
    },
    group_key="fleet-api-docs",
    tags=("read-only", "fleet", "vcf", "api-docs", "documentation"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to read the API documentation an Aria Suite Lifecycle 8.x or VCF Fleet "
            "appliance serves on its /lcm surface: the Lifecycle APIs page under "
            "/api/swagger-ui/ (or /api/swagger-ui.html before 8.18 Patch 3), the Content "
            "Management APIs page under /lcm/cms/api/swagger-ui/ (or "
            "/lcm/cms/api/swagger-ui.html), or one of the Swagger/OpenAPI files those pages "
            "load, for example to pin a vendor API file or to check which API calls an "
            "appliance version serves."
        ),
        "output_shape": (
            "{path, status, content_type, size, sha256, secrets_replaced, version, build, "
            "version_error, lcm_api_version, redirect_to, parts, items: [{index, text}]}. "
            "items hold the checked copy of the file in parts of at most 64 KiB, in order; "
            "a long file is behind a result handle. The copy is the parsed file written "
            "back (JSON, YAML, XML or HTML) with every value under a secret name replaced "
            "by [REDACTED:param_name]; secrets_replaced counts them. Join the parts in "
            "order to rebuild the copy; its SHA-256 equals sha256. On VCF 9.0 builds version "
            "and build are null and version_error names the HTTP 500 of the about call; "
            "lcm_api_version still names the API version."
        ),
        "next_step": (
            "If redirect_to is set, call again with that path. If status is not 200, "
            "read the text for the appliance's message. To rebuild the file, page the "
            "result handle with result_query in index order and join the text fields. "
            "A refused path means the path is outside the rule; fix the path. A file that "
            "does not parse, or is JavaScript, CSS or plain text, is refused; report it "
            "instead of retrying. A read that stops because the secret-redaction rules "
            "would change the copy cannot return it; report it instead of retrying."
        ),
        "result_scalars": {
            "keys": [
                "path",
                "status",
                "content_type",
                "size",
                "sha256",
                "secrets_replaced",
                "version",
                "build",
                "version_error",
                "lcm_api_version",
                "redirect_to",
                "parts",
            ]
        },
    },
)


#: The typed ops :class:`VcfFleetConnector` registers at lifespan startup:
#: the about probe, the environment list, the API description read, then the
#: request, environment, inventory sync, health and datacenter ops, then the
#: locker and product-certificate ops (``typed_ops_locker``).
FLEET_TYPED_OPS: tuple[FleetTypedOp, ...] = (
    _ABOUT,
    _ENVIRONMENT_LIST,
    _API_FILE_GET,
    *LIFECYCLE_OPS,
    *FLEET_LOCKER_OPS,
)
