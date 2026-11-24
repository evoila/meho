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


def test_only_the_cut_text_is_redacted_again() -> None:
    scope = CallSecrets([_WITH_TOKEN])
    original = {"a": f"x {_WITH_TOKEN}", "b": "keep [this]", "c": [f"{_WITH_TOKEN}"]}
    redacted = {"a": "x wombat-4410?[R]", "b": "keep [R]", "c": ["wombat-4410?[R]"]}
    seen: list[Any] = []

    def redo(node: Any) -> Any:
        seen.append(node)
        return {key: f"again:{value}" for key, value in node.items()}

    out = scope.keep_whole(original, redacted, redo)
    assert seen == [{"a": f"x {CALL_SECRET_MARKER}", "b": "keep [this]", "c": [CALL_SECRET_MARKER]}]
    assert out["a"] == f"again:x {CALL_SECRET_MARKER}"
    assert out["b"] == "keep [R]"
    assert out["c"] == ["wombat-4410?[R]"]  # the redo kept no list, so the walk keeps the text


def test_no_cut_means_no_second_pass() -> None:
    scope = CallSecrets([_SECRET])
    original = {"a": f"{_SECRET} Bearer abc", "b": "same"}
    redacted = {"a": f"{_SECRET} [R]", "b": "same"}

    def redo(node: Any) -> Any:
        raise AssertionError("no second pass")

    assert scope.keep_whole(original, redacted, redo) is redacted


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
