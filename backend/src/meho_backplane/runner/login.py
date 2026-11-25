# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The runner's own login: a key pair made at the site, short tokens.

On its first start the runner makes an EC P-256 key pair and keeps the
private key in ``MEHO_RUNNER_KEY_DIR`` (file mode ``0600``). It sends the
public key with the one-time enrollment code to central, which puts the key on
the runner's Keycloak client. After that the runner signs a short login
request (a JWT, ES256) with the private key and gets a short access token. It
asks for a new token before the old one ends, and once more after a ``401``.
Both requests go over https, or over plain http to this same machine only.
The private key never leaves the site: only the public key and the signed
login request travel, and nothing in this module logs a key or a token.

The fixed ``MEHO_RUNNER_TOKEN`` mode is unchanged and does not use this module.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import jwt
import structlog
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from meho_backplane.runner.key_id import ec_key_id
from meho_backplane.runner.settings import RunnerConfigError, RunnerSettings

__all__ = [
    "KEY_FILE_NAME",
    "LOGIN_FILE_NAME",
    "RunnerLoginError",
    "SignedJwtTokenProvider",
    "check_https",
    "enroll_with_code",
    "load_or_create_private_key",
    "open_signed_jwt_login",
    "public_jwk",
    "uses_proxy_settings",
]

#: The private key, PEM, mode ``0600``.
KEY_FILE_NAME: Final[str] = "runner-key.pem"
#: The login record written after enrollment: client id, issuer, token endpoint,
#: key id. It holds no secret.
LOGIN_FILE_NAME: Final[str] = "runner-login.json"

_ENROLL_PATH: Final[str] = "/api/v1/runner-principals/enroll"
_HTTP_TIMEOUT_SECONDS: Final[float] = 15.0
#: How long one signed login request is valid.
_ASSERTION_LIFETIME_SECONDS: Final[int] = 60
_SIGNING_ALG: Final[str] = "ES256"
#: A token is renewed when less than this share of its life is left ...
_RENEW_LIFETIME_SHARE: Final[float] = 0.25
#: ... or fewer seconds than this.
_RENEW_MIN_SECONDS: Final[float] = 30.0
_ASSERTION_TYPE: Final[str] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

#: What a person does when the saved login or its key is lost.
_RECOVERY: Final[str] = (
    "Put the right files back, or revoke this runner, register a new one under a new name "
    "(meho runner-principal register), and start with its MEHO_RUNNER_ID and "
    "MEHO_RUNNER_ENROLLMENT_CODE. Give the new runner an empty MEHO_RUNNER_SPOOL_DIR, "
    "and keep the old one: its results carry the old name and central refuses them "
    "under the new one"
)
#: The docs page with the rule for the key file's mode.
_KEY_FILE_DOCS: Final[str] = "https://evoila.github.io/meho/latest/guides/satellite-gateway/"
#: What a person does when the code belongs to a runner with another name.
_OTHER_RUNNER: Final[str] = (
    "the enrollment code is for another runner; set MEHO_RUNNER_ID to the name that was "
    "registered, or use the code of this runner. Nothing changed, and the code still works "
    "for its own runner"
)

_log = structlog.get_logger(__name__)


class RunnerLoginError(RuntimeError):
    """The runner could not enroll or could not get a token.

    The message never holds a key, a code or a token.
    """


class _SavedLoginError(RunnerLoginError):
    """The saved login on disk cannot be used."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"{reason}. {_RECOVERY}")


def check_https(what: str, url: str) -> None:
    """Refuse *url* unless it is https, or plain http to this same machine.

    *what* names the setting for the error text. The address itself is not in
    the text.
    """
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise RunnerConfigError(f"{what} is not a valid address") from exc
    if parsed.scheme == "https" and parsed.host:
        return
    if parsed.scheme == "http" and _is_this_machine(parsed.host):
        return
    raise RunnerConfigError(
        f"{what} must start with https:// (http:// works only for an address on this machine)"
    )


def _is_this_machine(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def uses_proxy_settings(url: str) -> bool:
    """Plain http goes straight to this machine, never through a proxy from the environment."""
    return httpx.URL(url).scheme == "https"


def _read_key_file(path: Path) -> bytes:
    """The bytes of the key file, after a check of its mode on the open file.

    Any right for the group or for other users stops the runner.
    """
    with path.open("rb") as handle:
        mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
        if mode & 0o077:
            raise RunnerLoginError(
                f"the runner key {path} has mode {mode:04o}, so its group or other users have "
                f"rights to it. Set its mode to 0600 (chmod 600 {path}) and start the runner "
                f"again. See {_KEY_FILE_DOCS}"
            )
        return handle.read()


def _read_private_key(path: Path) -> ec.EllipticCurvePrivateKey | None:
    """The private key in *path*, or ``None`` when there is no such file."""
    try:
        key = serialization.load_pem_private_key(_read_key_file(path), password=None)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise RunnerLoginError(
            f"cannot read the runner key {path}: {type(exc).__name__}. "
            f"When the file is damaged, move it away. {_RECOVERY}"
        ) from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise RunnerLoginError(
            f"the key file {path} is not an EC P-256 key. Move it away. {_RECOVERY}"
        )
    return key


def load_or_create_private_key(key_dir: str) -> ec.EllipticCurvePrivateKey:
    """Return the runner's private key; make and keep a new one on first start."""
    folder = Path(key_dir)
    path = folder / KEY_FILE_NAME
    key = _read_private_key(path)
    if key is not None:
        return key
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
    except OSError as exc:
        raise RunnerLoginError(
            f"cannot write the runner key in {key_dir}: {type(exc).__name__}"
        ) from exc
    return key


def _client_id(runner_id: str) -> str:
    """The login server's name for the runner that was registered as *runner_id*."""
    return f"runner:{runner_id}"


def _b64url(number: int, length: int) -> str:
    raw = number.to_bytes(length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def public_jwk(key: ec.EllipticCurvePrivateKey) -> dict[str, str]:
    """The public half of *key* as a JWK. Holds no private member."""
    numbers = key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(numbers.x, 32),
        "y": _b64url(numbers.y, 32),
    }


@dataclass(frozen=True)
class _LoginRecord:
    client_id: str
    issuer: str
    token_endpoint: str
    key_id: str
    signing_alg: str

    def save(self, path: Path) -> None:
        """Write the record in one step, so a stop never leaves half a file."""
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(self.__dict__))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @classmethod
    def from_file(cls, path: Path) -> _LoginRecord:
        try:
            data = json.loads(path.read_text())
            record = cls(**{name: str(data[name]) for name in cls.__dataclass_fields__})
        except OSError as exc:
            raise RunnerLoginError(
                f"cannot read the login record {path}: {type(exc).__name__}"
            ) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise _SavedLoginError(f"the login record {path} is damaged") from exc
        if record.signing_alg != _SIGNING_ALG:
            raise _SavedLoginError(
                f"the login record {path} has signing type {record.signing_alg}; "
                f"the runner reads only {_SIGNING_ALG}"
            )
        return record


async def enroll_with_code(
    *,
    central_url: str,
    code: str,
    runner_id: str,
    key: ec.EllipticCurvePrivateKey,
    key_dir: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> _LoginRecord:
    """Send the public key and the one-time code to central; keep the answer.

    Safe to run again with the same key: central gives the same answer, so a
    lost answer is asked for again at the next start.
    """
    check_https("MEHO_RUNNER_CENTRAL_URL", central_url)
    try:
        async with httpx.AsyncClient(
            base_url=central_url.rstrip("/"),
            timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS),
            transport=transport,
            trust_env=uses_proxy_settings(central_url),
        ) as client:
            resp = await client.post(
                _ENROLL_PATH,
                json={
                    "enrollment_code": code,
                    "public_key": public_jwk(key),
                    "runner_name": runner_id,
                },
            )
    except httpx.HTTPError as exc:
        raise RunnerLoginError(f"enrollment failed: {type(exc).__name__}") from exc
    if resp.status_code == httpx.codes.CONFLICT:
        raise RunnerLoginError(_OTHER_RUNNER)
    if resp.status_code != httpx.codes.OK:
        hint = f". {_RECOVERY}" if resp.status_code == httpx.codes.FORBIDDEN else ""
        raise RunnerLoginError(f"enrollment was refused: HTTP {resp.status_code}{hint}")
    try:
        body = resp.json()
        record = _LoginRecord(
            client_id=str(body["client_id"]),
            issuer=str(body["issuer"]),
            token_endpoint=str(body["token_endpoint"]),
            key_id=str(body["key_id"]),
            signing_alg=str(body["signing_alg"]),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise RunnerLoginError("enrollment answer is not valid") from exc
    if record.signing_alg != _SIGNING_ALG:
        raise RunnerLoginError(
            f"enrollment answer has signing type {record.signing_alg}; "
            f"the runner reads only {_SIGNING_ALG}"
        )
    if record.client_id != _client_id(runner_id):
        raise RunnerLoginError(f"{_OTHER_RUNNER} (the code is for {record.client_id})")
    if record.key_id != ec_key_id(public_jwk(key)):
        raise RunnerLoginError("enrollment answer is for another key")
    try:
        record.save(Path(key_dir) / LOGIN_FILE_NAME)
    except OSError as exc:
        raise RunnerLoginError(f"cannot write the login record: {type(exc).__name__}") from exc
    _log.info("runner_key_enrolled", client_id=record.client_id, key_id=record.key_id)
    return record


class SignedJwtTokenProvider:
    """Gives the runner a current access token, signed in for with its own key."""

    def __init__(
        self,
        *,
        key: ec.EllipticCurvePrivateKey,
        record: _LoginRecord,
        token_endpoint: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = key
        self._record = record
        self._token_endpoint = token_endpoint or record.token_endpoint
        self._transport = transport
        self._token: str | None = None
        self._renew_at = 0.0
        self._lock = asyncio.Lock()

    def _assertion(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self._record.client_id,
            "sub": self._record.client_id,
            "aud": self._record.issuer,
            "jti": uuid.uuid4().hex,
            "iat": now,
            "exp": now + _ASSERTION_LIFETIME_SECONDS,
        }
        return jwt.encode(
            claims,
            self._key,
            algorithm=self._record.signing_alg,
            headers={"kid": self._record.key_id},
        )

    async def _fetch(self) -> None:
        form = {
            "grant_type": "client_credentials",
            "client_id": self._record.client_id,
            "client_assertion_type": _ASSERTION_TYPE,
            "client_assertion": self._assertion(),
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS),
                transport=self._transport,
                trust_env=uses_proxy_settings(self._token_endpoint),
            ) as client:
                resp = await client.post(self._token_endpoint, data=form)
        except httpx.HTTPError as exc:
            raise RunnerLoginError(f"login failed: {type(exc).__name__}") from exc
        if resp.status_code != httpx.codes.OK:
            raise RunnerLoginError(f"login was refused: HTTP {resp.status_code}")
        try:
            body: dict[str, Any] = resp.json()
            token = str(body["access_token"])
            lifetime = float(body.get("expires_in", 60))
        except (ValueError, KeyError, TypeError) as exc:
            raise RunnerLoginError("login answer is not valid") from exc
        margin = max(_RENEW_MIN_SECONDS, lifetime * _RENEW_LIFETIME_SHARE)
        self._token = token
        self._renew_at = time.monotonic() + max(lifetime - margin, 0.0)
        _log.info("runner_token_renewed", expires_in=int(lifetime))

    async def get_token(self) -> str:
        """The current token; a new one is fetched when the old one is near its end."""
        async with self._lock:
            if self._token is None or time.monotonic() >= self._renew_at:
                await self._fetch()
            assert self._token is not None
            return self._token

    async def refresh(self) -> str:
        """Fetch a new token now (after a ``401``)."""
        async with self._lock:
            await self._fetch()
            assert self._token is not None
            return self._token


def _load_saved_login(
    settings: RunnerSettings,
) -> tuple[ec.EllipticCurvePrivateKey, _LoginRecord]:
    """The saved login and its private key; check that the two belong together."""
    folder = Path(settings.key_dir)
    record = _LoginRecord.from_file(folder / LOGIN_FILE_NAME)
    if record.client_id != _client_id(settings.runner_id):
        raise _SavedLoginError(
            f"the saved login in {settings.key_dir} is for {record.client_id}, "
            f"and MEHO_RUNNER_ID is {settings.runner_id}"
        )
    key = _read_private_key(folder / KEY_FILE_NAME)
    if key is None:
        raise _SavedLoginError(f"the runner key {folder / KEY_FILE_NAME} is missing")
    if ec_key_id(public_jwk(key)) != record.key_id:
        raise _SavedLoginError(
            f"the runner key {folder / KEY_FILE_NAME} does not match the saved login"
        )
    return key, record


async def _enroll(
    settings: RunnerSettings, transport: httpx.AsyncBaseTransport | None
) -> tuple[ec.EllipticCurvePrivateKey, _LoginRecord]:
    """Enroll the key on disk (a new one when there is none) with the code."""
    assert settings.enrollment_code is not None
    key = load_or_create_private_key(settings.key_dir)
    record = await enroll_with_code(
        central_url=settings.central_url,
        code=settings.enrollment_code,
        runner_id=settings.runner_id,
        key=key,
        key_dir=settings.key_dir,
        transport=transport,
    )
    return key, record


async def _saved_or_new_login(
    settings: RunnerSettings, transport: httpx.AsyncBaseTransport | None
) -> tuple[ec.EllipticCurvePrivateKey, _LoginRecord]:
    """The saved login; when it cannot be used, a new one with the code that is set."""
    try:
        return _load_saved_login(settings)
    except _SavedLoginError as problem:
        if settings.enrollment_code is None:
            raise
        _log.warning("runner_saved_login_not_usable", reason=problem.reason)
        try:
            return await _enroll(settings, transport)
        except RunnerLoginError as exc:
            raise RunnerLoginError(f"{problem.reason}; {exc}") from exc


async def open_signed_jwt_login(
    settings: RunnerSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SignedJwtTokenProvider | None:
    """Set up the key login when the runner is in that mode, else return ``None``.

    The key mode is on when a login record is on disk, or when an enrollment
    code is set (the first start). With neither, the fixed token mode runs and
    this returns ``None``. With no token, no code and no record, the runner
    cannot log in: that is a configuration error.

    A saved login is used only with the private key it was made for, and only
    for the runner named in the settings. When they do not fit, the enrollment
    code makes a new login, or the error says how to recover.
    """
    record_path = Path(settings.key_dir) / LOGIN_FILE_NAME
    has_record = record_path.exists()
    if not has_record and settings.enrollment_code is None:
        if settings.runner_token is None:
            raise RunnerConfigError(
                "set MEHO_RUNNER_TOKEN, or MEHO_RUNNER_ENROLLMENT_CODE for the first start"
            )
        return None
    check_https("MEHO_RUNNER_CENTRAL_URL", settings.central_url)
    if settings.token_endpoint_override is not None:
        check_https("MEHO_RUNNER_TOKEN_URL", settings.token_endpoint_override)
    if has_record:
        key, record = await _saved_or_new_login(settings, transport)
    else:
        key, record = await _enroll(settings, transport)
    if settings.token_endpoint_override is None:
        check_https(
            "the token address from central (set MEHO_RUNNER_TOKEN_URL to use another one)",
            record.token_endpoint,
        )
    return SignedJwtTokenProvider(
        key=key,
        record=record,
        token_endpoint=settings.token_endpoint_override,
        transport=transport,
    )
