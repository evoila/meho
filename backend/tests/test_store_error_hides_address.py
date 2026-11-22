# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A secret store error shows the caller no store address and no store text."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import hvac.exceptions
import pytest
from structlog.testing import capture_logs

from meho_backplane.auth import vault as vault_module
from meho_backplane.auth.operator import Operator
from meho_backplane.auth.store_errors import (
    hide_store_address,
    log_without_store_text,
    store_text_for_log,
)
from meho_backplane.auth.vault import (
    VaultClientError,
    VaultRoleDeniedError,
    vault_client_for_operator,
)
from meho_backplane.connectors._shared.vault_creds import (
    SecretRefReadError,
    TargetSecretForbiddenError,
    TargetSecretNotFoundError,
)
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.connectors.vault.ops_identity_oidc import _key_in_use_error
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._errors import (
    _sanitize_free_text,
    result_connector_error,
    result_connector_vault_forbidden,
    result_connector_vault_write_forbidden,
    result_vault_credentials_read_error,
)
from meho_backplane.operations._preview import preview_failure_reason
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings

_HOST = "marker-store.invalid"
_ADDR = f"https://{_HOST}:8200"
_STORE_TEXT = "marker-store-words"
_PASSWORD = "made-up-" + "pass-4411"
_SHORT_PASSWORD = "pw" + "41"
_TOKEN = "hvs." + "MADEUP" * 5
_OTHER_TOKEN = "made-up-" + "token-7722"
_SECRET_VALUES = (_PASSWORD, _SHORT_PASSWORD, _TOKEN, _OTHER_TOKEN)
#: What a store can write: its words, the address and secret values in several shapes.
_STORE_MESSAGE = (
    f"{_STORE_TEXT} at {_ADDR}: "
    f'{{"password": "{_PASSWORD}"}}; password={_SHORT_PASSWORD}; '
    f"invalid token {_TOKEN}; token: {_OTHER_TOKEN}"
)


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
        _STORE_MESSAGE,
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
        for value in _SECRET_VALUES:
            assert value not in text


def _assert_log_is_clean(event: Any) -> None:
    assert _STORE_TEXT in event["store_text"]
    text = json.dumps(event, default=str)
    assert _HOST not in text
    for value in _SECRET_VALUES:
        assert value not in text


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


def test_a_one_word_host_is_replaced_only_where_it_is_an_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
    get_settings.cache_clear()
    for kept in ("vault unreachable", "vault_role is empty", "read vault:tenants/t/app"):
        assert hide_store_address(kept) == kept
    for address in ("http://vault/v1/x", "vault:8200", "HTTPConnectionPool(host='vault')"):
        assert "<secret store>" in hide_store_address(f"failed at {address}")


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
    _assert_log_is_clean(event)
    assert caught.value.store_text == event["store_text"]
    # A traceback of the login error does not show the client's exception.
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    ("template", "value"),
    [
        ('bad password "{}"', _SHORT_PASSWORD),
        ("1 error occurred:\n\t* invalid password: {}\n\n", _SHORT_PASSWORD),
        ("password: {}", _SHORT_PASSWORD),
        ("client_secret={}", _SHORT_PASSWORD),
        ("bad token: {} for this mount", "ab12"),
        ("token='{}'", _SHORT_PASSWORD),
        ("token={}", "abc"),
    ],
)
def test_a_secret_in_the_store_text_is_cut_from_the_log(template: str, value: str) -> None:
    logged = store_text_for_log(hvac.exceptions.InvalidRequest(template.format(value)))
    assert value not in logged
    assert "REDACTED" in logged


@pytest.mark.parametrize(
    "text",
    ["permission denied", "error validating token: invalid audience (aud) claim"],
)
def test_the_log_keeps_the_stores_diagnosis(text: str) -> None:
    assert store_text_for_log(hvac.exceptions.Forbidden(text)) == text


def test_the_log_takes_empty_and_withholds_oversized_store_text() -> None:
    assert store_text_for_log(hvac.exceptions.Forbidden()) == ""
    assert store_text_for_log(hvac.exceptions.Forbidden("x" * 9000)) == "<store text withheld>"


def test_a_wrapper_that_repeats_a_store_error_shows_the_meho_sentence() -> None:
    store = _store_error(hvac.exceptions.InternalServerError)
    try:
        try:
            raise store
        except hvac.exceptions.VaultError as exc:
            raise RuntimeError(f"the step failed: {exc}. Check the user") from exc
    except RuntimeError as exc:
        wrapped = exc
    result = result_connector_error("op", wrapped, 1.0)
    _assert_clean(result)
    assert result.extras["exception_message"] == (
        "the step failed: The secret store returned an error.. Check the user"
    )


def test_an_approval_preview_shows_no_store_text() -> None:
    reason = preview_failure_reason(_store_error(hvac.exceptions.InternalServerError))
    assert reason == "InternalServerError: The secret store returned an error."


def test_a_key_in_use_answer_shows_no_store_text() -> None:
    store = hvac.exceptions.InvalidRequest(
        errors=['unable to delete key "k" because it is currently referenced by these roles: r1']
    )
    error = _key_in_use_error("k", store)
    assert error is not None
    result = result_connector_error("op", error, 1.0)
    assert "currently referenced" not in result.model_dump_json()
    assert result.extras["upstream_message"] == "key 'k' is still used by these roles: r1"


def test_a_caller_can_log_a_store_failure_without_the_store_text() -> None:
    with capture_logs() as logs, log_without_store_text():
        result_connector_vault_forbidden("op", _store_error(), None, 1.0)
    events = [e for e in logs if e["event"] == "vault_request_failed"]
    assert len(events) == 1
    assert events[0]["store_text"] is None
    assert events[0]["store_request_id"] == "req-1"
    assert _STORE_TEXT not in json.dumps(events, default=str)


def test_the_store_error_builders_set_no_message_source() -> None:
    for result in _builder_results():
        assert "message_source" not in result.extras


def test_a_refusal_with_no_target_names_the_path_in_the_error_only() -> None:
    result = result_connector_vault_forbidden("op", _store_error(), None, 1.0)
    assert result.error is not None
    assert "'secret/data/tenants/t/app'" in result.error
    assert set(result.extras) == {
        "error_code",
        "secret_ref",
        "secret_ref_derived",
        "expected_secret_ref",
        "exception_class",
        "exception_message",
    }


# ---------------------------------------------------------------------------
# Through the dispatcher: each failure path logs the cleaned detail
# ---------------------------------------------------------------------------


class _StoreConnector(Connector):
    product = "storefail"
    version = "1.x"
    impl_id = "storefail"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


async def _raise_store_failure(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    case = params.get("case", "refused")
    if case == "failed":
        raise _store_error(hvac.exceptions.InternalServerError)
    if case == "own_ref":
        own = TargetSecretForbiddenError(target_name="app", secret_ref="tenants/t/app")
        raise own from _store_error()
    if case == "own_ref_missing":
        missing = TargetSecretNotFoundError(target_name="app", secret_ref="tenants/t/app")
        raise missing from _store_error(hvac.exceptions.InvalidPath)
    if case == "param_ref":
        cause = _store_error()
        raise SecretRefReadError(
            param="password_secret_ref", secret_ref="tenants/t/app", cause=cause
        ) from cause
    raise _store_error()


@pytest.fixture
def _clean_dispatcher() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.mark.usefixtures("_clean_dispatcher")
@pytest.mark.parametrize(
    ("product", "op_id", "case", "code", "status"),
    [
        ("storefail", "storefail.item.read", "refused", "connector_vault_forbidden", 403),
        ("storefail", "storefail.item.read", "own_ref", "connector_vault_forbidden", 403),
        ("storefail", "storefail.item.read", "own_ref_missing", "target_secret_not_found", 404),
        ("storefail", "storefail.item.read", "param_ref", "vault_credentials_read_error", 403),
        ("storefail", "storefail.item.read", "failed", "connector_error", 500),
        ("vault", "vault.kv.put", "refused", "vault_write_identity_forbidden", 403),
    ],
)
async def test_the_dispatcher_logs_a_failed_store_request_and_answers_clean(
    product: str, op_id: str, case: str, code: str, status: int
) -> None:
    connector = type("_Registered", (_StoreConnector,), {"product": product, "impl_id": product})
    register_connector_v2(product=product, version="", impl_id="", cls=connector)
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    await register_typed_operation(
        product=product,
        version="1.x",
        impl_id=product,
        op_id=op_id,
        handler=_raise_store_failure,
        summary="Fail at the store.",
        description="The handler raises a store error.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=service,
    )

    with capture_logs() as logs:
        result = await dispatch(
            operator=_operator(),
            connector_id=f"{product}-1.x",
            op_id=op_id,
            target=None,
            params={"case": case, "path": "tenants/t/app"},
        )

    assert result.extras["error_code"] == code, result
    _assert_clean(result)
    events = [e for e in logs if e["event"] == "vault_request_failed"]
    assert len(events) == 1, logs
    assert events[0]["op_id"] == op_id
    assert events[0]["store_request_id"] == "req-1"
    assert events[0]["store_status"] == status
    _assert_log_is_clean(events[0])
