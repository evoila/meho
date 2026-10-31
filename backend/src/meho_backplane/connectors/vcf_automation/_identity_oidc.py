# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Handler of ``vcfa.provider.org.oidc.set``: set or remove an org's OIDC sign-in.

Follows ``go-vcloud-director`` v3 ``govcd/org_oidc.go``:

* **Set** (``state: present``). ``POST .../settings/oauth/openIdProviderConfig``
  makes the appliance read the identity provider's well-known address and
  answer the OIDC settings it found (the endpoints, the issuer, the keys and
  the default claims). An address that does not answer stops here:
  ``well_known_failed``, nothing changed. MEHO then sets the client id, the
  client secret (read from Vault, ``client_secret_ref``), the well-known
  address and the claim names the call gives, and sends the settings with
  ``PUT .../settings/oauth``. A refused ``PUT`` answers ``refused`` with the
  vendor's message, with the secret replaced. On success MEHO reads the
  settings again (the vendor SDK does the same: some versions answer the
  ``PUT`` with a wrong redirect address).
* **Remove** (``state: absent``). ``DELETE .../settings/oauth``.

For the provider's own org MEHO refuses to remove the OIDC sign-in or to
turn it off (``enabled: false``): the provider admin signs in there. It
answers ``refused_by_meho`` before any vendor call and before it reads Vault.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.vcf_automation._identity import (
    CLASSIC_JSON,
    http_error,
    is_provider_org,
    key_of,
    oidc_view,
    org_path,
    pick,
    read_secret,
    secret_missing_guidance,
)
from meho_backplane.connectors.vcf_automation._lookups import find_org
from meho_backplane.connectors.vcf_automation._paths import (
    PROVIDER_ORG_OAUTH_SETTINGS_PATH,
    PROVIDER_ORG_OIDC_PROVIDER_CONFIG_PATH,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = ["OIDC_CLAIM_NAMES", "provider_org_oidc_set"]

#: Caller claim names → vendor claim-mapping field names.
OIDC_CLAIM_NAMES: Final = {
    "subject": "subjectAttributeName",
    "email": "emailAttributeName",
    "full_name": "fullNameAttributeName",
    "first_name": "firstNameAttributeName",
    "last_name": "lastNameAttributeName",
    "groups": "groupsAttributeName",
    "roles": "rolesAttributeName",
}
#: Endpoints the vendor needs before it saves OIDC settings (the vendor SDK
#: checks the same three).
_NEEDED_ENDPOINTS: Final = ("userAuthorizationEndpoint", "accessTokenEndpoint", "userInfoEndpoint")


def _outcome(status: str, guidance: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "settings": None,
        "vendor_message": None,
        "guidance": guidance,
        **extra,
    }


def _merged(
    found: Mapping[str, Any], params: Mapping[str, Any], secret: str
) -> tuple[dict[str, Any], list[str]]:
    """The settings to send, and the names of required values that are still empty."""
    body = dict(found)
    body[key_of(body, "enabled")] = bool(params.get("enabled", True))
    body[key_of(body, "clientId")] = params["client_id"]
    body[key_of(body, "clientSecret")] = secret
    body[key_of(body, "wellKnownEndpoint")] = params["well_known_endpoint"]
    if params.get("scope"):
        body[key_of(body, "scope")] = list(params["scope"])
    mapping_key = key_of(body, "OIDCAttributeMapping")
    current = body.get(mapping_key)
    mapping = dict(current) if isinstance(current, Mapping) else {}
    for name, value in (params.get("claims") or {}).items():
        mapping[key_of(mapping, OIDC_CLAIM_NAMES[name])] = value
    body[mapping_key] = mapping
    missing = [name for name in _NEEDED_ENDPOINTS if not pick(body, name)]
    full_name = pick(mapping, "fullNameAttributeName")
    split_name = pick(mapping, "firstNameAttributeName") and pick(mapping, "lastNameAttributeName")
    missing += [
        f"claims.{claim}"
        for claim in ("subject", "email")
        if not pick(mapping, OIDC_CLAIM_NAMES[claim])
    ]
    if not (full_name or split_name):
        missing.append("claims.full_name (or claims.first_name and claims.last_name)")
    return body, missing


def _provider_org_refusal(org: Mapping[str, Any]) -> dict[str, Any]:
    return _outcome(
        "refused_by_meho",
        f"org {org.get('name')!r} is the provider's own org: the provider admin signs in "
        "there. MEHO does not remove or turn off its OIDC sign-in. Nothing was sent",
    )


async def _remove(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    org: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        await connector._post_json(
            target,
            org_path(PROVIDER_ORG_OAUTH_SETTINGS_PATH, org),
            operator=operator,
            verb="DELETE",
        )
    except httpx.HTTPStatusError as exc:
        return _outcome(
            "refused",
            "VCF Automation refused to remove the OIDC settings; they stay",
            vendor_message=http_error(exc),
        )
    return _outcome("removed", None)


async def provider_org_oidc_set(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.org.oidc.set`` — set, change or remove an org's OIDC sign-in."""
    name = str(params["org"])
    org = await find_org(connector, target, operator, name)
    if org is None:
        return _outcome("invalid_request", f"no org named {name!r}; nothing was sent")
    turns_off = params.get("state") == "absent" or params.get("enabled") is False
    if turns_off and is_provider_org(org):
        return _provider_org_refusal(org)
    if params.get("state") == "absent":
        return await _remove(connector, target, operator, org)
    oauth_path = org_path(PROVIDER_ORG_OAUTH_SETTINGS_PATH, org)
    well_known = params["well_known_endpoint"]
    try:
        config = await connector._post_json(
            target,
            org_path(PROVIDER_ORG_OIDC_PROVIDER_CONFIG_PATH, org),
            operator=operator,
            json={"openIdProviderConfigurationEndpoint": well_known},
            extra_headers={"Content-Type": CLASSIC_JSON},
        )
    except httpx.HTTPStatusError as exc:
        return _outcome(
            "well_known_failed",
            f"VCF Automation could not read the OpenID configuration at {well_known!r}. Check "
            "that the address answers from the appliance. Nothing was changed",
            vendor_message=http_error(exc),
        )
    found = pick(config, "orgOAuthSettings")
    secret = await read_secret(operator, target, params, "client", "client_secret")
    if secret is None:
        return _outcome(
            "invalid_request", secret_missing_guidance(params, "client", "client_secret")
        )
    body, missing = _merged(found if isinstance(found, Mapping) else {}, params, secret)
    if missing:
        return _outcome(
            "invalid_request",
            "the well-known address and the call give no value for: "
            + ", ".join(missing)
            + ". Give the claims in the call. Nothing was sent",
        )
    try:
        await connector._post_json(
            target,
            oauth_path,
            operator=operator,
            verb="PUT",
            json=body,
            extra_headers={"Content-Type": CLASSIC_JSON},
        )
    except httpx.HTTPStatusError as exc:
        return _outcome(
            "refused",
            "VCF Automation refused the OIDC settings (for example a client id or secret the "
            "identity provider does not accept). The old settings stay",
            vendor_message=http_error(exc, [secret]),
        )
    saved = await connector._request_json(target, "GET", oauth_path, operator=operator)
    return _outcome("set", None, settings=oidc_view(saved, org))
