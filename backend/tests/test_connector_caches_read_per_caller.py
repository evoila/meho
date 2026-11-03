# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every connector cache hands a cached entry to a caller only after their own Vault read.

A connector keeps a credential, a login token or a client per target. The
first caller's Vault read fills the cache. These tests show, for every such
cache, that a later caller does not get the cached entry for free:

* a caller whose own Vault read is refused (a person without the right, the
  check runner, also when it runs a Sensor under the first person's ``sub``,
  and the system operator) is refused on a warm cache too;
* a second caller who may read the secret gets the cached entry after one
  read of their own, with no second vendor login;
* a caller who has read keeps using the cache without another read;
* a target change forgets the readers of that target, and no other target.

The cache is warmed by hand for the first person, so the tests need no
vendor login and no HTTP mock. The Vault read is the only seam replaced:
the stand-in allows named people only and counts its reads.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.vault import VaultRoleDeniedError
from meho_backplane.connectors._shared.cache_key import credential_cache_key, target_cache_key
from meho_backplane.connectors._shared.microsoft_login import ClientSecretCredentials
from meho_backplane.connectors._shared.secret_readers import SecretReaders
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors.argocd.connector import ArgoCdConnector
from meho_backplane.connectors.azure.connector import AzureConnector
from meho_backplane.connectors.entra.connector import EntraConnector
from meho_backplane.connectors.github.connector import GitHubRestConnector
from meho_backplane.connectors.harbor.connector import HarborConnector
from meho_backplane.connectors.hetzner_robot.connector import HetznerRobotConnector
from meho_backplane.connectors.keycloak.connector import KeycloakConnector, _CachedToken
from meho_backplane.connectors.kubernetes.connector import KubernetesConnector
from meho_backplane.connectors.phpipam.connector import PhpipamConnector
from meho_backplane.connectors.phpipam.session import PhpipamCredentials
from meho_backplane.connectors.profiled import ProfiledRestConnector, _CachedSessionToken
from meho_backplane.connectors.proxmox.connector import ProxmoxConnector
from meho_backplane.connectors.proxmox.session import ProxmoxCredentials
from meho_backplane.connectors.rabbitmq.connector import RabbitMqConnector

_TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_SECRET_REF = "tenants/test/made-up-target"
#: The people whose Vault read passes. Everyone else is refused.
_ALLOWED = frozenset({"alice", "carol"})
_FAR_FUTURE = 1e12


@dataclass(frozen=True)
class _Target:
    """A target stub with every field the connector key helpers read."""

    name: str = "made-up-target"
    host: str = "target.example.invalid"
    port: int | None = 443
    secret_ref: str | None = _SECRET_REF
    auth_model: str | None = "shared_service_account"
    extras: dict[str, Any] = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    tenant_id: uuid.UUID = _TENANT_ID
    fqdn: str | None = None
    verify_tls: bool = True
    tls_ca_pin: str | None = None
    tls_server_name: str | None = None
    provider_secret_ref: str | None = None
    provider_username: str | None = None
    provider: str | None = None
    auth_source: str | None = None
    fingerprint: Any = None


def _person(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt=f"{sub}.test.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
    )


def _check_runner(sub: str = "__sensor__") -> Operator:
    """The identity the Sensor runner dispatches with: its own token, not a person's.

    A Sensor may run under its creator's ``sub``; the token and the Vault
    role are still the runner's.
    """
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="runner.test.jwt",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        check_runner_dispatch=True,
    )


class _Vault:
    """Stands in for the Vault read. Allows the named people only; counts reads."""

    def __init__(self, secret: Any) -> None:
        self._secret = secret
        self.reads: list[str] = []

    async def read(self, target: Any, operator: Operator) -> Any:
        del target
        self.reads.append(operator.sub)
        if operator.sub not in _ALLOWED or operator.check_runner_dispatch:
            # The runner's own Vault role may not read this secret, whatever
            # ``sub`` the Sensor runs under.
            raise VaultRoleDeniedError(f"read refused for {operator.sub}")
        return self._secret


_BASIC = {"username": "svc", "password": "made-up-password"}


@dataclass(frozen=True)
class _Case:
    """One connector cache: how to build it, warm it, call it and inspect it."""

    name: str
    secret: Any
    make: Callable[[Callable[..., Awaitable[Any]]], Any]
    warm: Callable[[Any, _Target], None]
    call: Callable[[Any, _Target, Operator], Awaitable[Any]]
    readers: Callable[[Any], SecretReaders]
    cached: Callable[[Any, _Target], bool]

    def __str__(self) -> str:
        return self.name


def _creds_cache_case(name: str, cls: type, secret: Any = _BASIC) -> _Case:
    """A connector with a per-target ``_creds_cache`` read through ``auth_headers``."""
    return _Case(
        name=name,
        secret=secret,
        make=lambda loader: cls(credentials_loader=loader),
        warm=lambda c, t: c._creds_cache.__setitem__(target_cache_key(t), secret),
        call=lambda c, t, op: c.auth_headers(t, op),
        readers=lambda c: c._readers,
        cached=lambda c, t: target_cache_key(t) in c._creds_cache,
    )


def _kubernetes_case(name: str, cache_attr: str, method: str) -> _Case:
    def _warm(c: Any, t: _Target) -> None:
        getattr(c, cache_attr)[KubernetesConnector._cache_key(t)] = MagicMock(close=AsyncMock())

    return _Case(
        name=name,
        secret={
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "default",
            "contexts": [{"name": "default", "context": {"cluster": "c1", "user": "u1"}}],
            "clusters": [{"name": "c1", "cluster": {"server": "https://k8s.example.invalid"}}],
            "users": [{"name": "u1", "user": {"token": "made-up-token"}}],
        },
        make=lambda loader: KubernetesConnector(kubeconfig_loader=loader),
        warm=_warm,
        call=lambda c, t, op: getattr(c, method)(t, op),
        readers=lambda c: c._readers,
        cached=lambda c, t: KubernetesConnector._cache_key(t) in getattr(c, cache_attr),
    )


def _microsoft_case(name: str, cls: type) -> _Case:
    creds = ClientSecretCredentials(
        tenant_id="00000000-0000-0000-0000-00000000aaaa",
        client_id="00000000-0000-0000-0000-00000000bbbb",
        client_secret="made-up-secret",
    )
    return _Case(
        name=name,
        secret=creds,
        make=lambda loader: cls(credentials_loader=loader),
        warm=lambda c, t: c._credentials.__setitem__((*target_cache_key(t), _SECRET_REF), creds),
        call=lambda c, t, op: c._credentials_for(t, op),
        readers=lambda c: c._readers,
        cached=lambda c, t: (*target_cache_key(t), _SECRET_REF) in c._credentials,
    )


def _warm_phpipam(c: Any, t: _Target) -> None:
    key = PhpipamConnector._key(t)
    c._credentials[key] = PhpipamCredentials(username="svc", password="made-up-password")
    c._tokens[key] = "made-up-token"


CASES: list[_Case] = [
    _creds_cache_case("harbor", HarborConnector),
    _creds_cache_case("argocd", ArgoCdConnector, {"token": "made-up-token"}),
    _creds_cache_case("hetzner_robot", HetznerRobotConnector),
    _creds_cache_case("rabbitmq", RabbitMqConnector),
    _creds_cache_case(
        "proxmox",
        ProxmoxConnector,
        ProxmoxCredentials(mode="token", token_id="svc@pam!meho", token_secret="made-up"),
    ),
    _Case(
        name="keycloak",
        secret=_BASIC,
        make=lambda loader: KeycloakConnector(credentials_loader=loader),
        warm=lambda c, t: c._admin_tokens.__setitem__(
            target_cache_key(t), _CachedToken("made-up-token", _FAR_FUTURE)
        ),
        call=lambda c, t, op: c.auth_headers(t, op),
        readers=lambda c: c._readers,
        cached=lambda c, t: target_cache_key(t) in c._admin_tokens,
    ),
    _Case(
        name="github",
        secret=_BASIC,
        make=lambda loader: GitHubRestConnector(credentials_loader=loader),
        warm=lambda c, t: c._pat_tokens.__setitem__(target_cache_key(t), "made-up-pat"),
        call=lambda c, t, op: c.auth_headers(t, op),
        readers=lambda c: c._readers,
        cached=lambda c, t: target_cache_key(t) in c._pat_tokens,
    ),
    _Case(
        name="profiled",
        secret=_BASIC,
        make=lambda loader: ProfiledRestConnector(credentials_loader=loader),
        warm=lambda c, t: c._session_tokens.__setitem__(
            credential_cache_key(t), _CachedSessionToken("made-up-token", None)
        ),
        call=lambda c, t, op: c._session_token(t, op),
        readers=lambda c: c._readers,
        cached=lambda c, t: credential_cache_key(t) in c._session_tokens,
    ),
    _kubernetes_case("kubernetes-rest", "_api_clients", "_get_api_client"),
    _kubernetes_case("kubernetes-ws", "_ws_api_clients", "_get_ws_api_client"),
    _Case(
        name="phpipam",
        secret=PhpipamCredentials(username="svc", password="made-up-password"),
        make=lambda loader: PhpipamConnector(credentials_loader=loader),
        warm=_warm_phpipam,
        call=lambda c, t, op: c.auth_headers(t, op),
        readers=lambda c: c._readers,
        cached=lambda c, t: PhpipamConnector._key(t) in c._credentials,
    ),
    _microsoft_case("azure", AzureConnector),
    _microsoft_case("entra", EntraConnector),
]


def _warm_for_alice(case: _Case) -> tuple[Any, _Vault, _Target]:
    """A connector whose cache holds the target's entry, read by alice."""
    vault = _Vault(case.secret)
    connector = case.make(vault.read)
    target = _Target()
    case.warm(connector, target)
    case.readers(connector).record(target, _person("alice"))
    return connector, vault, target


_REFUSED_CALLERS = {
    "person-without-the-right": _person("bob"),
    "check-runner": _check_runner(),
    "check-runner-under-the-persons-sub": _check_runner("alice"),
    "system-operator": synthesise_system_operator(),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=str)
@pytest.mark.parametrize("caller", _REFUSED_CALLERS.values(), ids=list(_REFUSED_CALLERS))
async def test_warm_cache_refuses_a_caller_whose_own_read_is_refused(
    case: _Case, caller: Operator
) -> None:
    """A warm cache is no shortcut: the caller's own Vault read runs, and its refusal counts."""
    connector, vault, target = _warm_for_alice(case)

    with pytest.raises(VaultRoleDeniedError):
        await case.call(connector, target, caller)

    assert vault.reads == [caller.sub]
    assert not case.readers(connector).has_read(target, caller)
    # The entry alice read is still there for alice.
    assert case.cached(connector, target)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=str)
async def test_second_allowed_caller_reads_once_then_uses_the_cache(case: _Case) -> None:
    """A second person who may read the secret gets the entry after one read, with no login."""
    connector, vault, target = _warm_for_alice(case)
    carol = _person("carol")

    await case.call(connector, target, carol)
    await case.call(connector, target, carol)
    await case.call(connector, target, _person("alice"))

    # One read for carol, none for alice. No vendor login ran: the test
    # mocks no HTTP, so a login would have failed.
    assert vault.reads == ["carol"]
    assert case.readers(connector).has_read(target, carol)
    assert case.cached(connector, target)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=str)
async def test_target_change_forgets_the_readers_of_that_target_only(case: _Case) -> None:
    """After a target change every caller reads the target's secret again."""
    connector, vault, target = _warm_for_alice(case)
    other = _Target(name="other-target", host="other.example.invalid")
    case.warm(connector, other)
    case.readers(connector).record(other, _person("alice"))

    await connector.invalidate_credentials(target)

    assert case.readers(connector).callers(target) == frozenset()
    assert case.readers(connector).callers(other) == frozenset({("operator", "alice")})
    assert not case.cached(connector, target)
    assert case.cached(connector, other)
    assert vault.reads == []
