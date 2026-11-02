# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""List a realm's identity providers: ``keycloak.identity_provider.list``.

An identity provider is an outside login (a company login over OIDC or SAML,
Google, GitHub, ...) that people can use to sign in to the realm. The op
answers, per provider: alias, display name, type, enabled, hidden on the
login page, and which login flows send people straight to it.

How it works, on the target's managed realm:

1. ``GET .../identity-provider/instances`` page by page (``first`` /
   ``max``) until a page is short, so the list is never cut at Keycloak's
   default page of 100.
2. ``GET .../authentication/flows``, then the steps of each top-level flow.
   Keycloak lists the steps as one flat list, depth first: a sub-flow row
   comes right before its own steps, which have a ``level`` one higher. A
   step ``identity-provider-redirector`` that is not ``DISABLED``, is not
   inside a ``DISABLED`` sub-flow, and has a config sends people to the
   provider named in the config key ``defaultProvider``
   (``GET .../authentication/config/{id}``). The flow's alias is listed on
   that provider as ``redirect_flows``.

No secret: the provider's ``config`` (which holds the client secret) is
never returned. Only the named fields are copied. A read that fails, or a
body that is not the expected JSON shape, is an error, never an empty list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.keycloak._paths import (
    _AUTH_CONFIG_PATH,
    _AUTH_FLOW_EXECUTIONS_PATH,
    _AUTH_FLOWS_PATH,
    _IDENTITY_PROVIDERS_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_authflow import get_admin_list_strict
from meho_backplane.connectors.keycloak.ops_read import KeycloakOp
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    KeycloakUnexpectedResponseError,
)
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "IDENTITY_PROVIDER_OPS",
    "WHEN_TO_USE_IDENTITY_PROVIDER_BY_GROUP",
    "keycloak_identity_provider_list",
]

_REDIRECTOR = "identity-provider-redirector"
_PAGE_SIZE = 100
#: 100 pages of 100: a realm with more providers is reported as not complete.
_MAX_PAGES = 100


async def _all_providers(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> tuple[list[Any], bool]:
    """Every provider of the realm, and whether the list is complete."""
    path = fill_path(_IDENTITY_PROVIDERS_PATH, {"realm": realm})
    rows: list[Any] = []
    for page in range(_MAX_PAGES):
        batch = await get_admin_list_strict(
            connector,
            target,
            path,
            operator=operator,
            params={"first": page * _PAGE_SIZE, "max": _PAGE_SIZE},
        )
        rows.extend(batch)
        if len(batch) < _PAGE_SIZE:
            return rows, True
    return rows, False


def _active_redirect_configs(steps: list[Any]) -> list[str]:
    """The config ids of the redirect steps that can run.

    A redirect step counts when it is not ``DISABLED`` and no sub-flow above
    it is ``DISABLED``. Keycloak lists the steps depth first with a ``level``
    per row, so the steps of a sub-flow follow it at the next level.
    """
    config_ids: list[str] = []
    disabled_parent_level: int | None = None
    for step in steps:
        if not isinstance(step, dict):
            continue
        level = step.get("level")
        level = level if isinstance(level, int) else 0
        if disabled_parent_level is not None and level <= disabled_parent_level:
            disabled_parent_level = None
        if disabled_parent_level is not None:
            continue
        if step.get("authenticationFlow") is True and step.get("requirement") == "DISABLED":
            disabled_parent_level = level
            continue
        if step.get("providerId") != _REDIRECTOR or step.get("requirement") == "DISABLED":
            continue
        config_id = step.get("authenticationConfig")
        if isinstance(config_id, str):
            config_ids.append(config_id)
    return config_ids


async def _redirect_flows(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> dict[str, list[str]]:
    """Map a provider alias to the top-level flows that redirect to it."""
    flows = await get_admin_list_strict(
        connector, target, fill_path(_AUTH_FLOWS_PATH, {"realm": realm}), operator=operator
    )
    redirects: dict[str, list[str]] = {}
    for flow in flows:
        alias = flow.get("alias") if isinstance(flow, dict) else None
        if not isinstance(alias, str) or not alias:
            continue
        steps = await get_admin_list_strict(
            connector,
            target,
            fill_path(
                _AUTH_FLOW_EXECUTIONS_PATH, {"realm": realm, "flowAlias": quote_segment(alias)}
            ),
            operator=operator,
        )
        for config_id in _active_redirect_configs(steps):
            config = await connector._get_admin_json(
                target,
                fill_path(_AUTH_CONFIG_PATH, {"realm": realm, "id": quote_segment(config_id)}),
                operator=operator,
            )
            settings = config.get("config")
            provider = settings.get("defaultProvider") if isinstance(settings, dict) else None
            if isinstance(provider, str) and provider and alias not in redirects.get(provider, []):
                redirects.setdefault(provider, []).append(alias)
    return redirects


def _hide_on_login(row: dict[str, Any]) -> bool | None:
    """Keycloak 26 has ``hideOnLogin``; older versions keep it in the config."""
    value = row.get("hideOnLogin")
    if isinstance(value, bool):
        return value
    config = row.get("config")
    legacy = config.get("hideOnLoginPage") if isinstance(config, dict) else None
    if isinstance(legacy, str):
        return legacy.lower() == "true"
    return None


async def keycloak_identity_provider_list(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """List the managed realm's identity providers, with no secret."""
    del params  # declared empty
    realm = resolve_realm_config(target).managed_realm
    realm_segment = quote_segment(realm)
    providers, complete = await _all_providers(self, operator, target, realm_segment)
    redirects = await _redirect_flows(self, operator, target, realm_segment)
    rows = []
    for row in providers:
        if not isinstance(row, dict):
            raise KeycloakUnexpectedResponseError("an identity provider is not a JSON object")
        alias = row.get("alias")
        rows.append(
            {
                "alias": alias,
                "display_name": row.get("displayName"),
                "type": row.get("providerId"),
                "enabled": row.get("enabled"),
                "hide_on_login": _hide_on_login(row),
                "redirect_flows": sorted(redirects.get(alias, []))
                if isinstance(alias, str)
                else [],
            }
        )
    return {"realm": realm, "rows": rows, "total": len(rows), "complete": complete}


_WHEN_TO_USE_IDENTITY_PROVIDER = (
    "Use to see how people can sign in to the realm through an outside login "
    "(``keycloak.identity_provider.list``): each identity provider's alias, "
    "type (for example oidc or saml), whether it is enabled, whether it is "
    "hidden on the login page, and which login flows send people straight to "
    "it. It never returns the provider's config or secret."
)

#: Group text for the op's group, merged into the connector's map.
WHEN_TO_USE_IDENTITY_PROVIDER_BY_GROUP: dict[str, str] = {
    "identity_provider": _WHEN_TO_USE_IDENTITY_PROVIDER
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "realm": {"type": "string", "description": "The target's managed realm."},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "alias": {"type": ["string", "null"]},
                    "display_name": {"type": ["string", "null"]},
                    "type": {
                        "type": ["string", "null"],
                        "description": "Provider type, for example 'oidc' or 'saml'.",
                    },
                    "enabled": {"type": ["boolean", "null"]},
                    "hide_on_login": {
                        "type": ["boolean", "null"],
                        "description": "true: no button on the login page. null: unknown.",
                    },
                    "redirect_flows": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Top-level login flows that send people straight here.",
                    },
                },
            },
        },
        "total": {"type": "integer"},
        "complete": {
            "type": "boolean",
            "description": "false: the realm has more than 10000 providers; the list is cut.",
        },
    },
    "required": ["realm", "rows", "total", "complete"],
}

IDENTITY_PROVIDER_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id="keycloak.identity_provider.list",
        handler_attr="identity_provider_list",
        summary="List the realm's identity providers (outside logins), with no secret.",
        description=(
            "Reads every identity provider of the target's managed realm, page "
            "by page, and the login flows that redirect to one. Returns alias, "
            "display name, type, enabled, hidden on the login page and the "
            "redirecting flows. Never the provider config or its secret."
        ),
        parameter_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "examples": [{}],
        },
        response_schema=_RESPONSE_SCHEMA,
        group_key="identity_provider",
        tags=("read-only", "identity-provider", "login-flow", "keycloak"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_IDENTITY_PROVIDER,
            "when_to_call": (
                "To check how people sign in to the realm: which outside logins "
                "exist, which are on, and which login flow sends people straight "
                "to one."
            ),
            "parameter_hints": {},
            "output_shape": (
                "``{realm, rows: [{alias, display_name, type, enabled, "
                "hide_on_login, redirect_flows}], total, complete}``."
            ),
            "next_step": (
                "A provider with redirect_flows sends people there without the "
                "login form. The approval sign-in flow must not redirect: check "
                "that 'meho approval sign-in' is not in any redirect_flows."
            ),
        },
    ),
)
