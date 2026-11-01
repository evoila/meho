# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Finish the Keycloak realm of the built-in core: the realm stage (#4043).

The ``bootstrap`` Helm hook Job runs this after every install and every
upgrade (``python -m meho_backplane.core.bootstrap keycloak``). Keycloak
reads the realm file only at its first start, so this stage brings the
running realm up to date through the Admin REST API:

* it logs in as the temporary first-start account when that still exists,
  else as ``meho-admin``;
* it adds every client scope, group, client, mapper and client scope
  assignment of the realm file that the realm does not have yet;
* it adds the login flows of the realm file the realm does not have yet,
  and binds a client to its own flow
  (:mod:`meho_backplane.core.keycloak_flows`);
* it sets the secrets of the confidential clients from the ``<release>-core-…``
  Secrets, so Keycloak and the backplane always agree;
* it makes the first admin and the second person with temporary passwords,
  stored in ``<release>-core-first-login``;
* it makes the five role groups of every extra tenant in
  ``coreKeycloak.tenants`` (``MEHO-<tenant>-<Role>``); these carry only the
  ``meho_tenant`` attribute;
* it sets the attributes of the realm file's groups and of the extra
  tenants' groups on groups that exist already, removes ``tenant_id``,
  ``tenant_role`` and ``approver`` from an extra tenant's groups, and
  leaves other attributes alone;
* when ``coreKeycloak.ci.enabled`` is on, it also makes a CI-only service
  client and a group for a second tenant;
* when ``coreKeycloak.companyLogin.enabled`` is on, it sets up the company
  login (:mod:`meho_backplane.core.company_login`);
* on the first run it checks that ``meho-admin`` can log in, then removes
  the first-start account.

A second run with the same inputs changes nothing. Output lines name
objects and ``created``, ``kept`` or ``removed``; they never hold a value.
Details: docs/codebase/core-bootstrap.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

import httpx
from kubernetes_asyncio import client, config
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from meho_backplane.core.company_login import (
    SECRET_ENV as COMPANY_SECRET_ENV,
)
from meho_backplane.core.company_login import (
    CompanyLoginPlan,
    ensure_company_login,
)
from meho_backplane.core.keycloak_api import (
    REALM,
    Admin,
    HttpAdmin,
    RealmStageError,
    Reply,
    expect,
    id_from,
)
from meho_backplane.core.keycloak_flows import OVERRIDES, ensure_flows, flow_overrides
from meho_backplane.core.secrets import (
    CoreSecretsError,
    KubernetesSecretStore,
    new_password,
)

__all__ = [
    "ADMIN_CLIENT_ID",
    "ADMIN_SECRET_ENV",
    "BOOTSTRAP_ID_ENV",
    "BOOTSTRAP_SECRET_ENV",
    "NAMESPACE_ENV",
    "PLAN_ENV",
    "REALM",
    "Admin",
    "CiPlan",
    "HttpAdmin",
    "Plan",
    "RealmStageError",
    "Reply",
    "SecretStore",
    "TenantPlan",
    "finish_realm",
    "load_plan",
    "load_realm_file",
    "run",
]

PLAN_ENV = "MEHO_CORE_KEYCLOAK_PLAN"
NAMESPACE_ENV = "POD_NAMESPACE"
BOOTSTRAP_ID_ENV = "MEHO_CORE_BOOTSTRAP_CLIENT_ID"
BOOTSTRAP_SECRET_ENV = "MEHO_CORE_BOOTSTRAP_CLIENT_SECRET"
ADMIN_SECRET_ENV = "MEHO_REALM_ADMIN_CLIENT_SECRET"
PUBLIC_URL_ENV = "MEHO_REALM_PUBLIC_URL"
TENANT_ID_ENV = "MEHO_REALM_TENANT_ID"
TENANT_NAME_ENV = "MEHO_REALM_TENANT_NAME"

ADMIN_CLIENT_ID = "meho-admin"
CI_CLIENT_ID = "meho-ci"
CI_GROUP = "MEHO-CI-Admins"
# Where the two first-login people go. The first admin is also in the
# platform group, which carries only ``platform_admin``; it is not one of
# the five role groups, so the one-role-group rule still holds.
ADMIN_GROUPS = ("MEHO-Admins", "MEHO-Platform-Admins")
APPROVER_GROUPS = ("MEHO-SA-Operators",)
FIRST_LOGIN_KEYS = ("admin_username", "admin_password", "approver_username", "approver_password")
# The role part of a role group's name, and of its ``meho_tenant`` value
# ``<tenant-uuid>:<tenant>:<Role>`` (docs/decisions/login-role-groups.md).
TENANT_ROLES = ("Readers", "Operators", "SA-Operators", "Admins", "Approvers")
# The attributes an older MEHO reads for access. An extra tenant's groups
# must not carry them, so the stage removes them from such a group.
OLD_ACCESS_ATTRIBUTES = ("tenant_id", "tenant_role", "approver")
# A tenant's short name. Tenant names that would make a group name that
# differs from a shipped group only in case are refused.
_TENANT_NAME = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"
_RESERVED_TENANT_NAMES = frozenset({"sa", "platform", "ci"})

# Seconds per request; the Job's backoffLimit decides whether it runs again.
_HTTP_TIMEOUT_S = 30
# The realm's discovery document must answer within this time. Same budget
# as the Keycloak pod's start probe.
_REALM_WAIT_S = 300
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


class CiPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    # The second CI tenant's id (a UUID), the ``tenant_id`` of the CI group.
    tenant_id: str = ""
    # The Secret that holds the CI client's id and secret.
    secret_name: str = ""


class TenantPlan(BaseModel):
    """One extra tenant: its five groups ``MEHO-<name>-<Role>``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_TENANT_NAME)
    id: UUID

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, name: str) -> str:
        if name in _RESERVED_TENANT_NAMES:
            raise ValueError(f"{name!r} is taken by a group that ships with MEHO")
        return name


class Plan(BaseModel):
    """What the chart renders into ``MEHO_CORE_KEYCLOAK_PLAN``: names, never values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Keycloak through its Service, for example ``http://rel-keycloak:8080/auth``.
    keycloak_url: str = Field(min_length=1)
    realm_file: str = Field(min_length=1)
    first_login_secret: str = Field(min_length=1)
    admin_username: str = Field(min_length=1)
    approver_username: str = Field(min_length=1)
    tenants: tuple[TenantPlan, ...] = ()
    ci: CiPlan = CiPlan()
    company_login: CompanyLoginPlan = CompanyLoginPlan()

    @model_validator(mode="after")
    def _tenants_differ(self) -> Plan:
        names = [tenant.name for tenant in self.tenants]
        ids = [tenant.id for tenant in self.tenants]
        if len(set(names)) != len(names) or len(set(ids)) != len(ids):
            raise ValueError("every extra tenant needs its own name and its own id")
        return self


class SecretStore(Protocol):
    async def read_data(self, name: str) -> dict[str, str] | None: ...

    async def create(self, name: str, data: Mapping[str, str], secret_type: str) -> bool: ...


def _say(line: str) -> None:
    print(line, flush=True)


def load_plan(environ: Mapping[str, str]) -> Plan:
    raw = environ.get(PLAN_ENV)
    if not raw:
        raise RealmStageError(f"{PLAN_ENV} is not set; the chart sets it on the Job.")
    try:
        return Plan.model_validate_json(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise RealmStageError(f"{PLAN_ENV} is not a valid plan: {problems}") from None


def load_realm_file(path: str, environ: Mapping[str, str]) -> dict[str, Any]:
    """Read the realm file and fill its ``${...}`` placeholders from the environment."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RealmStageError(f"cannot read the realm file {path}: {exc.strerror}") from None

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in environ:
            raise RealmStageError(f"the realm file needs {name}, which is not set on the Job")
        return environ[name]

    try:
        realm: dict[str, Any] = json.loads(_PLACEHOLDER.sub(fill, text))
    except ValueError as exc:
        raise RealmStageError(f"the realm file {path} is not valid JSON: {exc}") from None
    return realm


class _Realm:
    """One session on realm ``meho``, with the lookups the steps share."""

    def __init__(self, admin: Admin) -> None:
        self.admin = admin
        self.base = f"/admin/realms/{REALM}"
        self._group_ids: dict[str, str] = {}

    async def scope_ids(self) -> dict[str, str]:
        reply = expect(
            await self.admin.call("GET", f"{self.base}/client-scopes"),
            200,
            what="cannot list client scopes",
        )
        return {scope["name"]: scope["id"] for scope in reply.data}

    async def group_id(self, name: str) -> str | None:
        if name in self._group_ids:
            return self._group_ids[name]
        reply = expect(
            await self.admin.call(
                "GET", f"{self.base}/groups", params={"search": name, "exact": "true"}
            ),
            200,
            what=f"cannot look up group {name}",
        )
        for group in reply.data:
            if group["name"] == name:
                self._group_ids[name] = group["id"]
                return self._group_ids[name]
        return None

    async def ensure_group(self, group: Mapping[str, Any], drop: tuple[str, ...] = ()) -> None:
        """Make the group, or set its attributes; ``drop`` names attributes to remove."""
        name = group["name"]
        group_id = await self.group_id(name)
        if group_id is not None:
            changed = await self._set_attributes(
                group_id, name, group.get("attributes") or {}, drop
            )
            _say(f"group {name}: " + ("kept (attributes set)" if changed else "kept"))
            return
        reply = expect(
            await self.admin.call("POST", f"{self.base}/groups", json=dict(group)),
            201,
            what=f"cannot create group {name}",
        )
        self._group_ids[name] = id_from(reply)
        _say(f"group {name}: created")

    async def _set_attributes(
        self, group_id: str, name: str, want: Mapping[str, list[str]], drop: tuple[str, ...]
    ) -> bool:
        """Set ``want`` and remove ``drop`` on an existing group; keep every other attribute."""
        if not want and not drop:
            return False
        url = f"{self.base}/groups/{group_id}"
        current = expect(
            await self.admin.call("GET", url), 200, what=f"cannot read group {name}"
        ).data
        attributes = dict(current.get("attributes") or {})
        wanted = all(attributes.get(key) == list(value) for key, value in want.items())
        if wanted and not any(key in attributes for key in drop):
            return False
        attributes.update({key: list(value) for key, value in want.items()})
        for key in drop:
            attributes.pop(key, None)
        current["attributes"] = attributes
        expect(
            await self.admin.call("PUT", url, json=current),
            204,
            what=f"cannot set the attributes of group {name}",
        )
        return True

    async def ensure_client_scopes(self, scopes: list[Mapping[str, Any]]) -> dict[str, str]:
        ids = await self.scope_ids()
        for scope in scopes:
            name = scope["name"]
            if name in ids:
                _say(f"client scope {name}: kept")
                continue
            reply = expect(
                await self.admin.call("POST", f"{self.base}/client-scopes", json=dict(scope)),
                201,
                what=f"cannot create client scope {name}",
            )
            ids[name] = id_from(reply)
            _say(f"client scope {name}: created")
        return ids

    async def ensure_client(
        self,
        want: Mapping[str, Any],
        scope_ids: Mapping[str, str],
        flow_ids: Mapping[str, str],
    ) -> None:
        client_id = want["clientId"]
        overrides = flow_overrides(want, flow_ids)
        found = expect(
            await self.admin.call("GET", f"{self.base}/clients", params={"clientId": client_id}),
            200,
            what=f"cannot look up client {client_id}",
        )
        if not found.data:
            body = {key: value for key, value in want.items() if key != OVERRIDES}
            if overrides:
                body[OVERRIDES] = overrides
            expect(
                await self.admin.call("POST", f"{self.base}/clients", json=body),
                201,
                what=f"cannot create client {client_id}",
            )
            _say(f"client {client_id}: created")
            return
        url = f"{self.base}/clients/{found.data[0]['id']}"
        changes = await self._add_mappers(url, want)
        changes += await self._add_scopes(url, want, scope_ids)
        update: dict[str, Any] = {}
        if "secret" in want:
            update["secret"] = want["secret"]
            changes.append("secret set")
        bound = found.data[0].get(OVERRIDES) or {}
        if any(bound.get(binding) != flow_id for binding, flow_id in overrides.items()):
            update[OVERRIDES] = {**bound, **overrides}
            changes.append("login flow set")
        if update:
            current = expect(
                await self.admin.call("GET", url), 200, what=f"cannot read client {client_id}"
            ).data
            current.update(update)
            expect(
                await self.admin.call("PUT", url, json=current),
                204,
                what=f"cannot update client {client_id}",
            )
        _say(f"client {client_id}: kept" + (f" ({', '.join(changes)})" if changes else ""))

    async def _add_mappers(self, url: str, want: Mapping[str, Any]) -> list[str]:
        changes: list[str] = []
        wanted = want.get("protocolMappers") or []
        if not wanted:
            return changes
        have = {
            mapper["name"]
            for mapper in expect(
                await self.admin.call("GET", f"{url}/protocol-mappers/models"),
                200,
                what=f"cannot list the mappers of client {want['clientId']}",
            ).data
        }
        for mapper in wanted:
            if mapper["name"] in have:
                continue
            expect(
                await self.admin.call("POST", f"{url}/protocol-mappers/models", json=dict(mapper)),
                201,
                what=f"cannot add mapper {mapper['name']} to client {want['clientId']}",
            )
            changes.append(f"mapper {mapper['name']} added")
        return changes

    async def _add_scopes(
        self, url: str, want: Mapping[str, Any], scope_ids: Mapping[str, str]
    ) -> list[str]:
        changes: list[str] = []
        for kind in ("default", "optional"):
            wanted = want.get(f"{kind}ClientScopes") or []
            if not wanted:
                continue
            have = {
                scope["name"]
                for scope in expect(
                    await self.admin.call("GET", f"{url}/{kind}-client-scopes"),
                    200,
                    what=f"cannot list the {kind} scopes of client {want['clientId']}",
                ).data
            }
            for name in wanted:
                if name in have:
                    continue
                if name not in scope_ids:
                    raise RealmStageError(
                        f"client {want['clientId']} wants the client scope {name}, "
                        "which the realm does not have"
                    )
                expect(
                    await self.admin.call("PUT", f"{url}/{kind}-client-scopes/{scope_ids[name]}"),
                    204,
                    what=f"cannot add scope {name} to client {want['clientId']}",
                )
                changes.append(f"{kind} scope {name} added")
        return changes

    async def ensure_user(self, username: str, password: str, groups: tuple[str, ...]) -> None:
        found = expect(
            await self.admin.call(
                "GET", f"{self.base}/users", params={"username": username, "exact": "true"}
            ),
            200,
            what=f"cannot look up user {username}",
        )
        if found.data:
            _say(f"user {username}: kept")
            return
        for group in groups:
            if await self.group_id(group) is None:
                raise RealmStageError(f"group {group} does not exist, so {username} cannot join it")
        # The person and their group memberships are one request, so Keycloak
        # makes both or neither: a failed run never leaves a person without
        # groups that the next run would keep.
        body = {
            "username": username,
            "enabled": True,
            "credentials": [{"type": "password", "value": password, "temporary": True}],
            "requiredActions": ["UPDATE_PASSWORD"],
            "groups": [f"/{group}" for group in groups],
        }
        expect(
            await self.admin.call("POST", f"{self.base}/users", json=body),
            201,
            what=f"cannot create user {username}",
        )
        _say(f"user {username}: created (in {', '.join(groups)})")


async def _read_or_make(store: SecretStore, name: str, fresh: Mapping[str, str]) -> dict[str, str]:
    """Return the Secret's values, making it from ``fresh`` when it does not exist."""
    data = await store.read_data(name)
    if data is None:
        if await store.create(name, fresh, "Opaque"):
            _say(f"secret {name}: created")
            return dict(fresh)
        data = await store.read_data(name)
        if data is None:
            raise RealmStageError(f'Secret "{name}" could not be created or read')
    _say(f"secret {name}: kept")
    for key in fresh:
        if key not in data:
            raise RealmStageError(
                f'Secret "{name}" has no key "{key}"; add the key, or delete the Secret '
                "so the install makes a new one"
            )
    return data


def _require_two_people(admin: str, approver: str, where: str) -> None:
    """Keycloak ignores case and surrounding spaces in a user name."""
    if admin.strip().casefold() == approver.strip().casefold():
        raise RealmStageError(
            f"the first admin and the second person must be two different people, "
            f"but {where} names {admin!r} and {approver!r}, which are the same "
            "Keycloak user"
        )


async def _ensure_first_login(realm: _Realm, plan: Plan, store: SecretStore) -> None:
    _require_two_people(plan.admin_username, plan.approver_username, "the chart")
    fresh = {
        "admin_username": plan.admin_username,
        "admin_password": new_password(),
        "approver_username": plan.approver_username,
        "approver_password": new_password(),
    }
    data = await _read_or_make(store, plan.first_login_secret, fresh)
    _require_two_people(
        data["admin_username"], data["approver_username"], f'Secret "{plan.first_login_secret}"'
    )
    await realm.ensure_user(data["admin_username"], data["admin_password"], ADMIN_GROUPS)
    await realm.ensure_user(data["approver_username"], data["approver_password"], APPROVER_GROUPS)


def tenant_groups(tenant: TenantPlan) -> list[dict[str, Any]]:
    """The five groups of an extra tenant. They carry only ``meho_tenant``.

    No ``tenant_id`` or ``tenant_role``: an older MEHO, which reads only
    those, refuses a person who is only in these groups. ``finish_realm``
    removes ``OLD_ACCESS_ATTRIBUTES`` from such a group that exists already.
    """
    return [
        {
            "name": f"MEHO-{tenant.name}-{role}",
            "attributes": {"meho_tenant": [f"{tenant.id}:{tenant.name}:{role}"]},
        }
        for role in TENANT_ROLES
    ]


def _check_tenants(plan: Plan, environ: Mapping[str, str]) -> None:
    first_id, first_name = environ[TENANT_ID_ENV], environ[TENANT_NAME_ENV]
    for tenant in plan.tenants:
        if tenant.name == first_name or str(tenant.id) == first_id.lower():
            raise RealmStageError(
                f"the extra tenant {tenant.name!r} has the name or the id of the first tenant; "
                "pick another name and another UUID"
            )


def _hardcoded_claim(name: str, claim: str, value: str) -> dict[str, Any]:
    return {
        "name": name,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-hardcoded-claim-mapper",
        "config": {
            "claim.name": claim,
            "claim.value": value,
            "jsonType.label": "String",
            "access.token.claim": "true",
            "id.token.claim": "false",
            "userinfo.token.claim": "false",
        },
    }


def _audience(name: str, audience: str) -> dict[str, Any]:
    return {
        "name": name,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.custom.audience": audience,
            "id.token.claim": "false",
            "access.token.claim": "true",
            "introspection.token.claim": "true",
        },
    }


def _ci_client(client_id: str, secret: str, tenant_id: str, public_url: str) -> dict[str, Any]:
    """A service client for CI proofs: fixed claims of a tenant admin of the first tenant."""
    return {
        "clientId": client_id,
        "name": "MEHO CI login",
        "description": "Service client for automated install proofs (coreKeycloak.ci.enabled).",
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "clientAuthenticatorType": "client-secret",
        "secret": secret,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": True,
        "defaultClientScopes": ["basic", "roles", "web-origins", "acr"],
        "optionalClientScopes": [],
        "protocolMappers": [
            _audience("audience-meho-backplane", "meho-backplane"),
            _audience("meho-mcp-audience", f"{public_url}/mcp"),
            _hardcoded_claim("tenant-id-claim", "tenant_id", tenant_id),
            _hardcoded_claim("tenant-role-claim", "tenant_role", "tenant_admin"),
        ],
    }


async def _ensure_ci(
    realm: _Realm,
    plan: Plan,
    environ: Mapping[str, str],
    store: SecretStore,
    scope_ids: Mapping[str, str],
) -> None:
    data = await _read_or_make(
        store, plan.ci.secret_name, {"client_id": CI_CLIENT_ID, "client_secret": new_password()}
    )
    await realm.ensure_group(
        {
            "name": CI_GROUP,
            "attributes": {"tenant_id": [plan.ci.tenant_id], "tenant_role": ["tenant_admin"]},
        }
    )
    want = _ci_client(
        data["client_id"], data["client_secret"], environ[TENANT_ID_ENV], environ[PUBLIC_URL_ENV]
    )
    await realm.ensure_client(want, scope_ids, {})


Session = Literal["bootstrap", "admin"]


async def _log_in(admin: Admin, environ: Mapping[str, str]) -> Session:
    bootstrap_id = environ.get(BOOTSTRAP_ID_ENV, "")
    bootstrap_secret = environ.get(BOOTSTRAP_SECRET_ENV, "")
    if (
        bootstrap_id
        and bootstrap_secret
        and await admin.login("master", bootstrap_id, bootstrap_secret)
    ):
        _say("login: first-start account")
        return "bootstrap"
    if await admin.login(REALM, ADMIN_CLIENT_ID, environ[ADMIN_SECRET_ENV]):
        _say(f"login: {ADMIN_CLIENT_ID}")
        return "admin"
    raise RealmStageError(
        "cannot log in to Keycloak: neither the first-start account nor "
        f"{ADMIN_CLIENT_ID} accepted its secret"
    )


async def _remove_bootstrap_account(admin: Admin, environ: Mapping[str, str]) -> None:
    if not await admin.check_login(REALM, ADMIN_CLIENT_ID, environ[ADMIN_SECRET_ENV]):
        raise RealmStageError(
            f"{ADMIN_CLIENT_ID} cannot log in with the secret from its Secret, "
            "so the first-start account stays"
        )
    bootstrap_id = environ[BOOTSTRAP_ID_ENV]
    found = expect(
        await admin.call("GET", "/admin/realms/master/clients", params={"clientId": bootstrap_id}),
        200,
        what="cannot look up the first-start account",
    )
    if not found.data:
        _say("first-start account: already removed")
        return
    expect(
        await admin.call("DELETE", f"/admin/realms/master/clients/{found.data[0]['id']}"),
        204,
        what="cannot remove the first-start account",
    )
    _say("first-start account: removed")


def _require_env(environ: Mapping[str, str], *names: str) -> None:
    for name in names:
        if not environ.get(name):
            raise RealmStageError(f"{name} is not set; the chart sets it on the Job.")


async def finish_realm(
    plan: Plan, environ: Mapping[str, str], admin: Admin, store: SecretStore
) -> None:
    """Run the realm stage once. Safe to run again."""
    _require_env(environ, ADMIN_SECRET_ENV, PUBLIC_URL_ENV, TENANT_ID_ENV, TENANT_NAME_ENV)
    _check_tenants(plan, environ)
    realm_file = load_realm_file(plan.realm_file, environ)
    session = await _log_in(admin, environ)
    realm = _Realm(admin)
    scope_ids = await realm.ensure_client_scopes(realm_file.get("clientScopes") or [])
    for group in realm_file.get("groups") or []:
        await realm.ensure_group(group)
    for tenant in plan.tenants:
        for group in tenant_groups(tenant):
            await realm.ensure_group(group, drop=OLD_ACCESS_ATTRIBUTES)
    flow_ids = await ensure_flows(admin, realm_file.get("authenticationFlows") or [])
    for want in realm_file.get("clients") or []:
        await realm.ensure_client(want, scope_ids, flow_ids)
    if plan.ci.enabled:
        await _ensure_ci(realm, plan, environ, store, scope_ids)
    if plan.company_login.enabled:
        _require_env(environ, COMPANY_SECRET_ENV)
        secret = environ[COMPANY_SECRET_ENV]
        await ensure_company_login(admin, plan.company_login, secret, realm.group_id)
    await _ensure_first_login(realm, plan, store)
    if session == "bootstrap":
        await _remove_bootstrap_account(admin, environ)


async def _wait_for_realm(admin: Admin) -> None:
    deadline = time.monotonic() + _REALM_WAIT_S
    while not await admin.realm_ready():
        if time.monotonic() > deadline:
            raise RealmStageError(
                f"Keycloak did not serve realm {REALM} within {_REALM_WAIT_S} seconds"
            )
        await asyncio.sleep(5)


async def _run_in_cluster(plan: Plan, environ: Mapping[str, str], namespace: str) -> None:
    # kubernetes_asyncio ships no type hints (see the mypy override in pyproject.toml).
    config.load_incluster_config()  # type: ignore[no-untyped-call]
    async with (
        client.ApiClient() as api_client,
        httpx.AsyncClient(base_url=plan.keycloak_url, timeout=_HTTP_TIMEOUT_S) as http,
    ):
        store = KubernetesSecretStore(client.CoreV1Api(api_client), namespace)
        admin = HttpAdmin(http)
        await _wait_for_realm(admin)
        await finish_realm(plan, environ, admin, store)


async def run(environ: Mapping[str, str] | None = None) -> None:
    """The ``keycloak`` stage of ``python -m meho_backplane.core.bootstrap``."""
    env = dict(os.environ if environ is None else environ)
    plan = load_plan(env)
    namespace = env.get(NAMESPACE_ENV)
    if not namespace:
        raise RealmStageError(f"{NAMESPACE_ENV} is not set; the chart sets it on the Job.")
    try:
        await _run_in_cluster(plan, env, namespace)
    except CoreSecretsError as exc:
        raise RealmStageError(str(exc)) from None
