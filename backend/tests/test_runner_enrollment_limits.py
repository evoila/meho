# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The open runner enrollment route has limits and always leaves its audit row.

* One source address gets 429 after the per-minute limit; the answer text is fixed.
* A code over 256 characters gets 403 with no database read.
* The audit row "enrollment started" is saved with the claim. When it cannot
  be written, Keycloak is not called and the same code and key work on a retry.
* A body that fails the body check gets a 422 with none of the sent values. A
  right code writes one refusal row; a wrong code writes none.
* A runner gets one refusal row per reason and minute; the next row counts
  what the minute before left out.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.main import app
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._vault_fakes import install_fake_vault
from .test_runner_enrollment_api import (
    _ENROLL,
    _TENANT,
    _jwk,
    _kc_mock,
    _refused_rows,
    _register,
    _revoke,
    _row,
)

_KC = "meho_backplane.auth.runner_enrollment.KeycloakAdminClient.from_settings"
_LIMITS = "meho_backplane.auth.runner_enrollment_limits"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", "https://keycloak.test/admin/realms/meho")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "s3cr3t")
    get_settings.cache_clear()
    clear_jwks_cache()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)

    async def _noop_write(identity_ref: str, client_secret: str, *, tenant_id: Any) -> str:
        return "secret/data/x"

    monkeypatch.setattr("meho_backplane.auth.runner_principals.write_agent_secret", _noop_write)
    yield TestClient(app)


class _FakePipeline:
    def __init__(self, store: dict[str, int]) -> None:
        self._store = store
        self._keys: list[str] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def incr(self, name: str) -> None:
        self._keys.append(name)

    def expire(self, name: str, time: int) -> None:
        return None

    async def execute(self) -> list[Any]:
        counts = []
        for name in self._keys:
            self._store[name] = self._store.get(name, 0) + 1
            counts.append(self._store[name])
        return [*counts, True]


class _FakeValkey:
    def __init__(self) -> None:
        self.store: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self.store)

    async def get(self, name: str) -> str | None:
        value = self.store.get(name)
        return None if value is None else str(value)


@pytest.fixture
def valkey() -> Iterator[_FakeValkey]:
    fake = _FakeValkey()
    with patch(f"{_LIMITS}.get_broadcast_client", lambda: fake):
        yield fake


def _set_limit(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    monkeypatch.setenv("RUNNER_ENROLL_RATE_PER_MINUTE", str(value))
    get_settings.cache_clear()


async def _rows(path: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == path))
        return list(result.scalars().all())


_STARTED = "runner.principal.key_enrollment_started"


@pytest.mark.asyncio
async def test_a_source_over_the_limit_gets_429_with_a_fixed_text(
    client: TestClient, valkey: _FakeValkey, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_limit(monkeypatch, 3)
    answers = [client.post(_ENROLL, json={"enrollment_code": "x"}) for _ in range(5)]
    assert [a.status_code for a in answers] == [422, 422, 422, 429, 429]
    assert answers[3].json() == {"detail": "enrollment_rate_limited"}
    assert 1 <= int(answers[3].headers["Retry-After"]) <= 60


@pytest.mark.asyncio
async def test_the_limit_comes_before_the_body_check_and_zero_turns_it_off(
    client: TestClient, valkey: _FakeValkey, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_limit(monkeypatch, 1)
    assert client.post(_ENROLL, content=b"{not json").status_code == 422
    assert client.post(_ENROLL, content=b"{not json").status_code == 429
    _set_limit(monkeypatch, 0)
    assert client.post(_ENROLL, content=b"{not json").status_code == 422


@pytest.mark.asyncio
async def test_a_code_over_256_characters_gets_403_with_no_database_read(
    client: TestClient, valkey: _FakeValkey
) -> None:
    def _no_session() -> Any:
        raise AssertionError("the database was read")

    body = {"enrollment_code": "a" * 257, "public_key": {}, "runner_name": "site"}
    with patch("meho_backplane.auth.runner_enrollment.get_sessionmaker", _no_session):
        resp = client.post(_ENROLL, json=body)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "enrollment_code_invalid"}


@pytest.mark.asyncio
async def test_a_failed_audit_write_stops_before_keycloak_and_a_retry_works(
    client: TestClient, valkey: _FakeValkey, tmp_path: Any
) -> None:
    name = "site-audit-fails"
    code = (await _register(client, name))["enrollment_code"]
    body = {"enrollment_code": code, "public_key": _jwk(tmp_path), "runner_name": name}
    factory, kc = _kc_mock()
    broken = TestClient(app, raise_server_exceptions=False)
    with patch(_KC, factory):
        with patch(
            "meho_backplane.auth.runner_enrollment._started_audit_row",
            side_effect=RuntimeError("audit write failed"),
        ):
            failed = broken.post(_ENROLL, json=body)
        assert failed.status_code == 500
        kc.set_client_signed_jwt_key.assert_not_awaited()
        row = await _row(name)
        assert row.enrolled_key_id is None and row.key_enrolled_at is None
        assert await _rows(_STARTED) == []
        retry = client.post(_ENROLL, json=body)
    assert retry.status_code == 200, retry.text
    kc.set_client_signed_jwt_key.assert_awaited_once()
    (started,) = await _rows(_STARTED)
    assert started.tenant_id == _TENANT
    assert started.payload == {"runner": name, "key_id": retry.json()["key_id"]}
    assert code.partition(".")[2] not in json.dumps(started.payload)


@pytest.mark.asyncio
async def test_a_stored_key_has_one_started_row_also_after_keycloak_fails_and_repeats(
    client: TestClient, valkey: _FakeValkey, tmp_path: Any
) -> None:
    name = "site-started-once"
    code = (await _register(client, name))["enrollment_code"]
    body = {"enrollment_code": code, "public_key": _jwk(tmp_path), "runner_name": name}
    factory, kc = _kc_mock()
    from meho_backplane.auth.keycloak_admin import KeycloakAdminError

    kc.set_client_signed_jwt_key.side_effect = KeycloakAdminError("down")
    with patch(_KC, factory):
        assert client.post(_ENROLL, json=body).status_code == 502
        assert len(await _rows(_STARTED)) == 1
        kc.set_client_signed_jwt_key.side_effect = None
        assert client.post(_ENROLL, json=body).status_code == 200
        assert client.post(_ENROLL, json=body).status_code == 200
    assert len(await _rows(_STARTED)) == 1
    assert (await _row(name)).key_enrolled_at is not None


_BODY_FORMS = {
    "missing_name": lambda code, key: {"enrollment_code": code, "public_key": key},
    "extra_field": lambda code, key: {
        "enrollment_code": code,
        "public_key": key,
        "runner_name": "site-body",
        "made-up-extra-name": "made-up-extra-value",
    },
    "name_too_long": lambda code, key: {
        "enrollment_code": code,
        "public_key": key,
        "runner_name": "n" * 257,
    },
    "name_wrong_kind": lambda code, key: {
        "enrollment_code": code,
        "public_key": key,
        "runner_name": 12345,
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("form", sorted(_BODY_FORMS))
async def test_a_body_that_fails_the_check_repeats_nothing_and_a_right_code_writes_one_row(
    client: TestClient, valkey: _FakeValkey, tmp_path: Any, form: str
) -> None:
    name = "site-body"
    code = (await _register(client, name))["enrollment_code"]
    key = _jwk(tmp_path)
    sent = _BODY_FORMS[form](code, key)
    before = len(await _refused_rows())
    resp = client.post(_ENROLL, json=sent)
    assert resp.status_code == 422
    for part in (code, code.partition(".")[2], key["x"], key["y"], "made-up-extra"):
        assert part not in resp.text
    assert all("input" not in error for error in resp.json()["detail"])
    rows = await _refused_rows()
    assert len(rows) == before + 1
    row = next(r for r in rows if r.payload["runner"] == name)
    assert row.status_code == 422 and row.tenant_id == _TENANT
    assert row.payload == {"runner": name, "reason": "request_body_invalid", "key_id": None}
    stored = json.dumps(
        [getattr(row, column.key) for column in AuditLog.__table__.columns], default=str
    )
    for part in (code.partition(".")[2], key["x"], key["y"]):
        assert part not in stored
    wrong = {**sent, "enrollment_code": code[:-3] + "zzz"}
    assert client.post(_ENROLL, json=wrong).status_code == 422
    assert len(await _refused_rows()) == before + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [b"not json", b"[1, 2]", b'"text"', b'{"enrollment_code": 7}', b'{"enrollment_code": ["a"]}'],
)
async def test_a_body_of_the_wrong_shape_gets_422_and_no_row(
    client: TestClient, valkey: _FakeValkey, content: bytes
) -> None:
    before = len(await _refused_rows())
    resp = client.post(_ENROLL, content=content, headers={"Content-Type": "application/json"})
    assert resp.status_code in (400, 422)
    assert len(await _refused_rows()) == before


@pytest.mark.asyncio
async def test_a_long_code_with_another_body_error_gets_422_and_no_row(
    client: TestClient, valkey: _FakeValkey
) -> None:
    before = len(await _refused_rows())
    resp = client.post(_ENROLL, json={"enrollment_code": "a" * 300})
    assert resp.status_code == 422
    assert "a" * 20 not in resp.text
    assert len(await _refused_rows()) == before


@pytest.mark.asyncio
async def test_the_code_of_a_revoked_runner_writes_no_row_for_a_bad_body(
    client: TestClient, valkey: _FakeValkey
) -> None:
    name = "site-body-revoked"
    code = (await _register(client, name))["enrollment_code"]
    factory, _kc = _kc_mock()
    with patch("meho_backplane.auth.runner_principals.KeycloakAdminClient.from_settings", factory):
        await _revoke(client, name)
    before = len(await _refused_rows())
    assert client.post(_ENROLL, json={"enrollment_code": code}).status_code == 422
    assert len(await _refused_rows()) == before


@pytest.mark.asyncio
async def test_a_runner_gets_one_refusal_row_per_reason_and_minute(
    client: TestClient, valkey: _FakeValkey, tmp_path: Any
) -> None:
    name = "site-row-limit"
    code = (await _register(client, name))["enrollment_code"]
    bad = {"enrollment_code": code}
    with patch(f"{_LIMITS}.time.time", return_value=1_700_000_030.0):
        for _ in range(4):
            assert client.post(_ENROLL, json=bad).status_code == 422
        mismatch = {**bad, "public_key": _jwk(tmp_path), "runner_name": "site-else"}
        assert client.post(_ENROLL, json=mismatch).status_code == 409
        assert client.post(_ENROLL, json=mismatch).status_code == 409
    rows = await _refused_rows()
    assert sorted(r.payload["reason"] for r in rows) == [
        "request_body_invalid",
        "runner_name_mismatch",
    ]
    with patch(f"{_LIMITS}.time.time", return_value=1_700_000_030.0 + 60):
        assert client.post(_ENROLL, json=bad).status_code == 422
        assert client.post(_ENROLL, json=bad).status_code == 422
    rows = [r for r in await _refused_rows() if r.payload["reason"] == "request_body_invalid"]
    assert len(rows) == 2
    assert sorted(r.payload.get("left_out_before", 0) for r in rows) == [0, 3]
