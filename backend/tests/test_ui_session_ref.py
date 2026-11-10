# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the console session ref: the short stand-in for a session id."""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from meho_backplane.ui.auth import session_ref as session_ref_module
from meho_backplane.ui.auth.session_ref import SESSION_REF_LENGTH, session_ref
from meho_backplane.ui.auth.session_store import EncryptionKeyMissingError


def _use_key(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    settings = SimpleNamespace(ui_session_encryption_key=key)
    monkeypatch.setattr(session_ref_module, "get_settings", lambda: settings)


def test_ref_is_short_hex_stable_and_not_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_key(monkeypatch, Fernet.generate_key().decode())
    sid = uuid.uuid4()
    ref = session_ref(sid)
    assert re.fullmatch(rf"[0-9a-f]{{{SESSION_REF_LENGTH}}}", ref)
    assert session_ref(sid) == ref
    assert ref not in str(sid)
    assert session_ref(uuid.uuid4()) != ref


def test_ref_depends_on_the_server_key(monkeypatch: pytest.MonkeyPatch) -> None:
    sid = uuid.uuid4()
    _use_key(monkeypatch, Fernet.generate_key().decode())
    first = session_ref(sid)
    _use_key(monkeypatch, Fernet.generate_key().decode())
    assert session_ref(sid) != first


def test_ref_without_a_server_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_key(monkeypatch, "")
    with pytest.raises(EncryptionKeyMissingError):
        session_ref(uuid.uuid4())
