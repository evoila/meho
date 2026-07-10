# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Comprehensive Vault failure-mode tests (Task #25 — Vault half).

This module proves :func:`meho_backplane.auth.vault.vault_client_for_operator`
and :func:`meho_backplane.auth.vault.vault_readiness_probe` map every
failure shape Vault can produce onto the documented backplane error
classes — never an unhandled ``hvac.exceptions.VaultError`` escape, never
a 5xx through the route layer (Task #24's ``/api/v1/health`` cross-checks
those routes in :mod:`tests.test_api_health_failures`), never a
secret-bearing detail string.

Failure modes covered (rows from issue body, Vault half):

* Vault unreachable — DNS / TCP / TLS / read-timeout failure during
  ``jwt_login`` → :class:`VaultUnreachableError`. Readiness probe also
  reports ``unreachable: <ExceptionClassName>`` distinctly from
  ``sealed`` / ``uninitialized``.
* Vault role denied — Vault returns 403 on the JWT/OIDC login (role
  missing, bound claims mismatch, issuer trust misconfigured) →
  :class:`VaultRoleDeniedError`.
* Vault sealed — ``/sys/health`` returns 503 (and the JSON body has
  ``sealed: true``) → readiness ``ok=false`` with ``detail="sealed"``.
* Vault uninitialized — ``/sys/health`` returns 501 →
  ``detail="uninitialized"``.
* Vault standby — ``/sys/health`` returns 429 → readiness ``ok=true``
  with ``detail="http_429"`` (still serving from the operator's POV).
* Vault DR / performance-standby — 472 / 473 → ``ok=true`` with
  ``detail="http_472"`` / ``detail="http_473"``.
* Vault unexpected status — 418 → ``ok=false`` with
  ``unexpected_status`` detail.
* Secret-read denied (403 from Vault on the actual KV read after a
  successful login) — handled at the route layer in
  :mod:`tests.test_api_health_failures`.
* Misc Vault errors (4xx not 403, 5xx not 503) → :class:`VaultClientError`
  base class (sanity coverage already in ``test_auth_vault.py``;
  we extend with a few additional shapes operators routinely hit).

Test isolation re-uses the ``_install_fake_client`` pattern from
``tests/test_auth_vault.py`` — the production code constructs hvac
clients through one private helper (:func:`_build_client`); tests
monkey-patch it to return a controllable fake. No real HTTP, no
``requests``-level mocking, no Vault container.
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
    VaultNotConfiguredError,
    VaultRoleDeniedError,
    VaultUnreachableError,
    vault_client_for_operator,
    vault_readiness_probe,
)
from meho_backplane.settings import get_settings

# ---------------------------------------------------------------------------
# Fixtures + fake client
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin every env var :class:`Settings` reads."""
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
class _FakeJWTAuth:
    """Stand-in for ``client.auth.jwt`` exposing only ``jwt_login``."""

    login_calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_login: Exception | None = None
    issued_token: str = "fake-vault-token"
    parent: _FakeClient | None = None

    def jwt_login(self, role: str, jwt: str, path: str | None = None) -> dict[str, Any]:
        self.login_calls.append({"role": role, "jwt": jwt, "path": path})
        if self.raise_on_login is not None:
            raise self.raise_on_login
        if self.parent is not None:
            self.parent.token = self.issued_token
        return {"auth": {"client_token": self.issued_token}}


@dataclass
class _FakeTokenAuth:
    """Stand-in for ``client.auth.token`` covering ``revoke_self``."""

    revoke_calls: int = 0
    raise_on_revoke: Exception | None = None

    def revoke_self(self, mount_point: str = "token") -> None:
        self.revoke_calls += 1
        if self.raise_on_revoke is not None:
            raise self.raise_on_revoke


@dataclass
class _FakeAuth:
    jwt: _FakeJWTAuth
    token: _FakeTokenAuth


@dataclass
class _FakeSysBackend:
    """Stand-in for ``client.sys`` covering ``read_health_status``."""

    payload: Any = None
    raise_on_read: Exception | None = None
    read_calls: list[dict[str, Any]] = field(default_factory=list)

    def read_health_status(self, *, method: str = "HEAD", **_kwargs: Any) -> Any:
        self.read_calls.append({"method": method})
        if self.raise_on_read is not None:
            raise self.raise_on_read
        return self.payload


@dataclass
class _FakeKVv2:
    """Stand-in for ``client.secrets.kv.v2``.

    Tracks read calls and supports either returning a payload or
    raising — exercised by the secret-read denied / read-failed tests.
    """

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
class _FakeClient:
    """In-memory stand-in for :class:`hvac.Client`."""

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
    read_exc: Exception | None = None,
) -> _FakeClient:
    """Patch ``vault_module._build_client`` to return a controllable fake."""
    jwt_auth = _FakeJWTAuth(raise_on_login=login_exc)
    token_auth = _FakeTokenAuth(raise_on_revoke=revoke_exc)
    sys_backend = _FakeSysBackend(payload=health_payload, raise_on_read=health_exc)
    kv_v2 = _FakeKVv2(
        secret=secret_data or {"username": "demo"},
        read_exc=read_exc,
    )
    fake = _FakeClient(
        url="https://vault.test",
        timeout=5.0,
        namespace=None,
        token=None,
        auth=_FakeAuth(jwt=jwt_auth, token=token_auth),
        sys=sys_backend,
        secrets=_FakeSecrets(kv=_FakeKV(v2=kv_v2)),
    )
    jwt_auth.parent = fake

    def _fake_build_client(_settings: Any, *, token: str | None = None) -> _FakeClient:
        fake.token = token
        return fake

    monkeypatch.setattr(vault_module, "_build_client", _fake_build_client)
    return fake


def _make_operator(jwt: str = "fake.jwt.value") -> Operator:
    return Operator(
        sub="op-1",
        name="Alice",
        email="alice@example.com",
        raw_jwt=jwt,
        tenant_id="00000000-0000-0000-0000-00000000a0a0",
        tenant_role="operator",
    )


# ---------------------------------------------------------------------------
# Login-time failures: vault_client_for_operator
# ---------------------------------------------------------------------------


async def test_vault_unreachable_during_login_raises_unreachable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection error during ``jwt_login`` surfaces as
    :class:`VaultUnreachableError`. The detail carries only the
    exception *class* name — never the underlying URL or message."""
    _install_fake_client(
        monkeypatch,
        login_exc=requests.exceptions.ConnectionError("vault.test refused connection"),
    )

    with pytest.raises(VaultUnreachableError) as exc_info:
        async with vault_client_for_operator(_make_operator()):
            pytest.fail("body must not run when login raises")
    # Class name is in the detail; the underlying message ("refused
    # connection") is *not* — guards against operator-controllable
    # strings leaking through to a 5xx body.
    assert "ConnectionError" in str(exc_info.value)
    assert "vault.test" not in str(exc_info.value)


async def test_vault_timeout_during_login_raises_unreachable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read timeout during ``jwt_login`` surfaces as
    :class:`VaultUnreachableError` (same operator action — Vault is not
    serving)."""
    _install_fake_client(
        monkeypatch,
        login_exc=requests.exceptions.Timeout("read timed out"),
    )

    with pytest.raises(VaultUnreachableError):
        async with vault_client_for_operator(_make_operator()):
            pytest.fail("body must not run when login raises")


async def test_vault_dns_failure_during_login_raises_unreachable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DNS resolution failure (modelled as a ConnectionError subclass)
    surfaces as :class:`VaultUnreachableError`. ``requests`` raises
    ``requests.exceptions.ConnectionError`` for DNS failures, the same
    base type as TCP refused, so the dependency catches both at one
    seam."""

    class _DNSError(requests.exceptions.ConnectionError):
        pass

    _install_fake_client(monkeypatch, login_exc=_DNSError("name resolution failed"))

    with pytest.raises(VaultUnreachableError):
        async with vault_client_for_operator(_make_operator()):
            pytest.fail("body must not run when login raises")


async def test_vault_role_denied_raises_role_denied_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 from Vault on the JWT/OIDC login surfaces as
    :class:`VaultRoleDeniedError`. The most operator-actionable failure:
    the role is missing, the bound claims don't match, or the Vault
    trust of Keycloak is misconfigured."""
    _install_fake_client(
        monkeypatch,
        login_exc=hvac.exceptions.Forbidden("permission denied"),
    )

    with pytest.raises(VaultRoleDeniedError) as exc_info:
        async with vault_client_for_operator(_make_operator()):
            pytest.fail("body must not run when login raises")
    # The exception class hierarchy guarantees role-denied callers can
    # catch the subclass without also catching the unreachable one.
    assert not isinstance(exc_info.value, VaultUnreachableError)
    assert isinstance(exc_info.value, VaultClientError)


async def test_vault_invalid_request_during_login_raises_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 from Vault (bad mount path / malformed body) surfaces as
    the base :class:`VaultClientError` — not unreachable, not role-denied,
    just generic client-side failure. Operators chasing this read the
    detail and reach for ``VAULT_OIDC_MOUNT_PATH``."""
    _install_fake_client(
        monkeypatch,
        login_exc=hvac.exceptions.InvalidRequest("invalid mount path"),
    )

    with pytest.raises(VaultClientError) as exc_info:
        async with vault_client_for_operator(_make_operator()):
            pytest.fail("body must not run when login raises")
    assert not isinstance(exc_info.value, VaultUnreachableError | VaultRoleDeniedError)


async def test_vault_internal_server_error_during_login_raises_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5xx from Vault during login surfaces as
    :class:`VaultClientError`. Vault was reachable enough to answer
    HTTP, but its internal handler crashed — different operator action
    from "vault unreachable"."""
    _install_fake_client(
        monkeypatch,
        login_exc=hvac.exceptions.InternalServerError("backend exploded"),
    )

    with pytest.raises(VaultClientError) as exc_info:
        async with vault_client_for_operator(_make_operator()):
            pytest.fail("body must not run when login raises")
    # ``InternalServerError`` is a hvac-side concern; the wrapping
    # discards the message and surfaces only the class name.
    assert "InternalServerError" in str(exc_info.value)


async def test_vault_login_failure_skips_body_and_skips_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed login means no token was issued — the ``finally`` branch
    must NOT call ``revoke_self`` (it would 4xx since there's nothing to
    revoke). The context manager body is also bypassed."""
    fake = _install_fake_client(
        monkeypatch,
        login_exc=hvac.exceptions.Forbidden("denied"),
    )

    body_ran = False
    with pytest.raises(VaultRoleDeniedError):
        async with vault_client_for_operator(_make_operator()):
            body_ran = True

    assert body_ran is False
    assert fake.auth.token.revoke_calls == 0
    # And the login attempt was actually made (no early-out before
    # hvac saw the request).
    assert len(fake.auth.jwt.login_calls) == 1


# ---------------------------------------------------------------------------
# Readiness-probe failures
# ---------------------------------------------------------------------------


async def test_readiness_probe_reports_unreachable_on_dns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/sys/health`` raising ``ConnectionError`` surfaces as
    ``unreachable: ConnectionError``. The class name is in the detail;
    the URL / hostname is not."""
    _install_fake_client(
        monkeypatch,
        health_exc=requests.exceptions.ConnectionError("nx domain"),
    )
    result = await vault_readiness_probe()

    assert result.name == "vault"
    assert result.ok is False
    assert result.detail is not None
    assert result.detail.startswith("unreachable: ConnectionError")
    # Underlying message ("nx domain") must NOT leak.
    assert "nx domain" not in result.detail


async def test_readiness_probe_reports_unreachable_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read timeout against ``/sys/health`` reports ``unreachable: Timeout``.

    Pins the symmetric timeout-mapping shape — operators chasing a
    flaky Vault see the right discriminator between "down" and
    "running but slow"."""
    _install_fake_client(
        monkeypatch,
        health_exc=requests.exceptions.Timeout("upstream timeout"),
    )
    result = await vault_readiness_probe()

    assert result.ok is False
    assert result.detail is not None
    assert result.detail.startswith("unreachable: Timeout")


async def test_readiness_probe_reports_sealed_on_503_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/sys/health`` returning a 503 response object → ``detail="sealed"``.

    hvac's ``JSONAdapter`` returns the raw ``requests.Response`` for
    non-200s; the production code reads ``status_code`` and maps 503 to
    "sealed" (the canonical Vault-sealed status code)."""

    class _FakeResponse:
        status_code = 503

    _install_fake_client(monkeypatch, health_payload=_FakeResponse())
    result = await vault_readiness_probe()

    assert result.ok is False
    assert result.detail == "sealed"


async def test_readiness_probe_reports_uninitialized_on_501_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/sys/health`` returning 501 → ``detail="uninitialized"``."""

    class _FakeResponse:
        status_code = 501

    _install_fake_client(monkeypatch, health_payload=_FakeResponse())
    result = await vault_readiness_probe()

    assert result.ok is False
    assert result.detail == "uninitialized"


async def test_readiness_probe_passes_on_dr_secondary_472(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/sys/health`` returning 472 (DR secondary) → ``ok=true`` with
    ``detail="http_472"``. Vault is reachable and serving — operators
    may have a DR topology in v0.2."""

    class _FakeResponse:
        status_code = 472

    _install_fake_client(monkeypatch, health_payload=_FakeResponse())
    result = await vault_readiness_probe()

    assert result.ok is True
    assert result.detail == "http_472"


async def test_readiness_probe_passes_on_perf_standby_473(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/sys/health`` returning 473 (performance standby) → ``ok=true``."""

    class _FakeResponse:
        status_code = 473

    _install_fake_client(monkeypatch, health_payload=_FakeResponse())
    result = await vault_readiness_probe()

    assert result.ok is True
    assert result.detail == "http_473"


async def test_readiness_probe_reports_unexpected_status_for_unknown_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An undocumented status code surfaces verbatim as
    ``unexpected_status: <code>`` so operators can chase it via /ready."""

    class _FakeResponse:
        status_code = 599

    _install_fake_client(monkeypatch, health_payload=_FakeResponse())
    result = await vault_readiness_probe()

    assert result.ok is False
    assert result.detail is not None
    assert "599" in result.detail


async def test_readiness_probe_reports_sealed_when_dict_payload_says_sealed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dict payload with ``sealed: true`` (HTTP 200 path) →
    ``detail="sealed"`` and ``ok=false``.

    Vault's ``/sys/health`` can return 200 + sealed=true if a future
    proxy rewrites the status code; the dict-shape branch must still
    flip ok=false based on the payload flag."""
    _install_fake_client(
        monkeypatch,
        health_payload={"initialized": True, "sealed": True},
    )
    result = await vault_readiness_probe()

    assert result.ok is False
    assert result.detail == "sealed"


async def test_readiness_probe_reports_uninitialized_when_dict_payload_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dict payload with ``initialized: false`` → ``uninitialized``."""
    _install_fake_client(
        monkeypatch,
        health_payload={"initialized": False, "sealed": False},
    )
    result = await vault_readiness_probe()

    assert result.ok is False
    assert result.detail == "uninitialized"


async def test_readiness_probe_reports_vault_error_for_unclassifiable_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A :class:`hvac.exceptions.VaultError` from ``read_health_status``
    surfaces as ``vault_error: <ExceptionClassName>``.

    Reaches this branch when the response is wholly malformed (not a
    dict, not a Response with a status code) — hvac itself raises.
    The probe must surface the failure rather than crash the lifespan."""
    _install_fake_client(
        monkeypatch,
        health_exc=hvac.exceptions.VaultError("malformed response"),
    )
    result = await vault_readiness_probe()

    assert result.ok is False
    assert result.detail is not None
    assert result.detail.startswith("vault_error: VaultError")
    # The message ("malformed response") must NOT leak.
    assert "malformed response" not in result.detail


async def test_readiness_probe_handles_settings_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing required env vars on probe call → ``settings_unavailable: <Cls>``.

    Probes run on every ``/ready`` hit; a missing *required* env var
    mid-flight must surface as a probe-level failure rather than crash
    ``/ready``. ``VAULT_ADDR`` is no longer required (#2277 — its absence
    now means "skip Vault", see
    ``test_readiness_probe_skips_when_vault_unconfigured``), so this
    exercises the branch with ``KEYCLOAK_ISSUER_URL``."""
    monkeypatch.delenv("KEYCLOAK_ISSUER_URL", raising=False)
    get_settings.cache_clear()

    result = await vault_readiness_probe()

    assert result.ok is False
    assert result.detail is not None
    assert result.detail.startswith("settings_unavailable:")


async def test_readiness_probe_skips_when_vault_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``VAULT_ADDR`` (a Vault-free ``gsm`` install) → skipped, not failed.

    Vault is an optional credential backend since #2227; an unconfigured
    Vault is skipped so a GSM-only deploy's ``/api/v1/health`` readiness
    stays green (docs-backends skip-unconfigured contract, #1606)."""
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    get_settings.cache_clear()

    result = await vault_readiness_probe()

    assert result.name == "vault"
    assert result.ok is True
    assert result.detail == "not_configured"


async def test_vault_client_for_operator_raises_not_configured_when_vault_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CREDENTIAL_BACKEND=vault`` with no ``VAULT_ADDR`` fails at first use.

    The app boots (settings construct with ``vault_addr is None``), but
    the first Vault operation raises the actionable
    :class:`VaultNotConfiguredError` — a handled ``VaultClientError``
    (4xx-class via the dispatcher), never a 5xx."""
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    get_settings.cache_clear()

    with pytest.raises(VaultNotConfiguredError) as exc_info:
        async with vault_client_for_operator(_make_operator()):
            pass  # pragma: no cover - client construction raises first

    assert "VAULT_ADDR" in str(exc_info.value)
    assert "CREDENTIAL_BACKEND" in str(exc_info.value)
    assert isinstance(exc_info.value, VaultClientError)
