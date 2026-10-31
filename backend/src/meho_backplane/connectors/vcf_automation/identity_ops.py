# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed VCFA ops for how people sign in (provider plane, VCF Automation 9.x).

* ``vcfa.provider.identity.show`` -- safe: the provider's LDAP, and each
  org's LDAP and OIDC, as flat rows. Never a password or client secret.
* ``vcfa.provider.ldap.set`` -- dangerous, approval: the provider's own LDAP.
* ``vcfa.provider.org.ldap.set`` -- dangerous, approval: an org's LDAP.
* ``vcfa.provider.org.oidc.set`` -- dangerous, approval: an org's OIDC.

A bind password or client secret is always a Vault path (``*_secret_ref``);
MEHO reads it when the approved call runs and sends it only to VCF
Automation. The three changes carry the ``credential`` tag and are pinned as
credential writes in ``broadcast/events.py``. Handlers live in
:mod:`._identity`, :mod:`._identity_ldap` and :mod:`._identity_oidc`; the
approval previews in :mod:`._identity_preview`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcf_automation._op_schemas import field, guidance, list_of, obj
from meho_backplane.connectors.vcf_automation._paths import (
    PROVIDER_LDAP_SETTINGS_PATH,
    PROVIDER_ORG_LDAP_SETTINGS_PATH,
    PROVIDER_ORG_OAUTH_SETTINGS_PATH,
)
from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp, validate_typed_ops

__all__ = ["VCFA_IDENTITY_GROUP", "VCFA_IDENTITY_OPS"]

#: The group of the four sign-in ops.
VCFA_IDENTITY_GROUP: Final = "vcfa-provider-identity"

_ORG_NAME: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 256,
    "description": "The org name, as vcfa.provider.org.list shows it.",
}

_SHA256: dict[str, Any] = {
    "type": "string",
    "pattern": r"^(?:[0-9A-Fa-f]{64}|(?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2})$(?!\n)",
    "description": (
        "LDAPS only: the SHA-256 fingerprint of the server certificate MEHO may trust. "
        "Take it from an earlier answer (certificate_sha256) and check it with the "
        "server owner. MEHO trusts the certificate only when the server shows this one."
    ),
}

_ATTRIBUTE = {"type": "string", "minLength": 1, "description": "An LDAP attribute name."}

_USER_ATTRIBUTES: dict[str, Any] = {
    "type": "object",
    "description": (
        "How LDAP user entries map to users. Default for ACTIVE_DIRECTORY: objectClass "
        "'user', userName 'sAMAccountName', email 'mail'. Required for OPENLDAP."
    ),
    "properties": {
        name: {**_ATTRIBUTE, "description": f"The LDAP attribute for the user's {text}."}
        for name, text in (
            ("object_class", "object class"),
            ("object_identifier", "unique id"),
            ("user_name", "login name"),
            ("email", "email address"),
            ("full_name", "full name"),
            ("given_name", "given name"),
            ("surname", "surname"),
            ("telephone", "phone number"),
            ("group_membership_identifier", "group membership id"),
            ("group_back_link_identifier", "group back link"),
        )
    },
    "required": [
        "object_class",
        "object_identifier",
        "user_name",
        "email",
        "full_name",
        "given_name",
        "surname",
        "telephone",
        "group_membership_identifier",
    ],
    "additionalProperties": False,
}

_GROUP_ATTRIBUTES: dict[str, Any] = {
    "type": "object",
    "description": (
        "How LDAP group entries map to groups. Default for ACTIVE_DIRECTORY: objectClass "
        "'group', groupName 'cn', membership 'member'. Required for OPENLDAP."
    ),
    "properties": {
        name: {**_ATTRIBUTE, "description": f"The LDAP attribute for the group's {text}."}
        for name, text in (
            ("object_class", "object class"),
            ("object_identifier", "unique id"),
            ("group_name", "name"),
            ("membership", "members"),
            ("membership_identifier", "member id"),
            ("back_link_identifier", "back link"),
        )
    },
    "required": [
        "object_class",
        "object_identifier",
        "group_name",
        "membership",
        "membership_identifier",
    ],
    "additionalProperties": False,
}

#: The LDAP server fields shared by the provider and the org change.
_SERVER_PROPS: dict[str, Any] = {
    "host": {
        "type": "string",
        "minLength": 1,
        "maxLength": 253,
        "description": "The LDAP server host name or IP address.",
    },
    "port": {
        "type": "integer",
        "minimum": 1,
        "maximum": 65535,
        "description": "The LDAP port (default 636 with is_ssl, else 389).",
    },
    "is_ssl": {"type": "boolean", "description": "Use LDAPS (default false)."},
    "search_base": {
        "type": "string",
        "minLength": 1,
        "description": "The base DN where users and groups are searched.",
    },
    "group_search_base": {
        "type": "string",
        "minLength": 1,
        "description": "A separate base DN for groups (optional).",
    },
    "bind_user": {
        "type": "string",
        "minLength": 1,
        "description": "The user VCF Automation binds with (a DN or user name).",
    },
    "password_secret_ref": {
        "type": "string",
        "minLength": 1,
        "description": (
            "Vault KV-v2 path that holds the bind password, read under your identity "
            "when the approved call runs. Never pass the password itself."
        ),
    },
    "password_secret_mount": {
        "type": "string",
        "minLength": 1,
        "description": "KV-v2 mount of password_secret_ref (default 'secret').",
    },
    "password_secret_key": {
        "type": "string",
        "minLength": 1,
        "description": "Field of the secret that holds the password (default 'password').",
    },
    "connector_type": {
        "type": "string",
        "enum": ["ACTIVE_DIRECTORY", "OPENLDAP"],
        "description": "The kind of directory.",
    },
    "authentication_mechanism": {
        "type": "string",
        "enum": ["SIMPLE", "KERBEROS", "MD5DIGEST", "NTLM"],
        "description": "How VCF Automation binds (default SIMPLE).",
    },
    "user_attributes": _USER_ATTRIBUTES,
    "group_attributes": _GROUP_ATTRIBUTES,
    "trust_certificate_sha256": _SHA256,
}

_SERVER_REQUIRED: Final = [
    "host",
    "search_base",
    "bind_user",
    "password_secret_ref",
    "connector_type",
]

_LDAP_ROW: dict[str, Any] = {
    "applies_to": field("string", "'provider' or 'org'.", enum=["provider", "org"]),
    "org": field(["string", "null"], "The org name; null for the provider."),
    "org_id": field(["string", "null"], "The org id (a URN); null for the provider."),
    "kind": field("string", "'ldap' or 'oidc'.", enum=["ldap", "oidc"]),
    "ldap_mode": field(["string", "null"], "LDAP: NONE, SYSTEM (the provider's) or CUSTOM."),
    "host": field(["string", "null"], "LDAP server host."),
    "port": field(["integer", "null"], "LDAP server port."),
    "is_ssl": field(["boolean", "null"], "True for LDAPS."),
    "search_base": field(["string", "null"], "The base DN."),
    "bind_user": field(["string", "null"], "The bind user. The password is never shown."),
    "connector_type": field(["string", "null"], "ACTIVE_DIRECTORY or OPENLDAP."),
    "authentication_mechanism": field(["string", "null"], "How VCF Automation binds."),
    "custom_users_ou": field(["string", "null"], "SYSTEM mode: the OU of the org's users."),
}
_OIDC_ROW: dict[str, Any] = {
    "enabled": field(["boolean", "null"], "OIDC: true when the sign-in is on."),
    "well_known_endpoint": field(["string", "null"], "The identity provider's well-known URL."),
    "issuer": field(["string", "null"], "The issuer id."),
    "client_id": field(["string", "null"], "The client id. The secret is never shown."),
    "scope": field(["string", "null"], "The scopes, space separated."),
    "claim_subject": field(["string", "null"], "The claim of the user name."),
    "claim_email": field(["string", "null"], "The claim of the email."),
    "claim_full_name": field(["string", "null"], "The claim of the full name."),
    "claim_first_name": field(["string", "null"], "The claim of the first name."),
    "claim_last_name": field(["string", "null"], "The claim of the last name."),
    "claim_groups": field(["string", "null"], "The claim of the groups."),
    "claim_roles": field(["string", "null"], "The claim of the roles."),
}
_ROW: dict[str, Any] = {
    **_LDAP_ROW,
    **_OIDC_ROW,
    "error": field(["string", "null"], "Why this row could not be read; null when it was."),
}


def _change_response(statuses: list[str], extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": field("string", "What the op did.", enum=statuses),
            "settings": {
                **obj("The settings after the change, as a row of identity.show.", _ROW),
                "type": ["object", "null"],
            },
            "vendor_message": field(
                ["string", "null"], "The vendor's error, with any secret replaced."
            ),
            "guidance": field(["string", "null"], "What to do next, or null."),
            **extra,
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    }


_CERT_RESPONSE: dict[str, Any] = {
    "certificate_sha256": field(
        ["string", "null"], "The SHA-256 fingerprint of the LDAPS certificate, when one was seen."
    ),
    "trusted_certificate_id": field(
        ["string", "null"], "The id of the certificate this call trusted, or null."
    ),
}
_LDAP_STATUSES: Final = [
    "set",
    "unreachable",
    "tls_failed",
    "certificate_not_trusted",
    "certificate_mismatch",
    "refused",
    "invalid_request",
]

_IDENTITY_SHOW = VcfaTypedOp(
    op_id="vcfa.provider.identity.show",
    handler_attr="provider_identity_show",
    plane="provider",
    path=PROVIDER_LDAP_SETTINGS_PATH,
    summary="Show how people sign in: the provider's LDAP and each org's LDAP and OIDC.",
    description=(
        "Reads the provider's LDAP settings and, for each org, its LDAP and OIDC settings, "
        "with the classic /api calls. One flat row per setting. A password or client "
        "secret is never shown, even if VCF Automation sends it."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "org": {
                **_ORG_NAME,
                "description": "Only this org (without it: the provider and all orgs).",
            },
        },
        "additionalProperties": False,
        "examples": [{}, {"org": "<org-name>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": list_of(
                "One row per setting: the provider's LDAP, each org's LDAP and OIDC.", _ROW
            ),
            "org_count": field("integer", "How many orgs were read."),
            "guidance": field(["string", "null"], "A note, for example when the org is unknown."),
        },
        "required": ["rows", "org_count"],
        "additionalProperties": True,
    },
    group_key=VCFA_IDENTITY_GROUP,
    tags=("read-only", "vcfa", "provider", "identity", "ldap", "oidc"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call to see how people sign in to VCF Automation before you change LDAP or OIDC, "
            "and after a change to check it."
        ),
        output_shape=(
            "{rows: [{applies_to, org, org_id, kind: ldap | oidc, ldap_mode, host, port, "
            "is_ssl, search_base, bind_user, connector_type, enabled, well_known_endpoint, "
            "issuer, client_id, scope, claim_*, error}], org_count, guidance}. Fields of the "
            "other kind are null. A long list comes back as a result handle; read it with "
            "result_query."
        ),
        next_step=(
            "Change the provider's LDAP with vcfa.provider.ldap.set, an org's LDAP with "
            "vcfa.provider.org.ldap.set, an org's OIDC with vcfa.provider.org.oidc.set."
        ),
        result_scalars={"keys": ["org_count", "guidance"]},
    ),
)

_LDAP_SET = VcfaTypedOp(
    op_id="vcfa.provider.ldap.set",
    handler_attr="provider_ldap_set",
    plane="provider",
    path=PROVIDER_LDAP_SETTINGS_PATH,
    summary="Set or change the provider's own LDAP connection (bind password from Vault).",
    description=(
        "Tests the connection, then sets the provider's LDAP with PUT on the classic "
        "/api ldapSettings path. The bind password comes from Vault. An LDAPS certificate "
        "is trusted only when the call names its SHA-256 fingerprint."
    ),
    parameter_schema={
        "type": "object",
        "properties": _SERVER_PROPS,
        "required": _SERVER_REQUIRED,
        "additionalProperties": False,
        "examples": [
            {
                "host": "<ldap-host>",
                "is_ssl": True,
                "search_base": "<base-dn>",
                "bind_user": "<bind-user>",
                "password_secret_ref": "<vault-path>",
                "connector_type": "ACTIVE_DIRECTORY",
            }
        ],
    },
    response_schema=_change_response(_LDAP_STATUSES, _CERT_RESPONSE),
    group_key=VCFA_IDENTITY_GROUP,
    tags=("write", "vcfa", "provider", "identity", "ldap", "credential"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to connect the provider (the System org) to an LDAP directory.",
        preconditions=(
            "Store the bind password in Vault first and pass its path in password_secret_ref. "
            "A person must approve the call. For LDAPS, the first call answers "
            "certificate_not_trusted with certificate_sha256 when VCF Automation does not "
            "trust the server yet; check that fingerprint with the server owner, then call "
            "again with trust_certificate_sha256."
        ),
        output_shape=(
            "{status: set | unreachable | tls_failed | certificate_not_trusted | "
            "certificate_mismatch | refused | invalid_request, settings, certificate_sha256, "
            "trusted_certificate_id, vendor_message, guidance}. Only 'set' changed anything."
        ),
        next_step="Check the result with vcfa.provider.identity.show.",
    ),
)

_ORG_LDAP_SET = VcfaTypedOp(
    op_id="vcfa.provider.org.ldap.set",
    handler_attr="provider_org_ldap_set",
    plane="provider",
    path=PROVIDER_ORG_LDAP_SETTINGS_PATH,
    summary="Set, change or turn off an org's LDAP: none, the provider's LDAP, or its own.",
    description=(
        "Sets an org's LDAP with PUT on the classic /api org ldap path: mode none (off), "
        "system (the provider's LDAP) or custom (its own server; the bind password from "
        "Vault). Refuses the provider's own org."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "org": _ORG_NAME,
            "mode": {
                "type": "string",
                "enum": ["none", "system", "custom"],
                "description": "none: no LDAP. system: the provider's LDAP. custom: own server.",
            },
            "custom_users_ou": {
                "type": "string",
                "minLength": 1,
                "description": "Mode system only: the OU that holds this org's users.",
            },
            **_SERVER_PROPS,
        },
        "required": ["org", "mode"],
        "oneOf": [
            {
                "description": "Mode none or system: no server fields.",
                "properties": {"mode": {"enum": ["none", "system"], "description": "The mode."}},
            },
            {
                "description": "Mode custom: the org's own server.",
                "properties": {"mode": {"const": "custom", "description": "The mode."}},
                "required": _SERVER_REQUIRED,
            },
        ],
        "additionalProperties": False,
        "examples": [
            {"org": "<org-name>", "mode": "system"},
            {
                "org": "<org-name>",
                "mode": "custom",
                "host": "<ldap-host>",
                "search_base": "<base-dn>",
                "bind_user": "<bind-user>",
                "password_secret_ref": "<vault-path>",
                "connector_type": "ACTIVE_DIRECTORY",
            },
        ],
    },
    response_schema=_change_response([*_LDAP_STATUSES, "refused_by_meho"], _CERT_RESPONSE),
    group_key=VCFA_IDENTITY_GROUP,
    tags=("write", "vcfa", "provider", "identity", "ldap", "credential"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to choose how the users of one org sign in with LDAP.",
        preconditions=(
            "Mode custom needs the bind password in Vault (password_secret_ref). A person "
            "must approve the call. Mode none stops LDAP sign-in for the org. MEHO refuses "
            "the provider's own org (System): use vcfa.provider.ldap.set."
        ),
        output_shape=(
            "{status: set | unreachable | tls_failed | certificate_not_trusted | "
            "certificate_mismatch | refused | refused_by_meho | invalid_request, settings, "
            "certificate_sha256, trusted_certificate_id, vendor_message, guidance}. Only "
            "'set' changed anything."
        ),
        next_step="Check the result with vcfa.provider.identity.show and org.",
    ),
)

_CLAIM = {"type": "string", "minLength": 1}

_ORG_OIDC_SET = VcfaTypedOp(
    op_id="vcfa.provider.org.oidc.set",
    handler_attr="provider_org_oidc_set",
    plane="provider",
    path=PROVIDER_ORG_OAUTH_SETTINGS_PATH,
    summary="Set, change or remove an org's OIDC sign-in (client secret from Vault).",
    description=(
        "Reads the identity provider's well-known address through VCF Automation, adds "
        "the client id, the client secret from Vault and the claim names, and saves the "
        "org's OIDC settings. state absent removes them. Refuses to remove or turn off the "
        "System org's."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "org": _ORG_NAME,
            "state": {
                "type": "string",
                "enum": ["present", "absent"],
                "description": "present (default) sets the OIDC sign-in; absent removes it.",
            },
            "well_known_endpoint": {
                "type": "string",
                "pattern": "^https://[^\\s]+$(?!\\n)",
                "description": "The identity provider's .well-known/openid-configuration URL.",
            },
            "client_id": {
                "type": "string",
                "minLength": 1,
                "description": "The client id VCF Automation uses at the identity provider.",
            },
            "client_secret_ref": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Vault KV-v2 path that holds the client secret, read under your identity "
                    "when the approved call runs. Never pass the secret itself."
                ),
            },
            "client_secret_mount": {
                "type": "string",
                "minLength": 1,
                "description": "KV-v2 mount of client_secret_ref (default 'secret').",
            },
            "client_secret_key": {
                "type": "string",
                "minLength": 1,
                "description": "Field of the secret that holds it (default 'client_secret').",
            },
            "claims": {
                "type": "object",
                "description": (
                    "Claim names that override what the well-known address gives. VCF "
                    "Automation needs subject, email, and full_name or first and last name."
                ),
                "properties": {
                    "subject": {**_CLAIM, "description": "The claim of the user name."},
                    "email": {**_CLAIM, "description": "The claim of the email."},
                    "full_name": {**_CLAIM, "description": "The claim of the full name."},
                    "first_name": {**_CLAIM, "description": "The claim of the first name."},
                    "last_name": {**_CLAIM, "description": "The claim of the last name."},
                    "groups": {**_CLAIM, "description": "The claim of the groups."},
                    "roles": {**_CLAIM, "description": "The claim of the roles."},
                },
                "additionalProperties": False,
            },
            "scope": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "description": "One scope."},
                "minItems": 1,
                "description": "The scopes to ask for (default: what the well-known gives).",
            },
            "enabled": {"type": "boolean", "description": "Turn the sign-in on (default true)."},
        },
        "required": ["org"],
        "oneOf": [
            {
                "description": "Set: the well-known address, the client id and its secret.",
                "properties": {"state": {"const": "present", "description": "Set the sign-in."}},
                "required": ["well_known_endpoint", "client_id", "client_secret_ref"],
            },
            {
                "description": "Remove: only the org.",
                "properties": {"state": {"const": "absent", "description": "Remove it."}},
                "required": ["state"],
            },
        ],
        "additionalProperties": False,
        "examples": [
            {
                "org": "<org-name>",
                "well_known_endpoint": "https://<idp-host>/.well-known/openid-configuration",
                "client_id": "<client-id>",
                "client_secret_ref": "<vault-path>",
                "claims": {"subject": "preferred_username"},
            }
        ],
    },
    response_schema=_change_response(
        ["set", "removed", "well_known_failed", "refused", "refused_by_meho", "invalid_request"],
        {},
    ),
    group_key=VCFA_IDENTITY_GROUP,
    tags=("write", "vcfa", "provider", "identity", "oidc", "credential"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to let the users of one org sign in through an OIDC identity provider.",
        preconditions=(
            "Register VCF Automation as a client at the identity provider and store the client "
            "secret in Vault (client_secret_ref). A person must approve the call. MEHO refuses "
            "to remove or turn off the OIDC sign-in of the provider's own org (System)."
        ),
        output_shape=(
            "{status: set | removed | well_known_failed | refused | refused_by_meho | "
            "invalid_request, settings, vendor_message, guidance}. Only 'set' and 'removed' "
            "changed anything."
        ),
        next_step="Check the result with vcfa.provider.identity.show and org.",
    ),
)

#: The sign-in ops: the read, then the three changes.
VCFA_IDENTITY_OPS: Final[tuple[VcfaTypedOp, ...]] = (
    _IDENTITY_SHOW,
    _LDAP_SET,
    _ORG_LDAP_SET,
    _ORG_OIDC_SET,
)


validate_typed_ops(VCFA_IDENTITY_OPS)
