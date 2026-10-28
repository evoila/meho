"""Unit tests for the secret-named field scrub in ``redaction.secret_fields``."""

from __future__ import annotations

import time

import pytest

from meho_backplane.redaction.secret_fields import SECRET_FIELD_MARKER, scrub_secret_fields


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
