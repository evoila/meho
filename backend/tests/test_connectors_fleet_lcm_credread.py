# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Credential-read chain for the modern fleet-lcm loader (#3047, #4476).

Drives the connector's **default (non-injected)** loader end to end against an
in-process Vault fake:

    FleetLcmConnector()                                  # default loader
      -> auth_headers
      -> load_credentials_from_vault (session.py)        # the fleet-lcm loader
      -> load_vault_secret_data                          # operator-context read
      -> VCF Operations login (acquire, then exchange) when the secret has no
         ``token``; the stored ``token`` as is when it has one
      -> {"Authorization": "Bearer <token>"}

The login answers are mocked with ``respx``. Complements
:mod:`tests.test_connectors_fleet_lcm_auth`, which drives the same seam
through injected loaders; this module catches a regression in the Vault-read
wiring (fields read, ``vcf_ops_url`` check, operator-context login,
whitespace strip). Reuses the shared :mod:`tests._vault_fakes` scaffold.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
import respx
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.fleet_lcm import FleetLcmConnector
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

# ---------------------------------------------------------------------------
# Canary credential values — asserted to NEVER leak into logs. Fleet's local
# user store uses ``admin@local`` verbatim (the @local is part of the
# username, not a realm decoration).
# ---------------------------------------------------------------------------

_CANARY_USERNAME = "admin@local"
_CANARY_PASSWORD = "p4ss-canary-must-not-leak-fleet-lcm"
_CANARY_TOKEN = "brr-canary-token-must-not-leak"
_CANARY_OPS_TOKEN = "ops-canary-token-must-not-leak"
_CANARY_JWT = "jwt-canary-token-must-not-leak"
_TARGET_BASE_URL = "https://fleet-lcm-credread.test.invalid"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis env vars Settings + the Vault client read."""
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
class _StubTarget:
    name: str = "fleet-lcm-credread"
    host: str = "fleet-lcm-credread.test.invalid"
    port: int | None = 443
    secret_ref: str = "fleet-lcm/credread"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt so the fail-closed Vault gate passes."""
    return Operator(
        sub="op-credread-fleet-lcm",
        name="Cred Read Fleet LCM Operator",
        email=None,
        raw_jwt="op.credread.fleet-lcm.jwt",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


def _mock_login(mock: respx.MockRouter) -> tuple[respx.Route, respx.Route]:
    """Mock the VCF Operations acquire + exchange answers on *mock*."""
    acquire = mock.post("/suite-api/api/auth/token/acquire").respond(
        200, json={"token": _CANARY_OPS_TOKEN}
    )
    exchange = mock.post("/suite-api/api/auth/token/exchange").respond(
        200, json={"jwtToken": _CANARY_JWT}
    )
    return acquire, exchange


@pytest.mark.asyncio
async def test_default_loader_logs_in_when_secret_has_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secret with only username/password → VCF Operations login → Bearer."""
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = FleetLcmConnector()  # default loader — the real Vault read
    target = _StubTarget()
    operator = _make_operator()

    async with respx.mock(base_url=_TARGET_BASE_URL) as mock:
        acquire, _exchange = _mock_login(mock)
        headers = await connector.auth_headers(target, operator)

    assert headers == {"Authorization": f"Bearer {_CANARY_JWT}"}
    assert json.loads(acquire.calls[0].request.content) == {
        "username": _CANARY_USERNAME,
        "password": _CANARY_PASSWORD,
    }
    # The default loader read Vault under the operator's identity.
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.credread.fleet-lcm.jwt"
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == target.secret_ref
    await connector.aclose()


@pytest.mark.asyncio
async def test_default_loader_logs_in_at_vcf_ops_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``vcf_ops_url`` in the secret moves the login to that address."""
    install_fake_client(
        monkeypatch,
        secret={
            "username": _CANARY_USERNAME,
            "password": _CANARY_PASSWORD,
            "vcf_ops_url": "https://vcf-ops-credread.test.invalid/\n",
        },
    )
    connector = FleetLcmConnector()

    async with respx.mock(base_url="https://vcf-ops-credread.test.invalid") as mock:
        _mock_login(mock)
        headers = await connector.auth_headers(_StubTarget(), _make_operator())

    assert headers == {"Authorization": f"Bearer {_CANARY_JWT}"}
    await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_url",
    [
        "http://vcf-ops.test.invalid",
        "vcf-ops.test.invalid",
        "https://vcf-ops.test.invalid/suite-api",
        "https://vcf-ops.test.invalid?x=1",
        "https://user@vcf-ops.test.invalid",
        "https://vcf-ops.test.invalid:99999",
    ],
)
async def test_default_loader_rejects_a_bad_vcf_ops_url(
    monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    """Anything but ``https://host[:port]`` fails closed, naming the target."""
    install_fake_client(
        monkeypatch,
        secret={
            "username": _CANARY_USERNAME,
            "password": _CANARY_PASSWORD,
            "vcf_ops_url": bad_url,
        },
    )
    connector = FleetLcmConnector()

    with pytest.raises(VaultCredentialsReadError, match="vcf_ops_url") as exc_info:
        await connector.auth_headers(_StubTarget(), _make_operator())

    assert "fleet-lcm-credread" in str(exc_info.value)
    await connector.aclose()


@pytest.mark.asyncio
async def test_default_loader_emits_bearer_when_secret_carries_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Vault secret carrying a ``token`` → Bearer with it, no login (the fallback)."""
    install_fake_client(
        monkeypatch,
        secret={
            "username": _CANARY_USERNAME,
            "password": _CANARY_PASSWORD,
            "token": _CANARY_TOKEN,
        },
    )
    connector = FleetLcmConnector()  # default loader
    headers = await connector.auth_headers(_StubTarget(), _make_operator())

    assert headers == {"Authorization": f"Bearer {_CANARY_TOKEN}"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_default_loader_strips_whitespace_around_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token stored with a trailing newline is stripped before the Bearer header."""
    install_fake_client(
        monkeypatch,
        secret={
            "username": _CANARY_USERNAME,
            "password": _CANARY_PASSWORD,
            "token": f"  {_CANARY_TOKEN}\n",
        },
    )
    connector = FleetLcmConnector()
    headers = await connector.auth_headers(_StubTarget(), _make_operator())

    assert headers == {"Authorization": f"Bearer {_CANARY_TOKEN}"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_default_loader_blank_token_logs_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only ``token`` counts as not set → login, not an empty Bearer."""
    install_fake_client(
        monkeypatch,
        secret={
            "username": _CANARY_USERNAME,
            "password": _CANARY_PASSWORD,
            "token": "   ",
        },
    )
    connector = FleetLcmConnector()
    async with respx.mock(base_url=_TARGET_BASE_URL) as mock:
        _mock_login(mock)
        headers = await connector.auth_headers(_StubTarget(), _make_operator())

    assert headers == {"Authorization": f"Bearer {_CANARY_JWT}"}
    await connector.aclose()


@pytest.mark.asyncio
async def test_default_loader_missing_password_raises_naming_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token-only secret (no username/password pair) fails closed naming the target."""
    install_fake_client(monkeypatch, secret={"token": _CANARY_TOKEN})
    connector = FleetLcmConnector()

    with pytest.raises(VaultCredentialsReadError) as exc_info:
        await connector.auth_headers(_StubTarget(), _make_operator())

    message = str(exc_info.value)
    assert "fleet-lcm-credread" in message
    assert "username" in message and "password" in message
    await connector.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_token", [True, False], ids=["stored-token", "login"])
async def test_default_loader_never_leaks_credential_values_in_logs(
    monkeypatch: pytest.MonkeyPatch, stored_token: bool
) -> None:
    """No password or token value appears in any structlog event, on either path."""
    secret = {"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD}
    if stored_token:
        secret["token"] = _CANARY_TOKEN
    install_fake_client(monkeypatch, secret=secret)
    connector = FleetLcmConnector()

    with capture_logs() as captured:
        async with respx.mock(base_url=_TARGET_BASE_URL, assert_all_called=False) as mock:
            _mock_login(mock)
            await connector.auth_headers(_StubTarget(), _make_operator())

    log_blob = repr(captured)
    for value in (_CANARY_PASSWORD, _CANARY_TOKEN, _CANARY_OPS_TOKEN, _CANARY_JWT):
        assert value not in log_blob
    await connector.aclose()
