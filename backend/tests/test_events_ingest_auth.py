# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Per-strategy sender-auth unit tests for the ingest endpoint (#2881).

Drives :func:`meho_backplane.events.ingest.auth.verify_ingest_auth` directly
(no DB, no app) over the three strategies. Acceptance-criteria coverage:

* bad signature rejected (criterion 1)
* replayed / stale timestamp rejected (criterion 1)
* constant-time comparison for every secret check (criterion 4) -- asserted
  structurally by patching :func:`hmac.compare_digest` and proving every
  strategy routes its secret comparison through it, and behaviourally by a
  one-byte tamper being rejected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest
from pydantic import SecretStr

from meho_backplane.event_source.schemas import EventSourceAuthStrategy
from meho_backplane.events.ingest.auth import IngestAuthError, verify_ingest_auth
from meho_backplane.events.ingest.config import resolve_ingest_config

_SECRET = SecretStr("super-shared-key")
_BODY = b'{"alert":"firing","severity":"critical"}'
_NOW = 1_700_000_000.0


def _hmac_headers(*, secret: str = "super-shared-key", ts: float = _NOW, body: bytes = _BODY):
    """Build valid ``X-Meho-Signature`` + ``X-Meho-Timestamp`` headers."""
    signed = f"{ts}".encode() + b"." + body
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return {"X-Meho-Signature": sig, "X-Meho-Timestamp": f"{ts}"}


def _verify_hmac(headers: dict[str, str], *, now: float = _NOW, extras: dict | None = None) -> None:
    verify_ingest_auth(
        strategy=EventSourceAuthStrategy.HMAC_SHA256,
        secret=_SECRET,
        headers=headers,
        raw_body=_BODY,
        config=resolve_ingest_config(extras or {}),
        now=now,
    )


# --------------------------------------------------------------------------
# hmac-sha256
# --------------------------------------------------------------------------


def test_hmac_valid_signature_passes() -> None:
    _verify_hmac(_hmac_headers())  # no raise


def test_hmac_bad_signature_rejected() -> None:
    headers = _hmac_headers()
    headers["X-Meho-Signature"] = "0" * 64
    with pytest.raises(IngestAuthError) as exc:
        _verify_hmac(headers)
    assert exc.value.reason == "bad_signature"


def test_hmac_one_byte_tamper_rejected() -> None:
    """A single flipped body byte invalidates the signature (integrity)."""
    headers = _hmac_headers()
    verify = lambda body: verify_ingest_auth(  # noqa: E731
        strategy=EventSourceAuthStrategy.HMAC_SHA256,
        secret=_SECRET,
        headers=headers,
        raw_body=body,
        config=resolve_ingest_config({}),
        now=_NOW,
    )
    with pytest.raises(IngestAuthError):
        verify(_BODY + b" ")


def test_hmac_missing_signature_header_rejected() -> None:
    with pytest.raises(IngestAuthError) as exc:
        _verify_hmac({"X-Meho-Timestamp": f"{_NOW}"})
    assert exc.value.reason == "missing_signature"


def test_hmac_stale_timestamp_rejected() -> None:
    """A timestamp outside the replay window is rejected even if signed."""
    old_ts = _NOW - 10_000  # far outside the 300 s default window
    headers = _hmac_headers(ts=old_ts)
    with pytest.raises(IngestAuthError) as exc:
        _verify_hmac(headers, now=_NOW)
    assert exc.value.reason == "stale_timestamp"


def test_hmac_missing_timestamp_rejected_when_required() -> None:
    headers = _hmac_headers()
    del headers["X-Meho-Timestamp"]
    with pytest.raises(IngestAuthError) as exc:
        _verify_hmac(headers)
    assert exc.value.reason == "missing_timestamp"


def test_hmac_non_numeric_timestamp_rejected() -> None:
    headers = _hmac_headers()
    headers["X-Meho-Timestamp"] = "not-a-number"
    with pytest.raises(IngestAuthError) as exc:
        _verify_hmac(headers)
    assert exc.value.reason == "bad_timestamp"


def test_hmac_body_only_scheme_ignores_timestamp() -> None:
    """``require_timestamp=false`` signs the body alone (Grafana-style)."""
    sig = hmac.new(b"super-shared-key", _BODY, hashlib.sha256).hexdigest()
    headers = {"X-Grafana-Alerting-Signature": sig}
    _verify_hmac(
        headers,
        extras={"require_timestamp": False, "signature_header": "X-Grafana-Alerting-Signature"},
    )  # no raise


def test_hmac_uppercase_hex_signature_accepted() -> None:
    headers = _hmac_headers()
    headers["X-Meho-Signature"] = headers["X-Meho-Signature"].upper()
    _verify_hmac(headers)  # no raise


# --------------------------------------------------------------------------
# static-header
# --------------------------------------------------------------------------


def test_static_header_valid_passes() -> None:
    verify_ingest_auth(
        strategy=EventSourceAuthStrategy.STATIC_HEADER,
        secret=SecretStr("harbor-token"),
        headers={"Authorization": "harbor-token"},
        raw_body=_BODY,
        config=resolve_ingest_config({}),
        now=_NOW,
    )


def test_static_header_bad_value_rejected() -> None:
    with pytest.raises(IngestAuthError) as exc:
        verify_ingest_auth(
            strategy=EventSourceAuthStrategy.STATIC_HEADER,
            secret=SecretStr("harbor-token"),
            headers={"Authorization": "wrong"},
            raw_body=_BODY,
            config=resolve_ingest_config({}),
            now=_NOW,
        )
    assert exc.value.reason == "bad_static_header"


def test_static_header_custom_header_name() -> None:
    verify_ingest_auth(
        strategy=EventSourceAuthStrategy.STATIC_HEADER,
        secret=SecretStr("tok"),
        headers={"X-Harbor-Secret": "tok"},
        raw_body=_BODY,
        config=resolve_ingest_config({"static_header": "X-Harbor-Secret"}),
        now=_NOW,
    )


# --------------------------------------------------------------------------
# basic
# --------------------------------------------------------------------------


def _basic_header(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _verify_basic(headers: dict[str, str], *, username: str = "alertmanager") -> None:
    verify_ingest_auth(
        strategy=EventSourceAuthStrategy.BASIC,
        secret=SecretStr("am-password"),
        headers=headers,
        raw_body=_BODY,
        config=resolve_ingest_config({"basic_username": username}),
        now=_NOW,
    )


def test_basic_valid_passes() -> None:
    _verify_basic(_basic_header("alertmanager", "am-password"))  # no raise


def test_basic_wrong_password_rejected() -> None:
    with pytest.raises(IngestAuthError) as exc:
        _verify_basic(_basic_header("alertmanager", "nope"))
    assert exc.value.reason == "bad_basic_credentials"


def test_basic_wrong_username_rejected() -> None:
    with pytest.raises(IngestAuthError) as exc:
        _verify_basic(_basic_header("intruder", "am-password"))
    assert exc.value.reason == "bad_basic_credentials"


def test_basic_wrong_scheme_rejected() -> None:
    with pytest.raises(IngestAuthError) as exc:
        _verify_basic({"Authorization": "Bearer am-password"})
    assert exc.value.reason == "bad_authorization_scheme"


def test_basic_malformed_credentials_rejected() -> None:
    with pytest.raises(IngestAuthError) as exc:
        _verify_basic({"Authorization": "Basic !!!not-base64!!!"})
    assert exc.value.reason == "malformed_basic_credentials"


# --------------------------------------------------------------------------
# constant-time discipline (criterion 4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("strategy", "secret", "headers", "extras"),
    [
        (
            EventSourceAuthStrategy.HMAC_SHA256,
            _SECRET,
            _hmac_headers(),
            {},
        ),
        (
            EventSourceAuthStrategy.STATIC_HEADER,
            SecretStr("harbor-token"),
            {"Authorization": "harbor-token"},
            {},
        ),
        (
            EventSourceAuthStrategy.BASIC,
            SecretStr("am-password"),
            _basic_header("am", "am-password"),
            {"basic_username": "am"},
        ),
    ],
)
def test_every_strategy_uses_compare_digest(
    monkeypatch: pytest.MonkeyPatch, strategy, secret, headers, extras
) -> None:
    """Every secret comparison routes through ``hmac.compare_digest``."""
    calls: list[int] = []
    real = hmac.compare_digest

    def _spy(a: object, b: object) -> bool:
        calls.append(1)
        return real(a, b)  # type: ignore[arg-type]

    monkeypatch.setattr("meho_backplane.events.ingest.auth.hmac.compare_digest", _spy)
    verify_ingest_auth(
        strategy=strategy,
        secret=secret,
        headers=headers,
        raw_body=_BODY,
        config=resolve_ingest_config(extras),
        now=_NOW,
    )
    assert calls, "secret comparison must go through hmac.compare_digest"
