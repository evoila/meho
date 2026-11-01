# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed-op metadata: SRM appliance certificates and the storage replication adapter install.

``srm.appliance.csr.create``, ``srm.appliance.certificate.set`` and
``srm.storage_adapter.install``. Joined into ``SRM_TYPED_OPS`` in
:mod:`meho_backplane.connectors.srm.typed_ops`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.srm._op import SrmTypedOp
from meho_backplane.connectors.srm.appliance_ops import (
    APPLIANCE_REF,
    GROUP_APPLIANCE,
    GROUP_STORAGE_ADAPTERS,
    JOB_ANSWER_PROPERTIES,
    SSO_REF,
    STEPS_SCHEMA,
)

__all__ = ["APPLIANCE_MORE_TYPED_OPS"]

_PEM_LIST: Final[dict[str, Any]] = {
    "type": "array",
    "minItems": 1,
    "description": "CA certificates in PEM format.",
    "items": {"type": "string", "description": "One CA certificate in PEM format."},
}

_CERT_ROW: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "issuer": {"type": "string"},
        "not_after": {"type": "string"},
        "sha256_fingerprint": {"type": "string"},
    },
}

_CSR_CREATE = SrmTypedOp(
    op_id="srm.appliance.csr.create",
    handler_attr="appliance_csr_create",
    summary="Make a new key and certificate signing request on the SRM appliance.",
    description=(
        "Calls POST /api/rest/configure/v2/appliance/certificates/actions/generate-csr. "
        "The appliance makes a new private key, which stays on the appliance, and returns "
        "the certificate signing request (CSR) in PEM format for a CA to sign."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "appliance_secret_ref": APPLIANCE_REF,
            "common_name": {
                "type": "string",
                "description": "Common name; usually the appliance's FQDN. Default: its host name.",
            },
            "organization": {"type": "string", "description": "Legal name of the organization."},
            "organization_unit": {"type": "string", "description": "Unit of the organization."},
            "locality": {"type": "string", "description": "City of the organization."},
            "state": {"type": "string", "description": "State or province, not abbreviated."},
            "country": {
                "type": "string",
                "minLength": 2,
                "maxLength": 2,
                "description": "Two-letter ISO country code.",
            },
            "email": {"type": "string", "description": "Contact e-mail address."},
            "fqdns": {
                "type": "array",
                "description": "DNS names for the certificate. Default: the host name.",
                "items": {"type": "string", "description": "One DNS name."},
            },
            "ips": {
                "type": "array",
                "description": "IP addresses for the certificate. Default: none.",
                "items": {"type": "string", "description": "One IP address."},
            },
        },
        "required": ["appliance_secret_ref"],
        "additionalProperties": False,
        "examples": [
            {
                "appliance_secret_ref": "<vault-path-of-appliance-admin>",
                "common_name": "<appliance-fqdn>",
                "organization": "<organization>",
                "fqdns": ["<appliance-fqdn>"],
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["done"]},
            "csr_pem": {"type": "string"},
            "subject": {"type": "string"},
            "subject_alternative_names": {"type": "array", "items": {"type": "string"}},
            "public_key_sha256": {"type": "string"},
        },
    },
    group_key=GROUP_APPLIANCE,
    tags=("write", "srm", "live-recovery", "appliance", "certificate"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call as the first step of replacing the appliance's server certificate with "
            "one signed by a CA."
        ),
        "preconditions": (
            "Each call makes a new key; an older request no longer fits. Use the request "
            "of the newest call for the signed certificate."
        ),
        "output_shape": (
            "csr_pem (the request, PEM), subject, subject_alternative_names, "
            "public_key_sha256. No private key."
        ),
        "next_step": (
            "Have a CA sign csr_pem, then call srm.appliance.certificate.set with "
            "server_certificate: certificate_pem, csr_pem and ca_chain_pem."
        ),
    },
)

_CERTIFICATE_SET = SrmTypedOp(
    op_id="srm.appliance.certificate.set",
    handler_attr="appliance_certificate_set",
    summary="Install the SRM appliance's signed certificate, or add or remove trusted CAs.",
    description=(
        "server_certificate: checks key, name and dates, then POSTs .../server/actions/"
        "update and returns the vendor task (status read srm.appliance.task.get). "
        "add_ca_certificates / remove_ca_certificates: POST .../certificates/ca, done "
        "when the call returns."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "appliance_secret_ref": APPLIANCE_REF,
            "sso_secret_ref": {
                **SSO_REF,
                "description": (
                    "Vault path (KV v2) of the vCenter SSO admin user (username, password). "
                    "Needed for server_certificate: the appliance updates its registration."
                ),
            },
            "server_certificate": {
                "type": "object",
                "description": "The signed certificate for the request of csr.create.",
                "properties": {
                    "certificate_pem": {
                        "type": "string",
                        "description": "The signed server certificate, PEM.",
                    },
                    "csr_pem": {
                        "type": "string",
                        "description": "The request it was signed for (csr_pem of csr.create).",
                    },
                    "ca_chain_pem": {
                        "type": "array",
                        "description": "Intermediate and root CA certificates, PEM.",
                        "items": {"type": "string", "description": "One CA certificate, PEM."},
                    },
                },
                "required": ["certificate_pem", "csr_pem"],
                "additionalProperties": False,
            },
            "add_ca_certificates": {**_PEM_LIST, "description": "CA certificates to trust."},
            "remove_ca_certificates": {
                **_PEM_LIST,
                "description": "Trusted CA certificates to remove.",
            },
        },
        "required": ["appliance_secret_ref"],
        "oneOf": [
            {"required": ["server_certificate", "sso_secret_ref"]},
            {"required": ["add_ca_certificates"]},
            {"required": ["remove_ca_certificates"]},
        ],
        "additionalProperties": False,
        "examples": [
            {
                "appliance_secret_ref": "<vault-path-of-appliance-admin>",
                "sso_secret_ref": "<vault-path-of-sso-admin>",
                "server_certificate": {
                    "certificate_pem": "<signed-certificate-pem>",
                    "csr_pem": "<csr-pem-from-csr-create>",
                    "ca_chain_pem": ["<ca-certificate-pem>"],
                },
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            **JOB_ANSWER_PROPERTIES,
            "change": {
                "type": "string",
                "enum": ["server_certificate", "add_ca_certificates", "remove_ca_certificates"],
            },
            "certificate": _CERT_ROW,
            "certificates": {"type": "array", "items": _CERT_ROW},
        },
    },
    group_key=GROUP_APPLIANCE,
    tags=("write", "srm", "live-recovery", "appliance", "certificate", "vendor-job"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to install the CA-signed certificate after srm.appliance.csr.create, or "
            "to add or remove a CA certificate the appliance trusts."
        ),
        "preconditions": (
            "server_certificate: the certificate was signed for the newest request of "
            "srm.appliance.csr.create and names the appliance's host name; else MEHO "
            "refuses it. The SSO admin user is in Vault. Give exactly one change."
        ),
        "output_shape": (
            "server_certificate: outcome accepted (NOT done yet), already_running, "
            "refused (the certificate check failed; nothing changed) or failed; task_id, "
            "task, status_read, steps, certificate (subject, issuer, not_after, "
            "sha256_fingerprint). CA changes: outcome done and certificates."
        ),
        "next_step": (
            "accepted or already_running: call srm.appliance.task.get with task_id until "
            "it ends; not_reachable means read again later (the appliance restarts its "
            "services). If the reply was lost, read srm.appliance.show before a new start. "
            "Start with async: true so MEHO keeps the task_id under a run handle."
        ),
    },
)

_SRA_INSTALL = SrmTypedOp(
    op_id="srm.storage_adapter.install",
    handler_attr="storage_adapter_install",
    summary="Install a storage replication adapter on the SRM appliance from an archive URL.",
    description=(
        "MEHO fetches the storage vendor's adapter archive from a URL, checks its SHA-256 "
        "and uploads it (POST .../storage-replication-adapters), then lists the adapters. "
        "The adapter runs as a container on the appliance. A person runs this after a preview."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "appliance_secret_ref": APPLIANCE_REF,
            "archive_url": {
                "type": "string",
                "minLength": 1,
                # The parameter check refuses a query part, a fragment or a login before
                # the public preview builds its answer, so a signed URL's token is echoed
                # nowhere. The handler checks the URL again.
                "pattern": r"^https?://[^?#@]+$",
                "description": (
                    "http(s) URL of the adapter archive (.tar.gz) that MEHO can reach, with no "
                    "query part, user name or password. MEHO follows no redirect. For a signed "
                    "URL (its token is in the query part) give archive_url_secret_ref instead."
                ),
            },
            "archive_url_secret_ref": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Vault path of a secret whose field url holds the archive URL. Use it for "
                    "a signed URL: the token stays in Vault and is in no preview, approval, "
                    "broadcast or answer. MEHO reads it under the operator's identity."
                ),
            },
            "sha256": {
                "type": "string",
                "minLength": 1,
                "description": "SHA-256 of the archive from the storage vendor: 64 hex digits.",
            },
        },
        "required": ["appliance_secret_ref", "sha256"],
        "oneOf": [{"required": ["archive_url"]}, {"required": ["archive_url_secret_ref"]}],
        "additionalProperties": False,
        "examples": [
            {
                "appliance_secret_ref": "<vault-path-of-appliance-admin>",
                "archive_url": "https://<archive-host>/<adapter-archive>.tar.gz",
                "sha256": "<sha256-of-the-archive>",
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["installed", "failed"]},
            "installed": {"type": ["boolean", "null"]},
            "adapter": {"type": ["object", "null"]},
            "archive": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "source": {"type": "string"},
                    "size": {"type": ["integer", "null"]},
                    "sha256": {"type": ["string", "null"]},
                    "expected_sha256": {"type": "string"},
                },
            },
            "adapters_before": {"type": ["array", "null"]},
            "adapters_after": {"type": ["array", "null"]},
            "steps": STEPS_SCHEMA,
            "cleanup": {"type": "object"},
        },
    },
    group_key=GROUP_STORAGE_ADAPTERS,
    tags=("write", "srm", "live-recovery", "appliance", "storage-adapter", "destructive"),
    safety_level="destructive",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when a person wants a storage vendor's replication adapter on the SRM "
            "appliance, for array-based replication. Agents cannot run it."
        ),
        "preconditions": (
            "A person called preview_operation with the same inputs and passes its "
            "preview_hash. The archive and its SHA-256 come from the storage vendor. A "
            "signed archive URL goes into a Vault secret (field url) named by "
            "archive_url_secret_ref, never into archive_url. Use async: true: the download "
            "and upload can take many minutes."
        ),
        "output_shape": (
            "outcome installed or failed; installed (true, false, or null when unknown "
            "after a broken upload); adapter (id, name, vendor, version, image_id); "
            "archive (name, source without query, size, sha256, expected_sha256); "
            "adapters_before, adapters_after; steps; cleanup (temporary_folder_removed, "
            "leftovers_removed)."
        ),
        "next_step": (
            "installed: rescan the adapters in SRM and set up the array manager. failed "
            "with installed false: fix the cause in the failed step and run again. "
            "installed null: read the adapter list on the appliance before a new run."
        ),
    },
)

APPLIANCE_MORE_TYPED_OPS: Final[tuple[SrmTypedOp, ...]] = (
    _CSR_CREATE,
    _CERTIFICATE_SET,
    _SRA_INSTALL,
)
