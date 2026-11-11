# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""#6213 — the console's passkey touch level, in a real Keycloak.

The chart's realm file is imported into a real Keycloak (the same start as
``test_core_keycloak_realm_import.py``). A person with a password and a
passkey logs in to the console client ``meho-web`` in the browser flow:
plain HTTP requests and a small software passkey (an EC P-256 key) that
answers Keycloak's WebAuthn page the way a browser's passkey does.

What is proven:

* a plain console login asks for the password only and gets ``acr`` "1";
* a second request with ``acr_values=meho-touch`` and a ``nonce`` asks for
  the passkey only (the login cookie covers level 1). The ID token has
  ``acr=meho-touch``, ``hwk`` in ``amr``, the same ``nonce`` and a fresh
  ``auth_time``;
* the next request for the touch asks for the passkey again (maximum age 0);
* ``max_age=0`` without ``acr_values`` asks for the password, not the passkey;
* a plain login right after a touch does not have the touch level in
  ``acr``, so MEHO must never accept a touch on ``amr`` alone;
* without a maximum age on the passkey step's ``amr`` setting, ``amr`` is
  empty.

Skips when Docker is missing or the container cannot start.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from tests.integration.test_core_keycloak_realm_import import (
    _PUBLIC_URL,
    _bootstrap_headers,
    _Keycloak,
    _run_keycloak,
    _token_claims,
)

_REDIRECT = f"{_PUBLIC_URL}/ui/auth/callback"
_TOUCH = "meho-touch"
_TOUCH_FLOW = "meho approval touch"


@pytest.fixture(scope="module")
def keycloak() -> Iterator[_Keycloak]:
    with _run_keycloak() as kc:
        yield kc


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class _Passkey:
    """A software passkey: one EC P-256 key, as a platform passkey holds it."""

    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    credential_id: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    counter: int = 0

    def _cose_key(self) -> bytes:
        """The public key as a COSE_Key map (EC2, ES256), CBOR-encoded by hand."""
        numbers = self.key.public_key().public_numbers()
        x = numbers.x.to_bytes(32, "big")
        y = numbers.y.to_bytes(32, "big")
        # map(5): 1:2 (kty EC2), 3:-7 (alg ES256), -1:1 (crv P-256), -2:x, -3:y
        return b"\xa5\x01\x02\x03\x26\x20\x01" + b"\x21\x58\x20" + x + b"\x22\x58\x20" + y

    def credential(self) -> dict[str, Any]:
        """The credential as Keycloak's admin API takes it for a new person."""
        data = {
            "aaguid": "00000000-0000-0000-0000-000000000000",
            "credentialId": base64.b64encode(self.credential_id).decode(),
            "counter": 0,
            "attestationStatement": None,
            "credentialPublicKey": _b64url(self._cose_key()),
            "attestationStatementFormat": "none",
            "transports": ["internal"],
        }
        return {
            "type": "webauthn-passwordless",
            "userLabel": "test passkey",
            "credentialData": json.dumps(data),
            "secretData": "{}",
        }

    def answer(self, *, challenge: str, rp_id: str, origin: str, user_id: str) -> dict[str, str]:
        """Sign Keycloak's challenge with user presence and user verification."""
        self.counter += 1
        client_data = json.dumps(
            {"type": "webauthn.get", "challenge": challenge, "origin": origin},
            separators=(",", ":"),
        ).encode()
        flags = 0x01 | 0x04  # user present, user verified
        authenticator_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([flags])
            + self.counter.to_bytes(4, "big")
        )
        signature = self.key.sign(
            authenticator_data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
        return {
            "clientDataJSON": _b64url(client_data),
            "authenticatorData": _b64url(authenticator_data),
            "signature": _b64url(signature),
            "credentialId": _b64url(self.credential_id),
            "userHandle": _b64url(user_id.encode()),
            "error": "",
        }


@dataclass
class _Person:
    username: str
    password: str
    user_id: str
    passkey: _Passkey


def _add_person_with_passkey(kc: _Keycloak, username: str) -> _Person:
    headers = _bootstrap_headers(kc)
    admin = f"{kc.base_url}/admin/realms/meho"
    password = secrets.token_urlsafe(18)
    passkey = _Passkey()
    created = httpx.post(
        f"{admin}/users",
        headers=headers,
        json={
            "username": username,
            "enabled": True,
            "email": f"{username}@example.com",
            "emailVerified": True,
            "firstName": "Test",
            "lastName": username,
            "credentials": [
                {"type": "password", "value": password, "temporary": False},
                passkey.credential(),
            ],
        },
    )
    assert created.status_code == 201, created.text
    user_id = httpx.get(
        f"{admin}/users", headers=headers, params={"username": username, "exact": "true"}
    ).json()[0]["id"]
    types = {
        c["type"] for c in httpx.get(f"{admin}/users/{user_id}/credentials", headers=headers).json()
    }
    assert types == {"password", "webauthn-passwordless"}, types
    return _Person(username, password, user_id, passkey)


@dataclass
class _Browser:
    """One browser of the console: it keeps Keycloak's login cookie.

    It keeps its own cookies by name and path: Keycloak marks its cookies
    ``Secure`` when the host is ``localhost``, and httpx would then not send
    them over plain HTTP.
    """

    kc: _Keycloak
    client: httpx.Client = field(init=False)
    cookies: dict[tuple[str, str], str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.client = httpx.Client(follow_redirects=False, timeout=30.0)

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        path = urlsplit(url).path
        cookie = "; ".join(f"{n}={v}" for (n, p), v in self.cookies.items() if path.startswith(p))
        headers = {"Cookie": cookie} if cookie else {}
        response = self.client.request(method, url, headers=headers, **kwargs)
        self.client.cookies.clear()
        for raw in response.headers.get_list("set-cookie"):
            parsed: SimpleCookie = SimpleCookie()
            parsed.load(raw)
            for name, morsel in parsed.items():
                key = (name, morsel["path"] or "/")
                gone = morsel["max-age"] == "0" or "1970" in morsel["expires"]
                if gone or not morsel.value:
                    self.cookies.pop(key, None)
                else:
                    self.cookies[key] = morsel.value
        return response

    def start(self, **extra: str) -> tuple[httpx.Response, dict[str, str]]:
        """Open the console's authorization request; return the page and the PKCE state."""
        verifier = secrets.token_urlsafe(48)
        state = {"verifier": verifier, "state": secrets.token_urlsafe(12)}
        params = {
            "client_id": "meho-web",
            "redirect_uri": _REDIRECT,
            "response_type": "code",
            "scope": "openid",
            "state": state["state"],
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
            **extra,
        }
        page = self._send(
            "GET", f"{self.kc.base_url}/realms/meho/protocol/openid-connect/auth", params=params
        )
        return page, state

    def post(self, action: str, data: dict[str, str]) -> httpx.Response:
        return self._send("POST", action, data=data)

    def id_token(self, redirect: httpx.Response, state: dict[str, str]) -> dict[str, Any]:
        """Take the code from Keycloak's redirect back to the console, and swap it."""
        assert redirect.status_code == 302, _page_text(redirect.text)
        location = redirect.headers["location"]
        assert location.startswith(_REDIRECT), location
        query = parse_qs(urlsplit(location).query)
        assert query["state"] == [state["state"]]
        tokens = httpx.post(
            f"{self.kc.base_url}/realms/meho/protocol/openid-connect/token",
            data={
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "redirect_uri": _REDIRECT,
                "client_id": "meho-web",
                "client_secret": self.kc.client_secrets["meho-web"],
                "code_verifier": state["verifier"],
            },
        )
        assert tokens.status_code == 200, tokens.text
        return _token_claims(tokens.json()["id_token"])


def _page_text(page: str) -> str:
    """The title and the text of a page without its tags, for a failure message."""
    title = re.search(r"<title>(.*?)</title>", page, re.S)
    text = " ".join(re.sub(r"<[^>]+>", " ", page).split())
    return f"{title.group(1).strip() if title else '?'}: {text[:1500]}"


def _form_action(page: str, form_id: str) -> str | None:
    for tag in re.findall(r"<form\b[^>]*>", page):
        if re.search(rf'\bid="{re.escape(form_id)}"', tag):
            found = re.search(r'\baction="([^"]+)"', tag)
            return html.unescape(found.group(1)) if found else None
    return None


def _is_password_page(page: httpx.Response) -> bool:
    return page.status_code == 200 and _form_action(page.text, "kc-form-login") is not None


def _is_passkey_page(page: httpx.Response) -> bool:
    return page.status_code == 200 and _form_action(page.text, "webauth") is not None


def _log_in_with_password(browser: _Browser, person: _Person, **extra: str) -> dict[str, Any]:
    page, state = browser.start(**extra)
    assert _is_password_page(page), _page_text(page.text)
    action = _form_action(page.text, "kc-form-login")
    assert action is not None
    done = browser.post(action, {"username": person.username, "password": person.password})
    return browser.id_token(done, state)


def _touch(browser: _Browser, person: _Person, page: httpx.Response) -> httpx.Response:
    """Answer Keycloak's passkey page with the person's passkey."""
    assert _is_passkey_page(page), _page_text(page.text)
    action = _form_action(page.text, "webauth")
    assert action is not None
    challenge = re.search(r"challenge\s*:\s*[\"']([^\"']+)[\"']", page.text)
    rp_id = re.search(r"rpId\s*:\s*[\"']([^\"']+)[\"']", page.text)
    assert challenge is not None and rp_id is not None, _page_text(page.text)
    base = urlsplit(browser.kc.base_url)
    answer = person.passkey.answer(
        challenge=challenge.group(1),
        rp_id=rp_id.group(1),
        origin=f"{base.scheme}://{base.netloc}",
        user_id=person.user_id,
    )
    return browser.post(action, answer)


def _step_up(browser: _Browser, person: _Person) -> tuple[dict[str, Any], str, int]:
    """Ask for the touch level with a fresh nonce; return the claims, the nonce, the start."""
    nonce = _b64url(secrets.token_bytes(32))
    started = int(time.time())
    page, state = browser.start(acr_values=_TOUCH, nonce=nonce)
    return browser.id_token(_touch(browser, person, page), state), nonce, started


def test_plain_console_login_stays_at_level_1(keycloak: _Keycloak) -> None:
    person = _add_person_with_passkey(keycloak, "plain-login")
    browser = _Browser(keycloak)
    claims = _log_in_with_password(browser, person)
    assert claims["acr"] == "1"
    assert "hwk" not in claims.get("amr", [])


def test_touch_level_asks_for_the_passkey_and_signs_the_nonce(keycloak: _Keycloak) -> None:
    person = _add_person_with_passkey(keycloak, "touch-person")
    browser = _Browser(keycloak)
    _log_in_with_password(browser, person)

    claims, nonce, started = _step_up(browser, person)

    assert claims["acr"] == _TOUCH
    assert "hwk" in claims["amr"]
    assert claims["nonce"] == nonce
    assert claims["auth_time"] >= started - 2
    assert claims["sub"] == person.user_id

    # The next request for the touch asks for a new passkey signature, also
    # with the login cookie still there (maximum age 0).
    again, nonce_2, started_2 = _step_up(browser, person)
    assert again["acr"] == _TOUCH
    assert again["nonce"] == nonce_2 != nonce
    assert again["auth_time"] >= started_2 - 2


def test_max_age_0_asks_for_the_password_and_acr_values_for_the_passkey(
    keycloak: _Keycloak,
) -> None:
    """Ask for the touch with ``acr_values``: ``max_age=0`` asks for the password again."""
    person = _add_person_with_passkey(keycloak, "max-age-person")
    browser = _Browser(keycloak)
    _log_in_with_password(browser, person)
    # Keycloak compares max_age with auth_time in whole seconds: a sign-in
    # of this very second still counts as fresh.
    time.sleep(2)

    fresh, _ = browser.start(max_age="0")
    assert _is_password_page(fresh)
    assert not _is_passkey_page(fresh)

    touch, _ = browser.start(acr_values=_TOUCH, nonce=_b64url(secrets.token_bytes(16)))
    assert _is_passkey_page(touch)
    assert not _is_password_page(touch)


def test_a_plain_login_after_a_touch_has_no_touch_level(keycloak: _Keycloak) -> None:
    """``amr`` may still name the passkey after a touch; only ``acr`` and ``nonce`` count."""
    person = _add_person_with_passkey(keycloak, "after-touch")
    browser = _Browser(keycloak)
    _log_in_with_password(browser, person)
    _step_up(browser, person)

    nonce = _b64url(secrets.token_bytes(16))
    page, state = browser.start(nonce=nonce)
    claims = browser.id_token(page, state)
    assert claims["acr"] != _TOUCH
    assert claims["nonce"] == nonce


def _passkey_step_config(kc: _Keycloak) -> tuple[str, dict[str, Any]]:
    headers = _bootstrap_headers(kc)
    admin = f"{kc.base_url}/admin/realms/meho/authentication"
    steps = httpx.get(f"{admin}/flows/{_TOUCH_FLOW}/executions", headers=headers).json()
    step = next(s for s in steps if s.get("providerId") == "webauthn-authenticator-passwordless")
    url = f"{admin}/config/{step['authenticationConfig']}"
    return url, httpx.get(url, headers=headers).json()


def test_amr_is_empty_without_a_maximum_age_on_the_passkey_step(keycloak: _Keycloak) -> None:
    """Keycloak writes a step into ``amr`` only when its setting has a maximum age."""
    url, config = _passkey_step_config(keycloak)
    assert config["config"]["default.reference.value"] == "hwk"
    without = {**config, "config": {"default.reference.value": "hwk"}}
    headers = _bootstrap_headers(keycloak)
    httpx.put(url, headers=headers, json=without).raise_for_status()
    try:
        person = _add_person_with_passkey(keycloak, f"no-max-age-{uuid.uuid4().hex[:6]}")
        browser = _Browser(keycloak)
        _log_in_with_password(browser, person)
        claims, _, _ = _step_up(browser, person)
        assert claims["acr"] == _TOUCH
        assert "hwk" not in claims.get("amr", [])
    finally:
        httpx.put(url, headers=_bootstrap_headers(keycloak), json=config).raise_for_status()
