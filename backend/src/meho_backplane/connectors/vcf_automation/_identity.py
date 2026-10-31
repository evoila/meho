# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared parts of the VCFA sign-in ops, and the read ``vcfa.provider.identity.show``.

The sign-in ops (:mod:`.identity_ops`) read and set how people sign in to
VCF Automation: the provider's own LDAP, and each org's LDAP and OIDC. This
module holds what they share:

* the org lookup by name, with the check that refuses an org id that is not
  a plain path segment, and the test for the provider's own org (``System``);
* the read of a password or client secret from Vault (a ``*_secret_ref``
  param, read under the caller's identity, never a param value);
* the vendor error text with every secret replaced;
* the views of the vendor settings. A view copies named fields only, so a
  password or client secret the vendor sends back never reaches a result.

The read ``vcfa.provider.identity.show`` answers flat rows: one for the
provider's LDAP, and one LDAP row and one OIDC row per org.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx

from meho_backplane.connectors._shared.vault_creds import (
    DEFAULT_KV_MOUNT,
    load_param_secret_data,
    strip_credential_value,
)
from meho_backplane.connectors.vcf_automation._lookups import find_org, list_all, quote_segment
from meho_backplane.connectors.vcf_automation._paths import (
    PROVIDER_LDAP_SETTINGS_PATH,
    PROVIDER_ORG_LDAP_SETTINGS_PATH,
    PROVIDER_ORG_OAUTH_SETTINGS_PATH,
)
from meho_backplane.connectors.vcf_automation.typed_ops import PROVIDER_ORGS_PATH

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.vcf_automation.connector import VcfAutomationConnector
    from meho_backplane.connectors.vcf_automation.session import VcfAutomationTargetLike

__all__ = [
    "CLASSIC_JSON",
    "PROVIDER_ORG_NAME",
    "hide_secrets",
    "http_error",
    "is_provider_org",
    "key_of",
    "ldap_view",
    "oidc_view",
    "org_path",
    "pick",
    "provider_identity_show",
    "read_secret",
    "secret_missing_guidance",
    "vendor_text",
]

#: Content type of a classic ``/api`` JSON body (``go-vcloud-director``
#: ``types.JSONAllMime``). The ``Accept`` header comes from ``._routing``.
CLASSIC_JSON: Final = "application/*+json"
#: The provider's own org. The provider admin signs in to it.
PROVIDER_ORG_NAME: Final = "System"

_HIDDEN_TEXT: Final = "[MEHO hid this text: it held a password or secret from Vault]"


def pick(source: Any, name: str) -> Any:
    """The value of key *name* in *source*, matched without case; ``None`` when absent.

    The classic API's JSON names follow its XML names, so MEHO matches them
    without case (``OIDCAttributeMapping`` or ``oidcAttributeMapping``).
    """
    if not isinstance(source, Mapping):
        return None
    if name in source:
        return source[name]
    wanted = name.casefold()
    return next((value for key, value in source.items() if str(key).casefold() == wanted), None)


def key_of(source: Mapping[str, Any], name: str) -> str:
    """The key in *source* that matches *name* without case, else *name*."""
    wanted = name.casefold()
    return next((str(key) for key in source if str(key).casefold() == wanted), name)


def is_provider_org(org: Mapping[str, Any]) -> bool:
    """True for the provider's own org (``System``)."""
    return str(org.get("name") or "").casefold() == PROVIDER_ORG_NAME.casefold()


def org_path(template: str, org: Mapping[str, Any]) -> str:
    """*template* with the org uuid in ``{id}``, checked and percent-encoded.

    The classic paths take the uuid, not the ``urn:vcloud:org:<uuid>`` id
    (``go-vcloud-director`` ``extractUuid``). An empty id, ``.`` or ``..``
    is refused, so the value cannot reach another path.
    """
    uuid = str(org.get("id") or "").rsplit(":", 1)[-1].strip()
    if not uuid or uuid in {".", ".."} or any(ord(ch) < 32 for ch in uuid):
        raise ValueError(
            f"vcf-automation org {org.get('name')!r} has no usable id; nothing was sent"
        )
    return template.format(id=quote_segment(uuid))


@dataclass(slots=True)
class _RefTarget:
    """Points the Vault read at the path a call names, not at the target's login."""

    name: str
    host: str
    secret_ref: str | None


async def read_secret(
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: Mapping[str, Any],
    prefix: str,
    default_key: str,
) -> str | None:
    """Read the secret the call names in ``<prefix>_secret_ref``, under the caller's identity.

    ``<prefix>_secret_mount`` (default ``secret``) and ``<prefix>_secret_key``
    (default *default_key*) are optional. Returns ``None`` when the secret
    has no text under the key; the caller answers ``invalid_request``. A
    Vault refusal raises ``SecretRefReadError`` (the dispatcher answers
    ``vault_credentials_read_error``). The value never enters a log, a
    result or an error.
    """
    param = f"{prefix}_secret_ref"
    data = await load_param_secret_data(
        _RefTarget(name=target.name, host=target.host, secret_ref=str(params[param]).strip()),
        operator,
        param=param,
        mount=str(params.get(f"{prefix}_secret_mount") or DEFAULT_KV_MOUNT).strip(),
    )
    key = str(params.get(f"{prefix}_secret_key") or default_key).strip()
    value = data.get(key) if isinstance(data, dict) else None
    return (strip_credential_value(value) if isinstance(value, str) else "") or None


def secret_missing_guidance(params: Mapping[str, Any], prefix: str, default_key: str) -> str:
    """Why the secret could not be used, with no value in it."""
    key = params.get(f"{prefix}_secret_key") or default_key
    return (
        f"the Vault secret at {prefix}_secret_ref={params.get(f'{prefix}_secret_ref')!r} "
        f"has no text under the key {key!r}; store it there or set {prefix}_secret_key. "
        "Nothing was sent"
    )


def hide_secrets(text: str, secrets: Iterable[str | None]) -> str:
    """*text*, or a fixed note when it holds one of *secrets* (as is or escaped)."""
    for secret in secrets:
        if not secret:
            continue
        forms = {secret, quote(secret, safe=""), secret.replace('"', '\\"')}
        if any(form in text for form in forms):
            return _HIDDEN_TEXT
    return text


def vendor_text(exc: httpx.HTTPStatusError, secrets: Iterable[str | None] = ()) -> str:
    """The vendor's error message, from the answer only, with no secret in it.

    The request of *exc* (whose body may hold a password) is never read.
    """
    resp = exc.response
    try:
        body: Any = resp.json()
    except ValueError:
        body = None
    text = ""
    if isinstance(body, Mapping):
        text = str(pick(body, "message") or pick(body, "minorErrorCode") or "")
    elif resp.text and not resp.headers.get("content-type", "").startswith("text/html"):
        text = resp.text
    return hide_secrets(text.strip()[:500], list(secrets))


def http_error(exc: httpx.HTTPStatusError, secrets: Iterable[str | None] = ()) -> str:
    """``HTTP <status>`` plus the vendor's message, with no secret in it."""
    text = vendor_text(exc, secrets)
    return f"HTTP {exc.response.status_code}" + (f": {text}" if text else "")


# ---------------------------------------------------------------------------
# Views: named fields only, never a password or client secret
# ---------------------------------------------------------------------------

_LDAP_KEYS: Final = (
    "ldap_mode",
    "host",
    "port",
    "is_ssl",
    "search_base",
    "bind_user",
    "connector_type",
    "authentication_mechanism",
    "custom_users_ou",
)
_OIDC_CLAIMS: Final = (
    ("claim_subject", "subjectAttributeName"),
    ("claim_email", "emailAttributeName"),
    ("claim_full_name", "fullNameAttributeName"),
    ("claim_first_name", "firstNameAttributeName"),
    ("claim_last_name", "lastNameAttributeName"),
    ("claim_groups", "groupsAttributeName"),
    ("claim_roles", "rolesAttributeName"),
)
_OIDC_KEYS: Final = (
    "enabled",
    "well_known_endpoint",
    "issuer",
    "client_id",
    "scope",
    *(name for name, _vendor in _OIDC_CLAIMS),
)
_ROW_KEYS: Final = ("applies_to", "org", "org_id", "kind", *_LDAP_KEYS, *_OIDC_KEYS, "error")


def _row(
    applies_to: str, org: Mapping[str, Any] | None, kind: str, **values: Any
) -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(_ROW_KEYS)
    row.update(
        applies_to=applies_to,
        org=org.get("name") if org else None,
        org_id=org.get("id") if org else None,
        kind=kind,
    )
    row.update(values)
    return row


def _server_fields(server: Any) -> dict[str, Any]:
    return {
        "host": pick(server, "hostName"),
        "port": pick(server, "port"),
        "is_ssl": pick(server, "isSsl"),
        "search_base": pick(server, "searchBase"),
        "bind_user": pick(server, "userName"),
        "connector_type": pick(server, "connectorType"),
        "authentication_mechanism": pick(server, "authenticationMechanism"),
    }


def ldap_view(settings: Any, org: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One LDAP row. *org* ``None`` is the provider's own LDAP."""
    if org is None:
        fields = _server_fields(settings)
        mode = "CUSTOM" if fields["host"] else "NONE"
        return _row("provider", None, "ldap", ldap_mode=mode, **fields)
    custom = pick(settings, "customOrgLdapSettings")
    return _row(
        "org",
        org,
        "ldap",
        ldap_mode=pick(settings, "orgLdapMode"),
        custom_users_ou=pick(settings, "customUsersOu"),
        **(_server_fields(custom) if isinstance(custom, Mapping) else {}),
    )


def oidc_view(settings: Any, org: Mapping[str, Any]) -> dict[str, Any]:
    """One OIDC row of *org*."""
    mapping = pick(settings, "OIDCAttributeMapping")
    scope = pick(settings, "scope")
    return _row(
        "org",
        org,
        "oidc",
        enabled=pick(settings, "enabled"),
        well_known_endpoint=pick(settings, "wellKnownEndpoint"),
        issuer=pick(settings, "issuerId"),
        client_id=pick(settings, "clientId"),
        scope=" ".join(str(item) for item in scope) if isinstance(scope, list) else scope,
        **{name: pick(mapping, vendor) for name, vendor in _OIDC_CLAIMS},
    )


# ---------------------------------------------------------------------------
# vcfa.provider.identity.show
# ---------------------------------------------------------------------------


async def _read_row(
    connector: VcfAutomationConnector,
    target: VcfAutomationTargetLike,
    operator: Operator,
    path: str,
    org: Mapping[str, Any] | None,
    kind: str,
) -> dict[str, Any]:
    try:
        settings = await connector._request_json(target, "GET", path, operator=operator)
    except httpx.HTTPStatusError as exc:
        return _row("org" if org else "provider", org, kind, error=http_error(exc))
    if kind == "oidc" and org is not None:
        return oidc_view(settings, org)
    return ldap_view(settings, org)


async def provider_identity_show(
    connector: VcfAutomationConnector,
    operator: Operator,
    target: VcfAutomationTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``vcfa.provider.identity.show`` — the sign-in settings, as flat rows.

    Without ``org``: the provider's LDAP, then the LDAP and OIDC of every
    org. With ``org``: that org's two rows only. A read the vendor refuses
    gives a row with ``error`` set; the other rows still come back.
    """
    name = params.get("org")
    if name:
        org = await find_org(connector, target, operator, str(name))
        if org is None:
            return {"rows": [], "org_count": 0, "guidance": f"no org named {name!r}"}
        orgs: list[dict[str, Any]] = [org]
        rows: list[dict[str, Any]] = []
    else:
        orgs = await list_all(connector, target, operator, PROVIDER_ORGS_PATH)
        rows = [
            await _read_row(connector, target, operator, PROVIDER_LDAP_SETTINGS_PATH, None, "ldap")
        ]
    for org in orgs:
        ldap_path = org_path(PROVIDER_ORG_LDAP_SETTINGS_PATH, org)
        oauth_path = org_path(PROVIDER_ORG_OAUTH_SETTINGS_PATH, org)
        rows.append(await _read_row(connector, target, operator, ldap_path, org, "ldap"))
        rows.append(await _read_row(connector, target, operator, oauth_path, org, "oidc"))
    return {"rows": rows, "org_count": len(orgs), "guidance": None}
