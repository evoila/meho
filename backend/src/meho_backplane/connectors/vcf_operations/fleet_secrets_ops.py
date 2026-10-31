# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fleet certificate and password operations of the VCF Operations connector.

In VCF 9.1, fleet certificates and passwords are managed in VCF Operations.
Five typed operations read and change them; all need VCF Operations 9.1 or
newer (the handlers stop on 9.0 with a version message):

* ``vrops.fleet.certificate.list`` — the fleet certificates: product, expiry.
* ``vrops.fleet.certificate.replace`` — start a certificate replacement. A
  change: it waits for a person's approval.
* ``vrops.fleet.password.list`` — the fleet password accounts, never a value.
* ``vrops.fleet.password.rotate`` — start a password change, with both
  passwords from the secret store. A change: it waits for a person's approval.
* ``vrops.fleet.request.get`` — the state of one started request.

The handlers and the two approval previews live in
:mod:`~meho_backplane.connectors.vcf_operations.fleet_secrets_handlers`.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vcf_operations.fleet_secrets_handlers import (
    FLEET_DEFAULT_PAGE_SIZE,
)
from meho_backplane.connectors.vcf_operations.typed_ops import VropsTypedOp

__all__ = [
    "VROPS_FLEET_SECRETS_OPS",
    "VROPS_FLEET_SECRETS_WHEN_TO_USE_BY_GROUP",
]

_GROUP_KEY = "vrops-fleet-certificates-passwords"

VROPS_FLEET_SECRETS_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    _GROUP_KEY: (
        "Use for the fleet certificates and passwords that VCF Operations 9.1 and "
        "newer manages for the VCF products. Reads: list certificates, list password "
        "accounts (never the passwords), read the state of a started request. "
        "Changes: replace a certificate, change a password; each waits for a "
        "person's approval and starts a vendor job."
    ),
}

_TAGS: tuple[str, ...] = ("vrops", "vmware", "fleet", "vcf-9.1")

_PAGE_PROPS: dict[str, Any] = {
    "page": {"type": "integer", "minimum": 0, "description": "0-based page number."},
    "pageSize": {
        "type": "integer",
        "minimum": 1,
        "description": f"Rows per page. MEHO sends {FLEET_DEFAULT_PAGE_SIZE} when none is given.",
    },
    "sortOrder": {
        "type": "string",
        "enum": ["ASCENDING", "DESCENDING"],
        "description": "Sort direction.",
    },
}

_APPLIANCES: list[str] = [
    "VCENTER",
    "SDDC_MANAGER",
    "NSXT_MANAGER",
    "VCF_AUTOMATION",
    "LOG_MANAGEMENT",
    "VCF_OPERATIONS",
    "VCF_OPS_NETWORK",
    "IDENTITY_BROKER",
    "VCF_OPS_HCX",
    "ESX",
    "VCF_SERVICES_RUNTIME",
    "AVI_LOAD_BALANCER",
]

_CA_TYPES: list[str] = ["VMCA", "OPENSSL_CA", "MSCA", "EXTERNAL_CA"]

_PREVIOUS_REQUEST_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Optional. The request_id of an earlier start for the same object. If that "
        "request has not ended, MEHO returns it and starts nothing."
    ),
}


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _rows_response(rows_key: str, row_props: dict[str, Any], what: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            rows_key: {
                "type": "array",
                "description": f"One flat row per {what}.",
                "items": {"type": "object", "properties": row_props},
            },
            "total_count": {"type": "integer", "description": "All matches at the vendor."},
        },
    }


_CERTIFICATE_ROW: dict[str, Any] = {
    "certificateResourceKey": _string("The certificate id; pass it as certificateId."),
    "appliance": _string("The product type, for example VCENTER."),
    "applianceFqdn": _string("The address of the product appliance."),
    "displayApplianceType": _string("The product name for people."),
    "vcfComponent": _string("The VCF component."),
    "vcfEndpoint": _string("The VCF endpoint."),
    "domainId": _string("The workload domain id, if any."),
    "category": _string("TLS_CERT, ROOT_CERT or INTERMEDIATE_CERT."),
    "type": _string("The signing CA type, for example VMCA."),
    "status": _string("Expiry status: EXPIRED, EXPIRING_30, EXPIRING_60 or NORMAL."),
    "issuedToCommonName": _string("The common name the certificate is for."),
    "issuedBy": _string("The issuer."),
    "expiryDate": {
        "type": "integer",
        "description": "The end of validity, as the vendor sends it.",
    },
    "daysToExpire": {"type": "integer", "description": "Days until the certificate ends."},
    "autoRenewState": _string("The automatic renewal setting."),
    "autoRenewStatus": _string("The automatic renewal state."),
}

_PASSWORD_ROW: dict[str, Any] = {
    "passwordAccountKey": _string("The account id; pass it to vrops.fleet.password.rotate."),
    "userName": _string("The account name."),
    "accountType": _string("The account type."),
    "credentialType": _string("The credential type, for example SSH or API."),
    "appliance": _string("The product type, for example VCENTER."),
    "applianceFqdn": _string("The address of the product appliance."),
    "displayApplianceType": _string("The product name for people."),
    "applianceOpsResourceId": _string("The VCF Operations id of the appliance."),
    "vcfDomainId": _string("The workload domain id, if any."),
    "status": _string("Password status, for example ACTIVE, EXPIRING or EXPIRED."),
    "expiryDate": {
        "type": "integer",
        "description": "When the password ends, as the vendor sends it.",
    },
}

_START_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["accepted", "already_running"],
            "description": "accepted: the vendor took the job. already_running: nothing started.",
        },
        "started": {"type": "boolean", "description": "True when this call started a job."},
        "request_id": _string("The vendor request id; read it with vrops.fleet.request.get."),
        "state": _string("The vendor state of the request when MEHO saw it."),
        "request_type": _string("The vendor request type."),
        "status_read": _string("The operation that reads the request: vrops.fleet.request.get."),
        "op_id": _string("This operation."),
        "note": _string("What to do next."),
    },
}

_REQUEST_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "request_id": _string("The vendor request id."),
        "reachable": {
            "type": "boolean",
            "description": "False when VCF Operations did not answer.",
        },
        "state": _string("The vendor state, for example CREATED, IN_PROGRESS, COMPLETED, FAILED."),
        "outcome": {
            "type": "string",
            "enum": [
                "queued",
                "running",
                "succeeded",
                "failed",
                "canceled",
                "unknown",
                "not_reachable",
            ],
            "description": "The plain result of the state.",
        },
        "ended": {
            "type": "boolean",
            "description": "True when the request will not change any more.",
        },
        "request_name": _string("The vendor request name."),
        "request_type": _string("The vendor request type."),
        "category": _string("The vendor category, for example VCF_CERTIFICATE_MANAGEMENT."),
        "duration": {
            "type": "integer",
            "description": "How long the request ran, as the vendor sends it.",
        },
        "error_text": _string("The vendor's error messages when the request failed."),
        "checked_at": _string("When MEHO read the request (UTC, ISO 8601)."),
        "note": _string("Set when VCF Operations did not answer."),
    },
}

_LOST_REPLY = (
    "After a lost reply, do not start again blindly: read {read} first and decide from "
    "what you find. Run the start with async: true, so MEHO keeps its answer (with "
    "request_id) under a run handle."
)


VROPS_FLEET_CERTIFICATE_LIST_OP = VropsTypedOp(
    op_id="vrops.fleet.certificate.list",
    handler_attr="fleet_certificate_list",
    summary="List the fleet certificates in VCF Operations 9.1: product, appliance, expiry.",
    description=(
        "Sends POST /suite-api/api/fleet-management/certificate-management/certificates/"
        "query, or GET .../certificates/{certificateId} for one. Needs VCF Operations 9.1 "
        "or newer. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "certificateId": {
                "type": "string",
                "minLength": 1,
                "description": "Optional. Read only this certificate (certificateResourceKey).",
            },
            "appliance": {
                "type": "string",
                "enum": _APPLIANCES,
                "description": "Only this product type.",
            },
            "applianceFqdn": _string("Only the certificates of this appliance address."),
            "category": {
                "type": "string",
                "enum": ["TLS_CERT", "ROOT_CERT", "INTERMEDIATE_CERT"],
                "description": "Only this certificate category.",
            },
            "status": {
                "type": "string",
                "enum": ["EXPIRED", "EXPIRING_30", "EXPIRING_60", "NORMAL"],
                "description": "Only this expiry status.",
            },
            "type": {
                "type": "string",
                "enum": _CA_TYPES,
                "description": "Only this signing CA type.",
            },
            "sortBy": {
                "type": "string",
                "enum": [
                    "STATUS",
                    "APPLIANCE",
                    "CATEGORY",
                    "TYPE",
                    "APPLIANCE_FQDN",
                    "VCF_COMPONENT_TYPE",
                    "SDDC_DOMAIN_ID",
                    "VCF_INSTANCE_ENDPOINT",
                    "CERTIFICATE_COMMON_NAME",
                    "EXPIRY_DATE",
                    "AUTO_RENEW_STATUS",
                ],
                "description": "Sort field.",
            },
            **_PAGE_PROPS,
        },
        "additionalProperties": False,
        "examples": [{"status": "EXPIRING_30"}, {"certificateId": "<certificate-id>"}],
    },
    response_schema=_rows_response("certificates", _CERTIFICATE_ROW, "certificate"),
    group_key=_GROUP_KEY,
    tags=(*_TAGS, "read-only", "certificate"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which fleet certificates expire soon, or to find the id of a "
            "certificate to replace. Filter with status EXPIRED or EXPIRING_30."
        ),
        "parameter_hints": {
            "certificateId": "Use certificateResourceKey of a row.",
            "pageSize": "Leave it out to get up to 1000 rows in one call.",
        },
        "output_shape": (
            "{certificates: [{certificateResourceKey, appliance, applianceFqdn, category, "
            "type, status, issuedToCommonName, issuedBy, expiryDate, daysToExpire, ...}], "
            "total_count}. A long list comes back as a result handle."
        ),
        "next_step": (
            "Pass certificateResourceKey as certificateId to vrops.fleet.certificate.replace."
        ),
    },
)


VROPS_FLEET_CERTIFICATE_REPLACE_OP = VropsTypedOp(
    op_id="vrops.fleet.certificate.replace",
    handler_attr="fleet_certificate_replace",
    summary="Start replacing one fleet certificate in VCF Operations 9.1. Waits for approval.",
    description=(
        "Sends PUT /suite-api/api/fleet-management/certificate-management/certificates/"
        "{certificateId} with caType and an optional certificate chain. Returns the "
        "vendor request at once; the vendor does the work after. Needs 9.1 or newer."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "certificateId": {
                "type": "string",
                "minLength": 1,
                "description": "The certificate id (certificateResourceKey from the list).",
            },
            "caType": {
                "type": "string",
                "enum": _CA_TYPES,
                "description": "Who signs the new certificate.",
            },
            "certificateChain": {
                "type": "string",
                "minLength": 1,
                "not": {"pattern": "PRIVATE KEY"},
                "description": (
                    "The signed certificate and its chain, PEM text. Needed for EXTERNAL_CA. "
                    "Never a private key: MEHO refuses one."
                ),
            },
            "previous_request_id": _PREVIOUS_REQUEST_PROP,
        },
        "required": ["certificateId", "caType"],
        "additionalProperties": False,
        "examples": [{"certificateId": "<certificate-id>", "caType": "VMCA"}],
    },
    response_schema=_START_RESPONSE,
    group_key=_GROUP_KEY,
    tags=(*_TAGS, "certificate", "replace", "change"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when the operator asks to replace or renew a fleet certificate, for "
            "example one that expires soon."
        ),
        "preconditions": (
            "A person must approve the call; it waits until then. Needs VCF Operations "
            "9.1 or newer. For EXTERNAL_CA, the CSR must exist and certificateChain must "
            "hold the signed certificate. The product's services may restart."
        ),
        "parameter_hints": {
            "certificateId": "Use certificateResourceKey from vrops.fleet.certificate.list.",
            "previous_request_id": "Pass the request_id of an earlier start that may still run.",
        },
        "output_shape": (
            "{status: accepted | already_running, started, request_id, state, request_type, "
            "status_read, note}. accepted means the vendor took the job, not that it is done."
        ),
        "next_step": (
            "Read vrops.fleet.request.get with request_id until ended is true. Then check "
            "the new expiry with vrops.fleet.certificate.list. "
            + _LOST_REPLY.format(read="vrops.fleet.certificate.list (a new expiryDate)")
        ),
    },
)


VROPS_FLEET_PASSWORD_LIST_OP = VropsTypedOp(
    op_id="vrops.fleet.password.list",
    handler_attr="fleet_password_list",
    summary="List the fleet password accounts in VCF Operations 9.1, never the passwords.",
    description=(
        "Sends POST /suite-api/api/fleet-management/password-management/accounts/query. "
        "Returns account name, product, status and expiry; no password value. Needs VCF "
        "Operations 9.1 or newer. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "appliance": {
                "type": "string",
                "enum": [*_APPLIANCES, "NSXT_EDGE"],
                "description": "Only this product type.",
            },
            "applianceFqdn": _string("Only the accounts of this appliance address."),
            "status": {
                "type": "string",
                "enum": ["ACTIVE", "EXPIRING", "EXPIRED"],
                "description": "Only this password status.",
            },
            "username": _string("Only this account name."),
            "vcfDomainId": _string("Only the accounts of this workload domain."),
            "sortBy": {
                "type": "string",
                "enum": ["STATUS", "APPLIANCE", "APPLIANCE_IP", "USERNAME", "EXPIRY_DATE"],
                "description": "Sort field.",
            },
            **_PAGE_PROPS,
        },
        "additionalProperties": False,
        "examples": [{"status": "EXPIRING"}],
    },
    response_schema=_rows_response("accounts", _PASSWORD_ROW, "password account"),
    group_key=_GROUP_KEY,
    tags=(*_TAGS, "read-only", "password-account"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which fleet passwords expire soon, or to find the account to "
            "change. It never returns a password."
        ),
        "parameter_hints": {"pageSize": "Leave it out to get up to 1000 rows in one call."},
        "output_shape": (
            "{accounts: [{passwordAccountKey, userName, accountType, credentialType, "
            "appliance, applianceFqdn, status, expiryDate, ...}], total_count}. A long list "
            "comes back as a result handle."
        ),
        "next_step": "Pass passwordAccountKey and userName to vrops.fleet.password.rotate.",
    },
)


VROPS_FLEET_PASSWORD_ROTATE_OP = VropsTypedOp(
    op_id="vrops.fleet.password.rotate",
    handler_attr="fleet_password_rotate",
    summary="Start changing one fleet password in VCF Operations 9.1. Waits for approval.",
    description=(
        "Sends PUT /suite-api/api/fleet-management/password-management/accounts/"
        "{passwordAccountKey}/password. Both passwords come from the secret store, never "
        "as input. Returns the vendor request at once. Needs 9.1 or newer."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "passwordAccountKey": {
                "type": "string",
                "minLength": 1,
                "description": "The account id, from vrops.fleet.password.list.",
            },
            "userName": _string("Optional. The account name, from vrops.fleet.password.list."),
            "current_password_ref": {
                "type": "string",
                "pattern": "^[^#]+#[^#]+$",
                "description": "Where the current password is: '<vault-path>#<field>'.",
            },
            "new_password_ref": {
                "type": "string",
                "pattern": "^[^#]+#[^#]+$",
                "description": "Where the new password is: '<vault-path>#<field>'.",
            },
            "previous_request_id": _PREVIOUS_REQUEST_PROP,
        },
        "required": ["passwordAccountKey", "current_password_ref", "new_password_ref"],
        "additionalProperties": False,
        "examples": [
            {
                "passwordAccountKey": "<password-account-key>",
                "userName": "<account-name>",
                "current_password_ref": "<vault-path>#<field>",
                "new_password_ref": "<vault-path>#<field>",
            }
        ],
    },
    response_schema=_START_RESPONSE,
    group_key=_GROUP_KEY,
    tags=(*_TAGS, "password", "rotate", "change"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": "Call when the operator asks to change or rotate a fleet password.",
        "preconditions": (
            "A person must approve the call; it waits until then. Needs VCF Operations 9.1 "
            "or newer. Store the new password in the secret store first; never pass a "
            "password in any parameter. The new password must meet the product's rules."
        ),
        "parameter_hints": {
            "current_password_ref": "A '<vault-path>#<field>' reference, never the password.",
            "new_password_ref": "A '<vault-path>#<field>' reference, never the password.",
        },
        "output_shape": (
            "{status: accepted | already_running, started, request_id, state, request_type, "
            "status_read, note}. accepted means the vendor took the job, not that it is done. "
            "No password is in the answer."
        ),
        "next_step": (
            "Read vrops.fleet.request.get with request_id until ended is true. Then update "
            "the current password in the secret store. "
            + _LOST_REPLY.format(read="vrops.fleet.password.list (a new expiryDate)")
            + " A second start with the old current password fails once the first one worked."
        ),
    },
)


VROPS_FLEET_REQUEST_GET_OP = VropsTypedOp(
    op_id="vrops.fleet.request.get",
    handler_attr="fleet_request_get",
    summary="Read one fleet request in VCF Operations 9.1: state, outcome, error text.",
    description=(
        "Sends GET /suite-api/api/workflows/requests/{requestId}. The status read for a "
        "certificate replacement or a password change. Says not_reachable, not failed, "
        "when VCF Operations does not answer. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "request_id": {
                "type": "string",
                "minLength": 1,
                "description": "The request_id a start returned.",
            },
        },
        "required": ["request_id"],
        "additionalProperties": False,
        "examples": [{"request_id": "<request-id>"}],
    },
    response_schema=_REQUEST_RESPONSE,
    group_key=_GROUP_KEY,
    tags=(*_TAGS, "read-only", "request", "status"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call after vrops.fleet.certificate.replace or vrops.fleet.password.rotate, "
            "and again until ended is true."
        ),
        "parameter_hints": {"request_id": "Use request_id from the start's answer."},
        "output_shape": (
            "{request_id, reachable, state, outcome, ended, request_name, request_type, "
            "category, duration, error_text, checked_at}. The vendor gives no steps and no "
            "progress. outcome not_reachable means VCF Operations did not answer; the job "
            "may still run."
        ),
        "next_step": (
            "If ended is false, wait and read again. If outcome is failed, report "
            "error_text. If not_reachable, read again later with the same request_id."
        ),
        "result_scalars": {"keys": ["reachable", "state", "outcome"]},
    },
)


#: The fleet certificate and password operations. Registered by the 9.x
#: registrar only.
VROPS_FLEET_SECRETS_OPS: tuple[VropsTypedOp, ...] = (
    VROPS_FLEET_CERTIFICATE_LIST_OP,
    VROPS_FLEET_CERTIFICATE_REPLACE_OP,
    VROPS_FLEET_PASSWORD_LIST_OP,
    VROPS_FLEET_PASSWORD_ROTATE_OP,
    VROPS_FLEET_REQUEST_GET_OP,
)
