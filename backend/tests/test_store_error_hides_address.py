# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A secret store error shows the caller no store address and no store text."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import hvac.exceptions
import pytest
from structlog.testing import capture_logs

from meho_backplane.auth import vault as vault_module
from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import (
    VaultClientError,
    VaultRoleDeniedError,
    hide_store_address,
    vault_client_for_operator,
)
from meho_backplane.connectors._shared.vault_creds import (
    SecretRefReadError,
    TargetSecretForbiddenError,
)
from meho_backplane.operations._errors import (
    _sanitize_free_text,
    result_connector_error,
    result_connector_vault_forbidden,
    result_connector_vault_write_forbidden,
    result_vault_credentials_read_error,
)
from meho_backplane.settings import get_settings

_HOST = "marker-store.invalid"
_ADDR = f"https://{_HOST}:8200"
_STORE_TEXT = "marker-store-words"


@pytest.fixture(autouse=True)
def _store_address(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", _ADDR)
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _store_error(cls: type[hvac.exceptions.VaultError] = hvac.exceptions.Forbidden) -> Any:
    return cls(
        _STORE_TEXT,
        method="GET",
        url=f"{_ADDR}/v1/secret/data/tenants/t/app",
        json={"request_id": "req-1"},
    )


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (_strings(k) + _strings(v))]
    if isinstance(value, (list, tuple)):
        return [s for item in value for s in _strings(item)]
    return []


def _assert_clean(result: Any) -> None:
    payload = json.loads(result.model_dump_json())
    for text in _strings(payload):
        assert _HOST not in text
        assert _ADDR not in text
        assert _STORE_TEXT not in text


class _Target:
    name = "app"
    secret_ref = "tenants/t/app"


def _builder_results() -> list[Any]:
    own = TargetSecretForbiddenError(target_name="app", secret_ref="tenants/t/app")
    own.__cause__ = _store_error()
    ref_read = SecretRefReadError(
        param="password_secret_ref", secret_ref="tenants/t/app", cause=_store_error()
    )
    return [
        result_connector_error("op", _store_error(), 1.0),
        result_connector_error("op", _store_error(hvac.exceptions.InternalServerError), 1.0),
        result_connector_vault_forbidden("op", own, _Target(), 1.0, expected_secret_ref="x"),
        result_connector_vault_forbidden("op", _store_error(), _Target(), 1.0),
        result_connector_vault_forbidden("op", _store_error(), None, 1.0),
        result_connector_vault_write_forbidden(
            "vault.kv.put", _store_error(), 1.0, write_path="secret/data/tenants/t/app"
        ),
        result_vault_credentials_read_error("op", ref_read, 1.0),
    ]


def test_no_error_builder_shows_the_store_address_or_text() -> None:
    for result in _builder_results():
        _assert_clean(result)


def test_the_free_text_cleaner_replaces_the_store_address() -> None:
    cleaned = _sanitize_free_text(f"failed, on GET {_ADDR}/v1/x and {_HOST}:8200 and {_HOST}")
    assert _HOST not in cleaned
    assert "<secret store>" in cleaned


def test_hide_store_address_keeps_text_without_an_address() -> None:
    assert hide_store_address("nothing here") == "nothing here"


class _FakeJwt:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def jwt_login(self, **_: Any) -> None:
        raise self.exc


class _FakeClient:
    def __init__(self, exc: Exception) -> None:
        self.auth = type("A", (), {"jwt": _FakeJwt(exc)})()


def _operator() -> Operator:
    return Operator(
        sub="op-1",
        name="Alice",
        email="alice@example.com",
        raw_jwt="fake.jwt.value",
        tenant_id="00000000-0000-0000-0000-00000000a0a0",
        tenant_role="operator",
    )


@pytest.mark.parametrize(
    ("store_exc", "expected_class"),
    [
        (hvac.exceptions.Forbidden, VaultRoleDeniedError),
        (hvac.exceptions.InternalServerError, VaultClientError),
    ],
)
async def test_a_failed_login_gives_a_meho_sentence_and_a_log_event(
    monkeypatch: pytest.MonkeyPatch,
    store_exc: type[hvac.exceptions.VaultError],
    expected_class: type[VaultClientError],
) -> None:
    fake = _FakeClient(_store_error(store_exc))
    monkeypatch.setattr(vault_module, "_build_client", lambda *_a, **_k: fake)

    with capture_logs() as logs, pytest.raises(VaultClientError) as caught:
        async with vault_client_for_operator(_operator()):
            pytest.fail("body must not run when login raises")

    assert type(caught.value) is expected_class
    assert _STORE_TEXT not in str(caught.value)
    assert _HOST not in str(caught.value)

    # The dispatcher answer holds no address and no store text.
    result = result_connector_error("op", caught.value, 1.0)
    _assert_clean(result)
    assert result.extras["message_source"] == "meho"

    # The log has the detail, with the address cut.
    events = [e for e in logs if e["event"] == "vault_login_failed"]
    assert len(events) == 1
    event = events[0]
    assert event["error_class"] == store_exc.__name__
    assert event["store_request_id"] == "req-1"
    assert event["role"]
    assert event["login_mount"]
    assert _STORE_TEXT in event["store_text"]
    assert _HOST not in json.dumps(event, default=str)
