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
from collections.abc import Callable
from typing import Any

import pytest

from meho_backplane.connectors._shared import credential_backend, vault_creds
from meho_backplane.flight_recorder import capture
from meho_backplane.flight_recorder.store import SpanInput
from meho_backplane.redaction import call_secrets
from meho_backplane.redaction.call_secrets import (
    CALL_SECRET_MARKER,
    CallSecrets,
    bind_call_secrets,
    collect_param_secret_values,
    marked_secret_paths,
    reset_call_secrets,
    scrub_call_secrets,
)
from meho_backplane.redaction.flight_recorder import redact_span
from tests._call_secret_dispatch import (
    READ_OP,
    WRITE_OP,
    http_error,
    operator,
    required_settings_env,
    reset_module_state,
    run_dispatch,
)

__all__ = ["required_settings_env", "reset_module_state"]

_SECRET = "quokka-" + "7781-zebra-pass"


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
    real = CallSecrets.scrub

    def counting(self: CallSecrets, text: str) -> str:
        nonlocal searches
        searches += 1
        return real(self, text)

    monkeypatch.setattr(CallSecrets, "scrub", counting)
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
            "items": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}},
        },
        "components": {
            "schemas": {"Item": {"properties": {"seed": {"type": "string", "writeOnly": True}}}}
        },
    }
    marked = marked_secret_paths(schema)
    assert marked == frozenset({("seed",), ("other",), ("items", "seed")})
    params = {
        "plain": "keep-this-one",
        "password": "flat-secret-1",
        "body": {"nested": {"api_key": "nested-secret-2", "label": "visible"}},
        "items": [{"seed": "marked-secret-3"}],
        "flag": True,
        "other": 123456,
    }
    values = set(collect_param_secret_values(params, marked_paths=marked))
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


def _scope() -> Any:
    return capture._CaptureScope(audit_id=uuid.uuid4(), tenant_id=uuid.uuid4(), target_id=None)


def test_the_stored_trace_holds_no_value_of_the_call() -> None:
    scope = _scope()
    token = bind_call_secrets([_SECRET])
    try:
        span = redact_span(
            op_id=READ_OP,
            method="POST",
            request_body={"note": "ok"},
            response_body={
                "message": f"bad value {_SECRET}",
                "errors": [{"x": _forms(_SECRET)["percent"]}],
            },
            response_content_type="application/json",
        )
        scope.add(
            SpanInput(
                span_kind="vendor_call",
                name="POST /api",
                started_at=capture.now_utc(),
                attributes={"response_body": span.response_body},
            )
        )
    finally:
        reset_call_secrets(token)
    stored = json.dumps(scope.spans[0].attributes)
    assert _SECRET not in stored
    assert _forms(_SECRET)["percent"] not in stored
    assert CALL_SECRET_MARKER in stored


def test_the_trace_is_unchanged_without_a_value() -> None:
    body = {"message": "bad value", "n": 3}
    span = redact_span(
        op_id=READ_OP, method="POST", response_body=body, response_content_type="application/json"
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
    monkeypatch.setattr(vault_creds, "get_settings", type("S", (), {"credential_backend": "vault"}))
    assert credential_backend is not None
    token = bind_call_secrets([])
    try:
        creds = await vault_creds.load_basic_credentials(_StoreTarget(), operator())  # type: ignore[arg-type]
        assert creds["password"] == _SECRET
        out = scrub_call_secrets(f"{_SECRET} service-account-user store-note-text")
    finally:
        reset_call_secrets(token)
    # Only a secret-named field is known: the user name and the note stay.
    assert out == f"{CALL_SECRET_MARKER} service-account-user store-note-text"


# ---------------------------------------------------------------------------
# Through the dispatcher
# ---------------------------------------------------------------------------


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    op_id: str,
    behave: Callable[[], Any],
    params: dict[str, Any],
    schema: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    result, row = await run_dispatch(
        monkeypatch, op_id=op_id, behave=behave, params=params, schema=schema
    )
    return result, {"payload": row.payload, "raw_payload": row.raw_payload}


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
    ("403-json", lambda: http_error(403, _echo_body())),
    ("400-json", lambda: http_error(400, _echo_body())),
    ("403-text", lambda: http_error(403, f"Forbidden: {_SECRET} / {_forms(_SECRET)['percent']}")),
]


@pytest.mark.parametrize(("_shape", "params"), _PARAM_SHAPES, ids=[n for n, _ in _PARAM_SHAPES])
@pytest.mark.parametrize(("_name", "fail"), _FAILS, ids=[n for n, _ in _FAILS])
async def test_an_error_that_repeats_the_value_shows_it_nowhere(
    monkeypatch: pytest.MonkeyPatch,
    _shape: str,
    params: dict[str, Any],
    _name: str,
    fail: Callable[[], Any],
) -> None:
    result, payload = await _run(
        monkeypatch, op_id=WRITE_OP, behave=fail, params=params, schema=_SCHEMA
    )
    assert result.status == "error"
    text = _everything(result, payload)
    assert _SECRET not in text
    assert _forms(_SECRET)["percent"] not in text
    assert _forms(_SECRET)["json"] == _SECRET or _forms(_SECRET)["json"] not in text
    assert CALL_SECRET_MARKER in text


async def test_a_query_value_under_a_secret_name_is_hidden_on_a_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, payload = await _run(
        monkeypatch,
        op_id=READ_OP,
        behave=lambda: http_error(400, _echo_body()),
        params={"api_key": _SECRET},
    )
    assert result.status == "error"
    assert _SECRET not in _everything(result, payload)


async def test_a_success_answer_that_repeats_the_value_shows_it_nowhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, payload = await _run(
        monkeypatch,
        op_id=WRITE_OP,
        behave=lambda: {"echo": {"note": f"saved {_SECRET}"}, "id": 7},
        params={"password": _SECRET},
    )
    assert result.status == "ok"
    assert _SECRET not in _everything(result, payload)
    assert result.result == {"echo": {"note": f"saved {CALL_SECRET_MARKER}"}, "id": 7}


async def test_an_answer_with_no_secret_value_is_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = {"items": [{"name": "alpha", "size": 3}], "note": "all good"}
    result, _ = await _run(
        monkeypatch,
        op_id=WRITE_OP,
        behave=lambda: dict(answer),
        params={"password": _SECRET},
    )
    assert result.result == answer


async def test_a_mapping_under_a_secret_name_is_walked_by_its_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, _ = await _run(
        monkeypatch,
        op_id=WRITE_OP,
        behave=lambda: {"stored_path": "labs/gen-user", "note": f"saved {_SECRET}"},
        params={"generate_password": {"path": "labs/gen-user", "password": _SECRET}},
    )
    assert result.status == "ok"
    assert result.result == {"stored_path": "labs/gen-user", "note": f"saved {CALL_SECRET_MARKER}"}


def test_only_scalars_under_a_secret_name_are_known_values() -> None:
    params = {
        "generate_password": {"path": "labs/gen-user"},
        "secrets": ["list-secret-1", "list-secret-2"],
        "client_secret": {"value": "inner-plain"},
        "wrapper": {"password": "inner-secret-3"},
    }
    values = set(collect_param_secret_values(params))
    assert "labs/gen-user" not in values
    assert "inner-plain" not in values
    assert {"list-secret-1", "list-secret-2", "inner-secret-3"} <= values
