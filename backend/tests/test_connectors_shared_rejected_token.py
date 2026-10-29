# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for :func:`drop_rejected_token`: drop a shared token only when the failed call sent it."""

from __future__ import annotations

import httpx

from meho_backplane.connectors._shared.rejected_token import drop_rejected_token

_KEY = ("tenant", "secret/ref", "appliance.test.invalid", "", "")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _request(headers: dict[str, str]) -> httpx.Request:
    return httpx.Request("GET", "https://appliance.test.invalid/api", headers=headers)


def test_token_the_failed_call_sent_is_dropped() -> None:
    cache = {_KEY: "token-1"}

    dropped = drop_rejected_token(cache, _KEY, _request(_bearer("token-1")), _bearer)

    assert dropped is True
    assert cache == {}


def test_newer_token_stays_when_a_late_401_arrives() -> None:
    cache = {_KEY: "token-2"}

    dropped = drop_rejected_token(cache, _KEY, _request(_bearer("token-1")), _bearer)

    assert dropped is False
    assert cache == {_KEY: "token-2"}


def test_without_a_failed_request_the_token_is_dropped() -> None:
    cache = {_KEY: "token-1"}

    assert drop_rejected_token(cache, _KEY, None, _bearer) is True
    assert cache == {}


def test_nothing_cached_drops_nothing() -> None:
    cache: dict[tuple[str, ...], str] = {}

    assert drop_rejected_token(cache, _KEY, _request(_bearer("token-1")), _bearer) is False
    assert cache == {}


def test_every_session_header_must_match() -> None:
    """A session sent in several headers (Basic, cookie) is dropped only when all match."""

    def _headers(session: tuple[str, str]) -> dict[str, str]:
        return {"Authorization": session[0], "Cookie": session[1]}

    cache = {_KEY: ("Basic same", "session=new")}
    late = _request({"Authorization": "Basic same", "Cookie": "session=old"})

    assert drop_rejected_token(cache, _KEY, late, _headers) is False
    assert cache == {_KEY: ("Basic same", "session=new")}

    current = _request({"Authorization": "Basic same", "Cookie": "session=new"})
    assert drop_rejected_token(cache, _KEY, current, _headers) is True
    assert cache == {}
