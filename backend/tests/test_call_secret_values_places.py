# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Where and in which order a secret value of a call is hidden.

The value pass runs after the rules that read the vendor's field names and
after MEHO decided the outcome of the call. It covers a text before it is
cut, the fields a handler adds to the audit row, the whole stored trace,
keys and numbers, and the values read from the secret store under the names
the connector reads. All values here are made up.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.parse
import uuid
from typing import Any, ClassVar

import httpx
import pytest
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared import vault_creds
from meho_backplane.flight_recorder import capture
from meho_backplane.operations import _errors
from meho_backplane.redaction.call_secrets import (
    CALL_SECRET_MARKER,
    CallSecrets,
    bind_call_secrets,
    collect_param_secret_values,
    marked_secret_paths,
    reset_call_secrets,
    scrub_call_secrets,
)
from tests._call_secret_dispatch import (
    WRITE_OP,
    http_error,
    operator,
    required_settings_env,
    reset_module_state,
    run_dispatch,
)

__all__ = ["required_settings_env", "reset_module_state"]

_SECRET = "wombat-" + "4410-river-key"
_OTHER = "pelican-" + "2207-stone-word"


def _everything(result: Any, row: Any) -> str:
    return (
        result.model_dump_json()
        + json.dumps(row.payload, default=str)
        + json.dumps(row.raw_payload, default=str)
    )


# ---------------------------------------------------------------------------
# Order: the field-name rules and the outcome come first
# ---------------------------------------------------------------------------


async def test_a_pair_named_like_the_value_still_hides_its_own_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, row = await run_dispatch(
        monkeypatch,
        op_id=WRITE_OP,
        behave=lambda: {"settings": [{"name": "PASSWORD", "value": _OTHER}]},
        params={"password": "PASSWORD"},
    )
    assert result.status == "ok"
    assert _OTHER not in _everything(result, row)


async def _failing_steps(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    return {"status": "made_up_failed", "issues": [{"severity": "error", "message": "no"}]}


async def test_a_value_equal_to_the_failure_mark_keeps_the_call_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, row = await run_dispatch(
        monkeypatch,
        op_id="echodemo.steps.fail",
        behave=lambda: None,
        params={"password": "error"},
        source_kind="composite",
        handler_ref=f"{__name__}._failing_steps",
    )
    assert result.status == "error"
    assert row.payload["result_status"] == "error"


# ---------------------------------------------------------------------------
# A text is cleaned before it is cut
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cap", [_errors._EXC_MESSAGE_CAP, _errors._UPSTREAM_MESSAGE_CAP])
def test_a_value_across_a_cut_leaves_no_part(cap: int) -> None:
    text = "x" * (cap - 5) + _SECRET + " tail"
    token = bind_call_secrets([_SECRET])
    try:
        out = _errors._sanitize_free_text(text, cap=cap)
    finally:
        reset_call_secrets(token)
    assert _SECRET[:5] not in out


def test_a_vault_text_across_the_cut_leaves_no_part() -> None:
    text = "x" * (_errors._EXC_MESSAGE_CAP - 5) + _SECRET
    token = bind_call_secrets([_SECRET])
    try:
        read = _errors.result_connector_vault_forbidden("op", Exception(text), None, 0.0)
        write = _errors.result_connector_vault_write_forbidden(
            "op", Exception(text), 0.0, write_path=None
        )
    finally:
        reset_call_secrets(token)
    for result in (read, write):
        assert _SECRET[:5] not in result.model_dump_json()


async def test_a_long_vendor_text_across_the_cut_leaves_no_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "y" * (_errors._UPSTREAM_MESSAGE_CAP - 4) + _SECRET
    result, row = await run_dispatch(
        monkeypatch,
        op_id=WRITE_OP,
        behave=lambda: http_error(400, body),
        params={"password": _SECRET},
    )
    assert result.status == "error"
    assert _SECRET[:5] not in _everything(result, row)


# ---------------------------------------------------------------------------
# What a handler adds to the audit row
# ---------------------------------------------------------------------------


async def test_a_handler_audit_field_hides_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    def behave() -> Any:
        structlog.contextvars.bind_contextvars(
            audit_file_after=f"key {_SECRET}\n", audit_file_before="empty"
        )
        return {"done": True}

    result, row = await run_dispatch(
        monkeypatch, op_id=WRITE_OP, behave=behave, params={"password": _SECRET}
    )
    assert result.status == "ok"
    assert row.payload["file_after"] == f"key {CALL_SECRET_MARKER}\n"
    assert row.payload["file_before"] == "empty"


# ---------------------------------------------------------------------------
# The whole stored span
# ---------------------------------------------------------------------------


def test_the_whole_saved_span_hides_the_value() -> None:
    scope = capture._CaptureScope(audit_id=uuid.uuid4(), tenant_id=uuid.uuid4(), target_id=None)
    request = httpx.Request("POST", f"https://appliance.example.test/api/{_SECRET}/x")
    response = httpx.Response(
        400,
        content=b"plain text",
        headers={"content-type": f"text/{_SECRET}"},
        request=request,
    )
    scope_token = capture._active_capture_var.set(scope)
    token = bind_call_secrets([_SECRET])
    try:
        start = capture.span_start()
        capture.record_vendor_call(start, method="POST", request_headers={}, response=response)
    finally:
        reset_call_secrets(token)
        capture._active_capture_var.reset(scope_token)
    assert len(scope.spans) == 1
    saved = scope.spans[0]
    stored = json.dumps(
        {"name": saved.name, "status": saved.status, "attributes": saved.attributes},
        default=str,
    )
    assert _SECRET not in stored
    assert CALL_SECRET_MARKER in saved.name


# ---------------------------------------------------------------------------
# Which values are known
# ---------------------------------------------------------------------------


def test_a_mark_holds_only_at_its_own_path() -> None:
    schema = {
        "type": "object",
        "properties": {
            "login": {"properties": {"value": {"type": "string", "writeOnly": True}}},
            "display": {"properties": {"value": {"type": "string"}}},
        },
    }
    marked = marked_secret_paths(schema)
    assert marked == frozenset({("login", "value")})
    params = {"login": {"value": _SECRET}, "display": {"value": "public-name-text"}}
    assert set(collect_param_secret_values(params, marked_paths=marked)) == {_SECRET}


async def test_a_public_value_stays_visible_when_the_vendor_repeats_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = {
        "type": "object",
        "properties": {
            "login": {"properties": {"value": {"type": "string", "writeOnly": True}}},
            "display": {"properties": {"value": {"type": "string"}}},
        },
    }
    result, _row = await run_dispatch(
        monkeypatch,
        op_id=WRITE_OP,
        behave=lambda: {"note": "saved public-name-text"},
        params={"login": {"value": _SECRET}, "display": {"value": "public-name-text"}},
        schema=schema,
    )
    assert result.result == {"note": "saved public-name-text"}


async def test_a_request_pair_with_a_secret_name_is_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params = {"settings": [{"name": "PASSWORD", "value": _SECRET}, {"name": "USER", "value": "u"}]}
    assert set(collect_param_secret_values(params)) == {_SECRET}
    result, row = await run_dispatch(
        monkeypatch,
        op_id=WRITE_OP,
        behave=lambda: http_error(400, {"message": f"bad setting {_SECRET}"}),
        params=params,
    )
    assert result.status == "error"
    assert _SECRET not in _everything(result, row)


class _MappedBackend:
    async def load_secret_data(self, ref: str, operator: Any, **kwargs: Any) -> dict[str, object]:
        return {"user": "svc-user-name", "pw": f"{_SECRET}\n"}


class _MappedTarget:
    name = "appliance-a"
    host = "appliance.example.test"
    secret_ref = "fakekind:targets/appliance-a"
    secret_field_map: ClassVar[dict[str, str]] = {"username": "user", "password": "pw"}


async def test_a_mapped_and_trimmed_store_value_is_known(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault_creds, "resolve_credential_backend", lambda kind: _MappedBackend())
    monkeypatch.setattr(
        vault_creds, "get_settings", lambda: type("S", (), {"credential_backend": "vault"})()
    )
    token = bind_call_secrets([])
    try:
        creds = await vault_creds.load_basic_credentials(_MappedTarget(), operator())  # type: ignore[arg-type]
        assert creds["password"] == _SECRET
        out = scrub_call_secrets(f"sent {creds['password']}; user svc-user-name")
    finally:
        reset_call_secrets(token)
    assert out == f"sent {CALL_SECRET_MARKER}; user svc-user-name"


# ---------------------------------------------------------------------------
# Keys, numbers and encoded forms
# ---------------------------------------------------------------------------


def test_a_value_as_a_key_or_a_number_is_hidden() -> None:
    scope = CallSecrets([_SECRET, "90817263"])
    out = scope.scrub_tree({_SECRET: 1, f"x-{_SECRET}": 2, "pin": 90817263, "n": 5, "f": True})
    assert out == {
        CALL_SECRET_MARKER: 1,
        f"x-{CALL_SECRET_MARKER}": 2,
        "pin": CALL_SECRET_MARKER,
        "n": 5,
        "f": True,
    }


def test_two_keys_that_become_the_mark_keep_both_entries() -> None:
    out = CallSecrets([_SECRET, _OTHER]).scrub_tree({_SECRET: "a", _OTHER: "b"})
    assert sorted(out.values()) == ["a", "b"]
    assert all(CALL_SECRET_MARKER in key for key in out)


@pytest.mark.parametrize(
    "value", ['quo"te-ünï', "dir/with space", 'a/b "c" é'], ids=["quote", "slash", "all"]
)
def test_independently_encoded_forms_are_hidden(value: str) -> None:
    scope = CallSecrets([value])
    encoded = [
        json.dumps(json.dumps(value, ensure_ascii=False)[1:-1], ensure_ascii=False)[1:-1],
        json.dumps(json.dumps(value)[1:-1])[1:-1],
        json.dumps(json.dumps(value, ensure_ascii=False)[1:-1])[1:-1],
        urllib.parse.quote(value),
        urllib.parse.quote_plus(value, safe="/"),
        base64.b64encode(value.encode()).decode(),
    ]
    for form in encoded:
        assert scope.scrub(f"<{form}>") == f"<{CALL_SECRET_MARKER}>", form


# ---------------------------------------------------------------------------
# The matcher
# ---------------------------------------------------------------------------


def test_the_values_leave_the_pattern_cache_with_the_call() -> None:
    token = bind_call_secrets([_SECRET])
    try:
        assert scrub_call_secrets(_SECRET) == CALL_SECRET_MARKER
    finally:
        reset_call_secrets(token)
    cached = [*getattr(re, "_cache", {}), *getattr(re, "_cache2", {})]
    assert not [key for key in cached if _SECRET in repr(key)]


def test_many_repeated_and_overlapping_hits_leave_no_rest() -> None:
    scope = CallSecrets(["abcdXYZ1", "XYZ1efgh", "aaaa"])
    text = "abcdXYZ1efgh " * 2000 + "a" * 50 + " end"
    out = scope.scrub(text)
    assert out == f"{CALL_SECRET_MARKER} " * 2000 + f"{CALL_SECRET_MARKER} end"
