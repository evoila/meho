# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the certificate login to Microsoft Entra ID (client assertion).

Every key and certificate is made at run time. The login service is a respx
stand-in that checks the signature of the proof like Microsoft does.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from meho_backplane.connectors._shared.microsoft_login import (
    CLIENT_ASSERTION_TYPE,
    LOGIN_HOSTS,
    CertificateCredentials,
    CertificatePairError,
    ClientSecretCredentials,
    MicrosoftLoginError,
    build_client_assertion,
    fetch_client_credentials_token,
)
from meho_backplane.connectors._shared.vcf_auth import ConnectorAuthError
from meho_backplane.connectors.entra.connector import EntraConnector
from meho_backplane.connectors.entra.session import EntraSecretShapeError, parse_credentials
from tests._azure_fixtures import CLIENT_A, SECRET_A, TENANT_A, fake_jwt, operator, token_url
from tests._entra_fixtures import ENTRA_SECRET_REF, EntraTargetStub
from tests.conftest import MehoLogs

_TOKEN_URL = token_url()
_SCOPE = "https://graph.microsoft.com/.default"


class _Pair:
    """A key and a certificate made now, as PEM text."""

    def __init__(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "meho-test")])
        now = dt.datetime.now(dt.UTC)
        self.certificate_obj = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30))
            .sign(key, hashes.SHA256())
        )
        self.private_key = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        self.certificate = self.certificate_obj.public_bytes(serialization.Encoding.PEM).decode()

    def credentials(self) -> CertificateCredentials:
        return CertificateCredentials(TENANT_A, CLIENT_A, self.private_key, self.certificate)


@pytest.fixture(scope="module")
def pair() -> _Pair:
    return _Pair()


@pytest.fixture(scope="module")
def other_pair() -> _Pair:
    return _Pair()


def _thumbprint(pair: _Pair) -> str:
    digest = pair.certificate_obj.fingerprint(hashes.SHA256())
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_assertion_has_the_header_and_claims_microsoft_asks_for(pair: _Pair) -> None:
    token = build_client_assertion(pair.credentials(), audience=_TOKEN_URL, now=1_700_000_000)

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "PS256"
    assert header["typ"] == "JWT"
    assert header["x5t#S256"] == _thumbprint(pair)
    claims = jwt.decode(
        token,
        pair.certificate_obj.public_key(),
        algorithms=["PS256"],
        audience=_TOKEN_URL,
        options={"verify_exp": False, "verify_nbf": False, "verify_iat": False},
    )
    assert claims["aud"] == _TOKEN_URL
    assert claims["iss"] == CLIENT_A
    assert claims["sub"] == CLIENT_A
    assert claims["nbf"] == claims["iat"] == 1_700_000_000
    assert 0 < claims["exp"] - claims["nbf"] <= 600
    assert claims["jti"]


def test_each_assertion_has_its_own_jti(pair: _Pair) -> None:
    first = build_client_assertion(pair.credentials(), audience=_TOKEN_URL)
    second = build_client_assertion(pair.credentials(), audience=_TOKEN_URL)
    options: Any = {"verify_signature": False}
    assert jwt.decode(first, options=options)["jti"] != jwt.decode(second, options=options)["jti"]


@pytest.mark.asyncio
async def test_token_request_holds_the_assertion_and_no_client_secret(pair: _Pair) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_TOKEN_URL).respond(
            200, json={"expires_in": 3599, "access_token": fake_jwt({"appid": CLIENT_A})}
        )
        await fetch_client_credentials_token(
            pair.credentials(),
            login_host=LOGIN_HOSTS["AzureCloud"],
            scope=_SCOPE,
            target_name="entra-a",
            secret_ref="tenants/example/entra-login-a",
        )

    form = parse_qs(route.calls[0].request.content.decode())
    assert "client_secret" not in form
    assert form["client_assertion_type"] == [CLIENT_ASSERTION_TYPE]
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == [CLIENT_A]
    assert form["scope"] == [_SCOPE]
    assert jwt.get_unverified_header(form["client_assertion"][0])["alg"] == "PS256"


@pytest.mark.asyncio
async def test_client_secret_request_is_unchanged() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_TOKEN_URL).respond(
            200, json={"expires_in": 3599, "access_token": fake_jwt({"appid": CLIENT_A})}
        )
        await fetch_client_credentials_token(
            ClientSecretCredentials(TENANT_A, CLIENT_A, SECRET_A),
            login_host=LOGIN_HOSTS["AzureCloud"],
            scope=_SCOPE,
            target_name="entra-a",
            secret_ref="tenants/example/entra-login-a",
        )

    assert parse_qs(route.calls[0].request.content.decode()) == {
        "grant_type": ["client_credentials"],
        "client_id": [CLIENT_A],
        "client_secret": [SECRET_A],
        "scope": [_SCOPE],
    }


def _stand_in(public_certificate: x509.Certificate) -> Any:
    """A token address that checks the proof's signature like Microsoft does."""

    def answer(request: Any) -> Any:
        form = parse_qs(request.content.decode())
        try:
            jwt.decode(
                form["client_assertion"][0],
                public_certificate.public_key(),
                algorithms=["PS256"],
                audience=_TOKEN_URL,
            )
        except jwt.InvalidTokenError:
            return httpx.Response(401, json={"error": "invalid_client", "error_codes": [700027]})
        return httpx.Response(
            200, json={"expires_in": 3599, "access_token": fake_jwt({"appid": CLIENT_A})}
        )

    return answer


@pytest.mark.asyncio
async def test_stand_in_accepts_the_proof_made_with_the_right_key(pair: _Pair) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(_TOKEN_URL).mock(side_effect=_stand_in(pair.certificate_obj))
        token = await fetch_client_credentials_token(
            pair.credentials(),
            login_host=LOGIN_HOSTS["AzureCloud"],
            scope=_SCOPE,
            target_name="entra-a",
            secret_ref="tenants/example/entra-login-a",
        )
    assert token.expires_in == 3599.0


@pytest.mark.asyncio
async def test_stand_in_refuses_a_proof_that_the_uploaded_certificate_cannot_check(
    pair: _Pair, other_pair: _Pair
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(_TOKEN_URL).mock(side_effect=_stand_in(other_pair.certificate_obj))
        with pytest.raises(ConnectorAuthError) as raised:
            await fetch_client_credentials_token(
                pair.credentials(),
                login_host=LOGIN_HOSTS["AzureCloud"],
                scope=_SCOPE,
                target_name="entra-a",
                secret_ref="tenants/example/entra-login-a",
            )
    text = str(raised.value)
    assert "with the certificate" in text
    assert "uploaded to the app registration" in text
    assert "has not run out" in text
    assert "belongs to it" in text
    assert "client secret" not in text
    assert pair.private_key not in text


@pytest.mark.asyncio
async def test_a_refused_client_secret_login_names_the_client_secret() -> None:
    with respx.mock() as mock:
        mock.post(_TOKEN_URL).respond(401, json={"error": "invalid_client"})
        with pytest.raises(ConnectorAuthError) as raised:
            await fetch_client_credentials_token(
                ClientSecretCredentials(TENANT_A, CLIENT_A, SECRET_A),
                login_host=LOGIN_HOSTS["AzureCloud"],
                scope=_SCOPE,
                target_name="entra-a",
                secret_ref="tenants/example/entra-login-a",
            )
    assert "with the client secret" in str(raised.value)
    assert SECRET_A not in str(raised.value)


def test_a_key_that_does_not_fit_the_certificate_is_refused_without_the_key(
    pair: _Pair, other_pair: _Pair
) -> None:
    credentials = CertificateCredentials(
        TENANT_A, CLIENT_A, other_pair.private_key, pair.certificate
    )
    with pytest.raises(CertificatePairError) as raised:
        build_client_assertion(credentials, audience=_TOKEN_URL)
    assert "does not belong" in str(raised.value)
    assert other_pair.private_key not in str(raised.value)


def test_an_unreadable_key_is_refused_without_the_key(pair: _Pair) -> None:
    marker = "MARKER-NOT-A-KEY-9f3a"
    credentials = CertificateCredentials(TENANT_A, CLIENT_A, marker, pair.certificate)
    with pytest.raises(CertificatePairError) as raised:
        build_client_assertion(credentials, audience=_TOKEN_URL)
    assert marker not in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.asyncio
async def test_a_bad_pair_at_the_token_request_names_the_target_and_the_secret(
    pair: _Pair, other_pair: _Pair
) -> None:
    credentials = CertificateCredentials(
        TENANT_A, CLIENT_A, other_pair.private_key, pair.certificate
    )
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_TOKEN_URL)
        with pytest.raises(MicrosoftLoginError) as raised:
            await fetch_client_credentials_token(
                credentials,
                login_host=LOGIN_HOSTS["AzureCloud"],
                scope=_SCOPE,
                target_name="entra-a",
                secret_ref="tenants/example/entra-login-a",
            )
    text = str(raised.value)
    assert "'entra-a'" in text
    assert "'tenants/example/entra-login-a'" in text
    assert "does not belong" in text
    assert other_pair.private_key not in text
    assert not route.called


def test_the_private_key_is_in_no_repr_and_no_log_line(
    pair: _Pair, caplog: pytest.LogCaptureFixture, meho_logs: MehoLogs
) -> None:
    credentials = pair.credentials()
    body = "".join(pair.private_key.splitlines()[1:-1])
    with caplog.at_level(logging.DEBUG):
        build_client_assertion(credentials, audience=_TOKEN_URL)
    assert body not in repr(credentials)
    assert body not in str(credentials)
    assert body not in meho_logs.text()


def test_a_certificate_field_that_also_holds_the_key_is_in_no_repr(pair: _Pair) -> None:
    combined = pair.certificate + pair.private_key
    credentials = CertificateCredentials(TENANT_A, CLIENT_A, pair.private_key, combined)
    build_client_assertion(credentials, audience=_TOKEN_URL)
    key_line = pair.private_key.splitlines()[1]
    assert key_line not in repr(credentials)
    assert key_line not in str(credentials)


_BASE = {"tenant_id": TENANT_A, "client_id": CLIENT_A}


def _parse(extra: dict[str, object]) -> Any:
    return parse_credentials(
        {**_BASE, **extra}, target_name="entra-a", secret_ref="tenants/example/entra-login-a"
    )


def test_the_secret_with_a_certificate_gives_a_certificate_login(pair: _Pair) -> None:
    credentials = _parse(
        {"private_key": pair.private_key + "\n", "certificate": pair.certificate + "\n"}
    )
    assert isinstance(credentials, CertificateCredentials)
    assert credentials.private_key == pair.private_key.strip()
    assert credentials.client_id == CLIENT_A
    assert jwt.get_unverified_header(build_client_assertion(credentials, audience=_TOKEN_URL))


def test_the_secret_with_a_client_secret_still_gives_a_client_secret_login() -> None:
    credentials = _parse({"client_secret": SECRET_A})
    assert isinstance(credentials, ClientSecretCredentials)


def test_both_ways_in_one_secret_are_refused_without_a_value(pair: _Pair) -> None:
    with pytest.raises(EntraSecretShapeError) as raised:
        _parse(
            {
                "client_secret": SECRET_A,
                "private_key": pair.private_key,
                "certificate": pair.certificate,
            }
        )
    text = str(raised.value)
    assert "both" in text
    assert "private_key and certificate" in text
    assert SECRET_A not in text
    assert pair.private_key not in text


def test_neither_way_is_refused() -> None:
    with pytest.raises(EntraSecretShapeError) as raised:
        _parse({})
    assert "client_secret, or private_key and certificate" in str(raised.value)


def test_a_certificate_without_its_key_is_refused(pair: _Pair) -> None:
    with pytest.raises(EntraSecretShapeError) as raised:
        _parse({"certificate": pair.certificate})
    assert "'private_key'" in str(raised.value)
    assert pair.certificate not in str(raised.value)


def _encrypted_key(pair: _Pair) -> str:
    key = serialization.load_pem_private_key(pair.private_key.encode(), password=None)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"made-up-password"),
    ).decode()


def _ec_key() -> str:
    return (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )


@pytest.mark.parametrize(
    ("kind", "words"),
    [
        ("other-pair", "does not belong to the certificate"),
        ("unreadable", "not a readable PEM"),
        ("password", "not a readable PEM"),
        ("not-rsa", "must be RSA"),
    ],
)
def test_a_bad_pair_is_refused_when_the_secret_is_read(
    pair: _Pair, other_pair: _Pair, kind: str, words: str
) -> None:
    key = {
        "other-pair": other_pair.private_key,
        "unreadable": "MARKER-NOT-A-KEY-9f3a",
        "password": _encrypted_key(pair),
        "not-rsa": _ec_key(),
    }[kind]
    with pytest.raises(EntraSecretShapeError) as raised:
        _parse({"private_key": key, "certificate": pair.certificate})
    text = str(raised.value)
    assert words in text
    assert "'entra-a'" in text
    assert "'tenants/example/entra-login-a'" in text
    assert key.strip() not in text
    assert raised.value.__cause__ is None


class _SecretStore:
    """A secret loader that parses a secret a person can change between calls."""

    def __init__(self, secret: dict[str, object]) -> None:
        self.secret = secret
        self.calls = 0

    async def __call__(self, target: Any, operator: Any) -> Any:
        self.calls += 1
        return parse_credentials(self.secret, target_name=target.name, secret_ref=target.secret_ref)


@pytest.mark.asyncio
async def test_a_corrected_key_is_read_on_the_next_call_without_a_restart(
    pair: _Pair, other_pair: _Pair
) -> None:
    store = _SecretStore(
        {**_BASE, "private_key": other_pair.private_key, "certificate": pair.certificate}
    )
    connector = EntraConnector(credentials_loader=store)
    target = EntraTargetStub()
    assert target.secret_ref == ENTRA_SECRET_REF

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_TOKEN_URL).mock(side_effect=_stand_in(pair.certificate_obj))
        with pytest.raises(EntraSecretShapeError):
            await connector.auth_headers(target, operator())
        assert not route.called

        store.secret = {**_BASE, "private_key": pair.private_key, "certificate": pair.certificate}
        headers = await connector.auth_headers(target, operator())

    assert store.calls == 2
    assert route.call_count == 1
    assert headers["Authorization"].startswith("Bearer ")
