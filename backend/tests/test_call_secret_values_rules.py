# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A secret value of a call next to the other rules of the answer.

The value stays whole when a policy rule replaces only part of it, a value
used as a key is masked in the redaction manifest too, MEHO's own field
names stay readable, schema marks hold at their own place, connector texts
are cleaned before they are cut, and masked keys never replace another
entry. All values here are made up.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.azure._errors import _MESSAGE_CAP as _AZURE_CAP
from meho_backplane.connectors.azure._errors import clean_azure_text
from meho_backplane.connectors.phpipam._errors import _MESSAGE_CAP as _PHPIPAM_CAP
from meho_backplane.connectors.phpipam._errors import phpipam_message
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.operations import PassThroughReducer, set_default_reducer
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.redaction.call_secrets import (
    CALL_SECRET_MARKER,
    CallSecrets,
    bind_call_secrets,
    collect_param_secret_values,
    marked_secret_paths,
    reset_call_secrets,
)
from tests._call_secret_dispatch import (
    WRITE_OP,
    http_error,
    operator,
    required_settings_env,
    reset_module_state,
    run_dispatch,
)
from tests._result_store_helpers import MemoryResultStore

__all__ = ["operator", "required_settings_env", "reset_module_state"]

_SECRET = "wombat-" + "4410-river-key"
_OTHER = "pelican-" + "2207-stone-word"
# A rule of the default policy replaces the part from ``token=`` on.
_WITH_TOKEN = "wombat-4410" + "?token=river-key-7781"


def _everything(result: Any, row: Any) -> str:
    return (
        result.model_dump_json()
        + json.dumps(row.payload, default=str)
        + json.dumps(row.raw_payload, default=str)
        + json.dumps(row.redaction_manifest, default=str)
    )


# ---------------------------------------------------------------------------
# A rule that replaces part of a value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fails", [False, True], ids=["answer", "error"])
async def test_a_value_cut_by_another_rule_leaves_no_part(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    body = {"note": f"saved {_WITH_TOKEN} for you", "name": "PASSWORD"}
    result, row = await run_dispatch(
        monkeypatch,
        op_id=WRITE_OP,
        behave=(lambda: http_error(400, body)) if fails else (lambda: body),
        params={"password": _WITH_TOKEN},
    )
    assert result.status == ("error" if fails else "ok")
    shown = _everything(result, row)
    assert "wombat-4410" not in shown
    assert "river-key-7781" not in shown
    if not fails:
        assert result.result == {"note": f"saved {CALL_SECRET_MARKER} for you", "name": "PASSWORD"}


def test_a_cut_text_keeps_what_the_rule_hid_and_masks_the_value() -> None:
    scope = CallSecrets([_WITH_TOKEN])
    original = {"a": f"x {_WITH_TOKEN} y", "b": "keep [this]", "c": [_WITH_TOKEN]}
    redacted = {"a": "x wombat-4410?token=[R] y", "b": "keep [R]", "c": ["wombat-4410?token=[R]"]}
    out = scope.keep_whole(original, redacted)
    assert out == {"a": f"x {CALL_SECRET_MARKER} y", "b": "keep [R]", "c": [CALL_SECRET_MARKER]}


def test_a_value_inside_what_a_rule_hid_reveals_nothing_else() -> None:
    # The value of the call is the word ``Bearer``; the rule hid the whole token.
    scope = CallSecrets(["Bearer"])
    original = "send Bearer tok-5531-otter-9920 now"
    out = scope.keep_whole(original, "send [REDACTED:bearer] now")
    # The label holds a piece of the value ("earer"), so the marker is used.
    assert out == f"send {CALL_SECRET_MARKER} now"


def test_a_rule_that_hid_the_value_whole_keeps_its_label() -> None:
    scope = CallSecrets([_SECRET])
    out = scope.keep_whole(f"pw {_SECRET} end", "pw [REDACTED:param_name] end")
    assert out == "pw [REDACTED:param_name] end"


def test_a_value_partly_in_a_change_becomes_one_marker() -> None:
    scope = CallSecrets(["Bearer tok-5531"])
    original = "send Bearer tok-5531 otter-9920 now"
    out = scope.keep_whole(original, "send Bearer [R] otter-9920 now")
    assert out == f"send {CALL_SECRET_MARKER} otter-9920 now"


def test_a_rule_change_beside_the_value_stays() -> None:
    scope = CallSecrets([_SECRET])
    original = f"{_SECRET} and Bearer abc-7781-def"
    out = scope.keep_whole(original, f"{_SECRET} and [R]")
    assert out == f"{CALL_SECRET_MARKER} and [R]"


def test_no_change_keeps_the_same_answer() -> None:
    scope = CallSecrets([_SECRET])
    redacted = {"a": f"{_SECRET} [R]", "b": "same"}
    assert scope.keep_whole({"a": f"{_SECRET} [R]", "b": "same"}, redacted) is redacted


async def test_a_value_like_a_word_of_a_token_hides_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {"note": "use Bearer " + "tok5531otter9920river for it"}
    result, row = await run_dispatch(
        monkeypatch, op_id=WRITE_OP, behave=lambda: body, params={"password": "Bearer"}
    )
    assert result.status == "ok"
    assert result.result == {"note": f"use {CALL_SECRET_MARKER} for it"}
    assert "tok5531otter9920river" not in result.model_dump_json() + json.dumps(row.payload)


async def test_a_value_with_a_quoted_password_field_leaves_no_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = 'koala-6620 "password": "inner-word-4471" tail-3308'
    body = {"note": f"saved {value} for you"}
    result, row = await run_dispatch(
        monkeypatch, op_id=WRITE_OP, behave=lambda: body, params={"password": value}
    )
    assert result.status == "ok"
    shown = _everything(result, row)
    for part in ("koala-6620", "inner-word-4471", "tail-3308"):
        assert part not in shown
    assert result.result == {"note": f"saved {CALL_SECRET_MARKER} for you"}


# ---------------------------------------------------------------------------
# A value used as a key, in the manifest
# ---------------------------------------------------------------------------


async def test_a_value_as_a_key_is_masked_in_the_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = {"accounts": {_SECRET: f"saved ?token=river-key-7781 for {_OTHER}"}}
    result, row = await run_dispatch(
        monkeypatch, op_id=WRITE_OP, behave=lambda: body, params={"password": _SECRET}
    )
    assert result.status == "ok"
    paths = [entry["path"] for entry in row.redaction_manifest]
    assert f"accounts.{CALL_SECRET_MARKER}" in paths
    assert CALL_SECRET_MARKER in result.result["accounts"]
    assert _SECRET not in _everything(result, row)


@pytest.mark.parametrize("secret_first", [True, False], ids=["secret-first", "mark-first"])
async def test_a_masked_key_with_a_number_has_the_same_path_in_the_manifest(
    monkeypatch: pytest.MonkeyPatch, secret_first: bool
) -> None:
    items = [
        (_SECRET, "secret ?token=river-key-7781 entry"),
        (CALL_SECRET_MARKER, "marker ?token=lake-key-5519 entry"),
    ]
    body = {"accounts": dict(items if secret_first else reversed(items))}
    result, row = await run_dispatch(
        monkeypatch, op_id=WRITE_OP, behave=lambda: body, params={"password": _SECRET}
    )
    assert result.status == "ok"
    # The masked key gets the counter 2, after a hash sign.
    numbered = CALL_SECRET_MARKER + "#" + str(2)
    names = {CALL_SECRET_MARKER, numbered}
    assert set(row.raw_payload["accounts"]) == names
    shown = result.result["accounts"]
    assert set(shown) == names
    assert shown[numbered].startswith("secret")
    assert shown[CALL_SECRET_MARKER].startswith("marker")
    # Each manifest path names the entry the rule changed, as stored.
    paths = sorted(entry["path"] for entry in row.redaction_manifest)
    assert paths == [f"accounts.{CALL_SECRET_MARKER}", f"accounts.{numbered}"]
    assert _SECRET not in _everything(result, row)


# ---------------------------------------------------------------------------
# MEHO's own field names
# ---------------------------------------------------------------------------


async def _failing_steps(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    return {
        "status": "made_up_failed",
        "issues": [
            {"severity": "warning", "message": "first a warning"},
            {"severity": "error", "message": "the step broke"},
        ],
    }


async def test_a_value_like_a_field_name_keeps_the_code_and_message(
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
    assert result.extras["error_code"] == "made_up_failed"
    assert result.error == "made_up_failed: the step broke"
    assert row.payload["result_status"] == "error"
    assert row.payload["error"]["error_code"] == "made_up_failed"


async def test_a_value_like_the_message_name_keeps_the_failure_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, row = await run_dispatch(
        monkeypatch,
        op_id="echodemo.steps.fail",
        behave=lambda: None,
        params={"password": "message"},
        source_kind="composite",
        handler_ref=f"{__name__}._failing_steps",
    )
    assert result.status == "error"
    assert result.error == "made_up_failed: the step broke"
    assert row.payload["error"]["error_code"] == "made_up_failed"


async def _many_failing_steps(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    issues = [{"severity": "warning", "message": f"warning {n}"} for n in range(120)]
    issues[100] = {"severity": "error", "message": "the step broke"}
    return {"status": "made_up_failed", "issues": issues}


async def test_a_large_failure_keeps_its_error_inline_when_a_value_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_default_reducer(JsonFluxReducer(store=MemoryResultStore()))
    try:
        result, _row = await run_dispatch(
            monkeypatch,
            op_id="echodemo.steps.fail",
            behave=lambda: None,
            params={"password": "error"},
            source_kind="composite",
            handler_ref=f"{__name__}._many_failing_steps",
        )
    finally:
        set_default_reducer(PassThroughReducer())
    assert result.status == "error"
    assert result.handle is not None
    messages = [item["message"] for item in result.result["issues"]]
    assert "the step broke" in messages
    assert result.error == "made_up_failed: the step broke"


async def _returns_own_error(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target
    secret = params["password"]
    return OperationResult(
        status="error",
        op_id="echodemo.own.fail",
        error=f"refused {secret}",
        duration_ms=0.0,
        extras={"error_code": f"code_{secret}", secret: "vendor detail", "remediation": "x"},
    )


async def test_a_returned_error_hides_the_value_in_its_names_and_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, row = await run_dispatch(
        monkeypatch,
        op_id="echodemo.own.fail",
        behave=lambda: None,
        params={"password": _SECRET},
        source_kind="composite",
        handler_ref=f"{__name__}._returns_own_error",
    )
    assert result.status == "error"
    assert result.extras["error_code"] == f"code_{CALL_SECRET_MARKER}"
    assert result.extras["remediation"] == "x"
    assert row.payload["error"]["error_code"] == f"code_{CALL_SECRET_MARKER}"
    assert _SECRET not in _everything(result, row)


async def _returns_error_named_like_a_value(
    operator: Operator, target: Any, params: dict[str, Any]
) -> Any:
    del operator, target, params
    return OperationResult(
        status="error",
        op_id="echodemo.own.fail",
        error="refused",
        duration_ms=0.0,
        extras={"error_code": "made_up_refused"},
    )


async def test_a_returned_error_keeps_meho_names_like_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, row = await run_dispatch(
        monkeypatch,
        op_id="echodemo.own.fail",
        behave=lambda: None,
        params={"password": "error_code"},
        source_kind="composite",
        handler_ref=f"{__name__}._returns_error_named_like_a_value",
    )
    assert result.extras["error_code"] == "made_up_refused"
    assert row.payload["error"]["error_code"] == "made_up_refused"


# ---------------------------------------------------------------------------
# Schema marks
# ---------------------------------------------------------------------------

_MARKED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "login": {"$ref": "#/$defs/Password"},
        "keys": {"type": "array", "items": {"type": "string", "format": "password"}},
        "label": {"type": "string"},
    },
    "additionalProperties": {"type": "string", "writeOnly": True},
    "$defs": {"Password": {"type": "string", "format": "password"}},
}


def test_a_mark_through_a_ref_items_or_extra_keys_is_found() -> None:
    marked = marked_secret_paths(_MARKED_SCHEMA)
    params = {
        "login": _SECRET,
        "keys": [_OTHER],
        "label": "public-name-text",
        "extra": "extra-secret-9921",
    }
    assert set(collect_param_secret_values(params, marked_paths=marked)) == {
        _SECRET,
        _OTHER,
        "extra-secret-9921",
    }


async def test_marked_values_are_hidden_and_a_declared_label_stays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = f"bad {_SECRET} and {_OTHER} and extra-secret-9921 for public-name-text"
    result, row = await run_dispatch(
        monkeypatch,
        op_id=WRITE_OP,
        behave=lambda: http_error(400, {"message": text}),
        params={
            "login": _SECRET,
            "keys": [_OTHER],
            "label": "public-name-text",
            "extra": "extra-secret-9921",
        },
        schema=_MARKED_SCHEMA,
    )
    assert result.status == "error"
    shown = _everything(result, row)
    for value in (_SECRET, _OTHER, "extra-secret-9921"):
        assert value not in shown
    assert "public-name-text" in result.model_dump_json()


def test_a_mark_on_a_name_pattern_is_found() -> None:
    schema = {"type": "object", "patternProperties": {"^pw_": {"format": "password"}}}
    params = {"pw_main": _SECRET, "user": "public-user-name"}
    assert set(collect_param_secret_values(params, marked_paths=marked_secret_paths(schema))) == {
        _SECRET
    }


def test_a_field_of_a_public_name_pattern_is_not_an_extra_key() -> None:
    schema = {
        "type": "object",
        "patternProperties": {"^note_": {"type": "string"}},
        "additionalProperties": {"type": "string", "writeOnly": True},
    }
    params = {"note_a": "public-note-text", "other": "extra-secret-9921"}
    assert set(collect_param_secret_values(params, marked_paths=marked_secret_paths(schema))) == {
        "extra-secret-9921"
    }


_PAIR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pair": {
            "type": "array",
            "prefixItems": [{"type": "string"}, {"type": "string", "format": "password"}],
        }
    },
}


def test_a_mark_on_one_array_position_hides_only_that_position() -> None:
    params = {"pair": ["public-label-text", _SECRET]}
    marked = marked_secret_paths(_PAIR_SCHEMA)
    assert set(collect_param_secret_values(params, marked_paths=marked)) == {_SECRET}


async def test_the_label_next_to_a_marked_position_stays_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = f"bad {_SECRET} for public-label-text"
    result, row = await run_dispatch(
        monkeypatch,
        op_id=WRITE_OP,
        behave=lambda: http_error(400, {"message": text}),
        params={"pair": ["public-label-text", _SECRET]},
        schema=_PAIR_SCHEMA,
    )
    assert result.status == "error"
    assert _SECRET not in _everything(result, row)
    assert "public-label-text" in result.model_dump_json()


# ---------------------------------------------------------------------------
# Connectors that cut a vendor text themselves
# ---------------------------------------------------------------------------


def _with_secret(text_maker: Any) -> Any:
    token = bind_call_secrets([_SECRET])
    try:
        return text_maker()
    finally:
        reset_call_secrets(token)


def test_a_phpipam_message_across_the_cut_leaves_no_part() -> None:
    message = "x" * (_PHPIPAM_CAP - 5) + _SECRET
    response = httpx.Response(400, json={"message": message})
    out = _with_secret(lambda: phpipam_message(response))
    assert out is not None
    assert _SECRET[:5] not in out


#: A made-up value of the call that ends in a space.
_ENDS_IN_SPACE = "wombat-4410" + "-river "


def test_a_phpipam_value_that_ends_in_a_space_is_hidden() -> None:
    token = bind_call_secrets([_ENDS_IN_SPACE])
    try:
        out = phpipam_message(httpx.Response(400, json={"message": f"bad {_ENDS_IN_SPACE}"}))
    finally:
        reset_call_secrets(token)
    assert out == f"bad {CALL_SECRET_MARKER}"


async def test_a_plain_vendor_text_that_ends_with_a_value_and_a_space_hides_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, row = await run_dispatch(
        monkeypatch,
        op_id=WRITE_OP,
        behave=lambda: http_error(400, f"bad {_ENDS_IN_SPACE}"),
        params={"password": _ENDS_IN_SPACE},
    )
    assert result.status == "error"
    assert "wombat-4410-river" not in _everything(result, row)


def test_an_azure_text_across_the_cut_leaves_no_part() -> None:
    out = _with_secret(lambda: clean_azure_text("x" * (_AZURE_CAP - 5) + _SECRET))
    assert out is not None
    assert _SECRET[:5] not in out


# ---------------------------------------------------------------------------
# The matcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["dir/with space", 'a/b "c" é'], ids=["slash", "all"])
def test_a_value_encoded_twice_with_escaped_slashes_is_hidden(value: str) -> None:
    once = json.dumps(value)[1:-1].replace("/", "\\/")
    twice = json.dumps(once)[1:-1].replace("/", "\\/")
    assert CallSecrets([value]).scrub(f"<{twice}>") == f"<{CALL_SECRET_MARKER}>"


@pytest.mark.parametrize("secret_first", [True, False], ids=["secret-first", "mark-first"])
def test_a_masked_key_never_replaces_another_entry(secret_first: bool) -> None:
    items = [(_SECRET, "a"), (CALL_SECRET_MARKER, "b")]
    node = dict(items if secret_first else reversed(items))
    out = CallSecrets([_SECRET]).scrub_tree(node)
    assert sorted(out.values()) == ["a", "b"]
    assert out[CALL_SECRET_MARKER] == "b"
    assert _SECRET not in json.dumps(out)


def test_kept_keys_stay_as_they_are() -> None:
    out = CallSecrets(["error"]).scrub_tree(
        {"error_code": "x", "nested": {"error_code": "y"}}, keep_keys=True
    )
    assert out == {"error_code": "x", "nested": {f"{CALL_SECRET_MARKER}_code": "y"}}
