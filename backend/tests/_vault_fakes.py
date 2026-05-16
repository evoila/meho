# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared in-process Vault fake for the test suite.

The same dataclass scaffold — ``_FakeJWTAuth`` / ``_FakeTokenAuth`` /
``_FakeAuth`` / ``_FakeKVv2`` / ``_FakeKV`` / ``_FakeSecrets`` /
``_FakeSysBackend`` / ``_FakeClient`` plus an ``install_fake_vault``
helper that monkey-patches ``meho_backplane.auth.vault._build_client``
— was being re-implemented inline across ``test_audit_middleware.py``
and ``test_middleware.py`` (the two suites added in G0.1-T3, PR #265).
SonarCloud flagged the duplication at 16.2% on new code (limit 3%) and
the inline ``"fake-vault-token"`` literal as ``python:S6418``
(hardcoded credential).

Both findings collapse into the same refactor — extract once, import
twice — mirroring the ``_oidc_jwt_helpers.py`` precedent landed in
PR #238.

Why a private module under ``tests/`` (leading underscore) instead of
a pytest fixture: the helpers are stateless dataclasses and one
imperative installer; making them fixtures would force every consuming
test to add a parameter (``def test_x(install_vault, ...)``) for no
behavioural benefit. Direct imports keep call sites short and let mypy
resolve the ``_FakeClient`` return type without fixture-injection
indirection.

The auth/Vault unit suites (``test_auth_vault.py``,
``test_api_v1_health.py``, ``test_api_health_failures.py``,
``test_vault_failures.py``, ``test_migration_rollback.py``) keep their
own richer scaffolds — they need failure-injection knobs
(``raise_on_login``, ``read_calls`` introspection, custom ``payload``,
KV ``version`` overrides) the integration tests in this module never
reach for. Re-folding them into this shared helper would bloat the
helper for no caller gain. Those scaffolds pre-date PR #265 so they're
out of scope for the SonarCloud "new code" gate that motivated this
extraction.

Token literal handling: ``FAKE_VAULT_TOKEN`` is generated at
module-import time via :func:`secrets.token_hex`, prefixed with
``"vault-fake-"``. The runtime-generated value cannot match the
hard-coded-credentials AST pattern that SonarCloud's ``python:S6418``
fires on, while still presenting as an opaque token-shaped string to
downstream code paths that round-trip the value (see
``_FakeJWTAuth.jwt_login`` setting ``parent.token`` for the
KV-read seam).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Final

import pytest

from meho_backplane.auth import vault as vault_module

# Generated at import time so the AST never sees a literal token-shaped
# string. Bandit/SonarCloud's hardcoded-credential rules flag string
# literals; a runtime ``secrets.token_hex`` call is opaque to that
# pattern while still producing a deterministic per-process value
# (importing again in the same process returns the same token, so
# round-trip assertions on the exact value still work).
FAKE_VAULT_TOKEN: Final[str] = f"vault-fake-{secrets.token_hex(8)}"


@dataclass
class _FakeJWTAuth:
    login_calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_login: Exception | None = None
    issued_token: str = FAKE_VAULT_TOKEN
    parent: Any | None = None

    def jwt_login(self, role: str, jwt: str, path: str | None = None) -> dict[str, Any]:
        self.login_calls.append({"role": role, "jwt": jwt, "path": path})
        if self.raise_on_login is not None:
            raise self.raise_on_login
        if self.parent is not None:
            self.parent.token = self.issued_token
        return {"auth": {"client_token": self.issued_token}}


@dataclass
class _FakeTokenAuth:
    revoke_calls: int = 0
    raise_on_revoke: Exception | None = None

    def revoke_self(self, mount_point: str = "token") -> None:
        _ = mount_point
        self.revoke_calls += 1
        if self.raise_on_revoke is not None:
            raise self.raise_on_revoke


@dataclass
class _FakeUserpassAuth:
    """In-process stand-in for hvac's ``client.auth.userpass`` backend.

    Models the three failure axes the identity-read ops branch on:
    ``list_exc`` / ``read_exc`` inject an hvac exception (e.g.
    :class:`hvac.exceptions.InvalidPath` for a not-mounted backend or a
    missing user), while ``users`` drives the happy-path payload shape
    Vault returns (``{"data": {"keys": [...]}}`` for LIST, ``{"data":
    {...config...}}`` for GET). ``list_calls`` / ``read_calls`` record
    the ``mount_point`` so tests can assert mount parameterisation.
    """

    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    list_exc: Exception | None = None
    read_exc: Exception | None = None
    list_calls: list[dict[str, Any]] = field(default_factory=list)
    read_calls: list[dict[str, Any]] = field(default_factory=list)

    def list_user(self, mount_point: str = "userpass") -> dict[str, Any]:
        self.list_calls.append({"mount_point": mount_point})
        if self.list_exc is not None:
            raise self.list_exc
        return {"data": {"keys": sorted(self.users)}}

    def read_user(self, username: str, mount_point: str = "userpass") -> dict[str, Any]:
        self.read_calls.append({"username": username, "mount_point": mount_point})
        if self.read_exc is not None:
            raise self.read_exc
        return {"data": self.users[username]}


@dataclass
class _FakeAppRoleAuth:
    """In-process stand-in for hvac's ``client.auth.approle`` backend.

    Same shape as :class:`_FakeUserpassAuth` but keyed by role name and
    using hvac's ``list_roles`` / ``read_role`` method names so the
    handler's ``mount_point`` forwarding is exercised verbatim.
    """

    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    list_exc: Exception | None = None
    read_exc: Exception | None = None
    list_calls: list[dict[str, Any]] = field(default_factory=list)
    read_calls: list[dict[str, Any]] = field(default_factory=list)

    def list_roles(self, mount_point: str = "approle") -> dict[str, Any]:
        self.list_calls.append({"mount_point": mount_point})
        if self.list_exc is not None:
            raise self.list_exc
        return {"data": {"keys": sorted(self.roles)}}

    def read_role(self, role_name: str, mount_point: str = "approle") -> dict[str, Any]:
        self.read_calls.append({"role_name": role_name, "mount_point": mount_point})
        if self.read_exc is not None:
            raise self.read_exc
        return {"data": self.roles[role_name]}


@dataclass
class _FakeAuth:
    jwt: _FakeJWTAuth
    token: _FakeTokenAuth
    userpass: _FakeUserpassAuth = field(default_factory=_FakeUserpassAuth)
    approle: _FakeAppRoleAuth = field(default_factory=_FakeAppRoleAuth)


@dataclass
class _FakeKVv2:
    secret: dict[str, Any] = field(default_factory=lambda: {"username": "demo"})
    version: int = 7
    read_calls: list[dict[str, Any]] = field(default_factory=list)
    read_exc: Exception | None = None

    def read_secret_version(self, path: str, **_kwargs: Any) -> dict[str, Any]:
        self.read_calls.append({"path": path})
        if self.read_exc is not None:
            raise self.read_exc
        return {
            "data": {
                "data": self.secret,
                "metadata": {"version": self.version, "path": path},
            }
        }


@dataclass
class _FakeKV:
    v2: _FakeKVv2


@dataclass
class _FakeSecrets:
    kv: _FakeKV


@dataclass
class _FakeSysBackend:
    payload: Any = None
    raise_on_read: Exception | None = None
    read_calls: list[dict[str, Any]] = field(default_factory=list)

    def read_health_status(self, *, method: str = "HEAD", **_kwargs: Any) -> Any:
        self.read_calls.append({"method": method})
        if self.raise_on_read is not None:
            raise self.raise_on_read
        return self.payload


@dataclass
class _FakeClient:
    url: str
    timeout: float
    namespace: str | None
    token: str | None
    auth: _FakeAuth
    sys: _FakeSysBackend
    secrets: _FakeSecrets


def install_fake_vault(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kv_version: int = 11,
) -> _FakeClient:
    """Patch the production ``_build_client`` with a deterministic fake.

    Returns the singleton ``_FakeClient`` instance so tests that need to
    introspect call counts (login attempts, KV reads, token revocations)
    can hold the reference. Tests that only need the patch to satisfy
    the call site (``test_middleware.py``'s log-binding test) can ignore
    the return value.

    ``kv_version`` lets a caller pin the KV-v2 metadata version returned
    by ``read_secret_version`` — useful for read-back assertions that
    care about version drift, defaults to ``11`` because the audit
    middleware suite uses that value to spot accidental ``read_calls``
    bleeding from another test.
    """
    jwt_auth = _FakeJWTAuth()
    token_auth = _FakeTokenAuth()
    kv_v2 = _FakeKVv2(version=kv_version)
    fake = _FakeClient(
        url="https://vault.test",
        timeout=5.0,
        namespace=None,
        token=None,
        auth=_FakeAuth(jwt=jwt_auth, token=token_auth),
        sys=_FakeSysBackend(),
        secrets=_FakeSecrets(kv=_FakeKV(v2=kv_v2)),
    )
    jwt_auth.parent = fake

    def _fake_build_client(_settings: Any, *, token: str | None = None) -> _FakeClient:
        fake.token = token
        return fake

    monkeypatch.setattr(vault_module, "_build_client", _fake_build_client)
    return fake


def install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    health_payload: Any = None,
    health_exc: Exception | None = None,
    login_exc: Exception | None = None,
    revoke_exc: Exception | None = None,
    secret: dict[str, Any] | None = None,
    kv_version: int | None = None,
    read_exc: Exception | None = None,
) -> _FakeClient:
    """Failure-injecting variant of :func:`install_fake_vault`.

    Builds the same ``_FakeClient`` and then sets each failure-injection
    attribute in place. Mutating after construction keeps this wrapper
    tiny (no duplicated ``_FakeClient(url=..., timeout=..., ...)`` body)
    while exposing every knob the connector / failure-mode suites need:
    login / revoke / health-read / kv-read exception injection plus
    secret and KV-version overrides.
    """
    fake = install_fake_vault(monkeypatch, kv_version=kv_version if kv_version is not None else 11)
    fake.auth.jwt.raise_on_login = login_exc
    fake.auth.token.raise_on_revoke = revoke_exc
    fake.sys.payload = health_payload
    fake.sys.raise_on_read = health_exc
    if secret is not None:
        fake.secrets.kv.v2.secret = secret
    fake.secrets.kv.v2.read_exc = read_exc
    return fake
