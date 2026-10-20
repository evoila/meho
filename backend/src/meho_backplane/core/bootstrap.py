# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Finish the built-in Keycloak realm after every install and upgrade.

The chart runs this module as the ``bootstrap`` Helm hook Job
(``deploy/charts/meho/templates/core-bootstrap-job.yaml``), post-install and
post-upgrade: ``python -m meho_backplane.core.bootstrap keycloak``. Keycloak
imports the realm file only at its first start, so this stage does what the
file cannot do, and what a newer chart adds later:

1. Wait until Keycloak serves realm ``meho``.
2. Log in. While Keycloak's temporary bootstrap account still exists, check
   that ``meho-admin`` logs in (and set its secret through the bootstrap
   account if it does not). Every call below runs as ``meho-admin``.
3. Add what is missing: the role groups from values, and the clients and
   client mappers of the realm file. Nothing that exists is changed, except
   a client secret that differs from its Kubernetes Secret.
4. At the first install only: make the first admin and the second person,
   with temporary passwords stored first in ``<release>-core-first-login``.
5. Delete the bootstrap account, last, so a failed run can retry with it.

A second run with the same values changes nothing. The output names objects
and ``created``, ``kept``, ``set`` or ``deleted``. It never holds a value.
Exit code ``0`` on success, ``1`` on any failure, with the reason on stderr
as ``core_bootstrap_failed: <sentence>``. See docs/codebase/core-bootstrap.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import string
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import httpx
from kubernetes_asyncio import client, config
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from meho_backplane.auth.keycloak_admin import KeycloakAdminClient, KeycloakAdminError
from meho_backplane.core.secrets import (
    NAMESPACE_ENV,
    CoreSecretsError,
    KubernetesSecretStore,
    StoredSecret,
)

__all__ = [
    "BOOTSTRAP_ID_ENV",
    "BOOTSTRAP_SECRET_ENV",
    "DONE_ANNOTATION",
    "PLAN_ENV",
    "REALM_FILE_ENV",
    "CoreBootstrapError",
    "FirstLoginStore",
    "Plan",
    "load_plan",
    "load_realm",
    "main",
    "reconcile",
]

PLAN_ENV = "MEHO_CORE_BOOTSTRAP_PLAN"
REALM_FILE_ENV = "MEHO_CORE_REALM_FILE"
# The same variables the Keycloak pod reads, from the same Secret.
BOOTSTRAP_ID_ENV = "KC_BOOTSTRAP_ADMIN_CLIENT_ID"
BOOTSTRAP_SECRET_ENV = "KC_BOOTSTRAP_ADMIN_CLIENT_SECRET"
# Also a placeholder of the realm file: the meho-admin client's secret.
ADMIN_SECRET_ENV = "MEHO_REALM_ADMIN_CLIENT_SECRET"
# Set on the first-login Secret once both people exist. From then on the
# stage never creates them again, so a person deleted on purpose stays gone.
DONE_ANNOTATION = "meho.evoila.com/first-login-users"

_REALM = "meho"
_ADMIN_CLIENT_ID = "meho-admin"
_PLATFORM_ADMIN_ROLE = "platform_admin"
_ALPHABET = string.ascii_letters + string.digits
_PASSWORD_LENGTH = 32
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
# A plain `helm install` (no --wait) starts this hook while Keycloak still
# builds and imports the realm; its start probe allows 5 minutes.
_WAIT_FOR_REALM_S = 600.0
_WAIT_STEP_S = 5.0


class CoreBootstrapError(Exception):
    """A failure the Job reports as one plain sentence before it exits 1."""


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    tenant_role: str = Field(min_length=1)
    approver: bool = False


class Person(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    username: str = Field(min_length=1)
    group: str = Field(min_length=1)


class Plan(BaseModel):
    """What the chart renders into ``MEHO_CORE_BOOTSTRAP_PLAN``: no secret."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Keycloak's Service URL with the `/auth` path, for example
    # `http://meho-keycloak:8080/auth`.
    keycloak_url: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    groups: tuple[Group, ...] = Field(min_length=1)
    first_login_secret: str = Field(min_length=1)
    admin: Person
    approver: Person

    @model_validator(mode="after")
    def _people_in_listed_groups(self) -> Plan:
        names = {group.name for group in self.groups}
        for person in (self.admin, self.approver):
            if person.group not in names:
                raise ValueError(f"group {person.group} of {person.username} is not in groups")
        return self


class FirstLoginStore(Protocol):
    """The Secret calls the first-login step needs, in one namespace."""

    async def read_data(self, name: str) -> StoredSecret | None: ...

    async def create(self, name: str, data: Mapping[str, str], secret_type: str) -> bool: ...

    async def annotate(self, name: str, annotations: Mapping[str, str]) -> None: ...


def _say(subject: str, result: str) -> None:
    print(f"{subject}: {result}", flush=True)


def _new_password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_PASSWORD_LENGTH))


def _describe(resp: httpx.Response) -> str:
    """Status line plus Keycloak's own error text; never the request."""
    message = ""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        message = str(body.get("errorMessage") or body.get("error") or "")
    status = f"HTTP {resp.status_code}"
    return f"{status}: {message}" if message else status


async def _call(
    kc: KeycloakAdminClient,
    what: str,
    method: str,
    path: str,
    *,
    ok: tuple[int, ...] = (200, 201, 204),
    json_body: Any = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    try:
        resp = await kc.request(method, path, json_body=json_body, params=params)
    except KeycloakAdminError as exc:
        raise CoreBootstrapError(f"cannot {what}: {exc}") from None
    if resp.status_code not in ok:
        raise CoreBootstrapError(f"cannot {what}: {_describe(resp)}")
    return resp


def _created_id(resp: httpx.Response, what: str) -> str:
    location = str(resp.headers.get("location", "")).rstrip("/")
    if "/" not in location:
        raise CoreBootstrapError(f"cannot {what}: Keycloak returned no Location header")
    return location.rsplit("/", 1)[-1]


def load_realm(path: Path, environ: Mapping[str, str]) -> dict[str, Any]:
    """Read the realm file and fill its ``${NAME}`` placeholders from *environ*."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CoreBootstrapError(f"cannot read the realm file {path}: {exc.strerror}") from None

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        if not environ.get(name):
            raise CoreBootstrapError(f"{name} is not set; the chart sets it on the Job.")
        return environ[name]

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return _PLACEHOLDER.sub(fill, node)
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        return node

    try:
        realm = json.loads(text)
    except ValueError as exc:
        raise CoreBootstrapError(f"the realm file {path} is not JSON: {exc}") from None
    filled: dict[str, Any] = walk(realm)
    return filled


async def _wait_for_realm(base_url: str) -> None:
    url = f"{base_url}/realms/{_REALM}/.well-known/openid-configuration"
    deadline = time.monotonic() + _WAIT_FOR_REALM_S
    last = "no answer"
    async with httpx.AsyncClient(timeout=10.0) as http:
        while True:
            try:
                resp = await http.get(url)
                if resp.status_code == 200:
                    return
                last = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            if time.monotonic() >= deadline:
                raise CoreBootstrapError(
                    f"Keycloak did not serve realm {_REALM} at {base_url} within "
                    f"{int(_WAIT_FOR_REALM_S)} s (last: {last})."
                )
            await asyncio.sleep(_WAIT_STEP_S)


def _client(base_url: str, realm: str, client_id: str, client_secret: str) -> KeycloakAdminClient:
    # Admin paths below start with the realm (`/meho/...`, `/master/...`).
    return KeycloakAdminClient(
        admin_url=f"{base_url}/admin/realms",
        token_url=f"{base_url}/realms/{realm}/protocol/openid-connect/token",
        client_id=client_id,
        client_secret=client_secret,
    )


def _login_refused(exc: KeycloakAdminError) -> bool:
    # Keycloak answers an unknown client or a wrong secret with 400 or 401.
    cause = exc.__cause__
    return isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in (400, 401)


async def _open(stack: contextlib.AsyncExitStack, kc: KeycloakAdminClient) -> bool:
    """Log *kc* in; ``False`` if Keycloak refuses the credentials."""
    try:
        await stack.enter_async_context(kc)
    except KeycloakAdminError as exc:
        if _login_refused(exc):
            return False
        raise CoreBootstrapError(f"cannot log in to Keycloak: {exc}") from None
    return True


async def _client_uuid(kc: KeycloakAdminClient, realm: str, client_id: str) -> str | None:
    resp = await _call(
        kc,
        f"read client {client_id}",
        "GET",
        f"/{realm}/clients",
        params={"clientId": client_id},
    )
    found = [c for c in resp.json() if c.get("clientId") == client_id]
    return str(found[0]["id"]) if found else None


async def _set_client_secret(
    kc: KeycloakAdminClient, realm: str, client_id: str, secret: str
) -> bool:
    """Make the client's secret *secret*; ``True`` if it had another one."""
    uuid = await _client_uuid(kc, realm, client_id)
    if uuid is None:
        raise CoreBootstrapError(f"client {client_id} does not exist in realm {realm}")
    path = f"/{realm}/clients/{uuid}"
    current = await _call(kc, f"read the secret of {client_id}", "GET", f"{path}/client-secret")
    if current.json().get("value") == secret:
        return False
    # PUT replaces the whole client, so send back what GET returned.
    rep = (await _call(kc, f"read client {client_id}", "GET", path)).json()
    rep["secret"] = secret
    await _call(kc, f"set the secret of {client_id}", "PUT", path, json_body=rep)
    return True


async def _reconcile_groups(kc: KeycloakAdminClient, plan: Plan) -> None:
    resp = await _call(kc, "list groups", "GET", f"/{_REALM}/groups", params={"max": "1000"})
    present = {g.get("name") for g in resp.json()}
    for group in plan.groups:
        if group.name in present:
            _say(f"group {group.name}", "kept")
            continue
        attributes = {"tenant_id": [plan.tenant_id], "tenant_role": [group.tenant_role]}
        if group.approver:
            attributes["approver"] = ["true"]
        await _call(
            kc,
            f"create group {group.name}",
            "POST",
            f"/{_REALM}/groups",
            json_body={"name": group.name, "attributes": attributes},
        )
        _say(f"group {group.name}", "created")


async def _reconcile_clients(kc: KeycloakAdminClient, realm: Mapping[str, Any]) -> None:
    for rep in realm.get("clients", []):
        client_id = rep["clientId"]
        uuid = await _client_uuid(kc, _REALM, client_id)
        if uuid is None:
            await _call(
                kc, f"create client {client_id}", "POST", f"/{_REALM}/clients", json_body=rep
            )
            _say(f"client {client_id}", "created")
            continue
        mappers_path = f"/{_REALM}/clients/{uuid}/protocol-mappers/models"
        resp = await _call(kc, f"list the mappers of {client_id}", "GET", mappers_path)
        present = {m.get("name") for m in resp.json()}
        for mapper in rep.get("protocolMappers", []):
            if mapper["name"] not in present:
                await _call(
                    kc,
                    f"add mapper {mapper['name']} to {client_id}",
                    "POST",
                    mappers_path,
                    json_body=mapper,
                )
                _say(f"client {client_id} mapper {mapper['name']}", "created")
        if rep.get("secret"):
            changed = await _set_client_secret(kc, _REALM, client_id, rep["secret"])
            _say(f"client {client_id}", "secret set" if changed else "secret kept")
        else:
            _say(f"client {client_id}", "kept")


async def _user_id(kc: KeycloakAdminClient, username: str) -> str | None:
    resp = await _call(
        kc,
        f"read user {username}",
        "GET",
        f"/{_REALM}/users",
        params={"username": username, "exact": "true"},
    )
    found = [u for u in resp.json() if u.get("username") == username]
    return str(found[0]["id"]) if found else None


async def _create_person(
    kc: KeycloakAdminClient, person: Person, password: str, *, platform_admin: bool
) -> None:
    what = f"create user {person.username}"
    resp = await _call(
        kc,
        what,
        "POST",
        f"/{_REALM}/users",
        json_body={
            "username": person.username,
            "enabled": True,
            "groups": [f"/{person.group}"],
            "credentials": [{"type": "password", "value": password, "temporary": True}],
            "requiredActions": ["UPDATE_PASSWORD"],
        },
    )
    user_id = _created_id(resp, what)
    try:
        groups = await _call(kc, what, "GET", f"/{_REALM}/users/{user_id}/groups")
        if [g.get("name") for g in groups.json()] != [person.group]:
            raise CoreBootstrapError(f"cannot {what}: Keycloak did not put it in {person.group}")
        if platform_admin:
            roles_path = f"/{_REALM}/users/{user_id}/role-mappings/realm"
            available = await _call(kc, what, "GET", f"{roles_path}/available")
            role = [r for r in available.json() if r.get("name") == _PLATFORM_ADMIN_ROLE]
            if not role:
                raise CoreBootstrapError(
                    f"cannot {what}: realm {_REALM} has no role {_PLATFORM_ADMIN_ROLE}"
                )
            await _call(kc, what, "POST", roles_path, json_body=role)
    except BaseException:
        # Never leave a half-made person: the next run makes it again.
        with contextlib.suppress(Exception):
            await kc.request("DELETE", f"/{_REALM}/users/{user_id}")
        raise


async def _first_login(kc: KeycloakAdminClient, plan: Plan, store: FirstLoginStore) -> None:
    name = plan.first_login_secret
    stored = await store.read_data(name)
    if stored is not None and stored.annotations.get(DONE_ANNOTATION) == "made":
        _say(f"secret {name}", "kept (the first-login users were made at the first install)")
        return
    people = (("admin", plan.admin), ("approver", plan.approver))
    data: Mapping[str, str]
    if stored is None:
        made: dict[str, str] = {}
        for key, person in people:
            made[f"{key}_username"] = person.username
            made[f"{key}_password"] = _new_password()
        data = made
        if not await store.create(name, data, "Opaque"):
            raise CoreBootstrapError(f'Secret "{name}" appeared while the Job ran; run it again.')
        _say(f"secret {name}", "created")
    else:
        data = stored.data
        for key, person in people:
            if data.get(f"{key}_username") != person.username or not data.get(f"{key}_password"):
                raise CoreBootstrapError(
                    f'Secret "{name}" does not hold {key}_username "{person.username}" and '
                    f"{key}_password; delete it so the Job makes new first-login passwords."
                )
        _say(f"secret {name}", "kept")
    for key, person in people:
        if await _user_id(kc, person.username) is not None:
            _say(f"user {person.username}", "kept")
            continue
        await _create_person(kc, person, data[f"{key}_password"], platform_admin=key == "admin")
        _say(f"user {person.username}", f"created in {person.group}")
    await store.annotate(name, {DONE_ANNOTATION: "made"})


async def reconcile(
    plan: Plan,
    realm: Mapping[str, Any],
    environ: Mapping[str, str],
    store: FirstLoginStore,
) -> None:
    """Run the realm stage once against Keycloak at ``plan.keycloak_url``."""
    for env in (BOOTSTRAP_ID_ENV, BOOTSTRAP_SECRET_ENV, ADMIN_SECRET_ENV):
        if not environ.get(env):
            raise CoreBootstrapError(f"{env} is not set; the chart sets it on the Job.")
    base = plan.keycloak_url.rstrip("/")
    await _wait_for_realm(base)
    bootstrap_id = environ[BOOTSTRAP_ID_ENV]
    admin_secret = environ[ADMIN_SECRET_ENV]
    async with contextlib.AsyncExitStack() as stack:
        boot = _client(base, "master", bootstrap_id, environ[BOOTSTRAP_SECRET_ENV])
        have_bootstrap = await _open(stack, boot)
        _say("bootstrap account", "found" if have_bootstrap else "gone")
        admin = _client(base, _REALM, _ADMIN_CLIENT_ID, admin_secret)
        admin_ok = await _open(stack, admin)
        if not admin_ok and have_bootstrap:
            await _set_client_secret(boot, _REALM, _ADMIN_CLIENT_ID, admin_secret)
            _say(f"client {_ADMIN_CLIENT_ID}", "secret set")
            admin = _client(base, _REALM, _ADMIN_CLIENT_ID, admin_secret)
            admin_ok = await _open(stack, admin)
        if not admin_ok:
            raise CoreBootstrapError(
                f"Keycloak refuses {_ADMIN_CLIENT_ID} with the secret in its Kubernetes Secret, "
                "and the bootstrap account is gone; set the client's secret in Keycloak."
            )
        await _reconcile_groups(admin, plan)
        await _reconcile_clients(admin, realm)
        await _first_login(admin, plan, store)
        if have_bootstrap:
            uuid = await _client_uuid(boot, "master", bootstrap_id)
            if uuid is not None:
                await _call(
                    boot, "delete the bootstrap account", "DELETE", f"/master/clients/{uuid}"
                )
            _say("bootstrap account", "deleted")


def load_plan(environ: Mapping[str, str]) -> Plan:
    raw = environ.get(PLAN_ENV)
    if not raw:
        raise CoreBootstrapError(f"{PLAN_ENV} is not set; the chart sets it on the Job.")
    try:
        return Plan.model_validate_json(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise CoreBootstrapError(f"{PLAN_ENV} is not a valid plan: {problems}") from None


async def _run_in_cluster(plan: Plan, realm: Mapping[str, Any], namespace: str) -> None:
    # kubernetes_asyncio ships no type hints (see the mypy override in pyproject.toml).
    config.load_incluster_config()  # type: ignore[no-untyped-call]
    async with client.ApiClient() as api_client:
        store = KubernetesSecretStore(client.CoreV1Api(api_client), namespace)
        await reconcile(plan, realm, os.environ, store)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if args != ["keycloak"]:
            raise CoreBootstrapError("usage: python -m meho_backplane.core.bootstrap keycloak")
        plan = load_plan(os.environ)
        realm_file = os.environ.get(REALM_FILE_ENV)
        if not realm_file:
            raise CoreBootstrapError(f"{REALM_FILE_ENV} is not set; the chart sets it on the Job.")
        realm = load_realm(Path(realm_file), os.environ)
        namespace = os.environ.get(NAMESPACE_ENV)
        if not namespace:
            raise CoreBootstrapError(f"{NAMESPACE_ENV} is not set; the chart sets it on the Job.")
        asyncio.run(_run_in_cluster(plan, realm, namespace))
    except (CoreBootstrapError, CoreSecretsError) as exc:
        print(f"core_bootstrap_failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"core_bootstrap_failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
