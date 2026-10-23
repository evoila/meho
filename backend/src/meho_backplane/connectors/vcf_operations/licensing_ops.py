# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Licence-management operations of the VCF Operations 9.x connector.

VCF 9 licences are managed in VCF Operations. These six typed operations
read and change them:

* ``vrops.licensing.registration.status`` — is VCF Operations registered
  (connected or disconnected mode), its ids, the next usage-report date.
* ``vrops.licensing.server.list`` — the licence servers VCF Operations knows,
  with their registration and connection state.
* ``vrops.licensing.entitlement.query`` — the licences VCF Operations holds,
  and which licence server holds each.
* ``vrops.licensing.vcenter.query`` — the licence state of each vCenter
  (evaluation or licensed, end date).
* ``vrops.licensing.server.query`` — the licence servers as the licence store
  sees them.
* ``vrops.licensing.assign`` — assign licences to a vCenter. A change: it
  waits until a person approves it.

The calls are not published by the vendor (see
:mod:`~meho_backplane.connectors.vcf_operations.licensing_handlers`). VCF 9
licensing exists only on VCF Operations 9.x, so only the 9.x connector
registers these operations; the 8.x connector does not.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vcf_operations._response_schemas import (
    VROPS_LICENSING_ENTITLEMENT_QUERY_RESPONSE,
    VROPS_LICENSING_PAYLOAD_RESPONSE,
    VROPS_LICENSING_VCENTER_QUERY_RESPONSE,
)
from meho_backplane.connectors.vcf_operations.typed_ops import VropsTypedOp

__all__ = [
    "VROPS_LICENSING_ASSIGN_OP",
    "VROPS_LICENSING_OPS",
    "VROPS_LICENSING_WHEN_TO_USE_BY_GROUP",
]

_LICENSING_GROUP_KEY = "vrops-licensing"

_NOT_PUBLISHED = "The vendor does not publish this call."

_EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

_READ_TAGS: tuple[str, ...] = ("read-only", "vrops", "vmware", "licensing")

VROPS_LICENSING_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _LICENSING_GROUP_KEY: (
        "Use for VCF 9 licences in VCF Operations 9.x: is VCF Operations registered, "
        "which licence servers it knows, which licences it holds, the licence state "
        "and end date of each vCenter, and assigning licences to a vCenter. The "
        "assign call is a change and waits for a person's approval; the rest only read."
    ),
}


VROPS_LICENSING_REGISTRATION_STATUS_OP = VropsTypedOp(
    op_id="vrops.licensing.registration.status",
    handler_attr="licensing_registration_status",
    summary="Read the licence registration of VCF Operations: mode, ids, next usage report.",
    description=(
        "Reads GET /suite-api/internal/extension/vcf-license-cloud-integration/"
        f"registration/status. {_NOT_PUBLISHED} Read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema=VROPS_LICENSING_PAYLOAD_RESPONSE,
    group_key=_LICENSING_GROUP_KEY,
    tags=(*_READ_TAGS, "registration"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call first when planning a licence change: it shows whether VCF Operations "
            "is registered, in connected or disconnected mode, and when the next usage "
            "report is due."
        ),
        "parameter_hints": {},
        "output_shape": "{payload: <the vendor answer, unchanged>}.",
        "next_step": (
            "Read vrops.licensing.server.list for the licence servers, then "
            "vrops.licensing.entitlement.query for the licences."
        ),
    },
)


VROPS_LICENSING_SERVER_LIST_OP = VropsTypedOp(
    op_id="vrops.licensing.server.list",
    handler_attr="licensing_server_list",
    summary="List the licence servers VCF Operations knows, with registration and connection.",
    description=(
        "Reads GET /suite-api/internal/extension/vcf-license-cloud-integration/"
        f"license-servers. {_NOT_PUBLISHED} Read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema=VROPS_LICENSING_PAYLOAD_RESPONSE,
    group_key=_LICENSING_GROUP_KEY,
    tags=(*_READ_TAGS, "license-server"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which licence servers are registered and connected, for "
            "example after a licence server was replaced."
        ),
        "parameter_hints": {},
        "output_shape": (
            "{payload: <the vendor answer, unchanged>}. Seen per server: a registration "
            "state (for example ENTITLED or DELETED), connectivityStatus (for example "
            "CONNECTED) and licensedAssets."
        ),
        "next_step": (
            "Check that the server you expect is ENTITLED and CONNECTED, then read "
            "vrops.licensing.entitlement.query."
        ),
    },
)


VROPS_LICENSING_ENTITLEMENT_QUERY_OP = VropsTypedOp(
    op_id="vrops.licensing.entitlement.query",
    handler_attr="licensing_entitlement_query",
    summary="List the licences VCF Operations holds and can assign.",
    description=(
        "Sends POST /suite-api/internal/extension/vcf-entitlement/entitlements/query. "
        "Returns each licence with its id, name, product, capacity and usage. "
        f"{_NOT_PUBLISHED} Read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema=VROPS_LICENSING_ENTITLEMENT_QUERY_RESPONSE,
    group_key=_LICENSING_GROUP_KEY,
    tags=(*_READ_TAGS, "entitlement"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which licences can be assigned, and to check a licence "
            "import: the UI can say a licence was imported while this list is empty."
        ),
        "parameter_hints": {},
        "output_shape": (
            "{entitlementWithVcentersDetails: [{entitlementInfo: {id, name, "
            "productDisplayName, type, usage, capacity}}], totalCount}. totalCount 0 "
            "means the licence store is empty."
        ),
        "next_step": (
            "Pass a licence id to vrops.licensing.assign, or read "
            "vrops.licensing.vcenter.query for the vCenters."
        ),
    },
)


VROPS_LICENSING_VCENTER_QUERY_OP = VropsTypedOp(
    op_id="vrops.licensing.vcenter.query",
    handler_attr="licensing_vcenter_query",
    summary="Read the licence state of each vCenter: evaluation or licensed, and the end date.",
    description=(
        "Sends POST /suite-api/internal/extension/vcf-entitlement/vcenter-systems/query "
        "with optional page and pageSize (MEHO sends pageSize 1000 when none is given). "
        "Returns each vCenter with its licence state, "
        f"end date and usage per product family. {_NOT_PUBLISHED} Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "page": {
                "type": "integer",
                "minimum": 0,
                "description": "0-based page number.",
            },
            "pageSize": {
                "type": "integer",
                "minimum": 1,
                "description": ("Number of vCenters per page. MEHO sends 1000 when none is given."),
            },
        },
        "additionalProperties": False,
        "examples": [{"page": 0, "pageSize": 20}],
    },
    response_schema=VROPS_LICENSING_VCENTER_QUERY_RESPONSE,
    group_key=_LICENSING_GROUP_KEY,
    tags=(*_READ_TAGS, "vcenter"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see when each vCenter's evaluation ends, and to check a licence "
            "assignment: a vCenter is fully licensed when its state is LICENSED and "
            "unlicensedUsage is 0 for every product family."
        ),
        "parameter_hints": {
            "pageSize": (
                "Leave it out to get up to 1000 vCenters in one call. If a page has "
                "exactly pageSize entries, read the next page (page + 1) until a page "
                "has fewer entries."
            ),
        },
        "output_shape": (
            "{vcenterSystems: [{id, host, vcfAdapterName, version, state, "
            "expirationTimestamp, productFamilies: [{productFamily, type, "
            "licensedUsage, unlicensedUsage, licenses: [{allocationId, name}]}]}]}. "
            "Some builds use an older shape: id, host and vcfAdapterName sit under "
            "vcenterInfo in each entry (vcenterInfo.id, vcenterInfo.host). No total "
            "count is known for this answer; page by the number of entries."
        ),
        "next_step": (
            "Pass id and host (or vcenterInfo.id and vcenterInfo.host) of a vCenter "
            "that is not LICENSED to vrops.licensing.assign."
        ),
    },
)


VROPS_LICENSING_SERVER_QUERY_OP = VropsTypedOp(
    op_id="vrops.licensing.server.query",
    handler_attr="licensing_server_query",
    summary="List the licence servers as the licence store of VCF Operations sees them.",
    description=(
        "Sends POST /suite-api/internal/extension/vcf-entitlement/license-servers/query. "
        f"{_NOT_PUBLISHED} Read-only."
    ),
    parameter_schema=_EMPTY_PARAMS,
    response_schema=VROPS_LICENSING_PAYLOAD_RESPONSE,
    group_key=_LICENSING_GROUP_KEY,
    tags=(*_READ_TAGS, "license-server"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which licence server holds the licences, for example when a "
            "licence looks free but belongs to a server that was removed."
        ),
        "parameter_hints": {},
        "output_shape": "{payload: <the vendor answer, unchanged>}.",
        "next_step": (
            "Compare with vrops.licensing.server.list and vrops.licensing.entitlement.query."
        ),
    },
)


VROPS_LICENSING_ASSIGN_OP = VropsTypedOp(
    op_id="vrops.licensing.assign",
    handler_attr="licensing_assign",
    summary="Assign licences to a vCenter in VCF Operations. Waits for a person's approval.",
    description=(
        "Sends POST /suite-api/internal/extension/vcf-entitlement/assign for one vCenter "
        "with one or more licence ids. The licence covers the vCenter and its hosts. "
        f"{_NOT_PUBLISHED} A change."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "vcenterId": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The vCenter id, from vrops.licensing.vcenter.query (id or vcenterInfo.id)."
                ),
            },
            "vcenterHost": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The vCenter address, from vrops.licensing.vcenter.query "
                    "(host or vcenterInfo.host)."
                ),
            },
            "licenseIds": {
                "type": "array",
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "description": "One licence id.",
                },
                "minItems": 1,
                "description": (
                    "The licence ids to assign, from vrops.licensing.entitlement.query "
                    "(entitlementInfo.id). For example the cores licence and the vSAN add-on."
                ),
            },
        },
        "required": ["vcenterId", "vcenterHost", "licenseIds"],
        "additionalProperties": False,
        "examples": [
            {
                "vcenterId": "<vcenter-id>",
                "vcenterHost": "<vcenter-address>",
                "licenseIds": ["<license-id>"],
            }
        ],
    },
    response_schema=VROPS_LICENSING_PAYLOAD_RESPONSE,
    group_key=_LICENSING_GROUP_KEY,
    tags=("vrops", "vmware", "licensing", "assign", "change"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when the operator asks to license a vCenter, or to move it to a "
            "licence of a new licence server."
        ),
        "preconditions": (
            "A person must approve the call before it runs; the call waits until then. "
            "Pick a licence of a licence server that is ENTITLED and CONNECTED "
            "(vrops.licensing.server.list). A licence of a removed server fails."
        ),
        "parameter_hints": {
            "licenseIds": "Use entitlementInfo.id values from vrops.licensing.entitlement.query.",
        },
        "output_shape": "{payload: <the vendor answer, unchanged>}.",
        "next_step": (
            "Check the result with vrops.licensing.vcenter.query: state LICENSED and "
            "unlicensedUsage 0. If it fails with a 403 while all looks healthy, check "
            "the clocks of the vCenter and the licence server."
        ),
    },
)


#: The licence operations. Registered by the 9.x registrar only.
VROPS_LICENSING_OPS: tuple[VropsTypedOp, ...] = (
    VROPS_LICENSING_REGISTRATION_STATUS_OP,
    VROPS_LICENSING_SERVER_LIST_OP,
    VROPS_LICENSING_ENTITLEMENT_QUERY_OP,
    VROPS_LICENSING_VCENTER_QUERY_OP,
    VROPS_LICENSING_SERVER_QUERY_OP,
    VROPS_LICENSING_ASSIGN_OP,
)
