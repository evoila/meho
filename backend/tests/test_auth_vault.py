# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the Vault OIDC forward-auth primitive.

Coverage matrix (per Task #23 acceptance criteria):

* Happy path: ``vault_client_for_operator(operator)`` performs
  ``jwt_login`` against the configured role + mount path, yields a
  client that can read a secret, and revokes the issued token on exit.
* Token revocation: the same suite asserts ``revoke_self`` runs on
  context exit even when the body raises.
* Per-request login: each entry into the context manager performs an
  independent ``jwt_login`` (no caching across calls).
* Vault readiness probe: passes when ``/sys/health`` returns 200 with
  ``sealed: false``; reports ``sealed`` / ``uninitialized`` /
  ``http_429`` (standby) / ``unreachable`` distinctly in ``detail``.

Failure-mode coverage beyond these basics is the responsibility of
Task #25 (G2.2-T4); this file ships a happy-path-plus-sanity-check
suite so #23 can land in isolation. The Vault-unreachable and
role-denied paths are exercised here as sanity checks because they're
the two failure modes most likely to regress on hvac upgrades.

Test isolation strategy: the production code constructs the hvac
client through one private helper (``_build_client``). Tests
monkey-patch that helper with a fake whose methods record calls and
return whatever the test scenario demands — no real HTTP, no
``requests``-level mocking, no Vault container. This keeps the suite
fast (entire run is sub-second) and decouples it from the wire-level
``requests`` library that hvac depends on.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import hvac.exceptions
import pytest
import requests.exceptions

from meho_backplane.auth import vault as vault_module
from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import (
    VaultClientError,
    VaultRoleDeniedError,
    VaultUnreachableError,
    vault_client_for_operator,
    vault_readiness_probe,
)
from meho_backplane.settings import get_settings

from ._vault_fakes import _FakeAuth, _FakeJWTAuth, _FakeSysBackend, _FakeTokenAuth

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var the Settings model reads.

    Settings are cached per-process via ``functools.lru_cache``; without
    a per-test reset, an env-var change wouldn't propagate.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass
class _FakeSecretsKVv2:
    """Stand-in for ``client.secrets.kv.v2`` covering one secret read."""

    fixture_secret: dict[str, Any] = field(default_factory=dict)
    last_token_seen: str | None = None
    parent: _FakeClient | None = None

    def read_secret_version(self, path: str, **_kwargs: Any) -> dict[str, Any]:
        # Capture whichever token the client carries when the read fires —
        # the happy-path test asserts the post-login token is in effect.
        if self.parent is not None:
            self.last_token_seen = self.parent.token
        return {"data": {"data": self.fixture_secret, "metadata": {"path": path}}}


@dataclass
class _FakeSecretsKV:
    v2: _FakeSecretsKVv2


@dataclass
class _FakeSecrets:
    kv: _FakeSecretsKV


@dataclass
class _FakeClient:
    """In-memory stand-in for :class:`hvac.Client`.

    Carries the public attributes the production code touches —
    ``token``, ``auth``, ``sys``, ``secrets`` — and nothing more.
    Constructed through :func:`_install_fake_client` so the
    ``vault_module._build_client`` seam returns it on every call.
    """

    url: str
    timeout: float
    namespace: str | None
    token: str | None
    auth: _FakeAuth
    sys: _FakeSysBackend
    secrets: _FakeSecrets


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    health_payload: Any = None,
    health_exc: Exception | None = None,
    login_exc: Exception | None = None,
    revoke_exc: Exception | None = None,
    secret_data: dict[str, Any] | None = None,
) -> _FakeClient:
    """Patch ``vault_module._build_client`` to return a controllable fake.

    Returns the fake so individual tests can read ``login_calls`` /
    ``revoke_calls`` / ``read_calls`` after exercising the production
    code path.
    """
    jwt_auth = _FakeJWTAuth(raise_on_login=login_exc)
    token_auth = _FakeTokenAuth(raise_on_revoke=revoke_exc)
    sys_backend = _FakeSysBackend(
        payload=health_payload,
        raise_on_read=health_exc,
    )
    kv_v2 = _FakeSecretsKVv2(fixture_secret=secret_data or {})
    fake = _FakeClient(
        url="https://vault.test",
        timeout=5.0,
        namespace=None,
        token=None,
        auth=_FakeAuth(jwt=jwt_auth, token=token_auth),
        sys=sys_backend,
        secrets=_FakeSecrets(kv=_FakeSecretsKV(v2=kv_v2)),
    )
    # Cross-link so the fake jwt_login can mutate ``fake.token`` and the
    # secret read can capture the active token at call time — both
    # behaviours that the real hvac client exhibits.
    jwt_auth.parent = fake
    kv_v2.parent = fake

    def _fake_build_client(_settings: Any, *, token: str | None = None) -> _FakeClient:
        fake.token = token
        return fake

    monkeypatch.setattr(vault_module, "_build_client", _fake_build_client)
    return fake


def _make_operator(jwt: str = "fake.jwt.value") -> Operator:
    """Build a minimal :class:`Operator` for the forward-auth tests."""
    return Operator(
        sub="op-1",
        name="Alice",
        email="alice@example.com",
        raw_jwt=jwt,
        tenant_id="00000000-0000-0000-0000-00000000a0a0",
        tenant_role="operator",
    )


# ---------------------------------------------------------------------------
# Settings smoke-test
# ---------------------------------------------------------------------------


def test_settings_carry_vault_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vault settings load from env with the documented defaults."""
    monkeypatch.delenv("VAULT_OIDC_ROLE", raising=False)
    monkeypatch.delenv("VAULT_OIDC_MOUNT_PATH", raising=False)
    monkeypatch.delenv("VAULT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    assert str(settings.vault_addr).startswith("https://vault.test")
    assert settings.vault_oidc_role == "meho-mcp"
    assert settings.vault_oidc_mount_path == "jwt"
    assert settings.vault_namespace is None
    assert settings.vault_timeout_seconds == 10.0


def test_settings_vault_namespace_empty_string_normalises_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator misconfiguration (``VAULT_NAMESPACE=``) is treated as OSS.

    Vault's own CLI silently drops empty ``-namespace`` values; we
    follow that convention so a misset env var doesn't poison every
    request with an empty ``X-Vault-Namespace`` header.
    """
    monkeypatch.setenv("VAULT_NAMESPACE", "")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.vault_namespace is None


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_vault_client_for_operator_logs_in_and_yields_authenticated_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``jwt_login`` runs once, the client carries the issued token, the
    body can read a secret, and ``revoke_self`` runs on exit.
    """
    fake = _install_fake_client(
        monkeypatch,
        secret_data={"username": "demo", "password": "s3cret"},
    )
    operator = _make_operator(jwt="op-jwt")

    async with vault_client_for_operator(operator) as client:
        # Cast: the fake quacks like hvac.Client for the surface the
        # caller exercises, even though it isn't a subclass.
        secret = client.secrets.kv.v2.read_secret_version(  # type: ignore[attr-defined]
            path="meho/test/federation",
        )
        assert secret["data"]["data"] == {"username": "demo", "password": "s3cret"}
        # The fake captured the token in effect at call time — the
        # post-login one, not the pre-login None.
        assert fake.secrets.kv.v2.last_token_seen == fake.auth.jwt.issued_token

    # Login was called with the configured role + mount path + raw jwt.
    assert fake.auth.jwt.login_calls == [
        {"role": "meho-mcp", "jwt": "op-jwt", "path": "jwt"},
    ]
    # Token was revoked on context exit.
    assert fake.auth.token.revoke_calls == 1


async def test_vault_client_for_operator_revokes_token_even_when_body_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``finally`` branch guarantees revoke runs on the unhappy path too."""
    fake = _install_fake_client(monkeypatch)
    operator = _make_operator()

    class _BoomError(Exception):
        pass

    with pytest.raises(_BoomError):
        async with vault_client_for_operator(operator):
            raise _BoomError("user code blew up")

    assert fake.auth.token.revoke_calls == 1


async def test_vault_client_for_operator_per_request_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each entry performs an independent login; v0.1 has no token cache."""
    fake = _install_fake_client(monkeypatch)
    operator = _make_operator()

    async with vault_client_for_operator(operator):
        pass
    async with vault_client_for_operator(operator):
        pass

    assert len(fake.auth.jwt.login_calls) == 2
    assert fake.auth.token.revoke_calls == 2


async def test_vault_client_revoke_failure_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed revoke is best-effort — it must not surface as an error.

    The body has already returned its result. A failed revoke means
    the token will time out on its own TTL; it does not invalidate
    the secret read that succeeded a moment earlier.
    """
    fake = _install_fake_client(
        monkeypatch,
        revoke_exc=hvac.exceptions.InvalidRequest("token already revoked"),
    )
    operator = _make_operator()

    async with vault_client_for_operator(operator):
        pass

    assert fake.auth.token.revoke_calls == 1


# ---------------------------------------------------------------------------
# Sanity-check failure modes (full coverage in Task #25)
# ---------------------------------------------------------------------------


async def test_vault_unreachable_wraps_to_unreachable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection failure during login surfaces as ``VaultUnreachableError``."""
    _install_fake_client(
        monkeypatch,
        login_exc=requests.exceptions.ConnectionError("no route to host"),
    )

    with pytest.raises(VaultUnreachableError) as exc_info:
        async with vault_client_for_operator(_make_operator()):
            pytest.fail("body should not run when login raises")
    assert "ConnectionError" in str(exc_info.value)


async def test_vault_timeout_wraps_to_unreachable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read timeout maps to ``VaultUnreachableError`` (same operator action)."""
    _install_fake_client(
        monkeypatch,
        login_exc=requests.exceptions.Timeout("read timed out"),
    )

    with pytest.raises(VaultUnreachableError):
        async with vault_client_for_operator(_make_operator()):
            pytest.fail("body should not run when login raises")


async def test_vault_role_denied_wraps_to_role_denied_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 from Vault surfaces as ``VaultRoleDeniedError``."""
    _install_fake_client(
        monkeypatch,
        login_exc=hvac.exceptions.Forbidden("permission denied"),
    )

    with pytest.raises(VaultRoleDeniedError):
        async with vault_client_for_operator(_make_operator()):
            pytest.fail("body should not run when login raises")


async def test_vault_other_error_wraps_to_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-403 hvac errors collapse to the base ``VaultClientError``."""
    _install_fake_client(
        monkeypatch,
        login_exc=hvac.exceptions.InvalidRequest("bad mount path"),
    )

    with pytest.raises(VaultClientError) as exc_info:
        async with vault_client_for_operator(_make_operator()):
            pytest.fail("body should not run when login raises")
    # Must NOT be a subclass — it's the generic catchall.
    assert not isinstance(exc_info.value, VaultUnreachableError | VaultRoleDeniedError)


# ---------------------------------------------------------------------------
# Readiness probe
# ---------------------------------------------------------------------------


async def test_readiness_probe_passes_on_unsealed_active_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_client(
        monkeypatch,
        health_payload={
            "initialized": True,
            "sealed": False,
            "standby": False,
            "version": "1.18.0",
        },
    )
    result = await vault_readiness_probe()

    assert result.name == "vault"
    assert result.ok is True
    assert result.detail == "sealed=False"
    assert fake.sys.read_calls == [{"method": "GET"}]


async def test_readiness_probe_fails_on_sealed_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sealed Vault answers ``/sys/health`` but cannot honour OIDC logins."""
    _install_fake_client(
        monkeypatch,
        health_payload={"initialized": True, "sealed": True},
    )
    result = await vault_readiness_probe()

    assert result.ok is False
    assert result.detail == "sealed"


async def test_readiness_probe_fails_on_uninitialized_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_client(
        monkeypatch,
        health_payload={"initialized": False, "sealed": False},
    )
    result = await vault_readiness_probe()

    assert result.ok is False
    assert result.detail == "uninitialized"


async def test_readiness_probe_passes_on_standby_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standby Vault returns 429; hvac returns the raw Response.

    For v0.1 we accept standby as "Vault reachable" — the probe is
    binary, and a standby node is still serving at the API surface.
    """

    class _FakeResponse:
        status_code = 429

    _install_fake_client(monkeypatch, health_payload=_FakeResponse())
    result = await vault_readiness_probe()

    assert result.ok is True
    assert result.detail == "http_429"


async def test_readiness_probe_reports_unexpected_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A status code outside the documented matrix surfaces verbatim."""

    class _FakeResponse:
        status_code = 418

    _install_fake_client(monkeypatch, health_payload=_FakeResponse())
    result = await vault_readiness_probe()

    assert result.ok is False
    assert result.detail is not None
    assert "418" in result.detail


async def test_readiness_probe_fails_when_vault_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_client(
        monkeypatch,
        health_exc=requests.exceptions.ConnectionError("dns failure"),
    )
    result = await vault_readiness_probe()

    assert result.ok is False
    assert result.detail is not None
    assert result.detail.startswith("unreachable: ConnectionError")


async def test_readiness_probe_handles_settings_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing required env var on probe call yields ``settings_unavailable``.

    Probes run on every ``/ready`` hit; if an operator deletes
    ``VAULT_ADDR`` mid-flight (or the cache is cleared and the env is
    incomplete), the probe must report rather than raise — uncaught
    probe exceptions surface as 500 from ``/ready`` (Task #19's
    deliberate fail-loud contract for probe-implementation bugs), and
    a settings-resolution failure is a configuration drift, not a
    probe bug.
    """
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    get_settings.cache_clear()

    result = await vault_readiness_probe()

    assert result.ok is False
    assert result.detail is not None
    assert result.detail.startswith("settings_unavailable:")
