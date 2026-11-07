# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``hash`` redaction action writes a keyed fingerprint.

A ``hash`` rule may match a credential. The string it writes must then
depend on the server key, so it cannot be computed again from a guess,
and it must never equal a plain SHA-256 prefix of the match. The
second-stage (Presidio) ``hash`` action writes the same string.
"""

from __future__ import annotations

import hashlib
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from meho_backplane.operations._validate import compute_value_fingerprint
from meho_backplane.redaction import RedactionPolicy, parse_policy, redact
from meho_backplane.redaction.engine import hash_replacement
from meho_backplane.redaction.policy import Tier2Rule
from meho_backplane.redaction.presidio import _build_operators
from meho_backplane.settings import get_settings

#: A made-up labelled credential; the ``api_key`` pattern matches the whole pair.
_SECRET = "password=Tr0ub4dor-and-3-horses"
_PLAIN_SHA256_PREFIX = hashlib.sha256(_SECRET.encode("utf-8")).hexdigest()[:12]


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the env vars the settings need; the server key comes from them."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _hash_policy(pattern: str) -> RedactionPolicy:
    rules = textwrap.dedent(
        f"""
          - name: r
            pattern: {pattern}
            action: hash
            reason: "hash action"
        """
    ).strip("\n")
    return parse_policy(f"id: test\nversion: 1\nrules:\n{rules}\n")


def test_hash_action_never_writes_a_plain_sha256_prefix() -> None:
    leaf = f"db: {_SECRET} host=db.example.test"
    result = redact(leaf, _hash_policy("api_key"))
    start, end = result.manifest[0].span
    assert leaf[start:end] == _SECRET
    assert result.manifest[0].action == "hash"
    redacted = str(result.redacted)
    assert "Tr0ub4dor" not in redacted
    assert _PLAIN_SHA256_PREFIX not in redacted
    expected = compute_value_fingerprint(_SECRET.encode("utf-8"))[:12]
    assert redacted == f"db: hmac-sha256:{expected} host=db.example.test"


def test_hash_action_depends_on_the_server_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = hash_replacement(_SECRET)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'other.db'}")
    get_settings.cache_clear()
    try:
        after = hash_replacement(_SECRET)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    assert before.startswith("hmac-sha256:")
    assert after.startswith("hmac-sha256:")
    assert after != before


def test_hash_action_is_stable_inside_one_install() -> None:
    assert hash_replacement("db.example.test") == hash_replacement("db.example.test")
    assert hash_replacement("db.example.test") != hash_replacement("other.example.test")


def test_presidio_hash_action_uses_the_same_keyed_fingerprint() -> None:
    pytest.importorskip("presidio_anonymizer")
    rule = Tier2Rule(name="r", fields=("notes",), action="hash", reason="hash action")
    operator = _build_operators(rule)["DEFAULT"]
    assert operator.operator_name == "custom"
    assert operator.params["lambda"](_SECRET) == hash_replacement(_SECRET)
    assert _PLAIN_SHA256_PREFIX not in operator.params["lambda"](_SECRET)
