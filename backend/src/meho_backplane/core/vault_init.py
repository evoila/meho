# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Set up the built-in OpenBao: the ``vault-init`` Job (#4044).

The chart runs ``python -m meho_backplane.core.vault_init`` as the Job
``<release>-vault-init-<hash>``. It is a normal release object, not a Helm
hook; the name ends with a hash of the Job's pod and the policies, so a
changed setup starts a new Job under Helm, klipper-helm and Argo CD alike.

1. A new server: ``init`` with one key share. The unseal key goes to the
   Secret ``<release>-core-vault-unseal`` (the ``unseal`` helper of the
   OpenBao pod reads it) and the root token to the short-lived Secret
   ``<release>-core-vault-init``; then the Job unseals the server.
   A server that is already set up: the Job goes on with the root token
   from the short-lived Secret (an earlier run stopped halfway), else it
   logs in with its own service account (login ``kubernetes-sa``).
2. It waits until the server is unsealed and Keycloak serves realm ``meho``.
3. It sets up the key-value store at ``secret/``, the ``jwt`` login that
   trusts Keycloak, the policies, the ``meho-mcp`` role with one group per
   tenant role, the ``meho-admin`` role, the ``kubernetes-sa`` login for
   later runs, the scheduler and snapshot tokens and the health check's
   test secret.
4. It revokes the root token and deletes the short-lived Secret.

A second run changes nothing. Output lines name objects; they never hold a
key or a token. Details: docs/codebase/core-vault-init.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
import jwt
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meho_backplane.connectors.vault.tenant_identity import TENANT_POLICY_FOR_ROLE
from meho_backplane.core.secrets import CoreSecretsError, KubernetesSecretStore

__all__ = [
    "PLAN_ENV",
    "Bao",
    "Cluster",
    "HttpBao",
    "Plan",
    "Reply",
    "SecretStore",
    "VaultInitError",
    "load_plan",
    "load_policies",
    "main",
    "pem_keys",
    "set_up",
]

PLAN_ENV = "MEHO_CORE_VAULT_PLAN"
NAMESPACE_ENV = "POD_NAMESPACE"

KV_MOUNT = "secret"
JWT_MOUNT = "jwt"
SA_MOUNT = "kubernetes-sa"
SA_ROLE = "vault-init"
# The audience of the Job's projected token. Its normal API token has the
# API server's audience, so the login refuses that one.
SA_AUDIENCE = "openbao"
TOKEN_ROLE = "meho-periodic"
TOKEN_PERIOD = "768h"
TEST_SECRET = "meho/test/federation"
# The Keycloak group whose members get the `meho-admin` login. The realm file
# puts the first admin in it.
ADMIN_GROUP = "MEHO-Platform-Admins"
# Replaced in the policy files with the accessor of the `jwt` login, which
# exists only after that login is made.
ACCESSOR_MARK = "JWT_ACCESSOR"
REQUIRED_POLICIES = frozenset(
    {
        "meho-mcp",
        "meho-admin",
        "meho-configurator",
        "meho-scheduler",
        "meho-snapshot",
        *TENANT_POLICY_FOR_ROLE.values(),
    }
)

# Seconds per request; the Job's backoffLimit decides whether it runs again.
_HTTP_TIMEOUT_S = 30
# How long the Job waits for OpenBao, for the unseal and for Keycloak's
# realm. In keyholder mode a person must unseal within this time on a later run.
_WAIT_S = 600
_POLL_S = 5
# OpenBao fetches Keycloak's keys itself when the Job writes the `jwt` login's
# config, over the cluster network. On a fresh install that can fail for a
# short time after the Job already sees the realm, so the Job tries again.
_JWKS_WAIT_S = 120
_JWKS_ERROR = "error checking jwks URL"


class VaultInitError(Exception):
    """A failure the Job reports as one plain sentence before it exits 1."""


class Plan(BaseModel):
    """What the chart renders into ``MEHO_CORE_VAULT_PLAN``: names and addresses, never values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # OpenBao through its Service, for example ``http://rel-vault:8200``.
    openbao_url: str = Field(min_length=1)
    # Keycloak through its Service, for example ``http://rel-keycloak:8080/auth``.
    keycloak_url: str = Field(min_length=1)
    # Where OpenBao fetches Keycloak's keys (the Keycloak Service), and the
    # public issuer the tokens carry.
    jwks_url: str = Field(min_length=1)
    bound_issuer: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    policy_dir: str = Field(min_length=1)
    service_account: str = Field(min_length=1)
    sa_token_file: str = Field(min_length=1)
    unseal_secret: str = Field(min_length=1)
    init_secret: str = Field(min_length=1)
    scheduler_secret: str = Field(min_length=1)
    snapshot_secret: str = Field(min_length=1)


@dataclass(frozen=True)
class Reply:
    status: int
    data: Any


class Bao(Protocol):
    """One call to OpenBao's HTTP API (``/v1/<path>``)."""

    async def call(self, method: str, path: str, *, token: str = "", json: Any = None) -> Reply: ...


class Cluster(Protocol):
    async def issuer_keys(self) -> tuple[str, list[str]]:
        """The cluster's service-account token issuer and its signing keys as PEM text."""


class SecretStore(Protocol):
    async def read_data(self, name: str) -> dict[str, str] | None: ...

    async def put(self, name: str, data: Mapping[str, str]) -> None: ...

    async def delete(self, name: str) -> None: ...


class HttpBao:
    """:class:`Bao` over ``httpx`` against the OpenBao Service."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def call(self, method: str, path: str, *, token: str = "", json: Any = None) -> Reply:
        headers = {"X-Vault-Token": token} if token else {}
        try:
            reply = await self._http.request(method, f"/v1/{path}", json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise VaultInitError(f"OpenBao request failed: {type(exc).__name__}") from None
        data: Any = None
        if reply.content and "json" in reply.headers.get("content-type", ""):
            data = reply.json()
        return Reply(reply.status_code, data)


def _detail(reply: Reply) -> str:
    errors = reply.data.get("errors") if isinstance(reply.data, dict) else None
    if errors:
        return f"HTTP {reply.status}: {'; '.join(str(e) for e in errors)}"
    return f"HTTP {reply.status}"


def _expect(reply: Reply, *statuses: int, what: str) -> Reply:
    if reply.status not in statuses:
        raise VaultInitError(f"{what}: {_detail(reply)}")
    return reply


def _say(line: str) -> None:
    print(line, flush=True)


def pem_keys(jwks: Sequence[Mapping[str, Any]]) -> list[str]:
    """The signing keys of a JWKS document as PEM text.

    The CIS profile turns off anonymous access to the API server, and
    OpenBao sends no token when it fetches keys, so the ``kubernetes-sa``
    login gets the keys as PEM text instead of a JWKS address.
    """
    pems: list[str] = []
    for key in jwks:
        if key.get("use", "sig") != "sig":
            continue
        try:
            public = jwt.PyJWK(dict(key)).key
            pem = public.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        except (jwt.PyJWKError, AttributeError, ValueError) as exc:
            raise VaultInitError(
                f"the cluster published a signing key the Job cannot read: {exc}"
            ) from None
        pems.append(pem.decode("ascii"))
    if not pems:
        raise VaultInitError("the cluster published no signing key at /openid/v1/jwks")
    return pems


def load_policies(directory: str) -> dict[str, str]:
    """The policy files of the chart's ConfigMap, by policy name."""
    policies = {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(Path(directory).glob("*.hcl"))
    }
    missing = sorted(REQUIRED_POLICIES - set(policies))
    if missing:
        raise VaultInitError(f"the policy folder {directory} has no file for {', '.join(missing)}")
    return policies


def load_plan(environ: Mapping[str, str]) -> Plan:
    raw = environ.get(PLAN_ENV)
    if not raw:
        raise VaultInitError(f"{PLAN_ENV} is not set; the chart sets it on the Job.")
    try:
        return Plan.model_validate_json(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise VaultInitError(f"{PLAN_ENV} is not a valid plan: {problems}") from None


async def _wait(ready: Callable[[], Awaitable[bool]], timeout_message: str) -> None:
    deadline = time.monotonic() + _WAIT_S
    while not await ready():
        if time.monotonic() > deadline:
            raise VaultInitError(timeout_message)
        await asyncio.sleep(_POLL_S)


async def _seal_status(bao: Bao) -> dict[str, Any] | None:
    """The seal status, or None while OpenBao does not answer (its pod starts or restarts)."""
    try:
        reply = await bao.call("GET", "sys/seal-status")
    except VaultInitError:
        return None
    return reply.data if reply.status == 200 and isinstance(reply.data, dict) else None


async def _answers(bao: Bao) -> bool:
    return await _seal_status(bao) is not None


async def _unsealed(bao: Bao) -> bool:
    status = await _seal_status(bao)
    return status is not None and status.get("sealed") is False


@dataclass(frozen=True)
class _Session:
    token: str
    # The token is the root token, which the Job revokes at the end.
    root: bool
    # This run initialised the server, so Secrets from an earlier server are stale.
    fresh: bool


async def _sa_login(plan: Plan, bao: Bao) -> str:
    try:
        sa_token = Path(plan.sa_token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VaultInitError(
            f"cannot read the Job's token {plan.sa_token_file}: {exc.strerror}"
        ) from None
    reply = await bao.call(
        "POST", f"auth/{SA_MOUNT}/login", json={"role": SA_ROLE, "jwt": sa_token}
    )
    if reply.status != 200:
        raise VaultInitError(
            "OpenBao is set up, but the Job cannot log in with its service account "
            f"({_detail(reply)}). This happens when OpenBao was set up by hand, when the "
            "cluster's signing key changed, or when the first run stopped early. Put a "
            f"root token into the Secret {plan.init_secret} (key: token) and run the Job "
            "again; it revokes that token when it is done. See "
            "docs/codebase/core-vault-init.md."
        )
    token: str = reply.data["auth"]["client_token"]
    return token


async def _open_session(plan: Plan, bao: Bao, store: SecretStore) -> _Session:
    # A new install starts this Job and the OpenBao pod at the same time.
    await _wait(lambda: _answers(bao), f"OpenBao did not answer within {_WAIT_S} seconds")
    status = _expect(await bao.call("GET", "sys/init"), 200, what="cannot read OpenBao's status")
    if not status.data["initialized"]:
        made = _expect(
            await bao.call("PUT", "sys/init", json={"secret_shares": 1, "secret_threshold": 1}),
            200,
            what="OpenBao init failed",
        ).data
        key: str = made["keys_base64"][0]
        root: str = made["root_token"]
        # The key first: without it the server can never be unsealed again.
        await store.put(plan.unseal_secret, {"key": key})
        await store.put(plan.init_secret, {"token": root})
        _say(f"OpenBao: initialised; unseal key in Secret {plan.unseal_secret}")
        _expect(await bao.call("PUT", "sys/unseal", json={"key": key}), 200, what="unseal failed")
        return _Session(root, root=True, fresh=True)

    await _wait(
        lambda: _unsealed(bao),
        f"OpenBao is still sealed after {_WAIT_S} seconds. With the Secret "
        f"{plan.unseal_secret} the pod unseals itself; without it (keyholder mode) a "
        "person unseals it, and the Job runs again.",
    )
    saved = await store.read_data(plan.init_secret)
    if saved and saved.get("token"):
        alive = await bao.call("GET", "auth/token/lookup-self", token=saved["token"])
        if alive.status == 200:
            _say(
                f"OpenBao: an earlier run stopped halfway; going on with Secret {plan.init_secret}"
            )
            return _Session(saved["token"], root=True, fresh=False)
    session = _Session(await _sa_login(plan, bao), root=False, fresh=False)
    _say(f"OpenBao: logged in with service account {plan.service_account}")
    if saved is not None:
        # The root token in it is already revoked: an earlier run stopped
        # right after the revoke.
        await store.delete(plan.init_secret)
    return session


async def _mounts(bao: Bao, token: str, path: str) -> dict[str, Any]:
    reply = _expect(await bao.call("GET", path, token=token), 200, what=f"cannot read {path}")
    found: dict[str, Any] = reply.data["data"]
    return found


async def _ensure_kv(bao: Bao, token: str) -> None:
    mount = (await _mounts(bao, token, "sys/mounts")).get(f"{KV_MOUNT}/")
    if mount is None:
        _expect(
            await bao.call(
                "POST",
                f"sys/mounts/{KV_MOUNT}",
                token=token,
                json={"type": "kv", "options": {"version": "2"}},
            ),
            204,
            what=f"cannot make the key-value store at {KV_MOUNT}/",
        )
        _say(f"key-value store {KV_MOUNT}/: made")
        return
    if mount.get("type") != "kv" or (mount.get("options") or {}).get("version") != "2":
        raise VaultInitError(f"{KV_MOUNT}/ exists but is not a key-value store version 2")
    _say(f"key-value store {KV_MOUNT}/: kept")


async def _ensure_jwt_login(bao: Bao, token: str, path: str) -> str:
    """Make the ``jwt`` login at ``path`` if it is missing; return its accessor."""
    mount = (await _mounts(bao, token, "sys/auth")).get(f"{path}/")
    if mount is None:
        _expect(
            await bao.call("POST", f"sys/auth/{path}", token=token, json={"type": "jwt"}),
            204,
            what=f"cannot make the login {path}/",
        )
        _say(f"login {path}/: made")
        mount = (await _mounts(bao, token, "sys/auth"))[f"{path}/"]
    elif mount.get("type") != "jwt":
        raise VaultInitError(f"the login {path}/ exists but is not of type jwt")
    accessor: str = mount["accessor"]
    return accessor


async def _write(bao: Bao, token: str, path: str, body: Mapping[str, Any]) -> None:
    _expect(
        await bao.call("POST", path, token=token, json=dict(body)),
        200,
        204,
        what=f"cannot write {path}",
    )


async def _write_jwt_config(bao: Bao, token: str, plan: Plan) -> None:
    path = f"auth/{JWT_MOUNT}/config"
    body = {"jwks_url": plan.jwks_url, "bound_issuer": plan.bound_issuer}
    deadline = time.monotonic() + _JWKS_WAIT_S
    waited = False
    while True:
        reply = await bao.call("POST", path, token=token, json=body)
        if reply.status in (200, 204):
            return
        detail = _detail(reply)
        if _JWKS_ERROR not in detail or time.monotonic() > deadline:
            raise VaultInitError(f"cannot write {path}: {detail}")
        if not waited:
            _say(f"login {JWT_MOUNT}/: OpenBao cannot read Keycloak's keys yet; trying again")
            waited = True
        await asyncio.sleep(_POLL_S)


async def _ensure_group(bao: Bao, token: str, policy: str, role: str, accessor: str) -> None:
    """An external group that gives ``policy`` to every login whose tenant_role is ``role``."""
    path = f"identity/group/name/{policy}"

    async def read() -> dict[str, Any] | None:
        found = _expect(
            await bao.call("GET", path, token=token), 200, 404, what=f"cannot read group {policy}"
        )
        return None if found.status == 404 else dict(found.data["data"])

    # Read first: rewriting an unchanged group still changes its alias.
    group = await read()
    if group is None or group.get("type") != "external" or group.get("policies") != [policy]:
        await _write(bao, token, path, {"type": "external", "policies": [policy]})
        group = await read()
        if group is None:
            raise VaultInitError(f"group {policy} is missing right after it was written")
    alias = group.get("alias") or {}
    if alias.get("name") == role and alias.get("mount_accessor") == accessor:
        return
    body = {"name": role, "mount_accessor": accessor, "canonical_id": group["id"]}
    alias_path = (
        f"identity/group-alias/id/{alias['id']}" if alias.get("id") else "identity/group-alias"
    )
    await _write(bao, token, alias_path, body)


async def _ensure_token(
    bao: Bao, token: str, store: SecretStore, secret: str, policy: str, fresh: bool
) -> None:
    if not fresh:
        saved = await store.read_data(secret)
        if saved and saved.get("token"):
            alive = await bao.call("GET", "auth/token/lookup-self", token=saved["token"])
            if alive.status == 200:
                _say(f"Secret {secret}: kept")
                return
    made = _expect(
        await bao.call(
            "POST", f"auth/token/create/{TOKEN_ROLE}", token=token, json={"policies": [policy]}
        ),
        200,
        what=f"cannot make the {policy} token",
    ).data
    await store.put(secret, {"token": made["auth"]["client_token"]})
    _say(f"Secret {secret}: new {policy} token")


async def _ensure_test_secret(bao: Bao, token: str) -> None:
    path = f"{KV_MOUNT}/data/{TEST_SECRET}"
    found = await bao.call("GET", path, token=token)
    if found.status == 200:
        return
    _expect(found, 404, what=f"cannot read {path}")
    await _write(
        bao,
        token,
        path,
        {"data": {"purpose": "MEHO health check: GET /api/v1/health reads this path"}},
    )
    _say(f"test secret {KV_MOUNT}/{TEST_SECRET}: made")


async def set_up(
    plan: Plan,
    namespace: str,
    bao: Bao,
    cluster: Cluster,
    store: SecretStore,
    realm_ready: Callable[[], Awaitable[bool]],
) -> None:
    """Run the Job once. Safe to run again."""
    policies = load_policies(plan.policy_dir)
    session = await _open_session(plan, bao, store)
    token = session.token
    await _wait(realm_ready, f"Keycloak did not serve realm meho within {_WAIT_S} seconds")

    await _ensure_kv(bao, token)
    accessor = await _ensure_jwt_login(bao, token, JWT_MOUNT)
    await _write_jwt_config(bao, token, plan)
    for name, text in policies.items():
        _expect(
            await bao.call(
                "PUT",
                f"sys/policies/acl/{name}",
                token=token,
                json={"policy": text.replace(ACCESSOR_MARK, accessor)},
            ),
            204,
            what=f"cannot write policy {name}",
        )
    _say(f"policies: {', '.join(policies)}")

    # One role for every tenant. The token's tenant_id goes into the login's
    # metadata, which the tenant policies read; tenant_role names the group
    # that gives the role's policy.
    await _write(
        bao,
        token,
        f"auth/{JWT_MOUNT}/role/meho-mcp",
        {
            "role_type": "jwt",
            "user_claim": "sub",
            "bound_audiences": [plan.audience],
            "claim_mappings": {"tenant_id": "tenant_id", "tenant_role": "tenant_role"},
            "groups_claim": "tenant_role",
            "token_policies": ["meho-mcp"],
            "token_ttl": "1h",
        },
    )
    for role, policy in TENANT_POLICY_FOR_ROLE.items():
        await _ensure_group(bao, token, policy, role.value, accessor)
    await _write(
        bao,
        token,
        f"auth/{JWT_MOUNT}/role/meho-admin",
        {
            "role_type": "jwt",
            "user_claim": "sub",
            "bound_audiences": [plan.audience],
            "bound_claims": {"groups": [ADMIN_GROUP]},
            "token_policies": ["meho-admin"],
            "token_ttl": "1h",
        },
    )
    _say(f"login roles meho-mcp and meho-admin on {JWT_MOUNT}/: set")

    await _ensure_jwt_login(bao, token, SA_MOUNT)
    issuer, keys = await cluster.issuer_keys()
    await _write(
        bao,
        token,
        f"auth/{SA_MOUNT}/config",
        {"jwt_validation_pubkeys": keys, "bound_issuer": issuer},
    )
    await _write(
        bao,
        token,
        f"auth/{SA_MOUNT}/role/{SA_ROLE}",
        {
            "role_type": "jwt",
            "user_claim": "sub",
            "bound_subject": f"system:serviceaccount:{namespace}:{plan.service_account}",
            "bound_audiences": [SA_AUDIENCE],
            "token_policies": ["meho-configurator"],
            "token_ttl": "15m",
        },
    )
    _say(f"login role {SA_ROLE} on {SA_MOUNT}/: set (service account {plan.service_account})")

    # Orphan tokens: they outlive the token that made them. A token role
    # makes periodic tokens without root.
    await _write(
        bao,
        token,
        f"auth/token/roles/{TOKEN_ROLE}",
        {
            "allowed_policies": ["meho-scheduler", "meho-snapshot"],
            "orphan": True,
            "token_period": TOKEN_PERIOD,
        },
    )
    await _ensure_token(bao, token, store, plan.scheduler_secret, "meho-scheduler", session.fresh)
    await _ensure_token(bao, token, store, plan.snapshot_secret, "meho-snapshot", session.fresh)
    await _ensure_test_secret(bao, token)

    if session.root:
        _expect(
            await bao.call("POST", "auth/token/revoke-self", token=token),
            204,
            what="cannot revoke the root token",
        )
        await store.delete(plan.init_secret)
        _say(f"root token revoked; Secret {plan.init_secret} deleted")
    _say("OpenBao: set up")


class KubernetesCluster:
    """:class:`Cluster` over the Kubernetes API.

    Every service account may read the issuer and its keys (the default
    ``system:service-account-issuer-discovery`` binding).
    """

    def __init__(self, api_client: Any) -> None:
        self._api = api_client

    async def _json(self, call: Callable[..., Awaitable[Any]], what: str) -> Any:
        try:
            reply = await call(_preload_content=False, _request_timeout=_HTTP_TIMEOUT_S)
            return json.loads(await reply.read())
        except ApiException as exc:
            raise VaultInitError(f"cannot read the cluster's {what}: HTTP {exc.status}") from None

    async def issuer_keys(self) -> tuple[str, list[str]]:
        discovery = await self._json(
            client.WellKnownApi(self._api).get_service_account_issuer_open_id_configuration,
            "token issuer",
        )
        jwks = await self._json(
            client.OpenidApi(self._api).get_service_account_issuer_open_id_keyset, "signing keys"
        )
        return str(discovery["issuer"]), pem_keys(jwks["keys"])


async def _run_in_cluster(plan: Plan, namespace: str) -> None:
    # kubernetes_asyncio ships no type hints (see the mypy override in pyproject.toml).
    config.load_incluster_config()  # type: ignore[no-untyped-call]
    async with (
        client.ApiClient() as api_client,
        httpx.AsyncClient(base_url=plan.openbao_url, timeout=_HTTP_TIMEOUT_S) as bao_http,
        httpx.AsyncClient(base_url=plan.keycloak_url, timeout=_HTTP_TIMEOUT_S) as kc_http,
    ):

        async def realm_ready() -> bool:
            try:
                reply = await kc_http.get("/realms/meho/.well-known/openid-configuration")
            except httpx.TransportError:
                return False
            return reply.is_success

        await set_up(
            plan,
            namespace,
            HttpBao(bao_http),
            KubernetesCluster(api_client),
            KubernetesSecretStore(client.CoreV1Api(api_client), namespace),
            realm_ready,
        )


def main() -> int:
    try:
        plan = load_plan(os.environ)
        namespace = os.environ.get(NAMESPACE_ENV)
        if not namespace:
            raise VaultInitError(f"{NAMESPACE_ENV} is not set; the chart sets it on the Job.")
        asyncio.run(_run_in_cluster(plan, namespace))
    except (VaultInitError, CoreSecretsError) as exc:
        print(f"vault_init_failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"vault_init_failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
