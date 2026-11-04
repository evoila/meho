"""Unit tests for the secret-named field scrub in ``redaction.secret_fields``."""

from __future__ import annotations

import json
import time

import pytest

from meho_backplane.redaction.secret_fields import (
    SECRET_FIELD_MARKER,
    iter_quoted_secret_fields,
    scrub_secret_fields,
    scrub_secret_fields_in_text,
)


@pytest.mark.parametrize(
    "value",
    [
        "**",
        "***",
        "***REDACTED***",
        "*REDACTED*",
        "[REDACTED:param_name]",
        "[MEHO-REDACTED:body-path]",
        "[MEHO-OMITTED:redaction-uncertain]",
        "[redacted]",
    ],
)
def test_already_masked_value_stays(value: str) -> None:
    assert scrub_secret_fields({"password": value}) == {"password": value}


@pytest.mark.parametrize(
    "value", ["hunter2", "*", "*x", "***REDACTED", "REDACTED", "[REDACTED:a[b]"]
)
def test_plain_value_is_replaced(value: str) -> None:
    assert scrub_secret_fields({"password": value}) == {"password": SECRET_FIELD_MARKER}


@pytest.mark.parametrize(
    "value", ["*" * 50_000 + "x", "*" * 50_000 + "REDACTED", "[OMITTED:" * 10_000]
)
def test_long_value_is_replaced_quickly(value: str) -> None:
    started = time.monotonic()
    assert scrub_secret_fields({"password": value}) == {"password": SECRET_FIELD_MARKER}
    assert time.monotonic() - started < 1.0


# ---------------------------------------------------------------------------
# Quoted pairs in text: every quote form, quotes inside the value, no length cap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"password": "hunter2"}', f'{{"password": "{SECRET_FIELD_MARKER}"}}'),
        ("{'password': 'hunter2'}", f"{{'password': '{SECRET_FIELD_MARKER}'}}"),
        # Python repr of a value that holds a single quote: a double-quoted value.
        ("{'password': \"it's\"}", f"{{'password': \"{SECRET_FIELD_MARKER}\"}}"),
        ("{\"password\": 'hunter2'}", f"{{\"password\": '{SECRET_FIELD_MARKER}'}}"),
        (
            "{'note': \"it's\", 'password': 'hunter2'}",
            f"{{'note': \"it's\", 'password': '{SECRET_FIELD_MARKER}'}}",
        ),
        # An escaped quote inside the value does not end it.
        ('{"password": "ab\\"cd"}', f'{{"password": "{SECRET_FIELD_MARKER}"}}'),
        ("{'password': 'ab\\'cd'}", f"{{'password': '{SECRET_FIELD_MARKER}'}}"),
        ('"password":"p"', f'"password":"{SECRET_FIELD_MARKER}"'),
        # Not a secret name, a number, a flag: unchanged.
        ('{"user": "u", "bind_secret_id": true, "ttl": 3}', None),
        # An empty value: nothing to hide.
        ('{"password": ""}', None),
    ],
)
def test_quoted_pair_in_text(text: str, expected: str | None) -> None:
    assert scrub_secret_fields_in_text(text) == (text if expected is None else expected)


@pytest.mark.parametrize(
    "value",
    ['ab"cd', "ab\\cd", "ab\\", "a\nb", 'tail"after', "plain"],
)
def test_escaped_json_value_with_quotes_inside_is_replaced_whole(value: str) -> None:
    """Escaped JSON inside a JSON string: the whole value goes, not only its head."""
    text = json.dumps({"detail": json.dumps({"password": value, "next": "kept"})})
    scrubbed = scrub_secret_fields_in_text(text)
    inner = json.loads(json.loads(scrubbed)["detail"])
    assert inner == {"password": SECRET_FIELD_MARKER, "next": "kept"}


@pytest.mark.parametrize("length", [8, 4_000, 4_096, 4_097, 5_000, 100_000])
@pytest.mark.parametrize(
    "shape",
    ["json", "python", "escaped"],
)
def test_value_of_any_length_is_replaced(length: int, shape: str) -> None:
    """A long value is replaced like a short one: there is no length limit."""
    value = "v" * length
    if shape == "json":
        text = json.dumps({"encryptedPassword": value, "next": "kept"})
    elif shape == "python":
        text = repr({"encryptedPassword": value, "next": "kept"})
    else:
        text = json.dumps({"body": json.dumps({"encryptedPassword": value, "next": "kept"})})
    scrubbed = scrub_secret_fields_in_text(text)
    assert "vvvv" not in scrubbed
    assert SECRET_FIELD_MARKER in scrubbed
    assert "kept" in scrubbed


def test_value_with_no_closing_quote_is_replaced_to_the_end() -> None:
    """A diagnostic cut by a length cap can end inside the value."""
    text = '{"message": "denied", "password": "hunter2-and-the-cap-cut-here'
    assert scrub_secret_fields_in_text(text) == (
        f'{{"message": "denied", "password": "{SECRET_FIELD_MARKER}'
    )


def test_long_text_is_scrubbed_quickly() -> None:
    text = '{"password": "' + "y" * 2_000_000 + '"}'
    started = time.monotonic()
    scrubbed = scrub_secret_fields_in_text(text)
    assert time.monotonic() - started < 2.0
    assert scrubbed == f'{{"password": "{SECRET_FIELD_MARKER}"}}'


# ---------------------------------------------------------------------------
# An object or a list under a secret name in text: replaced whole
# ---------------------------------------------------------------------------

_QUOTED_MARKER = f'"{SECRET_FIELD_MARKER}"'


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '{"credentials": {"value": "hunter2"}, "next": "kept"}',
            f'{{"credentials": {_QUOTED_MARKER}, "next": "kept"}}',
        ),
        (
            '{"password": ["one", "two"], "next": "kept"}',
            f'{{"password": {_QUOTED_MARKER}, "next": "kept"}}',
        ),
        # A bracket inside a string does not end the object.
        (
            '{"credentials": {"value": "x}y", "list": [1, {"a": "]"}]}, "next": "kept"}',
            f'{{"credentials": {_QUOTED_MARKER}, "next": "kept"}}',
        ),
        (
            "{'credentials': {'value': \"it's\"}, 'next': 'kept'}",
            f"{{'credentials': {_QUOTED_MARKER}, 'next': 'kept'}}",
        ),
        (
            '{"credentials": {"nested": {"password": "hunter2"}}}',
            f'{{"credentials": {_QUOTED_MARKER}}}',
        ),
        # An empty object or list: nothing to hide.
        ('{"credentials": {}, "password": [ ], "flag": true}', None),
    ],
)
def test_object_or_list_under_a_secret_name_is_replaced_whole(
    text: str, expected: str | None
) -> None:
    scrubbed = scrub_secret_fields_in_text(text)
    assert scrubbed == (text if expected is None else expected)
    assert scrub_secret_fields_in_text(scrubbed) == scrubbed


def test_escaped_json_object_under_a_secret_name_is_replaced_whole() -> None:
    inner = {"credentials": {"value": 'x}y"z', "list": [1, {"a": "]"}]}, "next": "kept"}
    text = json.dumps({"detail": json.dumps(inner)})
    scrubbed = scrub_secret_fields_in_text(text)
    assert "hunter" not in scrubbed and "x}y" not in scrubbed
    assert json.loads(json.loads(scrubbed)["detail"]) == {
        "credentials": SECRET_FIELD_MARKER,
        "next": "kept",
    }


def test_object_with_no_closing_bracket_is_replaced_to_the_end() -> None:
    text = '{"message": "denied", "credentials": {"value": "hunter2-and-the-cap-cut-here'
    assert scrub_secret_fields_in_text(text) == (
        f'{{"message": "denied", "credentials": {_QUOTED_MARKER}'
    )


def test_large_object_under_a_secret_name_is_replaced_quickly() -> None:
    text = json.dumps({"credentials": {"value": "v" * 1_000_000, "list": ["w"] * 10_000}})
    started = time.monotonic()
    assert scrub_secret_fields_in_text(text) == f'{{"credentials": {_QUOTED_MARKER}}}'
    assert time.monotonic() - started < 2.0


def test_iter_quoted_secret_fields_lists_object_values() -> None:
    text = '{"credentials": {"value": "p"}, "password": "x", "detail": "{\\"secret\\": [1]}"}'
    fields = list(iter_quoted_secret_fields(text))
    assert [(f.name, f.value, f.bracketed, f.escaped) for f in fields] == [
        ("credentials", '{"value": "p"}', True, False),
        ("password", "x", False, False),
        ("secret", "[1]", True, True),
    ]
    assert all(text[field.start : field.end] == field.value for field in fields)


def test_iter_quoted_secret_fields_lists_secret_names_only() -> None:
    text = '{"user": "u", "password": "p1", "detail": "{\\"sessionToken\\": \\"t1\\"}"}'
    fields = list(iter_quoted_secret_fields(text))
    assert [(field.name, field.value) for field in fields] == [
        ("password", "p1"),
        ("sessionToken", "t1"),
    ]
    assert all(text[field.start : field.end] == field.value for field in fields)


def test_value_of_a_secret_named_pair_is_replaced() -> None:
    node = {
        "fields": [
            {"name": "USER", "value": "root"},
            {"name": "PASSWORD", "value": "hunter2"},
            {"name": "PASSWORD"},
        ],
        "name": "my-credential",
    }
    assert scrub_secret_fields(node) == {
        "fields": [
            {"name": "USER", "value": "root"},
            {"name": "PASSWORD", "value": SECRET_FIELD_MARKER},
            {"name": "PASSWORD"},
        ],
        "name": "my-credential",
    }


# ---------------------------------------------------------------------------
# A name/value pair object in text: the value of a secret-named pair is replaced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '{"name":"PASSWORD","value":"hunter2"}',
            f'{{"name":"PASSWORD","value":"{SECRET_FIELD_MARKER}"}}',
        ),
        (
            '[{"name": "USER", "value": "root"}, {"name": "PASSWORD", "value": "hunter2"}]',
            '[{"name": "USER", "value": "root"}, '
            f'{{"name": "PASSWORD", "value": "{SECRET_FIELD_MARKER}"}}]',
        ),
        # Other members between the two.
        (
            '{"name": "PASSWORD", "type": "string", "value": "hunter2"}',
            f'{{"name": "PASSWORD", "type": "string", "value": "{SECRET_FIELD_MARKER}"}}',
        ),
        # The value before the name.
        (
            '{"value": "hunter2", "name": "PASSWORD"}',
            f'{{"value": "{SECRET_FIELD_MARKER}", "name": "PASSWORD"}}',
        ),
        # Python repr and escaped JSON inside a JSON string.
        (
            "{'name': 'PASSWORD', 'value': 'hunter2'}",
            f"{{'name': 'PASSWORD', 'value': '{SECRET_FIELD_MARKER}'}}",
        ),
        (
            '{"detail": "{\\"name\\": \\"PASSWORD\\", \\"value\\": \\"hunter2\\"}"}',
            '{"detail": "{\\"name\\": \\"PASSWORD\\", '
            f'\\"value\\": \\"{SECRET_FIELD_MARKER}\\"}}"}}',
        ),
        # An object as the value: replaced whole.
        (
            '{"name": "credentials", "value": {"user": "u", "pw": "x"}}',
            f'{{"name": "credentials", "value": {_QUOTED_MARKER}}}',
        ),
        # A value with no closing quote: replaced to the end.
        (
            '{"name": "PASSWORD", "value": "hunter2',
            f'{{"name": "PASSWORD", "value": "{SECRET_FIELD_MARKER}',
        ),
        # Not a secret pair: a plain name, no value member, a nested value member,
        # a number, an empty value, an already masked value.
        ('{"name": "USER", "value": "root"}', None),
        ('{"value": "hunter2", "name": "USER"}', None),
        ('{"name": "PASSWORD"}', None),
        ('{"name": "PASSWORD", "other": {"value": "x"}}', None),
        ('{"name": "PASSWORD", "value": 3}', None),
        ('{"name": "PASSWORD", "value": ""}', None),
        ('{"name": "PASSWORD", "value": "[REDACTED:param_name]"}', None),
        # The pair of an inner object does not reach the members of the outer one.
        (
            '{"items": [{"name": "PASSWORD", "value": "p"}], "name": "USER", "value": "root"}',
            f'{{"items": [{{"name": "PASSWORD", "value": "{SECRET_FIELD_MARKER}"}}], '
            '"name": "USER", "value": "root"}',
        ),
    ],
)
def test_value_of_a_secret_named_pair_in_text(text: str, expected: str | None) -> None:
    assert scrub_secret_fields_in_text(text) == (text if expected is None else expected)


def test_secret_named_key_between_the_name_and_the_value_of_a_pair_is_replaced_too() -> None:
    text = '{"name": "PASSWORD", "token": "t1", "value": "hunter2"}'
    assert [(field.name, field.value) for field in iter_quoted_secret_fields(text)] == [
        ("token", "t1"),
        ("PASSWORD", "hunter2"),
    ]
    assert scrub_secret_fields_in_text(text) == (
        f'{{"name": "PASSWORD", "token": "{SECRET_FIELD_MARKER}", '
        f'"value": "{SECRET_FIELD_MARKER}"}}'
    )


def test_pair_value_found_from_both_members_is_listed_once() -> None:
    text = '{"name": "PASSWORD", "value": "hunter2", "name": "TOKEN"}'
    assert [(field.name, field.value) for field in iter_quoted_secret_fields(text)] == [
        ("PASSWORD", "hunter2"),
    ]


def test_many_pairs_in_text_are_scrubbed_quickly() -> None:
    pairs = [{"name": "PASSWORD", "value": "p" * 20}, {"name": "USER", "value": "root"}] * 25_000
    text = json.dumps(pairs)
    started = time.monotonic()
    scrubbed = scrub_secret_fields_in_text(text)
    assert time.monotonic() - started < 2.0
    assert "p" * 20 not in scrubbed
    assert scrubbed.count(SECRET_FIELD_MARKER) == 25_000
    assert scrubbed.count('"value": "root"') == 25_000


def test_many_value_members_before_the_name_are_scrubbed_quickly() -> None:
    members = ", ".join(f'"value": "v{index}"' for index in range(50_000))
    text = "{" + members + ', "name": "PASSWORD"}'
    started = time.monotonic()
    scrubbed = scrub_secret_fields_in_text(text)
    assert time.monotonic() - started < 2.0
    assert scrubbed.count(SECRET_FIELD_MARKER) == 1
    assert scrubbed.endswith(f'"value": "{SECRET_FIELD_MARKER}", "name": "PASSWORD"}}')
