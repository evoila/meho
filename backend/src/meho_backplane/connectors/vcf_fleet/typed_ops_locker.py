# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed op metadata: locker passwords and certificates, and product certificates.

Five ops of :class:`~meho_backplane.connectors.vcf_fleet.connector.VcfFleetConnector`
(Aria Suite Lifecycle 8.x and VCF 9.0 fleet management). The bodies live in
:mod:`.locker` and :mod:`.product_certificate`. Two reads are ``safe``; the
three changes wait for approval. The two changes that send a secret
(``fleet.locker.password.add`` and ``fleet.locker.certificate.import``) take
Vault references, never a value, and are pinned as credential writes in
``broadcast/events.py``.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vcf_fleet._typed_op import FleetTypedOp

__all__ = ["FLEET_LOCKER_OPS", "FLEET_LOCKER_WHEN_TO_USE_BY_GROUP"]

FLEET_LOCKER_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "fleet-locker": (
        "The locker of Aria Suite Lifecycle or VCF 9.0 fleet management: the stored "
        "passwords and certificates that products use. Reads: list the passwords "
        "(never their values) and the certificates. Changes: add a password from Vault, "
        "import a certificate (its key from Vault) or generate one."
    ),
    "fleet-product-certificates": (
        "The certificate a deployed product (in a fleet environment) uses. One change: "
        "replace it with a locker certificate, after a pre-validation. The replace runs "
        "as a lifecycle request; read it with fleet.request.get."
    ),
}

_SECRET_REF_PATTERN = r"^[^#]+#[^#]+$"


def _ref_field(what: str) -> dict[str, Any]:
    return {
        "type": "string",
        "pattern": _SECRET_REF_PATTERN,
        "description": (
            f"Vault reference to the {what}, as '<vault-path>#<field>'. MEHO reads it "
            "under your identity when the approved call runs. Never the value itself."
        ),
    }


_ALIAS_FILTER = {
    "type": "string",
    "description": "Only entries whose alias matches this text (vendor filter aliasQuery).",
}

_PASSWORD_ROW: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vmid": {"type": ["string", "null"], "description": "The locker id of the entry."},
        "alias": {"type": ["string", "null"], "description": "The alias of the entry."},
        "userName": {"type": ["string", "null"], "description": "The user name it is for."},
        "passwordDescription": {
            "type": ["string", "null"],
            "description": "The description of the entry.",
        },
        "referenced": {
            "type": ["boolean", "null"],
            "description": "True when a product or environment uses the entry.",
        },
        "createdOn": {
            "type": ["integer", "null"],
            "description": "When the entry was made, epoch milliseconds.",
        },
        "lastUpdatedOn": {
            "type": ["integer", "null"],
            "description": "When the entry last changed, epoch milliseconds.",
        },
    },
}

_CERT_ROW: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vmid": {"type": ["string", "null"], "description": "The locker id of the certificate."},
        "alias": {"type": ["string", "null"], "description": "The alias of the certificate."},
        "subject": {"type": ["string", "null"], "description": "The subject (DN)."},
        "issuer": {"type": ["string", "null"], "description": "The issuer (DN)."},
        "algorithm": {"type": ["string", "null"], "description": "The signature algorithm."},
        "sha1": {"type": ["string", "null"], "description": "The SHA-1 thumbprint."},
        "healthy": {
            "type": ["boolean", "null"],
            "description": "False when the appliance flags the certificate (for example expired).",
        },
        "referenced": {
            "type": ["boolean", "null"],
            "description": "True when a product or environment uses the certificate.",
        },
        "expiresOn": {
            "type": ["string", "integer", "null"],
            "description": "When it stops being valid, when the appliance lists it; else null.",
        },
    },
}

_STEP_ROW: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step": {
            "type": "string",
            "description": "The step name.",
            "enum": [
                "check_running_requests",
                "pre_validate",
                "wait_for_pre_validation",
                "replace",
            ],
        },
        "result": {
            "type": "string",
            "description": "What happened: done, failed, unknown (reply lost) or not_run.",
            "enum": ["done", "failed", "unknown", "not_run"],
        },
        "error": {"type": ["string", "null"], "description": "Why the step failed."},
    },
}


_PASSWORD_LIST = FleetTypedOp(
    op_id="fleet.locker.password.list",
    handler_attr="locker_password_list",
    summary="List the locker passwords: alias, user name and whether a product uses it.",
    description=(
        "Lists the password entries of the locker (GET /lcm/locker/api/v2/passwords), every "
        "page. Each row has the alias, user name, description and whether a product uses it. "
        "Never returns a password. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"alias": _ALIAS_FILTER},
        "additionalProperties": False,
        "examples": [{}, {"alias": "<password-alias>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "passwords": {
                "type": "array",
                "description": "One row per locker password entry.",
                "items": _PASSWORD_ROW,
            },
            "count": {"type": "integer", "description": "How many rows came back."},
            "total": {
                "type": ["integer", "null"],
                "description": "How many entries the appliance says it has.",
            },
        },
    },
    group_key="fleet-locker",
    tags=("read-only", "fleet", "vcf", "locker"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which passwords the fleet locker holds, for example before a "
            "deploy or a password change needs the alias of an entry."
        ),
        "output_shape": (
            "{passwords: [{vmid, alias, userName, passwordDescription, referenced, createdOn, "
            "lastUpdatedOn}], count, total}. A long list is behind a result handle. "
            "referenced is the vendor's 'used by' flag."
        ),
        "next_step": (
            "Use an alias or vmid in the next change. If count is smaller than total, read "
            "the rest with the alias filter. To add an entry, call fleet.locker.password.add."
        ),
        "result_scalars": {"keys": ["count", "total"]},
    },
)


_PASSWORD_ADD = FleetTypedOp(
    op_id="fleet.locker.password.add",
    handler_attr="locker_password_add",
    summary="Add a password to the locker from a Vault reference.",
    description=(
        "Adds a password entry to the locker (POST /lcm/locker/api/v2/passwords). The "
        "password is read from Vault (password_ref) when the approved call runs. The answer "
        "has the new entry's id and alias, never the password. Waits for approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "alias": {"type": "string", "description": "Alias of the new entry. Must be new."},
            "user_name": {
                "type": "string",
                "description": "The user name the password is for, for example 'admin'.",
            },
            "description": {"type": "string", "description": "Optional description."},
            "password_ref": _ref_field("password"),
        },
        "required": ["alias", "user_name", "password_ref"],
        "additionalProperties": False,
        "examples": [
            {
                "alias": "<password-alias>",
                "user_name": "<user-name>",
                "password_ref": "<vault-path>#<field>",
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "'added' on success.", "enum": ["added"]},
            **_PASSWORD_ROW["properties"],
        },
    },
    group_key="fleet-locker",
    tags=("write", "fleet", "vcf", "locker", "credential"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to store a password in the fleet locker so a product deploy or a password "
            "change can use it by alias. Store the password in Vault first."
        ),
        "preconditions": (
            "The password is in Vault under '<vault-path>#<field>' and you may read it. The "
            "alias is not in the locker yet (check with fleet.locker.password.list). A person "
            "approves the call."
        ),
        "output_shape": (
            "{status: 'added', vmid, alias, userName, passwordDescription, referenced, "
            "createdOn, lastUpdatedOn}. Never the password. When the appliance refuses, "
            "the error's remediation holds its message."
        ),
        "next_step": (
            "Use the alias in the product or environment change that needs it. On HTTP 409 "
            "the alias exists; pick another alias."
        ),
    },
)


_CERTIFICATE_LIST = FleetTypedOp(
    op_id="fleet.locker.certificate.list",
    handler_attr="locker_certificate_list",
    summary="List the locker certificates: alias, subject, issuer and whether a product uses it.",
    description=(
        "Lists the certificates of the locker (GET /lcm/locker/api/v2/certificates), every "
        "page. Each row has the alias, subject, issuer, thumbprint, health and whether a "
        "product uses it. Never returns a key. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"alias": _ALIAS_FILTER},
        "additionalProperties": False,
        "examples": [{}, {"alias": "<certificate-alias>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "certificates": {
                "type": "array",
                "description": "One row per locker certificate.",
                "items": _CERT_ROW,
            },
            "count": {"type": "integer", "description": "How many rows came back."},
            "total": {
                "type": ["integer", "null"],
                "description": "How many certificates the appliance says it has.",
            },
        },
    },
    group_key="fleet-locker",
    tags=("read-only", "fleet", "vcf", "locker", "certificate"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which certificates the fleet locker holds: before a certificate "
            "replace (you need the vmid), or to find certificates that are not healthy."
        ),
        "output_shape": (
            "{certificates: [{vmid, alias, subject, issuer, algorithm, sha1, healthy, "
            "referenced, expiresOn}], count, total}. A long list is behind a result handle. "
            "expiresOn is null when the appliance does not list it."
        ),
        "next_step": (
            "Pass a vmid as certificate_id to fleet.product.certificate.replace. To add a "
            "certificate, call fleet.locker.certificate.import."
        ),
        "result_scalars": {"keys": ["count", "total"]},
    },
)


_IMPORT_FIELDS: dict[str, Any] = {
    "mode": {
        "type": "string",
        "enum": ["import", "generate"],
        "description": "'import' a PEM chain with its key, or 'generate' a new certificate.",
    },
    "alias": {"type": "string", "description": "Alias of the new locker certificate."},
    "certificate_chain": {
        "type": "string",
        "description": "Mode import: the PEM certificate chain, leaf first. Public text.",
    },
    "private_key_ref": _ref_field("PEM private key of the leaf certificate"),
    "passphrase_ref": _ref_field("passphrase of the private key, when it has one"),
    "common_name": {"type": "string", "description": "Mode generate: the common name (CN)."},
    "hostnames": {
        "type": "array",
        "description": "Mode generate: host names for the subject alternative names.",
        "items": {"type": "string", "description": "One host name."},
    },
    "ip_addresses": {
        "type": "array",
        "description": "Mode generate: IP addresses for the subject alternative names.",
        "items": {"type": "string", "description": "One IP address."},
    },
    "organization": {"type": "string", "description": "Mode generate: organization (O)."},
    "organization_unit": {
        "type": "string",
        "description": "Mode generate: organization unit (OU).",
    },
    "locality": {"type": "string", "description": "Mode generate: locality (L)."},
    "state": {"type": "string", "description": "Mode generate: state (ST)."},
    "country": {"type": "string", "description": "Mode generate: two-letter country code (C)."},
    "key_size": {
        "type": "integer",
        "enum": [2048, 3072, 4096],
        "description": "Mode generate: key length in bits.",
    },
}

_CERTIFICATE_IMPORT = FleetTypedOp(
    op_id="fleet.locker.certificate.import",
    handler_attr="locker_certificate_import",
    summary="Import a certificate into the locker (key from Vault), or generate one there.",
    description=(
        "Mode import: POST /lcm/locker/api/v2/certificates/import with a PEM chain; the "
        "private key comes from Vault. Mode generate: POST /lcm/locker/api/v2/certificates. "
        "The answer has the id, subject and dates, never a key. Waits for approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": _IMPORT_FIELDS,
        "required": ["mode", "alias"],
        "oneOf": [
            {
                "description": "Import a PEM chain with its private key from Vault.",
                "properties": {
                    "mode": {"const": "import", "description": "Import mode."},
                },
                "required": ["certificate_chain", "private_key_ref"],
            },
            {
                "description": "Let the appliance generate the key and certificate.",
                "properties": {
                    "mode": {"const": "generate", "description": "Generate mode."},
                },
                "required": ["common_name"],
            },
        ],
        "additionalProperties": False,
        "examples": [
            {
                "mode": "import",
                "alias": "<certificate-alias>",
                "certificate_chain": "<pem-certificate-chain>",
                "private_key_ref": "<vault-path>#<field>",
            },
            {
                "mode": "generate",
                "alias": "<certificate-alias>",
                "common_name": "<fqdn>",
                "hostnames": ["<fqdn>"],
            },
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["imported", "generated"],
                "description": "What happened.",
            },
            "vmid": {"type": ["string", "null"], "description": "The locker id."},
            "alias": {"type": "string", "description": "The alias."},
            "certInfo": {
                "type": ["object", "null"],
                "description": "Subject, issuer, san, sha1, sha256, algorithm, key algorithm, key "
                "length.",
                "additionalProperties": True,
            },
            "validity": {
                "type": ["object", "null"],
                "description": "issuedOn, expiresOn, period and healthy, as the appliance gives.",
                "additionalProperties": True,
            },
            "validations": {
                "type": "array",
                "description": "The appliance's checks of the certificate.",
                "items": {"type": "object", "description": "One check result."},
            },
        },
    },
    group_key="fleet-locker",
    tags=("write", "fleet", "vcf", "locker", "certificate", "credential"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to put a certificate into the fleet locker, so a product certificate "
            "replace or a deploy can use it. Use mode import for a CA-signed certificate; "
            "use mode generate for a certificate the appliance signs itself."
        ),
        "preconditions": (
            "Mode import: the private key (and its passphrase, if any) is in Vault as "
            "'<vault-path>#<field>' and you may read it; certificate_chain is the PEM chain. "
            "Mode generate: common_name is set. The alias is new. A person approves the call."
        ),
        "output_shape": (
            "{status, vmid, alias, certInfo, validity, validations}. Never a private key or "
            "the chain text. When the appliance refuses, the error's remediation holds its "
            "message."
        ),
        "next_step": (
            "Pass vmid as certificate_id to fleet.product.certificate.replace. Check "
            "validations and validity before you use the certificate."
        ),
        "result_scalars": {"keys": ["status", "vmid", "alias"]},
    },
)


_PRODUCT_CERTIFICATE_REPLACE = FleetTypedOp(
    op_id="fleet.product.certificate.replace",
    handler_attr="product_certificate_replace",
    summary="Replace a product's certificate with a locker certificate, after a pre-validation.",
    description=(
        "Pre-validates, then starts the replace of a product's certificate in an environment "
        "(PUT /lcm/lcops/api/v2/environments/{environmentId}/products/{productId}/certificates/"
        "{certificateId}). Returns 'accepted' with the request id. Waits for approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "environment_id": {
                "type": "string",
                "description": "The environment id (from fleet.environment.list).",
            },
            "product_id": {
                "type": "string",
                "description": "The product id in the environment, for example 'vra' or 'vrops'.",
            },
            "certificate_id": {
                "type": "string",
                "description": "The vmid of the new locker certificate.",
            },
        },
        "required": ["environment_id", "product_id", "certificate_id"],
        "additionalProperties": False,
        "examples": [
            {
                "environment_id": "<environment-id>",
                "product_id": "<product-id>",
                "certificate_id": "<certificate-vmid>",
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["accepted", "already_running", "failed", "unknown"],
                "description": (
                    "accepted: the replace started. already_running: a certificate request "
                    "for this product runs; nothing started. failed: a step failed. unknown: "
                    "the replace was sent but the reply was lost."
                ),
            },
            "request_id": {
                "type": ["string", "null"],
                "description": "The vendor request id of the replace (or the running request).",
            },
            "request_state": {
                "type": ["string", "null"],
                "description": "already_running: the state of the running request.",
            },
            "pre_validation_request_id": {
                "type": ["string", "null"],
                "description": "The vendor request id of the pre-validation.",
            },
            "status_read": {
                "type": "string",
                "description": "The op that reads the request until it ends: fleet.request.get.",
            },
            "failed_step": {"type": "string", "description": "failed: the step that failed."},
            "error": {"type": "string", "description": "failed or unknown: what went wrong."},
            "pre_validation": {
                "type": "object",
                "description": "A failed pre-validation: request_state, overall_status, checks.",
                "properties": {
                    "request_state": {
                        "type": ["string", "null"],
                        "description": "The state of the pre-validation request.",
                    },
                    "overall_status": {
                        "type": ["boolean", "null"],
                        "description": "The report's overall result.",
                    },
                    "failing_checks": {
                        "type": "array",
                        "description": "The checks with an error: check, status, result, advice.",
                        "items": {"type": "object", "description": "One failing check."},
                    },
                },
            },
            "steps": {
                "type": "array",
                "description": "Every step with its result.",
                "items": _STEP_ROW,
            },
        },
    },
    group_key="fleet-product-certificates",
    tags=("write", "fleet", "vcf", "certificate", "lifecycle-request"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to replace the certificate of a product (for example an expiring one) with "
            "a certificate already in the locker. Use MEHO's async mode (async: true), so a "
            "lost reply does not lose the request id."
        ),
        "preconditions": (
            "The new certificate is in the locker (fleet.locker.certificate.list gives its "
            "vmid) and covers the product's host names. The product restarts its services "
            "during the replace. An agent needs a grant for this risk level; a person approves."
        ),
        "output_shape": (
            "{status, request_id, pre_validation_request_id, status_read, steps: [{step, "
            "result, error}]}. failed adds failed_step and error, and pre_validation with "
            "the failing checks. The op waits up to 180 seconds for the pre-validation."
        ),
        "next_step": (
            "accepted or already_running: read request_id with fleet.request.get until it "
            "ends. failed: fix what the error or failing checks say, then call again. "
            "unknown, or no reply at all: do not call again blindly; first list the "
            "requests (fleet.request.list) and look for a certificate request for this "
            "product, then read it with fleet.request.get."
        ),
        "result_scalars": {
            "keys": ["status", "request_id", "pre_validation_request_id", "status_read"]
        },
    },
)


#: The locker and product-certificate ops, in the order the connector registers them.
FLEET_LOCKER_OPS: tuple[FleetTypedOp, ...] = (
    _PASSWORD_LIST,
    _PASSWORD_ADD,
    _CERTIFICATE_LIST,
    _CERTIFICATE_IMPORT,
    _PRODUCT_CERTIFICATE_REPLACE,
)
