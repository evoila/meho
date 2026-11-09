# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The push key and the checks on a browser's push subscription."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from structlog.testing import capture_logs

from meho_backplane.settings import get_settings
from meho_backplane.web_push.endpoints import (
    SubscriptionRefusedError,
    check_subscription,
    push_service_name,
)
from meho_backplane.web_push.keys import (
    new_private_key,
    public_key_for,
    push_public_key,
    reset_key_cache_for_testing,
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _p256dh() -> str:
    point = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    return _b64url(point)


_AUTH = _b64url(bytes(range(16)))


@pytest.mark.parametrize(
    ("endpoint", "service"),
    [
        ("https://fcm.googleapis.com/fcm/send/abc", "Google"),
        ("https://updates.push.services.mozilla.com/wpush/v2/abc", "Mozilla"),
        ("https://web.push.apple.com/abc", "Apple"),
        ("https://wns2-am3p.notify.windows.com/w/?token=abc", "Microsoft"),
        ("https://FCM.googleapis.com:443/fcm/send/abc", "Google"),
    ],
)
def test_the_browser_makers_push_services_are_accepted(endpoint: str, service: str) -> None:
    checked = check_subscription(endpoint, _p256dh(), _AUTH)
    assert checked.endpoint == endpoint
    assert push_service_name(endpoint) == service


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://push.example/abc",
        "https://fcm.googleapis.com.push.example/abc",
        "https://evilpush.apple.com/abc",
        "https://push.apple.com.example/abc",
        "http://fcm.googleapis.com/fcm/send/abc",
        "https://fcm.googleapis.com:8443/fcm/send/abc",
        "https://fcm.googleapis.com:notaport/abc",
        "https://a:b@fcm.googleapis.com/abc",
        "https://127.0.0.1/abc",
        "fcm.googleapis.com/abc",
        "https://fcm.googleapis.com/" + "a" * 2100,
        "   ",
    ],
)
def test_other_endpoints_are_refused_without_quoting_them(endpoint: str) -> None:
    with pytest.raises(SubscriptionRefusedError) as caught:
        check_subscription(endpoint, _p256dh(), _AUTH)
    assert endpoint.strip() == "" or endpoint not in str(caught.value)


@pytest.mark.parametrize(
    ("p256dh", "auth", "word"),
    [
        ("", _AUTH, "p256dh"),
        (_b64url(b"\x04" + b"\x01" * 64), _AUTH, "P-256"),
        (_b64url(bytes(range(33))), _AUTH, "size"),
        ("not+base64/url", _AUTH, "base64url"),
        (None, _b64url(bytes(range(8))), "size"),
        (None, "", "auth"),
        (None, "x" * 200, "auth"),
    ],
)
def test_bad_keys_are_refused(p256dh: str | None, auth: str, word: str) -> None:
    with pytest.raises(SubscriptionRefusedError, match=word):
        check_subscription(
            "https://fcm.googleapis.com/x", p256dh if p256dh is not None else _p256dh(), auth
        )


def test_public_key_is_the_uncompressed_point_of_the_private_key() -> None:
    private_key = new_private_key()
    raw = base64.urlsafe_b64decode(public_key_for(private_key) + "=")
    assert len(raw) == 65
    assert raw[0] == 4


@pytest.mark.parametrize(
    "bad",
    ["", "short", "!!!!", _b64url(b"\x00" * 32), _b64url(b"\xff" * 32), _b64url(bytes(range(48)))],
)
def test_a_bad_private_key_is_refused_without_quoting_it(bad: str) -> None:
    with pytest.raises(ValueError) as caught:
        public_key_for(bad)
    assert not bad or bad not in str(caught.value)


@pytest.fixture
def _push_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name, value in (
        ("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho"),
        ("KEYCLOAK_AUDIENCE", "meho-backplane"),
        ("VAULT_ADDR", "https://vault.test"),
    ):
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    reset_key_cache_for_testing()
    yield monkeypatch
    get_settings.cache_clear()
    reset_key_cache_for_testing()


def test_with_the_setting_off_the_key_is_not_read(_push_env: pytest.MonkeyPatch) -> None:
    _push_env.setenv("WEB_PUSH_ENABLED", "false")
    _push_env.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", "not-a-key")
    with capture_logs() as captured:
        assert push_public_key() is None
    assert captured == []


def test_with_the_setting_on_a_missing_or_bad_key_turns_push_off(
    _push_env: pytest.MonkeyPatch,
) -> None:
    _push_env.setenv("WEB_PUSH_ENABLED", "true")
    _push_env.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", "")
    with capture_logs() as captured:
        assert push_public_key() is None
    assert [e["event"] for e in captured] == ["web_push_key_missing"]

    bad = _b64url(b"\xff" * 32)
    _push_env.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", bad)
    get_settings.cache_clear()
    reset_key_cache_for_testing()
    with capture_logs() as captured:
        assert push_public_key() is None
    assert [e["event"] for e in captured] == ["web_push_key_unusable"]
    assert bad not in repr(captured)


def test_with_the_setting_on_and_a_good_key_push_is_on(_push_env: pytest.MonkeyPatch) -> None:
    private_key = new_private_key()
    _push_env.setenv("WEB_PUSH_ENABLED", "true")
    _push_env.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", private_key)
    assert push_public_key() == public_key_for(private_key)
