# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The runner's own login: key pair, enrollment, short tokens, renewal.

Central and the login server are replaced by :class:`httpx.MockTransport`.
"""

from __future__ import annotations

import json
import logging
import stat
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from meho_backplane.runner.client import RunnerClient
from meho_backplane.runner.login import (
    KEY_FILE_NAME,
    LOGIN_FILE_NAME,
    RunnerLoginError,
    load_or_create_private_key,
    open_signed_jwt_login,
    public_jwk,
)
from meho_backplane.runner.settings import RunnerConfigError, RunnerSettings

_TOKEN_URL = "https://login.test/realms/meho/protocol/openid-connect/token"
_ISSUER = "https://login.test/realms/meho"


def _settings(key_dir: Path, **over: Any) -> RunnerSettings:
    base: dict[str, Any] = {
        "central_url": "https://central.test",
        "runner_id": "site-a",
        "key_dir": str(key_dir),
        "tick_interval_seconds": 1.0,
        "spool_dir": str(key_dir / "spool"),
        "spool_max_files": 10,
    }
    base.update(over)
    return RunnerSettings(**base)


class _Central:
    """A fake central and login server that records every request."""

    def __init__(self, *, expires_in: int = 300) -> None:
        self.requests: list[httpx.Request] = []
        self.expires_in = expires_in
        self.token_count = 0
        self.reject_next_api_call = False
        self.public_key: dict[str, Any] | None = None
        self.api_auth: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/v1/runner-principals/enroll":
            body = json.loads(request.content)
            self.public_key = body["public_key"]
            return httpx.Response(
                200,
                json={
                    "client_id": "runner:site-a",
                    "issuer": _ISSUER,
                    "token_endpoint": _TOKEN_URL,
                    "signing_alg": "ES256",
                    "key_id": "kid-1",
                },
            )
        if str(request.url) == _TOKEN_URL:
            self.token_count += 1
            return httpx.Response(
                200,
                json={"access_token": f"token-{self.token_count}", "expires_in": self.expires_in},
            )
        if request.url.path == "/api/v1/checks/assignment":
            self.api_auth.append(request.headers["Authorization"])
        if self.reject_next_api_call:
            self.reject_next_api_call = False
            return httpx.Response(401)
        return httpx.Response(304)


def test_first_start_makes_a_private_key_with_mode_0600(tmp_path: Path) -> None:
    key = load_or_create_private_key(str(tmp_path / "keys"))
    path = tmp_path / "keys" / KEY_FILE_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = load_or_create_private_key(str(tmp_path / "keys"))
    assert public_jwk(again) == public_jwk(key)
    jwk = public_jwk(key)
    assert set(jwk) == {"kty", "crv", "x", "y"}


@pytest.mark.asyncio
async def test_enrollment_sends_the_public_key_and_never_the_private_key(tmp_path: Path) -> None:
    central = _Central()
    settings = _settings(tmp_path, enrollment_code="the-code")
    provider = await open_signed_jwt_login(settings, transport=central.transport())
    assert provider is not None
    assert (tmp_path / LOGIN_FILE_NAME).exists()
    (enroll,) = central.requests
    private_pem = (tmp_path / KEY_FILE_NAME).read_bytes()
    private_numbers = (
        serialization.load_pem_private_key(private_pem, password=None).private_numbers()  # type: ignore[union-attr]
    )
    body_text = enroll.content.decode()
    assert "the-code" in body_text
    assert "PRIVATE KEY" not in body_text
    assert str(private_numbers.private_value) not in body_text
    assert "d" not in json.loads(body_text)["public_key"]


@pytest.mark.asyncio
async def test_a_refused_enrollment_stops_the_runner_with_a_clear_error(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(403, json={"detail": "x"}))
    settings = _settings(tmp_path, enrollment_code="bad")
    with pytest.raises(RunnerLoginError, match="refused"):
        await open_signed_jwt_login(settings, transport=transport)
    assert not (tmp_path / LOGIN_FILE_NAME).exists()


@pytest.mark.asyncio
async def test_login_request_is_signed_with_the_private_key_and_holds_no_key(
    tmp_path: Path,
) -> None:
    central = _Central()
    provider = await open_signed_jwt_login(
        _settings(tmp_path, enrollment_code="c"), transport=central.transport()
    )
    assert provider is not None
    assert await provider.get_token() == "token-1"
    token_request = central.requests[-1]
    form = {k: v[0] for k, v in parse_qs(token_request.content.decode()).items()}
    assert form["grant_type"] == "client_credentials"
    assert form["client_id"] == "runner:site-a"
    assertion = form["client_assertion"]
    public = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(central.public_key))
    claims = jwt.decode(assertion, public, algorithms=["ES256"], audience=_ISSUER)  # type: ignore[arg-type]
    assert claims["iss"] == claims["sub"] == "runner:site-a"
    assert claims["exp"] - claims["iat"] <= 60
    assert jwt.get_unverified_header(assertion)["kid"] == "kid-1"
    pem = (tmp_path / KEY_FILE_NAME).read_text()
    assert "PRIVATE KEY" not in token_request.content.decode()
    assert pem.splitlines()[1] not in token_request.content.decode()


@pytest.mark.asyncio
async def test_a_new_token_is_fetched_before_the_old_one_ends(tmp_path: Path) -> None:
    central = _Central(expires_in=100)
    provider = await open_signed_jwt_login(
        _settings(tmp_path, enrollment_code="c"), transport=central.transport()
    )
    assert provider is not None
    assert await provider.get_token() == "token-1"
    assert await provider.get_token() == "token-1"
    assert central.token_count == 1
    provider._renew_at = time.monotonic() - 1  # the old token is near its end
    assert await provider.get_token() == "token-2"


@pytest.mark.asyncio
async def test_after_a_401_the_client_gets_a_new_token_and_tries_once_more(
    tmp_path: Path,
) -> None:
    central = _Central()
    provider = await open_signed_jwt_login(
        _settings(tmp_path, enrollment_code="c"), transport=central.transport()
    )
    assert provider is not None
    central.reject_next_api_call = True
    async with RunnerClient(
        central_url="https://central.test",
        runner_id="site-a",
        token_provider=provider,
        transport=central.transport(),
    ) as client:
        await client.fetch_assignment(None)
    assert central.api_auth == ["Bearer token-1", "Bearer token-2"]


@pytest.mark.asyncio
async def test_after_revoke_no_new_token_comes_and_calls_fail(tmp_path: Path) -> None:
    central = _Central()
    provider = await open_signed_jwt_login(
        _settings(tmp_path, enrollment_code="c"), transport=central.transport()
    )
    assert provider is not None
    await provider.get_token()
    revoked = httpx.MockTransport(lambda _r: httpx.Response(401, json={"error": "invalid_client"}))
    provider._transport = revoked
    with pytest.raises(RunnerLoginError, match="refused"):
        await provider.refresh()

    def _central_refuses(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async with RunnerClient(
        central_url="https://central.test",
        runner_id="site-a",
        token_provider=provider,
        transport=httpx.MockTransport(_central_refuses),
    ) as client:
        from meho_backplane.runner.client import RunnerClientError

        with pytest.raises(RunnerClientError):
            await client.fetch_assignment(None)


@pytest.mark.asyncio
async def test_no_key_material_or_token_reaches_a_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    central = _Central()
    provider = await open_signed_jwt_login(
        _settings(tmp_path, enrollment_code="secret-code-value"), transport=central.transport()
    )
    assert provider is not None
    await provider.get_token()
    pem_body = (tmp_path / KEY_FILE_NAME).read_text().splitlines()[1]
    out, err = capfd.readouterr()
    seen = caplog.text + out + err
    for secret in (pem_body, "secret-code-value", "token-1", "PRIVATE KEY"):
        assert secret not in seen


@pytest.mark.asyncio
async def test_the_fixed_token_mode_works_unchanged(tmp_path: Path) -> None:
    settings = _settings(tmp_path, runner_token="fixed-token")
    assert await open_signed_jwt_login(settings) is None
    assert not (tmp_path / KEY_FILE_NAME).exists()
    seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(304)

    async with RunnerClient(
        central_url="https://central.test",
        runner_id="site-a",
        token="fixed-token",
        transport=httpx.MockTransport(_handler),
    ) as client:
        await client.fetch_assignment(None)
    assert seen == ["Bearer fixed-token"]


@pytest.mark.asyncio
async def test_no_token_no_code_and_no_key_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(RunnerConfigError, match="MEHO_RUNNER_ENROLLMENT_CODE"):
        await open_signed_jwt_login(_settings(tmp_path))


@pytest.mark.asyncio
async def test_a_runner_with_a_key_on_disk_starts_without_a_code(tmp_path: Path) -> None:
    central = _Central()
    await open_signed_jwt_login(
        _settings(tmp_path, enrollment_code="c"), transport=central.transport()
    )
    central.requests.clear()
    provider = await open_signed_jwt_login(_settings(tmp_path), transport=central.transport())
    assert provider is not None
    assert central.requests == []  # no second enrollment
    assert await provider.get_token() == "token-1"


def test_client_needs_exactly_one_login() -> None:
    with pytest.raises(ValueError, match="either"):
        RunnerClient(central_url="https://c", runner_id="r")
