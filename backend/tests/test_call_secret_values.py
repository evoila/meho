# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A secret value of a call is hidden wherever the vendor repeats it.

MEHO knows the secret values it sends: the params under a secret name or a
field the schema marks as secret, and the values it reads from the secret
store for the call. A vendor text that repeats such a value is cleaned by
value, in the answer, the audit row and the stored trace. All values here are
made up and are not secret-shaped, so only the rule under test can hide them.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared import credential_backend, vault_creds
from meho_backplane.connectors.adapters import HttpConnector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance
from meho_backplane.redaction import call_secrets
from meho_backplane.redaction.call_secrets import (
    CALL_SECRET_MARKER,
    CallSecrets,
    bind_call_secrets,
    collect_param_secret_values,
    marked_secret_names,
    reset_call_secrets,
    scrub_call_secrets,
)
from meho_backplane.redaction.flight_recorder import redact_span
from meho_backplane.settings import get_settings

_SECRET = "quokka-" + "7781-zebra-pass"
_PRODUCT = "echodemo"
_VERSION = "1"
_TENANT = UUID("00000000-0000-0000-0000-00000000b787")
_WRITE_OP = "POST:/api/v1/accounts"
_READ_OP = "GET:/api/v1/accounts"


def _forms(value: str) -> dict[str, str]:
    return {
        "plain": value,
        "json": json.dumps(value)[1:-1],
        "json-ascii-off": json.dumps(value, ensure_ascii=False)[1:-1],
        "percent": urllib.parse.quote(value, safe=""),
        "percent-plus": urllib.parse.quote_plus(value),
        "base64": base64.b64encode(value.encode()).decode(),
        "base64-url": base64.urlsafe_b64encode(value.encode()).decode(),
        "base64-bare": base64.b64encode(value.encode()).decode().rstrip("="),
    }


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("form", list(_forms(_SECRET)))
def test_every_form_of_the_value_is_hidden(form: str) -> None:
    text = f"upstream said: {_forms(_SECRET)[form]} is not valid"
    assert CallSecrets([_SECRET]).scrub(text) == f"upstream said: {CALL_SECRET_MARKER} is not valid"


ODD_VALUES = [
    "abcd",
    "abcde",
    "x" * 5000,
    'has "quote" inside',
    "back\\slash\\value",
    "line\nbreak\r\nvalue",
    "pässwörd-日本語-ключ",
    "password",
    "with/slash+plus=eq&amp",
]


@pytest.mark.parametrize("value", ODD_VALUES, ids=[f"odd-{i}" for i in range(len(ODD_VALUES))])
def test_odd_values_are_hidden_in_each_form(value: str) -> None:
    scope = CallSecrets([value])
    for form, shown in _forms(value).items():
        out = scope.scrub(f"[{shown}]")
        assert out == f"[{CALL_SECRET_MARKER}]", form
    assert scope.scrub(f"a{value}b{value}c") == f"a{CALL_SECRET_MARKER}b{CALL_SECRET_MARKER}c"


@pytest.mark.parametrize("value", ["", "a", "abc"])
def test_a_value_under_four_characters_is_not_known(value: str) -> None:
    scope = CallSecrets([value])
    assert not scope
    assert scope.scrub("abc abc a") == "abc abc a"


def test_two_values_where_one_is_part_of_the_other_leave_no_rest() -> None:
    scope = CallSecrets(["hunter22", "hunter2"])
    assert (
        scope.scrub("x hunter22 y hunter2 z") == f"x {CALL_SECRET_MARKER} y {CALL_SECRET_MARKER} z"
    )


def test_two_values_that_overlap_leave_no_rest() -> None:
    scope = CallSecrets(["abcdXYZ1", "XYZ1efgh"])
    assert scope.scrub("abcdXYZ1efgh") == CALL_SECRET_MARKER


def test_a_text_with_no_secret_value_is_returned_as_it_is() -> None:
    text = "403 Forbidden: the account is locked"
    assert CallSecrets([_SECRET]).scrub(text) == text


def test_the_tree_walk_hides_in_every_string_and_keeps_the_rest() -> None:
    tree = {"a": [f"x {_SECRET}", 5, None, {"b": _SECRET}], "c": (f"{_SECRET}",), "n": 1.5}
    out = CallSecrets([_SECRET]).scrub_tree(tree)
    assert out == {
        "a": [f"x {CALL_SECRET_MARKER}", 5, None, {"b": CALL_SECRET_MARKER}],
        "c": (CALL_SECRET_MARKER,),
        "n": 1.5,
    }


def test_outside_a_call_nothing_is_hidden() -> None:
    assert scrub_call_secrets(_SECRET) == _SECRET


def test_a_call_with_no_secret_value_does_no_scanning(monkeypatch: pytest.MonkeyPatch) -> None:
    searches = 0
    real = CallSecrets.has_value_in

    def counting(self: CallSecrets, text: str) -> bool:
        nonlocal searches
        searches += 1
        return real(self, text)

    monkeypatch.setattr(CallSecrets, "has_value_in", counting)
    token = bind_call_secrets(collect_param_secret_values({"name": "demo", "size": 3}))
    try:
        out = call_secrets.scrub_call_secrets_tree({"a": ["x", "y"], "b": "z"})
    finally:
        reset_call_secrets(token)
    assert out == {"a": ["x", "y"], "b": "z"}
    assert searches == 0


def test_the_values_come_from_secret_names_and_marked_fields() -> None:
    schema = {
        "type": "object",
        "properties": {
            "seed": {"type": "string", "writeOnly": True},
            "other": {"type": "string", "format": "password"},
            "plain": {"type": "string"},
        },
    }
    marked = marked_secret_names(schema)
    assert marked == frozenset({"seed", "other"})
    params = {
        "plain": "keep-this-one",
        "password": "flat-secret-1",
        "body": {"nested": {"api_key": "nested-secret-2", "label": "visible"}},
        "items": [{"seed": "marked-secret-3"}],
        "flag": True,
        "other": 123456,
    }
    values = set(collect_param_secret_values(params, marked_names=marked))
    assert values == {"flat-secret-1", "nested-secret-2", "marked-secret-3", "123456"}


def test_a_step_starts_with_the_values_of_its_parent() -> None:
    parent = bind_call_secrets([_SECRET])
    try:
        child = bind_call_secrets(["other-secret-value"])
        try:
            assert scrub_call_secrets(f"{_SECRET} other-secret-value") == (
                f"{CALL_SECRET_MARKER} {CALL_SECRET_MARKER}"
            )
        finally:
            reset_call_secrets(child)
        assert scrub_call_secrets("other-secret-value") == "other-secret-value"
    finally:
        reset_call_secrets(parent)


# ---------------------------------------------------------------------------
# The stored trace
# ---------------------------------------------------------------------------


def test_the_stored_trace_holds_no_value_of_the_call() -> None:
    token = bind_call_secrets([_SECRET])
    try:
        span = redact_span(
            op_id=_READ_OP,
            method="POST",
            request_body={"note": "ok"},
            response_body={
                "message": f"bad value {_SECRET}",
                "errors": [{"x": _forms(_SECRET)["percent"]}],
            },
            response_content_type="application/json",
        )
    finally:
        reset_call_secrets(token)
    assert _SECRET not in json.dumps(span.response_body)
    assert _forms(_SECRET)["percent"] not in json.dumps(span.response_body)
    assert CALL_SECRET_MARKER in json.dumps(span.response_body)


def test_the_trace_is_unchanged_without_a_value() -> None:
    body = {"message": "bad value", "n": 3}
    span = redact_span(
        op_id=_READ_OP, method="POST", response_body=body, response_content_type="application/json"
    )
    assert span.response_body == body


# ---------------------------------------------------------------------------
# A value read from the secret store
# ---------------------------------------------------------------------------


class _FakeBackend:
    async def load_secret_data(self, ref: str, operator: Any, **kwargs: Any) -> dict[str, object]:
        return {"username": "service-account-user", "password": _SECRET, "note": "store-note-text"}


class _StoreTarget:
    name = "appliance-a"
    host = "appliance.example.test"
    secret_ref = "fakekind:targets/appliance-a"


async def test_values_read_from_the_store_are_known_for_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vault_creds, "resolve_credential_backend", lambda kind: _FakeBackend())
    monkeypatch.setattr(
        vault_creds, "get_settings", lambda: type("S", (), {"credential_backend": "vault"})()
    )
    assert credential_backend is not None
    token = bind_call_secrets([])
    try:
        creds = await vault_creds.load_basic_credentials(_StoreTarget(), _operator())  # type: ignore[arg-type]
        assert creds["password"] == _SECRET
        out = scrub_call_secrets(f"{_SECRET} service-account-user store-note-text")
    finally:
        reset_call_secrets(token)
    # Only a secret-named field is known: the user name and the note stay.
    assert out == f"{CALL_SECRET_MARKER} service-account-user store-note-text"


# ---------------------------------------------------------------------------
# Through the dispatcher
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


def _http_error(status: int, body: Any) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://appliance.example.test/api/v1/accounts")
    if isinstance(body, str):
        response = httpx.Response(status, text=body, request=request)
    else:
        response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class _EchoConnector(HttpConnector):
    """Every call fails with, or answers with, what :attr:`behave` returns."""

    product = _PRODUCT
    accepts_ingested_operations = True
    version = _VERSION
    impl_id = "echodemo-rest"
    supported_version_range = ">=1,<2"
    priority = 1

    behave: ClassVar[Callable[[], Any]]

    def _go(self) -> Any:
        outcome = type(self).behave()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def _request_json(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        return self._go()

    async def _post_json(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        return self._go()

    async def fingerprint(  # type: ignore[override]
        self, target: Any, operator: Operator | None = None
    ) -> FingerprintResult:
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(  # type: ignore[override]
        self, target: Any, op_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        raise NotImplementedError


class _Fingerprint:
    version = _VERSION


class _DispatchTarget:
    def __init__(self) -> None:
        self.product = _PRODUCT
        self.fingerprint = _Fingerprint()
        self.preferred_impl_id: str | None = None
        self.id = uuid.uuid4()
        self.name = "appliance-a"
        self.host = "appliance.example.test"
        self.port = 443
        self.auth_model = "shared_service_account"
        self.secret_ref: str | None = None


def _operator() -> Operator:
    return Operator(
        sub="op-echo",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


async def _seed(session: AsyncSession, op_id: str, method: str, schema: dict[str, Any]) -> None:
    session.add(
        EndpointDescriptor(
            id=uuid.uuid4(),
            tenant_id=None,
            product=_PRODUCT,
            version=_VERSION,
            impl_id=_EchoConnector.impl_id,
            op_id=op_id,
            source_kind="ingested",
            method=method,
            path="/api/v1/accounts",
            handler_ref=None,
            summary="Made-up op.",
            description="Made-up op with no masking code of its own.",
            tags=["accounts"],
            parameter_schema=schema,
            response_schema=None,
            llm_instructions=None,
            safety_level="safe",
            requires_approval=False,
            is_enabled=True,
            embedding=[0.1] * 384,
            custom_description=None,
            custom_notes=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def _run(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    op_id: str,
    behave: Callable[[], Any],
    params: dict[str, Any],
    schema: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    monkeypatch.setattr(_EchoConnector, "behave", staticmethod(behave), raising=False)
    register_connector_v2(
        product=_PRODUCT, version=_VERSION, impl_id=_EchoConnector.impl_id, cls=_EchoConnector
    )
    method = "GET" if op_id.startswith("GET") else "POST"
    await _seed(session, op_id, method, schema or {"type": "object", "additionalProperties": True})
    result = await dispatch(
        operator=_operator(),
        connector_id=f"{_EchoConnector.impl_id}-{_VERSION}",
        op_id=op_id,
        target=_DispatchTarget(),
        params=params,
    )
    assert get_or_create_connector_instance(_EchoConnector) is not None
    async with get_sessionmaker()() as fresh:
        rows = (await fresh.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    assert len(rows) == 1
    return result, rows[0].payload


def _everything(result: Any, payload: dict[str, Any]) -> str:
    return result.model_dump_json() + json.dumps(payload, default=str)


def _echo_body() -> dict[str, Any]:
    return {
        "message": f"the value {_SECRET} is not allowed",
        "errors": [{"field": "x", "detail": _forms(_SECRET)["json"]}, {"other": _SECRET}],
        "again": _forms(_SECRET)["percent"],
    }


_PARAM_SHAPES: list[tuple[str, dict[str, Any]]] = [
    ("flat", {"password": _SECRET}),
    ("nested", {"body": {"auth": {"password": _SECRET}}}),
    ("marked", {"seed": _SECRET}),
]
_SCHEMA = {
    "type": "object",
    "properties": {"seed": {"type": "string", "writeOnly": True}},
    "additionalProperties": True,
}
_FAILS: list[tuple[str, Callable[[], Any]]] = [
    ("403-json", lambda: _http_error(403, _echo_body())),
    ("400-json", lambda: _http_error(400, _echo_body())),
    ("403-text", lambda: _http_error(403, f"Forbidden: {_SECRET} / {_forms(_SECRET)['percent']}")),
]


@pytest.mark.parametrize(("_shape", "params"), _PARAM_SHAPES, ids=[n for n, _ in _PARAM_SHAPES])
@pytest.mark.parametrize(("_name", "fail"), _FAILS, ids=[n for n, _ in _FAILS])
async def test_an_error_that_repeats_the_value_shows_it_nowhere(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    _shape: str,
    params: dict[str, Any],
    _name: str,
    fail: Callable[[], Any],
) -> None:
    result, payload = await _run(
        session, monkeypatch, op_id=_WRITE_OP, behave=fail, params=params, schema=_SCHEMA
    )
    assert result.status == "error"
    text = _everything(result, payload)
    assert _SECRET not in text
    assert _forms(_SECRET)["percent"] not in text
    assert _forms(_SECRET)["json"] == _SECRET or _forms(_SECRET)["json"] not in text
    assert CALL_SECRET_MARKER in text


async def test_a_query_value_under_a_secret_name_is_hidden_on_a_read(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, payload = await _run(
        session,
        monkeypatch,
        op_id=_READ_OP,
        behave=lambda: _http_error(400, _echo_body()),
        params={"api_key": _SECRET},
    )
    assert result.status == "error"
    assert _SECRET not in _everything(result, payload)


async def test_a_success_answer_that_repeats_the_value_shows_it_nowhere(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, payload = await _run(
        session,
        monkeypatch,
        op_id=_WRITE_OP,
        behave=lambda: {"echo": {"note": f"saved {_SECRET}"}, "id": 7},
        params={"password": _SECRET},
    )
    assert result.status == "ok"
    assert _SECRET not in _everything(result, payload)
    assert result.result == {"echo": {"note": f"saved {CALL_SECRET_MARKER}"}, "id": 7}


async def test_an_answer_with_no_secret_value_is_the_same(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = {"items": [{"name": "alpha", "size": 3}], "note": "all good"}
    result, _ = await _run(
        session,
        monkeypatch,
        op_id=_WRITE_OP,
        behave=lambda: dict(answer),
        params={"password": _SECRET},
    )
    assert result.result == answer
