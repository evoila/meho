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


def test_iter_quoted_secret_fields_lists_secret_names_only() -> None:
    text = '{"user": "u", "password": "p1", "detail": "{\\"sessionToken\\": \\"t1\\"}"}'
    fields = list(iter_quoted_secret_fields(text))
    assert [(field.name, field.value) for field in fields] == [
        ("password", "p1"),
        ("sessionToken", "t1"),
    ]
    assert all(text[field.start : field.end] == field.value for field in fields)
