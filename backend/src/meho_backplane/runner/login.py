# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The runner's own login: a key pair made at the site, short tokens.

On its first start the runner makes an EC P-256 key pair and keeps the
private key in ``MEHO_RUNNER_KEY_DIR`` (file mode ``0600``). It sends the
public key with the one-time enrollment code to central, which puts the key on
the runner's Keycloak client. After that the runner signs a short login
request (a JWT, ES256) with the private key and gets a short access token. It
asks for a new token before the old one ends, and once more after a ``401``.
The private key never leaves the site: only the public key and the signed
login request travel, and nothing in this module logs a key or a token.

The fixed ``MEHO_RUNNER_TOKEN`` mode is unchanged and does not use this module.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import jwt
import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from meho_backplane.runner.settings import RunnerConfigError, RunnerSettings

__all__ = [
    "KEY_FILE_NAME",
    "LOGIN_FILE_NAME",
    "RunnerLoginError",
    "SignedJwtTokenProvider",
    "enroll_with_code",
    "load_or_create_private_key",
    "open_signed_jwt_login",
    "public_jwk",
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
#: A token is renewed when less than this share of its life is left ...
_RENEW_LIFETIME_SHARE: Final[float] = 0.25
#: ... or fewer seconds than this.
_RENEW_MIN_SECONDS: Final[float] = 30.0
_ASSERTION_TYPE: Final[str] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

_log = structlog.get_logger(__name__)


class RunnerLoginError(RuntimeError):
    """The runner could not enroll or could not get a token.

    The message never holds a key, a code or a token.
    """


def load_or_create_private_key(key_dir: str) -> ec.EllipticCurvePrivateKey:
    """Return the runner's private key; make and keep a new one on first start."""
    folder = Path(key_dir)
    path = folder / KEY_FILE_NAME
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey):
                raise RunnerLoginError(f"the key file {path} is not an EC key")
            return key
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
        return key
    except (OSError, ValueError) as exc:
        raise RunnerLoginError(
            f"cannot read or write the runner key in {key_dir}: {type(exc).__name__}"
        ) from exc


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

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_file(cls, path: Path) -> _LoginRecord:
        try:
            data = json.loads(path.read_text())
            return cls(**{name: str(data[name]) for name in cls.__dataclass_fields__})
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RunnerLoginError(f"the login record {path} is not readable") from exc


async def enroll_with_code(
    *,
    central_url: str,
    code: str,
    key: ec.EllipticCurvePrivateKey,
    key_dir: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Send the public key and the one-time code to central; keep the answer."""
    try:
        async with httpx.AsyncClient(
            base_url=central_url.rstrip("/"),
            timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS),
            transport=transport,
        ) as client:
            resp = await client.post(
                _ENROLL_PATH,
                json={"enrollment_code": code, "public_key": public_jwk(key)},
            )
    except httpx.HTTPError as exc:
        raise RunnerLoginError(f"enrollment failed: {type(exc).__name__}") from exc
    if resp.status_code != httpx.codes.OK:
        raise RunnerLoginError(f"enrollment was refused: HTTP {resp.status_code}")
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
    try:
        (Path(key_dir) / LOGIN_FILE_NAME).write_text(record.to_json())
    except OSError as exc:
        raise RunnerLoginError(f"cannot write the login record: {type(exc).__name__}") from exc
    _log.info("runner_key_enrolled", client_id=record.client_id, key_id=record.key_id)


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
                timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS), transport=self._transport
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
    """
    record_path = Path(settings.key_dir) / LOGIN_FILE_NAME
    has_record = record_path.exists()
    if not has_record and settings.enrollment_code is None:
        if settings.runner_token is None:
            raise RunnerConfigError(
                "set MEHO_RUNNER_TOKEN, or MEHO_RUNNER_ENROLLMENT_CODE for the first start"
            )
        return None
    key = load_or_create_private_key(settings.key_dir)
    if not has_record:
        assert settings.enrollment_code is not None
        await enroll_with_code(
            central_url=settings.central_url,
            code=settings.enrollment_code,
            key=key,
            key_dir=settings.key_dir,
            transport=transport,
        )
    record = _LoginRecord.from_file(record_path)
    return SignedJwtTokenProvider(
        key=key,
        record=record,
        token_endpoint=settings.token_endpoint_override,
        transport=transport,
    )
