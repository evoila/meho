# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time previews of the three VCFA sign-in changes.

A person approves each change, so the card shows what changes, for which org,
and what else is affected: every person who signs in through it. A secret
shows only as its Vault path (``password_from_vault_path`` /
``client_secret_from_vault_path``); MEHO does not read Vault for a preview.

With a connector the preview also reads the current settings (as the views
in :mod:`._identity`, with no password or client secret), and for LDAPS asks
the appliance for the server certificate: the card then shows its SHA-256
fingerprint and whether the call's ``trust_certificate_sha256`` matches it.
A failed read leaves those fields ``null``; the park still goes ahead.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from meho_backplane.connectors.vcf_automation._identity import (
    is_provider_org,
    ldap_view,
    oidc_view,
    org_path,
)
from meho_backplane.connectors.vcf_automation._identity_ldap import (
    probe_ldap_server,
    same_fingerprint,
)
from meho_backplane.connectors.vcf_automation._lookups import find_org
from meho_backplane.connectors.vcf_automation._paths import (
    PROVIDER_LDAP_SETTINGS_PATH,
    PROVIDER_ORG_LDAP_SETTINGS_PATH,
    PROVIDER_ORG_OAUTH_SETTINGS_PATH,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

__all__: list[str] = []

_READ_ERRORS = (httpx.HTTPError, RuntimeError, ValueError)
_log = structlog.get_logger(__name__)


def _server(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": params.get("host"),
        "port": params.get("port"),
        "is_ssl": bool(params.get("is_ssl")),
        "search_base": params.get("search_base"),
        "bind_user": params.get("bind_user"),
        "connector_type": params.get("connector_type"),
        "password_from_vault_path": params.get("password_secret_ref"),
        "trust_certificate_sha256": params.get("trust_certificate_sha256"),
        "certificate_sha256": None,
        "certificate_matches": None,
    }


async def _certificate(ctx: PreviewContext, preview: dict[str, Any]) -> None:
    """Fill the certificate fields of an LDAPS preview from a connection test."""
    params = ctx.params
    connector: Any = ctx.connector_instance
    if connector is None or not params.get("is_ssl") or not params.get("host"):
        return
    try:
        seen = await probe_ldap_server(connector, ctx.target, ctx.operator, params)
    except _READ_ERRORS as exc:
        _log.debug(
            "vcfa_identity_preview_read_failed", read="certificate", error=type(exc).__name__
        )
        return
    fingerprint = seen["certificate_sha256"]
    preview["certificate_sha256"] = fingerprint
    if fingerprint:
        preview["certificate_matches"] = same_fingerprint(
            params.get("trust_certificate_sha256"), fingerprint
        )


async def _ldap_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    preview: dict[str, Any] = {
        "action": "set_provider_ldap",
        **_server(ctx.params),
        "current": None,
        "affects": "every provider user who signs in with LDAP",
    }
    connector: Any = ctx.connector_instance
    if connector is None:
        return preview
    try:
        current = await connector._request_json(
            ctx.target, "GET", PROVIDER_LDAP_SETTINGS_PATH, operator=ctx.operator
        )
        preview["current"] = ldap_view(current)
    except _READ_ERRORS as exc:
        # The preview is best effort: "current" stays null and the park goes ahead.
        _log.debug(
            "vcfa_identity_preview_read_failed", read="provider_ldap", error=type(exc).__name__
        )
    await _certificate(ctx, preview)
    return preview


async def _org_and_current(ctx: PreviewContext, template: str, kind: str) -> dict[str, Any]:
    """``org_id``, ``provider_org`` and the current settings of the org the call names."""
    found: dict[str, Any] = {"org_id": None, "provider_org": None, "current": None}
    connector: Any = ctx.connector_instance
    if connector is None:
        return found
    try:
        org = await find_org(connector, ctx.target, ctx.operator, str(ctx.params.get("org")))
        if org is None:
            return {**found, "org_id": "not found"}
        found.update(org_id=org.get("id"), provider_org=is_provider_org(org))
        current = await connector._request_json(
            ctx.target, "GET", org_path(template, org), operator=ctx.operator
        )
        found["current"] = oidc_view(current, org) if kind == "oidc" else ldap_view(current, org)
    except _READ_ERRORS as exc:
        # The preview is best effort: the fields it could not read stay null.
        _log.debug(
            "vcfa_identity_preview_read_failed", read=f"org_{kind}", error=type(exc).__name__
        )
    return found


async def _org_ldap_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    params = ctx.params
    mode = params.get("mode")
    preview: dict[str, Any] = {
        "action": "set_org_ldap",
        "org": params.get("org"),
        "mode": mode,
        "custom_users_ou": params.get("custom_users_ou"),
        **(_server(params) if mode == "custom" else {}),
        **await _org_and_current(ctx, PROVIDER_ORG_LDAP_SETTINGS_PATH, "ldap"),
        "affects": f"every user of org {params.get('org')!r} who signs in with LDAP",
    }
    if mode == "custom":
        await _certificate(ctx, preview)
    return preview


async def _org_oidc_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    params = ctx.params
    state = params.get("state") or "present"
    preview: dict[str, Any] = {
        "action": "remove_org_oidc" if state == "absent" else "set_org_oidc",
        "org": params.get("org"),
        "well_known_endpoint": params.get("well_known_endpoint"),
        "client_id": params.get("client_id"),
        "client_secret_from_vault_path": params.get("client_secret_ref"),
        "claims": params.get("claims"),
        **await _org_and_current(ctx, PROVIDER_ORG_OAUTH_SETTINGS_PATH, "oidc"),
        "affects": f"every user of org {params.get('org')!r} who signs in with OIDC",
    }
    return preview


register_preview_builder("vcfa.provider.ldap.set", _ldap_set_preview)
register_preview_builder("vcfa.provider.org.ldap.set", _org_ldap_set_preview)
register_preview_builder("vcfa.provider.org.oidc.set", _org_oidc_set_preview)
