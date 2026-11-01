# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the agent run console's run-handoff token.

The token rides in the stream URL query string, so it must be opaque:
neither the console session id nor the run prompt may be readable from
it, and the decrypted payload holds only a keyed digest of the session.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet

from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.session_store import EncryptionKeyMissingError
from meho_backplane.ui.routes.agents.run_token import (
    RUN_TOKEN_TTL_SECONDS,
    mint_run_token,
    verify_run_token,
)

_PROMPT = "summarise the topology of the north cluster"
_NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", key)
    get_settings.cache_clear()
    yield key
    get_settings.cache_clear()


def _mint(session_id: str, *, now: float = _NOW) -> str:
    return mint_run_token(
        session_id=session_id, name="a1", input_=_PROMPT, work_ref="ref-1", now=now
    )


def _b64_decoded(token: str) -> bytes:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))


def test_round_trip_returns_bound_values() -> None:
    session_id = str(uuid.uuid4())
    decoded = verify_run_token(session_id=session_id, token=_mint(session_id), now=_NOW + 5)
    assert decoded is not None
    assert decoded.name == "a1"
    assert decoded.input == _PROMPT
    assert decoded.work_ref == "ref-1"


def test_token_does_not_carry_session_id_or_prompt() -> None:
    session_id = str(uuid.uuid4())
    token = _mint(session_id)
    for blob in (token.encode("ascii"), _b64_decoded(token)):
        assert session_id.encode() not in blob
        assert session_id.replace("-", "").encode() not in blob
        assert _PROMPT.encode() not in blob
        assert b"north cluster" not in blob


def test_decrypted_payload_holds_only_a_session_digest(_key: str) -> None:
    session_id = str(uuid.uuid4())
    payload = json.loads(Fernet(_key.encode()).decrypt(_mint(session_id).encode()))
    assert set(payload) == {"sd", "name", "input", "work_ref"}
    assert session_id not in json.dumps(payload)
    assert session_id.replace("-", "") not in json.dumps(payload)


def test_token_for_another_session_is_rejected() -> None:
    token = _mint(str(uuid.uuid4()))
    assert verify_run_token(session_id=str(uuid.uuid4()), token=token, now=_NOW + 5) is None


def test_expired_token_is_rejected() -> None:
    session_id = str(uuid.uuid4())
    token = _mint(session_id)
    later = _NOW + RUN_TOKEN_TTL_SECONDS + 1
    assert verify_run_token(session_id=session_id, token=token, now=later) is None


def test_token_under_another_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = str(uuid.uuid4())
    token = _mint(session_id)
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    assert verify_run_token(session_id=session_id, token=token, now=_NOW + 5) is None


@pytest.mark.parametrize("token", ["", "abc", "abc.def", "é" * 40])
def test_malformed_token_is_rejected(token: str) -> None:
    assert verify_run_token(session_id=str(uuid.uuid4()), token=token, now=_NOW) is None


def test_tampered_token_is_rejected() -> None:
    session_id = str(uuid.uuid4())
    token = _mint(session_id)
    flipped = token[:-6] + ("A" if token[-6] != "A" else "B") + token[-5:]
    assert verify_run_token(session_id=session_id, token=flipped, now=_NOW + 5) is None


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UI_SESSION_ENCRYPTION_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(EncryptionKeyMissingError):
        _mint(str(uuid.uuid4()))
