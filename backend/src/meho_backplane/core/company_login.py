# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The company login of the built-in core's realm stage (#4592).

When ``coreKeycloak.companyLogin.enabled`` is on, the realm stage of the
``bootstrap`` Job (:mod:`meho_backplane.core.keycloak`) runs
:func:`ensure_company_login` on every install and upgrade. It sets up
Keycloak's identity broker, so people log in on the company login system
and come back with a MEHO token from Keycloak:

* one OpenID Connect identity provider, from the company login's discovery
  document (Keycloak reads it through ``identity-provider/import-config``):
  PKCE S256, signature check with the published keys, sync mode FORCE,
  "trust email" off, Keycloak's default first-login steps;
* one "Advanced Claim to Group" mapper per ``groupMap`` line (sync mode
  FORCE). Mappers this stage made carry the name prefix ``meho-group-``;
  the stage removes those that are no longer listed and never touches
  other mappers;
* the company login as the default of the "Identity Provider Redirector"
  step when ``autoRedirect`` is on; that default is removed when it is off.

A second run with the same inputs changes nothing. Output lines never hold
the client secret. Details: docs/codebase/core-bootstrap.md.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from meho_backplane.core.keycloak_api import REALM, Admin, RealmStageError, expect

__all__ = [
    "MAPPER_PREFIX",
    "REDIRECT_CONFIG_ALIAS",
    "SECRET_ENV",
    "CompanyLoginPlan",
    "ensure_company_login",
]

SECRET_ENV = "MEHO_COMPANY_LOGIN_CLIENT_SECRET"
MAPPER_PREFIX = "meho-group-"
REDIRECT_CONFIG_ALIAS = "meho-company-login-redirect"
_GROUP_MAPPER = "oidc-advanced-group-idp-mapper"
_REDIRECTOR = "identity-provider-redirector"
_FIRST_LOGIN_FLOW = "first broker login"
# The keys Keycloak takes from the discovery document (OIDCIdentityProviderFactory).
_FROM_DISCOVERY = (
    "issuer",
    "authorizationUrl",
    "tokenUrl",
    "userInfoUrl",
    "jwksUrl",
    "useJwksUrl",
    "validateSignature",
    "logoutUrl",
    "tokenIntrospectionUrl",
    "metadataDescriptorUrl",
)
_SECRET_KEY = "clientSecret"

GroupLookup = Callable[[str], Awaitable[str | None]]


class CompanyLoginPlan(BaseModel):
    """``coreKeycloak.companyLogin`` as the chart renders it: no secret here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    alias: str = Field(default="company", min_length=1)
    display_name: str = "Company login"
    discovery_url: str = ""
    client_id: str = ""
    scopes: str = "openid profile email"
    groups_claim: str = Field(default="groups", min_length=1)
    # Company group name -> MEHO group name.
    group_map: dict[str, str] = Field(default_factory=dict)
    auto_redirect: bool = True


def _say(line: str) -> None:
    print(line, flush=True)


class _CompanyLogin:
    def __init__(self, admin: Admin, plan: CompanyLoginPlan, client_secret: str) -> None:
        self.admin = admin
        self.plan = plan
        self.secret = client_secret
        self.base = f"/admin/realms/{REALM}"
        self.idp_url = f"{self.base}/identity-provider/instances/{quote(plan.alias, safe='')}"
        self.label = f"identity provider {plan.alias}"

    async def _current(self) -> dict[str, Any] | None:
        reply = expect(
            await self.admin.call("GET", self.idp_url), 200, 404, what=f"cannot read {self.label}"
        )
        if reply.status == 404:
            return None
        current: dict[str, Any] = reply.data
        return current

    async def _discover(self, current: Mapping[str, Any] | None) -> dict[str, str]:
        """The provider's addresses from the discovery document.

        When the company login cannot be reached but the provider already
        exists with the same discovery address, the saved addresses stay. So
        a ``helm upgrade`` that turns ``autoRedirect`` off still works while
        the company login is down.
        """
        reply = await self.admin.call(
            "POST",
            f"{self.base}/identity-provider/import-config",
            json={
                "providerId": "oidc",
                "alias": self.plan.alias,
                "fromUrl": self.plan.discovery_url,
            },
        )
        if reply.status == 200 and isinstance(reply.data, dict):
            found = {k: str(v) for k, v in reply.data.items() if k in _FROM_DISCOVERY and v}
            if found.get("validateSignature") != "true" or not found.get("jwksUrl"):
                raise RealmStageError(
                    f"the company login at {self.plan.discovery_url} publishes no signing keys "
                    "(jwks_uri), so Keycloak cannot check its tokens; give it a signing key "
                    "such as RS256"
                )
            return found
        saved = dict((current or {}).get("config") or {})
        if saved.get("metadataDescriptorUrl") == self.plan.discovery_url:
            _say(f"{self.label}: discovery document not readable, kept the saved addresses")
            return {k: saved[k] for k in _FROM_DISCOVERY if k in saved}
        expect(reply, 200, what=f"Keycloak cannot read the discovery document of {self.label}")
        raise RealmStageError(f"the discovery document of {self.label} is not a JSON object")

    def _want(self, discovered: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
        top = {
            "alias": self.plan.alias,
            "displayName": self.plan.display_name,
            "providerId": "oidc",
            "enabled": True,
            "trustEmail": False,
            "storeToken": False,
            "linkOnly": False,
            "hideOnLogin": False,
            "firstBrokerLoginFlowAlias": _FIRST_LOGIN_FLOW,
        }
        config = {
            **discovered,
            "clientId": self.plan.client_id,
            "clientAuthMethod": "client_secret_post",
            "defaultScope": self.plan.scopes,
            "pkceEnabled": "true",
            "pkceMethod": "S256",
            "syncMode": "FORCE",
        }
        return top, config

    async def ensure_provider(self) -> None:
        current = await self._current()
        top, config = self._want(await self._discover(current))
        if current is None:
            body = {**top, "config": {**config, _SECRET_KEY: self.secret}}
            expect(
                await self.admin.call(
                    "POST", f"{self.base}/identity-provider/instances", json=body
                ),
                201,
                what=f"cannot create {self.label}",
            )
            _say(f"{self.label}: created")
            return
        have = dict(current.get("config") or {})
        changed = sorted(
            [key for key, value in top.items() if current.get(key) != value]
            + [key for key, value in config.items() if have.get(key) != value]
        )
        # Keycloak never returns the secret, so it is always sent again: a
        # new value in the Secret reaches Keycloak at the next run.
        body = {**current, **top, "config": {**have, **config, _SECRET_KEY: self.secret}}
        expect(
            await self.admin.call("PUT", self.idp_url, json=body),
            204,
            what=f"cannot update {self.label}",
        )
        if changed:
            _say(f"{self.label}: updated ({', '.join(changed)}; secret set)")
        else:
            _say(f"{self.label}: kept (secret set)")

    def _mapper(self, company_group: str, meho_group: str) -> dict[str, Any]:
        claims = [{"key": self.plan.groups_claim, "value": company_group}]
        return {
            "name": f"{MAPPER_PREFIX}{company_group}",
            "identityProviderAlias": self.plan.alias,
            "identityProviderMapper": _GROUP_MAPPER,
            "config": {
                "syncMode": "FORCE",
                "claims": json.dumps(claims),
                "are.claim.values.regex": "false",
                "group": meho_group if meho_group.startswith("/") else f"/{meho_group}",
            },
        }

    @staticmethod
    def _same(have: Mapping[str, Any], want: Mapping[str, Any]) -> bool:
        have_config = dict(have.get("config") or {})
        want_config = dict(want["config"])
        try:
            claims_match = json.loads(have_config.pop("claims", "null")) == json.loads(
                want_config.pop("claims")
            )
        except ValueError:
            return False
        return (
            claims_match
            and have.get("identityProviderMapper") == want["identityProviderMapper"]
            and all(have_config.get(k) == v for k, v in want_config.items())
        )

    async def check_groups(self, groups: GroupLookup) -> None:
        for meho_group in sorted(set(self.plan.group_map.values())):
            if await groups(meho_group.lstrip("/")) is None:
                raise RealmStageError(
                    f"companyLogin.groupMap names the group {meho_group}, "
                    "which the realm does not have"
                )

    async def ensure_mappers(self) -> None:
        url = f"{self.idp_url}/mappers"
        listed = expect(
            await self.admin.call("GET", url), 200, what=f"cannot list the mappers of {self.label}"
        ).data
        ours = {m["name"]: m for m in listed if str(m.get("name", "")).startswith(MAPPER_PREFIX)}
        for company_group, meho_group in sorted(self.plan.group_map.items()):
            want = self._mapper(company_group, meho_group)
            name = want["name"]
            have = ours.pop(name, None)
            if have is None:
                expect(
                    await self.admin.call("POST", url, json=want),
                    201,
                    what=f"cannot create mapper {name}",
                )
                _say(f"mapper {name}: created")
            elif self._same(have, want):
                _say(f"mapper {name}: kept")
            else:
                expect(
                    await self.admin.call(
                        "PUT", f"{url}/{have['id']}", json={**want, "id": have["id"]}
                    ),
                    204,
                    what=f"cannot update mapper {name}",
                )
                _say(f"mapper {name}: updated")
        for name, stale in sorted(ours.items()):
            expect(
                await self.admin.call("DELETE", f"{url}/{stale['id']}"),
                204,
                what=f"cannot remove mapper {name}",
            )
            _say(f"mapper {name}: removed")

    async def _redirector(self) -> dict[str, Any] | None:
        realm = expect(
            await self.admin.call("GET", self.base), 200, what=f"cannot read realm {REALM}"
        ).data
        flow = str(realm.get("browserFlow") or "browser")
        executions = expect(
            await self.admin.call(
                "GET", f"{self.base}/authentication/flows/{quote(flow, safe='')}/executions"
            ),
            200,
            what=f"cannot list the steps of the login flow {flow}",
        ).data
        step: dict[str, Any] | None = next(
            (e for e in executions if e.get("providerId") == _REDIRECTOR), None
        )
        return step

    async def ensure_redirect(self) -> None:
        step = await self._redirector()
        label = "auto-redirect"
        if step is None:
            if self.plan.auto_redirect:
                raise RealmStageError(
                    "the login flow has no Identity Provider Redirector step, "
                    "so autoRedirect cannot be set"
                )
            _say(f"{label}: off")
            return
        config_id = step.get("authenticationConfig")
        config: dict[str, Any] | None = None
        if config_id:
            config = expect(
                await self.admin.call("GET", f"{self.base}/authentication/config/{config_id}"),
                200,
                what="cannot read the Identity Provider Redirector settings",
            ).data
        current = ((config or {}).get("config") or {}).get("defaultProvider")
        config_url = f"{self.base}/authentication/config/{config_id}"
        if not self.plan.auto_redirect:
            if config is not None and current == self.plan.alias:
                expect(
                    await self.admin.call("DELETE", config_url),
                    204,
                    what="cannot remove the Identity Provider Redirector default",
                )
                _say(f"{label}: removed")
            else:
                _say(f"{label}: off")
            return
        if current == self.plan.alias:
            _say(f"{label}: kept")
            return
        if config is None:
            expect(
                await self.admin.call(
                    "POST",
                    f"{self.base}/authentication/executions/{step['id']}/config",
                    json={
                        "alias": REDIRECT_CONFIG_ALIAS,
                        "config": {"defaultProvider": self.plan.alias},
                    },
                ),
                201,
                what="cannot set the Identity Provider Redirector default",
            )
        else:
            config["config"] = {**(config.get("config") or {}), "defaultProvider": self.plan.alias}
            expect(
                await self.admin.call("PUT", config_url, json=config),
                204,
                what="cannot set the Identity Provider Redirector default",
            )
        _say(f"{label}: set to {self.plan.alias}")


async def ensure_company_login(
    admin: Admin, plan: CompanyLoginPlan, client_secret: str, groups: GroupLookup
) -> None:
    """Create or update the company login. Safe to run again."""
    if not plan.discovery_url or not plan.client_id:
        raise RealmStageError("companyLogin needs discoveryUrl and clientId")
    if not client_secret:
        raise RealmStageError(f"{SECRET_ENV} is empty; check companyLogin.clientSecret")
    login = _CompanyLogin(admin, plan, client_secret)
    try:
        await login.check_groups(groups)
        await login.ensure_provider()
        await login.ensure_mappers()
        await login.ensure_redirect()
    except RealmStageError as exc:
        if "HTTP 403" in str(exc):
            raise RealmStageError(
                f"{exc}. The Job's Keycloak account needs the realm-management roles "
                "manage-identity-providers and manage-realm; see "
                "docs/codebase/core-bootstrap.md"
            ) from None
        raise
