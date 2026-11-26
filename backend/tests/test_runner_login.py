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
import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from meho_backplane.runner import wire
from meho_backplane.runner.client import RunnerClient
from meho_backplane.runner.key_id import ec_key_id
from meho_backplane.runner.login import (
    KEY_FILE_NAME,
    LOGIN_FILE_NAME,
    RunnerLoginError,
    SignedJwtTokenProvider,
    _LoginRecord,
    load_or_create_private_key,
    open_signed_jwt_login,
    public_jwk,
)
from meho_backplane.runner.settings import (
    DEFAULT_KEY_DIR,
    RunnerConfigError,
    RunnerSettings,
    get_runner_settings,
)

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
        self.client_id = "runner:site-a"
        self.token_url = _TOKEN_URL
        self.refuse_enrollment = False
        self.refuse_name = False
        self.signing_alg = "ES256"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/v1/runner-principals/enroll":
            body = json.loads(request.content)
            if self.refuse_enrollment:
                return httpx.Response(403, json={"detail": "enrollment_code_invalid"})
            if self.refuse_name:
                return httpx.Response(409, json={"detail": "enrollment_code_for_another_runner"})
            self.public_key = body["public_key"]
            return httpx.Response(
                200,
                json={
                    "client_id": self.client_id,
                    "issuer": _ISSUER,
                    "token_endpoint": self.token_url,
                    "signing_alg": self.signing_alg,
                    "key_id": ec_key_id(body["public_key"]),
                },
            )
        if request.url.path.endswith("/openid-connect/token"):
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
    assert central.public_key is not None
    assert jwt.get_unverified_header(assertion)["kid"] == ec_key_id(central.public_key)
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


# ---------------------------------------------------------------------------
# https
# ---------------------------------------------------------------------------

_PLAIN_HTTP = [
    "http://central.test",
    "http://192.0.2.10:8080",
    "http://localhost.example.test",
    "http://user@central.test",
    "ftp://central.test",
    "central.test",
    "https://",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", _PLAIN_HTTP)
async def test_central_must_be_https_before_the_code_is_sent(tmp_path: Path, url: str) -> None:
    central = _Central()
    settings = _settings(tmp_path, enrollment_code="c", central_url=url)
    with pytest.raises(RunnerConfigError, match=r"MEHO_RUNNER_CENTRAL_URL must start with https"):
        await open_signed_jwt_login(settings, transport=central.transport())
    assert central.requests == []
    assert not (tmp_path / KEY_FILE_NAME).exists()


@pytest.mark.asyncio
async def test_central_must_be_https_also_with_a_saved_login(tmp_path: Path) -> None:
    central = _Central()
    await open_signed_jwt_login(
        _settings(tmp_path, enrollment_code="c"), transport=central.transport()
    )
    with pytest.raises(RunnerConfigError, match="MEHO_RUNNER_CENTRAL_URL"):
        await open_signed_jwt_login(
            _settings(tmp_path, central_url="http://central.test"), transport=central.transport()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("url", _PLAIN_HTTP)
async def test_the_token_url_setting_must_be_https_before_anything_is_sent(
    tmp_path: Path, url: str
) -> None:
    central = _Central()
    settings = _settings(tmp_path, enrollment_code="c", token_endpoint_override=url)
    with pytest.raises(RunnerConfigError, match=r"MEHO_RUNNER_TOKEN_URL must start with https"):
        await open_signed_jwt_login(settings, transport=central.transport())
    assert central.requests == []


@pytest.mark.asyncio
async def test_the_token_address_from_central_must_be_https(tmp_path: Path) -> None:
    central = _Central()
    central.token_url = "http://login.test/realms/meho/protocol/openid-connect/token"
    settings = _settings(tmp_path, enrollment_code="c")
    with pytest.raises(RunnerConfigError, match="token address"):
        await open_signed_jwt_login(settings, transport=central.transport())
    assert central.token_count == 0
    # The enrollment is kept: with an https token address the runner starts.
    provider = await open_signed_jwt_login(
        _settings(tmp_path, token_endpoint_override=_TOKEN_URL), transport=central.transport()
    )
    assert provider is not None
    assert await provider.get_token() == "token-1"
    assert len(central.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"]
)
async def test_plain_http_to_this_machine_works(tmp_path: Path, base: str) -> None:
    central = _Central()
    central.token_url = f"{base}/realms/meho/protocol/openid-connect/token"
    provider = await open_signed_jwt_login(
        _settings(tmp_path, enrollment_code="c", central_url=base), transport=central.transport()
    )
    assert provider is not None
    assert await provider.get_token() == "token-1"


def test_plain_http_to_this_machine_uses_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:3128")
    provider = SignedJwtTokenProvider(
        key=ec.generate_private_key(ec.SECP256R1()),
        record=_LoginRecord(
            client_id="runner:r", issuer="i", token_endpoint="t", key_id="k", signing_alg="ES256"
        ),
    )
    local = RunnerClient(
        central_url="http://localhost:8000", runner_id="r", token_provider=provider
    )
    assert local._client._mounts == {}
    remote = RunnerClient(
        central_url="https://central.test", runner_id="r", token_provider=provider
    )
    assert remote._client._mounts != {}


# ---------------------------------------------------------------------------
# A lost or other key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_missing_key_is_reported_and_no_new_key_is_made(tmp_path: Path) -> None:
    central = _Central()
    await open_signed_jwt_login(
        _settings(tmp_path, enrollment_code="c"), transport=central.transport()
    )
    (tmp_path / KEY_FILE_NAME).unlink()
    central.requests.clear()
    with pytest.raises(RunnerLoginError, match="is missing") as caught:
        await open_signed_jwt_login(_settings(tmp_path), transport=central.transport())
    assert "runner-principal register" in str(caught.value)
    assert "MEHO_RUNNER_ENROLLMENT_CODE" in str(caught.value)
    assert "empty MEHO_RUNNER_SPOOL_DIR" in str(caught.value)
    assert "keep the old one" in str(caught.value)
    assert central.requests == []
    assert not (tmp_path / KEY_FILE_NAME).exists()


@pytest.mark.asyncio
async def test_another_key_is_reported_and_the_old_key_put_back_works(tmp_path: Path) -> None:
    central = _Central()
    await open_signed_jwt_login(
        _settings(tmp_path, enrollment_code="c"), transport=central.transport()
    )
    key_file = tmp_path / KEY_FILE_NAME
    old_key = key_file.read_bytes()
    key_file.unlink()
    load_or_create_private_key(str(tmp_path))
    central.requests.clear()
    with pytest.raises(RunnerLoginError, match="does not match the saved login"):
        await open_signed_jwt_login(_settings(tmp_path), transport=central.transport())
    assert central.requests == []
    key_file.write_bytes(old_key)
    provider = await open_signed_jwt_login(_settings(tmp_path), transport=central.transport())
    assert provider is not None
    assert await provider.get_token() == "token-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["key-missing", "other-key", "record-damaged"])
async def test_a_new_code_recovers_a_runner_that_lost_its_login(tmp_path: Path, loss: str) -> None:
    central = _Central()
    await open_signed_jwt_login(
        _settings(tmp_path, enrollment_code="old"), transport=central.transport()
    )
    old_key_id = json.loads((tmp_path / LOGIN_FILE_NAME).read_text())["key_id"]
    if loss == "record-damaged":
        (tmp_path / LOGIN_FILE_NAME).write_text('{"client_id": "run')
    else:
        (tmp_path / KEY_FILE_NAME).unlink()
        if loss == "other-key":
            load_or_create_private_key(str(tmp_path))
    central.client_id = "runner:site-a2"
    provider = await open_signed_jwt_login(
        _settings(tmp_path, runner_id="site-a2", enrollment_code="new"),
        transport=central.transport(),
    )
    assert provider is not None
    assert await provider.get_token() == "token-1"
    record = json.loads((tmp_path / LOGIN_FILE_NAME).read_text())
    assert record["client_id"] == "runner:site-a2"
    assert record["key_id"] == ec_key_id(public_jwk(load_or_create_private_key(str(tmp_path))))
    assert (record["key_id"] == old_key_id) is (loss == "record-damaged")
    # The next start needs no code.
    again = _settings(tmp_path, runner_id="site-a2")
    assert await open_signed_jwt_login(again, transport=central.transport()) is not None


@pytest.mark.asyncio
async def test_a_missing_key_with_the_used_code_says_how_to_recover(tmp_path: Path) -> None:
    central = _Central()
    settings = _settings(tmp_path, enrollment_code="used")
    await open_signed_jwt_login(settings, transport=central.transport())
    record = (tmp_path / LOGIN_FILE_NAME).read_text()
    (tmp_path / KEY_FILE_NAME).unlink()
    central.refuse_enrollment = True
    with pytest.raises(RunnerLoginError, match=r"is missing.*refused") as caught:
        await open_signed_jwt_login(settings, transport=central.transport())
    assert "runner-principal register" in str(caught.value)
    assert (tmp_path / LOGIN_FILE_NAME).read_text() == record


@pytest.mark.asyncio
async def test_the_runner_reads_only_es256_in_the_answer_and_in_the_saved_login(
    tmp_path: Path,
) -> None:
    central = _Central()
    central.signing_alg = "RS256"
    with pytest.raises(RunnerLoginError, match=r"signing type RS256.*only ES256"):
        await open_signed_jwt_login(
            _settings(tmp_path, enrollment_code="c"), transport=central.transport()
        )
    assert not (tmp_path / LOGIN_FILE_NAME).exists()
    assert [r.url.path for r in central.requests] == ["/api/v1/runner-principals/enroll"]

    central = _Central()
    await open_signed_jwt_login(
        _settings(tmp_path, enrollment_code="c"), transport=central.transport()
    )
    record = json.loads((tmp_path / LOGIN_FILE_NAME).read_text())
    record["signing_alg"] = "HS256"
    (tmp_path / LOGIN_FILE_NAME).write_text(json.dumps(record))
    central.requests.clear()
    with pytest.raises(RunnerLoginError, match=r"signing type HS256.*only ES256"):
        await open_signed_jwt_login(_settings(tmp_path), transport=central.transport())
    assert central.requests == []
    with pytest.raises(RunnerLoginError, match=r"signing type HS256.*only ES256"):
        await open_signed_jwt_login(
            _settings(tmp_path, enrollment_code="c"), transport=central.transport()
        )
    assert central.requests == []
    assert json.loads((tmp_path / LOGIN_FILE_NAME).read_text())["signing_alg"] == "HS256"


@pytest.mark.asyncio
async def test_a_saved_login_of_another_runner_is_not_used(tmp_path: Path) -> None:
    central = _Central()
    await open_signed_jwt_login(
        _settings(tmp_path, enrollment_code="c"), transport=central.transport()
    )
    with pytest.raises(RunnerLoginError, match="is for runner:site-a"):
        await open_signed_jwt_login(
            _settings(tmp_path, runner_id="site-b"), transport=central.transport()
        )


@pytest.mark.asyncio
async def test_a_code_of_another_runner_is_reported_and_not_saved(tmp_path: Path) -> None:
    central = _Central()
    central.client_id = "runner:site-z"
    with pytest.raises(RunnerLoginError, match=r"another runner.*code is for runner:site-z"):
        await open_signed_jwt_login(
            _settings(tmp_path, enrollment_code="c"), transport=central.transport()
        )
    assert not (tmp_path / LOGIN_FILE_NAME).exists()


@pytest.mark.asyncio
async def test_the_runner_sends_its_name_and_reports_a_refused_name(tmp_path: Path) -> None:
    central = _Central()
    central.refuse_name = True
    with pytest.raises(RunnerLoginError, match="code is for another runner") as caught:
        await open_signed_jwt_login(
            _settings(tmp_path, enrollment_code="c"), transport=central.transport()
        )
    assert "Nothing changed" in str(caught.value)
    (request,) = central.requests
    assert json.loads(request.content)["runner_name"] == "site-a"
    assert not (tmp_path / LOGIN_FILE_NAME).exists()


@pytest.mark.asyncio
async def test_a_damaged_key_file_is_reported_and_kept(tmp_path: Path) -> None:
    (tmp_path / KEY_FILE_NAME).write_text("")
    (tmp_path / KEY_FILE_NAME).chmod(0o600)
    central = _Central()
    with pytest.raises(RunnerLoginError, match="move it away"):
        await open_signed_jwt_login(
            _settings(tmp_path, enrollment_code="c"), transport=central.transport()
        )
    assert central.requests == []
    assert (tmp_path / KEY_FILE_NAME).read_text() == ""


# ---------------------------------------------------------------------------
# The settings from the environment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_reads_the_code_the_key_folder_and_the_token_url_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_dir = tmp_path / "kept" / "keys"
    token_url = "https://other-login.test/realms/meho/protocol/openid-connect/token"
    monkeypatch.delenv("MEHO_RUNNER_TOKEN", raising=False)
    monkeypatch.setenv("MEHO_RUNNER_CENTRAL_URL", "https://central.test")
    monkeypatch.setenv("MEHO_RUNNER_ID", "site-a")
    monkeypatch.setenv("MEHO_RUNNER_ENROLLMENT_CODE", "code-from-env")
    monkeypatch.setenv("MEHO_RUNNER_KEY_DIR", str(key_dir))
    monkeypatch.setenv("MEHO_RUNNER_TOKEN_URL", token_url)
    central = _Central()
    get_runner_settings.cache_clear()
    try:
        settings = get_runner_settings()
    finally:
        get_runner_settings.cache_clear()
    assert "code-from-env" not in repr(settings)
    provider = await open_signed_jwt_login(settings, transport=central.transport())
    assert provider is not None
    assert await provider.get_token() == "token-1"
    enroll, token = central.requests
    assert str(enroll.url) == "https://central.test/api/v1/runner-principals/enroll"
    assert json.loads(enroll.content)["enrollment_code"] == "code-from-env"
    assert str(token.url) == token_url
    assert sorted(path.name for path in key_dir.iterdir()) == [KEY_FILE_NAME, LOGIN_FILE_NAME]
    for path in key_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_without_a_key_folder_setting_the_default_folder_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("TOKEN", "ENROLLMENT_CODE", "KEY_DIR", "TOKEN_URL"):
        monkeypatch.delenv(f"MEHO_RUNNER_{name}", raising=False)
    monkeypatch.setenv("MEHO_RUNNER_CENTRAL_URL", "https://central.test")
    monkeypatch.setenv("MEHO_RUNNER_ID", "site-a")
    get_runner_settings.cache_clear()
    try:
        settings = get_runner_settings()
    finally:
        get_runner_settings.cache_clear()
    assert settings.key_dir == DEFAULT_KEY_DIR
    assert settings.enrollment_code is None
    assert settings.token_endpoint_override is None
    assert settings.runner_token is None


async def _enrolled(tmp_path: Path) -> _Central:
    """A runner that enrolled once; the fake central has forgotten the requests."""
    central = _Central()
    await open_signed_jwt_login(
        _settings(tmp_path, enrollment_code="c"), transport=central.transport()
    )
    central.requests.clear()
    return central


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", [0o640, 0o660, 0o604, 0o644, 0o620, 0o610, 0o602, 0o601, 0o606, 0o440]
)
@pytest.mark.parametrize("with_code", [False, True])
async def test_a_key_file_with_group_or_other_rights_stops_the_runner(
    tmp_path: Path, mode: int, with_code: bool
) -> None:
    central = await _enrolled(tmp_path)
    if with_code:
        (tmp_path / LOGIN_FILE_NAME).unlink()
    (tmp_path / KEY_FILE_NAME).chmod(mode)
    settings = _settings(tmp_path, enrollment_code="c") if with_code else _settings(tmp_path)
    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises(RunnerLoginError, match="0600") as raised,
    ):
        await open_signed_jwt_login(settings, transport=central.transport())
    error = str(raised.value)
    assert str(tmp_path / KEY_FILE_NAME) in error
    assert f"has mode {mode:04o}" in error
    assert "https://evoila.github.io/meho/latest/guides/satellite-gateway/" in error
    assert central.requests == []
    assert [e for e in logs if e["log_level"] == "warning"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [0o600, 0o400])
@pytest.mark.parametrize("with_code", [False, True])
async def test_a_key_file_only_its_owner_can_use_works(
    tmp_path: Path, mode: int, with_code: bool
) -> None:
    central = await _enrolled(tmp_path)
    if with_code:
        (tmp_path / LOGIN_FILE_NAME).unlink()
    (tmp_path / KEY_FILE_NAME).chmod(mode)
    settings = _settings(tmp_path, enrollment_code="c") if with_code else _settings(tmp_path)
    provider = await open_signed_jwt_login(settings, transport=central.transport())
    assert provider is not None
    token = await provider.get_token()
    assert token == "token-1"


@pytest.mark.asyncio
async def test_a_key_mode_client_sends_the_scheduled_checks_feature(tmp_path: Path) -> None:
    central = await _enrolled(tmp_path)
    provider = await open_signed_jwt_login(_settings(tmp_path), transport=central.transport())
    assert provider is not None
    async with RunnerClient(
        central_url="https://central.test",
        runner_id="site-a",
        token_provider=provider,
        transport=central.transport(),
    ) as client:
        await client.fetch_assignment(None)
    (call,) = [r for r in central.requests if r.url.path == "/api/v1/checks/assignment"]
    assert call.headers[wire.FEATURES_HEADER] == wire.FEATURE_SCHEDULED_CHECKS
    assert call.headers["Authorization"] == "Bearer token-1"
