# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handlers of ``vcfa.provider.ldap.set`` and ``vcfa.provider.org.ldap.set``.

Both follow ``go-vcloud-director`` v3 ``govcd/tm_ldap.go``:

1. ``POST /cloudapi/1.0.0/testConnection`` checks that the appliance reaches
   the LDAP server. A server it cannot reach answers ``unreachable``.
2. For LDAPS, a server certificate the appliance does not trust yet answers
   ``certificate_not_trusted`` with its SHA-256 fingerprint. The call must
   then name that fingerprint in ``trust_certificate_sha256``; a person
   approves the call, and the run trusts the certificate
   (``POST /cloudapi/1.0.0/ssl/trustedCertificates``) only when the
   certificate the server shows now has that fingerprint.
3. The bind password is read from Vault (``password_secret_ref``) and sent
   only in the ``PUT`` body of the classic LDAP settings path.
4. When the vendor refuses the ``PUT`` (a wrong bind password, a search base
   it cannot read), MEHO answers ``refused`` with the vendor's message, and
   removes a certificate this run trusted, so nothing changes.

The org op also sets the modes ``none`` (no LDAP) and ``system`` (the
provider's LDAP). It refuses the provider's own org: the provider admin
signs in there, and the provider's LDAP is set with ``vcfa.provider.ldap.set``.
"""

from __future__ import annotations

import hashlib
import ssl
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vcf_automation._identity import (
    CLASSIC_JSON,
    http_error,
    is_provider_org,
    ldap_view,
    org_path,
    pick,
    read_secret,
    secret_missing_guidance,
)
from meho_backplane.connectors.vcf_automation._lookups import (
    find_org,
    quote_segment,
    tenant_context_headers,
)
from meho_backplane.connectors.vcf_automation._paths import (
    PROVIDER_LDAP_SETTINGS_PATH,
    PROVIDER_ORG_LDAP_SETTINGS_PATH,
    PROVIDER_TEST_CONNECTION_PATH,
    PROVIDER_TRUSTED_CERTIFICATE_PATH,
    PROVIDER_TRUSTED_CERTIFICATES_PATH,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "LDAP_ATTRIBUTE_DEFAULTS",
    "certificate_sha256",
    "probe_ldap_server",
    "provider_ldap_set",
    "provider_org_ldap_set",
    "same_fingerprint",
]

#: The SSL result of a server whose certificate the appliance does not trust.
_UNTRUSTED: Final = "ERROR_UNTRUSTED_CERTIFICATE"
_PEM_END: Final = "-----END CERTIFICATE-----"

#: User and group attributes for Active Directory: the values the vendor SDK's
#: own LDAP test uses. Other directories must give both maps.
LDAP_ATTRIBUTE_DEFAULTS: Final[dict[str, dict[str, dict[str, str]]]] = {
    "ACTIVE_DIRECTORY": {
        "userAttributes": {
            "objectClass": "user",
            "objectIdentifier": "objectGuid",
            "userName": "sAMAccountName",
            "email": "mail",
            "fullName": "displayName",
            "givenName": "givenName",
            "surname": "sn",
            "telephone": "telephoneNumber",
            "groupMembershipIdentifier": "dn",
            "groupBackLinkIdentifier": "tokenGroups",
        },
        "groupAttributes": {
            "objectClass": "group",
            "objectIdentifier": "objectGuid",
            "groupName": "cn",
            "membership": "member",
            "membershipIdentifier": "dn",
            "backLinkIdentifier": "objectSid",
        },
    },
}

#: Caller attribute names → vendor attribute names.
_USER_ATTRIBUTES: Final = {
    "object_class": "objectClass",
    "object_identifier": "objectIdentifier",
    "user_name": "userName",
    "email": "email",
    "full_name": "fullName",
    "given_name": "givenName",
    "surname": "surname",
    "telephone": "telephone",
    "group_membership_identifier": "groupMembershipIdentifier",
    "group_back_link_identifier": "groupBackLinkIdentifier",
}
_GROUP_ATTRIBUTES: Final = {
    "object_class": "objectClass",
    "object_identifier": "objectIdentifier",
    "group_name": "groupName",
    "membership": "membership",
    "membership_identifier": "membershipIdentifier",
    "back_link_identifier": "backLinkIdentifier",
}


def certificate_sha256(chain: str) -> tuple[str, str] | None:
    """``(pem, fingerprint)`` of the certificate to trust in *chain*, or ``None``.

    Like ``go-vcloud-director`` ``trustCertificate``: a single certificate is
    trusted as it is; of a chain, the last certificate is trusted. The
    fingerprint is the SHA-256 of the DER bytes, upper-case hex pairs with
    ``:`` between them.
    """
    parts = [part.strip() for part in chain.split(_PEM_END) if part.strip()]
    if not parts:
        return None
    pem = parts[-1][parts[-1].find("-----BEGIN") :] + "\n" + _PEM_END + "\n"
    try:
        der = ssl.PEM_cert_to_DER_cert(pem)
    except ValueError:
        return None
    digest = hashlib.sha256(der).hexdigest().upper()
    return pem, ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def same_fingerprint(given: Any, actual: str) -> bool:
    """True when *given* is *actual*, with or without ``:`` and in any case."""

    def norm(value: str) -> str:
        return value.replace(":", "").replace(" ", "").casefold()

    return isinstance(given, str) and norm(given) == norm(actual)


async def probe_ldap_server(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    server: Mapping[str, Any],
) -> dict[str, Any]:
    """Ask the appliance to connect to the LDAP server; return what it saw.

    Keys: ``reachable``, ``ssl_result``, ``result`` (the vendor's short
    text), and for an untrusted LDAPS certificate ``certificate_pem`` and
    ``certificate_sha256``.
    """
    is_ssl = bool(server.get("is_ssl"))
    answer = await connector._post_json(
        target,
        PROVIDER_TEST_CONNECTION_PATH,
        operator=operator,
        json={
            "host": server["host"],
            "port": int(server.get("port") or (636 if is_ssl else 389)),
            "secure": is_ssl,
            "hostnameVerificationAlgorithm": "LDAPS",
            "timeout": 30,
        },
    )
    probe = pick(answer, "targetProbe")
    probe = probe if isinstance(probe, Mapping) else {}
    seen: dict[str, Any] = {
        "reachable": bool(probe.get("canConnect")) and probe.get("connectionResult") == "SUCCESS",
        "ssl_result": probe.get("sslResult"),
        "result": probe.get("result"),
        "certificate_pem": None,
        "certificate_sha256": None,
    }
    if is_ssl and seen["ssl_result"] == _UNTRUSTED:
        found = certificate_sha256(str(probe.get("certificateChain") or ""))
        if found is not None:
            seen["certificate_pem"], seen["certificate_sha256"] = found
    return seen


def _server_body(params: Mapping[str, Any], password: str) -> dict[str, Any]:
    """The LDAP server part of the vendor body (``TmLdapSettings`` field names)."""
    connector_type = params["connector_type"]
    defaults = LDAP_ATTRIBUTE_DEFAULTS.get(connector_type, {})
    users = params.get("user_attributes")
    groups = params.get("group_attributes")
    is_ssl = bool(params.get("is_ssl"))
    body: dict[str, Any] = {
        "hostName": params["host"],
        "port": int(params.get("port") or (636 if is_ssl else 389)),
        "isSsl": is_ssl,
        "searchBase": params["search_base"],
        "userName": params["bind_user"],
        "password": password,
        "authenticationMechanism": params.get("authentication_mechanism") or "SIMPLE",
        "connectorType": connector_type,
        "isGroupSearchBaseEnabled": bool(params.get("group_search_base")),
        "groupSearchBase": params.get("group_search_base") or "",
        "userAttributes": (
            {_USER_ATTRIBUTES[k]: v for k, v in users.items()}
            if isinstance(users, Mapping)
            else defaults.get("userAttributes")
        ),
        "groupAttributes": (
            {_GROUP_ATTRIBUTES[k]: v for k, v in groups.items()}
            if isinstance(groups, Mapping)
            else defaults.get("groupAttributes")
        ),
    }
    return body


def _outcome(status: str, guidance: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "settings": None,
        "certificate_sha256": None,
        "trusted_certificate_id": None,
        "vendor_message": None,
        "guidance": guidance,
        **extra,
    }


async def _check_server(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    params: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Probe the server → ``(stop_answer, certificate_to_trust)``; both ``None`` to go on."""
    seen = await probe_ldap_server(connector, target, operator, params)
    host = params["host"]
    if not seen["reachable"]:
        return _outcome(
            "unreachable",
            f"VCF Automation cannot connect to the LDAP server {host!r} "
            f"({seen['result'] or 'no detail'}); check the host, the port and the firewall. "
            "Nothing was changed",
        ), None
    if not params.get("is_ssl") or seen["ssl_result"] in (None, "SUCCESS"):
        return None, None
    fingerprint = seen["certificate_sha256"]
    if seen["ssl_result"] != _UNTRUSTED or not fingerprint:
        return _outcome(
            "tls_failed",
            f"the LDAPS check of {host!r} failed ({seen['ssl_result']}: "
            f"{seen['result'] or 'no detail'}). Nothing was changed",
        ), None
    wanted = params.get("trust_certificate_sha256")
    if not wanted:
        return _outcome(
            "certificate_not_trusted",
            "VCF Automation does not trust the certificate of this LDAP server yet. Check the "
            "fingerprint with the owner of the server. Then call again with "
            "trust_certificate_sha256 set to it; a person approves the trust. "
            "Nothing was changed",
            certificate_sha256=fingerprint,
        ), None
    if not same_fingerprint(wanted, fingerprint):
        return _outcome(
            "certificate_mismatch",
            "the LDAP server now shows a certificate with another fingerprint than "
            "trust_certificate_sha256. Nothing was trusted or changed. Check the server "
            "before you call again",
            certificate_sha256=fingerprint,
        ), None
    return None, {"pem": seen["certificate_pem"], "sha256": fingerprint}


async def _trust(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    host: str,
    pem: str,
    headers: dict[str, str] | None,
) -> str | None:
    """Trust *pem* (alias ``<host>_<UTC time>``, like the vendor SDK); return its id."""
    created = await connector._post_json(
        target,
        PROVIDER_TRUSTED_CERTIFICATES_PATH,
        operator=operator,
        json={
            "alias": f"{host}_{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "certificate": pem,
        },
        extra_headers=headers,
    )
    cert_id = created.get("id")
    return str(cert_id) if cert_id else None


async def _untrust(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    cert_id: str | None,
    headers: dict[str, str] | None,
) -> bool:
    """Remove a certificate this run trusted; ``False`` when that failed too."""
    if not cert_id:
        return False
    try:
        await connector._post_json(
            target,
            PROVIDER_TRUSTED_CERTIFICATE_PATH.format(id=quote_segment(cert_id)),
            operator=operator,
            verb="DELETE",
            extra_headers=headers,
        )
    except httpx.HTTPError:
        return False
    return True


async def _apply(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    params: Mapping[str, Any],
    *,
    path: str,
    build_body: Any,
    org: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Probe, trust, read the password, ``PUT``; undo the trust when the ``PUT`` fails."""
    stop, cert = await _check_server(connector, target, operator, params)
    if stop is not None:
        return stop
    password = await read_secret(operator, target, params, "password", "password")
    if password is None:
        return _outcome("invalid_request", secret_missing_guidance(params, "password", "password"))
    headers = tenant_context_headers(org) if org is not None else None
    cert_id = None
    if cert is not None:
        cert_id = await _trust(connector, target, operator, params["host"], cert["pem"], headers)
    try:
        saved = await connector._post_json(
            target,
            path,
            operator=operator,
            verb="PUT",
            json=build_body(password),
            extra_headers={"Content-Type": CLASSIC_JSON},
        )
    except httpx.HTTPStatusError as exc:
        undone = await _untrust(connector, target, operator, cert_id, headers)
        note = (
            ""
            if cert_id is None or undone
            else (
                f" MEHO could not remove the certificate it trusted (id {cert_id!r}); remove it "
                "in the VCF Automation console."
            )
        )
        return _outcome(
            "refused",
            "VCF Automation refused the LDAP settings (a wrong bind password, user or search "
            "base, or a server that refused the bind). The old settings stay." + note,
            vendor_message=http_error(exc, [password]),
            certificate_sha256=cert["sha256"] if cert else None,
        )
    return _outcome(
        "set",
        None,
        settings=ldap_view(saved, org),
        certificate_sha256=cert["sha256"] if cert else None,
        trusted_certificate_id=cert_id,
    )


async def provider_ldap_set(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.ldap.set`` — set or change the provider's own LDAP connection."""
    problem = _attributes_problem(params)
    if problem:
        return _outcome("invalid_request", problem)

    def body(password: str) -> dict[str, Any]:
        return {**_server_body(params, password), "pagedSearchDisabled": False}

    return await _apply(
        connector,
        target,
        operator,
        params,
        path=PROVIDER_LDAP_SETTINGS_PATH,
        build_body=body,
        org=None,
    )


def _attributes_problem(params: Mapping[str, Any]) -> str | None:
    if params["connector_type"] in LDAP_ATTRIBUTE_DEFAULTS:
        return None
    if params.get("user_attributes") and params.get("group_attributes"):
        return None
    return (
        f"connector_type {params['connector_type']!r} has no default attribute maps; give "
        "user_attributes and group_attributes. Nothing was sent"
    )


async def provider_org_ldap_set(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.org.ldap.set`` — an org's LDAP: none, the provider's, or its own."""
    name, mode = str(params["org"]), str(params["mode"])
    org = await find_org(connector, target, operator, name)
    if org is None:
        return _outcome("invalid_request", f"no org named {name!r}; nothing was sent")
    if is_provider_org(org):
        return _outcome(
            "refused_by_meho",
            f"org {org.get('name')!r} is the provider's own org: the provider admin signs in "
            "there. MEHO does not change or turn off its LDAP here. Set the provider's LDAP "
            "with vcfa.provider.ldap.set. Nothing was sent",
        )
    path = org_path(PROVIDER_ORG_LDAP_SETTINGS_PATH, org)
    if mode != "custom":
        body: dict[str, Any] = {"orgLdapMode": mode.upper()}
        if mode == "system" and params.get("custom_users_ou"):
            body["customUsersOu"] = params["custom_users_ou"]
        saved = await connector._post_json(
            target,
            path,
            operator=operator,
            verb="PUT",
            json=body,
            extra_headers={"Content-Type": CLASSIC_JSON},
        )
        return _outcome("set", None, settings=ldap_view(saved, org))
    problem = _attributes_problem(params)
    if problem:
        return _outcome("invalid_request", problem)

    def custom_body(password: str) -> dict[str, Any]:
        return {"orgLdapMode": "CUSTOM", "customOrgLdapSettings": _server_body(params, password)}

    return await _apply(
        connector, target, operator, params, path=path, build_body=custom_body, org=org
    )
