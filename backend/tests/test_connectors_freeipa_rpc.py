# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the FreeIPA wire helpers: request body, answer, value decoding, ``ping``.

Pure functions of :mod:`meho_backplane.connectors.freeipa.rpc`; no server.
"""

from __future__ import annotations

from typing import Any

import pytest

from meho_backplane.connectors.freeipa.rpc import (
    API_VERSION,
    FreeipaCommandError,
    build_request,
    decode_values,
    parse_ping_summary,
    unwrap_answer,
)
from tests._freeipa_fixtures import PRINCIPAL


def test_build_request_adds_the_api_version_and_drops_empty_options() -> None:
    assert build_request("host_find", ["web"], {"sizelimit": 5, "in_hostgroup": None}) == {
        "method": "host_find",
        "params": [["web"], {"sizelimit": 5, "version": API_VERSION}],
        "id": 0,
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "not a JSON object"),
        ({"error": None}, "has no result"),
        ({"error": "plain text"}, "Error: plain text"),
        (
            {"result": None, "error": {"code": 4001, "name": "NotFound", "message": "gone"}},
            "NotFound 4001: gone",
        ),
    ],
)
def test_unwrap_answer_errors(payload: Any, message: str) -> None:
    with pytest.raises(FreeipaCommandError, match=message):
        unwrap_answer(payload, "user_show")


def test_unwrap_answer_returns_result_principal_and_version() -> None:
    answer = unwrap_answer(
        {"result": {"value": "x"}, "error": None, "principal": PRINCIPAL, "version": "4.13.4"},
        "user_show",
    )
    assert (answer.result, answer.principal, answer.server_version) == (
        {"value": "x"},
        PRINCIPAL,
        "4.13.4",
    )


def test_decode_values_turns_tagged_values_into_text() -> None:
    assert decode_values(
        {
            "when": [{"__datetime__": "20261029120000Z"}],
            "zone": {"__dns_name__": "example.test."},
            "blob": {"__base64__": "AAE="},
            "bad": {"__datetime__": "not-a-date"},
            "plain": ["a", 1, True, None],
        }
    ) == {
        "when": ["2026-10-29T12:00:00Z"],
        "zone": "example.test.",
        "blob": {"__base64__": "AAE="},
        "bad": "not-a-date",
        "plain": ["a", 1, True, None],
    }


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("IPA server version 4.13.4. API version 2.257", ("4.13.4", "2.257")),
        ("IPA server version 4.12.2-1.el9. API version 2.254", ("4.12.2-1.el9", "2.254")),
        ("something else", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_ping_summary(summary: Any, expected: tuple[str | None, str | None]) -> None:
    assert parse_ping_summary(summary) == expected
