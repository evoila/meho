# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of ``hcx.site_pairing.set`` and ``hcx.certificate.trust``.

Both are ``dangerous`` and wait for a person's approval. They are in the
group ``hcx-manager``, next to ``hcx.site_pairing.list``. The bodies live
in :mod:`meho_backplane.connectors.hcx.site_pairing_writes`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.hcx.op_types import GROUP_MANAGER, HcxTypedOp, string_schema

__all__ = ["HCX_SITE_PAIRING_OPS"]

_CERT_FIELDS: Final[dict[str, Any]] = {
    "certificate": string_schema("The certificate in PEM form."),
    "sha256": string_schema("SHA-256 fingerprint of the certificate, hex with colons."),
    "subject": string_schema("Subject of the certificate."),
    "issuer": string_schema("Issuer of the certificate."),
    "not_after": string_schema("End of the certificate's validity, ISO 8601."),
}

_SITE_PAIRING_SET = HcxTypedOp(
    op_id="hcx.site_pairing.set",
    handler_attr="site_pairing_set",
    summary="Pair this HCX Manager with a remote HCX site, or remove a site pairing.",
    description=(
        "action pair registers the remote HCX Manager at url, with the user and password "
        "read from the Vault path remote_secret_ref. action unpair removes the pairing "
        "of endpoint_id, but not while a service mesh uses it."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["pair", "unpair"],
                "description": "pair: add a pairing. unpair: remove one.",
            },
            "url": {
                "type": "string",
                "pattern": "^https://[^\\s/?#]+(:[0-9]+)?/?$",
                "description": "pair: address of the remote HCX Manager, https://<host>[:port].",
            },
            "remote_secret_ref": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "pair: Vault path of the secret with username and password of the "
                    "remote site. Never the password itself."
                ),
            },
            "endpoint_id": {
                "type": "string",
                "minLength": 1,
                "description": "unpair: id of the paired site (endpointId of the pairing).",
            },
        },
        "required": ["action"],
        "oneOf": [
            {
                "properties": {
                    "action": {"const": "pair", "description": "Add a pairing."},
                },
                "required": ["url", "remote_secret_ref"],
                "not": {"required": ["endpoint_id"]},
            },
            {
                "properties": {
                    "action": {"const": "unpair", "description": "Remove a pairing."},
                },
                "required": ["endpoint_id"],
                "not": {"anyOf": [{"required": ["url"]}, {"required": ["remote_secret_ref"]}]},
            },
        ],
        "additionalProperties": False,
        "examples": [
            {
                "action": "pair",
                "url": "https://<remote-hcx-host>",
                "remote_secret_ref": "<vault-path>",
            },
            {"action": "unpair", "endpoint_id": "<endpoint-id>"},
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": [
                    "accepted",
                    "already_paired",
                    "certificate_not_trusted",
                    "in_use",
                    "removed",
                ],
                "description": (
                    "accepted: the pairing job started. already_paired: a pairing with this "
                    "url exists, nothing was sent. certificate_not_trusted: the Manager does "
                    "not trust the remote certificate, nothing was paired. in_use: a service "
                    "mesh uses the site, nothing was removed. removed: the pairing is gone."
                ),
            },
            "action": {"type": "string", "description": "pair or unpair."},
            "url": string_schema("Address of the remote HCX Manager (pair)."),
            "endpoint_id": string_schema("Id of the paired site (already_paired, unpair)."),
            "job_id": string_schema("Id of the pairing job to follow with hcx.job.get."),
            "status_read": string_schema("The read that follows the job."),
            "remote_certificate": {
                "type": ["object", "null"],
                "description": "certificate_not_trusted: the certificate the remote site showed.",
                "properties": _CERT_FIELDS,
            },
            "service_mesh_ids": {
                "type": "array",
                "description": "in_use: the service meshes that use the site.",
                "items": {"type": "string"},
            },
            "warnings": {
                "type": "array",
                "description": "Warnings the Manager returned.",
                "items": {"type": "object", "properties": {"message": string_schema("Text.")}},
            },
        },
    },
    group_key=GROUP_MANAGER,
    tags=("write", "hcx", "site-pairing"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call on the source site's HCX Manager (the connector) to pair it with a "
            "destination site before any compute profile or service mesh can use that "
            "site, or to remove a pairing that is no longer needed."
        ),
        "preconditions": (
            "pair: the Vault secret at remote_secret_ref holds username and password of "
            "an admin of the remote site. unpair: no service mesh uses the site "
            "(hcx.service_mesh.list); delete those meshes first. It waits for a person's "
            "approval."
        ),
        "output_shape": (
            "{status: accepted|already_paired|certificate_not_trusted|in_use|removed, action, "
            "url, endpoint_id, job_id, status_read, remote_certificate: {certificate, sha256, "
            "subject, issuer, not_after}, service_mesh_ids, warnings}. No password comes "
            "back. The op sends a password, so an error shows its code and HTTP status but "
            "not the vendor's text."
        ),
        "next_step": (
            "accepted: call hcx.job.get with job_id until it is done, then "
            "hcx.site_pairing.list. certificate_not_trusted: have a person check "
            "remote_certificate.sha256 against the remote site, call hcx.certificate.trust "
            "with the certificate and that sha256, then call this again. in_use: delete "
            "the meshes in service_mesh_ids first. After an error, error_code and "
            "http_status say what failed; check the url and the Vault secret."
        ),
    },
)

_CERTIFICATE_TRUST = HcxTypedOp(
    op_id="hcx.certificate.trust",
    handler_attr="certificate_trust",
    summary="Add a remote site's certificate to the HCX Manager's trust store.",
    description=(
        "Imports one PEM certificate into the trusted certificates of this HCX Manager, "
        "so a site pairing with that site can be made. With expected_sha256 it refuses a "
        "certificate with another fingerprint. A private key is refused."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "certificate": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "One certificate in PEM form, for example remote_certificate.certificate "
                    "of hcx.site_pairing.set."
                ),
            },
            "expected_sha256": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "SHA-256 fingerprint the certificate must have: 64 hex digits, with or "
                    "without colons. A person checks it against the remote site."
                ),
            },
        },
        "required": ["certificate"],
        "additionalProperties": False,
        "examples": [
            {
                "certificate": "<pem-certificate>",
                "expected_sha256": "<sha256-fingerprint>",
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["trusted"],
                "description": "trusted: the Manager added the certificate.",
            },
            **{key: value for key, value in _CERT_FIELDS.items() if key != "certificate"},
        },
    },
    group_key=GROUP_MANAGER,
    tags=("write", "hcx", "certificate", "site-pairing"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when hcx.site_pairing.set answered certificate_not_trusted, to trust the "
            "remote site's certificate before the pairing."
        ),
        "preconditions": (
            "A person has checked the fingerprint against the remote site; pass it as "
            "expected_sha256. Trusting a wrong certificate lets another system act as the "
            "remote site. It waits for a person's approval."
        ),
        "output_shape": "{status: trusted, sha256, subject, issuer, not_after}.",
        "next_step": "Call hcx.site_pairing.set with action pair again.",
    },
)

HCX_SITE_PAIRING_OPS: Final[tuple[HcxTypedOp, ...]] = (_SITE_PAIRING_SET, _CERTIFICATE_TRUST)
