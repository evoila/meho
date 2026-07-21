# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the check-runner's service-principal identity (#2642).

:mod:`meho_backplane.auth.runner_identity` is what gives background dispatch
a bearer token. The contract this suite pins:

* **Opt-in.** Unconfigured (either setting blank) ⇒ ``""`` and Keycloak is
  never contacted, so the pre-#2642 behaviour is bit-for-bit preserved.
* **Cached.** A check-runner tick fans out one evaluation per due Sensor;
  minting per evaluation would put the token endpoint on every dispatch's
  hot path. One mint serves subsequent calls until the token nears expiry,
  and a concurrent burst after expiry mints once, not N times.
* **Fail-soft.** A Keycloak failure degrades to ``""`` (today's fail-closed
  credential read) rather than propagating into the runner loop, and never
  logs the secret or the token.

The Keycloak token endpoint is mocked with respx — no network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from meho_backplane.auth.runner_identity import (
    check_runner_jwt,
    reset_check_runner_token_cache,
)
from meho_backplane.settings import get_settings

_ISSUER = "https://kc.test/realms/meho"
_TOKEN_URL = f"{_ISSUER}/protocol/openid-connect/token"
_CLIENT_ID = "meho-check-runner"
_SECRET = "check-runner-secret-canary"
_TOKEN = "runner.principal.jwt-canary"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis env and drop the token cache around each test."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.delenv("CHECK_RUNNER_CLIENT_ID", raising=False)
    monkeypatch.delenv("CHECK_RUNNER_CLIENT_SECRET", raising=False)
    get_settings.cache_clear()
    reset_check_runner_token_cache()
    yield
    reset_check_runner_token_cache()
    get_settings.cache_clear()


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECK_RUNNER_CLIENT_ID", _CLIENT_ID)
    monkeypatch.setenv("CHECK_RUNNER_CLIENT_SECRET", _SECRET)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_unconfigured_returns_empty_and_never_calls_keycloak() -> None:
    with respx.mock as r:
        route = r.post(_TOKEN_URL)
        assert await check_runner_jwt() == ""
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_client_id_without_secret_stays_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half-configured is not configured -- a blank secret cannot authenticate."""
    monkeypatch.setenv("CHECK_RUNNER_CLIENT_ID", _CLIENT_ID)
    get_settings.cache_clear()
    with respx.mock as r:
        route = r.post(_TOKEN_URL)
        assert await check_runner_jwt() == ""
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_mints_a_client_credentials_token_for_the_configured_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    with respx.mock as r:
        route = r.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": _TOKEN, "expires_in": 300})
        )
        assert await check_runner_jwt() == _TOKEN

    body = route.calls[0].request.content.decode()
    assert "grant_type=client_credentials" in body
    assert _CLIENT_ID in body


@pytest.mark.asyncio
async def test_token_is_cached_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    with respx.mock as r:
        route = r.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": _TOKEN, "expires_in": 300})
        )
        for _ in range(5):
            assert await check_runner_jwt() == _TOKEN
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_concurrent_first_use_mints_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tick's fan-out hits an empty cache simultaneously; one mint serves all."""
    _configure(monkeypatch)
    with respx.mock as r:
        route = r.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": _TOKEN, "expires_in": 300})
        )
        results = await asyncio.gather(*(check_runner_jwt() for _ in range(8)))
    assert results == [_TOKEN] * 8
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_short_lived_token_is_reminted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lifetime inside the refresh skew is never cached -- refresh early."""
    _configure(monkeypatch)
    with respx.mock as r:
        route = r.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": _TOKEN, "expires_in": 5})
        )
        assert await check_runner_jwt() == _TOKEN
        assert await check_runner_jwt() == _TOKEN
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_keycloak_failure_degrades_to_empty_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed mint logs a code, returns ``""``, and never echoes the secret."""
    _configure(monkeypatch)
    with respx.mock as r:
        r.post(_TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
        with capture_logs() as captured:
            assert await check_runner_jwt() == ""

    blob = repr(captured)
    assert _SECRET not in blob
    event = next(e for e in captured if e["event"] == "check_runner_token_failed")
    assert event["code"] == "http_401"


@pytest.mark.asyncio
async def test_failed_mint_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient outage must not pin ``""`` for the process's lifetime."""
    _configure(monkeypatch)
    with respx.mock as r:
        r.post(_TOKEN_URL).mock(return_value=httpx.Response(503))
        assert await check_runner_jwt() == ""
    with respx.mock as r:
        r.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": _TOKEN, "expires_in": 300})
        )
        assert await check_runner_jwt() == _TOKEN


@pytest.mark.asyncio
async def test_reset_drops_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    with respx.mock as r:
        route = r.post(_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": _TOKEN, "expires_in": 300})
        )
        assert await check_runner_jwt() == _TOKEN
        reset_check_runner_token_cache()
        assert await check_runner_jwt() == _TOKEN
    assert route.call_count == 2
