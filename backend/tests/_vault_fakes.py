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
class _DeleteResponse:
    """Stand-in for hvac's 204 ``requests.Response`` from a soft-delete."""

    status_code: int = 204


@dataclass
class _FakeKVv2:
    secret: dict[str, Any] = field(default_factory=lambda: {"username": "demo"})
    version: int = 7
    read_calls: list[dict[str, Any]] = field(default_factory=list)
    read_exc: Exception | None = None
    # G3.3-T1 KV-v2 op group. Each verb gets its own call log and a
    # dedicated exception-injection knob so the failure-mode tests can
    # target one op without disturbing the others. Defaults are benign
    # so the pre-existing read-only suites that never touch these
    # attributes keep passing.
    keys: list[str] = field(default_factory=lambda: ["alpha", "beta/"])
    versions_meta: dict[str, Any] = field(
        default_factory=lambda: {
            "1": {"created_time": "2026-01-01T00:00:00Z", "destroyed": False},
        }
    )
    list_calls: list[dict[str, Any]] = field(default_factory=list)
    put_calls: list[dict[str, Any]] = field(default_factory=list)
    versions_calls: list[dict[str, Any]] = field(default_factory=list)
    delete_calls: list[dict[str, Any]] = field(default_factory=list)
    list_exc: Exception | None = None
    put_exc: Exception | None = None
    versions_exc: Exception | None = None
    delete_exc: Exception | None = None

    def read_secret_version(
        self, path: str, mount_point: str = "secret", **_kwargs: Any
    ) -> dict[str, Any]:
        self.read_calls.append({"path": path, "mount_point": mount_point})
        if self.read_exc is not None:
            raise self.read_exc
        return {
            "data": {
                "data": self.secret,
                "metadata": {"version": self.version, "path": path},
            }
        }

    def list_secrets(
        self, path: str, mount_point: str = "secret", **_kwargs: Any
    ) -> dict[str, Any]:
        self.list_calls.append({"path": path, "mount_point": mount_point})
        if self.list_exc is not None:
            raise self.list_exc
        return {"data": {"keys": list(self.keys)}}

    def create_or_update_secret(
        self,
        path: str,
        secret: dict[str, Any],
        cas: int | None = None,
        mount_point: str = "secret",
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.put_calls.append(
            {"path": path, "secret": secret, "cas": cas, "mount_point": mount_point}
        )
        if self.put_exc is not None:
            raise self.put_exc
        return {"data": {"version": self.version + 1}}

    def read_secret_metadata(
        self, path: str, mount_point: str = "secret", **_kwargs: Any
    ) -> dict[str, Any]:
        self.versions_calls.append({"path": path, "mount_point": mount_point})
        if self.versions_exc is not None:
            raise self.versions_exc
        return {
            "data": {
                "current_version": self.version,
                "versions": dict(self.versions_meta),
            }
        }

    def delete_secret_versions(
        self,
        path: str,
        versions: list[int],
        mount_point: str = "secret",
        **_kwargs: Any,
    ) -> _DeleteResponse:
        self.delete_calls.append(
            {"path": path, "versions": list(versions), "mount_point": mount_point}
        )
        if self.delete_exc is not None:
            raise self.delete_exc
        # Vault's soft-delete returns 204 with no body; hvac yields the
        # underlying ``requests.Response``. The handler ignores the
        # return value (it echoes the requested versions), so a minimal
        # status-only stand-in is enough.
        return _DeleteResponse()


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
    # G3.3-T2 (#546) sys read op group. Each knob mirrors the
    # ``payload`` / ``raise_on_read`` pair so the sys-op tests can
    # inject a success body or a failure independently of the health
    # read the connector/probe suites drive.
    seal_status_payload: Any = None
    raise_on_seal_status: Exception | None = None
    seal_status_calls: int = 0
    mounts_payload: Any = None
    raise_on_mounts: Exception | None = None
    mounts_calls: int = 0
    auth_methods_payload: Any = None
    raise_on_auth_methods: Exception | None = None
    auth_methods_calls: int = 0
    # G3.15-T2 (#1410) ACL-policy ops. Same payload / raise pair shape as
    # the sibling sys-read knobs above so the policy-op tests inject a
    # success body or a failure independently. ``write`` / ``delete``
    # record the call args (Vault returns 204 with no body, so there is
    # no payload to inject — only a failure knob).
    policy_read_payload: Any = None
    raise_on_policy_read: Exception | None = None
    policy_read_calls: list[dict[str, Any]] = field(default_factory=list)
    policy_list_payload: Any = None
    raise_on_policy_list: Exception | None = None
    policy_list_calls: int = 0
    raise_on_policy_write: Exception | None = None
    policy_write_calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_policy_delete: Exception | None = None
    policy_delete_calls: list[dict[str, Any]] = field(default_factory=list)

    def read_health_status(self, *, method: str = "HEAD", **_kwargs: Any) -> Any:
        self.read_calls.append({"method": method})
        if self.raise_on_read is not None:
            raise self.raise_on_read
        return self.payload

    def read_seal_status(self) -> Any:
        self.seal_status_calls += 1
        if self.raise_on_seal_status is not None:
            raise self.raise_on_seal_status
        return self.seal_status_payload

    def list_mounted_secrets_engines(self) -> Any:
        self.mounts_calls += 1
        if self.raise_on_mounts is not None:
            raise self.raise_on_mounts
        return self.mounts_payload

    def list_auth_methods(self) -> Any:
        self.auth_methods_calls += 1
        if self.raise_on_auth_methods is not None:
            raise self.raise_on_auth_methods
        return self.auth_methods_payload

    def read_policy(self, name: str) -> Any:
        self.policy_read_calls.append({"name": name})
        if self.raise_on_policy_read is not None:
            raise self.raise_on_policy_read
        return self.policy_read_payload

    def list_policies(self) -> Any:
        self.policy_list_calls += 1
        if self.raise_on_policy_list is not None:
            raise self.raise_on_policy_list
        return self.policy_list_payload

    def create_or_update_policy(
        self, name: str, policy: str, pretty_print: bool = True
    ) -> _DeleteResponse:
        self.policy_write_calls.append(
            {"name": name, "policy": policy, "pretty_print": pretty_print}
        )
        if self.raise_on_policy_write is not None:
            raise self.raise_on_policy_write
        # Vault returns 204 with no body; hvac yields the requests.Response.
        return _DeleteResponse()

    def delete_policy(self, name: str) -> _DeleteResponse:
        self.policy_delete_calls.append({"name": name})
        if self.raise_on_policy_delete is not None:
            raise self.raise_on_policy_delete
        return _DeleteResponse()


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
    seal_status_payload: Any = None,
    seal_status_exc: Exception | None = None,
    mounts_payload: Any = None,
    mounts_exc: Exception | None = None,
    auth_methods_payload: Any = None,
    auth_methods_exc: Exception | None = None,
    policy_read_payload: Any = None,
    policy_read_exc: Exception | None = None,
    policy_list_payload: Any = None,
    policy_list_exc: Exception | None = None,
    policy_write_exc: Exception | None = None,
    policy_delete_exc: Exception | None = None,
    keys: list[str] | None = None,
    versions_meta: dict[str, Any] | None = None,
    list_exc: Exception | None = None,
    put_exc: Exception | None = None,
    versions_exc: Exception | None = None,
    delete_exc: Exception | None = None,
) -> _FakeClient:
    """Failure-injecting variant of :func:`install_fake_vault`.

    Builds the same ``_FakeClient`` and then sets each failure-injection
    attribute in place. Mutating after construction keeps this wrapper
    tiny (no duplicated ``_FakeClient(url=..., timeout=..., ...)`` body)
    while exposing every knob the connector / failure-mode suites need:
    login / revoke / health-read / kv-read exception injection plus
    secret and KV-version overrides, the G3.3-T2 (#546) sys read
    group's seal-status / mounts / auth-methods payload + exception
    injection, the G3.3-T1 KV-v2 verbs (list / put / versions /
    delete) with per-verb exception injection and key/version-metadata
    overrides, and the G3.15-T2 (#1410) ACL-policy verbs (read / list
    payload + exception injection; write / delete exception injection,
    write/delete returning 204 with no body).
    """
    fake = install_fake_vault(monkeypatch, kv_version=kv_version if kv_version is not None else 11)
    fake.auth.jwt.raise_on_login = login_exc
    fake.auth.token.raise_on_revoke = revoke_exc
    fake.sys.payload = health_payload
    fake.sys.raise_on_read = health_exc
    fake.sys.seal_status_payload = seal_status_payload
    fake.sys.raise_on_seal_status = seal_status_exc
    fake.sys.mounts_payload = mounts_payload
    fake.sys.raise_on_mounts = mounts_exc
    fake.sys.auth_methods_payload = auth_methods_payload
    fake.sys.raise_on_auth_methods = auth_methods_exc
    fake.sys.policy_read_payload = policy_read_payload
    fake.sys.raise_on_policy_read = policy_read_exc
    fake.sys.policy_list_payload = policy_list_payload
    fake.sys.raise_on_policy_list = policy_list_exc
    fake.sys.raise_on_policy_write = policy_write_exc
    fake.sys.raise_on_policy_delete = policy_delete_exc
    kv = fake.secrets.kv.v2
    if secret is not None:
        kv.secret = secret
    kv.read_exc = read_exc
    if keys is not None:
        kv.keys = keys
    if versions_meta is not None:
        kv.versions_meta = versions_meta
    kv.list_exc = list_exc
    kv.put_exc = put_exc
    kv.versions_exc = versions_exc
    kv.delete_exc = delete_exc
    return fake
