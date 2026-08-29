# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Recorded-fixture credential-read chain for the modern fleet-lcm loader (#3047).

Proves the fleet-lcm connector's **default (non-injected)** credential loader
end to end against an in-process Vault fake:

    FleetLcmConnector()                                  # default loader
      -> auth_headers
      -> load_credentials_from_vault (session.py)        # the fleet-lcm loader
      -> load_vault_secret_data (G3.9-T2)                # operator-context read
      -> {"Authorization": "Bearer <token>"}  when the Vault secret carries a
         non-empty ``token`` (the spec's primary ``bearerToken`` scheme),
         else {"Authorization": "Basic <b64>"} (the ``basicAuth`` alternative).

The Bearer branch is the **#3047 token-provisioning seam**: an operator opts a
target into Bearer by staging a ``token`` field alongside username/password in
the target's Vault secret; the loader surfaces it and ``auth_headers`` prefers
it. No live appliance is reached (#1002 / #995) — the transport is a Vault fake
— so this proves the *loader → header* seam, not the live appliance handshake
(the documented live-verify follow-up).

Complements :mod:`tests.test_connectors_fleet_lcm_auth`, which exercises the
same header branches through **injected** loader stubs; this module drives the
**real** default loader so a regression in the Vault-read wiring (fields read,
operator-context login, whitespace strip) is caught. Layout mirrors
:mod:`tests.test_connectors_vcf_fleet_credread` (the legacy impl's live
cred-read gate) and reuses the shared :mod:`tests._vault_fakes` scaffold.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
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


def _decode_basic_auth(authorization_header: str) -> tuple[str, str]:
    """Decode an ``Authorization: Basic <b64>`` header into (username, password)."""
    assert authorization_header.startswith("Basic ")
    decoded = base64.b64decode(authorization_header[len("Basic ") :]).decode()
    username, _, password = decoded.partition(":")
    return username, password


@pytest.mark.asyncio
async def test_default_loader_emits_basic_when_secret_has_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Vault secret with only username/password → Basic, read under the operator."""
    fake = install_fake_client(
        monkeypatch,
        secret={"username": _CANARY_USERNAME, "password": _CANARY_PASSWORD},
    )
    connector = FleetLcmConnector()  # default loader — the real Vault read
    target = _StubTarget()
    operator = _make_operator()

    headers = await connector.auth_headers(target, operator)

    username, password = _decode_basic_auth(headers["Authorization"])
    assert username == _CANARY_USERNAME
    assert password == _CANARY_PASSWORD
    # The default loader read Vault under the operator's identity.
    assert fake.auth.jwt.login_calls[-1]["jwt"] == "op.credread.fleet-lcm.jwt"
    assert fake.secrets.kv.v2.read_calls[-1]["path"] == target.secret_ref
    await connector.aclose()


@pytest.mark.asyncio
async def test_default_loader_emits_bearer_when_secret_carries_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Vault secret carrying a ``token`` → Bearer (the token-provisioning seam)."""
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
async def test_default_loader_blank_token_falls_back_to_basic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only ``token`` counts as unconfigured → Basic, not an empty Bearer."""
    install_fake_client(
        monkeypatch,
        secret={
            "username": _CANARY_USERNAME,
            "password": _CANARY_PASSWORD,
            "token": "   ",
        },
    )
    connector = FleetLcmConnector()
    headers = await connector.auth_headers(_StubTarget(), _make_operator())

    assert headers["Authorization"].startswith("Basic ")
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
async def test_default_loader_never_leaks_credential_values_in_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No credential *value* (password or token) appears in any structlog event.

    The shared reader logs only the field *names* present, never a value; this
    confirms the discipline survives the fleet-lcm loader's raw-payload read
    even on the Bearer path (where the token is the live credential).
    """
    install_fake_client(
        monkeypatch,
        secret={
            "username": _CANARY_USERNAME,
            "password": _CANARY_PASSWORD,
            "token": _CANARY_TOKEN,
        },
    )
    connector = FleetLcmConnector()

    with capture_logs() as captured:
        await connector.auth_headers(_StubTarget(), _make_operator())

    log_blob = repr(captured)
    assert _CANARY_PASSWORD not in log_blob
    assert _CANARY_TOKEN not in log_blob
    await connector.aclose()
