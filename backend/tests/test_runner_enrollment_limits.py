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
import traceback
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.runner_enrollment_limits import give_back_refusal_slot, take_refusal_slot
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, RunnerPrincipal
from meho_backplane.main import app
from meho_backplane.runner.key_id import ec_key_id
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._runner_enroll_fakes import LIMITS, BrokenValkey, FakeValkey
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


@pytest.fixture
def valkey() -> Iterator[FakeValkey]:
    fake = FakeValkey()
    with patch(f"{LIMITS}.get_broadcast_client", lambda: fake):
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
    client: TestClient, valkey: FakeValkey, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_limit(monkeypatch, 3)
    answers = [client.post(_ENROLL, json={"enrollment_code": "x"}) for _ in range(5)]
    assert [a.status_code for a in answers] == [422, 422, 422, 429, 429]
    assert answers[3].json() == {"detail": "enrollment_rate_limited"}
    assert 1 <= int(answers[3].headers["Retry-After"]) <= 60


@pytest.mark.asyncio
async def test_the_limit_comes_before_the_body_check_and_zero_turns_it_off(
    client: TestClient, valkey: FakeValkey, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_limit(monkeypatch, 1)
    assert client.post(_ENROLL, content=b"{not json").status_code == 422
    assert client.post(_ENROLL, content=b"{not json").status_code == 429
    _set_limit(monkeypatch, 0)
    assert client.post(_ENROLL, content=b"{not json").status_code == 422


@pytest.mark.asyncio
async def test_a_code_over_256_characters_gets_403_with_no_database_read(
    client: TestClient, valkey: FakeValkey
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
    client: TestClient, valkey: FakeValkey, tmp_path: Any
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
async def test_each_attempt_to_store_the_key_has_a_started_row_and_a_repeat_adds_none(
    client: TestClient, valkey: FakeValkey, tmp_path: Any
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
    assert len(await _rows(_STARTED)) == 2
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
    client: TestClient, valkey: FakeValkey, tmp_path: Any, form: str
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
    client: TestClient, valkey: FakeValkey, content: bytes
) -> None:
    before = len(await _refused_rows())
    resp = client.post(_ENROLL, content=content, headers={"Content-Type": "application/json"})
    assert resp.status_code in (400, 422)
    assert len(await _refused_rows()) == before


@pytest.mark.asyncio
async def test_a_long_code_with_another_body_error_gets_422_and_no_row(
    client: TestClient, valkey: FakeValkey
) -> None:
    before = len(await _refused_rows())
    resp = client.post(_ENROLL, json={"enrollment_code": "a" * 300})
    assert resp.status_code == 422
    assert "a" * 20 not in resp.text
    assert len(await _refused_rows()) == before


@pytest.mark.asyncio
async def test_the_code_of_a_revoked_runner_writes_no_row_for_a_bad_body(
    client: TestClient, valkey: FakeValkey
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
    client: TestClient, valkey: FakeValkey, tmp_path: Any
) -> None:
    name = "site-row-limit"
    code = (await _register(client, name))["enrollment_code"]
    bad = {"enrollment_code": code}
    with patch(f"{LIMITS}.time.time", return_value=1_700_000_030.0):
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
    with patch(f"{LIMITS}.time.time", return_value=1_700_000_030.0 + 60):
        assert client.post(_ENROLL, json=bad).status_code == 422
        assert client.post(_ENROLL, json=bad).status_code == 422
    rows = [r for r in await _refused_rows() if r.payload["reason"] == "request_body_invalid"]
    assert len(rows) == 2
    assert sorted(r.payload.get("left_out_before", 0) for r in rows) == [0, 3]


@pytest.mark.asyncio
async def test_a_request_with_no_client_address_counts_under_unknown(
    client: TestClient, valkey: FakeValkey
) -> None:
    sent: list[dict[str, Any]] = []
    body = json.dumps({"enrollment_code": "x"}).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": _ENROLL,
        "raw_path": _ENROLL.encode(),
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "http_version": "1.1",
        "headers": [(b"host", b"t"), (b"content-type", b"application/json")],
        "client": None,
        "server": ("t", 80),
    }
    await app(scope, receive, send)
    assert sent[0]["status"] == 422
    assert any(":runner-enroll:unknown:" in key for key in valkey.store)


@pytest.mark.asyncio
async def test_an_unreachable_counter_gives_503_with_a_fixed_text(client: TestClient) -> None:
    with patch(f"{LIMITS}.get_broadcast_client", BrokenValkey):
        resp = client.post(_ENROLL, json={"enrollment_code": "x"})
    assert resp.status_code == 503
    assert resp.json() == {"detail": "enrollment_limit_unavailable"}


@pytest.mark.asyncio
async def test_with_the_limit_off_no_valkey_call_is_made(
    client: TestClient, valkey: FakeValkey, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_limit(monkeypatch, 0)
    assert client.post(_ENROLL, json={"enrollment_code": "x"}).status_code == 422
    assert valkey.calls == 0


@pytest.mark.asyncio
async def test_a_runner_login_stops_on_503_and_writes_no_record(tmp_path: Any) -> None:
    import httpx

    from meho_backplane.runner.login import (
        LOGIN_FILE_NAME,
        RunnerLoginError,
        enroll_with_code,
        load_or_create_private_key,
    )

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "enrollment_limit_unavailable"})

    key = load_or_create_private_key(str(tmp_path))
    with pytest.raises(RunnerLoginError, match="HTTP 503"):
        await enroll_with_code(
            central_url="https://central.test",
            code="c",
            runner_id="site",
            key=key,
            key_dir=str(tmp_path),
            transport=httpx.MockTransport(answer),
        )
    assert not (tmp_path / LOGIN_FILE_NAME).exists()


@pytest.mark.asyncio
async def test_a_refusal_row_after_a_gap_carries_the_whole_count(
    client: TestClient, valkey: FakeValkey
) -> None:
    code = (await _register(client, "site-gap"))["enrollment_code"]
    bad = {"enrollment_code": code}
    with patch(f"{LIMITS}.time.time", return_value=1_700_000_030.0):
        for _ in range(4):
            client.post(_ENROLL, json=bad)
    with patch(f"{LIMITS}.time.time", return_value=1_700_000_030.0 + 60 * 7):
        client.post(_ENROLL, json=bad)
    counts = sorted(r.payload.get("left_out_before", 0) for r in await _refused_rows())
    assert counts == [0, 3]


@pytest.mark.asyncio
async def test_a_failed_refusal_write_gives_the_slot_and_the_count_back(
    client: TestClient, valkey: FakeValkey
) -> None:
    code = (await _register(client, "site-give-back"))["enrollment_code"]
    bad = {"enrollment_code": code}
    broken = TestClient(app, raise_server_exceptions=False)
    with patch(f"{LIMITS}.time.time", return_value=1_700_000_030.0):
        client.post(_ENROLL, json=bad)
        client.post(_ENROLL, json=bad)
        client.post(_ENROLL, json=bad)
    with patch(f"{LIMITS}.time.time", return_value=1_700_000_030.0 + 60):
        with patch(
            "meho_backplane.auth.runner_enrollment.write_internal_audit_row",
            side_effect=RuntimeError("audit write failed"),
        ):
            assert broken.post(_ENROLL, json=bad).status_code == 500
        assert client.post(_ENROLL, json=bad).status_code == 422
    counts = sorted(r.payload.get("left_out_before", 0) for r in await _refused_rows())
    assert counts == [0, 2]


@pytest.mark.asyncio
async def test_a_refusal_row_is_written_when_the_counter_cannot_be_reached(
    client: TestClient, valkey: FakeValkey
) -> None:
    code = (await _register(client, "site-no-counter"))["enrollment_code"]
    bad = {"enrollment_code": code}
    before = len(await _refused_rows())

    async def _open(source: str) -> None:
        return None

    with (
        patch("meho_backplane.api.v1.runner_enrollment.enforce_enroll_request_limit", _open),
        patch(f"{LIMITS}.get_broadcast_client", BrokenValkey),
    ):
        assert client.post(_ENROLL, json=bad).status_code == 422
        assert client.post(_ENROLL, json=bad).status_code == 422
    assert len(await _refused_rows()) == before + 2


async def _claim_without_started_row(name: str, key_id: str) -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(RunnerPrincipal).where(RunnerPrincipal.name == name))
        ).scalar_one()
        row.enrolled_key_id = key_id
        await session.commit()


@pytest.mark.asyncio
async def test_a_claim_with_no_started_row_gets_one_before_keycloak_is_called(
    client: TestClient, valkey: FakeValkey, tmp_path: Any
) -> None:
    name = "site-old-claim"
    code = (await _register(client, name))["enrollment_code"]
    jwk = _jwk(tmp_path)
    await _claim_without_started_row(name, ec_key_id(jwk))
    body = {"enrollment_code": code, "public_key": jwk, "runner_name": name}
    factory, kc = _kc_mock()
    broken = TestClient(app, raise_server_exceptions=False)
    with patch(_KC, factory):
        with patch(
            "meho_backplane.auth.runner_enrollment._started_audit_row",
            side_effect=RuntimeError("audit write failed"),
        ):
            assert broken.post(_ENROLL, json=body).status_code == 500
        kc.set_client_signed_jwt_key.assert_not_awaited()
        assert await _rows(_STARTED) == []
        assert (await _row(name)).key_enrolled_at is None
        assert client.post(_ENROLL, json=body).status_code == 200
    kc.set_client_signed_jwt_key.assert_awaited_once()
    assert len(await _rows(_STARTED)) == 1


@pytest.mark.asyncio
async def test_a_failed_started_write_on_a_retry_after_keycloak_failed_calls_no_keycloak(
    client: TestClient, valkey: FakeValkey, tmp_path: Any
) -> None:
    from meho_backplane.auth.keycloak_admin import KeycloakAdminError

    name = "site-retry-audit"
    code = (await _register(client, name))["enrollment_code"]
    body = {"enrollment_code": code, "public_key": _jwk(tmp_path), "runner_name": name}
    factory, kc = _kc_mock()
    kc.set_client_signed_jwt_key.side_effect = KeycloakAdminError("down")
    broken = TestClient(app, raise_server_exceptions=False)
    with patch(_KC, factory):
        assert client.post(_ENROLL, json=body).status_code == 502
        kc.set_client_signed_jwt_key.reset_mock()
        with patch(
            "meho_backplane.auth.runner_enrollment._started_audit_row",
            side_effect=RuntimeError("audit write failed"),
        ):
            assert broken.post(_ENROLL, json=body).status_code == 500
        kc.set_client_signed_jwt_key.assert_not_awaited()
        kc.set_client_signed_jwt_key.side_effect = None
        assert client.post(_ENROLL, json=body).status_code == 200
    assert len(await _rows(_STARTED)) == 2


@pytest.mark.asyncio
async def test_another_route_keeps_the_input_of_its_validation_errors(
    client: TestClient,
) -> None:
    resp = client.post("/api/v1/runner-principals", json={"made-up-extra": "made-up-value"})
    assert resp.status_code in (401, 403, 422)
    if resp.status_code == 422:
        assert any("input" in e for e in resp.json()["detail"])


@pytest.mark.asyncio
async def test_a_failed_bad_body_audit_keeps_the_code_out_of_the_logged_error(
    client: TestClient, valkey: FakeValkey, tmp_path: Any
) -> None:
    code = (await _register(client, "site-audit-log"))["enrollment_code"]
    key = _jwk(tmp_path)
    with (
        patch(
            "meho_backplane.auth.runner_enrollment.write_internal_audit_row",
            side_effect=RuntimeError("audit write failed"),
        ),
        pytest.raises(RuntimeError) as raised,
    ):
        client.post(_ENROLL, json={"enrollment_code": code, "public_key": key})
    # The request logger writes this exception with its whole chain.
    logged = "".join(traceback.format_exception(raised.value))
    assert raised.value.__context__ is None
    for part in (code.partition(".")[2], key["x"], key["y"]):
        assert part not in logged


@pytest.mark.asyncio
async def test_an_extra_field_named_body_is_not_named_in_the_answer(
    client: TestClient, valkey: FakeValkey, tmp_path: Any
) -> None:
    sent = {
        "enrollment_code": "x",
        "public_key": _jwk(tmp_path),
        "runner_name": "site-extra-body",
        "body": "made-up-extra-value",
    }
    resp = client.post(_ENROLL, json=sent)
    assert resp.status_code == 422
    assert [e["loc"] for e in resp.json()["detail"]] == [["body", "extra_field"]]
    assert "made-up-extra-value" not in resp.text


def _surrogate_body(code: str, **fields: Any) -> bytes:
    """A JSON body whose code is the runner's id, a dot and a lone surrogate."""
    text = json.dumps({"enrollment_code": f"{code.partition('.')[0]}.abc", **fields})
    return text.replace(".abc", ".abc\\ud800").encode()


@pytest.mark.asyncio
async def test_a_code_with_a_lone_surrogate_is_refused_as_invalid_and_writes_no_row(
    client: TestClient, valkey: FakeValkey, tmp_path: Any
) -> None:
    name = "site-surrogate"
    code = (await _register(client, name))["enrollment_code"]
    headers = {"Content-Type": "application/json"}
    before = len(await _refused_rows())
    bad_body = client.post(_ENROLL, content=_surrogate_body(code), headers=headers)
    assert bad_body.status_code == 422
    full = _surrogate_body(code, public_key=_jwk(tmp_path), runner_name=name)
    resp = client.post(_ENROLL, content=full, headers=headers)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "enrollment_code_invalid"}
    assert len(await _refused_rows()) == before


@pytest.mark.asyncio
async def test_a_right_code_with_a_lone_surrogate_in_the_name_gets_422_and_one_row(
    client: TestClient, valkey: FakeValkey, tmp_path: Any
) -> None:
    name = "site-surrogate-name"
    code = (await _register(client, name))["enrollment_code"]
    text = json.dumps({"enrollment_code": code, "public_key": _jwk(tmp_path), "runner_name": "s"})
    body = text.replace('"runner_name": "s"', '"runner_name": "s\\ud800"').encode()
    before = len(await _refused_rows())
    resp = client.post(_ENROLL, content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 422
    rows = await _refused_rows()
    assert len(rows) == before + 1
    assert any(
        r.payload == {"runner": name, "reason": "request_body_invalid", "key_id": None}
        for r in rows
    )


_RUNNER = uuid.UUID(int=7)


@pytest.mark.asyncio
async def test_overlapping_refusals_give_back_only_the_slot_that_was_taken(
    valkey: FakeValkey,
) -> None:
    with patch(f"{LIMITS}.time.time", return_value=1_700_000_030.0):
        first = await take_refusal_slot(_RUNNER, "r")
        assert first is not None and first.left_out == 0
        assert await take_refusal_slot(_RUNNER, "r") is None
        await give_back_refusal_slot(_RUNNER, "r", first)
        retry = await take_refusal_slot(_RUNNER, "r")
    assert retry is not None and retry.left_out == 1


@pytest.mark.asyncio
async def test_a_slot_given_back_after_the_minute_changed_frees_its_own_minute(
    valkey: FakeValkey,
) -> None:
    start = 1_700_000_030.0
    with patch(f"{LIMITS}.time.time", return_value=start):
        first = await take_refusal_slot(_RUNNER, "r")
    assert first is not None
    with patch(f"{LIMITS}.time.time", return_value=start + 60):
        second = await take_refusal_slot(_RUNNER, "r")
        assert second is not None
        await give_back_refusal_slot(_RUNNER, "r", first)
        assert await take_refusal_slot(_RUNNER, "r") is None
    assert first.key not in valkey.store and second.key in valkey.store
    with patch(f"{LIMITS}.time.time", return_value=start):
        assert await take_refusal_slot(_RUNNER, "r") is not None


@pytest.mark.asyncio
async def test_the_left_out_count_is_kept_until_a_row_records_it(valkey: FakeValkey) -> None:
    start = 1_700_000_030.0
    with patch(f"{LIMITS}.time.time", return_value=start):
        assert await take_refusal_slot(_RUNNER, "r") is not None
        assert await take_refusal_slot(_RUNNER, "r") is None
        assert await take_refusal_slot(_RUNNER, "r") is None
    with patch(f"{LIMITS}.time.time", return_value=start + 45 * 24 * 3600):
        later = await take_refusal_slot(_RUNNER, "r")
    assert later is not None and later.left_out == 2
